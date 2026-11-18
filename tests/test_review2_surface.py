"""The API surface and the extraction helpers: a request written for String's /v1/fetch
is accepted unchanged (its other documented fields, POST / and POST /fetch), and the
second review's findings in extract.py."""

from __future__ import annotations

import itertools
import json
import logging
import random
import re
import sys

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import extract
from webfetch import fetcher as fetcher_mod
from webfetch.api import FetchBody, create_app
from webfetch.config import Settings

SECURITY = {"content-security-policy": "sandbox; default-src 'none'", "x-content-type-options": "nosniff",
            "x-frame-options": "DENY"}
# One value per String field that webfetch accepts without effect, and the value that
# asks for nothing webfetch does differently.
IGNORED = {"countryCode": "de", "solveCaptcha": True, "ignoreCertificateErrors": True, "requireWSS": True}
ALL_IGNORED = "countryCode, solveCaptcha, ignoreCertificateErrors, requireWSS"
ROUTES = ["/v1/fetch", "/", "/fetch"]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)


def _client(**settings) -> TestClient:
    return TestClient(create_app(Settings(**{"allow_private": True, "anon_rate_per_min": 10_000, **settings})),
                      raise_server_exceptions=False)


@pytest.fixture()
def client():
    with _client() as c:
        yield c


@pytest.fixture()
def page(server):
    return f"http://echo.test:{server.server_address[1]}"


def _rejected(client, field: str, value, route: str = "/v1/fetch") -> str:
    """The message of the 400 that ``field: value`` gets."""
    r = client.post(route, json={"url": "https://example.com/", field: value})
    assert r.status_code == 400, (field, value, r.text[:200])
    body = r.json()
    assert body["error"] == "validation_error" and body["requestId"] == r.headers["x-request-id"]
    assert [i["field"] for i in body["issues"]] == [field]
    return body["message"]


# --- A: String request compatibility -----------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"method": "GET"}, {"method": "get"}, {"method": "gEt"}, {"method": None}, {"body": None},
    {"captureXHR": False}, {"captureXHR": None}, {"screenshot": False}, {"screenshot": None},
    {"actions": []}, {"actions": None},
    {"solveCaptcha": False}, {"ignoreCertificateErrors": False}, {"requireWSS": False}, {"countryCode": None},
    {"method": "GET", "body": None, "captureXHR": False, "screenshot": False, "actions": [], "solveCaptcha": False,
     "ignoreCertificateErrors": False, "requireWSS": False, "executeJS": False, "markdownMode": "full",
     "mainContentOnly": False, "headers": {"Accept-Language": "en"}},
])
def test_string_fields_that_ask_for_nothing_are_accepted_silently(client, page, extra):
    r = client.post("/v1/fetch", json={"url": f"{page}/product", **extra})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["ok"] is True and r.json()["outcome"] == "ok"
    assert "ignoredParameters" not in r.json() and "x-ignored-parameters" not in r.headers


@pytest.mark.parametrize("method", ["POST", "post", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "", " GET"])
def test_only_get_is_a_supported_method(client, method):
    assert "webfetch only reads pages with GET" in _rejected(client, "method", method)


@pytest.mark.parametrize("value", ["a=1", "", {"a": 1}, {}, [], 0, False])
def test_any_request_body_is_refused(client, value):
    assert "request bodies are not supported" in _rejected(client, "body", value)


@pytest.mark.parametrize("field,value", [
    ("captureXHR", True), ("captureXHR", {}), ("captureXHR", {"include": [{"url": "/api/"}]}), ("captureXHR", "yes"),
    ("screenshot", True), ("screenshot", 1),
    ("actions", [{"type": "click", "selector": "#more"}]), ("actions", [{}]), ("actions", {"type": "wait"}),
])
def test_a_field_that_asks_for_an_unsupported_feature_is_refused_by_name(client, field, value):
    assert f"{field} is not supported" in _rejected(client, field, value)


@pytest.mark.parametrize("field,value", [("countryCode", "USA"), ("countryCode", "U"), ("countryCode", 49),
                                         ("solveCaptcha", "maybe"), ("ignoreCertificateErrors", []),
                                         ("requireWSS", {}), ("method", 7)])
def test_ignored_fields_keep_strings_types(client, field, value):
    _rejected(client, field, value)


def test_a_field_neither_service_has_is_still_refused(client):
    assert "Extra inputs are not permitted" in _rejected(client, "proxyCountry", "de")
    r = client.post("/v1/batch", json={"requests": [{"url": "https://example.com/", "proxyCountry": "de"}]})
    assert r.status_code == 400 and r.json()["issues"][0]["field"] == "requests.0.proxyCountry"


@pytest.mark.parametrize("field,value", sorted(IGNORED.items()))
def test_each_ignored_field_is_reported(client, page, field, value):
    r = client.post("/v1/fetch", json={"url": f"{page}/product", field: value})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["ignoredParameters"] == [field] and r.headers["x-ignored-parameters"] == field


@pytest.mark.parametrize("path,fmt,status,outcome", [
    ("/product", "json", 200, "ok"), ("/product", "markdown", 200, "ok"), ("/product", "text", 200, "ok"),
    ("/product", "html", 200, "ok"), ("/product", "raw", 200, "ok"),
    ("/challenge", "json", 502, "challenge"), ("/challenge", "markdown", 502, "challenge"),
    ("/challenge", "raw", 502, "challenge"),
    ("/loop", "json", 502, "fetch_error"), ("/loop", "markdown", 502, "fetch_error"),
    ("/doc.pdf", "markdown", 415, "ok"), ("/doc.pdf", "text", 415, "ok"),
])
def test_ignored_parameters_are_reported_on_every_answer(client, page, path, fmt, status, outcome):
    plain = client.post("/v1/fetch", json={"url": page + path, "format": fmt})
    r = client.post("/v1/fetch", json={"url": page + path, "format": fmt, **IGNORED})
    assert (r.status_code, r.headers["x-outcome"]) == (status, outcome) == (plain.status_code, plain.headers["x-outcome"])
    assert r.headers["x-ignored-parameters"] == ALL_IGNORED and "x-ignored-parameters" not in plain.headers
    if r.headers["content-type"].startswith("application/json"):
        assert r.json()["ignoredParameters"] == list(IGNORED) and "ignoredParameters" not in plain.json()
        # Nothing else about the answer changes.
        volatile = {"requestId", "timing", "ignoredParameters", "attempts", "redirects"}
        assert ({k: v for k, v in r.json().items() if k not in volatile}
                == {k: v for k, v in plain.json().items() if k not in volatile})
    else:
        assert fmt != "json" and status == 200 and r.content == plain.content


def test_ignored_parameters_are_reported_on_refused_urls():
    with _client(allow_private=False) as c:
        bad = c.post("/v1/fetch", json={"url": "ftp://example.com/", "countryCode": "us"})
        private = c.post("/v1/fetch", json={"url": "http://169.254.169.254/", "requireWSS": True, "format": "markdown"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_url"
    assert bad.json()["ignoredParameters"] == ["countryCode"] == [bad.headers["x-ignored-parameters"]]
    assert private.status_code == 403 and private.json()["error"] == "blocked_destination"
    assert private.json()["ignoredParameters"] == ["requireWSS"] == [private.headers["x-ignored-parameters"]]


def test_batch_items_take_string_fields_and_report_their_own(client, page, monkeypatch):
    from webfetch import extract
    real = extract.to_markdown

    def flaky(html_, base, mode="full"):
        if "/cookies" in base:
            raise ValueError("boom")
        return real(html_, base, mode)

    monkeypatch.setattr(extract, "to_markdown", flaky)
    r = client.post("/v1/batch", json={"requests": [
        {"url": f"{page}/product", "method": "get", "countryCode": "nl", "solveCaptcha": False},
        {"url": f"{page}/product", "method": "GET", "body": None, "screenshot": False, "actions": []},
        {"url": f"{page}/challenge", "solveCaptcha": True, "ignoreCertificateErrors": True},
        {"url": "ftp://example.com/", "requireWSS": True},
        {"url": f"{page}/cookies", "countryCode": "be"},
    ]})
    assert r.status_code == 200, r.text[:300]
    assert "x-ignored-parameters" not in r.headers and "ignoredParameters" not in r.json()
    res = r.json()["results"]
    assert [x["httpStatus"] for x in res] == [200, 200, 502, 400, 500]
    assert [x.get("ignoredParameters") for x in res] == [["countryCode"], None, ["solveCaptcha", "ignoreCertificateErrors"],
                                                         ["requireWSS"], ["countryCode"]]
    assert "ignoredParameters" not in res[1]
    for field, value in [("method", "POST"), ("body", "x"), ("screenshot", True), ("captureXHR", True),
                         ("actions", [{"type": "wait"}])]:
        r = client.post("/v1/batch", json={"requests": [{"url": f"{page}/product"}, {"url": f"{page}/product", field: value}]})
        assert r.status_code == 400 and r.json()["issues"][0]["field"] == f"requests.1.{field}", (field, r.text[:200])


@pytest.mark.parametrize("route", ROUTES[1:])
def test_the_bare_url_and_fetch_answer_like_v1_fetch(client, page, route, caplog):
    for body in ({"url": f"{page}/product", "countryCode": "de"},  # 200 JSON
                 {"url": f"{page}/product", "format": "markdown"},  # 200 text
                 {"url": f"{page}/challenge", "format": "markdown"},  # 502 wall
                 {"url": "ftp://example.com/"},  # 400 invalid_url
                 {"url": f"{page}/product", "bogus": 1},  # 400 validation_error
                 {"url": f"{page}/product", "method": "POST"},
                 {}):
        with caplog.at_level(logging.INFO, logger="webfetch"):
            caplog.clear()
            want, got = client.post("/v1/fetch", json=body), client.post(route, json=body)
        assert got.status_code == want.status_code, (route, body, got.text[:200])
        volatile = {"x-request-id", "x-request-duration-ms", "server-timing", "content-length"}
        assert ({k: v for k, v in got.headers.items() if k not in volatile}
                == {k: v for k, v in want.headers.items() if k not in volatile}), (route, body)
        assert {k: got.headers[k] for k in SECURITY} == SECURITY
        if got.headers["content-type"].startswith("application/json"):
            drop = {"requestId", "timing", "attempts"}
            assert ({k: v for k, v in got.json().items() if k not in drop}
                    == {k: v for k, v in want.json().items() if k not in drop})
            assert got.json()["requestId"] == got.headers["x-request-id"]
        else:
            assert got.content == want.content
        # One request-log line each, with the route that was called.
        logged = [json.loads(rec.getMessage()) for rec in caplog.records if rec.levelno == logging.INFO]
        assert [(e["path"], e["status"]) for e in logged] == [("/v1/fetch", want.status_code), (route, got.status_code)]
        assert logged[1]["rid"] == got.headers["x-request-id"] and logged[1]["outcome"] == got.headers.get("x-outcome")
    bad = client.post(route, content=b'{"url": ', headers={"content-type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"] == "validation_error"
    assert {k: bad.headers[k] for k in SECURITY} == SECURITY


@pytest.mark.parametrize("route", ROUTES)
def test_every_fetch_route_has_the_same_auth_and_rate_limit(route):
    with _client(api_keys={"k1"}) as c:
        denied = c.post(route, json={"url": "ftp://x"})
        allowed = c.post(route, json={"url": "ftp://x"}, headers={"authorization": "Bearer k1"})
    assert denied.status_code == 401 and denied.json()["error"] == "unauthorized"
    assert {k: denied.headers[k] for k in SECURITY} == SECURITY
    assert allowed.status_code == 400 and allowed.json()["error"] == "invalid_url"
    with _client(anon_rate_per_min=6) as c:  # a render costs 5 tokens, a plain fetch 1
        codes = [c.post(route, json={"url": "ftp://x", **extra}).status_code
                 for extra in ({"executeJS": True}, {}, {}, {"render": "always"})]
        limited = c.post(route, json={"url": "ftp://x"})
    assert codes == [400, 400, 429, 429]
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited" and "retry-after" in limited.headers
    with _client(anon_rate_per_min=6) as c:  # one bucket for all three names
        codes = [c.post(r, json={"url": "ftp://x"}).status_code for r in ROUTES * 3]
    assert codes == [400] * 6 + [429] * 3


def test_index_stays_the_index_and_names_the_aliases(client):
    r = client.get("/")
    assert r.status_code == 200 and r.json()["name"] == "webfetch"
    assert "content-security-policy" not in r.headers  # only the fetch routes serve third-party content
    mentions = [k for k in r.json()["endpoints"] if "POST /fetch" in k and "POST /" in k]
    assert len(mentions) == 1 and "/v1/fetch" in r.json()["endpoints"][mentions[0]]
    assert client.get("/fetch").status_code == 405 and client.put("/", json={"url": "ftp://x"}).status_code == 405
    assert client.get("/docs").status_code == 200 and "content-security-policy" not in client.get("/docs").headers


def test_openapi_describes_strings_fields_and_hides_the_aliases(client):
    spec = client.get("/openapi.json").json()
    assert "/v1/fetch" in spec["paths"] and "/fetch" not in spec["paths"]
    assert "post" not in spec["paths"].get("/", {})
    props = spec["components"]["schemas"]["FetchBody"]["properties"]
    for field in ("method", "body", "captureXHR", "screenshot", "actions", *IGNORED):
        text = props[field]["description"]
        assert "\n" not in text and "String" in text, field
    for field in IGNORED:
        assert "ignoredParameters" in props[field]["description"]
    assert set(FetchBody.model_fields) >= {  # every field of String's FetchRequest
        "url", "method", "body", "format", "markdownMode", "mainContentOnly", "jsonSchema", "executeJS", "requireWSS",
        "captureXHR", "headers", "countryCode", "solveCaptcha", "ignoreCertificateErrors", "screenshot", "actions"}


# --- PERF-9: repairing lone surrogates costs no Python call per escape --------------------

# What _loads_lenient did before: one callback per escaped backslash, pair or surrogate.
_OLD_ESCAPE = re.compile(r"\\\\|\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


def _python_calls(fn) -> tuple[int, object]:
    """How many Python-level function calls ``fn()`` makes, and its result."""
    calls = 0

    def count(_frame, event, _arg):
        nonlocal calls
        calls += event == "call"

    sys.setprofile(count)
    try:
        out = fn()
    finally:
        sys.setprofile(None)
    return calls, out


@pytest.mark.parametrize("name,expected", [
    ("\\\\" * 200_000 + "\\ud800", "\\" * 200_000 + "�"),
    ("\\ud800" * 100_000, "�" * 100_000),
    ("\\ud83d\\ude00" * 50_000 + "\\udc00", "\U0001f600" * 50_000 + "�"),
    ("\\\\\\ud800\\\\\\\\ud800\\ud83d\\ude00\\udc00x" * 30_000, "\\�\\\\ud800\U0001f600�x" * 30_000),
])
def test_surrogate_repair_makes_no_call_per_escape(name, expected):
    calls, data = _python_calls(lambda: extract._loads_lenient('{"@type":"Thing","name":"' + name + '"}'))
    assert data == {"@type": "Thing", "name": expected}
    # A few hundred when the patterns are first compiled; it was one per escaped backslash,
    # pair and surrogate (100,000 to 200,000 here).
    assert calls < 2_000, calls


def test_surrogate_repair_is_unchanged_on_any_mix_of_escapes():
    tokens = ["\\\\", "\\ud83d", "\\ude00", "\\uD83D", "\\uDE00", "\\udbff", "\\udfff", "\\ud800", "\\udc00", "\\u0041",
              "\\ud7ff", "\\ue000", "\\n", '\\"', "\\/", "ud83d", "u", "d", "x", " ", "é", "\\u00e9"]
    rng = random.Random(9)
    for _ in range(4000):
        raw = '{"s":"' + "".join(rng.choice(tokens) for _ in range(rng.randint(0, 12))) + '","t":["\\ud83d"]}'
        was = json.loads(_OLD_ESCAPE.sub(lambda m: "\\ufffd" if len(m.group()) == 6 else m.group(), raw))
        now = extract._loads_lenient(raw)
        assert now == was, raw
        json.dumps(now, ensure_ascii=False).encode("utf-8")  # and it can be written out


# --- PERF-10: readable mode resolves the common URL shapes without parsing each ------------

def _urljoin_loop(tree, page_url: str) -> None:
    """What _absolutize did before: urljoin for every attribute."""
    href = next((b.get("href") for b in tree.iter("base") if b.get("href") is not None), None)
    base = extract._base_from(href, page_url)
    for node in tree.iter("a", "img"):
        for attr in ("href",) if node.tag == "a" else ("src", "data-src"):
            value = node.get(attr)
            if value and not value.lstrip(extract._URL_STRIP).lower().startswith(extract._NOT_RESOLVED):
                url = extract._resolve(base, value)
                if url:
                    node.set(attr, url)


def _link_tree(values: list[str], base_href: str | None = None, images: bool = True):
    """An lxml document with one <a href> and one <img src data-src> per value."""
    from lxml import html as lhtml
    root = lhtml.fromstring("<html><head></head><body></body></html>")
    if base_href is not None:
        root[0].append(lhtml.Element("base", href=base_href))
    for i, value in enumerate(values):
        root[1].append(lhtml.Element("a", href=value))
        if images:
            root[1].append(lhtml.Element("img", {"src": value, "data-src": values[-1 - i]}))
    return root


def _url_attrs(tree) -> list[tuple[str | None, ...]]:
    return [(n.get("href"), n.get("src"), n.get("data-src")) for n in tree.iter("a", "img")]


def _count_urljoin(monkeypatch) -> list[str]:
    """The URLs handed to urljoin from here on."""
    seen: list[str] = []
    real = extract.urljoin
    monkeypatch.setattr(extract, "urljoin", lambda base, url: (seen.append(url), real(base, url))[1])
    return seen


_URL_PIECES = ["", "/", "//", "..", ".", "../", "./", "a", "b.html", "c d", "x;y", ";", "?", "#", "?q=1", "#f", ":", "a:b",
               "http:", "https:", "HTTP:", "http://", "https://", "//h.test", "h.test", "e.com", "[", "]", "[::1]", "@",
               "u:p@", ":80", "%2e%2e", "%20", "é", "日本", "\\", "\t", "\n", " ", "\x7f", "&", "=", "+", "~", "!", "*",
               "(", ")", ",", "javascript:", "data:", "mailto:x", "tel:1", "ftp://f.test/x", "..a", "a..", "...", "x", "0",
               "wiki/Talk:Foo", "File:x.png", "{", "|", "<", '"', " ", "​"]


@pytest.mark.parametrize("page_url,base_href", [
    ("https://e.com/a/b/c.html", None), ("https://e.com", None), ("http://e.com/a/b/?q=1#f", None),
    ("https://e.com/a//b/../c/d;p?q", None), ("http://[::1]:8000/a/", None), ("https://u:p@e.com:8443/x/y/", None),
    ("https://e.com/a/b/", "https://cdn.test/assets/v1/"), ("https://e.com/a/b/", "../up/"),
    ("https://e.com/a/b/", "//other.test/dir/"), ("https://e.com/a/b/", "?b=1"), ("https://e.com/a/b/", "javascript:0"),
    ("", None), ("about:blank", None), ("https://[bad/a/", None),
])
def test_absolutize_resolves_exactly_as_urljoin_does(page_url, base_href):
    rng = random.Random(f"{page_url} {base_href}")
    values = ["".join(rng.choice(_URL_PIECES) for _ in range(rng.choice([1, 1, 2, 2, 3, 3, 4, 5, 6]))) for _ in range(2500)]
    plain = sum(extract._PLAIN_URL.fullmatch(v) is not None for v in values)
    assert 300 < plain < 2200, plain  # both ways of resolving are exercised
    want, got = _link_tree(values, base_href), _link_tree(values, base_href)
    _urljoin_loop(want, page_url)
    extract._absolutize(got, page_url)
    assert _url_attrs(got) == _url_attrs(want)


def test_absolutize_parses_no_url_of_a_common_shape(monkeypatch):
    shapes = list(itertools.product(["", "/", "../", "./", "../../", "/wiki/Talk:", "https://cdn.test/", "http://e.com/a/../"],
                                    ["", "?q=1&u=http://x/", "#f", "?q#f:1", "/"]))
    values = [f"{lead}p{i}/{i}.html{tail}" for i, (lead, tail) in enumerate(shapes * 50)]
    values += ["#top", "?page=2", "?page=2#x", "/", "../", "./"]
    # A page cannot make the starts many: longer and mixed runs of dots are not "plain".
    values += ["../" * 8 + "deep.html", "../" * 9 + "deeper.html", "./../mixed.html", ".././mixed.html"] * 100
    want, got = _link_tree(values), _link_tree(values)
    _urljoin_loop(want, "https://e.com/a/b/c.html?x=1")
    parsed = _count_urljoin(monkeypatch)
    extract._absolutize(got, "https://e.com/a/b/c.html?x=1")
    assert _url_attrs(got) == _url_attrs(want)
    assert got[1][0].get("href") == "https://e.com/a/b/p0/0.html" and got[1][-1].get("src") == "https://e.com/a/mixed.html"
    # One probe per way a reference can start and one call per distinct other value, however
    # many URLs there are (7,218 here; it was one call each).
    assert sorted(parsed) == sorted(["#x", "../../x", "../x", "./x", "/x", "?x", "x", "../" * 8 + "x",
                                     "../" * 9 + "deeper.html", "./../mixed.html", ".././mixed.html"])


def test_absolutize_bounds_the_urls_it_parses(monkeypatch):
    monkeypatch.setattr(extract, "MAX_PARSED_URLS", 200)
    odd = [f"my file {i};v=1" for i in range(500)]  # a space and params: left to urljoin
    values = odd[:200] * 3 + odd[200:] + ["plain/after.html", "javascript:void(0)", "data:image/png;base64,AAAA"] + odd[:50]
    tree = _link_tree(values, images=False)
    parsed = _count_urljoin(monkeypatch)
    extract._absolutize(tree, "https://e.com/a/b/")
    assert len(parsed) == 201 and len(set(parsed)) == 201  # 200 distinct values and one probe, each once
    hrefs = [a.get("href") for a in tree.iter("a")]
    assert hrefs[:600] == [f"https://e.com/a/b/my file {i};v=1" for i in range(200)] * 3  # repeats cost nothing
    assert hrefs[600:900] == odd[200:]  # past the bound: as the page wrote them
    assert hrefs[900:903] == ["https://e.com/a/b/plain/after.html", "javascript:void(0)", "data:image/png;base64,AAAA"]
    assert hrefs[903:] == [f"https://e.com/a/b/my file {i};v=1" for i in range(50)]


def test_readable_markdown_is_unchanged_on_a_link_heavy_page(monkeypatch):
    links = "".join(f"<a href='../p{i}'>link {i}</a> <img src='i{i}.png' data-src='/lazy/l{i}.png' alt='i'> "
                    f"<a href='https://other.test/{i}?a=1#b'>o</a> <a href='doc {i}.pdf'>d</a>\n" for i in range(300))
    html = ("<html><head><base href='https://e.com/a/b/'></head><body><article><h1>Links</h1><p>"
            + "Plenty of ordinary article text. " * 20 + "</p><p>" + links + "</p></article></body></html>")
    now = extract.to_markdown(html, "https://e.com/x/", "readable")
    for target in ("https://e.com/a/p7", "https://e.com/lazy/l7.png", "https://other.test/7?a=1#b", "https://e.com/a/b/doc 7.pdf"):
        assert target in now, target
    monkeypatch.setattr(extract, "_absolutize", _urljoin_loop)
    assert now == extract.to_markdown(html, "https://e.com/x/", "readable")


# --- SEC-3: links() reads a link's scheme the way a browser does ---------------------------

@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "JavaScript:alert(2)", "java&#9;script:alert(3)", " javascript:alert(4)",
    "&#1;javascript:alert(5)", "java&#10;script:alert(6)", "&#1;&#31; j&#13;a&#10;v&#9;ascript&#9;:alert(7)",
    "vbscript:msgbox(8)", "VB&#9;Script:msgbox(9)", "data:text/html,<b>10</b>", "&#12;DATA:text/html;base64,PGI+",
    "d&#10;ata:,x", "mailto:a@b.test", "&#9;tel:+123", "#top", "&#11;#top", " &#10; ",
])
def test_links_skips_script_and_data_targets_however_they_are_spelled(href):
    tree = extract.parse(f'<html><body><a href="{href}">bad</a> <a href=" /ok&#10;/x?q=data:1#javascript: ">ok</a></body></html>')
    assert extract.links(tree, "https://e.com/a/") == [{"url": "https://e.com/ok/x?q=data:1#javascript:", "text": "ok"}]


def _without_label_join(monkeypatch, html: str, url: str) -> str:
    """Readable Markdown as it was before label/value pairs were joined."""
    with monkeypatch.context() as m:
        m.setattr(extract, "_join_labelled_links", lambda tree: None)
        return extract.to_markdown(html, url, "readable")


# --- Readable mode keeps a label's value when the value is a link ---------------------------

@pytest.mark.parametrize("page", ["NU_HOME", "ZEIT_PAGE", "PRODUCT_PAGE", "AGECHECK_PAGE", "CHALLENGE_PAGE"])
def test_readable_mode_is_unchanged_on_pages_without_such_pairs(monkeypatch, page):
    from tests import conftest
    html = getattr(conftest, page)
    assert extract.to_markdown(html, "https://e.com/a/", "readable") == _without_label_join(monkeypatch, html, "https://e.com/a/")


def test_readable_mode_joins_the_pairs_of_the_local_steam_page(monkeypatch):
    from tests.conftest import STEAM_PAGE
    url = "https://store.steampowered.com/app/271590/"
    now, was = extract.to_markdown(STEAM_PAGE, url, "readable"), _without_label_join(monkeypatch, STEAM_PAGE, url)
    assert "\nDeveloper:\n" in was and "Rockstar North" not in was
    assert "\nDeveloper: [Rockstar North](https://store.steampowered.com/dev/rockstar)\n" in now
    assert "\nPublisher: [Rockstar Games](https://store.steampowered.com/pub/rockstar)\n" in now
    assert "Release Date:" in now and "13 Apr, 2015" in now and now.count("](") == was.count("](") + 2


_TEXT = "<p>" + "Plenty of ordinary article text about the game and its world. " * 8 + "</p>"
_MORE = "<p>" + "A second paragraph that says something else about the studio. " * 8 + "</p>"
_NOT_PAIRS = [
    '<div>Follow us:</div><div><a href="/t">Twitter</a> <a href="/f">Facebook</a> <a href="/y">YouTube</a></div>',
    '<div>Pages:</div><div><a href="?p=1">1</a> <a href="?p=2">2</a></div>',
    '<div>Menu</div><div><a href="/a">Home</a></div>',  # not a label
    '<div>Source:</div><div>according to <a href="/s">the studio</a></div>',  # text of its own: kept or not as before
    '<div>Source:</div><div><a href="/s">the studio</a> said</div>',
    '<div>Developer:</div> and <div><a href="/d">Studio</a></div>',
    '<div><b>Developer:</b></div><div><a href="/d">Studio</a></div>',
    '<div>' + "A very long sentence that happens to end with a colon" + ':</div><div><a href="/d">Studio</a></div>',
    '<div>Developer:</div><div><a href="/d">' + "Studio " * 20 + '</a></div>',
    '<span>Developer:</span><span><a href="/d">Studio</a></span>',
    '<ul><li>Developer:</li><li><a href="/d">Studio</a></li></ul>',
    '<dl><dt>Developer:</dt><dd><a href="/d">Studio</a></dd></dl>',
    '<table><tr><td>Developer:</td><td><a href="/d">Studio</a></td></tr><tr><td>Genre:</td><td>RPG</td></tr></table>',
]


@pytest.mark.parametrize("markup", _NOT_PAIRS)
def test_label_join_leaves_everything_else_alone(monkeypatch, markup):
    from lxml import html as lhtml
    tree = lhtml.fromstring(f"<html><body><article>{_TEXT}{markup}{_MORE}</article></body></html>")
    before = lhtml.tostring(tree)
    extract._join_labelled_links(tree)
    assert lhtml.tostring(tree) == before
    html = f"<html><body><article><h1>Title of the piece</h1>{_TEXT}{markup}{_MORE}</article></body></html>"
    assert extract.to_markdown(html, "https://e.com/a/", "readable") == _without_label_join(monkeypatch, html, "https://e.com/a/")


@pytest.mark.parametrize("label,value", [("div", "div"), ("p", "div"), ("div", "p"), ("p", "p")])
@pytest.mark.parametrize("colon", [":", "："])
def test_label_join_writes_the_pair_as_one_paragraph(monkeypatch, label, value, colon):
    pair = (f'<div class="row"><{label} class="k"> Developer{colon} </{label}>\n'
            f'<{value} class="v">\n <a href="/dev/x">CD PROJEKT RED</a>\n</{value}> tail</div>')
    html = f"<html><body><article><h1>Title of the piece</h1>{_TEXT}{pair}{_MORE}</article></body></html>"
    from lxml import html as lhtml
    tree = lhtml.fromstring(html)
    extract._join_labelled_links(tree)
    assert lhtml.tostring(tree.find(".//div[@class='row']"), encoding="unicode") == (
        f'<div class="row"><p class="k">Developer{colon} <a href="/dev/x">CD PROJEKT RED</a>\n</p>\n tail</div>')
    now = extract.to_markdown(html, "https://e.com/a/", "readable")
    assert f"\nDeveloper{colon} [CD PROJEKT RED](https://e.com/dev/x)\n" in now
    assert "CD PROJEKT RED" not in _without_label_join(monkeypatch, html, "https://e.com/a/")
    assert now.count("Plenty of ordinary") == 8 and now.count("A second paragraph") == 8


@pytest.mark.parametrize("place", [
    "<header><nav>{}</nav></header><article><h1>Title of the piece</h1>" + _TEXT + _MORE + "</article>",
    "<article><h1>Title of the piece</h1>" + _TEXT + _MORE + "</article><footer><div class='footer'>{}</div></footer>",
    "<div id='content'><h1>Title of the piece</h1>" + _TEXT + _MORE + "</div><aside class='sidebar'>{}</aside>",
])
def test_label_join_brings_no_navigation_back(monkeypatch, place):
    html = "<html><body>" + place.format('<div>Sort by:</div><div><a href="?s=new">Newest first</a></div>'
                                         '<div>Account:</div><div><a href="/login">Sign in</a></div>') + "</body></html>"
    now = extract.to_markdown(html, "https://e.com/a/", "readable")
    assert "Newest first" not in now and "Sign in" not in now
    assert now == _without_label_join(monkeypatch, html, "https://e.com/a/")


def test_links_keeps_what_only_resembles_those_schemes():
    hrefs = ["javascript/x.html", "data/set.csv", "./data:1", "/javascript:void", "vbscript.html", "x#top", "?data:1",
             "https://e.com/javascript:alert(1)", "telephone.html", " #notananchor"]
    tree = extract.parse("<html><body>" + "".join(f'<a href="{h}">l</a>' for h in hrefs) + "</body></html>")
    assert [link["url"] for link in extract.links(tree, "https://e.com/a/")] == [
        "https://e.com/a/javascript/x.html", "https://e.com/a/data/set.csv", "https://e.com/a/data:1",
        "https://e.com/javascript:void", "https://e.com/a/vbscript.html", "https://e.com/a/x#top", "https://e.com/a/?data:1",
        "https://e.com/javascript:alert(1)", "https://e.com/a/telephone.html", "https://e.com/a/ #notananchor"]
