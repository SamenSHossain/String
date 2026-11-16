# webfetch: the pages String returns walls for

A page-fetching API, deployed on [Modal](https://modal.com), that gets the real page
where String's `/v1/fetch` returns a consent wall, an age gate or an empty 403 and
reports it as a success. It is drop-in compatible with String's request and response
shape, and adds honest outcome reporting, caller-side content checks, a latency
breakdown and deterministic structured extraction with per-field provenance.

**Endpoint:** `{{ENDPOINT}}/v1/fetch` (interactive docs at `{{ENDPOINT}}/docs`)

```bash
curl -s {{ENDPOINT}}/v1/fetch -H 'Content-Type: application/json' \
  -d '{"url":"https://store.steampowered.com/app/1091500/Cyberpunk_2077/","format":"markdown"}'
```

## 1. Targets String can't scrape

String was called through its API (`POST https://request.usestring.ai/v1/fetch`,
`format: markdown`), once in its default mode and once with `executeJS: true`, its
strongest setting available without an account. Every row below came back as **HTTP 200
with `x-status-code: 200`**, i.e. reported as a success, except zeit.de (`403`, empty
body). None of them is the page that was asked for. The full responses are in
[`bench/string_failures.jsonl`](bench/string_failures.jsonl).

| Target (domain size) | What String returned | String request IDs (default / `executeJS`) |
|---|---|---|
| `store.steampowered.com/app/1091500/Cyberpunk_2077/` (Steam, a top-100 global site) | the `/agecheck/app/1091500/` birth-date form, no price, description or reviews | `303431e8-f507-4b51-a7c9-25ef8ac7b8af` / `6fbaa183-d8c9-40de-8a99-f7b2dd8a5a4c` |
| `store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/` | the `/agecheck/app/271590/` form | `91b4c845-e745-4133-a997-5281ee29a124` / `e3b6ed11-74d6-4a87-a1da-521e13c85b9d` |
| `www.nu.nl/` (largest Dutch news site) | 124 characters: `# DPG Media Privacy Gate` | `b3ec39c5-c056-47e5-b440-4b3b96cb39ec` / `0d496a30-0cb3-47c5-bc7c-a24d0f77ab52` |
| `tweakers.net/` | DPG Media Privacy Gate | `f1053fa4-d28b-45c7-9b6f-ad25fc539720` |
| `www.volkskrant.nl/` | DPG Media Privacy Gate | `eefefdd0-613b-41df-a708-d93b6978cde0` |
| `www.ad.nl/` | DPG Media Privacy Gate | `1fe6ca1f-e45e-455c-bac4-26116bcf0510` |
| `www.derstandard.at/` (largest Austrian news site) | the `/consent/tcf/` "Willkommen bei DER STANDARD" wall | `9902fecd-d832-480a-a454-453ec7c71c38` / `25b4becf-1de4-4d9b-9d9b-22f52aebb4b8` |
| `www.golem.de/` | the `/sonstiges/zustimmung/` "Cookies zustimmen" wall | `1a418d5b-8e91-43dc-839e-c0a40fbf8191` / `ef15d9b6-f2dd-46a1-8cd5-d5dc434424d4` |
| `www.zeit.de/index` | `403`, empty body (Centinel Analytica JS challenge) | `a9d7fe9e-967a-4401-989a-5c9baa88f781` / `acea813e-6238-4916-bc35-8c9d0b7b03a3` |

The same requests through webfetch: see [Results](#4-results) for request IDs, timings
and extracted fields.

## 2. Why String fails here, and what webfetch does instead

These sites put an interstitial in front of the page until the visitor clicks
"Accept" or confirms their age. A fetcher that does not click returns the interstitial,
and because the interstitial is served with `200 OK`, a status-code check calls it a
success. Browser rendering (`executeJS`) does not help: the browser still has nobody to
click the button.

webfetch does what the visitor's click does, per site, and nothing more:

| Gate | What the click does | What webfetch does |
|---|---|---|
| Steam age check (`/agecheck/app/<id>/`) | the birth-date form sets `birthtime`, `lastagecheckage`; "View page" sets `wants_mature_content` | sends those cookies with the first request (same as yt-dlp's Steam extractor); if a redirect to `/agecheck` still happens, sets them and retries |
| DPG Media Privacy Gate (`myprivacy.dpgmedia.nl/consent?...&callbackUrl=...`) | "Akkoord" sends the browser to the site's `callbackUrl` with an `authId`; the site sets its consent cookies and redirects back | reads `callbackUrl` from the redirect and makes that same callback request (calibre's Volkskrant recipe does the same); covers nu.nl, tweakers.net, volkskrant.nl, ad.nl, trouw.nl, parool.nl, hln.be and the other DPG titles |
| derStandard (`/consent/tcf/`) | accepting sets `DSGVO_ZUSAGE_V1=true` | sends that cookie |
| Golem (`/sonstiges/zustimmung/auswahl.html`) | the accept button calls `/abo/setconsentcookie.php?type=cmp&version=<v>`, which sets `golem_consent20=cmp<v>` | reads `cookieVersion` from the consent page's own config and makes the same call, so a policy-version bump does not break it |
| ZEIT ONLINE | consent sets `zonconsent`; the 403 in front of it is a Centinel Analytica JavaScript challenge | sends the cookie; the challenge is classified as such and handed to the headless-browser tier |
| Google/YouTube EU consent | sets `SOCS` | sends `SOCS=CAI` (reject-all, essential cookies only) |

Nothing here logs in, defeats a paywall or solves a captcha: the content returned is the
content any visitor gets after the click.

## 3. Architecture

```
POST /v1/fetch
  └─ validate (SSRF guard: every hop resolved, private/link-local/metadata IPs refused,
     curl pinned to the checked IP with CURLOPT_RESOLVE)
  └─ HTTP tier: curl_cffi with Chrome 152's TLS + HTTP/2 fingerprint, redirects followed
     by hand, cookie jar per request over a shared connection pool
       └─ gate recipes: preset cookies, or act on the redirect / gate page
  └─ decode like a browser (BOM > header > <meta> > UTF-8 > charset-normalizer;
     Shift_JIS→cp932, GBK→GB18030, ...)
  └─ classify: ok | challenge | blocked | consent_wall | age_gate | login_wall |
     js_required | empty | not_found | rate_limited | http_error | server_error
       └─ refused? retry once with Safari's fingerprint
       └─ challenge / JS shell? headless Chromium (patchright) in its own Modal app,
          started with the gate cookies, waits out challenge reloads
  └─ caller's `expect` checks (contains / notContains / selector / minChars)
  └─ outputs: Markdown (Rust html-to-markdown, links absolutized; or main content via
     trafilatura), text, html, raw, metadata, JSON-LD, microdata, links, hydration data
  └─ jsonSchema: filled from site extractors > JSON-LD > microdata > meta > DOM, every
     value with its source, null instead of a guess
```

Code: [`webfetch/`](webfetch). Deployment: [`modal_app.py`](modal_app.py) (API),
[`modal_browser.py`](modal_browser.py) (browser tier), [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)
(tests → deploy → smoke test of the live endpoint → live check of every gate recipe).

## 4. Results

{{RESULTS}}

## 5. Improvements over String's API

{{IMPROVEMENTS}}

## 6. API

`POST /v1/fetch` takes String's fields (`url`, `format`, `jsonSchema`, `executeJS`,
`headers`, `markdownMode`, `mainContentOnly`) plus:

| Field | Meaning |
|---|---|
| `format` | `json` (envelope, default), `markdown`, `text`, `html`, `raw` (original bytes) |
| `include` | for `json`: any of `markdown`, `html`, `text`, `metadata`, `jsonLd`, `microdata`, `links`, `hydration` (default `markdown`, `metadata`) |
| `expect` | `{contains, notContains, selector, minChars}`: what the real page must contain; failing pages are not reported as success and trigger escalation |
| `resolveGates` | default `true`; `false` returns the wall (and labels it) |
| `render` | `auto` (default: browser only when HTTP meets a challenge or JS shell), `never`, `always` |
| `cookies` | extra cookies for the target host |
| `timeout`, `maxBytes` | per-request limits (≤ 90 s, ≤ 25 MB) |

Response headers on every fetch: `x-request-id`, `x-status-code` (destination),
`x-final-url`, `x-outcome`, `x-strategy`, `x-gates`, `server-timing`
(`dns;dur=…, connect, tls, ttfb, download, redirects, fetch, process, total`).

Status codes: `200` = the destination's real answer (its status is in `statusCode`);
`502` = we could not get past what the destination served, and `outcome` says what it
was; `504` timeout; `400` invalid input; `403` destination not public (SSRF guard);
`429` rate limited.

`POST /v1/batch` runs up to 20 fetches concurrently.

## 7. Running it

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                   # 61 tests; a local server emulates each gate flow
python -m webfetch.cli https://www.nu.nl/   # one fetch, JSON line out
uvicorn --factory webfetch.api:create_app   # local API on :8000
modal deploy modal_app.py && modal deploy modal_browser.py
```

## 8. Limits, stated plainly

{{LIMITS}}
