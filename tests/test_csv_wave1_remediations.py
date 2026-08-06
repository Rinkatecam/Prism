"""Tests for Wave 1 CSV remediations (F-AT-1, F-S-1, F-A-1, F-BR-1, F-D-1, F-D-2).

Each finding gets focused tests that pin the new behaviour so a future
refactor can't silently roll it back.

Findings closed:
  * F-AT-1 — periodic audit-chain verifier registered + state slot
  * F-S-1  — MAX_CONTENT_LENGTH set on the Flask app
  * F-A-1  — restart_log.actor column persists operator attribution
  * F-BR-1 — backup tool includes install_state.json
  * F-D-1  — log_audit insert failures surface via counter
  * F-D-2  — log_audit docstring documents the details-length convention
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── F-AT-1: audit-chain verifier registered as a periodic job ───────

def test_audit_chain_verifier_registered_with_hourly_cadence():
    from collector_v2 import periodics
    jobs = periodics._build_jobs(
        get_servers=lambda: [],
        get_settings=lambda: {},
        db=MagicMock(),
    )
    names = {j.name: j for j in jobs}
    assert "audit_chain_verifier" in names, (
        "F-AT-1: scheduled audit_chain_verifier job must be registered"
    )
    assert names["audit_chain_verifier"].interval_s == 3600


def test_audit_chain_verifier_writes_state_on_ok_result():
    """On a clean chain, the verifier updates state.last_audit_chain_check."""
    from collector_v2 import periodics, state
    state.last_audit_chain_check = None  # reset before test
    mock_db = MagicMock()
    mock_db.verify_audit_chain.return_value = {
        "ok": True, "checked": 1234,
        "first_break_id": None, "first_break_reason": None,
    }
    jobs = periodics._build_jobs(
        get_servers=lambda: [],
        get_settings=lambda: {},
        db=mock_db,
    )
    verifier = next(j for j in jobs if j.name == "audit_chain_verifier")
    verifier.handler()
    snap = state.last_audit_chain_check
    assert snap is not None
    assert snap["ok"] is True
    assert snap["checked"] == 1234
    # Should NOT have written a tamper-detected audit row.
    mock_db.log_audit.assert_not_called()


def test_audit_chain_verifier_logs_tamper_finding_on_break():
    """When verify_audit_chain returns ok=False, the verifier writes a
    Critical audit_log row + sets state."""
    from collector_v2 import periodics, state
    state.last_audit_chain_check = None
    mock_db = MagicMock()
    mock_db.verify_audit_chain.return_value = {
        "ok": False, "checked": 99,
        "first_break_id": 42, "first_break_reason": "hash mismatch",
    }
    jobs = periodics._build_jobs(
        get_servers=lambda: [],
        get_settings=lambda: {},
        db=mock_db,
    )
    verifier = next(j for j in jobs if j.name == "audit_chain_verifier")
    verifier.handler()
    snap = state.last_audit_chain_check
    assert snap["ok"] is False
    assert snap["first_break_id"] == 42
    # Critical audit row recorded.
    mock_db.log_audit.assert_called_once()
    call = mock_db.log_audit.call_args
    assert call.kwargs.get("action") == "audit_chain_tamper_detected"


# ─── F-S-1: MAX_CONTENT_LENGTH set ───────────────────────────────────

def test_app_has_max_content_length_set():
    """Flask app must have an explicit body size cap so an attacker
    can't exhaust memory by POSTing an arbitrarily large body."""
    import app as flask_app_module
    mcl = flask_app_module.app.config.get("MAX_CONTENT_LENGTH")
    assert mcl is not None and mcl > 0, "F-S-1: MAX_CONTENT_LENGTH must be set"
    # Sanity: ≤ 16 MB. Internal monitoring app; nothing legitimate exceeds this.
    assert mcl <= 16 * 1024 * 1024, (
        f"F-S-1: MAX_CONTENT_LENGTH ({mcl}) exceeds the 16 MB sanity ceiling"
    )


# ─── F-A-1: restart_log.actor column ─────────────────────────────────

def test_restart_log_has_actor_column(tmp_path):
    """F-A-1: schema must include the ``actor`` column."""
    from database import Database
    db = Database(tmp_path / "actor.db")
    conn = sqlite3.connect(db.db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(restart_log)").fetchall()]
    conn.close()
    assert "actor" in cols, f"F-A-1: restart_log must have 'actor' column; got {cols}"


def test_restart_log_actor_defaults_to_system(tmp_path):
    """Legacy callers that don't pass actor produce 'system' rows."""
    from database import Database
    db = Database(tmp_path / "actor2.db")
    db.insert_restart_log(
        run_id="run-1", server_name="srv1",
        action="restart", status="success", details="",
        updates_installed=0,
        # actor omitted — should default to 'system'
    )
    conn = sqlite3.connect(db.db_path)
    row = conn.execute("SELECT actor FROM restart_log WHERE run_id='run-1'").fetchone()
    conn.close()
    assert row[0] == "system"


def test_restart_log_actor_persists_caller_provided_value(tmp_path):
    """Explicit actor flows through."""
    from database import Database
    db = Database(tmp_path / "actor3.db")
    db.insert_restart_log(
        run_id="run-2", server_name="srv1",
        action="restart", status="success",
        actor="alice",
    )
    conn = sqlite3.connect(db.db_path)
    row = conn.execute("SELECT actor FROM restart_log WHERE run_id='run-2'").fetchone()
    conn.close()
    assert row[0] == "alice"


# ─── F-BR-1: backup includes install_state.json ──────────────────────

def test_backup_includes_install_state_when_present(tmp_path, monkeypatch):
    """F-BR-1: install_state.json is operational state; must be backed up."""
    from tools import backup as backup_mod
    # Set up a minimal data dir.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Seed prism.db (real sqlite file).
    db_file = data_dir / "prism.db"
    sqlite3.connect(str(db_file)).close()
    # Seed config.json.
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    # Seed install_state.json — the asset under test.
    install_state_payload = {"srv1": {"status": "restart_required"}}
    (data_dir / "install_state.json").write_text(
        json.dumps(install_state_payload), encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir)

    # Verify install_state.json landed in the backup AND is in the manifest.
    assert (out_dir / "install_state.json").exists()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    install_role = [f for f in files if f.get("role") == "install_state"]
    assert install_role, (
        "F-BR-1: install_state.json must appear in backup manifest"
    )
    assert install_role[0]["name"] == "install_state.json"


def test_backup_omits_install_state_when_absent(tmp_path):
    """When install_state.json doesn't exist (fresh install), the backup
    succeeds without crashing — it just doesn't include the file."""
    from tools import backup as backup_mod
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sqlite3.connect(str(data_dir / "prism.db")).close()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir)
    assert not (out_dir / "install_state.json").exists()
    # Backup still succeeded — manifest exists and is valid.
    assert (out_dir / "manifest.json").exists()


# ─── F-D-1: log_audit failure surfaces via counter ───────────────────

class _BrokenConnProxy:
    """Wrap a real sqlite3.Connection but make INSERTs into audit_log
    blow up. sqlite3.Connection's attributes are read-only so we can't
    monkeypatch ``execute`` directly — wrapping is the clean way."""
    def __init__(self, real):
        self._real = real
    def execute(self, stmt, *a, **kw):
        if "INSERT INTO audit_log" in stmt:
            raise sqlite3.OperationalError("simulated DB error")
        return self._real.execute(stmt, *a, **kw)
    def commit(self): return self._real.commit()
    def close(self): return self._real.close()
    def __getattr__(self, name):
        return getattr(self._real, name)


def test_audit_insert_failure_increments_counter(tmp_path, monkeypatch):
    """F-D-1: a failing log_audit insert must bump the public counter
    so the watchdog can detect 'audit blind' state."""
    from database import Database
    db = Database(tmp_path / "f.db")
    assert db._audit_insert_failures == 0

    real_get_conn = db._get_conn
    def _broken():
        return _BrokenConnProxy(real_get_conn())
    monkeypatch.setattr(db, "_get_conn", _broken)

    db.log_audit(username="alice", action="x", category="test")
    assert db._audit_insert_failures == 1, (
        "F-D-1: insert failure must increment _audit_insert_failures"
    )


def test_audit_insert_failure_counter_starts_at_zero(tmp_path):
    """Fresh DB: counter starts at zero (no failures observed)."""
    from database import Database
    db = Database(tmp_path / "z.db")
    assert db._audit_insert_failures == 0
    assert db._audit_mirror_failures == 0


# ─── F-D-2: log_audit docstring documents details-length convention ──

def test_log_audit_docstring_mentions_details_convention():
    """F-D-2: the caller convention for ``details`` should be documented
    so callers know to put salient info first."""
    from database import Database
    doc = Database.log_audit.__doc__ or ""
    # Loose match — just verify the convention is mentioned.
    assert "F-D-2" in doc or "details" in doc.lower(), (
        "F-D-2: log_audit docstring should describe the details-field convention"
    )
    assert "500" in doc or "salient" in doc.lower(), (
        "F-D-2: docstring should hint at the 500-char operational convention OR "
        "the 'put salient info first' guidance"
    )
