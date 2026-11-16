"""FastAPI application.

``POST /v1/fetch`` accepts the same core body as String's ``/v1/fetch`` (``url``,
``format``, ``jsonSchema``, ``executeJS``, ``headers``, ``markdownMode``,
``mainContentOnly``) and answers with the same headers (``x-request-id``,
``x-status-code``, ``x-final-url``), so a String client can point at it unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .config import Settings, load_settings
from .fetcher import HttpFetcher
from .netguard import BlockedDestination, InvalidUrl, normalize_url, resolve_public
from .service import JSON_INCLUDES, BrowserClient, Expect, FetchOptions, run_fetch

log = logging.getLogger("webfetch")

# Outcomes that mean "we could not get past what the destination served".
FAILED_OUTCOMES = {"challenge", "blocked", "consent_wall", "age_gate", "login_wall", "js_required",
                   "empty", "validation_failed", "rate_limited"}
# Python codec name -> the label browsers and HTTP clients expect in Content-Type.
_CHARSET_LABELS = {"cp932": "Shift_JIS", "gb18030": "GB18030", "big5hkscs": "Big5", "cp949": "EUC-KR",
                   "cp1252": "windows-1252", "cp1251": "windows-1251", "cp1250": "windows-1250",
                   "cp1253": "windows-1253", "cp1254": "windows-1254", "cp874": "windows-874",
                   "koi8-r": "KOI8-R", "euc_jp": "EUC-JP", "utf-8": "utf-8"}
FORBIDDEN_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "te", "upgrade",
                     "keep-alive", "trailer", "proxy-authorization", "proxy-authenticate", "proxy-connection"}
MAX_SCHEMA_BYTES = 50_000
MAX_SCHEMA_KEYS = 200
MAX_SCHEMA_DEPTH = 10
MAX_BATCH = 20
BROWSER_COST = 5  # a headless-browser render uses ~5x the resources of an HTTP fetch


def _schema_size(node: Any, depth: int = 0) -> tuple[int, int]:
    """(number of keys, nesting depth) of a JSON value."""
    if isinstance(node, dict):
        keys, deepest = len(node), depth
        for v in node.values():
            k, d = _schema_size(v, depth + 1)
            keys, deepest = keys + k, max(deepest, d)
        return keys, deepest
    if isinstance(node, list):
        keys, deepest = 0, depth
        for v in node:
            k, d = _schema_size(v, depth + 1)
            keys, deepest = keys + k, max(deepest, d)
        return keys, deepest
    return 0, depth


class ExpectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contains: list[str] | str | None = None
    notContains: list[str] | str | None = None
    selector: str | None = Field(None, max_length=500)
    minChars: int | None = Field(None, ge=0, le=50_000_000)

    @field_validator("selector")
    @classmethod
    def _selector(cls, v):
        if v is None:
            return v
        from selectolax.lexbor import LexborHTMLParser
        try:
            LexborHTMLParser("<p></p>").css_first(v)
        except Exception as exc:
            raise ValueError(f"invalid CSS selector: {exc}") from exc
        return v


class FetchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=8192)
    format: Literal["json", "markdown", "html", "text", "raw"] = "json"
    markdownMode: Literal["full", "readable"] = "full"
    mainContentOnly: bool | None = None
    include: list[str] | None = None
    jsonSchema: dict[str, Any] | None = None
    expect: ExpectModel | None = None
    resolveGates: bool = True
    render: Literal["auto", "never", "always"] = "auto"
    executeJS: bool | None = None
    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    timeout: float | None = Field(None, gt=0, le=90)
    maxBytes: int | None = Field(None, gt=0, le=10 * 1024 * 1024)

    @field_validator("include")
    @classmethod
    def _include(cls, v):
        if v is None:
            return v
        bad = [x for x in v if x not in JSON_INCLUDES]
        if bad:
            raise ValueError(f"unknown include value(s) {bad}; allowed: {sorted(JSON_INCLUDES)}")
        return v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v):
        if v is None:
            return v
        if len(v) > 50:
            raise ValueError("at most 50 headers")
        for k, val in v.items():
            if k.lower() in FORBIDDEN_HEADERS:
                raise ValueError(f"header {k!r} is managed by the service")
            if any(c in k + val for c in "\r\n\x00") or any(ord(c) > 0xFF for c in val):
                raise ValueError(f"header {k!r} contains forbidden characters")
        return v

    @field_validator("jsonSchema")
    @classmethod
    def _schema(cls, v):
        if v is None:
            return v
        raw = json.dumps(v)
        if len(raw) > MAX_SCHEMA_BYTES:
            raise ValueError(f"jsonSchema is larger than {MAX_SCHEMA_BYTES} bytes")
        if v.get("type") not in ("object", None) or not isinstance(v.get("properties"), dict):
            raise ValueError('jsonSchema must be an object schema with "properties"')
        keys, depth = _schema_size(v)
        if keys > MAX_SCHEMA_KEYS or depth > MAX_SCHEMA_DEPTH:
            raise ValueError(f"jsonSchema is limited to {MAX_SCHEMA_KEYS} keys and nesting depth {MAX_SCHEMA_DEPTH}")
        return v

    def to_options(self) -> FetchOptions:
        render = self.render
        if self.executeJS is True:
            render = "always"
        elif self.executeJS is False and render == "auto":
            render = "never"
        include = set(self.include) if self.include is not None else {"markdown", "metadata"}
        exp = None
        if self.expect is not None:
            def _list(x):
                return [x] if isinstance(x, str) else list(x or [])
            exp = Expect(contains=_list(self.expect.contains), not_contains=_list(self.expect.notContains),
                         selector=self.expect.selector, min_chars=self.expect.minChars)
        return FetchOptions(
            url=self.url,
            format=self.format,
            markdown_mode="readable" if self.mainContentOnly else self.markdownMode,
            include=include,
            json_schema=self.jsonSchema,
            expect=exp,
            resolve_gates=self.resolveGates,
            render=render,
            headers=self.headers,
            cookies=self.cookies,
            timeout=self.timeout,
            max_bytes=self.maxBytes,
        )


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[FetchBody] = Field(..., min_length=1, max_length=MAX_BATCH)
    concurrency: int = Field(5, ge=1, le=10)


class RateLimiter:
    """Token bucket per caller (API key or client IP), per container."""

    def __init__(self):
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, per_minute: int, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        rate = per_minute / 60.0
        tokens, last = self.buckets.get(key, (float(per_minute), now))
        tokens = min(float(per_minute), tokens + (now - last) * rate)
        if tokens >= cost:
            self.buckets[key] = (tokens - cost, now)
            return True, 0.0
        self.buckets[key] = (tokens, now)
        if len(self.buckets) > 20_000:
            self.buckets.clear()
        return False, (cost - tokens) / rate


def _client_ip(request: Request) -> str:
    # The platform proxy appends the connecting address; earlier entries are whatever the
    # client sent, so only the rightmost one is trustworthy for rate limiting.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _api_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


def _error(status: int, error: str, message: str, rid: str, **extra) -> JSONResponse:
    return JSONResponse({"error": error, "message": message, "requestId": rid, **extra}, status_code=status,
                        headers={"x-request-id": rid})


def _server_timing(t: dict) -> str:
    parts = []
    for key, name in (("dnsMs", "dns"), ("connectMs", "connect"), ("tlsMs", "tls"), ("ttfbMs", "ttfb"),
                      ("downloadMs", "download"), ("redirectMs", "redirects"), ("fetchMs", "fetch"),
                      ("processMs", "process"), ("totalMs", "total")):
        if key in t:
            parts.append(f"{name};dur={t[key]}")
    return ", ".join(parts)


def create_app(settings: Settings | None = None, browser: BrowserClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    fetcher = HttpFetcher(settings)
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await fetcher.aclose()

    app = FastAPI(
        title="webfetch",
        version=__version__,
        description="Fetch any public page through consent walls and age gates, with honest outcomes, "
                    "clean Markdown and schema-shaped data with provenance.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.fetcher = fetcher
    app.state.browser = browser

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "rid", str(uuid.uuid4()))
        issues = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]) or "body", "message": e.get("msg", "")}
                  for e in exc.errors()]
        return _error(400, "validation_error", issues[0]["message"] if issues else "invalid request", rid, issues=issues)

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        rid = str(uuid.uuid4())
        request.state.rid = rid
        t0 = time.monotonic()
        response = await call_next(request)
        ms = round((time.monotonic() - t0) * 1000, 1)
        response.headers["x-request-id"] = rid
        response.headers["x-request-duration-ms"] = str(ms)
        if request.url.path.startswith("/v1/"):
            # Fetched pages are third-party content: never let them run on this origin.
            response.headers["content-security-policy"] = "sandbox; default-src 'none'"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["x-frame-options"] = "DENY"
        if request.url.path.startswith("/v1/"):
            log.info(json.dumps({"rid": rid, "path": request.url.path, "status": response.status_code,
                                 "outcome": response.headers.get("x-outcome"), "ms": ms}))
        return response

    def _client_id(request: Request) -> str:
        key = _api_key(request)
        return f"key:{key}" if key and key in settings.api_keys else f"ip:{_client_ip(request)}"

    def _auth(request: Request, cost: int = 1) -> JSONResponse | None:
        rid = request.state.rid
        key = _api_key(request)
        if settings.api_keys and key not in settings.api_keys:
            # With WEBFETCH_API_KEYS set, every request needs one of the keys.
            return _error(401, "unauthorized", "a valid API key is required", rid)
        keyed = key is not None and key in settings.api_keys
        ident = f"key:{key}" if keyed else f"ip:{_client_ip(request)}"
        allowed, wait = limiter.allow(ident, settings.key_rate_per_min if keyed else settings.anon_rate_per_min, cost)
        if not allowed:
            resp = _error(429, "rate_limited", f"too many requests; retry in {wait:.1f}s", rid)
            resp.headers["retry-after"] = str(max(1, int(wait + 0.999)))
            return resp
        return None

    async def _do_fetch(body: FetchBody, rid: str, client: str = "anonymous") -> Response:
        opts = body.to_options()
        opts.client = client
        try:
            target = normalize_url(opts.url)
            if settings.proxy is None:
                # Same 403 whichever tier would run (the browser tier has its own egress check too).
                try:
                    await resolve_public(target.host, target.port, allow_private=settings.allow_private)
                except InvalidUrl:
                    pass  # DNS failure: the fetch itself reports it as dns_error
            env = await run_fetch(opts, fetcher, app.state.browser)
        except InvalidUrl as exc:
            return _error(400, "invalid_url", str(exc), rid)
        except BlockedDestination as exc:
            return _error(403, "blocked_destination", str(exc), rid)
        raw = env.pop("_raw", None)
        env = {"requestId": rid, **env}
        outcome = env.get("outcome", "")
        hdrs = {"x-request-id": rid, "x-outcome": outcome}
        if "statusCode" in env:
            hdrs["x-status-code"] = str(env["statusCode"])
        if env.get("finalUrl"):
            hdrs["x-final-url"] = env["finalUrl"].encode("ascii", "backslashreplace").decode("ascii")
        if env.get("strategy"):
            hdrs["x-strategy"] = env["strategy"]
        if env.get("gates"):
            hdrs["x-gates"] = ", ".join(f"{g['site']}:{g['kind']}:{g['trigger']}" for g in env["gates"])
        if env.get("timing"):
            hdrs["server-timing"] = _server_timing(env["timing"])

        if outcome == "fetch_error":
            status = 504 if env.get("error") == "timeout" else 502
            return JSONResponse(env, status_code=status, headers=hdrs)
        if env.pop("_unsupported", False) and body.format in ("markdown", "text", "html"):
            env.update({"error": "unsupported_content",
                        "message": f"{env.get('contentType') or 'this content'} cannot be returned as {body.format}; "
                                   "use format raw or json"})
            return JSONResponse(env, status_code=415, headers=hdrs)
        failed = outcome in FAILED_OUTCOMES
        if body.format == "json":
            return JSONResponse(env, status_code=502 if failed else 200, headers=hdrs)
        if failed:
            env.pop("markdown", None), env.pop("text", None), env.pop("html", None)
            return JSONResponse(env, status_code=502, headers=hdrs)
        if body.format == "markdown":
            return Response(env.get("markdown") or "", media_type="text/markdown; charset=utf-8", headers=hdrs)
        if body.format == "text":
            return Response(env.get("text") or "", media_type="text/plain; charset=utf-8", headers=hdrs)
        if body.format == "raw":
            hdrs["x-encoding"] = env.get("encoding", "")
            ctype = env.get("contentType") or "application/octet-stream"
            if "charset=" not in ctype.lower() and (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
                # Original bytes in their original encoding: label it, or clients assume UTF-8.
                ctype = f"{ctype}; charset={_CHARSET_LABELS.get(env.get('encoding', ''), env.get('encoding') or 'utf-8')}"
            hdrs["content-type"] = ctype
            hdrs["content-disposition"] = "attachment"
            return Response(raw or b"", headers=hdrs)
        return Response((env.get("html") or "").encode("utf-8"), media_type="text/html; charset=utf-8", headers=hdrs)

    @app.get("/")
    async def index():
        return {
            "name": "webfetch",
            "version": __version__,
            "endpoints": {
                "POST /v1/fetch": "fetch one URL (JSON body; String /v1/fetch compatible)",
                "GET /v1/fetch?url=...&format=markdown": "same, as a GET",
                "POST /v1/batch": f"up to {MAX_BATCH} fetches in one call",
                "GET /docs": "interactive OpenAPI docs",
            },
            "example": {"url": "https://store.steampowered.com/app/271590/", "format": "json",
                        "jsonSchema": {"type": "object", "properties": {"name": {"type": "string"},
                                                                         "price": {"type": "number"}}}},
            "source": "https://github.com/SamenSHossain/String",
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__, "browser": app.state.browser is not None}

    @app.post("/v1/fetch")
    async def fetch_post(body: FetchBody, request: Request):
        denied = _auth(request, cost=BROWSER_COST if body.to_options().render == "always" else 1)
        if denied is not None:
            return denied
        return await _do_fetch(body, request.state.rid, _client_id(request))

    @app.get("/v1/fetch")
    async def fetch_get(request: Request, url: str, format: str = "json", markdownMode: str = "full",
                        render: str = "auto", resolveGates: bool = True, include: str | None = None):
        denied = _auth(request)
        if denied is not None:
            return denied
        try:
            body = FetchBody(url=url, format=format, markdownMode=markdownMode, render=render,
                             resolveGates=resolveGates,
                             include=[x.strip() for x in include.split(",") if x.strip()] if include else None)
        except Exception as exc:  # pydantic ValidationError
            return _error(400, "validation_error", str(exc).splitlines()[0][:300], request.state.rid)
        if render == "always":
            denied = _auth(request, cost=BROWSER_COST - 1)  # one token was already taken above
            if denied is not None:
                return denied
        return await _do_fetch(body, request.state.rid, _client_id(request))

    @app.post("/v1/batch")
    async def batch(body: BatchBody, request: Request):
        denied = _auth(request, cost=sum(BROWSER_COST if r.to_options().render == "always" else 1
                                         for r in body.requests))
        if denied is not None:
            return denied
        rid = request.state.rid
        sem = asyncio.Semaphore(body.concurrency)

        async def one(i: int, item: FetchBody) -> dict:
            async with sem:
                if item.format != "json":
                    item = item.model_copy(update={"format": "json"})
                resp = await _do_fetch(item, f"{rid}:{i}", _client_id(request))
                return json.loads(resp.body)

        t0 = time.monotonic()
        results = await asyncio.gather(*(one(i, r) for i, r in enumerate(body.requests)))
        return JSONResponse({"requestId": rid, "ms": round((time.monotonic() - t0) * 1000, 1),
                             "results": results}, headers={"x-request-id": rid})

    return app
