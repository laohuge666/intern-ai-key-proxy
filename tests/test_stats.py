from __future__ import annotations

from app.stats import Stats


def test_empty_snapshot():
    s = Stats()
    snap = s.snapshot()
    assert snap["total_requests"] == 0
    assert snap["cache_hit_rate"] == 0.0


def test_record_counts_requests_and_tokens():
    s = Stats()
    s.record(model="m1", path="/v1/chat/completions", tokens=100)
    s.record(model="m1", path="/v1/chat/completions", tokens=50)
    snap = s.snapshot()
    assert snap["total_requests"] == 2
    assert snap["total_tokens"] == 150
    assert len(snap["recent"]) == 2
    assert snap["recent"][0]["model"] == "m1"


def test_cache_hit_rate():
    s = Stats()
    s.record(tokens=10, cache_hit=True)
    s.record(tokens=10, cache_hit=False)
    assert s.snapshot()["cache_hit_rate"] == 50.0


def test_errors_counted():
    s = Stats()
    s.record(status_code=500)
    s.record(status_code=200)
    assert s.snapshot()["errors"] == 1


def test_recent_capped():
    s = Stats(max_recent=5)
    for i in range(10):
        s.record(tokens=i)
    assert len(s.snapshot()["recent"]) == 5
