"""Config-surface tests for Detection Fusion (docs/plans/DETECTION_FUSION_PLAN.md §7, §8).

Covers only the configuration knobs added ahead of the fused verdict engine
(detection.py / collector_v2/aggregator.py are a separate, reserved
workstream and are not touched here):

  (a) the five new default settings surface via ConfigManager.get_settings()
      on a fresh instance with no config.json on disk — the CI posture.
  (b) POST /api/config clamps out-of-range values for the same five keys,
      in both directions, and coerces allow_downgrade to a real bool.
"""

from __future__ import annotations

import pytest
from flask import Flask

from config_manager import ConfigManager


# ---------------------------------------------------------------------------
# (a) Defaults surface via ConfigManager on a fresh (no config.json) instance
# ---------------------------------------------------------------------------

def test_exhaustion_and_baseline_defaults_present(tmp_path):
    # No config.json is written — get_settings() must merge in _DEFAULT_SETTINGS,
    # matching how a fresh CI checkout (no config.json) sees these settings.
    cm = ConfigManager(tmp_path / "config.json")
    settings = cm.get_settings()

    thresholds = settings.get("thresholds", {})
    assert thresholds.get("exhaustion_ram") == 98
    assert thresholds.get("exhaustion_disk") == 95

    baseline = settings.get("baseline_detection", {})
    assert baseline.get("allow_downgrade") is True
    assert baseline.get("min_span_weeks") == 2
    assert baseline.get("min_coverage_pct") == 50


# ---------------------------------------------------------------------------
# (b) POST /api/config clamps out-of-range values
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client(tmp_path):
    """Fresh Flask app + real Database/ConfigManager wired to the api blueprint.

    Mirrors tests/test_rbac_uniform.py's app_client fixture, but uses a real
    ConfigManager (not a stub) so save_config's clamping + on-disk persistence
    can be verified end-to-end via a subsequent get_settings() call. No
    config.json is written up front, matching the CI posture (no configured
    instance to assume).
    """
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "detection_fusion_config.db")
    cfg = ConfigManager(tmp_path / "config.json")

    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)

    # Ensure shared globals point at our fresh fixtures even if a previous
    # test wired something else (routes/api/_shared module state is global).
    shared._db = db
    shared._config = cfg

    client = app.test_client()
    return client, cfg


def test_post_config_clamps_high_side_and_coerces_bool(app_client):
    client, cfg = app_client

    r = client.post("/api/config", json={
        "settings": {
            "thresholds": {
                "enabled": True,
                "exhaustion_ram": 50,      # below 90 floor -> clamp up to 90
                "exhaustion_disk": 999,    # above 100 ceiling -> clamp to 100
            },
            "baseline_detection": {
                "enabled": True,
                "allow_downgrade": "yes",  # truthy non-bool -> coerced True
                "min_span_weeks": 0,       # below 1 -> clamp to 1
                "min_coverage_pct": 500,   # above 100 -> clamp to 100
            },
        },
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("ok") is True

    saved = cfg.get_settings()
    assert saved["thresholds"]["exhaustion_ram"] == 90
    assert saved["thresholds"]["exhaustion_disk"] == 100
    assert saved["baseline_detection"]["allow_downgrade"] is True
    assert saved["baseline_detection"]["min_span_weeks"] == 1
    assert saved["baseline_detection"]["min_coverage_pct"] == 100


def test_post_config_clamps_low_side(app_client):
    """A second POST proves clamping isn't one-directional (low side too)."""
    client, cfg = app_client

    r = client.post("/api/config", json={
        "settings": {
            "thresholds": {
                "enabled": True,
                "exhaustion_ram": 100,
                "exhaustion_disk": 10,     # below 80 floor -> clamp up to 80
            },
            "baseline_detection": {
                "enabled": True,
                "allow_downgrade": False,
                "min_span_weeks": 20,      # above 8 -> clamp down to 8
                "min_coverage_pct": 1,     # below 10 -> clamp up to 10
            },
        },
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    saved = cfg.get_settings()
    assert saved["thresholds"]["exhaustion_disk"] == 80
    assert saved["baseline_detection"]["allow_downgrade"] is False
    assert saved["baseline_detection"]["min_span_weeks"] == 8
    assert saved["baseline_detection"]["min_coverage_pct"] == 10


def test_post_config_persists_deviation_gates(app_client):
    """The three Layer-3 raise gates (ALERT_NOISE_AND_VERDICT_UX_PLAN §3)."""
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"baseline_detection": {
        "enabled": True,
        "deviation_direction": "BOTH",              # normalised to lowercase
        "deviation_min_pct_of_warning": 65,
        "deviation_requires_authority": "yes",      # truthy -> real bool
    }}})

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = cfg.get_settings()["baseline_detection"]
    assert saved["deviation_direction"] == "both"
    assert saved["deviation_min_pct_of_warning"] == 65
    assert saved["deviation_requires_authority"] is True


@pytest.mark.parametrize("pct,expected", [(-10, 0), (0, 0), (100, 100), (250, 100)])
def test_post_config_clamps_deviation_pct(app_client, pct, expected):
    """0 is meaningful (gate disabled) and must survive, not be coerced away."""
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"baseline_detection": {
        "enabled": True, "deviation_min_pct_of_warning": pct}}})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert cfg.get_settings()["baseline_detection"]["deviation_min_pct_of_warning"] == expected


@pytest.mark.parametrize("bad", ["sideways", "", "low", 3])
def test_post_config_rejects_bad_deviation_direction(app_client, bad):
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"baseline_detection": {
        "enabled": True, "deviation_direction": bad}}})

    assert r.status_code == 400
    assert "deviation_direction" in r.get_json().get("error", "")


def test_post_config_rejects_non_numeric_deviation_pct(app_client):
    client, _cfg = app_client

    r = client.post("/api/config", json={"settings": {"baseline_detection": {
        "enabled": True, "deviation_min_pct_of_warning": "most"}}})

    assert r.status_code == 400


def test_legacy_rollback_triple_round_trips(app_client):
    """The documented instant-rollback config must persist verbatim."""
    client, cfg = app_client

    r = client.post("/api/config", json={"settings": {"baseline_detection": {
        "enabled": True,
        "deviation_direction": "both",
        "deviation_min_pct_of_warning": 0,
        "deviation_requires_authority": False,
    }}})

    assert r.status_code == 200, r.get_data(as_text=True)
    saved = cfg.get_settings()["baseline_detection"]
    assert (saved["deviation_direction"], saved["deviation_min_pct_of_warning"],
            saved["deviation_requires_authority"]) == ("both", 0, False)


def test_post_config_rejects_non_numeric_exhaustion_value(app_client):
    client, cfg = app_client

    r = client.post("/api/config", json={
        "settings": {
            "thresholds": {
                "enabled": True,
                "exhaustion_ram": "not-a-number",
            },
        },
    })
    assert r.status_code == 400
    body = r.get_json()
    assert body.get("ok") is False
    assert "exhaustion_ram" in body.get("error", "")
