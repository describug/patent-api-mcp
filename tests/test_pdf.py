"""공보 원본 PDF(pdf_download): 파싱·파일 이름·허용 폴더·덮어쓰기 거부·쪽 합치기·후보 반환·출처 선택 (네트워크 없이, 실제 응답 fixture)."""

import io
import os
import unicodedata
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from core import errors as E
from core import pdf as PDF
from core.config import Settings, default_pdf_allowed_dir
from core.kipris import parse_fulltext_path
from core.ops import images_throttle, parse_images, section_range
from core.service import NO_DATA_NOTE
from test_service import Router, assert_no_secrets, is_auth, make_service, no_sleep, run, token_ok  # noqa: F401

KIPRIS_FILE_HOST = "plus.kipris.or.kr"


def make_pdf(pages: int, width: int = 100, text: str | None = None) -> bytes:
    w = PdfWriter()
    for i in range(pages):
        page = w.add_blank_page(width=width + i, height=200)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})}
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 8 Tf 5 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def widths(path) -> list[float]:
    return [float(p.mediabox.width) for p in PdfReader(str(path)).pages]


# --- 파싱 -------------------------------------------------------------------


def test_parse_kipris_fulltext_real(fixture_text):
    pub = parse_fulltext_path(fixture_text("real_kipris_fulltext_pub.xml"))
    ann = parse_fulltext_path(fixture_text("real_kipris_fulltext_ann.xml"))
    assert pub["docName"] == "1020200168607.pdf"
    # 실제 응답은 http지만 https로 바꿔 둔다(같은 파일이 온다, 실측).
    assert pub["url"].startswith("https://plus.kipris.or.kr/openapi/fileToss.jsp?arg=")
    assert ann["url"] != pub["url"]
    assert parse_fulltext_path(fixture_text("real_kipris_fulltext_nodata.xml")) is None


def test_parse_kipris_fulltext_rejects_foreign_host():
    xml = (
        "<response><header><successYN>Y</successYN><resultCode>00</resultCode></header>"
        "<body><item><docName>x.pdf</docName><path>http://evil.example/x.pdf</path></item></body></response>"
    )
    with pytest.raises(E.PatentApiError) as ei:
        parse_fulltext_path(xml)
    assert ei.value.code == E.PARSE_ERROR


def test_parse_ops_images_real(fixture_text):
    (doc,) = parse_images(fixture_text("real_ops_images.xml"))
    assert doc["publicationNumber"] == "EP1000000B1" and doc["kind"] == "B1"
    full, drawings = doc["instances"]["full"], doc["instances"]["drawings"]
    assert full["pages"] == 11 and full["link"] == "published-data/images/EP/1000000/B1/fullimage"
    assert "application/pdf" in full["formats"]
    assert full["sections"] == {"BIBLIOGRAPHY": 1, "DESCRIPTION": 2, "CLAIMS": 4, "DRAWINGS": 6}
    assert drawings == {"link": "published-data/images/EP/1000000/B1/thumbnail", "pages": 6,
                        "formats": ["application/pdf", "application/tiff"], "sections": {"DRAWINGS": 1}}
    assert doc["instances"]["firstPage"]["pages"] == 1


def test_parse_ops_images_epodoc_lists_each_kind(fixture_text):
    docs = parse_images(fixture_text("real_ops_images_epodoc.xml"))
    assert [(d["publicationNumber"], d["instances"]["full"]["pages"]) for d in docs] == [("EP1000000B1", 11), ("EP1000000A1", 12)]


def test_section_range(fixture_text):
    (wo,) = parse_images(fixture_text("real_ops_images_wo.xml"))
    full = wo["instances"]["full"]
    # 도면 33쪽부터, 조사보고서 41쪽부터 → 33~40쪽
    assert section_range(full["sections"], "DRAWINGS", full["pages"]) == (33, 40)
    assert section_range({"DESCRIPTION": 2, "DRAWINGS": 6}, "DRAWINGS", 11) == (6, 11)
    assert section_range({"DESCRIPTION": 2}, "DRAWINGS", 11) is None


def test_images_throttle():
    assert images_throttle({"x-throttling-control": "busy (images=yellow:60, inpadoc=green:45)"}) == ("yellow", 60)
    assert images_throttle({"x-throttling-control": "idle (other=green:1000)"}) is None
    assert images_throttle(None) is None


# --- 파일 이름·폴더 ----------------------------------------------------------


def test_file_name_and_label():
    assert PDF.file_name("D1", "WO2022119250A1", "full") == "D1_WO2022119250A1.pdf"
    assert PDF.file_name(None, "CN112345678A", "full") == "CN112345678A.pdf"
    assert PDF.file_name("D2", "EP1000000B1", "drawings") == "D2_EP1000000B1_drawings.pdf"
    # 미국 공개번호는 OPS 표기(US2013200536A1) 대신 흔히 쓰는 표기로
    assert PDF.file_name("D3", "US2022181889A1", "full") == "D3_US20220181889A1.pdf"
    assert PDF.file_name(None, "US10000000B2", "full") == "US10000000B2.pdf"
    assert PDF.clean_label(" D1 ") == "D1" and PDF.clean_label("") is None
    for bad in ("인용1", "D1/../x", "a b"):
        with pytest.raises(E.PatentApiError) as ei:
            PDF.clean_label(bad)
        assert ei.value.code == E.INVALID_INPUT


def test_kind_of():
    assert PDF.kind_of("A1") == "publication" and PDF.kind_of("U") == "publication"
    assert all(PDF.kind_of(k) == "registration" for k in ("B1", "B2", "C", "Y1", "E"))


@pytest.fixture
def allowed(tmp_path):
    root = tmp_path / "업무" / "허용"
    root.mkdir(parents=True)
    return root


def test_save_dir_inside_allowed(allowed):
    assert PDF.resolve_save_dir(None, allowed) == Path(os.path.realpath(allowed))
    sub = PDF.resolve_save_dir(str(allowed / "사건A" / "인용문헌"), allowed)  # 없는 하위 폴더도 된다(저장할 때 만든다)
    assert sub.name == "인용문헌" and not sub.exists()
    # NFD로 온 한글 경로도 같은 폴더로 본다
    nfd = unicodedata.normalize("NFD", str(allowed / "사건A"))
    assert PDF.resolve_save_dir(nfd, allowed) == PDF.resolve_save_dir(str(allowed / "사건A"), allowed)


@pytest.mark.parametrize("make_path", [
    lambda root, tmp: str(tmp / "밖"),                 # 허용 폴더 밖
    lambda root, tmp: str(root / ".." / "밖"),          # '..'로 빠져나감
    lambda root, tmp: "상대/경로",                       # 절대경로가 아님
    lambda root, tmp: str(root) + "-옆폴더",              # 이름만 비슷한 옆 폴더
])
def test_save_dir_outside_rejected(allowed, tmp_path, make_path):
    with pytest.raises(E.PatentApiError) as ei:
        PDF.resolve_save_dir(make_path(allowed, tmp_path), allowed)
    assert ei.value.code == E.INVALID_INPUT


def test_save_dir_symlink_escape_rejected(allowed, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (allowed / "link").symlink_to(outside)
    with pytest.raises(E.PatentApiError) as ei:
        PDF.resolve_save_dir(str(allowed / "link" / "sub"), allowed)
    assert ei.value.code == E.INVALID_INPUT


def test_allowed_dir_missing(tmp_path):
    with pytest.raises(E.PatentApiError) as ei:
        PDF.resolve_save_dir(None, tmp_path / "없는폴더")
    assert ei.value.code == E.FILE_ERROR


def test_save_new_never_overwrites(allowed):
    root = PDF.allowed_root(allowed)
    first = PDF.save_new(root / "a", "x.pdf", make_pdf(1), root)
    with pytest.raises(FileExistsError):
        PDF.save_new(root / "a", "x.pdf", make_pdf(2), root)
    assert len(PdfReader(str(first)).pages) == 1  # 처음 파일 그대로
    assert [p.name for p in (root / "a").iterdir()] == ["x.pdf"]  # 임시 파일이 남지 않는다


def test_merge_keeps_page_order():
    merged = PDF.merge([make_pdf(1, 100), make_pdf(2, 300), make_pdf(1, 500)])
    assert [float(p.mediabox.width) for p in PdfReader(io.BytesIO(merged)).pages] == [100, 300, 301, 500]


def test_text_layer_detection():
    assert PDF.describe(make_pdf(2))["hasTextLayer"] is False
    info = PDF.describe(make_pdf(1, text="A claimed apparatus comprising a housing and a fan unit"))
    assert info["hasTextLayer"] is True and info["pages"] == 1


def test_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("PATENT_API_PDF_ALLOWED_DIR", raising=False)
    env = tmp_path / ".env"
    env.write_text("PATENT_API_PDF_ALLOWED_DIR=~/Documents/Claude\n", encoding="utf-8")
    assert Settings.load(env).pdf_allowed_dir == Path.home() / "Documents" / "Claude"
    env.write_text("", encoding="utf-8")
    s = Settings.load(env)
    assert s.pdf_allowed_dir == default_pdf_allowed_dir() == Path.home() / "Downloads"
    assert s.pdf_max_pages == 100
    # 확장 설정 칸을 비워 두면 치환되지 않은 자리표시가 온다 → 기본값
    monkeypatch.setenv("PATENT_API_PDF_ALLOWED_DIR", "${user_config.pdf_allowed_dir}")
    assert Settings.load(env).pdf_allowed_dir == default_pdf_allowed_dir()


# --- 서비스 공통 --------------------------------------------------------------


def pdf_service(router, allowed, **kw):
    svc = make_service(router, **kw)
    svc.settings.pdf_allowed_dir = allowed
    svc.slept = []

    async def fake_sleep(seconds):
        svc.slept.append(seconds)

    svc._sleep = fake_sleep
    return svc


def is_images_query(q):
    return q.url.path.endswith("/images")


def is_page(q):
    return "/published-data/images/" in q.url.path


def pages_requested(router, part=""):
    return [int(c.url.params["Range"]) for c in router.calls if is_page(c) and part in c.url.path]


def ops_router(fixture_text, images="real_ops_images.xml", throttle="idle (images=green:100, other=green:1000)", page_hook=None):
    r = Router()
    r.on(is_auth, token_ok)
    r.on(is_images_query, lambda q: httpx.Response(200, text=fixture_text(images), headers={"X-Throttling-Control": throttle}))

    def page(q):
        assert q.headers["Accept"] == "application/pdf" and q.url.path.endswith(".pdf")
        n = int(q.url.params["Range"])
        if page_hook:
            hooked = page_hook(n)
            if hooked is not None:
                return hooked
        return httpx.Response(200, content=make_pdf(1, 100 + 10 * n), headers={"X-Throttling-Control": throttle})

    r.on(is_page, page)
    return r


# --- 해외(EPO OPS) -----------------------------------------------------------


def test_ops_full_document(fixture_text, allowed):
    router = ops_router(fixture_text)
    svc = pdf_service(router, allowed)
    out = run(svc.pdf_download("EP 1000000 B1", save_dir=str(allowed / "사건" / "인용문헌"), label="D1"))
    assert out["ok"] and out["source"] == "EPO OPS", out
    d = out["data"]
    assert Path(d["path"]).name == "D1_EP1000000B1.pdf" and Path(d["path"]).parent.name == "인용문헌"
    assert d["source"] == "OPS" and d["part"] == "full" and d["pages"] == 11
    assert d["bytes"] == Path(d["path"]).stat().st_size
    assert d["hasTextLayer"] is False and any("텍스트층" in n for n in out["notes"])
    assert d["sections"]["DRAWINGS"] == 6
    assert widths(d["path"]) == [100 + 10 * n for n in range(1, 12)]  # 쪽 순서대로 합쳐짐
    assert pages_requested(router) == list(range(1, 12))
    assert "images=green" in svc.quota.ops_usage()["headers"]["x-throttling-control"]
    assert svc.slept == []
    assert_no_secrets(out)


def test_ops_existing_file_not_overwritten(fixture_text, allowed):
    router = ops_router(fixture_text)
    svc = pdf_service(router, allowed)
    (allowed / "EP1000000B1.pdf").write_bytes(make_pdf(2))
    out = run(svc.pdf_download("EP1000000B1"))
    assert out["ok"] and out["data"]["alreadyExists"] and out["data"]["pages"] == 2
    assert out["data"]["path"] == str(PDF.allowed_root(allowed) / "EP1000000B1.pdf")
    assert any("덮어쓰지 않음" in n for n in out["notes"])
    assert router.calls == []  # 종류코드가 있으면 아무것도 부르지 않는다


def test_ops_without_kind_returns_candidates(fixture_text, allowed):
    router = ops_router(fixture_text, images="real_ops_images_epodoc.xml")
    out = run(pdf_service(router, allowed).pdf_download("EP1000000"))
    assert not out["ok"] and out["error"]["code"] == E.AMBIGUOUS_NUMBER
    assert [c["number"] for c in out["error"]["candidates"]] == ["EP1000000B1", "EP1000000A1"]
    assert "EP1000000A1(12쪽)" in out["error"]["message"]
    assert pages_requested(router) == []  # 추측해서 받지 않는다


def test_ops_other_kind_returns_candidates(fixture_text, allowed):
    # EP1000000A1을 요청했는데 조회 결과에 B1만 있으면 B1로 바꿔 받지 않고, 종류코드 없이 다시 본 후보를 돌려준다
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda q: "/epodoc/EP1000000/images" in q.url.path,
              lambda q: httpx.Response(200, text=fixture_text("real_ops_images_epodoc.xml")))
    router.on(is_images_query, lambda q: httpx.Response(200, text=fixture_text("real_ops_images.xml")))
    out = run(pdf_service(router, allowed).pdf_download("EP1000000A1"))
    assert not out["ok"] and out["error"]["code"] == E.AMBIGUOUS_NUMBER
    assert [c["number"] for c in out["error"]["candidates"]] == ["EP1000000B1", "EP1000000A1"]
    assert pages_requested(router) == []


def test_ops_wrong_kind_returns_candidates(fixture_text, allowed):
    # 없는 종류코드(A2) → 종류코드 없이 한 번 더 보고, 자동으로 고르지 않고 후보를 돌려준다
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda q: "/docdb/EP.1000000.A2/images" in q.url.path,
              lambda q: httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
    router.on(lambda q: "/epodoc/EP1000000/images" in q.url.path,
              lambda q: httpx.Response(200, text=fixture_text("real_ops_images_epodoc.xml")))
    out = run(pdf_service(router, allowed).pdf_download("EP1000000A2"))
    assert not out["ok"] and out["error"]["code"] == E.AMBIGUOUS_NUMBER
    assert [c["number"] for c in out["error"]["candidates"]] == ["EP1000000B1", "EP1000000A1"]
    assert "A2인 문헌(EP1000000A2)은 OPS에 원본이 없고" in out["error"]["message"]
    assert pages_requested(router) == []


def test_ops_jp_era_retry_downloads(fixture_text, allowed):
    # 연호 없이 적은 JP 구번호(JP10123456A) → 자료 없음이면 연호 후보(JPH10123456A)로 받는다
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda q: "/docdb/JP.10123456.A/images" in q.url.path,
              lambda q: httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
    router.on(lambda q: "/docdb/JP.H10123456.A/images" in q.url.path,
              lambda q: httpx.Response(200, text=fixture_text("real_ops_images_jp_era.xml")))
    router.on(is_page, lambda q: httpx.Response(200, content=make_pdf(1, 100 + int(q.url.params["Range"]))))
    out = run(pdf_service(router, allowed).pdf_download("JP10123456A"))
    assert out["ok"] and out["data"]["publicationNumber"] == "JPH10123456A"
    assert out["data"]["path"].endswith("JPH10123456A.pdf") and out["data"]["pages"] == 4
    assert out["query"]["retriedAs"] == "docdb:JP.H10123456.A"
    assert any("연호를 붙인 JPH10123456A" in n for n in out["notes"])
    assert pages_requested(router) == [1, 2, 3, 4]
    assert all("/images/JP/H10123456/A/fullimage" in c.url.path for c in router.calls if is_page(c))


def test_ops_jp_era_number_downloads(fixture_text, allowed):
    # 연호를 붙여 적은 번호는 그대로 OPS docdb 'JP.H10123456.A'로 묻는다
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda q: "/docdb/JP.H10123456.A/images" in q.url.path,
              lambda q: httpx.Response(200, text=fixture_text("real_ops_images_jp_era.xml")))
    router.on(is_page, lambda q: httpx.Response(200, content=make_pdf(1, 100 + int(q.url.params["Range"]))))
    out = run(pdf_service(router, allowed).pdf_download("JP H10-123456 A", label="D2"))
    assert out["ok"] and out["data"]["path"].endswith("D2_JPH10123456A.pdf")


def test_ops_not_found(fixture_text, allowed):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(is_images_query, httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
    out = run(pdf_service(router, allowed).pdf_download("CN112345678A"))
    assert out["ok"] and out["data"] is None and NO_DATA_NOTE in out["notes"]


def test_ops_drawings_from_drawing_instance(fixture_text, allowed):
    router = ops_router(fixture_text)
    out = run(pdf_service(router, allowed).pdf_download("EP1000000B1", part="drawings", label="D2"))
    d = out["data"]
    assert Path(d["path"]).name == "D2_EP1000000B1_drawings.pdf" and d["pages"] == 6
    assert pages_requested(router, "/thumbnail") == list(range(1, 7))
    assert pages_requested(router, "/fullimage") == []


def test_ops_drawings_sliced_from_full_document(fixture_text, allowed):
    xml = fixture_text("real_ops_images_wo.xml")
    start = xml.index('<ops:document-instance system="ops.epo.org" number-of-pages="8" desc="Drawing"')
    end = xml.index("</ops:document-instance>", start) + len("</ops:document-instance>")
    no_drawing = xml[:start] + xml[end:]
    router = Router()
    router.on(is_auth, token_ok)
    router.on(is_images_query, httpx.Response(200, text=no_drawing))
    router.on(is_page, lambda q: httpx.Response(200, content=make_pdf(1, int(q.url.params["Range"]))))
    out = run(pdf_service(router, allowed).pdf_download("WO2022/119250 A1", part="drawings"))
    d = out["data"]
    assert pages_requested(router, "/fullimage") == list(range(33, 41))  # 조사보고서(41쪽~)는 빼고 도면 구간만
    assert d["pages"] == 8 and widths(d["path"]) == list(range(33, 41))
    assert any("33~40쪽" in n for n in out["notes"])


def test_ops_page_limit(fixture_text, allowed):
    router = ops_router(fixture_text)
    out = run(pdf_service(router, allowed).pdf_download("EP1000000B1", max_pages=10))
    assert out["ok"] and out["data"]["downloaded"] is False and out["data"]["pages"] == 11
    assert pages_requested(router) == [] and not list(allowed.iterdir())


def test_ops_yellow_slows_down(fixture_text, allowed):
    router = ops_router(fixture_text, throttle="busy (images=yellow:30, other=green:1000)")
    svc = pdf_service(router, allowed)
    out = run(svc.pdf_download("EP1000000B1"))
    assert out["ok"] and out["data"]["pages"] == 11
    assert svc.slept == [2.0] * 10  # 분당 30회 → 쪽 사이 2초
    assert any("간격" in n for n in out["notes"])


def test_ops_black_stops_without_partial_file(fixture_text, allowed):
    def hook(n):
        if n == 3:
            return httpx.Response(200, content=make_pdf(1), headers={"X-Throttling-Control": "overloaded (images=black:0)"})
        return None

    router = ops_router(fixture_text, page_hook=hook)
    out = run(pdf_service(router, allowed).pdf_download("EP1000000B1", save_dir=str(allowed / "a")))
    assert not out["ok"] and out["error"]["code"] == E.RATE_LIMITED
    assert pages_requested(router) == [1, 2, 3]
    assert not (allowed / "a").exists() or not list((allowed / "a").iterdir())


def test_ops_quota_error_leaves_no_file(fixture_text, allowed):
    def hook(n):
        if n == 5:
            return httpx.Response(403, text=fixture_text("ops_fault_403_quota.xml"), headers={"X-Rejection-Reason": "IndividualQuotaPerHour"})
        return None

    out = run(pdf_service(ops_router(fixture_text, page_hook=hook), allowed).pdf_download("EP1000000B1"))
    assert not out["ok"] and out["error"]["code"] == E.QUOTA_EXCEEDED
    assert not list(allowed.iterdir())


def test_outside_folder_and_bad_input(fixture_text, allowed, tmp_path):
    svc = pdf_service(ops_router(fixture_text), allowed)
    out = run(svc.pdf_download("EP1000000B1", save_dir=str(tmp_path / "밖")))
    assert not out["ok"] and out["error"]["code"] == E.INVALID_INPUT and "허용 폴더" in out["error"]["message"]
    for kwargs in ({"number": "EP1000000B1", "part": "claims"}, {"number": "123456"}, {"number": "EP1000000B1", "label": "인용1"}):
        out = run(svc.pdf_download(**kwargs))
        assert not out["ok"] and out["error"]["code"] == E.INVALID_INPUT, kwargs
    out = run(svc.pdf_download("10-2011-0040179"))
    assert out["error"]["code"] == E.AMBIGUOUS_NUMBER and "종류코드" in out["error"]["message"]
    assert svc.ops._token is None  # 입력 오류는 아무것도 부르지 않는다


# --- 한국(KIPRIS 우선) -------------------------------------------------------


def kr_router(fixture_text, *, biblio=True, ann="real_kipris_fulltext_ann.xml", pub="real_kipris_fulltext_pub.xml", file_body=None):
    r = Router()
    r.on(is_auth, token_ok)
    r.on(lambda q: q.url.path.endswith("/biblio"),
         httpx.Response(200, text=fixture_text("real_ops_biblio_kr.xml")) if biblio else httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
    r.on(lambda q: q.url.path.endswith("/getAdvancedSearch"), httpx.Response(200, text=fixture_text("real_kipris_search.xml")))
    r.on(lambda q: q.url.path.endswith("/getPubFullTextInfoSearch"), httpx.Response(200, text=fixture_text(pub)))
    r.on(lambda q: q.url.path.endswith("/getAnnFullTextInfoSearch"), httpx.Response(200, text=fixture_text(ann)))
    text_pdf = make_pdf(3, text="Korean patent gazette full text layer for testing purposes only")
    r.on(lambda q: q.url.host == KIPRIS_FILE_HOST, httpx.Response(200, content=file_body if file_body is not None else text_pdf))
    r.on(is_images_query, httpx.Response(200, text=fixture_text("real_ops_images_kr.xml")))
    r.on(is_page, lambda q: httpx.Response(200, content=make_pdf(1)))
    return r


def test_kr_publication_from_kipris(fixture_text, allowed):
    router = kr_router(fixture_text)
    svc = pdf_service(router, allowed)
    out = run(svc.pdf_download("KR 10-2022-0079145 A", label="D1"))
    assert out["ok"] and out["source"] == "KIPRIS", out
    d = out["data"]
    assert Path(d["path"]).name == "D1_KR20220079145A.pdf"
    assert d["source"] == "KIPRIS" and d["pages"] == 3 and d["hasTextLayer"] is True
    assert d["sourceUrl"].startswith("https://plus.kipris.or.kr/")
    # 출원번호는 OPS 서지(KIPRIS 한도와 무관)에서 찾고, KIPRIS는 공개전문 1회만 부른다
    kipris_ops = [c.url.path.rsplit("/", 1)[1] for c in router.calls if c.url.host == "kipris.test"]
    assert kipris_ops == ["getPubFullTextInfoSearch"]
    assert [c for c in router.calls if c.url.host == "kipris.test"][0].url.params["applicationNumber"] == "1020200168607"
    assert svc.quota.kipris_count() == 1
    assert not any(is_page(c) for c in router.calls)
    file_calls = [c for c in router.calls if c.url.host == KIPRIS_FILE_HOST]
    assert file_calls and all("ServiceKey" not in str(c.url) for c in file_calls)
    assert any("10-2020-0168607" in n for n in out["notes"])
    assert_no_secrets(out)


def test_kr_registration_number_lookup_via_kipris_search(fixture_text, allowed):
    # OPS에 없는 최근 등록공보: KIPRIS 검색(등록번호)으로 출원번호를 찾은 뒤 공고전문
    router = kr_router(fixture_text, biblio=False)
    svc = pdf_service(router, allowed)
    out = run(svc.pdf_download("KR 10-3028032 B1"))
    assert out["ok"] and out["data"]["source"] == "KIPRIS"
    assert Path(out["data"]["path"]).name == "KR103028032B1.pdf"
    calls = [c for c in router.calls if c.url.host == "kipris.test"]
    assert [c.url.path.rsplit("/", 1)[1] for c in calls] == ["getAdvancedSearch", "getAnnFullTextInfoSearch"]
    assert calls[0].url.params["registerNumber"] == "1030280320000"
    assert calls[1].url.params["applicationNumber"] == "1020200168607"
    assert svc.quota.kipris_count() == 2


def test_kr_falls_back_to_ops_when_kipris_has_none(fixture_text, allowed):
    router = kr_router(fixture_text, pub="real_kipris_fulltext_nodata.xml")
    out = run(pdf_service(router, allowed).pdf_download("KR 10-2022-0079145 A"))
    assert out["ok"] and out["data"]["source"] == "OPS" and out["data"]["pages"] == 28
    assert any("KIPRIS" in n and "OPS로" in n for n in out["notes"])


def test_kr_falls_back_to_ops_without_kipris_key(fixture_text, allowed):
    router = kr_router(fixture_text)
    out = run(pdf_service(router, allowed, kipris_key=None).pdf_download("KR 10-2022-0079145 A"))
    assert out["ok"] and out["data"]["source"] == "OPS"
    assert any("KIPRIS_SERVICE_KEY" in n for n in out["notes"])


def test_kr_broken_kipris_file_falls_back(fixture_text, allowed):
    router = kr_router(fixture_text, file_body=b"<html>error</html>")
    out = run(pdf_service(router, allowed).pdf_download("KR 10-2022-0079145 A"))
    assert out["ok"] and out["data"]["source"] == "OPS"


def test_kr_drawings_use_ops(fixture_text, allowed):
    router = kr_router(fixture_text)
    out = run(pdf_service(router, allowed).pdf_download("KR 10-2022-0079145 A", part="drawings"))
    assert out["ok"] and out["data"]["source"] == "OPS" and out["data"]["pages"] == 8
    assert not any(c.url.host == "kipris.test" for c in router.calls)


def test_kr_existing_file_skips_kipris(fixture_text, allowed):
    router = kr_router(fixture_text)
    svc = pdf_service(router, allowed)
    (allowed / "KR20220079145A.pdf").write_bytes(make_pdf(1))
    out = run(svc.pdf_download("KR 10-2022-0079145 A"))
    assert out["ok"] and out["data"]["alreadyExists"]
    assert router.calls == [] and svc.quota.kipris_count() == 0
