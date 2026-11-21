"""Regressions found by the independent check of the second review's fixes: schema filling
(defaults, the size of a result, reference lists), the String-compatible request fields and
readable-mode label joining."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import fetcher as fetcher_mod
from webfetch.api import create_app
from webfetch.config import Settings
from webfetch.schema_fill import build_candidates, fill_schema

BASE = "https://www.example.test/page"
NAME_LIKE = ["name", "title", "headline", "heading", "productTitle", "productName", "gameTitle", "gameName",
             "appName", "articleTitle", "pageTitle", "itemName"]


def _obj(**props) -> dict:
    return {"type": "object", "properties": props}


def _fill(schema: dict, *nodes: dict, metadata: dict | None = None) -> dict:
    return fill_schema(schema, build_candidates(jsonld=list(nodes), microdata=[], metadata=metadata or {}), BASE)


def _response_bytes(result: dict) -> int:
    """What the result takes in a response: the JSON encoding the API writes."""
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ld_page(*nodes: dict) -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)
    return f"<html><head>{scripts}</head><body><p>{'Some text here. ' * 30}</p></body></html>"


@pytest.fixture(scope="module")
def pages():
    """A local server for this file's own pages: ``url = pages(path, html)``."""
    served: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = served.get(self.path, b"not found")
            self.send_response(200 if self.path in served else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def add(path: str, html: str) -> str:
        served[path] = html.encode()
        return f"http://pages.test:{srv.server_address[1]}{path}"
    yield add
    srv.shutdown()


@pytest.fixture()
def client(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)
    with TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000)),
                    raise_server_exceptions=False) as c:
        yield c


def _post_raw(client, url: str, schema_json: str, route: str = "/v1/fetch"):
    """A request whose jsonSchema is written by hand: NaN and 1e999 are not what json.dumps of
    a test's dict would send for every client, but they are what a Python client sends."""
    body = '{"url": %s, "include": [], "jsonSchema": %s}' % (json.dumps(url), schema_json)
    return client.post(route, content=body, headers={"content-type": "application/json"})


# ----------------------------------------------------------------------------- schema defaults

@pytest.mark.parametrize("default", [float("nan"), float("inf"), float("-inf"), [1.0, float("inf")],
                                     {"limits": {"max": float("nan")}}])
def test_a_default_that_json_cannot_carry_is_not_used(default):
    # "score: float = float('nan')" and "max_price: float = math.inf" are ordinary Pydantic
    # defaults; the response cannot spell either, so the member stays null as it did without one.
    schema = _obj(name={"type": "string"}, score={"type": "number", "default": default},
                  tags={"type": "array", "items": {"type": "string"}, "default": ["a"]})
    out = _fill(schema, {"@type": "Product", "name": "Widget"})
    assert out["data"] == {"name": "Widget", "score": None, "tags": ["a"]}
    assert out["missing"] == ["score", "tags"] and "truncated" not in out
    json.dumps(out, allow_nan=False)  # what the API does with it


@pytest.mark.parametrize("default", ["NaN", "Infinity", "-Infinity", "1e999", "[1e999]", '{"a": {"b": NaN}}'])
def test_a_non_finite_default_through_the_api_is_null_not_a_500(pages, client, default):
    url = pages("/widget", _ld_page({"@type": "Product", "name": "Widget"}))
    schema = '{"type":"object","properties":{"name":{"type":"string"},"score":{"default": %s}}}' % default
    r = _post_raw(client, url, schema)  # 500 internal_error (ValueError): the envelope could not be written
    assert r.status_code == 200, r.text[:300]
    assert r.json()["extracted"]["data"] == {"name": "Widget", "score": None}
    batch = client.post("/v1/batch", headers={"content-type": "application/json"},
                        content='{"requests": [{"url": %s, "include": [], "jsonSchema": %s}]}' % (json.dumps(url), schema))
    assert [item["httpStatus"] for item in batch.json()["results"]] == [200]


# ----------------------------------------------------------------------------- the size of a result

def _groups(n: int, names: list[str]) -> dict:
    """n object properties that are one $def with ``names`` as its string properties."""
    return {"type": "object", "properties": {f"g{i}": {"$ref": "#/$defs/G"} for i in range(n)},
            "$defs": {"G": _obj(**{name: {"type": "string"} for name in names})}}


@pytest.mark.parametrize("char,per_char", [("\x01", 6), ("€", 3), ("\U0001f600", 4), ('"', 2), ("a", 1)])
def test_the_result_budget_counts_bytes_of_json_not_characters(char, per_char):
    # One 5,200-character name asked for under 192 names. Counted in characters, the control
    # characters of a 32 KB page came back as 5.9 MB (6 bytes each), and a 20-item batch as 119 MB.
    value = char * 5200
    out = _fill(_groups(16, NAME_LIKE), {"@type": "Product", "name": value})
    assert out["truncated"] is True and _response_bytes(out) <= 1_050_000
    filled = [v for group in out["data"].values() if group for v in group.values() if v is not None]
    assert filled and set(filled) == {value}
    assert len(filled) in range(900_000 // (5200 * per_char) - 1, 1_000_000 // (5200 * per_char) + 1)


def test_property_names_and_page_keys_are_counted_as_they_are_written():
    # Paths are returned too, once per member: 40 control characters are 240 bytes each time.
    key = "\x02" * 40
    schema = {"type": "object", "properties": {"root": {"$ref": "#/$defs/D0"}}, "$defs": {
        **{f"D{i}": {"properties": {f"{key}{j}": {"$ref": f"#/$defs/D{i + 1}"} for j in range(3)}} for i in range(6)},
        "D6": {"properties": {f"{key}{j}": {} for j in range(3)}}}}
    out = _fill(schema, {"@type": "Product", "name": "Widget"})
    assert out["truncated"] is True and _response_bytes(out) <= 1_050_000  # 2.4 MB counted in characters
    out = _fill(_obj(**{f"p{i}": {} for i in range(150)}), {"@type": "Product", **{f"p{i}": "v" for i in range(150)}})
    assert "truncated" not in out and len(out["sources"]) == 150


def test_a_large_ordinary_result_is_not_truncated():
    # 300 reviews of 1,200 characters, non-ASCII included, with their authors: about 0.5 MB.
    reviews = [{"@type": "Review", "name": f"Review {i}", "reviewBody": "Sehr schön, gerne wieder. " * 46,
                "author": {"@type": "Person", "name": f"Jürgen {i}"}} for i in range(300)]
    schema = _obj(name={"type": "string"}, review={"type": "array", "items": _obj(
        name={"type": "string"}, reviewBody={"type": "string"}, author=_obj(name={"type": "string"}))})
    out = _fill(schema, {"@type": "Product", "name": "Widget", "review": reviews})
    assert "truncated" not in out and len(out["data"]["review"]) == 300 and out["missing"] == []
    assert out["data"]["review"][299] == {"name": "Review 299", "reviewBody": reviews[299]["reviewBody"].strip(),
                                           "author": {"name": "Jürgen 299"}}


def test_control_characters_through_the_api_stay_within_the_budget(pages, client):
    url = pages("/control", _ld_page({"@type": "Product", "name": "\x01" * 5200}))
    r = client.post("/v1/fetch", json={"url": url, "include": [], "jsonSchema": _groups(16, NAME_LIKE)})
    assert r.status_code == 200 and r.json()["extracted"]["truncated"] is True
    assert len(r.content) <= 1_100_000  # 5,938,829 bytes for this 32 KB page


# ----------------------------------------------------------------------------- reference lists

def _catalogue(n: int, key: str = "itemListElement") -> list[dict]:
    """A category page as an @graph: a list of n @id references, then the n products."""
    return [{"@type": "ItemList", "name": "Anvils", key: [{"@id": f"#p{i}"} for i in range(n)]}] + [
        {"@type": "Product", "@id": f"#p{i}", "name": f"Anvil {i}", "sku": f"A-{i}",
         "offers": {"@type": "Offer", "price": 10 + i}} for i in range(n)]


PRODUCTS = {"type": "array", "items": _obj(name={"type": "string"}, sku={"type": "string"},
                                           offers=_obj(price={"type": "number"}))}


@pytest.mark.parametrize("n", [200, 201, 350, 1000])
def test_an_array_is_filled_from_every_entry_of_a_reference_list(n):
    # Past its 200th entry the list was dropped without a word: 1,000 products came back as
    # 200, with no "truncated".
    graph = _catalogue(n) + [{"@type": "CollectionPage", "hasPart": [{"@id": f"#p{i}"} for i in range(n)]}]
    out = _fill(_obj(itemListElement=PRODUCTS, hasPart={"type": "array", "items": {"type": "string"}}), *graph)
    items = out["data"]["itemListElement"]
    assert len(items) == n and "truncated" not in out and out["missing"] == []
    assert items[n - 1] == {"name": f"Anvil {n - 1}", "sku": f"A-{n - 1}", "offers": {"price": 10.0 + n - 1}}
    assert out["data"]["hasPart"] == [f"Anvil {i}" for i in range(n)]


def test_the_rest_of_a_reference_list_keeps_what_the_first_entries_do():
    # Beyond the 200th entry too: a node named twice is one item, an entry that is no
    # reference stays what it is, and a reference back to the node being read is not followed.
    refs = [{"@id": f"#p{i % 250}"} for i in range(500)] + [{"name": "inline"}, {"@id": "#list"}, {"@id": "#nowhere"}]
    graph = _catalogue(250)
    graph[0].update({"@id": "#list", "itemListElement": refs})
    out = _fill(_obj(itemListElement={"type": "array", "items": _obj(name={"type": "string"})}), *graph)
    names = [item["name"] for item in out["data"]["itemListElement"]]
    assert names == [f"Anvil {i}" for i in range(250)] + ["inline", None, None] and "truncated" not in out


def test_a_reference_list_is_read_whole_only_where_an_array_asks_for_it(monkeypatch):
    from webfetch import schema_fill
    reads = {"n": 0}
    real = schema_fill._ref_id

    def counted(v):
        reads["n"] += 1
        return real(v)
    monkeypatch.setattr(schema_fill, "_ref_id", counted)
    n = 30_000
    graph = _catalogue(n, key="hasPart")
    cands = build_candidates(jsonld=graph, microdata=[], metadata={})
    assert reads["n"] <= 400  # collecting candidates reads 200 entries of a list, however long
    out = fill_schema(_obj(name={"type": "string"}, hasPart=_obj(name={"type": "string"})), cands, BASE)
    assert out["data"] == {"name": "Anvil 0", "hasPart": {"name": "Anvil 0"}} and reads["n"] <= 400
    # 150 arrays that each ask for all 30,000 entries: the work budget pays for three of them.
    schema = {"type": "object", "properties": {f"g{i}": {"$ref": "#/$defs/G"} for i in range(150)},
              "$defs": {"G": _obj(hasPart={"type": "array", "items": {"type": "string"}, "maxItems": 2})}}
    out = fill_schema(schema, cands, BASE)
    assert out["truncated"] is True and reads["n"] <= 400 + 4 * n
    assert out["data"]["g0"] == {"hasPart": ["Anvil 0", "Anvil 1"]} and out["data"]["g149"] is None


# ----------------------------------------------------------------------------- the GET form

@pytest.mark.parametrize("extra", [{"executeJS": "true"}, {"screenshot": "true"}, {"method": "POST"},
                                   {"countryCode": "de"}, {"mainContentOnly": "true"}, {"timeout": "5"},
                                   {"jsonSchema": "{}", "solveCaptcha": "true"}])
def test_the_get_form_refuses_a_body_field_it_does_not_read(pages, client, extra):
    # Each was answered 200 as if it had been applied: no render, no picture, and no
    # ignoredParameters, where the same field in a POST is applied, refused or reported.
    url = pages("/widget", _ld_page({"@type": "Product", "name": "Widget"}))
    r = client.get("/v1/fetch", params={"url": url, **extra})
    assert r.status_code == 400, r.text[:300]
    body = r.json()
    assert body["error"] == "validation_error" and [i["field"] for i in body["issues"]] == sorted(extra)
    assert all(name in body["message"] for name in extra) and "POST" in body["message"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_get_form_reads_its_own_parameters_and_ignores_strangers(pages, client):
    url = pages("/widget", _ld_page({"@type": "Product", "name": "Widget"}))
    r = client.get("/v1/fetch", params={"url": url, "format": "json", "markdownMode": "readable", "render": "never",
                                        "resolveGates": "false", "include": "links,metadata", "utm_source": "x", "_": "1"})
    assert r.status_code == 200 and r.json()["outcome"] == "ok" and "links" in r.json()
    assert client.get("/v1/fetch", params={"url": url, "format": "markdown"}).text.startswith("Some text here.")


# ----------------------------------------------------------------------------- what a schema may hold

@pytest.mark.parametrize("schema", [
    '{"type":"object","properties":{"a\\ud83d":{"type":"string"}}}',
    '{"type":"object","properties":{"offers":{"type":"object","properties":{"\\udc00":{"type":"string"}}}}}',
    '{"type":"object","properties":{"name":{"type":"string","default":"\\ud800"}}}',
    '{"$ref":"#/$defs/M","$defs":{"M":{"type":"object","properties":{"tags":{"default":["ok","\\udfff"]}}}}}',
])
@pytest.mark.parametrize("route", ["/v1/fetch", "/fetch"])
def test_a_schema_with_an_unpaired_surrogate_is_a_400(pages, client, schema, route):
    # Its property names and defaults are returned, and the response could not be encoded:
    # 500 internal_error (UnicodeEncodeError).
    r = _post_raw(client, pages("/widget", _ld_page({"@type": "Product", "name": "Widget"})), schema, route)
    assert r.status_code == 400, r.text[:300]
    body = r.json()
    assert body["error"] == "validation_error" and body["issues"][0]["field"] == "jsonSchema"
    assert "surrogate" in body["message"]


def test_a_schema_with_any_other_text_is_accepted(pages, client):
    schema = _obj(**{"名前": {"type": "string", "description": "the title"},
                     "emoji \U0001f600": {"default": "\U0001f600 \x00"}, "name": {"enum": ["Widget", "é"]}})
    r = client.post("/v1/fetch", json={"url": pages("/widget", _ld_page({"@type": "Product", "name": "Widget"})),
                                       "include": [], "jsonSchema": schema})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["extracted"]["data"] == {"名前": "Widget", "emoji \U0001f600": "\U0001f600 \x00", "name": "Widget"}
