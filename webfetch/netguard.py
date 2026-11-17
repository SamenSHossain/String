"""URL validation and SSRF protection.

Every hop (the first request and each redirect) is resolved here before it is fetched.
The fetcher then pins curl to the address that was checked (CURLOPT_RESOLVE), so a DNS
answer that changes between the check and the connect (rebinding) cannot reach a
private address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

MAX_URL_LENGTH = 8192
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")
BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
# Caller headers that may reach any origin: they describe the client, not who it is.
# Every other caller header (Authorization, Cookie, X-Api-Key, any custom header) is
# origin-bound: only the origin the caller named receives it.
CROSS_ORIGIN_SAFE_HEADERS = frozenset({"accept", "accept-language", "user-agent", "cache-control", "pragma",
                                       "dnt", "upgrade-insecure-requests"})


def cross_origin_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """The caller headers that may be sent to an origin other than the one named."""
    return {k: v for k, v in (headers or {}).items() if k.lower() in CROSS_ORIGIN_SAFE_HEADERS}


class InvalidUrl(ValueError):
    """The URL is malformed or uses an unsupported scheme."""


class BlockedDestination(ValueError):
    """The URL points at a private, loopback, link-local or otherwise non-public address."""


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str  # IDNA-encoded, lower case, no brackets
    port: int


def normalize_url(raw: str) -> Target:
    """Validate a user-supplied URL and return it in canonical form.

    A bare domain such as ``example.com/path`` is treated as https.
    """
    if not isinstance(raw, str):
        raise InvalidUrl("url must be a string")
    url = raw.strip()
    if not url:
        raise InvalidUrl("url is empty")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrl(f"url is longer than {MAX_URL_LENGTH} characters")
    if any(ord(c) < 0x20 or c == "\x7f" for c in url) or " " in url:
        raise InvalidUrl("url contains whitespace or control characters")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidUrl("only http and https URLs can be fetched")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrl("credentials in the URL are not supported")
    host = parts.hostname
    if not host:
        raise InvalidUrl("url has no host")
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidUrl(f"invalid host name: {exc}") from exc
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise InvalidUrl("invalid port") from exc
    netloc_host = f"[{host}]" if ":" in host else host
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = netloc_host if default_port else f"{netloc_host}:{port}"
    path = _requote(parts.path or "/")
    canonical = urlunsplit((scheme, netloc, path, _requote(parts.query), ""))
    return Target(url=canonical, scheme=scheme, host=host, port=port)


_SAFE = "!#$%&'()*+,/:;=?@[]~-._"


def _requote(component: str) -> str:
    """Percent-encode anything outside printable ASCII (e.g. a raw UTF-8 redirect target),
    leaving existing %XX escapes and reserved characters alone."""
    if all(0x20 < ord(c) < 0x7F for c in component):
        return component
    return quote(component, safe=_SAFE)


# IPv6 forms that carry an IPv4 address in their low 32 bits; a translator (NAT64, SIIT)
# or a dual-stack host may deliver them to that IPv4, so they are judged by it.
_LOW32_IPV4_NETS = (
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64 well-known prefix (RFC 6052)
    ipaddress.IPv6Network("::/96"),  # IPv4-compatible ::a.b.c.d (deprecated, RFC 4291)
    ipaddress.IPv6Network("::ffff:0:0:0/96"),  # IPv4-translated ::ffff:0:a.b.c.d (RFC 2765)
)
# NAT64 local-use prefix (RFC 8215): never globally reachable, and operators may place the
# IPv4 at several bit positions inside it, so it is refused whatever it embeds.
_LOCAL_NAT64 = ipaddress.IPv6Network("64:ff9b:1::/48")


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        return addr.teredo[1]
    if any(addr in net for net in _LOW32_IPV4_NETS):
        return ipaddress.IPv4Address(int(addr) & 0xFFFF_FFFF)
    return None


def ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in _LOCAL_NAT64:
            return False
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            addr = embedded
    return bool(addr.is_global) and not addr.is_multicast


def host_is_blocked(host: str) -> bool:
    return host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES)


async def resolve_public(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Resolve ``host`` and return its addresses, refusing any non-public one."""
    if host_is_blocked(host) and not allow_private:
        raise BlockedDestination(f"{host} is not a public host")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=10
            )
        except (socket.gaierror, asyncio.TimeoutError) as exc:
            raise InvalidUrl(f"could not resolve {host}: {exc}") from exc
        ips = []
        for info in infos:
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    if not ips:
        raise InvalidUrl(f"{host} has no addresses")
    if not allow_private:
        bad = [ip for ip in ips if not ip_is_public(ip)]
        if bad:
            raise BlockedDestination(f"{host} does not resolve to a public address")
    # Prefer IPv4 first (more widely routable from cloud egress), keep IPv6 as fallback.
    ips.sort(key=lambda ip: ":" in ip)
    return ips
