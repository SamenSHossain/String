"""Consent walls and age gates: detect them and pass them the way a visitor would.

A *gate* is an interstitial a site serves instead of the page until the visitor clicks
"Accept" or confirms their age. Generic fetchers return the interstitial and report
success. Each recipe here does what the visitor's click does: it sets the cookie the
site sets on accept, or calls the site's own accept endpoint, then requests the page
again. Nothing here bypasses a login or a paywall; the content served is the content
any visitor gets after clicking through.

Recipes act in two ways:
  * ``preset``: cookies sent with the very first request, so the gate never appears
    (saves a round trip).
  * ``on_response``: inspect a response (usually a redirect to the gate) and return a
    ``GateAction`` telling the fetcher which cookies to add and which URL to load next.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urljoin, urlsplit


@dataclass
class GateEvent:
    site: str
    kind: str  # "consent_wall" | "age_gate"
    trigger: str  # "preset" | "redirect" | "page"
    detail: str

    def as_dict(self) -> dict:
        return {"site": self.site, "kind": self.kind, "trigger": self.trigger, "detail": self.detail}


@dataclass
class GateAction:
    url: str
    event: GateEvent
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_domain: str = ""
    # When set, ``url`` is an accept endpoint called for its cookies only; the fetch then
    # continues at ``then_url`` (the page the visitor originally asked for).
    then_url: str | None = None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _domain_match(host: str, domains: tuple[str, ...]) -> str | None:
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


class Recipe:
    name = "base"
    kind = "consent_wall"
    domains: tuple[str, ...] = ()

    def applies(self, host: str) -> str | None:
        return _domain_match(host, self.domains)

    def preset(self, host: str) -> dict[str, str]:
        return {}

    def on_response(
        self, *, original_url: str, url: str, status: int, location: str | None, head: str
    ) -> GateAction | None:
        return None


class DpgMediaPrivacyGate(Recipe):
    """DPG Media (nu.nl, tweakers.net, volkskrant.nl, ad.nl, hln.be, ...).

    The sites redirect first-time visitors to
    ``https://myprivacy.dpgmedia.nl/consent?siteKey=...&callbackUrl=<site>/privacy-gate/accept?redirectUri=/``.
    Clicking "Akkoord" sends the browser to ``callbackUrl`` with an ``authId``; the site
    then sets its consent cookies and redirects to ``redirectUri``. We make that same
    callback request (calibre's Volkskrant recipe does the same with a random UUID).
    """

    name = "dpg-media"
    domains = (
        "nu.nl", "tweakers.net", "volkskrant.nl", "ad.nl", "trouw.nl", "parool.nl", "hln.be",
        "demorgen.be", "7sur7.be", "ed.nl", "bndestem.nl", "pzc.nl", "tubantia.nl", "destentor.nl",
        "gelderlander.nl", "bd.nl", "autoweek.nl", "humo.be", "dpgmedia.nl", "dpgmedia.be",
        "qmusic.nl", "qmusic.be", "vtm.be", "nieuwsblad.be", "libelle.be", "flair.be", "goedgevoel.be",
        "margriet.nl", "libelle.nl", "donaldduck.nl", "kieskeurig.nl", "independer.nl",
    )
    GATE_HOSTS = ("myprivacy.dpgmedia.nl", "myprivacy.dpgmedia.be", "myprivacy.dpgmedia.net")

    def preset(self, host: str) -> dict[str, str]:
        # Long-standing DPG consent cookies (privacy wall version + accepted purposes).
        return {
            "pwv": "2",
            "pws": "functional|analytics|content_recommendation|targeted_advertising|social_media",
        }

    def on_response(self, *, original_url, url, status, location, head):
        target = urljoin(url, location) if location else url
        if _host(target) not in self.GATE_HOSTS:
            return None
        query = parse_qs(urlsplit(target).query)
        callback = (query.get("callbackUrl") or query.get("callbackurl") or [None])[0]
        if not callback:
            return None
        sep = "&" if "?" in callback else "?"
        accept_url = f"{callback}{sep}authId={uuid.uuid4()}"
        return GateAction(
            url=accept_url,
            event=GateEvent(self.name, self.kind, "redirect",
                            "privacy gate: called the site's accept callback (same request as clicking 'Akkoord')"),
        )


class SteamAgeGate(Recipe):
    """Steam store pages for mature titles redirect to /agecheck/app/<id>/.

    Submitting the birth-date form sets ``birthtime`` and ``lastagecheckage``; the
    content-descriptor interstitial sets ``wants_mature_content``. yt-dlp's Steam
    extractor uses the same cookies.
    """

    name = "steam"
    kind = "age_gate"
    domains = ("store.steampowered.com",)
    COOKIES = {
        "birthtime": "946684801",  # 2000-01-01T00:00:01Z
        "lastagecheckage": "1-January-2000",
        "wants_mature_content": "1",
        "mature_content": "1",
    }
    _AGECHECK = re.compile(r"^/agecheck/(app|bundle|sub)/(\d+)")

    def preset(self, host: str) -> dict[str, str]:
        return dict(self.COOKIES)

    def on_response(self, *, original_url, url, status, location, head):
        target = urljoin(url, location) if location else url
        m = self._AGECHECK.match(urlsplit(target).path)
        if not m or _host(target) not in self.domains:
            return None
        if self._AGECHECK.match(urlsplit(original_url).path):
            next_url = f"https://store.steampowered.com/{m.group(1)}/{m.group(2)}/"
        else:
            next_url = original_url
        return GateAction(
            url=next_url,
            cookies=dict(self.COOKIES),
            cookie_domain="store.steampowered.com",
            event=GateEvent(self.name, self.kind, "redirect",
                            "age check: set the birth-date and mature-content cookies the form sets"),
        )


class CookieRetryRecipe(Recipe):
    """Sites that redirect to a consent page and accept a known consent cookie."""

    gate_path: re.Pattern[str] = re.compile(r"$^")
    return_param = "url"
    detail = ""

    def cookies(self) -> dict[str, str]:
        return {}

    def preset(self, host: str) -> dict[str, str]:
        return self.cookies()

    def on_response(self, *, original_url, url, status, location, head):
        target = urljoin(url, location) if location else url
        parts = urlsplit(target)
        if not self.applies(_host(target)) or not self.gate_path.search(parts.path):
            return None
        back = (parse_qs(parts.query).get(self.return_param) or [None])[0]
        next_url = back if back and back.startswith(("http://", "https://")) else original_url
        domain = "." + (self.applies(_host(original_url)) or self.applies(_host(target)) or "")
        return GateAction(
            url=next_url,
            cookies=self.cookies(),
            cookie_domain=domain,
            event=GateEvent(self.name, self.kind, "redirect", self.detail),
        )


class ZeitConsent(CookieRetryRecipe):
    name = "zeit-online"
    domains = ("zeit.de",)
    gate_path = re.compile(r"^/zustimmung")
    return_param = "url"
    detail = "consent page: set the zonconsent cookie that accepting sets"

    def cookies(self):
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"zonconsent": now}


class DerStandardConsent(CookieRetryRecipe):
    name = "derstandard"
    domains = ("derstandard.at", "derstandard.de")
    gate_path = re.compile(r"^/consent/")
    return_param = "redirectUrl"
    detail = "consent page: set the DSGVO_ZUSAGE_V1 cookie that accepting sets"

    def cookies(self):
        return {"DSGVO_ZUSAGE_V1": "true"}


class GolemConsent(CookieRetryRecipe):
    """golem.de redirects to /sonstiges/zustimmung/auswahl.html?from=<url>. Its accept
    button calls ``<cookieUrl>/setconsentcookie.php?type=cmp&version=<cookieVersion>&...``,
    which sets ``golem_consent20=cmp<cookieVersion>``. We read cookieUrl, cookieVersion
    and cookieExpire from the consent page's own config and make that same call."""

    name = "golem"
    domains = ("golem.de",)
    gate_path = re.compile(r"^/sonstiges/zustimmung/")
    return_param = "from"
    detail = "consent page: set the golem_consent20 cookie that accepting sets"
    FALLBACK_VERSION = "|261101"
    _VERSION = re.compile(r"""["']?cookieVersion["']?\s*:\s*["'](\|?\d{3,8})["']""")
    _EXPIRE = re.compile(r"""["']?cookieExpire["']?\s*:\s*(\d{1,4})""")
    _URL = re.compile(r"""["']?cookieUrl["']?\s*:\s*["'](https?://[^"']+)["']""")

    def __init__(self):
        self.version = self.FALLBACK_VERSION

    def cookies(self):
        return {"golem_consent20": "cmp" + self.version}

    def on_response(self, *, original_url, url, status, location, head):
        here = urlsplit(url)
        if status != 200 or not self.applies(_host(url)) or not self.gate_path.search(here.path):
            return None
        versions = [v for v in self._VERSION.findall(head) if v.strip("|0")]  # skip the "|000" default
        if versions:
            self.version = versions[-1] if versions[-1].startswith("|") else "|" + versions[-1]
        expire = (self._EXPIRE.findall(head) or ["90"])[-1]
        base = (self._URL.findall(head) or ["https://www.golem.de/abo"])[-1].rstrip("/")
        back = (parse_qs(here.query).get(self.return_param) or [original_url])[0]
        if not back.startswith(("http://", "https://")):
            back = original_url
        accept = (f"{base}/setconsentcookie.php?type=cmp&version={quote(self.version, safe='')}"
                  f"&expire={expire}&referer={quote(back, safe='')}")
        return GateAction(
            url=accept,
            then_url=back,
            cookies=self.cookies(),
            cookie_domain="." + (self.applies(_host(url)) or "golem.de"),
            event=GateEvent(self.name, self.kind, "page",
                            f"consent page: called setconsentcookie.php (version {self.version}), "
                            "the request its accept button makes"),
        )


class GoogleConsent(Recipe):
    """consent.google.<tld> / consent.youtube.com interstitials (served to EU visitors)."""

    name = "google-consent"
    _HOST = re.compile(r"(?:^|\.)(google\.(?:com?\.)?[a-z]{2,3}|youtube\.com)$")

    def applies(self, host: str) -> str | None:
        m = self._HOST.search(host)
        return m.group(1) if m else None

    def preset(self, host: str) -> dict[str, str]:
        return {"SOCS": "CAI"}  # "reject all" consent record: essential cookies only

    def on_response(self, *, original_url, url, status, location, head):
        target = urljoin(url, location) if location else url
        host = _host(target)
        if not (host.startswith("consent.google.") or host == "consent.youtube.com"):
            return None
        back = (parse_qs(urlsplit(target).query).get("continue") or [original_url])[0]
        # The cookie must be set on the site being returned to (google.se, google.co.uk, ...).
        base = self.applies(_host(back)) or self.applies(_host(original_url)) or host.split(".", 1)[1]
        return GateAction(
            url=back,
            cookies={"SOCS": "CAI"},
            cookie_domain="." + base,
            event=GateEvent(self.name, self.kind, "redirect", "consent page: set the SOCS consent cookie"),
        )


RECIPES: tuple[Recipe, ...] = (
    DpgMediaPrivacyGate(),
    SteamAgeGate(),
    ZeitConsent(),
    DerStandardConsent(),
    GolemConsent(),
    GoogleConsent(),
)


def presets_for(host: str) -> list[tuple[Recipe, str, dict[str, str]]]:
    """Recipes that apply to ``host``, with the cookie domain and cookies to preset."""
    out = []
    for r in RECIPES:
        matched = r.applies(host)
        if matched:
            cookies = r.preset(host)
            if cookies:
                domain = host if r.name == "steam" else "." + matched
                out.append((r, domain, cookies))
    return out


def check_response(*, original_url: str, url: str, status: int, location: str | None, head: str) -> GateAction | None:
    for r in RECIPES:
        action = r.on_response(original_url=original_url, url=url, status=status, location=location, head=head)
        if action is not None:
            return action
    return None


# Patterns for gates no recipe resolves; used by the classifier to label them honestly.
CONSENT_URL_PATTERNS = re.compile(
    r"(^|\.)(myprivacy\.dpgmedia\.[a-z]+|consent\.google\.[a-z.]+|consent\.youtube\.com|guce\.yahoo\.com|"
    r"consent\.yahoo\.com|consent\.[a-z0-9-]+\.[a-z.]+)$"
)
CONSENT_PATH_PATTERNS = re.compile(
    r"/(consent|cookie-?consent|cookie-?wall|cookiewall|privacy-?gate|privacygate|privacy-wall|zustimmung|"
    r"einwilligung|gdpr-consent|consent-wall)(/|$|\?|\.html)",
    re.I,
)
AGE_PATH_PATTERNS = re.compile(r"/(agecheck|age-?gate|age-?verification|ageverify|age_check|verify-age)(/|$|\?)", re.I)
