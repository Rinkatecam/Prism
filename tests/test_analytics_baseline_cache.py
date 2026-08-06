"""Tests for analytics.baseline_cache — the per-(server, segment) TTL
cache that backs ``detect_anomalies()``.

Why this matters: under v2 the aggregator calls ``detect_anomalies()``
on every metric sample (~1/min/server). Without the cache, each call
issues ``db.get_metric_stats()`` which reads a 7-day window. With the
cache, only one DB read per server per ``BASELINE_CACHE_TTL_S`` (5 min)
window — scales linearly with fleet, not with sample rate. These tests
guard the correctness of that optimization.

Test plan:
  1. Cache hit returns IDENTICAL anomalies to cache miss (no semantic
     drift from refactoring detect_anomalies into get→put→build).
  2. TTL expiry causes a re-read of the DB.
  3. clear_baseline_cache(None) flushes everything.
  4. clear_baseline_cache(name) scoped flush touches only that server.
  5. Memory cap evicts oldest entry.
  6. Segment isolation: business vs off_hours cache distinctly.
  7. "Insufficient data" verdict is cached too (regression: would
     thrash the DB without this).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import analytics


@pytest.fixture(autouse=True)
def _flush_cache_between_tests():
    """Each test gets a clean cache + counter slate. Without this, the
    hit/miss totals would bleed across tests and assertions on them
    would be order-dependent."""
    analytics.clear_baseline_cache()
    analytics._cache_hits = 0
    analytics._cache_misses = 0
    analytics._cache_evictions = 0
    yield
    analytics.clear_baseline_cache()


def _mock_db_with_history(rows: list[dict]) -> MagicMock:
    """Build a mock DB that returns ``rows`` from get_metric_stats(...).

    Setting ``return_value`` (rather than ``side_effect``) lets the test
    count call-count to verify cache behaviour."""
    db = MagicMock()
    db.get_metric_stats.return_value = rows
    return db


def _history(n: int, cpu_mean: float = 30.0) -> list[dict]:
    """Synthetic history: n rows of stable cpu around ``cpu_mean``,
    with predictable jitter so stddev is deterministic. The other
    metrics get fixed values that won't trigger anomalies."""
    return [
        {
            "timestamp": f"2026-05-{(i % 28) + 1:02d}T10:00:00Z",
            "cpu_percent": cpu_mean + ((i % 5) - 2),  # ±2 jitter
            "ram_percent": 50.0,
            "disk_c_percent": 40.0,
            "disk_d_percent": -1,  # not present
        }
        for i in range(n)
    ]


def test_cache_hit_returns_identical_anomalies_to_cache_miss():
    """Refactor safety: build path (miss) and read path (hit) must agree.

    This is the property test that proves the cache doesn't change
    detection semantics — only how the stats are sourced.
    """
    db = _mock_db_with_history(_history(200, cpu_mean=30.0))
    current = {"cpu": 90.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    miss_result = analytics.detect_anomalies(db, "srv1", current)
    assert len(miss_result) > 0, "anomaly at cpu=90 vs baseline 30 should fire"
    assert db.get_metric_stats.call_count == 1

    hit_result = analytics.detect_anomalies(db, "srv1", current)
    assert hit_result == miss_result
    # Critical: the DB must NOT have been touched on the second call.
    assert db.get_metric_stats.call_count == 1


def test_ttl_expiry_triggers_db_reread(monkeypatch):
    """After TTL elapses, the next call must hit the DB again."""
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    analytics.detect_anomalies(db, "srv1", current)
    assert db.get_metric_stats.call_count == 1

    # Fast-forward the cache TTL by mutating the stored timestamp.
    key = ("srv1", "")
    analytics._BASELINE_CACHE[key]["ts"] -= analytics.BASELINE_CACHE_TTL_S + 1

    analytics.detect_anomalies(db, "srv1", current)
    assert db.get_metric_stats.call_count == 2, "expired entry should trigger re-read"


def test_global_clear_flushes_everything():
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}
    analytics.detect_anomalies(db, "srvA", current)
    analytics.detect_anomalies(db, "srvB", current)
    assert len(analytics._BASELINE_CACHE) == 2

    cleared = analytics.clear_baseline_cache()
    assert cleared == 2
    assert len(analytics._BASELINE_CACHE) == 0


def test_scoped_clear_touches_only_named_server():
    """Per-server flush should leave other servers' entries intact."""
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}
    analytics.detect_anomalies(db, "srvA", current)
    analytics.detect_anomalies(db, "srvB", current)
    analytics.detect_anomalies(db, "srvA", current, segment="business")
    assert len(analytics._BASELINE_CACHE) == 3  # srvA-, srvB-, srvA-business

    cleared = analytics.clear_baseline_cache("srvA")
    assert cleared == 2, "both srvA entries (default + business) should clear"
    remaining = list(analytics._BASELINE_CACHE.keys())
    assert remaining == [("srvB", "")]


def test_memory_cap_evicts_oldest_on_overflow(monkeypatch):
    """When the cache fills to BASELINE_CACHE_MAX_ENTRIES, a new put()
    should evict the oldest entry. This guards against memory growth
    if a deployment has wild server churn."""
    monkeypatch.setattr(analytics, "BASELINE_CACHE_MAX_ENTRIES", 3)
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    analytics.detect_anomalies(db, "srv1", current)
    time.sleep(0.001)  # spread timestamps so "oldest" is unambiguous
    analytics.detect_anomalies(db, "srv2", current)
    time.sleep(0.001)
    analytics.detect_anomalies(db, "srv3", current)
    assert len(analytics._BASELINE_CACHE) == 3

    analytics.detect_anomalies(db, "srv4", current)  # should evict srv1
    assert len(analytics._BASELINE_CACHE) == 3
    keys = {k[0] for k in analytics._BASELINE_CACHE.keys()}
    assert "srv1" not in keys, "oldest entry should have been evicted"
    assert keys == {"srv2", "srv3", "srv4"}
    assert analytics._cache_evictions == 1


def test_segment_isolation():
    """The same server in different segments must cache separately —
    'CPU at 30%' might be normal during business hours and anomalous
    off-hours (or vice versa)."""
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    analytics.detect_anomalies(db, "srv1", current, segment="business")
    analytics.detect_anomalies(db, "srv1", current, segment="off_hours")
    assert ("srv1", "business") in analytics._BASELINE_CACHE
    assert ("srv1", "off_hours") in analytics._BASELINE_CACHE
    assert db.get_metric_stats.call_count == 2


def test_insufficient_data_verdict_is_cached():
    """Regression: if a server has < MIN_READINGS samples,
    detect_anomalies returns []. Without caching this verdict, every
    future sample would re-read the DB to learn the same thing — a
    DoS vector for fresh servers in large fleets.
    """
    db = _mock_db_with_history(_history(5))  # well under MIN_READINGS=24
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    result1 = analytics.detect_anomalies(db, "fresh-server", current)
    assert result1 == []
    assert db.get_metric_stats.call_count == 1

    result2 = analytics.detect_anomalies(db, "fresh-server", current)
    assert result2 == []
    # Critical: do NOT re-read the DB just because the verdict was empty.
    assert db.get_metric_stats.call_count == 1


def test_get_cache_stats_shape():
    """The /api/system/health endpoint exposes these — make sure the
    shape stays stable."""
    db = _mock_db_with_history(_history(200))
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}
    analytics.detect_anomalies(db, "srv1", current)  # 1 miss
    analytics.detect_anomalies(db, "srv1", current)  # 1 hit

    stats = analytics.get_baseline_cache_stats()
    assert set(stats.keys()) == {
        "size", "hits", "misses", "evictions", "hit_rate",
        "ttl_s", "max_entries",
    }
    assert stats["size"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    assert stats["evictions"] == 0


def test_db_failure_does_not_poison_cache():
    """If db.get_metric_stats raises, detect_anomalies returns [] and
    we MUST NOT cache anything — otherwise a transient DB blip would
    silently disable detection for a server for TTL seconds.
    """
    db = MagicMock()
    db.get_metric_stats.side_effect = RuntimeError("transient DB blip")
    current = {"cpu": 30.0, "ram": 50.0, "disk_c": 40.0, "disk_d": -1}

    result = analytics.detect_anomalies(db, "srv1", current)
    assert result == []
    assert ("srv1", "") not in analytics._BASELINE_CACHE, (
        "DB failures must not populate the cache — otherwise the next "
        "5 min of detection would silently be a no-op"
    )

    # Recovery path: next call should re-attempt the DB.
    db.get_metric_stats.side_effect = None
    db.get_metric_stats.return_value = _history(200)
    analytics.detect_anomalies(db, "srv1", current)
    assert ("srv1", "") in analytics._BASELINE_CACHE
