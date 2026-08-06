"""Tests for the sop_log DB layer (compliance feature).

Pins the contract of:
  * ``Database.insert_sop_execution`` — writes sop_log + audit_log
  * ``Database.get_latest_sop_execution`` — most recent per sop_id
  * ``Database.get_sop_execution_history`` — paginated history
  * ``Database.get_all_latest_sop_executions`` — aggregate for dashboard
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def db(tmp_path):
    from database import Database
    return Database(tmp_path / "sop.db")


# ── schema ────────────────────────────────────────────────────────────

def test_sop_log_table_exists(db):
    conn = sqlite3.connect(db.db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sop_log)").fetchall()]
    conn.close()
    assert set(cols) >= {"id", "sop_id", "executed_at", "executed_by",
                         "result", "notes", "evidence_ref"}


def test_sop_log_has_index_on_sop_id_time(db):
    """Dashboard reads ``MAX(executed_at) WHERE sop_id=?`` — needs the index."""
    conn = sqlite3.connect(db.db_path)
    indices = [r[1] for r in conn.execute("PRAGMA index_list(sop_log)").fetchall()]
    conn.close()
    assert any("sop_log" in i for i in indices)


# ── insert ────────────────────────────────────────────────────────────

def test_insert_sop_execution_returns_new_row_id(db):
    row_id = db.insert_sop_execution(
        sop_id="SOP-05", executed_by="alice",
        result="pass", notes="Monthly review complete.",
    )
    assert isinstance(row_id, int) and row_id > 0


def test_insert_sop_execution_writes_audit_row(db):
    db.insert_sop_execution("SOP-05", "alice", result="pass", notes="ok")
    conn = sqlite3.connect(db.db_path)
    rows = conn.execute(
        "SELECT username, action, category, details FROM audit_log "
        "WHERE action = 'sop_execution_recorded'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "alice"
    assert rows[0][2] == "compliance"
    assert "SOP-05" in rows[0][3]


def test_insert_sop_execution_rejects_unknown_result(db):
    with pytest.raises(ValueError):
        db.insert_sop_execution("SOP-05", "alice", result="bogus")


def test_insert_sop_execution_truncates_long_notes(db):
    long_notes = "x" * 5000
    row_id = db.insert_sop_execution("SOP-05", "alice", notes=long_notes)
    conn = sqlite3.connect(db.db_path)
    notes = conn.execute(
        "SELECT notes FROM sop_log WHERE id = ?", (row_id,)
    ).fetchone()[0]
    conn.close()
    # 2000 chars max; truncation marker added.
    assert len(notes) <= 2000
    assert notes.endswith("...")


# ── read paths ────────────────────────────────────────────────────────

def test_get_latest_sop_execution_returns_most_recent(db):
    import time
    db.insert_sop_execution("SOP-05", "alice", notes="first")
    time.sleep(1.05)  # SQLite timestamp resolution is 1 s
    db.insert_sop_execution("SOP-05", "bob", notes="second")
    latest = db.get_latest_sop_execution("SOP-05")
    assert latest is not None
    assert latest["executed_by"] == "bob"
    assert latest["notes"] == "second"


def test_get_latest_sop_execution_returns_none_when_never_run(db):
    assert db.get_latest_sop_execution("SOP-99") is None


def test_get_sop_execution_history_returns_newest_first(db):
    import time
    db.insert_sop_execution("SOP-05", "alice", notes="run1")
    time.sleep(1.05)
    db.insert_sop_execution("SOP-05", "alice", notes="run2")
    time.sleep(1.05)
    db.insert_sop_execution("SOP-05", "alice", notes="run3")
    history = db.get_sop_execution_history("SOP-05")
    assert len(history) == 3
    assert history[0]["notes"] == "run3"
    assert history[2]["notes"] == "run1"


def test_get_sop_execution_history_respects_limit(db):
    for i in range(10):
        db.insert_sop_execution("SOP-05", "alice", notes=f"run{i}")
    assert len(db.get_sop_execution_history("SOP-05", limit=3)) == 3


def test_get_all_latest_sop_executions_one_row_per_sop(db):
    import time
    db.insert_sop_execution("SOP-03", "alice", notes="acl-review")
    db.insert_sop_execution("SOP-05", "bob", notes="baseline")
    time.sleep(1.05)
    db.insert_sop_execution("SOP-03", "alice", notes="acl-review-2")
    latest = db.get_all_latest_sop_executions()
    assert set(latest.keys()) == {"SOP-03", "SOP-05"}
    # SOP-03's latest is the SECOND insert.
    assert latest["SOP-03"]["notes"] == "acl-review-2"


def test_get_all_latest_sop_executions_empty_when_no_rows(db):
    assert db.get_all_latest_sop_executions() == {}


# ─── F-PHD-2: append-only enforcement ────────────────────────────────

def test_sop_log_update_is_blocked_by_trigger(db):
    """F-PHD-2: sop_log rows are regulated evidence. The UPDATE trigger
    must abort any in-process attempt to mutate notes / result / etc.
    Operators who need to amend a record must add a new row with
    result='partial' and a 'supersedes' note (per SOP-05)."""
    rid = db.insert_sop_execution("SOP-05", "alice", notes="original")
    conn = sqlite3.connect(db.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE sop_log SET notes = ? WHERE id = ?",
                ("tampered", rid),
            )
            conn.commit()
    finally:
        conn.close()
    # Original value survives.
    latest = db.get_latest_sop_execution("SOP-05")
    assert latest["notes"] == "original"


def test_sop_log_delete_is_blocked_by_trigger(db):
    """Same shape for DELETE — once recorded, the row stays."""
    db.insert_sop_execution("SOP-05", "alice", notes="recorded")
    conn = sqlite3.connect(db.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM sop_log WHERE sop_id = 'SOP-05'")
            conn.commit()
    finally:
        conn.close()
    # Row still present.
    assert db.get_latest_sop_execution("SOP-05") is not None


def test_sop_log_triggers_present_on_fresh_db(tmp_path):
    """The triggers must exist on every fresh install (not only after
    a migration). Verifies SCHEMA_SQL carries them."""
    from database import Database
    fresh = Database(tmp_path / "fresh.db")
    conn = sqlite3.connect(fresh.db_path)
    trigs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='sop_log'"
    ).fetchall()]
    conn.close()
    assert "sop_log_no_update" in trigs
    assert "sop_log_no_delete" in trigs
