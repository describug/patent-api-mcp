"""해외 키워드 검색(ep_search): CQL 조립·이스케이프, 응답 파싱, 0건, 쪽 나누기, 스로틀 안내 (네트워크 없이, 실제 응답 fixture)."""

import httpx
import pytest

from core import errors as E
from core.errors import PatentApiError
from core.ops import build_cql, parse_search, throttle_state
from test_service import Router, is_auth, make_service, run, token_ok

BUSY = {"X-Throttling-Control": "busy (images=green:100, inpadoc=green:45, other=green:1000, retrieval=green:100, search=green:15)"}


def is_search(r):
    return "/published-data/search" in r.url.path


def search_router(fixture_text, status=200, body_name="real_ops_search.xml", headers=BUSY):
    router = Router()
    router.on(is_auth, token_ok)
    body = fixture_text(body_name) if body_name else ""
    router.on(is_search, lambda r: httpx.Response(status, text=body, headers=headers))
    return router


# --- CQL 조립 ----------------------------------------------------------------


def test_build_cql_fields():
    cql = build_cql(
        keywords="wireless charging", applicant="samsung", date_from="2020", date_to="2022", countries=["ep", "US", "EP"]
    )
    assert cql == 'ta all "wireless charging" and pa="samsung" and pd within "20200101 20221231" and (pn=EP or pn=US)'


def test_build_cql_single_word_and_classes():
    assert build_cql(title="charg*", inventor="Kim Minsu") == 'ti="charg*" and in all "Kim Minsu"'
    assert build_cql(ipc="H02J 50/10, h02j") == "(ipc=H02J50/10 or ipc=H02J)"
    assert build_cql(cpc="H02J50/12", countries=["WO"]) == "cpc=H02J50/12 and pn=WO"


def test_build_cql_escapes_quotes_and_parens():
    # 따옴표·괄호·역슬래시로 검색식을 깨거나 조건을 덧붙일 수 없다
    cql = build_cql(keywords='coil" or pa="apple', applicant="LG (Electronics)\\")
    assert cql == 'ta all "coil or pa= apple" and pa all "LG Electronics"'
    assert cql.count('"') % 2 == 0


def test_build_cql_dates():
    assert build_cql(applicant="x", date_from="2021-03-05") == 'pa="x" and pd>=20210305'
    assert build_cql(applicant="x", date_to="2024-02") == 'pa="x" and pd<=20240229'
    with pytest.raises(PatentApiError) as ei:
        build_cql(applicant="x", date_from="2021-13-01")
    assert ei.value.code == E.INVALID_INPUT
    with pytest.raises(PatentApiError):
        build_cql(applicant="x", date_from="2023", date_to="2021")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"keywords": '  ""  '},
        {"date_from": "2020", "countries": ["EP"]},  # 기간·국가만으로는 검색하지 않는다
    ],
)
def test_build_cql_needs_condition(kwargs):
    with pytest.raises(PatentApiError) as ei:
        build_cql(**kwargs)
    assert ei.value.code == E.INVALID_INPUT


def test_build_cql_rejects_bad_codes():
    with pytest.raises(PatentApiError):
        build_cql(ipc="wireless")
    with pytest.raises(PatentApiError):
        build_cql(applicant="x", countries=["KOR"])


# --- 응답 파싱 ----------------------------------------------------------------


def test_parse_real_search(fixture_text):
    d = parse_search(fixture_text("real_ops_search.xml"))
    assert d["totalCount"] == 164 and d["range"] == "1-5"
    assert len(d["items"]) == 5
    first = d["items"][0]
    assert first == {
        "publicationNumber": "WO2022250346A1",
        "country": "WO",
        "kind": "A1",
        "date": "2022-12-01",
        "title": "ELECTRONIC DEVICE INCLUDING WIRELESS CHARGING STRUCTURE",
        "applicants": ["SAMSUNG ELECTRONICS CO LTD [KR]"],
        "ipc": first["ipc"],
        "familyId": "84228254",
    }
    assert "H02J 50/10" in first["ipc"]
    assert {i["country"] for i in d["items"]} == {"WO", "KR"}


def test_parse_search_without_biblio():
    xml = (
        '<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">'
        '<ops:biblio-search total-result-count="10000"><ops:query syntax="CQL">pa=x</ops:query>'
        '<ops:range begin="26" end="27"/><ops:search-result>'
        '<ops:publication-reference family-id="1"><document-id document-id-type="docdb">'
        "<country>EP</country><doc-number>1000000</doc-number><kind>A1</kind></document-id></ops:publication-reference>"
        '<ops:publication-reference family-id="2"><document-id document-id-type="docdb">'
        "<country>US</country><doc-number>2020123456</doc-number><kind>A1</kind></document-id></ops:publication-reference>"
        "</ops:search-result></ops:biblio-search></ops:world-patent-data>"
    )
    d = parse_search(xml)
    assert d["totalCount"] == 10000 and d["range"] == "26-27"
    assert d["items"][1] == {"publicationNumber": "US2020123456A1", "country": "US", "kind": "A1", "familyId": "2"}


def test_throttle_state():
    assert throttle_state(BUSY, "search") == ("green", 15)
    assert throttle_state({"x-throttling-control": "idle (search=yellow:20, images=red:30)"}, "search") == ("yellow", 20)
    assert throttle_state({}, "search") is None


# --- 서비스 -------------------------------------------------------------------


def test_ep_search_ok_and_cached(fixture_text):
    router = search_router(fixture_text)
    svc = make_service(router)
    r = run(svc.ep_search(keywords="wireless charging", applicant="samsung", date_from="2020", date_to="2022", page_size=5))
    assert r["ok"] is True and r["source"] == "EPO OPS" and r["cached"] is False
    d = r["data"]
    assert list(d) == ["totalCount", "cql", "range", "items"]
    assert d["totalCount"] == 164 and len(d["items"]) == 5
    assert d["cql"] == 'ta all "wireless charging" and pa="samsung" and pd within "20200101 20221231"'
    req = [c for c in router.calls if is_search(c)][0]
    assert req.url.path.endswith("/rest-services/published-data/search/biblio")  # 서지까지 한 번에
    assert req.url.params["q"] == d["cql"]
    assert req.headers["X-OPS-Range"] == "1-5"
    assert any("page=2" in n for n in r["notes"])
    assert not any("붐빕니다" in n for n in r["notes"])  # green이면 안내하지 않는다
    assert svc.quota.ops_usage()["headers"]["x-throttling-control"].startswith("busy")

    again = run(svc.ep_search(keywords="wireless charging", applicant="samsung", date_from="2020", date_to="2022", page_size=5))
    assert again["cached"] is True and again["data"] == d
    assert len([c for c in router.calls if is_search(c)]) == 1


def test_ep_search_page_range(fixture_text):
    router = search_router(fixture_text)
    svc = make_service(router)
    r = run(svc.ep_search(applicant="samsung", page=3, page_size=100))
    assert r["query"]["page"] == 3 and r["query"]["pageSize"] == 100
    assert [c for c in router.calls if is_search(c)][0].headers["X-OPS-Range"] == "201-300"
    # 2000번째를 넘는 쪽은 부르지 않는다
    r = run(svc.ep_search(applicant="samsung", page=21, page_size=100))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT and "2000" in r["error"]["message"]
    r = run(svc.ep_search(applicant="samsung", page=2, page_size=1000))  # 최대 100건으로 줄인다
    assert [c for c in router.calls if is_search(c)][-1].headers["X-OPS-Range"] == "101-200"


def test_ep_search_over_2000_note(fixture_text):
    body = fixture_text("real_ops_search.xml").replace('total-result-count="164"', 'total-result-count="10000"')
    router = Router()
    router.on(is_auth, token_ok)
    router.on(is_search, httpx.Response(200, text=body))
    r = run(make_service(router).ep_search(applicant="samsung", page_size=5))
    assert any("앞 2000건" in n and "10000" in n for n in r["notes"])


def test_ep_search_no_result(fixture_text):
    router = search_router(fixture_text, status=404, body_name="real_ops_search_nodata.xml")
    r = run(make_service(router).ep_search(keywords="qzxwvplmkjh", applicant="samsung"))
    assert r["ok"] is True and r["data"] is None
    assert "검색 결과 없음(자료 없음)" in r["notes"]


def test_ep_search_cql_direct_and_exclusive(fixture_text):
    router = search_router(fixture_text)
    svc = make_service(router)
    r = run(svc.ep_search(cql='  ta=wireless  prox/distance<=3 ta=charging and pa=samsung '))
    assert r["ok"] is True
    assert [c for c in router.calls if is_search(c)][0].url.params["q"] == "ta=wireless prox/distance<=3 ta=charging and pa=samsung"
    calls = len(router.calls)
    r = run(svc.ep_search(cql="pa=samsung", applicant="samsung"))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT
    r = run(svc.ep_search())
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT
    assert len(router.calls) == calls  # 입력 오류는 호출하지 않는다


def test_ep_search_bad_cql_message():
    router = Router()
    router.on(is_auth, token_ok)
    router.on(is_search, httpx.Response(400, text="<fault><code>CLIENT.CQL</code><message>The query provided is invalid</message></fault>"))
    r = run(make_service(router).ep_search(cql="ta=("))
    assert r["error"]["code"] == E.INVALID_INPUT
    assert "검색식" in r["error"]["message"] and "CLIENT.CQL" in r["error"]["message"]


@pytest.mark.parametrize("color", ["yellow", "red"])
def test_ep_search_busy_note(fixture_text, color):
    headers = {"X-Throttling-Control": f"busy (retrieval=green:100, search={color}:15)"}
    router = search_router(fixture_text, headers=headers)
    r = run(make_service(router).ep_search(applicant="samsung", page_size=5))
    assert any(f"search={color}" in n and "분당 15회" in n for n in r["notes"])


@pytest.mark.parametrize(
    "status, headers, code",
    [
        (403, {"X-Throttling-Control": "overloaded (search=black:5)"}, E.QUOTA_EXCEEDED),
        (403, {"X-Rejection-Reason": "IndividualQuotaPerHour"}, E.QUOTA_EXCEEDED),
        (429, {}, E.RATE_LIMITED),
    ],
)
def test_ep_search_limits_not_retried(status, headers, code):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(is_search, httpx.Response(status, text="", headers=headers))
    r = run(make_service(router).ep_search(applicant="samsung"))
    assert r["ok"] is False and r["error"]["code"] == code
    assert len([c for c in router.calls if is_search(c)]) == 1
