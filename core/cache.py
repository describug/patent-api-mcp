"""SQLite 캐시. 키 = 도구명 + 정규화된 입력."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_MISSING = object()


def make_key(tool: str, params: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Cache:
    def __init__(self, path: Path | str, *, clock=time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL,"
                " created_at REAL NOT NULL, expires_at REAL NOT NULL)"
            )

    def get(self, tool: str, params: dict[str, Any], default: Any = None) -> Any:
        """보존 기간 안의 값을 돌려준다. 없으면 default. (저장된 값이 None일 수 있으므로 default로 구분)"""
        key = make_key(tool, params)
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        value, expires_at = row
        if expires_at <= self._clock():
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            return default
        return json.loads(value)

    def set(self, tool: str, params: dict[str, Any], value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        now = self._clock()
        key = make_key(tool, params)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now, now + ttl_seconds),
            )

    def purge_expired(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM cache WHERE expires_at <= ?", (self._clock(),))
            return cur.rowcount

    def close(self) -> None:
        self._conn.close()
