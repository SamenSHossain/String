"""Structured-output accuracy on Steam product pages, against Steam's own API.

    python bench/steam_accuracy.py --ours https://<workspace>--webfetch.modal.run [--string]

Ground truth: https://store.steampowered.com/api/appdetails?appids=<id>&cc=us&l=en
(fetched through the endpoint as raw JSON). For each game the product page is fetched
through webfetch with a jsonSchema, and every field is compared with the API value.

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
    # age-gated (mature content) on the US store
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
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(), time.monotonic() - t0


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
    tot = {"ours": [0, 0], "string": [0, 0]}
    with open(a.out, "w", encoding="utf-8") as fh:
        for appid, slug in GAMES.items():
            url = f"https://store.steampowered.com/app/{appid}/{slug}/"
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
                   "gated": any(g.get("kind") == "age_gate" for g in env.get("gates") or [] if g.get("trigger") != "preset")}
            row["oursCorrect"] = {f: same(f, got.get(f), truth[f]) for f in FIELDS}
            tot["ours"][0] += sum(row["oursCorrect"].values())
            tot["ours"][1] += len(FIELDS)
            if a.string:
                s_status, sh, sbody, ssecs = post("https://request.usestring.ai/v1/fetch", {"url": url, "format": "markdown"})
                text = sbody.decode("utf-8", "replace")
                row.update({"stringFinalUrl": sh.get("x-final-url"), "stringRequestId": sh.get("x-request-id"),
                            "stringSecs": round(ssecs, 3), "stringHttp": s_status})
                row["stringContains"] = {f: appears(f, text, truth[f]) for f in FIELDS}
                tot["string"][0] += sum(row["stringContains"].values())
                tot["string"][1] += len(FIELDS)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{appid:>8} ours={sum(row['oursCorrect'].values())}/{len(FIELDS)} "
                  + (f"string-ceiling={sum(row['stringContains'].values())}/{len(FIELDS)} " if a.string else "")
                  + f"{row['oursOutcome']} {url}", flush=True)
    for k, (ok, n) in tot.items():
        if n:
            print(f"{k}: {ok}/{n} fields = {100 * ok / n:.1f}%")


if __name__ == "__main__":
    main()
