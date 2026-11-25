"""Test resolver: host names go to the local server, IP literals get the real checks."""

from __future__ import annotations

import ipaddress

from webfetch import netguard


async def resolve(host, port, *, allow_private=False):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if netguard.host_is_blocked(host) and not allow_private:
            raise netguard.BlockedDestination(f"{host} is not a public host")
        return ["127.0.0.1"]
    return await netguard.resolve_public(host, port, allow_private=allow_private)
