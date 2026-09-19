from __future__ import annotations

import time
from collections import deque
from threading import Lock

# 进程内轻量统计：请求数、token 用量、缓存命中、最近请求记录
# 不做持久化，容器重启归零；如需持久化可后续落 SQLite


class Stats:
    def __init__(self, max_recent: int = 100) -> None:
        self._lock = Lock()
        self._start = time.time()
        self._total_requests = 0
        self._today_requests = 0
        self._day = time.strftime("%Y-%m-%d")
        self._total_tokens = 0
        self._today_tokens = 0
        self._cache_hits = 0
        self._errors = 0
        self._recent: deque = deque(maxlen=max_recent)

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._today_requests = 0
            self._today_tokens = 0

    def record(
        self,
        model: str = "",
        path: str = "",
        tokens: int = 0,
        cache_hit: bool = False,
        error: bool = False,
        status_code: int = 200,
    ) -> None:
        with self._lock:
            self._roll_day()
            self._total_requests += 1
            self._today_requests += 1
            if tokens > 0:
                self._total_tokens += tokens
                self._today_tokens += tokens
            if cache_hit:
                self._cache_hits += 1
            if error or status_code >= 400:
                self._errors += 1
            self._recent.appendleft(
                {
                    "time": time.strftime("%H:%M:%S"),
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
