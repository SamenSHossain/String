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
    ("39.99 29.99", 39.99), ("Was $39.99, now $29.99", 39.99), ("2 for 5", 2.0), ("12 345 678", 12345678.0),
])
def test_parse_number(raw, num):
    assert parse_number(raw) == num


def test_consent_path_only_counts_after_a_detour():
    article = "<html><body>" + "An article about consent management. " * 60 + "</body></html>"
    assert _verdict(200, article, url="https://gdpr.example/consent/", final="https://gdpr.example/consent/").outcome == "ok"
    assert _verdict(200, article, url="https://news.example/", final="https://news.example/consent/").outcome == "consent_wall"


def test_soft_rate_limit_page():
    page = ("<html><body><table><tr><td>Hacker News</td></tr></table>"
            "<p>Sorry, we're not able to serve your requests this quickly.</p></body></html>")
    assert _verdict(200, page).outcome == "rate_limited"


def test_markdown_drops_inline_data_images():
    html = '<p>Hi <img alt="Logo" src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="> <img alt="p" src="/a.jpg"></p>'
    md = extract.to_markdown(html, "https://ex.test/")
    assert "base64" not in md and "Logo" in md and "![p](https://ex.test/a.jpg)" in md


def test_non_ascii_urls_are_percent_encoded():
    assert normalize_url("https://example.com/\u00fcn\u00efcode/p\u00e4th?q=\u00e9").url == \
        "https://example.com/%C3%BCn%C3%AFcode/p%C3%A4th?q=%C3%A9"
    assert normalize_url("https://example.com/a%20b?x=%2F").url == "https://example.com/a%20b?x=%2F"


def test_microdata_walk_is_linear():
    import time
    inner = "".join(f"<div><p><span>t{i}</span><em>x</em></p></div>" for i in range(20000))
    html = f"<html><body><div itemscope itemtype='https://schema.org/Thing'><span itemprop='name'>N</span>{inner}</div></body></html>"
    t = time.perf_counter()
    items = extract.microdata(extract.parse(html), "https://x/")
    assert items[0]["name"] == "N"
    assert time.perf_counter() - t < 2.0


# --- regressions from the independent review -------------------------------------------

LONG = "<p>" + "Real paragraph text about the topic at hand. " * 70 + "</p>"


def test_vendor_names_in_prose_are_not_challenges():
    for snippet in ("Request unsuccessful. Incapsula incident ID: 123-456", "Sucuri Website Firewall",
                    "window.awsWafCookieDomainList", "geo.captcha-delivery.com", "errors.edgesuite.net"):
        page = f"<html><head><title>How bot walls work</title></head><body>{LONG}<pre>{snippet}</pre></body></html>"
        assert _verdict(200, page).outcome == "ok", snippet


def test_cloudflare_origin_error_is_server_error():
    page = '<html><head><title>example.com | 522: Connection timed out</title></head><body><div id="cf-error-details">' \
           "The origin web server timed out responding to this request.</div></body></html>"
    v = _verdict(522, page)
    assert v.outcome == "server_error" and v.vendor == "cloudflare"


def test_imperva_sitewide_script_is_not_a_challenge():
    page = f'<html><head><script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3"></script></head><body>{LONG}</body></html>'
    assert _verdict(200, page).outcome == "ok"
    block = '<html><body><iframe src="/_Incapsula_Resource?CWUDNSAI=24&xinfo=1"></iframe>Incapsula incident ID: 1</body></html>'
    assert _verdict(200, block).outcome == "challenge"


def test_sitewide_scripts_on_small_pages():
    small = "<html><head><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></head><body>" \
            + "<p>Release notes for version 2.4. " * 20 + "</p></body></html>"
    assert _verdict(200, small).outcome == "ok"
    nf = "<html><head><script src='https://www.google.com/recaptcha/api.js?render=x'></script></head><body>Page not found</body></html>"
    assert _verdict(404, nf).outcome == "not_found"


def test_tiny_real_page_is_ok():
    example = ("<!doctype html><html><head><title>Example Domain</title></head><body><div><h1>Example Domain</h1>"
               "<p>This domain is for use in documentation examples without needing permission.</p>"
               '<p><a href="https://iana.org/domains/example">Learn more</a></p></div></body></html>')
    assert _verdict(200, example).outcome == "ok"


def test_canonicalising_redirect_is_not_a_gate():
    article = f"<html><head><title>What age verification laws require</title></head><body>{LONG}</body></html>"
    assert _verdict(200, article, url="https://news.example/explainers/age-verification",
                    final="https://news.example/explainers/age-verification/").outcome == "ok"


def test_cp1252_page_with_a_stray_utf8_fragment_keeps_its_charset():
    page = ("<html><body><p>" + "Café crème brûlée à la française. " * 30 + "</p><p>Łódź</p></body></html>")
    raw = page.replace("Łódź", "Łodz").encode("cp1252", errors="ignore")  # Ł is not in cp1252
    raw = raw.replace(b"odz", b"\xc5\x81odz")  # embed UTF-8 'Ł' bytes (C5 81) as CMS fields do
    text, enc, src = decode_body(raw, "text/html; charset=windows-1252")
    assert enc == "cp1252" and src == "http-header"
    assert "Café crème brûlée" in text


def test_nan_in_json_ld_is_dropped():
    tree = extract.parse('<script type="application/ld+json">{"@type":"Product","name":"X",'
                         '"aggregateRating":{"ratingValue":NaN,"reviewCount":Infinity}}</script>')
    ld = extract.json_ld(tree)
    assert ld[0]["aggregateRating"] == {"ratingValue": None, "reviewCount": None}
    import json
    json.dumps(ld, allow_nan=False)  # serializable
