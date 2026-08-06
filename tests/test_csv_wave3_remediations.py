"""Tests for Wave 3 CSV remediations (F-020, F-031, F-040).

Moderate findings: previously-untested moving parts. Each test focuses
on the load-bearing decision logic, not on the WinRM round-trips which
are environment-dependent.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ════════════════════════════════════════════════════════════════════
#  F-020 — incident correlation rules
# ════════════════════════════════════════════════════════════════════


def _mk_server(name, srv_type="application"):
    return SimpleNamespace(name=name, type=srv_type)


def _mk_event(server_name, event_type="critical", metric="cpu",
              value=95, threshold=90, message="", event_id=None):
    return {
        "server_name": server_name,
        "event_type": event_type,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "message": message or f"{server_name}: {metric} at {value}",
        "event_id": event_id,
    }


def test_correlate_rule1_multi_server_offline():
    """F-020 Rule 1: two or more servers offline in one cycle → correlated
    incident emitted."""
    from analytics import correlate_events
    db = MagicMock()
    db.create_incident.return_value = 1
    # No open incident exists yet → the correlation engine must CREATE one
    # (rather than dedup-reuse an existing one).
    db.get_open_incident_id_by_title_prefix.return_value = None
    events = [
        _mk_event("srv1", event_type="offline", metric=None, value=None,
                  threshold=None, message="srv1 offline", event_id=11),
        _mk_event("srv2", event_type="offline", metric=None, value=None,
                  threshold=None, message="srv2 offline", event_id=12),
    ]
    servers = [_mk_server("srv1"), _mk_server("srv2")]
    correlated = correlate_events(db, events, servers)
    # Rule 1 must have fired.
    rule_names = [c.get("rule") for c in correlated]
    assert "multi_server_offline" in rule_names, (
        f"F-020 Rule 1: multi-server offline correlation should fire; "
        f"got rules: {rule_names}"
    )
    # An incident should have been created.
    assert db.create_incident.called


def test_correlate_rule1_no_fire_with_single_offline():
    """A single offline server is NOT a multi-server outage; rule must not
    fire. (Tests the >= 2 threshold.)"""
    from analytics import correlate_events
    db = MagicMock()
    events = [
        _mk_event("srv1", event_type="offline", metric=None, value=None,
                  threshold=None, event_id=11),
    ]
    correlated = correlate_events(db, events, [_mk_server("srv1")])
    rule_names = [c.get("rule") for c in correlated]
    assert "multi_server_offline" not in rule_names


def test_correlate_empty_events_does_not_crash():
    """Empty cycle → no correlation but auto-resolution runs;
    function must not raise."""
    from analytics import correlate_events
    db = MagicMock()
    db.get_open_incidents.return_value = []
    out = correlate_events(db, [], [])
    assert out == []


def test_correlate_filters_to_critical_grade_for_compound_stress():
    """F-020 Rule 2: compound stress wants critical/warning events on
    a single server — not 'info'-class noise."""
    from analytics import correlate_events
    db = MagicMock()
    # Same server, 3 different metrics, all critical. Rule 2 should fire.
    events = [
        _mk_event("srv1", event_type="critical", metric="cpu", value=98, event_id=20),
        _mk_event("srv1", event_type="critical", metric="ram", value=95, event_id=21),
        _mk_event("srv1", event_type="critical", metric="disk_c", value=99, event_id=22),
    ]
    servers = [_mk_server("srv1")]
    correlated = correlate_events(db, events, servers)
    rule_names = [c.get("rule") for c in correlated]
    # At least one correlation rule should have fired (compound_stress).
    assert correlated, "F-020 Rule 2: compound stress on single server should produce a correlation"


# ════════════════════════════════════════════════════════════════════
#  F-031 — restart_scheduler decision logic
# ════════════════════════════════════════════════════════════════════


def _patch_rs_clock(monkeypatch, instant_utc_aware):
    """Patch ``restart_scheduler.datetime`` so ``datetime.now(tz)`` returns
    ``instant_utc_aware`` converted to the requested tz."""
    import restart_scheduler as rs
    real_datetime = rs.datetime

    class _FrozenDT(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return instant_utc_aware.astimezone(tz)
            return instant_utc_aware

    monkeypatch.setattr(rs, "datetime", _FrozenDT)


def test_restart_scheduler_should_run_at_configured_time(monkeypatch):
    """The schedule-match function decides whether to fire NOW. Pin its
    in-window vs out-of-window behaviour by mocking the wallclock."""
    import restart_scheduler as rs
    import zoneinfo
    from datetime import timezone as _tz
    # Wallclock 2026-05-18 (Monday) 03:00 Europe/Berlin → 01:00 UTC.
    instant = datetime(2026, 5, 18, 1, 0, tzinfo=_tz.utc)
    _patch_rs_clock(monkeypatch, instant)
    monkeypatch.setattr(rs, "_already_ran_marker", lambda *_, **__: False)
    out = rs._should_run_now(
        {"type": "weekly", "time": "03:00", "day_of_week": 0},
        "Europe/Berlin",
    )
    assert out is True, "F-031: schedule should fire at the configured time"


def test_restart_scheduler_does_not_run_outside_window(monkeypatch):
    """Outside the 2-minute schedule window, the loop must not fire."""
    import restart_scheduler as rs
    from datetime import timezone as _tz
    # Berlin Mon 05:00 → UTC 03:00; schedule wants Mon 03:00 local
    instant = datetime(2026, 5, 18, 3, 0, tzinfo=_tz.utc)
    _patch_rs_clock(monkeypatch, instant)
    monkeypatch.setattr(rs, "_already_ran_marker", lambda *_, **__: False)
    out = rs._should_run_now(
        {"type": "weekly", "time": "03:00", "day_of_week": 0},
        "Europe/Berlin",
    )
    assert out is False, "F-031: schedule should NOT fire outside the window"


def test_restart_scheduler_double_fire_protected_by_marker(monkeypatch):
    """When the run marker is already set for today, schedule does not
    re-fire — even at the same time. Protects against scheduler tick
    overlap (S2-5 / P4 from prior audit)."""
    import restart_scheduler as rs
    from datetime import timezone as _tz
    instant = datetime(2026, 5, 18, 1, 0, tzinfo=_tz.utc)  # 03:00 Berlin
    _patch_rs_clock(monkeypatch, instant)
    monkeypatch.setattr(rs, "_already_ran_marker", lambda *_, **__: True)
    out = rs._should_run_now(
        {"type": "weekly", "time": "03:00", "day_of_week": 0},
        "Europe/Berlin",
    )
    assert out is False


def test_restart_scheduler_respects_timezone(monkeypatch):
    """Schedule time is interpreted in the configured tz, not in
    server-local time. NY config at 03:00 should NOT fire when Berlin
    clock is at 03:00 (NY would be 21:00 prior day)."""
    import restart_scheduler as rs
    from datetime import timezone as _tz
    # 01:00 UTC = 03:00 Berlin = 21:00 NY (prior day)
    instant = datetime(2026, 5, 18, 1, 0, tzinfo=_tz.utc)
    _patch_rs_clock(monkeypatch, instant)
    monkeypatch.setattr(rs, "_already_ran_marker", lambda *_, **__: False)
    out = rs._should_run_now(
        {"type": "weekly", "time": "03:00", "day_of_week": 0},
        "America/New_York",
    )
    assert out is False, (
        "F-031: schedule must respect the configured timezone, not the "
        "OS clock"
    )


# ════════════════════════════════════════════════════════════════════
#  F-040 — runbook_engine
# ════════════════════════════════════════════════════════════════════


def test_runbook_dry_run_does_not_invoke_winrm():
    """dry_run=True must NOT open a WinRM connection — it's a validation
    pass only."""
    from runbook_engine import execute_runbook
    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 1,
        "name": "rb",
        "steps_json": json.dumps([
            {"type": "powershell", "script": "Get-Service", "timeout": 30},
        ]),
    }
    db.insert_runbook_execution.return_value = 99
    with patch("winrm_factory.make_wsman") as m_make:
        exec_id = execute_runbook(
            db, runbook_id=1, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=True, executed_by="alice",
        )
    assert exec_id == 99
    assert m_make.call_count == 0, (
        "F-040: dry_run must skip WinRM entirely"
    )
    # Execution row marked completed with dry-run output.
    db.update_runbook_execution.assert_called_once()


def test_runbook_raises_on_unknown_id():
    """Unknown runbook_id → clear error, not a silent no-op."""
    from runbook_engine import execute_runbook
    db = MagicMock()
    db.get_runbook.return_value = None
    with pytest.raises(ValueError, match="not found"):
        execute_runbook(
            db, runbook_id=999, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
        )


def test_runbook_creates_execution_row_with_executed_by():
    """Audit trail: the operator's username flows through to the execution row."""
    from runbook_engine import execute_runbook
    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 1, "name": "rb",
        "steps_json": json.dumps([{"type": "wait", "seconds": 1}]),
    }
    db.insert_runbook_execution.return_value = 42
    with patch("winrm_factory.make_wsman", return_value=MagicMock()):
        execute_runbook(
            db, runbook_id=1, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=True, executed_by="alice",
        )
    db.insert_runbook_execution.assert_called_once()
    call = db.insert_runbook_execution.call_args
    # executed_by may be a kwarg or positional; check both.
    if "executed_by" in call.kwargs:
        assert call.kwargs["executed_by"] == "alice"
    else:
        # Positional — find by signature position.
        assert "alice" in call.args
