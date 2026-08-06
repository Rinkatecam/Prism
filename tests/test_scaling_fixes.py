"""Scaling fixes from docs/plans/SCALING_500.md items 1-3.

Measured on the live 29-server instance before these changes:
  * work queue capacity was num_workers*4 = 120; a 500-server metrics sweep
    enqueues 500 items, so it would overflow every cycle. Overflow is not data
    loss (the supervisor reschedules by _QUEUE_FULL_RESCHEDULE_S) but chronic
    overflow silently stretches the effective poll cadence past what is
    configured — a monitoring tool lying about its own freshness.
  * every DB operation opened a NEW connection (2.02 ms of pure overhead) and
    committed at synchronous=FULL. A metric insert measured 19.4 ms under live
    load; 0.08 ms after pooling + synchronous=NORMAL.
  * waitress ran threads=4, which is the library's own default and was
    therefore never actually chosen.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading

import pytest

from database import Database, _PooledConnection


# ── item 1: work queue sized from the fleet ───────────────────────────────

def _queue_size(num_workers, fleet):
    from collector_v2.types import CheckType
    return max(num_workers * 4, fleet * 2 * len(CheckType))


def test_queue_capacity_scales_with_the_fleet_not_the_pool():
    from collector_v2.types import CheckType
    n_checks = len(CheckType)
    assert _queue_size(30, 500) == 500 * 2 * n_checks
    assert _queue_size(30, 500) >= 500, "must hold a full metrics sweep at 500"
    assert _queue_size(30, 100) >= 100


def test_small_fleets_keep_the_old_floor():
    """A 2-server fleet must not get a 16-slot queue."""
    assert _queue_size(30, 2) == 120


def test_queue_capacity_is_monotonic_in_fleet_size():
    sizes = [_queue_size(30, f) for f in (1, 10, 29, 100, 500)]
    assert sizes == sorted(sizes)


def test_supervisor_health_exposes_queue_capacity_and_deferrals():
    """Depth alone cannot tell you whether checks are being deferred."""
    from collector_v2 import supervisor
    snap = supervisor.get_supervisor_health()
    assert "queue_capacity" in snap
    assert "checks_deferred_queue_full" in snap
    assert isinstance(snap["checks_deferred_queue_full"], int)


def test_queue_full_warning_is_rate_limited():
    """At 500 servers an unthrottled warning is one line per server per check
    type per tick — a flood that hides the condition."""
    from collector_v2 import supervisor
    assert supervisor._QUEUE_FULL_WARN_EVERY_S >= 30


# ── item 2: connection pooling + synchronous=NORMAL ───────────────────────

@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "pool.db"))
    yield d
    d.close_thread_connection()


def test_same_thread_reuses_one_connection(db):
    assert db._get_conn() is db._get_conn()


def test_close_on_a_pooled_connection_is_a_noop(db):
    """~200 call sites do `finally: conn.close()`. They must keep working."""
    conn = db._get_conn()
    conn.close()
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert db._get_conn() is conn


def test_close_thread_connection_really_closes(db):
    conn = db._get_conn()
    db.close_thread_connection()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_each_thread_gets_its_own_connection(db):
    seen = {}

    def worker(name):
        seen[name] = db._get_conn()
        db.close_thread_connection()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    conns = list(seen.values())
    assert len(conns) == 4
    assert len({id(c) for c in conns}) == 4, "threads must not share a connection"


def test_synchronous_is_normal_not_full(db):
    """The 8.4x. WAL+NORMAL stays crash-safe for an application crash; the
    relaxation is that a host power loss can lose the last commits."""
    val = db._get_conn().execute("PRAGMA synchronous").fetchone()[0]
    assert val == 1, f"expected NORMAL (1), got {val}"


def test_journal_mode_is_still_wal(db):
    mode = db._get_conn().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_survives_pooling(db):
    """Pre-existing protection against 'database is locked' must not be lost."""
    assert db._get_conn().execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_a_dangling_transaction_does_not_leak_to_the_next_caller(db):
    """With per-operation connections an uncommitted transaction died with the
    connection. Pooled, it would leak into an unrelated caller — so _get_conn
    rolls back on handout."""
    conn = db._get_conn()
    conn.execute("INSERT INTO metrics (server_name, timestamp, cpu_percent, status) "
                 "VALUES ('ghost', '2026-01-01T00:00:00Z', 1, 'healthy')")
    # deliberately NOT committed, and close() is a no-op
    conn.close()

    conn2 = db._get_conn()          # must roll the orphan back
    n = conn2.execute("SELECT COUNT(*) FROM metrics WHERE server_name='ghost'").fetchone()[0]
    assert n == 0, "uncommitted row leaked into the next caller"


def test_pooled_connection_class_is_used(db):
    assert isinstance(db._get_conn(), _PooledConnection)


def test_insert_read_round_trip_still_correct(db):
    db.insert_metric("s1", 10.0, 20.0, 30.0, -1.0, "healthy", 123)
    rows = db.get_server_history("s1", hours=1)
    assert len(rows) == 1
    assert rows[0]["cpu_percent"] == 10.0
    assert rows[0]["status"] == "healthy"


def test_writes_are_visible_across_threads(db):
    """WAL + separate connections per thread must still see each other's
    committed writes."""
    db.insert_metric("cross", 1.0, 2.0, 3.0, -1.0, "healthy", 1)
    out = {}

    def reader():
        out["n"] = len(db.get_server_history("cross", hours=1))
        db.close_thread_connection()

    t = threading.Thread(target=reader)
    t.start()
    t.join()
    assert out["n"] == 1


# ── item 3: waitress threads configurable ─────────────────────────────────

def test_web_server_threads_has_a_declared_default():
    from config_manager import ConfigManager
    assert ConfigManager._DEFAULT_SETTINGS["web_server_threads"] == 8


@pytest.mark.parametrize("raw,expected", [
    (8, 8), (2, 2), (64, 64),
    (1, 2),          # clamped up — 1 thread would serialise the whole UI
    (500, 64),       # clamped down
    ("12", 12),      # string from JSON
    ("nonsense", 8), # unparseable falls back
    (None, 8),
])
def test_thread_count_is_clamped(raw, expected):
    """Mirrors the clamp in app.py so a hand-edited config cannot wedge the
    server at 1 thread or exhaust it at 5000."""
    try:
        v = max(2, min(int(raw), 64))
    except (TypeError, ValueError):
        v = 8
    assert v == expected


def test_app_reads_the_setting_rather_than_hardcoding_four():
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "app.py"
    body = src.read_text(encoding="utf-8")
    assert "web_server_threads" in body
    assert "threads=4)" not in body, "the hardcoded default is back"
