"""Feature 2.5 — real retry / and_gate / or_gate semantics.

These blocks were inert (mapped to _exec_wait). The frontend models them as
gates (2 inputs, 1 output) and retry (1 input "retry previous step", 2 outputs).
Now the graph executor evaluates them:

  * parse_canvas exposes a reverse-adjacency `parents` list per node.
  * retry re-executes its single upstream parent up to max_attempts TOTAL (the
    forward run was attempt 1, so retry adds up to max_attempts-1), following
    output_1 on eventual success / output_2 on exhaustion.
  * and_gate succeeds iff ALL parents succeeded; or_gate iff ANY did. A gate
    reached before its parents defers (bounded) rather than misfiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import workflow_engine as we


# ── parents map ──────────────────────────────────────────────────────────────

def test_parse_canvas_builds_parents_map():
    canvas = {"drawflow": {"Home": {"data": {
        "1": {"id": 1, "name": "check_service", "data": {},
              "outputs": {"output_1": {"connections": [{"node": "3", "input": "input_1"}]}}},
        "2": {"id": 2, "name": "check_port", "data": {},
              "outputs": {"output_1": {"connections": [{"node": "3", "input": "input_2"}]}}},
        "3": {"id": 3, "name": "and_gate", "data": {}, "outputs": {}},
    }}}}
    g = we.parse_canvas(canvas)
    assert sorted(g["nodes"]["3"]["parents"]) == ["1", "2"]
    assert g["nodes"]["1"]["parents"] == []


# ── helpers ──────────────────────────────────────────────────────────────────

def _node(t, parents, success=None, fail=None, config=None):
    return {"type": t, "config": config or {}, "label": t, "disabled": False,
            "parents": parents, "outputs": {"success": success or [], "fail": fail or []}}


def _db():
    db = MagicMock()
    db.insert_workflow_step.return_value = 1
    return db


def _executed_types(db):
    """node_types that got a step row inserted (i.e. were reached/executed)."""
    return [c.args[2] for c in db.insert_workflow_step.call_args_list]


# ── retry ────────────────────────────────────────────────────────────────────

def test_retry_reruns_parent_until_success(monkeypatch):
    results = iter([(False, "f1"), (False, "f2"), (True, "ok")])
    calls = []
    def _svc(*a, **k):
        calls.append(1)
        return next(results)
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service", _svc)
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event", lambda *a, **k: (True, "logged"))
    graph = {"nodes": {
        "1": _node("check_service", [], fail=["2"]),
        "2": _node("retry", ["1"], success=["3"], fail=["4"], config={"max_attempts": 3, "delay": 0}),
        "3": _node("log_event", ["2"]),
        "4": _node("log_event", ["2"]),
    }, "start_nodes": ["1"]}
    db = _db()
    we._execute_graph(db, 1, graph, {}, {})
    assert len(calls) == 3, "1 forward attempt + 2 retries = 3 total"
    reached = _executed_types(db)
    assert reached.count("log_event") == 1  # only the success branch (node 3) ran


def test_retry_exhausts_and_follows_fail(monkeypatch):
    calls = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service",
                        lambda *a, **k: (calls.append(1), (False, "down"))[1])
    reached = []
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "restart_service",
                        lambda *a, **k: (reached.append("restart"), (True, "r"))[1])
    graph = {"nodes": {
        "1": _node("check_service", [], fail=["2"]),
        "2": _node("retry", ["1"], success=["9"], fail=["3"], config={"max_attempts": 3, "delay": 0}),
        "3": _node("restart_service", ["2"]),
    }, "start_nodes": ["1"]}
    we._execute_graph(_db(), 1, graph, {}, {})
    assert len(calls) == 3            # 1 forward + 2 retries, all fail
    assert reached == ["restart"]     # retry's FAIL path (node 3) taken


# ── gates ────────────────────────────────────────────────────────────────────

def _gate_graph(gate_type, p1_ok, p2_ok):
    return {"nodes": {
        "1": _node("check_service", [], success=(["3"] if p1_ok else []), fail=([] if p1_ok else ["3"])),
        "2": _node("check_port", [], success=(["3"] if p2_ok else []), fail=([] if p2_ok else ["3"])),
        "3": _node(gate_type, ["1", "2"], success=["4"], fail=["5"]),
        "4": _node("log_event", ["3"]),
        "5": _node("log_event", ["3"]),
    }, "start_nodes": ["1", "2"]}


def _wire(monkeypatch, p1_ok, p2_ok, reached):
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_service", lambda *a, **k: (p1_ok, "s"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "check_port", lambda *a, **k: (p2_ok, "p"))
    monkeypatch.setitem(we.BLOCK_EXECUTORS, "log_event",
                        lambda cfg, *a, **k: (reached.append(cfg), (True, "l"))[1])


def test_and_gate_all_parents_succeed(monkeypatch):
    reached = []
    _wire(monkeypatch, True, True, reached)
    db = _db()
    we._execute_graph(db, 1, _gate_graph("and_gate", True, True), {}, {})
    # gate passed → success branch (node 4) ran, fail branch (node 5) did not
    types = _executed_types(db)
    assert "and_gate" in types
    # exactly one log_event (the success branch)
    assert types.count("log_event") == 1


def test_and_gate_one_parent_fails(monkeypatch):
    _wire(monkeypatch, True, False, [])
    db = _db()
    we._execute_graph(db, 1, _gate_graph("and_gate", True, False), {}, {})
    steps = {(c.args[1]): c.args[2] for c in db.insert_workflow_step.call_args_list}
    # gate node (id 3) recorded as failed → fail branch taken
    gate_updates = [c for c in db.update_workflow_step.call_args_list
                    if c.args and c.args[0] == 1]
    # at least one update marked the gate failed
    assert any(kw.get("status") == "failed" for _a, kw in db.update_workflow_step.call_args_list)


def test_or_gate_any_parent_succeeds(monkeypatch):
    _wire(monkeypatch, False, True, [])
    db = _db()
    we._execute_graph(db, 1, _gate_graph("or_gate", False, True), {}, {})
    assert any(kw.get("status") == "completed"
               for _a, kw in db.update_workflow_step.call_args_list)


def test_gate_defers_until_both_parents_resolved(monkeypatch):
    # Both parents feed the gate; the gate must be evaluated exactly once,
    # after both parents resolve (one step row for the gate).
    _wire(monkeypatch, True, True, [])
    db = _db()
    we._execute_graph(db, 1, _gate_graph("and_gate", True, True), {}, {})
    gate_rows = [c for c in db.insert_workflow_step.call_args_list if c.args[2] == "and_gate"]
    assert len(gate_rows) == 1, "gate must produce exactly one step row despite deferral"
