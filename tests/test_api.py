from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import fetcher as fetcher_mod
from webfetch.api import create_app
from webfetch.config import Settings


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)


@pytest.fixture()
def client():
    app = create_app(Settings(allow_private=True, anon_rate_per_min=1000))
    with TestClient(app) as c:
        yield c


def test_json_envelope(server, client):
    port = server.server_address[1]
    r = client.post("/v1/fetch", json={"url": f"http://store.steampowered.com:{port}/app/271590/"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["outcome"] == "ok" and body["statusCode"] == 200
    assert body["requestId"] == r.headers["x-request-id"]
    assert "Grand Theft Auto V" in body["markdown"]
    assert body["metadata"]["title"].startswith("Grand Theft Auto V")
    assert r.headers["x-status-code"] == "200" and r.headers["x-outcome"] == "ok"
    assert "steam:age_gate:preset" in r.headers["x-gates"]
    assert "total;dur=" in r.headers["server-timing"]


def test_markdown_format_headers(server, client):
    port = server.server_address[1]
    r = client.post("/v1/fetch", json={"url": f"http://www.nu.nl:{port}/", "format": "markdown"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["x-final-url"].endswith("/")
    assert "Het laatste nieuws" in r.text


def test_string_compat_fields(server, client):
    port = server.server_address[1]
    r = client.post("/v1/fetch", json={"url": f"http://www.zeit.de:{port}/index", "format": "markdown",
                                       "mainContentOnly": True, "executeJS": False})
    assert r.status_code == 200 and "Politik" in r.text


def test_failure_is_502_with_reason(server, client):
    port = server.server_address[1]
    r = client.post("/v1/fetch", json={"url": f"http://echo.test:{port}/challenge", "format": "markdown"})
    assert r.status_code == 502
    body = r.json()
    assert body["outcome"] == "challenge" and "markdown" not in body


def test_validation_errors(client):
    r = client.post("/v1/fetch", json={"url": "https://example.com", "bogus": 1})
    assert r.status_code == 400 and r.json()["error"] == "validation_error"
    r = client.post("/v1/fetch", json={"url": "ftp://example.com"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_url"
    r = client.post("/v1/fetch", json={"url": "https://example.com", "headers": {"Host": "evil"}})
    assert r.status_code == 400
    r = client.post("/v1/fetch", json={"url": "https://example.com", "jsonSchema": {"title": "string"}})
    assert r.status_code == 400


def test_ssrf_blocked(monkeypatch):
    app = create_app(Settings(allow_private=False))
    with TestClient(app) as c:
        r = c.post("/v1/fetch", json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 403 and r.json()["error"] == "blocked_destination"


def test_rate_limit():
    app = create_app(Settings(allow_private=True, anon_rate_per_min=2))
    with TestClient(app) as c:
        codes = [c.post("/v1/fetch", json={"url": "ftp://x"}).status_code for _ in range(4)]
    assert codes[:2] == [400, 400] and codes[2] == 429


def test_batch(server, client):
    port = server.server_address[1]
    r = client.post("/v1/batch", json={"requests": [
        {"url": f"http://store.steampowered.com:{port}/app/271590/", "include": ["metadata"]},
        {"url": f"http://echo.test:{port}/empty"},
    ]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["ok"] is True and res[1]["outcome"] == "empty"


def test_batch_costs_one_token_per_item():
    app = create_app(Settings(allow_private=True, anon_rate_per_min=3))
    with TestClient(app) as c:
        r = c.post("/v1/batch", json={"requests": [{"url": "ftp://x"}] * 4})
    assert r.status_code == 429


def test_rate_limit_uses_rightmost_forwarded_for():
    app = create_app(Settings(allow_private=True, anon_rate_per_min=1))
    with TestClient(app) as c:
        a = c.post("/v1/fetch", json={"url": "ftp://x"}, headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9"})
        b = c.post("/v1/fetch", json={"url": "ftp://x"}, headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"})
    assert a.status_code == 400 and b.status_code == 429  # spoofed left entry does not help


def test_raw_utf8_location_does_not_500(server, client):
    port = server.server_address[1]
    r = client.post("/v1/fetch", json={"url": f"http://echo.test:{port}/utf8-redirect", "format": "markdown"})
    assert r.status_code == 200, r.text
    assert r.headers["x-final-url"].endswith("/caf%C3%A9-r%C3%A9sum%C3%A9")
