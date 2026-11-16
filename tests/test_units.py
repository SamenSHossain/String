from __future__ import annotations

import asyncio

import pytest

from webfetch import extract
from webfetch.classify import classify
from webfetch.decode import decode_body
from webfetch.netguard import BlockedDestination, InvalidUrl, ip_is_public, normalize_url, resolve_public
from webfetch.schema_fill import parse_number


@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com/"),
    ("HTTPS://Example.COM:443/a?b=1#frag", "https://example.com/a?b=1"),
    ("http://example.com:8080", "http://example.com:8080/"),
    ("https://bücher.de/", "https://xn--bcher-kva.de/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw).url == expected


@pytest.mark.parametrize("raw", ["ftp://x.com/", "https://user:pw@x.com/", "", "https://exa mple.com", "javascript:alert(1)"])
def test_normalize_url_rejects(raw):
    with pytest.raises(InvalidUrl):
        normalize_url(raw)


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False),
    ("100.64.0.1", False), ("::1", False), ("::ffff:127.0.0.1", False), ("2001:4860:4860::8888", True),
    ("fd00::1", False), ("0.0.0.0", False),
])
def test_ip_is_public(ip, public):
    assert ip_is_public(ip) is public


def test_resolve_public_blocks_literals():
    with pytest.raises(BlockedDestination):
        asyncio.run(resolve_public("169.254.169.254", 80))
    with pytest.raises(BlockedDestination):
        asyncio.run(resolve_public("localhost", 80))
    assert asyncio.run(resolve_public("127.0.0.1", 80, allow_private=True)) == ["127.0.0.1"]


def test_decode_variants():
    assert decode_body("ok".encode("utf-8-sig"), None)[1:] == ("utf-8", "bom")
    gbk = '<html><head><meta charset="gb2312"></head><body>中文网页内容</body></html>'.encode("gbk")
    text, enc, src = decode_body(gbk, "text/html")
    assert "中文网页内容" in text and enc == "gb18030" and src == "meta"
    # header lies (says utf-8) but bytes are cp1251: falls through to detection, no exception
    cyr = ("<html><body>" + "Привет, мир! Это тестовая страница. " * 20 + "</body></html>").encode("cp1251")
    text, enc, src = decode_body(cyr, "text/html; charset=utf-8")
    assert "Привет" in text and src in {"detected", "meta"}


def _verdict(status=200, body="", url="https://x.test/", final=None, headers=None):
    tree = extract.parse(body)
    vis = extract.visible_text(tree)
    title_node = tree.css_first("title")
    return classify(status=status, headers=headers or {}, original_url=url, final_url=final or url,
                    body=body.encode(), text=body, visible_chars=len(vis),
                    title=title_node.text() if title_node else "", content_type="text/html")


def test_classify_variants():
    assert _verdict(body="<html><title>Hi</title><body>" + "word " * 400 + "</body></html>").outcome == "ok"
    assert _verdict(403, "<html><head><title>Just a moment...</title></head><body></body></html>").vendor == "cloudflare"
    assert _verdict(200, "<html><body>x</body></html>", final="https://myprivacy.dpgmedia.nl/consent?x=1").outcome == "consent_wall"
    assert _verdict(200, "<html><body>age</body></html>", final="https://store.steampowered.com/agecheck/app/1/").outcome == "age_gate"
    assert _verdict(200, "").outcome == "empty"
    shell = "<html><head><script src=a.js></script><script src=b.js></script><script>x</script></head><body><div id=root></div></body></html>"
    assert _verdict(200, shell).outcome == "js_required"
    assert _verdict(404, "<html><body>missing</body></html>").outcome == "not_found"
    assert _verdict(403, "<html><body>" + "no " * 10 + "</body></html>", headers={"server": "AkamaiGHost"}).vendor == "akamai"
    # Cloudflare's injected script on a normal, content-rich page is not a challenge
    rich = "<html><body>" + "content " * 600 + "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body></html>"
    assert _verdict(200, rich).outcome == "ok"
    assert _verdict(200, "<html><body>" + "x " * 400 + "</body></html>", url="https://x.test/account",
                    final="https://x.test/login?next=/account").outcome == "login_wall"


def test_metadata_and_structured_data():
    html = """<html lang="en"><head><title> Hello  World </title>
    <meta name="description" content="Desc"><meta property="og:image" content="/i.png">
    <link rel="canonical" href="/canon">
    <script type="application/ld+json"><!-- {"@context":"https://schema.org","@graph":[{"@type":"Article","headline":"H"},{"@type":"WebSite","name":"S"}]} --></script>
    </head><body><div itemscope itemtype="https://schema.org/Recipe"><span itemprop="name">Soup</span>
    <div itemprop="author" itemscope itemtype="https://schema.org/Person"><span itemprop="name">Ann</span></div>
    <meta itemprop="cookTime" content="PT30M"></div><h1>Hello</h1></body></html>"""
    tree = extract.parse(html)
    meta = extract.page_metadata(tree, "https://ex.test/a/b")
    assert meta["title"] == "Hello World" and meta["canonicalUrl"] == "https://ex.test/canon"
    assert meta["image"] == "https://ex.test/i.png" and meta["language"] == "en"
    ld = extract.json_ld(tree)
    assert [n.get("@type") for n in ld] == ["Article", "WebSite"]
    md = extract.microdata(tree, "https://ex.test/")
    assert md == [{"@type": "Recipe", "name": "Soup", "author": {"@type": "Person", "name": "Ann"}, "cookTime": "PT30M"}]


def test_markdown_absolute_links():
    md = extract.to_markdown("<html><body><h1>T</h1><p><a href='/x'>link</a></p></body></html>", "https://ex.test/a/")
    assert "[link](https://ex.test/x)" in md


@pytest.mark.parametrize("raw,num", [
    ("$29.98", 29.98), ("29,98€", 29.98), ("1.299,00 €", 1299.0), ("1,299.00", 1299.0), ("¥ 4,620", 4620.0),
    ("Free To Play", 0.0), (12, 12.0), ("abc", None), ("CDN$ 1 234,50", 1234.5),
])
def test_parse_number(raw, num):
    assert parse_number(raw) == num
