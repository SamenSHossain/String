"""HTTP tier: Chrome-grade TLS/HTTP2 fingerprint via curl_cffi, redirects followed by
hand so that every hop is SSRF-checked and gate recipes can step in, a cookie jar per
request (never shared between callers), and a per-hop timing breakdown."""

from __future__ import annotations

import asyncio
import html as _html
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

from curl_cffi import AsyncCurl, CurlInfo, CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from .config import Settings
from .gates import GateEvent, check_response, presets_for
from .netguard import BlockedDestination, InvalidUrl, ip_is_public, normalize_url, resolve_public

_REDIRECT = {301, 302, 303, 307, 308}
_META_REFRESH = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']?\s*(\d+)\s*[;,]\s*url\s*=\s*['"]?([^"'>\s]+)""",
    re.I,
)
_NOSCRIPT = re.compile(rb"<noscript\b.*?</noscript\s*>", re.I | re.S)
_HTTP_VERSIONS = {1: "1.0", 2: "1.1", 3: "2", 4: "2", 5: "2", 30: "3", 31: "3"}
_INFOS = [CurlInfo.NAMELOOKUP_TIME, CurlInfo.CONNECT_TIME, CurlInfo.APPCONNECT_TIME,
          CurlInfo.STARTTRANSFER_TIME, CurlInfo.TOTAL_TIME]
MAX_GATE_ACTIONS = 4
MAX_META_REFRESH = 2


class FetchError(Exception):
    def __init__(self, code: str, message: str, *, hops: list | None = None, gates: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hops = hops or []
        self.gates = gates or []


@dataclass
class Hop:
    url: str
    status: int
    location: str | None
    ms: float
    remote_ip: str = ""

    def as_dict(self) -> dict:
        d = {"url": self.url, "status": self.status, "ms": round(self.ms, 1)}
        if self.location:
            d["location"] = self.location
        return d


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    http_version: str
    remote_ip: str
    hops: list[Hop]
    gates: list[GateEvent]
    timing: dict[str, float]
    strategy: str
    extra: dict = field(default_factory=dict)


def _curl_error_code(exc: RequestException) -> str:
    code = int(getattr(exc, "code", 0) or 0)
    if code == 28:
        return "timeout"
    if code == 6:
        return "dns_error"
    if code == 7:
        return "connect_error"
    if code in (35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91, 98):
        return "tls_error"
    if code == 63:
        return "too_large"
    if code in (16, 92):
        return "http2_error"
    if code in (52, 55, 56):
        return "connection_error"
    if code == 47:
        return "too_many_redirects"
    return "fetch_error"


class HttpFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._acurl: AsyncCurl | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _shared_curl(self) -> AsyncCurl:
        """One libcurl multi handle per event loop: connections (and TLS sessions) are
        reused across API requests, while each request keeps its own cookie jar."""
        loop = asyncio.get_running_loop()
        if self._acurl is None or self._loop is not loop:
            ca = self.settings.ca_bundle
            self._acurl = AsyncCurl(cacert=ca, loop=loop) if ca else AsyncCurl(loop=loop)
            self._loop = loop
        return self._acurl

    async def aclose(self) -> None:
        if self._acurl is not None:
            await self._acurl.close()
            self._acurl = None

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
        resolve_gates: bool = True,
        max_bytes: int | None = None,
        impersonate: str | None = None,
        http_version: str | None = None,
    ) -> FetchResult:
        s = self.settings
        target = normalize_url(url)
        timeout = min(timeout or s.default_timeout, s.max_timeout)
        max_bytes = max_bytes or s.max_bytes
        started = time.monotonic()
        deadline = started + timeout

        session = AsyncSession(
            async_curl=self._shared_curl(),
            impersonate=impersonate or s.impersonate,
            curl_infos=_INFOS,
            verify=s.ca_bundle or True,
            max_clients=4,
        )
        hops: list[Hop] = []
        gates: list[GateEvent] = []
        try:
            jar = session.cookies
            for name, value in (cookies or {}).items():
                jar.set(name, value, domain=target.host)
            if resolve_gates:
                for recipe, domain, preset in presets_for(target.host):
                    for name, value in preset.items():
                        if name not in (cookies or {}):
                            jar.set(name, value, domain=domain)
                    what = "confirming their age" if recipe.kind == "age_gate" else "accepting cookies"
                    gates.append(GateEvent(recipe.name, recipe.kind, "preset",
                                           f"sent the cookies a visitor holds after {what}"))

            current = target.url
            redirects = gate_actions = meta_refreshes = 0
            resolve_ms = 0.0
            while True:
                try:
                    hop_target = normalize_url(current)
                except InvalidUrl as exc:
                    raise FetchError("invalid_redirect", f"redirected to an invalid URL: {exc}", hops=hops, gates=gates) from exc
                curl_options: dict = {CurlOpt.MAXFILESIZE_LARGE: max_bytes}
                if s.proxy is None:
                    t_dns = time.monotonic()
                    try:
                        ips = await resolve_public(hop_target.host, hop_target.port, allow_private=s.allow_private)
                    except InvalidUrl as exc:
                        raise FetchError("dns_error", str(exc), hops=hops, gates=gates) from exc
                    resolve_ms += (time.monotonic() - t_dns) * 1000
                    pinned = ",".join(f"[{ip}]" if ":" in ip else ip for ip in ips[:4])
                    curl_options[CurlOpt.RESOLVE] = [f"{hop_target.host}:{hop_target.port}:{pinned}"]
                    curl_options[CurlOpt.NOPROXY] = "*"
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    raise FetchError("timeout", f"no response within {timeout:.0f}s", hops=hops, gates=gates)
                hop_start = time.monotonic()
                kwargs = {}
                if http_version:
                    kwargs["http_version"] = http_version
                # Per-hop options (DNS pin, size cap). The session belongs to this request
                # only, so replacing its option dict between hops is safe.
                session.curl_options = curl_options
                try:
                    resp = await session.get(
                        current,
                        headers=headers or None,
                        allow_redirects=False,
                        timeout=remaining,
                        proxy=s.proxy,
                        **kwargs,
                    )
                except RequestException as exc:
                    raise FetchError(_curl_error_code(exc), str(exc).split(". See ")[0][:300], hops=hops, gates=gates) from exc
                hop_ms = (time.monotonic() - hop_start) * 1000
                remote_ip = resp.primary_ip or ""
                if s.proxy is None and remote_ip and not s.allow_private and not ip_is_public(remote_ip):
                    raise BlockedDestination(f"connected to a non-public address ({remote_ip})")
                status = resp.status_code
                location = resp.headers.get("location") if status in _REDIRECT else None
                hops.append(Hop(current, status, location, hop_ms, remote_ip))

                if resolve_gates and gate_actions < MAX_GATE_ACTIONS:
                    head = resp.content[:262144].decode("utf-8", "replace")
                    action = check_response(original_url=target.url, url=current, status=status,
                                            location=location, head=head)
                    if action is not None:
                        for name, value in action.cookies.items():
                            jar.set(name, value, domain=action.cookie_domain or hop_target.host)
                        gates.append(action.event)
                        gate_actions += 1
                        current = action.url
                        continue

                if location:
                    redirects += 1
                    if redirects > s.max_redirects:
                        raise FetchError("too_many_redirects", f"more than {s.max_redirects} redirects", hops=hops, gates=gates)
                    current = urljoin(current, location.strip())
                    continue

                if status == 200 and len(resp.content) < 16384 and meta_refreshes < MAX_META_REFRESH:
                    # A refresh inside <noscript> only fires for browsers with JavaScript off;
                    # following it lands on "JavaScript required" pages a real visitor never sees.
                    m = _META_REFRESH.search(_NOSCRIPT.sub(b"", resp.content))
                    if m and int(m.group(1)) <= 5:
                        meta_refreshes += 1
                        current = urljoin(current, _html.unescape(m.group(2).decode("latin-1")))
                        continue
                break

            infos = resp.infos or {}
            dns = infos.get(CurlInfo.NAMELOOKUP_TIME, 0.0) or 0.0
            conn = infos.get(CurlInfo.CONNECT_TIME, 0.0) or 0.0
            tls = infos.get(CurlInfo.APPCONNECT_TIME, 0.0) or 0.0
            ttfb = infos.get(CurlInfo.STARTTRANSFER_TIME, 0.0) or 0.0
            total = infos.get(CurlInfo.TOTAL_TIME, 0.0) or 0.0
            timing = {
                "dnsMs": round(resolve_ms + dns * 1000, 1),
                "connectMs": round(max(conn - dns, 0) * 1000, 1),
                "tlsMs": round(max(tls - conn, 0) * 1000, 1) if tls else 0.0,
                "ttfbMs": round(max(ttfb - max(tls, conn), 0) * 1000, 1),
                "downloadMs": round(max(total - ttfb, 0) * 1000, 1),
                "redirectMs": round(sum(h.ms for h in hops[:-1]), 1),
                "fetchMs": round((time.monotonic() - started) * 1000, 1),
            }
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return FetchResult(
                requested_url=target.url,
                final_url=current,
                status=resp.status_code,
                headers=resp_headers,
                body=resp.content,
                http_version=_HTTP_VERSIONS.get(int(resp.http_version or 0), str(resp.http_version)),
                remote_ip=resp.primary_ip or "",
                hops=hops,
                gates=gates,
                timing=timing,
                strategy=f"http:{impersonate or s.impersonate}",
            )
        finally:
            await session.close()
