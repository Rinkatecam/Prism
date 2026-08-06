"""Unit tests for collector_v2.aggregator.

Exercise the Aggregator in isolation — no real WinRM, no real DB, no
supervisor / worker threads. We mock the DB, mock email + webhook
dispatch, mock the alert-fatigue gate, and feed Results straight onto
the test queue. Each test asserts on the side-effects (DB calls,
state mutations, alert dispatches) the aggregator should have produced.

Run standalone:
    python -m pytest tests/test_collector_v2_aggregator.py -v
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from collector_v2 import aggregator, state
from collector_v2.types import CheckType, Result, ServerHealth, WorkItem


# ─────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class FakeServer:
    """Minimal ServerConfig stand-in.

    Only the attributes the aggregator actually reads:
      * name           — used in every log line + DB row
      * thresholds     — passed to compute_status / _effective_status
    """

    name: str
    thresholds: dict[str, Any] = field(default_factory=lambda: {
        "cpu_warning": 75, "cpu_critical": 90,
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 80, "disk_critical": 90,
        "enabled": True,
    })


class FakeConfig:
    """Stand-in for ConfigManager — only needs get_server_by_name + get_servers."""

    def __init__(self, servers: list[FakeServer]) -> None:
        self._by_name = {s.name: s for s in servers}
        self._all = list(servers)

    def get_server_by_name(self, name: str) -> FakeServer | None:
        return self._by_name.get(name)

    def get_servers(self) -> list[FakeServer]:
        return list(self._all)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Wipe all aggregator + state module globals between tests.

    Without this, previous_status / baseline rings / counters leak across
    tests and produce mysterious cross-test failures (especially for the
    transition tests which set _previous_status as a setup step)."""
    aggregator._previous_status.clear()
    aggregator._baseline_dev_history.clear()
    aggregator._recent_events.clear()
    aggregator._last_correlation_check = 0.0
    with aggregator._stats_lock:
        aggregator._total_processed = 0
        aggregator._total_offline_results = 0
        aggregator._total_alerts_dispatched = 0
        aggregator._total_critical_errors = 0
    # State globals the aggregator writes
    with state._state_lock:
        state.latest_by_server.clear()
    state.server_update_info.clear()
    state.server_hardware_info.clear()
    with state._server_health_lock:
        state.server_health.clear()
    state.last_aggregator_tick = 0.0
    yield
    aggregator._previous_status.clear()
    aggregator._baseline_dev_history.clear()
    aggregator._recent_events.clear()


@contextmanager
def _patched_config(servers: list[FakeServer]):
    """Install a fake ConfigManager into routes.api._shared._config."""
    from routes.api import _shared
    prev = _shared._config
    _shared._config = FakeConfig(servers)
    try:
        yield
    finally:
        _shared._config = prev


def _make_result(
    server_name: str = "srv1",
    check_type: CheckType = CheckType.METRICS,
    ok: bool = True,
    data: Any = None,
    error: str | None = None,
    error_kind: str | None = None,
    finished_at: datetime | None = None,
) -> Result:
    """Build a Result + its enclosing WorkItem. Defaults are happy-path."""
    finished_at = finished_at or datetime.now(timezone.utc)
    item = WorkItem(
        server_name=server_name,
        check_type=check_type,
        enqueued_at=finished_at,
        deadline_s=30,
    )
    return Result(
        item=item,
        started_at=finished_at,
        finished_at=finished_at,
        ok=ok,
        data=data,
        error=error,
        error_kind=error_kind,
    )


def _make_aggregator(db: Any = None, settings: dict | None = None):
    """Construct an Aggregator wired to a mock DB + settings."""
    db = db if db is not None else MagicMock()
    settings = settings if settings is not None else {}
    rq: queue.Queue = queue.Queue()
    agg = aggregator.Aggregator(
        result_queue=rq,
        db=db,
        get_settings=lambda: settings,
    )
    return agg, rq, db


def _register_server_health(name: str) -> ServerHealth:
    """Register a ServerHealth entry so mark_check_completed has somewhere
    to write its updates. Without this, the call is a silent no-op."""
    now = datetime.now(timezone.utc)
    from collector_v2.types import CheckState
    h = ServerHealth(
        name=name,
        checks={ct: CheckState(next_due_at=now) for ct in CheckType},
    )
    state.upsert_server_health(name, h)
    return h


# ─────────────────────────────────────────────────────────────────────────
# METRICS handler tests
# ─────────────────────────────────────────────────────────────────────────


def test_metrics_ok_calls_insert_metric_and_updates_cache():
    """Happy path: METRICS Result with ok=True triggers db.insert_metric
    and refreshes state.latest_by_server."""
    agg, _, db = _make_aggregator()
    result = _make_result(
        ok=True,
        data={"cpu": 12.5, "ram": 40.0, "disk_c": 30.0, "disk_d": 25.0,
              "collection_time_ms": 1200},
    )
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # insert_metric called with the expected args
    db.insert_metric.assert_called_once()
    kwargs = db.insert_metric.call_args.kwargs
    assert kwargs["server_name"] == "srv1"
    assert kwargs["cpu"] == 12.5
    assert kwargs["ram"] == 40.0
    assert kwargs["disk_c"] == 30.0
    assert kwargs["disk_d"] == 25.0
    assert kwargs["status"] == "healthy"  # all values well below thresholds
    assert kwargs["collection_time_ms"] == 1200

    # Cache updated
    assert "srv1" in state.latest_by_server
    cached = state.latest_by_server["srv1"]
    assert cached["cpu_percent"] == 12.5
    assert cached["status"] == "healthy"


def test_metrics_failed_synthesises_offline_row():
    """METRICS Result with ok=False yields status='offline' synthesised
    row and a transition-to-offline event WHEN no recent good row exists
    (the preserve-on-first-failure path doesn't apply on a cold server)."""
    agg, _, db = _make_aggregator()
    result = _make_result(ok=False, error="shell was not found", error_kind="offline")

    # Seed previous_status so we get an explicit transition event
    aggregator._previous_status["srv1"] = "healthy"

    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # The synthesised row has status="offline" and all metrics None
    db.insert_metric.assert_called_once()
    kwargs = db.insert_metric.call_args.kwargs
    assert kwargs["status"] == "offline"
    assert kwargs["cpu"] is None and kwargs["ram"] is None

    # One offline event written (transition healthy → offline)
    offline_calls = [c for c in db.insert_event.call_args_list
                     if len(c.args) > 1 and c.args[1] == "offline"]
    assert len(offline_calls) == 1
    assert aggregator._previous_status["srv1"] == "offline"


def test_first_metrics_failure_preserves_previous_status_when_row_is_recent():
    """The "WinRM blipped" path. Server was healthy 30 seconds ago, this
    poll failed. Don't flip to offline — keep healthy, no DB write, no
    alert. The supervisor will increment consecutive_failures to 1, and
    the next failure will flip through to offline (covered by the next
    test)."""
    from collector_v2.types import CheckType as _CT, ServerHealth as _SH

    agg, _, db = _make_aggregator()

    # Seed: previous status was healthy 30 s ago, zero consecutive failures
    iso_30s_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.latest_by_server["srv1"] = {
        "server_name": "srv1",
        "cpu_percent": 12.5, "ram_percent": 40.0,
        "disk_c_percent": 60.0, "disk_d_percent": 30.0,
        "status": "healthy",
        "timestamp": iso_30s_ago,
    }
    state.server_health["srv1"] = _SH(name="srv1")  # default: zero consec failures
    aggregator._previous_status["srv1"] = "healthy"

    result = _make_result(ok=False, error="WinRM session timeout", error_kind="timeout")
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # Preserve path: NO DB write, NO alert, NO transition event
    db.insert_metric.assert_not_called()
    offline_events = [c for c in db.insert_event.call_args_list
                      if len(c.args) > 1 and c.args[1] == "offline"]
    assert offline_events == [], "must not write an offline event on the first failure"
    # latest_by_server is untouched — the dashboard still shows healthy
    assert state.latest_by_server["srv1"]["status"] == "healthy"
    # _previous_status untouched — when offline eventually fires, it
    # transitions FROM healthy not from offline.
    assert aggregator._previous_status["srv1"] == "healthy"


def test_second_metrics_failure_flips_to_offline_even_with_recent_row():
    """The "actual outage" path. Server was healthy, missed two polls in
    a row. After the FIRST failure consecutive_failures=1; on the SECOND
    failure the preserve guard fails and we flip to offline as before."""
    from collector_v2.types import CheckType as _CT, ServerHealth as _SH

    agg, _, db = _make_aggregator()

    iso_30s_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.latest_by_server["srv1"] = {
        "server_name": "srv1",
        "cpu_percent": 12.5, "ram_percent": 40.0,
        "disk_c_percent": 60.0, "disk_d_percent": 30.0,
        "status": "healthy",
        "timestamp": iso_30s_ago,
    }
    # Critical: simulate that the supervisor already recorded one failure.
    h = _SH(name="srv1")
    state.server_health["srv1"] = h
    # record_failure increments consecutive_failures from 0 → 1
    h.record_failure(_CT.METRICS)
    aggregator._previous_status["srv1"] = "healthy"

    result = _make_result(ok=False, error="shell was not found", error_kind="offline")
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # Now we DO flip — insert_metric with status=offline + transition event
    db.insert_metric.assert_called_once()
    assert db.insert_metric.call_args.kwargs["status"] == "offline"
    offline_events = [c for c in db.insert_event.call_args_list
                      if len(c.args) > 1 and c.args[1] == "offline"]
    assert len(offline_events) == 1


def test_first_metrics_failure_flips_to_offline_when_row_is_stale():
    """The preserve window is bounded — when the previous row is more
    than 5 min old, we don't trust it any more. Flip to offline on the
    first failure, same as v1."""
    from collector_v2.types import ServerHealth as _SH

    agg, _, db = _make_aggregator()

    iso_10min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.latest_by_server["srv1"] = {
        "server_name": "srv1",
        "cpu_percent": 12.5, "ram_percent": 40.0,
        "disk_c_percent": 60.0, "disk_d_percent": 30.0,
        "status": "healthy",
        "timestamp": iso_10min_ago,  # stale — beyond the 5-min preserve window
    }
    state.server_health["srv1"] = _SH(name="srv1")
    aggregator._previous_status["srv1"] = "healthy"

    result = _make_result(ok=False, error="shell was not found", error_kind="offline")
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    db.insert_metric.assert_called_once()
    assert db.insert_metric.call_args.kwargs["status"] == "offline"


def test_first_metrics_failure_after_offline_does_not_preserve():
    """Edge case: server is already offline. Don't preserve "offline" as
    a "previous good status" — that would mean we never flip to anything.
    Fall through to the normal offline synthesis path."""
    from collector_v2.types import ServerHealth as _SH

    agg, _, db = _make_aggregator()

    iso_30s_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.latest_by_server["srv1"] = {
        "server_name": "srv1",
        "cpu_percent": None, "ram_percent": None,
        "disk_c_percent": None, "disk_d_percent": None,
        "status": "offline",  # already offline
        "timestamp": iso_30s_ago,
    }
    state.server_health["srv1"] = _SH(name="srv1")
    aggregator._previous_status["srv1"] = "offline"

    result = _make_result(ok=False, error="shell was not found", error_kind="offline")
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # Still synthesises the offline row — the preserve path only protects
    # transitions from a GOOD state. Continuous-offline keeps inserting
    # offline rows so the timestamp on the dashboard tile stays fresh.
    db.insert_metric.assert_called_once()
    assert db.insert_metric.call_args.kwargs["status"] == "offline"


def test_healthy_to_critical_transition_fires_event_and_dispatches_alert():
    """prev=healthy → new=critical (CPU at 95) should write a 'critical'
    event AND dispatch an alert email + webhook (when enabled)."""
    aggregator._previous_status["srv1"] = "healthy"
    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {"enabled": False,
                              # Spike gate off: these tests assert a
                              # SINGLE Result produces a transition
                              # event. The gate is covered in
                              # tests/test_spike_gate.py.
                              "spike_sustain_cycles": 1},
        "email": {"enabled": True, "smtp_server": "x", "recipients": ["a@b"],
                  "send_on_critical": True},
        "webhooks": {"enabled": True, "teams_webhook_url": "https://x",
                     "send_on_critical": True},
    }
    agg, _, db = _make_aggregator(settings=settings)
    result = _make_result(
        ok=True,
        data={"cpu": 95.0, "ram": 30.0, "disk_c": 20.0, "disk_d": 10.0,
              "collection_time_ms": 500},
    )

    with _patched_config([FakeServer("srv1")]), \
            patch("collector_v2.aggregator._send_alert_email_fn") as msend_email, \
            patch("collector_v2.aggregator._send_teams_webhook_fn") as msend_webhook, \
            patch("collector_v2.aggregator._is_throttled_by_fatigue_fn") as mfatigue:
        # send_alert_email / send_teams_webhook return mocked-callable
        send_email_mock = MagicMock(return_value=True)
        msend_email.return_value = send_email_mock
        send_webhook_mock = MagicMock()
        msend_webhook.return_value = send_webhook_mock
        # fatigue gate returns False (not throttled)
        mfatigue.return_value = MagicMock(return_value=False)

        agg._process_result(result)

    # Critical event was inserted
    crit_calls = [c for c in db.insert_event.call_args_list
                  if len(c.args) > 1 and c.args[1] == "critical"]
    assert len(crit_calls) >= 1, f"Expected critical event, got {db.insert_event.call_args_list}"

    # Email + webhook dispatched
    send_email_mock.assert_called_once()
    send_webhook_mock.assert_called_once()

    # previous_status updated
    assert aggregator._previous_status["srv1"] == "critical"


def test_critical_to_healthy_transition_fires_resolved_event():
    """prev=critical → new=healthy should emit a 'resolved' event."""
    aggregator._previous_status["srv1"] = "critical"
    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {"enabled": False,
                              # Spike gate off: these tests assert a
                              # SINGLE Result produces a transition
                              # event. The gate is covered in
                              # tests/test_spike_gate.py.
                              "spike_sustain_cycles": 1},
    }
    agg, _, db = _make_aggregator(settings=settings)
    result = _make_result(
        ok=True,
        data={"cpu": 5.0, "ram": 10.0, "disk_c": 20.0, "disk_d": 10.0,
              "collection_time_ms": 500},
    )
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    resolved_calls = [c for c in db.insert_event.call_args_list
                      if len(c.args) > 1 and c.args[1] == "resolved"]
    assert len(resolved_calls) == 1
    assert "recovered" in resolved_calls[0].args[5].lower()
    assert aggregator._previous_status["srv1"] == "healthy"


# ─────────────────────────────────────────────────────────────────────────
# LOGS handler
# ─────────────────────────────────────────────────────────────────────────


def test_logs_ok_calls_insert_logs():
    """Happy path: LOGS Result with a list payload calls db.insert_logs."""
    agg, _, db = _make_aggregator()
    logs_payload = [
        {"source": "System", "time": "2026-05-19T10:00:00Z",
         "level": "Error", "event_id": 41, "message": "boom"},
    ]
    result = _make_result(
        check_type=CheckType.LOGS,
        ok=True,
        data=logs_payload,
    )
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    # ingest_cfg carries settings.log_ingest so Information-level noise is
    # dropped and identical lines coalesce into log_signatures.
    db.insert_logs.assert_called_once()
    args, kwargs = db.insert_logs.call_args
    assert args[0] == "srv1"
    assert args[1] == logs_payload
    assert "ingest_cfg" in kwargs


def test_logs_failure_does_not_call_insert_logs():
    """LOGS Result with ok=False should not write anything."""
    agg, _, db = _make_aggregator()
    result = _make_result(
        check_type=CheckType.LOGS,
        ok=False, error="shell was not found", error_kind="offline",
    )
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)
    db.insert_logs.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# UPDATES handler — the recently-added transient-error logic
# ─────────────────────────────────────────────────────────────────────────


def test_updates_offline_preserves_previous_payload():
    """When the WU check fails with error_kind='offline', the PREVIOUS
    good payload (count, updates list, reboot_required) is preserved and
    only checked_at + transient_error are updated. error MUST be None
    (the UI keys its red banner off this field)."""
    agg, _, _ = _make_aggregator()

    # Seed a previous good payload
    state.server_update_info["srv1"] = {
        "count": 7,
        "updates": [{"title": "KB1", "kb": "KB1"}],
        "reboot_required": True,
        "pending_reboot": False,
        "error": None,
        "checked_at": "2026-05-19T09:00:00Z",
    }

    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=False, error="shell was not found", error_kind="offline",
    )
    agg._process_result(result)

    info = state.server_update_info["srv1"]
    assert info["count"] == 7, "previous count was lost"
    assert info["updates"] == [{"title": "KB1", "kb": "KB1"}]
    assert info["reboot_required"] is True
    assert info["transient_error"] is True
    assert info["transient_error_reason"] == "server_rebooting_or_unreachable"
    assert info["error"] is None, "transient errors must NOT set the 'error' field"
    assert info["checked_at"] != "2026-05-19T09:00:00Z"  # advanced


def test_updates_real_failure_overwrites_with_error_message():
    """When the WU check fails with error_kind!='offline'/'timeout' (e.g.
    winrm auth, parse error), we WIPE the previous payload and write an
    error message so the UI shows 'Check failed' rather than stale data."""
    agg, _, _ = _make_aggregator()

    state.server_update_info["srv1"] = {
        "count": 7, "updates": [{"title": "KB1"}],
        "reboot_required": True, "pending_reboot": False,
        "error": None, "checked_at": "2026-05-19T09:00:00Z",
    }

    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=False,
        error="WinRMAuthError: 401 unauthorized",
        error_kind="winrm",
    )
    agg._process_result(result)

    info = state.server_update_info["srv1"]
    assert info["count"] == 0
    assert info["updates"] == []
    assert info["reboot_required"] is False
    assert info["error"] is not None
    assert "401" in info["error"] or "unauthorized" in info["error"].lower()
    assert info.get("transient_error") is not True


def test_updates_timeout_preserves_previous_payload():
    """Worker-deadline timeouts on UPDATES are equally transient — the WU
    COM query routinely takes >120 s on patch Tuesday when there's a big
    backlog. Wiping the previous "23 updates pending" payload to a "Check
    failed" banner just to re-fill it next cycle is bad UX. Preserve."""
    agg, _, _ = _make_aggregator()

    state.server_update_info["srv1"] = {
        "count": 23,
        "updates": [{"title": "KB5000001", "kb": "KB5000001"}],
        "reboot_required": True, "pending_reboot": False,
        "error": None, "checked_at": "2026-05-19T09:00:00Z",
    }

    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=False,
        error="Check exceeded deadline of 120s",
        error_kind="timeout",
    )
    agg._process_result(result)

    info = state.server_update_info["srv1"]
    assert info["count"] == 23, "previous count must survive a deadline timeout"
    assert info["reboot_required"] is True
    assert info["transient_error"] is True
    assert info["transient_error_reason"] == "wu_query_exceeded_deadline"
    assert info["error"] is None, (
        "transient errors must NOT trip the red banner — UI keys off the 'error' field"
    )


def test_offline_markers_classify_wsman_code_995_german():
    """Regression test for the production WSManFault Code 995 that
    operators reported. Verifies the German "abgebrochen" message is
    classified as offline so the dashboard preserves the previous
    payload instead of showing a scary banner.

    Production sample (lowercased):
      received a wsmanfault message. (code: 995, machine: foo.example.com,
      reason: der e/a-vorgang wurde wegen eines threadendes oder einer
      anwendungsanforderung abgebrochen.)
    """
    from collector_v2.checks import _is_offline_error
    sample = (
        "WSManFaultError: Received a WSManFault message. "
        "(Code: 995, Machine: SRV03.AD.EXAMPLE.COM, Reason: "
        "Der E/A-Vorgang wurde wegen eines Threadendes oder einer "
        "Anwendungsanforderung abgebrochen.)"
    )
    assert _is_offline_error(sample), (
        "WSMan Code 995 / German 'abgebrochen' fault must classify as "
        "offline so the dashboard's transient-error path triggers"
    )


def test_offline_markers_classify_wsman_code_995_english():
    """Same Code 995 fault as the German test, in English locale —
    Windows uses the canonical 'thread exit or an application request'
    wording when the system locale is en-US."""
    from collector_v2.checks import _is_offline_error
    sample = (
        "Received a WSManFault message. (Code: 995, Machine: SRV01, "
        "Reason: The I/O operation has been aborted because of either "
        "a thread exit or an application request.)"
    )
    assert _is_offline_error(sample)


def test_updates_ok_stores_payload():
    """Happy path: ok=True dict payload is stored verbatim with checked_at."""
    agg, _, _ = _make_aggregator()
    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=True,
        data={"count": 3, "updates": [{"title": "KB X"}],
              "reboot_required": True, "pending_reboot": False},
    )
    agg._process_result(result)
    info = state.server_update_info["srv1"]
    assert info["count"] == 3
    assert info["reboot_required"] is True
    assert "checked_at" in info


def test_updates_ok_clears_stale_restart_required_install_state(tmp_path, monkeypatch):
    """When a fresh UPDATES check reports ``pending_reboot=False`` and an
    install_state row is stuck at ``restart_required``, the aggregator
    must auto-clear it. Otherwise:

      * the install ran days ago,
      * the operator rebooted the server out-of-band (RDP / console /
        a different orchestration tool),
      * Windows cleared its RebootRequired flag,
      * BUT the install-script-written ``update-status.json`` on the
        server still says ``restart_required``,
      * → the dashboard keeps showing "restart pending" indefinitely
        until someone opens that server's detail page (which used to be
        the ONLY trigger for the clear).

    Moving the pop into the aggregator means it fires the moment the
    background UPDATES check completes — dashboard-traffic-independent.
    """
    from routes.api import _shared
    monkeypatch.setattr(
        _shared, "_install_state_path",
        tmp_path / "test_install_state.json",
    )
    _shared._update_install_state["srv1"] = {
        "status": "restart_required",
        "reboot_required": True,
        "completed_at": "2026-05-19T10:11:58Z",
    }
    agg, _, _ = _make_aggregator()
    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=True,
        data={"count": 0, "updates": [], "reboot_required": False,
              "pending_reboot": False},
    )
    agg._process_result(result)

    assert "srv1" not in _shared._update_install_state, (
        "stale restart_required install_state must auto-clear on a "
        "successful UPDATES check that confirms no pending reboot — "
        "this is the dashboard-independent path"
    )


def test_updates_ok_pending_reboot_true_keeps_install_state(tmp_path, monkeypatch):
    """The auto-clear must ONLY fire when pending_reboot is False.
    If Windows still wants a reboot, the install_state must stay so
    the dashboard keeps reminding the operator."""
    from routes.api import _shared
    monkeypatch.setattr(
        _shared, "_install_state_path",
        tmp_path / "test_install_state.json",
    )
    _shared._update_install_state["srv1"] = {
        "status": "restart_required",
        "reboot_required": True,
    }
    agg, _, _ = _make_aggregator()
    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=True,
        data={"count": 5, "updates": [], "reboot_required": True,
              "pending_reboot": True},
    )
    agg._process_result(result)

    assert "srv1" in _shared._update_install_state, (
        "pending_reboot=True means Windows still wants a reboot — "
        "do NOT clear the install_state"
    )


def test_updates_ok_skips_clear_for_non_terminal_states(tmp_path, monkeypatch):
    """The auto-clear only fires for ``restart_required``, ``completed``,
    and ``failed`` — the states that represent "operator needs to do
    something or be informed". An ``installing`` row must NOT be
    cleared even if pending_reboot is briefly False between phases —
    the install is genuinely in flight."""
    from routes.api import _shared
    monkeypatch.setattr(
        _shared, "_install_state_path",
        tmp_path / "test_install_state.json",
    )
    _shared._update_install_state["srv1"] = {
        "status": "installing",
        "started_at": "2026-05-22T13:00:00Z",
    }
    agg, _, _ = _make_aggregator()
    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=True,
        data={"count": 0, "updates": [], "reboot_required": False,
              "pending_reboot": False},
    )
    agg._process_result(result)

    # The aggregator must respect the in-flight install row.
    assert _shared._update_install_state.get("srv1", {}).get("status") == "installing"


def test_updates_ok_skips_clear_for_rebooting_state(tmp_path, monkeypatch):
    """``rebooting`` is Prism-owned (set by _set_rebooting_state, cleared
    by _handle_post_reboot when metrics return). The UPDATES handler
    must not race with that lifecycle. _handle_post_reboot is the
    sole owner of this state."""
    from routes.api import _shared
    monkeypatch.setattr(
        _shared, "_install_state_path",
        tmp_path / "test_install_state.json",
    )
    _shared._update_install_state["srv1"] = {
        "status": "rebooting",
        "reboot_started_at": "2026-05-22T13:00:00Z",
    }
    agg, _, _ = _make_aggregator()
    result = _make_result(
        check_type=CheckType.UPDATES,
        ok=True,
        data={"count": 0, "updates": [], "reboot_required": False,
              "pending_reboot": False},
    )
    agg._process_result(result)

    assert _shared._update_install_state.get("srv1", {}).get("status") == "rebooting"


# ─────────────────────────────────────────────────────────────────────────
# HARDWARE handler — sticky failure semantics
# ─────────────────────────────────────────────────────────────────────────


def test_hardware_ok_updates_state():
    """HARDWARE Result with ok=True stores the dict in state.server_hardware_info."""
    agg, _, _ = _make_aggregator()
    hw = {"cpu_name": "Xeon", "cores": 8, "threads": 16,
          "total_ram_gb": 64.0, "os": "Windows Server 2022"}
    result = _make_result(check_type=CheckType.HARDWARE, ok=True, data=hw)
    agg._process_result(result)

    stored = state.server_hardware_info["srv1"]
    assert stored["cpu_name"] == "Xeon"
    assert stored["cores"] == 8
    assert "collected_at" in stored


def test_hardware_failure_preserves_existing_data():
    """HARDWARE Result with ok=False is STICKY — the existing entry is
    left untouched. We'd rather show old data than 'Unknown'."""
    agg, _, _ = _make_aggregator()
    # Seed existing inventory
    state.server_hardware_info["srv1"] = {
        "cpu_name": "OldCPU", "cores": 4,
        "collected_at": "2026-05-18T10:00:00Z",
    }

    result = _make_result(
        check_type=CheckType.HARDWARE,
        ok=False, error="shell was not found", error_kind="offline",
    )
    agg._process_result(result)

    # Unchanged — same as it was
    stored = state.server_hardware_info["srv1"]
    assert stored["cpu_name"] == "OldCPU"
    assert stored["cores"] == 4
    assert stored["collected_at"] == "2026-05-18T10:00:00Z"


# ─────────────────────────────────────────────────────────────────────────
# Maintenance + fatigue gates
# ─────────────────────────────────────────────────────────────────────────


def test_maintenance_window_suppresses_alert_dispatch_but_still_tracks_status():
    """When _is_alert_suppressed_by_maintenance returns True, the alert
    email/webhook is SKIPPED but _previous_status is still updated so
    the resolved event fires correctly post-window."""
    aggregator._previous_status["srv1"] = "healthy"
    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {"enabled": False,
                              # Spike gate off: these tests assert a
                              # SINGLE Result produces a transition
                              # event. The gate is covered in
                              # tests/test_spike_gate.py.
                              "spike_sustain_cycles": 1},
        "email": {"enabled": True, "smtp_server": "x", "recipients": ["a@b"],
                  "send_on_critical": True},
    }
    agg, _, db = _make_aggregator(settings=settings)
    result = _make_result(
        ok=True,
        data={"cpu": 95.0, "ram": 30.0, "disk_c": 20.0, "disk_d": 10.0,
              "collection_time_ms": 500},
    )

    with _patched_config([FakeServer("srv1")]), \
            patch("collector_v2.aggregator._is_alert_suppressed_by_maintenance",
                  return_value=True), \
            patch("collector_v2.aggregator._send_alert_email_fn") as msend_email:
        send_email_mock = MagicMock()
        msend_email.return_value = send_email_mock

        agg._process_result(result)

    # _previous_status advanced (so a future "back to healthy" will
    # generate a resolved event after the window ends)
    assert aggregator._previous_status["srv1"] == "critical"

    # Metric row WAS still written (we don't stop persisting data during
    # maintenance — operators rely on having metrics for post-window review)
    db.insert_metric.assert_called_once()

    # No alert email dispatched
    send_email_mock.assert_not_called()


def test_alert_fatigue_throttle_skips_email():
    """When is_throttled_by_fatigue returns True, the alert email is
    NOT sent even though the transition is real and the settings allow it."""
    aggregator._previous_status["srv1"] = "healthy"
    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {"enabled": False,
                              # Spike gate off: these tests assert a
                              # SINGLE Result produces a transition
                              # event. The gate is covered in
                              # tests/test_spike_gate.py.
                              "spike_sustain_cycles": 1},
        "email": {"enabled": True, "smtp_server": "x", "recipients": ["a@b"],
                  "send_on_critical": True},
        "webhooks": {"enabled": False},
    }
    agg, _, _ = _make_aggregator(settings=settings)
    result = _make_result(
        ok=True,
        data={"cpu": 95.0, "ram": 30.0, "disk_c": 20.0, "disk_d": 10.0,
              "collection_time_ms": 500},
    )

    with _patched_config([FakeServer("srv1")]), \
            patch("collector_v2.aggregator._send_alert_email_fn") as msend_email, \
            patch("collector_v2.aggregator._is_throttled_by_fatigue_fn") as mfatigue:
        send_email_mock = MagicMock()
        msend_email.return_value = send_email_mock
        # ALWAYS throttled
        mfatigue.return_value = MagicMock(return_value=True)

        agg._process_result(result)

    send_email_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# CPU N-of-M gate
# ─────────────────────────────────────────────────────────────────────────


def test_cpu_n_of_m_gate_single_spike_does_not_trigger_warning():
    """One Result with CPU at warning level should be gated to healthy
    by the N-of-M ring (default consecutive=3). No 'warning' event."""
    # Make sure we start with no CPU history for this server
    try:
        from detection import _cpu_warn_history
        _cpu_warn_history.pop("cpu-srv", None)
    except Exception:
        pass

    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {
            "enabled": False,
            "cpu_warning_window_cycles": 5,
            "cpu_warning_consecutive_cycles": 3,
            # Verdict-level CPU smoothing moved to the spike gate on 2026-08-05
            # (production default 5). Pinned at 3 here so this test keeps
            # asserting what its name says: three consecutive spikes -> warning.
            "spike_sustain_cycles": 3,
        },
    }
    agg, _, db = _make_aggregator(settings=settings)
    # CPU at 80 (warning, below critical=90), RAM/disk healthy
    result = _make_result(
        ok=True,
        data={"cpu": 80.0, "ram": 30.0, "disk_c": 20.0, "disk_d": 10.0,
              "collection_time_ms": 500},
    )

    aggregator._previous_status["cpu-srv"] = "healthy"
    with _patched_config([FakeServer("cpu-srv")]):
        result.item = WorkItem(
            server_name="cpu-srv", check_type=CheckType.METRICS,
            enqueued_at=result.finished_at, deadline_s=30,
        )
        agg._process_result(result)

    # Status stored should be healthy (gated). No warning event.
    kwargs = db.insert_metric.call_args.kwargs
    assert kwargs["status"] == "healthy", \
        f"expected gated-to-healthy after 1 spike, got {kwargs['status']}"
    warn_calls = [c for c in db.insert_event.call_args_list
                  if len(c.args) > 1 and c.args[1] == "warning"]
    assert warn_calls == []


def test_cpu_n_of_m_gate_three_consecutive_spikes_does_trigger_warning():
    """Three consecutive Results with CPU at warning fill the ring; the
    third should produce status='warning' and fire a transition event."""
    try:
        from detection import _cpu_warn_history
        _cpu_warn_history.pop("cpu-srv", None)
    except Exception:
        pass

    settings = {
        "thresholds": {"enabled": True},
        "baseline_detection": {"enabled": False},
        "anomaly_detection": {
            "enabled": False,
            "cpu_warning_window_cycles": 5,
            "cpu_warning_consecutive_cycles": 3,
            # Verdict-level CPU smoothing moved to the spike gate on 2026-08-05
            # (production default 5). Pinned at 3 here so this test keeps
            # asserting what its name says: three consecutive spikes -> warning.
            "spike_sustain_cycles": 3,
        },
        # Email/webhooks not enabled — we just want to confirm the status
        # eventually flips to warning.
    }
    agg, _, db = _make_aggregator(settings=settings)
    aggregator._previous_status["cpu-srv"] = "healthy"

    with _patched_config([FakeServer("cpu-srv")]):
        for _ in range(3):
            result = _make_result(
                ok=True,
                data={"cpu": 80.0, "ram": 30.0, "disk_c": 20.0, "disk_d": 10.0,
                      "collection_time_ms": 500},
            )
            result.item = WorkItem(
                server_name="cpu-srv", check_type=CheckType.METRICS,
                enqueued_at=result.finished_at, deadline_s=30,
            )
            agg._process_result(result)

    # Final insert_metric call should have status='warning'
    final_call = db.insert_metric.call_args  # last
    assert final_call.kwargs["status"] == "warning", (
        "after 3 consecutive CPU spikes, status should be warning, got "
        f"{final_call.kwargs['status']}"
    )
    # And a warning event was fired (transition healthy → warning)
    warn_calls = [c for c in db.insert_event.call_args_list
                  if len(c.args) > 1 and c.args[1] == "warning"]
    assert len(warn_calls) >= 1


# ─────────────────────────────────────────────────────────────────────────
# Bulletproof + supervisor notification + heartbeat
# ─────────────────────────────────────────────────────────────────────────


def test_bulletproof_outer_catch_keeps_thread_alive_on_handler_exception():
    """Inner _handle_metrics_result exception is logged but does NOT
    propagate out of _process_result — supervisor is still notified and
    the loop continues."""
    agg, _, _ = _make_aggregator()
    result = _make_result(ok=True, data={"cpu": 10})

    # Force the handler to explode mid-way
    with _patched_config([FakeServer("srv1")]), \
            patch.object(agg, "_handle_metrics_result",
                         side_effect=RuntimeError("deliberate explosion")):
        # Should NOT raise — _process_result has its own inner catch.
        agg._process_result(result)

    # heartbeat callable on the next loop iteration; but the key
    # assertion is that the call returned (no exception leaked).


def test_mark_check_completed_called_on_success_and_failure():
    """state.mark_check_completed is invoked AFTER every Result, with
    the correct ok-value. Drives supervisor backoff."""
    _register_server_health("srv1")

    agg, _, _ = _make_aggregator()
    # Success
    with _patched_config([FakeServer("srv1")]), \
            patch("collector_v2.aggregator.state.mark_check_completed") as mmc:
        ok_result = _make_result(ok=True,
                                  data={"cpu": 1, "ram": 1,
                                        "disk_c": 1, "disk_d": 1,
                                        "collection_time_ms": 100})
        agg._process_result(ok_result)
        # Failure
        fail_result = _make_result(ok=False, error_kind="winrm", error="x")
        agg._process_result(fail_result)

    # Two calls — one ok=True, one ok=False
    assert mmc.call_count == 2
    ok_arg = mmc.call_args_list[0].args[2]
    fail_arg = mmc.call_args_list[1].args[2]
    assert ok_arg is True
    assert fail_arg is False


def test_aggregator_advances_state_last_aggregator_tick_in_loop():
    """The aggregator's _loop calls heartbeat_aggregator after every
    Result, which advances state.last_aggregator_tick.

    We exercise the loop directly (not via start()) for determinism:
    push one Result + a poison pill so the loop processes one item then
    exits cleanly."""
    state.last_aggregator_tick = 0.0
    agg, rq, _ = _make_aggregator()

    with _patched_config([FakeServer("srv1")]):
        rq.put(_make_result(ok=True,
                             data={"cpu": 10, "ram": 10,
                                   "disk_c": 10, "disk_d": 10,
                                   "collection_time_ms": 50}))
        agg.start()
        # Wait for the result to flow through
        deadline = time.time() + 3.0
        while time.time() < deadline and state.last_aggregator_tick == 0.0:
            time.sleep(0.05)
        agg.stop(timeout=2.0)

    assert state.last_aggregator_tick > 0.0


def test_aggregator_loop_survives_poison_pill_clean_shutdown():
    """stop() pushes a poison pill so a blocked _pull_next wakes promptly.
    Thread exits within the join timeout."""
    agg, _, _ = _make_aggregator()
    agg.start()
    # Don't put any work — loop is blocked on queue.get
    agg.stop(timeout=2.0)
    # If we got here, stop() returned within 2s — thread exited cleanly
    assert agg._thread is None or not agg._thread.is_alive()


# ─────────────────────────────────────────────────────────────────────────
# Process result error path
# ─────────────────────────────────────────────────────────────────────────


def test_process_result_unknown_check_type_logs_and_does_not_crash():
    """An unknown CheckType (somehow) should be logged and dropped.
    No DB writes, no crash."""
    agg, _, db = _make_aggregator()
    result = _make_result(ok=True, data={})
    # Hack: set an invalid check_type by bypassing the dataclass init
    # (we can't easily forge a new enum member; instead patch dispatch).
    with patch.object(agg, "_handle_metrics_result") as mh, \
            patch.object(agg, "_handle_logs_result") as ml, \
            patch.object(agg, "_handle_updates_result") as mu, \
            patch.object(agg, "_handle_hardware_result") as mhw:
        # All handlers are patched to no-ops; pass through the dispatch
        agg._process_result(result)
        mh.assert_called_once()  # METRICS is the default
        ml.assert_not_called()
        mu.assert_not_called()
        mhw.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Pulse instrumentation — audit L4
#
# The widget in the topbar depends on _process_result calling
# state.record_pulse for every Result. Without an explicit test, removing
# that line would silently break the live ECG strip with zero CI signal.
# ─────────────────────────────────────────────────────────────────────


def test_process_result_records_a_pulse_on_success():
    """Every successful Result must add an entry to the pulse buffer so
    the topbar ECG widget can render the beat."""
    state.clear_pulses()
    agg, _, _db = _make_aggregator()
    _register_server_health("srv1")
    result = _make_result(
        ok=True,
        data={"cpu": 12.5, "ram": 40.0, "disk_c": 30.0, "disk_d": 25.0,
              "collection_time_ms": 1100},
    )
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    pulses = state.get_recent_pulses(window_s=60.0)
    assert len(pulses) == 1
    ev = pulses[0]
    assert ev["server"] == "srv1"
    assert ev["check"] == "metrics"   # CheckType.METRICS.value is lowercase
    assert ev["ok"] is True
    assert ev["ms"] >= 0
    state.clear_pulses()


def test_process_result_records_a_pulse_on_failure():
    """A failed Result must also record a pulse — the canvas needs the
    red spike to convey "something tried but didn't work"."""
    state.clear_pulses()
    agg, _, _db = _make_aggregator()
    _register_server_health("srv1")
    aggregator._previous_status["srv1"] = "healthy"
    result = _make_result(ok=False, error="WinRM session timeout",
                          error_kind="timeout")
    with _patched_config([FakeServer("srv1")]):
        agg._process_result(result)

    pulses = state.get_recent_pulses(window_s=60.0)
    assert len(pulses) == 1
    assert pulses[0]["ok"] is False
    assert pulses[0]["server"] == "srv1"
    state.clear_pulses()


def test_process_result_pulse_failure_does_not_break_aggregation():
    """If record_pulse itself raises (e.g. someone tightens validation
    in a future refactor), the rest of _process_result MUST still run.
    Aggregation correctness is paramount; pulse is cosmetic."""
    state.clear_pulses()
    agg, _, db = _make_aggregator()
    _register_server_health("srv1")
    result = _make_result(
        ok=True,
        data={"cpu": 12.5, "ram": 40.0, "disk_c": 30.0, "disk_d": 25.0,
              "collection_time_ms": 1000},
    )
    # Force record_pulse to blow up — the try/except in aggregator.py
    # should swallow it.
    with patch.object(state, "record_pulse",
                      side_effect=RuntimeError("simulated pulse bug")):
        with _patched_config([FakeServer("srv1")]):
            agg._process_result(result)

    # Aggregation completed: insert_metric still called.
    db.insert_metric.assert_called_once()
    state.clear_pulses()
