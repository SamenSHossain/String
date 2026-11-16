"""HTML -> Markdown / text / metadata / embedded structured data."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

import html_to_markdown as h2m
from html_to_markdown import ConversionOptions, PreprocessingOptions
from selectolax.lexbor import LexborHTMLParser

_INVISIBLE = ["script", "style", "noscript", "template", "svg", "iframe", "object", "head"]
_WS = re.compile(r"\s+")
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
            canonical = urljoin(base_url, link.attributes["href"].strip())
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
        "image": urljoin(base_url, image) if image else None,
        "author": first("author", "article:author", "parsely-author", "sailthru.author"),
        "publishedTime": first("article:published_time", "datepublished", "date", "pubdate", "parsely-pub-date"),
        "modifiedTime": first("article:modified_time", "datemodified", "og:updated_time"),
        "type": first("og:type"),
        "robots": first("robots"),
        "h1": _WS.sub(" ", h1.text()).strip() if h1 else None,
        "openGraph": {k: v for k, v in meta.items() if k.startswith(("og:", "article:", "product:", "book:", "video:", "music:"))},
        "twitter": {k: v for k, v in meta.items() if k.startswith("twitter:")},
    }


def _loads_lenient(raw: str) -> Any:
    raw = raw.strip()
    raw = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", raw)
    raw = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw)  # raw control chars
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)  # trailing commas
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        return None


def _flatten_ld(obj: Any, out: list[dict]) -> None:
    if isinstance(obj, list):
        for item in obj:
            _flatten_ld(item, out)
    elif isinstance(obj, dict):
        graph = obj.get("@graph")
        if isinstance(graph, list):
            ctx = {k: v for k, v in obj.items() if k != "@graph"}
            for item in graph:
                if isinstance(item, dict) and ctx and "@context" not in item:
                    item = {**item, "@context": ctx.get("@context")}
                _flatten_ld(item, out)
            if len(ctx) > 1:
                out.append(ctx)
        else:
            out.append(obj)


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
    if tag in ("a", "link", "area") and attrs.get("href"):
        return urljoin(base_url, attrs["href"])
    if tag in ("img", "audio", "video", "source", "embed", "iframe", "track") and attrs.get("src"):
        return urljoin(base_url, attrs["src"])
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
    for scope in tree.css("[itemscope]"):
        if "itemprop" in scope.attributes and _has_scope_ancestor(scope):
            continue  # nested; included in its parent item
        item = _microdata_item(scope, base_url)
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
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        if url in seen:
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


def _readable_markdown(html: str, base_url: str) -> str | None:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover - optional dependency
        return None
    md = trafilatura.extract(
        html,
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
