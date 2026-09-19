from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.keypool import KeyPool


@pytest.mark.asyncio
async def test_round_robin_cycles_through_keys():
    pool = KeyPool(["k1", "k2", "k3"], cooldown_seconds=30)
    picked = [await pool.acquire() for _ in range(6)]
    keys = [p[1] for p in picked]
    assert keys == ["k1", "k2", "k3", "k1", "k2", "k3"]
    assert all(p[2] is False for p in picked)  # 无降级


@pytest.mark.asyncio
async def test_error_cools_key_and_skips_it():
    pool = KeyPool(["k1", "k2"], cooldown_seconds=30)
    await pool.acquire()          # k1
    _, k2, _ = await pool.acquire()
    assert k2 == "k2"
    await pool.mark_error(1)      # k2 冷却
    picked = [await pool.acquire() for _ in range(3)]
    assert [p[1] for p in picked] == ["k1", "k1", "k1"]


@pytest.mark.asyncio
async def test_all_cooling_returns_earliest_thaw_and_degrades():
    pool = KeyPool(["k1", "k2"], cooldown_seconds=30)
    await pool.acquire()
    await pool.acquire()
    await pool.mark_error(0)
    await pool.mark_error(1)
    with patch("app.keypool.time.time", return_value=time.time() + 10):
        idx, key, degraded = await pool.acquire()
    assert degraded is True
    assert key in ("k1", "k2")


@pytest.mark.asyncio
async def test_mark_ok_clears_cooldown():
    pool = KeyPool(["k1", "k2"], cooldown_seconds=30)
    await pool.acquire()
    await pool.mark_error(0)
    snap = await pool.snapshot()
    assert snap[0]["status"] == "cooling"
    await pool.mark_ok(0)
    snap = await pool.snapshot()
    assert snap[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_snapshot_masks_keys():
    pool = KeyPool(["sk-abcdef1234567890", "short"], cooldown_seconds=30)
    snap = await pool.snapshot()
    assert snap[0]["key"].startswith("sk-abc")
    assert snap[0]["key"].endswith("7890")
    assert "***" in snap[0]["key"]
    assert snap[1]["key"] == "***"
