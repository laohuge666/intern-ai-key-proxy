from __future__ import annotations

import os

import pytest

from app.stats import Stats


def test_persists_across_instances(tmp_path):
    db = str(tmp_path / "stats.db")
    s1 = Stats(db_path=db)
    s1.record(model="m1", path="/v1/chat/completions", tokens=100)
    snap1 = s1.snapshot()
    assert snap1["total_requests"] == 1
    assert snap1["total_tokens"] == 100
    del s1

    # 重新打开同一个 DB，累计数据应保留
    s2 = Stats(db_path=db)
    snap2 = s2.snapshot()
    assert snap2["total_requests"] == 1
    assert snap2["total_tokens"] == 100
    assert len(snap2["recent"]) == 1
    assert snap2["recent"][0]["model"] == "m1"


def test_recent_loaded_from_db(tmp_path):
    db = str(tmp_path / "stats.db")
    s1 = Stats(db_path=db, max_recent=5)
    for i in range(3):
        s1.record(model=f"m{i}", path="/v1/models")
    del s1
    s2 = Stats(db_path=db, max_recent=5)
    snap = s2.snapshot()
    assert len(snap["recent"]) == 3


def test_file_created(tmp_path):
    db = str(tmp_path / "stats.db")
    Stats(db_path=db)
    assert os.path.exists(db)
