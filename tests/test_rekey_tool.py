"""Tests for tools/rekey.py and the plain-text migration gate.

These tests run on Linux/macOS too — we drive crypto_utils in its
plain-text branch (via PRISM_DPAPI=0) so the tool's logic is exercised
without requiring pywin32 or DPAPI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import crypto_utils
from tools import rekey as rekey_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_key_and_config(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Fernet]:
    """Stand up a tmp data/ + config.json with a known plain-text key.

    Returns (config_path, data_dir, old_fernet).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Force plain-text key path by disabling DPAPI for the duration of the test.
    monkeypatch.setenv(crypto_utils._DPAPI_DISABLED_ENV, "0")
    # Repoint module-level paths into our tmp data dir.
    key_path = data_dir / "prism.key"
    key_path_dpapi = data_dir / "prism.key.dpapi"
    monkeypatch.setattr(crypto_utils, "KEY_PATH", key_path)
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", key_path_dpapi)

    # Generate the OLD fernet and write it as plain-text.
    old_key = Fernet.generate_key()
    key_path.write_bytes(old_key)
    old_fernet = Fernet(old_key)

    # Build a config.json with three encrypted fields.
    cfg = {
        "servers": [
            {"name": "S1", "host": "h1", "username": "u",
             "password": crypto_utils.ENCRYPTED_PREFIX
                         + old_fernet.encrypt(b"secret-1").decode("ascii")},
            {"name": "S2", "host": "h2", "username": "u",
             "password": crypto_utils.ENCRYPTED_PREFIX
                         + old_fernet.encrypt(b"secret-2").decode("ascii")},
            # One server with empty password is left alone (skipped).
            {"name": "S3", "host": "h3", "username": "u", "password": ""},
        ],
        "settings": {
            "email": {
                "password": crypto_utils.ENCRYPTED_PREFIX
                            + old_fernet.encrypt(b"smtp-pass").decode("ascii"),
            },
            "auth": {
                "ldap_bind_password": crypto_utils.ENCRYPTED_PREFIX
                                      + old_fernet.encrypt(b"ldap-pass").decode("ascii"),
            },
        },
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_path, data_dir, old_fernet


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_rekey_round_trip(tmp_path, monkeypatch):
    cfg_path, data_dir, old_fernet = _seed_key_and_config(tmp_path, monkeypatch)

    # Decrypt baseline -- proves the seed worked.
    old_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    old_servers = {s["name"]: s["password"] for s in old_cfg["servers"]}

    summary = rekey_mod.run(
        config_path=cfg_path,
        data_dir=data_dir,
        dry_run=False,
        db=None,
    )

    assert summary["dry_run"] is False
    assert summary["failed"] == 0
    # 2 server passwords + 1 smtp + 1 ldap = 4 rekeyed; 1 empty server skipped.
    assert summary["rekeyed"] == 4
    assert summary["skipped"] == 1
    assert summary["backup"]  # old key archived
    assert Path(summary["backup"]).exists()

    # New key file is on disk.
    assert (data_dir / "prism.key").exists()
    new_key = (data_dir / "prism.key").read_bytes().strip()
    assert new_key != old_fernet._signing_key + old_fernet._encryption_key  # paranoia

    new_fernet = Fernet(new_key)

    # New config decrypts cleanly under the new key.
    new_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    new_servers = {s["name"]: s["password"] for s in new_cfg["servers"]}

    s1 = new_servers["S1"][len(crypto_utils.ENCRYPTED_PREFIX):]
    assert new_fernet.decrypt(s1.encode("ascii")) == b"secret-1"
    s2 = new_servers["S2"][len(crypto_utils.ENCRYPTED_PREFIX):]
    assert new_fernet.decrypt(s2.encode("ascii")) == b"secret-2"
    assert new_servers["S3"] == ""  # untouched

    smtp_token = new_cfg["settings"]["email"]["password"][len(crypto_utils.ENCRYPTED_PREFIX):]
    assert new_fernet.decrypt(smtp_token.encode("ascii")) == b"smtp-pass"
    ldap_token = new_cfg["settings"]["auth"]["ldap_bind_password"][len(crypto_utils.ENCRYPTED_PREFIX):]
    assert new_fernet.decrypt(ldap_token.encode("ascii")) == b"ldap-pass"

    # The OLD key should no longer decrypt the new ciphertexts.
    with pytest.raises(Exception):
        old_fernet.decrypt(s1.encode("ascii"))

    # Ciphertext changed.
    assert new_servers["S1"] != old_servers["S1"]


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    cfg_path, data_dir, old_fernet = _seed_key_and_config(tmp_path, monkeypatch)

    cfg_before = cfg_path.read_text(encoding="utf-8")
    key_before = (data_dir / "prism.key").read_bytes()

    summary = rekey_mod.run(
        config_path=cfg_path,
        data_dir=data_dir,
        dry_run=True,
        db=None,
    )

    assert summary["dry_run"] is True
    assert summary["rekeyed"] == 4
    assert summary["failed"] == 0
    assert summary["backup"] == ""  # nothing archived

    # File contents unchanged.
    assert cfg_path.read_text(encoding="utf-8") == cfg_before
    assert (data_dir / "prism.key").read_bytes() == key_before
    # No .bak files created.
    assert not list(data_dir.glob("*.bak"))


def test_rekey_with_database_writes_audit_row(tmp_path, monkeypatch):
    cfg_path, data_dir, _ = _seed_key_and_config(tmp_path, monkeypatch)

    import sqlite3
    from database import Database
    db = Database(data_dir / "prism.db")

    def _count() -> int:
        c = sqlite3.connect(str(data_dir / "prism.db"))
        try:
            return c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='fernet_key_rotated'"
            ).fetchone()[0]
        finally:
            c.close()

    before = _count()
    rekey_mod.run(config_path=cfg_path, data_dir=data_dir, dry_run=False, db=db)
    after = _count()
    assert after == before + 1


# ---------------------------------------------------------------------------
# Plain-text migration gate (B5 escape hatch)
# ---------------------------------------------------------------------------

def test_plaintext_migration_refused_without_env(tmp_path, monkeypatch):
    """When DPAPI is available and a plain-text key is on disk without a
    .dpapi sibling, _load_or_create_key must REFUSE to migrate unless
    PRISM_ALLOW_PLAINTEXT_MIGRATION=1 is set."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    key_path = data_dir / "prism.key"
    key_path_dpapi = data_dir / "prism.key.dpapi"
    key_path.write_bytes(Fernet.generate_key())

    monkeypatch.setattr(crypto_utils, "KEY_PATH", key_path)
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", key_path_dpapi)

    # Force "DPAPI available" without actually requiring pywin32 / Windows.
    monkeypatch.setattr(crypto_utils, "_dpapi_available", lambda: True)
    # Stub the wrap/unwrap so the migration body, if it ran, would succeed.
    monkeypatch.setattr(crypto_utils, "_dpapi_encrypt", lambda b: b"WRAPPED:" + b)
    monkeypatch.setattr(crypto_utils, "_dpapi_decrypt",
                        lambda b: b[len(b"WRAPPED:"):])
    monkeypatch.delenv("PRISM_ALLOW_PLAINTEXT_MIGRATION", raising=False)

    with pytest.raises(RuntimeError, match="PRISM_ALLOW_PLAINTEXT_MIGRATION"):
        crypto_utils._load_or_create_key()

    # Still untouched.
    assert key_path.exists()
    assert not key_path_dpapi.exists()


def test_plaintext_migration_allowed_with_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    key_path = data_dir / "prism.key"
    key_path_dpapi = data_dir / "prism.key.dpapi"
    seed_key = Fernet.generate_key()
    key_path.write_bytes(seed_key)

    monkeypatch.setattr(crypto_utils, "KEY_PATH", key_path)
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", key_path_dpapi)
    monkeypatch.setattr(crypto_utils, "_dpapi_available", lambda: True)
    monkeypatch.setattr(crypto_utils, "_dpapi_encrypt", lambda b: b"WRAPPED:" + b)
    monkeypatch.setattr(crypto_utils, "_dpapi_decrypt",
                        lambda b: b[len(b"WRAPPED:"):])
    # Don't touch the real filesystem ACL during the test.
    monkeypatch.setattr(crypto_utils, "_restrict_file_permissions",
                        lambda p: None)

    monkeypatch.setenv("PRISM_ALLOW_PLAINTEXT_MIGRATION", "1")

    key = crypto_utils._load_or_create_key()
    assert key == seed_key
    # Migration happened: dpapi file written, plain-text removed.
    assert key_path_dpapi.exists()
    assert not key_path.exists()


def test_plaintext_steady_state_when_dpapi_unavailable(tmp_path, monkeypatch):
    """On non-Windows / pywin32-missing dev boxes, plain-text key is the
    supported steady state. No env var should be required."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    key_path = data_dir / "prism.key"
    key_path_dpapi = data_dir / "prism.key.dpapi"
    seed_key = Fernet.generate_key()
    key_path.write_bytes(seed_key)

    monkeypatch.setattr(crypto_utils, "KEY_PATH", key_path)
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", key_path_dpapi)
    monkeypatch.setattr(crypto_utils, "_dpapi_available", lambda: False)
    monkeypatch.delenv("PRISM_ALLOW_PLAINTEXT_MIGRATION", raising=False)

    # Should NOT raise.
    key = crypto_utils._load_or_create_key()
    assert key == seed_key
    # No migration.
    assert not key_path_dpapi.exists()
    assert key_path.exists()
