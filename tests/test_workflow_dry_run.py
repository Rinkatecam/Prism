"""Feature 2.6 — dry-run mode.

A dry run walks the workflow's success path WITHOUT executing any side-effecting
block (WinRM actions/checks, notifications, waits) so an operator can inspect
the plan safely. Only pure pass-through triggers actually run; every other block
is recorded status="dry_run" and treated as success for routing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import workflow_engine as we


def _graph(node_type, config=None):
    return {
        "nodes": {
            "1": {"type": node_type, "config": config or {}, "label": node_type,
                  "disabled": False, "outputs": {"success": [], "fail": []}},
        },
        "start_nodes": ["1"],
    }


def _mock_db():
    db = MagicMock()
    db.insert_workflow_step.return_value = 100
    return db


def test_dry_run_skips_winrm_block(monkeypatch):
    called = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "run_powershell",
                        lambda *a, **k: (called.append(1), (True, "ran"))[1])
    db = _mock_db()
    we._execute_graph(db, 1, _graph("run_powershell"), {}, {}, dry_run=True)
    assert called == [], "WinRM executor must not run in dry-run"
    statuses = [kw.get("status") for _a, kw in db.update_workflow_step.call_args_list]
    assert "dry_run" in statuses


def test_non_dry_run_still_calls_executor(monkeypatch):
    called = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "run_powershell",
                        lambda *a, **k: (called.append(1), (True, "ran"))[1])
    db = _mock_db()
    we._execute_graph(db, 1, _graph("run_powershell"), {}, {}, dry_run=False)
    assert called == [1], "normal run must call the executor"


def test_dry_run_does_not_send_notifications(monkeypatch):
    sent = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "send_email",
                        lambda *a, **k: (sent.append(1), (True, "sent"))[1])
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "send_webhook",
                        lambda *a, **k: (sent.append(1), (True, "sent"))[1])
    db = _mock_db()
    we._execute_graph(db, 1, _graph("send_email"), {}, {}, dry_run=True)
    we._execute_graph(db, 1, _graph("send_webhook"), {}, {}, dry_run=True)
    assert sent == [], "notifications must not dispatch in dry-run"


def test_dry_run_follows_success_path(monkeypatch):
    # Two nodes: a suppressed check → a suppressed action on its success path.
    graph = {
        "nodes": {
            "1": {"type": "check_service", "config": {}, "label": "c",
                  "disabled": False, "outputs": {"success": ["2"], "fail": []}},
            "2": {"type": "restart_service", "config": {}, "label": "r",
                  "disabled": False, "outputs": {"success": [], "fail": []}},
        },
        "start_nodes": ["1"],
    }
    ran = []
    for t in ("check_service", "restart_service"):
        monkeypatch.setitem(we.BLOCK_EXECUTORS, t,
                            lambda *a, **k: (ran.append(1), (True, "x"))[1])
    db = _mock_db()
    we._execute_graph(db, 1, graph, {}, {}, dry_run=True)
    assert ran == [], "no executor runs in dry-run"
    # Both steps were inserted (the success path was followed to node 2).
    assert db.insert_workflow_step.call_count == 2


def test_execute_workflow_accepts_dry_run_param():
    db = MagicMock()
    db.get_workflow.return_value = {"id": 3, "name": "wf", "canvas_json": "{}"}
    db.create_workflow_execution.return_value = 7
    db.get_workflow_steps.return_value = []
    # Should not raise; dry_run is a real parameter.
    exec_id = we.execute_workflow(db, 3, lambda: [], {}, dry_run=True)
    assert exec_id == 7
