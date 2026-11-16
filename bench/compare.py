"""Head-to-head: the same URLs through String's /v1/fetch and through webfetch.

    python bench/compare.py --ours https://<workspace>--webfetch.modal.run \
        --runs 2 --out bench/results.jsonl

String is called keyless (no Authorization header) unless STRING_API_KEY is set.
Success is judged the way String's own benchmark judges it: the response must contain
a marker that only appears on the real page, not just a 2xx status.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request

STRING_API = "https://request.usestring.ai/v1/fetch"

# url -> markers that appear only on the real page (any one suffices)
CASES = {
    "https://store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/": ["Rockstar North"],
    "https://store.steampowered.com/app/1091500/Cyberpunk_2077/": ["CD PROJEKT RED"],
    "https://www.nu.nl/": ["Net binnen", "Voorpagina"],
    "https://tweakers.net/": ["Pricewatch"],
    "https://www.volkskrant.nl/": ["Opinie"],
    "https://www.ad.nl/": ["Sport"],
    "https://www.derstandard.at/": ["Inland"],
    "https://www.golem.de/": ["golem.de/news/"],
    "https://www.southwest.com/": ["Book a flight", "Flight status", "Low Fare Calendar"],
    "https://www.zeit.de/index": ["ZEIT ONLINE"],
}


# Popular pages both services can fetch: a like-for-like latency comparison.
SPEED = {
    "https://en.wikipedia.org/wiki/Web_scraping": ["Web scraping"],
    "https://www.python.org/": ["Python"],
    "https://news.ycombinator.com/": ["Hacker News"],
    "https://www.bbc.com/news": ["BBC"],
    "https://www.spiegel.de/": ["SPIEGEL"],
    "https://github.com/trending": ["Trending"],
    "https://www.theguardian.com/international": ["Guardian"],
    "https://store.steampowered.com/app/413150/Stardew_Valley/": ["Stardew Valley", "ConcernedApe"],
}


def post(url: str, payload: dict, headers: dict, timeout: float = 120) -> tuple[int, dict, bytes, float]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, body, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, {k.lower(): v for k, v in e.headers.items()}, body, time.monotonic() - t0


def string_call(url: str, execute_js: bool) -> dict:
    payload = {"url": url, "format": "markdown"}
    if execute_js:
        payload["executeJS"] = True
    headers = {}
    key = os.environ.get("STRING_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    status, h, body, secs = post(STRING_API, payload, headers)
    text = body.decode("utf-8", "replace")
    return {"provider": "string" + ("+js" if execute_js else ""), "http": status, "secs": round(secs, 3),
            "destStatus": h.get("x-status-code"), "finalUrl": h.get("x-final-url"),
            "requestId": h.get("x-request-id"), "billed": h.get("x-billed-request-type"),
            "chars": len(text), "text": text}


def ours_call(base: str, url: str) -> dict:
    status, h, body, secs = post(base.rstrip("/") + "/v1/fetch", {"url": url, "format": "markdown"}, {})
    text = body.decode("utf-8", "replace")
    return {"provider": "webfetch", "http": status, "secs": round(secs, 3), "destStatus": h.get("x-status-code"),
            "finalUrl": h.get("x-final-url"), "requestId": h.get("x-request-id"), "outcome": h.get("x-outcome"),
            "gates": h.get("x-gates"), "serverTiming": h.get("server-timing"), "chars": len(text), "text": text}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--string-js", action="store_true", help="also call String with executeJS")
    ap.add_argument("--only", nargs="*", help="subset of URLs")
    ap.add_argument("--set", choices=["gated", "speed"], default="gated")
    ap.add_argument("--out", default="bench/results.jsonl")
    a = ap.parse_args()
    cases = CASES if a.set == "gated" else SPEED
    urls = a.only or list(cases)
    rows = []
    with open(a.out, "a", encoding="utf-8") as fh:
        for run in range(a.runs):
            for url in urls:
                calls = [lambda: ours_call(a.ours, url), lambda: string_call(url, False)]
                if a.string_js:
                    calls.append(lambda: string_call(url, True))
                for call in calls:
                    r = call()
                    markers = cases.get(url, [])
                    r["markerHit"] = bool(markers) and any(m.lower() in r["text"].lower() for m in markers)
                    r["success"] = r["http"] == 200 and r["markerHit"]
                    r.update({"url": url, "run": run, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                    r["head"] = " ".join(r.pop("text").split())[:300]
                    rows.append(r)
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    print(f"{r['provider']:<9} {r['http']} ok={r['success']!s:<5} {r['secs']:>6.2f}s "
                          f"chars={r['chars']:<7} id={r['requestId']} {url}", flush=True)
    for prov in sorted({r["provider"] for r in rows}):
        rs = [r for r in rows if r["provider"] == prov]
        ok = [r for r in rs if r["success"]]
        lat = [r["secs"] for r in ok]
        print(f"{prov}: {len(ok)}/{len(rs)} real pages"
              + (f", median {statistics.median(lat):.2f}s on successes" if lat else ""))


if __name__ == "__main__":
    main()
