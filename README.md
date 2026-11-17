# webfetch: the pages String returns walls for

A page-fetching API, deployed on [Modal](https://modal.com), that gets the real page where
String's keyless `/v1/fetch` returns a consent wall, an age gate or an empty JavaScript
shell. It accepts String's core request fields (`url`, `format`, `markdownMode`,
`mainContentOnly`, `jsonSchema`, `executeJS`, `headers`) and returns String's response
headers (`x-request-id`, `x-status-code`, `x-final-url`), so `format: markdown` calls are
interchangeable. It adds an explicit outcome for every response, caller-side content
checks, a latency breakdown and deterministic structured extraction with per-field
provenance.

**Endpoint:** `https://samenh--webfetch.modal.run/v1/fetch` (interactive docs at `https://samenh--webfetch.modal.run/docs`)

```bash
curl -s https://samenh--webfetch.modal.run/v1/fetch -H 'Content-Type: application/json' \
  -d '{"url":"https://store.steampowered.com/app/1091500/Cyberpunk_2077/","format":"markdown"}'
```

## 1. Targets String's keyless API returns walls for

Every target was sent to `POST https://request.usestring.ai/v1/fetch` with
`format: markdown`, once in the default mode and once with `executeJS: true` (String's
strongest setting without an account; the keyless API accepts only `url`, `format` and
`executeJS`). For the walls and gates, String answered **HTTP 200 with
`x-status-code: 200`** and the wall's text as the page. String's docs say a challenge it
detects "will report `statusCode: 200` on a `502`"; these were not detected. zeit.de came
back as HTTP 200, `x-status-code: 403`, empty body. The failures reproduce: the same
requests failed the same way on Nov 10 and Nov 16, and again in the benchmark runs in §4.
Sizes, final URLs, request IDs and the first 240 characters of every String response are
in [`bench/string_failures.jsonl`](bench/string_failures.jsonl).

Not tested: String's keyed `actions` parameter (browser steps such as a click), which
needs an account and could in principle click an accept button.

| Target | What String returned | String request IDs (default / `executeJS`) |
|---|---|---|
| `store.steampowered.com/app/1091500/Cyberpunk_2077/` (Steam, the largest PC game store) | the `/agecheck/app/1091500/` birth-date form: no price, description or reviews | `303431e8-f507-4b51-a7c9-25ef8ac7b8af` / `6fbaa183-d8c9-40de-8a99-f7b2dd8a5a4c` |
| `store.steampowered.com/app/271590/Grand_Theft_Auto_V_Legacy/` | the `/agecheck/app/271590/` form | `91b4c845-e745-4133-a997-5281ee29a124` / `e3b6ed11-74d6-4a87-a1da-521e13c85b9d` |
| `www.nu.nl/` (the Netherlands' biggest news site) | `# DPG Media Privacy Gate`: 124 characters (1,889 with `executeJS`) | `b3ec39c5-c056-47e5-b440-4b3b96cb39ec` / `0d496a30-0cb3-47c5-bc7c-a24d0f77ab52` |
| `tweakers.net/` | DPG Media Privacy Gate | `f1053fa4-d28b-45c7-9b6f-ad25fc539720` / `6954f927-b8c5-4c91-b965-79541e6d6ad9` |
| `www.volkskrant.nl/` | DPG Media Privacy Gate (billed `browser_premium` with `executeJS`) | `eefefdd0-613b-41df-a708-d93b6978cde0` / `266539f3-6b06-4bc5-af9b-7b1e76c110e3` |
| `www.ad.nl/` | DPG Media Privacy Gate | `1fe6ca1f-e45e-455c-bac4-26116bcf0510` / `c3f33d4e-b1cb-472f-970a-dd80ab1552a3` |
| `www.derstandard.at/` (one of Austria's largest news sites) | the `/consent/tcf/` "Willkommen bei DER STANDARD" wall | `9902fecd-d832-480a-a454-453ec7c71c38` / `25b4becf-1de4-4d9b-9d9b-22f52aebb4b8` |
| `www.golem.de/` | the `/sonstiges/zustimmung/` "Cookies zustimmen" wall | `1a418d5b-8e91-43dc-839e-c0a40fbf8191` / `ef15d9b6-f2dd-46a1-8cd5-d5dc434424d4` |
| `www.southwest.com/` (the largest US domestic airline) | an **empty body**; with `executeJS`, 79 characters (title + "Skip to content") | `fe396072-ba6d-409b-a1cd-3bbc66b2e00e` / `721c8d1a-9fad-4f9b-8368-faadedf7569e` |
| `www.zeit.de/index` | `x-status-code: 403`, empty body (a Centinel Analytica JavaScript challenge) | `a9d7fe9e-967a-4401-989a-5c9baa88f781` / `acea813e-6238-4916-bc35-8c9d0b7b03a3` |

## 2. Why String fails here, and what webfetch does instead

These sites put an interstitial in front of the page until the visitor clicks "Accept"
or confirms their age. A fetcher that does not click returns the interstitial, and since
it is served with `200 OK`, a status-code check calls it a success. Rendering the page in
a browser (`executeJS`) does not help on its own: nothing clicks the button.

webfetch does what the visitor's click does, per site, and nothing more:

| Gate | What the click does | What webfetch does |
|---|---|---|
| Steam age check (`/agecheck/app/<id>/`) | the birth-date form sets `birthtime` and `lastagecheckage`; "View page" sets `wants_mature_content` | sends those cookies with the first request (as yt-dlp's Steam extractor does); if a redirect to `/agecheck` still happens, sets them and retries |
| DPG Media Privacy Gate (`myprivacy.dpgmedia.nl/consent?...&callbackUrl=...`) | "Akkoord" sends the browser to the site's `callbackUrl` with an `authId`; the site sets its consent cookies and redirects back | reads `callbackUrl` from the redirect and makes that same request (calibre's Volkskrant recipe does the same); covers nu.nl, tweakers.net, volkskrant.nl, ad.nl, trouw.nl, parool.nl, hln.be and the other DPG titles |
| derStandard (`/consent/tcf/`) | accepting sets `DSGVO_ZUSAGE_V1=true` | sends that cookie |
| Golem (`/sonstiges/zustimmung/auswahl.html`) | the accept button calls `/abo/setconsentcookie.php?type=cmp&version=<v>`, which sets `golem_consent20=cmp<v>` | sends the cookie; if the site still shows the wall, reads `cookieVersion` from the consent page's own config and makes the same call, so a policy-version bump does not break it |
| ZEIT ONLINE | consent sets `zonconsent`; the 403 in front of it is a Centinel Analytica JavaScript challenge | sends the cookie; the challenge is labelled as such (vendor `centinel`) and handed to the headless-browser tier |
| Google/YouTube EU consent (`consent.google.<tld>`) | sets `SOCS` | sends `SOCS=CAI` (reject-all, essential cookies only) on the right Google domain |

Nothing here logs in, defeats a paywall or solves a captcha: the content returned is the
content any visitor gets after the click.

## 3. Architecture

```
POST /v1/fetch
  └─ validate (jsonSchema at most 50 KB, 200 keys, depth 10); SSRF guard (every hop
     resolved; private/loopback/link-local/CGNAT/metadata refused, IPv6 forms that embed an
     IPv4 judged by that IPv4; curl pinned to the checked IP with CURLOPT_RESOLVE)
  └─ HTTP tier: curl_cffi with Chrome 152's TLS + HTTP/2 fingerprint; redirects followed by
     hand; a cookie jar per request over a shared connection pool; caller headers kept to
     the requested origin (only Accept, Accept-Language, User-Agent, Cache-Control, Pragma,
     DNT and Upgrade-Insecure-Requests go further); decoded size capped in the write callback
       └─ gate recipes: preset cookies, or act on the redirect / wall page
  └─ decode like a browser: BOM > header > <meta> > UTF-8 > the declared charset with
     U+FFFD for a few bad bytes > charset-normalizer, with the WHATWG Encoding Standard's
     labels and decoders
  └─ classify: ok | challenge | blocked | consent_wall | age_gate | login_wall |
     js_required | empty | rate_limited | not_found | http_error | server_error
       └─ challenge, block or rate limit? retry once with Safari's fingerprint
       └─ still failing, or a JS shell, an empty page or failed `expect` checks? headless
          Chromium (patchright) in its own Modal app, started with the gate cookies and only
          the cross-origin-safe caller headers (Chromium carries headers across redirects);
          all of its traffic goes through a local egress proxy that applies the same SSRF
          checks to every connection, redirect hops included
  └─ caller's `expect` checks (contains / notContains / selector / minChars)
  └─ outputs: Markdown (Rust html-to-markdown; links absolutized; inline base64 images
     reduced to alt text), main-content Markdown (trafilatura; links resolved against the
     page URL and <base href> first), text, html, raw, metadata, JSON-LD, microdata, links,
     hydration data; a part that fails on a page is left out and named in `warnings`
  └─ jsonSchema: filled from site extractors > the page's declared main entity
     (JSON-LD/microdata) > page-level meta > other entities, every value with its source;
     null, never a guess
```

Code: [`webfetch/`](webfetch). Deployment: [`modal_app.py`](modal_app.py) (API) and
[`modal_browser.py`](modal_browser.py) (browser tier). CI ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)):
tests → deploy → smoke test of the live endpoint; in parallel, an informational
`live-recipes` job fetches the gated targets from the CI runner.

## 4. Results

Measured on 2026-11-16 against the deployed endpoint, called from a separate machine,
with String called back to back from the same client. Raw data: [`bench/`](bench)
(`results_gated.jsonl`, `results_speed.jsonl`, `steam_accuracy.jsonl`); the first run of
the same day, before the fixes from an independent review, is in
[`bench/archive/2026-11-16-first-run/`](bench/archive/2026-11-16-first-run).

### 4.1 The targets: String 0/10, webfetch 9/10

Same URL, `format: markdown`. A "real page" must contain a marker only the real page has
(the rule String's own benchmark uses), not just a 2xx status.

| Target | webfetch | webfetch request ID | String | String request ID |
|---|---|---|---|---|
| Steam: Cyberpunk 2077 | ✅ 12,919 chars, 0.52 s | `5f0c5bd0-f40e-4063-a976-94e7b7aea3dc` | ❌ `/agecheck/` form | `bca90bac-08c2-4a57-9323-a03d022fdb75` |
| Steam: GTA V Legacy | ✅ 16,490 chars, 0.71 s | `1bce7e50-2360-4a21-b9d2-8d03ad5b38fb` | ❌ `/agecheck/` form | `3c38c322-ff0d-4f23-808b-e0b065b9dad0` |
| nu.nl | ✅ 47,081 chars, 1.52 s | `aeea0c7c-21d7-448a-86c6-c846a780e80c` | ❌ Privacy Gate, 124 chars | `38c9bed7-e7ea-4535-9f31-3806efe18a54` |
| tweakers.net | ✅ 43,690 chars, 0.75 s | `028df15c-7f63-468f-8ba2-064a41ba2be6` | ❌ Privacy Gate | `3d9da026-309f-4c15-9bc8-e218745f49fb` |
| volkskrant.nl | ✅ 49,503 chars, 1.48 s | `61081b00-713a-4fed-ab34-70a2a94b9f5a` | ❌ Privacy Gate | `d82bb808-2605-411f-8c26-c3f245cfe64f` |
| ad.nl | ✅ 49,047 chars, 1.26 s | `b1f41883-3848-440e-ba18-d6a8a681699e` | ❌ Privacy Gate | `fbd00917-cdd9-445d-8aaa-14fc3e8c26de` |
| derstandard.at | ✅ 96,380 chars, 1.69 s | `aca94596-ed50-48dc-a1ff-9c9261a0f6c6` | ❌ consent wall | `b5ea1d20-fa95-4387-ae89-4fe3b3da3576` |
| golem.de | ✅ 52,155 chars, 1.72 s | `d7d9dd03-efcd-4935-8547-3389368cd8de` | ❌ consent wall | `51f084fc-dc0b-4fbd-9641-10516c685e07` |
| southwest.com | ✅ 14,282 chars, 5.54 s (headless browser) | `55b04ae8-aa68-4de9-a9bc-34651db8021f` | ❌ empty body | `1a4b0d32-d79f-49cf-bab9-56bba4193524` |
| zeit.de | ❌ `502`, `outcome: challenge`, `vendor: centinel` | `ce91c685-88db-4661-a296-0e5dec079311` | ❌ `x-status-code: 403`, empty body | `2a594fb6-64ef-443c-b617-7fe558f82fef` |

Neither gets zeit.de. String returns an empty body with `x-status-code: 403`; webfetch
returns `502` and names the cause: a Centinel Analytica challenge that also refuses a
headless browser from a datacenter IP.

### 4.2 Structured output: 98.3% field accuracy against Steam's own API

20 Steam games, 9 of which redirect to the age check. Each product page was fetched with a
`jsonSchema` (`name, price, currency, releaseDate, developer, publisher`) and every value
compared with Steam's official API (`/api/appdetails`), fetched in the same run. Rule:
exact match, except developer/publisher, which count when the API's first entry appears in
the extracted value.

| | webfetch: field matches the API | String: true value present in its output at all |
|---|---|---|
| all 20 games | **118 / 120 = 98.3%** | 79 / 120 = 65.8% |
| 9 age-gated games | **52 / 54 = 96.3%** | 13 / 54 = 24.1% |
| 11 ungated games | 66 / 66 = 100% | 66 / 66 = 100% |

String's column is a ceiling, not a score: a field counts when the true value appears
anywhere in String's Markdown, so no extractor working from that fetch, String's own
`jsonSchema` AI included, can do better. String's `jsonSchema` needs an account key and was
not called. webfetch's two misses: GTA V's release date (the store page says Apr 14, 2015;
the API says Apr 13), and Call of Duty, whose app page is a launcher with a $69.99 purchase
box for another product while the API lists no price for the app itself. Steam's API
answers are not perfectly stable either: in the first run it gave Cyberpunk 2077's release
date as Dec 10, 2020, in this one as Dec 9 (the archive keeps both runs).

The result holds wherever the container runs. Steam prices by the store country it infers
from the caller's IP, and its age gate is per store too: GTA V has one on the US store and
none on the Polish one. Modal does not keep the container in one region. The run above
happened to be on a US container (every price in it is USD); on Nov 17 the endpoint went
out through Azure in Warsaw. So the script now pins the product page to `?cc=us`, as the
ground truth already was, and decides "age-gated" itself: the same URL fetched with
`resolveGates: false` has to land on `/agecheck`. Re-run on Nov 17 from Warsaw, webfetch
only: **118 / 120**, 52 / 54 on the age-gated games, 66 / 66 on the rest, with the same
two misses and the same 9 games found gated
([`bench/steam_accuracy_rerun.jsonl`](bench/steam_accuracy_rerun.jsonl)).

Every value carries its source (`"price": "site:steam:[data-price-final]"`,
`"currency": "microdata:Product.offers.priceCurrency"`, `"headline": "json-ld:BlogPosting.headline"`),
and a field with no source is `null` and listed in `missing`. Extraction time on the
server, from the response's `extracted.ms`: median **0.96 ms**, maximum 1.4 ms over the 20 pages.

### 4.3 Latency on pages both services fetch: on par

8 popular pages × 2 rounds; both services returned the real page 16/16.

| | median | mean | p90 |
|---|---|---|---|
| webfetch | **0.46 s** | 0.64 s | 1.46 s |
| String | 0.52 s | 0.70 s | 1.29 s |

webfetch was faster on 5 of 8 pages (Wikipedia, python.org, Hacker News, BBC, GitHub
Trending) and slower on 3 (spiegel.de, the Guardian, Steam/Stardew Valley). Server-side
processing (decode, classify, Markdown) took a median of 12 ms and at most 89 ms (spiegel.de,
2 MB). Of webfetch's client-observed time, 41% was the destination fetch and about half
was spent between the client and Modal, which says more about where Modal runs than about
the fetcher. Each HTTP-tier response's `server-timing` header splits the server's share
into `dns, connect, tls, ttfb, download, redirects, process`; browser-tier responses
report `fetch, process, total`.

## 5. Improvements over String's API

1. **Gets through consent walls and age gates.** String's keyless API returns the
   interstitial; webfetch does what the visitor's click does (§2). 9/10 vs 0/10 on the
   targets; 96.3% field accuracy vs a ≤24.1% ceiling on age-gated Steam pages.
2. **A wall is never reported as success.** String's docs: a challenge served as `200` is
   reported "on a `502`" when String detects it; the walls in §1 came back as HTTP 200 with
   `x-status-code: 200`. webfetch classifies every response (`outcome`: `ok`, `challenge`,
   `blocked`, `consent_wall`, `age_gate`, `login_wall`, `js_required`, `empty`,
   `rate_limited`, ...), names the vendor when known (Cloudflare, Akamai, DataDome,
   PerimeterX, Imperva, AWS WAF, Centinel, Amazon, ...) and answers `502` with the reason instead
   of the wall's Markdown. Seen live: zeit.de (`challenge`, `centinel`), southwest.com's
   first response (`js_required`, escalated to the browser), and Hacker News' "Sorry,
   we're not able to serve your requests this quickly" page, which the first run received
   (with status 503) and which is now labelled `rate_limited`.
3. **Caller-side content checks.** `expect: {contains, notContains, selector, minChars}`
   says what the real page must contain; a page that fails is `validation_failed`, never
   `ok`, and triggers the next strategy. String's `/fetch` has no equivalent parameter.
4. **Structured output with provenance, nothing generated, about 1 ms.** String's
   `jsonSchema` extracts "with AI" for "a dynamic extraction fee". webfetch fills the
   schema from what the page carries (site extractors, the JSON-LD/microdata of the main
   entity the page declares, page meta, other entities) and returns `sources` per field,
   array items included (`recipeInstructions[0].text`), and `null` plus `missing` instead
   of a guess. It resolves JSON-LD `@id` references, lists of them included (Yoast's
   authors), and takes schemas as Pydantic emits them: nullable `anyOf`, `$ref`/`$defs`, a
   root `$ref`, and self-referencing models (filled along the page's data, at most 12
   levels deep). An `Optional` object the page does not carry is `null`, and identifiers
   (SKU, MPN, GTIN, EAN, UPC, ISBN) are read only from their own keys, never from another
   key or another entity on the page.
5. **Escalation only when needed.** Plain HTTP with Chrome's TLS/HTTP2 fingerprint first;
   Safari's fingerprint on a challenge, block or rate limit; headless Chromium when the HTTP
   tier fails to fetch, or returns a challenge, block, rate limit, empty page or JavaScript
   shell, or the `expect` checks fail. southwest.com:
   `attempts: [http:chrome → js_required, browser:patchright → ok]`. `timeout` is one budget
   for all attempts.
6. **Visible latency.** `server-timing`, `timing`, `redirects` (every hop, gate detours
   and the browser's own redirects included) and `attempts` (every strategy tried and why
   it was rejected).
7. **Leaner Markdown.** Inline base64 images are reduced to their alt text: spiegel.de's
   Markdown went from 308,809 to about 143,000 characters, nu.nl's from 82,694 to 47,081
   (first run vs this run, same converter otherwise). Relative links and image sources are
   resolved the way a browser resolves them (against the page URL and `<base href>`), in
   both Markdown modes, in `links` and in microdata.
8. **Correct text on legacy encodings.** The WHATWG Encoding Standard's label table and
   decoders, as browsers use them: EUC-JP, Big5 (with HKSCS), Shift_JIS and EUC-KR decode
   the web-platform-tests corpora exactly, single-byte encodings follow WHATWG's indexes,
   and invalid bytes follow WHATWG's error rules. A declared charset is kept when only a
   few bytes are invalid (shown as U+FFFD, as in browsers; `encodingSource` ends in
   `-lossy`); when errors are pervasive, the declaration is taken to be wrong and a
   detector decides. `format: raw` returns the original bytes with the origin's
   `Content-Type`; when that names no charset, webfetch adds the one it decoded with, under
   its browser name (`Shift_JIS`, `EUC-JP`, `windows-1252`, ...).
9. **Safe to expose.**
   - Every connection is SSRF-checked: the HTTP tier's hops, gate callbacks, and in the
     browser every request through the egress proxy. IPv6 forms that embed an IPv4
     (IPv4-mapped, -compatible and -translated, 6to4, Teredo, NAT64) are judged by that IPv4.
   - Caller headers reach only the origin the caller named, except `Accept`,
     `Accept-Language`, `User-Agent`, `Cache-Control`, `Pragma`, `DNT` and
     `Upgrade-Insecure-Requests`. The browser tier cannot keep a header off a redirect
     target, so it gets only those: a `Cookie` header becomes cookies for the requested
     host, and the rest (`Authorization`, `X-Api-Key`, ...) are named in that attempt's
     `droppedHeaders`.
   - Decoded size, parse size, HTML and JSON nesting depth (page JSON nested deeper than
     128 levels is skipped), schema size and depth, and concurrent analysis are bounded;
     patterns that could backtrack are bounded.
   - An unexpected error is a JSON `500` (`internal_error`, with its `requestId`), never a
     bare one, and in a batch it fails only its own item.
   - Fetched pages are served with `Content-Security-Policy: sandbox`.
   - Browser renders cost 5 rate-limit tokens, at most 2 at a time per client.
10. **Batch.** `POST /v1/batch`: up to 20 fetches per call, 5 at a time by default
    (`concurrency` up to 10). Each result carries `httpStatus`, the status `/v1/fetch`
    would have answered for it as `format: json`.

The code and this write-up went through an independent three-part review (security,
correctness, and every claim checked against the data). Its 53 findings were fixed or, for
claims, corrected; the commits are titled "Fix the security review's findings" and "Fix the
correctness review's findings".

## 6. API

`POST /v1/fetch` takes String's core fields (`url`, `format`, `jsonSchema`, `executeJS`,
`headers`, `markdownMode`, `mainContentOnly`) plus:

| Field | Meaning |
|---|---|
| `format` | `json` (envelope, default), `markdown`, `text`, `html`, `raw` (original bytes, as an attachment) |
| `include` | for `json`: any of `markdown`, `html`, `text`, `metadata`, `jsonLd`, `microdata`, `links`, `hydration` (default `markdown`, `metadata`) |
| `expect` | `{contains, notContains, selector, minChars}`: what the real page must contain |
| `resolveGates` | default `true`; `false` leaves the wall in place and reports it (`502`; its content is included with `format: json`) |
| `render` | `auto` (default: browser only after the HTTP tier fails, see §5.5), `never`, `always` (= `executeJS: true`) |
| `cookies` | extra cookies for the target host |
| `jsonSchema` | an object schema with `properties`, or a root `$ref` into `$defs`/`definitions` (what Pydantic emits for self-referencing models); at most 50 KB, 200 keys and nesting depth 10, else `400` |
| `timeout`, `maxBytes` | budget for the whole request (≤ 90 s) and size cap (≤ 10 MB) |

Fields String has and webfetch does not (`countryCode`, `method`/`body`, `actions`,
`screenshot`, `captureXHR`, `solveCaptcha`, ...) are rejected with `400`.

Response headers: `x-request-id`, `x-outcome`, `x-final-url`, `x-strategy` and
`server-timing` on every fetch (after a `fetch_error`, `x-final-url` is the last URL
reached and `x-strategy` the last strategy tried); `x-status-code` whenever the destination
answered, so not on a `fetch_error`; `x-gates` only when a gate recipe acted (the
envelope's `gates` is `[]` otherwise); `x-encoding` with `format: raw`. The JSON envelope
adds `warnings` (`[{part, message}]`) when a structured part could not be computed from
the page, and a browser attempt lists the caller headers it did not send in
`droppedHeaders`.

Status codes: `200` means the destination's own answer (its status is in `statusCode`);
`502` means we could not get past what the destination served, and `outcome` says what it
was; `504` timeout; `400` invalid input; `403` destination not public (SSRF guard); `415`
binary content requested as markdown/text/html (`ok: false`); `429` rate limited; `500`
`internal_error`, a bug (the body carries the `requestId` to report).

## 7. Running it

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                   # 95 tests; a local server emulates the Steam,
                                            # DPG, ZEIT and Golem gate flows
python -m webfetch.cli https://www.nu.nl/   # one fetch, JSON line out
uvicorn --factory webfetch.api:create_app   # local API on :8000
modal deploy modal_app.py && modal deploy modal_browser.py
python3 bench/verify_live.py                # 21 pass/fail checks against the live endpoint
```

Environment: `WEBFETCH_API_KEYS` (comma-separated; when set, every request needs one),
`WEBFETCH_ANON_RATE` / `WEBFETCH_KEY_RATE` (requests per minute), `WEBFETCH_MAX_BYTES`.

## 8. Limits, stated plainly

- **zeit.de is not solved.** Its 403 is a Centinel Analytica JavaScript challenge that also
  refuses a headless Chromium from a datacenter IP. webfetch labels it correctly; getting
  through would need a residential or mobile exit, or a headful browser, which this
  deployment does not have. The same goes for sahibinden.com (Cloudflare Turnstile) and
  ozon.ru (refuses non-Russian datacenter IPs). Centinel does let the odd request through:
  of five tries in a row on Nov 17 from the Warsaw container, one got the full ZEIT front
  page over plain HTTP, returned as `ok` (`a279bcb3-8a8e-4477-a13a-cc8251582825`), and
  four were labelled challenges.
- **Gate recipes are per site.** Six recipes: Steam, the DPG Media network, ZEIT,
  derStandard, Golem, and Google/YouTube consent. Unknown walls are detected and labelled
  (`consent_wall`, `age_gate`), not resolved.
- **String with an account was not tested.** Its keyed `actions` (clicks) and AI
  `jsonSchema` might do better than the keyless API on some of these pages.
- **Modal picks the region.** Containers run wherever Modal has capacity: the Nov 16
  benchmarks ran on a US container, and on Nov 17 the endpoint went out through Azure in
  Warsaw. Sites that localise by IP follow the container: Steam shows złoty there, and
  GTA V has no age gate. There is no `countryCode`. `?cc=us` pins Steam's prices and age
  rule, and Modal's `region=` option on the functions (not set here) would pin the whole
  deployment. From a container in a country without the gate, Steam's `/agecheck/` sends
  the visitor straight back, so with `resolveGates: false` the fetch loops between `/app/`
  and `/agecheck/`; that loop is reported as `age_gate` (`502`) with every hop in
  `redirects` (the default, with the recipe, is unaffected).
- **The browser tier sends no credentials.** `Authorization`, API-key and other custom
  headers would follow Chromium's redirects to other origins, so `render: always` does not
  send them (see `droppedHeaders`); a `Cookie` header or the `cookies` field works, scoped
  to the requested host.
- **Some encodings are not WHATWG-exact.** GB18030 uses Python's codec, which differs from
  WHATWG on 20 two-byte codes and on the byte 0x80; ISO-2022-JP/KR and HZ use Python's
  codecs; a UTF-16 label without a byte-order mark is read as UTF-8.
- **Readable mode is trafilatura's main-content extraction.** `mainContentOnly` /
  `markdownMode: readable` drop asides and footnotes and can flatten code blocks nested in
  lists; the default mode keeps the whole page.
- **Extraction reads only what the page carries.** Fields that exist only as free text
  (not in JSON-LD, microdata, meta tags or a site extractor) come back `null`. An LLM
  fallback for those fields, with each value checked against the page text, is the
  natural next step.
- **The deployment is open** (no key), rate-limited to 30 requests per minute per client
  IP; set `WEBFETCH_API_KEYS` to require keys.
