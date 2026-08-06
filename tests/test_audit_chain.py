"""Tests for the audit-log hash chain + JSONL mirror (S1-7 from AUDIT-2026-05)."""

import json
import sqlite3
import pytest


def test_chain_starts_clean(tmp_db):
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 0


def test_single_row_chain(tmp_db):
    tmp_db.log_audit("alice", "test", "test", "first event")
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 1


def test_multi_row_chain_intact(tmp_db):
    tmp_db.log_audit("alice", "a", "test", "1")
    tmp_db.log_audit("bob", "b", "test", "2")
    tmp_db.log_audit("carol", "c", "test", "3")
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 3


def test_chain_detects_content_tampering(tmp_db):
    """Drop the triggers (simulating an attacker with file write), tamper with
    a row's details, restore triggers — chain verification must catch it."""
    tmp_db.log_audit("alice", "a", "test", "original")
    tmp_db.log_audit("bob", "b", "test", "second")

    conn = tmp_db._get_conn()
    try:
        # Simulate out-of-band tampering: drop triggers, mutate, recreate.
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
        conn.execute("UPDATE audit_log SET details = 'TAMPERED' WHERE username = 'alice'")
        conn.commit()
    finally:
        conn.close()

    res = tmp_db.verify_audit_chain()
    assert res["ok"] is False
    assert res["first_break_reason"].startswith("row_hash mismatch")


def test_chain_detects_row_deletion(tmp_db):
    """An attacker who deletes a middle row breaks the prev_hash chain
    even if every remaining row still matches its own row_hash."""
    tmp_db.log_audit("alice", "a", "test", "first")
    tmp_db.log_audit("bob", "b", "test", "MIDDLE — to delete")
    tmp_db.log_audit("carol", "c", "test", "third")

    conn = tmp_db._get_conn()
    try:
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
        conn.execute("DELETE FROM audit_log WHERE username = 'bob'")
        conn.commit()
    finally:
        conn.close()

    res = tmp_db.verify_audit_chain()
    assert res["ok"] is False
    assert "prev_hash" in res["first_break_reason"]


def test_jsonl_mirror_written(tmp_db, tmp_path, monkeypatch):
    """log_audit() must append a parallel JSONL line to AUDIT_MIRROR_PATH."""
    mirror = tmp_path / "audit_mirror.jsonl"
    monkeypatch.setattr(tmp_db, "AUDIT_MIRROR_PATH", mirror)

    tmp_db.log_audit("alice", "test_action", "test", "hello world")
    tmp_db.log_audit("bob", "another", "test", "second")

    assert mirror.exists()
    lines = mirror.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["username"] == "alice"
    assert rows[0]["action"] == "test_action"
    assert "row_hash" in rows[0]
    # Mirror's chain must match: row 2's prev_hash equals row 1's row_hash
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]


def test_log_audit_accepts_explicit_context(tmp_db):
    """Callers outside Flask context can pass ip/session/request_id explicitly."""
    tmp_db.log_audit("alice", "test", "test", "details",
                     source_ip="10.0.0.5", session_id="sess123", request_id="req456")
    conn = tmp_db._get_conn()
    try:
        row = conn.execute(
            "SELECT source_ip, session_id, request_id FROM audit_log WHERE username='alice'"
        ).fetchone()
    finally:
        conn.close()
    assert row["source_ip"] == "10.0.0.5"
    assert row["session_id"] == "sess123"
    assert row["request_id"] == "req456"
