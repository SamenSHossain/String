"""Turn response bytes into text the way a browser would, with a statistical fallback.

Order: byte-order mark, then the HTTP Content-Type charset, then a <meta> prescan of the
first bytes, then strict UTF-8. A declared charset with only a few bad bytes is then kept,
each bad sequence shown as U+FFFD (browsers never drop a declared charset over bad bytes);
pervasive errors mean the declaration is wrong, and charset-normalizer decides. It also
decides where a legacy multi-byte declaration with few errors reads as mojibake next to its guess.

Labels follow the WHATWG Encoding Standard's table (Shift_JIS -> cp932, GB2312/GBK ->
GB18030, latin1/ascii -> windows-1252, ...). The decoders follow its indexes and error
rules: Python's codecs plus the corrections below, checked against the web-platform-tests
corpora for EUC-JP, Big5, Shift_JIS and EUC-KR and against every single-byte index.
"""

from __future__ import annotations

import codecs
import functools
import re
import threading
from _multibytecodec import MultibyteIncrementalDecoder

from charset_normalizer import from_bytes
from charset_normalizer.md import mess_ratio

_META_RE = re.compile(rb"""<meta[^>]{0,512}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]{1,40})""", re.I)
_CT_RE = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]{1,40})""", re.I)

# The WHATWG label table (encoding.spec.whatwg.org/encodings.json): the Python codec we decode
# with, the encoding's name, its labels. The first row for a codec gives its name.
_ENCODINGS = (
    ("utf-8", "UTF-8", "unicode-1-1-utf-8 unicode11utf8 unicode20utf8 utf-8 utf8 x-unicode20utf8"),
    ("cp866", "IBM866", "866 cp866 csibm866 ibm866"),
    ("iso8859-2", "ISO-8859-2",
     "csisolatin2 iso-8859-2 iso-ir-101 iso8859-2 iso88592 iso_8859-2 iso_8859-2:1987 l2 latin2"),
    ("iso8859-3", "ISO-8859-3",
     "csisolatin3 iso-8859-3 iso-ir-109 iso8859-3 iso88593 iso_8859-3 iso_8859-3:1988 l3 latin3"),
    ("iso8859-4", "ISO-8859-4",
     "csisolatin4 iso-8859-4 iso-ir-110 iso8859-4 iso88594 iso_8859-4 iso_8859-4:1988 l4 latin4"),
    ("iso8859-5", "ISO-8859-5",
     "csisolatincyrillic cyrillic iso-8859-5 iso-ir-144 iso8859-5 iso88595 iso_8859-5 iso_8859-5:1988"),
    ("iso8859-6", "ISO-8859-6", "arabic asmo-708 csiso88596e csiso88596i csisolatinarabic ecma-114 iso-8859-6 "
                                "iso-8859-6-e iso-8859-6-i iso-ir-127 iso8859-6 iso88596 iso_8859-6 iso_8859-6:1987"),
    ("iso8859-7", "ISO-8859-7", "csisolatingreek ecma-118 elot_928 greek greek8 iso-8859-7 iso-ir-126 iso8859-7 "
                                "iso88597 iso_8859-7 iso_8859-7:1987 sun_eu_greek"),
    ("iso8859-8", "ISO-8859-8", "csiso88598e csisolatinhebrew hebrew iso-8859-8 iso-8859-8-e iso-ir-138 iso8859-8 "
                                "iso88598 iso_8859-8 iso_8859-8:1988 visual"),
    ("iso8859-8", "ISO-8859-8-I", "csiso88598i iso-8859-8-i logical"),  # same index, logical order
    ("iso8859-10", "ISO-8859-10", "csisolatin6 iso-8859-10 iso-ir-157 iso8859-10 iso885910 l6 latin6"),
    ("iso8859-13", "ISO-8859-13", "iso-8859-13 iso8859-13 iso885913"),
    ("iso8859-14", "ISO-8859-14", "iso-8859-14 iso8859-14 iso885914"),
    ("iso8859-15", "ISO-8859-15", "csisolatin9 iso-8859-15 iso8859-15 iso885915 iso_8859-15 l9"),
    ("iso8859-16", "ISO-8859-16", "iso-8859-16"),
    ("koi8-r", "KOI8-R", "cskoi8r koi koi8 koi8-r koi8_r"),
    ("koi8-u", "KOI8-U", "koi8-ru koi8-u"),
    ("mac-roman", "macintosh", "csmacintosh mac macintosh x-mac-roman"),
    ("cp874", "windows-874", "dos-874 iso-8859-11 iso8859-11 iso885911 tis-620 windows-874"),
    ("cp1250", "windows-1250", "cp1250 windows-1250 x-cp1250"),
    ("cp1251", "windows-1251", "cp1251 windows-1251 x-cp1251"),
    ("cp1252", "windows-1252", "ansi_x3.4-1968 ascii cp1252 cp819 csisolatin1 ibm819 iso-8859-1 iso-ir-100 iso8859-1 "
                               "iso88591 iso_8859-1 iso_8859-1:1987 l1 latin1 us-ascii windows-1252 x-cp1252"),
    ("cp1253", "windows-1253", "cp1253 windows-1253 x-cp1253"),
    ("cp1254", "windows-1254", "cp1254 csisolatin5 iso-8859-9 iso-ir-148 iso8859-9 iso88599 iso_8859-9 "
                               "iso_8859-9:1989 l5 latin5 windows-1254 x-cp1254"),
    ("cp1255", "windows-1255", "cp1255 windows-1255 x-cp1255"),
    ("cp1256", "windows-1256", "cp1256 windows-1256 x-cp1256"),
    ("cp1257", "windows-1257", "cp1257 windows-1257 x-cp1257"),
    ("cp1258", "windows-1258", "cp1258 windows-1258 x-cp1258"),
    ("mac-cyrillic", "x-mac-cyrillic", "x-mac-cyrillic x-mac-ukrainian"),
    ("gb18030", "GB18030", "gb18030"),
    # GBK's decoder is gb18030's
    ("gb18030", "GBK", "chinese csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk iso-ir-58 x-gbk"),
    ("big5hkscs", "Big5", "big5 big5-hkscs cn-big5 csbig5 x-x-big5"),
    ("euc_jp", "EUC-JP", "cseucpkdfmtjapanese euc-jp x-euc-jp"),
    ("iso2022_jp_ext", "ISO-2022-JP", "csiso2022jp iso-2022-jp"),  # the Python variant that has half-width kana
    ("cp932", "Shift_JIS", "csshiftjis ms932 ms_kanji shift-jis shift_jis sjis windows-31j x-sjis"),
    ("cp949", "EUC-KR", "cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 ks_c_5601-1989 ksc5601 "
                        "ksc_5601 windows-949"),
    # A document that names UTF-16 in a <meta> tag is UTF-8 (HTML spec); real UTF-16 documents are
    # caught by their BOM before labels are looked at.
    ("utf-16-le", "UTF-16LE", ""),
    ("utf-16-be", "UTF-16BE", ""),
    ("utf-8", "UTF-8", "csunicode iso-10646-ucs-2 ucs-2 unicode unicodefeff unicodefffe utf-16 utf-16be utf-16le"),
    # x-user-defined maps bytes 0x80-0xFF to private-use characters, useless as text; browsers
    # read a <meta> naming it as windows-1252 (HTML spec), and so do we wherever it is named.
    ("cp1252", "windows-1252", "x-user-defined"),
    # WHATWG's "replacement" labels decode to a single U+FFFD, so that these escape-based encodings
    # cannot hide markup from filters. Text extraction has no such concern: decode the real encoding
    # where Python has one. iso-2022-cn, iso-2022-cn-ext and "replacement" stay unknown labels.
    ("iso2022_kr", "ISO-2022-KR", "csiso2022kr iso-2022-kr"),
    ("hz", "HZ-GB-2312", "hz-gb-2312"),
)
_LABELS = {label: codec for codec, _, labels in _ENCODINGS for label in labels.split()}
_NAMES = {codec: name for codec, name, _ in reversed(_ENCODINGS)}  # reversed: the first row wins

# Labels outside the table reach Python's codec registry (charset-normalizer's guesses too).
# Codecs for which browsers use a related WHATWG encoding decode with that one: ISO-8859-1,
# -9 and -11 as windows-125x/874, GB2312 as GB18030, Shift_JIS and the JIS X 0213 variants
# as cp932 or WHATWG EUC-JP (the detector picks JIS X 0213 when a page uses NEC or IBM kanji),
# Python's big5 and cp950 as WHATWG Big5 (their tables put kana where Big5 has ①-⑩, ⅰ-ⅹ).
_BROWSER_CODEC = {"ascii": "cp1252", "iso8859-1": "cp1252", "iso8859-9": "cp1254", "iso8859-11": "cp874",
             "tis-620": "cp874", "gb2312": "gb18030", "gbk": "gb18030", "big5": "big5hkscs", "cp950": "big5hkscs",
             "shift_jis": "cp932", "shift_jis_2004": "cp932", "shift_jisx0213": "cp932", "euc_jis_2004": "euc_jp",
             "euc_jisx0213": "euc_jp", "euc_kr": "cp949", "iso2022_jp": "iso2022_jp_ext"}
# Codecs that are not charsets, or let a page rewrite its own text (escape decoders, and UTF-7,
# which browsers must not support).
_NOT_CHARSETS = {"undefined", "unicode-escape", "raw-unicode-escape", "idna", "punycode", "utf-7"}

_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_FFFD = "\ufffd"

# A page chooses its bytes and its label, so decoding has to cost about the same whatever they
# are. C code does the work; what runs in Python once per bad or special sequence (the error
# handlers and the twin scan below) is counted in steps, and a decode gets _MAX_STEPS of them:
# some tens of milliseconds, and far more than a real page takes. decode_body decodes a body at
# most four times (for a byte-order mark, the header's charset, the <meta>'s, the detector's guess).
_MAX_STEPS = 50_000
_running = threading.local()  # .budget: [steps left] for the decode running on this thread


class _GiveUp(Exception):
    """Raised by an error handler to end a decode whose result would not be used."""


# Single-byte encodings, decoded through a 256-entry table: Python's codec, with WHATWG's
# differences. windows-874 and windows-125x fill their gaps in 0x80-0x9F with the C1 control of
# the same number; every other byte Python leaves undefined is undefined in WHATWG's index too
# (an error, U+FFFD, which the table holds: no handler runs), except for the entries below.
_SINGLE_BYTE = {"cp866", "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5", "iso8859-6", "iso8859-7", "iso8859-8",
                "iso8859-10", "iso8859-13", "iso8859-14", "iso8859-15", "iso8859-16", "koi8-r", "koi8-u", "mac-roman",
                "cp874", "cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258",
                "mac-cyrillic"}
_SINGLE_BYTE_FIXES = {"cp1255": {0xCA: "\u05ba"}, "koi8-u": {0xAE: "\u045e", 0xBE: "\u040e"}}


@functools.lru_cache(maxsize=None)
def _byte_table(codec: str) -> str:
    chars = []
    for b in range(256):
        try:
            chars.append(bytes((b,)).decode(codec))
        except UnicodeDecodeError:
            chars.append(chr(b) if 0x80 <= b <= 0x9F else _FFFD)
    for b, ch in _SINGLE_BYTE_FIXES.get(codec, {}).items():
        chars[b] = ch
    return "".join(chars)


# Multi-byte encodings: Python's CJK codec, with an error handler that follows WHATWG's error
# rules and supplies the index entries Python's tables lack, then a few remapped characters.

# WHATWG index-big5 entries that Python's big5hkscs (HKSCS-2004) cannot decode: HKSCS-2008
# (lead 0x87), the control pictures and euro sign at 0xA3C0-0xA3E1, and positions of characters
# that also have another one. Runs of "<pointer> <code point> <code point> ...": consecutive pointers.
_BIG5_EXTRA_RUNS = """
1000 3875 21D53 2369E 26021 3EEC 258DE 3AF5 7AFC 9F97 24161 2890D 231EA 20A8A 2325E 430A 8484 9F96 942F 4930 8613
5896 974A 9218 79D0 7A32 6660 6A29 889D 744C 7BC5 6782 7A2C 524F 9046 34E6 73C4 25DB9 74C6 9FC7 57B3 492F 544C 4131
2368E 5818 7A72 27B65 8B8F 46AE 26E88 4181 25D99 7BAE 224BC 9FC8 224C1 224C9 224CC 9FC9 8504 235BB 40B4 9FCA 44E1
2ADFF 62C1 706E 9FCB; 2082 7BB8; 2088 7C06; 2103 7CCE; 2114 7DD2; 2123 7E1D; 2148 8005; 2151 8028; 2221 83C1;
2239 84A8; 2244 840F; 2303 89A6 89A9; 2354 8D77; 2400 90FD; 2413 92B9; 2477 975C; 2498 97FF; 2605 9F16; 2673 8503;
2746 5159 515B 515D 515E; 2771 936E; 2780 7479; 2990 6D67; 3087 799B; 3259 9097; 3301 975D; 3436 701E; 3451 5B28;
4136 7201; 4138 77D7; 4141 7E87; 4182 99D6; 4206 91D4; 4220 60DE; 4230 6FB6; 4241 8F36; 4258 4FBB; 4273 71DF;
4279 9104; 4282 9DF0; 4294 83CF; 4329 5C10 79E3; 4349 5A67; 4419 8F0B; 4422 7B51; 4494 62D0; 4624 6062; 4694 75F9;
4708 6C4A; 4742 9B2E; 4748 9F17; 4815 50ED; 4828 5F0C; 4902 880F; 4922 62CE; 4982 7468; 4992 7162; 4997 7250;
5432 2400-241F 2421 20AC; 10942 5EF4; 10946 65E0; 10948 7676; 10950 96B6; 10957 3003 4EDD; 19028 5029; 19035 507D;
19088 5305; 19096 5344; 19112 537F; 19162 5605; 19240 5A77; 19299 5E75; 19305 5ED0; 19326 5F58; 19355 60A4;
19398 6490; 19439 6674; 19454 675E; 19553 6C9C 6E1D; 19557 6E2F; 19611 716E; 19643 732A; 19672 745C; 19697 74E9;
19748 7809
"""


def _runs(spec: str) -> dict[int, str]:
    table = {}
    for run in spec.split(";"):
        first, *points = run.split()
        cps = []
        for p in points:
            lo, _, hi = p.partition("-")
            cps.extend(range(int(lo, 16), int(hi or lo, 16) + 1))
        table.update((int(first) + k, chr(cp)) for k, cp in enumerate(cps))
    return table


_BIG5_EXTRA = _runs(_BIG5_EXTRA_RUNS)


def _after_lead(data: bytes, i: int) -> tuple[str, int]:
    """WHATWG: a lead byte that starts no character is one error together with the byte after it,
    unless that byte is ASCII, which is decoded again."""
    if i + 1 >= len(data):
        return _FFFD, len(data)
    return _FFFD, i + (2 if data[i + 1] > 0x7F else 1)


def _whatwg_errors(err: UnicodeDecodeError) -> tuple[str, int]:
    # Python's CJK codecs report a bad sequence at its first byte, whatever its length.
    data, i, codec = err.object, err.start, err.encoding
    budget = _running.budget
    if budget[0] <= 0:
        # Out of steps: the rest in one piece by Python's codec alone, so with its tables and one
        # U+FFFD per bad byte. All of the input is still decoded, and the errors still count.
        return data[i:].decode(codec, "replace"), len(data)
    budget[0] -= 1
    b = data[i]
    if codec == "euc_jp":
        t = data[i + 1] if i + 1 < len(data) else -1
        if b == 0x8F and 0xA1 <= t <= 0xFE:
            return _after_lead(data, i + 1)  # JIS X 0212 lead: Python's table has every WHATWG entry
        if 0xA1 <= b <= 0xFE and 0xA1 <= t <= 0xFE:
            # NEC row 13 and the IBM extensions, which Python's JIS X 0208 lacks: WHATWG's index
            # agrees with cp932 at the same row and cell, so ask cp932 for the Shift_JIS form.
            j1, j2 = b - 0x80, t - 0x80
            s1 = ((j1 + 1) >> 1) + (0x70 if j1 <= 0x5E else 0xB0)
            s2 = j2 + (0x7E if j1 % 2 == 0 else (0x1F if j2 <= 0x5F else 0x20))
            try:
                return bytes((s1, s2)).decode("cp932"), i + 2
            except UnicodeDecodeError:
                return _FFFD, i + 2
        is_lead = b in (0x8E, 0x8F) or 0xA1 <= b <= 0xFE
    elif codec == "big5hkscs":
        t = data[i + 1] if i + 1 < len(data) else -1
        if 0x81 <= b <= 0xFE and (0x40 <= t <= 0x7E or 0xA1 <= t <= 0xFE):
            ch = _BIG5_EXTRA.get((b - 0x81) * 157 + t - (0x40 if t < 0x7F else 0x62))
            if ch:
                return ch, i + 2
        is_lead = 0x81 <= b <= 0xFE
    elif codec == "cp932":
        is_lead = 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC
    else:  # cp949
        is_lead = 0x81 <= b <= 0xFE
    return _after_lead(data, i) if is_lead else (_FFFD, i + 1)


def _whatwg_clean(err: UnicodeDecodeError) -> tuple[str, int]:
    """_whatwg_errors for a caller that wants the text only if it has no errors."""
    out = _whatwg_errors(err)
    if out[0] == _FFFD:
        raise _GiveUp
    return out


def _replace_errors(err: UnicodeDecodeError) -> tuple[str, int]:
    """"replace" for Python's other decoders. Its CJK codecs replace inside their C loop; the rest
    (UTF-16, UTF-32, charmap tables) call the handler for every bad sequence, a few million times
    a second. Text with more bad sequences than the budget is not in that encoding."""
    budget = _running.budget
    if budget[0] <= 0:
        raise _GiveUp
    budget[0] -= 1
    return _FFFD, err.end


# Error handler names are process-wide.
_ERRORS, _CLEAN, _REPLACE = "webfetch-whatwg", "webfetch-whatwg-clean", "webfetch-replace"
codecs.register_error(_ERRORS, _whatwg_errors)
codecs.register_error(_CLEAN, _whatwg_clean)
codecs.register_error(_REPLACE, _replace_errors)

# Characters Python's tables give for a symbol where WHATWG's index (which follows Windows) has
# another code point. Each source character comes from that one byte sequence only.
_REMAP = {
    "euc_jp": (("\u301c", "\uff5e"), ("\u2016", "\u2225"), ("\u2212", "\uff0d"),
               ("\u00a2", "\uffe0"), ("\u00a3", "\uffe1"), ("\u00ac", "\uffe2")),
    "big5hkscs": (("\u2022", "\u2027"), ("\uff64", "\ufe51"), ("\u203e", "\u00af"),
                  ("\u223c", "\uff5e"), ("\u2641", "\u2295"), ("\u2609", "\u2299"),
                  ("\u00a5", "\uffe5"), ("\u00a2", "\uffe0"), ("\u00a3", "\uffe1")),
    # cp932 decodes the single bytes 0xA0 and 0xFD-0xFF as private-use characters; WHATWG: errors.
    "cp932": tuple((chr(cp), _FFFD) for cp in range(0xF8F0, 0xF8F4)),
}


def _eucjp_inside(pending: bytes) -> bool:
    if len(pending) == 2:  # 0x8F and a second byte, which is a JIS X 0212 lead or an error
        return 0xA1 <= pending[1] <= 0xFE
    return bool(pending) and (pending[0] in (0x8E, 0x8F) or 0xA1 <= pending[0] <= 0xFE)


# Sequences Python decodes to the same character as another sequence, where WHATWG gives a
# different one (EUC-JP 0x8FA2B7 is "~" in Python, like ASCII 0x7E; Big5 0xA241 and 0xA242 are
# Python's 0xA1FE and 0xA240). They are found in the bytes and decided where the decoder is
# between characters; `inside` says from the decoder's pending bytes whether WHATWG's decoder is
# inside a sequence there, which then takes the found bytes as its trail byte or an error.
_TWINS = {
    "euc_jp": ({b"\x8f\xa2\xb7": "\uff5e"}, _eucjp_inside),
    "big5hkscs": ({b"\xa2A": "\u2215", b"\xa2B": "\ufe68"},
                  lambda pending: bool(pending) and 0x81 <= pending[0] <= 0xFE),
}
_TWIN_RE = {codec: re.compile(b"|".join(map(re.escape, chars))) for codec, (chars, _) in _TWINS.items()}


def _decode_twins(data: bytes, codec: str, errors: str) -> str:
    chars, inside = _TWINS[codec]
    dec = codecs.getincrementaldecoder(codec)(errors)
    budget = _running.budget
    out, pos = [], 0
    for m in _TWIN_RE[codec].finditer(data):
        if budget[0] <= 0:  # the twins that are left decode as Python has them
            break
        budget[0] -= 1
        out.append(dec.decode(data[pos:m.start()]))
        pending = dec.getstate()[0]
        if inside(pending):
            out.append(dec.decode(m.group()))
        else:  # pending bytes, if any, start no character: decode them as the errors they are
            dec.reset()
            out.append(pending.decode(codec, errors) + chars[m.group()])
        pos = m.end()
    out.append(dec.decode(data[pos:]))
    # The tail, decoded in one go: with final=True, CPython's incremental CJK decoders keep the
    # bytes after an error at the end pending instead of decoding them.
    out.append(dec.getstate()[0].decode(codec, errors))
    return "".join(out)


def _decode_multibyte(data: bytes, codec: str, errors: str) -> str:
    if codec in _TWIN_RE and _TWIN_RE[codec].search(data):
        text = _decode_twins(data, codec, errors)
    else:
        text = data.decode(codec, errors)
    for src, dst in _REMAP.get(codec, ()):
        if src in text:
            text = text.replace(src, dst)
    return text


def _inline_replace(enc: str) -> bool:
    decoder = codecs.lookup(enc).incrementaldecoder
    return isinstance(decoder, type) and issubclass(decoder, MultibyteIncrementalDecoder)


def _decode(data: bytes, enc: str, clean: bool = False, steps: int = _MAX_STEPS) -> tuple[str, int] | None:
    """Decode the WHATWG way: ``(text, number of errors)``, or None if the codec is unusable
    (for this body: see _replace_errors). A caller that uses the text only if it has no errors
    says `clean`, and may get None for text with errors."""
    _running.budget = [steps]
    try:
        if enc == "utf-8":
            try:
                return data.decode("utf-8"), 0
            except UnicodeDecodeError:
                text = data.decode("utf-8", "replace")  # one U+FFFD per maximal bad subpart, as WHATWG
        elif enc in ("euc_jp", "big5hkscs", "cp932", "cp949"):
            text = _decode_multibyte(data, enc, _CLEAN if clean else _ERRORS)
        elif enc in _SINGLE_BYTE:
            text = codecs.charmap_decode(data, "strict", _byte_table(enc))[0]
        else:
            text = data.decode(enc, "replace" if _inline_replace(enc) else _REPLACE)
    except (LookupError, UnicodeError, ValueError, _GiveUp):
        return None
    errors = text.count(_FFFD)
    if errors and enc == "utf-8":
        errors = max(0, errors - data.count(b"\xef\xbf\xbd"))  # U+FFFD the page itself contains
    return text, errors


def _decoded_chars(text: str, errors: int) -> int:
    """The non-ASCII characters that decoded: what speaks for the charset the text was decoded with."""
    return len(text) - len(text.encode("ascii", "ignore")) - errors


def _few_errors(text: str, errors: int) -> bool:
    """A declared charset with a few bad bytes stays, as in browsers; pervasive errors mean the
    declaration is wrong (e.g. a utf-8 header over cp1251 bytes)."""
    return errors <= 2 or _decoded_chars(text, errors) >= 4 * errors


# The legacy multi-byte encodings share their byte ranges: text in one decodes in another as
# other characters, with an error only here and there. For these, few errors do not show that a
# declaration is right; how the text reads does.
_CROSS_DECODING = {"euc_jp", "big5hkscs", "cp932", "cp949", "gb18030"}
_HIGH_BYTE = re.compile(rb"[\x80-\xff]")
_TEXT_RUN = re.compile(r"[^\x00-\x7f][^<>]{0,300}")  # non-ASCII text, to the end of its text node


def _reads_better(data: bytes, guess: str | None, enc: str) -> bool:
    """Whether the detector's guess makes clearly better text of the body than `enc`, a declared
    charset that decoded it with few errors. Judged by charset-normalizer's measure of mojibake
    (0 for clean text, 0.2 where the detector itself gives a decoding up), on the same stretch of
    the body, so that stray bytes in a correctly declared page cost both readings alike."""
    if guess is None or guess == enc:
        return False
    start = _HIGH_BYTE.search(data)
    start = start.start() if start else 0
    mess = []
    for codec in (enc, guess):
        decoded = _decode(data[start:start + 100_000], codec, steps=1_000)  # a sample: it need not be exact
        runs = "\n".join(_TEXT_RUN.findall(decoded[0])).replace(_FFFD, "")[:16_000] if decoded else ""
        if len(runs) - len(runs.encode("ascii", "ignore")) < 16:  # too little text to judge by
            return False
        mess.append(mess_ratio(runs))
    return mess[0] - mess[1] >= 0.1


def normalize_label(label: str | None) -> str | None:
    """A charset label -> the Python codec we decode it with, or None if it names no charset."""
    if not label:
        return None
    key = label.strip().strip("\"'").lower()
    if key in _LABELS:
        return _LABELS[key]
    try:
        info = codecs.lookup(key)
    except LookupError:
        return None
    # bytes.decode refuses codecs that are not text encodings (base64, zlib, rot13, ...)
    if not getattr(info, "_is_text_encoding", True) or info.name in _NOT_CHARSETS:
        return None
    return _BROWSER_CODEC.get(info.name, info.name)


def whatwg_name(enc: str) -> str:
    """The name browsers and HTTP clients understand for a codec decode_body returned
    (cp932 -> Shift_JIS, big5hkscs -> Big5, cp1252 -> windows-1252, ...); others pass through."""
    if enc in _NAMES:
        return _NAMES[enc]
    try:
        name = codecs.lookup(enc).name
    except LookupError:
        return enc
    return _NAMES.get(_BROWSER_CODEC.get(name, name), enc)


def charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    m = _CT_RE.search(content_type)
    return normalize_label(m.group(1)) if m else None


def charset_from_meta(head: bytes) -> str | None:
    m = _META_RE.search(head)
    if not m:
        return None
    return normalize_label(m.group(1).decode("ascii", "ignore"))


def decode_body(data: bytes, content_type: str | None = None) -> tuple[str, str, str]:
    """Return ``(text, encoding, source)`` where source names the rule that decided."""
    if not data:
        return "", "utf-8", "empty"
    for bom, enc in _BOMS:
        if data.startswith(bom):
            decoded = _decode(data[len(bom):], enc)
            if decoded is not None:
                return decoded[0], enc, "bom"

    candidates: list[tuple[str, str]] = []
    header = charset_from_content_type(content_type)
    if header:
        candidates.append((header, "http-header"))
    meta = charset_from_meta(data[:8192])
    if meta and meta not in {c for c, _ in candidates}:
        candidates.append((meta, "meta"))

    try:
        utf8 = data.decode("utf-8")
    except UnicodeDecodeError:
        utf8 = None

    lossy: dict[str, tuple[str, int, str]] = {}
    for enc, source in candidates:
        if utf8 is not None and enc == "utf-8":
            return utf8, enc, source
        # A single-byte decoder takes nearly any bytes: that a <meta> naming one finds no error
        # says nothing against a UTF-8 header. Where text decoded as UTF-8 and little did not,
        # the header stays (below); bytes of which nothing did are left to the <meta>.
        if enc in _SINGLE_BYTE and "utf-8" in lossy and _decoded_chars(*lossy["utf-8"][:2]) >= 4 * lossy["utf-8"][1]:
            break
        # Valid UTF-8 is what the body is unless a declaration decodes it without a single error
        # (a browser shows that decoding), so there a decode can end at its first error.
        decoded = _decode(data, enc, clean=utf8 is not None)
        if decoded is None:
            continue
        if not decoded[1]:
            return decoded[0], enc, source
        lossy[enc] = (*decoded, source)

    if utf8 is not None:
        # The escape-based encodings are ASCII, so valid UTF-8 too, whatever the escapes say:
        # where one decoded text, a few errors in it do not make the page UTF-8.
        for enc, (text, errors, source) in lossy.items():
            if enc.startswith(("iso2022", "hz")) and _few_errors(text, errors) and _decoded_chars(text, errors):
                return text, enc, source + "-lossy"
        return utf8, "utf-8", "utf-8-valid"

    detected: list[str | None] = []  # the detector's guess, asked for at most once

    def guess() -> str | None:
        if not detected:
            best = from_bytes(data[:500_000]).best()
            detected.append(normalize_label(best.encoding) if best is not None and best.encoding else None)
        return detected[0]

    for enc, (text, errors, source) in lossy.items():
        if _few_errors(text, errors) and not (enc in _CROSS_DECODING and _reads_better(data, guess(), enc)):
            return text, enc, source + "-lossy"  # the declared charset, with U+FFFD for the bad bytes

    enc = guess()
    decoded = lossy.get(enc) or (_decode(data, enc) if enc else None)
    if decoded is not None:
        return decoded[0], enc, "detected"

    for enc, (text, _, source) in lossy.items():
        return text, enc, source + "-lossy"
    return _decode(data, "cp1252")[0], "cp1252", "fallback"
