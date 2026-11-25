"""Charset decoding regressions from the audit: WHATWG decoders, labels and declared-charset tolerance.

Expected values come from the WHATWG Encoding Standard (encoding.spec.whatwg.org: its indexes and
decoder algorithms) and the web-platform-tests encoding corpora, not from this implementation.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from webfetch import decode
from webfetch.decode import decode_body, normalize_label

FFFD = "\ufffd"


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ej(s: str) -> bytes:
    return s.encode("euc_jp")


# --- EUC-JP ---------------------------------------------------------------------------------

def test_eucjp_page_with_nec_and_ibm_kanji_keeps_its_charset():
    # 髙 (FC E2) and 﨑 (F9 F5) are IBM extensions, ① (AD A1), № ㈱ Ⅰ are NEC row 13: Python's
    # euc_jp has none of them, which used to throw the declaration away (detected euc_jis_2004: 郄, 粼).
    body = _ej("株式会社") + b"\xfc\xe2" + _ej("橋製作所 担当：山") + b"\xf9\xf5" + _ej(" ") + b"\xad\xa1" + _ej("受付")
    page = (b'<html><head><meta charset="euc-jp"></head><body><p>' + _ej("お問い合わせ先") + b"</p><p>" + body
            + b"\xad\xe2\xad\xea\xad\xb5</p></body></html>")
    text, enc, src = decode_body(page, "text/html")
    assert "株式会社髙橋製作所 担当：山﨑 ①受付№㈱Ⅰ" in text
    assert (enc, src) == ("euc_jp", "meta")


def test_undeclared_eucjp_page_with_ibm_kanji_is_read_as_eucjp():
    # charset-normalizer guesses euc_jis_2004 for this page; browsers read EUC-JP, and so do we now.
    body = _ej("株式会社") + b"\xfc\xe2" + _ej("橋製作所 担当：山") + b"\xf9\xf5" + _ej(" ") + b"\xad\xa1" + _ej("受付")
    page = (b"<html><body><p>" + _ej("お問い合わせ先は下記の通りです。営業時間は平日の午前九時から午後五時までとなります。")
            + b"</p><p>" + body + b"</p></body></html>")
    text, enc, src = decode_body(page, "text/html")
    assert "株式会社髙橋製作所 担当：山﨑 ①受付" in text
    assert (enc, src) == ("euc_jp", "detected")


def test_eucjp_symbols_use_the_code_points_browsers_use():
    raw = bytes.fromhex("a1c1 a1c2 a1dd a1f1 a1f2 a2cc 8fa2b7 7e")
    text, enc, src = decode_body(b'<meta charset="euc-jp"><p>' + raw + b"</p>", None)
    assert "<p>\uff5e\u2225\uff0d\uffe0\uffe1\uffe2\uff5e~</p>" in text
    assert (enc, src) == ("euc_jp", "meta")


@pytest.mark.parametrize("label", ["x-euc-jp", "cseucpkdfmtjapanese", "EUC-JP"])
def test_eucjp_labels(label):
    page = b'<meta charset="' + label.encode() + b'"><p>' + _ej("日本語のページです。") + b"</p>"
    assert decode_body(page, None)[1:] == ("euc_jp", "meta")
    assert decode_body(page, "text/html; charset=" + label)[1:] == ("euc_jp", "http-header")


# --- Big5 -----------------------------------------------------------------------------------

def test_big5_hkscs_2008_euro_and_control_pictures():
    raw = bytes.fromhex("a3e1 a3c0 a3e0 877a 877b 87df fb48 fe52")
    text, enc, src = decode_body(b'<meta charset="big5"><p>' + raw + b"</p>", None)
    assert "<p>\u20ac\u2400\u2421\u3875\U00021d53\u9fcb\u5605\u732a</p>" in text
    assert (enc, src) == ("big5hkscs", "meta")


def test_big5_symbols_use_the_code_points_browsers_use():
    # A241 and A242 are U+FF0F and U+FF3C in Python, like A1FE and A240, which keep them.
    raw = bytes.fromhex("a145 a14e a1c2 a1e3 a1f2 a1f3 a241 a242 a244 a246 a247 a1fe a240")
    text = decode_body(b'<meta charset="big5"><p>' + raw + b"</p>", None)[0]
    assert ("<p>\u2027\ufe51\u00af\uff5e\u2295\u2299\u2215\ufe68\uffe5\uffe0"
            "\uffe1\uff0f\uff3c</p>") in text


# --- WHATWG error rules: a lead byte and the byte after it are one error, unless that byte is
# ASCII, which is decoded again. Each sequence sits in valid text, so the declaration stays.

_FILLER = {"euc-jp": ("euc_jp", "日本語の文章です。お問い合わせはこちらまで。"),
           "big5": ("big5hkscs", "中文網頁內容，歡迎光臨本站，謝謝您的支持。"),
           "shift_jis": ("cp932", "日本語の文章です。お問い合わせはこちらまで。"),
           "euc-kr": ("cp949", "한국어 문장입니다. 문의는 이쪽으로 해 주세요."),
           "utf-8": ("utf-8", "Grüße aus München, schöne Grüße! Äpfel, Öl, Übung.")}


@pytest.mark.parametrize("label,raw,expected", [
    ("euc-jp", "8e e0", FFFD), ("euc-jp", "8e 41", FFFD + "A"), ("euc-jp", "8e 8e", FFFD),
    ("euc-jp", "8f 41", FFFD + "A"), ("euc-jp", "8f 80", FFFD), ("euc-jp", "8f a1 41", FFFD + "A"),
    ("euc-jp", "8f a1 80", FFFD), ("euc-jp", "a1 41", FFFD + "A"), ("euc-jp", "a1 80", FFFD),
    ("euc-jp", "a9 a1", FFFD), ("euc-jp", "80 ff", FFFD * 2), ("euc-jp", "8f", FFFD), ("euc-jp", "8f a1", FFFD),
    # 8F A2 B7 is U+FF5E only where a sequence starts; after a lead byte its 0x8F is a bad trail byte
    ("euc-jp", "8f a2 b7", "\uff5e"), ("euc-jp", "a1 8f a2 b7", FFFD * 2),
    ("euc-jp", "8f 41 8f a2 b7", FFFD + "A\uff5e"), ("euc-jp", "8f a2 8f a2 b7", FFFD * 2),
    ("big5", "80 ff 81", FFFD * 3), ("big5", "a1 80", FFFD), ("big5", "a1 30", FFFD + "0"), ("big5", "c8 f2", FFFD),
    # Big5 trail bytes can be ASCII or lead bytes: A2 41 is U+2215 only where a sequence starts
    ("big5", "a4 a2 41", "\u4e10A"), ("big5", "80 a2 41", FFFD + "\u2215"),
    ("big5", "a4 40 a2 42", "\u4e00\ufe68"),
    ("shift_jis", "a0", FFFD), ("shift_jis", "fd fe ff", FFFD * 3), ("shift_jis", "80", "\x80"),
    ("shift_jis", "81 fd", FFFD), ("shift_jis", "81 7f", FFFD + "\x7f"), ("shift_jis", "eb 40", FFFD + "@"),
    ("euc-kr", "80 ff", FFFD * 2), ("euc-kr", "c9 80", FFFD), ("euc-kr", "fe 41", FFFD + "A"),
    ("utf-8", "f0 9f 92", FFFD), ("utf-8", "ed a0 80", FFFD * 3), ("utf-8", "e2 28 a1", FFFD + "(" + FFFD),
    ("utf-8", "f4 90 80 80", FFFD * 4),
])
def test_error_rules(label, raw, expected):
    codec, filler = _FILLER[label]
    text = filler.encode(codec) * 2
    page = b'<html><head><meta charset="%s"></head><body><p>%s</p><b>%s</b><p>%s</p></body></html>' % (
        label.encode(), text, bytes.fromhex(raw), text)
    decoded, enc, src = decode_body(page, "text/html")
    assert decoded.split("<b>", 1)[1].split("</b>", 1)[0] == expected
    assert enc == normalize_label(label) and src.startswith("meta")


def _pairs(leads, trails, prefix=b""):
    """Every lead/trail pair on its own line: each decodes independently."""
    return b"\n".join(prefix + bytes((lead, trail)) for lead in leads for trail in trails)


# Fingerprints of WHATWG's decoding (its indexes, run through its decoder algorithm) of every
# sequence below: they pin every index entry and the error rules for each lead byte.
@pytest.mark.parametrize("codec,data,fingerprint", [
    ("euc_jp", _pairs(range(0xA1, 0xFF), range(0xA1, 0xFF)), "740f3c368feb6fae"),  # JIS X 0208
    ("euc_jp", _pairs(range(0xA1, 0xFF), range(0xA1, 0xFF), b"\x8f"), "816cdd19b564072a"),  # JIS X 0212
    ("euc_jp", _pairs([0x8E], range(0x100)), "418b9df1765eab2c"),  # half-width katakana
    ("big5hkscs", _pairs(range(0x81, 0xFF), [*range(0x40, 0x7F), *range(0xA1, 0xFF)]), "a431748a8eda361d"),
    ("cp932", _pairs([*range(0x81, 0xA0), *range(0xE0, 0xFD)], [*range(0x40, 0x7F), *range(0x80, 0xFD)]),
     "32a67f73b65ff072"),
    ("cp949", _pairs(range(0x81, 0xFF), range(0x41, 0xFF)), "0b0001b7bfa6f0e4"),
    ("euc_jp", b"\n".join(bytes((b,)) for b in range(0x80, 0x100)), "35d2e4be41af2d46"),
    ("big5hkscs", b"\n".join(bytes((b,)) for b in range(0x80, 0x100)), "35d2e4be41af2d46"),
    ("cp932", b"\n".join(bytes((b,)) for b in range(0x80, 0x100)), "aabb943f9c6cfd8d"),
    ("cp949", b"\n".join(bytes((b,)) for b in range(0x80, 0x100)), "35d2e4be41af2d46"),
])
def test_every_sequence_decodes_as_whatwg_specifies(codec, data, fingerprint):
    assert _fp(decode._decode(data, codec)[0]) == fingerprint


def test_large_pages_stay_fast():
    # Guards against per-character Python loops: these take ~10 ms; a pure-Python decoder ~1 s.
    filler = _ej("日本語のテキストです。漢字とひらがなとカタカナ。") * 30_000  # ~1.3 MB
    eucjp = b'<meta charset="euc-jp">' + filler + bytes.fromhex("ada1 fce2 a1c1 8fa2b7") * 200
    big5 = b'<meta charset="big5">' + "中文網頁內容，歡迎光臨本站。".encode("big5hkscs") * 50_000 + b"\xa2A\xa1\xfe" * 200
    for page in (eucjp, big5):
        start = time.perf_counter()
        decode_body(page, None)
        assert time.perf_counter() - start < 0.5


# --- Single-byte encodings ------------------------------------------------------------------

_SINGLE_BYTE = {  # name: (codec, bytes WHATWG leaves undefined, fingerprint of decoding 0x00-0xFF)
    "IBM866": ("cp866", "", "3c8cc5cb485f93d2"),
    "ISO-8859-2": ("iso8859-2", "", "a5871b0f978b840b"),
    "ISO-8859-3": ("iso8859-3", "A5 AE BE C3 D0 E3 F0", "e83895f2b7d7b82b"),
    "ISO-8859-4": ("iso8859-4", "", "449076e20ebf45eb"),
    "ISO-8859-5": ("iso8859-5", "", "9f31ddc0f7444afa"),
    "ISO-8859-6": ("iso8859-6", "A1-A3 A5-AB AE-BA BC-BE C0 DB-DF F3-FF", "beba4e6cf97dce83"),
    "ISO-8859-7": ("iso8859-7", "AE D2 FF", "71069977a6798ab7"),
    "ISO-8859-8": ("iso8859-8", "A1 BF-DE FB FC FF", "b43535e7aaeb7bcf"),
    "ISO-8859-10": ("iso8859-10", "", "282514fbd01219c4"),
    "ISO-8859-13": ("iso8859-13", "", "4426f6d2f1b025cd"),
    "ISO-8859-14": ("iso8859-14", "", "f03afb7e01e66cac"),
    "ISO-8859-15": ("iso8859-15", "", "9b58b26dbd8fbff2"),
    "ISO-8859-16": ("iso8859-16", "", "2de1faef4dc524c9"),
    "KOI8-R": ("koi8-r", "", "fb0243455e64ef70"),
    "KOI8-U": ("koi8-u", "", "896c218aaf12ca1b"),
    "macintosh": ("mac-roman", "", "54112bce885d7b1a"),
    "windows-874": ("cp874", "DB-DE FC-FF", "6a2c7940c3d68216"),
    "windows-1250": ("cp1250", "", "03772ed2e875bd12"),
    "windows-1251": ("cp1251", "", "b16600cf4e6d1a2d"),
    "windows-1252": ("cp1252", "", "cc916e51644a12e8"),
    "windows-1253": ("cp1253", "AA D2 FF", "e4570135cbc6e3d5"),
    "windows-1254": ("cp1254", "", "4a8e99647c3e28e6"),
    "windows-1255": ("cp1255", "D9-DF FB FC FF", "870c5c5e687fabcd"),
    "windows-1256": ("cp1256", "", "6f6e8626197b1b6b"),
    "windows-1257": ("cp1257", "A1 A5", "d19a4e888879e36a"),
    "windows-1258": ("cp1258", "", "e79b48db126bc71d"),
    "x-mac-cyrillic": ("mac-cyrillic", "", "784db55e1c90195e"),
}


def _byte_list(spec: str) -> list[int]:
    out = []
    for part in spec.split():
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo, 16), int(hi or lo, 16) + 1))
    return out


@pytest.mark.parametrize("name", sorted(_SINGLE_BYTE))
def test_single_byte_encodings_match_the_whatwg_index(name):
    codec, undefined, fingerprint = _SINGLE_BYTE[name]
    assert normalize_label(name) == codec
    text = decode._decode(bytes(range(256)), codec)[0]
    assert [b for b in range(256) if text[b] == FFFD] == _byte_list(undefined)
    assert _fp(text) == fingerprint


def test_single_byte_undefined_bytes_and_whatwg_differences():
    def decoded(charset, raw):
        text = decode_body(b"<p>" + raw + b"</p>", "text/html; charset=" + charset)[0]
        return text[3:-4]

    assert decoded("windows-1252", b"\x80\x81\x8d\x8f\x90\x9d") == "\u20ac\x81\x8d\x8f\x90\x9d"  # C1 controls
    assert decoded("windows-874", b"\x81\xdb\xa1") == "\x81" + FFFD + "\u0e01"  # DB is undefined, not 'Û'
    assert decoded("windows-1253", b"\xe1\xaa") == "\u03b1" + FFFD  # AA is undefined, not 'ª'
    assert decoded("windows-1255", b"\xe0\xca") == "\u05d0\u05ba"  # CA is HOLAM HASER FOR VAV
    assert decoded("iso-8859-7", b"\xe1\xae") == "\u03b1" + FFFD  # AE is undefined, not '®'
    assert decoded("iso-8859-3", b"\xa1\xa5") == "\u0126" + FFFD  # a stray byte keeps ISO-8859-3
    assert decoded("koi8-u", b"\xae\xbe") == "\u045e\u040e"  # KOI8-U is WHATWG's KOI8-RU: ў Ў


# --- Labels -----------------------------------------------------------------------------------

_WHATWG_LABELS = {  # encoding.spec.whatwg.org/encodings.json
    "UTF-8": "unicode-1-1-utf-8 unicode11utf8 unicode20utf8 utf-8 utf8 x-unicode20utf8",
    "IBM866": "866 cp866 csibm866 ibm866",
    "ISO-8859-2": "csisolatin2 iso-8859-2 iso-ir-101 iso8859-2 iso88592 iso_8859-2 iso_8859-2:1987 l2 latin2",
    "ISO-8859-3": "csisolatin3 iso-8859-3 iso-ir-109 iso8859-3 iso88593 iso_8859-3 iso_8859-3:1988 l3 latin3",
    "ISO-8859-4": "csisolatin4 iso-8859-4 iso-ir-110 iso8859-4 iso88594 iso_8859-4 iso_8859-4:1988 l4 latin4",
    "ISO-8859-5": "csisolatincyrillic cyrillic iso-8859-5 iso-ir-144 iso8859-5 iso88595 iso_8859-5 iso_8859-5:1988",
    "ISO-8859-6": ("arabic asmo-708 csiso88596e csiso88596i csisolatinarabic ecma-114 iso-8859-6 "
                   "iso-8859-6-e iso-8859-6-i iso-ir-127 iso8859-6 iso88596 iso_8859-6 iso_8859-6:1987"),
    "ISO-8859-7": ("csisolatingreek ecma-118 elot_928 greek greek8 iso-8859-7 iso-ir-126 iso8859-7 "
                   "iso88597 iso_8859-7 iso_8859-7:1987 sun_eu_greek"),
    "ISO-8859-8": ("csiso88598e csisolatinhebrew hebrew iso-8859-8 iso-8859-8-e iso-ir-138 iso8859-8 "
                   "iso88598 iso_8859-8 iso_8859-8:1988 visual"),
    "ISO-8859-8-I": "csiso88598i iso-8859-8-i logical",
    "ISO-8859-10": "csisolatin6 iso-8859-10 iso-ir-157 iso8859-10 iso885910 l6 latin6",
    "ISO-8859-13": "iso-8859-13 iso8859-13 iso885913",
    "ISO-8859-14": "iso-8859-14 iso8859-14 iso885914",
    "ISO-8859-15": "csisolatin9 iso-8859-15 iso8859-15 iso885915 iso_8859-15 l9",
    "ISO-8859-16": "iso-8859-16",
    "KOI8-R": "cskoi8r koi koi8 koi8-r koi8_r",
    "KOI8-U": "koi8-ru koi8-u",
    "macintosh": "csmacintosh mac macintosh x-mac-roman",
    "windows-874": "dos-874 iso-8859-11 iso8859-11 iso885911 tis-620 windows-874",
    "windows-1250": "cp1250 windows-1250 x-cp1250",
    "windows-1251": "cp1251 windows-1251 x-cp1251",
    "windows-1252": ("ansi_x3.4-1968 ascii cp1252 cp819 csisolatin1 ibm819 iso-8859-1 iso-ir-100 iso8859-1 "
                     "iso88591 iso_8859-1 iso_8859-1:1987 l1 latin1 us-ascii windows-1252 x-cp1252"),
    "windows-1253": "cp1253 windows-1253 x-cp1253",
    "windows-1254": ("cp1254 csisolatin5 iso-8859-9 iso-ir-148 iso8859-9 iso88599 iso_8859-9 "
                     "iso_8859-9:1989 l5 latin5 windows-1254 x-cp1254"),
    "windows-1255": "cp1255 windows-1255 x-cp1255",
    "windows-1256": "cp1256 windows-1256 x-cp1256",
    "windows-1257": "cp1257 windows-1257 x-cp1257",
    "windows-1258": "cp1258 windows-1258 x-cp1258",
    "x-mac-cyrillic": "x-mac-cyrillic x-mac-ukrainian",
    "GBK": "chinese csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk iso-ir-58 x-gbk",
    "gb18030": "gb18030",
    "Big5": "big5 big5-hkscs cn-big5 csbig5 x-x-big5",
    "EUC-JP": "cseucpkdfmtjapanese euc-jp x-euc-jp",
    "ISO-2022-JP": "csiso2022jp iso-2022-jp",
    "Shift_JIS": "csshiftjis ms932 ms_kanji shift-jis shift_jis sjis windows-31j x-sjis",
    "EUC-KR": ("cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 ks_c_5601-1989 ksc5601 "
               "ksc_5601 windows-949"),
    "replacement": "csiso2022kr hz-gb-2312 iso-2022-cn iso-2022-cn-ext iso-2022-kr replacement",
    "UTF-16BE": "unicodefffe utf-16be",
    "UTF-16LE": "csunicode iso-10646-ucs-2 ucs-2 unicode unicodefeff utf-16 utf-16le",
    "x-user-defined": "x-user-defined",
}
# Where we deliberately decode differently from the encoding's own decoder (see decode.py):
_DECODED_AS = {
    "GBK": "GB18030", "gb18030": "GB18030",  # the same decoder; IANA's spelling
    "ISO-8859-8-I": "ISO-8859-8",  # the same index
    "UTF-16BE": "UTF-8", "UTF-16LE": "UTF-8",  # what a <meta> naming UTF-16 means; real UTF-16 has a BOM
    "x-user-defined": "windows-1252",  # what browsers do for a <meta>; its own decoder gives private-use chars
}
_REPLACEMENT = {"csiso2022kr": "ISO-2022-KR", "iso-2022-kr": "ISO-2022-KR", "hz-gb-2312": "HZ-GB-2312",
                "iso-2022-cn": None, "iso-2022-cn-ext": None, "replacement": None}


def test_every_whatwg_label_resolves():
    wrong = []
    for name, labels in _WHATWG_LABELS.items():
        for label in labels.split():
            want = _REPLACEMENT[label] if name == "replacement" else _DECODED_AS.get(name, name)
            for spelling in (label, label.upper(), f" {label}\t"):
                enc = normalize_label(spelling)
                got = decode.whatwg_name(enc) if enc else None
                if got != want:
                    wrong.append((spelling, want, enc, got))
    assert wrong == []


@pytest.mark.parametrize("label", _WHATWG_LABELS["windows-1252"].split())
def test_every_windows_1252_label_decodes_as_windows_1252(label):
    # Not latin-1 (C1 controls for 0x80-0x9F) and not strict ASCII (U+FFFD for every byte above 0x7F).
    text, enc, src = decode_body(b"<p>caf\xe9 \x80 \x92 \x81</p>", "text/html; charset=" + label)
    assert text == "<p>caf\u00e9 \u20ac \u2019 \x81</p>"
    assert (enc, src) == ("cp1252", "http-header")


@pytest.mark.parametrize("label", ["undefined", "unicode_escape", "raw_unicode_escape", "idna", "punycode",
                                   "utf-7", "base64", "rot13", "zlib", "replacement", "iso-2022-cn", "x-nonsense"])
def test_labels_that_name_no_charset_are_ignored(label):
    # "undefined" used to raise; unicode_escape let a page rewrite its own text (\u0041 became "A").
    assert normalize_label(label) is None
    text, enc, src = decode_body(b"<p>\\u0041 +AGEAYgBj- caf\xc3\xa9</p>", "text/html; charset=" + label)
    assert text == "<p>\\u0041 +AGEAYgBj- caf\u00e9</p>"
    assert (enc, src) == ("utf-8", "utf-8-valid")


def test_detector_guesses_decode_the_browser_way():
    assert normalize_label("euc_jis_2004") == "euc_jp"
    assert normalize_label("shift_jisx0213") == "cp932"
    assert normalize_label("cp950") == "big5hkscs"  # Python's cp950 puts kana where Big5 has ①-⑩
    assert normalize_label("iso8859_1") == "cp1252"
    assert normalize_label("latin-1") == "cp1252"


def test_whatwg_name():
    names = {"cp932": "Shift_JIS", "euc_jp": "EUC-JP", "big5hkscs": "Big5", "gb18030": "GB18030", "cp949": "EUC-KR",
             "cp1252": "windows-1252", "cp1251": "windows-1251", "cp874": "windows-874", "iso8859-2": "ISO-8859-2",
             "iso8859-15": "ISO-8859-15", "koi8-r": "KOI8-R", "koi8-u": "KOI8-U", "cp866": "IBM866",
             "mac-roman": "macintosh", "mac-cyrillic": "x-mac-cyrillic", "utf-8": "UTF-8", "utf-16-le": "UTF-16LE",
             "utf-16-be": "UTF-16BE", "iso2022_jp_ext": "ISO-2022-JP", "utf_8": "UTF-8", "latin_1": "windows-1252",
             "mac-latin2": "mac-latin2", "x-unknown": "x-unknown"}
    assert {enc: decode.whatwg_name(enc) for enc in names} == names
    # what decode_body returns names the charset of the original bytes
    for page, ctype, name in [(b"\xef\xbb\xbfhi", None, "UTF-8"), (b"\xff\xfeh\x00i\x00", None, "UTF-16LE"),
                              ("<p>Ελληνικά</p>".encode("iso8859_7"), "text/html; charset=greek", "ISO-8859-7"),
                              (b"<p>" + _ej("日本語") + b"</p>", "text/html; charset=x-euc-jp", "EUC-JP"),
                              (b"<p>\x93Hi\x94</p>", "text/html; charset=l1", "windows-1252")]:
        assert decode.whatwg_name(decode_body(page, ctype)[1]) == name


# --- A declared charset with a few bad bytes stays, as in browsers --------------------------

def test_utf8_page_with_a_stray_byte_stays_utf8():
    page = ("<p>Die Bürger in München können Anträge stellen.</p>" * 12 + "<p>It\x92s</p>").encode("utf-8")
    page = page.replace(b"\xc2\x92", b"\x92")  # one cp1252 apostrophe in a UTF-8 page
    text, enc, src = decode_body(page, "text/html; charset=utf-8")
    assert (enc, src) == ("utf-8", "http-header-lossy")
    assert "Die Bürger in München können Anträge stellen." in text and text.count(FFFD) == 1


@pytest.mark.parametrize("tail,name", [(b"\xf0", "One-byte"), (b"\xf0\x9f", "Two-byte"),
                                       (b"\xf0\x9f\x92", "Three-byte")])
def test_utf8_page_truncated_at_the_end(tail, name):
    # web-platform-tests encoding/eof-utf-8-{one,two,three}.html
    page = (b"<!doctype html>\n<meta charset=utf-8>\n<title>UTF-8 file ending with a truncated sequence</title>\n"
            + name.encode() + b" truncated sequence:" + tail)
    text, enc, src = decode_body(page, "application/octet-stream")
    assert text.endswith(" truncated sequence:" + FFFD)
    assert (enc, src) == ("utf-8", "meta-lossy")


def test_bad_pair_in_a_declared_eucjp_page_costs_one_replacement_character():
    page = b'<meta charset="euc-jp"><p>' + _ej("日本語のページです。") * 3 + b"\xa9\xa1" + _ej("続き") + b"</p>"
    text, enc, src = decode_body(page, None)
    assert (enc, src) == ("euc_jp", "meta-lossy")
    assert "日本語のページです。" + FFFD + "続き</p>" in text


def test_literal_replacement_characters_are_not_errors():
    page = ("<p>" + ("Kaputt: " + FFFD + ". ") * 30 + "Grüße " * 10 + "</p>").encode("utf-8") + b"\x92\x92\x92"
    text, enc, src = decode_body(page, "text/html; charset=utf-8")
    assert (enc, src) == ("utf-8", "http-header-lossy")
    assert text.count(FFFD) == 33


def test_pervasive_errors_still_mean_the_declaration_is_wrong():
    cyr = ("<html><body>" + "Привет, мир! Это тестовая страница. " * 20 + "</body></html>").encode("cp1251")
    text, enc, src = decode_body(cyr, "text/html; charset=utf-8")
    assert "Привет, мир!" in text and (enc, src) == ("cp1251", "detected")
    sjis = ('<html><head><meta charset="Shift_JIS"></head><body><p>' + "日本語のページです。" * 30
            + "</p></body></html>").encode("cp932")
    text, enc, src = decode_body(sjis, "text/html; charset=utf-8")
    assert "日本語のページです。" in text and (enc, src) == ("cp932", "meta")
