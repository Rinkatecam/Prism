"""Feature 2.6 — verify-after (final plan step).

When a remediation block (restart_service / start_service) has
``config.verify_after`` set, the graph executor re-checks the SAME target with
check_service right after a successful remediation. If the follow-up check
fails, the remediation is treated as FAILED (routes to the fail path) so a
"restarted but still down" outcome isn't reported green. The verify is recorded
as its own step. Skipped entirely in dry-run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import workflow_engine as we


def _node(t, parents, success=None, fail=None, config=None):
    return {"type": t, "config": config or {}, "label": t, "disabled": False,
            "parents": parents, "outputs": {"success": success or [], "fail": fail or []}}


def _db():
    db = MagicMock()
    db.insert_workflow_step.return_value = 1
    return db


def _graph(verify_after=True):
    cfg = {"service_name": "W3SVC", "server": "s"}
    if verify_after:
        cfg["verify_after"] = True
    return {"nodes": {
        "1": _node("restart_service", [], success=["2"], fail=["3"], config=cfg),
        "2": _node("log_event", ["1"], config={"label": "ok"}),
        "3": _node("log_event", ["1"], config={"label": "bad"}),
    }, "start_nodes": ["1"]}


def _step_types(db):
    return [c.args[2] for c in db.insert_workflow_step.call_args_list]


def test_verify_after_success_routes_success(monkeypatch):
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service", lambda *a, **k: (True, "restarted"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service", lambda *a, **k: (True, "running"))
    reached = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event",
                        lambda cfg, *a, **k: (reached.append(cfg.get("label")), (True, "l"))[1])
    we._execute_graph(_db(), 1, _graph(), {}, {})
    assert "ok" in reached and "bad" not in reached


def test_verify_after_failure_routes_fail(monkeypatch):
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service", lambda *a, **k: (True, "restarted"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service", lambda *a, **k: (False, "still down"))
    reached = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event",
                        lambda cfg, *a, **k: (reached.append(cfg.get("label")), (True, "l"))[1])
    db = _db()
    we._execute_graph(db, 1, _graph(), {}, {})
    assert "bad" in reached and "ok" not in reached, "verify failure must route to the fail path"
    assert "check_service" in _step_types(db), "verify must be recorded as its own step"


def test_verify_after_not_run_when_disabled(monkeypatch):
    checks = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service", lambda *a, **k: (True, "restarted"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service",
                        lambda *a, **k: (checks.append(1), (True, "x"))[1])
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event", lambda *a, **k: (True, "l"))
    we._execute_graph(_db(), 1, _graph(verify_after=False), {}, {})
    assert checks == [], "no verify when verify_after is not set"


def test_verify_after_skipped_in_dry_run(monkeypatch):
    checks = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service", lambda *a, **k: (True, "restarted"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service",
                        lambda *a, **k: (checks.append(1), (True, "x"))[1])
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event", lambda *a, **k: (True, "l"))
    we._execute_graph(_db(), 1, _graph(), {}, {}, dry_run=True)
    assert checks == [], "dry-run must not run the verify check (no WinRM)"


def test_verify_after_not_run_when_remediation_fails(monkeypatch):
    checks = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service", lambda *a, **k: (False, "restart failed"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service",
                        lambda *a, **k: (checks.append(1), (True, "x"))[1])
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event", lambda *a, **k: (True, "l"))
    we._execute_graph(_db(), 1, _graph(), {}, {})
    assert checks == [], "no verify when the remediation itself failed"
