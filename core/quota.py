"""호출 횟수·한도 관리.

- KIPRIS: 월별 호출 수를 센다. 경고선 이상이면 notes에 경고, 한도에 닿으면 호출하지 않는다.
- OPS: 응답 헤더의 사용량 정보를 기록해 둔다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
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
