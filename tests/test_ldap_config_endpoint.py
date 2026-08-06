"""Regression guard: LDAP directory settings must be saveable — and only via
their own endpoint.

The 2026-08-03 bug: POST /api/config carries a defence-in-depth strip filter
(_SENSITIVE_AUTH_KEYS) that replaces any posted ``auth.ldap_*`` value with the
one already on disk. settings.html rebuilt ``data.settings.auth`` from the DOM on
every save, so the strip condition was always true and LDAP edits were
*unconditionally discarded* — with a "Config saved" success toast. LDAP had had
no working save path since the AUDIT-2026-05 R1 filter was added.

The fix is a dedicated ``POST /api/config/ldap``: admin-gated, validated,
audited, writing only the five LDAP keys. The strip filter stays, because
``ldap_url`` is in the same risk class as ``backup_admin`` — repointing it at a
hostile directory server is an authentication bypass.

Both halves are pinned here: the new endpoint persists, and the generic writer
still refuses to.
"""

from __future__ import annotations

import pytest
from flask import Flask

from config_manager import ConfigManager
from crypto_utils import decrypt_password, encrypt_password

GOOD_URL = "ldap://dc01.ad.example.com:389"
GOOD_BASE_DN = "DC=ad,DC=example,DC=com"


@pytest.fixture()
def app_client(tmp_path):
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "ldap_cfg.db")
    cfg = ConfigManager(tmp_path / "config.json")

    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)

    # Save/restore the _shared module globals so this fixture can't leak a
    # tmp_path DB/config into later tests (see test_auth_allowed_users_null.py).
    prev_db, prev_cfg = getattr(shared, "_db", None), getattr(shared, "_config", None)
    shared._db = db
    shared._config = cfg
    try:
        yield app.test_client(), cfg, db
    finally:
        shared._db, shared._config = prev_db, prev_cfg


def _auth(cfg):
    return cfg.get_settings().get("auth", {}) or {}


def _bind_pw(cfg):
    """Return the stored bind password *decrypted*, asserting it is encrypted
    at rest on the way through.

    These assertions used to compare the on-disk value directly against the
    plain-text the test posted — which passed happily while the credential sat
    unencrypted in config.json, and pinned that as correct. Going through this
    helper means the encryption cannot silently regress without a test failing.
    """
    stored = _auth(cfg).get("ldap_bind_password", "")
    if stored:
        assert stored.startswith("enc:"), (
            "bind password must be encrypted at rest; found a bare "
            f"{len(stored)}-char value"
        )
    return decrypt_password(stored)


# ---------------------------------------------------------------------------
# The core defect: LDAP settings must actually persist
# ---------------------------------------------------------------------------

def test_ldap_settings_persist_via_dedicated_endpoint(app_client):
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL,
        "ldap_base_dn": GOOD_BASE_DN,
        "ldap_bind_user": "svc_prism",
        "ldap_bind_password": "s3cret-bind-pw",
    })

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = _auth(cfg)
    assert saved["ldap_url"] == GOOD_URL
    assert saved["ldap_base_dn"] == GOOD_BASE_DN
    assert saved["ldap_bind_user"] == "svc_prism"
    assert _bind_pw(cfg) == "s3cret-bind-pw"


def test_generic_config_endpoint_still_refuses_to_write_ldap(app_client):
    """The strip filter must remain effective — this is the security half."""
    client, cfg, _db = app_client
    # Establish a known-good directory config through the proper endpoint.
    client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL, "ldap_base_dn": GOOD_BASE_DN,
    })

    # Now try to hijack it through the generic writer.
    r = client.post("/api/config", json={
        "settings": {
            "auth": {
                "enabled": True,
                "type": "ldap",
                "ldap_url": "ldap://attacker.example.net:389",
                "ldap_base_dn": "DC=evil",
                "ldap_user_filter": "(sAMAccountName={username})",
                "ldap_bind_user": "pwned",
                "ldap_bind_password": "pwned",
            },
        },
    })

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = _auth(cfg)
    assert saved["ldap_url"] == GOOD_URL, "generic writer must not mutate ldap_url"
    assert saved["ldap_base_dn"] == GOOD_BASE_DN
    assert saved["ldap_bind_user"] != "pwned"


def test_generic_config_save_preserves_ldap_rather_than_blanking_it(app_client):
    """A partial settings save must not wipe the directory config (Bug 5).

    save_config used to write ``config["settings"] = settings`` verbatim, so an
    omitted ``auth`` sub-tree disappeared from disk and get_settings() fell back
    to the empty defaults. It now deep-merges over the on-disk settings.
    """
    client, cfg, _db = app_client
    client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL, "ldap_base_dn": GOOD_BASE_DN, "ldap_bind_user": "svc_prism",
    })

    # A save that touches only unrelated settings, as the UI does.
    r = client.post("/api/config", json={"settings": {"language": "de"}})

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = _auth(cfg)
    assert saved["ldap_url"] == GOOD_URL
    assert saved["ldap_bind_user"] == "svc_prism"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "http://dc01.ad.example.com",
    "https://dc01.ad.example.com",
    "file:///etc/passwd",
    "dc01.ad.example.com",
    "ldap://",
    "javascript:alert(1)",
])
def test_non_ldap_url_rejected(app_client, bad_url):
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={"ldap_url": bad_url})

    assert r.status_code == 400, f"{bad_url} should be rejected"
    assert r.get_json().get("ok") is False
    assert "ldap://" in r.get_json().get("error", "")


@pytest.mark.parametrize("good_url", [
    "ldap://dc01.ad.example.com:389",
    "ldaps://dc01.ad.example.com:636",
    "LDAP://DC01.AD.EXAMPLE.COM",
    "ldaps://10.0.0.5",
])
def test_valid_ldap_urls_accepted(app_client, good_url):
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={"ldap_url": good_url})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _auth(cfg)["ldap_url"] == good_url


def test_empty_url_allowed_for_clearing_config(app_client):
    """An empty URL is a legitimate 'not configured yet' / disable state."""
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={"ldap_url": ""})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _auth(cfg)["ldap_url"] == ""


def test_user_filter_without_username_placeholder_rejected(app_client):
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL, "ldap_user_filter": "(objectClass=user)",
    })

    assert r.status_code == 400
    assert "{username}" in r.get_json().get("error", "")


def test_blank_user_filter_falls_back_to_default(app_client):
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_user_filter": ""})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _auth(cfg)["ldap_user_filter"] == "(sAMAccountName={username})"


def test_missing_body_rejected(app_client):
    client, _cfg, _db = app_client

    r = client.post("/api/config/ldap", json={})

    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Bind-password preservation
# ---------------------------------------------------------------------------

def test_blank_bind_password_preserves_existing(app_client):
    """The UI sends an empty field when the operator didn't retype the password."""
    client, cfg, _db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": "original-pw"})

    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": ""})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _bind_pw(cfg) == "original-pw"


def test_masked_bind_password_preserves_existing(app_client):
    """GET /api/config returns PASSWORD_MASK — a round-trip must not store it."""
    from crypto_utils import PASSWORD_MASK
    client, cfg, _db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": "original-pw"})

    r = client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL, "ldap_bind_password": PASSWORD_MASK,
    })

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _bind_pw(cfg) == "original-pw"


def test_new_bind_password_replaces_existing(app_client):
    client, cfg, _db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": "original-pw"})

    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": "rotated-pw"})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert _bind_pw(cfg) == "rotated-pw"


# ---------------------------------------------------------------------------
# Encryption at rest (2026-08-05). The field used to be stored plain text,
# which meant tools/rekey.py — which lists it as a canonical credential path —
# silently skipped it on every key rotation, because rekey ignores any value
# without the 'enc:' prefix.
# ---------------------------------------------------------------------------

def test_bind_password_is_encrypted_at_rest(app_client):
    client, cfg, _db = app_client
    secret = "plain-text-would-fail-this"

    client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL, "ldap_bind_password": secret,
    })

    stored = _auth(cfg)["ldap_bind_password"]
    assert stored.startswith("enc:"), "must be Fernet-encrypted on disk"
    assert secret not in stored, "plain text must not appear in the stored value"
    assert decrypt_password(stored) == secret, "must round-trip"


def test_legacy_plaintext_on_disk_is_upgraded_by_a_save(app_client):
    """An operator who never retypes the password must still end up encrypted.

    The blank/masked 'unchanged' branch re-runs the stored value through
    encrypt_password, so the upgrade happens on the next save of any LDAP field.
    """
    client, cfg, _db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                          "ldap_bind_password": "legacy-pw"})
    # Force the on-disk value back to plain text, simulating a pre-fix config.
    raw = cfg._get_raw_config()
    raw["settings"]["auth"]["ldap_bind_password"] = "legacy-pw"
    import json as _json
    with open(cfg.config_path, "w") as f:
        _json.dump(raw, f, indent=2)
    cfg._cache = None
    cfg._cache_mtime = 0.0
    assert _auth(cfg)["ldap_bind_password"] == "legacy-pw"  # precondition

    # Save an unrelated field with a blank password (the UI's normal round-trip).
    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                              "ldap_bind_password": ""})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _bind_pw(cfg) == "legacy-pw", "value preserved and now encrypted"


def test_resaving_an_unchanged_password_is_not_audited_as_a_rotation(app_client):
    """Fernet embeds a random IV, so an unchanged password re-encrypts to a
    DIFFERENT token. Comparing ciphertexts would flag a rotation on every save
    and make the audit trail meaningless."""
    client, cfg, db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                          "ldap_bind_password": "stable-pw"})

    # Re-post the identical password.
    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                              "ldap_bind_password": "stable-pw"})
    assert r.status_code == 200, r.get_data(as_text=True)

    entries = db.get_audit_log(limit=50) if hasattr(db, "get_audit_log") else []
    # Filter to THIS endpoint's entries rather than taking the globally-last
    # row: the audit log is shared, so an unrelated test running before this one
    # could otherwise supply the last row and make the assertion order-dependent.
    ldap_entries = [str(e) for e in entries if "update_ldap_config" in str(e)]
    assert ldap_entries, "the LDAP save should have been audited"
    # Oldest-first, so the second save is the last LDAP entry. The first save
    # legitimately IS a rotation (empty -> stable-pw).
    last = ldap_entries[-1]
    assert "ldap_bind_password" not in last, (
        f"unchanged password must not be reported as rotated; got: {last}"
    )
    assert _bind_pw(cfg) == "stable-pw"


def test_changed_password_IS_audited_as_a_rotation(app_client):
    """The other half — don't fix the false positive by reporting nothing."""
    client, cfg, db = app_client
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                          "ldap_bind_password": "first-pw"})
    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL,
                                          "ldap_bind_password": "second-pw"})

    entries = db.get_audit_log(limit=50) if hasattr(db, "get_audit_log") else []
    assert "ldap_bind_password" in " ".join(str(e) for e in entries)
    assert _bind_pw(cfg) == "second-pw"


# ---------------------------------------------------------------------------
# Scope + audit
# ---------------------------------------------------------------------------

def test_endpoint_cannot_write_backup_admin_or_allowed_users(app_client):
    """Scope guard: this endpoint owns ldap_* only, nothing else under auth."""
    client, cfg, _db = app_client

    r = client.post("/api/config/ldap", json={
        "ldap_url": GOOD_URL,
        "backup_admin": {"password_hash": "injected"},
        "allowed_users": ["attacker@example.com"],
        "enabled": True,
    })

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = _auth(cfg)
    assert saved.get("backup_admin", {}) != {"password_hash": "injected"}
    assert "attacker@example.com" not in (saved.get("allowed_users") or [])


def test_ldap_save_is_audited(app_client):
    """A directory-server change must leave an audit trail."""
    client, _cfg, db = app_client

    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_base_dn": GOOD_BASE_DN})

    entries = db.get_audit_log(limit=50) if hasattr(db, "get_audit_log") else []
    actions = " ".join(str(e) for e in entries)
    assert "update_ldap_config" in actions, "LDAP change was not audited"


def test_audit_detail_never_contains_the_password(app_client):
    client, _cfg, db = app_client
    secret = "do-not-log-me-9412"

    client.post("/api/config/ldap", json={"ldap_url": GOOD_URL, "ldap_bind_password": secret})

    entries = db.get_audit_log(limit=50) if hasattr(db, "get_audit_log") else []
    assert secret not in " ".join(str(e) for e in entries)


def test_other_settings_untouched_by_ldap_save(app_client):
    """Writing into the raw dict must not clobber unrelated settings."""
    client, cfg, _db = app_client
    client.post("/api/config", json={"settings": {"language": "de", "retention_days": 90}})

    r = client.post("/api/config/ldap", json={"ldap_url": GOOD_URL})

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = cfg.get_settings()
    assert saved["language"] == "de"
    assert saved["retention_days"] == 90
    assert saved["auth"]["ldap_url"] == GOOD_URL
