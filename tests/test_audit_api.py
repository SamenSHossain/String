"""Regressions for the API-layer findings of the live-endpoint audit: origin-bound caller
headers, no raw 500s, honest fetch-error and redirect-loop envelopes, browser-tier
redirects, and the SSRF guard's embedded-IPv4 forms."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from tests import fakedns
from webfetch import extract
from webfetch import fetcher as fetcher_mod
from webfetch.api import FetchBody, create_app
from webfetch.config import Settings
from webfetch.fetcher import HttpFetcher
from webfetch.netguard import BlockedDestination, ip_is_public, resolve_public
from webfetch.service import FetchOptions, run_fetch

CHROMIUM = os.environ.get("WEBFETCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
try:
    import patchright  # noqa: F401
    HAVE_BROWSER = os.path.exists(CHROMIUM)
except ImportError:  # CI installs only the HTTP-tier requirements
    HAVE_BROWSER = False
needs_browser = pytest.mark.skipif(not HAVE_BROWSER, reason="no local Chromium")

SECRETS = {"Authorization": "Bearer secret-token", "X-Api-Key": "key-123", "X-Custom": "custom",
           "Cookie": "sid=from-header; theme=dark"}
SAFE = {"Accept-Language": "de-DE", "User-Agent": "AuditAgent/1.0"}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)


def _client() -> TestClient:
    # raise_server_exceptions=False: see the 500 a real client gets instead of the exception.
    return TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000)),
                      raise_server_exceptions=False)


def run(coro):
    return asyncio.run(coro)


async def _fetch(settings, url, **kw):
    f = HttpFetcher(settings)
    try:
        return await f.fetch(url, **kw)
    finally:
        await f.aclose()


async def _pipeline(settings, opts, browser=None):
    f = HttpFetcher(settings)
    try:
        return await run_fetch(opts, f, browser)
    finally:
        await f.aclose()


def _echoed(page: str | bytes) -> dict:
    """The request headers the local /echo-headers route saw."""
    text = page.decode() if isinstance(page, bytes) else page
    m = re.search(r"<pre id=[\"']h[\"']>(.*?)</pre>", text, re.S)
    assert m, text[:300]
    return json.loads(html.unescape(m.group(1)))


class FakeBrowser:
    """Records what the browser tier is asked to send; serves a fixed page."""

    def __init__(self, redirects: list | None = None):
        self.calls: list[dict] = []
        self.redirects = redirects

    async def render(self, url, *, cookies, headers, timeout, max_bytes=0):
        self.calls.append({"url": url, "cookies": cookies, "headers": headers})
        out = {"finalUrl": url.replace("/start", "/end"), "status": 200, "headers": {"content-type": "text/html"},
               "html": "<html><head><title>T</title></head><body><p>" + "Rendered text. " * 40 + "</p></body></html>",
               "ms": 12.0, "strategy": "browser:fake"}
        if self.redirects is not None:
            out["redirects"] = self.redirects
        return out


# --- A1: caller headers stay with the origin the caller named ----------------------------

def test_http_tier_sends_only_cross_origin_safe_headers_to_other_origins(server, settings):
    port = server.server_address[1]
    other = f"http://other.test:{port}/echo-headers"
    res = run(_fetch(settings, f"http://echo.test:{port}/redirect-to?url={quote(other, safe='')}",
                     headers={**SECRETS, **SAFE}))
    assert res.final_url == other
    got = _echoed(res.body)
    assert not {"authorization", "x-api-key", "x-custom", "cookie"} & set(got), got
    assert got["accept-language"] == "de-DE" and got["user-agent"] == "AuditAgent/1.0"


def test_http_tier_same_origin_redirect_keeps_every_caller_header(server, settings):
    port = server.server_address[1]
    same = f"http://echo.test:{port}/echo-headers"
    res = run(_fetch(settings, f"http://echo.test:{port}/redirect-to?url={quote(same, safe='')}",
                     headers={**SECRETS, **SAFE}))
    got = _echoed(res.body)
    assert got["authorization"] == "Bearer secret-token" and got["x-api-key"] == "key-123"
    assert got["x-custom"] == "custom" and got["accept-language"] == "de-DE"


def test_browser_tier_gets_only_safe_headers_and_the_cookie_header_as_cookies(settings):
    fake = FakeBrowser()
    opts = FetchOptions(url="http://echo.test:9/start", render="always", include={"markdown"},
                        headers={**SECRETS, **SAFE}, cookies={"theme": "light", "lang": "en"})
    env = run(_pipeline(settings, opts, fake))
    assert env["ok"] is True
    call = fake.calls[0]
    assert call["headers"] == SAFE
    jar = {(c["name"], c["domain"], c["path"]): c["value"] for c in call["cookies"]}
    # the Cookie header becomes cookies for the requested host; the cookies field wins a clash
    assert jar == {("sid", "echo.test", "/"): "from-header", ("theme", "echo.test", "/"): "light",
                   ("lang", "echo.test", "/"): "en"}
    assert env["attempts"][0]["droppedHeaders"] == ["authorization", "x-api-key", "x-custom"]


def test_browser_attempt_without_origin_bound_headers_reports_none(settings):
    fake = FakeBrowser()
    env = run(_pipeline(settings, FetchOptions(url="http://echo.test:9/start", render="always",
                                               headers=dict(SAFE)), fake))
    assert "droppedHeaders" not in env["attempts"][0]
    assert fake.calls[0]["headers"] == SAFE and fake.calls[0]["cookies"] == []


def _render(url: str, **kw) -> dict:
    from webfetch.browser import BrowserPool

    async def go():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        try:
            return await pool.render(url, cookies=kw.pop("cookies", []), headers=kw.pop("headers", None),
                                     timeout=20, **kw)
        finally:
            await pool.stop()

    return run(go())


@needs_browser
def test_browser_never_carries_caller_credentials_to_another_origin(server):
    """Chromium carries headers set by the route handler across a redirect without calling
    the handler again, so origin-bound headers must never be handed to it at all."""
    port = server.server_address[1]
    target = f"http://localhost:{port}/echo-headers"
    out = _render(f"http://127.0.0.1:{port}/redirect-to?url={quote(target, safe='')}",
                  headers={"Authorization": "Bearer secret-token", "X-Api-Key": "key-123", "Accept-Language": "de-DE"})
    assert out.get("error") is None, out
    assert out["finalUrl"] == target
    got = _echoed(out["html"])
    assert "authorization" not in got and "x-api-key" not in got, got
    assert got["accept-language"] == "de-DE"


@needs_browser
def test_browser_tier_sends_the_cookie_header_as_cookies(server, settings):
    from webfetch.browser import BrowserPool

    port = server.server_address[1]

    async def go():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        try:
            opts = FetchOptions(url=f"http://127.0.0.1:{port}/echo-headers", render="always", include={"html"},
                                headers={**SECRETS, **SAFE}, cookies={"theme": "light"})
            return await _pipeline(settings, opts, pool)
        finally:
            await pool.stop()

    env = run(go())
    got = _echoed(env["html"])
    assert sorted(got["cookie"].split("; ")) == ["sid=from-header", "theme=light"]
    assert got["accept-language"] == "de-DE" and got["user-agent"] == "AuditAgent/1.0"
    assert not {"authorization", "x-api-key", "x-custom"} & set(got), got
    assert env["attempts"][0]["droppedHeaders"] == ["authorization", "x-api-key", "x-custom"]


# --- A2:jsonSchema validation never 500s; a root $ref is accepted -------------------------

def _deep_schema_body(depth: int) -> str:
    return ('{"url":"https://example.com/","jsonSchema":{"type":"object","properties":{"a":'
            + "[" * depth + "]" * depth + "}}}")


@pytest.mark.parametrize("depth", [1300, 3000, 8000])
def test_deeply_nested_json_schema_is_a_400(depth):
    with _client() as c:
        r = c.post("/v1/fetch", content=_deep_schema_body(depth), headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text[:200]
    body = r.json()
    assert body["error"] == "validation_error" and "nesting depth" in body["message"]
    assert body["requestId"] == r.headers["x-request-id"]


def test_body_nested_past_the_json_parser_limit_is_a_validation_error():
    with _client() as c:
        r = c.post("/v1/fetch", content=_deep_schema_body(50_000), headers={"content-type": "application/json"})
        missing = c.get("/v1/nope")
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error" and r.json()["requestId"] == r.headers["x-request-id"]
    assert missing.status_code == 404  # other HTTP errors keep FastAPI's answer


def test_schema_too_deep_for_json_dumps_is_a_validation_error(monkeypatch):
    """Belt and braces: even if the size walk let a deep schema through, json.dumps'
    RecursionError becomes a validation error."""
    import webfetch.api as api_mod
    monkeypatch.setattr(api_mod, "_schema_size", lambda node: (0, 0))
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(ValidationError) as exc:
        FetchBody(url="https://example.com/", jsonSchema={"type": "object", "properties": {"a": deep}})
    assert "nests too deeply" in exc.value.errors()[0]["msg"]


class Node(BaseModel):
    name: str
    children: list[Node] = []


def test_pydantic_root_ref_schema_is_accepted(server):
    schema = Node.model_json_schema()
    assert schema["$ref"] == "#/$defs/Node" and "properties" not in schema  # what Pydantic emits
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "jsonSchema": schema, "include": []})
        legacy = json.loads(json.dumps(schema).replace('"$defs"', '"definitions"').replace("#/$defs/", "#/definitions/"))
        r2 = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "jsonSchema": legacy, "include": []})
        bad = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "include": [],
                                        "jsonSchema": {"$defs": {"X": {"type": "string"}}, "$ref": "#/$defs/X"}})
        dangling = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "include": [],
                                             "jsonSchema": {"$ref": "#/$defs/Missing"}})
    assert r.status_code == 200, r.text
    assert r.json()["extracted"]["data"]["name"] == "Acme Anvil 50kg"
    assert r2.status_code == 200, r2.text
    assert bad.status_code == 400 and dangling.status_code == 400


class Category(BaseModel):
    name: str
    parent: Category | None = None


class Page(BaseModel):
    title: str
    category: Category | None = None


class Tree(BaseModel):
    name: str
    parent: Tree | None = None


@pytest.mark.parametrize("model", [Page, Tree])
def test_self_referencing_models_never_500(server, model):
    """Whether or not the filler copes with the cycle, the request is served."""
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "include": [],
                                      "jsonSchema": model.model_json_schema()})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "extracted" in body or [w["part"] for w in body["warnings"]] == ["extracted"]


# --- A3: no raw 500s ---------------------------------------------------------------------

def test_unexpected_error_is_a_json_500_with_request_id_and_security_headers(server, monkeypatch, caplog):
    def boom(*a, **k):
        raise ValueError("converter exploded with /secret/path")

    monkeypatch.setattr(extract, "to_markdown", boom)
    port = server.server_address[1]
    with caplog.at_level(logging.ERROR, logger="webfetch"), _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "format": "markdown"})
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"] == "internal_error" and body["requestId"] == r.headers["x-request-id"]
    assert "secret" not in body["message"]  # no internals in the answer
    assert r.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    logged = [rec for rec in caplog.records if body["requestId"] in rec.getMessage()]
    assert logged and logged[0].exc_info is not None  # the traceback is in the logs


def test_failing_structured_part_is_a_warning_not_a_500(server, monkeypatch):
    def too_deep(*a, **k):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(extract, "json_ld", too_deep)
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product",
                                      "include": ["markdown", "metadata", "jsonLd", "links"],
                                      "jsonSchema": {"type": "object", "properties": {"name": {"type": "string"}}}})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["ok"] is True and "jsonLd" not in body
    assert body["warnings"] == [{"part": "jsonLd", "message": "RecursionError: maximum recursion depth exceeded"}]
    assert "Forged steel" in body["markdown"] and body["metadata"]["title"] and body["links"] is not None
    assert body["extracted"]["data"]["name"]  # still filled, from the page's other sources


def test_failing_schema_fill_omits_only_extracted(server, monkeypatch):
    import webfetch.service as service_mod

    def broken(*a, **k):
        raise KeyError("x")

    monkeypatch.setattr(service_mod, "fill_schema", broken)
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/product", "include": ["jsonLd"],
                                      "jsonSchema": {"type": "object", "properties": {"name": {"type": "string"}}}})
    assert r.status_code == 200
    body = r.json()
    assert "extracted" not in body and body["jsonLd"]
    assert [w["part"] for w in body["warnings"]] == ["extracted"]


def test_batch_survives_a_failing_item_and_reports_each_status(server, monkeypatch):
    real = extract.to_markdown

    def flaky(html_, base, mode="full"):
        if "/product" in base:
            raise ValueError("boom")
        return real(html_, base, mode)

    monkeypatch.setattr(extract, "to_markdown", flaky)
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/batch", json={"requests": [
            {"url": f"http://echo.test:{port}/product"},
            {"url": f"http://store.steampowered.com:{port}/app/271590/", "include": ["metadata"]},
            {"url": "ftp://example.com/"},
            {"url": f"http://echo.test:{port}/challenge"},
        ]})
    assert r.status_code == 200, r.text[:300]
    rid = r.headers["x-request-id"]
    res = r.json()["results"]
    assert res[0]["error"] == "internal_error" and res[0]["requestId"] == f"{rid}:0"
    assert res[0]["httpStatus"] == 500 and res[0]["url"] == f"http://echo.test:{port}/product"
    assert res[1]["ok"] is True and res[1]["httpStatus"] == 200
    assert res[2]["error"] == "invalid_url" and res[2]["httpStatus"] == 400 and res[2]["url"] == "ftp://example.com/"
    assert res[3]["outcome"] == "challenge" and res[3]["httpStatus"] == 502


# --- A4: fetch errors say which strategy failed and where ---------------------------------

def test_fetch_errors_carry_strategy_and_final_url(server):
    port = server.server_address[1]
    with _client() as c:
        slow = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/slow", "timeout": 1})
        loop = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/loop"})
    assert slow.status_code == 504 and slow.json()["error"] == "timeout"
    assert slow.headers["x-strategy"] == "http:chrome" == slow.json()["strategy"]
    assert slow.headers["x-final-url"] == f"http://echo.test:{port}/slow" == slow.json()["finalUrl"]
    assert "x-status-code" not in slow.headers and "statusCode" not in slow.json()
    assert loop.status_code == 502 and loop.json()["outcome"] == "fetch_error"
    assert loop.json()["error"] == "too_many_redirects"  # a plain loop is not a wall
    assert loop.headers["x-strategy"] == "http:chrome"
    assert loop.headers["x-final-url"] == f"http://echo.test:{port}/loop" == loop.json()["redirects"][-1]["url"]
    assert "x-status-code" not in loop.headers


# --- A5: a redirect loop through an age check or consent page is that wall ----------------

def test_redirect_loop_through_an_age_check_is_an_age_gate(server):
    port = server.server_address[1]
    url = f"http://store.steampowered.com:{port}/app/1174180/?cc=us"
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": url, "resolveGates": False, "render": "never", "include": []})
        gated = c.post("/v1/fetch", json={"url": url, "render": "never", "include": []})
    assert r.status_code == 502, r.text[:300]
    body = r.json()
    assert body["ok"] is False and body["outcome"] == "age_gate"
    assert body["reason"] == "destination keeps redirecting to its age check (/agecheck/app/1174180/)"
    assert body["url"] == url and body["finalUrl"] == body["redirects"][-1]["url"] == r.headers["x-final-url"]
    assert body["statusCode"] == 302 and r.headers["x-status-code"] == "302"
    assert body["strategy"] == "http:chrome" == r.headers["x-strategy"]
    assert len(body["redirects"]) == 11 and body["gates"] == []
    assert [a.get("error") for a in body["attempts"]] == ["too_many_redirects"]
    assert body["timing"]["totalMs"] > 0
    # with the recipe on, the loop is still reported as the age gate it is, recipe actions listed
    assert gated.status_code == 502 and gated.json()["outcome"] == "age_gate"
    assert {g["trigger"] for g in gated.json()["gates"]} == {"preset", "redirect"}


def test_redirect_loop_through_a_consent_page_is_a_consent_wall(server):
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://news.consent-loop.test:{port}/", "render": "never",
                                      "format": "markdown"})
    assert r.status_code == 502
    body = r.json()
    assert body["outcome"] == "consent_wall" and "markdown" not in body
    assert body["reason"] == "destination keeps redirecting to its consent page (news.consent-loop.test/consent/)"


# --- A6: browser-tier responses list the redirects the browser followed ------------------

def test_browser_redirects_reach_the_envelope(settings):
    hops = [{"url": "http://echo.test:9/start", "status": 302, "location": "/middle", "ms": 3.2},
            {"url": "http://echo.test:9/middle", "status": 301, "location": "http://echo.test:9/end", "ms": 1.0}]
    env = run(_pipeline(settings, FetchOptions(url="http://echo.test:9/start", render="always"), FakeBrowser(hops)))
    assert env["finalUrl"] == "http://echo.test:9/end"
    assert env["redirects"] == hops


@needs_browser
def test_browser_reports_its_redirect_hops(server, settings):
    from webfetch.browser import BrowserPool

    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    out = _render(f"{base}/two-hops")
    assert out["finalUrl"] == f"{base}/echo-headers"
    assert [(h["url"], h["status"], h["location"]) for h in out["redirects"]] == [
        (f"{base}/two-hops", 302, "/two-hops/next"), (f"{base}/two-hops/next", 301, "/echo-headers")]

    async def through_pipeline():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        try:
            return await _pipeline(settings, FetchOptions(url=f"{base}/two-hops", render="always"), pool)
        finally:
            await pool.stop()

    env = run(through_pipeline())
    assert env["strategy"] == "browser:patchright" and env["finalUrl"] == f"{base}/echo-headers"
    assert [(h["url"], h["status"]) for h in env["redirects"]] == [(f"{base}/two-hops", 302),
                                                                   (f"{base}/two-hops/next", 301)]


# --- A8: IPv6 forms that embed an IPv4 are judged by that IPv4 -----------------------------

@pytest.mark.parametrize("ip", [
    "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1",  # NAT64 well-known prefix
    "64:ff9b:1::7f00:1", "64:ff9b:1::a9fe:a9fe", "64:ff9b:1::a00:1",  # NAT64 local-use prefix
    "::7f00:1", "::a9fe:a9fe", "::a00:1",  # IPv4-compatible (deprecated)
    "::ffff:0:7f00:1", "::ffff:0:a9fe:a9fe", "::ffff:0:a00:1",  # IPv4-translated (SIIT)
])
def test_ipv6_embedding_a_private_ipv4_is_not_public(ip):
    assert ip_is_public(ip) is False
    with pytest.raises(BlockedDestination):
        run(resolve_public(ip, 80))


def test_ipv6_embedding_a_public_ipv4_stays_public():
    assert ip_is_public("64:ff9b::808:808") and ip_is_public("::ffff:0:808:808") and ip_is_public("::808:808")
    # RFC 8215's local-use NAT64 prefix is never a public destination, whatever it embeds
    assert ip_is_public("64:ff9b:1::808:808") is False


def test_nat64_metadata_address_is_refused_by_the_api():
    with TestClient(create_app(Settings(allow_private=False))) as c:
        r = c.post("/v1/fetch", json={"url": "http://[64:ff9b::a9fe:a9fe]/latest/meta-data/", "timeout": 3})
    assert r.status_code == 403 and r.json()["error"] == "blocked_destination"
