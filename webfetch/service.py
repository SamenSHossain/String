"""The fetch pipeline: fetch -> decode -> classify -> (escalate) -> validate -> extract."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import extract
from .classify import Verdict, classify
from .decode import decode_body
from .fetcher import FetchError, FetchResult, Hop, HttpFetcher
from .gates import AGE_PATH_PATTERNS, CONSENT_PATH_PATTERNS, CONSENT_URL_PATTERNS, GateEvent, presets_for
from .netguard import CROSS_ORIGIN_SAFE_HEADERS, cross_origin_headers, normalize_url
from .schema_fill import build_candidates, fill_schema, site_candidates

JSON_INCLUDES = {"markdown", "html", "text", "metadata", "jsonLd", "microdata", "links", "hydration"}
DEFAULT_INCLUDES = {"markdown", "metadata"}
# Second TLS/HTTP2 fingerprint tried when the first one is refused.
ALT_IMPERSONATE = "safari"


class BrowserClient(Protocol):
    async def render(self, url: str, *, cookies: list[dict], headers: dict[str, str] | None,
                     timeout: float, max_bytes: int = ...) -> dict[str, Any]: ...


MAX_BROWSER_PER_CLIENT = 2
_active_renders: dict[str, int] = {}


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
    client: str = "anonymous"  # set by the API layer; not user input


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
        try:
            found = bool(page.tree is not None and page.tree.css_first(expect.selector) is not None)
        except Exception:
            found = False
        checks.append({"check": "selector", "value": expect.selector, "passed": found})
    if expect.min_chars is not None:
        checks.append({"check": "minChars", "value": expect.min_chars, "actual": len(page.visible),
                       "passed": len(page.visible) >= expect.min_chars})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


MAX_PARSE_CHARS = 6_000_000
MAX_NESTING = 1500
MAX_CONCURRENT_ANALYSIS = 4
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]{0,15})(?=[\s/>])")
# Only HTML container/formatting elements count toward depth: SVG children (<path/>,
# <use/>) self-close, and p/li/td-style elements are routinely left unclosed.
_COUNTED = {"div", "span", "section", "article", "aside", "nav", "header", "footer", "main", "ul", "ol", "dl",
            "table", "form", "b", "i", "u", "s", "em", "strong", "small", "big", "font", "a", "blockquote",
            "center", "figure", "figcaption", "label", "button", "select", "details", "summary", "fieldset",
            "code", "pre", "q", "cite", "abbr", "sub", "sup", "mark", "del", "ins", "h1", "h2", "h3", "h4", "h5",
            "h6", "picture", "object", "nobr", "tt", "strike", "dfn", "kbd", "samp", "var", "bdo", "bdi", "time",
            "address", "hgroup", "search", "menu", "map", "canvas", "video", "audio", "legend", "output"}
_analysis_slots: dict[int, asyncio.Semaphore] = {}


class TooComplex(Exception):
    pass


def _check_nesting(text: str) -> None:
    """HTML parsers are quadratic on pathological nesting (100k nested <div>s take
    minutes); browsers cap the DOM depth for the same reason. Refuse such pages early with
    one linear scan."""
    if len(text) < 100_000:
        return
    depth = 0
    for m in _TAG.finditer(_strip_raw_text(text)):
        name = m.group(2).lower()
        if name not in _COUNTED:
            continue
        if m.group(1):
            depth = depth - 1 if depth else 0
        else:
            depth += 1
            if depth > MAX_NESTING:
                raise TooComplex(f"page nests elements more than {MAX_NESTING} levels deep")


def _strip_raw_text(text: str) -> str:
    """Drop <script>/<style> contents (where '<d' or '<i' are code, not tags), linearly."""
    low = text.lower()
    out, pos = [], 0
    while True:
        starts = [i for i in (low.find("<script", pos), low.find("<style", pos)) if i >= 0]
        if not starts:
            out.append(text[pos:])
            return "".join(out)
        start = min(starts)
        closer = "</script" if low.startswith("<script", start) else "</style"
        out.append(text[pos:start])
        end = low.find(closer, start)
        if end < 0:
            return "".join(out)
        pos = end + len(closer)


async def _bounded(fn, *args):
    """Run CPU-heavy page work in a thread, at most a few at a time per process."""
    loop = asyncio.get_running_loop()
    sem = _analysis_slots.setdefault(id(loop), asyncio.Semaphore(MAX_CONCURRENT_ANALYSIS))
    async with sem:
        return await asyncio.to_thread(fn, *args)


def _analyze(result: FetchResult, expect: Expect | None) -> Page:
    ctype = result.headers.get("content-type", "")
    text, enc, enc_src = decode_body(result.body, ctype)
    if len(text) > MAX_PARSE_CHARS:
        text = text[:MAX_PARSE_CHARS]
    _check_nesting(text)
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


def _header_cookies(headers: dict[str, str] | None) -> dict[str, str]:
    """The caller's Cookie header as name -> value."""
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if key.lower() == "cookie":
            for pair in value.split(";"):
                name, sep, val = pair.partition("=")
                if sep and name.strip():
                    out[name.strip()] = val.strip()
    return out


def _dropped_headers(headers: dict[str, str] | None) -> list[str]:
    """Caller headers the browser tier does not send. Chromium carries request headers
    across a redirect to another origin, so origin-bound ones (Authorization, API keys,
    custom headers) stay out of it; the Cookie header goes in as host-scoped cookies."""
    names = (k.lower() for k in headers or {})
    return list(dict.fromkeys(n for n in names if n not in CROSS_ORIGIN_SAFE_HEADERS and n != "cookie"))


def _browser_result(url: str, data: dict[str, Any], gates: list[GateEvent]) -> FetchResult:
    html = data.get("html") or ""
    final_url = data.get("finalUrl") or url
    status = int(data.get("status") or 0)
    # The redirects Chromium followed, then the page itself (the envelope lists all but it).
    hops = [Hop(str(h.get("url") or ""), int(h.get("status") or 0), h.get("location"), float(h.get("ms") or 0.0))
            for h in data.get("redirects") or [] if isinstance(h, dict)]
    hops.append(Hop(final_url, status, None, 0.0))
    return FetchResult(
        requested_url=normalize_url(url).url,
        final_url=final_url,
        status=status,
        headers={k.lower(): v for k, v in (data.get("headers") or {}).items()},
        body=html.encode("utf-8"),
        http_version=str(data.get("httpVersion") or ""),
        remote_ip=data.get("remoteIp") or "",
        hops=hops,
        gates=gates,
        timing={"fetchMs": float(data.get("ms") or 0.0)},
        strategy=data.get("strategy") or "browser:chromium",
        extra={"browser": True},
    )


MIN_ESCALATION_SECONDS = 2.0


def _wall_in_redirect_loop(err: FetchError) -> tuple[str, str] | None:
    """(outcome, reason) when the redirects that ran out pass through an age check or a
    consent page: the destination sends every visitor there and back without serving the
    page (Steam does this between /app/ and /agecheck/ in regions without the age rule)."""
    if err.code != "too_many_redirects":
        return None
    for hop in err.hops[1:]:  # detours only: the requested URL itself may match a pattern
        parts = urlsplit(hop.url)
        host = (parts.hostname or "").lower()
        if AGE_PATH_PATTERNS.search(parts.path):
            return "age_gate", f"destination keeps redirecting to its age check ({parts.path})"
        if CONSENT_URL_PATTERNS.search(host) or CONSENT_PATH_PATTERNS.search(parts.path):
            return "consent_wall", f"destination keeps redirecting to its consent page ({host}{parts.path})"
    return None


async def run_fetch(opts: FetchOptions, fetcher: HttpFetcher, browser: BrowserClient | None) -> dict[str, Any]:
    t0 = time.monotonic()
    # One budget for the whole request: every attempt gets what is left of it.
    budget = min(opts.timeout or fetcher.settings.default_timeout, fetcher.settings.max_timeout)
    deadline = t0 + budget

    def left() -> float:
        return deadline - time.monotonic()
    attempts: list[dict] = []
    best: Page | None = None
    last_error: FetchError | None = None
    last_error_strategy = ""

    async def http_attempt(impersonate: str | None) -> Page | None:
        nonlocal last_error, last_error_strategy
        a0 = time.monotonic()
        strategy = f"http:{impersonate or fetcher.settings.impersonate}"
        try:
            res = await fetcher.fetch(
                opts.url, headers=opts.headers, cookies=opts.cookies, timeout=max(left(), 0.5),
                resolve_gates=opts.resolve_gates, max_bytes=opts.max_bytes, impersonate=impersonate,
            )
        except FetchError as exc:
            last_error, last_error_strategy = exc, strategy
            attempts.append({"strategy": strategy, "error": exc.code, "message": exc.message,
                             "ms": round((time.monotonic() - a0) * 1000, 1)})
            return None
        try:
            page = await _bounded(_analyze, res, opts.expect)
        except TooComplex as exc:
            last_error, last_error_strategy = FetchError("too_complex", str(exc)), res.strategy
            attempts.append({"strategy": res.strategy, "error": "too_complex", "message": str(exc),
                             "ms": round((time.monotonic() - a0) * 1000, 1)})
            return None
        attempts.append({"strategy": res.strategy, "outcome": page.verdict.outcome, "statusCode": res.status,
                         "ms": round((time.monotonic() - a0) * 1000, 1),
                         **({"validationPassed": page.validation["passed"]} if page.validation else {})})
        return page

    async def browser_attempt() -> Page | None:
        if browser is None:
            return None
        a0 = time.monotonic()
        if _active_renders.get(opts.client, 0) >= MAX_BROWSER_PER_CLIENT:
            attempts.append({"strategy": "browser:chromium", "error": "browser_busy",
                             "message": f"at most {MAX_BROWSER_PER_CLIENT} concurrent browser renders per client",
                             "ms": 0.0})
            return None
        # Only cross-origin-safe headers go to the browser; a Cookie header becomes cookies
        # for the requested host (the cookies field wins a name clash).
        dropped = _dropped_headers(opts.headers)
        note = {"droppedHeaders": dropped} if dropped else {}
        cookies = {**_header_cookies(opts.headers), **(opts.cookies or {})}
        _active_renders[opts.client] = _active_renders.get(opts.client, 0) + 1
        try:
            data = await asyncio.wait_for(browser.render(
                normalize_url(opts.url).url,
                cookies=_browser_cookies(opts.url, cookies, opts.resolve_gates),
                headers=cross_origin_headers(opts.headers) or None, timeout=max(left() - 1.0, 1.0),
                max_bytes=opts.max_bytes or fetcher.settings.max_bytes,
            ), timeout=max(left(), 1.0))
        except Exception as exc:  # browser tier is best effort
            attempts.append({"strategy": "browser:chromium", "error": "browser_error",
                             "message": str(exc)[:300], "ms": round((time.monotonic() - a0) * 1000, 1), **note})
            return None
        finally:
            _active_renders[opts.client] -= 1
            if _active_renders[opts.client] <= 0:
                _active_renders.pop(opts.client, None)
        if data.get("error"):
            attempts.append({"strategy": "browser:chromium", "error": data.get("error"),
                             "message": str(data.get("message", ""))[:300],
                             "ms": round((time.monotonic() - a0) * 1000, 1), **note})
            return None
        gates = []
        if opts.resolve_gates:
            gates = [GateEvent(r.name, r.kind, "preset", "browser started with the gate cookies")
                     for r, _d, _c in presets_for(normalize_url(opts.url).host)]
        res = _browser_result(opts.url, data, gates)
        try:
            page = await _bounded(_analyze, res, opts.expect)
        except TooComplex as exc:
            attempts.append({"strategy": res.strategy, "error": "too_complex", "message": str(exc),
                             "ms": round((time.monotonic() - a0) * 1000, 1), **note})
            return None
        attempts.append({"strategy": res.strategy, "outcome": page.verdict.outcome, "statusCode": res.status,
                         "ms": round((time.monotonic() - a0) * 1000, 1),
                         **({"validationPassed": page.validation["passed"]} if page.validation else {}), **note})
        return page

    if opts.render == "always" and browser is not None:
        best = await browser_attempt()
    else:
        best = await http_attempt(None)
        refused = (best is not None and not best.verdict.ok and best.verdict.outcome in {"challenge", "blocked", "rate_limited"}) \
            or (best is None and last_error is not None and last_error.code in {"http2_error", "connection_error", "tls_error"})
        if best is None and last_error is not None and last_error.code in {"too_complex", "too_large"}:
            refused = False
        if refused and left() > MIN_ESCALATION_SECONDS:
            alt = await http_attempt(ALT_IMPERSONATE)
            if alt is not None and (best is None or _good(alt) or (not best.verdict.ok and alt.verdict.ok)):
                best = alt
        needs_browser = (best is None or not _good(best)) and not (
            best is None and last_error is not None and last_error.code in {"too_complex", "too_large"})
        if needs_browser and opts.render == "auto" and browser is not None and left() > MIN_ESCALATION_SECONDS:
            if best is None or best.verdict.browser_may_help or (best.verdict.ok and not _good(best)):
                rendered = await browser_attempt()
                if rendered is not None and (best is None or _good(rendered) or
                                             (not best.verdict.ok and rendered.verdict.ok)):
                    best = rendered

    total_ms = round((time.monotonic() - t0) * 1000, 1)
    if best is None:
        err = last_error or FetchError("fetch_error", "the page could not be fetched")
        wall = _wall_in_redirect_loop(err)
        if wall is not None:
            last = err.hops[-1]
            return {
                "ok": False, "outcome": wall[0], "reason": wall[1], "vendor": None,
                "url": normalize_url(opts.url).url, "finalUrl": last.url, "statusCode": last.status,
                "strategy": last_error_strategy, "gates": [g.as_dict() for g in err.gates],
                "redirects": [h.as_dict() for h in err.hops], "attempts": attempts,
                "timing": {"totalMs": total_ms},
            }
        return {
            "ok": False, "outcome": "fetch_error", "error": err.code, "reason": err.message,
            # No destination answer, so no statusCode; how far it got, and with what:
            "url": opts.url, "finalUrl": err.hops[-1].url if err.hops else normalize_url(opts.url).url,
            "strategy": attempts[-1]["strategy"] if attempts else None,
            "attempts": attempts,
            "gates": [g.as_dict() for g in err.gates],
            "redirects": [h.as_dict() for h in err.hops],
            "timing": {"totalMs": total_ms},
        }

    p0 = time.monotonic()
    envelope = await _bounded(_render_outputs, best, opts)
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
    validation_failed = v.ok and page.validation is not None and not page.validation["passed"]
    env: dict[str, Any] = {
        "ok": ok,
        "outcome": "validation_failed" if validation_failed else v.outcome,
        "reason": "expect checks failed" if validation_failed else v.reason,
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

        def part(name: str, fn, *args, keep: bool):
            """One structured part, computed in isolation: if it fails (say, JSON-LD nested
            10,000 levels deep), the response is served without it and a warning says why."""
            try:
                value = fn(*args)
            except Exception as exc:
                env.setdefault("warnings", []).append({"part": name, "message": f"{type(exc).__name__}: {exc}"[:300]})
                return None
            if keep:
                env[name] = value
            return value

        meta = part("metadata", extract.page_metadata, page.tree, base, keep="metadata" in want) \
            if "metadata" in want or opts.json_schema else None
        ld = part("jsonLd", extract.json_ld, page.tree, keep="jsonLd" in want) \
            if "jsonLd" in want or opts.json_schema else []
        md = part("microdata", extract.microdata, page.tree, base, keep="microdata" in want) \
            if "microdata" in want or opts.json_schema else []
        if "links" in want:
            part("links", extract.links, page.tree, base, keep=True)
        if "hydration" in want:
            part("hydration", extract.hydration_data, page.tree, keep=True)
        if opts.json_schema:
            # A part that failed above is simply not a source here.
            part("extracted", _fill, page, opts.json_schema, ld or [], md or [], meta or {}, base, keep=True)
    elif _is_textual(page.content_type):
        if fmt == "markdown" or "markdown" in want:
            env["markdown"] = page.text
        if fmt == "text" or "text" in want:
            env["text"] = page.text
    elif fmt in ("markdown", "text", "html"):
        env["_unsupported"] = True  # binary (PDF, image, ...): the API answers 415
    if (fmt == "html" or "html" in want) and (page.is_html or _is_textual(page.content_type)):
        env["html"] = page.text
    if fmt == "raw":
        env["_raw"] = r.body  # popped by the API layer; never serialized
    return env


def _fill(page: Page, schema: dict, ld: list, md: list, meta: dict, base: str) -> dict[str, Any]:
    t = time.monotonic()
    cands = build_candidates(jsonld=ld, microdata=md, metadata=meta, site=site_candidates(page.tree, base))
    filled = fill_schema(schema, cands, base)
    filled["ms"] = round((time.monotonic() - t) * 1000, 2)
    return filled


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return (not ct or ct.startswith("text/") or "json" in ct or "xml" in ct or "javascript" in ct
            or "x-www-form-urlencoded" in ct)
