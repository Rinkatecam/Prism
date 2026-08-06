"""Tests for the Sprint-2 auth-hardening batch (S2-1, S2-12, S2-13, S2-15).

These tests use the existing `tmp_db` fixture from conftest.py — a fresh
file-backed SQLite database in a tmp dir. No Flask test client is spun up;
we exercise the Database methods and the policy / probe helpers directly.
"""

from __future__ import annotations

import pytest


# ── S2-1 (BL3): revoked_sessions + disabled_users ─────────────────────────
class TestRevokedSessions:
    def test_revoke_then_is_revoked(self, tmp_db):
        assert tmp_db.is_session_revoked("alice", "2026-05-06T10:00:00Z") is False
        tmp_db.revoke_session("alice", "2026-05-06T10:00:00Z", by="admin")
        assert tmp_db.is_session_revoked("alice", "2026-05-06T10:00:00Z") is True

    def test_negative_lookup_with_different_login_time(self, tmp_db):
        tmp_db.revoke_session("alice", "2026-05-06T10:00:00Z", by="admin")
        # Same user, different session — should not be marked revoked
        assert tmp_db.is_session_revoked("alice", "2026-05-06T11:00:00Z") is False

    def test_multiple_sessions_for_same_user(self, tmp_db):
        tmp_db.revoke_session("alice", "2026-05-06T10:00:00Z", by="admin")
        tmp_db.revoke_session("alice", "2026-05-06T11:00:00Z", by="admin")
        rows = tmp_db.list_revoked_sessions()
        users = [r["username"] for r in rows]
        assert users.count("alice") == 2

    def test_revoke_is_idempotent(self, tmp_db):
        tmp_db.revoke_session("alice", "T1", by="admin")
        tmp_db.revoke_session("alice", "T1", by="admin")  # second call
        rows = [r for r in tmp_db.list_revoked_sessions() if r["username"] == "alice"]
        assert len(rows) == 1

    def test_empty_args_return_false(self, tmp_db):
        assert tmp_db.is_session_revoked("", "") is False
        assert tmp_db.is_session_revoked("alice", "") is False


class TestDisabledUsers:
    def test_disable_then_is_disabled(self, tmp_db):
        assert tmp_db.is_user_disabled("bob") is False
        tmp_db.disable_user("bob", by="admin", reason="left company")
        assert tmp_db.is_user_disabled("bob") is True

    def test_disable_is_case_insensitive(self, tmp_db):
        tmp_db.disable_user("Bob", by="admin")
        assert tmp_db.is_user_disabled("bob") is True
        assert tmp_db.is_user_disabled("BOB") is True

    def test_enable_removes_disable(self, tmp_db):
        tmp_db.disable_user("bob", by="admin")
        n = tmp_db.enable_user("bob")
        assert n == 1
        assert tmp_db.is_user_disabled("bob") is False

    def test_enable_unknown_returns_zero(self, tmp_db):
        assert tmp_db.enable_user("nobody") == 0


# ── S2-12 (W3): account lockout primitives ────────────────────────────────
class TestAccountLockout:
    def test_record_and_count(self, tmp_db):
        for _ in range(3):
            tmp_db.record_auth_failure("alice", ip="10.0.0.1")
        assert tmp_db.count_recent_failures("alice") == 3

    def test_lockout_threshold_reached(self, tmp_db):
        for _ in range(10):
            tmp_db.record_auth_failure("alice")
        assert tmp_db.count_recent_failures("alice") >= 10

    def test_clear_failures_resets(self, tmp_db):
        for _ in range(5):
            tmp_db.record_auth_failure("alice")
        n = tmp_db.clear_failures_for("alice")
        assert n == 5
        assert tmp_db.count_recent_failures("alice") == 0

    def test_count_isolates_users(self, tmp_db):
        tmp_db.record_auth_failure("alice")
        tmp_db.record_auth_failure("alice")
        tmp_db.record_auth_failure("bob")
        assert tmp_db.count_recent_failures("alice") == 2
        assert tmp_db.count_recent_failures("bob") == 1

    def test_count_case_insensitive(self, tmp_db):
        tmp_db.record_auth_failure("Alice")
        assert tmp_db.count_recent_failures("alice") == 1

    def test_cleanup_auth_failures_prunes_old(self, tmp_db):
        # Insert a row, then back-date it via raw SQL so cleanup catches it.
        tmp_db.record_auth_failure("alice")
        with tmp_db._write_lock:
            conn = tmp_db._get_conn()
            try:
                conn.execute(
                    "UPDATE auth_failures SET attempted_at = datetime('now', '-2 days') "
                    "WHERE username = 'alice'"
                )
                conn.commit()
            finally:
                conn.close()
        n = tmp_db.cleanup_auth_failures(hours=24)
        assert n == 1
        assert tmp_db.count_recent_failures("alice") == 0


# ── S2-13 (W4): backup-admin password policy ──────────────────────────────
class TestBackupAdminPasswordPolicy:
    def test_short_rejected(self):
        from auth import validate_backup_admin_password
        ok, err = validate_backup_admin_password("Sh0rt!")
        assert ok is False
        assert "12 characters" in err

    def test_no_digit_rejected(self):
        from auth import validate_backup_admin_password
        ok, err = validate_backup_admin_password("NoDigitsHere!@#")
        assert ok is False
        assert "digit" in err

    def test_no_symbol_rejected(self):
        from auth import validate_backup_admin_password
        ok, err = validate_backup_admin_password("AllAlpha1234567")
        assert ok is False
        assert "symbol" in err

    def test_common_password_rejected_when_policy_otherwise_passes(self):
        """Take an entry from the embedded corpus that already satisfies the
        length+digit+symbol gates and confirm it is still rejected by the
        common-password lookup."""
        from auth import validate_backup_admin_password, _COMMON_PASSWORDS
        candidate = next(
            (p for p in _COMMON_PASSWORDS
             if len(p) >= 12
             and any(c.isdigit() for c in p)
             and any(not c.isalnum() for c in p)),
            None,
        )
        assert candidate is not None, "embedded corpus missing a passing-but-common entry"
        ok, err = validate_backup_admin_password(candidate)
        assert ok is False
        assert "common" in err.lower() or "breached" in err.lower()

    def test_valid_password_accepted(self):
        from auth import validate_backup_admin_password
        ok, err = validate_backup_admin_password("CorrectHorseBattery42!")
        assert ok is True
        assert err is None

    def test_reuse_prevention(self):
        from auth import validate_backup_admin_password
        from werkzeug.security import generate_password_hash
        prev = generate_password_hash("CorrectHorseBattery42!", method="scrypt")
        ok, err = validate_backup_admin_password(
            "CorrectHorseBattery42!", previous_hash=prev,
        )
        assert ok is False
        assert "different" in err or "previous" in err

    def test_reuse_with_different_password_ok(self):
        from auth import validate_backup_admin_password
        from werkzeug.security import generate_password_hash
        prev = generate_password_hash("CorrectHorseBattery42!", method="scrypt")
        ok, err = validate_backup_admin_password(
            "DifferentChoice99#Foo", previous_hash=prev,
        )
        assert ok is True


# ── S2-15 (W7): LDAP probe — function exists and survives missing config ──
class TestLdapHealthProbe:
    def test_probe_callable_with_disabled_auth(self, fresh_config):
        from auth import ldap_health_probe
        snap = ldap_health_probe(fresh_config)
        assert isinstance(snap, dict)
        assert "ok" in snap
        assert "last_check" in snap

    def test_get_ldap_health_returns_dict(self):
        from auth import get_ldap_health
        snap = get_ldap_health()
        assert isinstance(snap, dict)

    def test_assert_ldap_startup_safe_with_no_auth_is_noop(self, fresh_config):
        from auth import assert_ldap_startup_safe
        # Auth disabled → no exception, no SystemExit
        assert_ldap_startup_safe(fresh_config)

    def test_assert_ldap_startup_safe_refuses_when_unrecoverable(self, tmp_path):
        """auth.enabled=true AND no ldap_url AND no backup admin → SystemExit."""
        import json
        from config_manager import ConfigManager
        from auth import assert_ldap_startup_safe
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "servers": [],
            "settings": {"auth": {"enabled": True, "ldap_url": ""}},
        }), encoding="utf-8")
        cfg = ConfigManager(cfg_file)
        with pytest.raises(SystemExit):
            assert_ldap_startup_safe(cfg)
