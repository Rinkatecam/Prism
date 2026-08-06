"""Tests for ``failed_logins._collect_all_failed_logins`` — F-015 remediation.

The failed-login spike detector is security-critical: it must catch
brute-force / spray attacks in time for the operator to respond. The
key behaviours under test:

  * Count >= threshold → emit ``warning`` event
  * Count >= 2× threshold → emit ``critical`` event
  * Account lockout (Event 4740) → emit immediate ``critical`` event
  * Maintenance window with suppress_alerts=True → skip alerting entirely
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _server(name="srv1", host="srv1.local"):
    return SimpleNamespace(
        name=name, host=host, username="u", password="p",
    )


def _settings(threshold=10, lockout_alert=True, email_enabled=False,
              webhook_enabled=False, suppress=False):
    return {
        "security_alerts": {
            "login_failure_threshold": threshold,
            "lockout_alert": lockout_alert,
        },
        "email": {"enabled": email_enabled},
        "webhooks": {"enabled": webhook_enabled},
        # No maintenance windows by default (suppress=False).
        "maintenance_windows": ([
            {
                "servers": ["srv1"], "days": [0, 1, 2, 3, 4, 5, 6],
                "start_time": "00:00", "end_time": "23:59",
                "suppress_alerts": True,
            },
        ] if suppress else []),
        "timezone": "Europe/Berlin",
    }


def _setup_mocks(failed_login_count, sample_events=None):
    """Build the (db, mock_pypsrp) test harness.

    ``failed_login_count`` is what ``db.get_failed_login_count`` returns
    when the production code asks 'how many fails in last 15 min'.
    ``sample_events`` is what the (mocked) PS payload returns — a list
    of dicts with at least ``event_id``.
    """
    db = MagicMock()
    db.get_failed_login_count.return_value = failed_login_count
    db.insert_failed_logins.return_value = None
    db.insert_event.return_value = None

    fake_events = sample_events if sample_events is not None else []
    # Mock the WinRM stack: PowerShell+RunspacePool import path
    fake_ps_output = ['[]' if not fake_events else __import__('json').dumps(fake_events)]
    return db, fake_ps_output


def _patches(fake_ps_output):
    """Return a context-manager that mocks pypsrp.powershell + winrm_factory."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        mock_ps = MagicMock()
        mock_ps.invoke.return_value = fake_ps_output
        mock_ps.had_errors = False
        mock_ps.add_script.return_value = mock_ps
        mock_pool = MagicMock()
        mock_pool.__enter__.return_value = mock_pool
        mock_pool.__exit__.return_value = False
        with patch("pypsrp.powershell.PowerShell", return_value=mock_ps), \
             patch("pypsrp.powershell.RunspacePool", return_value=mock_pool), \
             patch("winrm_factory.make_wsman", return_value=MagicMock()):
            yield
    return _ctx()


# ── threshold escalation ─────────────────────────────────────────────

def test_spike_warning_when_count_meets_threshold():
    """count == threshold (10) → warning event emitted."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=10, sample_events=[
        {"event_id": "4625", "timestamp": "2026-05-22T12:00:00Z",
         "source_ip": "1.2.3.4", "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(db, [_server()], _settings(threshold=10))
    # Look at db.insert_event calls — should have ONE call with severity=warning
    # for metric=failed_logins.
    spike_calls = [
        c for c in db.insert_event.call_args_list
        if len(c.args) >= 3 and c.args[2] == "failed_logins"
    ]
    assert len(spike_calls) == 1, f"expected 1 spike event, got {len(spike_calls)}: {spike_calls}"
    severity_arg = spike_calls[0].args[1]
    assert severity_arg == "warning", f"expected warning, got {severity_arg}"


def test_spike_critical_when_count_meets_two_x_threshold():
    """count == 2 × threshold → critical event."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=20, sample_events=[
        {"event_id": "4625", "timestamp": "2026-05-22T12:00:00Z",
         "source_ip": "1.2.3.4", "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(db, [_server()], _settings(threshold=10))
    spike_calls = [
        c for c in db.insert_event.call_args_list
        if len(c.args) >= 3 and c.args[2] == "failed_logins"
    ]
    assert len(spike_calls) == 1
    assert spike_calls[0].args[1] == "critical"


def test_spike_no_event_below_threshold():
    """count < threshold → no spike event."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=5, sample_events=[
        {"event_id": "4625", "timestamp": "2026-05-22T12:00:00Z",
         "source_ip": "1.2.3.4", "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(db, [_server()], _settings(threshold=10))
    spike_calls = [
        c for c in db.insert_event.call_args_list
        if len(c.args) >= 3 and c.args[2] == "failed_logins"
    ]
    assert not spike_calls


# ── account lockout (Event 4740) ─────────────────────────────────────

def test_account_lockout_fires_critical_event():
    """Any 4740 event → immediate critical, regardless of threshold."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=0, sample_events=[
        {"event_id": "4740", "timestamp": "2026-05-22T12:00:00Z",
         "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(db, [_server()], _settings(threshold=10))
    lockout_calls = [
        c for c in db.insert_event.call_args_list
        if len(c.args) >= 3 and c.args[2] == "account_lockout"
    ]
    assert len(lockout_calls) == 1
    assert lockout_calls[0].args[1] == "critical"
    assert "alice" in lockout_calls[0].args[5]  # message contains account


def test_account_lockout_not_fired_when_lockout_alert_disabled():
    """Setting ``lockout_alert: false`` suppresses the immediate alert."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=0, sample_events=[
        {"event_id": "4740", "timestamp": "2026-05-22T12:00:00Z",
         "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(
            db, [_server()], _settings(threshold=10, lockout_alert=False),
        )
    lockout_calls = [
        c for c in db.insert_event.call_args_list
        if len(c.args) >= 3 and c.args[2] == "account_lockout"
    ]
    assert not lockout_calls


# ── maintenance window suppression ───────────────────────────────────

def test_maintenance_window_with_suppress_alerts_skips_server_entirely():
    """When a suppress_alerts=True window is active, the server is
    skipped before WinRM is even opened — saving connection time AND
    preventing event emission."""
    from failed_logins import _collect_all_failed_logins
    db, fake_ps = _setup_mocks(failed_login_count=99, sample_events=[
        {"event_id": "4625", "timestamp": "2026-05-22T12:00:00Z",
         "source_ip": "1.2.3.4", "account_name": "alice"},
    ])
    with _patches(fake_ps):
        _collect_all_failed_logins(db, [_server()], _settings(threshold=10, suppress=True))
    # Nothing should be inserted at all.
    assert db.insert_event.call_count == 0
    assert db.insert_failed_logins.call_count == 0


# ── failure isolation across servers ─────────────────────────────────

def test_one_bad_server_does_not_block_others():
    """If WinRM fails for server A, server B must still be processed.
    The function wraps per-server work in try/except."""
    from failed_logins import _collect_all_failed_logins
    db = MagicMock()
    db.get_failed_login_count.return_value = 0
    servers = [_server("bad"), _server("good")]
    settings = _settings(threshold=10)

    # First call to make_wsman raises, second succeeds.
    mock_ps = MagicMock()
    mock_ps.invoke.return_value = ["[]"]
    mock_ps.had_errors = False
    mock_ps.add_script.return_value = mock_ps
    mock_pool = MagicMock()
    mock_pool.__enter__.return_value = mock_pool
    mock_pool.__exit__.return_value = False
    call_count = {"n": 0}
    def _make(*_, **__):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated WinRM failure on first server")
        return MagicMock()
    with patch("pypsrp.powershell.PowerShell", return_value=mock_ps), \
         patch("pypsrp.powershell.RunspacePool", return_value=mock_pool), \
         patch("winrm_factory.make_wsman", side_effect=_make):
        _collect_all_failed_logins(db, servers, settings)
    # Even though the first server raised, the second should have been
    # attempted (call_count == 2).
    assert call_count["n"] == 2
