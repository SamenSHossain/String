"""The submission's claim in one command: the same request through String and through webfetch.

    python3 bench/pair.py                        # Steam's Cyberpunk 2077 page, format markdown
    python3 bench/pair.py --js                   # the same request with executeJS: true
    python3 bench/pair.py --url https://www.southwest.com/ --marker "Rapid Rewards" --marker "Flight status"
    python3 bench/pair.py --out bench/pairs.jsonl

Prints both request IDs and whether each response is the real page: it has to contain a
marker that only the real page has (the rule String's own benchmark uses), not just a 2xx
status. Each run spends one request of String's keyless quota; --ours-only spends none.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

STRING_API = "https://request.usestring.ai/v1/fetch"
OURS = "https://samenh--webfetch.modal.run"
TARGET = "https://store.steampowered.com/app/1091500/Cyberpunk_2077/"
MARKERS = ["CD PROJEKT RED"]  # the developer line: on the product page, not on the age check


def post(url: str, payload: dict, timeout: float = 150) -> tuple[int, dict, str, float]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "webfetch-pair/1"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, headers, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, e.headers, e.read()
    return status, {k.lower(): v for k, v in headers.items()}, body.decode("utf-8", "replace"), time.monotonic() - t0


def summarize(status: int, h: dict, text: str, secs: float, markers: list[str]) -> dict:
    return {"http": status, "destinationStatus": h.get("x-status-code"), "finalUrl": h.get("x-final-url"),
            "requestId": h.get("x-request-id"), "secs": round(secs, 2), "chars": len(text),
            "realPage": any(m in text for m in markers),
            "outcome": h.get("x-outcome"), "strategy": h.get("x-strategy"), "billed": h.get("x-billed-request-type"),
            "head": " ".join(text[:240].split())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=TARGET)
    ap.add_argument("--marker", action="append", help="text only the real page has (repeatable; any one suffices)")
    ap.add_argument("--js", action="store_true", help="send executeJS: true")
    ap.add_argument("--ours", default=OURS, help="base URL of the webfetch deployment")
    ap.add_argument("--ours-only", action="store_true", help="do not call String")
    ap.add_argument("--out", help="append the result to this JSON-lines file")
    a = ap.parse_args()
    markers = a.marker or (MARKERS if a.url == TARGET else None)
    if not markers:
        ap.error("--marker is required for a --url other than the default")
    payload = {"url": a.url, "format": "markdown"}
    if a.js:
        payload["executeJS"] = True
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request": payload, "markers": markers}
    if not a.ours_only:
        row["string"] = summarize(*post(STRING_API, payload), markers)
    row["webfetch"] = summarize(*post(a.ours.rstrip("/") + "/v1/fetch", payload), markers)

    print(f"request: POST /v1/fetch {json.dumps(payload)}")
    for name in ("string", "webfetch"):
        r = row.get(name)
        if r is None:
            continue
        verdict = "REAL PAGE" if r["realPage"] else "not the page"
        print(f"{name:9} {verdict:13} HTTP {r['http']}, x-status-code {r['destinationStatus']}, "
              f"{r['chars']:,} chars in {r['secs']} s")
        print(f"{'':9} request ID {r['requestId']}")
        print(f"{'':9} final URL  {r['finalUrl']}")
        print(f"{'':9} starts     {r['head'][:110]!r}")
    if a.out:
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0 if row["webfetch"]["realPage"] else 1


if __name__ == "__main__":
    sys.exit(main())
