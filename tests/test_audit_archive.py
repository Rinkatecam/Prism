"""Tests for the audit-log JSONL archive helper."""

import json


def test_archive_writes_jsonl(tmp_db, tmp_path):
    tmp_db.log_audit("alice", "test_a", "test", "first")
    tmp_db.log_audit("bob", "test_b", "test", "second")
    out = tmp_path / "audit.jsonl"
    n = tmp_db.export_audit_log_jsonl(out)
    assert n == 2
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert {r["username"] for r in rows} == {"alice", "bob"}


def test_archive_audit_rows_remain_in_db(tmp_db, tmp_path):
    """Archiving must NOT delete from audit_log (append-only)."""
    tmp_db.log_audit("alice", "x", "test", "y")
    out = tmp_path / "audit.jsonl"
    tmp_db.export_audit_log_jsonl(out)
    # Source table still has the row
    conn = tmp_db._get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()
        assert row["c"] == 1
    finally:
        conn.close()
