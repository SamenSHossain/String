"""Regression tests for the second review's charset findings (PERF-4, SEC-1, PERF-6, INT-4,
SEM-1, SEM-3, SEM-4)."""

from __future__ import annotations

import asyncio
import codecs
import collections
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from webfetch import decode
from webfetch.api import create_app
from webfetch.config import Settings
from webfetch.decode import decode_body
from webfetch.fetcher import HttpFetcher
from webfetch.service import Expect, FetchOptions, run_fetch

FFFD = "\ufffd"
MAX_STEPS = 50_000  # decode._MAX_STEPS, spelled out: the bounds below are part of the contract
MIB = 1024 * 1024

JA = "日本語のテキストです。漢字とひらがなとカタカナ。"
ZH = "中文網頁內容，歡迎光臨本站，謝謝您的支持。"
KO = "한국어 문장입니다. 문의는 이쪽으로 해 주세요. "


# --- PERF-4, SEC-1, PERF-6: hostile bytes cost what C code costs -------------------------------

@pytest.fixture
def handler_calls():
    """Counts calls of the error handlers decode.py registers: its work in Python per sequence."""
    calls: collections.Counter = collections.Counter()
    originals = {}
    for name in ("webfetch-whatwg", "webfetch-whatwg-clean", "webfetch-replace"):
        try:
            originals[name] = codecs.lookup_error(name)
        except LookupError:
            continue

        def counting(err, name=name):
            calls[name] += 1
            return originals[name](err)

        codecs.register_error(name, counting)
    yield calls
    for name, handler in originals.items():
        codecs.register_error(name, handler)


def _big5_then(junk: bytes, size: int) -> bytes:
    """Real Big5 text for the detector's sample, then junk: every decode is a handler codec."""
    head = b'<meta charset="cseuckr">' + ZH.encode("big5hkscs") * 13_000
    return head + junk * ((size - len(head)) // len(junk))


@pytest.mark.parametrize("ctype,unit", [
    ("text/html; charset=euc-jp", b"\x80"), ("text/html; charset=big5", b"\x80"),
    ("text/html; charset=euc-kr", b"\xff"), ("text/html; charset=shift_jis", b"\x81\x20"),
    ("text/html; charset=euc-jp", b"\xad\xa1"),  # a character the handler supplies (NEC row 13)
    ("text/html; charset=big5", b"\x87\x40\xa3\xe1"),  # HKSCS-2008 is Python's, the euro sign the handler's
    ("text/html; charset=big5", b"\xa2A\x80"), ("text/html; charset=euc-jp", b"\x8f\xa2\xb7\x80"),
    ("text/html; charset=euc_jis_2004", b"\x80"),  # a detector's name for EUC-JP
    ("text/html; charset=utf_16_le", b"\x00\xd8"), ("text/html; charset=utf-32", b"\xff"),
    ("text/html; charset=cp856", b"\x9f"),  # one of Python's charmap codecs, outside WHATWG's table
])
def test_bad_sequences_cost_a_bounded_number_of_handler_calls(handler_calls, ctype, unit):
    body = unit * 400_000
    text, enc, src = decode_body(body, ctype)
    assert sum(handler_calls.values()) <= MAX_STEPS + 1  # one decode needed the handler: the declared one
    assert isinstance(text, str) and text


def test_every_decode_of_a_body_is_bounded(handler_calls):
    # header EUC-JP, <meta> EUC-KR, and a detector that says Big5: three decodes that need the handler
    body = _big5_then(b"\xa2A\x80", 2 * MIB)
    text, enc, src = decode_body(body, "text/html; charset=euc-jp")
    assert (enc, src) == ("big5hkscs", "detected") and ZH in text
    assert sum(handler_calls.values()) <= 3 * (MAX_STEPS + 2)
    # Bad bytes up front, then enough text for both declarations to pass as having few errors:
    # each is also sampled against the detector's guess, and a sample gets 1,000 steps.
    handler_calls.clear()
    body = b'<meta charset="euc-kr">' + b"\x80" * 60_000 + KO.encode("cp949") * 20_000
    text, enc, src = decode_body(body, "text/html; charset=big5")
    assert KO * 20_000 in text
    assert MAX_STEPS < sum(handler_calls.values()) <= 3 * (MAX_STEPS + 1) + 4 * 1001


@pytest.mark.parametrize("make,ctype", [
    (lambda: b"\x80" * (10 * MIB), "text/html; charset=euc-jp"),
    (lambda: b"\xa2A\x80" * (10 * MIB // 3), "text/html; charset=big5"),
    (lambda: b'<meta charset="big5">' + b"\x80" * (10 * MIB), "text/html; charset=euc-jp"),
    (lambda: _big5_then(b"\xa2A\x80", 10 * MIB), "text/html; charset=euc-jp"),
])
def test_ten_mebibytes_of_hostile_bytes_decode_quickly(make, ctype):
    # 0.1-0.3 s here; 6-12 s when a Python handler ran for every sequence. The handler-call
    # bounds above are the exact form of this; the clock is only a backstop with a wide margin.
    body = make()
    start = time.perf_counter()
    decode_body(body, ctype)
    assert time.perf_counter() - start < 3.0


def test_steps_are_counted_per_decode_and_per_thread():
    assert decode._MAX_STEPS == MAX_STEPS
    pages = [(b"\xad\xa1" * 30_000, "text/html; charset=euc-jp"), (b"\x80\xa2A" * 20_000, "text/html; charset=big5"),
             (b"\xff\xfe" + b"\x00\xd8A\x00" * 20_000, "text/html"), (b"\x81\x20" * 40_000, "text/html; charset=sjis")]
    alone = [decode_body(*page) for page in pages]
    with ThreadPoolExecutor(4) as pool:
        together = list(pool.map(lambda page: decode_body(*page), pages * 8))
    assert together == alone * 8
    assert alone[0] == ("①" * 30_000, "euc_jp", "http-header")  # one decode's use does not leak into the next
    assert alone[2] == ((FFFD + "A") * 20_000, "utf-16-le", "bom")


def test_within_the_budget_special_sequences_are_exact():
    # 12,000 company names with the NEC ligature, an IBM kanji, a JIS X 0212 tilde and a circled
    # digit: four steps each, and far more of them than a real page has
    row = "<li>㈱髙橋商事\uff5e①</li>"
    page = b"<ul>" + (b"<li>\xad\xea\xfc\xe2\xb6\xb6\xbe\xa6\xbb\xf6\x8f\xa2\xb7\xad\xa1</li>" * 12_000) + b"</ul>"
    text, enc, src = decode_body(page, "text/html; charset=euc-jp")
    assert (enc, src) == ("euc_jp", "http-header") and text == "<ul>" + row * 12_000 + "</ul>"
    page = ZH.encode("big5hkscs") + b"\xa2A\xa2B\xa3\xe1\x87\x40" * 12_000  # twins, the euro sign, HKSCS-2008
    text, enc, src = decode_body(page, "text/html; charset=big5")
    assert (enc, src) == ("big5hkscs", "http-header") and text == ZH + "\u2215\ufe68€\u43f0" * 12_000


@pytest.mark.parametrize("label,codec,twin,exact,python", [
    ("big5", "big5hkscs", b"\xa2A", "\u2215", "\uff0f"), ("big5", "big5hkscs", b"\xa2B", "\ufe68", "\uff3c"),
    ("euc-jp", "euc_jp", b"\x8f\xa2\xb7", "\uff5e", "~"),
])
def test_twin_sequences_beyond_the_budget_decode_as_python_has_them(label, codec, twin, exact, python):
    # The scan that tells a twin from its other byte sequence is a Python loop: it stops at the
    # budget, and the rest of the body is one C decode.
    text, enc, src = decode_body(twin * (MAX_STEPS + 1000), "text/html; charset=" + label)
    assert (enc, src) == (codec, "http-header")
    assert text == exact * MAX_STEPS + python * 1000


def test_beyond_the_budget_the_whole_body_is_still_decoded():
    good = JA * 25_000
    page = good.encode("euc_jp") + b"\x80" * (MAX_STEPS + 10_000) + b"<p>" + JA.encode("euc_jp") + b"</p>"
    text, enc, src = decode_body(page, "text/html; charset=euc-jp")
    assert (enc, src) == ("euc_jp", "http-header-lossy")
    assert text == good + FFFD * (MAX_STEPS + 10_000) + "<p>" + JA + "</p>"


def test_utf16_with_a_bom_and_errors_within_the_budget_is_unchanged(handler_calls):
    body = "<p>café \U0001f600</p>".encode("utf-16-le") + b"\x00\xd8" * 1000 + "<p>fin</p>".encode("utf-16-le") + b"\x41"
    assert decode_body(codecs.BOM_UTF16_LE + body, "text/html; charset=windows-1252") == (
        "<p>café \U0001f600</p>" + FFFD * 1000 + "<p>fin</p>" + FFFD, "utf-16-le", "bom")
    assert sum(handler_calls.values()) == 1001


@pytest.mark.parametrize("bom,unit", [(codecs.BOM_UTF16_LE, b"\x00\xd8"), (codecs.BOM_UTF16_BE, b"\xdc\x00")])
def test_utf16_bom_over_bytes_that_are_not_utf16_is_not_believed(handler_calls, bom, unit):
    # Python's UTF-16 decoder calls a handler for every lone surrogate: past the budget the body is
    # taken not to be UTF-16, and the rules for a body without a BOM decide.
    text, enc, src = decode_body(bom + unit * 400_000, "text/html")
    assert src != "bom" and len(text) >= 400_000
    assert sum(handler_calls.values()) <= 2 * (MAX_STEPS + 1)  # the BOM's decode, the detector's guess


def test_single_byte_tables_never_call_a_handler():
    # charmap_decode calls the error handler for every U+FFFE in its table; the tables hold the
    # U+FFFD that an undefined byte decodes to instead.
    for codec in sorted(decode._SINGLE_BYTE):
        assert "\ufffe" not in decode._byte_table(codec)
    assert decode._decode(b"\xe1\xae\xd2\xff" * 3, "iso8859-7") == (("α" + FFFD * 3) * 3, 9)


@pytest.mark.parametrize("label,text", [("EUC-JP", JA), ("Shift_JIS", JA), ("Big5", ZH), ("EUC-KR", KO),
                                        ("x-euc-jp", "café \U0001f600 " + JA)])
def test_utf8_body_under_a_cjk_label_is_not_decoded_twice(handler_calls, label, text):
    # What a UTF-8 page with a stale legacy header looks like: the declared decode ends at its
    # first error instead of running the handler over every character of the page.
    page = "<html><head><meta charset={}></head><body>{}</body></html>".format(label.lower(), "<p>" + text * 2000 + "</p>")
    decoded, enc, src = decode_body(page.encode("utf-8"), "text/html; charset=" + label)
    assert (decoded, enc, src) == (page, "utf-8", "utf-8-valid")
    assert sum(handler_calls.values()) <= 1


def test_declared_charset_that_decodes_valid_utf8_bytes_cleanly_still_wins():
    # As in browsers: bytes that are valid in both are read as declared.
    page = b"<p>\xc3\xa9\xc2\xa1</p>"
    assert page.decode("utf-8") == "<p>é\u00a1</p>"
    assert decode_body(page, "text/html; charset=euc-kr") == ("<p>" + b"\xc3\xa9\xc2\xa1".decode("cp949") + "</p>",
                                                             "cp949", "http-header")
    assert decode_body(page, "text/html; charset=windows-1252")[1:] == ("cp1252", "http-header")
    assert decode_body(page, "text/html; charset=utf-8") == ("<p>é\u00a1</p>", "utf-8", "http-header")
    assert decode_body(page, "text/html") == ("<p>é\u00a1</p>", "utf-8", "utf-8-valid")


# --- INT-4: a browser-tier body is the DOM in UTF-8, whatever the origin declared --------------

RU = "Привет, мир. Это тестовая страница. "
DE = "Grüße aus München, schöne Grüße! "
CHROMIUM = os.environ.get("WEBFETCH_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
try:
    import patchright  # noqa: F401
    HAVE_BROWSER = os.path.exists(CHROMIUM)
except ImportError:  # CI installs only the HTTP-tier requirements
    HAVE_BROWSER = False
needs_browser = pytest.mark.skipif(not HAVE_BROWSER, reason="no local Chromium")


class DomBrowser:
    """A browser tier that hands back one rendered page: the origin's response headers, and the
    DOM as text (which keeps the page's own <meta charset>)."""

    def __init__(self, headers: dict, text: str, meta: str | None = None):
        tag = f'<meta charset="{meta}">' if meta else ""
        self.html = f"<html><head>{tag}<title>T</title></head><body><p>{text * 30}</p></body></html>"
        self.headers = headers

    async def render(self, url, *, cookies, headers, timeout, max_bytes=0):
        return {"finalUrl": url, "status": 200, "headers": self.headers, "html": self.html, "ms": 5.0,
                "strategy": "browser:fake"}


def _rendered(settings, browser, **opts) -> dict:
    async def go():
        fetcher = HttpFetcher(settings)
        try:
            return await run_fetch(FetchOptions(url="http://127.0.0.1:9/page", render="always", **opts), fetcher, browser)
        finally:
            await fetcher.aclose()

    return asyncio.run(go())


@pytest.mark.parametrize("ctype,meta,text", [
    ("text/html; charset=koi8-r", None, RU),  # lib.ru, opennet.ru
    ("text/html; charset=windows-1251", "windows-1251", RU), ("text/html", "windows-1251", RU),
    ("text/html; charset=iso-8859-1", None, DE), ("text/html", "windows-1252", DE), ("text/html", "x-user-defined", DE),
    ("text/html; charset=iso-8859-5", "koi8-r", RU), ("text/html;charset=\"windows-1250\"", None, DE),
    ("text/html; charset=Shift_JIS", "shift_jis", JA), ("text/html; charset=euc-jp", None, JA),
    ("text/html; charset=gbk", "gb2312", ZH), ("text/html; charset=big5", None, ZH), ("text/html; charset=euc-kr", None, KO),
    ("text/html; charset=utf-16", "utf-16", RU), ("text/html; charset=utf-8", "utf-8", JA), ("text/html", None, KO),
    (None, None, RU), (None, "iso-8859-7", DE),
])
def test_browser_tier_text_is_the_dom_whatever_charset_the_origin_declared(settings, handler_calls, ctype, meta, text):
    browser = DomBrowser({"Content-Type": ctype} if ctype else {}, text, meta)
    env = _rendered(settings, browser, include={"text", "html"})
    assert env["outcome"] == "ok" and env["strategy"] == "browser:fake"
    assert env["text"].count(text.strip()) == 30 and env["html"] == browser.html
    assert (env["encoding"], env["encodingSource"]) == ("utf-8", "browser")
    assert env["contentType"] == "text/html; charset=utf-8"
    assert not handler_calls  # and no legacy decoder ran over the UTF-8 bytes first (PERF-6)


@pytest.mark.parametrize("origin,served", [
    ("text/html; charset=windows-1251", "text/html; charset=utf-8"),
    ("TEXT/HTML;Charset=\"KOI8-R\"", "TEXT/HTML; charset=utf-8"),
    ("application/xhtml+xml; charset = iso-8859-1; profile=x", "application/xhtml+xml; profile=x; charset=utf-8"),
    ("text/html", "text/html; charset=utf-8"), ("charset=koi8-r", "text/html; charset=utf-8"),
    ("text/html; charset=utf-8", "text/html; charset=utf-8"), (None, "text/html; charset=utf-8"),
])
def test_browser_tier_raw_output_is_labelled_utf8(origin, served):
    browser = DomBrowser({"content-type": origin} if origin else {}, RU, "windows-1251")
    client = TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000), browser=browser))
    r = client.post("/v1/fetch", json={"url": "http://127.0.0.1:9/page", "render": "always", "format": "raw"})
    assert r.status_code == 200 and r.headers["x-strategy"] == "browser:fake"
    assert r.headers["content-type"] == served and r.headers["x-encoding"] == "utf-8"
    assert r.content == browser.html.encode("utf-8")
    r = client.post("/v1/fetch", json={"url": "http://127.0.0.1:9/page", "render": "always", "format": "markdown"})
    assert r.status_code == 200 and r.text.count(RU.strip()) == 30


def test_expectations_on_a_browser_tier_page_see_its_real_text(settings):
    # decoded with the origin's charset, the page failed an `expect` in its own language
    browser = DomBrowser({"content-type": "text/html; charset=koi8-r"}, RU)
    env = _rendered(settings, browser, expect=Expect(contains=["тестовая страница"]))
    assert env["outcome"] == "ok" and env["validation"]["passed"] is True


@needs_browser
@pytest.mark.parametrize("query,http_tier", [
    ("codec=koi8_r&header=koi8-r", ("koi8-r", "http-header")),
    ("codec=cp1251&meta=windows-1251", ("cp1251", "meta")),
    ("codec=cp1251&header=windows-1251&meta=windows-1251", ("cp1251", "http-header")),
])
def test_chromium_rendered_legacy_page_is_not_mojibake(server, query, http_tier):
    from webfetch.browser import BrowserPool

    url = f"http://127.0.0.1:{server.server_address[1]}/encoded?{query}&text={quote(RU)}"

    async def go():
        pool = BrowserPool(allow_private=True, executable_path=CHROMIUM)
        app = create_app(Settings(allow_private=True, anon_rate_per_min=10_000), browser=pool)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
                return [await client.post("/v1/fetch", json={"url": url, "timeout": 20, **body}, timeout=60)
                        for body in ({"render": "never", "include": ["text"]}, {"render": "always", "include": ["text"]},
                                     {"render": "always", "format": "raw"})]
        finally:
            await pool.stop()

    plain, rendered, raw = asyncio.run(go())
    plain, rendered = plain.json(), rendered.json()
    assert plain["strategy"].startswith("http:") and (plain["encoding"], plain["encodingSource"]) == http_tier
    assert rendered["strategy"].startswith("browser:") and rendered["outcome"] == "ok"
    assert (rendered["encoding"], rendered["encodingSource"]) == ("utf-8", "browser")
    assert plain["text"].count(RU.strip()) == 30 and rendered["text"] == plain["text"]
    assert raw.headers["content-type"] == "text/html; charset=utf-8" and raw.headers["x-encoding"] == "utf-8"
    assert raw.content.decode("utf-8").count(RU.strip()) == 30


# --- SEM-1: few errors keep a declared legacy multi-byte charset only if its text reads as text --

# A paragraph of ordinary prose per language, and the codec a page of it is really in.
PROSE = {
    "euc_jp": "全文検索システムNamazuは、手軽に使えることを第一に目指した日本語全文検索システムです。CGIとして動作させることにより"
              "Webサーバ用の全文検索エンジンとして利用できるほか、個人のハードディスク内の文書やメールの検索にも使うことができます。"
              "インデックスを事前に作成しておくことで、大量の文書の中から目的の単語を含むものを高速に探し出します。対応している文書形式は、"
              "プレーンテキストやHTMLのほか、フィルタを追加することでPDFやワープロソフトの文書などにも広がります。",
    "cp949": "서울특별시는 대한민국의 수도이자 최대 도시이다. 한강을 사이에 두고 강북과 강남으로 나뉘며, 조선 시대부터 육백 년 넘게 나라의 "
             "중심지 역할을 해 왔다. 오늘날에는 정치와 경제, 문화와 교육의 중심으로서 천만 명에 가까운 사람들이 살고 있다. 지하철과 버스 "
             "노선이 촘촘하게 연결되어 있어 시내 어디든 편리하게 이동할 수 있고, 경복궁과 창덕궁 같은 고궁과 남산이 어우러져 많은 관광객이 찾는다.",
    "big5hkscs": "臺北市是中華民國的首都，位於臺灣北部的臺北盆地，四周與新北市相鄰。這座城市是全國的政治、經濟與文化中心，人口約有兩百五十萬人。"
                 "市內交通便利，捷運路線四通八達，公車與公共自行車也相當普及。著名的景點包括國立故宮博物院、中正紀念堂、龍山寺以及士林夜市，"
                 "每年吸引大量來自世界各地的旅客。臺北的氣候屬於副熱帶季風氣候，夏季炎熱多雨，冬季則溫和潮濕。",
    "gb18030": "北京是中华人民共和国的首都，也是全国的政治、文化和国际交往中心。这座城市有三千多年的建城历史，故宫、天坛、颐和园和长城等名胜古迹"
               "每年吸引着数以千万计的游客。随着经济的快速发展，北京的城市面貌发生了巨大的变化，高楼大厦与古老的胡同并存，地铁线路已经覆盖了"
               "大部分城区。这里聚集了许多著名的高等院校和科研机构，是中国科技创新的重要基地。",
}
PROSE["cp932"] = PROSE["euc_jp"]
LABELS = {"euc_jp": "EUC-JP", "cp932": "Shift_JIS", "cp949": "EUC-KR", "big5hkscs": "Big5", "gb18030": "GBK"}
# One sequence per codec that its decoder answers with a single U+FFFD.
BAD = {"euc_jp": b"\xa9\xa1", "cp932": b"\x81\xfd", "big5hkscs": b"\xc8\xf2", "cp949": b"\xc9\x80", "gb18030": b"\xff"}


def _prose_page(codec: str, meta: str | None = None, bad: int = 0) -> tuple[bytes, str]:
    """An HTML page of PROSE[codec] with `bad` invalid sequences spread through its text, and
    the text a browser makes of it."""
    text = PROSE[codec] * 2
    cuts = [len(text) * i // (bad + 1) for i in range(bad + 2)]
    parts = [text[a:b] for a, b in zip(cuts, cuts[1:])]
    before = "<html><head>" + (f'<meta charset="{meta}">' if meta else "") + "<title>Page</title></head><body><p>"
    after = "</p></body></html>"
    page = before.encode() + BAD[codec].join(part.encode(codec) for part in parts) + after.encode()
    return page, before + FFFD.join(parts) + after


def _declared(codec: str, via: str, **page) -> tuple[bytes, str, str]:
    """(page, the text a browser makes of it, Content-Type) with `codec` declared in the header or a <meta>."""
    if via == "meta":
        return *_prose_page(page.pop("real", codec), meta=LABELS[codec], **page), "text/html"
    return *_prose_page(page.pop("real", codec), **page), "text/html; charset=" + LABELS[codec]


# real: the page's codec; declared: what its header or <meta> says instead. In each pair the
# wrong decoder takes most byte pairs for other characters and fails on a few per cent of them.
WRONG_LABELS = [("euc_jp", "cp932"), ("euc_jp", "cp949"), ("cp932", "cp949"), ("cp949", "cp932"), ("cp949", "big5hkscs"),
                ("big5hkscs", "cp932"), ("gb18030", "big5hkscs"), ("gb18030", "cp932"), ("gb18030", "euc_jp"),
                ("gb18030", "cp949")]


@pytest.mark.parametrize("real,declared", WRONG_LABELS)
@pytest.mark.parametrize("via", ["http-header", "meta"])
def test_wrong_legacy_multibyte_label_with_few_errors_is_left_to_the_detector(real, declared, via):
    page, shown, ctype = _declared(declared, via, real=real)
    wrong, errors = decode._decode(page, declared)
    assert errors and decode._few_errors(wrong, errors) and PROSE[real][:20] not in wrong  # mojibake that mostly decodes
    assert decode_body(page, ctype) == (shown, real, "detected")


@pytest.mark.parametrize("codec", sorted(PROSE))
@pytest.mark.parametrize("bad", [1, 2, 3, 8, 20])
@pytest.mark.parametrize("via", ["http-header", "meta"])
def test_right_legacy_multibyte_label_with_bad_sequences_keeps_its_declaration(codec, bad, via):
    # What the tolerance is for: the declared charset stays, and each bad sequence is one U+FFFD.
    page, shown, ctype = _declared(codec, via, bad=bad)
    assert shown.count(FFFD) == bad
    assert decode_body(page, ctype) == (shown, codec, via + "-lossy")


@pytest.mark.parametrize("codec", sorted(PROSE))
def test_right_legacy_multibyte_label_on_a_page_cut_mid_character_keeps_its_declaration(codec):
    # maxBytes, or a connection that drops: the last character is half there
    page, shown = _prose_page(codec)
    at = next(i for i in range(shown.index("<p>") + 300, len(shown)) if not shown[i].isascii())
    kept = shown[:at]
    cut = page[:len(kept.encode(codec)) + 1]
    assert decode_body(cut, "text/html; charset=" + LABELS[codec]) == (kept + FFFD, codec, "http-header-lossy")


def test_random_stray_bytes_do_not_cost_a_right_declaration():
    rng = random.Random(20261118)
    for codec in sorted(PROSE):
        for strays in (1, 3, 10, 30):
            raw = bytearray(_prose_page(codec)[0])
            for _ in range(strays):
                at = rng.randrange(60, len(raw) - 20)
                raw[at:at] = bytes((rng.randrange(0x80, 0x100),))
            text, enc, src = decode_body(bytes(raw), "text/html; charset=" + LABELS[codec])
            assert enc == codec and src in ("http-header", "http-header-lossy"), (codec, strays, enc, src)


def test_detector_guess_that_decodes_cleanly_is_not_enough():
    # GB18030 decodes Korean bytes without a single error, as other characters: a clean decode by
    # the detector's guess must not take a right EUC-KR declaration with stray bytes away.
    page, shown, ctype = _declared("cp949", "http-header", bad=3)
    assert decode._decode(page.replace(BAD["cp949"], b""), "gb18030")[1] == 0
    assert decode_body(page, ctype) == (shown, "cp949", "http-header-lossy")


def test_wrong_header_does_not_hide_a_right_meta_with_a_bad_sequence():
    page, shown = _prose_page("euc_jp", meta="euc-jp", bad=1)
    assert decode_body(page, "text/html; charset=Shift_JIS") == (shown, "euc_jp", "meta-lossy")


def test_too_little_text_to_judge_keeps_the_declaration():
    # web-platform-tests encoding/eof-shift_jis.html: an ASCII page that ends in a lead byte
    page = b"<!doctype html>\n<meta charset=shift_jis>\n<title>Shift_JIS file ending with a truncated sequence</title>\n\x81"
    text, enc, src = decode_body(page, "text/html")
    assert (enc, src) == ("cp932", "meta-lossy") and text.endswith("sequence</title>\n" + FFFD)


def test_wrong_label_is_fixed_through_the_api(server):
    client = TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000)))
    query = f"codec=euc_jp&header=Shift_JIS&text={quote(PROSE['euc_jp'])}"
    r = client.post("/v1/fetch", json={"url": f"http://127.0.0.1:{server.server_address[1]}/encoded?{query}",
                                       "render": "never", "include": ["text"]})
    env = r.json()
    assert r.status_code == 200 and (env["encoding"], env["encodingSource"]) == ("euc_jp", "detected")
    assert env["text"].count(PROSE["euc_jp"]) == 30


# --- SEM-3: a stale single-byte <meta> does not beat a UTF-8 header with a few bad bytes --------

GERMAN = "<p>Die Bürgerinnen und Bürger in München können ab Montag Anträge für die neue Förderung stellen.</p>\n"


@pytest.mark.parametrize("meta", ['<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">',
                                  '<meta charset="windows-1252">', "<meta charset=koi8-r>", '<meta charset="x-mac-roman">'])
def test_utf8_header_with_a_stray_byte_is_kept_over_a_stale_single_byte_meta(meta):
    shown = "<html><head>" + meta + "<title>Stadt</title></head><body>" + GERMAN * 20 + "<p>It" + FFFD + "s done.</p></body></html>"
    page = shown.encode("utf-8").replace(FFFD.encode("utf-8"), b"\x92")  # one cp1252 apostrophe in UTF-8 content
    assert decode_body(page, "text/html; charset=utf-8") == (shown, "utf-8", "http-header-lossy")
    # without the header the <meta> is the page's only declaration, for browsers too
    text, enc, src = decode_body(page, "text/html")
    assert src == "meta" and enc != "utf-8" and FFFD not in text


def test_utf8_page_cut_mid_character_is_kept_over_a_stale_single_byte_meta():
    shown = '<html><head><meta charset="iso-8859-1"></head><body>' + GERMAN * 20
    cut = shown.encode("utf-8")
    cut = cut[:cut.rindex("ö".encode("utf-8")) + 1]  # maxBytes fell inside the last "ö"
    assert decode_body(cut, "text/html; charset=UTF-8") == (shown[:shown.rindex("ö")] + FFFD, "utf-8", "http-header-lossy")


def test_wrong_utf8_header_still_loses_to_a_right_single_byte_meta():
    page = ('<html><head><meta charset="windows-1251"></head><body><p>' + RU * 20 + "</p></body></html>").encode("cp1251")
    text, enc, src = decode_body(page, "text/html; charset=utf-8")
    assert (enc, src) == ("cp1251", "meta") and text.count(RU) == 20
    # one or two bad bytes are few, but where nothing decoded as UTF-8 they do not speak for the header
    page = b'<html><head><meta charset="windows-1252"></head><body><p>Caf\xe9 menu \xa9 2019</p></body></html>'
    text, enc, src = decode_body(page, "text/html; charset=utf-8")
    assert (enc, src) == ("cp1252", "meta") and "<p>Café menu © 2019</p>" in text


# --- SEM-4: a few errors in a declared escape-based encoding do not turn the page into UTF-8 ----

def test_iso2022jp_page_with_one_sequence_python_lacks_stays_iso2022jp():
    words = "株式会社の山田です。"
    body = (words * 10).encode("iso2022_jp") + b"\x1b$B\x2d\x21\x1b(B"  # ESC $ B 2D 21: NEC row 13, as Windows writes it
    page = b'<meta http-equiv="Content-Type" content="text/html; charset=iso-2022-jp"><p>' + body + b"</p>"
    text, enc, src = decode_body(page, "text/html")
    assert (enc, src) == ("iso2022_jp_ext", "meta-lossy")
    assert text.endswith("<p>" + words * 10 + FFFD + "</p>") and "\x1b" not in text
    text, enc, src = decode_body(b"<p>" + body + b"</p>", "text/html; charset=ISO-2022-JP")
    assert (text, enc, src) == ("<p>" + words * 10 + FFFD + "</p>", "iso2022_jp_ext", "http-header-lossy")


@pytest.mark.parametrize("label,codec,words", [("hz-gb-2312", "hz", "中文网页内容，欢迎光临本站。"),
                                               ("iso-2022-kr", "iso2022_kr", "한국어 문장입니다")])
def test_other_escape_based_pages_with_a_bad_pair_keep_their_declaration(label, codec, words):
    good = (words * 5).encode(codec)
    assert good.isascii()
    bad = good[:-6] + b"\x21\x7f" + good[-6:]  # one pair that is no character, inside the last shifted run
    page = b'<meta charset="%s"><p>' % label.encode() + bad + b"</p>"
    text, enc, src = decode_body(page, "text/html")
    assert (enc, src) == (codec, "meta-lossy") and words * 4 in text and 0 < text.count(FFFD) <= 2


@pytest.mark.parametrize("label", ["iso-2022-jp", "hz-gb-2312", "iso-2022-kr"])
def test_pages_that_are_not_in_the_escape_based_encoding_they_declare_are_unchanged(label):
    ctype = "text/html; charset=" + label
    # UTF-8 (8-bit) under the label: valid UTF-8 is what it is
    page = ("<p>" + JA * 20 + "</p>").encode("utf-8")
    assert decode_body(page, ctype) == (page.decode("utf-8"), "utf-8", "utf-8-valid")
    # ASCII with a stray escape or tilde but no text in the encoding: nothing was decoded, the bytes stay as they are
    page = b"<p>Press \x1b$ to quit, see ~/notes and http://example.com/~{user}/ for more.</p>"
    text, enc, src = decode_body(page, ctype)
    assert text == page.decode("ascii") and src in ("http-header", "utf-8-valid")
    # escapes everywhere and next to no text: the errors are not few
    page = b"<p>" + b"\x1b$B!\x1b(B ~{!~} \x0e!\x0f " * 200 + (JA[:3] * 2).encode("iso2022_jp") + b"</p>"
    assert decode_body(page, ctype)[1:] == ("utf-8", "utf-8-valid")
