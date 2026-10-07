"""XML → JSON 변환 테스트 (저장해 둔 응답 샘플 사용)."""

import pytest

from core import errors as E
from core.errors import PatentApiError
from core.kipris import check_header, normalize_date, parse_biblio_detail, parse_search, parse_xml
from core.ops import parse_abstract, parse_biblio, parse_family, parse_fault, parse_fulltext, parse_legal

# --- KIPRIS -----------------------------------------------------------------


def test_normalize_date():
    assert normalize_date("20260115") == "2026-01-15"
    assert normalize_date("2026.01.15") == "2026-01-15"
    assert normalize_date("2026-01-15") == "2026-01-15"
    assert normalize_date(" ") is None
    assert normalize_date(None) is None


def test_kipris_biblio(fixture_text):
    d = parse_biblio_detail(fixture_text("kipris_biblio.xml"))
    assert d["applicationNumber"] == "10-2020-0026123"
    assert d["applicationDate"] == "2020-03-02"
    assert d["inventionTitle"] == "무선 통신 시스템에서 자원 할당 방법 및 장치"
    assert d["status"] == "등록"
    assert d["openNumber"] == "10-2021-0110001"
    assert d["openDate"] == "2021-09-10"
    assert d["registerNumber"] == "10-2654321"  # 1026543210000 → 등록번호 모양
    assert d["registerDate"] == "2024-05-13"
    assert "publicationNumber" not in d  # 빈 값은 빠진다
    assert d["applicants"][0]["name"] == "삼성전자주식회사"
    assert d["applicants"][0]["nameEng"] == "SAMSUNG ELECTRONICS CO., LTD."
    assert [i["name"] for i in d["inventors"]] == ["김철수", "이영희"]
    assert d["agents"][0]["name"] == "박대리"
    assert [i["code"] for i in d["ipc"]] == ["H04W 72/04", "H04L 5/00"]
    assert d["abstract"].startswith("본 발명은 무선 통신 시스템에서 <단말>이")  # 엔티티 풀림
    assert len(d["claims"]) == 2
    assert d["priorities"][0]["date"] == "2019-03-01"
    assert d["priorArt"][0]["number"] == "US20180123456 A1"
    assert d["history"][0]["date"] == "2020-03-02"
    assert "international" not in d  # 값이 모두 빈 칸이면 빠진다
    assert d["examinationRequest"] == {"flag": "Y", "date": "2023-02-28"}
    assert "originalApplication" in d and "number" not in d["originalApplication"]


def test_kipris_search(fixture_text):
    d = parse_search(fixture_text("kipris_search.xml"))
    assert d["totalCount"] == 137
    assert len(d["items"]) == 2
    first = d["items"][0]
    assert first["applicationNumber"] == "10-2024-0012345"
    assert first["applicationDate"] == "2024-01-29"
    assert first["openNumber"] == "10-2025-0098765"
    assert first["applicant"] == "주식회사 엘지에너지솔루션"
    assert "registerNumber" not in first
    assert d["items"][1]["applicationNumber"] == "20-2023-0001234"
    assert d["items"][1]["registerNumber"] == "10-2700000"


def test_kipris_nodata(fixture_text):
    assert parse_biblio_detail(fixture_text("kipris_nodata.xml")) is None


def test_kipris_key_error(fixture_text):
    with pytest.raises(PatentApiError) as ei:
        parse_biblio_detail(fixture_text("kipris_key_error.xml"))
    assert ei.value.code == E.AUTH_FAILED
    assert "서비스키" in ei.value.message


def test_datagokr_key_error(fixture_text):
    with pytest.raises(PatentApiError) as ei:
        parse_search(fixture_text("datagokr_key_error.xml"))
    assert ei.value.code == E.AUTH_FAILED


@pytest.mark.parametrize(
    "code, msg, expected",
    [
        ("03", "NODATA_ERROR", "nodata"),
        ("20", "검색결과가 없습니다", "nodata"),
        ("20", "SERVICE ACCESS DENIED", E.PERMISSION_DENIED),
        ("22", "LIMITED", E.QUOTA_EXCEEDED),
        ("31", "DEADLINE", E.AUTH_FAILED),
        ("10", "INVALID", E.UPSTREAM_BAD_REQUEST),
        ("05", "SERVICE TIMEOUT", E.UPSTREAM_UNAVAILABLE),
        ("77", "???", E.UPSTREAM_ERROR),
    ],
)
def test_kipris_result_codes(code, msg, expected):
    xml = f"<response><header><successYN>N</successYN><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header></response>"
    root = parse_xml(xml)
    if expected == "nodata":
        assert check_header(root) is True
    else:
        with pytest.raises(PatentApiError) as ei:
            check_header(root)
        assert ei.value.code == expected
        assert f"resultCode {code}" in ei.value.message


def test_kipris_html_error_page():
    with pytest.raises(PatentApiError) as ei:
        parse_xml("<!DOCTYPE html><html><body>점검중</body></html>")
    assert ei.value.code == E.UPSTREAM_UNAVAILABLE


def test_kipris_broken_xml():
    with pytest.raises(PatentApiError) as ei:
        parse_xml("<response><header>")
    assert ei.value.code == E.PARSE_ERROR


# --- OPS --------------------------------------------------------------------


def test_ops_biblio(fixture_text):
    d = parse_biblio(fixture_text("ops_biblio.xml"))
    doc = d["documents"][0]
    assert doc["publicationNumber"] == "EP1000000A1"
    assert doc["publicationDate"] == "2000-05-17"
    assert doc["familyId"] == "19768124"
    assert doc["title"] == "Apparatus for manufacturing green bricks for the brick manufacturing industry"
    assert doc["titleLang"] == "en"
    assert doc["applicants"] == ["BEHEERMAATSCHAPPIJ DE BOER NIJ [NL]"]
    assert doc["applicantsOriginal"] == ["Beheermaatschappij De Boer Nijmegen B.V."]
    assert doc["inventors"] == ["BOER JOHN DE [NL]"]
    assert doc["ipc"] == ["B28B 7/00", "B28B 7/10"]
    assert doc["cpc"] == ["B28B 7/0064"]
    assert doc["application"] == {"number": "EP99203729A", "date": "1999-11-05"}
    assert doc["priorities"] == [{"number": "NL19981010536", "date": "1998-11-10"}]
    assert doc["abstract"].startswith("The invention relates")
    assert doc["citations"] == [{"number": "DE3637377A1", "category": "A", "phase": "search"}]
    assert doc["nonPatentCitations"] == ["PATENT ABSTRACTS OF JAPAN vol. 1998, no. 01"]


def test_ops_family(fixture_text):
    d = parse_family(fixture_text("ops_family.xml"))
    assert d["familyId"] == "54001234"
    assert d["totalCount"] == 3
    assert d["countries"] == ["EP", "KR", "US"]
    ep = d["members"][0]
    assert [p["number"] for p in ep["publications"]] == ["EP3100000A1", "EP3100000B1"]
    assert ep["publications"][1]["date"] == "2019-03-13"
    assert ep["application"] == {"number": "EP15700001A", "country": "EP", "date": "2015-01-05"}
    assert ep["priorities"] == [{"number": "KR20140001234A", "date": "2014-01-06", "active": "YES"}]
    kr = d["members"][1]
    assert kr["publications"][0]["number"] == "KR20150082001A"


def test_ops_legal(fixture_text):
    d = parse_legal(fixture_text("ops_legal.xml"))
    assert d["eventCount"] == 2
    events = d["members"][0]["events"]
    assert events[0]["code"] == "AK"
    assert events[0]["date"] == "2016-12-07"
    assert events[0]["influence"] == "+"
    assert events[0]["description"] == "DESIGNATED CONTRACTING STATES:"
    assert "Designated state(s): DE FR GB" in events[0]["text"]
    assert events[1]["country"] == "FR"
    assert events[1]["influence"] == "-"


def test_ops_claims_prefers_english(fixture_text):
    d = parse_fulltext(fixture_text("ops_claims.xml"), "claims")
    assert d["lang"] == "EN"
    assert d["availableLanguages"] == ["DE", "EN"]
    assert d["paragraphs"][0].startswith("1. Apparatus")
    assert len(d["paragraphs"]) == 2
    assert d["publicationNumber"] == "EP1000000B1"


def test_ops_claims_lang_choice(fixture_text):
    d = parse_fulltext(fixture_text("ops_claims.xml"), "claims", "de")
    assert d["lang"] == "DE"
    assert d["paragraphs"] == ["1. Vorrichtung zur Herstellung von Grünlingen."]


def test_ops_description(fixture_text):
    d = parse_fulltext(fixture_text("ops_description.xml"), "description")
    assert len(d["paragraphs"]) == 3


def test_ops_abstract(fixture_text):
    d = parse_abstract(fixture_text("ops_biblio.xml"))
    assert d["lang"] == "en"
    assert d["paragraphs"][0].startswith("The invention relates")


def test_ops_fault(fixture_text):
    assert parse_fault(fixture_text("ops_fault_404.xml")) == ("SERVER.EntityNotFound", "No results found")
    assert parse_fault("not xml") == (None, None)


def test_ops_empty_family():
    xml = '<ops:world-patent-data xmlns:ops="http://ops.epo.org"><ops:patent-family family-id="1" total-result-count="0"/></ops:world-patent-data>'
    assert parse_family(xml) is None


def test_real_kipris_no_period(fixture_text):
    """실제 응답: 키는 맞지만 상품 신청(이용기간)이 없을 때 resultCode 31."""
    with pytest.raises(PatentApiError) as ei:
        parse_search(fixture_text("real_kipris_no_period.xml"))
    assert ei.value.code == E.AUTH_FAILED
    assert "이용기간" in ei.value.message and "resultCode 31" in ei.value.message


def test_real_kipris_biblio(fixture_text):
    """실제 응답(공개 특허 10-2020-0168607)."""
    d = parse_biblio_detail(fixture_text("real_kipris_biblio.xml"))
    assert d["applicationNumber"] == "10-2020-0168607"
    assert d["applicationDate"] == "2020-12-04"
    assert d["openNumber"] == "10-2022-0079145"
    assert d["registerNumber"] == "10-3028032"
    assert d["registerDate"] == "2026-09-30"
    assert d["applicants"][0]["name"] == "삼성전자주식회사"
    assert d["agents"][0]["name"] == "특허법인태평양"
    assert d["ipc"][0]["code"] == "H02J 7/00"
    assert len(d["claims"]) > 0 and len(d["history"]) > 0
    assert all("number" in p for p in d["priorArt"])


def test_real_kipris_search(fixture_text):
    d = parse_search(fixture_text("real_kipris_search.xml"))
    assert d["totalCount"] == 44
    first = d["items"][0]
    assert first["applicationNumber"] == "10-2020-0168607"
    assert first["registerNumber"] == "10-3028032"
    assert first["applicant"] == "삼성전자주식회사"


# --- 실제 OPS 응답 (공개 문헌) -------------------------------------------------


def test_real_ops_biblio(fixture_text):
    d = parse_biblio(fixture_text("real_ops_biblio.xml"))
    doc = d["documents"][0]
    assert doc["publicationNumber"] == "EP1000000A1"
    assert doc["publicationDate"] == "2000-05-17"
    assert doc["title"].startswith("Apparatus for manufacturing green bricks")
    assert doc["application"]["number"] == "EP99203729A"
    assert "B28B 7/00" in doc["ipc"]
    assert any(c["number"] == "EP0680812A1" for c in doc["citations"])


def test_real_ops_family_has_family_id(fixture_text):
    d = parse_family(fixture_text("real_ops_family.xml"))
    assert d["familyId"]  # family-member에만 붙은 family-id를 찾아 온다
    assert "EP" in d["countries"] and "US" in d["countries"]


def test_real_ops_family_includes_kr(fixture_text):
    """EP4210193B1의 패밀리에 한국 공개 KR20220079145A가 들어 있다."""
    d = parse_family(fixture_text("real_ops_family_kr.xml"))
    assert "KR" in d["countries"]
    kr = [p["number"] for m in d["members"] for p in m.get("publications", []) if p.get("country") == "KR"]
    assert kr == ["KR20220079145A"]


def test_real_ops_claims(fixture_text):
    d = parse_fulltext(fixture_text("real_ops_claims.xml"), "claims")
    assert d["lang"] == "EN"
    assert set(d["availableLanguages"]) == {"DE", "EN", "FR"}
    assert d["paragraphs"][0].startswith("1. Apparatus for manufacturing green bricks")


def test_real_ops_legal_codes_trimmed(fixture_text):
    d = parse_legal(fixture_text("real_ops_legal.xml"))
    assert d["eventCount"] > 10
    events = [e for m in d["members"] for e in m["events"]]
    assert all(e.get("code") == e.get("code", "").strip() for e in events)
    assert any(e.get("code") == "17Q" for e in events)
    assert all(e.get("influence") in (None, "+", "-") for e in events)
