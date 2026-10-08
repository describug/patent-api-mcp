"""호출 횟수·한도 관리.

- KIPRIS: 월별 호출 수를 센다. 경고선 이상이면 notes에 경고, 한도에 닿으면 호출하지 않는다.
- OPS: 응답 헤더의 사용량 정보를 기록해 둔다.
- KIPRIS 상품별 신청 상태: 호출 결과로 '신청됨/미신청'을 기억한다. 미신청은 하루만 기억해
  다음 조회 때 다시 확인한다(신청하면 자동으로 풀림). 인증키가 바뀌면 기록을 무시한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from .errors import QUOTA_EXCEEDED, PatentApiError

# OPS 사용량 관련 헤더(소문자). 이 밖에 'quota'·'throttl'·'rejection'이 들어간 헤더도 기록한다.
OPS_USAGE_HEADERS = (
    "x-throttling-control",
    "x-individualquotaperhour-used",
    "x-registeredquotaperweek-used",
    "x-registeredpayingquotaperweek-used",
    "x-rejection-reason",
)

SUBSCRIBED = "subscribed"
NOT_SUBSCRIBED = "not_subscribed"
NOT_SUBSCRIBED_MEMORY = timedelta(days=1)


def key_fingerprint(key: str | None) -> str:
    """인증키 자체는 저장하지 않고, 바뀌었는지만 알 수 있게 짧은 해시를 쓴다."""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


def _now() -> datetime:
    return datetime.now().astimezone()


class Quota:
    def __init__(
        self,
        path: Path | str,
        *,
        kipris_monthly_limit: int = 1000,
        kipris_warn_at: int = 900,
        now: Callable[[], datetime] = _now,
    ):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.limit = kipris_monthly_limit
        self.warn_at = kipris_warn_at
        self._now = now
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kipris_calls (month TEXT PRIMARY KEY, count INTEGER NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_usage ("
                " id INTEGER PRIMARY KEY CHECK (id = 1), headers TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kipris_products ("
                " product TEXT PRIMARY KEY, status TEXT NOT NULL, key_fp TEXT NOT NULL,"
                " checked_at TEXT NOT NULL, detail TEXT)"
            )

    # --- KIPRIS ---------------------------------------------------------

    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def kipris_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM kipris_calls WHERE month = ?", (self._month(),)
            ).fetchone()
        return row[0] if row else 0

    def check_kipris(self) -> None:
        """한도에 닿았으면 호출 전에 막는다."""
        count = self.kipris_count()
        if count >= self.limit:
            raise PatentApiError(
                QUOTA_EXCEEDED,
                f"이번 달 KIPRIS 호출 한도({self.limit}회)에 도달해 새 조회를 하지 않았습니다 "
                f"(현재 {count}회). 캐시에 있는 자료는 계속 응답합니다. "
                "한도는 KIPRIS_MONTHLY_LIMIT(.env 또는 확장 설정의 월 호출 한도)로 바꿀 수 있습니다.",
            )

    def record_kipris_call(self) -> int:
        month = self._month()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kipris_calls (month, count) VALUES (?, 1) "
                "ON CONFLICT(month) DO UPDATE SET count = count + 1",
                (month,),
            )
            row = self._conn.execute("SELECT count FROM kipris_calls WHERE month = ?", (month,)).fetchone()
        return row[0]

    def kipris_warning(self) -> str | None:
        count = self.kipris_count()
        if count >= self.warn_at:
            return f"이번 달 KIPRIS 호출 {count}/{self.limit}회 — 한도에 가깝습니다."
        return None

    # --- OPS ------------------------------------------------------------

    def record_ops_headers(self, headers: Mapping[str, str]) -> None:
        picked = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in OPS_USAGE_HEADERS or ("quota" in lk or "throttl" in lk or "rejection" in lk):
                picked[lk] = v
        if not picked:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ops_usage (id, headers, updated_at) VALUES (1, ?, ?)",
                (json.dumps(picked, ensure_ascii=False), self._now().isoformat(timespec="seconds")),
            )

    def ops_usage(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT headers, updated_at FROM ops_usage WHERE id = 1").fetchone()
        if not row:
            return None
        return {"headers": json.loads(row[0]), "recordedAt": row[1]}

    # --- KIPRIS 상품 신청 상태 ------------------------------------------

    def set_product_status(self, product: str, status: str, key: str | None, detail: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO kipris_products (product, status, key_fp, checked_at, detail) VALUES (?, ?, ?, ?, ?)",
                (product, status, key_fingerprint(key), self._now().isoformat(timespec="seconds"), detail),
            )

    def product_status(self, product: str, key: str | None) -> dict | None:
        """{'status', 'checkedAt', 'detail', 'active'} 또는 None(확인 안 함).

        active: 미신청 기록이 아직 유효해(하루 안) 호출을 건너뛸지. 신청됨이면 항상 False.
        인증키가 바뀌었으면 None(다시 확인).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status, key_fp, checked_at, detail FROM kipris_products WHERE product = ?", (product,)
            ).fetchone()
        if not row or row[1] != key_fingerprint(key):
            return None
        status, _, checked_at, detail = row
        active = False
        if status == NOT_SUBSCRIBED:
            try:
                checked = datetime.fromisoformat(checked_at)
                active = self._now() - checked < NOT_SUBSCRIBED_MEMORY
            except ValueError:
                active = False
        return {"status": status, "checkedAt": checked_at, "detail": detail, "active": active}

    def any_product_subscribed(self, key: str | None) -> bool:
        fp = key_fingerprint(key)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM kipris_products WHERE status = ? AND key_fp = ? LIMIT 1", (SUBSCRIBED, fp)
            ).fetchone()
        return row is not None

    def clear_product_status(self, status: str | None = None) -> int:
        with self._lock, self._conn:
            if status:
                cur = self._conn.execute("DELETE FROM kipris_products WHERE status = ?", (status,))
            else:
                cur = self._conn.execute("DELETE FROM kipris_products")
            return cur.rowcount

    # --- 요약 -----------------------------------------------------------

    def status(self) -> dict:
        count = self.kipris_count()
        return {
            "kipris": {
                "month": self._month(),
                "calls": count,
                "monthlyLimit": self.limit,
                "warnAt": self.warn_at,
                "remaining": max(self.limit - count, 0),
            },
            "ops": self.ops_usage(),
        }

    def close(self) -> None:
        self._conn.close()
