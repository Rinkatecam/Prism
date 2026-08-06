"""Regression guard: the Scheduled Restarts save/read round-trip.

The 2026-08-03 bug: ``ConfigManager.get_settings()`` builds its result by
iterating ``_DEFAULT_SETTINGS`` only, so any TOP-LEVEL settings.json key absent
from that dict is silently dropped from every caller's view.

``POST /api/scheduled-restarts`` writes its four keys straight into the raw
on-disk dict (by design), but none of them were declared in
``_DEFAULT_SETTINGS`` — so every reader was blind:

  * ``GET /api/scheduled-restarts``  -> form always repopulated as disabled
  * ``POST /api/restart-now``        -> manual trigger saw an empty schedule
  * ``restart_scheduler`` thread     -> ``enabled`` always False, never fired

Net effect: the whole feature was inert while every save reported success.
These tests pin the fix (the four keys are declared) and would have caught the
original defect, which 650+ existing tests did not.
"""

from __future__ import annotations

import json

import pytest

from config_manager import ConfigManager


# The four top-level keys the Operations page writes. Each MUST be declared in
# _DEFAULT_SETTINGS or get_settings() drops it.
RESTART_KEYS = (
    "scheduled_flask_restart",
    "scheduled_server_restart_schedule",
    "scheduled_server_restarts",
    "restart_delay_between_seconds",
)


def _write_config(tmp_path, settings: dict) -> ConfigManager:
    """Write a config.json with the given settings block, as the POST handler
    does (straight into the raw dict), and return a ConfigManager over it."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"servers": [], "settings": settings}), encoding="utf-8")
    return ConfigManager(cfg_file)


@pytest.mark.parametrize("key", RESTART_KEYS)
def test_restart_key_is_declared_in_defaults(key):
    """The root cause: an undeclared top-level key is invisible to readers."""
    assert key in ConfigManager._DEFAULT_SETTINGS, (
        f"{key} missing from _DEFAULT_SETTINGS — get_settings() will silently "
        "drop it and the Scheduled Restarts feature breaks again."
    )


def test_saved_schedule_survives_get_settings(tmp_path):
    """The exact bug: an ENABLED schedule on disk must be visible to readers.

    Before the fix get_settings() returned no such key at all, so
    ``srs.get("enabled", False)`` in restart_scheduler.py was always False.
    """
    cm = _write_config(tmp_path, {
        "scheduled_server_restart_schedule": {
            "enabled": True,
            "schedule": "weekly",
            "time": "02:30",
            "day": "3",
            "month_day": 1,
        },
    })

    srs = cm.get_settings().get("scheduled_server_restart_schedule", {})

    assert srs.get("enabled") is True, "scheduled schedule was dropped by get_settings()"
    assert srs.get("time") == "02:30"
    assert srs.get("day") == "3"


def test_saved_per_server_list_survives_get_settings(tmp_path):
    """restart_scheduler.py:87 and /api/restart-now both read this list."""
    entries = [{"server": "SRV01", "enabled": True}, {"server": "SRV02", "enabled": False}]
    cm = _write_config(tmp_path, {"scheduled_server_restarts": entries})

    assert cm.get_settings().get("scheduled_server_restarts") == entries


def test_saved_delay_survives_get_settings(tmp_path):
    """restart_scheduler.py:257 read this and always got the hardcoded 60."""
    cm = _write_config(tmp_path, {"restart_delay_between_seconds": 180})

    assert cm.get_settings().get("restart_delay_between_seconds") == 180


def test_flask_restart_block_survives_get_settings(tmp_path):
    cm = _write_config(tmp_path, {
        "scheduled_flask_restart": {
            "enabled": True, "schedule": "weekly", "time": "04:15", "day": "monday",
        },
    })

    fr = cm.get_settings().get("scheduled_flask_restart", {})

    assert fr.get("enabled") is True
    assert fr.get("time") == "04:15"


def test_partial_dict_merges_over_declared_defaults(tmp_path):
    """A partially-written dict must merge over the defaults, not replace them.

    get_settings() nested-merges dict-valued defaults, so a config that only
    sets ``enabled`` still gets the remaining sub-keys. This is what lets the
    Operations form render without KeyErrors after a partial save.
    """
    cm = _write_config(tmp_path, {"scheduled_server_restart_schedule": {"enabled": True}})

    srs = cm.get_settings()["scheduled_server_restart_schedule"]

    assert srs["enabled"] is True
    assert srs["time"] == "03:00", "missing sub-key should fall back to the default"
    assert srs["schedule"] == "weekly"
    assert srs["month_day"] == 1


def test_defaults_are_inert_when_nothing_is_saved(tmp_path):
    """First-run / no-config CI path: defaults must be safely disabled.

    CI runs on Linux with no config.json, so an accidentally-enabled default
    would make the scheduler act on an empty fleet.
    """
    cm = _write_config(tmp_path, {})
    s = cm.get_settings()

    assert s["scheduled_flask_restart"]["enabled"] is False
    assert s["scheduled_server_restart_schedule"]["enabled"] is False
    assert s["scheduled_server_restarts"] == []
    assert s["restart_delay_between_seconds"] == 60


def test_declared_defaults_match_get_endpoint_fallbacks():
    """Drift guard.

    ``routes/api/misc.py``'s GET handler carries its own inline fallback
    literals. Those were the *only* values readers ever saw while the bug was
    live, so the declared defaults must match them or the fix silently changes
    the Operations form's default state.
    """
    d = ConfigManager._DEFAULT_SETTINGS

    assert d["scheduled_flask_restart"] == {
        "enabled": False, "schedule": "daily", "time": "03:00", "day": "sunday",
    }
    assert d["scheduled_server_restart_schedule"] == {
        "enabled": False, "schedule": "weekly", "time": "03:00", "day": "6", "month_day": 1,
    }
    assert d["scheduled_server_restarts"] == []
    assert d["restart_delay_between_seconds"] == 60
