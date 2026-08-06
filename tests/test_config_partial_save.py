"""Regression guard: a partial settings POST must not wipe the keys it omits.

Bug 5 (docs/plans/CRITICAL_BUGS_REMEDIATION.md §5), found 2026-08-03 while
testing the LDAP fix. ``ConfigManager.save_config`` wrote
``config["settings"] = settings`` verbatim, so every top-level settings key
absent from the request was deleted from disk. ``get_settings()`` then backfilled
the default, making the loss invisible.

``templates/monitoring.html`` builds ``const data = { settings: {} }`` from
scratch, so ONE Monitoring-page save wiped the operator's SMTP credentials,
Teams webhook URL, LDAP config and restart schedule, and reset retention, poll
interval and UI language — behind a green success toast.

Two halves are pinned here:
  * dicts MERGE, so omitted sub-trees and scalars survive;
  * lists REPLACE, so deleting a recipient / certificate / window still works.
"""

from __future__ import annotations

import pytest
from flask import Flask

from config_manager import ConfigManager


@pytest.fixture()
def app_client(tmp_path):
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "partial_save.db")
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
        yield app.test_client(), cfg
    finally:
        shared._db, shared._config = prev_db, prev_cfg


def _seed(client):
    """Configure a realistic instance across several settings sub-trees."""
    r = client.post("/api/config", json={"settings": {
        "language": "de",
        "timezone": "Europe/Zurich",
        "retention_days": 90,
        "poll_interval_seconds": 120,
        "collector_v2_num_workers": 25,
        "update_check_interval_minutes": 60,
        "email": {
            "enabled": True, "smtp_server": "smtp.example.com", "smtp_port": 587,
            "recipients": ["ops@example.com", "oncall@example.com"],
        },
        "webhooks": {"enabled": True, "teams_webhook_url": "https://outlook.office.com/webhook/x"},
        "scheduled_server_restart_schedule": {
            "enabled": True, "schedule": "weekly", "time": "02:30", "day": "3", "month_day": 1},
    }})
    assert r.status_code == 200, r.get_data(as_text=True)


# The exact payload shape templates/monitoring.html sends: built from scratch,
# carrying only the five monitoring sub-trees.
MONITORING_PAYLOAD = {"settings": {
    "thresholds": {"enabled": True, "exhaustion_ram": 98, "exhaustion_disk": 95},
    "anomaly_detection": {"enabled": True, "suppression_hours": 4},
    "baseline_detection": {
        "enabled": True, "allow_downgrade": True, "min_span_weeks": 2, "min_coverage_pct": 50},
    "security_alerts": {"failed_login_tracking": True},
    "tls_monitoring": {"enabled": False, "certificates": []},
}}


# ---------------------------------------------------------------------------
# The headline regression: the Monitoring-page save
# ---------------------------------------------------------------------------

def test_monitoring_page_save_preserves_every_unrelated_setting(app_client):
    client, cfg = app_client
    _seed(client)
    before = cfg.get_settings()

    r = client.post("/api/config", json=MONITORING_PAYLOAD)

    assert r.status_code == 200, r.get_data(as_text=True)
    after = cfg.get_settings()

    assert after["email"] == before["email"], "SMTP settings were wiped"
    assert after["webhooks"] == before["webhooks"], "webhook settings were wiped"
    assert after["scheduled_server_restart_schedule"] == \
        before["scheduled_server_restart_schedule"], "restart schedule was wiped"
    assert after["retention_days"] == 90
    assert after["poll_interval_seconds"] == 120
    assert after["language"] == "de"
    assert after["timezone"] == "Europe/Zurich"
    assert after["collector_v2_num_workers"] == 25
    assert after["update_check_interval_minutes"] == 60


def test_monitoring_page_save_still_applies_its_own_values(app_client):
    """Preserving the rest must not stop the posted values landing."""
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json=MONITORING_PAYLOAD)

    saved = cfg.get_settings()
    assert saved["baseline_detection"]["enabled"] is True
    assert saved["baseline_detection"]["min_coverage_pct"] == 50
    assert saved["thresholds"]["exhaustion_ram"] == 98
    assert saved["tls_monitoring"]["enabled"] is False


# ---------------------------------------------------------------------------
# Scalars: absent means untouched, present means applied
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,seeded", [
    ("retention_days", 90),
    ("poll_interval_seconds", 120),
    ("collector_v2_num_workers", 25),
    ("update_check_interval_minutes", 60),
    ("language", "de"),
    ("timezone", "Europe/Zurich"),
])
def test_absent_scalar_is_not_reset_to_default(app_client, key, seeded):
    """The validation block used to inject its default for every absent key."""
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {"thresholds": {"enabled": True}}})

    assert cfg.get_settings()[key] == seeded


def test_posted_scalar_still_overwrites(app_client):
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {"retention_days": 7, "language": "fr"}})

    saved = cfg.get_settings()
    assert saved["retention_days"] == 7
    assert saved["language"] == "fr"
    assert saved["poll_interval_seconds"] == 120, "untouched key must be unaffected"


# ---------------------------------------------------------------------------
# Lists replace wholesale — deletion must keep working
# ---------------------------------------------------------------------------

def _full_email(**over):
    """A COMPLETE email sub-tree. See the sub-tree contract test below for why
    callers must post the whole thing rather than a fragment."""
    base = {
        "enabled": True, "smtp_server": "smtp.example.com", "smtp_port": 587,
        "use_tls": True, "username": "", "password": "",
        "recipients": ["ops@example.com", "oncall@example.com"],
        "send_on_critical": True, "send_on_warning": False,
    }
    base.update(over)
    return base


def test_shrinking_a_list_removes_entries(app_client):
    """Merging lists element-wise would make recipients unremovable."""
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {
        "email": _full_email(recipients=["ops@example.com"]),
    }})

    saved = cfg.get_settings()
    assert saved["email"]["recipients"] == ["ops@example.com"]
    assert saved["email"]["smtp_server"] == "smtp.example.com"
    assert saved["email"]["enabled"] is True


def test_emptying_a_list_clears_it(app_client):
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {"email": _full_email(recipients=[])}})

    assert cfg.get_settings()["email"]["recipients"] == []


def test_explicit_empty_string_clears_a_value(app_client):
    """Clearing a webhook URL is expressed as "", not by omitting the key."""
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {
        "webhooks": {
            "enabled": False, "teams_webhook_url": "",
            "send_on_critical": True, "send_on_warning": False,
        },
    }})

    saved = cfg.get_settings()
    assert saved["webhooks"]["teams_webhook_url"] == ""
    assert saved["webhooks"]["enabled"] is False


def test_false_is_not_treated_as_absent(app_client):
    """A falsy posted value must still be written — not skipped as 'missing'."""
    client, cfg = app_client
    _seed(client)

    client.post("/api/config", json={"settings": {"email": _full_email(enabled=False)}})

    saved = cfg.get_settings()
    assert saved["email"]["enabled"] is False
    assert saved["email"]["smtp_server"] == "smtp.example.com"


def test_subtree_contract_partial_subtree_resets_its_siblings(app_client):
    """DOCUMENTED CONSTRAINT, not an aspiration.

    The merge in save_config makes omitting a whole TOP-LEVEL key safe. It does
    NOT make omitting a key *inside* one of the validated sub-trees safe: the
    https / auth / email / webhooks / scheduled_reports validators in
    routes/api/config.py normalise their sub-tree by writing every field back
    (``email_cfg["smtp_server"] = str(email_cfg.get("smtp_server", "")).strip()``),
    so a fragment posted for one of those keys blanks its siblings BEFORE the
    merge ever sees it.

    Every current caller posts complete sub-trees, so this is unreachable from
    the UI — but it is a real trap for a future page or API client. This test
    pins the actual behaviour so the constraint is visible and any future change
    to it is a deliberate, reviewed decision rather than a surprise.

    Rule for callers: omit a top-level key freely; never post a partial sub-tree
    for https / auth / email / webhooks / scheduled_reports.
    """
    client, cfg = app_client
    _seed(client)

    # A fragment: only 'recipients', no smtp_server.
    client.post("/api/config", json={"settings": {"email": {"recipients": ["a@example.com"]}}})

    saved = cfg.get_settings()
    assert saved["email"]["recipients"] == ["a@example.com"], "posted value applied"
    assert saved["email"]["smtp_server"] == "", (
        "KNOWN CONSTRAINT: the email validator blanks omitted sibling fields. If "
        "this now preserves 'smtp.example.com', the sub-tree validators were made "
        "presence-guarded — update this test and the note in "
        "docs/plans/CRITICAL_BUGS_REMEDIATION.md §5."
    )


# ---------------------------------------------------------------------------
# _deep_merge_settings unit behaviour
# ---------------------------------------------------------------------------

def test_deep_merge_recurses_into_nested_dicts():
    base = {"a": {"b": {"c": 1, "d": 2}}, "keep": "yes"}
    out = ConfigManager._deep_merge_settings(base, {"a": {"b": {"c": 9}}})

    assert out == {"a": {"b": {"c": 9, "d": 2}}, "keep": "yes"}


def test_deep_merge_replaces_lists_not_merges_them():
    out = ConfigManager._deep_merge_settings({"xs": [1, 2, 3]}, {"xs": [9]})

    assert out["xs"] == [9]


def test_deep_merge_replaces_dict_with_scalar_when_types_differ():
    out = ConfigManager._deep_merge_settings({"x": {"a": 1}}, {"x": "scalar"})

    assert out["x"] == "scalar"


def test_deep_merge_does_not_mutate_its_inputs():
    base = {"a": {"b": 1}}
    incoming = {"a": {"c": 2}}

    ConfigManager._deep_merge_settings(base, incoming)

    assert base == {"a": {"b": 1}}, "base was mutated"
    assert incoming == {"a": {"c": 2}}, "incoming was mutated"


# ---------------------------------------------------------------------------
# save_config(servers) with no settings must preserve what's on disk
# ---------------------------------------------------------------------------

def test_server_only_save_preserves_settings(app_client):
    """routes/api/servers.py calls save_config(servers) with settings=None.

    That path used to round-trip get_settings() — the DEFAULTS-FILTERED view —
    which both baked every default into config.json and dropped any undeclared
    top-level key.
    """
    client, cfg = app_client
    _seed(client)
    before = cfg.get_settings()

    cfg.save_config(cfg.get_raw_servers())

    after = cfg.get_settings()
    assert after["email"] == before["email"]
    assert after["retention_days"] == 90
    assert after["language"] == "de"
    assert after["scheduled_server_restart_schedule"]["enabled"] is True
