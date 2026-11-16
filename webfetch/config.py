"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Comma-separated in WEBFETCH_API_KEYS. When empty the API is open (rate limited).
    api_keys: frozenset[str] = field(default_factory=frozenset)
    # Tests only: lets the fetcher reach 127.0.0.1 and other non-public addresses.
    allow_private: bool = False
    # Outbound HTTP proxy, for local development behind a proxy. Unset in production.
    proxy: str | None = None
    # CA bundle for TLS verification. None means curl_cffi's bundled certifi store.
    ca_bundle: str | None = None
    impersonate: str = "chrome"
    max_bytes: int = 15 * 1024 * 1024
    default_timeout: float = 30.0
    max_timeout: float = 90.0
    max_redirects: int = 10
    anon_rate_per_min: int = 30
    key_rate_per_min: int = 600
    # "off" disables the headless-browser tier, "modal" calls the Modal browser class.
    browser_mode: str = "off"


def load_settings() -> Settings:
    keys = frozenset(k.strip() for k in os.environ.get("WEBFETCH_API_KEYS", "").split(",") if k.strip())
    proxy = os.environ.get("WEBFETCH_PROXY") or None
    ca = os.environ.get("WEBFETCH_CA_BUNDLE") or None
    return Settings(
        api_keys=keys,
        allow_private=_env_bool("WEBFETCH_ALLOW_PRIVATE"),
        proxy=proxy,
        ca_bundle=ca,
        impersonate=os.environ.get("WEBFETCH_IMPERSONATE", "chrome"),
        max_bytes=_env_int("WEBFETCH_MAX_BYTES", 15 * 1024 * 1024),
        anon_rate_per_min=_env_int("WEBFETCH_ANON_RATE", 30),
        key_rate_per_min=_env_int("WEBFETCH_KEY_RATE", 600),
        browser_mode=os.environ.get("WEBFETCH_BROWSER", "off").strip().lower(),
    )
