"""Local HTTP server that emulates the gate flows of real sites.

The fetcher pins DNS with CURLOPT_RESOLVE, so tests point real host names
(store.steampowered.com, www.nu.nl, ...) at this server and exercise the same recipe
code paths that run in production.
"""

from __future__ import annotations

import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from webfetch.config import Settings

STEAM_PAGE = """<!DOCTYPE html><html lang="en"><head><title>Grand Theft Auto V Legacy on Steam</title>
<meta property="og:title" content="Grand Theft Auto V Legacy on Steam">
<meta name="description" content="Grand Theft Auto V for PC offers players the option to explore the award-winning world.">
<link rel="canonical" href="https://store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/">
</head><body>
<div class="apphub_AppName" id="appHubAppName">Grand Theft Auto V Legacy</div>
<div class="game_description_snippet">Grand Theft Auto V for PC offers players the option to explore the award-winning world of Los Santos.</div>
<div class="user_reviews_summary_row" data-tooltip-html="87% of the 1,734,567 user reviews for this game are positive.">
<span class="game_review_summary positive">Very Positive</span></div>
<div class="release_date"><div class="subtitle">Release Date:</div><div class="date">13 Apr, 2015</div></div>
<div class="dev_row"><div class="subtitle">Developer:</div><div class="summary" id="developers_list"><a href="/dev/rockstar">Rockstar North</a></div></div>
<div class="dev_row"><div class="subtitle">Publisher:</div><div class="summary"><a href="/pub/rockstar">Rockstar Games</a></div></div>
<div class="glance_tags popular_tags"><a class="app_tag" href="/tags/Open World">Open World</a><a class="app_tag" href="/tags/Action"> Action </a></div>
<div class="game_area_purchase_game_wrapper"><div class="game_area_purchase_game">
<div class="game_purchase_action"><div class="game_purchase_price price" data-price-final="2998">$29.98</div></div></div></div>
<div itemprop="offers" itemscope itemtype="http://schema.org/Offer"><meta itemprop="priceCurrency" content="USD"><meta itemprop="price" content="29.98"></div>
<p>""" + ("About This Game. " * 40) + """</p></body></html>"""

AGECHECK_PAGE = """<!DOCTYPE html><html><head><title>Site Error</title></head><body>
<h2>Content in this product may not be appropriate for all ages.</h2><p>Please enter your birth date to continue:</p>
<select name="ageDay"></select><a id="view_product_page_btn">View Page</a></body></html>"""

NU_HOME = """<!DOCTYPE html><html lang="nl"><head><title>NU - Het laatste nieuws het eerst op NU.nl</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"NU.nl","url":"https://www.nu.nl"},
{"@type":"NewsArticle","headline":"Kabinet presenteert plannen","datePublished":"2026-11-15T08:00:00+01:00","author":[{"@type":"Person","name":"Redactie"}]}]}</script>
</head><body><main><h1>Het laatste nieuws</h1><p>""" + ("Nieuwsbericht over Nederland. " * 60) + """</p></main></body></html>"""

ZEIT_PAGE = """<!DOCTYPE html><html lang="de"><head><title>ZEIT ONLINE | Nachrichten</title></head>
<body><main><h1>Nachrichten, Hintergründe und Debatten</h1><p>""" + ("Ein Artikel über Politik. " * 60) + """</p></main></body></html>"""

CHALLENGE_PAGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>
<div id="cf-wrapper">Checking your browser</div><script>window._cf_chl_opt={cvId:'3'}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: bytes | str = b"", headers: dict | None = None, cookies: list[str] | None = None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        hdrs = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        for k, v in hdrs.items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        return {k: m.value for k, m in jar.items()}

    def do_GET(self):  # noqa: C901 - a small router
        host = (self.headers.get("Host") or "").split(":")[0]
        port = self.server.server_address[1]
        parts = urlsplit(self.path)
        path, qs = parts.path, parse_qs(parts.query)
        cookies = self._cookies()
        self.server.seen.append({"host": host, "path": self.path, "cookies": cookies,
                                 "ua": self.headers.get("User-Agent", "")})

        if host == "store.steampowered.com":
            if path.startswith("/app/271590"):
                if cookies.get("birthtime") and cookies.get("wants_mature_content") == "1":
                    return self._send(200, STEAM_PAGE)
                return self._send(302, headers={"Location": f"http://store.steampowered.com:{port}/agecheck/app/271590/"})
            if path.startswith("/agecheck/app/271590"):
                return self._send(200, AGECHECK_PAGE)

        if host == "www.nu.nl":
            if path == "/privacy-gate/accept":
                if not qs.get("authId"):
                    return self._send(400, "missing authId")
                dest = qs.get("redirectUri", ["/"])[0]
                return self._send(302, headers={"Location": dest}, cookies=["dpg_consent=granted; Path=/"])
            if cookies.get("dpg_consent") == "granted":
                return self._send(200, NU_HOME)
            cb = quote(f"http://www.nu.nl:{port}/privacy-gate/accept?redirectUri={quote(path, safe='')}", safe="")
            return self._send(302, headers={"Location": f"https://myprivacy.dpgmedia.nl/consent?siteKey=abc&callbackUrl={cb}&isLoggedIn=false"})

        if host == "www.zeit.de":
            if path.startswith("/zustimmung"):
                return self._send(200, "<html><head><title>Zustimmung</title></head><body>Bitte zustimmen</body></html>")
            if "zonconsent" in cookies:
                return self._send(200, ZEIT_PAGE)
            back = quote(f"http://www.zeit.de:{port}{path}", safe="")
            return self._send(302, headers={"Location": f"/zustimmung?url={back}"})

        if host == "echo.test":
            if path == "/set":
                return self._send(200, "<html><body>set</body></html>", cookies=["sess=secret123; Path=/"])
            if path == "/cookies":
                body = "<html><body><p>" + ";".join(f"{k}={v}" for k, v in sorted(cookies.items())) + "</p></body></html>"
                return self._send(200, body)
            if path == "/sjis":
                body = ('<html><head><meta charset="Shift_JIS"><title>テスト</title></head><body><p>'
                        + "日本語のページです。" * 30 + "</p></body></html>").encode("cp932")
                return self._send(200, body, headers={"Content-Type": "text/html"})
            if path == "/challenge":
                return self._send(403, CHALLENGE_PAGE)
            if path == "/empty":
                return self._send(200, b"")
            if path == "/loop":
                return self._send(302, headers={"Location": "/loop"})
            if path == "/to-private":
                return self._send(302, headers={"Location": "http://10.0.0.1/admin"})
            if path == "/product":
                return self._send(200, PRODUCT_PAGE)

        self._send(404, "<html><body>not found</body></html>")


PRODUCT_PAGE = """<!DOCTYPE html><html lang="en"><head><title>Acme Anvil | Acme Store</title>
<meta property="og:title" content="Acme Anvil">
<meta property="og:image" content="/img/anvil.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"Product","name":"Acme Anvil 50kg","sku":"ANV-50",
 "brand":{"@type":"Brand","name":"Acme"},
 "image":["https://cdn.acme.test/anvil-1.jpg","https://cdn.acme.test/anvil-2.jpg"],
 "description":"A very heavy anvil.",
 "offers":{"@type":"Offer","price":"1.299,00","priceCurrency":"EUR","availability":"https://schema.org/InStock"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.6","reviewCount":"212"},}
</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme Corp","url":"https://acme.test"}</script>
</head><body><h1>Acme Anvil 50kg</h1><p>""" + ("Forged steel. " * 50) + """</p></body></html>"""


@pytest.fixture(scope="session")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.seen = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture()
def settings():
    return Settings(allow_private=True, proxy=None, ca_bundle=None)
