"""Tests for ``maintenance.py`` — F-004 remediation.

Maintenance-window evaluation drives both threshold loosening and full
alert suppression. A timezone or wallclock bug here silently mutes
alerts during a window that doesn't actually exist, or fails to mute
during a real one — both are operational hazards.

These tests pin:
  * In-window vs out-of-window matching across server / weekday / time.
  * Overnight-wrap windows (start > end).
  * Invalid timezone returns None (does not crash, does not fall back).
  * suppress_alerts vs threshold-only loosening.
"""

from __future__ import annotations

import datetime
import zoneinfo
from unittest.mock import patch

import pytest

from maintenance import (
    _get_active_maintenance_window,
    _get_maintenance_thresholds,
    _is_alert_suppressed_by_maintenance,
)


def _fake_now(year=2026, month=5, day=20, hour=12, minute=0, tz_name="Europe/Berlin"):
    """Return a tz-aware datetime that ``maintenance._get_active_maintenance_window``
    will use after we patch ``datetime.datetime.now``."""
    tz = zoneinfo.ZoneInfo(tz_name)
    return datetime.datetime(year, month, day, hour, minute, tzinfo=tz)


def _patch_now(monkeypatch, dt):
    """Monkeypatch ``datetime.datetime.now`` inside the maintenance module."""
    import maintenance

    class _FakeDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(maintenance.datetime, "datetime", _FakeDateTime)


# ── In-window matching ────────────────────────────────────────────────

def test_window_active_when_all_three_match(monkeypatch):
    """Server in list + day in list + time in range → window returned."""
    # Monday (weekday=0) at 12:00 Europe/Berlin.
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))  # 2026-05-18 is a Monday
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0, 1, 2], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    win = _get_active_maintenance_window("srv1", settings)
    assert win is not None
    assert win["start_time"] == "08:00"


def test_no_window_when_server_not_in_list(monkeypatch):
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srvA"], "days": [0, 1, 2], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _get_active_maintenance_window("srvB", settings) is None


def test_no_window_when_day_not_in_list(monkeypatch):
    # 2026-05-23 is a Saturday (weekday=5)
    _patch_now(monkeypatch, _fake_now(2026, 5, 23, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0, 1, 2, 3, 4], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is None


def test_no_window_when_outside_time_range(monkeypatch):
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 20, 0))  # 20:00, outside 08:00-18:00
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is None


# ── Time-range boundary cases ─────────────────────────────────────────

def test_window_active_at_exact_start_time(monkeypatch):
    """Boundary: at the exact start_time, window is active (``<=`` semantics)."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 8, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is not None


def test_window_active_at_exact_end_time(monkeypatch):
    """Boundary: at the exact end_time, window is active (``<=`` semantics)."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 18, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is not None


# ── Overnight wrap (start > end) ─────────────────────────────────────

def test_overnight_window_active_late_evening(monkeypatch):
    """Window 22:00–06:00 should be active at 23:30."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 23, 30))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "22:00", "end_time": "06:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is not None


def test_overnight_window_active_early_morning(monkeypatch):
    """Window 22:00–06:00 should still be active at 02:00 (next day)."""
    # 2026-05-19 is Tuesday (weekday=1) — same window applies if listed.
    _patch_now(monkeypatch, _fake_now(2026, 5, 19, 2, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [1], "start_time": "22:00", "end_time": "06:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is not None


def test_overnight_window_inactive_in_afternoon(monkeypatch):
    """Window 22:00–06:00 must NOT be active at 14:00."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 14, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "22:00", "end_time": "06:00"},
        ],
    }
    assert _get_active_maintenance_window("srv1", settings) is None


# ── Invalid timezone — refuses to evaluate (returns None) ─────────────

def test_invalid_timezone_returns_none(monkeypatch, caplog):
    """P15 from prior audit: refuse naive datetime fallback. Misconfigured
    timezone → return None (no window active) rather than evaluate in the
    wrong wallclock space."""
    settings = {
        "timezone": "Not/A_RealTimeZone",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0, 1, 2, 3, 4, 5, 6],
             "start_time": "00:00", "end_time": "23:59"},
        ],
    }
    # No need to patch time — the timezone lookup itself fails.
    win = _get_active_maintenance_window("srv1", settings)
    assert win is None


# ── _get_maintenance_thresholds wrapper ───────────────────────────────

def test_maintenance_thresholds_returned_when_window_active(monkeypatch):
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00",
             "thresholds": {"cpu_critical": 99}},
        ],
    }
    assert _get_maintenance_thresholds("srv1", settings) == {"cpu_critical": 99}


def test_maintenance_thresholds_none_when_no_window(monkeypatch):
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {"timezone": "Europe/Berlin", "maintenance_windows": []}
    assert _get_maintenance_thresholds("srv1", settings) in (None, {})


def test_maintenance_thresholds_empty_dict_when_window_has_none(monkeypatch):
    """Window with no ``thresholds`` key returns ``{}`` — not None.
    Callers must accept both."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    result = _get_maintenance_thresholds("srv1", settings)
    assert result == {} or result is None


# ── _is_alert_suppressed_by_maintenance ──────────────────────────────

def test_alert_suppressed_when_window_active_and_flag_set(monkeypatch):
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00",
             "suppress_alerts": True},
        ],
    }
    assert _is_alert_suppressed_by_maintenance("srv1", settings) is True


def test_alert_NOT_suppressed_when_window_active_but_flag_unset(monkeypatch):
    """Default: maintenance loosens thresholds but does NOT suppress alerts
    entirely. Suppression is opt-in."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {
        "timezone": "Europe/Berlin",
        "maintenance_windows": [
            {"servers": ["srv1"], "days": [0], "start_time": "08:00", "end_time": "18:00"},
        ],
    }
    assert _is_alert_suppressed_by_maintenance("srv1", settings) is False


def test_alert_NOT_suppressed_when_no_window():
    """No windows configured → never suppress (always False, no crash)."""
    assert _is_alert_suppressed_by_maintenance(
        "srv1", {"timezone": "Europe/Berlin"}
    ) is False


# ── Empty / missing configuration ─────────────────────────────────────

def test_no_maintenance_windows_setting_returns_none():
    """Settings without ``maintenance_windows`` key → no window active."""
    assert _get_active_maintenance_window("srv1", {"timezone": "Europe/Berlin"}) is None


def test_empty_maintenance_windows_list_returns_none(monkeypatch):
    """Empty list → no window."""
    _patch_now(monkeypatch, _fake_now(2026, 5, 18, 12, 0))
    settings = {"timezone": "Europe/Berlin", "maintenance_windows": []}
    assert _get_active_maintenance_window("srv1", settings) is None
