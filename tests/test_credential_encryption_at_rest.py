"""Every credential in config.json must be encrypted at rest.

Found 2026-08-05: `settings.auth.ldap_bind_password` was stored plain text. The
damage was not just the plain text itself — tools/rekey.py lists that field as
one of three canonical credential paths, but its rotation pass SKIPS any value
without the 'enc:' prefix. So key rotation silently never re-protected it, and
reported it as `skipped`, which reads like "nothing to do" rather than "gap".

The guard that matters most here is test_migration_covers_every_rekey_path: it
pins ConfigManager._migrate_plaintext_passwords and rekey._iter_credential_paths
to the same set of fields, so adding a credential to one without the other fails
a test instead of quietly creating another unrotatable secret.
"""

from __future__ import annotations

import json

import pytest

import crypto_utils
from config_manager import ConfigManager
from crypto_utils import decrypt_password, encrypt_password


def _write(path, cfg):
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _base_cfg(**auth):
    return {
        "servers": [],
        "settings": {
            "auth": {"ldap_bind_user": "svc", **auth},
            "email": {"password": ""},
        },
    }


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── the migration ─────────────────────────────────────────────────────────

def test_migration_encrypts_plaintext_ldap_bind_password(tmp_path):
    p = tmp_path / "config.json"
    _write(p, _base_cfg(ldap_bind_password="plain-bind-pw"))

    ConfigManager(str(p))  # migration runs in __init__

    stored = _load(p)["settings"]["auth"]["ldap_bind_password"]
    assert stored.startswith("enc:")
    assert "plain-bind-pw" not in stored
    assert decrypt_password(stored) == "plain-bind-pw"


def test_migration_encrypts_plaintext_email_password(tmp_path):
    p = tmp_path / "config.json"
    cfg = _base_cfg()
    cfg["settings"]["email"]["password"] = "smtp-secret"
    _write(p, cfg)

    ConfigManager(str(p))

    stored = _load(p)["settings"]["email"]["password"]
    assert stored.startswith("enc:")
    assert decrypt_password(stored) == "smtp-secret"


def test_migration_encrypts_server_passwords_too(tmp_path):
    """The pre-existing behaviour must not regress."""
    p = tmp_path / "config.json"
    cfg = _base_cfg()
    cfg["servers"] = [{"name": "s1", "host": "h1", "username": "u",
                       "password": "server-plain"}]
    _write(p, cfg)

    ConfigManager(str(p))

    stored = _load(p)["servers"][0]["password"]
    assert stored.startswith("enc:")
    assert decrypt_password(stored) == "server-plain"


def test_migration_is_idempotent_and_does_not_double_encrypt(tmp_path):
    p = tmp_path / "config.json"
    _write(p, _base_cfg(ldap_bind_password="once-only"))

    ConfigManager(str(p))
    first = _load(p)["settings"]["auth"]["ldap_bind_password"]

    ConfigManager(str(p))
    second = _load(p)["settings"]["auth"]["ldap_bind_password"]

    assert first == second, "a second run must not rewrite an encrypted value"
    assert decrypt_password(second) == "once-only"
    assert not decrypt_password(second).startswith("enc:"), "not double-encrypted"


def test_migration_leaves_empty_credentials_alone(tmp_path):
    p = tmp_path / "config.json"
    _write(p, _base_cfg(ldap_bind_password=""))

    ConfigManager(str(p))

    assert _load(p)["settings"]["auth"]["ldap_bind_password"] == ""


def test_migration_survives_a_mangled_settings_block(tmp_path):
    """Runs in __init__, so it must never block startup."""
    p = tmp_path / "config.json"
    _write(p, {"servers": [], "settings": {"auth": None, "email": "not-a-dict"}})

    ConfigManager(str(p))  # must not raise

    assert _load(p)["settings"]["auth"] is None


def test_migration_never_logs_the_credential(tmp_path, caplog):
    p = tmp_path / "config.json"
    _write(p, _base_cfg(ldap_bind_password="do-not-log-me-7781"))

    with caplog.at_level("DEBUG"):
        ConfigManager(str(p))

    assert "do-not-log-me-7781" not in caplog.text
    assert "ldap_bind_password" in caplog.text, "field NAME should be logged"


# ── the invariant that keeps this from happening again ────────────────────

def test_migration_covers_every_rekey_path():
    """rekey skips non-'enc:' values, so any credential path rekey knows about
    MUST also be a path the migration encrypts. Otherwise that credential is
    permanently unrotatable — which is exactly how ldap_bind_password got here.
    """
    from tools.rekey import _iter_credential_paths

    probe = {
        "servers": [{"password": "a"}],
        "settings": {"email": {"password": "b"},
                     "auth": {"ldap_bind_password": "c"}},
    }
    rekey_keys = {key for _parent, key in _iter_credential_paths(probe)}
    assert rekey_keys == {"password", "ldap_bind_password"}, (
        "rekey's credential set changed — update _migrate_plaintext_passwords "
        "in config_manager.py to match, then update this test"
    )


def test_every_rekey_path_is_encrypted_after_migration(tmp_path):
    """End-to-end version of the above: migrate a config with plain text in all
    three canonical locations, then assert rekey would find nothing to skip."""
    from tools.rekey import _iter_credential_paths

    p = tmp_path / "config.json"
    cfg = {
        "servers": [{"name": "s1", "host": "h", "username": "u", "password": "p1"},
                    {"name": "s2", "host": "h", "username": "u", "password": "p2"}],
        "settings": {"email": {"password": "p3"},
                     "auth": {"ldap_bind_password": "p4"}},
    }
    _write(p, cfg)

    ConfigManager(str(p))

    migrated = _load(p)
    unencrypted = [
        key for parent, key in _iter_credential_paths(migrated)
        if parent.get(key) and not str(parent.get(key)).startswith("enc:")
    ]
    assert unencrypted == [], f"rekey would skip these: {unencrypted}"


# ── the plain-text key purge ──────────────────────────────────────────────

def test_purge_plaintext_key_removes_a_redundant_file(tmp_path, monkeypatch):
    kp = tmp_path / "prism.key"
    kp.write_bytes(encrypt_password("x").encode()[:44])
    monkeypatch.setattr(crypto_utils, "KEY_PATH", kp)
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", tmp_path / "prism.key.dpapi")

    assert crypto_utils._purge_plaintext_key_if_redundant() is True
    assert not kp.exists()


def test_purge_plaintext_key_is_a_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto_utils, "KEY_PATH", tmp_path / "nope.key")
    monkeypatch.setattr(crypto_utils, "KEY_PATH_DPAPI", tmp_path / "x.dpapi")
    assert crypto_utils._purge_plaintext_key_if_redundant() is False


def test_restrict_permissions_grants_delete(tmp_path):
    """The (R,W) grant left the owner unable to delete its own file, which is
    what stranded a plain-text key next to its DPAPI replacement for 34 days.
    (R,W,D) must be requested, and the file must actually be deletable after."""
    import os
    if os.name != "nt":
        pytest.skip("icacls is Windows-only")
    f = tmp_path / "restricted.bin"
    f.write_bytes(b"x" * 16)

    crypto_utils._restrict_file_permissions(f)

    f.unlink()  # would raise PermissionError under the old (R,W) grant
    assert not f.exists()
