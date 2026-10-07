"""서비스 계층: 응답 틀, 캐시, 한도, 재시도, 토큰 재발급, 비밀값 비노출 (네트워크 없이)."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from core import errors as E
from core import http as core_http
from core.cache import Cache
from core.config import Settings, load_env_file, normalize_service_key
from core.quota import Quota
from core.service import PatentService

KIPRIS_KEY = "kipris+s3cr3tXQ/key=="
EPO_KEY = "epo-consumer-s3cr3tXQ"
EPO_SECRET = "epo-secret-s3cr3tXQ"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast_sleep(_):
        return None

    monkeypatch.setattr(core_http.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(core_http, "RETRY_DELAYS", (0, 0))


class Router:
    """요청을 기록하고 미리 정한 응답을 돌려주는 가짜 서버."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.handlers = []

    def on(self, predicate, responder):
        self.handlers.append((predicate, responder))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for pred, resp in self.handlers:
            if pred(request):
                return resp(request) if callable(resp) else resp
        return httpx.Response(500, text="no handler")


def make_service(router, *, kipris_key=KIPRIS_KEY, epo_key=EPO_KEY, epo_secret=EPO_SECRET, limit=1000, warn=900):
    settings = Settings(
        kipris_service_key=kipris_key,
        epo_consumer_key=epo_key,
        epo_consumer_secret=epo_secret,
        kipris_monthly_limit=limit,
        kipris_warn_at=warn,
        kipris_base_url="https://kipris.test/svc",
        ops_base_url="https://ops.test/3.2",
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(router))
    quota = Quota(":memory:", kipris_monthly_limit=limit, kipris_warn_at=warn, now=lambda: datetime(2026, 10, 7))
    return PatentService(settings, http=http, cache=Cache(":memory:"), quota=quota)


def token_ok(_req=None):
    return httpx.Response(200, json={"access_token": "TOKEN-1", "expires_in": "1199", "token_type": "BearerToken"})


def is_auth(r):
    return r.url.path.endswith("/auth/accesstoken")


def run(coro):
    return asyncio.run(coro)


def assert_no_secrets(obj):
    blob = json.dumps(obj, ensure_ascii=False)
    for s in (KIPRIS_KEY, EPO_KEY, EPO_SECRET, "s3cr3tXQ"):
        assert s not in blob


# --- 설정 -------------------------------------------------------------------


def test_service_key_percent_decoding():
    assert normalize_service_key("abc%2Bdef%3D%3D") == "abc+def=="
    assert normalize_service_key("abc+def==") == "abc+def=="
    assert normalize_service_key("  ") is None
    assert normalize_service_key(None) is None


def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# 주석\nKIPRIS_SERVICE_KEY="a%2Bb"\nexport EPO_OPS_CONSUMER_KEY=k\nEMPTY=\n\nBAD LINE\n', encoding="utf-8")
    env = load_env_file(p)
    assert env == {"KIPRIS_SERVICE_KEY": "a%2Bb", "EPO_OPS_CONSUMER_KEY": "k", "EMPTY": ""}
    s = Settings.load(p)
    assert s.kipris_service_key == "a+b"


# --- KIPRIS -----------------------------------------------------------------


def test_kr_biblio_ok_and_cached(fixture_text):
    router = Router()
    router.on(lambda r: "getBibliographyDetailInfoSearch" in r.url.path, httpx.Response(200, text=fixture_text("kipris_biblio.xml")))
    svc = make_service(router)

    r1 = run(svc.kr_biblio("KR 10-2020-0026123"))
    assert r1["ok"] is True
    assert r1["source"] == "KIPRIS"
    assert r1["query"] == {"applicationNumber": "1020200026123"}
    assert r1["cached"] is False
    assert r1["data"]["inventionTitle"].startswith("무선 통신")
    assert "claims" not in r1["data"] and "history" not in r1["data"]
    assert any("청구항 2개 생략" in n for n in r1["notes"])
    # 키는 percent-encoding 없이 한 번만 인코딩되어 나간다
    sent = router.calls[0].url.params
    assert sent["applicationNumber"] == "1020200026123"
    assert sent["ServiceKey"] == KIPRIS_KEY

    r2 = run(svc.kr_biblio("1020200026123", include_claims=True, include_history=True))
    assert r2["cached"] is True
    assert len(r2["data"]["claims"]) == 2 and len(r2["data"]["history"]) == 2
    assert len(router.calls) == 1  # 두 번째는 캐시
    assert svc.quota.kipris_count() == 1
    assert_no_secrets([r1, r2])


def test_kr_biblio_nodata_is_not_error(fixture_text):
    router = Router()
    router.on(lambda r: True, httpx.Response(200, text=fixture_text("kipris_nodata.xml")))
    svc = make_service(router)
    r = run(svc.kr_biblio("10-2026-0012345"))
    assert r["ok"] is True and r["data"] is None
    assert "해당 번호의 자료 없음" in r["notes"]
    # '자료 없음'도 캐시된다
    r2 = run(svc.kr_biblio("10-2026-0012345"))
    assert r2["cached"] is True and r2["data"] is None and "해당 번호의 자료 없음" in r2["notes"]


def test_kr_biblio_bad_key(fixture_text):
    router = Router()
    router.on(lambda r: True, httpx.Response(200, text=fixture_text("kipris_key_error.xml")))
    svc = make_service(router)
    r = run(svc.kr_biblio("10-2026-0012345"))
    assert r["ok"] is False
    assert r["error"]["code"] == E.AUTH_FAILED
    assert "KIPRIS_SERVICE_KEY" in r["error"]["message"]
    assert_no_secrets(r)


def test_kr_biblio_missing_key():
    router = Router()
    svc = make_service(router, kipris_key=None)
    r = run(svc.kr_biblio("10-2026-0012345"))
    assert r["ok"] is False and r["error"]["code"] == E.CONFIG_MISSING_KEY
    assert "KIPRIS_SERVICE_KEY" in r["error"]["message"]
    assert router.calls == []


def test_kr_biblio_invalid_number_no_call():
    router = Router()
    svc = make_service(router)
    r = run(svc.kr_biblio("KR 10-1234567 B1"))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT
    assert r["query"] == {"input": "KR 10-1234567 B1"}
    assert router.calls == []


def test_kipris_quota_warning_and_limit(fixture_text):
    router = Router()
    router.on(lambda r: True, httpx.Response(200, text=fixture_text("kipris_biblio.xml")))
    svc = make_service(router, limit=3, warn=2)
    run(svc.kr_biblio("10-2026-0000001"))
    r2 = run(svc.kr_biblio("10-2026-0000002"))
    assert any("2/3" in n for n in r2["notes"])
    run(svc.kr_biblio("10-2026-0000003"))
    r4 = run(svc.kr_biblio("10-2026-0000004"))
    assert r4["ok"] is False and r4["error"]["code"] == E.QUOTA_EXCEEDED
    assert len(router.calls) == 3
    # 한도에 닿아도 캐시는 응답한다
    r5 = run(svc.kr_biblio("10-2026-0000001"))
    assert r5["ok"] is True and r5["cached"] is True


def test_kipris_retry_on_503_then_ok(fixture_text):
    router = Router()
    responses = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200, text=fixture_text("kipris_biblio.xml"))])
    router.on(lambda r: True, lambda r: next(responses))
    svc = make_service(router)
    r = run(svc.kr_biblio("10-2020-0026123"))
    assert r["ok"] is True
    assert len(router.calls) == 3
    assert svc.quota.kipris_count() == 3  # 재시도도 호출 수에 들어간다


def test_kipris_timeout_gives_up_after_two_retries():
    router = Router()

    def boom(req):
        raise httpx.ReadTimeout("timeout", request=req)

    router.on(lambda r: True, boom)
    svc = make_service(router)
    r = run(svc.kr_biblio("10-2020-0026123"))
    assert r["ok"] is False and r["error"]["code"] == E.TIMEOUT
    assert len(router.calls) == 3


def test_kipris_network_error_hides_url():
    router = Router()

    def boom(req):
        raise httpx.ConnectError(f"failed {req.url}", request=req)

    router.on(lambda r: True, boom)
    svc = make_service(router)
    r = run(svc.kr_biblio("10-2020-0026123"))
    assert r["error"]["code"] == E.NETWORK_ERROR
    assert_no_secrets(r)


def test_kr_search_params(fixture_text):
    router = Router()
    router.on(lambda r: "getAdvancedSearch" in r.url.path, httpx.Response(200, text=fixture_text("kipris_search.xml")))
    svc = make_service(router)
    r = run(svc.kr_search(applicant="엘지에너지솔루션", ipc="H01M", date_from="2023-01-01", date_to="2024-12-31", page_size=2))
    assert r["ok"] is True and r["data"]["totalCount"] == 137
    p = router.calls[0].url.params
    assert p["applicant"] == "엘지에너지솔루션"
    assert p["ipcNumber"] == "H01M"
    assert p["applicationDate"] == "20230101~20241231"
    assert p["numOfRows"] == "2" and p["pageNo"] == "1"
    assert any("전체 137건" in n for n in r["notes"])


def test_kr_search_requires_condition():
    svc = make_service(Router())
    r = run(svc.kr_search())
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT


def test_kr_search_bad_date():
    svc = make_service(Router())
    r = run(svc.kr_search(keyword="배터리", date_from="2023-1"))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT


# --- OPS --------------------------------------------------------------------


def test_ep_biblio_ok(fixture_text):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: r.url.path.endswith("/biblio"), httpx.Response(
        200, text=fixture_text("ops_biblio.xml"), headers={"X-IndividualQuotaPerHour-Used": "100", "X-Throttling-Control": "idle (other=green:1000)"}
    ))
    svc = make_service(router)
    r = run(svc.ep_biblio("EP 1000000 A1"))
    assert r["ok"] is True and r["source"] == "EPO OPS"
    assert r["data"]["documents"][0]["publicationNumber"] == "EP1000000A1"
    assert router.calls[1].url.path == "/3.2/rest-services/published-data/publication/docdb/EP.1000000.A1/biblio"
    assert router.calls[1].headers["Authorization"] == "Bearer TOKEN-1"
    assert router.calls[0].content == b"grant_type=client_credentials"
    usage = run(svc.quota_status())["data"]["ops"]["headers"]
    assert usage["x-individualquotaperhour-used"] == "100"
    assert_no_secrets(r)


def test_ops_404_is_nodata(fixture_text):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: True, httpx.Response(404, text=fixture_text("ops_fault_404.xml")))
    svc = make_service(router)
    r = run(svc.family("EP1234567A1"))
    assert r["ok"] is True and r["data"] is None
    assert "해당 번호의 자료 없음" in r["notes"]


def test_ops_token_refresh_on_401(fixture_text):
    router = Router()
    tokens = iter(["TOKEN-1", "TOKEN-2"])
    router.on(is_auth, lambda r: httpx.Response(200, json={"access_token": next(tokens), "expires_in": "1199"}))
    router.on(
        lambda r: r.headers.get("Authorization") == "Bearer TOKEN-1",
        httpx.Response(401, text="<fault><code>CLIENT.InvalidAccessToken</code></fault>"),
    )
    router.on(lambda r: r.headers.get("Authorization") == "Bearer TOKEN-2", httpx.Response(200, text=fixture_text("ops_family.xml")))
    svc = make_service(router)
    r = run(svc.family("EP3100000A1"))
    assert r["ok"] is True and r["data"]["countries"] == ["EP", "KR", "US"]
    assert sum(1 for c in router.calls if is_auth(c)) == 2


def test_ops_token_reused_until_expiry(fixture_text):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: True, httpx.Response(200, text=fixture_text("ops_family.xml")))
    svc = make_service(router)
    run(svc.family("EP3100000A1"))
    run(svc.legal_status("EP3100000B1"))
    assert sum(1 for c in router.calls if is_auth(c)) == 1


def test_ops_bad_credentials():
    router = Router()
    router.on(is_auth, httpx.Response(401, json={"error": "invalid_client"}))
    svc = make_service(router)
    r = run(svc.ep_biblio("EP1000000A1"))
    assert r["ok"] is False and r["error"]["code"] == E.AUTH_FAILED
    assert "EPO_OPS_CONSUMER_KEY" in r["error"]["message"]
    assert_no_secrets(r)


def test_ops_missing_key():
    router = Router()
    svc = make_service(router, epo_secret=None)
    r = run(svc.ep_biblio("EP1000000A1"))
    assert r["error"]["code"] == E.CONFIG_MISSING_KEY
    assert "EPO_OPS_CONSUMER_SECRET" in r["error"]["message"]
    assert "EPO_OPS_CONSUMER_KEY," not in r["error"]["message"]
    assert router.calls == []


@pytest.mark.parametrize(
    "status, headers, body, code",
    [
        (429, {}, "", E.RATE_LIMITED),
        (403, {"X-Rejection-Reason": "IndividualQuotaPerHour"}, "", E.QUOTA_EXCEEDED),
        (403, {}, "ops_fault_403_quota.xml", E.QUOTA_EXCEEDED),
        (403, {}, "<fault><code>CLIENT.Forbidden</code><message>no</message></fault>", E.PERMISSION_DENIED),
        (400, {}, "<fault><code>CLIENT.InvalidReference</code><message>bad</message></fault>", E.INVALID_INPUT),
        (500, {}, "", E.UPSTREAM_ERROR),
    ],
)
def test_ops_status_mapping_no_retry(fixture_text, status, headers, body, code):
    if body.endswith(".xml") and not body.startswith("<"):
        body = fixture_text(body)
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: True, httpx.Response(status, text=body, headers=headers))
    svc = make_service(router)
    r = run(svc.legal_status("EP1000000B1"))
    assert r["ok"] is False and r["error"]["code"] == code
    data_calls = [c for c in router.calls if not is_auth(c)]
    assert len(data_calls) == 1  # 한도·권한 오류는 재시도하지 않는다


def test_ops_503_retried_twice():
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: True, httpx.Response(503))
    svc = make_service(router)
    r = run(svc.family("EP1000000A1"))
    assert r["error"]["code"] == E.UPSTREAM_UNAVAILABLE
    assert len([c for c in router.calls if not is_auth(c)]) == 3


def test_ep_text_truncation(fixture_text):
    long_claims = fixture_text("ops_claims.xml").replace(
        "1. Apparatus for manufacturing green bricks, comprising a mould.", "1. " + "가" * 5000
    )
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: r.url.path.endswith("/claims"), httpx.Response(200, text=long_claims))
    svc = make_service(router)
    r = run(svc.ep_text("EP1000000B1", "claims", max_chars=1000))
    assert r["ok"] is True
    d = r["data"]
    assert d["truncated"] is True and len(d["text"]) <= 1000 and d["length"] > 5000
    assert any("잘랐습니다" in n for n in r["notes"])
    # 같은 원문은 캐시에서 다른 길이로 다시 자른다
    r2 = run(svc.ep_text("EP1000000B1", "claims"))
    assert r2["cached"] is True and r2["data"]["truncated"] is False


def test_ep_text_lang_fallback_note(fixture_text):
    router = Router()
    router.on(is_auth, token_ok)
    router.on(lambda r: True, httpx.Response(200, text=fixture_text("ops_claims.xml")))
    svc = make_service(router)
    r = run(svc.ep_text("EP1000000B1", "claims", lang="fr"))
    assert r["data"]["lang"] == "DE"  # fr이 없으면 첫 언어
    assert any("요청한 언어" in n for n in r["notes"])


def test_ep_text_bad_section():
    svc = make_service(Router())
    r = run(svc.ep_text("EP1000000B1", "drawings"))
    assert r["ok"] is False and r["error"]["code"] == E.INVALID_INPUT


def test_ambiguous_kr_number_for_family():
    router = Router()
    svc = make_service(router)
    r = run(svc.family("10-2026-0012345"))
    assert r["ok"] is False and r["error"]["code"] == E.AMBIGUOUS_NUMBER
    assert router.calls == []


def test_quota_status_shape():
    svc = make_service(Router(), kipris_key=None)
    r = run(svc.quota_status())
    assert r["ok"] is True and r["source"] == "internal"
    assert r["data"]["kipris"]["calls"] == 0 and r["data"]["kipris"]["monthlyLimit"] == 1000
    assert r["data"]["keys"] == {"kipris": False, "epoOps": True}
    assert_no_secrets(r)


def test_blank_and_placeholder_values_are_missing(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("KIPRIS_SERVICE_KEY=fromfile\nEPO_OPS_CONSUMER_KEY=k\n", encoding="utf-8")
    # 확장 설치 창에서 비워 둔 값(빈 문자열·치환 안 된 자리표시)은 .env 값을 덮어쓰지 않는다
    monkeypatch.setenv("KIPRIS_SERVICE_KEY", "")
    monkeypatch.setenv("EPO_OPS_CONSUMER_KEY", "${user_config.epo_consumer_key}")
    monkeypatch.setenv("EPO_OPS_CONSUMER_SECRET", "${user_config.epo_consumer_secret}")
    monkeypatch.setenv("PATENT_API_CACHE_PATH", str(tmp_path / "c.sqlite3"))
    s = Settings.load(p)
    assert s.kipris_service_key == "fromfile"
    assert s.epo_consumer_key == "k"
    assert s.epo_consumer_secret is None


def test_mcpb_runtime_key_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("PATENT_API_RUNTIME", "mcpb")
    monkeypatch.setenv("PATENT_API_CACHE_PATH", str(tmp_path / "c.sqlite3"))
    monkeypatch.delenv("KIPRIS_SERVICE_KEY", raising=False)
    s = Settings.load(tmp_path / "none.env")
    assert s.runtime == "mcpb"
    svc = PatentService(s, http=httpx.AsyncClient(transport=httpx.MockTransport(Router())), cache=Cache(":memory:"), quota=Quota(":memory:"))
    r = run(svc.kr_biblio("10-2020-0168607"))
    assert r["error"]["code"] == E.CONFIG_MISSING_KEY
    assert "확장 프로그램" in r["error"]["message"]


def test_default_cache_path_migrates_legacy(tmp_path, monkeypatch):
    from core import config as cfg

    legacy = tmp_path / "old" / "cache.sqlite3"
    legacy.parent.mkdir()
    legacy.write_bytes(b"legacy")
    monkeypatch.setattr(cfg, "LEGACY_CACHE_PATH", legacy)
    monkeypatch.setattr(cfg, "user_data_dir", lambda: tmp_path / "new")
    path = cfg.default_cache_path()
    assert path == tmp_path / "new" / "cache.sqlite3"
    assert path.read_bytes() == b"legacy"
