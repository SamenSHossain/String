"""Post-deploy smoke test: fetch the challenge targets through a deployed endpoint.

    python bench/smoke.py https://<workspace>--webfetch.modal.run

Standard library only, so CI needs no extra installs. Exits non-zero only if /health
fails; per-target results are informational and printed as JSON lines.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

TARGETS = [
    # (url, expect, note)
    ("https://store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/", {"selector": "#appHubAppName"}, "steam age gate"),
    ("https://store.steampowered.com/app/1091500/Cyberpunk_2077/", {"selector": "#appHubAppName"}, "steam age gate"),
    ("https://www.nu.nl/", {"minChars": 3000}, "dpg privacy gate"),
    ("https://tweakers.net/", {"minChars": 3000}, "dpg privacy gate"),
    ("https://www.volkskrant.nl/", {"minChars": 3000}, "dpg privacy gate"),
    ("https://www.ad.nl/", {"minChars": 3000}, "dpg privacy gate"),
    ("https://www.zeit.de/index", {"minChars": 3000}, "zeit consent / 403"),
    ("https://www.derstandard.at/", {"minChars": 3000}, "derstandard consent"),
    ("https://www.golem.de/", {"minChars": 3000}, "golem consent"),
    ("https://www.southwest.com/", {"minChars": 1000}, "empty body"),
    ("https://www.sahibinden.com/", {"minChars": 1000}, "login redirect"),
    ("https://www.ozon.ru/", {"minChars": 1000}, "abt challenge"),
    ("https://example.com/", None, "control"),
]

STEAM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
        "releaseDate": {"type": "string", "format": "date"}, "developer": {"type": "string"},
        "publisher": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
        "reviewSummary": {"type": "string"}, "reviewCount": {"type": "integer"},
    },
}


def call(base: str, path: str, payload: dict | None = None, timeout: float = 150) -> tuple[int, dict, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "webfetch-smoke/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, dict(e.headers), json.loads(body or b"{}")
        except json.JSONDecodeError:
            return e.code, dict(e.headers), {"raw": body[:300].decode("utf-8", "replace")}


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].startswith("https://"):
        print("usage: smoke.py https://<endpoint>", file=sys.stderr)
        return 2
    base = sys.argv[1]
    for attempt in range(6):  # first call may hit a cold start
        try:
            status, _h, body = call(base, "/health", timeout=60)
            if status == 200:
                print(json.dumps({"health": body}))
                break
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"health_error": str(exc)}))
        time.sleep(5)
    else:
        return 1

    for url, expect, note in TARGETS:
        payload = {"url": url, "include": ["metadata"]}
        if expect:
            payload["expect"] = expect
        if "steampowered" in url:
            payload["jsonSchema"] = STEAM_SCHEMA
        t0 = time.monotonic()
        try:
            status, headers, body = call(base, "/v1/fetch", payload)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"url": url, "note": note, "error": str(exc)}))
            continue
        print(json.dumps({
            "url": url, "note": note, "http": status, "ok": body.get("ok"), "outcome": body.get("outcome"),
            "reason": body.get("reason"), "statusCode": body.get("statusCode"), "finalUrl": body.get("finalUrl"),
            "strategy": body.get("strategy"), "gates": body.get("gates"), "attempts": body.get("attempts"),
            "title": (body.get("metadata") or {}).get("title"), "visibleChars": body.get("visibleChars"),
            "bytes": body.get("bytes"), "timing": body.get("timing"), "extracted": body.get("extracted"),
            "requestId": headers.get("x-request-id") or body.get("requestId"),
            "wallMs": round((time.monotonic() - t0) * 1000), "remoteIp": (body.get("http") or {}).get("remoteIp"),
            "httpVersion": (body.get("http") or {}).get("version"),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
