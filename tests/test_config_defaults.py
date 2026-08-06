"""Feature 1.8 — T5: database_backup settings defaults surface via get_settings."""

from __future__ import annotations

from config_manager import ConfigManager


def test_database_backup_defaults_present(tmp_path):
    # Missing config.json → get_settings() merges _DEFAULT_SETTINGS.
    cm = ConfigManager(tmp_path / "config.json")
    cfg = cm.get_settings().get("database_backup", {})
    assert cfg.get("enabled") is True
    assert cfg.get("interval_hours") == 24
    assert cfg.get("keep") == 14
    assert cfg.get("stale_after_hours") == 26
    assert cfg.get("alert_severity") == "warning"
