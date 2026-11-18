"""Check every behaviour the write-up claims, against the live endpoint, with pass/fail.

    python3 bench/verify_live.py [https://samenh--webfetch.modal.run]

Standard library only. Exits 1 if any check fails. Honours 429 Retry-After, so running it
twice in a row is fine (the endpoint allows 30 requests/min per client IP).
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://samenh--webfetch.modal.run").rstrip("/")

# Same markers the head-to-head benchmark judges by (text only the real page contains; any one
# suffices). zeit.de is checked separately below: its challenge decides whether we get it.
from compare import CASES  # noqa: E402  (bench/ is on sys.path when run as a script)

TARGETS = {url: markers for url, markers in CASES.items() if "zeit.de" not in url}


def request(method: str, path: str, payload: dict | None = None, timeout: float = 150, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    for _ in range(4):
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "webfetch-verify/1"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in e.headers.items()}
            if e.code == 429:
                time.sleep(float(headers.get("retry-after", "5")) + 0.5)
                continue
            return e.code, headers, e.read(), time.monotonic() - t0
    return 429, {}, b"", 0.0


def parse_json(body: bytes) -> dict:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


# Pydantic 2 model_json_schema() output for two self-referencing models:
#   class Category(BaseModel): name: str; parent: Optional["Category"] = None
#   class Page(BaseModel): title: str; category: Optional[Category] = None
#   class Node(BaseModel): name: str; children: List["Node"] = []   (Pydantic puts this one behind a root $ref)
CATEGORY = {"properties": {"name": {"title": "Name", "type": "string"},
                           "parent": {"anyOf": [{"$ref": "#/$defs/Category"}, {"type": "null"}], "default": None}},
            "required": ["name"], "title": "Category", "type": "object"}
PAGE_SCHEMA = {"$defs": {"Category": CATEGORY}, "properties": {
    "title": {"title": "Title", "type": "string"},
    "category": {"anyOf": [{"$ref": "#/$defs/Category"}, {"type": "null"}], "default": None}},
    "required": ["title"], "title": "Page", "type": "object"}
NODE_SCHEMA = {"$defs": {"Node": {"properties": {
    "name": {"title": "Name", "type": "string"},
    "children": {"default": [], "items": {"$ref": "#/$defs/Node"}, "title": "Children", "type": "array"}},
    "required": ["name"], "title": "Node", "type": "object"}}, "$ref": "#/$defs/Node"}

# web-platform-tests encoding corpora: every character of the encoding in a <span data-cp="...">.
WPT = "https://raw.githubusercontent.com/web-platform-tests/wpt/master/encoding/"
SPAN = re.compile(r'<span data-cp="([0-9A-Fa-f]+)" data-bytes="[0-9A-Fa-f ]+">(.*?)</span>', re.S)
MD_LINK = re.compile(r"\]\((https?://[^)\s]+)")


def second_audit() -> None:
    """The findings of the second audit, each checked where it was found."""
    print("\nFixes from the second audit:")

    deep = '{"type":"object","properties":{"a":' * 1200 + '{"type":"string"}' + "}}" * 1200
    status, h, body, _s = request("POST", "/v1/fetch", raw=(
        '{"url":"https://example.com/","include":[],"jsonSchema":' + deep + "}").encode())
    env = parse_json(body)
    check("jsonSchema nested 1,200 levels deep is a 400 validation_error, with x-request-id",
          status == 400 and env.get("error") == "validation_error" and "x-request-id" in h,
          f"{status} {env.get('error')} id={h.get('x-request-id')}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://example.com/", "include": [],
                                                        "jsonSchema": PAGE_SCHEMA})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("self-referencing Pydantic model: filled, and the Optional object the page lacks is null",
          status == 200 and d.get("title") == "Example Domain" and d.get("category") is None
          and "category" in (ex.get("missing") or []), f"{status} {d}")
    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://example.com/", "include": [],
                                                        "jsonSchema": NODE_SCHEMA})
    env = parse_json(body)
    d = (env.get("extracted") or {}).get("data") or {}
    check("Pydantic root $ref schema accepted and filled",
          status == 200 and d.get("name") == "Example Domain" and not env.get("warnings"), f"{status} {d}")

    # A header that is only for the requested origin must not follow a redirect to another one.
    echo = "https://postman-echo.com/redirect-to?url=https://httpbin.org/headers"
    sent = {"Authorization": "Bearer verify-dummy-1", "X-Api-Key": "verify-dummy-2", "Accept-Language": "nl-NL,nl;q=0.9"}
    for render in ("never", "always"):
        status, h, body, _s = request("POST", "/v1/fetch", {"url": echo, "headers": sent, "include": ["text"],
                                                            "render": render})
        env = parse_json(body)
        text = env.get("text") or ""
        dropped = sorted({x for a in env.get("attempts") or [] for x in a.get("droppedHeaders") or []})
        reached = "httpbin.org" in text  # httpbin echoes the Host header it received
        if render == "never":
            ok = status == 200 and reached and "verify-dummy" not in text and "nl-NL" in text
            detail = f"{status}; Accept-Language passed on: {'nl-NL' in text}"
        else:
            ok = (status == 200 and reached and "verify-dummy" not in text
                  and (env.get("strategy") or "").startswith("browser")
                  and {"authorization", "x-api-key"} <= set(dropped))
            detail = f"{status} via {env.get('strategy')}; droppedHeaders={dropped}"
        check(f"render {render}: Authorization and X-Api-Key never reach the redirect's other origin", ok,
              detail + f"; leaked: {'verify-dummy' in text} id={h.get('x-request-id')}")

    page = ('<html><head><title>Deep JSON-LD</title><script type="application/ld+json">' + "[" * 1100 + "]" * 1100
            + "</script></head><body><p>This page's JSON-LD nests 1,100 levels deep.</p></body></html>")
    url = "https://httpbin.org/base64/" + base64.urlsafe_b64encode(page.encode()).decode()
    status, h, body, _s = request("POST", "/v1/fetch", {"url": url, "include": ["jsonLd"], "render": "never"})
    env = parse_json(body)
    check("a page with JSON-LD nested 1,100 levels deep is served (the blob is skipped), not a 500",
          status == 200 and env.get("ok") is True and env.get("jsonLd") == [],
          f"{status} {env.get('outcome')} jsonLd={env.get('jsonLd')!r}")

    for name, path, encoder_only in [
            ("EUC-JP", "legacy-mb-japanese/euc-jp/eucjp_chars.html", {"A5", "203E", "2212"}),
            ("Big5", "legacy-mb-tchinese/big5/big5_chars.html", set())]:
        status, h, body, _s = request("POST", "/v1/fetch", {"url": WPT + path, "format": "html", "render": "never"})
        spans = SPAN.findall(body.decode("utf-8", "replace"))
        wrong = {cp.upper() for cp, ch in spans if ch != chr(int(cp, 16))}
        # The only differences allowed are the spec's encoder-only entries (e.g. U+00A5 is encoded as 0x5C,
        # but 0x5C decodes to "\").
        check(f"{name}: the web-platform-tests corpus decodes as browsers do",
              status == 200 and len(spans) > 10_000 and wrong == encoder_only,
              f"{status}, {len(spans):,} characters, {len(wrong)} differ")

    intro = "https://docs.python.org/3/tutorial/introduction.html"
    _st, _h, full, _s = request("POST", "/v1/fetch", {"url": intro, "format": "markdown"})
    status, h, readable, _s = request("POST", "/v1/fetch", {"url": intro, "format": "markdown", "mainContentOnly": True})
    full_links = set(MD_LINK.findall(full.decode("utf-8", "replace")))
    readable_links = set(MD_LINK.findall(readable.decode("utf-8", "replace")))
    check("readable mode resolves relative links the way full mode does",
          status == 200 and len(readable_links) >= 10 and readable_links <= full_links,
          f"{len(readable_links)} links, {len(readable_links - full_links)} not among full mode's")

    status, h, body, secs = request("POST", "/v1/fetch", {"url": "https://httpbin.org/delay/10", "timeout": 3,
                                                          "render": "never"})
    env = parse_json(body)
    check("a fetch that times out is a 504 that still carries x-strategy and x-final-url",
          status == 504 and env.get("error") == "timeout" and "x-strategy" in h and "x-final-url" in h,
          f"{status} {env.get('error')} x-strategy={h.get('x-strategy')} in {secs:.1f}s")

    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf", "format": "markdown",
        "render": "never"})
    env = parse_json(body)
    check("a PDF asked for as Markdown is a 415 with ok: false",
          status == 415 and env.get("ok") is False and env.get("error") == "unsupported_content",
          f"{status} ok={env.get('ok')} {env.get('error')}")

    codes = [request("POST", "/v1/fetch", {"url": f"http://{host}/latest/meta-data/"})[0]
             for host in ("[64:ff9b::a9fe:a9fe]", "[::ffff:0:a9fe:a9fe]", "[::7f00:1]")]
    check("SSRF: IPv6 forms of private IPv4 addresses (NAT64, IPv4-translated, IPv4-compatible) refused",
          codes == [403, 403, 403], str(codes))

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "http://python.org/", "render": "always", "include": []})
    env = parse_json(body)
    hops = env.get("redirects") or []
    check("browser-tier responses list their redirect hops",
          status == 200 and (env.get("strategy") or "").startswith("browser") and len(hops) >= 1,
          f"{status} via {env.get('strategy')}: {[(x.get('status'), x.get('url')) for x in hops]}")

    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://www.bbcgoodfood.com/recipes/easy-pancakes", "include": [],
        "jsonSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "recipeIngredient": {"type": "array", "items": {"type": "string"}},
            "recipeInstructions": {"type": "array", "items": {"type": "object",
                                                              "properties": {"text": {"type": "string"}}}}}}})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("recipe: the declared Recipe is the main entity, and array items carry their own sources",
          status == 200 and d.get("name") == "Easy pancakes" and len(d.get("recipeIngredient") or []) >= 3
          and any(k.startswith("recipeInstructions[0].") for k in ex.get("sources") or {}),
          f"{status} name={d.get('name')!r}, {len(d.get('recipeInstructions') or [])} steps")

    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://techcrunch.com/2023/03/14/openai-releases-gpt-4-ai-that-it-claims-is-state-of-the-art/",
        "include": [], "jsonSchema": {"type": "object", "properties": {
            "headline": {"type": "string"},
            "authors": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "url": {"type": "string"}}}}}}})
    d = (parse_json(body).get("extracted") or {}).get("data") or {}
    authors = d.get("authors") or [{}]
    check("JSON-LD @id references in a list (Yoast authors) resolve to the person",
          status == 200 and authors[0].get("name") == "Kyle Wiggers", f"{status} authors={authors}")

    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://www.gog.com/en/game/the_witcher_3_wild_hunt", "include": [],
        "jsonSchema": {"type": "object", "properties": {"name": {"type": "string"}, "sku": {"type": "string"},
                                                        "isbn": {"type": "string"}, "upc": {"type": "string"}}}})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("identifiers are never guessed: a game page has no ISBN or UPC",
          status == 200 and "Witcher" in (d.get("name") or "") and d.get("isbn") is None and d.get("upc") is None
          and {"isbn", "upc"} <= set(ex.get("missing") or []), f"{status} {d}")


def review_fixes() -> None:
    """The findings of the review that followed the second audit, and String request compatibility."""
    print("\nFixes from the review of those fixes:")
    example = "https://example.com/"

    # $ref fan-out: these two are small on purpose (the unfixed service answers them with
    # 8,192 and 16,384 paths); with one more property each grows by orders of magnitude.
    selfref = {"type": "object", "properties": {"name": {"$ref": "#"}, "title": {"$ref": "#"}}}
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "include": [], "jsonSchema": selfref})
    ex = parse_json(body).get("extracted") or {}
    check("a schema that refers to itself through name-like properties is not filled inside itself over and over",
          status == 200 and len(ex.get("missing") or []) <= 16 and len(body) < 20_000,
          f"{status}, {len(ex.get('missing') or []):,} missing paths, {len(body):,} bytes")

    defs = {f"D{i}": {"type": "object", "properties": {k: {"$ref": f"#/$defs/D{i + 1}"} for k in "abcd"}}
            for i in range(6)}
    defs["D6"] = {"type": "object", "properties": {k: {"type": "string"} for k in "abcd"}}
    chain = {"type": "object", "properties": {"root": {"$ref": "#/$defs/D0"}}, "$defs": defs}
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "include": [], "jsonSchema": chain})
    ex = parse_json(body).get("extracted") or {}
    check("a $defs chain that fans out to 16,384 fields stops at the member budget and says so",
          status == 200 and ex.get("truncated") is True and len(ex.get("missing") or []) <= 10_000,
          f"{status}, truncated={ex.get('truncated')}, {len(ex.get('missing') or []):,} missing paths, {len(body):,} bytes")

    # class Node(BaseModel): name: str; children: list["Node"] = []; note: Optional[str] = None; kind: str = "page"
    node = {"$defs": {"Node": {"properties": {
        "name": {"title": "Name", "type": "string"},
        "children": {"default": [], "items": {"$ref": "#/$defs/Node"}, "title": "Children", "type": "array"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Note"},
        "kind": {"default": "page", "title": "Kind", "type": "string"}},
        "required": ["name"], "title": "Node", "type": "object"}}, "$ref": "#/$defs/Node"}
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "include": [], "jsonSchema": node})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("a property the page lacks takes its schema default (a Pydantic model with defaults validates)",
          status == 200 and d == {"name": "Example Domain", "children": [], "note": None, "kind": "page"}
          and {"children", "note", "kind"} <= set(ex.get("missing") or []), f"{status} {d}")

    bad = "http://[object Object]/"
    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://postman-echo.com/redirect-to?url=" + urllib.request.quote(bad, safe=""), "render": "never"})
    env = parse_json(body)
    check("a redirect to something that is not a URL is a 502 invalid_redirect, not a 500",
          status == 502 and env.get("error") == "invalid_redirect", f"{status} {env.get('error')}")
    # httpbin serves this as text/plain: there the tag is the resource's text, not an instruction.
    page = '<html><head><meta http-equiv="refresh" content="0;url=https://example.com/"></head><body>moved</body></html>'
    plain = "https://httpbin.org/base64/" + base64.urlsafe_b64encode(page.encode()).decode()
    status, h, body, _s = request("POST", "/v1/fetch", {"url": plain, "render": "never", "include": []})
    env = parse_json(body)
    check("a meta refresh inside a text/plain response is not followed (only a document refreshes)",
          status == 200 and env.get("finalUrl") == plain and env.get("redirects") == []
          and (env.get("contentType") or "").startswith("text/plain"),
          f"{status} {env.get('contentType')} finalUrl={(env.get('finalUrl') or '')[:40]} redirects={len(env.get('redirects') or [])}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "http://lib.ru/", "render": "always", "include": ["text"]})
    env = parse_json(body)
    check("browser tier: a KOI8-R page comes back as text, not as UTF-8 read as KOI8-R",
          status == 200 and (env.get("strategy") or "").startswith("browser") and "Библиотека" in (env.get("text") or "")
          and env.get("encoding") == "utf-8",
          f"{status} via {env.get('strategy')}, encoding {env.get('encoding')}/{env.get('encodingSource')}: "
          f"{' '.join((env.get('text') or '').split())[:40]!r}")

    # A request written for String, sent unchanged.
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "format": "markdown", "method": "get",
                                                        "solveCaptcha": True, "countryCode": "US",
                                                        "ignoreCertificateErrors": False})
    ignored = {x.strip() for x in (h.get("x-ignored-parameters") or "").split(",") if x.strip()}
    check("String's other request fields are accepted, and the ones without effect are named",
          status == 200 and h.get("x-outcome") == "ok" and len(body) > 50 and ignored == {"countryCode", "solveCaptcha"},
          f"{status} x-ignored-parameters: {h.get('x-ignored-parameters')}")
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "method": "POST", "body": "{}"})
    env = parse_json(body)
    fields = {i.get("field") for i in env.get("issues") or []}
    check("String's method and body: only GET without a body, anything else is a 400 that says so",
          status == 400 and {"method", "body"} <= fields and "GET" in (env.get("message") or ""),
          f"{status} {env.get('message')}")
    status, h, body, _s = request("POST", "/v1/fetch", {"url": example, "actions": [{"type": "click", "selector": "a"}]})
    env = parse_json(body)
    check("String's actions (and captureXHR, screenshot) are a 400 naming the field, never silently dropped",
          status == 400 and "actions" in (env.get("message") or ""), f"{status} {env.get('message')}")
    answers = [request("POST", path, {"url": example, "format": "markdown"}) for path in ("/v1/fetch", "/", "/fetch")]
    check("POST / and POST /fetch answer like POST /v1/fetch",
          all(a[0] == 200 and a[2] == answers[0][2] and a[1].get("x-outcome") == "ok"
              and "content-security-policy" in a[1] for a in answers),
          str([(a[0], len(a[2])) for a in answers]))

    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://store.steampowered.com/app/1091500/Cyberpunk_2077/", "format": "markdown", "markdownMode": "readable"})
    text = body.decode("utf-8", "replace")
    check("readable mode keeps a label's value when the value is a link (Steam's Developer and Publisher)",
          status == 200 and "Developer: [CD PROJEKT RED](" in text and "Publisher: [CD PROJEKT RED](" in text,
          f"{status}, {len(text):,} chars")


def main() -> int:
    print(f"Endpoint: {BASE}\n")

    status, _h, body, secs = request("GET", "/health", timeout=90)
    health = parse_json(body)
    check("health", status == 200 and health.get("ok") is True and health.get("browser") is True,
          f"{status}, browser tier {'on' if health.get('browser') else 'off'}, {secs:.1f}s")
    status, _h, _b, _s = request("GET", "/docs")
    check("interactive docs (/docs)", status == 200, str(status))

    print("\nTargets String returns walls for (format: markdown):")
    for url, markers in TARGETS.items():
        status, h, body, secs = request("POST", "/v1/fetch", {"url": url, "format": "markdown"})
        text = body.decode("utf-8", "replace").lower()
        ok = status == 200 and h.get("x-outcome") == "ok" and any(m.lower() in text for m in markers)
        check(url, ok, f"{status} {h.get('x-outcome')} {len(text):,} chars {secs:.1f}s "
                       f"via {h.get('x-strategy')} id={h.get('x-request-id')}")

    # zeit.de sits behind a Centinel Analytica challenge that usually refuses us and now and
    # then waves a request through. Either answer is correct; a wall returned as the page is not.
    zeit = "https://www.zeit.de/index"
    status, h, body, secs = request("POST", "/v1/fetch", {"url": zeit, "format": "markdown"})
    if status == 200:
        text = body.decode("utf-8", "replace").lower()
        check("zeit.de: Centinel let this one through, and it is the real page",
              h.get("x-outcome") == "ok" and any(m.lower() in text for m in CASES[zeit]),
              f"{len(text):,} chars {secs:.1f}s via {h.get('x-strategy')} id={h.get('x-request-id')}")
    else:
        env = parse_json(body)
        check("zeit.de: Centinel challenge labelled as a challenge, not passed off as the page",
              status == 502 and env.get("outcome") == "challenge",
              f"{status} {env.get('outcome')} {env.get('vendor')} id={h.get('x-request-id')}")

    print("\nAPI behaviour:")
    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://store.steampowered.com/app/1091500/Cyberpunk_2077/", "include": [],
        "jsonSchema": {"type": "object", "properties": {
            "name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
            "developer": {"type": "string"}, "warranty": {"type": "string"}}}})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("jsonSchema: Steam fields extracted with sources, missing field null",
          status == 200 and d.get("name") == "Cyberpunk 2077" and isinstance(d.get("price"), float)
          and d.get("developer") == "CD PROJEKT RED" and d.get("warranty") is None
          and "warranty" in (ex.get("missing") or []) and bool(ex.get("sources")),
          f"{d} in {ex.get('ms')} ms")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://www.nu.nl/", "format": "markdown"})
    check("String-compatible headers (x-request-id, x-status-code, x-final-url) + server-timing",
          all(k in h for k in ("x-request-id", "x-status-code", "x-final-url", "server-timing")),
          f"x-status-code={h.get('x-status-code')} x-final-url={h.get('x-final-url')}")

    status, h, body, _s = request("GET", "/v1/fetch?url=https://www.derstandard.at/")
    env = parse_json(body)
    check("GET /v1/fetch (JSON envelope)", status == 200 and env.get("ok") is True and bool(env.get("markdown")),
          f"{status} {env.get('outcome')} title={(env.get('metadata') or {}).get('title')!r}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://www.nu.nl/", "resolveGates": False})
    env = parse_json(body)
    check("resolveGates:false shows the wall is labelled, not passed off as the page",
          status == 502 and env.get("outcome") == "consent_wall", f"{status} {env.get('outcome')}")

    # Steam's age gate is per store (GTA V has one on the US store, none on the Polish one) and
    # Modal may put the container in either country, so the Steam targets above can pass without
    # meeting the gate. Pin the US store: the gate must be there without the recipe, gone with it.
    gta = "https://store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/?cc=us"
    s_off, _h, b_off, _s = request("POST", "/v1/fetch", {"url": gta, "include": [], "resolveGates": False})
    s_on, h_on, b_on, _s = request("POST", "/v1/fetch", {"url": gta, "format": "markdown"})
    off = parse_json(b_off)
    check("Steam age gate (US store): there without the recipe, passed with it",
          s_off == 502 and off.get("outcome") == "age_gate" and s_on == 200 and h_on.get("x-outcome") == "ok"
          and "rockstar north" in b_on.decode("utf-8", "replace").lower(),
          f"without: {s_off} {off.get('outcome')} at {off.get('finalUrl')}; "
          f"with: {s_on} {h_on.get('x-outcome')} id={h_on.get('x-request-id')}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "http://169.254.169.254/latest/meta-data/"})
    check("SSRF: cloud metadata address refused", status == 403, f"{status} {parse_json(body).get('error')}")
    status, h, body, _s = request("POST", "/v1/fetch", {"url": "ftp://example.com/"})
    check("invalid URL is a 400", status == 400, str(status))

    status, h, body, secs = request("POST", "/v1/batch", {"requests": [
        {"url": "https://www.python.org/", "include": []},
        {"url": "https://tweakers.net/", "include": []},
        {"url": "https://store.steampowered.com/app/413150/Stardew_Valley/", "include": []}]})
    res = parse_json(body).get("results") or []
    check("batch of 3", status == 200 and len(res) == 3 and all(r.get("ok") for r in res),
          f"{status}, {[r.get('outcome') for r in res]}, {secs:.1f}s")

    def one(url):
        return request("POST", "/v1/fetch", {"url": url, "include": []})[0]

    urls = ["https://en.wikipedia.org/wiki/Web_scraping", "https://www.python.org/",
            "https://store.steampowered.com/app/1091500/Cyberpunk_2077/", "https://www.derstandard.at/",
            "https://tweakers.net/", "https://www.bbc.com/news"]
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(len(urls)) as pool:
        codes = list(pool.map(one, urls))
    check(f"{len(urls)} concurrent requests", all(c == 200 for c in codes),
          f"{codes} in {time.monotonic() - t0:.1f}s")

    second_audit()
    review_fixes()

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
