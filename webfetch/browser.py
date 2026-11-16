"""Headless-browser tier, used when the HTTP tier meets a JavaScript challenge or an
empty JavaScript shell. Runs in its own Modal container (see ``modal_browser.py``).

Uses patchright (Playwright with the CDP leaks that bot detectors look for patched out)
when installed, plain Playwright otherwise, with full Chromium in new-headless mode.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from .netguard import host_is_blocked, ip_is_public

# DOM markers of interstitials that finish by reloading the page. DOM-only on purpose:
# patchright evaluates in an isolated world where page globals are not visible.
CHALLENGE_DOM = """() => !!document.querySelector(
  'script[src*="centinelanalytica.com"], #challenge-form, #challenge-running, #challenge-stage,'
  + ' #sec-if-cpt-container, #sec-cpt-if, iframe[src*="captcha-delivery.com"], #px-captcha')"""
QUIET_SECONDS = 2.5
CHALLENGE_TITLES = ("just a moment", "attention required", "please wait", "checking your browser",
                    "one moment", "verifying you are human", "pardon our interruption")
BLOCKED_RESOURCES = {"image", "media", "font"}


async def _host_is_public(host: str, cache: dict[str, bool]) -> bool:
    if host in cache:
        return cache[host]
    ok = False
    if host and not host_is_blocked(host):
        try:
            ok = ip_is_public(str(ipaddress.ip_address(host.strip("[]"))))
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                ok = bool(infos) and all(ip_is_public(i[4][0]) for i in infos)
            except OSError:
                ok = True  # let the browser report the DNS failure itself
    cache[host] = ok
    return ok


class BrowserPool:
    def __init__(self, *, allow_private: bool = False, executable_path: str | None = None) -> None:
        self._pw = None
        self._browser = None
        self.engine = "playwright"
        self._lock = asyncio.Lock()
        self.allow_private = allow_private  # tests only
        self.executable_path = executable_path

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return
            try:
                from patchright.async_api import async_playwright
                self.engine = "patchright"
            except ImportError:  # pragma: no cover
                from playwright.async_api import async_playwright
                self.engine = "playwright"
            if self._pw is None:
                self._pw = await async_playwright().start()
            launch_kw = {"executable_path": self.executable_path} if self.executable_path else {"channel": "chromium"}
            self._browser = await self._pw.chromium.launch(
                **launch_kw,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                      "--lang=en-US"],
            )

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = self._pw = None

    async def render(self, url: str, *, cookies: list[dict], headers: dict | None, timeout: float) -> dict:
        t0 = time.monotonic()
        deadline = t0 + timeout
        await self.start()
        ctx = await self._browser.new_context(
            locale="en-US", timezone_id="America/New_York", viewport={"width": 1366, "height": 900},
            extra_http_headers=headers or None,
        )
        dns_cache: dict[str, bool] = {}
        try:
            if cookies:
                await ctx.add_cookies([{"name": c["name"], "value": c["value"], "domain": c["domain"],
                                        "path": c.get("path", "/")} for c in cookies])
            page = await ctx.new_page()

            async def guard(route):
                req = route.request
                if req.resource_type in BLOCKED_RESOURCES:
                    return await route.abort()
                if self.allow_private:
                    return await route.continue_()
                host = urlsplit(req.url).hostname or ""
                if req.is_navigation_request():
                    if not await _host_is_public(host, dns_cache):
                        return await route.abort("blockedbyclient")
                else:
                    try:
                        if not ip_is_public(str(ipaddress.ip_address(host.strip("[]")))):
                            return await route.abort("blockedbyclient")
                    except ValueError:
                        if host_is_blocked(host):
                            return await route.abort("blockedbyclient")
                await route.continue_()

            await page.route("**/*", guard)
            nav = {"t": time.monotonic(), "resp": None}

            def on_nav(frame):
                if frame == page.main_frame:
                    nav["t"] = time.monotonic()

            def on_resp(r):
                try:
                    if r.request.is_navigation_request() and r.frame == page.main_frame:
                        nav["resp"] = r
                except Exception:
                    pass

            page.on("framenavigated", on_nav)
            page.on("response", on_resp)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=max(1.0, timeout - 2) * 1000)
            try:
                left = (deadline - time.monotonic()) * 1000
                await page.wait_for_load_state("networkidle", timeout=max(500, min(6000, left - 2000)))
            except Exception:
                pass
            # Interstitial challenges (Cloudflare, Akamai, Centinel, ...) run their script and
            # then reload the page: wait until the main frame has been quiet and no challenge
            # marker is left, or the deadline is near.
            while time.monotonic() < deadline - 1.5:
                try:
                    title = (await page.title()).lower()
                    challenged = any(t in title for t in CHALLENGE_TITLES) or await page.evaluate(CHALLENGE_DOM)
                except Exception:  # page navigating mid-check
                    challenged = True
                if not challenged and time.monotonic() - nav["t"] >= QUIET_SECONDS:
                    break
                if not challenged and nav["resp"] is not None and nav["resp"].status < 400:
                    break
                await page.wait_for_timeout(500)
            resp = nav["resp"] or resp
            html = await page.content()
            status = resp.status if resp is not None else 0
            hdrs = await resp.all_headers() if resp is not None else {}
            return {
                "finalUrl": page.url,
                "status": status,
                "headers": hdrs,
                "html": html,
                "ms": round((time.monotonic() - t0) * 1000, 1),
                "strategy": f"browser:{self.engine}",
            }
        except Exception as exc:
            return {"error": "browser_error", "message": f"{type(exc).__name__}: {exc}"[:500],
                    "ms": round((time.monotonic() - t0) * 1000, 1)}
        finally:
            await ctx.close()
