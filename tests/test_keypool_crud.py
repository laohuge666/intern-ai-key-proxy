from __future__ import annotations

import json

import pytest

from app.keypool import KeyPool


def _pool(keys=("k1", "k2"), cooldown=30):
    return KeyPool(list(keys), cooldown_seconds=cooldown)


@pytest.mark.asyncio
async def test_add_appends_key_and_returns_index():
    pool = _pool(["k1"])
    idx = await pool.add("sk-newkey1234567890")
    assert idx == 1
    snap = await pool.snapshot()
    assert len(snap) == 2
    assert snap[1]["key"].startswith("sk-new")


@pytest.mark.asyncio
async def test_add_rejects_empty_and_duplicate():
    pool = _pool(["k1"])
    with pytest.raises(ValueError):
        await pool.add("")
    with pytest.raises(ValueError):
        await pool.add("   ")
    with pytest.raises(ValueError):
        await pool.add("k1")


@pytest.mark.asyncio
async def test_remove_deletes_correct_entry():
    pool = _pool(["k1", "k2", "k3"])
    await pool.acquire()
    await pool.remove(1)
    snap = await pool.snapshot()
    assert len(snap) == 2
    # 删除后剩下的两个 key 通过 acquire 顺序验证（k1 已被 acquire 过，下一个应是 k3）
    _, key, _ = await pool.acquire()
    assert key == "k3"


@pytest.mark.asyncio
async def test_remove_rejects_last_key():
    pool = _pool(["only"])
    with pytest.raises(ValueError):
        await pool.remove(0)


@pytest.mark.asyncio
async def test_remove_out_of_range():
    pool = _pool(["k1", "k2"])
    with pytest.raises(IndexError):
        await pool.remove(9)


@pytest.mark.asyncio
async def test_persist_locked_writes_all_keys(tmp_path):
    import app.keypool as kp

    store = str(tmp_path / "keys.json")
    pool = _pool(["k1"])
    orig = kp.KEY_STORE
    try:
        kp.KEY_STORE = store
        await pool.add("sk-persisted1234567890")
        with open(store) as fh:
            keys = json.load(fh)
        assert keys == ["k1", "sk-persisted1234567890"]
    finally:
        kp.KEY_STORE = orig
