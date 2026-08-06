"""Feature 2.6 — per-workflow concurrency lock.

Never run two instances of the SAME workflow concurrently. This kills the
scheduler+event double-fire and rapid manual double-clicks that would otherwise
stack overlapping remediation on the same servers. In-memory per workflow_id.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import workflow_engine as we


@pytest.fixture(autouse=True)
def _clear():
    with we._inflight_lock:
        we._inflight.clear()
    with we._breaker_lock:
        we._breaker.clear()
    yield
    with we._inflight_lock:
        we._inflight.clear()


def test_second_acquire_of_same_workflow_fails_until_released():
    assert we._try_acquire_inflight(7) is True
    assert we._try_acquire_inflight(7) is False  # already in flight
    we._release_inflight(7)
    assert we._try_acquire_inflight(7) is True    # released → reacquirable


def test_different_workflows_are_independent():
    assert we._try_acquire_inflight(1) is True
    assert we._try_acquire_inflight(2) is True


def test_execute_workflow_skips_when_already_in_flight():
    we._try_acquire_inflight(9)  # pretend a run is already active
    db = MagicMock()
    db.get_workflow.return_value = {"id": 9, "name": "wf", "canvas_json": "{}"}
    result = we.execute_workflow(db, 9, lambda: [], {}, trigger_source="event")
    assert result is None
    db.create_workflow_execution.assert_not_called()
    logged = " ".join(str(c) for c in db.log_audit.call_args_list).lower()
    assert "inflight" in logged or "in flight" in logged or "already running" in logged
