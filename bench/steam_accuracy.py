"""Structured-output accuracy on Steam product pages, against Steam's own API.

    python bench/steam_accuracy.py --ours https://<workspace>--webfetch.modal.run [--string]

Ground truth: https://store.steampowered.com/api/appdetails?appids=<id>&cc=us&l=en
(fetched through the endpoint as raw JSON). For each game the product page is fetched
through webfetch with a jsonSchema, and every field is compared with the API value.
The page URL carries ?cc=us as well: Steam prices by the caller's IP country, and Modal
places containers wherever it has capacity (a US region and Azure Warsaw so far), so
without it a run from a Polish container compares złoty prices with dollar ones. (The age
gate is per store too: GTA V has none on the Polish store and one on the US store.) A game
counts as age-gated when the same URL fetched with resolveGates false lands on /agecheck.

With --string, the same product page is fetched through String's /v1/fetch (keyless,
Markdown) and we check whether each true value appears in what String returned at all:
the ceiling for any extractor, String's own AI extraction included, working from
String's output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request

GAMES = {
    # mature content: Steam sends most of these to the age check on the US store
    271590: "Grand_Theft_Auto_V_Legacy", 1091500: "Cyberpunk_2077", 1174180: "Red_Dead_Redemption_2",
    292030: "The_Witcher_3_Wild_Hunt", 782330: "DOOM_Eternal", 2050650: "Resident_Evil_4",
    1086940: "Baldurs_Gate_3", 1245620: "ELDEN_RING", 381210: "Dead_by_Daylight", 1938090: "Call_of_Duty",
    # usually not gated
    413150: "Stardew_Valley", 105600: "Terraria", 1145360: "Hades", 620: "Portal_2", 367520: "Hollow_Knight",
    427520: "Factorio", 504230: "Celeste", 1794680: "Vampire_Survivors", 646570: "Slay_the_Spire",
    892970: "Valheim",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price": {"type": "number", "description": "current price"},
        "currency": {"type": "string"},
        "releaseDate": {"type": "string", "format": "date"},
        "developer": {"type": "string"},
        "publisher": {"type": "string"},
    },
}
FIELDS = list(SCHEMA["properties"])


def post(url: str, payload: dict, timeout: float = 120) -> tuple[int, dict, bytes, float]:
    """POST JSON. Waits out short 429s: a run makes 40 calls, over the endpoint's 30/min."""
    for _ in range(5):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), time.monotonic() - t0
        except urllib.error.HTTPError as e:
            code, headers = e.code, {k.lower(): v for k, v in e.headers.items()}
            body, secs = e.read(), time.monotonic() - t0
        try:
            wait = float(headers.get("retry-after", "5"))
        except ValueError:
            wait = 5.0
        if code != 429 or wait > 90:
            break
        time.sleep(wait + 0.5)
    return code, headers, body, secs


def norm_date(s: str | None) -> str | None:
    if not s:
        return None
    for f in ("%Y-%m-%d", "%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s.strip(), f).date().isoformat()
        except ValueError:
            continue
    return s.strip()


def truth_for(ours: str, appid: int) -> dict | None:
    api = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=en"
    status, _h, body, _s = post(ours.rstrip("/") + "/v1/fetch", {"url": api, "format": "raw", "resolveGates": False})
    if status != 200:
        return None
    try:
        d = json.loads(body)[str(appid)]["data"]
    except (KeyError, ValueError, TypeError):
        return None
    price = d.get("price_overview") or {}
    return {
        "name": d.get("name"),
        "price": 0.0 if d.get("is_free") else (price.get("final", 0) / 100.0 if price else None),
        "currency": price.get("currency") if price else None,
        "releaseDate": norm_date((d.get("release_date") or {}).get("date")),
        "developer": (d.get("developers") or [None])[0],
        "publisher": (d.get("publishers") or [None])[0],
    }


def age_gated(ours: str, url: str) -> bool:
    """With the gate recipe off, does Steam send this URL to the age check?"""
    _status, _h, body, _s = post(ours.rstrip("/") + "/v1/fetch", {"url": url, "include": [], "resolveGates": False})
    try:
        return json.loads(body).get("outcome") == "age_gate"
    except (ValueError, AttributeError):
        return False


def same(field: str, got, want) -> bool:
    if want is None:
        return got is None
    if got is None:
        return False
    if field == "price":
        return abs(float(got) - float(want)) < 0.005
    if field in ("developer", "publisher"):
        return want.lower() in str(got).lower()
    return str(got).strip().lower() == str(want).strip().lower()


def appears(field: str, text: str, want) -> bool:
    if want is None:
        return True
    t = text.lower()
    if field == "price":
        return f"{want:.2f}" in t or ("free" in t and want == 0)
    if field == "releaseDate":
        d = dt.date.fromisoformat(want)
        variants = {d.strftime("%-d %b, %Y"), d.strftime("%b %-d, %Y"), d.isoformat()}
        return any(v.lower() in t for v in variants)
    if field == "currency":
        return want.lower() in t or (want == "USD" and "$" in t)
    return str(want).lower() in t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", required=True)
    ap.add_argument("--string", action="store_true")
    ap.add_argument("--out", default="bench/steam_accuracy.jsonl")
    a = ap.parse_args()
    rows = []
    with open(a.out, "w", encoding="utf-8") as fh:
        for appid, slug in GAMES.items():
            url = f"https://store.steampowered.com/app/{appid}/{slug}/?cc=us"
            truth = truth_for(a.ours, appid)
            if truth is None:
                print(f"skip {appid}: no ground truth")
                continue
            status, h, body, secs = post(a.ours.rstrip("/") + "/v1/fetch", {"url": url, "jsonSchema": SCHEMA, "include": []})
            env = json.loads(body or b"{}")
            got = ((env.get("extracted") or {}).get("data")) or {}
            row = {"appid": appid, "url": url, "truth": truth, "ours": got, "oursOutcome": env.get("outcome"),
                   "oursExtractMs": (env.get("extracted") or {}).get("ms"),
                   "oursServerMs": (env.get("timing") or {}).get("totalMs"),
                   "oursRequestId": h.get("x-request-id"), "oursSecs": round(secs, 3),
                   "oursSources": (env.get("extracted") or {}).get("sources"),
                   "gated": age_gated(a.ours, url)}
            row["oursCorrect"] = {f: same(f, got.get(f), truth[f]) for f in FIELDS}
            if a.string:
                s_status, sh, sbody, ssecs = post("https://request.usestring.ai/v1/fetch", {"url": url, "format": "markdown"})
                text = sbody.decode("utf-8", "replace")
                row.update({"stringFinalUrl": sh.get("x-final-url"), "stringRequestId": sh.get("x-request-id"),
                            "stringSecs": round(ssecs, 3), "stringHttp": s_status})
                row["stringContains"] = {f: appears(f, text, truth[f]) for f in FIELDS}
            rows.append(row)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{appid:>8} ours={sum(row['oursCorrect'].values())}/{len(FIELDS)} "
                  + (f"string-ceiling={sum(row['stringContains'].values())}/{len(FIELDS)} " if a.string else "")
                  + f"{'gated  ' if row['gated'] else 'ungated'} {row['oursOutcome']} {url}", flush=True)
    groups = {"all": rows, "age-gated": [r for r in rows if r["gated"]], "ungated": [r for r in rows if not r["gated"]]}
    for name, rs in groups.items():
        n = len(FIELDS) * len(rs)
        if not n:
            continue
        ok = sum(sum(r["oursCorrect"].values()) for r in rs)
        line = f"{name} ({len(rs)} games): ours {ok}/{n} = {100 * ok / n:.1f}%"
        if a.string:
            hit = sum(sum(r["stringContains"].values()) for r in rs)
            line += f", string ceiling {hit}/{n} = {100 * hit / n:.1f}%"
        print(line)


if __name__ == "__main__":
    main()
