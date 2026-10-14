"""EP Register(ep_register): 응답 파싱, 번호 → 경로, 자료 없음·인용문헌 빈 경우 안내, 사용량 (네트워크 없이, 실제 응답 fixture)."""

import re

import httpx

from core import errors as E
from core.register import parse_register
from core.service import NO_DATA_NOTE
from test_service import Router, is_auth, make_service, run, token_ok

BUSY = {"X-Throttling-Control": "busy (images=green:100, inpadoc=green:45, other=yellow:800, retrieval=green:100, search=green:15)"}

# 분할 관계(related-documents)는 실측 응답과 같은 구조로 만든 조각이다(번호는 가짜).
DIVISION = """
<reg:related-documents><reg:division><reg:relation><reg:parent-doc>
  <reg:document-id document-id-type="application number"><reg:country>EP</reg:country><reg:doc-number>20190123456</reg:doc-number><reg:kind>D</reg:kind></reg:document-id>
  <reg:document-id document-id-type="publication number"><reg:country>EP</reg:country><reg:doc-number>3600000</reg:doc-number><reg:kind>D</reg:kind></reg:document-id>
</reg:parent-doc><reg:child-doc><reg:document-id><reg:country/><reg:doc-number/></reg:document-id></reg:child-doc></reg:relation></reg:division></reg:related-documents>
"""


def with_division(xml: str) -> str:
    return xml.replace("</reg:bibliographic-data>", DIVISION + "</reg:bibliographic-data>", 1)


def without_citations(xml: str) -> str:
    return re.sub(r"<reg:references-cited>.*?</reg:references-cited>", "", xml, flags=re.S)


def register_router(body: str, status: int = 200):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: "/register/" in r.url.path, lambda r: httpx.Response(status, text=body, headers=BUSY))
    return router


def register_paths(router):
    return [c.url.path.split("/rest-services/")[1] for c in router.calls if "/register/" in c.url.path]


# --- 파싱 ---------------------------------------------------------------------


def test_parse_register_real(fixture_text):
    d = parse_register(fixture_text("real_ops_register.xml"), max_items=5)
    assert d["status"] == {"status": "No opposition filed within time limit", "code": "7"}
    assert d["application"] == {"number": "EP99203729", "filingDate": "1999-11-08", "filingLanguage": "nl"}
    assert [(p["number"], p["date"]) for p in d["publications"]] == [("EP1000000A1", "2000-05-17"), ("EP1000000B1", "2003-02-12")]
    assert d["applicants"] == ["Beheermaatschappij De Boer Nijmegen B.V."]
    assert d["agents"] == ["'t Jong, Bastiaan Jacobus, et al"]
    assert d["dates"] == {"request-for-examination": "2000-10-27", "first-examination-report-despatched": "2002-04-23"}
    assert d["searchReports"][1] == {
        "office": "EP", "type": "national-supplementary", "label": "(Supplementary) European search report", "mailed": "2000-03-09",
    }
    first = d["citations"][0]
    assert first == {
        "number": "EP0680812", "category": "A", "phase": "search", "citedIn": "search", "office": "EP",
        "text": "(BOER BEHEER NIJMEGEN BV DE et al.) [A] 1,10,11 * the whole document *",
    }
    assert "NL" in d["designatedStates"]
    # 최근 것부터, max_items까지만
    assert d["proceduralStepsTotal"] == 6 and len(d["proceduralSteps"]) == 5
    dates = [s.get("date") or "" for s in d["proceduralSteps"]]
    assert dates == sorted(dates, reverse=True)
    assert d["eventsTotal"] == 32 and len(d["events"]) == 5
    assert d["events"][0]["date"] >= d["events"][-1]["date"]


def test_parse_register_division(fixture_text):
    d = parse_register(with_division(fixture_text("real_ops_register.xml")))
    # 연도 4자리 + 일련번호 7자리 출원번호는 epodoc 8자리로 바꾼다
    assert d["parentApplications"] == [{"relation": "division", "application": "EP19123456", "publication": "EP3600000"}]


def test_parse_register_nodata(fixture_text):
    assert parse_register("<world-patent-data xmlns='http://ops.epo.org'/>") is None


# --- 도구 ---------------------------------------------------------------------


def test_ep_register_application_number(fixture_text):
    router = register_router(fixture_text("real_ops_register.xml"))
    out = run(make_service(router).ep_register("99203729.2", "application"))
    assert out["ok"] and out["data"]["application"]["number"] == "EP99203729"
    assert register_paths(router) == ["register/application/epodoc/EP99203729/biblio,events,procedural-steps"]
    assert any("응답 기한은 계산하지 않았습니다" in n for n in out["notes"])
    assert any("전체 32건" in n for n in out["notes"])


def test_ep_register_eight_digits_is_application(fixture_text):
    router = register_router(fixture_text("real_ops_register.xml"))
    svc = make_service(router)
    assert run(svc.ep_register("EP99203729"))["ok"]
    assert run(svc.ep_register("99203729.2"))["ok"]  # 국가코드 없는 EP 출원번호(검증숫자 포함)
    assert register_paths(router) == ["register/application/epodoc/EP99203729/biblio,events,procedural-steps"]  # 둘째는 캐시


def test_ep_register_publication_drops_kind(fixture_text):
    router = register_router(fixture_text("real_ops_register.xml"))
    svc = make_service(router)
    assert run(svc.ep_register("EP1000000B1"))["ok"]
    assert run(svc.ep_register("WO2022/119250"))["ok"]
    assert register_paths(router) == [
        "register/publication/epodoc/EP1000000/biblio,events,procedural-steps",
        "register/publication/epodoc/WO2022119250/biblio,events,procedural-steps",
    ]


def test_ep_register_rejects_other_countries(fixture_text):
    router = register_router(fixture_text("real_ops_register.xml"))
    out = run(make_service(router).ep_register("US 2022/0181889 A1"))
    assert not out["ok"] and out["error"]["code"] == E.INVALID_INPUT
    assert register_paths(router) == []


def test_ep_register_nodata(fixture_text):
    router = register_router(fixture_text("real_ops_register_nodata.xml"), status=404)
    out = run(make_service(router).ep_register("EP99999999"))
    assert out["ok"] and out["data"] is None
    assert NO_DATA_NOTE in out["notes"] and any("EP Register에 없는 번호" in n for n in out["notes"])


def test_ep_register_report_without_citations(fixture_text):
    router = register_router(without_citations(fixture_text("real_ops_register.xml")))
    out = run(make_service(router).ep_register("EP1000000"))
    assert out["ok"] and "citations" not in out["data"]
    assert any("인용문헌이 비어 있습니다" in n for n in out["notes"])


def test_ep_register_cached_and_quota_throttle(fixture_text):
    router = register_router(fixture_text("real_ops_register.xml"))
    svc = make_service(router)
    run(svc.ep_register("EP1000000"))
    again = run(svc.ep_register("EP1000000"))
    assert again["cached"] and len(register_paths(router)) == 1
    q = run(svc.quota_status())
    assert q["data"]["opsThrottle"]["other"] == {"state": "yellow", "perMinute": 800}
    assert any("ep_register" in n for n in q["notes"])
