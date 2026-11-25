"""Regression tests for the fetch half of the third review."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests import fakedns
from webfetch import decode
from webfetch import fetcher as fetcher_mod
from webfetch.api import create_app
from webfetch.config import Settings
from webfetch.decode import decode_body

JA = ("全文検索システムNamazuは、手軽に使えることを第一に目指した日本語全文検索システムです。CGIとして動作させることにより"
      "Webサーバ用の全文検索エンジンとして利用できるほか、個人のハードディスク内の文書やメールの検索にも使うことができます。イン"
      "デックスを事前に作成しておくことで、大量の文書の中から目的の単語を含むものを高速に探し出します。対応している文書形式は、プレ"
      "ーンテキストやHTMLのほか")
KO = "서울특별시는 대한민국의 수도이자 최대 도시이다. 한강을 사이에 두고 강북과 강남으로 나뉘며, 조선 시대부터 나라의 중심지였다."
FFFD = "\ufffd"


def _page(codec: str, text: str, stray: bytes, paragraphs: int = 4) -> bytes:
    """A page of `paragraphs` copies of `text` in `codec`, with `stray` in the middle of the first."""
    raw, mid = text.encode(codec), len(text) // 2
    first = text[:mid].encode(codec) + stray + text[mid:].encode(codec)
    return (b"<html><head><title>t</title></head><body><p>" + b"</p>\n<p>".join([first] + [raw] * (paragraphs - 1))
            + b"</p></body></html>")


# A stray byte sequence can swallow the lead byte after it, so that the rest of its paragraph decodes
# as other characters: the declared charset then reads worse than a permissive guess (GB18030 and
# EUC-KR take nearly any bytes), on that paragraph only.
@pytest.mark.parametrize("label,codec,text,stray", [
    ("euc-jp", "euc_jp", JA, b"\x8f\xa1"),  # a JIS X 0212 lead and half a character
    ("euc-jp", "euc_jp", JA, "日本".encode()),  # UTF-8 pasted into the page
    ("euc-jp", "euc_jp", "１２３４５６７８９０ＡＢＣＤＥＦＧ　￥１，０００", b"\x80"),
    ("euc-kr", "cp949", KO, "日本".encode()),
    ("gbk", "gb18030", "欢迎访问我们的网站。", b"\xa0"),
    ("gb2312", "gb18030", "欢迎访问我们的网站。", b"\xfd"),
])
def test_stray_bytes_in_one_paragraph_do_not_overrule_a_correct_legacy_declaration(label, codec, text, stray):
    page = _page(codec, text, stray)
    shown, enc, source = decode_body(page, f"text/html; charset={label}")
    assert (enc, source) == (codec, "http-header-lossy")
    assert shown.count(text) == 3 and FFFD in shown  # the other paragraphs, as written
    via_meta = page.replace(b"<title>", f'<meta charset="{label}"><title>'.encode())
    assert decode_body(via_meta, "text/html")[1:] == (codec, "meta-lossy")


def test_a_guess_outside_the_legacy_multibyte_encodings_does_not_overrule_one():
    # the detector reads full-width digits in EUC-JP with a bad byte as Thai (windows-874)
    page = _page("euc_jp", "１２３４５６７８９０ＡＢＣＤＥＦＧ　￥１，０００", b"\x80")
    assert not decode._reads_better(page, "cp874", "euc_jp")
    assert not decode._reads_better(page, None, "euc_jp")


def test_a_wrong_legacy_declaration_is_still_overruled():
    # no paragraph of an EUC-JP page reads as Japanese in Shift_JIS, before its first error or after
    page = _page("euc_jp", JA, b"")
    wrong, errors = decode._decode(page, "cp932")
    assert errors and decode._few_errors(wrong, errors) and JA[:20] not in wrong
    assert decode_body(page, "text/html; charset=shift_jis") == (page.decode("euc_jp"), "euc_jp", "detected")
    assert decode._reads_better(page, "euc_jp", "cp932")


def test_two_legacy_declarations_are_read_against_each_other():
    # header Big5, <meta> EUC-KR, Korean text, and a detector that guesses neither
    page = _page("cp949", KO, b"\x80" * 8).replace(b"<title>", b'<meta charset="euc-kr"><title>')
    shown, enc, source = decode_body(page, "text/html; charset=big5")
    assert (enc, source) == ("cp949", "meta-lossy") and shown.count(KO) == 3


# --- redirects and meta refresh ---------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    import webfetch.api as api_mod
    monkeypatch.setattr(fetcher_mod, "resolve_public", fakedns.resolve)
    monkeypatch.setattr(api_mod, "resolve_public", fakedns.resolve)
    with TestClient(create_app(Settings(allow_private=True, anon_rate_per_min=10_000)), raise_server_exceptions=False) as c:
        yield c


def _refresh(port: int, target: str, ctype: str | None = None) -> str:
    return f"http://echo.test:{port}/r2s/refresh?url={quote(target, safe='')}" + (f"&ctype={quote(ctype)}" if ctype else "")


@pytest.mark.parametrize("ctype,followed", [
    ("text/plain; note=html", False), ("multipart/mixed; boundary=html", False),
    ('application/json; profile="https://example.test/schema.html"', False),
    ("text/html;;;", True), ("Text/HTML ; charset=utf-8", True), ("application/xhtml+xml; charset=utf-8", True),
])
def test_meta_refresh_goes_by_the_media_type_not_its_parameters(server, client, ctype, followed):
    port = server.server_address[1]
    url = _refresh(port, "/echo-headers", ctype)
    body = client.post("/v1/fetch", json={"url": url, "format": "json"}).json()
    assert body["finalUrl"] == (f"http://echo.test:{port}/echo-headers" if followed else url)
    assert len(body["redirects"]) == int(followed)


@pytest.mark.parametrize("target,final", [
    ("/echo-headers?q=caf\u00e9", "http://echo.test:{port}/echo-headers?q=caf%C3%A9"),
    ("/echo-headers/\u65e5\u672c", "http://echo.test:{port}/echo-headers/%E6%97%A5%E6%9C%AC"),
    ("http://b\u00fccher.test:{port}/echo-headers", "http://xn--bcher-kva.test:{port}/echo-headers"),
])
def test_meta_refresh_target_with_non_ascii_characters_is_read_as_the_page_wrote_it(server, client, target, final):
    # was read as Latin-1: /caf%C3%83%C2%A9 for /café, and another host name for an IDN
    port = server.server_address[1]
    body = client.post("/v1/fetch", json={"url": _refresh(port, target.format(port=port)), "format": "json"}).json()
    assert body["finalUrl"] == final.format(port=port)
    assert [h["status"] for h in body["redirects"]] == [200]


def test_meta_refresh_target_in_a_legacy_encoding_keeps_its_bytes():
    assert fetcher_mod._meta_refresh(b'<meta http-equiv="refresh" content="0;url=/caf\xe9?q=\x93\xfa">') == (0, "/caf%E9?q=%93%FA")
    assert fetcher_mod._meta_refresh('<meta http-equiv="refresh" content="1; url=/caf\u00e9">'.encode()) == (1, "/caf\u00e9")


@pytest.mark.parametrize("resolve_gates", [True, False])
def test_a_space_in_a_redirect_location_is_followed_as_a_browser_does(server, client, resolve_gates):
    port = server.server_address[1]
    url = f"http://echo.test:{port}/redirect-to?url={quote('/echo-headers?q=a b&c=d', safe='')}"
    r = client.post("/v1/fetch", json={"url": url, "format": "json", "resolveGates": resolve_gates})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["finalUrl"] == f"http://echo.test:{port}/echo-headers?q=a%20b&c=d"
    assert [(h["status"], h["location"]) for h in body["redirects"]] == [(302, "/echo-headers?q=a b&c=d")]


# --- Stripping script/style contents before the nesting check is linear ---------------------

def test_strip_raw_text_looks_for_each_opener_once_per_stretch(monkeypatch):
    from webfetch import service

    # 20,000 scripts and no <style>: searching for both openers from every position scanned the
    # rest of the page 20,000 times for a <style> that is not there (2 s for 360 KB, 14 s for 900 KB).
    page = "<html><body>" + "<script>1</script>" * 20_000 + "<div>text</div></body></html>"
    scanned = [0]

    class Counting(str):
        def find(self, sub, start=0, *rest):
            at = str.find(self, sub, start, *rest)
            scanned[0] += (len(self) if at < 0 else at + len(sub)) - start
            return at

        def lower(self):
            return Counting(str.lower(self))

    out = service._strip_raw_text(Counting(page))
    assert out == "<html><body>" + ">" * 20_000 + "<div>text</div></body></html>"
    assert scanned[0] < 6 * len(page)  # each character is read a bounded number of times


@pytest.mark.parametrize("page,expected", [
    ('a<script>x<div></script>b<style>.c{}<i></style>d', 'a>b>d'),
    ('a<STYLE>x</STYLE>b<Script>y</Script>c', 'a>b>c'),
    ('<style>a</style><style>b</style><script>c</script><style>d</style>z', '>>>>z'),
    ('no raw text <div>here</div>', 'no raw text <div>here</div>'),
    ('a<script>never closed <div>', 'a'),
    ('a<style>x</style>b<script>never closed', 'a>b'),
    ('<script>a</style>b</script>c', '>c'),
    ('', ''),
])
def test_strip_raw_text_output_is_what_it_was(page, expected):
    from webfetch import service

    assert service._strip_raw_text(page) == expected
