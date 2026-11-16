"""The fetch pipeline: fetch -> decode -> classify -> (escalate) -> validate -> extract."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import extract
from .classify import Verdict, classify
from .decode import decode_body
from .fetcher import FetchError, FetchResult, HttpFetcher
from .gates import GateEvent, presets_for
from .netguard import normalize_url
from .schema_fill import build_candidates, fill_schema, site_candidates

JSON_INCLUDES = {"markdown", "html", "text", "metadata", "jsonLd", "microdata", "links", "hydration"}
DEFAULT_INCLUDES = {"markdown", "metadata"}
# Second TLS/HTTP2 fingerprint tried when the first one is refused.
ALT_IMPERSONATE = "safari"


class BrowserClient(Protocol):
    async def render(self, url: str, *, cookies: list[dict], headers: dict[str, str] | None,
                     timeout: float) -> dict[str, Any]: ...


@dataclass
class Expect:
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    selector: str | None = None
    min_chars: int | None = None


@dataclass
class FetchOptions:
    url: str
    format: str = "json"
    markdown_mode: str = "full"
    include: set[str] = field(default_factory=lambda: set(DEFAULT_INCLUDES))
    json_schema: dict | None = None
    expect: Expect | None = None
    resolve_gates: bool = True
    render: str = "auto"  # auto | never | always
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    timeout: float | None = None
    max_bytes: int | None = None


@dataclass
class Page:
    """A fetched document plus everything derived from it."""

    result: FetchResult
    text: str
    encoding: str
    encoding_source: str
    content_type: str
    is_html: bool
    tree: Any
    visible: str
    title: str
    verdict: Verdict
    validation: dict | None = None


def _validate(expect: Expect | None, page: Page) -> dict | None:
    if expect is None:
        return None
    checks = []
    hay = (page.title + " " + page.visible).lower()
    for s in expect.contains:
        checks.append({"check": "contains", "value": s, "passed": s.lower() in hay})
    for s in expect.not_contains:
        checks.append({"check": "notContains", "value": s, "passed": s.lower() not in hay})
    if expect.selector:
        found = bool(page.tree is not None and page.tree.css_first(expect.selector) is not None)
        checks.append({"check": "selector", "value": expect.selector, "passed": found})
    if expect.min_chars is not None:
        checks.append({"check": "minChars", "value": expect.min_chars, "actual": len(page.visible),
                       "passed": len(page.visible) >= expect.min_chars})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def _analyze(result: FetchResult, expect: Expect | None) -> Page:
    ctype = result.headers.get("content-type", "")
    text, enc, enc_src = decode_body(result.body, ctype)
    sniff = text[:1024].lstrip().lower()
    is_html = "html" in ctype.lower() or sniff.startswith(("<!doctype html", "<html", "<head", "<body"))
    tree = extract.parse(text) if is_html else None
    visible = extract.visible_text(tree) if tree is not None else text.strip()
    title = ""
    if tree is not None:
        node = tree.css_first("title")
        title = " ".join(node.text().split()) if node is not None else ""
    verdict = classify(
        status=result.status, headers=result.headers, original_url=result.requested_url,
        final_url=result.final_url, body=result.body, text=text, visible_chars=len(visible),
        title=title, content_type=ctype,
    )
    page = Page(result, text, enc, enc_src, ctype, is_html, tree, visible, title, verdict)
    page.validation = _validate(expect, page)
    return page


def _good(page: Page) -> bool:
    return page.verdict.ok and (page.validation is None or page.validation["passed"])


def _browser_cookies(url: str, extra: dict[str, str] | None, resolve_gates: bool) -> list[dict]:
    host = normalize_url(url).host
    cookies = []
    if resolve_gates:
        for _recipe, domain, preset in presets_for(host):
            for k, v in preset.items():
                cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
    for k, v in (extra or {}).items():
        cookies.append({"name": k, "value": v, "domain": host, "path": "/"})
    return cookies


def _browser_result(url: str, data: dict[str, Any], gates: list[GateEvent]) -> FetchResult:
    html = data.get("html") or ""
    return FetchResult(
        requested_url=normalize_url(url).url,
        final_url=data.get("finalUrl") or url,
        status=int(data.get("status") or 0),
        headers={k.lower(): v for k, v in (data.get("headers") or {}).items()},
        body=html.encode("utf-8"),
        http_version=str(data.get("httpVersion") or ""),
        remote_ip=data.get("remoteIp") or "",
        hops=[],
        gates=gates,
        timing={"fetchMs": float(data.get("ms") or 0.0)},
        strategy=data.get("strategy") or "browser:chromium",
        extra={"browser": True},
    )


async def run_fetch(opts: FetchOptions, fetcher: HttpFetcher, browser: BrowserClient | None) -> dict[str, Any]:
    t0 = time.monotonic()
    attempts: list[dict] = []
    best: Page | None = None
    last_error: FetchError | None = None

    async def http_attempt(impersonate: str | None) -> Page | None:
        nonlocal last_error
        a0 = time.monotonic()
        try:
            res = await fetcher.fetch(
                opts.url, headers=opts.headers, cookies=opts.cookies, timeout=opts.timeout,
                resolve_gates=opts.resolve_gates, max_bytes=opts.max_bytes, impersonate=impersonate,
            )
        except FetchError as exc:
            last_error = exc
            attempts.append({"strategy": f"http:{impersonate or fetcher.settings.impersonate}",
                             "error": exc.code, "message": exc.message,
                             "ms": round((time.monotonic() - a0) * 1000, 1)})
            return None
        page = await asyncio.to_thread(_analyze, res, opts.expect)
        attempts.append({"strategy": res.strategy, "outcome": page.verdict.outcome, "statusCode": res.status,
                         "ms": round((time.monotonic() - a0) * 1000, 1),
                         **({"validationPassed": page.validation["passed"]} if page.validation else {})})
        return page

    async def browser_attempt() -> Page | None:
        if browser is None:
            return None
        a0 = time.monotonic()
        remaining = (opts.timeout or fetcher.settings.default_timeout) + 15
        try:
            data = await browser.render(
                normalize_url(opts.url).url,
                cookies=_browser_cookies(opts.url, opts.cookies, opts.resolve_gates),
                headers=opts.headers, timeout=min(remaining, fetcher.settings.max_timeout),
            )
        except Exception as exc:  # browser tier is best effort
            attempts.append({"strategy": "browser:chromium", "error": "browser_error",
                             "message": str(exc)[:300], "ms": round((time.monotonic() - a0) * 1000, 1)})
            return None
        if data.get("error"):
            attempts.append({"strategy": "browser:chromium", "error": data.get("error"),
                             "message": str(data.get("message", ""))[:300],
                             "ms": round((time.monotonic() - a0) * 1000, 1)})
            return None
        gates = []
        if opts.resolve_gates:
            gates = [GateEvent(r.name, r.kind, "preset", "browser started with the gate cookies")
                     for r, _d, _c in presets_for(normalize_url(opts.url).host)]
        res = _browser_result(opts.url, data, gates)
        page = await asyncio.to_thread(_analyze, res, opts.expect)
        attempts.append({"strategy": res.strategy, "outcome": page.verdict.outcome, "statusCode": res.status,
                         "ms": round((time.monotonic() - a0) * 1000, 1),
                         **({"validationPassed": page.validation["passed"]} if page.validation else {})})
        return page

    if opts.render == "always" and browser is not None:
        best = await browser_attempt()
    else:
        best = await http_attempt(None)
        refused = (best is not None and not best.verdict.ok and best.verdict.outcome in {"challenge", "blocked", "rate_limited"}) \
            or (best is None and last_error is not None and last_error.code in {"http2_error", "connection_error", "tls_error"})
        if refused:
            alt = await http_attempt(ALT_IMPERSONATE)
            if alt is not None and (best is None or _good(alt) or (not best.verdict.ok and alt.verdict.ok)):
                best = alt
        needs_browser = best is None or not _good(best)
        if needs_browser and opts.render == "auto" and browser is not None:
            if best is None or best.verdict.browser_may_help or (best.verdict.ok and not _good(best)):
                rendered = await browser_attempt()
                if rendered is not None and (best is None or _good(rendered) or
                                             (not best.verdict.ok and rendered.verdict.ok)):
                    best = rendered

    total_ms = round((time.monotonic() - t0) * 1000, 1)
    if best is None:
        err = last_error or FetchError("fetch_error", "the page could not be fetched")
        return {
            "ok": False, "outcome": "fetch_error", "error": err.code, "reason": err.message,
            "url": opts.url, "attempts": attempts,
            "gates": [g.as_dict() for g in err.gates],
            "redirects": [h.as_dict() for h in err.hops],
            "timing": {"totalMs": total_ms},
        }

    p0 = time.monotonic()
    envelope = await asyncio.to_thread(_render_outputs, best, opts)
    process_ms = round((time.monotonic() - p0) * 1000, 1)
    r = best.result
    envelope.update({
        "attempts": attempts,
        "timing": {**r.timing, "processMs": process_ms, "totalMs": round((time.monotonic() - t0) * 1000, 1)},
    })
    return envelope


def _render_outputs(page: Page, opts: FetchOptions) -> dict[str, Any]:
    r = page.result
    v = page.verdict
    ok = v.ok and (page.validation is None or page.validation["passed"])
    env: dict[str, Any] = {
        "ok": ok,
        "outcome": v.outcome if (page.validation is None or page.validation["passed"] or not v.ok) else "validation_failed",
        "reason": v.reason if (page.validation is None or page.validation["passed"]) else "expect checks failed",
        "vendor": v.vendor,
        "url": r.requested_url,
        "finalUrl": r.final_url,
        "statusCode": r.status,
        "strategy": r.strategy,
        "gates": [g.as_dict() for g in r.gates],
        "redirects": [h.as_dict() for h in r.hops[:-1]] if len(r.hops) > 1 else [],
        "http": {"version": r.http_version, "remoteIp": r.remote_ip},
        "contentType": page.content_type,
        "encoding": page.encoding,
        "encodingSource": page.encoding_source,
        "bytes": len(r.body),
        "visibleChars": len(page.visible),
    }
    if page.validation is not None:
        env["validation"] = page.validation

    want = set(opts.include) if opts.format == "json" else set()
    fmt = opts.format
    base = r.final_url
    if page.is_html:
        if fmt == "markdown" or "markdown" in want:
            env["markdown"] = extract.to_markdown(page.text, base, opts.markdown_mode)
        if fmt == "text" or "text" in want:
            env["text"] = extract.to_text(page.text)
        meta = None
        if "metadata" in want or opts.json_schema:
            meta = extract.page_metadata(page.tree, base)
            if "metadata" in want:
                env["metadata"] = meta
        ld = extract.json_ld(page.tree) if ("jsonLd" in want or opts.json_schema) else []
        md = extract.microdata(page.tree, base) if ("microdata" in want or opts.json_schema) else []
        if "jsonLd" in want:
            env["jsonLd"] = ld
        if "microdata" in want:
            env["microdata"] = md
        if "links" in want:
            env["links"] = extract.links(page.tree, base)
        if "hydration" in want:
            env["hydration"] = extract.hydration_data(page.tree)
        if opts.json_schema:
            t = time.monotonic()
            cands = build_candidates(jsonld=ld, microdata=md, metadata=meta or {},
                                     site=site_candidates(page.tree, base))
            filled = fill_schema(opts.json_schema, cands, base)
            filled["ms"] = round((time.monotonic() - t) * 1000, 2)
            env["extracted"] = filled
    elif _is_textual(page.content_type):
        if fmt in ("markdown", "text") or "markdown" in want or "text" in want:
            key = "markdown" if (fmt == "markdown" or "markdown" in want) else "text"
            env[key] = page.text
    if (fmt == "html" or "html" in want) and (page.is_html or _is_textual(page.content_type)):
        env["html"] = page.text
    if fmt == "raw":
        env["_raw"] = r.body  # popped by the API layer; never serialized
    return env


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return (not ct or ct.startswith("text/") or "json" in ct or "xml" in ct or "javascript" in ct
            or "x-www-form-urlencoded" in ct)
