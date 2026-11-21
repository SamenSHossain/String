"""Regressions for the second review's findings in the orchestration, HTTP and browser
tiers: destination URLs that do not parse, analysis bounded in threads and in time, and
fetch-error envelopes that describe the attempt that failed."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import fetcher as fetcher_mod
from webfetch import service
from webfetch.api import create_app
from webfetch.classify import classify
from webfetch.config import Settings
from webfetch.fetcher import HttpFetcher
from webfetch.gates import check_response
from webfetch.netguard import InvalidUrl, normalize_url
from webfetch.service import FetchOptions, run_fetch

CHROMIUM = os.environ.get("WEBFETCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
try:
    import patchright  # noqa: F401
    HAVE_BROWSER = os.path.exists(CHROMIUM)
except ImportError:  # CI installs only the HTTP-tier requirements
    HAVE_BROWSER = False
needs_browser = pytest.mark.skipif(not HAVE_BROWSER, reason="no local Chromium")

RENDERED = "<html><head><title>T</title></head><body><p>" + "Rendered text. " * 40 + "</p></body></html>"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)


def _client(browser=None) -> TestClient:
    # raise_server_exceptions=False: see the 500 a real client gets instead of the exception.
    return TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000), browser=browser),
                      raise_server_exceptions=False)


def run(coro):
    return asyncio.run(coro)


async def _pipeline(settings, opts, browser=None):
    f = HttpFetcher(settings)
    try:
        return await run_fetch(opts, f, browser)
    finally:
        await f.aclose()


class FakeBrowser:
    """Serves a fixed page; ``out`` overrides parts of what the browser tier reports."""

    def __init__(self, **out):
        self.calls: list[str] = []
        self.out = out

    async def render(self, url, *, cookies, headers, timeout, max_bytes=0):
        self.calls.append(url)
        return {"finalUrl": url, "status": 200, "headers": {"content-type": "text/html"}, "html": RENDERED,
                "ms": 12.0, "strategy": "browser:fake", **self.out}


def _via(port: int, location: str, host: str = "echo.test") -> str:
    """A URL on the local server that answers 302 with exactly this Location."""
    return f"http://{host}:{port}/redirect-to?url={quote(location, safe='')}"


# --- INT-1: a destination URL that does not parse is that destination's fetch error -------

# urljoin and urlsplit raise ValueError on each of these.
UNPARSABLE = ["http://[object Object]/", "http://[::1/x", "//[bad", "http://[1.2.3.4]/", "http://a]b/"]


@pytest.mark.parametrize("resolve_gates", [True, False])
@pytest.mark.parametrize("location", UNPARSABLE)
def test_unparsable_redirect_location_is_an_invalid_redirect(server, location, resolve_gates):
    port = server.server_address[1]
    url = _via(port, location)
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": url, "resolveGates": resolve_gates})
    assert r.status_code == 502, r.text[:300]
    body = r.json()
    assert body["outcome"] == "fetch_error" and body["error"] == "invalid_redirect"
    assert repr(location) in body["reason"]
    # how far it got: the hop that sent the bad Location
    assert [(h["url"], h["status"], h["location"]) for h in body["redirects"]] == [(url, 302, location)]
    assert body["finalUrl"] == url == r.headers["x-final-url"]


@pytest.mark.parametrize("target", ["//[x", "http://[object Object]/", "http://ex&#x2100;mple.com/"])
def test_unparsable_meta_refresh_target_is_an_invalid_redirect(server, target):
    port = server.server_address[1]
    url = f"http://echo.test:{port}/r2s/refresh?url={quote(target, safe='')}"
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": url})
    assert r.status_code == 502, r.text[:300]
    body = r.json()
    assert body["error"] == "invalid_redirect" and body["redirects"][-1]["status"] == 200
    assert body["finalUrl"] == url


def test_browser_fallback_follows_an_unparsable_redirect_like_any_fetch_error(server):
    port = server.server_address[1]
    browser = FakeBrowser()
    with _client(browser) as c:
        loc = c.post("/v1/fetch", json={"url": _via(port, "http://[object Object]/")})
        refresh = c.post("/v1/fetch", json={"url": f"http://echo.test:{port}/r2s/refresh?url=%2F%2F%5Bx"})
    assert len(browser.calls) == 2
    for r in (loc, refresh):
        assert r.status_code == 200 and r.headers["x-strategy"] == "browser:fake"
        assert [a.get("error") or a.get("outcome") for a in r.json()["attempts"]] == ["invalid_redirect", "ok"]


def test_gate_recipes_survive_unparsable_urls_in_the_gate_redirect(server):
    port = server.server_address[1]
    bad = quote("http://[bad", safe="")
    # The URL each of these recipes would continue at does not parse.
    cases = {
        "dpg": _via(port, f"https://myprivacy.dpgmedia.nl/consent?callbackUrl={bad}", "www.nu.nl"),
        "zeit": _via(port, f"http://www.zeit.de:{port}/zustimmung?url={bad}", "www.zeit.de"),
        "golem": f"http://www.golem.de:{port}/sonstiges/zustimmung/auswahl.html?from={bad}",
    }
    with _client() as c:
        for name, url in cases.items():
            r = c.post("/v1/fetch", json={"url": url, "render": "never"})
            assert r.status_code == 502 and r.json().get("error") == "invalid_redirect", (name, r.text[:300])
        # Google's recipe reads the host of ?continue=: unparsable, so the redirect is simply followed
        r = c.post("/v1/fetch", json={"url": _via(port, f"http://consent.google.com:{port}/?continue={bad}"),
                                      "render": "never"})
        assert r.status_code != 500 and r.json()["finalUrl"].startswith(f"http://consent.google.com:{port}/")


@pytest.mark.parametrize("location", UNPARSABLE)
def test_no_recipe_raises_on_an_unparsable_location(location):
    assert check_response(original_url="http://www.nu.nl/", url="http://www.nu.nl/", status=302,
                          location=location, head="") is None


@pytest.mark.parametrize("url", ["http://[::1", "http://[object Object]/", "https://[1.2.3.4]/x", "//[bad"])
def test_unparsable_caller_url_is_a_400(url):
    with pytest.raises(InvalidUrl):
        normalize_url(url)
    with _client() as c:
        post = c.post("/v1/fetch", json={"url": url})
        get = c.get("/v1/fetch", params={"url": url})
    assert post.status_code == get.status_code == 400
    assert post.json()["error"] == get.json()["error"] == "invalid_url"


# urlsplit accepts these hosts; libcurl refuses them (a bare CurlError) or, for %XX, decodes
# them and looks up a name other than the one that was resolved and checked.
@pytest.mark.parametrize("host", ["a&b.test", "a%41b.test", "%65cho.test", "a~b.test", "a\\b.test", "a{b}.test",
                                  "a'b.test", "a+b.test", "[::1%25eth0]", "[::1%25$$]"])
def test_host_libcurl_would_refuse_or_rewrite_is_an_invalid_url(server, host):
    port, before = server.server_address[1], len(server.seen)
    with pytest.raises(InvalidUrl):
        normalize_url(f"http://{host}/")
    with _client() as c:
        direct = c.post("/v1/fetch", json={"url": f"http://{host}:{port}/product"})
        redirected = c.post("/v1/fetch", json={"url": _via(port, f"http://{host}:{port}/product")})
    assert direct.status_code == 400 and direct.json()["error"] == "invalid_url", direct.text[:300]
    assert redirected.status_code == 502 and redirected.json()["error"] == "invalid_redirect", redirected.text[:300]
    # nothing was requested from such a host: only the redirecting hop reached the server
    assert [seen["path"].split("?")[0] for seen in server.seen[before:]] == ["/redirect-to"]


@pytest.mark.parametrize("host", ["a_b.test", "xn--bcher-kva.test", "a-b.test.", "Bücher.test", "0x7f.1", "[::1]"])
def test_ordinary_host_names_still_pass(host):
    assert normalize_url(f"http://{host}/").url.startswith("http://")


def test_a_url_libcurl_refuses_is_a_fetch_error(settings, monkeypatch):
    # Should a URL get past normalize_url and still be refused by libcurl, it is the
    # destination's error (curl_cffi raises its bare CurlError for it), not a 500.
    from curl_cffi import CurlError
    from curl_cffi.requests import AsyncSession

    async def refuse(self, url, **kw):
        raise CurlError("Failed to getinfo 1048577, curl: (27) URL rejected: Bad hostname.", 27)

    monkeypatch.setattr(AsyncSession, "get", refuse)
    env = run(_pipeline(settings, FetchOptions(url="http://echo.test:9/x", render="never")))
    assert env["outcome"] == "fetch_error" and env["error"] == "fetch_error" and "URL rejected" in env["reason"]


def test_batch_item_with_an_unparsable_redirect_is_a_502_item(server):
    port = server.server_address[1]
    with _client() as c:
        r = c.post("/v1/batch", json={"requests": [{"url": _via(port, "http://[object Object]/")},
                                                   {"url": f"http://echo.test:{port}/product"}]})
    assert [(x["httpStatus"], x.get("error") or x["outcome"]) for x in r.json()["results"]] == [
        (502, "invalid_redirect"), (200, "ok")]


def test_browser_final_url_that_does_not_parse_is_still_classified(settings):
    # Chromium, not urlsplit, decides what the browser tier calls a URL.
    env = run(_pipeline(settings, FetchOptions(url="http://echo.test:9/start", render="always"),
                        FakeBrowser(finalUrl="http://[not an ip]/end")))
    assert env["ok"] is True and env["finalUrl"] == "http://[not an ip]/end"
    v = classify(status=200, headers={}, original_url="http://a.test/", final_url="http://[bad", body=b"x" * 900,
                 text="x" * 900, visible_chars=900, title="t", content_type="text/html")
    assert v.outcome == "ok"


# --- PERF-5: analysis is bounded in threads and by the request's timeout -------------------

class Blocker:
    """Page work that parks its thread until released, counting how many run at once."""

    def __init__(self):
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.running = self.peak = 0
        self.started: list = []
        self.finished: list = []

    def __call__(self, tag, then=None):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append(tag)
        self.gate.wait(20)  # released by the test (20 s only bounds a failing run)
        with self.lock:
            self.running -= 1
            self.finished.append(tag)
        return then() if then else tag


@pytest.fixture()
def blocker():
    b = Blocker()
    yield b
    b.gate.set()  # never leave a parked analysis thread behind


async def _until(cond, seconds=10.0):
    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, "condition not reached"
        await asyncio.sleep(0.01)


def test_an_analysis_slot_stays_taken_until_its_thread_finishes(blocker):
    n = service.MAX_CONCURRENT_ANALYSIS

    async def main():
        first = [asyncio.create_task(service._bounded(blocker, i)) for i in range(n)]
        await _until(lambda: len(blocker.started) == n)
        queued = [asyncio.create_task(service._bounded(blocker, 100 + i)) for i in range(3)]
        await asyncio.sleep(0.05)
        for task in first:  # the requests go away (client disconnect, platform timeout); their threads do not
            task.cancel()
        assert all(isinstance(r, asyncio.CancelledError) for r in await asyncio.gather(*first, return_exceptions=True))
        await asyncio.sleep(0.3)  # time for the queued work to start, were the slots free
        assert blocker.started == list(range(n)) and blocker.running == n
        queued[0].cancel()  # a waiter that gives up takes no slot with it
        blocker.gate.set()
        assert await asyncio.gather(*queued[1:]) == [101, 102]
        # Two freed threads are enough for the queued work; the other parked threads end when they are scheduled.
        await _until(lambda: len(blocker.finished) == n + 2)
        assert blocker.peak == n and sorted(blocker.finished) == [*range(n), 101, 102]
        # every slot is free again
        assert await asyncio.gather(*(service._bounded(lambda i=i: i) for i in range(2 * n))) == list(range(2 * n))

    run(main())


def test_a_timed_out_analysis_keeps_its_slot_and_a_waiter_times_out_without_starting(blocker):
    n = service.MAX_CONCURRENT_ANALYSIS

    async def main():
        first = [asyncio.create_task(service._bounded(blocker, i, timeout=0.05)) for i in range(n)]
        results = await asyncio.gather(*first, return_exceptions=True)
        assert all(isinstance(r, service.ProcessingTimeout) for r in results), results
        assert blocker.running == n  # the threads run on
        for tag in ("waiter", "late"):
            with pytest.raises(service.ProcessingTimeout):  # no thread is free: it waits, then gives up
                await service._bounded(blocker, tag, timeout=0.05 if tag == "waiter" else 0)
        assert blocker.started == list(range(n)) and blocker.peak == n
        blocker.gate.set()
        await _until(lambda: len(blocker.finished) == n)
        assert await service._bounded(lambda: "free again", timeout=5) == "free again"
        assert blocker.started == list(range(n))  # the waiters' work never ran

    run(main())
    assert sum(t.name.startswith("webfetch-analysis") for t in threading.enumerate()) <= n


def test_work_that_raises_frees_its_slot_and_its_own_timeout_error_is_not_ours():
    def boom(exc):
        raise exc

    async def main():
        for _ in range(3 * service.MAX_CONCURRENT_ANALYSIS):
            with pytest.raises(service.TooComplex):
                await service._bounded(boom, service.TooComplex("deep"), timeout=5)
        with pytest.raises(TimeoutError) as err:
            await service._bounded(boom, TimeoutError("from the work"), timeout=5)
        assert not isinstance(err.value, service.ProcessingTimeout) and str(err.value) == "from the work"
        assert await service._bounded(lambda: 1, timeout=5) == 1

    run(main())


def _blocked(monkeypatch, blocker, name):
    """Make service.<name> park until the blocker is released, then do its real work."""
    real = getattr(service, name)
    monkeypatch.setattr(service, name, lambda *a: blocker(name, lambda: real(*a)))


def test_analysis_that_outlives_the_timeout_is_a_504_naming_the_attempt(server, monkeypatch, blocker):
    port = server.server_address[1]
    page = f"http://echo.test:{port}/product"
    monkeypatch.setattr(service, "MIN_PROCESSING_SECONDS", 0.3, raising=False)
    _blocked(monkeypatch, blocker, "_analyze")
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": _via(port, page), "timeout": 1})
        answered_while_analysing = blocker.started == ["_analyze"] and not blocker.finished
        blocker.gate.set()
    assert r.status_code == 504, r.text[:300]
    assert answered_while_analysing  # the answer did not wait for the analysis
    body = r.json()
    assert body["outcome"] == "fetch_error" and body["error"] == "timeout" and "1s timeout" in body["reason"]
    assert body["strategy"] == "http:chrome" == r.headers["x-strategy"]
    assert body["finalUrl"] == page == r.headers["x-final-url"]
    assert [(h["url"], h["status"]) for h in body["redirects"]] == [(_via(port, page), 302)]
    assert [(a["strategy"], a["error"]) for a in body["attempts"]] == [("http:chrome", "timeout")]
    assert "statusCode" not in body


def test_output_rendering_that_outlives_the_timeout_is_a_504_naming_the_page(server, monkeypatch, blocker):
    port = server.server_address[1]
    page = f"http://echo.test:{port}/product"
    monkeypatch.setattr(service, "MIN_PROCESSING_SECONDS", 0.3, raising=False)
    _blocked(monkeypatch, blocker, "_render_outputs")
    with _client() as c:
        r = c.post("/v1/fetch", json={"url": _via(port, page), "timeout": 1})
        answered_while_rendering = blocker.started == ["_render_outputs"] and not blocker.finished
        blocker.gate.set()
    assert r.status_code == 504 and answered_while_rendering, r.text[:300]
    body = r.json()
    assert body["error"] == "timeout" and body["strategy"] == "http:chrome" and body["finalUrl"] == page
    assert [(h["url"], h["status"]) for h in body["redirects"]] == [(_via(port, page), 302)]
    assert [(a["strategy"], a.get("outcome")) for a in body["attempts"]] == [("http:chrome", "ok")]


def test_a_request_waiting_for_an_analysis_thread_respects_its_timeout(server, settings, monkeypatch, blocker):
    port = server.server_address[1]
    n = service.MAX_CONCURRENT_ANALYSIS
    analysed = []
    real = service._analyze
    monkeypatch.setattr(service, "_analyze", lambda *a: analysed.append(a) or real(*a))
    monkeypatch.setattr(service, "MIN_PROCESSING_SECONDS", 0.3, raising=False)
    opts = dict(url=f"http://echo.test:{port}/product", timeout=1)

    async def main():
        busy = [asyncio.create_task(service._bounded(blocker, i)) for i in range(n)]
        await _until(lambda: len(blocker.started) == n)
        env = await _pipeline(settings, FetchOptions(**opts))
        still_busy = blocker.running
        blocker.gate.set()
        await asyncio.gather(*busy)
        return env, still_busy, await _pipeline(settings, FetchOptions(**opts))

    env, still_busy, after = run(main())
    assert env["outcome"] == "fetch_error" and env["error"] == "timeout" and still_busy == n
    assert env["finalUrl"] == opts["url"] and env["strategy"] == "http:chrome"
    assert len(analysed) == 1 and after["ok"] is True  # only the request made after the release was analysed


def test_processing_gets_the_rest_of_the_timeout_and_never_less_than_the_floor(server, settings, monkeypatch):
    port = server.server_address[1]
    given: list[float] = []
    real = service._bounded

    async def recording(fn, *args, timeout=None):
        given.append(timeout)
        return await real(fn, *args, timeout=timeout)

    monkeypatch.setattr(service, "_bounded", recording)
    floor = service.MIN_PROCESSING_SECONDS
    opts = dict(url=f"http://echo.test:{port}/product", render="never")
    assert run(_pipeline(settings, FetchOptions(**opts, timeout=60)))["ok"] is True
    assert len(given) == 2 and all(50 < t <= 60 for t in given), given  # analysis, then rendering
    given.clear()
    assert run(_pipeline(settings, FetchOptions(**opts, timeout=0.5)))["ok"] is True
    # a timeout shorter than the floor: both stages share the floor, less what the first one used
    assert floor - 1 < given[1] <= given[0] <= floor, given


def test_a_page_is_not_failed_because_the_fetch_used_up_a_short_timeout(settings):
    class SlowBrowser(FakeBrowser):
        async def render(self, url, **kw):
            await asyncio.sleep(0.6)  # past the request's 0.5 s; the browser attempt may take up to 1 s
            return await super().render(url, **kw)

    env = run(_pipeline(settings, FetchOptions(url="http://echo.test:9/start", render="always", timeout=0.5),
                        SlowBrowser()))
    assert env["ok"] is True and env["strategy"] == "browser:fake" and "Rendered text." in env["markdown"]


# --- INT-5, SEM-7: a fetch_error envelope describes the attempt and the hop that failed ----

DEEP = "<html><body>" + "<div>" * 3000 + "x" + "</div>" * 3000 + "<p>" + "pad " * 30000 + "</p></body></html>"


class BrokenBrowser:
    async def render(self, url, **kw):
        raise RuntimeError("browser tier is down")


def _hops(body: dict) -> list[tuple]:
    return [(h["url"], h["status"]) for h in body["redirects"]]


def test_fetch_error_after_a_redirect_names_the_url_that_failed(server, monkeypatch):
    port = server.server_address[1]
    base = f"http://echo.test:{port}"

    async def resolve(host, port, *, allow_private=False):
        if host == "gone.test":
            raise InvalidUrl("could not resolve gone.test")
        return await fakedns.resolve(host, port, allow_private=allow_private)

    monkeypatch.setattr(fetcher_mod, "resolve_public", resolve)
    cases = [  # failing URL, request options, status, error
        (f"{base}/slow", {"timeout": 1}, 504, "timeout"),
        (f"{base}/r2s/big", {"maxBytes": 50_000}, 502, "too_large"),
        (f"{base}/deep", {}, 502, "too_complex"),
        ("http://127.0.0.1:1/x", {}, 502, "connect_error"),
        (f"http://gone.test:{port}/x", {}, 502, "dns_error"),
    ]
    with _client() as c:
        for failing, extra, status, error in cases:
            url = _via(port, failing)
            r = c.post("/v1/fetch", json={"url": url, "render": "never", **extra})
            body = r.json()
            assert (r.status_code, body["outcome"], body["error"]) == (status, "fetch_error", error), r.text[:300]
            assert body["finalUrl"] == failing == r.headers["x-final-url"], error
            assert _hops(body) == [(url, 302)], error  # the hop that answered, not the one that failed
            assert body["strategy"] == "http:chrome" == r.headers["x-strategy"] and body["url"] == url
            # the same failure without the redirect in front
            direct = c.post("/v1/fetch", json={"url": failing, "render": "never", **extra}).json()
            assert (direct["error"], direct["finalUrl"], direct["redirects"]) == (error, failing, []), error


def test_fetch_error_strategy_is_the_attempt_whose_error_is_reported(server):
    port = server.server_address[1]
    url = _via(port, "http://127.0.0.1:1/x")
    with _client(BrokenBrowser()) as c:  # render auto: the HTTP error is the one reported
        r = c.post("/v1/fetch", json={"url": url})
        only_browser = c.post("/v1/fetch", json={"url": url, "render": "always"})
    body = r.json()
    assert [(a["strategy"], a["error"]) for a in body["attempts"]] == [("http:chrome", "connect_error"),
                                                                       ("browser:chromium", "browser_error")]
    assert body["error"] == "connect_error" and body["strategy"] == "http:chrome" == r.headers["x-strategy"]
    assert body["finalUrl"] == "http://127.0.0.1:1/x" == r.headers["x-final-url"] and _hops(body) == [(url, 302)]
    # a browser attempt the tier could not make has nothing but itself to report
    body = only_browser.json()
    assert body["error"] == "fetch_error" and body["strategy"] == "browser:chromium" and body["finalUrl"] == url
    assert body["redirects"] == []


def test_page_too_complex_in_the_browser_tier_is_reported_as_that_attempt(server):
    port = server.server_address[1]
    url = _via(port, "http://127.0.0.1:1/x")
    landed = f"http://echo.test:{port}/landed"
    hops = [{"url": url, "status": 302, "location": "/landed", "ms": 2.0}]
    with _client(FakeBrowser(html=DEEP, finalUrl=landed, redirects=hops)) as c:
        always = c.post("/v1/fetch", json={"url": url, "render": "always"})
        auto = c.post("/v1/fetch", json={"url": url})  # the HTTP attempt fails first; the later error is reported
    for r, strategies in ((always, ["browser:fake"]), (auto, ["http:chrome", "browser:fake"])):
        body = r.json()
        assert r.status_code == 502 and body["error"] == "too_complex", r.text[:300]
        assert [a["strategy"] for a in body["attempts"]] == strategies
        assert body["strategy"] == "browser:fake" == r.headers["x-strategy"]
        assert body["finalUrl"] == landed == r.headers["x-final-url"] and _hops(body) == [(url, 302)]


# --- SEM-5: the browser tier reports the hops of every navigation, not only the last ------

@needs_browser
def test_browser_reports_the_hops_of_every_navigation(server, settings):
    from webfetch.browser import BrowserPool

    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    challenge = f"{base}/redirect-to?url={quote(base + '/js-challenge', safe='')}"  # 302, then a 403 that reloads
    scripted = f"{base}/r2s/js-redirect?url=%2Ftwo-hops"  # a script navigates into two redirects
    refresh = f"{base}/r2s/refresh?url={quote('/redirect-to?url=/product', safe='')}"  # meta refresh into a redirect

    async def main():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        try:
            outs = [await pool.render(u, cookies=[], headers=None, timeout=20) for u in (challenge, scripted, refresh)]
            return outs, await _pipeline(settings, FetchOptions(url=challenge, render="always"), pool)
        finally:
            await pool.stop()

    def chain(out):
        return [(h["url"], h["status"], h["location"]) for h in out["redirects"]]

    (challenged, by_script, by_refresh), env = run(main())
    assert (challenged["finalUrl"], challenged["status"]) == (f"{base}/js-challenge", 200)
    assert chain(challenged) == [(challenge, 302, f"{base}/js-challenge"), (f"{base}/js-challenge", 403, None)]
    assert by_script["finalUrl"] == f"{base}/echo-headers"
    assert chain(by_script) == [(scripted, 200, None), (f"{base}/two-hops", 302, "/two-hops/next"),
                                (f"{base}/two-hops/next", 301, "/echo-headers")]
    assert by_refresh["finalUrl"] == f"{base}/product"
    assert chain(by_refresh) == [(refresh, 200, None), (f"{base}/redirect-to?url=/product", 302, "/product")]
    # and through the pipeline: every hop but the page itself is in the envelope
    assert env["ok"] is True and env["finalUrl"] == f"{base}/js-challenge" and env["statusCode"] == 200
    assert _hops(env) == [(challenge, 302), (f"{base}/js-challenge", 403)]


def test_browser_hop_report_is_capped_to_the_newest_hops():
    from types import SimpleNamespace

    from webfetch import browser

    def response(i, status):
        return SimpleNamespace(url=f"http://a.test/{i}", status=status, headers={"location": f"/{i + 1}"},
                               request=SimpleNamespace(timing={"responseEnd": 1.26}))

    cap = browser.MAX_REPORTED_REDIRECTS
    hops = browser._hops([response(i, 302 if i % 2 else 200) for i in range(cap + 10)])
    assert [h["url"] for h in hops] == [f"http://a.test/{i}" for i in range(9, cap + 9)]  # the page itself is not a hop
    assert hops[0] == {"url": "http://a.test/9", "status": 302, "location": "/10", "ms": 1.3}
    assert hops[1]["location"] is None  # a document that navigated on redirected nowhere by header
    assert browser._hops([]) == [] == browser._hops([response(0, 200)])
    assert browser._hops([object(), response(1, 200)]) == []  # reporting hops never costs the page


# --- A meta refresh is followed in a document, and is content anywhere else ----------------

@pytest.mark.parametrize("ctype, followed", [
    ("text/html", True), ("text/html; charset=utf-8", True), ("application/xhtml+xml", True),
    ("text/plain", False), ("text/plain; charset=utf-8", False), ("application/json", False),
    ("application/xml", False), ("text/markdown", False),
])
def test_meta_refresh_is_followed_only_in_html(server, settings, ctype, followed):
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/r2s/refresh?url=%2Fproduct&ctype={quote(ctype, safe='')}"
    env = run(_pipeline(settings, FetchOptions(url=url, render="never", format="json")))
    assert env["ok"] is True
    if followed:
        assert env["finalUrl"] == f"http://127.0.0.1:{port}/product" and _hops(env) == [(url, 200)]
    else:  # the resource that was asked for, with the tag as part of its text
        assert env["finalUrl"] == url and _hops(env) == []
        assert env["contentType"].startswith(ctype.split(";")[0])

