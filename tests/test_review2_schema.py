"""Schema-filling regressions from the second review: one fill does bounded work and returns a
bounded result, for any schema the API accepts on any page.

A hostile case that the budgets stop used to run for minutes and gigabytes. The ``work`` fixture
counts what the filler does and aborts past a bound, so such a test fails within seconds on
code without the budget instead of taking the machine down.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Optional

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests import fakedns
from webfetch import extract
from webfetch import fetcher as fetcher_mod
from webfetch import schema_fill
from webfetch.api import FetchBody, create_app
from webfetch.config import Settings
from webfetch.schema_fill import SchemaFiller, build_candidates, fill_schema, site_candidates

MAX_FIELDS = 10_000  # schema members one call fills
NAME_LIKE = ["name", "title", "headline", "heading", "productTitle", "productName", "gameTitle", "appName"]
EXAMPLE_COM = ("<html><head><title>Example Domain</title></head>"
               "<body><h1>Example Domain</h1><p>This domain is for use in examples.</p></body></html>")


def _ld(*nodes: dict, head: str = "", body: str = "") -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)
    return f"<html><head>{head}{scripts}</head><body>{body}</body></html>"


def _cands(html: str, base: str = "https://www.example.test/page") -> list:
    tree = extract.parse(html)
    return build_candidates(jsonld=extract.json_ld(tree), microdata=extract.microdata(tree, base),
                            metadata=extract.page_metadata(tree, base), site=site_candidates(tree, base))


def _fill(html: str, schema: dict, base: str = "https://www.example.test/page") -> dict:
    return fill_schema(schema, _cands(html, base), base)


def _accepted(schema: dict) -> dict:
    """The schema, after the API's own validation: every hostile schema here is one it accepts."""
    return FetchBody(url="https://example.com/", jsonSchema=schema).jsonSchema


def _members(data) -> int:
    """Schema members present in a result: every key of every object, at any depth."""
    if isinstance(data, dict):
        return len(data) + sum(_members(v) for v in data.values())
    return sum(_members(v) for v in data) if isinstance(data, list) else 0


@pytest.fixture()
def work(monkeypatch):
    counts = {"objects": 0, "lookups": 0}
    started = time.monotonic()

    def counted(name: str, key: str):
        real = getattr(SchemaFiller, name)

        def wrapper(self, *args, **kwargs):
            counts[key] += 1
            assert counts[key] <= 4 * MAX_FIELDS, f"unbounded: {counts[key]} {key} in one fill"
            assert time.monotonic() - started < 20, f"unbounded: {counts} and still filling"
            return real(self, *args, **kwargs)
        monkeypatch.setattr(SchemaFiller, name, wrapper)

    counted("_fill_object", "objects")
    counted("_ranked", "lookups")
    return counts


# ----------------------------------------------------------------------------- A: $ref fan-out

def _self_ref(f: int, defs: bool) -> dict:
    """f name-like properties, each the model itself: through "#" or through one $def."""
    ref = "#/$defs/T" if defs else "#"
    props = {n: {"$ref": ref} for n in NAME_LIKE[:f]}
    return {"type": "object", "properties": props,
            **({"$defs": {"T": {"type": "object", "properties": dict(props)}}} if defs else {})}


def _ref_chain(f: int, names: list[str] | None = None) -> dict:
    """Distinct $defs D0..D11, each with f properties that are the next one: f^12 members."""
    names = names or [f"p{j}" for j in range(f)]
    defs = {f"D{i}": {"properties": {n: {"$ref": f"#/$defs/D{i + 1}"} for n in names}} for i in range(12)}
    defs["D12"] = {"properties": {"leaf": {}}}  # no "type" anywhere: 7 properties are exactly the 200 keys
    return {"type": "object", "properties": {"root": {"$ref": "#/$defs/D0"}}, "$defs": defs}


@pytest.mark.parametrize("defs", [False, True])
@pytest.mark.parametrize("f", range(2, 9))
def test_a_string_names_one_object_not_the_model_inside_itself(f, defs, work):
    # The page title, re-wrapped as an object's name, matched every name-like property again at
    # every level: 3 properties were 1,594,323 paths and a 172 MB response.
    out = _fill(EXAMPLE_COM, _accepted(_self_ref(f, defs)))
    names = NAME_LIKE[:f]
    assert out["data"] == {n: dict.fromkeys(names) for n in names}
    assert out["missing"] == [f"{a}.{b}" for a in names for b in names] and out["sources"] == {}
    assert "truncated" not in out and work["objects"] == 1 + f


def test_recursive_models_still_follow_the_page():
    class Tree(BaseModel):  # Pydantic emits a root $ref: the model is active from the first level
        name: str
        parent: Optional["Tree"] = None

    kid = {"@type": "Thing", "name": "kid"}
    # A parent given as a string is still that object's name; the string is used once.
    out = _fill(_ld({**kid, "parent": "Dad"}), Tree.model_json_schema())
    assert out["data"] == {"name": "kid", "parent": {"name": "Dad", "parent": None}}
    assert out["sources"]["parent.name"] == "json-ld:Thing.parent" and out["missing"] == ["parent.parent"]
    out = _fill(_ld({**kid, "parent": {"name": "Dad", "parent": {"name": "Gran"}}}), Tree.model_json_schema())
    assert out["data"] == {"name": "kid", "parent": {"name": "Dad", "parent": {"name": "Gran", "parent": None}}}

    class Titled(BaseModel):  # the review's model: two members of its own type with name-like names
        name: str
        title: Optional["Titled"] = None
        headline: Optional["Titled"] = None

    out = _fill(EXAMPLE_COM, Titled.model_json_schema())
    inner = {"name": "Example Domain", "title": None, "headline": None}
    assert out["data"] == {"name": "Example Domain", "title": inner, "headline": inner}
    assert len(out["missing"]) == 4 and "truncated" not in out


def _check_truncated(out: dict, schema: dict) -> None:
    assert out["truncated"] is True
    assert len(out["missing"]) + len(out["sources"]) <= MAX_FIELDS and _members(out["data"]) <= 2 * MAX_FIELDS
    assert len(json.dumps(out)) < 1_000_000
    assert len(set(out["missing"])) == len(out["missing"])


@pytest.mark.parametrize("f", range(3, 8))
def test_ref_chain_fan_out_stops_at_the_member_budget(f, work):
    schema = _accepted(_ref_chain(f))  # 3 properties: 531,441 paths and 32 MB; 4: no end within a minute
    out = _fill(EXAMPLE_COM, schema)
    _check_truncated(out, schema)
    assert work["objects"] <= MAX_FIELDS and out["sources"] == {}
    # The result keeps its shape: every object has all its keys, null where the budget ended.
    names, node, depth = [f"p{j}" for j in range(f)], out["data"]["root"], 0
    while isinstance(node, dict):
        assert list(node) == names
        node, depth = node[names[-1]], depth + 1
    assert node is None and depth == 1  # the last property of D0 was never reached
    first = out["data"]["root"]
    for _ in range(11):
        first = first["p0"]
    assert first == dict.fromkeys(names)  # ... while the first branch was filled to the nesting limit
    assert out["missing"][0] == "root" + ".p0" * 12


def test_a_chain_within_the_budget_is_filled_as_before(work):
    out = _fill(EXAMPLE_COM, _accepted(_ref_chain(2)))
    assert "truncated" not in out and len(out["missing"]) == 4096 and work["objects"] == 4096


def test_fan_out_along_a_string_stops_at_the_member_budget(work):
    # No $ref repeats, so every level takes the title as its name again.
    schema = _ref_chain(3, NAME_LIKE[:3])
    schema["properties"] = {"name": schema["properties"]["root"]}
    schema["$defs"]["D12"] = {"properties": {"name": {"type": "string"}}}
    _check_truncated(_fill(EXAMPLE_COM, _accepted(schema)), schema)


def _dense_page() -> list:
    """A page at the candidate cap: one JSON-LD node with 60,000 keys."""
    cands = build_candidates(jsonld=[{"@type": "Product", "name": "Anvil", "sku": "A-1", "k7": {"k8": "deep"},
                                      **{f"x{i}": "v" for i in range(60_000)}}],
                             microdata=[], metadata={"title": "Anvil | Shop"})
    assert len(cands) >= schema_fill._MAX_CANDS
    return cands


def test_fan_out_on_a_page_at_the_candidate_cap(work):
    # A cap on members alone leaves 10,000 lookups of ~1 ms each over the 50,000 candidates.
    cands = _dense_page()
    t = time.monotonic()
    for schema in (_ref_chain(4), _ref_chain(7), _ref_chain(4, ["k7", "x1", "x2", "name"]), _self_ref(8, False)):
        out = fill_schema(_accepted(schema), cands, "https://x.test/")
        assert len(json.dumps(out)) < 1_000_000
    assert time.monotonic() - t < 5  # 0.15 s here


def test_a_flat_200_key_schema_on_a_page_at_the_candidate_cap_is_not_truncated():
    cands = _dense_page()
    schema = _accepted({"type": "object", "properties": {f"p{i}": {} for i in range(198)}})
    out = fill_schema(schema, cands, "https://x.test/")
    assert "truncated" not in out and len(out["missing"]) == 198
    schema = _accepted({"type": "object", "properties": {
        "name": {"type": "string"}, "sku": {"type": "string"}, "k7": {"type": "object", "properties": {"k8": {}}},
        **{f"x{i}": {"type": "string"} for i in range(0, 60_000, 1000)}, **{f"p{i}": {} for i in range(60)}}})
    out = fill_schema(schema, cands, "https://x.test/")
    assert "truncated" not in out and len(out["sources"]) == 2 + 1 + 50 and len(out["missing"]) == 10 + 60
    assert out["data"]["name"] == "Anvil" and out["data"]["k7"] == {"k8": "deep"} and out["data"]["x49000"] == "v"


def test_one_long_value_under_many_names_does_not_multiply():
    # 90 properties described as the title, on a page whose name is 200,000 characters long.
    cands = build_candidates(jsonld=[{"@type": "Product", "name": "n" * 200_000, "sku": "A-1"}], microdata=[],
                             metadata={})
    schema = _accepted({"type": "object", "properties": {
        "sku": {"type": "string"}, **{f"t{i}": {"description": "title"} for i in range(90)}}})
    out = fill_schema(schema, cands, "https://x.test/")
    assert out["truncated"] is True and len(json.dumps(out)) < 1_100_000  # was 18 MB
    assert out["data"]["sku"] == "A-1" and out["data"]["t0"] == "n" * 200_000 and out["data"]["t89"] is None
    assert list(out["data"]) == list(schema["properties"]) and out["missing"] == []


def test_long_property_names_do_not_multiply_along_the_paths(work):
    # "missing" repeats every name on the way down: 2 names of 1,000 characters were 50 MB of paths.
    schema = _accepted(_ref_chain(2, ["a" * 1000, "b" * 1000]))
    out = _fill(EXAMPLE_COM, schema)
    assert out["truncated"] is True and len(json.dumps(out)) < 1_100_000


def test_a_long_list_is_deduplicated_in_linear_time():
    words = [f"k{i}" for i in range(40_000)]  # was quadratic: 15 s for these
    cands = build_candidates(jsonld=[{"@type": "Article", "headline": "H", "keywords": words + words[:10]}],
                             microdata=[], metadata={})
    t = time.monotonic()
    out = fill_schema({"type": "object", "properties": {"keywords": {"type": "array", "items": {"type": "string"}}}},
                      cands, "https://x.test/")
    assert out["data"]["keywords"] == words and "truncated" not in out
    assert time.monotonic() - t < 2  # 0.05 s here


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"a": {"$ref": "#/properties/b/anyOf/" + "9" * 5000}, "b": {"anyOf": [{}, {}]}}},
    {"type": "object", "properties": {"a": {"$ref": "#/properties/b/anyOf/²"}, "b": {"anyOf": [{}, {}]}}},
    {"type": "object", "properties": {"a": {"description": {"x": ["name"]}}, "b": {"description": 5}}},
])
def test_malformed_refs_and_descriptions_do_not_raise(schema):
    out = _fill(EXAMPLE_COM, schema)
    assert out["data"] == {"a": None, "b": None}


@pytest.fixture()
def client(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)
    with TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000)),
                    raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("schema,truncated", [
    (_self_ref(3, False), False),  # the review's 108 bytes: 172 MB after 9 s
    (_self_ref(8, True), False),
    (_ref_chain(4), True),  # 2 KB: over a minute and the container's memory
    (_ref_chain(7), True),
])
def test_fan_out_schemas_through_the_api(server, client, work, schema, truncated):
    t = time.monotonic()
    r = client.post("/v1/fetch", json={"url": f"http://echo.test:{server.server_address[1]}/product",
                                       "jsonSchema": schema, "include": [], "timeout": 5})
    assert r.status_code == 200 and time.monotonic() - t < 5, r.text[:300]
    body = r.json()
    assert "warnings" not in body and len(r.content) < 1_000_000
    assert body["extracted"].get("truncated", False) is truncated
    assert len(body["extracted"]["missing"]) <= MAX_FIELDS


# ----------------------------------------------------------------------------- B: what a page can cost

ONE = {"type": "object", "properties": {"name": {"type": "string"}}}
ITEMS_OF_ITEMS = {"type": "object", "properties": {"items": {"type": "array", "items": {"properties": {
    "x": {"type": "string"}, "items": {"type": "array", "items": {"properties": {"x": {"type": "string"}}}}}}}}}


@pytest.fixture()
def refs_read(monkeypatch):
    """Counts the list items and objects checked for being an @id reference."""
    counts = {"n": 0}
    real = schema_fill._ref_id

    def counted(v):
        counts["n"] += 1
        assert counts["n"] <= 1_000_000, "unbounded: a million reference checks for one page"
        return real(v)
    monkeypatch.setattr(schema_fill, "_ref_id", counted)
    return counts


def test_a_node_named_many_times_in_reference_lists_is_walked_once(refs_read):
    # 200 references to b, which holds 200 to c: c was walked about 25,000 times. Its 40 x 40
    # numbers yield no candidate, so the candidate cap did not stop it (7 KB: 4.6 s; 85 KB: minutes).
    graph = [{"@id": "a", "@type": "Product", "name": "p", "l": [{"@id": "b"}] * 200},
             {"@id": "b", "@type": "Thing", "l": [{"@id": "c"}] * 200},
             {"@id": "c", "@type": "Thing", "y": [[1] * 40] * 40, "z": "deep"}]
    cands = build_candidates(jsonld=graph, microdata=[], metadata={})
    assert refs_read["n"] <= 2000 and len(cands) < 40
    assert [c.path for c in cands if c.key == "z"] == ["Product.l.l.z", "Thing.l.z", "Thing.z"]
    assert fill_schema(ONE, cands, "https://x.test/")["data"] == {"name": "p"}


def test_two_long_reference_lists_naming_each_other_are_read_once(refs_read):
    n = 20_000  # every walk scanned both whole lists: 5,000,000 checks for this page, 21 s for a 5.9 MB one
    graph = [{"@id": "a", "@type": "Product", "name": "p", "x": [{"@id": "b"}] * n},
             {"@id": "b", "@type": "Thing", "x": [{"@id": "a"}] * n}]
    cands = build_candidates(jsonld=graph, microdata=[], metadata={})
    assert refs_read["n"] <= 2000
    assert fill_schema(ONE, cands, "https://x.test/")["data"] == {"name": "p"}


def test_distinct_nodes_behind_reference_lists_stop_at_the_step_budget():
    # 90 nodes that each list the same 90 others: no node is named twice in a list, yet each of
    # those is reached 90 times, with 30 x 30 numbers to walk every time.
    graph = ([{"@id": "a", "@type": "Product", "name": "p", "l": [{"@id": f"b{i}"} for i in range(90)]}]
             + [{"@id": f"b{i}", "@type": "Thing", "l": [{"@id": f"c{j}"} for j in range(90)]} for i in range(90)]
             + [{"@id": f"c{j}", "@type": "Thing", "y": [[1] * 30] * 30} for j in range(90)])
    t = time.monotonic()
    cands = build_candidates(jsonld=graph, microdata=[], metadata={})
    assert time.monotonic() - t < 1.5  # 0.08 s here, 2.2 s before
    assert len(cands) <= schema_fill._MAX_CANDS
    assert fill_schema(ONE, cands, "https://x.test/")["data"] == {"name": "p"}


def test_array_items_do_not_multiply_through_repeated_references(work):
    n = 300  # 1,000 x 1,000 (a 24 KB page) was 1,000,000 item fills, 6.5 s and a 37 MB response
    graph = [{"@id": "a", "@type": "Product", "name": "p", "items": [{"@id": "b"}] * n},
             {"@id": "b", "@type": "Thing", "x": "v", "items": [{"@id": "c"}] * n},
             {"@id": "c", "@type": "Thing", "x": "w"}]
    out = fill_schema(ITEMS_OF_ITEMS, build_candidates(jsonld=graph, microdata=[], metadata={}), "https://x.test/")
    # The node named 300 times is one item. (Its own list keeps its 300 entries: references are
    # followed from the page's nodes, not inside an item that is being filled.)
    assert len(out["data"]["items"]) == 1 and out["data"]["items"][0]["x"] == "v"
    assert len(out["missing"]) == n and "truncated" not in out
    assert out["sources"] == {"items": "json-ld:Product.items", "items[0].x": "json-ld:Product.items.x",
                              "items[0].items": "json-ld:Product.items.items"}


def test_array_items_of_distinct_nodes_stop_at_the_member_budget(work):
    n = 150  # 150 distinct items, each with 150 items of its own: 22,500 objects to fill
    graph = ([{"@id": "a", "@type": "Product", "name": "p", "items": [{"@id": f"b{i}"} for i in range(n)]}]
             + [{"@id": f"b{i}", "@type": "Thing", "x": f"v{i}", "items": [{"x": f"w{j}"} for j in range(n)]}
                for i in range(n)])
    out = fill_schema(ITEMS_OF_ITEMS, build_candidates(jsonld=graph, microdata=[], metadata={}), "https://x.test/")
    assert out["truncated"] is True and len(out["sources"]) <= MAX_FIELDS and len(json.dumps(out)) < 1_000_000
    assert _members(out["data"]) <= MAX_FIELDS
    items = out["data"]["items"]
    assert 20 < len(items) < n and all(set(i) == {"x", "items"} for i in items)  # cut short, never misshapen
    assert items[0]["x"] == "v0" and items[0]["items"] == [{"x": f"w{j}"} for j in range(n)]
    assert out["sources"]["items[0].items[149].x"] == "json-ld:Product.items.items.x"


def test_type_markers_count_against_the_candidate_cap():
    nodes = [{"@type": "A"} for _ in range(120_000)]  # one marker each: 421,429 candidates from a 5.9 MB page
    cands = build_candidates(jsonld=nodes, microdata=[], metadata={"title": "T"})
    assert len(cands) <= schema_fill._MAX_CANDS + 10
    schema = {"type": "object", "properties": {"title": {"type": "string"}, **{f"f{i}": {} for i in range(190)}}}
    out = fill_schema(schema, cands, "https://x.test/")
    assert out["data"]["title"] == "T" and "truncated" not in out  # page-level facts are not crowded out


def test_a_node_with_100000_types_is_read_for_its_first_ones():
    node = {"@type": ["Product"] + [f"t{i:06d}" for i in range(100_000)], "name": "x"}
    cands = build_candidates(jsonld=[node], microdata=[], metadata={})
    assert max(len(c.value) for c in cands if c.key == "@type") <= 20
    # Every nullable object the page does not carry was compared with every type: 95 x 590,000.
    filler = SchemaFiller(cands, "https://x.test/")
    assert len(filler.main_types) <= 64 and filler._names_main_entity("product")
    schema = {"type": "object", "properties": {"product": {"$ref": "#/$defs/N"}, "video": {"$ref": "#/$defs/N"}},
              "$defs": {"N": {"type": ["object", "null"], "properties": {"name": {"type": "string"}}}}}
    assert fill_schema(schema, cands, "https://x.test/")["data"] == {"product": {"name": "x"}, "video": None}


def test_meta_tags_and_long_keys_are_capped():
    og = {f"og:x{i}": "v" for i in range(5000)}
    cands = build_candidates(jsonld=[], microdata=[],
                             metadata={"openGraph": {"og:title": "T", **og}, "twitter": dict(og)})
    assert len(cands) <= 3 * 200 and fill_schema(ONE, cands, "https://x.test/")["data"] == {"name": "T"}
    # Every candidate's path repeats the keys above it: 5,000 values under one 5,000-character key
    # were 25 MB of paths. No property has such a name.
    node = {"@type": "Product", "name": "p", "k" * 5000: {f"c{i}": "v" for i in range(5000)}, "offers": {"price": "5"}}
    cands = build_candidates(jsonld=[node], microdata=[], metadata={})
    assert sum(len(c.path) for c in cands) < 1000
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "number"}}}
    assert fill_schema(schema, cands, "https://x.test/")["data"] == {"name": "p", "price": 5.0}


def test_yoast_graph_reference_lists_fill_as_before():
    site = "https://blog.example.com/"
    people = [{"@type": "Person", "@id": f"{site}#/schema/person/{n}", "name": f"Author {n}",
               "url": f"{site}author/{n}/", "worksFor": {"@id": f"{site}#organization"}} for n in (1, 2)]
    html = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "Article", "@id": f"{site}p/#article", "headline": "Post title",
         "author": [{"@id": f"{site}#/schema/person/1"}, {"@id": f"{site}#/schema/person/2"}],
         "editor": {"@id": f"{site}#/schema/person/1"}, "publisher": {"@id": f"{site}#organization"},
         "image": {"@id": f"{site}p/#primaryimage"}, "isPartOf": {"@id": f"{site}p/"},
         "mainEntityOfPage": {"@id": f"{site}p/"}},
        {"@type": "WebPage", "@id": f"{site}p/", "name": "Post title - Example Blog",
         "primaryImageOfPage": {"@id": f"{site}p/#primaryimage"}, "isPartOf": {"@id": f"{site}#website"}},
        {"@type": "ImageObject", "@id": f"{site}p/#primaryimage", "url": f"{site}hero.jpg", "width": 1200},
        {"@type": "WebSite", "@id": f"{site}#website", "name": "Example Blog",
         "publisher": {"@id": f"{site}#organization"}},
        {"@type": "Organization", "@id": f"{site}#organization", "name": "Example Blog", "url": site,
         "logo": {"@id": f"{site}#logo"}, "founder": [{"@id": f"{site}#/schema/person/1"}]},
        {"@type": "ImageObject", "@id": f"{site}#logo", "url": f"{site}logo.png"}, *people]})
    who = {"type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string", "format": "uri"}}}
    schema = {"type": "object", "properties": {
        "headline": {"type": "string"}, "image": {"type": "string", "format": "uri"}, "author": who,
        "authors": {"type": "array", "items": who}, "publisher": who}}
    out = _fill(html, schema, f"{site}p/")
    one, two = ({"name": f"Author {n}", "url": f"{site}author/{n}/"} for n in (1, 2))
    assert out["data"] == {"headline": "Post title", "image": f"{site}hero.jpg", "author": one, "authors": [one, two],
                           "publisher": {"name": "Example Blog", "url": site}}
    assert out["missing"] == [] and out["sources"]["authors[1].url"] == "json-ld:Article.author.url"
    assert out["sources"]["image"] == "json-ld:Article.image"


HOSTILE_SCHEMAS = {
    "198 leaves": {"type": "object", "properties": {f"p{i}": {} for i in range(198)}},
    "99 titles": {"type": "object", "properties": {f"n{i}": {"description": "title"} for i in range(99)}},
    "66 numbers": {"type": "object", "properties": {
        f"n{i}": {"type": "number", "description": "title"} for i in range(66)}},
    "arrays": {"type": "object", "properties": {n: {"type": "array", "items": {"type": "string"}}
                                                for n in NAME_LIKE + ["keywords", "l", "items", "y"]}},
    "arrays of objects": {"type": "object", "properties": {
        n: {"type": "array", "items": {"properties": {m: {} for m in NAME_LIKE + ["l", "items", "x"]}}}
        for n in NAME_LIKE + ["l", "items", "y", "x"]}},
    "items of items": ITEMS_OF_ITEMS,
    "chain 4": _ref_chain(4), "chain 7": _ref_chain(7), "name chain": _ref_chain(4, NAME_LIKE[:4]),
    "self 8": _self_ref(8, False), "self mixed": {"type": "object", "properties": {
        n: {"$ref": "#"} for n in ("name", "names", "l", "items", "y", "k0", "x")}},
}


def _deep(levels: int):
    v: object = "x"
    for _ in range(levels):
        v = {"name": v, "k0": v, "l": [v]}
    return v


def _hostile_pages():
    yield "names", [{"@type": "Thing", "x": [{"name": f"n{i}"} for i in range(200)]} for _ in range(25)]
    yield "empty names", [{"@type": "Product", "x": [{"name": ""} for i in range(200)]} for _ in range(38)]
    yield "one long name", [{"@type": "Product", "name": "x" * 90_000, "title": " " * 5000}]
    yield "long lists", [{"@type": "Product", "name": [f"s{i}" for i in range(4000)], "y": [[None] * 80] * 80,
                          "keywords": [f"k{i}" for i in range(4000)]}]
    yield "nested names", [{"@type": "Product", **_deep(7)}]
    yield "reference lists", (
        [{"@id": "a", "@type": "Product", "name": "p", "l": [{"@id": f"b{i}"} for i in range(50)],
          "items": [{"@id": f"b{i}"} for i in range(50)]}]
        + [{"@id": f"b{i}", "@type": "Thing", "name": "b", "l": [{"@id": f"c{j}"} for j in range(30)],
            "items": [{"@id": f"c{j}"} for j in range(30)]} for i in range(50)]
        + [{"@id": f"c{j}", "@type": "Thing", "x": "w", "y": [[1] * 20] * 20, "l": [{"@id": "a"}]} for j in range(30)])
    yield "typed nodes", [{"@type": ["Product", f"T{i}"], "name": "x"} for i in range(2200)]


@pytest.mark.parametrize("page,nodes", list(_hostile_pages()))
def test_a_100_kb_hostile_page_costs_well_under_a_second_with_any_schema(page, nodes, work):
    html = _ld(nodes, head="<title>Hostile</title>")
    assert len(html) < 110_000
    base = "https://x.test/"
    tree = extract.parse(html)
    ld, md, meta = extract.json_ld(tree), extract.microdata(tree, base), extract.page_metadata(tree, base)
    for name, schema in HOSTILE_SCHEMAS.items():
        schema = _accepted(schema)
        work["objects"] = work["lookups"] = 0
        t = time.monotonic()  # what service._fill does for one request
        out = fill_schema(schema, build_candidates(jsonld=ld, microdata=md, metadata=meta), base)
        assert time.monotonic() - t < 3, (page, name)  # at most 0.3 s here
        assert len(json.dumps(out)) < 1_100_000, (page, name)


# ----------------------------------------------------------------------------- C: the declared main entity

PAGE_URL = "https://shop.example.com/anvil/"
# Yoast's FAQ block: the WebPage's mainEntity names the Question nodes.
YOAST_FAQ = {"@context": "https://schema.org", "@graph": [
    {"@type": ["WebPage", "FAQPage"], "@id": PAGE_URL, "name": "Acme Anvil 50kg - Acme Store",
     "mainEntity": [{"@id": PAGE_URL + "#faq-question-1"}]},
    {"@type": "Question", "@id": PAGE_URL + "#faq-question-1", "name": "How heavy is it?",
     "acceptedAnswer": {"@type": "Answer", "text": "50 kg"}}]}
SHOP_HEAD = ('<title>Acme Anvil 50kg - Acme Store</title><meta property="og:type" content="article">'
             '<meta property="og:title" content="Acme Anvil 50kg - Acme Store">'
             '<meta property="og:description" content="Buy the Acme anvil.">')
NAME_AND_DESCRIPTION = {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}}}


class ProductInfo(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None


class Wrapper(BaseModel):
    product: Optional[ProductInfo] = None


@pytest.mark.parametrize("entity", [
    {"@type": "Product", "name": "Acme Anvil 50kg", "description": "A very heavy anvil.", "sku": "ANV-50"},
    {"@type": "Event", "name": "Anvil Night", "description": "An evening of forging.", "startDate": "2026-12-01"},
    {"@type": "Recipe", "name": "Anvil cake", "description": "Heavy.", "recipeIngredient": ["flour"]},
])
def test_a_faq_block_does_not_demote_the_pages_main_entity(entity):
    # WooCommerce, The Events Calendar and recipe plugins emit their node without mainEntityOfPage.
    out = _fill(_ld(entity, YOAST_FAQ, head=SHOP_HEAD), NAME_AND_DESCRIPTION, PAGE_URL)
    assert out["data"] == {"name": entity["name"], "description": entity["description"]}  # were the og: tags
    assert out["sources"] == {"name": f"json-ld:{entity['@type']}.name",
                              "description": f"json-ld:{entity['@type']}.description"}
    assert _fill(_ld(entity, head=SHOP_HEAD), NAME_AND_DESCRIPTION, PAGE_URL)["data"] == out["data"]


def test_a_wrapper_named_after_the_entity_is_filled_beside_a_faq_block():
    product = {"@type": "Product", "name": "Acme Anvil 50kg", "description": "A very heavy anvil.", "sku": "ANV-50"}
    out = _fill(_ld(product, YOAST_FAQ, head=SHOP_HEAD), Wrapper.model_json_schema(), PAGE_URL)
    assert out["data"] == {"product": {"name": "Acme Anvil 50kg", "sku": "ANV-50"}}  # was null
    assert out["missing"] == []


def test_a_named_main_type_node_still_outranks_the_others():
    video = {"@type": "VideoObject", "name": "How to forge", "uploadDate": "2020-01-01"}
    recipe = {"@type": "Recipe", "@id": PAGE_URL + "#recipe", "name": "Anvil cake", "description": "Heavy."}
    page = {"@type": "WebPage", "@id": PAGE_URL, "mainEntity": [{"@id": PAGE_URL + "#recipe"},
                                                               {"@id": PAGE_URL + "#faq-question-1"}]}
    out = _fill(_ld(video, recipe, page, YOAST_FAQ["@graph"][1], head=SHOP_HEAD), NAME_AND_DESCRIPTION, PAGE_URL)
    assert out["data"] == {"name": "Anvil cake", "description": "Heavy."}
    assert out["sources"]["name"] == "json-ld:Recipe.name"


# ----------------------------------------------------------------------------- D: values that are no URL

WIDGET = {"@type": "Product", "name": "Widget", "description": "A widget."}
WIDGET_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                                                  "image": {"type": "string"}}}


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


@pytest.mark.parametrize("html", [
    _ld(WIDGET, head='<meta property="og:image" content="//[cdn]/w.jpg">'),  # urljoin: 'cdn' is no IPv6 address
    _ld({**WIDGET, "image": "//[bad"}),  # urljoin: Invalid IPv6 URL
    _ld({**WIDGET, "image": ["//[bad", {"@type": "ImageObject", "url": "/[x]:y//[z"}]}),
    _ld({**WIDGET, "image": "https://[bad/w.jpg"}),  # absolute, and no URL either
])
def test_an_unparsable_url_is_a_missing_value_not_a_lost_extraction(html):
    out = _fill(html, WIDGET_SCHEMA)  # raised ValueError: the response came without "extracted"
    assert out["data"] == {"name": "Widget", "description": "A widget.", "image": None}
    assert out["missing"] == ["image"] and set(out["sources"]) == {"name", "description"}


def test_the_next_candidate_is_tried_after_an_unparsable_url():
    html = _ld({**WIDGET, "image": ["//[bad", "/img/a.jpg", "https://[bad/b.jpg", "https://cdn.test/c.jpg"]},
               head='<meta property="og:image" content="/img/og.jpg">')
    out = _fill(_ld({**WIDGET, "image": "//[bad"}, head='<meta property="og:image" content="/img/og.jpg">'),
                WIDGET_SCHEMA)
    assert out["data"]["image"] == "https://www.example.test/img/og.jpg" and out["sources"]["image"] == "meta:og:image"
    out = _fill(html, {"type": "object", "properties": {
        "images": {"type": "array", "items": {"type": "string", "format": "uri"}}}})
    assert out["data"] == {"images": ["https://www.example.test/img/a.jpg", "https://cdn.test/c.jpg"]}


def test_an_unparsable_og_image_through_the_api(pages, client):
    url = pages("/widget", _ld(WIDGET, head='<title>Widget</title><meta property="og:image" content="//[cdn]/w.jpg">',
                               body="<h1>Widget</h1><p>" + "A widget for every purpose. " * 20 + "</p>"))
    r = client.post("/v1/fetch", json={"url": url, "jsonSchema": WIDGET_SCHEMA, "include": ["metadata"]})
    body = r.json()
    assert r.status_code == 200 and "warnings" not in body, r.text[:300]
    assert body["extracted"]["data"] == {"name": "Widget", "description": "A widget.", "image": None}
    assert body["extracted"]["missing"] == ["image"] and body["metadata"]["image"] is None


# ----------------------------------------------------------------------------- E: schema defaults

class Node(BaseModel):
    name: str
    children: list["Node"] = []


class Limits(BaseModel):
    perOrder: int = 5
    note: Optional[str] = None


class Seller(BaseModel):
    name: str
    url: Optional[str] = None


class Listing(BaseModel):
    name: str
    tags: list[str] = []
    currency: str = "USD"
    inStock: bool = True
    quantity: int = 1
    kind: Literal["product", "service"] = "product"
    attributes: dict[str, str] = {}
    related: list[Node] = []
    limits: Limits = Limits()
    rating: Optional[float] = None
    seller: Optional[Seller] = None
    sku: Optional[str] = "n/a"


def test_missing_properties_take_their_schema_default_and_the_result_validates():
    out = _fill(_ld({"@type": "Product", "name": "Anvil"}), Listing.model_json_schema())
    assert out["data"] == {"name": "Anvil", "tags": [], "currency": "USD", "inStock": True, "quantity": 1,
                           "kind": "product", "attributes": {}, "related": [], "limits": {"perOrder": 5, "note": None},
                           "rating": None, "seller": None, "sku": "n/a"}
    assert Listing.model_validate(out["data"]) == Listing(name="Anvil")  # children/tags as null were rejected
    # A default is not a value read from the page: no source, still missing, coverage as before.
    assert out["sources"] == {"name": "json-ld:Product.name"}
    assert out["missing"] == ["tags", "currency", "inStock", "quantity", "kind", "related", "limits.perOrder",
                              "limits.note", "rating", "seller", "sku"]
    assert out["coverage"] == round(1 / 12, 3) and out["missingRequired"] == []


def test_a_value_on_the_page_wins_over_the_default():
    page = {"@type": "Product", "name": "Anvil", "keywords": ["iron", "heavy"], "sku": "ANV-50", "quantity": "3",
            "offers": {"priceCurrency": "EUR", "availability": "https://schema.org/OutOfStock"},
            "related": [{"name": "Hammer"}], "limits": {"perOrder": 2}}
    data = _fill(_ld(page), Listing.model_json_schema())["data"]
    listing = Listing.model_validate(data)
    assert (listing.tags, listing.currency, listing.inStock, listing.quantity, listing.sku) == (
        ["iron", "heavy"], "EUR", False, 3, "ANV-50")
    assert listing.related == [Node(name="Hammer")] and listing.limits == Limits(perOrder=2)


def test_recursive_model_with_defaulted_children_validates():
    page = {"@type": "Thing", "name": "root", "children": [{"name": "a", "children": [{"name": "a1"}]}, {"name": "b"}]}
    out = _fill(_ld(page), Node.model_json_schema())
    tree = Node.model_validate(out["data"])  # "children": null at every leaf was a validation error
    assert tree == Node(name="root", children=[Node(name="a", children=[Node(name="a1")]), Node(name="b")])
    assert {"children[0].children[0].children", "children[1].children"} <= set(out["missing"])
    assert Node.model_validate(_fill(EXAMPLE_COM, Node.model_json_schema())["data"]) == Node(name="Example Domain")


def test_a_default_is_copied_and_null_stays_null_without_one():
    default = {"sizes": [1, 2], "unit": "kg"}
    schema = {"type": "object", "required": ["name", "packaging", "weight"], "properties": {
        "name": {"type": "string"}, "packaging": {"type": "object", "default": default, "properties": {}},
        "boxes": {"type": "array", "items": {"type": "object", "properties": {
            "packaging": {"type": ["object", "null"], "default": default, "properties": {"unit": {"type": "string"}}},
            "label": {"type": "string"}}}},
        "weight": {"type": "number", "default": 0}, "colour": {"type": "string"},
        "warranty": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}
    out = _fill(_ld({"@type": "Product", "name": "Anvil", "boxes": [{"label": "A"}, {"label": "B"}]}), schema)
    boxes = out["data"]["boxes"]
    assert out["data"] == {"name": "Anvil", "packaging": {}, "boxes": [{"packaging": default, "label": "A"},
                                                                       {"packaging": default, "label": "B"}],
                           "weight": 0, "colour": None, "warranty": None}  # never a guess
    boxes[0]["packaging"]["sizes"].append(3)  # the caller's copy: neither the schema's nor the next item's
    assert default == {"sizes": [1, 2], "unit": "kg"} and boxes[1]["packaging"] == default
    assert out["missing"] == ["boxes[0].packaging", "boxes[1].packaging", "weight", "colour", "warranty"]
    assert out["missingRequired"] == ["weight"]  # required and not on the page, whatever stands in for it


def test_defaults_are_not_given_to_members_a_budget_never_reached(work):
    schema = _ref_chain(3)
    for definition in schema["$defs"].values():
        for prop in definition["properties"].values():
            prop["default"] = "unknown"
    out = _fill(EXAMPLE_COM, _accepted(schema))
    values: list = []

    def collect(node):
        for v in node.values():
            collect(v) if isinstance(v, dict) else values.append(v)
    collect(out["data"])
    assert out["truncated"] is True and set(values) == {"unknown", None}
    assert values.count("unknown") == len(out["missing"])  # reached and not found; the rest is null
    assert len(json.dumps(out)) < 1_000_000


def test_a_large_default_cannot_inflate_the_result(work):
    schema = _ref_chain(3)
    schema["$defs"]["D11"]["properties"]["p0"]["default"] = {"blob": "d" * 20_000}  # one 20 KB default, 2,500 uses
    out = _fill(EXAMPLE_COM, _accepted(schema))
    assert out["truncated"] is True and len(json.dumps(out)) < 1_200_000  # 50 MB without the budget
    first = out["data"]["root"]
    for _ in range(11):
        first = first["p0"]
    assert first == {"p0": {"blob": "d" * 20_000}, "p1": None, "p2": None}


def test_defaults_through_the_api(server, client):
    r = client.post("/v1/fetch", json={"url": f"http://echo.test:{server.server_address[1]}/product",
                                       "jsonSchema": Node.model_json_schema(), "include": []})
    extracted = r.json()["extracted"]
    assert r.status_code == 200 and Node.model_validate(extracted["data"]) == Node(name="Acme Anvil 50kg")
    assert extracted["missing"] == ["children"] and extracted["sources"] == {"name": "json-ld:Product.name"}


# ----------------------------------------------------------------------------- A, continued: the cost of one value

def test_a_long_enum_is_read_once_and_matches_as_before():
    # The enum was compared, entry by entry and lowercased, with every candidate tried:
    # 4 properties x 3,000 entries x 4,000 names.
    names = [{"@type": "Thing", "name": f"n{i}"} for i in range(4000)]
    enum = [f"e{j}" for j in range(3000)]
    schema = _accepted({"type": "object", "$defs": {"E": {"description": "title", "enum": enum}}, "properties": {
        **{f"t{i}": {"$ref": "#/$defs/E"} for i in range(4)},
        "availability": {"enum": ["InStock", "instock", 5, None, {"a": 1}, "OutOfStock"]},
        "stock": {"description": "availability", "enum": ["instock", "InStock"]},
        "kind": {"description": "title", "enum": ["N3999", "n3999"]}}})
    product = {"@type": "Product", "offers": {"availability": "https://schema.org/INSTOCK"}}
    cands = build_candidates(jsonld=[*names, product], microdata=[], metadata={})
    t = time.monotonic()
    out = fill_schema(schema, cands, "https://x.test/")
    assert time.monotonic() - t < 1.5  # 0.08 s here, 3 s before
    # An exact match first, then the first entry that matches whatever the case.
    assert out["data"] == {"t0": None, "t1": None, "t2": None, "t3": None, "availability": "InStock",
                           "stock": "instock", "kind": "n3999"}


def test_huge_numbers_and_blank_strings_are_charged_for_what_they_cost():
    items = [{"price": 1e308, "name": " " * 300} for _ in range(6000)]  # 309 digits each as an integer
    cands = build_candidates(jsonld=[{"@type": "Product", "name": "p", "items": items}], microdata=[], metadata={})
    schema = _accepted({"type": "object", "properties": {"items": {"type": "array", "items": {"properties": {
        "price": {"type": "integer"}, "name": {"properties": {}}}}}}})
    out = fill_schema(schema, cands, "https://x.test/")
    assert out["truncated"] is True and len(json.dumps(out)) < 1_100_000  # 1.9 MB of digits before
    assert out["data"]["items"][0] == {"price": int(1e308), "name": {}}
