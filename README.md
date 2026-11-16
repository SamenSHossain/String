# webfetch: the pages String returns walls for

A page-fetching API, deployed on [Modal](https://modal.com), that gets the real page
where String's `/v1/fetch` returns a consent wall, an age gate or an empty 403 and
reports it as a success. It is drop-in compatible with String's request and response
shape, and adds honest outcome reporting, caller-side content checks, a latency
breakdown and deterministic structured extraction with per-field provenance.

**Endpoint:** `https://samenh--webfetch.modal.run/v1/fetch` (interactive docs at `https://samenh--webfetch.modal.run/docs`)

```bash
curl -s https://samenh--webfetch.modal.run/v1/fetch -H 'Content-Type: application/json' \
  -d '{"url":"https://store.steampowered.com/app/1091500/Cyberpunk_2077/","format":"markdown"}'
```

## 1. Targets String can't scrape

String was called through its API (`POST https://request.usestring.ai/v1/fetch`,
`format: markdown`), once in its default mode and once with `executeJS: true`, its
strongest setting available without an account. Every row below came back as **HTTP 200
with `x-status-code: 200`**, i.e. reported as a success, except zeit.de (`403`, empty
body). None of them is the page that was asked for. The failures reproduce: the same
requests failed the same way on Nov 10, Nov 16 and again in the benchmark in section 4.
The full responses are in [`bench/string_failures.jsonl`](bench/string_failures.jsonl).

| Target (domain size) | What String returned | String request IDs (default / `executeJS`) |
|---|---|---|
| `store.steampowered.com/app/1091500/Cyberpunk_2077/` (Steam, the largest PC game store) | the `/agecheck/app/1091500/` birth-date form, no price, description or reviews | `303431e8-f507-4b51-a7c9-25ef8ac7b8af` / `6fbaa183-d8c9-40de-8a99-f7b2dd8a5a4c` |
| `store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/` | the `/agecheck/app/271590/` form | `91b4c845-e745-4133-a997-5281ee29a124` / `e3b6ed11-74d6-4a87-a1da-521e13c85b9d` |
| `www.nu.nl/` (largest Dutch news site) | 124 characters: `# DPG Media Privacy Gate` | `b3ec39c5-c056-47e5-b440-4b3b96cb39ec` / `0d496a30-0cb3-47c5-bc7c-a24d0f77ab52` |
| `tweakers.net/` | DPG Media Privacy Gate | `f1053fa4-d28b-45c7-9b6f-ad25fc539720` |
| `www.volkskrant.nl/` | DPG Media Privacy Gate | `eefefdd0-613b-41df-a708-d93b6978cde0` |
| `www.ad.nl/` | DPG Media Privacy Gate | `1fe6ca1f-e45e-455c-bac4-26116bcf0510` |
| `www.derstandard.at/` (one of Austria's largest news sites) | the `/consent/tcf/` "Willkommen bei DER STANDARD" wall | `9902fecd-d832-480a-a454-453ec7c71c38` / `25b4becf-1de4-4d9b-9d9b-22f52aebb4b8` |
| `www.golem.de/` | the `/sonstiges/zustimmung/` "Cookies zustimmen" wall | `1a418d5b-8e91-43dc-839e-c0a40fbf8191` / `ef15d9b6-f2dd-46a1-8cd5-d5dc434424d4` |
| `www.southwest.com/` (largest US domestic airline) | `200` with an **empty body**; with `executeJS`, 79 characters (title + "Skip to content") | `fe396072-ba6d-409b-a1cd-3bbc66b2e00e` / `721c8d1a-9fad-4f9b-8368-faadedf7569e` |
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

All numbers below come from the deployed endpoint, called from a separate machine, on
2026-11-16. Raw data: [`bench/results_gated.jsonl`](bench/results_gated.jsonl),
[`bench/results_speed.jsonl`](bench/results_speed.jsonl),
[`bench/steam_accuracy.jsonl`](bench/steam_accuracy.jsonl). Scripts: [`bench/`](bench).

### 4.1 The targets: String 0/10, webfetch 9/10

Same URL, same moment, `format: markdown`. "Real page" means the response contains a marker
that only the real page has (the rule String's own benchmark uses), not just a 2xx status.

| Target | webfetch | webfetch request ID | String | String request ID |
|---|---|---|---|---|
| Steam: Cyberpunk 2077 | ✅ real page, 16,343 chars, 0.60 s | `885b36b1-51c3-41d1-ad7c-06ecbb0c53b3` | ❌ `/agecheck/` form | `bd5d733c-8399-4e88-9c72-b501e1dd6ba5` |
| Steam: GTA V Legacy | ✅ 22,350 chars, 1.07 s | `d843379e-21f4-403b-a280-8419b9edce24` | ❌ `/agecheck/` form | `1b13543e-0b64-437a-85d7-95db08c72079` |
| nu.nl | ✅ 82,694 chars, 1.70 s | `3efff823-ac8e-4730-bc88-ab2bc9acdae6` | ❌ Privacy Gate, 124 chars | `eca41176-11ea-47cc-a2ca-81836a39f77d` |
| tweakers.net | ✅ 87,296 chars, 1.59 s | `bedbc672-990a-4073-99fe-f0fb2fd1dc55` | ❌ Privacy Gate | `657331a9-d167-453d-87ea-18a7fabd8642` |
| volkskrant.nl | ✅ 110,377 chars, 1.59 s | `d0609da8-d1c8-4550-80f8-5d812663d67f` | ❌ Privacy Gate | `bbb83abe-48e0-412a-ba4e-238d68d61baf` |
| ad.nl | ✅ 143,100 chars, 1.24 s | `699d2239-0ecc-47ef-8e0c-c591221cfd96` | ❌ Privacy Gate | `b4ce84af-1f2a-44a6-9228-370c4b28ee63` |
| derstandard.at | ✅ 109,415 chars, 1.02 s | `b6fc4eef-5ff4-48c0-9c42-74ac3feacc5d` | ❌ consent wall | `c7124cc4-9c03-4e4a-b2ee-dfdb3b593543` |
| golem.de | ✅ 149,039 chars, 1.26 s | `82141afc-0f46-4821-8460-59c0f816cd27` | ❌ consent wall | `61a6c5e5-f149-40f6-b15b-e4fbc11125a3` |
| southwest.com | ✅ 31,832 chars, 6.54 s (headless browser) | `ff7a86db-9cbc-4cb5-a72a-d1b705b1114b` | ❌ empty body (79 chars with `executeJS`) | `e6816d11-b89d-47d0-966e-8cbd6e4420b0` |
| zeit.de | ❌ `502`, `outcome: challenge`, vendor `centinel` | `880b3ec3-0a51-4367-8ffd-e108e2faa7a6` | ❌ empty body, reported as `200` | `6d44208c-ddf1-463b-a55a-a92758b9a067` |

On zeit.de neither gets the page, but only webfetch says so: String answers `200` with an
empty body; webfetch answers `502` with `outcome: challenge, vendor: centinel`.

### 4.2 Structured output: 96.7% field accuracy against Steam's own API

20 Steam games (9 of which redirect to the age check). For each, the product page was
fetched with a `jsonSchema` (`name, price, currency, releaseDate, developer, publisher`) and
every value compared with Steam's official API (`/api/appdetails`), fetched separately.

| | webfetch, exact field match | String: true value present in its output at all |
|---|---|---|
| all 20 games | **116 / 120 = 96.7%** | 79 / 120 = 65.8% |
| 9 age-gated games | **50 / 54 = 92.6%** | 13 / 54 = 24.1% |
| 11 ungated games | 66 / 66 = 100% | 66 / 66 = 100% |

String's column is a ceiling, not a score: it counts a field when the true value appears
anywhere in String's Markdown, so no extractor, String's own `jsonSchema` AI included, can
do better from that fetch. (String's `jsonSchema` needs an account key; it was not called.)
webfetch's 4 misses, shown as-is: three release dates one day off because the store page
renders US-Pacific dates while the API reports UTC (e.g. Cyberpunk: page "Dec 9, 2020", API
"10 Dec, 2020"); and Call of Duty, whose app page is a launcher whose first purchase box
sells a different product ($69.99) while the API lists no price for the app itself.

Every value carries its source, e.g.
`"price": "site:steam:[data-price-final]"`, `"currency": "microdata:Product.offers.priceCurrency"`,
`"headline": "json-ld:BlogPosting.headline"`, and a field with no source is `null` and listed
in `missing`. Extraction time on the server: **≈1 ms** (`extracted.ms`), against an LLM round
trip for an AI extractor.

### 4.3 Latency on pages both services fetch: parity

8 popular pages × 2 rounds, both services called back to back from the same client:

| | median | mean | p90 | success |
|---|---|---|---|---|
| webfetch | **0.48 s** | 0.69 s | 1.46 s | 16 / 16 |
| String | 0.53 s | 0.69 s | 1.03 s | 16 / 16 |

webfetch was faster on 4 of 8 pages (Wikipedia, Hacker News, BBC, GitHub Trending) and
slower on 4 (most on spiegel.de, 1.5 s vs 1.0 s: a 1 MB page). Server-side, a warm request
spends ~1–5 ms on extraction; the rest is the destination. Each response's `server-timing`
header shows where the time went (`dns, connect, tls, ttfb, download, redirects, process`).


## 5. Improvements over String's API

Each item is something String's API does today (quoted from its docs) and what webfetch
does instead, with the evidence above.

1. **Gets through consent walls and age gates.** String returns the interstitial; webfetch
   performs the visitor's click (section 2). 9/10 vs 0/10 on the targets; 92.6% vs ≤24.1%
   field accuracy on age-gated Steam pages.
2. **Never reports a wall as success.** String's docs: *"a destination that serves an
   anti-bot challenge or a login wall as a `200` will report `statusCode: 200`"* and its
   benchmark found 400 such responses across the field. webfetch classifies every response
   (`outcome`: `ok`, `challenge`, `blocked`, `consent_wall`, `age_gate`, `login_wall`,
   `js_required`, `empty`, `rate_limited`, ...) with the vendor when known (Cloudflare,
   Akamai, DataDome, PerimeterX, Imperva, AWS WAF, Centinel, ...), and returns `502` with the
   reason instead of the wall's Markdown. Seen live: zeit.de (`challenge/centinel`),
   southwest.com's first response (`js_required`, escalated to the browser), Hacker News'
   200-status "Sorry, we're not able to serve your requests this quickly" (`rate_limited`).
3. **Caller-side content checks.** `expect: {contains, notContains, selector, minChars}`
   states what the real page must contain; a page that fails is `validation_failed`, never
   `ok`, and triggers the next strategy. Nothing equivalent exists in String's `/fetch`.
4. **Structured output with provenance, no hallucination, ~1 ms.** String's `jsonSchema`
   is *"extract into with AI"* with *"a dynamic extraction fee"*; webfetch fills the schema
   deterministically from what the page carries (site extractors, JSON-LD, microdata, meta),
   returns `sources` per field, `null` + `missing` instead of a guess, and `missingRequired`
   against the schema's `required`.
5. **Escalation only when needed.** Plain HTTP with Chrome's TLS/HTTP2 fingerprint first;
   Safari's fingerprint if refused; headless Chromium (patchright, separate container) only
   for challenges and JavaScript shells, started with the gate cookies. southwest.com shows
   it: `attempts: [http:chrome → js_required, browser:patchright → ok]`.
6. **Latency you can see.** `server-timing` and `timing` break every request into DNS,
   connect, TLS, TTFB, download, redirect hops and processing; `redirects` lists each hop
   (including gate detours); `attempts` lists every strategy tried and why it was rejected.
7. **Leaner Markdown.** Inline base64 images (icons, logos) are replaced by their alt text:
   on spiegel.de they were 53% of the Markdown (308,809 → 143,695 characters).
   Relative links are absolutized against the final URL.
8. **Correct text on legacy encodings.** Browser-order charset detection with WHATWG
   superset mapping (Shift_JIS→cp932, GB2312/GBK→GB18030, Big5→Big5-HKSCS, EUC-KR→cp949) and
   a detector fallback when the declared charset is wrong (tested with Shift_JIS, GBK and
   a mislabeled cp1251 page).
9. **Safe to expose.** Every hop (including gate callbacks and redirects) is resolved and
   checked against private, loopback, link-local, CGNAT and metadata ranges, and curl is
   pinned to the checked IP so DNS rebinding cannot slip through; decoded bodies are capped
   in the write callback (stops gzip bombs); per-request cookie jars; batch requests are
   rate-limited per item.
10. **Batch.** `POST /v1/batch` runs up to 20 fetches concurrently in one call.


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
pytest -q                                   # 67 tests; a local server emulates each gate flow
python -m webfetch.cli https://www.nu.nl/   # one fetch, JSON line out
uvicorn --factory webfetch.api:create_app   # local API on :8000
modal deploy modal_app.py && modal deploy modal_browser.py
```

## 8. Limits, stated plainly

- **zeit.de is not solved.** Its 403 is a Centinel Analytica JavaScript challenge that also
  refuses a headless Chromium from a datacenter IP. webfetch labels it correctly; getting
  through would need a residential/mobile exit or a headful browser, which this deployment
  does not have. Same for sahibinden.com (Cloudflare Turnstile) and ozon.ru (refuses
  non-Russian datacenter IPs).
- **Gate recipes are per site.** Six recipes cover Steam, the DPG Media network, ZEIT,
  derStandard, Golem and Google/YouTube consent. Unknown walls are detected and labelled
  (`consent_wall`, `age_gate`) rather than resolved. The `live-recipes` CI job fetches every
  target from GitHub's runners on each push, so a site that changes its gate shows up there.
- **One region.** Modal runs the endpoint in its default US region; there is no
  `countryCode`.
- **Structured extraction only reads what the page carries.** Fields that exist only as
  free text (not in JSON-LD, microdata, meta tags or a site extractor) come back `null`.
  An LLM fallback for those fields, with each value checked against the page text, is the
  natural next step.
- **The endpoint is open** (no key) and rate-limited to 30 requests/min per client IP; set
  `WEBFETCH_API_KEYS` to require keys.

