"""Fill a caller's JSON Schema from data the page already carries.

Sources, in priority order: curated site extractors, JSON-LD, microdata, meta tags
(OpenGraph/Twitter/standard), then a few DOM facts (title, h1, lang, canonical).
Every value is returned with the place it was read from; a field without a source stays
``null`` and is listed in ``missing``. Nothing is generated, so nothing is hallucinated,
and the whole pass takes milliseconds instead of an LLM round trip.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

_NORM = re.compile(r"[^a-z0-9]")


def norm(key: str) -> str:
    return _NORM.sub("", key.lower())


# Property-name synonyms -> concept.
_SYNONYMS: dict[str, str] = {}
for concept, words in {
    "name": "name title headline heading producttitle productname gametitle gamename appname articletitle pagetitle itemname",
    "description": "description desc summary abstract blurb about overview excerpt snippet shortdescription",
    "price": "price cost amount currentprice saleprice finalprice priceusd pricevalue priceamount",
    "currency": "currency pricecurrency currencycode",
    "image": "image img imageurl thumbnail thumbnailurl picture photo cover coverimage mainimage headerimage",
    "url": "url link href canonical canonicalurl pageurl permalink",
    "author": "author authors byline writer writtenby journalist",
    "datepublished": "date published publishdate publisheddate publicationdate datepublished publishedat pubdate releasedate released release releasedon",
    "datemodified": "updated modified lastmodified datemodified updatedat lastupdated",
    "rating": "rating stars score averagerating ratingvalue",
    "reviewcount": "reviews reviewcount numreviews ratingcount numberofreviews totalreviews",
    "reviewsummary": "reviewsummary userreviews reviewscore",
    "brand": "brand manufacturer make",
    "availability": "availability instock stock stockstatus available",
    "sku": "sku productid mpn gtin ean upc isbn",
    "language": "language lang locale inlanguage",
    "sitename": "site sitename source outlet publication website",
    "keywords": "keywords tags topics labels usertags",
    "category": "category categories section genre genres articlesection",
    "developer": "developer developers studio",
    "publisher": "publisher publishers",
}.items():
    for w in words.split():
        _SYNONYMS.setdefault(w, concept)

def _concept(p: str) -> str | None:
    """Concept for a normalized property name, tolerating plurals (images -> image)."""
    if p in _SYNONYMS:
        return _SYNONYMS[p]
    for suffix in ("es", "s"):
        if p.endswith(suffix) and p[: -len(suffix)] in _SYNONYMS:
            return _SYNONYMS[p[: -len(suffix)]]
    return None


# Concept -> candidate keys (normalized), most specific first.
_CONCEPT_KEYS: dict[str, list[str]] = {
    "name": ["name", "headline", "title", "ogtitle", "twittertitle", "h1", "pagetitle"],
    "description": ["description", "ogdescription", "twitterdescription", "abstract", "summary"],
    "price": ["price", "lowprice", "priceamount", "productpriceamount", "ogpriceamount", "highprice"],
    "currency": ["pricecurrency", "currency", "productpricecurrency", "ogpricecurrency"],
    "image": ["image", "headerimage", "ogimage", "twitterimage", "thumbnailurl", "contenturl"],
    "url": ["canonical", "url", "ogurl"],
    "author": ["author", "creator", "byline", "articleauthor"],
    "datepublished": ["datepublished", "releasedate", "articlepublishedtime", "publishedtime", "datecreated", "uploaddate", "pubdate", "date"],
    "datemodified": ["datemodified", "articlemodifiedtime", "modifiedtime", "ogupdatedtime"],
    "rating": ["ratingvalue"],
    "reviewcount": ["reviewcount", "ratingcount"],
    "reviewsummary": ["reviewsummary"],
    "brand": ["brand", "manufacturer"],
    "availability": ["availability"],
    "sku": ["sku", "gtin13", "gtin12", "gtin14", "gtin8", "gtin", "mpn", "productid", "isbn"],
    "language": ["inlanguage", "lang", "language", "oglocale"],
    "sitename": ["ogsitename", "sitename", "publisher"],
    "keywords": ["keywords", "tags", "articletag", "newskeywords"],
    "category": ["genre", "genres", "articlesection", "category", "applicationcategory"],
    "developer": ["developer", "developers"],
    "publisher": ["publisher", "publishers"],
}

_SOURCE_RANK = {"site": 0, "json-ld": 1, "microdata": 2, "meta": 3, "dom": 4}
_MAIN_TYPES = {
    "product", "newsarticle", "article", "blogposting", "reportagenewsarticle", "analysisnewsarticle",
    "recipe", "jobposting", "event", "videogame", "softwareapplication", "mobileapplication", "book",
    "movie", "tvseries", "course", "localbusiness", "restaurant", "hotel", "review", "videoobject",
    "musicrecording", "musicalbum", "podcastepisode", "offer", "realestatelisting", "vehicle", "car",
    "place", "howto", "dataset", "scholarlyarticle", "liveblogposting",
}
_BOILERPLATE_TYPES = {"website", "organization", "breadcrumblist", "webpage", "sitenavigationelement",
                      "imageobject", "searchaction", "wpheader", "wpfooter", "listitem", "collectionpage"}


@dataclass
class Cand:
    key: str  # normalized last path segment
    path: str
    value: Any
    source: str  # site | json-ld | microdata | meta | dom
    entity: int  # 0 main entity, 1 other, 2 boilerplate
    depth: int

    @property
    def label(self) -> str:
        return f"{self.source}:{self.path}"


def _types_of(node: dict) -> list[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def _entity_rank(types: list[str]) -> int:
    low = {t.lower() for t in types}
    if low & _MAIN_TYPES:
        return 0
    if low and low <= _BOILERPLATE_TYPES:
        return 2
    return 1


def _walk(node: Any, path: str, source: str, entity: int, depth: int, out: list[Cand]) -> None:
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("@context",) or (k.startswith("@") and k not in ("@id", "@value")):
                continue
            p = f"{path}.{k}" if path else k
            out.append(Cand(norm(k), p, v, source, entity, depth))
            if isinstance(v, (dict, list)):
                _walk(v, p, source, entity, depth + 1, out)
    elif isinstance(node, list):
        for item in node[:200]:
            _walk(item, path, source, entity, depth, out)


def build_candidates(
    *,
    jsonld: list[dict],
    microdata: list[dict],
    metadata: dict[str, Any],
    site: dict[str, tuple[Any, str]] | None = None,
) -> list[Cand]:
    out: list[Cand] = []
    for key, (value, path) in (site or {}).items():
        out.append(Cand(norm(key), path, value, "site", 0, 0))
    for source, nodes in (("json-ld", jsonld), ("microdata", microdata)):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = _types_of(node)
            label = types[0] if types else "Thing"
            _walk(node, label, source, _entity_rank(types), 0, out)
    for key, value in (metadata.get("openGraph") or {}).items():
        out.append(Cand(norm(key.split(":", 1)[-1]) if key.startswith("og:") else norm(key), key, value, "meta", 1, 0))
        out.append(Cand(norm(key), key, value, "meta", 1, 0))
    for key, value in (metadata.get("twitter") or {}).items():
        out.append(Cand(norm(key), key, value, "meta", 1, 0))
    for field_name, key in (("title", "title"), ("description", "description"), ("author", "author"),
                            ("publishedTime", "publishedtime"), ("modifiedTime", "modifiedtime"),
                            ("siteName", "sitename"), ("image", "image")):
        v = metadata.get(field_name)
        if v:
            out.append(Cand(key, f"meta:{field_name}", v, "meta", 1, 0))
    for field_name, key in (("h1", "h1"), ("language", "lang"), ("canonicalUrl", "canonical"), ("title", "pagetitle")):
        v = metadata.get(field_name)
        if v:
            out.append(Cand(key, field_name, v, "dom", 1, 0))
    return out


# --------------------------------------------------------------------------- coercion

_NUM_RE = re.compile(r"[-+]?\d[\d.,'   ]*")
_DATE_FORMATS = ("%Y-%m-%d", "%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                 "%b %Y", "%B %Y", "%d.%m.%Y", "%m/%d/%Y")
_IN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale", "backorder")
_OUT_STOCK = ("outofstock", "soldout", "discontinued")


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.lower() in {"free", "free to play", "gratis", "kostenlos", "gratuit"}:
        return 0.0
    m = _NUM_RE.search(s)
    if not m:
        return None
    raw = re.sub(r"[   ']", "", m.group(0)).rstrip(".,")
    if "," in raw and "." in raw:
        dec = "," if raw.rfind(",") > raw.rfind(".") else "."
        thou = "." if dec == "," else ","
        raw = raw.replace(thou, "").replace(dec, ".")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[1]
        raw = raw.replace(",", ".") if raw.count(",") == 1 and len(tail) in (1, 2) else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _to_text(value: Any, join: bool) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for k in ("name", "@value", "text", "url", "contentUrl", "@id"):
            if isinstance(value.get(k), (str, int, float)):
                return str(value[k]).strip() or None
        return None
    if isinstance(value, list):
        parts = [t for t in (_to_text(v, False) for v in value) if t]
        if not parts:
            return None
        return ", ".join(dict.fromkeys(parts)) if join else parts[0]
    return None


def _to_date(text: str, fmt: str) -> str:
    s = text.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10] if fmt == "date" else s
    for f in _DATE_FORMATS:
        try:
            d = _dt.datetime.strptime(s, f)
            return d.date().isoformat() if fmt == "date" or "%H" not in f else d.isoformat()
        except ValueError:
            continue
    return s


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    t = _to_text(value, False)
    if t is None:
        return None
    low = norm(t.rsplit("/", 1)[-1])
    if low in _IN_STOCK or low in {"true", "yes", "1", "available"}:
        return True
    if low in _OUT_STOCK or low in {"false", "no", "0", "unavailable"}:
        return False
    return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and "," in value and len(value) < 500:
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


# --------------------------------------------------------------------------- filling

class SchemaFiller:
    def __init__(self, cands: list[Cand], base_url: str):
        self.cands = cands
        self.base_url = base_url
        self.sources: dict[str, str] = {}
        self.missing: list[str] = []

    def _ranked(self, prop: str, description: str = "", pool: list[Cand] | None = None) -> list[Cand]:
        pool = self.cands if pool is None else pool
        p = norm(prop)
        concept = _concept(p)
        if concept is None and description:
            for word in re.findall(r"[a-z]+", description.lower()):
                if word in _SYNONYMS:
                    concept = _SYNONYMS[word]
                    break
        keys = _CONCEPT_KEYS.get(concept or "", [])
        scored = []
        for c in pool:
            if c.key == p:
                kind = 0
            elif c.key in keys:
                kind = 1 + keys.index(c.key)
            else:
                continue
            scored.append(((kind if c.source != "site" else -1, _SOURCE_RANK[c.source], c.entity, c.depth), c))
        scored.sort(key=lambda x: x[0])
        return [c for _, c in scored]

    def fill(self, schema: dict, prop: str = "", path: str = "", pool: list[Cand] | None = None,
             description: str = "") -> Any:
        stype = schema.get("type")
        if isinstance(stype, list):
            stype = next((t for t in stype if t != "null"), None)
        if stype == "object" or (stype is None and isinstance(schema.get("properties"), dict)):
            return self._fill_object(schema, path, pool)
        if not prop:
            return None
        ranked = self._ranked(prop, description or str(schema.get("description") or ""), pool)
        if stype == "array":
            return self._fill_array(schema, prop, path, ranked)
        for c in ranked:
            value = self._coerce(schema, stype, c.value, prop)
            if value is not None:
                self.sources[path] = c.label
                return value
        self.missing.append(path)
        return None

    def _fill_object(self, schema: dict, path: str, pool: list[Cand] | None) -> dict:
        out = {}
        for name, sub in (schema.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            sub_path = f"{path}.{name}" if path else name
            sub_type = sub.get("type")
            if sub_type == "object" or isinstance(sub.get("properties"), dict):
                scoped = self._scoped_pool(name, pool)
                out[name] = self._fill_object(sub, sub_path, scoped or pool)
            else:
                out[name] = self.fill(sub, name, sub_path, pool, str(sub.get("description") or ""))
        return out

    def _scoped_pool(self, name: str, pool: list[Cand] | None) -> list[Cand] | None:
        """Candidates read from inside the JSON-LD/microdata object whose key matches ``name``."""
        for c in self._ranked(name, "", pool):
            if isinstance(c.value, dict):
                sub: list[Cand] = []
                _walk(c.value, c.path, c.source, c.entity, c.depth + 1, sub)
                return sub
            if isinstance(c.value, list) and c.value and isinstance(c.value[0], dict):
                sub = []
                _walk(c.value[0], c.path, c.source, c.entity, c.depth + 1, sub)
                return sub
        return None

    def _fill_array(self, schema: dict, prop: str, path: str, ranked: list[Cand]) -> list | None:
        items_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        item_type = items_schema.get("type")
        for c in ranked:
            values = _as_list(c.value)
            out = []
            for v in values:
                if item_type == "object" or isinstance(items_schema.get("properties"), dict):
                    if isinstance(v, dict):
                        sub: list[Cand] = []
                        _walk(v, c.path, c.source, c.entity, c.depth + 1, sub)
                        sub_filler = SchemaFiller(sub, self.base_url)
                        out.append(sub_filler._fill_object(items_schema, "", sub))
                    continue
                coerced = self._coerce(items_schema, item_type, v, prop)
                if coerced is not None and coerced not in out:
                    out.append(coerced)
            if out:
                max_items = schema.get("maxItems")
                self.sources[path] = c.label
                return out[: int(max_items)] if isinstance(max_items, int) else out
        self.missing.append(path)
        return None

    def _coerce(self, schema: dict, stype: str | None, value: Any, prop: str) -> Any:
        fmt = schema.get("format") or ""
        concept = _concept(norm(prop))
        if stype in ("number", "integer"):
            if isinstance(value, dict):
                for k in ("price", "lowPrice", "value", "ratingValue", "reviewCount", "ratingCount", "amount"):
                    if k in value:
                        value = value[k]
                        break
            if isinstance(value, list):
                value = next((v for v in value if parse_number(v) is not None), None)
            n = parse_number(value)
            if n is None:
                return None
            return int(round(n)) if stype == "integer" else n
        if stype == "boolean":
            return _to_bool(value)
        text = _to_text(value, join=concept in {"author", "keywords", "category", "developer", "publisher"})
        if text is None:
            return None
        if fmt in ("uri", "url", "iri") or concept in {"image", "url"}:
            if not text.startswith(("http://", "https://", "data:")):
                text = urljoin(self.base_url, text)
        elif fmt in ("date", "date-time"):
            text = _to_date(text, fmt)
        elif concept == "availability":
            text = text.rsplit("/", 1)[-1]
        enum = schema.get("enum")
        if isinstance(enum, list) and text not in enum:
            match = next((e for e in enum if isinstance(e, str) and e.lower() == text.lower()), None)
            if match is None:
                return None
            text = match
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(text) > max_len:
            text = text[:max_len]
        return text


def fill_schema(schema: dict, cands: list[Cand], base_url: str) -> dict[str, Any]:
    filler = SchemaFiller(cands, base_url)
    data = filler.fill(schema)
    total = len(filler.sources) + len(filler.missing)
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    missing_required = [r for r in required if isinstance(data, dict) and data.get(r) in (None, [], "")]
    return {
        "data": data,
        "sources": filler.sources,
        "missing": filler.missing,
        "missingRequired": missing_required,
        "coverage": round(len(filler.sources) / total, 3) if total else 0.0,
        "method": "deterministic",
    }


# --------------------------------------------------------------------------- site extractors

SiteExtractor = Callable[[LexborHTMLParser, str], dict[str, tuple[Any, str]]]


def _txt(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip() if node is not None else ""


def steam_extractor(tree: LexborHTMLParser, url: str) -> dict[str, tuple[Any, str]]:
    out: dict[str, tuple[Any, str]] = {}
    name = tree.css_first("#appHubAppName") or tree.css_first(".apphub_AppName")
    if _txt(name):
        out["name"] = (_txt(name), "steam:#appHubAppName")
    price_node = tree.css_first(".game_area_purchase_game_wrapper [data-price-final], .game_area_purchase_game [data-price-final]")
    if price_node is not None and price_node.attributes.get("data-price-final", "").isdigit():
        out["price"] = (int(price_node.attributes["data-price-final"]) / 100.0, "steam:[data-price-final]")
    else:
        p = tree.css_first(".game_area_purchase_game .discount_final_price, .game_area_purchase_game .game_purchase_price")
        if _txt(p):
            out["price"] = (_txt(p), "steam:.game_purchase_price")
    p_text = tree.css_first(".game_area_purchase_game .discount_final_price, .game_area_purchase_game .game_purchase_price")
    if _txt(p_text):
        out["pricetext"] = (_txt(p_text), "steam:.game_purchase_price")
    date = tree.css_first(".release_date .date")
    if _txt(date):
        out["releasedate"] = (_txt(date), "steam:.release_date .date")
    devs = [_txt(a) for a in tree.css("#developers_list a") if _txt(a)]
    if devs:
        out["developer"] = (devs, "steam:#developers_list")
    for row in tree.css(".dev_row"):
        label = _txt(row.css_first(".subtitle")).rstrip(":").lower()
        names = [_txt(a) for a in row.css(".summary a") if _txt(a)]
        if label.startswith("publisher") and names:
            out["publisher"] = (names, "steam:.dev_row(Publisher)")
        elif label.startswith("developer") and names and "developer" not in out:
            out["developer"] = (names, "steam:.dev_row(Developer)")
    tags = [_txt(a) for a in tree.css(".glance_tags a.app_tag") if _txt(a)]
    if tags:
        out["tags"] = (tags, "steam:.glance_tags")
    genres = [_txt(a) for a in tree.css("#genresAndManufacturer a[href*='/genre/']") if _txt(a)]
    if genres:
        out["genre"] = (genres, "steam:#genresAndManufacturer")
    desc = tree.css_first(".game_description_snippet")
    if _txt(desc):
        out["description"] = (_txt(desc), "steam:.game_description_snippet")
    rows = tree.css(".user_reviews_summary_row")
    # Steam shows "Recent Reviews" (last 30 days) before "All Reviews"; prefer the latter.
    def _is_all(row) -> bool:
        tip = row.attributes.get("data-tooltip-html") or ""
        label = _txt(row.css_first(".subtitle")).lower()
        return "all reviews" in label or "for this game" in tip
    ordered = sorted(rows, key=lambda r: 0 if _is_all(r) else 1)
    for row in ordered:
        summary = row.css_first(".game_review_summary")
        tip = row.attributes.get("data-tooltip-html") or ""
        m = re.search(r"([\d,.]+)%\s+of the\s+([\d,.]+)\s+user reviews", tip)
        if _txt(summary) and "reviewsummary" not in out:
            out["reviewsummary"] = (_txt(summary), "steam:All Reviews .game_review_summary")
        if m and "reviewcount" not in out:
            out["rating"] = (parse_number(m.group(1)), "steam:All Reviews @data-tooltip-html")
            out["reviewcount"] = (parse_number(m.group(2)), "steam:All Reviews @data-tooltip-html")
    img = tree.css_first("img.game_header_image_full")
    if img is not None and img.attributes.get("src"):
        out["headerimage"] = (img.attributes["src"], "steam:img.game_header_image_full")
    return out


SITE_EXTRACTORS: dict[str, SiteExtractor] = {"store.steampowered.com": steam_extractor}


def site_candidates(tree: LexborHTMLParser, url: str) -> dict[str, tuple[Any, str]]:
    host = (urlsplit(url).hostname or "").lower()
    fn = SITE_EXTRACTORS.get(host)
    if fn is None:
        return {}
    try:
        return fn(tree, url)
    except Exception:  # a site redesign must never break the response
        return {}
