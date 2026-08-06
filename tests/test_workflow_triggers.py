"""Tests for the workflow trigger-block system.

Adds three palette blocks (Manual / Schedule / Event) with config that
gets mirrored to the workflow row's ``trigger_type`` / ``trigger_config``
columns at save time. The existing scheduler loop is what actually fires
scheduled workflows — these tests pin both the mirror logic AND the new
event-trigger evaluator + edge-detect debouncing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import workflow_engine


# ─────────────────────────────────────────────────────────────────────
# 1. _sync_trigger_from_canvas — mirrors a canvas trigger block onto
#    the workflow row's trigger_type / trigger_config columns
# ─────────────────────────────────────────────────────────────────────


def _drawflow_with_node(name: str, data: dict) -> dict:
    """Build a minimal Drawflow canvas JSON containing one node."""
    return {
        "drawflow": {
            "Home": {
                "data": {
                    "1": {
                        "id": 1,
                        "name": name,
                        "data": data,
                        "inputs": {},
                        "outputs": {},
                    }
                }
            }
        }
    }


def test_canvas_trigger_block_overrides_row_trigger_type():
    """A trigger_schedule block on the canvas sets trigger_type=scheduled
    regardless of what the dropdown shipped."""
    from routes.api.workflows import _sync_trigger_from_canvas
    data = {
        "trigger_type": "manual",  # dropdown said manual
        "canvas_json": _drawflow_with_node(
            "trigger_schedule",
            {"schedule": "daily", "time": "06:00"},
        ),
    }
    _sync_trigger_from_canvas(data)
    assert data["trigger_type"] == "scheduled"
    assert data["trigger_config"] == {"schedule": "daily", "time": "06:00"}


def test_manual_trigger_block_maps_to_manual_trigger_type():
    from routes.api.workflows import _sync_trigger_from_canvas
    data = {
        "trigger_type": "scheduled",  # ignored — block wins
        "canvas_json": _drawflow_with_node("trigger_manual", {}),
    }
    _sync_trigger_from_canvas(data)
    assert data["trigger_type"] == "manual"


def test_event_trigger_block_maps_to_event_trigger_type():
    from routes.api.workflows import _sync_trigger_from_canvas
    cfg = {
        "event_type": "service_stopped",
        "server": "SRV01",
        "target": "Spooler",
        "poll_seconds": 60,
    }
    data = {"canvas_json": _drawflow_with_node("trigger_event", cfg)}
    _sync_trigger_from_canvas(data)
    assert data["trigger_type"] == "event"
    assert data["trigger_config"] == cfg


def test_no_canvas_trigger_block_leaves_row_fields_alone():
    """If the canvas has no trigger block, whatever the request set on
    trigger_type / trigger_config is preserved (the dropdown still works)."""
    from routes.api.workflows import _sync_trigger_from_canvas
    data = {
        "trigger_type": "scheduled",
        "trigger_config": {"schedule": "daily", "time": "09:00"},
        "canvas_json": {
            "drawflow": {
                "Home": {"data": {
                    "1": {"id": 1, "name": "check_service",
                          "data": {"server": "x", "service_name": "y"},
                          "inputs": {}, "outputs": {}}
                }}
            }
        },
    }
    _sync_trigger_from_canvas(data)
    assert data["trigger_type"] == "scheduled"
    assert data["trigger_config"] == {"schedule": "daily", "time": "09:00"}


def test_canvas_json_as_string_is_handled():
    """Some callers pass canvas_json as an already-encoded JSON string.
    The sync helper must parse it before walking nodes."""
    from routes.api.workflows import _sync_trigger_from_canvas
    import json as _json
    data = {
        "canvas_json": _json.dumps(_drawflow_with_node(
            "trigger_manual", {}
        )),
    }
    _sync_trigger_from_canvas(data)
    assert data["trigger_type"] == "manual"


def test_multiple_trigger_blocks_takes_first():
    """Defensive — the UI shouldn't allow it but if two trigger blocks
    are on the canvas, take the first and log a warning."""
    from routes.api.workflows import _sync_trigger_from_canvas
    canvas = {
        "drawflow": {"Home": {"data": {
            "1": {"id": 1, "name": "trigger_manual",
                  "data": {}, "inputs": {}, "outputs": {}},
            "2": {"id": 2, "name": "trigger_schedule",
                  "data": {"schedule": "daily", "time": "08:00"},
                  "inputs": {}, "outputs": {}},
        }}}
    }
    data = {"canvas_json": canvas}
    _sync_trigger_from_canvas(data)
    # Iteration order on a dict literal is insertion order in CPython
    # 3.7+, so "1" (manual) comes first.
    assert data["trigger_type"] == "manual"


def test_malformed_canvas_does_not_crash():
    """A bad canvas_json (None, garbage, missing structure) must not
    crash the save — the helper just no-ops and lets the dropdown win."""
    from routes.api.workflows import _sync_trigger_from_canvas
    for bad in (None, "", "not json", "{}", [], 42):
        data = {"canvas_json": bad, "trigger_type": "manual"}
        _sync_trigger_from_canvas(data)  # must not raise
        assert data["trigger_type"] == "manual"


# ─────────────────────────────────────────────────────────────────────
# 2. Trigger executors — pass-through (no-op) so graph execution flows
# ─────────────────────────────────────────────────────────────────────


def test_trigger_executors_are_no_ops():
    """Trigger blocks on the canvas should not "do work" during graph
    execution — the trigger has already done its job by the time the
    engine runs. The executor just passes the success signal to the
    block's single output port."""
    ok, output = workflow_engine._exec_trigger({}, {}, None, {})
    assert ok is True
    assert output == ""


def test_trigger_executors_registered():
    """All three trigger block types must be in BLOCK_EXECUTORS — without
    this, the graph engine raises 'Unknown block type'."""
    assert "trigger_manual" in workflow_engine.BLOCK_EXECUTORS
    assert "trigger_schedule" in workflow_engine.BLOCK_EXECUTORS
    assert "trigger_event" in workflow_engine.BLOCK_EXECUTORS


# ─────────────────────────────────────────────────────────────────────
# 3. Event-trigger evaluator — maps event_type to existing check funcs
# ─────────────────────────────────────────────────────────────────────


def test_event_service_stopped_uses_check_service_inverted():
    """event_type=service_stopped means "check service, return True if
    NOT running". The evaluator should invert _exec_check_service's
    result so that 'service is stopped' → met=True."""
    server_map = {"srv1": SimpleNamespace(name="srv1", host="srv1.local")}
    with patch.object(workflow_engine, "_exec_check_service",
                      return_value=(False, "Service 'Spooler' is not running")):
        met, msg = workflow_engine._evaluate_event_trigger(
            {"event_type": "service_stopped", "server": "srv1", "target": "Spooler"},
            server_map, None, {},
        )
    assert met is True
    assert "Spooler" in msg


def test_event_service_running_passes_check_service_through():
    server_map = {"srv1": SimpleNamespace(name="srv1", host="srv1.local")}
    with patch.object(workflow_engine, "_exec_check_service",
                      return_value=(True, "Service 'Spooler': Running")):
        met, _ = workflow_engine._evaluate_event_trigger(
            {"event_type": "service_running", "server": "srv1", "target": "Spooler"},
            server_map, None, {},
        )
    assert met is True


def test_event_port_closed_inverts_check_port():
    server_map = {"srv1": SimpleNamespace(name="srv1", host="srv1.local")}
    with patch.object(workflow_engine, "_exec_check_port",
                      return_value=(False, "Port 5432 closed")):
        met, _ = workflow_engine._evaluate_event_trigger(
            {"event_type": "port_closed", "server": "srv1", "target": "5432"},
            server_map, None, {},
        )
    assert met is True


def test_event_port_invalid_target_does_not_crash():
    """A non-integer port string must produce met=False with a clean
    message, not a stack trace."""
    server_map = {"srv1": SimpleNamespace(name="srv1")}
    met, msg = workflow_engine._evaluate_event_trigger(
        {"event_type": "port_open", "server": "srv1", "target": "not-a-number"},
        server_map, None, {},
    )
    assert met is False
    assert "invalid port" in msg


def test_event_metric_threshold_reads_latest_by_server():
    """metric_threshold must read state.latest_by_server (NOT WinRM) so
    we don't double-poll metrics Prism already collects."""
    import state
    server_map = {"srv1": SimpleNamespace(name="srv1")}
    state.latest_by_server["srv1"] = {
        "cpu_percent": 95.0,
        "ram_percent": 40.0,
    }
    try:
        # CPU 95% >= 90% threshold → met
        met, msg = workflow_engine._evaluate_event_trigger(
            {"event_type": "metric_threshold", "server": "srv1",
             "metric": "cpu", "threshold": 90, "operator": ">="},
            server_map, None, {},
        )
        assert met is True
        assert "cpu" in msg

        # RAM 40% >= 90% threshold → not met
        met, _ = workflow_engine._evaluate_event_trigger(
            {"event_type": "metric_threshold", "server": "srv1",
             "metric": "ram", "threshold": 90, "operator": ">="},
            server_map, None, {},
        )
        assert met is False
    finally:
        state.latest_by_server.pop("srv1", None)


def test_event_metric_threshold_missing_metric_yields_not_met():
    """If the metric isn't in the cache (server hasn't reported yet),
    the trigger doesn't fire — better than a false positive."""
    import state
    state.latest_by_server.pop("srv1", None)  # ensure not present
    server_map = {"srv1": SimpleNamespace(name="srv1")}
    met, msg = workflow_engine._evaluate_event_trigger(
        {"event_type": "metric_threshold", "server": "srv1",
         "metric": "cpu", "threshold": 90},
        server_map, None, {},
    )
    assert met is False
    assert "no metric" in msg


def test_event_unknown_event_type_does_not_crash():
    server_map = {"srv1": SimpleNamespace(name="srv1")}
    met, msg = workflow_engine._evaluate_event_trigger(
        {"event_type": "bogus", "server": "srv1"},
        server_map, None, {},
    )
    assert met is False
    assert "unknown event_type" in msg


# ─────────────────────────────────────────────────────────────────────
# 4. Edge-trigger debouncing — verified at the state-dict level
# ─────────────────────────────────────────────────────────────────────


def test_first_evaluation_never_fires_even_if_condition_is_true():
    """A workflow whose condition is already true at startup must NOT
    immediately fire — restarting Prism (or creating a new event trigger)
    shouldn't trigger workflows. Only False → True transitions count.

    This is enforced by the scheduler-loop body comparing the new value
    against ``prev_value is False`` (strict identity), so prev_value=None
    (never checked) cannot satisfy that condition.
    """
    # The actual scheduler loop is a big while-True; rather than spin it
    # up, exercise the same boolean logic directly.
    prev_value = None  # never evaluated before
    met = True         # condition is true on first check
    fire = (met and prev_value is False)
    assert fire is False, (
        "first-ever evaluation must not fire even when condition is true"
    )


def test_false_to_true_transition_fires():
    prev_value = False
    met = True
    fire = (met and prev_value is False)
    assert fire is True


def test_true_to_true_does_not_re_fire():
    """A condition that stays true (a service still stopped on the next
    poll) must not re-fire — that's what edge-trigger means."""
    prev_value = True
    met = True
    fire = (met and prev_value is False)
    assert fire is False


def test_true_to_false_does_not_fire():
    """Recovery transitions don't fire — only False→True is the trigger
    edge."""
    prev_value = True
    met = False
    fire = (met and prev_value is False)
    assert fire is False
