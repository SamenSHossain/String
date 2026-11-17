"""Regressions for the extraction and wall-classification audit findings.

D1  readable mode (mainContentOnly / markdownMode readable) resolves links like a browser:
    against the page URL's directory and <base href>, not scheme://host.
D2  JSON embedded in a page (ld+json, __NEXT_DATA__) never raises and is always writable:
    pathological nesting is skipped, NaN/infinities become null, lone surrogates U+FFFD.
D3  Amazon's robot check and its anti-automation 503 are walls (escalated, answered 502),
    not a server error answered 200 with the refusal page's Markdown. Tag signatures stay
    linear on hostile markup.
D4  A JavaScript shell whose only text is a Japanese "JavaScript is disabled" notice
    (rakuten.co.jp) is js_required, not ok.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import extract
from webfetch import fetcher as fetcher_mod
from webfetch.api import create_app
from webfetch.classify import classify
from webfetch.config import Settings

PAGE_URL = "https://ex.test/a/b/p"
_PARA = "<p>" + "Plenty of ordinary article text, so the main-content extractor keeps this paragraph. " * 4 + "</p>"
_LINKS = ('<a href="../x.html">up</a>, <a href=" #f ">fragment</a>, <a href="/root">root</a>, '
          '<a href="sib.html?q=1">sibling</a>, <a href="//cdn.example/lib">scheme-relative</a>, '
          '<a href="https://abs.example/z">absolute</a>, <a href="mailto:a@b.example">mail</a>, '
          '<a href="tel:+100">call</a> <img src="pic.png" alt="picture">')


def _article(head: str = "", links: str = _LINKS) -> str:
    return (f"<!doctype html><html><head><title>Links</title>{head}</head><body><article><h1>Relative links</h1>"
            f"{_PARA}<p>See {links}.</p>{_PARA}{_PARA}</article></body></html>")


def _targets(md: str) -> list[str]:
    return re.findall(r"\]\(<?([^)\s>]+)", md)


def _verdict(status: int, body: str, url: str = "https://www.amazon.com/s?k=laptop"):
    tree = extract.parse(body)
    title = tree.css_first("title")
    return classify(status=status, headers={}, original_url=url, final_url=url, body=body.encode(), text=body,
                    visible_chars=len(extract.visible_text(tree)), title=title.text() if title else "",
                    content_type="text/html")


# --- D1: readable mode resolves links the way a browser does -------------------------------

_NO_BASE = ["https://ex.test/a/x.html", "https://ex.test/a/b/p#f", "https://ex.test/root",
            "https://ex.test/a/b/sib.html?q=1", "https://cdn.example/lib", "https://abs.example/z",
            "mailto:a@b.example", "tel:+100", "https://ex.test/a/b/pic.png"]
_ABS_BASE = ["https://other.example/docs/x.html", "https://other.example/docs/v2/#f", "https://other.example/root",
             "https://other.example/docs/v2/sib.html?q=1", "https://cdn.example/lib", "https://abs.example/z",
             "mailto:a@b.example", "tel:+100", "https://other.example/docs/v2/pic.png"]
_REL_BASE = ["https://ex.test/a/b/sub/x.html", "https://ex.test/a/b/sub/dir/#f", "https://ex.test/root",
             "https://ex.test/a/b/sub/dir/sib.html?q=1", "https://cdn.example/lib", "https://abs.example/z",
             "mailto:a@b.example", "tel:+100", "https://ex.test/a/b/sub/dir/pic.png"]


@pytest.mark.parametrize("head,expected", [
    ("", _NO_BASE),
    ('<base href="https://other.example/docs/v2/">', _ABS_BASE),
    ('<base href="sub/dir/">', _REL_BASE),
    # only the first <base href> counts; a javascript: base is ignored, as in browsers
    ('<base target="_blank"><base href="https://other.example/docs/v2/"><base href="https://third.example/">', _ABS_BASE),
    ('<base href="javascript:alert(1)">', _NO_BASE),
])
def test_readable_mode_resolves_links_like_a_browser(head, expected):
    html = _article(head)
    readable = extract._readable_markdown(html, PAGE_URL)
    assert readable is not None and "Plenty of ordinary article text" in readable  # trafilatura's output
    assert extract.to_markdown(html, PAGE_URL, "readable") == readable
    assert _targets(readable) == expected
    # full mode (html-to-markdown) already resolved these; the two modes now agree
    assert _targets(extract.to_markdown(html, PAGE_URL, "full")) == expected


def test_readable_mode_resolves_lazy_image_sources():
    html = _article(links='<img data-src="lazy.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="lazy">')
    assert _targets(extract._readable_markdown(html, PAGE_URL)) == ["https://ex.test/a/b/lazy.jpg"]
    based = _article('<base href="https://cdn.example/img/">', '<img data-src="lazy.jpg" alt="lazy">')
    assert _targets(extract._readable_markdown(based, PAGE_URL)) == ["https://cdn.example/img/lazy.jpg"]


def test_links_metadata_and_microdata_use_the_document_base():
    html = ('<html><head><base href="https://other.example/docs/v2/"><link rel="canonical" href="canon">'
            '<meta property="og:image" content="/i.png"></head><body><a href="page.html">p</a>'
            '<a href="JavaScript:void(0)">js</a><a href="../up.html">u</a>'
            '<div itemscope itemtype="https://schema.org/Thing"><a itemprop="url" href="item">i</a></div></body></html>')
    tree = extract.parse(html)
    assert [link["url"] for link in extract.links(tree, PAGE_URL)] == [
        "https://other.example/docs/v2/page.html", "https://other.example/docs/up.html",
        "https://other.example/docs/v2/item"]
    meta = extract.page_metadata(tree, PAGE_URL)
    assert meta["canonicalUrl"] == "https://other.example/docs/v2/canon"
    assert meta["image"] == "https://ex.test/i.png"  # meta content, not a link: against the page URL
    assert extract.microdata(tree, PAGE_URL) == [{"@type": "Thing", "url": "https://other.example/docs/v2/item"}]


def test_unparsable_urls_do_not_raise():
    # urljoin raises ValueError on these; links, metadata and microdata used to propagate it
    html = ('<html><head><link rel="canonical" href="http://[::1"><meta property="og:image" content="//[x">'
            '</head><body><a href="//[bad">x</a><a href="/ok">ok</a>'
            '<div itemscope itemtype="https://schema.org/Thing"><a itemprop="url" href="http://[x">u</a></div>'
            '</body></html>')
    tree = extract.parse(html)
    assert extract.links(tree, PAGE_URL) == [{"url": "https://ex.test/ok", "text": "ok"}]
    meta = extract.page_metadata(tree, PAGE_URL)
    assert meta["canonicalUrl"] is None and meta["image"] is None
    assert extract.microdata(tree, PAGE_URL) == [{"@type": "Thing", "url": ""}]  # HTML spec: "" when unparsable
    assert "[x](//[bad)" in extract.to_markdown(html, PAGE_URL, "readable")


# --- D2: page JSON never raises, never returns what cannot be written back out -------------

_KEPT = '<script type="application/ld+json">{"@type":"Thing","name":"kept"}</script>'


def _json_page(ld: str, next_data: str | None = None) -> str:
    nd = f'<script id="__NEXT_DATA__" type="application/json">{next_data}</script>' if next_data is not None else ""
    return (f'<html><head><script type="application/ld+json">{ld}</script>{_KEPT}{nd}</head>'
            "<body><p>x</p></body></html>")


@pytest.mark.parametrize("depth", [1_200, 100_000])
def test_deeply_nested_page_json_is_skipped(depth):
    for blob in ("[" * depth + "]" * depth, '{"a":' * depth + "1" + "}" * depth):
        tree = extract.parse(_json_page(blob, next_data=blob))
        assert extract.json_ld(tree) == [{"@type": "Thing", "name": "kept"}]  # the deep blob alone is dropped
        assert extract.hydration_data(tree) == {}


def test_json_depth_limit_counts_structure_not_strings():
    limit = extract.MAX_JSON_DEPTH
    at_limit = '{"@type":"Thing","x":' + "[" * (limit - 1) + "]" * (limit - 1) + "}"
    over = '{"@type":"Thing","x":' + "[" * limit + "]" * limit + "}"
    assert extract.json_ld(extract.parse(_json_page(at_limit)))[0]["@type"] == "Thing"
    assert extract.json_ld(extract.parse(_json_page(over))) == [{"@type": "Thing", "name": "kept"}]
    # brackets inside strings (escaped quotes and backslashes included) are text, not nesting
    texty = '{"@type":"Thing","s":"' + "[{" * 300 + '\\"]]","t":"\\\\","u":"\\\\\\"[["}'
    assert extract.json_ld(extract.parse(_json_page(texty)))[0]["s"] == "[{" * 300 + '"]]'
    # ... and closing brackets inside strings cannot hide real nesting
    hidden = '{"@type":"Thing","s":"' + "]" * 300 + '","x":' + "[" * (limit + 5) + "]" * (limit + 5) + "}"
    assert extract.json_ld(extract.parse(_json_page(hidden))) == [{"@type": "Thing", "name": "kept"}]


def test_ordinary_json_ld_is_unchanged():
    graph = {"@context": "https://schema.org", "@id": "https://ex.test/#site", "@graph": [
        {"@type": "WebSite", "name": "S"},
        {"@type": "Article", "headline": "H",
         "author": {"@type": "Person", "name": "Ann", "knows": [{"@type": "Person", "name": "Bo"}]}},
        {"@context": "https://schema.org/", "@graph": [{"@type": "ImageObject", "url": "/i.png"}]},
        [{"@type": "Comment", "text": "in a nested list"}],
    ]}
    html = ("<html><head>"
            f'<script type="application/ld+json">{json.dumps(graph)}</script>'
            '<script type="application/ld+json">[{"@type":"Product","name":"P","offers":{"@type":"Offer","price":"9.50"}},'
            '{"@type":"Organization","name":"O"}]</script>'
            '<script type="application/ld+json"><!-- {"@type":"Recipe","name":"R","recipeYield":4,} --></script>'
            "</head><body><p>x</p></body></html>")
    assert extract.json_ld(extract.parse(html)) == [
        {"@type": "WebSite", "name": "S", "@context": "https://schema.org"},
        {"@type": "Article", "headline": "H", "@context": "https://schema.org",
         "author": {"@type": "Person", "name": "Ann", "knows": [{"@type": "Person", "name": "Bo"}]}},
        {"@type": "ImageObject", "url": "/i.png", "@context": "https://schema.org/"},
        {"@type": "Comment", "text": "in a nested list"},
        {"@context": "https://schema.org", "@id": "https://ex.test/#site"},
        {"@type": "Product", "name": "P", "offers": {"@type": "Offer", "price": "9.50"}},
        {"@type": "Organization", "name": "O"},
        {"@type": "Recipe", "name": "R", "recipeYield": 4},
    ]


def test_page_json_is_always_serializable():
    ld = ('{"@type":"Product","name":"Cut emoji \\ud83d","alt":"pair \\ud83d\\ude00","path":"C:\\\\ud83d",'
          '"w":1e999,"n":-1E400,"r":NaN,"ok":2.5}')
    huge_int = '{"@type":"Thing","n":' + "7" * 5000 + "}"  # int() refuses > 4,300 digits: ValueError
    html = (f'<html><head><script type="application/ld+json">{ld}</script>'
            f'<script type="application/ld+json">{huge_int}</script>'
            '<script id="__NEXT_DATA__" type="application/json">{"props":{"t":"\\udc00x","v":1e309}}</script>'
            "</head><body><p>x</p></body></html>")
    tree = extract.parse(html)
    got_ld, got_hydration = extract.json_ld(tree), extract.hydration_data(tree)
    assert got_ld == [{"@type": "Product", "name": "Cut emoji \ufffd", "alt": "pair \U0001F600",
                       "path": "C:\\ud83d", "w": None, "n": None, "r": None, "ok": 2.5}]
    assert got_hydration == {"__NEXT_DATA__": {"props": {"t": "\ufffdx", "v": None}}}
    # what the API's JSONResponse does with them
    json.dumps({"jsonLd": got_ld, "hydration": got_hydration}, ensure_ascii=False, allow_nan=False).encode("utf-8")


# --- D3: Amazon's robot check and anti-automation refusal are walls ------------------------

# Amazon's anti-automation refusal as served (17 Nov 2026, www.amazon.com/s?k=laptop, 503),
# with its stylesheet shortened. It has no visible text at all.
AMAZON_503 = """<!--
        To discuss automated access to Amazon data please contact api-services-support@amazon.com.
        For information about migrating to our APIs refer to our Marketplace APIs at https://developer.amazonservices.com/ref=rm_5_sv, or our Product Advertising API at https://affiliate-program.amazon.com/gp/advertising/api/detail/main.html/ref=rm_5_ac for advertising use cases.
-->
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="x-ua-compatible" content="ie=edge">
  <title>Sorry! Something went wrong!</title>
  <style>#d { display: none } #d[src] { display: inline }</style>
</head>
<body>
    <a href="/ref=cs_503_logo"><img id="b" src="https://images-na.ssl-images-amazon.com/images/G/01/error/logo._TTD_.png" alt="Amazon.com"></a>
    <form id="a" accept-charset="utf-8" action="/s" method="GET" role="search">
        <div id="c">
            <input id="e" name="field-keywords" placeholder="Search">
            <input name="ref" type="hidden" value="cs_503_search">
            <input id="f" type="submit" value="Go">
        </div>
    </form>
<div id="g">
  <div><a href="/ref=cs_503_link"><img src="https://images-na.ssl-images-amazon.com/images/G/01/error/500_503.png"
                                        alt="Sorry! Something went wrong on our end. Please go back and try again or go to Amazon's home page."></a>
  </div>
  <a href="/dogsofamazon/ref=cs_503_d" target="_blank" rel="noopener noreferrer"><img id="d" alt="Dogs of Amazon"></a>
  <script>document.getElementById("d").src = "https://images-na.ssl-images-amazon.com/images/G/01/error/" + (Math.floor(Math.random() * 43) + 1) + "._TTD_.jpg";</script>
</div>
</body>
</html>
"""
# The robot check, reduced to its known structure (no live sample was captured): the same
# comment, and a form that posts to /errors/validateCaptcha.
AMAZON_CAPTCHA = """<!doctype html><html lang="en-us" class="a-no-js"><head><meta charset="utf-8">
<title dir="ltr">Amazon.com</title></head><body>
<!--
        To discuss automated access to Amazon data please contact api-services-support@amazon.com.
        For information about migrating to our APIs refer to our Marketplace APIs at https://developer.amazonservices.com/ref=rm_c_sv, or our Product Advertising API at https://affiliate-program.amazon.com/gp/advertising/api/detail/main.html/ref=rm_c_ac for advertising use cases.
-->
<div class="a-container a-padding-double-large"><div class="a-box a-alert a-alert-info a-spacing-base">
<h4>Enter the characters you see below</h4>
<p class="a-last">Sorry, we just need to make sure you're not a robot. For best results, please make sure your browser is accepting cookies.</p></div>
<form method="get" action="/errors/validateCaptcha" name="">
<input type=hidden name="amzn" value="b5Xk2" /><input type=hidden name="amzn-r" value="&#047;s?k=laptop" />
<img src="https://images-na.ssl-images-amazon.com/captcha/usvmgloq/Captcha_kwrrnqwkph.jpg">
<input autocomplete="off" placeholder="Type characters" id="captchacharacters" name="field-keywords" type="text">
<button type="submit" class="a-button-text">Continue shopping</button></form></div></body></html>"""
AMAZON_RESULTS = ("<!doctype html><html><head><title>Amazon.com : laptop</title></head><body>"
                  "<span>1-16 of over 100,000 results for \"laptop\"</span>"
                  + "<div><h2>Laptop 15.6 inch, 16GB RAM, 512GB SSD</h2><span>4.4 out of 5 stars</span>"
                    "<span>$499.99</span><span>FREE delivery Sat, Nov 21</span></div>" * 60
                  + "</body></html>")


def test_amazon_anti_automation_503_is_blocked():
    v = _verdict(503, AMAZON_503)
    assert (v.outcome, v.vendor) == ("blocked", "amazon") and v.browser_may_help


@pytest.mark.parametrize("status", [200, 503])
def test_amazon_robot_check_is_a_challenge(status):
    v = _verdict(status, AMAZON_CAPTCHA)
    assert (v.outcome, v.vendor) == ("challenge", "amazon")


@pytest.mark.parametrize("markup", ["<div" * 50_000, '<div id="x"' * 20_000, "<form" * 40_000, "<!--" * 50_000])
def test_tag_signatures_stay_linear_on_hostile_markup(markup):
    # in-tag scans stop at "<", so scans from consecutive tag starts never overlap: "<div" x 50,000
    # took ~0.3 s per pattern (Cloudflare's challenge div, the JS-shell root div) with [^>]{0,300}
    page = "<html><head><script></script><script></script><script></script></head><body>" + markup + "</body></html>"
    t = time.perf_counter()
    _verdict(200, page)  # near-empty: every interstitial signature and the shell check run
    assert time.perf_counter() - t < 0.25


def test_normal_amazon_pages_are_unaffected():
    assert _verdict(200, AMAZON_RESULTS).outcome == "ok"
    # a 503 without the refusal comment is still the destination's own error
    outage = AMAZON_503.split("-->", 1)[1]
    assert _verdict(503, outage).outcome == "server_error"
    # a short page quoting both (escaped, as prose does) is not the wall itself
    prose = ("<html><head><title>Why does my scraper fail?</title></head><body><p>The page source says "
             "&lt;!-- To discuss automated access to Amazon data please contact api-services-support@amazon.com "
             "--&gt; and the robot check form posts to /errors/validateCaptcha.</p></body></html>")
    assert _verdict(200, prose, url="https://forum.example/t/1").outcome == "ok"


# --- D4: a JavaScript shell whose only text is a Japanese "JavaScript is disabled" notice --

# rakuten.co.jp's top page fetched without JavaScript (17 Nov 2026), reduced to its skeleton:
# 35 scripts, empty containers, and 79 visible characters, all of them the notice. Rendered
# by the browser tier, the same page has 54,379 visible characters.
RAKUTEN_TOP = ("<!DOCTYPE html><html class=\"no-js\" lang=\"ja\"><head><meta charset=\"utf-8\">"
               "<title>【楽天市場】Shopping is Entertainment! ： インターネット最大級の通信販売、通販オンラインショッピングコミュニティ</title>"
               + '<script src="//www.rakuten.co.jp/com/js/x.js"></script>' * 6 + "</head><body><div id=\"wrapper\">"
               "<div class=\"globalBannerWrapper\"><div class=\"globalBannerInner\"></div></div>"
               "<div id=\"no-js-caution\"><p class=\"caution-read\">ご使用のブラウザでJavaScriptが無効なため、一部の機能をご利用できません。</p>"
               "<p class=\"caution-sub-read\">JavaScriptの設定方法は、<a href=\"https://ichiba.faq.rakuten.net/\">楽天市場ヘルプページ</a>"
               "をご覧ください。</p></div><div id=\"page-app\"><div class=\"app-root\"></div></div>"
               "<div class=\"gradation-background\"><div id=\"bigBannerWidget\"></div><div id=\"container\">"
               "<div id=\"main-container\"><div id=\"main-section\"></div></div><div id=\"side-navi\"></div></div>"
               "<div id=\"footer-section\"></div></div></div>"
               + "<script>window.sc_vars = window.sc_vars || {};</script>" * 29 + "</body></html>")


def test_japanese_javascript_disabled_shell_is_js_required():
    v = _verdict(200, RAKUTEN_TOP, url="https://www.rakuten.co.jp/")
    assert v.outcome == "js_required" and v.browser_may_help  # render "auto" now escalates to the browser
    # an ordinary short Japanese page is still a page
    short = ("<html lang=\"ja\"><head><title>お知らせ</title></head><body><h1>営業時間のお知らせ</h1>"
             "<p>年末年始は12月29日から1月3日まで休業いたします。ご不便をおかけしますが、よろしくお願いいたします。</p></body></html>")
    assert _verdict(200, short, url="https://shop.example.jp/news").outcome == "ok"
    # and a full article carrying the same notice in <noscript> is not a shell
    article = ("<html lang=\"ja\"><head><title>記事</title><script src=a.js></script><script src=b.js></script>"
               "<script src=c.js></script></head><body><noscript>JavaScriptを有効にしてください。</noscript><p>"
               + "東京の天気は晴れ、最高気温は十八度の予想です。" * 30 + "</p></body></html>")
    assert _verdict(200, article, url="https://news.example.jp/a/1").outcome == "ok"


# --- the same findings through the API --------------------------------------------------

DEEP_JSON_PAGE = ('<html><head><script type="application/ld+json">' + "[" * 1_200 + "]" * 1_200 + "</script>"
                  + _KEPT + '<script type="application/ld+json">{"@type":"Product","name":"Odd \\ud83d","w":1e999}'
                  '</script><script id="__NEXT_DATA__" type="application/json">' + '{"a":' * 1_500 + "1"
                  + "}" * 1_500 + "</script></head><body><p>" + "Body text. " * 40 + "</p></body></html>")
PAGES = {
    ("www.amazon.com", "/s"): (503, AMAZON_503),
    ("docs.test", "/a/b/p"): (200, _article()),
    ("json.test", "/deep"): (200, DEEP_JSON_PAGE),
}


class _Pages(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        status, body = PAGES.get((host, urlsplit(self.path).path), (404, "<html><body>not found</body></html>"))
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def port():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)


class _FakeBrowser:
    """Stands in for the browser tier: renders the real results page."""

    def __init__(self):
        self.calls = 0

    async def render(self, url, *, cookies, headers, timeout, max_bytes=0):
        self.calls += 1
        return {"html": AMAZON_RESULTS, "finalUrl": url, "status": 200, "headers": {"content-type": "text/html"},
                "strategy": "browser:patchright", "ms": 5.0}


def _client(browser=None):
    return TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=1000), browser=browser))


def test_api_readable_markdown_keeps_link_directories(port):
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://docs.test:{port}/a/b/p", "format": "markdown",
                                      "mainContentOnly": True})
    assert r.status_code == 200
    assert _targets(r.text)[:4] == [f"http://docs.test:{port}/a/x.html", f"http://docs.test:{port}/a/b/p#f",
                                    f"http://docs.test:{port}/root", f"http://docs.test:{port}/a/b/sib.html?q=1"]


def test_api_survives_hostile_page_json(port):
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": f"http://json.test:{port}/deep",
                                      "include": ["jsonLd", "hydration", "markdown"]})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["ok"] and body["hydration"] == {}
    assert body["jsonLd"] == [{"@type": "Thing", "name": "kept"}, {"@type": "Product", "name": "Odd \ufffd", "w": None}]


def test_api_amazon_refusal_is_a_502_wall_and_escalates(port):
    url = f"http://www.amazon.com:{port}/s?k=laptop"
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": url, "format": "markdown"})
    assert r.status_code == 502 and r.headers["x-outcome"] == "blocked"  # not 200 with the refusal's Markdown
    body = r.json()
    assert body["vendor"] == "amazon" and "markdown" not in body
    assert [(a["strategy"], a["outcome"]) for a in body["attempts"]] == [("http:chrome", "blocked"),
                                                                          ("http:safari", "blocked")]
    browser = _FakeBrowser()
    with _client(browser) as c:
        r = c.post("/v1/fetch", json={"url": url, "format": "markdown"})
    assert browser.calls == 1 and r.status_code == 200 and r.headers["x-strategy"] == "browser:patchright"
    assert "1-16 of over 100,000 results" in r.text
