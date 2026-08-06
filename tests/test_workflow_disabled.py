"""Tests for the disabled-block + disabled-connection support.

Operators can right-click a block on the canvas → Disable. The block's
``data.disabled`` flag is set; the executor must:
  1. Skip the executor function entirely (no WinRM, no side effects)
  2. Mark the step as ``status='skipped'`` in the DB
  3. Treat the block as a SUCCESS pass-through so downstream nodes still
     run via the success path

Operators can also right-click a connection → Disable. The signature
``"<output_class>:<target_node>:<input_class>"`` is appended to the
source node's ``data._disabled_conns`` list. The executor's parse step
must filter those connections out of the success/fail target lists.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import workflow_engine


def _canvas(nodes_dict):
    """Wrap a dict of Drawflow nodes in the canonical Drawflow shape."""
    return {"drawflow": {"Home": {"data": nodes_dict}}}


def _node(node_id, name, *, data=None, out1=None, out2=None):
    """Build one Drawflow node entry. ``out1`` / ``out2`` are lists of
    target ids (or (id, input_class) tuples for fine-grained disable)."""
    data = dict(data or {})

    def _conn_list(targets):
        out = []
        for t in (targets or []):
            if isinstance(t, tuple):
                out.append({"node": str(t[0]), "input": t[1]})
            else:
                out.append({"node": str(t), "input": "input_1"})
        return out

    return {
        "id": node_id,
        "name": name,
        "data": data,
        "inputs": {},
        "outputs": {
            "output_1": {"connections": _conn_list(out1 or [])},
            "output_2": {"connections": _conn_list(out2 or [])},
        },
    }


# ─────────────────────────────────────────────────────────────────────
# 1. parse_canvas — surfaces the disabled flag + filters out disabled
#    connections by signature
# ─────────────────────────────────────────────────────────────────────


def test_parse_canvas_exposes_disabled_flag_per_node():
    canvas = _canvas({
        "1": _node(1, "check_service", data={"disabled": True}),
        "2": _node(2, "restart_service"),
    })
    graph = workflow_engine.parse_canvas(canvas)
    assert graph["nodes"]["1"]["disabled"] is True
    assert graph["nodes"]["2"]["disabled"] is False


def test_parse_canvas_drops_disabled_success_connection():
    """Disabling a connection puts its signature in the SOURCE node's
    ``_disabled_conns``. parse_canvas must not include that target."""
    canvas = _canvas({
        "1": _node(
            1, "check_service",
            data={"_disabled_conns": ["output_1:2:input_1"]},
            out1=[2, 3],   # two success targets — one of them is disabled
        ),
        "2": _node(2, "restart_service"),
        "3": _node(3, "send_email"),
    })
    graph = workflow_engine.parse_canvas(canvas)
    # node "1" should only forward to node "3" — node "2" filtered out
    assert graph["nodes"]["1"]["outputs"]["success"] == ["3"]


def test_parse_canvas_drops_disabled_fail_connection():
    canvas = _canvas({
        "1": _node(
            1, "check_service",
            data={"_disabled_conns": ["output_2:4:input_1"]},
            out1=[2], out2=[4],
        ),
        "2": _node(2, "restart_service"),
        "4": _node(4, "send_email"),
    })
    graph = workflow_engine.parse_canvas(canvas)
    assert graph["nodes"]["1"]["outputs"]["fail"] == []
    # The disabled fail target is no longer marked as having incoming,
    # so it becomes a start_node candidate (no parents). That's fine —
    # it's behaving like a disconnected node.
    assert "4" in graph["start_nodes"]


def test_parse_canvas_no_disabled_conns_field_is_treated_as_empty():
    """Old workflows (saved before this feature) have no _disabled_conns
    key. Parse must default to "nothing disabled" — backwards compatible."""
    canvas = _canvas({
        "1": _node(1, "check_service", out1=[2]),
        "2": _node(2, "send_email"),
    })
    graph = workflow_engine.parse_canvas(canvas)
    assert graph["nodes"]["1"]["outputs"]["success"] == ["2"]


# ─────────────────────────────────────────────────────────────────────
# 2. Executor — disabled blocks are skipped (no executor call) but
#    propagate to the success path. Steps get status="skipped".
# ─────────────────────────────────────────────────────────────────────


def test_executor_skips_disabled_block_no_executor_call():
    """A disabled check_service must NOT call _exec_check_service. That's
    the whole point of "disabled" — no side effects on the target."""
    db = MagicMock()
    db.insert_workflow_step.return_value = 99
    graph = {
        "nodes": {
            "1": {
                "type": "check_service",
                "config": {"server": "srv1", "service_name": "Spooler"},
                "label": "Check Spooler",
                "disabled": True,
                "outputs": {"success": [], "fail": []},
            },
        },
        "start_nodes": ["1"],
    }
    with patch.object(workflow_engine, "_exec_check_service") as mocked:
        workflow_engine._execute_graph(db, 1, graph, {}, {})
    mocked.assert_not_called()


def test_executor_disabled_block_marks_step_skipped():
    db = MagicMock()
    db.insert_workflow_step.return_value = 99
    graph = {
        "nodes": {
            "1": {
                "type": "check_service",
                "config": {},
                "label": "x",
                "disabled": True,
                "outputs": {"success": [], "fail": []},
            },
        },
        "start_nodes": ["1"],
    }
    workflow_engine._execute_graph(db, 1, graph, {}, {})
    # Find the update_workflow_step call that set the final status
    calls = [c for c in db.update_workflow_step.call_args_list
             if c.kwargs.get("status") in ("skipped", "completed", "failed")]
    assert len(calls) == 1
    assert calls[0].kwargs["status"] == "skipped"
    assert "disabled" in calls[0].kwargs["output"].lower()


def test_executor_disabled_block_continues_via_success_path():
    """Disabled = pass-through. Downstream node on the success path
    MUST still run (otherwise disabling a block silently aborts the
    workflow). Patches BLOCK_EXECUTORS directly because the registry
    holds direct function references — patching the module-level name
    doesn't change what the executor calls."""
    db = MagicMock()
    db.insert_workflow_step.side_effect = lambda *a, **kw: id(a)
    graph = {
        "nodes": {
            "1": {
                "type": "check_service",
                "config": {},
                "label": "disabled",
                "disabled": True,
                "outputs": {"success": ["2"], "fail": []},
            },
            "2": {
                "type": "send_email",
                "config": {"to": "x", "subject": "y", "body": "z"},
                "label": "send",
                "disabled": False,
                "outputs": {"success": [], "fail": []},
            },
        },
        "start_nodes": ["1"],
    }
    fake_executor = MagicMock(return_value=(True, "sent"))
    with patch.dict(workflow_engine.BLOCK_EXECUTORS,
                    {"send_email": fake_executor}):
        workflow_engine._execute_graph(db, 1, graph, {}, {})
    fake_executor.assert_called_once()
