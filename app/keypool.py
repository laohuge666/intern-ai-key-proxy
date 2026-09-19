from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import settings

KEY_STORE = os.environ.get("KEY_STORE", "/app/data/keys.json")


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
    - add(key) / remove(index): 运行期增删 Key，并持久化到 data/keys.json。
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

    async def add(self, key: str) -> int:
        """添加新 Key，返回新索引。重复 Key 报错。持久化明文到 keys.json。"""
        key = (key or "").strip()
        if not key:
            raise ValueError("Key 不能为空")
        async with self._lock:
            if any(e.key == key for e in self._entries):
                raise ValueError("该 Key 已存在")
            self._entries.append(_Entry(key=key))
            self._persist_locked()
            return len(self._entries) - 1

    async def remove(self, index: int) -> None:
        """删除指定索引的 Key。至少保留一个，否则调度无 Key 可用。"""
        async with self._lock:
            if not 0 <= index < len(self._entries):
                raise IndexError("索引超出范围")
            if len(self._entries) <= 1:
                raise ValueError("至少保留一个 Key，不能删除")
            self._entries.pop(index)
            if self._cursor >= len(self._entries):
                self._cursor = 0
            self._persist_locked()

    def _persist_locked(self) -> None:
        """把当前全部 Key 明文落盘（管理后台增删后立即生效，重启不丢失）。"""
        try:
            os.makedirs(os.path.dirname(KEY_STORE) or ".", exist_ok=True)
            tmp = f"{KEY_STORE}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump([e.key for e in self._entries], fh, ensure_ascii=False, indent=2)
            os.replace(tmp, KEY_STORE)
            try:
                os.chmod(KEY_STORE, 0o600)
            except OSError:
                pass
        except OSError:
            pass

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


def _load_persisted_keys(defaults: List[str]) -> List[str]:
    """启动时优先读 data/keys.json；没有则用环境变量的 Key，并写一份过去。"""
    try:
        with open(KEY_STORE, "r", encoding="utf-8") as fh:
            keys = [k.strip() for k in json.load(fh) if isinstance(k, str) and k.strip()]
        if keys:
            return keys
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return list(defaults)


_init_keys = _load_persisted_keys(settings.keys)
pool = KeyPool(_init_keys, settings.key_cooldown_seconds)

# 首次启动（环境变量有 Key 但还没有 keys.json）时落一份盘
if _init_keys and not os.path.exists(KEY_STORE):
    try:
        os.makedirs(os.path.dirname(KEY_STORE) or ".", exist_ok=True)
        with open(KEY_STORE, "w", encoding="utf-8") as fh:
            json.dump(_init_keys, fh, ensure_ascii=False, indent=2)
        os.chmod(KEY_STORE, 0o600)
    except OSError:
        pass


def mask(key: Optional[str]) -> str:
    if not key:
        return "<none>"
    return key[:6] + "***" + key[-4:] if len(key) > 12 else "***"
