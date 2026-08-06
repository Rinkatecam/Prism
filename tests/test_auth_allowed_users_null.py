"""Regression guard: a null `settings.auth.allowed_users` must not break anything.

Bug 6 (docs/plans/CRITICAL_BUGS_REMEDIATION.md §6), found 2026-08-03 by loading
the real Settings page in a browser — every static and structural check passed.

Chain:
  1. `allowed_users` was NOT declared in _DEFAULT_SETTINGS["auth"], so
     get_settings() had no default to supply.
  2. POST /api/config's strip filter did `existing_auth.get(k)` with no default,
     writing a literal `None` for any sensitive auth key that had never been
     configured. That null got persisted to config.json.
  3. settings.html rendered `allowed_users | default([]) | join('\n')`. Jinja's
     `default` filter only substitutes for UNDEFINED, not None — so `join`
     raised TypeError and the whole Settings page returned HTTP 500.

Login was never affected (`if allowed_list:` treats None as falsy, i.e. "allow
all", which matches the documented empty behaviour), so the only symptom was a
dead Settings page — invisible to the 786 tests that existed at the time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from jinja2 import Environment

from config_manager import ConfigManager


# ---------------------------------------------------------------------------
# Layer 1: the default must be declared, and be a list
# ---------------------------------------------------------------------------

def test_allowed_users_is_declared_as_a_list_default():
    auth_defaults = ConfigManager._DEFAULT_SETTINGS["auth"]
    assert "allowed_users" in auth_defaults, (
        "allowed_users must be declared so get_settings() has a default to fall "
        "back to when disk holds a null."
    )
    assert auth_defaults["allowed_users"] == []


def test_fresh_config_yields_a_list(tmp_path):
    cm = ConfigManager(tmp_path / "config.json")

    assert cm.get_settings()["auth"]["allowed_users"] == []


# ---------------------------------------------------------------------------
# Layer 2: the strip filter must never persist None
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Real app + monkeypatched _shared, matching the convention in
    tests/test_get_server_endpoint.py and tests/test_compliance_phd_audit.py.

    Deliberately does NOT build a throwaway Flask app and call
    register_api_routes: that rebinds the module-level globals in
    routes/api/_shared.py while routes/views.py keeps its OWN _db/_config, and
    the two then point at different objects. Tests that patch through one
    reference while the route under test reads the other silently 404 —
    test_compliance_phd_audit.py fails exactly that way, as an
    ordering-dependent failure that only appears in a full-suite run.

    monkeypatch guarantees restoration, so nothing leaks into later tests.
    """
    from app import app as flask_app
    from database import Database
    from routes.api import _shared as shared

    db = Database(tmp_path / "allowed_users.db")
    cfg = ConfigManager(tmp_path / "config.json")

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    monkeypatch.setattr(shared, "_db", db)
    monkeypatch.setattr(shared, "_config", cfg)

    client = flask_app.test_client()
    now = datetime.now(timezone.utc).isoformat()
    with client.session_transaction() as sess:
        sess["username"] = "allowed_users_test"
        sess["login_time"] = now
        sess["last_activity"] = now
    return client, cfg


def test_save_never_writes_null_for_unconfigured_allowed_users(app_client):
    """The exact corruption: posting the field on a never-configured instance."""
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"auth": {
        "enabled": False, "type": "ldap",
        "allowed_users": ["someone@example.com"],   # stripped — must not persist
    }}})

    assert r.status_code == 200, r.get_data(as_text=True)
    on_disk = json.loads((cfg.config_path).read_text(encoding="utf-8"))
    stored = on_disk["settings"]["auth"].get("allowed_users")
    assert stored is not None, "a null was persisted — this is the original bug"
    assert stored == [], "unconfigured allowlist should fall back to the declared []"


def test_existing_null_on_disk_self_heals_on_next_save(app_client):
    """An instance already corrupted must repair itself, not stay broken."""
    client, cfg = app_client
    raw = {"servers": [], "settings": {"auth": {
        "enabled": False, "type": "ldap", "allowed_users": None}}}
    cfg.config_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg._cache = None
    cfg._cache_mtime = 0.0

    r = client.post("/api/config", json={"settings": {"auth": {
        "enabled": False, "type": "ldap", "allowed_users": ["x@example.com"]}}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["auth"]["allowed_users"] == []


def test_a_real_configured_allowlist_is_still_preserved(app_client):
    """The strip filter must keep doing its job — no privilege escalation."""
    client, cfg = app_client
    raw = {"servers": [], "settings": {"auth": {
        "enabled": True, "type": "ldap",
        "allowed_users": ["trusted@example.com"]}}}
    cfg.config_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg._cache = None
    cfg._cache_mtime = 0.0

    client.post("/api/config", json={"settings": {"auth": {
        "enabled": True, "type": "ldap",
        "allowed_users": ["attacker@example.com"]}}})

    saved = cfg.get_settings()["auth"]["allowed_users"]
    assert saved == ["trusted@example.com"], "strip filter must not be bypassable"


# ---------------------------------------------------------------------------
# Layer 3: the template expression must survive a None
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, [], ["a@example.com", "b@example.com"]])
def test_template_expression_survives_none_and_lists(value):
    """Pins the `or []` form. The old `| default([])` raised on None.

    Guards against a future edit reverting to the default filter, which reads as
    equivalent but is not.
    """
    tmpl = Environment().from_string(
        "{{ (settings.auth.allowed_users or []) | join('\\n') }}")

    out = tmpl.render(settings={"auth": {"allowed_users": value}})

    assert out == ("" if not value else "\n".join(value))


def test_old_default_filter_form_would_have_raised():
    """Documents WHY the change was needed, so nobody 'simplifies' it back."""
    tmpl = Environment().from_string(
        "{{ settings.auth.allowed_users | default([]) | join('\\n') }}")

    with pytest.raises(TypeError):
        tmpl.render(settings={"auth": {"allowed_users": None}})


def test_settings_template_has_no_unguarded_default_join():
    """The real template must not reintroduce the pattern anywhere."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "templates" / "settings.html"
    text = src.read_text(encoding="utf-8")

    assert "default([]) | join" not in text, (
        "`| default([]) | join` is None-unsafe — use `(value or []) | join`."
    )


# ---------------------------------------------------------------------------
# Layer 4: the login path stays None-safe
# ---------------------------------------------------------------------------

def test_login_allowlist_read_is_none_safe():
    """auth.py must not iterate a None allowlist.

    `.get("allowed_users", [])` returns None when the key EXISTS with a null
    value, so the default argument does not help.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "auth.py"
    text = src.read_text(encoding="utf-8")

    assert 'auth_cfg.get("allowed_users") or []' in text, (
        "the allowlist read must coerce None to [] explicitly"
    )
    assert 'auth_cfg.get("allowed_users", [])' not in text, (
        "this form returns None for an existing null key"
    )
