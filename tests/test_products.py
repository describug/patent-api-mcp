"""KIPRIS 추가 상품: 상품 선택 설정, 실제 응답 파싱, 업무 단위 도구, 신청 상태 기억 (네트워크 없이)."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import httpx
import pytest

from core import errors as E
from core import http as core_http
from core import kipris_docs as KD
from core import products as P
from core.cache import Cache
from core.config import Settings
from core.errors import PatentApiError
from core.numbers import normalize_kr_registration_number
from core.quota import NOT_SUBSCRIBED, SUBSCRIBED, Quota
from core.service import PatentService

from conftest import ROOT

KIPRIS_KEY = "kipris+s3cr3tXQ/key=="
APP = "10-2020-0168607"
APP13 = "1020200168607"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast_sleep(_):
        return None

    monkeypatch.setattr(core_http.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(core_http, "RETRY_DELAYS", (0, 0))


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeKipris:
    """오퍼레이션 이름(경로 끝)별로 응답을 돌려주고 요청을 기록한다."""

    def __init__(self, fixture_text, mapping: dict[str, str | httpx.Response]):
        self.calls: list[httpx.Request] = []
        self.mapping = mapping
        self.fixture_text = fixture_text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        op = request.url.path.rsplit("/", 1)[-1]
        resp = self.mapping.get(op)
        if resp is None:
            return httpx.Response(500, text="no handler")
        if isinstance(resp, httpx.Response):
            return resp
        return httpx.Response(200, text=self.fixture_text(resp))

    def ops(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.calls]


def make_service(router, *, products="all", key=KIPRIS_KEY, clock=None, quota=None, cache=None):
    enabled, _ = P.resolve_enabled(products, {})
    settings = Settings(
        kipris_service_key=key,
        kipris_base_url="https://kipris.test/kipo-api/kipi/patUtiModInfoSearchSevice",
        kipris_rest_base_url="https://kipris.test/openapi/rest",
        kipris_products=enabled,
    )
    clock = clock or Clock(datetime(2026, 10, 7, 9, 0).astimezone())
    http = httpx.AsyncClient(transport=httpx.MockTransport(router))
    quota = quota or Quota(":memory:", now=clock)
    return PatentService(settings, http=http, cache=cache or Cache(":memory:"), quota=quota)


def assert_no_secrets(obj):
    blob = json.dumps(obj, ensure_ascii=False)
    assert KIPRIS_KEY not in blob and "s3cr3tXQ" not in blob


EXAM_MAP = {
    # 의견제출통지서·등록결정서는 같은 오퍼레이션 이름을 쓰므로 서비스로 나눠 받는다(아래 exam_router)
    "examineResultInfo": "real_kipris_opinion_exam_result.xml",
    "additionRejectInfo": "real_kipris_opinion_addition.xml",
    "rejectDecisionInfo": "real_kipris_opinion_decision.xml",
    "contentInfo": "real_kipris_allowance_content.xml",
}


def exam_router(fixture_text, *, rejection=None):
    fake = FakeKipris(fixture_text, dict(EXAM_MAP))

    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/bibliographicInfo"):
            fake.calls.append(request)
            if "IntermediateDocumentOPService" in path:
                return httpx.Response(200, text=fixture_text("real_kipris_opinion_biblio.xml"))
            if "IntermediateDocumentREService" in path:
                return rejection or httpx.Response(200, text=fixture_text("real_kipris_rest_nodata.xml"))
            return httpx.Response(200, text=fixture_text("real_kipris_allowance_biblio.xml"))
        return fake(request)

    return fake, route


# --- 상품 선택 설정 ----------------------------------------------------------


def test_resolve_enabled_default_and_list():
    enabled, warn = P.resolve_enabled(None, {})
    assert enabled == {"publication"} and warn == []
    enabled, _ = P.resolve_enabled("publication, opinion,REJECTION", {})
    assert enabled == {"publication", "opinion", "rejection"}
    enabled, _ = P.resolve_enabled("all", {"family": "false"})
    assert "family" not in enabled and "registration" in enabled and len(enabled) == len(P.PRODUCTS) - 1
    enabled, warn = P.resolve_enabled("none,wrongname", {"deadline": "true"})
    assert enabled == {"publication", "deadline"} and "wrongname" in warn[0]
    # 기본 상품(공개·등록공보)은 목록에서 빼거나 false로 꺼도 항상 켜진다
    enabled, _ = P.resolve_enabled("opinion", {"publication": "false"})
    assert enabled == {"publication", "opinion"}


def test_settings_load_products_from_env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("KIPRIS_PRODUCT_") or k == "PATENT_API_KIPRIS_PRODUCTS":
            monkeypatch.delenv(k)
    env = tmp_path / ".env"
    env.write_text("PATENT_API_KIPRIS_PRODUCTS=publication,opinion,claim_history\n", encoding="utf-8")
    # 확장 설정의 체크박스(개별 플래그)가 목록보다 우선한다. 치환 안 된 자리표시는 '설정 없음'.
    monkeypatch.setenv("KIPRIS_PRODUCT_CLAIM_HISTORY", "false")
    monkeypatch.setenv("KIPRIS_PRODUCT_FAMILY", "true")
    monkeypatch.setenv("KIPRIS_PRODUCT_DEADLINE", "${user_config.kipris_product_deadline}")
    s = Settings.load(env)
    assert s.kipris_products == {"publication", "opinion", "family"}
    assert s.tool_enabled("kr_exam_documents") and s.tool_enabled("kr_family")
    assert not s.tool_enabled("kr_claim_history") and not s.tool_enabled("kr_deadlines")
    assert s.kipris_kipi_root.endswith("/kipo-api/kipi")


def test_tools_registered_only_for_enabled_products():
    code = (
        "import asyncio, server; "
        "print(','.join(sorted(t.name for t in asyncio.run(server.mcp.list_tools()))))"
    )
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("KIPRIS_", "EPO_", "PATENT_API_"))}

    def tools(products: str) -> set[str]:
        env = dict(base_env, PATENT_API_KIPRIS_PRODUCTS=products, PATENT_API_CACHE_PATH=":memory:")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        return set(out.stdout.strip().split(","))

    ops_tools = {"ep_biblio", "ep_text", "family", "legal_status", "quota_status"}
    assert tools("publication") == ops_tools | {"kr_biblio", "kr_search"}
    assert tools("opinion,citing") == ops_tools | {"kr_biblio", "kr_search", "kr_exam_documents", "kr_citations"}
    assert tools("all") == ops_tools | {
        "kr_biblio", "kr_search", "kr_exam_documents", "kr_claim_history", "kr_deadlines",
        "kr_registration", "kr_citations", "kr_legal_history", "kr_family",
    }


def test_registration_number_normalization():
    for raw in ("10-3028032", "1030280320000", "10-3028032-0000", "KR 10-3028032 B1", "103028032"):
        assert normalize_kr_registration_number(raw) == "1030280320000"
    assert normalize_kr_registration_number("20-0123456 Y1") == "2001234560000"
    for raw in ("10-2020-0168607", "30-1234567", "abc"):
        with pytest.raises(PatentApiError) as ei:
            normalize_kr_registration_number(raw)
        assert ei.value.code == E.INVALID_INPUT


# --- 실제 응답 파싱 ----------------------------------------------------------


def test_parse_real_opinion(fixture_text):
    docs = KD.parse_doc_biblio(fixture_text("real_kipris_opinion_biblio.xml"))
    assert docs == [
        {
            "sendNumber": "952025088260221",
            "sendDate": "2025-09-11",
            "document": "의견제출통지서",
            "submitDueDate": "2026-01-11",
            "inventionTitle": "복수의 배터리들을 충전하는 방법 및 그 방법을 적용한 전자 장치",
        }
    ]
    res = KD.parse_exam_result(fixture_text("real_kipris_opinion_exam_result.xml"))["952025088260221"]
    assert res["examinedClaims"] == "제1-20항"
    assert res["refusalTable"] == [{"no": "1", "part": "청구항 전항", "law": "특허법 제42조제4항제2호"}]
    add = KD.parse_addition_reject(fixture_text("real_kipris_opinion_addition.xml"))["952025088260221"]
    assert add[0].startswith("이 출원은 청구범위의 청구항 전항의 기재가")
    assert "<BR>" not in add[1] and "\n청구항 제1,11항은" in add[1]
    dec = KD.parse_reject_decision(fixture_text("real_kipris_opinion_decision.xml"))["952025088260221"]
    assert "제출기일(2026.01.11.)" in dec["notice"] and "guidance" not in dec
    assert KD.parse_doc_biblio(fixture_text("real_kipris_rest_nodata.xml")) is None


def test_parse_real_allowance(fixture_text):
    docs = KD.parse_doc_biblio(fixture_text("real_kipris_allowance_biblio.xml"))
    assert docs[0]["document"] == "특허결정서" and docs[0]["claimCount"] == 16 and docs[0]["sendDate"] == "2026-07-15"
    content = KD.parse_allowance_content(fixture_text("real_kipris_allowance_content.xml"))["952026064420527"]
    assert content == [
        {"type": "결정내용", "text": "이 출원에 대하여 특허법 제66조에 따라 특허결정합니다.\n"
         "(특허권은 특허료를 납부하여 특허법 제87조에 따라 설정등록을 받음으로써 발생하게 됩니다.) 끝."}
    ]


def test_refusal_table_and_cited_references():
    rows = KD.parse_refusal_table("순번†거절이유가 있는 부분†관련 법조항§1†상세한 설명†구 실용신안법 제8조제3항§2†청구항 제1항†실용신안법 제8조제4항제2호")
    assert rows == [
        {"no": "1", "part": "상세한 설명", "law": "구 실용신안법 제8조제3항"},
        {"no": "2", "part": "청구항 제1항", "law": "실용신안법 제8조제4항제2호"},
    ]
    text = "- 아 래 -\n인용고안 1 : 일본 공개실용신안공보 평05-063869호(1993.08.24.)인용고안 2 : 일본 공개특허공보 특개2001-353849호(2001.12.25.)1.1. 청구항 1 고안의"
    assert KD.extract_cited_references(text) == [
        {"label": "인용고안 1", "document": "일본 공개실용신안공보 평05-063869호(1993.08.24.)"},
        {"label": "인용고안 2", "document": "일본 공개특허공보 특개2001-353849호(2001.12.25.)"},
    ]
    assert KD.extract_cited_references("인용발명 1: 한국 공개특허공보 제10-2012-0103937호\n2. 이유") == [
        {"label": "인용발명 1", "document": "한국 공개특허공보 제10-2012-0103937호"}
    ]


def test_parse_real_due_dates_and_claims(fixture_text):
    assert KD.parse_due_dates(fixture_text("real_kipris_due_dates.xml")) == [
        {"applicationNumber": APP, "sendNumber": "952025088260221", "document": "의견제출통지서", "dueDate": "2026-01-11"}
    ]
    order = KD.parse_claim_history_order(fixture_text("real_kipris_claim_order.xml"))
    assert [(o["version"], o["date"], o["document"]) for o in order] == [
        (1, "2020-12-04", "[특허출원]특허출원서"),
        (2, "2026-01-08", "[명세서등 보정]보정서"),
    ]
    detail = KD.parse_claim_history_detail(fixture_text("real_kipris_claim_detail.xml"))
    assert len(detail) == 39
    c4 = [d for d in detail if d["claim"] == 4 and d["version"] == 2][0]
    assert c4["change"] == "수정" and c4["diff"].startswith("청구항 [-2-]{+1+}에 있어서,\n")
    assert "<br" not in c4["text"]


def test_parse_real_registration(fixture_text):
    r = KD.parse_registration(fixture_text("real_kipris_registration.xml"))
    assert r["registrationNumber"] == "10-3028032"
    assert r["registrationDate"] == "2026-09-30" and r["expirationDate"] == "2040-12-04"
    assert r["applicationNumber"] == APP and r["ipc"] == "H02J 7/00" and r["claimCount"] == 16
    assert r["currentRightHolders"] == [{"name": "삼성전자주식회사", "country": "대한민국"}]
    assert r["annualFees"] == [{"fromAnnual": 1, "toAnnual": 3, "paidDate": "2026-10-01", "amount": 605000, "installment": 1}]
    assert r["paidThroughAnnual"] == 3
    # 주소는 돌려주지 않는다
    assert "Address" not in json.dumps(r) and "수원시" not in json.dumps(r, ensure_ascii=False)


def test_parse_real_citations_st27_family(fixture_text):
    cites = KD.parse_citations(fixture_text("real_kipris_citations.xml"))
    assert cites[0] == {
        "number": "KR1020120103937 A", "country": "KR", "standardNumber": "1020120103937", "kind": "A",
        "publicationDate": "2012-09-20", "type": "선행기술조사문헌", "standardized": "표준화",
    }
    assert KD.parse_citing(fixture_text("real_kipris_citing.xml")) == [
        {"applicationNumber": "10-2013-0028607", "type": "선행기술조사문헌"}
    ]
    st = KD.parse_st27(fixture_text("real_kipris_st27.xml"))
    assert st["openNumber"] == "10-2022-0079145" and len(st["events"]) == 11
    first, oa = st["events"][0], st["events"][3]
    assert first == {"seq": 1, "date": "2020-12-04", "category": "출원", "keyEvent": "A10", "detailEvent": "A12",
                     "indicator": "NAP", "nationalCode": "PA0109", "state": "A", "stage": "0→1"}
    assert oa["date"] == "2025-09-11" and oa["category"] == "심사" and oa["nationalCode"] == "PE0902"
    fam = KD.parse_family(fixture_text("real_kipris_family.xml"))
    assert fam["countries"] == ["EP", "WO"] and fam["docdbFamilyIds"] == ["81854188"]
    assert fam["members"][0] == {"country": "EP", "publication": "EP 4210193 A1", "publicationDate": "2023-07-12",
                                 "application": "EP 21900938 A", "applicationDate": "2021-11-26"}


def test_parse_not_subscribed_codes(fixture_text):
    with pytest.raises(PatentApiError) as ei:
        KD.parse_citations(fixture_text("real_kipris_rest_not_subscribed.xml"))
    assert ei.value.upstream_code == "101" and ei.value.code == E.PERMISSION_DENIED
    with pytest.raises(PatentApiError) as ei:
        KD.parse_family(fixture_text("real_kipris_kipi_not_subscribed.xml"))
    assert ei.value.upstream_code == "31"


# --- 업무 단위 도구 ----------------------------------------------------------


def test_exam_documents_combines_and_caches(fixture_text):
    fake, route = exam_router(fixture_text)
    svc = make_service(route)
    r = run(svc.kr_exam_documents(APP))
    assert r["ok"] is True and r["cached"] is False
    docs = r["data"]["documents"]
    assert [d["kind"] for d in docs] == ["의견제출통지서", "등록결정서"]
    oa = docs[0]
    assert oa["submitDueDate"] == "2026-01-11" and oa["examinedClaims"] == "제1-20항"
    assert oa["refusalTable"][0]["law"] == "특허법 제42조제4항제2호"
    assert len(oa["reasons"]) == 2 and "decision" not in oa
    assert docs[1]["contents"][0]["type"] == "결정내용"
    assert "확인한 서류: 의견제출통지서, 거절결정서, 등록결정서" in r["notes"]
    # 의견제출 4회 + 거절결정(서류 없음) 1회 + 등록결정 2회
    assert len(fake.calls) == 7
    for req in fake.calls:
        assert req.url.params["accessKey"] == KIPRIS_KEY and "ServiceKey" not in req.url.params
        assert req.url.path.startswith("/openapi/rest/IntermediateDocument")
    assert_no_secrets(r)
    r2 = run(svc.kr_exam_documents(APP))
    assert r2["cached"] is True and r2["data"] == r["data"] and len(fake.calls) == 7


def test_exam_documents_kinds_send_number_and_no_text(fixture_text):
    fake, route = exam_router(fixture_text)
    svc = make_service(route)
    r = run(svc.kr_exam_documents(APP, kinds=["opinion"], include_text=False))
    assert [d["kind"] for d in r["data"]["documents"]] == ["의견제출통지서"]
    assert "reasons" not in r["data"]["documents"][0]
    assert sorted(fake.ops()) == ["bibliographicInfo", "examineResultInfo"]
    r = run(svc.kr_exam_documents(APP, send_number="000000000000000"))
    assert r["ok"] is True and r["data"] is None and any("자료 없음" in n for n in r["notes"])
    r = run(svc.kr_exam_documents(APP, kinds=["wrong"]))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT


def test_exam_documents_partial_not_subscribed(fixture_text):
    not_sub = httpx.Response(200, text=fixture_text("real_kipris_rest_not_subscribed.xml"))
    fake, route = exam_router(fixture_text, rejection=not_sub)
    svc = make_service(route)
    r = run(svc.kr_exam_documents(APP))
    assert r["ok"] is True and len(r["data"]["documents"]) == 2
    assert any("'거절결정서'" in n and "무료 플랜으로 신청" in n for n in r["notes"])
    statuses = {p["key"]: p["status"] for p in run(svc.quota_status())["data"]["kiprisProducts"]}
    assert statuses["rejection"] == "미신청" and statuses["opinion"] == "신청됨" and statuses["family"] == "확인 안 함"


def test_exam_documents_only_disabled_products(fixture_text):
    _, route = exam_router(fixture_text)
    svc = make_service(route, products="publication,opinion")
    r = run(svc.kr_exam_documents(APP, kinds=["rejection"]))
    assert r["ok"] is False and "설정에서 꺼져" in r["error"]["message"]
    r = run(svc.kr_exam_documents(APP))  # 기본은 켜진 상품만
    assert r["query"]["kinds"] == ["opinion"] and r["ok"] is True


def test_not_subscribed_is_remembered_for_a_day(fixture_text):
    fake = FakeKipris(fixture_text, {"registrationInfo": "real_kipris_rest_not_subscribed.xml"})
    clock = Clock(datetime(2026, 10, 7, 9, 0).astimezone())
    svc = make_service(fake, clock=clock)
    r = run(svc.kr_registration(registration_number="10-3028032"))
    assert r["ok"] is False and r["error"]["code"] == E.PRODUCT_NOT_SUBSCRIBED
    msg = r["error"]["message"]
    assert "'등록사항'" in msg and "무료 플랜으로 신청하면 바로 쓸 수 있습니다" in msg and "resultCode 101" in msg
    assert "인증키" in msg  # 신청된 상품을 아직 하나도 확인하지 못했으므로 키도 의심
    assert_no_secrets(r)
    assert len(fake.calls) == 1
    # 하루 안에는 다시 부르지 않는다
    r = run(svc.kr_registration(registration_number="10-3028032"))
    assert r["error"]["code"] == E.PRODUCT_NOT_SUBSCRIBED and "기억" in r["error"]["message"] and len(fake.calls) == 1
    # 하루가 지나면 다시 확인하고, 신청했으면 풀린다
    fake.mapping["registrationInfo"] = "real_kipris_registration.xml"
    clock.now += timedelta(days=1, minutes=1)
    r = run(svc.kr_registration(registration_number="10-3028032"))
    assert r["ok"] is True and r["data"]["registrationNumber"] == "10-3028032" and len(fake.calls) == 2
    assert svc.quota.product_status("registration", KIPRIS_KEY)["status"] == SUBSCRIBED


def test_recheck_products_and_key_change_clear_memory(fixture_text):
    fake = FakeKipris(fixture_text, {"getAppNoPatFamInfoSearch": "real_kipris_kipi_not_subscribed.xml"})
    quota = Quota(":memory:", now=Clock(datetime(2026, 10, 7, 9, 0).astimezone()))
    svc = make_service(fake, quota=quota)
    r = run(svc.kr_family(APP))
    assert r["error"]["code"] == E.PRODUCT_NOT_SUBSCRIBED and "'특허 패밀리'" in r["error"]["message"]
    assert "resultCode 31" in r["error"]["message"]
    assert fake.calls[0].url.path == "/kipo-api/kipi/patFamInfoSearchService/getAppNoPatFamInfoSearch"
    assert fake.calls[0].url.params["ServiceKey"] == KIPRIS_KEY
    # 키가 바뀌면 기록을 무시하고 다시 확인한다
    svc2 = make_service(fake, key="another-key-1234", quota=quota)
    run(svc2.kr_family(APP))
    assert len(fake.calls) == 2
    # recheck_products=true면 미신청 기록을 지운다
    q = run(svc.quota_status(recheck_products=True))
    assert any("기록 1건을 지웠습니다" in n for n in q["notes"])
    fake.mapping["getAppNoPatFamInfoSearch"] = "real_kipris_family.xml"
    r = run(svc.kr_family(APP))
    assert r["ok"] is True and r["data"]["countries"] == ["EP", "WO"] and len(fake.calls) == 3


def test_publication_not_subscribed_keeps_auth_message(fixture_text):
    fake = FakeKipris(fixture_text, {"getBibliographyDetailInfoSearch": "real_kipris_no_period.xml"})
    svc = make_service(fake)
    r = run(svc.kr_biblio(APP))
    assert r["error"]["code"] == E.AUTH_FAILED and "이용기간" in r["error"]["message"]
    assert svc.quota.product_status("publication", KIPRIS_KEY)["status"] == NOT_SUBSCRIBED
    r = run(svc.kr_biblio(APP))
    assert r["error"]["code"] == E.AUTH_FAILED and "기억" in r["error"]["message"] and len(fake.calls) == 1


def test_quota_counts_all_products_and_limit_applies(fixture_text):
    fake = FakeKipris(fixture_text, {"BasicInfo": "real_kipris_st27.xml", "dueDateApplicationNoticeApplnoInfo": "real_kipris_due_dates.xml"})
    svc = make_service(fake)
    run(svc.kr_legal_history(APP))
    run(svc.kr_deadlines(application_number=APP))
    assert svc.quota.kipris_count() == 2
    svc.quota.limit = 2
    r = run(svc.kr_family(APP))
    assert r["error"]["code"] == E.QUOTA_EXCEEDED and len(fake.calls) == 2


def test_deadlines(fixture_text):
    fake = FakeKipris(fixture_text, {"dueDateApplicationNoticeApplnoInfo": "real_kipris_due_dates.xml",
                                     "dueDateRegistratioNoticeRgstnoInfo": "real_kipris_rest_nodata.xml"})
    svc = make_service(fake)
    r = run(svc.kr_deadlines(application_number=APP))
    d = r["data"]["deadlines"][0]
    assert r["data"]["asOf"] == "2026-10-07"
    assert d["dueDate"] == "2026-01-11" and d["passed"] is True and d["daysLeft"] == -269
    assert any("공식 기한관리를 대체할 수 없습니다" in n for n in r["notes"])
    assert any("지난 기한도" in n for n in r["notes"])
    r = run(svc.kr_deadlines(registration_number="10-3028032"))
    assert r["ok"] is True and r["data"] is None and any("공개 전 출원" in n for n in r["notes"])
    assert fake.calls[-1].url.params["registrationNumber"] == "1030280320000"
    r = run(svc.kr_deadlines())
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT


def test_claim_history(fixture_text):
    fake = FakeKipris(fixture_text, {"amendmentHistoryInfo": "real_kipris_claim_order.xml",
                                     "amendmentHistoryDetailInfo": "real_kipris_claim_detail.xml"})
    svc = make_service(fake)
    r = run(svc.kr_claim_history(APP))
    v1, v2 = r["data"]["versions"]
    assert v1["changes"] == {"신규": "1-20"} and v1["claimCount"] == 20 and "claims" not in v1
    assert v2["changes"] == {"수정": "1-2, 4-6, 8-9, 11-12, 14-16, 18-20", "삭제": "3, 7, 13, 17"}
    assert v2["claimCount"] == 16 and v2["date"] == "2026-01-08"
    r = run(svc.kr_claim_history(APP, claim=4))
    assert r["cached"] is True and len(fake.calls) == 2
    c = r["data"]["versions"][1]["claims"]
    assert c[0]["claim"] == 4 and c[0]["diff"].startswith("청구항 [-2-]{+1+}")
    r = run(svc.kr_claim_history(APP, claim=3))
    assert r["data"]["versions"][1]["claims"] == [{"claim": 3, "change": "삭제"}]


def test_registration_by_application_number(fixture_text):
    fake = FakeKipris(fixture_text, {"getBibliographyDetailInfoSearch": "real_kipris_biblio.xml",
                                     "registrationInfo": "real_kipris_registration.xml"})
    svc = make_service(fake)
    r = run(svc.kr_registration(application_number=APP))
    assert r["ok"] is True and r["query"] == {"registrationNumber": "1030280320000"}
    assert any("등록번호 10-3028032로 조회" in n for n in r["notes"])
    assert any("3년차분까지" in n for n in r["notes"])
    # 서지는 kr_biblio와 캐시를 같이 쓴다
    run(svc.kr_biblio(APP))
    assert fake.ops() == ["getBibliographyDetailInfoSearch", "registrationInfo"]
    # 공개·등록공보는 항상 켜지므로 등록사항만 켜도 출원번호→등록번호 변환이 된다
    svc2 = make_service(fake, products="registration")
    r = run(svc2.kr_registration(application_number=APP))
    assert r["ok"] is True


def test_registration_without_register_number(fixture_text):
    fake = FakeKipris(fixture_text, {"getBibliographyDetailInfoSearch": "kipris_nodata.xml"})
    svc = make_service(fake)
    r = run(svc.kr_registration(application_number="10-2026-0012345"))
    assert r["ok"] is True and r["data"] is None
    assert any("등록번호가 없습니다" in n for n in r["notes"])
    assert fake.ops() == ["getBibliographyDetailInfoSearch"]  # 등록사항은 부르지 않는다


def test_citations_both_and_disabled(fixture_text):
    fake = FakeKipris(fixture_text, {"citationInfoV3": "real_kipris_citations.xml", "citingInfo": "real_kipris_citing.xml"})
    svc = make_service(fake)
    r = run(svc.kr_citations(APP))
    assert len(r["data"]["cites"]) == 3 and r["data"]["citedBy"] == [{"applicationNumber": "10-2013-0028607", "type": "선행기술조사문헌"}]
    citing_req = [c for c in fake.calls if c.url.path.endswith("citingInfo")][0]
    assert citing_req.url.params["standardCitationApplicationNumber"] == APP13
    svc2 = make_service(fake, products="citation")
    r = run(svc2.kr_citations(APP))
    assert "citedBy" not in r["data"] and any("피인용문헌' 상품이 설정에서 꺼져" in n for n in r["notes"])
    r = run(svc2.kr_citations(APP, direction="citing"))
    assert r["ok"] is False


def test_family_and_legal_no_data(fixture_text):
    nodata = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><response><header><successYN>Y</successYN>'
        "<resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header><body><items/></body></response>"
    )
    fake = FakeKipris(fixture_text, {"getAppNoPatFamInfoSearch": httpx.Response(200, text=nodata),
                                     "BasicInfo": "real_kipris_rest_nodata.xml"})
    svc = make_service(fake)
    r = run(svc.kr_family(APP))
    assert r["ok"] is True and r["data"] is None and any("자료 없음" in n for n in r["notes"])
    r = run(svc.kr_legal_history(APP))
    assert r["ok"] is True and r["data"] is None


def test_exam_text_cap(fixture_text):
    _, route = exam_router(fixture_text)
    svc = make_service(route)
    svc.settings.max_text_chars = 120
    r = run(svc.kr_exam_documents(APP, kinds=["opinion"]))
    reasons = r["data"]["documents"][0]["reasons"]
    assert sum(len(x) for x in reasons) <= 120
    assert any("잘랐습니다" in n for n in r["notes"])


def test_quota_status_lists_products():
    svc = make_service(lambda r: httpx.Response(500), products="publication,opinion")
    q = run(svc.quota_status())
    prods = {p["key"]: p for p in q["data"]["kiprisProducts"]}
    assert set(prods) == {p.key for p in P.PRODUCTS}
    assert prods["opinion"]["enabled"] is True and prods["family"]["enabled"] is False
    assert all(p["status"] == "확인 안 함" for p in prods.values())
