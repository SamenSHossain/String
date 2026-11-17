"""Schema-filling regressions from the endpoint audit (structured output).

Each fixture reproduces the shape of a real page the audit replayed: example.com, TechCrunch's
Yoast graph, BBC Good Food, GOG, the Guardian and Steam. Models are real Pydantic models; the
pinned Pydantic emits the same schemas the audit sent.
"""

from __future__ import annotations

import json
from typing import List, Optional

import pytest
from pydantic import BaseModel

from webfetch import extract
from webfetch.schema_fill import build_candidates, fill_schema, site_candidates


def _ld(*nodes: dict, head: str = "", body: str = "") -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)
    return f"<html><head>{head}{scripts}</head><body>{body}</body></html>"


def _fill(html: str, schema: dict, base: str = "https://www.example.test/page") -> dict:
    tree = extract.parse(html)
    cands = build_candidates(jsonld=extract.json_ld(tree), microdata=extract.microdata(tree, base),
                             metadata=extract.page_metadata(tree, base), site=site_candidates(tree, base))
    return fill_schema(schema, cands, base)


def _schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


# ----------------------------------------------------------------------------- models

class Category(BaseModel):
    name: str
    parent: Optional["Category"] = None


class Page(BaseModel):  # the audit's self-referencing model (500 on the live endpoint)
    title: str
    category: Optional[Category] = None


class Tree(BaseModel):  # a self-referencing root: Pydantic emits {"$ref": "#/$defs/Tree", "$defs": ...}
    name: str
    parent: Optional["Tree"] = None


class Node(BaseModel):
    name: str
    children: List["Node"] = []


class CategoryFlat(BaseModel):
    name: str
    parent: Optional[str] = None


class Warranty(BaseModel):
    name: Optional[str] = None
    durationMonths: Optional[int] = None


class PageFlat(BaseModel):
    title: str
    category: Optional[CategoryFlat] = None
    warranty: Optional[Warranty] = None


class Step(BaseModel):
    text: Optional[str] = None
    name: Optional[str] = None


class Nutrition(BaseModel):
    calories: Optional[float] = None
    fatContent: Optional[str] = None


class Recipe(BaseModel):
    name: Optional[str] = None
    recipeIngredient: Optional[List[str]] = None
    recipeInstructions: Optional[List[Step]] = None
    nutrition: Optional[Nutrition] = None
    totalTime: Optional[str] = None


# The audit's Pydantic Article (TechCrunch requests), verbatim.
ARTICLE_SCHEMA = {
    "$defs": {
        "Person": {"properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Name"},
            "url": {"anyOf": [{"type": "string", "format": "uri"}, {"type": "null"}], "default": None, "title": "Url"}},
            "title": "Person", "type": "object"},
        "Organization": {"properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Name"},
            "url": {"anyOf": [{"type": "string", "format": "uri"}, {"type": "null"}], "default": None, "title": "Url"}},
            "title": "Organization", "type": "object"},
        "Warranty": {"properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Name"},
            "durationMonths": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                               "title": "Durationmonths"}},
            "title": "Warranty", "type": "object"}},
    "properties": {
        "headline": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Headline"},
        "author": {"anyOf": [{"$ref": "#/$defs/Person"}, {"type": "null"}], "default": None},
        "authors": {"anyOf": [{"items": {"$ref": "#/$defs/Person"}, "type": "array"}, {"type": "null"}],
                    "default": None, "title": "Authors"},
        "publisher": {"anyOf": [{"$ref": "#/$defs/Organization"}, {"type": "null"}], "default": None},
        "warranty": {"anyOf": [{"$ref": "#/$defs/Warranty"}, {"type": "null"}], "default": None},
        "isbn": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Isbn"}},
    "required": ["headline"], "title": "Article", "type": "object"}

EXAMPLE_COM = ("<html><head><title>Example Domain</title></head>"
               "<body><div><p>This domain is for use in examples.</p></div></body></html>")

# TechCrunch / Yoast: the author is a list of {"@id"} references into the @graph.
YOAST_AUTHOR_LIST = _ld({"@context": "https://schema.org", "@graph": [
    {"@type": "Article", "@id": "https://blog.example.com/p/#article", "headline": "Post title",
     "author": [{"@id": "https://blog.example.com/#/schema/person/1"}],
     "publisher": {"@id": "https://blog.example.com/#organization"},
     "mainEntityOfPage": {"@id": "https://blog.example.com/p/"}},
    {"@type": "WebPage", "@id": "https://blog.example.com/p/", "name": "Post title - Example Blog"},
    {"@type": "Organization", "@id": "https://blog.example.com/#organization", "name": "Example Blog",
     "url": "https://blog.example.com/", "founder": [{"@id": "https://blog.example.com/#/schema/person/1"}]},
    {"@type": "Person", "@id": "https://blog.example.com/#/schema/person/1", "name": "Jane Doe",
     "url": "https://blog.example.com/author/jane/",
     "worksFor": {"@id": "https://blog.example.com/#organization"},  # a reference cycle
     "knows": [{"@id": "https://blog.example.com/#/schema/person/1"}]},
]}, head="<title>Post title - Example Blog</title>", body="<h1>Post title</h1>")

# BBC Good Food: an embedded VideoObject comes first; the Recipe declares mainEntityOfPage;
# a second node with the Recipe's @id carries its rating.
VIDEO = {"@context": "https://schema.org", "@type": "VideoObject", "name": "How to make perfect pancakes",
         "contentUrl": "https://media.example/pancakes.mp4", "uploadDate": "2015-02-16T14:23:57.041Z"}
RECIPE = {"@context": "https://schema.org", "@id": "https://www.bbcgoodfood.com/recipes/easy-pancakes#Recipe",
          "@type": "Recipe", "name": "Easy pancakes", "headline": "Easy pancakes", "totalTime": "PT30M",
          "author": [{"@type": "Person", "name": "Cassie Best",
                      "url": "https://www.bbcgoodfood.com/author/cassiebest"}],
          "nutrition": {"@type": "NutritionInformation", "calories": "61 calories", "fatContent": "2 grams fat"},
          "recipeIngredient": ["100g plain flour", "2 large eggs", "300ml milk"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Whisk to a smooth batter."},
                                 {"@type": "HowToStep", "text": "Cook for 1 min on each side."}]}
RATING = {"@context": "https://schema.org/", "@id": RECIPE["@id"],
          "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4, "reviewCount": 893}}
BBC_HEAD = '<title>Easy pancakes recipe | Good Food</title><meta property="og:title" content="Easy pancakes">'
BBC_GOOD_FOOD = _ld(VIDEO, {**RECIPE, "mainEntityOfPage": {"@type": "WebPage", "@id": RECIPE["@id"][:-7]}}, RATING,
                    head=BBC_HEAD, body="<h1>Easy pancakes</h1>")

# GOG: the only identifier is the shop's SKU.
GOG = _ld({"@context": "https://schema.org", "@type": ["Product", "VideoGame", "SoftwareApplication"],
           "@id": "https://www.gog.com/en/game/the_witcher_3#product", "name": "The Witcher 3: Wild Hunt",
           "sku": "1640424747", "brand": {"@type": "Brand", "name": "CD PROJEKT RED"},
           "offers": [{"@type": "Offer", "price": "149.99", "priceCurrency": "PLN",
                       "availability": "https://schema.org/InStock",
                       "seller": {"@type": "Organization", "name": "GOG.com"}}]})

# The Guardian: a subscription product (Google's markup) is nested in isPartOf.
GUARDIAN = _ld({"@type": "NewsArticle", "@context": "https://schema.org",
                "@id": "https://www.theguardian.com/world/2026/nov/17/protests",
                "headline": "Hundreds arrested in anti-Trump protests ",
                "isPartOf": {"@type": ["CreativeWork", "Product"], "name": "The Guardian",
                             "productID": "theguardian.com:basic"},
                "mainEntityOfPage": "https://www.theguardian.com/world/2026/nov/17/protests"})


# ----------------------------------------------------------------------------- B1/B2: recursion, root $ref

def test_self_referencing_model_is_filled_not_a_500():
    out = _fill(EXAMPLE_COM, _schema(Page))  # the audit's request: RecursionError -> 500
    assert out["data"] == {"title": "Example Domain", "category": None}
    assert out["missing"] == ["category"] and out["missingRequired"] == []


def test_self_referencing_model_follows_nested_page_data():
    html = _ld({"@type": "Product", "name": "Trail shoe",
                "category": {"name": "Trail running", "parent": {"name": "Running", "parent": {"name": "Sport"}}}})
    out = _fill(html, _schema(Page))
    assert out["data"]["category"] == {"name": "Trail running", "parent": {
        "name": "Running", "parent": {"name": "Sport", "parent": None}}}
    assert out["sources"]["category.parent.parent.name"] == "json-ld:Product.category.parent.parent.name"
    assert "category.parent.parent.parent" in out["missing"]


def test_non_nullable_self_reference_stops_where_the_page_data_ends():
    schema = {"$defs": {"Cat": {"type": "object", "properties": {
        "name": {"type": "string"}, "parent": {"$ref": "#/$defs/Cat"}}}},
        "type": "object", "properties": {"category": {"$ref": "#/$defs/Cat"}}}
    out = _fill(_ld({"@type": "Product", "name": "Trail shoe", "category": "Shoes"}), schema)
    assert out["data"] == {"category": {"name": "Shoes", "parent": None}}
    assert out["missing"] == ["category.parent"]


def test_ref_to_the_root_is_followed_along_the_page_data():
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "child": {"$ref": "#"}}}
    out = _fill(_ld({"@type": "Thing", "name": "root", "child": {"name": "kid"}}), schema)
    assert out["data"] == {"name": "root", "child": {"name": "kid", "child": None}}
    assert "child.child" in out["missing"]


def test_recursion_along_data_that_never_ends_is_capped():
    # A model whose "name" is the model itself re-matches the same string at every level.
    schema = {"$defs": {"N": {"type": "object", "properties": {"name": {"$ref": "#/$defs/N"}}}}, "$ref": "#/$defs/N"}
    out = _fill(_ld({"@type": "Thing", "name": "x"}), schema)
    depth, node = 0, out["data"]
    while isinstance(node, dict):
        node, depth = node["name"], depth + 1
    assert node is None and depth <= 13
    assert len(out["missing"]) == 1 and out["missing"][0].endswith(".name")


def test_root_ref_model_uses_its_definition_and_its_required_list():
    assert _schema(Tree)["$ref"] == "#/$defs/Tree" and "properties" not in _schema(Tree)
    out = _fill("<html><body><p>No title, no data.</p></body></html>", _schema(Tree))
    assert out["data"] == {"name": None, "parent": None}
    assert out["missingRequired"] == ["name"]  # "required" lives in $defs.Tree, not at the root
    out = _fill(EXAMPLE_COM, _schema(Tree))
    assert out["data"] == {"name": "Example Domain", "parent": None} and out["missingRequired"] == []


def test_one_item_allof_and_escaped_pointer_refs_resolve():
    # Pydantic v1 wraps a described $ref in a one-item allOf; a JSON pointer writes "/" as "~1".
    offer = {"type": "object", "properties": {"price": {"type": "number"}, "priceCurrency": {"type": "string"}}}
    schema = {"$defs": {"Offer": offer, "a/b": offer}, "type": "object", "properties": {
        "offers": {"allOf": [{"$ref": "#/$defs/Offer"}], "description": "The offer"},
        "offer": {"$ref": "#/$defs/a~1b"}}}
    html = _ld({"@type": "Product", "name": "Anvil",
                "offers": {"@type": "Offer", "price": "12.50", "priceCurrency": "EUR"}})
    both = {"price": 12.5, "priceCurrency": "EUR"}
    assert _fill(html, schema)["data"] == {"offers": both, "offer": both}


def test_recursive_list_model_follows_nested_page_data_with_item_provenance():
    html = _ld({"@type": "Thing", "name": "root",
                "children": [{"name": "a", "children": [{"name": "a1"}]}, {"name": "b"}]})
    out = _fill(html, _schema(Node))
    assert out["data"] == {"name": "root", "children": [
        {"name": "a", "children": [{"name": "a1", "children": None}]}, {"name": "b", "children": None}]}
    assert out["sources"]["children"] == "json-ld:Thing.children"
    assert out["sources"]["children[0].children[0].name"] == "json-ld:Thing.children.children.name"
    assert {"children[0].children[0].children", "children[1].children"} <= set(out["missing"])
    assert out["missingRequired"] == []


# ----------------------------------------------------------------------------- B3: @id references in lists

def test_yoast_author_list_of_id_references_is_resolved():
    out = _fill(YOAST_AUTHOR_LIST, ARTICLE_SCHEMA, "https://blog.example.com/p/")
    d = out["data"]
    jane = {"name": "Jane Doe", "url": "https://blog.example.com/author/jane/"}
    assert d["headline"] == "Post title"
    assert d["author"] == jane and d["authors"] == [jane]
    assert d["publisher"] == {"name": "Example Blog", "url": "https://blog.example.com/"}
    assert out["sources"]["author.name"] == "json-ld:Article.author.name"
    assert out["sources"]["authors[0].url"] == "json-ld:Article.author.url"


def test_unhashable_id_in_a_reference_does_not_crash():
    nodes = [{"@type": "Article", "@id": "a", "headline": "H", "image": {"@id": ["x"]}, "author": [{"@id": {"y": 1}}]},
             {"@type": "Person", "@id": "p", "name": "P"}]
    cands = build_candidates(jsonld=nodes, microdata=[], metadata={})
    out = fill_schema({"type": "object", "properties": {"headline": {"type": "string"}}}, cands, "https://x.test/")
    assert out["data"] == {"headline": "H"}


# ----------------------------------------------------------------------------- B4: the declared main entity

def test_declared_main_entity_beats_an_embedded_video_listed_first():
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "totalTime": {"type": "string"},
        "author": {"type": "object", "properties": {"name": {"type": "string"}}}}}
    out = _fill(BBC_GOOD_FOOD, schema, "https://www.bbcgoodfood.com/recipes/easy-pancakes")
    assert out["data"] == {"name": "Easy pancakes", "totalTime": "PT30M", "author": {"name": "Cassie Best"}}
    assert out["sources"]["name"] == "json-ld:Recipe.name"


def test_main_entity_named_by_a_webpage_mainentity_reference():
    page = {"@context": "https://schema.org", "@type": "WebPage",
            "@id": "https://www.bbcgoodfood.com/recipes/easy-pancakes", "mainEntity": {"@id": RECIPE["@id"]}}
    html = _ld(VIDEO, RECIPE, page, head=BBC_HEAD)
    out = _fill(html, {"type": "object", "properties": {"name": {"type": "string"}}})
    assert out["data"]["name"] == "Easy pancakes" and out["sources"]["name"] == "json-ld:Recipe.name"


# ----------------------------------------------------------------------------- B5: identifiers

IDS = {"type": "object", "properties": {k: {"type": "string"} for k in
       ("name", "sku", "mpn", "gtin", "ean", "upc", "isbn")}}


def test_a_shop_sku_is_not_an_isbn_upc_ean_or_gtin():
    out = _fill(GOG, IDS, "https://www.gog.com/en/game/the_witcher_3")
    assert out["data"] == {"name": "The Witcher 3: Wild Hunt", "sku": "1640424747", "mpn": None, "gtin": None,
                           "ean": None, "upc": None, "isbn": None}
    assert out["sources"]["sku"] == "json-ld:Product.sku"
    assert set(out["missing"]) == {"mpn", "gtin", "ean", "upc", "isbn"}


def test_identifiers_are_never_read_from_another_entity():
    out = _fill(GUARDIAN, {"type": "object", "properties": {k: {"type": "string"} for k in
                                                            ("headline", "isbn", "ean", "gtin", "sku")}})
    assert out["data"] == {"headline": "Hundreds arrested in anti-Trump protests", "isbn": None, "ean": None,
                           "gtin": None, "sku": None}
    assert set(out["missing"]) == {"isbn", "ean", "gtin", "sku"}


@pytest.mark.parametrize("node,expected", [
    ({"@type": "Product", "name": "Kettle", "sku": "KT-1", "mpn": "K100", "gtin13": "4006381333931"},
     {"sku": "KT-1", "mpn": "K100", "gtin": "4006381333931", "ean": "4006381333931", "upc": None, "isbn": None}),
    ({"@type": "Product", "name": "Kettle", "productID": "kettle-7", "gtin12": "012345678905"},
     {"sku": "kettle-7", "mpn": None, "gtin": "012345678905", "ean": None, "upc": "012345678905", "isbn": None}),
    ({"@type": "Book", "name": "Dune", "isbn": "9780441172719"},
     {"sku": None, "mpn": None, "gtin": None, "ean": None, "upc": None, "isbn": "9780441172719"}),
])
def test_each_identifier_reads_its_own_keys(node, expected):
    data = _fill(_ld(node), IDS)["data"]
    assert {k: v for k, v in data.items() if k != "name"} == expected


def test_identifiers_inside_a_requested_object_are_that_objects_own():
    html = _ld({"@type": "Book", "name": "Dune", "workExample": [
        {"@type": "Book", "isbn": "9780441172719", "bookFormat": "https://schema.org/Paperback"}]})
    schema = {"type": "object", "properties": {
        "isbn": {"type": "string"},
        "workExample": {"type": "array", "items": {"type": "object", "properties": {"isbn": {"type": "string"}}}}}}
    out = _fill(html, schema)
    # The edition's ISBN is filled where it was asked for, and is not the work's own ISBN.
    assert out["data"] == {"isbn": None, "workExample": [{"isbn": "9780441172719"}]}
    assert out["sources"]["workExample[0].isbn"] == "json-ld:Book.workExample.isbn"


# ----------------------------------------------------------------------------- B6: nullable objects

def test_optional_objects_the_page_does_not_carry_are_null():
    out = _fill(EXAMPLE_COM, _schema(PageFlat))
    assert out["data"] == {"title": "Example Domain", "category": None, "warranty": None}
    assert out["sources"] == {"title": "meta:title"}
    assert out["missing"] == ["category", "warranty"]
    out = _fill(YOAST_AUTHOR_LIST, ARTICLE_SCHEMA, "https://blog.example.com/p/")
    assert out["data"]["warranty"] is None and "warranty" in out["missing"]
    assert not any(k.startswith("warranty") for k in out["sources"])


def test_a_list_of_strings_fills_an_object_as_its_name():
    html = """<html><head><title>Hogwarts Legacy on Steam</title></head><body>
    <div class="apphub_AppName" id="appHubAppName">Hogwarts Legacy</div>
    <div id="genresAndManufacturer"><b>Genre:</b> <span>
      <a href="https://store.steampowered.com/genre/Action/">Action</a>,
      <a href="https://store.steampowered.com/genre/Adventure/">Adventure</a></span></div></body></html>"""
    schema = {"$defs": {"CategoryFlat": _schema(CategoryFlat)}, "type": "object", "required": ["title"], "properties": {
        "title": {"type": "string"},
        "category": {"anyOf": [{"$ref": "#/$defs/CategoryFlat"}, {"type": "null"}], "default": None}}}
    out = _fill(html, schema, "https://store.steampowered.com/app/990080/Hogwarts_Legacy/")
    assert out["data"] == {"title": "Hogwarts Legacy", "category": {"name": "Action", "parent": None}}
    assert out["sources"]["category.name"] == "site:steam:#genresAndManufacturer"


class Info(BaseModel):
    name: Optional[str] = None


@pytest.mark.parametrize("html,field,filled", [
    (GOG, "product", True),  # the page's main entity is a Product
    (GOG, "game", True),  # ... and a VideoGame
    (_ld({"@type": "NewsArticle", "headline": "H", "name": "H"}), "article", True),
    (_ld({"@type": "BlogPosting", "headline": "H", "name": "H"}), "article", True),
    ('<html><head><meta property="og:type" content="product"><meta property="og:title" content="Anvil"></head></html>',
     "product", True),
    (_ld({"@type": "NewsArticle", "headline": "H", "name": "H"}), "product", False),
    (BBC_GOOD_FOOD, "video", False),  # the embedded video is not the page's main entity
])
def test_optional_wrapper_named_after_the_main_entity_is_filled(html, field, filled):
    schema = {"$defs": {"Info": _schema(Info)}, "type": "object", "properties": {
        field: {"anyOf": [{"$ref": "#/$defs/Info"}, {"type": "null"}], "default": None}}}
    data = _fill(html, schema)["data"]
    assert (data[field] is not None and data[field]["name"] is not None) is filled


def test_non_nullable_object_keeps_grouping_and_nested_page_objects_still_fill():
    out = _fill(YOAST_AUTHOR_LIST, {"type": "object", "properties": {
        "seo": {"type": "object", "properties": {"title": {"type": "string"}}}}}, "https://blog.example.com/p/")
    assert out["data"] == {"seo": {"title": "Post title"}}  # a plain grouping: filled from the page
    out = _fill(BBC_GOOD_FOOD, _schema(Recipe), "https://www.bbcgoodfood.com/recipes/easy-pancakes")
    assert out["data"]["nutrition"] == {"calories": 61.0, "fatContent": "2 grams fat"}


def test_optional_object_finds_its_plural_on_the_page():
    html = """<html><body><div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Sekiro</h1>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="price" content="59,99">
    <meta itemprop="priceCurrency" content="EUR"></div></div></body></html>"""
    offer = {"type": "object", "title": "Offer", "properties": {
        "price": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
        "priceCurrency": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}
    schema = {"$defs": {"Offer": offer}, "type": "object", "properties": {
        "offer": {"anyOf": [{"$ref": "#/$defs/Offer"}, {"type": "null"}], "default": None}}}
    out = _fill(html, schema)
    assert out["data"] == {"offer": {"price": 59.99, "priceCurrency": "EUR"}}
    assert out["sources"]["offer.price"] == "microdata:Product.offers.price"


# ----------------------------------------------------------------------------- B7: fields inside array items

def test_fields_inside_array_items_have_sources_and_missing():
    out = _fill(BBC_GOOD_FOOD, _schema(Recipe), "https://www.bbcgoodfood.com/recipes/easy-pancakes")
    assert out["data"]["recipeInstructions"] == [{"text": "Whisk to a smooth batter.", "name": None},
                                                 {"text": "Cook for 1 min on each side.", "name": None}]
    assert out["sources"]["recipeInstructions"] == "json-ld:Recipe.recipeInstructions"
    assert out["sources"]["recipeInstructions[1].text"] == "json-ld:Recipe.recipeInstructions.text"
    assert out["missing"] == ["recipeInstructions[0].name", "recipeInstructions[1].name"]
    assert out["coverage"] == 0.8  # 8 sourced, 2 missing


def test_only_the_items_returned_are_recorded():
    html = _ld({"@type": "Article", "headline": "H", "author": [
        {"@type": "Person", "name": "A", "url": "https://x.test/a"}, {"@type": "Person", "name": "B"}]})
    schema = {"type": "object", "properties": {"authors": {"type": "array", "maxItems": 1, "items": {
        "type": "object", "properties": {"name": {"type": "string"}, "url": {"type": "string", "format": "uri"}}}}}}
    out = _fill(html, schema)
    assert out["data"] == {"authors": [{"name": "A", "url": "https://x.test/a"}]}
    assert set(out["sources"]) == {"authors", "authors[0].name", "authors[0].url"} and out["missing"] == []


# ----------------------------------------------------------------------------- hostile pages and schemas

def test_deeply_nested_lists_in_page_data_do_not_exhaust_the_stack():
    deep: object = "x"
    for _ in range(3000):
        deep = [deep]
    cands = build_candidates(jsonld=[{"@type": "Product", "name": deep, "image": deep}], microdata=[], metadata={})
    out = fill_schema({"type": "object", "properties": {"name": {"type": "string"},
                                                        "image": {"type": "array", "items": {"type": "string"}}}},
                      cands, "https://x.test/")
    assert out["data"] == {"name": None, "image": None}


def test_a_dense_id_graph_stays_bounded():
    n = 11  # every node references every other one: candidates used to grow combinatorially
    nodes = [{"@type": "Thing", "@id": f"n{i}", **{f"k{j}": {"@id": f"n{j}"} for j in range(n) if j != i}}
             for i in range(n)]
    cands = build_candidates(jsonld=nodes, microdata=[], metadata={})
    assert len(cands) <= 50_100
    fill_schema({"type": "object", "properties": {"name": {"type": "string"}}}, cands, "https://x.test/")


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"offers": {"type": "object", "properties": ["price"]}}},
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": 5},
    {"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}, "b": {"$ref": "#/properties/0"}}},
    {"$ref": "#", "properties": {"a": {"type": "string"}}},
    {"type": "object", "properties": {"a": {"anyOf": "x"}, "b": {"oneOf": [None]}, "c": {"allOf": [1, 2]},
                                      "d": {"type": {"x": 1}}, "e": {"type": "array", "items": [1], "maxItems": -1},
                                      "f": {"type": "array", "items": {"type": "object"}, "maxItems": True}}},
])
def test_malformed_schemas_do_not_raise(schema):
    cands = build_candidates(jsonld=[{"@type": "Product", "name": "Anvil", "offers": {"price": "5"}}],
                             microdata=[], metadata={"title": "T"})
    out = fill_schema(schema, cands, "https://x.test/")
    assert set(out) >= {"data", "sources", "missing", "missingRequired", "coverage"}
