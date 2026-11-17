"""HTML -> Markdown / text / metadata / embedded structured data."""

from __future__ import annotations

import json
import math
import re
from itertools import accumulate
from typing import Any
from urllib.parse import urljoin

import html_to_markdown as h2m
from html_to_markdown import ConversionOptions, PreprocessingOptions
from selectolax.lexbor import LexborHTMLParser

_INVISIBLE = ["script", "style", "noscript", "template", "svg", "iframe", "object", "head"]
_WS = re.compile(r"\s+")
# Browsers strip leading/trailing C0 controls and spaces from an href or src, and drop
# tabs and newlines inside it, before resolving it (URL standard).
_URL_STRIP = "".join(map(chr, range(0x21)))
_URL_TAB_NL = str.maketrans("", "", "\t\n\r")
_NOT_RESOLVED = ("javascript:", "data:", "mailto:", "tel:")
# Consent banners and similar overlays that survive in fetched HTML.
_OVERLAY_SELECTORS = [
    "#onetrust-consent-sdk", "#onetrust-banner-sdk", "#CybotCookiebotDialog", "#usercentrics-root",
    "#didomi-host", ".didomi-popup", "#sp_message_container", "[id^='sp_message_container']",
    "#qc-cmp2-container", ".fc-consent-root", "#truste-consent-track", "#cookie-banner",
    ".cookie-banner", "#cookieConsent", ".cc-window", "#pg-shadow-host-dom",
]


def parse(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html or "")


def visible_text(tree: LexborHTMLParser) -> str:
    """Text a reader would see: no scripts, styles or head content, whitespace collapsed."""
    body = tree.body
    if body is None:
        return ""
    clone = LexborHTMLParser(body.html or "")
    clone.strip_tags(_INVISIBLE)
    node = clone.body
    return _WS.sub(" ", node.text(separator=" ") if node is not None else "").strip()


def _resolve(base: str, value: str) -> str | None:
    """``value`` resolved against ``base`` the way a browser resolves an href; None when it
    cannot be parsed (urljoin raises ValueError on "http://[x", and page content must never
    turn into an exception)."""
    try:
        return urljoin(base, value.strip(_URL_STRIP).translate(_URL_TAB_NL))
    except ValueError:
        return None


def _base_from(href: str | None, page_url: str) -> str:
    """The document base URL: a <base href> resolved against the page URL, else the page
    URL. A base that is not http(s) (javascript:, data:) is ignored, as browsers do."""
    base = _resolve(page_url, href) if href is not None else None
    return base if base and base.lower().startswith(("http://", "https://")) else page_url


def document_base(tree: LexborHTMLParser, page_url: str) -> str:
    """What relative links on the page resolve against: the first <base href>, else the page URL."""
    node = tree.css_first("base[href]")
    return _base_from(node.attributes.get("href") if node is not None else None, page_url)


def _meta(tree: LexborHTMLParser) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.css("meta"):
        attrs = node.attributes
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content")
        if key and content is not None and key.lower() not in out:
            out[key.lower()] = content.strip()
    return out


def page_metadata(tree: LexborHTMLParser, base_url: str) -> dict[str, Any]:
    meta = _meta(tree)
    title_node = tree.css_first("title")
    title = _WS.sub(" ", title_node.text()).strip() if title_node else ""
    canonical = None
    for link in tree.css("link[rel]"):
        rel = (link.attributes.get("rel") or "").lower().split()
        if "canonical" in rel and link.attributes.get("href"):
            # A <link href> resolves against <base> like any link; og:image below is meta
            # content, not a link, so it keeps resolving against the page URL.
            canonical = _resolve(document_base(tree, base_url), link.attributes["href"])
            break
    html_node = tree.css_first("html")
    lang = (html_node.attributes.get("lang") or "").strip() if html_node else ""
    h1 = tree.css_first("h1")

    def first(*keys: str) -> str | None:
        for k in keys:
            v = meta.get(k)
            if v:
                return v
        return None

    image = first("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    return {
        "title": title or first("og:title", "twitter:title"),
        "description": first("description", "og:description", "twitter:description"),
        "canonicalUrl": canonical,
        "language": lang or first("og:locale", "content-language", "language"),
        "siteName": first("og:site_name", "application-name"),
        "image": _resolve(base_url, image) if image else None,
        "author": first("author", "article:author", "parsely-author", "sailthru.author"),
        "publishedTime": first("article:published_time", "datepublished", "date", "pubdate", "parsely-pub-date"),
        "modifiedTime": first("article:modified_time", "datemodified", "og:updated_time"),
        "type": first("og:type"),
        "robots": first("robots"),
        "h1": _WS.sub(" ", h1.text()).strip() if h1 else None,
        "openGraph": {k: v for k, v in meta.items() if k.startswith(("og:", "article:", "product:", "book:", "video:", "music:"))},
        "twitter": {k: v for k, v in meta.items() if k.startswith("twitter:")},
    }


def _no_constant(_name: str) -> None:
    return None  # NaN / Infinity in JSON-LD: not valid JSON, and not serializable back out


def _finite(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None  # 1e999 parses to inf: not serializable either


# JSON nested deeper than this is skipped: json.loads, and anything that walks or writes
# the value, recurses once per level, so a page's [[[[...]]]] would raise RecursionError.
# Real JSON-LD nests a few levels, framework state a few dozen (BBC News' Next.js data: 16).
MAX_JSON_DEPTH = 128
_JSON_NOISE = bytes(c for c in range(256) if c not in b'"[]{}')
_JSON_STEP = {ord("["): 1, ord("{"): 1, ord("]"): -1, ord("}"): -1}
# "\ud83d" alone (a string cut mid-emoji, as JSON.stringify writes it) parses into a str
# that cannot be encoded as UTF-8, so the response could not be written. Lone surrogates
# become U+FFFD; escaped backslashes are matched as pairs so "\\ud83d" is left alone.
_U_ESCAPE = re.compile(r"\\\\"  # an escaped backslash: kept
                       r"|\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"  # a pair: kept
                       r"|\\u[dD][89a-fA-F][0-9a-fA-F]{2}")  # a lone surrogate: replaced


def _json_depth(raw: str) -> int:
    """How deep brackets nest outside strings: linear, no recursion, C-speed passes.

    On UTF-8 bytes (multi-byte sequences never contain ASCII bytes): drop escaped
    backslashes, then escaped quotes (JSON pairs escapes from the left, as replace does),
    so every quote left delimits a string; keep only quotes and brackets; drop strings.
    """
    b = raw.encode("utf-8", "replace").replace(b"\\\\", b"").replace(b'\\"', b"").translate(None, _JSON_NOISE)
    outside = b"".join(b.split(b'"')[::2])
    return max(accumulate(map(_JSON_STEP.__getitem__, outside)), default=0)


def _loads_lenient(raw: str) -> Any:
    """A page's JSON blob, or None. Never raises, and never returns what cannot be written
    back out: blobs nested deeper than MAX_JSON_DEPTH are skipped, NaN and infinities become
    null, lone surrogates U+FFFD."""
    raw = raw.strip()
    raw = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", raw)
    raw = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", raw).strip()
    if _json_depth(raw) > MAX_JSON_DEPTH:
        return None
    if "\\ud" in raw or "\\uD" in raw:
        raw = _U_ESCAPE.sub(lambda m: "\\ufffd" if len(m.group()) == 6 else m.group(), raw)
    try:
        return json.loads(raw, parse_constant=_no_constant, parse_float=_finite)
    except (ValueError, RecursionError):  # JSONDecodeError, or an integer over 4,300 digits
        pass
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw)  # raw control chars
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)  # trailing commas
    try:
        return json.loads(cleaned, strict=False, parse_constant=_no_constant, parse_float=_finite)
    except (ValueError, RecursionError):
        return None


def _flatten_ld(obj: Any, out: list[dict]) -> None:
    """Append the nodes of a JSON-LD value to ``out`` in document order: lists and @graph
    are unwrapped, and a graph's own properties follow its items. Iterative, so nesting
    cannot exhaust the stack."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, tuple):  # a graph's own properties, queued after its items
            out.append(node[0])  # (JSON never yields tuples, so the marker is unambiguous)
        elif isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            graph = node.get("@graph")
            if not isinstance(graph, list):
                out.append(node)
                continue
            ctx = {k: v for k, v in node.items() if k != "@graph"}
            if len(ctx) > 1:
                stack.append((ctx,))
            for item in reversed(graph):
                if isinstance(item, dict) and ctx and "@context" not in item:
                    item = {**item, "@context": ctx.get("@context")}
                stack.append(item)


def json_ld(tree: LexborHTMLParser) -> list[dict]:
    nodes: list[dict] = []
    for script in tree.css('script[type="application/ld+json"], script[type="application/ld+json; charset=utf-8"]'):
        data = _loads_lenient(script.text() or "")
        if data is not None:
            _flatten_ld(data, nodes)
    return nodes


def _microdata_value(node, base_url: str) -> Any:
    attrs = node.attributes
    tag = node.tag
    if "itemscope" in attrs:
        return None  # handled by the caller (nested item)
    if attrs.get("content") is not None:
        return attrs["content"].strip()
    # URL properties: absolute, against the document base; "" if unparsable (HTML spec)
    if tag in ("a", "link", "area") and attrs.get("href"):
        return _resolve(base_url, attrs["href"]) or ""
    if tag in ("img", "audio", "video", "source", "embed", "iframe", "track") and attrs.get("src"):
        return _resolve(base_url, attrs["src"]) or ""
    if tag == "time" and attrs.get("datetime"):
        return attrs["datetime"].strip()
    if tag in ("data", "meter") and attrs.get("value") is not None:
        return attrs["value"].strip()
    return _WS.sub(" ", node.text(separator=" ")).strip()


def _microdata_item(scope, base_url: str, depth: int = 0) -> dict:
    item: dict[str, Any] = {}
    itemtype = scope.attributes.get("itemtype") or ""
    if itemtype:
        item["@type"] = itemtype.rstrip("/").split("/")[-1]
    if depth > 6:
        return item
    # Walk descendants in document order, skipping those that belong to a nested itemscope.
    # A LIFO stack with children pushed in reverse keeps this O(n) (no pop(0)/prepend).
    stack = list(_children(scope))[::-1]
    while stack:
        node = stack.pop()
        attrs = node.attributes
        props = (attrs.get("itemprop") or "").split()
        if props:
            if "itemscope" in attrs:
                value: Any = _microdata_item(node, base_url, depth + 1)
            else:
                value = _microdata_value(node, base_url)
            for p in props:
                if p in item:
                    if not isinstance(item[p], list):
                        item[p] = [item[p]]
                    item[p].append(value)
                else:
                    item[p] = value
        if "itemscope" not in attrs:
            stack.extend(list(_children(node))[::-1])
    return item


def _children(node):
    child = node.child
    while child is not None:
        if child.tag and not child.tag.startswith("-"):
            yield child
        child = child.next


def _has_scope_ancestor(node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.attributes and "itemscope" in parent.attributes:
            return True
        parent = parent.parent
    return False


def microdata(tree: LexborHTMLParser, base_url: str) -> list[dict]:
    items = []
    base = document_base(tree, base_url)
    for scope in tree.css("[itemscope]"):
        if "itemprop" in scope.attributes and _has_scope_ancestor(scope):
            continue  # nested; included in its parent item
        item = _microdata_item(scope, base)
        if len(item) > 1 or (item and "@type" not in item):
            items.append(item)
        if len(items) >= 50:
            break
    return items


def hydration_data(tree: LexborHTMLParser) -> dict[str, Any]:
    """Framework state blobs that often hold the page's data (Next.js, Nuxt)."""
    out: dict[str, Any] = {}
    nxt = tree.css_first("script#__NEXT_DATA__")
    if nxt is not None:
        data = _loads_lenient(nxt.text() or "")
        if data is not None:
            out["__NEXT_DATA__"] = data
    nuxt = tree.css_first("script#__NUXT_DATA__")
    if nuxt is not None:
        data = _loads_lenient(nuxt.text() or "")
        if data is not None:
            out["__NUXT_DATA__"] = data
    return out


def links(tree: LexborHTMLParser, base_url: str, limit: int = 2000) -> list[dict[str, str]]:
    out, seen = [], set()
    base = document_base(tree, base_url)
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = _resolve(base, href)
        if url is None or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": _WS.sub(" ", a.text(separator=" ")).strip()[:200]})
        if len(out) >= limit:
            break
    return out


def to_markdown(html: str, base_url: str, mode: str = "full") -> str:
    if mode == "readable":
        md = _readable_markdown(html, base_url)
        if md:
            return md
    opts = ConversionOptions(
        extract_metadata=False,
        base_url=base_url,
        # Inline base64 images (icons, logos) can be half of a page's Markdown and are
        # useless to a reader or an LLM: keep their alt text only.
        inline_data_media="alt_text_only",
        exclude_selectors=_OVERLAY_SELECTORS,
        preprocessing=PreprocessingOptions(
            enabled=True,
            preset="standard",
            remove_navigation=(mode == "readable"),
            remove_forms=True,
        ),
    )
    return h2m.convert(html, opts).content.strip() + "\n"


def to_text(html: str) -> str:
    opts = ConversionOptions(extract_metadata=False, output_format="plain", inline_data_media="alt_text_only",
                             exclude_selectors=_OVERLAY_SELECTORS)
    return h2m.convert(html, opts).content.strip() + "\n"


def _absolutize(tree, page_url: str) -> None:
    """Resolve link and image URLs in trafilatura's (lxml) tree the way a browser would.

    trafilatura resolves hrefs against scheme://host only: ../x.html loses its directory,
    #frag points at the site root, //host/x becomes http:, and <base href> is ignored.
    Its images prefer data-src over src, so both are resolved."""
    href = next((b.get("href") for b in tree.iter("base") if b.get("href") is not None), None)
    base = _base_from(href, page_url)
    for node in tree.iter("a", "img"):
        for attr in ("href",) if node.tag == "a" else ("src", "data-src"):
            value = node.get(attr)
            if value and not value.lstrip(_URL_STRIP).lower().startswith(_NOT_RESOLVED):
                url = _resolve(base, value)
                if url:
                    node.set(attr, url)


def _readable_markdown(html: str, base_url: str) -> str | None:
    try:
        import trafilatura
        from trafilatura.utils import load_html
    except ImportError:  # pragma: no cover - optional dependency
        return None
    try:
        tree = load_html(html)  # the parse trafilatura.extract() would do on the string
    except (TypeError, ValueError):  # what trafilatura.extract() itself swallows
        return None
    if tree is None:
        return None
    _absolutize(tree, base_url)
    md = trafilatura.extract(
        tree,
        url=base_url,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
        with_metadata=False,
    )
    if md and len(md.strip()) >= 200:
        return md.strip() + "\n"
    return None
