"""Log ingest filtering, signature coalescing, and per-table retention.

`logs` was 1,771,744 rows on the live 29-server fleet — 96% of every row in the
database — of which 73.1% was Information level, with a 10.8x duplicate ratio
(1.77M rows collapsing to 163,986 distinct signatures). Projected to 500 servers
that is ~58M rows and an 8 GB database.

Two controls, both at ingest because the cost being managed is storage and
write-lock time, not read time:
  * Information-level rows are dropped unless their Source/EventID is on an
    allow-list.
  * Surviving rows are ALSO coalesced into log_signatures as per-signature,
    per-hour counts, which is what survives the raw table's short retention.

Measured replaying 200,000 real log rows through this path: 122,105 dropped,
19,053 rescued by the allow-list, 77,895 stored (2.6x fewer raw rows) and 7,018
signature rows (28.5x collapse).

Everything the filter discards is COUNTED and exposed on /api/system/health.
Silent filtering is how a monitoring tool loses trust.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "logs.db"))
    Database.logs_dropped_information = 0
    Database.logs_kept_by_allowlist = 0
    yield d
    d.close_thread_connection()


DEFAULT_CFG = {
    "drop_information": True,
    "information_allowlist": ["System/7036", "System/1074", "System/6006",
                              "System/6008", "System/7045"],
    "coalesce_signatures": True,
}


def _ts(mins_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _log(level="Error", source="System", event_id=1000, message="boom", mins_ago=0):
    return {"time": _ts(mins_ago), "source": source, "level": level,
            "event_id": event_id, "message": message}


def _count(db, table="logs"):
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ── the level filter ──────────────────────────────────────────────────────

def test_information_rows_are_dropped(db):
    db.insert_logs("s1", [_log(level="Information", event_id=9999)] * 5,
                   ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 0
    assert Database.logs_dropped_information == 5


def test_error_and_warning_are_always_kept(db):
    db.insert_logs("s1", [_log(level="Error"), _log(level="Warning"),
                          _log(level="Critical")], ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 3
    assert Database.logs_dropped_information == 0


@pytest.mark.parametrize("event_id", [7036, 1074, 6006, 6008, 7045])
def test_allowlisted_information_events_survive(db, event_id):
    """Service state changes and shutdown records answer real questions, so
    they are kept even at Information level."""
    db.insert_logs("s1", [_log(level="Information", source="System",
                               event_id=event_id)], ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 1
    assert Database.logs_kept_by_allowlist == 1
    assert Database.logs_dropped_information == 0


def test_allowlist_matches_on_source_AND_event_id(db):
    """7036 from a different source must not be rescued — the allow-list is
    Source/EventID, not a bare event number."""
    db.insert_logs("s1", [_log(level="Information", source="Application",
                               event_id=7036)], ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 0
    assert Database.logs_dropped_information == 1


def test_filter_can_be_switched_off(db):
    db.insert_logs("s1", [_log(level="Information", event_id=1)] * 3,
                   ingest_cfg={"drop_information": False,
                               "coalesce_signatures": False})
    assert _count(db) == 3
    assert Database.logs_dropped_information == 0


def test_no_config_defaults_to_filtering_on(db):
    """The collector always passes settings.log_ingest, but a caller that
    forgets must still get the safe (volume-controlled) behaviour."""
    db.insert_logs("s1", [_log(level="Information", event_id=1)])
    assert _count(db) == 0


def test_a_batch_filtered_to_nothing_writes_nothing_and_does_not_crash(db):
    db.insert_logs("s1", [_log(level="Information", event_id=1)] * 10,
                   ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 0
    assert _count(db, "log_signatures") == 0


# ── signature coalescing ──────────────────────────────────────────────────

def test_identical_rows_collapse_to_one_signature_with_a_count(db):
    db.insert_logs("s1", [_log(message="disk failure")] * 50,
                   ingest_cfg=DEFAULT_CFG)
    assert _count(db) == 50, "raw rows are still written for drill-down"
    conn = sqlite3.connect(db.db_path)
    rows = conn.execute("SELECT count, sample FROM log_signatures").fetchall()
    conn.close()
    assert len(rows) == 1, "50 identical rows must be ONE signature"
    assert rows[0][0] == 50
    assert rows[0][1] == "disk failure"


def test_repeated_batches_increment_rather_than_duplicate(db):
    for _ in range(3):
        db.insert_logs("s1", [_log(message="same")] * 4, ingest_cfg=DEFAULT_CFG)
    conn = sqlite3.connect(db.db_path)
    rows = conn.execute("SELECT count FROM log_signatures").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 12, "upsert must accumulate across batches"


def test_different_messages_are_different_signatures(db):
    db.insert_logs("s1", [_log(message="alpha"), _log(message="beta")],
                   ingest_cfg=DEFAULT_CFG)
    assert _count(db, "log_signatures") == 2


def test_signatures_are_per_server(db):
    """'this signature is on 1 server' vs 'on 400' is the whole value of a
    fleet report, so the server must be part of the key."""
    db.insert_logs("s1", [_log(message="x")], ingest_cfg=DEFAULT_CFG)
    db.insert_logs("s2", [_log(message="x")], ingest_cfg=DEFAULT_CFG)
    assert _count(db, "log_signatures") == 2


def test_first_and_last_seen_bracket_the_occurrences(db):
    """Aggregated across rows rather than asserting on one: the hour is part of
    the signature key, so two timestamps 40 minutes apart land in one bucket or
    two depending purely on what time the test runs. Asserting on a single row
    made this pass or fail by wall clock."""
    db.insert_logs("s1", [_log(message="m", mins_ago=50),
                          _log(message="m", mins_ago=10)],
                   ingest_cfg=DEFAULT_CFG)
    conn = sqlite3.connect(db.db_path)
    total, first, last = conn.execute(
        "SELECT SUM(count), MIN(first_seen), MAX(last_seen) FROM log_signatures"
    ).fetchone()
    conn.close()
    assert total == 2, "both occurrences must be counted, in one bucket or two"
    assert first < last, "the window must bracket both"


def test_coalescing_can_be_switched_off(db):
    db.insert_logs("s1", [_log(message="x")] * 5,
                   ingest_cfg={"drop_information": True,
                               "coalesce_signatures": False})
    assert _count(db) == 5
    assert _count(db, "log_signatures") == 0


def test_a_long_message_does_not_break_the_key(db):
    db.insert_logs("s1", [_log(message="y" * 5000)], ingest_cfg=DEFAULT_CFG)
    assert _count(db, "log_signatures") == 1


# ── per-table retention ───────────────────────────────────────────────────

def _insert_raw(db, table, cols, values):
    conn = sqlite3.connect(db.db_path)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?'*len(values))})",
                 values)
    conn.commit()
    conn.close()


def test_logs_use_their_own_shorter_retention(db):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_raw(db, "logs", "server_name,timestamp,log_source,level,event_id,message",
                ("s1", old, "System", "Error", 1, "old"))
    _insert_raw(db, "metrics", "server_name,timestamp,cpu_percent,status",
                ("s1", old, 10.0, "healthy"))

    db.cleanup_old_data(30, per_table={"logs_days": 7, "metrics_days": 30})

    assert _count(db) == 0, "a 10-day-old log must go at 7-day retention"
    assert _count(db, "metrics") == 1, "metrics must survive at 30-day retention"


def test_without_per_table_everything_uses_the_single_value(db):
    """Backward compatibility — existing callers pass one number."""
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_raw(db, "logs", "server_name,timestamp,log_source,level,event_id,message",
                ("s1", old, "System", "Error", 1, "old"))
    db.cleanup_old_data(30)
    assert _count(db) == 0


def test_signature_retention_is_independent_of_raw_logs(db):
    """The point of the two tiers: raw rows expire quickly, the history in
    signatures outlives them."""
    recent = _ts(0)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_raw(db, "logs", "server_name,timestamp,log_source,level,event_id,message",
                ("s1", old, "System", "Error", 1, "old"))
    conn = sqlite3.connect(db.db_path)
    conn.execute("""INSERT INTO log_signatures
                    (server_name, log_source, level, event_id, msg_hash, hour_utc,
                     count, first_seen, last_seen, sample)
                    VALUES ('s1','System','Error',1,'h','2026-01-01T00',5,?,?,'x')""",
                 (old, old))
    conn.commit(); conn.close()

    db.cleanup_old_data(30, per_table={"logs_days": 7, "log_signatures_days": 90})

    assert _count(db) == 0, "raw log expired"
    assert _count(db, "log_signatures") == 1, "signature history survives"


def test_chunked_delete_removes_everything_past_the_chunk_size(db):
    """The loop must not stop after one batch — logs projects to tens of
    millions of rows and a single statement would stall the write lock."""
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db.db_path)
    conn.executemany(
        "INSERT INTO logs (server_name,timestamp,log_source,level,event_id,message)"
        " VALUES (?,?,?,?,?,?)",
        [("s1", old, "System", "Error", 1, f"m{i}") for i in range(250)])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db.db_path)
    try:
        deleted = db._chunked_delete(conn, "logs", "timestamp", 30, chunk=100)
    finally:
        conn.close()
    assert deleted == 250
    assert _count(db) == 0


# ── the counters are visible ──────────────────────────────────────────────

def test_drop_counters_accumulate_for_the_health_endpoint(db):
    db.insert_logs("s1", [_log(level="Information", event_id=1)] * 7,
                   ingest_cfg=DEFAULT_CFG)
    db.insert_logs("s1", [_log(level="Information", source="System",
                               event_id=7036)] * 2, ingest_cfg=DEFAULT_CFG)
    assert Database.logs_dropped_information == 7
    assert Database.logs_kept_by_allowlist == 2


def test_health_endpoint_reports_the_counters():
    from pathlib import Path
    src = Path("routes/api/health.py").read_text(encoding="utf-8")
    assert "logs_dropped_information" in src
    assert "logs_kept_by_allowlist" in src
