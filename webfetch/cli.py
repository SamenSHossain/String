"""Run the fetch pipeline from the command line and print one JSON line per URL.

    python -m webfetch.cli https://www.nu.nl/ https://store.steampowered.com/app/271590/
    python -m webfetch.cli --schema steam --markdown 400 URL...

Used by CI's live-recipes job to check every gate recipe against the real sites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import load_settings
from .fetcher import HttpFetcher
from .service import FetchOptions, run_fetch

SCHEMAS = {
    "steam": {"type": "object", "properties": {
        "name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
        "releaseDate": {"type": "string", "format": "date"}, "developer": {"type": "string"},
        "publisher": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
        "reviewSummary": {"type": "string"}, "reviewCount": {"type": "integer"}}},
    "article": {"type": "object", "properties": {
        "headline": {"type": "string"}, "author": {"type": "string"},
        "datePublished": {"type": "string"}, "description": {"type": "string"}}},
}


async def main_async(args) -> None:
    fetcher = HttpFetcher(load_settings())
    try:
        for url in args.urls:
            schema = SCHEMAS.get(args.schema) if args.schema else (SCHEMAS["steam"] if "steampowered" in url else None)
            opts = FetchOptions(url=url, include={"markdown", "metadata"}, json_schema=schema,
                                resolve_gates=not args.no_gates, render="never")
            try:
                env = await run_fetch(opts, fetcher, None)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"url": url, "exception": f"{type(exc).__name__}: {exc}"}), flush=True)
                continue
            md = env.pop("markdown", "") or ""
            meta = env.pop("metadata", {}) or {}
            out = {k: env.get(k) for k in ("url", "ok", "outcome", "reason", "vendor", "statusCode", "finalUrl",
                                           "strategy", "gates", "redirects", "attempts", "http", "encoding",
                                           "bytes", "visibleChars", "timing", "extracted")}
            out["title"] = meta.get("title")
            out["markdownHead"] = " ".join(md.split())[: args.markdown]
            print(json.dumps(out, ensure_ascii=False), flush=True)
    finally:
        await fetcher.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m webfetch.cli")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--schema", choices=sorted(SCHEMAS))
    ap.add_argument("--markdown", type=int, default=300, help="characters of Markdown to print")
    ap.add_argument("--no-gates", action="store_true", help="disable gate recipes (to see the raw wall)")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
