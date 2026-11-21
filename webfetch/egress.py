"""Local egress proxy for the headless browser.

Chromium is started with ``--proxy-server`` pointing here (and ``<-loopback>`` so that even
localhost goes through it). Every connection the browser makes - navigations, every hop
of a redirect chain, subresources, workers, WebSockets - arrives as a CONNECT (https,
wss) or an absolute-form request (http, ws). Each destination is resolved and checked
with the same rules as the HTTP tier, and the proxy connects to the address it checked,
so neither redirects nor DNS rebinding can reach a private, loopback or metadata address.
Anything that fails the check, including a DNS error, is refused (fail closed).
"""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from .netguard import BlockedDestination, InvalidUrl, resolve_public

_HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}
READ_TIMEOUT = 30.0
MAX_HEADER_BYTES = 64 * 1024


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class EgressProxy:
    def __init__(self, *, allow_private: bool = False):
        self.allow_private = allow_private
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.refused: list[str] = []  # recent refusals, for diagnostics

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None

    async def _checked_connect(self, host: str, port: int):
        ips = await resolve_public(host, port, allow_private=self.allow_private)
        last: Exception | None = None
        for ip in ips[:4]:
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=15)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise ConnectionError(f"could not connect to {host}:{port}: {last}")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_w = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
            if len(head) > MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = [ln for ln in lines[1:] if ln]

            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                host = host.strip("[]").lower()
                port = int(port_s)
                upstream_r, upstream_w = await self._checked_connect(host, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                url = urlsplit(target)
                if url.scheme not in ("http", "ws") or not url.hostname:
                    raise ValueError(f"unsupported proxy request {target[:100]!r}")
                host = url.hostname.lower()
                port = url.port or 80
                upstream_r, upstream_w = await self._checked_connect(host, port)
                path = url.path or "/"
                if url.query:
                    path += "?" + url.query
                kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP]
                kept.append("Connection: close")  # one request per proxy connection
                upstream_w.write(f"{method} {path} {version}\r\n".encode("latin-1")
                                 + "\r\n".join(kept).encode("latin-1") + b"\r\n\r\n")
                await upstream_w.drain()

            await asyncio.gather(_pipe(reader, upstream_w), _pipe(upstream_r, writer))
        except (BlockedDestination, InvalidUrl) as exc:
            self.refused.append(str(exc)[:200])
            del self.refused[:-50]
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
        except Exception:
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            for w in (writer, upstream_w):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()
