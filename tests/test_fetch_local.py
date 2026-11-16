"""End-to-end fetch pipeline against the local gate-emulating server."""

from __future__ import annotations

import asyncio

import pytest

from tests import fakedns
from webfetch import fetcher as fetcher_mod
from webfetch import gates
from webfetch.config import Settings
from webfetch.fetcher import FetchError, HttpFetcher
from webfetch.netguard import BlockedDestination
from webfetch.service import Expect, FetchOptions, run_fetch


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)


def run(coro):
    return asyncio.run(coro)


async def _fetch(settings, url, **kw):
    f = HttpFetcher(settings)
    try:
        return await f.fetch(url, **kw)
    finally:
        await f.aclose()


async def _pipeline(settings, opts):
    f = HttpFetcher(settings)
    try:
        return await run_fetch(opts, f, None)
    finally:
        await f.aclose()


def test_steam_age_gate_preset_cookies(server, settings):
    port = server.server_address[1]
    res = run(_fetch(settings, f"http://store.steampowered.com:{port}/app/271590/Grand_Theft_Auto_V/"))
    assert res.status == 200
    assert b"appHubAppName" in res.body
    assert any(g.site == "steam" and g.trigger == "preset" for g in res.gates)
    assert len(res.hops) == 1  # no detour through /agecheck


def test_steam_age_gate_redirect_recovery(server, settings, monkeypatch):
    """If the preset cookies are missing, the /agecheck redirect is handled."""
    port = server.server_address[1]
    monkeypatch.setattr(gates.SteamAgeGate, "preset", lambda self, host: {})
    res = run(_fetch(settings, f"http://store.steampowered.com:{port}/app/271590/"))
    assert res.status == 200 and b"appHubAppName" in res.body
    assert any(g.site == "steam" and g.trigger == "redirect" for g in res.gates)


def test_steam_without_gate_resolution_is_flagged(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://store.steampowered.com:{port}/app/271590/",
                                               resolve_gates=False)))
    assert env["ok"] is False
    assert env["outcome"] == "age_gate"
    assert "/agecheck/" in env["finalUrl"]


def test_dpg_privacy_gate_callback(server, settings):
    port = server.server_address[1]
    res = run(_fetch(settings, f"http://www.nu.nl:{port}/"))
    assert res.status == 200
    assert b"Het laatste nieuws" in res.body
    events = [g for g in res.gates if g.trigger == "redirect"]
    assert events and events[0].site == "dpg-media"
    accept = [h for h in res.hops if "/privacy-gate/accept" in h.url]
    assert accept and "authId=" in accept[0].url


def test_zeit_consent_redirect(server, settings, monkeypatch):
    port = server.server_address[1]
    monkeypatch.setattr(gates.ZeitConsent, "preset", lambda self, host: {})
    res = run(_fetch(settings, f"http://www.zeit.de:{port}/index"))
    assert res.status == 200 and b"ZEIT ONLINE" in res.body
    assert any(g.site == "zeit-online" and g.trigger == "redirect" for g in res.gates)


def test_cookie_jars_are_isolated(server, settings):
    port = server.server_address[1]

    async def go():
        f = HttpFetcher(settings)
        try:
            await f.fetch(f"http://echo.test:{port}/set")
            second = await f.fetch(f"http://echo.test:{port}/cookies")
            return second
        finally:
            await f.aclose()

    second = run(go())
    assert b"sess=secret123" not in second.body


def test_shift_jis_page_decodes(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/sjis", include={"markdown", "metadata"})))
    assert env["ok"]
    assert env["encoding"] == "cp932"
    assert "日本語のページです" in env["markdown"]
    assert env["metadata"]["title"] == "テスト"


def test_challenge_is_not_success(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/challenge")))
    assert env["ok"] is False
    assert env["outcome"] == "challenge" and env["vendor"] == "cloudflare"
    # a second TLS fingerprint was tried before giving up
    assert [a["strategy"] for a in env["attempts"]] == ["http:chrome", "http:safari"]


def test_empty_body_is_not_success(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/empty")))
    assert env["ok"] is False and env["outcome"] == "empty"


def test_redirect_loop_errors(server, settings):
    port = server.server_address[1]
    with pytest.raises(FetchError) as exc:
        run(_fetch(settings, f"http://echo.test:{port}/loop"))
    assert exc.value.code == "too_many_redirects"


def test_redirect_to_private_address_blocked(server):
    port = server.server_address[1]
    strict = Settings(allow_private=False, proxy=None)
    with pytest.raises(BlockedDestination):
        run(_fetch(strict, f"http://echo.test:{port}/to-private"))


def test_expect_checks(server, settings):
    port = server.server_address[1]
    ok = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/product",
                                              expect=Expect(contains=["Forged steel"], selector="h1", min_chars=100))))
    assert ok["ok"] and ok["validation"]["passed"]
    bad = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/product",
                                               expect=Expect(contains=["Add to basket"]))))
    assert bad["ok"] is False and bad["outcome"] == "validation_failed"


def test_json_schema_extraction(server, settings):
    port = server.server_address[1]
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
            "brand": {"type": "string"},
            "inStock": {"type": "boolean"},
            "rating": {"type": "number"},
            "reviewCount": {"type": "integer"},
            "images": {"type": "array", "items": {"type": "string", "format": "uri"}},
            "warranty": {"type": "string"},
        },
        "required": ["name", "price"],
    }
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/product", json_schema=schema)))
    ex = env["extracted"]
    assert ex["data"] == {
        "name": "Acme Anvil 50kg", "price": 1299.0, "currency": "EUR", "brand": "Acme", "inStock": True,
        "rating": 4.6, "reviewCount": 212,
        "images": ["https://cdn.acme.test/anvil-1.jpg", "https://cdn.acme.test/anvil-2.jpg"], "warranty": None,
    }
    assert ex["sources"]["price"] == "json-ld:Product.offers.price"
    assert ex["missing"] == ["warranty"] and ex["missingRequired"] == []


def test_steam_schema_uses_site_extractor(server, settings):
    port = server.server_address[1]
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
        "releaseDate": {"type": "string", "format": "date"}, "developer": {"type": "string"},
        "publisher": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
        "reviewSummary": {"type": "string"}, "reviewCount": {"type": "integer"}}}
    env = run(_pipeline(settings, FetchOptions(url=f"http://store.steampowered.com:{port}/app/271590/", json_schema=schema)))
    d = env["extracted"]["data"]
    assert d["name"] == "Grand Theft Auto V Legacy"
    assert d["price"] == 29.98 and d["currency"] == "USD"
    assert d["releaseDate"] == "2015-04-13"
    assert d["developer"] == "Rockstar North" and d["publisher"] == "Rockstar Games"
    assert d["tags"] == ["Open World", "Action"]
    assert d["reviewSummary"] == "Very Positive" and d["reviewCount"] == 1734567


def test_timing_breakdown_present(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://www.nu.nl:{port}/")))
    t = env["timing"]
    for key in ("dnsMs", "connectMs", "ttfbMs", "fetchMs", "processMs", "totalMs"):
        assert key in t
    assert env["redirects"]  # the privacy-gate detour is visible


def test_noscript_refresh_not_followed(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/noscript-refresh")))
    assert env["finalUrl"].endswith("/noscript-refresh")
    assert env["ok"] is False and env["outcome"] == "js_required"


def test_js_required_page_is_flagged(server, settings):
    port = server.server_address[1]
    env = run(_pipeline(settings, FetchOptions(url=f"http://echo.test:{port}/html/errors/javascript_disabled.html")))
    assert env["ok"] is False and env["outcome"] == "js_required"


def test_golem_accept_endpoint(server, settings, monkeypatch):
    """Without a valid preset, the consent page's own accept call is made, then the page."""
    port = server.server_address[1]
    monkeypatch.setattr(gates.GolemConsent, "preset", lambda self, host: {})
    res = run(_fetch(settings, f"http://www.golem.de:{port}/"))
    assert res.status == 200 and b"IT-News" in res.body
    calls = [h.url for h in res.hops if "setconsentcookie.php" in h.url]
    assert calls and "type=cmp" in calls[0] and "version=%7C261101" in calls[0]
