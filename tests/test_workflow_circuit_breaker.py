"""Feature 2.6 — execution circuit breaker (the loop-stopper).

Ground truth: workflow auto-fires come from the scheduler (trigger_source
"scheduled", workflow_engine.py:1531) and event triggers ("event", :1491);
manual runs come from the API. An unattended remediation that keeps failing
must not re-fire forever. The breaker is in-memory + per-workflow_id (a process
restart de-fangs a firing loop, matching _event_trigger_state). It suppresses
ONLY auto-fires after N consecutive failures; manual always proceeds (audited
override); a success resets it; a cooldown half-opens it for one trial.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import workflow_engine as we


@pytest.fixture(autouse=True)
def _clear_breaker():
    with we._breaker_lock:
        we._breaker.clear()
    yield
    with we._breaker_lock:
        we._breaker.clear()


_S = {"workflows": {"circuit_breaker": {
    "enabled": True, "max_consecutive_failures": 3, "cooldown_seconds": 1800}}}


def _fail_n(wf_id, n, now=1000):
    for _ in range(n):
        we._breaker_record(wf_id, False, _S, now=now)


def test_breaker_closed_allows_auto():
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is True


def test_breaker_opens_after_n_failures_blocks_auto():
    _fail_n(1, 3)
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is False
    assert we._breaker_allows(1, "event", _S, now=1000) is False


def test_breaker_below_threshold_still_allows():
    _fail_n(1, 2)
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is True


def test_breaker_never_blocks_manual():
    _fail_n(1, 5)
    assert we._breaker_allows(1, "manual", _S, now=1000) is True


def test_breaker_resets_on_success():
    _fail_n(1, 3)
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is False
    we._breaker_record(1, True, _S, now=1000)
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is True


def test_breaker_half_opens_after_cooldown():
    _fail_n(1, 3)
    assert we._breaker_allows(1, "scheduled", _S, now=1000 + 100) is False       # within cooldown
    assert we._breaker_allows(1, "scheduled", _S, now=1000 + 1801) is True        # cooldown elapsed → one trial


def test_breaker_is_per_workflow():
    _fail_n(1, 3)
    assert we._breaker_allows(1, "scheduled", _S, now=1000) is False
    assert we._breaker_allows(2, "scheduled", _S, now=1000) is True   # unrelated workflow unaffected


def test_breaker_disabled_config_always_allows():
    s = {"workflows": {"circuit_breaker": {"enabled": False}}}
    for _ in range(5):
        we._breaker_record(1, False, s, now=1000)
    assert we._breaker_allows(1, "scheduled", s, now=1000) is True


def test_should_record_breaker_only_for_auto_non_dryrun():
    # Only real auto-fires feed the breaker: manual is an override (must never
    # open it), and a dry-run must never reset an open breaker.
    assert we._should_record_breaker("scheduled", False) is True
    assert we._should_record_breaker("event", False) is True
    assert we._should_record_breaker("manual", False) is False
    assert we._should_record_breaker("api", False) is False
    assert we._should_record_breaker("scheduled", True) is False   # dry-run
    assert we._should_record_breaker("event", True) is False


def test_execute_workflow_suppresses_auto_fire_when_open():
    import time as _t
    now = _t.time()
    _fail_n(42, 3, now=now)  # open with a real-clock opened_at so cooldown hasn't elapsed
    db = MagicMock()
    db.get_workflow.return_value = {"id": 42, "name": "wf", "canvas_json": "{}"}
    db.create_workflow_execution.return_value = 777
    result = we.execute_workflow(db, 42, lambda: [], _S, trigger_source="scheduled")
    # The auto-fire is suppressed but must be VISIBLE: a terminal "suppressed"
    # execution row is recorded (not a silent stop).
    db.create_workflow_execution.assert_called_once()
    upd = db.update_workflow_execution.call_args
    assert upd.kwargs.get("status") == "suppressed"
    assert result == 777
    logged = " ".join(str(c) for c in db.log_audit.call_args_list).lower()
    assert "breaker" in logged or "suppress" in logged
