"""Schema-filling regressions from the independent review."""

from __future__ import annotations

from webfetch import extract
from webfetch.gates import GoogleConsent
from webfetch.schema_fill import build_candidates, fill_schema, parse_number

YOAST = """<html><head><title>Post title - Blog</title>
<meta property="og:image" content="https://blog.example.com/wp-content/uploads/hero.jpg">
<link rel="canonical" href="https://blog.example.com/2026/11/post-title/">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Article","@id":"https://blog.example.com/2026/11/post-title/#article","headline":"Post title",
  "author":{"name":"Jane Doe","@id":"https://blog.example.com/#/schema/person/1"},
  "image":{"@id":"https://blog.example.com/2026/11/post-title/#primaryimage"},
  "publisher":{"@id":"https://blog.example.com/#organization"}},
 {"@type":"ImageObject","@id":"https://blog.example.com/2026/11/post-title/#primaryimage",
  "url":"https://blog.example.com/wp-content/uploads/hero.jpg","contentUrl":"https://blog.example.com/wp-content/uploads/hero.jpg"},
 {"@type":"Organization","@id":"https://blog.example.com/#organization","name":"Example Blog","url":"https://blog.example.com/"},
 {"@type":"Person","@id":"https://blog.example.com/#/schema/person/1","name":"Jane Doe","url":"https://blog.example.com/author/jane/"}
]}</script></head><body><h1>Post title</h1></body></html>"""


def _fill(html, schema, base="https://blog.example.com/2026/11/post-title/"):
    tree = extract.parse(html)
    cands = build_candidates(jsonld=extract.json_ld(tree), microdata=extract.microdata(tree, base),
                             metadata=extract.page_metadata(tree, base))
    return fill_schema(schema, cands, base)


def test_main_entity_wins_over_nested_entities_and_ids_resolve():
    out = _fill(YOAST, {"type": "object", "properties": {
        "name": {"type": "string"}, "url": {"type": "string", "format": "uri"},
        "image": {"type": "string", "format": "uri"}, "author": {"type": "string"}}})
    d = out["data"]
    assert d["name"] == "Post title"
    assert d["url"] == "https://blog.example.com/2026/11/post-title/"
    assert d["image"] == "https://blog.example.com/wp-content/uploads/hero.jpg"
    assert d["author"] == "Jane Doe"


def test_pydantic_style_nullable_and_refs():
    html = """<script type="application/ld+json">{"@type":"Product","name":"Anvil","image":["https://c.test/a.jpg"],
      "offers":{"@type":"Offer","price":"1.299,00","priceCurrency":"EUR","availability":"https://schema.org/InStock"},
      "aggregateRating":{"ratingValue":"4.6","reviewCount":"212"}}</script>"""
    schema = {"type": "object", "$defs": {"Offer": {"type": "object", "properties": {
        "price": {"anyOf": [{"type": "number"}, {"type": "null"}]}, "priceCurrency": {"type": "string"}}}},
        "properties": {
            "price": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
            "reviewCount": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "inStock": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "images": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
            "offers": {"anyOf": [{"$ref": "#/$defs/Offer"}, {"type": "null"}]}}}
    d = _fill(html, schema, "https://shop.test/p")["data"]
    assert d["price"] == 1299.0 and d["reviewCount"] == 212 and d["inStock"] is True
    assert d["images"] == ["https://c.test/a.jpg"]
    assert d["offers"] == {"price": 1299.0, "priceCurrency": "EUR"}


def test_string_valued_object_key_is_not_filled_from_other_entities():
    html = """<script type="application/ld+json">{"@type":"NewsArticle","headline":"H","author":"Staff Reporter",
      "publisher":{"@type":"Organization","name":"Daily Planet Media Group","url":"https://dp.example/"}}</script>"""
    d = _fill(html, {"type": "object", "properties": {"author": {"type": "object", "properties": {
        "name": {"type": "string"}, "url": {"type": "string", "format": "uri"}}}}}, "https://dp.example/a")["data"]
    assert d["author"] == {"name": "Staff Reporter", "url": None}


def test_urls_with_commas_are_not_split_and_text_is_not_made_a_url():
    html = """<meta property="og:site_name" content="Daily Planet">
    <script type="application/ld+json">{"@type":"Product","name":"Runner",
      "image":"https://res.cloudinary.com/acme/image/upload/c_fill,w_800,h_800/v1712/products/runner2.jpg"}</script>"""
    d = _fill(html, {"type": "object", "properties": {
        "images": {"type": "array", "items": {"type": "string", "format": "uri"}},
        "website": {"type": "string", "format": "uri"}}}, "https://shop.example.com/p/")["data"]
    assert d["images"] == ["https://res.cloudinary.com/acme/image/upload/c_fill,w_800,h_800/v1712/products/runner2.jpg"]
    assert d["website"] is None


def test_number_edge_cases():
    assert parse_number("49,99 39,99 €") == 49.99
    assert parse_number("4.6 212 reviews") == 4.6
    assert parse_number("1.299 €") == 1299.0 and parse_number("€ 1.299,-") == 1299.0
    assert parse_number(float("nan")) is None


def test_google_consent_cookie_goes_to_the_right_cctld():
    g = GoogleConsent()
    a = g.on_response(original_url="https://www.google.se/search?q=x", url="https://www.google.se/search?q=x",
                      status=302, location="https://consent.google.se/ml?continue=https://www.google.se/search?q%3Dx",
                      head="")
    assert a is not None and a.cookie_domain == ".google.se"
    assert g.applies("www.google.co.uk") == "google.co.uk" and g.applies("example.com") is None
