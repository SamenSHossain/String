"""Headless-browser tier, run against the local server. Skipped where no Chromium is
installed (CI installs only the HTTP-tier requirements)."""

from __future__ import annotations

import asyncio
import os

import pytest

CHROMIUM = os.environ.get("WEBFETCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
pytest.importorskip("patchright")
if not os.path.exists(CHROMIUM):
    pytest.skip("no local Chromium", allow_module_level=True)

from webfetch.browser import BrowserPool  # noqa: E402


def _render(path_url: str, **kw) -> dict:
    async def go():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        try:
            return await pool.render(path_url, cookies=[], headers=None, timeout=20, **kw)
        finally:
            await pool.stop()

    return asyncio.run(go())


def test_challenge_then_reload(server):
    port = server.server_address[1]
    out = _render(f"http://127.0.0.1:{port}/js-challenge")
    assert out.get("error") is None, out
    assert out["status"] == 200
    assert "Real content" in out["html"]


def test_client_rendered_page(server):
    port = server.server_address[1]
    out = _render(f"http://127.0.0.1:{port}/spa")
    assert "Rendered by JavaScript" in out["html"]
