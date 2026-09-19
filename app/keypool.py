from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import settings


@dataclass
class _Entry:
    key: str
    # 冷却到期时间戳（0 表示可用）
    cooldown_until: float = 0.0
    # 累计被采用次数
    used: int = 0
    # 累计出错次数
    errors: int = 0


class KeyPool:
    """轮询调度 + 冷却切换的 API Key 池。

    - acquire(): 按轮询顺序取一个未冷却的 Key，返回 (index, key)。
      若全部冷却中，则返回（强制）最早解冻的那一个，并标记降级。
    - mark_error(index): 记录错误并把该 Key 置入冷却。
    - mark_ok(index): 记录成功使用。
    """

    def __init__(self, keys: List[str], cooldown_seconds: int) -> None:
        if not keys:
            raise ValueError("KeyPool 需要至少一个 Key")
        self._entries: List[_Entry] = [_Entry(key=k) for k in keys]
        self._cooldown = max(1, int(cooldown_seconds))
        self._cursor = 0
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    async def acquire(self) -> Tuple[int, str, bool]:
        """返回 (index, key, degraded)。degraded=True 表示所有 Key 都在冷却，
        返回的是最早解冻的那一个（按到期时间升序选择）。"""
        async with self._lock:
            now = time.time()
            n = len(self._entries)
            for _ in range(n):
                idx = self._cursor % n
                self._cursor = (self._cursor + 1) % n
                entry = self._entries[idx]
                if entry.cooldown_until <= now:
                    entry.used += 1
                    return idx, entry.key, False
            # 全部冷却中：选最早解冻的
            idx = min(range(n), key=lambda i: self._entries[i].cooldown_until)
            entry = self._entries[idx]
            entry.used += 1
            return idx, entry.key, True

    async def mark_error(self, index: int) -> None:
        async with self._lock:
            entry = self._entries[index]
            entry.errors += 1
            entry.cooldown_until = time.time() + self._cooldown

    async def mark_ok(self, index: int) -> None:
        async with self._lock:
            entry = self._entries[index]
            entry.cooldown_until = 0.0

    def _snapshot(self) -> List[Dict]:
        now = time.time()
        return [
            {
                "index": i,
                "key": e.key[:6] + "***" + e.key[-4:] if len(e.key) > 12 else "***",
                "status": "cooling" if e.cooldown_until > now else "ready",
                "cooldown_left": round(max(0.0, e.cooldown_until - now), 1),
                "used": e.used,
                "errors": e.errors,
            }
            for i, e in enumerate(self._entries)
        ]

    async def snapshot(self) -> List[Dict]:
        async with self._lock:
            return self._snapshot()

    def snapshot_sync(self) -> List[Dict]:
        return self._snapshot()


pool = KeyPool(settings.keys, settings.key_cooldown_seconds)


def mask(key: Optional[str]) -> str:
    if not key:
        return "<none>"
    return key[:6] + "***" + key[-4:] if len(key) > 12 else "***"
