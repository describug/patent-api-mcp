from datetime import datetime

import pytest

from core import errors as E
from core.cache import Cache, make_key
from core.errors import PatentApiError
from core.quota import Quota


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_cache_key_is_stable():
    assert make_key("family", {"b": 1, "a": "가"}) == make_key("family", {"a": "가", "b": 1})
    assert make_key("family", {"a": 1}) != make_key("legal_status", {"a": 1})


def test_cache_roundtrip_and_expiry():
    clock = Clock()
    c = Cache(":memory:", clock=clock)
    c.set("ep_biblio", {"ref": "x"}, {"title": "제목"}, ttl_seconds=60)
    assert c.get("ep_biblio", {"ref": "x"}) == {"title": "제목"}
    assert c.get("ep_biblio", {"ref": "y"}) is None
    clock.t += 61
    assert c.get("ep_biblio", {"ref": "x"}) is None


def test_cache_distinguishes_stored_none_from_miss():
    c = Cache(":memory:", clock=Clock())
    miss = object()
    assert c.get("kr_biblio", {"n": 1}, miss) is miss
    c.set("kr_biblio", {"n": 1}, None, ttl_seconds=60)
    assert c.get("kr_biblio", {"n": 1}, miss) is None


def test_cache_zero_ttl_not_stored():
    c = Cache(":memory:", clock=Clock())
    c.set("x", {}, 1, ttl_seconds=0)
    assert c.get("x", {}) is None


def test_cache_purge(tmp_path):
    clock = Clock()
    c = Cache(tmp_path / "c.sqlite3", clock=clock)
    c.set("a", {}, 1, 10)
    c.set("b", {}, 2, 100)
    clock.t += 50
    assert c.purge_expired() == 1
    assert c.get("b", {}) == 2


class Now:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def test_quota_counts_per_month():
    now = Now(datetime(2026, 10, 7, 9, 0))
    q = Quota(":memory:", kipris_monthly_limit=5, kipris_warn_at=3, now=now)
    for _ in range(3):
        q.record_kipris_call()
    assert q.kipris_count() == 3
    now.dt = datetime(2026, 11, 1, 0, 0)
    assert q.kipris_count() == 0
    assert q.status()["kipris"]["month"] == "2026-11"


def test_quota_warning_and_limit():
    q = Quota(":memory:", kipris_monthly_limit=3, kipris_warn_at=2, now=Now(datetime(2026, 10, 7)))
    q.record_kipris_call()
    assert q.kipris_warning() is None
    q.check_kipris()
    q.record_kipris_call()
    assert "2/3" in q.kipris_warning()
    q.record_kipris_call()
    with pytest.raises(PatentApiError) as ei:
        q.check_kipris()
    assert ei.value.code == E.QUOTA_EXCEEDED
    assert "3회" in ei.value.message


def test_quota_ops_headers():
    q = Quota(":memory:", now=Now(datetime(2026, 10, 7, 12, 0)))
    assert q.ops_usage() is None
    q.record_ops_headers(
        {
            "X-Throttling-Control": "idle (images=green:200, inpadoc=green:60, other=green:1000, retrieval=green:200, search=green:30)",
            "X-IndividualQuotaPerHour-Used": "12345",
            "X-RegisteredQuotaPerWeek-Used": "678910",
            "Content-Type": "application/xml",
        }
    )
    u = q.ops_usage()
    assert u["headers"]["x-individualquotaperhour-used"] == "12345"
    assert "content-type" not in u["headers"]
    assert u["recordedAt"].startswith("2026-10-07T12:00")
    q.record_ops_headers({"Content-Type": "text/xml"})  # 사용량 헤더가 없으면 이전 기록 유지
    assert q.ops_usage()["headers"]["x-registeredquotaperweek-used"] == "678910"
