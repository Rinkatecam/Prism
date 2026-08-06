"""Feature 1.8 — T4: single-row backup_state table + set/get helpers.

Persists the outcome of the scheduled DB backup periodic so the health endpoint
can report "age of last successful backup" and the periodic can dedup its
stale-backup alert. Exactly one row (id=1), upserted.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from database import Database


@pytest.fixture()
def db():
    return Database(Path(tempfile.mkdtemp()) / "test.db")


def test_fresh_db_has_no_backup_state(db):
    assert db.get_backup_state() is None


def test_set_and_get_backup_state(db):
    db.set_backup_state(
        last_success_ts="2026-07-01T10:00:00Z", last_ok=1,
        last_path="data/backups/20260701T100000Z",
    )
    row = db.get_backup_state()
    assert row is not None
    assert row["last_success_ts"] == "2026-07-01T10:00:00Z"
    assert row["last_ok"] == 1
    assert row["last_path"] == "data/backups/20260701T100000Z"


def test_set_backup_state_upserts_single_row(db):
    db.set_backup_state(last_success_ts="2026-07-01T10:00:00Z", last_ok=1)
    db.set_backup_state(last_success_ts="2026-07-01T11:00:00Z", last_ok=1)
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM backup_state").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert db.get_backup_state()["last_success_ts"] == "2026-07-01T11:00:00Z"


def test_set_backup_state_coalesce_preserves_unset_fields(db):
    db.set_backup_state(last_success_ts="2026-07-01T10:00:00Z", last_ok=1,
                        last_path="p1")
    # A later failure records last_ok=0 + error WITHOUT nuking last_success_ts.
    db.set_backup_state(last_ok=0, last_error="disk full")
    row = db.get_backup_state()
    assert row["last_ok"] == 0
    assert row["last_error"] == "disk full"
    assert row["last_success_ts"] == "2026-07-01T10:00:00Z"  # preserved
    assert row["last_path"] == "p1"  # preserved
