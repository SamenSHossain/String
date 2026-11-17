"""Check every behaviour the write-up claims, against the live endpoint, with pass/fail.

    python3 bench/verify_live.py [https://samenh--webfetch.modal.run]

Standard library only. Exits 1 if any check fails. Honours 429 Retry-After, so running it
twice in a row is fine (the endpoint allows 30 requests/min per client IP).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://samenh--webfetch.modal.run").rstrip("/")

# Same markers the head-to-head benchmark judges by (text only the real page contains; any one
# suffices). zeit.de is checked separately below: its challenge decides whether we get it.
from compare import CASES  # noqa: E402  (bench/ is on sys.path when run as a script)

TARGETS = {url: markers for url, markers in CASES.items() if "zeit.de" not in url}


def request(method: str, path: str, payload: dict | None = None, timeout: float = 150):
    data = json.dumps(payload).encode() if payload is not None else None
    for _ in range(4):
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "webfetch-verify/1"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in e.headers.items()}
            if e.code == 429:
                time.sleep(float(headers.get("retry-after", "5")) + 0.5)
                continue
            return e.code, headers, e.read(), time.monotonic() - t0
    return 429, {}, b"", 0.0


def parse_json(body: bytes) -> dict:
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def main() -> int:
    print(f"Endpoint: {BASE}\n")

    status, _h, body, secs = request("GET", "/health", timeout=90)
    health = parse_json(body)
    check("health", status == 200 and health.get("ok") is True and health.get("browser") is True,
          f"{status}, browser tier {'on' if health.get('browser') else 'off'}, {secs:.1f}s")
    status, _h, _b, _s = request("GET", "/docs")
    check("interactive docs (/docs)", status == 200, str(status))

    print("\nTargets String returns walls for (format: markdown):")
    for url, markers in TARGETS.items():
        status, h, body, secs = request("POST", "/v1/fetch", {"url": url, "format": "markdown"})
        text = body.decode("utf-8", "replace").lower()
        ok = status == 200 and h.get("x-outcome") == "ok" and any(m.lower() in text for m in markers)
        check(url, ok, f"{status} {h.get('x-outcome')} {len(text):,} chars {secs:.1f}s "
                       f"via {h.get('x-strategy')} id={h.get('x-request-id')}")

    # zeit.de sits behind a Centinel Analytica challenge that usually refuses us and now and
    # then waves a request through. Either answer is correct; a wall returned as the page is not.
    zeit = "https://www.zeit.de/index"
    status, h, body, secs = request("POST", "/v1/fetch", {"url": zeit, "format": "markdown"})
    if status == 200:
        text = body.decode("utf-8", "replace").lower()
        check("zeit.de: Centinel let this one through, and it is the real page",
              h.get("x-outcome") == "ok" and any(m.lower() in text for m in CASES[zeit]),
              f"{len(text):,} chars {secs:.1f}s via {h.get('x-strategy')} id={h.get('x-request-id')}")
    else:
        env = parse_json(body)
        check("zeit.de: Centinel challenge labelled as a challenge, not passed off as the page",
              status == 502 and env.get("outcome") == "challenge",
              f"{status} {env.get('outcome')} {env.get('vendor')} id={h.get('x-request-id')}")

    print("\nAPI behaviour:")
    status, h, body, _s = request("POST", "/v1/fetch", {
        "url": "https://store.steampowered.com/app/1091500/Cyberpunk_2077/", "include": [],
        "jsonSchema": {"type": "object", "properties": {
            "name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
            "developer": {"type": "string"}, "warranty": {"type": "string"}}}})
    ex = parse_json(body).get("extracted") or {}
    d = ex.get("data") or {}
    check("jsonSchema: Steam fields extracted with sources, missing field null",
          status == 200 and d.get("name") == "Cyberpunk 2077" and isinstance(d.get("price"), float)
          and d.get("developer") == "CD PROJEKT RED" and d.get("warranty") is None
          and "warranty" in (ex.get("missing") or []) and bool(ex.get("sources")),
          f"{d} in {ex.get('ms')} ms")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://www.nu.nl/", "format": "markdown"})
    check("String-compatible headers (x-request-id, x-status-code, x-final-url) + server-timing",
          all(k in h for k in ("x-request-id", "x-status-code", "x-final-url", "server-timing")),
          f"x-status-code={h.get('x-status-code')} x-final-url={h.get('x-final-url')}")

    status, h, body, _s = request("GET", "/v1/fetch?url=https://www.derstandard.at/")
    env = parse_json(body)
    check("GET /v1/fetch (JSON envelope)", status == 200 and env.get("ok") is True and bool(env.get("markdown")),
          f"{status} {env.get('outcome')} title={(env.get('metadata') or {}).get('title')!r}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "https://www.nu.nl/", "resolveGates": False})
    env = parse_json(body)
    check("resolveGates:false shows the wall is labelled, not passed off as the page",
          status == 502 and env.get("outcome") == "consent_wall", f"{status} {env.get('outcome')}")

    # Steam's age gate is per store (GTA V has one on the US store, none on the Polish one) and
    # Modal may put the container in either country, so the Steam targets above can pass without
    # meeting the gate. Pin the US store: the gate must be there without the recipe, gone with it.
    gta = "https://store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/?cc=us"
    s_off, _h, b_off, _s = request("POST", "/v1/fetch", {"url": gta, "include": [], "resolveGates": False})
    s_on, h_on, b_on, _s = request("POST", "/v1/fetch", {"url": gta, "format": "markdown"})
    off = parse_json(b_off)
    check("Steam age gate (US store): there without the recipe, passed with it",
          s_off == 502 and off.get("outcome") == "age_gate" and s_on == 200 and h_on.get("x-outcome") == "ok"
          and "rockstar north" in b_on.decode("utf-8", "replace").lower(),
          f"without: {s_off} {off.get('outcome')} at {off.get('finalUrl')}; "
          f"with: {s_on} {h_on.get('x-outcome')} id={h_on.get('x-request-id')}")

    status, h, body, _s = request("POST", "/v1/fetch", {"url": "http://169.254.169.254/latest/meta-data/"})
    check("SSRF: cloud metadata address refused", status == 403, f"{status} {parse_json(body).get('error')}")
    status, h, body, _s = request("POST", "/v1/fetch", {"url": "ftp://example.com/"})
    check("invalid URL is a 400", status == 400, str(status))

    status, h, body, secs = request("POST", "/v1/batch", {"requests": [
        {"url": "https://www.python.org/", "include": []},
        {"url": "https://tweakers.net/", "include": []},
        {"url": "https://store.steampowered.com/app/413150/Stardew_Valley/", "include": []}]})
    res = parse_json(body).get("results") or []
    check("batch of 3", status == 200 and len(res) == 3 and all(r.get("ok") for r in res),
          f"{status}, {[r.get('outcome') for r in res]}, {secs:.1f}s")

    def one(url):
        return request("POST", "/v1/fetch", {"url": url, "include": []})[0]

    urls = ["https://en.wikipedia.org/wiki/Web_scraping", "https://www.python.org/",
            "https://store.steampowered.com/app/1091500/Cyberpunk_2077/", "https://www.derstandard.at/",
            "https://tweakers.net/", "https://www.bbc.com/news"]
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(len(urls)) as pool:
        codes = list(pool.map(one, urls))
    check(f"{len(urls)} concurrent requests", all(c == 200 for c in codes),
          f"{codes} in {time.monotonic() - t0:.1f}s")

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
