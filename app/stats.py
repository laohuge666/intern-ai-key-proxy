from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from typing import Optional

DB_PATH = os.environ.get("STATS_DB", "/app/data/stats.db")


class Stats:
    """请求统计，SQLite 持久化。

    - 累计/今日/总量 汇总：启动时从 DB 读出，变更时写回（WAL，低频）
    - 最近请求明细：写 DB，同时内存留一份最近 N 条供页面快速展示
    容器重启后累计数据不丢失；今日计数按自然日重置。
    """

    def __init__(self, max_recent: int = 100, db_path: Optional[str] = None) -> None:
        self._path = db_path or DB_PATH
        self._lock = threading.Lock()
        self._max_recent = max_recent
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS recent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                model TEXT,
                path TEXT,
                tokens INTEGER DEFAULT 0,
                status INTEGER DEFAULT 0,
                cache INTEGER DEFAULT 0
            );
            """
        )
        self._conn.commit()
        self._start = time.time()
        self._day = time.strftime("%Y-%m-%d")
        self._today_requests = self._counter("today_requests")
        self._total_requests = self._counter("total_requests")
        self._today_tokens = self._counter("today_tokens")
        self._total_tokens = self._counter("total_tokens")
        self._cache_hits = self._counter("cache_hits")
        self._errors = self._counter("errors")
        self._recent: deque = deque(maxlen=self._max_recent)
        self._load_recent()

    def _counter(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT value FROM counters WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, 0)", (name,)
            )
            self._conn.commit()
            return 0
        return int(row["value"])

    def _bump(self, name: str, delta: int) -> None:
        self._conn.execute(
            "UPDATE counters SET value = value + ? WHERE name=?", (delta, name)
        )

    def _load_recent(self) -> None:
        rows = self._conn.execute(
            "SELECT ts, model, path, tokens, status, cache FROM recent "
            "ORDER BY id DESC LIMIT ?",
            (self._max_recent,),
        ).fetchall()
        for r in reversed(rows):
            self._recent.append(
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(r["ts"])),
                    "model": r["model"] or "",
                    "path": r["path"] or "",
                    "tokens": r["tokens"],
                    "status": r["status"],
                    "cache": bool(r["cache"]),
                }
            )

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._today_requests = 0
            self._today_tokens = 0
            self._conn.execute(
                "UPDATE counters SET value=0 WHERE name IN ('today_requests','today_tokens')"
            )
            self._conn.commit()

    def record(
        self,
        model: str = "",
        path: str = "",
        tokens: int = 0,
        cache_hit: bool = False,
        error: bool = False,
        status_code: int = 200,
    ) -> None:
        now = time.time()
        with self._lock:
            self._roll_day()
            self._today_requests += 1
            self._total_requests += 1
            if tokens > 0:
                self._today_tokens += tokens
                self._total_tokens += tokens
            if cache_hit:
                self._cache_hits += 1
            if error or status_code >= 400:
                self._errors += 1
            self._bump("today_requests", 1)
            self._bump("total_requests", 1)
            if tokens > 0:
                self._bump("today_tokens", tokens)
                self._bump("total_tokens", tokens)
            if cache_hit:
                self._bump("cache_hits", 1)
            if error or status_code >= 400:
                self._bump("errors", 1)
            self._conn.execute(
                "INSERT INTO recent(ts, model, path, tokens, status, cache) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (now, model, path, tokens, status_code, int(cache_hit)),
            )
            self._conn.commit()
            self._recent.appendleft(
                {
                    "time": time.strftime("%H:%M:%S", time.localtime(now)),
                    "model": model,
                    "path": path,
                    "tokens": tokens,
                    "status": status_code,
                    "cache": cache_hit,
                }
            )

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            hit_rate = (
                round(self._cache_hits / self._total_requests * 100, 1)
                if self._total_requests
                else 0.0
            )
            return {
                "uptime": int(time.time() - self._start),
                "total_requests": self._total_requests,
                "today_requests": self._today_requests,
                "total_tokens": self._total_tokens,
                "today_tokens": self._today_tokens,
                "cache_hits": self._cache_hits,
                "cache_hit_rate": hit_rate,
                "errors": self._errors,
                "recent": list(self._recent),
            }


stats = Stats()
