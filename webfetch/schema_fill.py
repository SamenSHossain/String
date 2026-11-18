"""Fill a caller's JSON Schema from data the page already carries.

Sources, in priority order: curated site extractors, JSON-LD, microdata, meta tags
(OpenGraph/Twitter/standard), then a few DOM facts (title, h1, lang, canonical).
Every value is returned with the place it was read from; a field without a source stays
``null`` and is listed in ``missing``. Nothing is generated, so nothing is hallucinated,
and the whole pass takes milliseconds instead of an LLM round trip.
"""

from __future__ import annotations

import copy
import datetime as _dt
import math
import re
from dataclasses import dataclass
from itertools import islice
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
    # Identifiers are separate concepts: a shop's SKU is not an ISBN, a UPC or an EAN.
    "sku": "sku productid",
    "mpn": "mpn",
    "gtin": "gtin barcode",
    "ean": "ean ean13",
    "upc": "upc",
    "isbn": "isbn",
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
    "sku": ["sku", "productid"],
    "mpn": ["mpn"],
    "gtin": ["gtin13", "gtin12", "gtin14", "gtin8", "gtin"],
    "ean": ["ean", "gtin13"],  # EAN-13 is GTIN-13
    "upc": ["upc", "gtin12"],  # UPC-A is GTIN-12
    "isbn": ["isbn"],
    "language": ["inlanguage", "lang", "language", "oglocale"],
    "sitename": ["ogsitename", "sitename", "publisher"],
    "keywords": ["keywords", "tags", "articletag", "newskeywords"],
    "category": ["genre", "genres", "articlesection", "category", "applicationcategory"],
    "developer": ["developer", "developers"],
    "publisher": ["publisher", "publishers"],
}
# An identifier is only ever read from the entity itself, never from one nested in it.
_IDENTIFIERS = {"sku", "mpn", "gtin", "ean", "upc", "isbn"}
_IDENTIFIER_KEYS = {k for c in _IDENTIFIERS for k in _CONCEPT_KEYS[c]}

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
# Broader names a caller may give the page's main entity (an "article" field on a BlogPosting).
_TYPE_ALIASES = {
    "blogposting": ("article", "post"), "liveblogposting": ("article", "post"), "jobposting": ("job",),
    "videoobject": ("video",), "softwareapplication": ("app", "software"), "mobileapplication": ("app",),
    "vehicle": ("product",), "car": ("product", "vehicle"), "musicrecording": ("song", "track"),
    "tvseries": ("show",), "restaurant": ("business",), "hotel": ("business",),
}
# What one page can cost, whatever it carries.
_MAX_CANDS = 50_000  # candidates per page: a dense @id graph or a huge JSON-LD blob stops here
_MAX_STEPS = 10 * _MAX_CANDS  # page values visited to collect them (most lists yield one candidate, or none)
_LIST_ITEMS = 200  # items read from one list
_KEY_CHARS = 100  # a longer key or @type names no property; every path below it would repeat it
_NODE_TYPES = 20  # @types read from one node (a real one has one to three)
_META_TAGS = 200  # OpenGraph and Twitter tags read, each
_MAIN_TYPES_READ = 32  # types of the main entity a wrapper's name is compared with
_MAX_NESTING = 12  # object/array levels filled; a self-referencing model stops here at the latest
# Budgets of one fill_schema call. The limits on a schema (keys, depth, bytes) do not bound the
# work it asks for: $refs fan out (f properties over 12 levels are f^12 members), the page
# multiplies array items, and one long page value can be asked for under 200 names.
_MAX_FIELDS = 10_000  # schema members filled: properties and array items
_MAX_WORK = 100_000  # candidates ranked, page values walked, list items and (per 32 chars) text coerced
_MAX_CHARS = 1_000_000  # characters returned: values, keys, and the paths in sources and missing


@dataclass(slots=True)
class Cand:
    key: str  # normalized last path segment
    path: str
    value: Any
    source: str  # site | json-ld | microdata | meta | dom
    entity: int  # 0 main entity, 1 page-level, 2 other entity, 3 boilerplate
    depth: int
    base: int = 0  # leading path segments that name the scope it was read in (not foreign)
    _inside: frozenset | None = None

    @property
    def label(self) -> str:
        return f"{self.source}:{self.path}"

    @property
    def inside(self) -> frozenset:
        """The normalized keys this value sits under, within its scope. Kept: a candidate is
        ranked once per member that asks for its key."""
        if self._inside is None:
            self._inside = frozenset(norm(seg) for seg in self.path.split(".")[max(1, self.base):-1])
        return self._inside


class Pool:
    """The candidates of one scope, by key: a lookup reads the ones that can match, not the page."""
    __slots__ = ("by_key", "text")

    def __init__(self, cands: list[Cand], text: bool = False):
        self.by_key: dict[str, list[Cand]] = {}
        for c in cands:
            self.by_key.setdefault(c.key, []).append(c)
        self.text = text  # the scope is one plain string, standing in for an object as its name


def _types_of(node: dict) -> list[str]:
    t = node.get("@type")
    t = [t] if isinstance(t, str) else t if isinstance(t, list) else []
    return [x for x in t[:_NODE_TYPES] if isinstance(x, str) and len(x) <= _KEY_CHARS]


def _entity_rank(types: list[str]) -> int:
    """0 = the page's main entity (Product, Article, ...); 1 is reserved for page-level
    facts (canonical URL, OpenGraph, title); 2 = other entities; 3 = site boilerplate."""
    low = {t.lower() for t in types}
    if low & _MAIN_TYPES:
        return 0
    if low and low <= _BOILERPLATE_TYPES:
        return 3
    return 2


# Keys that lead into a *different* entity (the author, the publisher, a review...). A
# generic field such as "name" or "url" found under them describes that other entity, so
# it ranks below the page's own value unless the requested field is about that entity.
_FOREIGN = {"author", "creator", "publisher", "brand", "manufacturer", "review", "reviews", "ispartof",
            "mainentityofpage", "breadcrumb", "itemlistelement", "provider", "seller", "sourceorganization",
            "copyrightholder", "contributor", "editor", "logo", "potentialaction", "about", "mentions",
            "hasmap", "subjectof", "workexample", "parentorganization", "founder", "employee", "member",
            "image", "thumbnail", "thumbnailurl", "video", "audio", "associatedmedia", "primaryimageofpage",
            "isrelatedto", "issimilarto", "isaccessoryorsparepartfor", "isconsumablefor", "hasvariant",
            "isvariantof"}


def _ref_id(v: Any) -> str | None:
    """The @id of a pure reference ({"@id": ...}, optionally with "@type"), else None."""
    if isinstance(v, dict) and isinstance(v.get("@id"), str) and set(v) <= {"@id", "@type"}:
        return v["@id"]
    return None


def _deref(v: Any, index: dict[str, dict], seen: frozenset) -> tuple[Any, frozenset]:
    """Replace a reference to another @graph node, or a list of them (Yoast's
    "author": [{"@id": ...}]), with the node itself. ``seen`` holds the @ids already expanded
    on the way here, so a reference cycle stops instead of looping.

    A list is read as far as the walk reads it, and a node it names twice is one node: 1,000
    references to a node that holds 1,000 more would be a million items to walk and to fill."""
    if isinstance(v, dict):
        ref = _ref_id(v)
        return (index[ref], seen | {ref}) if ref in index and ref not in seen else (v, seen)
    out, ids = [], set()
    for item in v[:_LIST_ITEMS]:
        ref = _ref_id(item)
        if ref not in index or ref in seen:
            out.append(item)
        elif ref not in ids:
            ids.add(ref)
            out.append(index[ref])
    return (out, seen | ids) if ids else (v, seen)


class _Walk:
    """Flattens page data into candidates. Bounded twice: in candidates (``out``), and in values
    visited (``steps``), since a list of numbers costs a visit per item and yields no candidate."""

    def __init__(self, steps: int, index: dict[str, dict] | None = None):
        self.out: list[Cand] = []
        self.steps = steps
        self.index = index  # @id -> JSON-LD node, to follow references; None reads the data as it is

    def walk(self, node: Any, path: str, source: str, entity: int, depth: int,
             seen: frozenset = frozenset(), base: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if self.steps <= 0 or len(self.out) >= _MAX_CANDS:
                    return
                self.steps -= 1
                if (k.startswith("@") and k not in ("@id", "@value")) or len(k) > _KEY_CHARS:
                    continue
                p = f"{path}.{k}" if path else k
                if not isinstance(v, (dict, list)):
                    self.out.append(Cand(norm(k), p, v, source, entity, depth, base))
                    continue
                # {"@id": "...#primaryimage"} points at another node of the @graph: use that node.
                # A node is expanded once per object (not again for a later key), which keeps
                # Yoast-style graphs, where everything points at everything, small.
                if self.index and (isinstance(v, list) or "@id" in v):
                    v, seen = _deref(v, self.index, seen)
                self.out.append(Cand(norm(k), p, v, source, entity, depth, base))
                self.walk(v, p, source, entity, depth + 1, seen, base)
        elif isinstance(node, list):
            for item in node[:_LIST_ITEMS]:
                if self.steps <= 0:
                    return
                self.steps -= 1
                # A list directly inside a list counts as a level, so [[[...]]] cannot recurse unbounded.
                self.walk(item, path, source, entity, depth + 1 if isinstance(item, list) else depth, seen, base)


def _declared_main(nodes: list) -> set[int]:
    """The JSON-LD nodes (by object id) the page declares as its main entity: a main-type
    node with mainEntityOfPage, or one whose @id another node's mainEntity names. A named
    node of any other type declares nothing: a Yoast FAQ block names its Questions, and the
    page's Product is no less the main entity for it."""
    named: set[str] = set()
    for n in nodes:
        if isinstance(n, dict):
            v = n.get("mainEntity")
            for item in (v if isinstance(v, list) else [v])[:50]:
                ref = item.get("@id") if isinstance(item, dict) else item
                if isinstance(ref, str):
                    named.add(ref)
    return {id(n) for n in nodes if isinstance(n, dict) and _entity_rank(_types_of(n)) == 0 and (
        n.get("mainEntityOfPage") or (isinstance(n.get("@id"), str) and n["@id"] in named))}


def build_candidates(
    *,
    jsonld: list[dict],
    microdata: list[dict],
    metadata: dict[str, Any],
    site: dict[str, tuple[Any, str]] | None = None,
) -> list[Cand]:
    walk = _Walk(_MAX_STEPS)
    out = walk.out
    for key, (value, path) in (site or {}).items():
        out.append(Cand(norm(key), path, value, "site", 0, 0))
    jsonld = jsonld[:_MAX_CANDS]  # every pass over the nodes is bounded, not only the walk
    index = {n["@id"]: n for n in jsonld if isinstance(n, dict) and isinstance(n.get("@id"), str)}
    declared = _declared_main(jsonld)
    for source, nodes in (("json-ld", jsonld), ("microdata", microdata)):
        walk.index = index if source == "json-ld" else None
        for node in nodes:
            if len(out) >= _MAX_CANDS or walk.steps <= 0:
                break
            if not isinstance(node, dict):
                continue
            types = _types_of(node)
            rank = _entity_rank(types)
            # A page that names its main entity (BBC Good Food: the Recipe, after an embedded
            # VideoObject) ranks its other Recipe/Article/Video... nodes as other entities.
            if rank == 0 and declared and source == "json-ld" and id(node) not in declared:
                rank = 2
            label = types[0] if types else "Thing"
            if types:  # never matches a field (no normalized name starts with "@")
                out.append(Cand("@type", f"{label}.@type", [norm(t) for t in types], source, rank, 0))
            own = node.get("@id") if source == "json-ld" else None
            walk.walk(node, label, source, rank, 0, frozenset([own]) if isinstance(own, str) else frozenset())
    # Page-level facts are few and are added whatever the structured data above came to.
    for key, value in islice((metadata.get("openGraph") or {}).items(), _META_TAGS):
        out.append(Cand(norm(key.split(":", 1)[-1]) if key.startswith("og:") else norm(key), key, value, "meta", 1, 0))
        out.append(Cand(norm(key), key, value, "meta", 1, 0))
    for key, value in islice((metadata.get("twitter") or {}).items(), _META_TAGS):
        out.append(Cand(norm(key), key, value, "meta", 1, 0))
    for field_name, key in (("title", "title"), ("description", "description"), ("author", "author"),
                            ("publishedTime", "publishedtime"), ("modifiedTime", "modifiedtime"),
                            ("siteName", "sitename"), ("image", "image")):
        v = metadata.get(field_name)
        if v:
            out.append(Cand(key, field_name, v, "meta", 1, 0))
    for field_name, key in (("h1", "h1"), ("language", "lang"), ("canonicalUrl", "canonical"), ("title", "pagetitle")):
        v = metadata.get(field_name)
        if v:
            out.append(Cand(key, field_name, v, "dom", 1, 0))
    return out


# --------------------------------------------------------------------------- coercion

# A number with optional thousands groups (".", ",", "'", or a space only when followed by
# exactly three digits) and an optional decimal part. "39.99 29.99" is two numbers.
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:(?:[.,']|[ \u00a0\u202f](?=\d{3}(?!\d)))\d{3})+(?:[.,]\d+)?|[-+]?\d+(?:[.,]\d+)?")
_DATE_FORMATS = ("%Y-%m-%d", "%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                 "%b %Y", "%B %Y", "%d.%m.%Y", "%m/%d/%Y")
_IN_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale", "backorder")
_OUT_STOCK = ("outofstock", "soldout", "discontinued")
# The keys of a page object that stand for it when a text or a number is asked for.
_TEXT_KEYS = ("name", "@value", "text", "url", "contentUrl")
_NUMBER_KEYS = ("price", "lowPrice", "value", "ratingValue", "reviewCount", "ratingCount", "amount")


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
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
    elif raw.count(".") == 1 and len(raw.rsplit(".", 1)[1]) == 3 and re.search(r"€|,-|\bkr\b|\bzł|\bkč", s, re.I):
        raw = raw.replace(".", "")  # "1.299 €", "€ 1.299,-": European thousands separator
    try:
        return float(raw)
    except ValueError:
        return None


def _to_text(value: Any, join: bool, depth: int = 0) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for k in _TEXT_KEYS:
            if isinstance(value.get(k), (str, int, float)):
                return str(value[k]).strip() or None
        return None
    if isinstance(value, list) and depth < 4:  # a page's [[[...]]] must not exhaust the stack
        parts = [t for t in (_to_text(v, False, depth + 1) for v in value) if t]
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


def _as_list(value: Any, split: bool = True) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if (split and isinstance(value, str) and "," in value and len(value) < 500
            and not re.match(r"^\s*(https?:)?//", value)):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


_URLISH = re.compile(r"^(https?://|//|/|\./|\.\./|data:)|^[^\s/]+\.[a-z]{2,}(/|$)|^[\w\-./%~]+\.(jpe?g|png|gif|webp|avif|svg)(\?|$)", re.I)


# --------------------------------------------------------------------------- filling

def _number_variants(p: str) -> tuple[str, ...]:
    """Singular/plural spellings of a normalized name: offer <-> offers, category <-> categories."""
    if p.endswith("ies"):
        return (p[:-3] + "y",)
    if p.endswith("s"):
        return (p[:-1], p[:-2]) if p.endswith("es") else (p[:-1],)
    return (p + "s", p[:-1] + "ies") if p.endswith("y") else (p + "s", p + "es")


def _weight(value: Any, depth: int = 0) -> int:
    """What coercing a page value costs, in the units of _MAX_WORK: one per list item and one
    per 32 characters of text (a strip, a regex or a join reads all of it)."""
    if isinstance(value, str):
        return len(value) >> 5
    if isinstance(value, dict):
        return sum(len(v) >> 5 for k in _TEXT_KEYS if isinstance(v := value.get(k), str))
    if isinstance(value, list) and depth < 4:
        total = len(value)
        for v in value:
            if total > _MAX_WORK:  # more than a whole call may spend: no need to count on
                break
            total += _weight(v, depth + 1)
        return total
    return 0


class SchemaFiller:
    def __init__(self, cands: list[Cand], base_url: str, root: dict | None = None):
        self.pool = Pool(cands)
        self.base_url = base_url
        self.root = root or {}
        self.sources: dict[str, str] = {}
        self.missing: list[str] = []
        self.defaulted: set[str] = set()  # root properties answered with their schema default
        self.fields, self.work, self.chars = _MAX_FIELDS, _MAX_WORK, _MAX_CHARS
        self.truncated = False  # a budget ran out: what was not reached is null and not in ``missing``
        self._main_types: set[str] | None = None
        # A member reached through 10,000 paths is the same schema node, name, description and
        # enum 10,000 times: each is read once, so a visit costs the same however long they are.
        self._resolved: dict[int, tuple[dict, tuple[dict, frozenset, bool]]] = {}
        self._enums: dict[int, tuple[dict, set, dict]] = {}
        self._norms: dict[str, str] = {}
        self._described: dict[str, str | None] = {}

    def _spend(self, work: int = 0, chars: int = 0) -> bool:
        """Charge the call's budgets, before the work is done. False once one is spent: the
        caller gives up its value, and so does every member after it."""
        if not self.truncated:
            self.work -= work
            self.chars -= chars
            self.truncated = self.work < 0 or self.chars < 0
        return not self.truncated

    def _source(self, path: str, c: Cand, chars: int = 0) -> bool:
        """Record where the value at ``path`` (``chars`` long) was read; False when the result
        has no room left for it."""
        if self._spend(chars=chars + len(path) + len(c.path) + 16):
            self.sources[path] = c.label
        return not self.truncated

    def _miss(self, path: str) -> None:
        if self._spend(chars=len(path) + 4):
            self.missing.append(path)

    def _default(self, default: Any) -> Any:
        """The caller's own copy of a schema default, paid for like any value returned."""
        try:
            return copy.deepcopy(default) if self._spend(chars=len(repr(default))) else None
        except (RecursionError, ValueError):  # nested deeper, or an integer longer, than the API lets through
            return None

    def _enum(self, schema: dict, enum: list) -> tuple[set, dict]:
        """An enum's strings, and each by its lowercase form (the first one wins)."""
        hit = self._enums.get(id(schema))
        if hit is None:
            strings = [e for e in enum if isinstance(e, str)]
            hit = self._enums[id(schema)] = (schema, set(strings), {e.lower(): e for e in reversed(strings)})
        return hit[1], hit[2]

    def _norm(self, name: str) -> str:
        if name not in self._norms:
            self._norms[name] = norm(name)
        return self._norms[name]

    def _resolve(self, schema: Any) -> tuple[dict, frozenset, bool]:
        """Resolve $ref and collapse a one-option anyOf/oneOf/allOf (the shapes Pydantic emits).

        Returns the schema, the $refs followed (a model that contains itself shows up as a
        $ref already being filled) and whether the field allows null (Optional[...])."""
        if not isinstance(schema, dict):
            return {}, frozenset(), False
        hit = self._resolved.get(id(schema))
        if hit is None:  # the node is kept with its result, so its id stays its own
            hit = self._resolved[id(schema)] = (schema, self._resolve_node(schema))
        return hit[1]

    def _resolve_node(self, schema: Any) -> tuple[dict, frozenset, bool]:
        refs: set[str] = set()
        nullable = False
        for _ in range(20):  # a longer $ref chain is left unresolved
            if not isinstance(schema, dict):
                return {}, frozenset(refs), nullable
            t = schema.get("type")
            if (isinstance(t, list) and "null" in t) or ("default" in schema and schema["default"] is None):
                nullable = True
            ref = schema.get("$ref")
            if isinstance(ref, str) and (ref == "#" or ref.startswith("#/")):
                refs.add(ref)
                schema = {**self._pointer(ref), **{k: v for k, v in schema.items() if k != "$ref"}}
                continue
            for key in ("anyOf", "oneOf", "allOf"):
                options = schema.get(key)
                if not isinstance(options, list):
                    continue
                real = options if key == "allOf" else [
                    o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
                nullable = nullable or len(real) < len(options)
                if len(real) == 1 and isinstance(real[0], dict):
                    schema = {**real[0], **{k: v for k, v in schema.items() if k != key}}
                    break
            else:
                break  # nothing left to resolve
        return schema if isinstance(schema, dict) else {}, frozenset(refs), nullable

    def _pointer(self, ref: str) -> dict:
        """The schema a local $ref ("#", "#/$defs/Model") points at; {} when there is none."""
        target: Any = self.root
        for part in ref[2:].split("/") if ref != "#" else ():
            part = part.replace("~1", "/").replace("~0", "~")
            # An index is a few ASCII digits: int() raises on "²" and on 5,000 of them.
            if (isinstance(target, list) and part.isascii() and part.isdigit() and len(part) < 10
                    and int(part) < len(target)):
                target = target[int(part)]
            elif isinstance(target, dict):
                target = target.get(part)
            else:
                return {}
        return target if isinstance(target, dict) else {}

    @property
    def main_types(self) -> set[str]:
        """Normalized @types of the page's main entity, plus og:type when it names one."""
        if self._main_types is None:
            types: set[str] = set()
            for c in self.pool.by_key.get("@type", ()):
                if c.entity == 0 and len(types) < _MAIN_TYPES_READ:
                    types.update(c.value)
            for c in self.pool.by_key.get("ogtype", ()):
                if c.path == "og:type" and isinstance(c.value, str):
                    t = norm(c.value.rsplit(".", 1)[-1])  # "product", "video.movie"
                    if t in _MAIN_TYPES:
                        types.add(t)
            self._main_types = types
        return self._main_types

    def _names_main_entity(self, name: str) -> bool:
        """Does ``name`` name the page's main entity ("product" on a Product page, "article" on
        a NewsArticle or BlogPosting, "game" on a VideoGame)?"""
        w = self._norm(name)
        return any(w == t or w in _TYPE_ALIASES.get(t, ()) or (len(w) >= 4 and t.endswith(w))
                   for t in self.main_types)

    def _ranked(self, prop: str, description: str = "", pool: Pool | None = None,
                exact: bool = False) -> list[Cand]:
        """The candidates for ``prop``, best first: those under its own name, then (unless
        ``exact``) under the keys of its concept."""
        pool = self.pool if pool is None else pool
        p = self._norm(prop)
        concept = _concept(p)
        if concept is None and description:
            if description not in self._described:
                self._described[description] = next(
                    (_SYNONYMS[w] for w in re.findall(r"[a-z]+", description.lower()) if w in _SYNONYMS), None)
            concept = self._described[description]
        wanted = {p, concept or ""}
        ident = concept in _IDENTIFIERS or p in _IDENTIFIER_KEYS
        groups = [(0, pool.by_key.get(p, ()))]
        if not exact:
            groups += [(1 + i, pool.by_key[k]) for i, k in enumerate(_CONCEPT_KEYS.get(concept or "", ()))
                       if k != p and k in pool.by_key]
        if not self._spend(work=sum(len(group) for _, group in groups)):
            return []
        scored = []
        for kind, group in groups:
            for c in group:
                # Inside another entity (Article.author.name for "name")? Rank it below the page's own.
                foreign = bool(c.inside & _FOREIGN) and not (c.inside & wanted)
                if foreign and ident:
                    continue  # another entity's ID (a subscription's isPartOf.productID) is never this one's
                # Curated site fields first; then the page's main entity before other entities, so
                # Article.headline beats a Person node's exact "name"; then match quality and source.
                rank = (-1 if c.source == "site" else 0, 1 if foreign else 0, c.entity, kind, _SOURCE_RANK[c.source],
                        c.depth)
                scored.append((rank, c))
        scored.sort(key=lambda x: x[0])
        return [c for _, c in scored]

    def fill(self, schema: dict, prop: str = "", path: str = "", pool: Pool | None = None,
             description: str = "", active: frozenset = frozenset(), level: int = 0) -> Any:
        """``active``: the $refs being filled on this path; ``level``: object/array nesting."""
        schema, refs, _ = self._resolve(schema)
        return self._fill_resolved(schema, prop, path, pool, description, active | refs if refs else active, level)

    def _fill_resolved(self, schema: dict, prop: str, path: str, pool: Pool | None, description: str,
                       active: frozenset, level: int) -> Any:
        stype = schema.get("type")
        if isinstance(stype, list):
            stype = next((t for t in stype if t != "null"), None)
        if stype == "object" or (stype is None and isinstance(schema.get("properties"), dict)):
            return self._fill_object(schema, path, pool, active, level)
        if not prop:
            return None
        description = description or schema.get("description")
        ranked = self._ranked(prop, description if isinstance(description, str) else "", pool)
        if stype == "array":
            return self._fill_array(schema, prop, path, ranked, active, level)
        for c in ranked:
            value = self._coerce(schema, stype, c.value, prop)
            if value is not None:
                return value if self._source(path, c, len(str(value))) else None
            if self.truncated:
                return None
        self._miss(path)
        return None

    def _fill_object(self, schema: dict, path: str, pool: Pool | None,
                     active: frozenset = frozenset(), level: int = 0) -> dict | None:
        schema, refs, _ = self._resolve(schema)
        active = active | refs if refs else active
        props = schema.get("properties")
        props = props if isinstance(props, dict) else {}
        # The object's keys are paid for before it is opened: whatever runs out inside, its
        # shape is complete, with null for the members that were not reached.
        if not self._spend(chars=sum(len(name) + 8 for name in props)):
            return None
        out: dict[str, Any] = dict.fromkeys(props)
        for name, raw in props.items():
            if self.truncated or self.fields <= 0:
                self.truncated = True
                break
            self.fields -= 1
            sub, sub_refs, nullable = self._resolve(raw)
            sub_path = f"{path}.{name}" if path else name
            if sub.get("type") == "object" or isinstance(sub.get("properties"), dict):
                out[name] = self._fill_member(name, sub, sub_path, pool, active, sub_refs, nullable, level)
            else:
                out[name] = self._fill_resolved(sub, name, sub_path, pool, "",
                                                active | sub_refs if sub_refs else active, level + 1)
            if out[name] is None and sub.get("default") is not None and not self.truncated:
                # The page has no value: the schema's default stands in for null, so that
                # "children: list[Node] = []" validates. It was not read from the page: the
                # property stays in ``missing`` and has no source.
                out[name] = self._default(sub["default"])
                if not path:
                    self.defaulted.add(name)
        return out

    def _fill_member(self, name: str, schema: dict, path: str, pool: Pool | None, active: frozenset,
                     refs: frozenset, nullable: bool, level: int) -> dict | None:
        """An object-typed property, filled from the page object its name matches. With no such
        object, a plain grouping ({"price", "currency"}) is filled from the enclosing scope, but
        a model inside itself (Category.parent) or an Optional[Model] the page does not carry
        (warranty) is null: the page's title under another name would be a guess. An optional
        wrapper named after the page's main entity (product on a Product page) is filled."""
        scoped = self._scoped_pool(name, pool) if level < _MAX_NESTING else None
        if scoped is not None and scoped.text and pool is not None and pool.text and refs & active:
            # A string names one object. Inside it, the same string would name the model again
            # and again, at every level and under every name-like property.
            scoped = None
        if scoped is None and (level >= _MAX_NESTING or refs & active
                               or (nullable and not self._names_main_entity(name))):
            self._miss(path)
            return None
        return self._fill_object(schema, path, pool if scoped is None else scoped, active | refs, level + 1)

    def _scoped_pool(self, name: str, pool: Pool | None) -> Pool | None:
        """Candidates read from inside the object whose key matches ``name`` (or its singular or
        plural: "offer" finds "offers"). None when the page has no such key. When the key holds
        a plain string ("author": "Staff Reporter") or a list of strings (genres), the object is
        filled from that string alone (as its name), never from other entities."""
        for c in self._ranked(name, "", pool):
            sub = self._scope(c)
            if sub is not None:
                return sub
        for alt in _number_variants(self._norm(name)):
            for c in self._ranked(alt, "", pool, exact=True):
                sub = self._scope(c)
                if sub is not None:
                    return sub
        return None

    def _scope(self, c: Cand) -> Pool | None:
        """The candidates inside the page object ``c`` holds (the first of a list). A plain string
        ("author": "Staff Reporter", or Steam's list of genres) becomes that object's name."""
        v = c.value
        if isinstance(v, list):
            v = v[0] if v and isinstance(v[0], (dict, str)) else None
        if isinstance(v, dict):
            return Pool(self._candidates_in(v, c))
        if isinstance(v, (str, int, float)) and self._spend(work=_weight(v)) and str(v).strip():
            return Pool([Cand("name", c.path, v, c.source, c.entity, c.depth, c.path.count(".") + 1)], text=True)
        return None

    def _candidates_in(self, v: dict, c: Cand) -> list[Cand]:
        """The candidates of the page object ``v`` found under ``c``, each value visited charged
        as work. ``c.path`` names their scope: inside it, nothing is foreign."""
        walk = _Walk(self.work)
        walk.walk(v, c.path, c.source, c.entity, c.depth + 1, base=c.path.count(".") + 1)
        self._spend(work=self.work - walk.steps + (walk.steps <= 0))  # cut short: the scope is incomplete
        return walk.out

    def _fill_array(self, schema: dict, prop: str, path: str, ranked: list[Cand],
                    active: frozenset = frozenset(), level: int = 0) -> list | None:
        items = schema.get("items")
        items_schema, item_refs, _ = self._resolve(items) if isinstance(items, dict) else ({}, frozenset(), False)
        item_type = items_schema.get("type")
        objects = item_type == "object" or isinstance(items_schema.get("properties"), dict)
        concept = _concept(self._norm(prop))
        url_items = (items_schema.get("format") in ("uri", "url", "iri")) or concept in {"image", "url"}
        limit = schema.get("maxItems")
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0 else None
        for c in ranked:
            values = _as_list(c.value, split=not url_items)
            if not self._spend(work=len(values)):
                return None
            if objects:
                dicts = [v for v in values if isinstance(v, dict)][:limit]
                if not dicts or level >= _MAX_NESTING:
                    continue
                if not self._source(path, c):
                    return None
                out = []
                for i, v in enumerate(dicts):
                    if self.truncated or self.fields <= 0:
                        self.truncated = True
                        break
                    self.fields -= 1
                    # Each item's fields keep their own source, and its nulls are listed in
                    # missing ("recipeInstructions[0].name", "authors[0].url").
                    item = self._fill_object(items_schema, f"{path}[{i}]", Pool(self._candidates_in(v, c)),
                                             active | item_refs, level + 1)
                    if item is None:
                        break
                    out.append(item)
                return out
            out, seen = [], set()
            for v in values:
                coerced = self._coerce(items_schema, item_type, v, prop)
                if coerced is not None and coerced not in seen:
                    seen.add(coerced)
                    out.append(coerced)
                if self.truncated:
                    return None
            if out:
                out = out[:limit]
                return out if self._source(path, c, sum(len(str(v)) + 4 for v in out)) else None
        self._miss(path)
        return None

    def _coerce(self, schema: dict, stype: str | None, value: Any, prop: str) -> Any:
        number = stype in ("number", "integer")
        if number and isinstance(value, dict):
            value = next((value[k] for k in _NUMBER_KEYS if k in value), value)
        if not self._spend(work=_weight(value)):
            return None
        fmt = schema.get("format") or ""
        concept = _concept(self._norm(prop))
        if number:
            if isinstance(value, list):
                value = next((v for v in value if parse_number(v) is not None), None)
            n = parse_number(value)
            if n is None or not math.isfinite(n):
                return None
            return int(round(n)) if stype == "integer" else n
        if stype == "boolean":
            return _to_bool(value)
        text = _to_text(value, join=concept in {"author", "keywords", "category", "developer", "publisher"})
        if text is None:
            return None
        if fmt in ("uri", "url", "iri") or concept in {"image", "url"}:
            if not _URLISH.search(text.strip()):
                return None  # "Daily Planet" is not a URL; never build one from text
            if not self._spend(work=4):  # parsing a URL costs a few lookups, trying 11 date formats more
                return None
            try:
                if not text.startswith(("http://", "https://", "data:")):
                    text = urljoin(self.base_url, text)
                elif not text.startswith("data:"):
                    urlsplit(text)
            except ValueError:
                return None  # "//[cdn]/w.jpg" is no URL at all: no value, and the next candidate is tried
        elif fmt in ("date", "date-time"):
            if not self._spend(work=40):
                return None
            text = _to_date(text, fmt)
        elif concept == "availability":
            text = text.rsplit("/", 1)[-1]
        enum = schema.get("enum")
        if isinstance(enum, list):
            exact, folded = self._enum(schema, enum)
            if text not in exact:
                text = folded.get(text.lower())
                if text is None:
                    return None
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and len(text) > max_len:
            text = text[:max_len]
        return text


def fill_schema(schema: dict, cands: list[Cand], base_url: str) -> dict[str, Any]:
    filler = SchemaFiller(cands, base_url, root=schema)
    data = filler.fill(schema, active=frozenset({"#"}))
    total = len(filler.sources) + len(filler.missing)
    # Read "required" from the resolved root: Pydantic emits a self-referencing model as
    # {"$ref": "#/$defs/Model", "$defs": {...}}, with the list inside the definition.
    required = filler._resolve(schema)[0].get("required")
    required = [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
    missing_required = [r for r in required if isinstance(data, dict)
                        and (data.get(r) in (None, [], "") or r in filler.defaulted)]
    return {
        "data": data,
        "sources": filler.sources,
        "missing": filler.missing,
        "missingRequired": missing_required,
        "coverage": round(len(filler.sources) / total, 3) if total else 0.0,
        "method": "deterministic",
        # A budget of the call ran out: members after that point are null and in neither list.
        **({"truncated": True} if filler.truncated else {}),
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
    # Steam shows "Recent Reviews" (last 30 days) first, then the overall row, which for
    # logged-out visitors is labelled "English Reviews" (.subtitle.all, itemprop=aggregateRating).
    def _overall_rank(row) -> int:
        tip = (row.attributes.get("data-tooltip-html") or "").lower()
        sub = row.css_first(".subtitle")
        cls = (sub.attributes.get("class") or "") if sub is not None else ""
        if "last 30 days" in tip or "recent" in _txt(sub).lower():
            return 3
        if row.attributes.get("itemprop") == "aggregateRating" or " all" in f" {cls}":
            return 0
        return 1 if tip else 2

    for row in sorted(tree.css(".user_reviews_summary_row"), key=_overall_rank):
        if _overall_rank(row) == 3:
            break
        tip = row.attributes.get("data-tooltip-html") or ""
        m = re.search(r"([\d,.]+)%\s+of the\s+([\d,.]+)\s+user reviews", tip)
        summary = row.css_first(".game_review_summary")
        if _txt(summary) and "reviewsummary" not in out:
            out["reviewsummary"] = (_txt(summary), "steam:overall reviews .game_review_summary")
        if m and "reviewcount" not in out:
            out["rating"] = (parse_number(m.group(1)), "steam:overall reviews @data-tooltip-html")
            out["reviewcount"] = (parse_number(m.group(2)), "steam:overall reviews @data-tooltip-html")
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
