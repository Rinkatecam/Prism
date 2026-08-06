"""Tests for the per-server RBAC layer (database.py helpers)."""

import pytest


def test_acl_starts_empty(tmp_db):
    assert tmp_db.acl_is_empty() is True
    assert tmp_db.list_acl() == []


def test_grant_and_lookup(tmp_db):
    rec = tmp_db.grant_acl("alice", "WEB01", "control", granted_by="admin")
    assert rec > 0
    assert tmp_db.acl_is_empty() is False
    assert tmp_db.get_user_permission("alice", "WEB01") == "control"
    assert tmp_db.get_user_permission("ALICE@CORP.LOCAL", "WEB01") == "control"  # normalised
    assert tmp_db.get_user_permission("bob", "WEB01") is None


def test_wildcard_acl_applies_to_any_server(tmp_db):
    tmp_db.grant_acl("ops", "*", "admin", granted_by="admin")
    assert tmp_db.get_user_permission("ops", "WEB01") == "admin"
    assert tmp_db.get_user_permission("ops", "DC01") == "admin"


def test_specific_acl_wins_when_higher(tmp_db):
    tmp_db.grant_acl("ops", "*", "view")
    tmp_db.grant_acl("ops", "WEB01", "admin")
    assert tmp_db.get_user_permission("ops", "WEB01") == "admin"
    assert tmp_db.get_user_permission("ops", "OTHER") == "view"


def test_revoke(tmp_db):
    tmp_db.grant_acl("alice", "WEB01", "control")
    n = tmp_db.revoke_acl("alice", "WEB01")
    assert n == 1
    assert tmp_db.get_user_permission("alice", "WEB01") is None


def test_grant_invalid_permission_rejected(tmp_db):
    with pytest.raises(ValueError):
        tmp_db.grant_acl("alice", "WEB01", "root")


def test_audit_log_is_append_only(tmp_db):
    """The triggers in SCHEMA_SQL must reject UPDATE/DELETE on audit_log."""
    import sqlite3
    tmp_db.log_audit("alice", "test_action", "test", "details here")
    conn = tmp_db._get_conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log WHERE 1=1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET details='tampered' WHERE 1=1")
    finally:
        conn.close()


# ---- approval flow ----

def test_approval_request_and_decide(tmp_db):
    aid = tmp_db.create_approval_request("alice", "DC01", "restart", '{}')
    assert aid > 0
    pending = tmp_db.list_pending_approvals()
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    # Self-approval blocked
    assert tmp_db.decide_approval(aid, "alice", True) is False
    # Different admin approves
    assert tmp_db.decide_approval(aid, "bob", True) is True
    # Single-use consume
    consumed = tmp_db.consume_approval(aid)
    assert consumed is not None
    assert tmp_db.consume_approval(aid) is None
