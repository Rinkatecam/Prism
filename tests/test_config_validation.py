"""Server-side validation for the two collector settings that had none.

Both keys were writable via POST /api/config with no bounds check at all — the
only guard was the HTML min/max, which a direct API call ignores.

  * ``collector_v2_num_workers`` — load-bearing for startup. app.py converted it
    with a bare ``int()`` positioned BEFORE the try/except that guards collector
    startup, so a non-numeric value killed the process on the next boot instead
    of degrading gracefully. app.py is now defensive too (it has to be — a
    hand-edited config.json never reaches this endpoint), but this is the gate
    that stops a bad value being persisted in the first place.
  * ``update_check_interval_minutes`` — lower severity (supervisor.py already
    try/excepts and floors it), validated here for consistency with its sibling
    cadence fields.

Rejection style deliberately matches the neighbouring fields
(``poll_interval_seconds`` / ``retention_days`` / ``log_collection_interval_minutes``):
out-of-range returns 400 rather than silently clamping, so the operator finds out.
"""

from __future__ import annotations

import pytest
from flask import Flask

from config_manager import ConfigManager


@pytest.fixture()
def app_client(tmp_path):
    """Fresh Flask app + real Database/ConfigManager wired to the api blueprint.

    Mirrors tests/test_detection_fusion_config.py's fixture. No config.json is
    written up front, matching the CI posture (Linux runner, first-run mode).
    """
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "config_validation.db")
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


# ---------------------------------------------------------------------------
# collector_v2_num_workers — 2..100
# ---------------------------------------------------------------------------

def test_num_workers_in_range_persists(app_client):
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"collector_v2_num_workers": 30}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["collector_v2_num_workers"] == 30


@pytest.mark.parametrize("value", [2, 100])
def test_num_workers_accepts_inclusive_bounds(app_client, value):
    """2 and 100 are valid — the bounds are inclusive, matching the HTML."""
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"collector_v2_num_workers": value}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["collector_v2_num_workers"] == value


@pytest.mark.parametrize("value", [1, 0, -5, 101, 9999])
def test_num_workers_out_of_range_rejected(app_client, value):
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"collector_v2_num_workers": value}})

    assert r.status_code == 400
    body = r.get_json()
    assert body.get("ok") is False
    assert "Worker pool size" in body.get("error", "")


@pytest.mark.parametrize("value", ["not-a-number", None, [], {}])
def test_num_workers_non_numeric_rejected(app_client, value):
    """The startup-killing case: a non-numeric value must never persist."""
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"collector_v2_num_workers": value}})

    assert r.status_code == 400
    assert r.get_json().get("ok") is False


def test_num_workers_bad_value_is_not_persisted(app_client):
    """A rejected POST must leave the previous good value intact on disk."""
    client, cfg = app_client
    client.post("/api/config", json={"settings": {"collector_v2_num_workers": 20}})

    client.post("/api/config", json={"settings": {"collector_v2_num_workers": "garbage"}})

    assert cfg.get_settings()["collector_v2_num_workers"] == 20


# ---------------------------------------------------------------------------
# update_check_interval_minutes — 10..120
# ---------------------------------------------------------------------------

def test_update_interval_in_range_persists(app_client):
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"update_check_interval_minutes": 45}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["update_check_interval_minutes"] == 45


@pytest.mark.parametrize("value", [10, 120])
def test_update_interval_accepts_inclusive_bounds(app_client, value):
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"update_check_interval_minutes": value}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["update_check_interval_minutes"] == value


@pytest.mark.parametrize("value", [9, 0, -1, 121, 100000])
def test_update_interval_out_of_range_rejected(app_client, value):
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"update_check_interval_minutes": value}})

    assert r.status_code == 400
    assert "Update check interval" in r.get_json().get("error", "")


def test_update_interval_non_numeric_rejected(app_client):
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"update_check_interval_minutes": "soon"}})

    assert r.status_code == 400
    assert r.get_json().get("ok") is False


# ---------------------------------------------------------------------------
# CI posture: omitting the keys entirely must still pass
# ---------------------------------------------------------------------------

def test_settings_post_without_either_key_still_succeeds(app_client):
    """CI runs with no config.json. A settings POST that omits both keys must
    fall back to their defaults rather than 400 — same ``settings.get(key,
    default)`` shape the pre-existing validated fields use.
    """
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"language": "de"}})

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = cfg.get_settings()
    assert saved["language"] == "de"
    assert saved["collector_v2_num_workers"] == 15
    assert saved["update_check_interval_minutes"] == 30
