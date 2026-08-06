"""Tests for the workflow card 'last_execution_status' field.

Before 2026-05-22 the SQL alias was ``last_exec_status`` but the
frontend (templates/workflows.html) read ``last_execution_status``. The
mismatch silently coerced to ``undefined`` in JS and the workflow card
always showed "Never run" — even for workflows that had executed
hundreds of times. These tests pin the alias name + the JOIN behaviour.
"""

from __future__ import annotations

import sqlite3
import time

import pytest


def _make_db(tmp_path):
    from database import Database
    return Database(tmp_path / "wf_status.db")


def _make_workflow(db, name="wf"):
    """Insert a minimal workflow row and return its id."""
    return db.create_workflow(
        name=name,
        description=None,
        category_id=None,
        trigger_type="manual",
        trigger_config="{}",
        canvas_json="{}",
    )


def _make_execution(db, workflow_id, status):
    """Insert a workflow execution row directly so we control the status
    independently of the engine's real run logic."""
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(
            """INSERT INTO workflow_executions (workflow_id, status,
                                                 trigger_source, executed_by)
               VALUES (?, ?, 'manual', 'test')""",
            (workflow_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_workflows_includes_last_execution_status(tmp_path):
    """The frontend reads ``wf.last_execution_status`` so the SQL alias
    MUST be exactly that — not ``last_exec_status`` (the pre-fix name)."""
    db = _make_db(tmp_path)
    wf_id = _make_workflow(db, "wf-status-test")
    _make_execution(db, wf_id, "completed")

    rows = db.get_workflows()
    row = next(r for r in rows if r["id"] == wf_id)
    assert "last_execution_status" in row, (
        "Workflow row must expose last_execution_status — the workflow "
        "card's status badge reads this exact key. The previous alias "
        "last_exec_status silently shipped as undefined in JS."
    )
    assert row["last_execution_status"] == "completed"


def test_get_workflows_returns_none_for_workflow_with_no_executions(tmp_path):
    """A brand-new workflow has no executions — the field should be None
    (not raise, not 'never run' literal, not 'unknown')."""
    db = _make_db(tmp_path)
    wf_id = _make_workflow(db, "wf-fresh")
    rows = db.get_workflows()
    row = next(r for r in rows if r["id"] == wf_id)
    assert row["last_execution_status"] is None


def test_get_workflows_picks_latest_execution_status(tmp_path):
    """If a workflow has run multiple times the subquery uses LIMIT 1
    on started_at DESC — must return the most recent status, not the
    first. Critical otherwise: a failed-then-succeeded workflow would
    still show 'failed' forever."""
    db = _make_db(tmp_path)
    wf_id = _make_workflow(db, "wf-multi")
    _make_execution(db, wf_id, "failed")
    # Make sure the second execution has a strictly later timestamp.
    # SQLite's default strftime resolution is seconds; sleep guarantees
    # the ORDER BY started_at DESC picks the second row.
    time.sleep(1.05)
    _make_execution(db, wf_id, "completed")

    rows = db.get_workflows()
    row = next(r for r in rows if r["id"] == wf_id)
    assert row["last_execution_status"] == "completed"


def test_get_workflows_also_exposes_last_execution_at(tmp_path):
    """The timestamp of the last run is useful in the workflow card too
    (e.g. "ran 3 min ago"). Pin the alias so a future refactor doesn't
    silently drop it."""
    db = _make_db(tmp_path)
    wf_id = _make_workflow(db, "wf-timestamps")
    _make_execution(db, wf_id, "completed")
    rows = db.get_workflows()
    row = next(r for r in rows if r["id"] == wf_id)
    assert "last_execution_at" in row
