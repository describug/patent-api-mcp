"""자료 없음일 때 한 번 더 묻기: JP 연호 후보, 틀린 종류코드, 공개 직후 안내 (네트워크 없이, 실제 응답 fixture)."""

import httpx

from core.service import JP_ERA_NOTE, NO_DATA_NOTE, RECENT_PUBLICATION_NOTE
from test_service import Router, is_auth, make_service, run, token_ok


def ops_router(fixture_text, routes):
    """routes: [(경로에 들어 있는 문자열, fixture 이름 또는 None=404)] 앞에서부터 맞춰 본다."""
    router = Router()
    router.on(is_auth, token_ok)
    for frag, name in routes:
        if name is None:
            router.on(lambda r, f=frag: f in r.url.path, lambda r: httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
        else:
            router.on(lambda r, f=frag: f in r.url.path, lambda r, n=name: httpx.Response(200, text=fixture_text(n)))
    return router


def ops_paths(router):
    return [c.url.path.split("/rest-services/")[1] for c in router.calls if "/rest-services/" in c.url.path]


# --- 틀린 종류코드 -------------------------------------------------------------


def test_ep_biblio_wrong_kind_retries_without_kind(fixture_text):
    router = ops_router(fixture_text, [("docdb/EP.1000000.A2", None), ("epodoc/EP1000000/biblio", "real_ops_biblio_epodoc.xml")])
    out = run(make_service(router).ep_biblio("EP1000000A2"))
    assert out["ok"] and [d["kind"] for d in out["data"]["documents"]] == ["A1", "B1"]
    assert NO_DATA_NOTE not in out["notes"]
    assert any("입력한 종류코드 A2인 문헌(EP1000000A2)은 없고, 같은 번호로 A1, B1가 있습니다" in n for n in out["notes"])
    assert out["query"]["retriedAs"] == "epodoc:EP1000000"
    # 다시 물은 서지는 같은 캐시를 쓰므로 종류코드 목록을 위해 또 부르지 않는다
    assert ops_paths(router) == ["published-data/publication/docdb/EP.1000000.A2/biblio", "published-data/publication/epodoc/EP1000000/biblio"]


def test_family_wrong_kind_lists_actual_kinds(fixture_text):
    router = ops_router(
        fixture_text,
        [("family/publication/docdb/EP.1000000.A2", None), ("family/publication/epodoc/EP1000000", "real_ops_family.xml"),
         ("epodoc/EP1000000/biblio", "real_ops_biblio_epodoc.xml")],
    )
    out = run(make_service(router).family("EP1000000A2"))
    assert out["ok"] and out["data"]["members"]
    assert any("A1, B1가 있습니다" in n for n in out["notes"])


def test_legal_and_text_wrong_kind(fixture_text):
    router = ops_router(
        fixture_text,
        [("docdb/EP.1000000.A2", None), ("legal/publication/epodoc/EP1000000", "real_ops_legal.xml"),
         ("epodoc/EP1000000/claims", "real_ops_claims.xml"), ("epodoc/EP1000000/biblio", "real_ops_biblio_epodoc.xml")],
    )
    svc = make_service(router)
    legal = run(svc.legal_status("EP1000000A2"))
    assert legal["ok"] and legal["data"] and any("A1, B1가 있습니다" in n for n in legal["notes"])
    text = run(svc.ep_text("EP1000000A2"))
    assert text["ok"] and text["data"]["text"]
    assert any("어느 공보의 원문을 줄지는 OPS가 정합니다" in n for n in text["notes"])


def test_wrong_kind_still_no_data(fixture_text):
    router = ops_router(fixture_text, [("1000000", None)])
    out = run(make_service(router).ep_biblio("EP1000000A2"))
    assert out["ok"] and out["data"] is None
    assert NO_DATA_NOTE in out["notes"] and RECENT_PUBLICATION_NOTE in out["notes"]
    assert len(ops_paths(router)) == 2  # 종류코드로 1회 + 종류코드 없이 1회


def test_kr_wrong_kind_is_not_retried(fixture_text):
    router = ops_router(fixture_text, [("KR", None)])
    out = run(make_service(router).ep_biblio("KR 10-1500000 B2"))
    assert out["ok"] and out["data"] is None
    assert ops_paths(router) == ["published-data/publication/docdb/KR.101500000.B2/biblio"]
    assert RECENT_PUBLICATION_NOTE not in out["notes"]


# --- 일본 연호 -----------------------------------------------------------------


def test_jp_without_era_retries_with_era(fixture_text):
    router = ops_router(fixture_text, [("JP.10123456.A", None), ("JP.H10123456.A/biblio", "real_ops_biblio_jp_era.xml")])
    out = run(make_service(router).ep_biblio("JP10123456A"))
    assert out["ok"] and out["data"]["documents"][0]["publicationNumber"] == "JPH10123456A"
    assert any("연호를 붙인 JPH10123456A로 조회했습니다" in n for n in out["notes"])
    assert out["query"]["retriedAs"] == "docdb:JP.H10123456.A"


def test_jp_era_number_goes_straight(fixture_text):
    router = ops_router(fixture_text, [("JP.H10123456.A/biblio", "real_ops_biblio_jp_era.xml")])
    out = run(make_service(router).ep_biblio("JPH10123456A"))
    assert out["ok"] and out["data"]["documents"][0]["docNumber"] == "H10123456"
    assert ops_paths(router) == ["published-data/publication/docdb/JP.H10123456.A/biblio"]


def test_jp_without_era_not_found_suggests_era(fixture_text):
    router = ops_router(fixture_text, [("JP", None)])
    out = run(make_service(router).family("JP60012345A"))
    assert out["ok"] and out["data"] is None and JP_ERA_NOTE in out["notes"]
    # 원래 번호, 연호 후보(S), 종류코드 없이 — 각 1회
    assert ops_paths(router) == [
        "family/publication/docdb/JP.60012345.A",
        "family/publication/docdb/JP.S60012345.A",
        "family/publication/epodoc/JP60012345",
    ]
