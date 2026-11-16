"""Decide what a response actually is, so success is never reported for a wall.

Outcomes:
  ok            the requested page
  challenge     an anti-bot interstitial (JS/captcha) instead of the page
  blocked       an anti-bot or WAF refusal page
  consent_wall  a cookie/consent interstitial that was not resolved
  age_gate      an age-verification interstitial that was not resolved
  login_wall    redirected to a sign-in page
  js_required   an empty JavaScript shell (needs a browser to render)
  empty         no body at all
  not_found     404/410
  rate_limited  429
  http_error    other 4xx
  server_error  5xx
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .gates import AGE_PATH_PATTERNS, CONSENT_PATH_PATTERNS, CONSENT_URL_PATTERNS

# (vendor, outcome, pattern). Matched against the lower-cased first 200 KB of the body.
# "strong" signatures only appear on interstitials; "weak" ones also appear on normal
# pages (e.g. Cloudflare injects challenge-platform scripts everywhere), so they only
# count when the page has little visible text or the status is a refusal.
# (vendor, outcome, pattern), matched against the lower-cased first 200 KB of the body.
# Interstitials are small pages, so text signatures only count on refusal statuses or
# pages with little visible text: an article that *mentions* "Incapsula incident ID" or
# quotes errors.edgesuite.net must not be mistaken for the interstitial itself.
_TITLE_CHALLENGE = re.compile(r"<title>\s*just a moment\.\.\.\s*</title>")
_STRONG = [
    ("cloudflare", "challenge", re.compile(r"cf_chl_opt|cf-browser-verification|<div[^>]+id=\"challenge-(form|running|stage)\"")),
    ("cloudflare", "blocked", re.compile(r"<title>\s*attention required! \| cloudflare\s*</title>")),
    ("datadome", "challenge", re.compile(r"geo\.captcha-delivery\.com|ct\.captcha-delivery\.com|interstitial\.captcha-delivery\.com")),
    ("perimeterx", "challenge", re.compile(r"px-captcha|_pxcaptcha|captcha\.px-cdn\.net|<title>\s*access to this page has been denied")),
    ("akamai", "blocked", re.compile(r"errors\.edgesuite\.net|<title>\s*access denied\s*</title>[\s\S]{0,3000}reference(&#32;| )#")),
    ("akamai", "challenge", re.compile(r"sec-if-cpt-container|/_sec/cp_challenge/|bm-verify")),
    # Imperva injects /_Incapsula_Resource scripts into normal pages; only its block page has these.
    ("imperva", "challenge", re.compile(r"incapsula incident id|_incapsula_resource\?cwudnsai=")),
    ("aws-waf", "challenge", re.compile(r"awswafcookiedomainlist|window\.gokuprops|aws-waf-token")),
    ("sucuri", "challenge", re.compile(r"sucuri website firewall|sucuri_cloudproxy_js")),
    ("ddos-guard", "challenge", re.compile(r"check\.ddos-guard\.net|<title>\s*ddos-guard\s*</title>")),
    ("ozon", "blocked", re.compile(r"abt-challenge")),
    ("centinel", "challenge", re.compile(r"centinelanalytica\.com|window\.__centinel")),
]
# Scripts that many sites load on every page (Cloudflare's JS detections, reCAPTCHA v3):
# only meaningful on a refusal or an almost empty page.
_WEAK = [
    ("cloudflare", "challenge", re.compile(r"/cdn-cgi/challenge-platform/|challenges\.cloudflare\.com/turnstile")),
    ("kasada", "challenge", re.compile(r"kpsdk|x-kpsdk")),
    ("captcha", "challenge", re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com/1/api\.js|recaptcha/api\.js")),
    ("generic", "blocked", re.compile(r"<title>\s*(access denied|forbidden|request blocked|403 forbidden)\s*</title>")),
]
_ROOT_SHELL = re.compile(r"""<div[^>]+id=["'](root|app|__next|__nuxt|main|application|svelte)["'][^>]*>\s*</div>""")
_JS_REQUIRED = re.compile(
    r"(enable|activate|turn on)\s+javascript|javascript\s+(is\s+)?(required|disabled|must be enabled)|"
    r"requires\s+javascript|you need to enable javascript",
    re.I,
)
_JS_URL = re.compile(r"(javascript[_-]?(disabled|required)|/nojs|/no-js|enable[_-]?javascript)", re.I)
# Soft rate limits served as 200 pages (e.g. Hacker News: "Sorry, we're not able to serve
# your requests this quickly").
_SOFT_LIMIT = re.compile(
    r"not able to serve your requests this quickly|too many requests|rate limit(ed)? exceeded|"
    r"you are being rate limited|slow down, you're making too many requests",
    re.I,
)
_LOGIN_PATH = re.compile(r"/(login|signin|sign-in|log-in|auth/login|accounts/login|session/new|users/sign_in)(/|$|\?|\.)", re.I)
_CONSENT_TITLE = re.compile(r"privacy gate|cookie ?wall|cookie consent|zustimmung|toestemming|consent", re.I)
_AGE_TITLE = re.compile(r"age (check|verification|gate)|verify your age|altersverifikation|altersprüfung", re.I)

STRONG_TEXT_LIMIT = 1500  # interstitials and wall pages are small
WEAK_TEXT_LIMIT = 300
SHELL_TEXT_LIMIT = 250


@dataclass
class Verdict:
    outcome: str
    reason: str
    vendor: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def browser_may_help(self) -> bool:
        return self.outcome in {"challenge", "blocked", "js_required", "empty", "rate_limited"}

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "reason": self.reason, "vendor": self.vendor}


def classify(
    *,
    status: int,
    headers: dict[str, str],
    original_url: str,
    final_url: str,
    body: bytes,
    text: str,
    visible_chars: int,
    title: str = "",
    content_type: str = "",
) -> Verdict:
    h = {k.lower(): v for k, v in headers.items()}
    head = text[:200_000].lower()

    # Header verdicts that are unambiguous.
    if h.get("cf-mitigated", "").lower() == "challenge":
        return Verdict("challenge", "cf-mitigated: challenge header", "cloudflare")
    waf = h.get("x-amzn-waf-action", "").lower()
    if waf in {"challenge", "captcha"}:
        return Verdict("challenge", f"x-amzn-waf-action: {waf}", "aws-waf")
    if status == 202 and "awswaf" in head:
        return Verdict("challenge", "AWS WAF challenge (202)", "aws-waf")
    if _TITLE_CHALLENGE.search(head):
        return Verdict("challenge", "cloudflare interstitial title", "cloudflare")

    # The destination's own errors win over page signatures (Cloudflare's 52x origin-error
    # pages carry its branding but mean "origin down", not "blocked").
    if status >= 500 and status != 503:
        vendor = "cloudflare" if "cf-error-details" in head else None
        return Verdict("server_error", f"destination answered {status}", vendor)
    if status in (404, 410):
        return Verdict("not_found", f"destination answered {status}")

    refusal = status in {401, 403, 429, 503, 202}
    if refusal or visible_chars < STRONG_TEXT_LIMIT:
        for vendor, outcome, pat in _STRONG:
            if pat.search(head):
                return Verdict(outcome, f"{vendor} interstitial signature", vendor)
    if refusal or visible_chars < WEAK_TEXT_LIMIT:
        for vendor, outcome, pat in _WEAK:
            if pat.search(head):
                return Verdict(outcome, f"{vendor} signature on a {'refusal' if refusal else 'near-empty'} page", vendor)
    if status == 503:
        return Verdict("server_error", "destination answered 503")

    parts = urlsplit(final_url)
    host = (parts.hostname or "").lower()
    orig = urlsplit(original_url)
    orig_host = (orig.hostname or "").lower()
    near_empty = visible_chars < STRONG_TEXT_LIMIT
    # A URL pattern means "sent to a wall" only if the page asked for did not already match
    # it (an article at /age-verification/ is not a gate) or the page is nearly empty.
    final_consent = CONSENT_URL_PATTERNS.search(host) or CONSENT_PATH_PATTERNS.search(parts.path)
    orig_consent = CONSENT_URL_PATTERNS.search(orig_host) or CONSENT_PATH_PATTERNS.search(orig.path)
    if final_consent and (not orig_consent or near_empty):
        return Verdict("consent_wall", f"landed on a consent page ({host}{parts.path})")
    if AGE_PATH_PATTERNS.search(parts.path) and (not AGE_PATH_PATTERNS.search(orig.path) or near_empty):
        return Verdict("age_gate", f"landed on an age-verification page ({parts.path})")
    if near_empty and title:
        if _AGE_TITLE.search(title):
            return Verdict("age_gate", f"age-verification page (title: {title[:80]!r})")
        if _CONSENT_TITLE.search(title):
            return Verdict("consent_wall", f"consent page (title: {title[:80]!r})")

    if _LOGIN_PATH.search(parts.path) and not _LOGIN_PATH.search(orig.path):
        return Verdict("login_wall", f"redirected to a sign-in page ({host}{parts.path})")

    if status == 429:
        return Verdict("rate_limited", "destination answered 429")
    if status in (401, 403):
        server = h.get("server", "").lower()
        vendor = "akamai" if "akamai" in server else ("cloudflare" if "cloudflare" in server else None)
        return Verdict("blocked", f"destination answered {status}" + (f" ({server})" if server else ""), vendor)
    if status >= 400:
        return Verdict("http_error", f"destination answered {status}")

    if not body.strip():
        return Verdict("empty", f"destination answered {status} with an empty body")
    if near_empty and _SOFT_LIMIT.search(head):
        return Verdict("rate_limited", "destination served a rate-limit notice")
    if near_empty and (_JS_REQUIRED.search(title) or _JS_URL.search(parts.path)):
        return Verdict("js_required", f"'JavaScript required' page ({title[:60]!r})")
    is_html = "html" in content_type.lower() or head.lstrip().startswith(("<!doctype html", "<html"))
    if is_html and visible_chars < SHELL_TEXT_LIMIT:
        # Short is not broken (example.com has 127 visible characters): call it a shell only
        # with positive evidence of client-side rendering.
        scripts = head.count("<script")
        if _JS_REQUIRED.search(head) or (scripts >= 3 and (_ROOT_SHELL.search(head) or visible_chars < 50)):
            return Verdict("js_required", f"only {visible_chars} visible characters; page is a JavaScript shell")
        if visible_chars == 0:
            return Verdict("empty", "no visible text")
    return Verdict("ok", "content returned")
