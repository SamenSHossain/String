"""Turn response bytes into text the way a browser would, with a statistical fallback.

Order: byte-order mark, then the HTTP Content-Type charset, then a <meta> prescan of the
first bytes, then strict UTF-8, then charset-normalizer. Labels are mapped to the
supersets browsers actually use (Shift_JIS -> cp932, GB2312/GBK -> GB18030, ...), which
avoids mojibake on legacy Japanese, Chinese, Korean and Cyrillic pages.
"""

from __future__ import annotations

import codecs
import re

from charset_normalizer import from_bytes

_META_RE = re.compile(rb"""<meta[^>]{0,512}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]{1,40})""", re.I)
_CT_RE = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]{1,40})""", re.I)

# WHATWG Encoding Standard label mapping, reduced to the cases that matter in practice.
_SUPERSETS = {
    "ascii": "cp1252",
    "us-ascii": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "iso_8859-1": "cp1252",
    "iso-8859-9": "cp1254",
    "iso-8859-11": "cp874",
    "tis-620": "cp874",
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "ms_kanji": "cp932",
    "windows-31j": "cp932",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "euc-cn": "gb18030",
    "euc-kr": "cp949",
    "ks_c_5601-1987": "cp949",
    "big5": "big5hkscs",
    "x-x-big5": "big5hkscs",
    "utf8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
    # A document that claims UTF-16 in a <meta> tag is decoded as UTF-8 (WHATWG rule);
    # real UTF-16 documents are caught by their BOM before this mapping is used.
    "utf-16": "utf-8",
    "utf-16le": "utf-8",
    "utf-16be": "utf-8",
}

_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def normalize_label(label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().strip("\"'").lower()
    key = _SUPERSETS.get(key, key)
    try:
        return codecs.lookup(key).name
    except LookupError:
        return None


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
            return data[len(bom):].decode(enc, errors="replace"), enc, "bom"

    candidates: list[tuple[str, str]] = []
    header = charset_from_content_type(content_type)
    if header:
        candidates.append((header, "http-header"))
    meta = charset_from_meta(data[:8192])
    if meta and meta not in {c for c, _ in candidates}:
        candidates.append((meta, "meta"))

    for enc, source in candidates:
        try:
            return data.decode(enc), enc, source
        except (UnicodeDecodeError, LookupError):
            continue  # declared charset is wrong; fall through to the next rule

    try:
        return data.decode("utf-8"), "utf-8", "utf-8-valid"
    except UnicodeDecodeError:
        pass

    best = from_bytes(data[:500_000]).best()
    if best is not None and best.encoding:
        enc = normalize_label(best.encoding) or best.encoding
        try:
            return data.decode(enc, errors="replace"), enc, "detected"
        except LookupError:
            pass

    if candidates:
        enc = candidates[0][0]
        return data.decode(enc, errors="replace"), enc, candidates[0][1] + "-lossy"
    return data.decode("cp1252", errors="replace"), "cp1252", "fallback"
