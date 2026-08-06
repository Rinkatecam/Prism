"""Workflows endpoints — split out from the original routes/api.py."""

import re
import time
import io
import json
from pathlib import Path
from flask import jsonify, request, Response, make_response, current_app
from flask import session as flask_session
from crypto_utils import is_password_masked, decrypt_password, PASSWORD_MASK
import collector_v2 as _collector_v2
from collector_v2 import (
    accelerate_server,
    sync_now as _v2_sync_now,
    sync_logs_now as _v2_sync_logs_now,
    sync_updates_now as _v2_sync_updates_now,
)
from state import (
    server_auth_info,
    server_update_info,
    server_hardware_info,
)
from email_alerts import send_test_email
from analytics import get_server_analytics, forecast_disk, forecast_metric
from reports import generate_csv_metrics, generate_csv_events, generate_pdf_report
from i18n import get_translations

from . import _shared
from ._shared import (
    api_bp,
    logger,
    _require_auth,
    _current_actor,
    _is_backup_admin,
    _server_tier,
    _require_server_permission,
    _require_rbac_admin,
)


@api_bp.route("/runbooks")
def get_runbooks():
    """List all runbooks, optional ?category= filter."""
    category = request.args.get("category")
    runbooks = _shared._db.get_runbooks(category=category)
    return jsonify({"ok": True, "runbooks": runbooks})


@api_bp.route("/runbooks", methods=["POST"])
def create_runbook():
    """Create a custom runbook. Auth required."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    category = (data.get("category") or "general").strip()
    steps_json = data.get("steps_json", "")
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    if not steps_json:
        return jsonify({"ok": False, "error": "Steps JSON is required"}), 400
    # Validate JSON
    try:
        parsed = json.loads(steps_json) if isinstance(steps_json, str) else steps_json
        if not isinstance(parsed, list):
            return jsonify({"ok": False, "error": "Steps must be a JSON array"}), 400
        steps_str = json.dumps(parsed) if not isinstance(steps_json, str) else steps_json
    except (json.JSONDecodeError, TypeError) as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400
    rid = _shared._db.create_runbook(name=name, description=description, category=category,
                              steps_json=steps_str,
                              created_by=flask_session.get("username", "admin"),
                              is_builtin=False)
    if not rid:
        return jsonify({"ok": False, "error": "Runbook with that name already exists"}), 409
    _shared._db.log_audit(flask_session.get("username", "system"), "create_runbook",
                  "runbook", f"Created runbook '{name}'")
    return jsonify({"ok": True, "id": rid}), 201


@api_bp.route("/runbooks/<int:rid>", methods=["PUT"])
def update_runbook(rid):
    """Update a custom runbook (non-builtin only). Auth required."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    rb = _shared._db.get_runbook(rid)
    if not rb:
        return jsonify({"ok": False, "error": "Runbook not found"}), 404
    if rb["is_builtin"]:
        return jsonify({"ok": False, "error": "Cannot edit built-in runbooks"}), 403
    data = request.get_json(silent=True) or {}
    updates = {}
    if "name" in data:
        updates["name"] = data["name"].strip()
    if "description" in data:
        updates["description"] = data["description"].strip()
    if "category" in data:
        updates["category"] = data["category"].strip()
    if "steps_json" in data:
        try:
            parsed = json.loads(data["steps_json"]) if isinstance(data["steps_json"], str) else data["steps_json"]
            if not isinstance(parsed, list):
                return jsonify({"ok": False, "error": "Steps must be a JSON array"}), 400
            updates["steps_json"] = json.dumps(parsed) if not isinstance(data["steps_json"], str) else data["steps_json"]
        except (json.JSONDecodeError, TypeError) as e:
            return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    _shared._db.update_runbook(rid, **updates)
    _shared._db.log_audit(flask_session.get("username", "system"), "update_runbook",
                  "runbook", f"Updated runbook #{rid}: {list(updates.keys())}")
    return jsonify({"ok": True})


@api_bp.route("/runbooks/<int:rid>", methods=["DELETE"])
def delete_runbook(rid):
    """Delete a custom runbook (non-builtin only). Auth required."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    rb = _shared._db.get_runbook(rid)
    if not rb:
        return jsonify({"ok": False, "error": "Runbook not found"}), 404
    if rb["is_builtin"]:
        return jsonify({"ok": False, "error": "Cannot delete built-in runbooks"}), 403
    deleted = _shared._db.delete_runbook(rid)
    if deleted:
        _shared._db.log_audit(flask_session.get("username", "system"), "delete_runbook",
                      "runbook", f"Deleted runbook '{rb['name']}'")
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/runbooks/<int:rid>/execute", methods=["POST"])
def execute_runbook_api(rid):
    """Execute a runbook on a server. Admin RBAC required (per-server, plus
    tier-0 dual-approval enforced inside _require_server_permission)."""
    # Auth must come first so the server_name parse below has a session.
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    server_name = (data.get("server_name") or "").strip()
    dry_run = bool(data.get("dry_run", False))
    if not server_name:
        return jsonify({"ok": False, "error": "server_name is required"}), 400
    # Look up server config
    servers = {s.name: s for s in _shared._config.get_servers()}
    server_cfg = servers.get(server_name)
    if not server_cfg:
        return jsonify({"ok": False, "error": f"Server '{server_name}' not found in config"}), 404
    # Per-server admin RBAC check (also handles tier-0 approval token)
    perm_err = _require_server_permission(server_name, "admin")
    if perm_err:
        actor = flask_session.get("username", "anonymous")
        _shared._db.log_audit(actor, "rbac_denied_runbook_execute", "rbac",
                              f"runbook={rid} server={server_name}")
        return perm_err
    try:
        from runbook_engine import execute_runbook
        # Pass settings so execute_runbook can dispatch failure notifications
        # via email + webhook (see runbook_engine.py NOTIFICATION WIRING docstring).
        exec_id = execute_runbook(
            _shared._db, rid, server_name, server_cfg,
            dry_run=dry_run,
            executed_by=flask_session.get("username", "system"),
            settings=_shared._config.get_settings(),
        )
        return jsonify({"ok": True, "execution_id": exec_id})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        logger.exception("Runbook execution error")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/runbooks/executions")
def get_runbook_executions():
    """Execution history, optional ?server= and ?runbook_id= filters."""
    server = request.args.get("server")
    rb_id = request.args.get("runbook_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    execs = _shared._db.get_runbook_executions(server_name=server, runbook_id=rb_id, limit=limit)
    return jsonify({"ok": True, "executions": execs})


@api_bp.route("/runbooks/executions/<int:exec_id>")
def get_runbook_execution_detail(exec_id):
    """Single execution detail (for polling status)."""
    ex = _shared._db.get_runbook_execution(exec_id)
    if not ex:
        return jsonify({"ok": False, "error": "Execution not found"}), 404
    return jsonify({"ok": True, "execution": ex})


@api_bp.route("/workflow-categories", methods=["GET"])
def get_workflow_categories():
    return jsonify({"ok": True, "categories": _shared._db.get_workflow_categories()})


@api_bp.route("/workflow-categories", methods=["POST"])
def create_workflow_category():
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    color = data.get("color")
    cat_id = _shared._db.create_workflow_category(name, color)
    _shared._db.log_audit(flask_session.get("username", "system"), "create_workflow_category", "workflow_category", f"Created category '{name}'")
    return jsonify({"ok": True, "id": cat_id})


@api_bp.route("/workflow-categories/<int:cat_id>", methods=["PUT"])
def update_workflow_category(cat_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    _shared._db.update_workflow_category(cat_id, **data)
    _shared._db.log_audit(flask_session.get("username", "system"), "update_workflow_category", "workflow_category", f"Updated category {cat_id}")
    return jsonify({"ok": True})


@api_bp.route("/workflow-categories/<int:cat_id>", methods=["DELETE"])
def delete_workflow_category(cat_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    _shared._db.delete_workflow_category(cat_id)
    _shared._db.log_audit(flask_session.get("username", "system"), "delete_workflow_category", "workflow_category", f"Deleted category {cat_id}")
    return jsonify({"ok": True})


@api_bp.route("/workflows", methods=["GET"])
def get_workflows():
    cat = request.args.get("category_id", None, type=int)
    return jsonify({"ok": True, "workflows": _shared._db.get_workflows(category_id=cat)})


@api_bp.route("/workflows/templates", methods=["GET"])
def get_workflow_templates():
    all_wf = _shared._db.get_workflows(include_templates=True)
    templates = [w for w in all_wf if w.get("is_template")]
    return jsonify({"ok": True, "templates": templates})


def _sync_trigger_from_canvas(data):
    """Mirror a trigger block on the canvas onto the workflow row's
    ``trigger_type`` / ``trigger_config`` columns.

    Why: the canvas-block model (operator-facing) and the column model
    (scheduler-facing) need to agree. The scheduler reads only the
    columns, so when the user draws a Schedule block on the canvas with
    "daily at 09:00", we have to copy that into ``trigger_config`` so
    the scheduler thread picks it up. Keeps the existing scheduler loop
    unchanged.

    Rules:
      * If the canvas contains a ``trigger_*`` block, derive trigger_type
        and trigger_config from it (canvas wins over UI dropdown).
      * If multiple trigger blocks are present, take the first and log
        a warning — the UI shouldn't allow this but we're defensive.
      * If no trigger block, leave the existing trigger_type alone (or
        the explicit value the request specified).
    """
    cj = data.get("canvas_json")
    if not cj:
        return
    # canvas_json may already be a JSON string OR a dict depending on
    # caller; normalise to dict so we can scan nodes.
    if isinstance(cj, str):
        try:
            cj = json.loads(cj)
        except Exception:
            return
    if not isinstance(cj, dict):
        return

    # Drawflow shape: cj["drawflow"]["Home"]["data"] is a dict of nodes
    # keyed by id. Each node has .name (block type) and .data (config).
    nodes = (
        (cj.get("drawflow") or {})
        .get("Home", {})
        .get("data", {})
    )
    if not isinstance(nodes, dict):
        return

    _TRIGGER_BLOCK_TO_TYPE = {
        "trigger_manual": "manual",
        "trigger_schedule": "scheduled",
        "trigger_event": "event",
    }
    triggers = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if name in _TRIGGER_BLOCK_TO_TYPE:
            triggers.append((name, node.get("data") or {}))

    if not triggers:
        # No canvas trigger — caller's trigger_type / trigger_config win
        return

    if len(triggers) > 1:
        # Defensive: the UI shouldn't allow more than one trigger but if
        # one slipped through, take the first and continue. The operator
        # will see the chosen trigger in the dropdown after a reload.
        from ._shared import logger as _lg
        _lg.warning(
            "Workflow has %d trigger blocks; using the first (%s)",
            len(triggers), triggers[0][0],
        )

    block_type, block_data = triggers[0]
    data["trigger_type"] = _TRIGGER_BLOCK_TO_TYPE[block_type]
    # The trigger_config column is JSON-shaped. Different trigger types
    # use different keys but we just pass the block's data through — the
    # scheduler / event loop parses what it needs.
    data["trigger_config"] = dict(block_data)


@api_bp.route("/workflows", methods=["POST"])
def create_workflow():
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    import json as _json
    # Canvas trigger block overrides workflow-level trigger fields, so
    # do the sync BEFORE we serialise trigger_config to a JSON string.
    _sync_trigger_from_canvas(data)
    tc = data.get("trigger_config", {})
    cj = data.get("canvas_json", {})
    wf_id = _shared._db.create_workflow(
        name=name,
        description=data.get("description"),
        category_id=data.get("category_id"),
        trigger_type=data.get("trigger_type", "manual"),
        trigger_config=_json.dumps(tc) if isinstance(tc, (dict, list)) else str(tc or "{}"),
        canvas_json=_json.dumps(cj) if isinstance(cj, (dict, list)) else str(cj or "{}"),
    )
    _shared._db.log_audit(flask_session.get("username", "system"), "create_workflow", "workflow", f"Created workflow '{name}'")
    return jsonify({"ok": True, "id": wf_id})


@api_bp.route("/workflows/<int:wf_id>", methods=["PUT"])
def update_workflow(wf_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    import json as _json
    # Same sync as create: a Schedule block dropped onto the canvas of
    # an existing workflow must update trigger_type/trigger_config.
    _sync_trigger_from_canvas(data)
    if "trigger_config" in data and isinstance(data["trigger_config"], (dict, list)):
        data["trigger_config"] = _json.dumps(data["trigger_config"])
    if "canvas_json" in data and isinstance(data["canvas_json"], (dict, list)):
        data["canvas_json"] = _json.dumps(data["canvas_json"])
    _shared._db.update_workflow(wf_id, **data)
    _shared._db.log_audit(flask_session.get("username", "system"), "update_workflow", "workflow", f"Updated workflow {wf_id}")
    return jsonify({"ok": True})


@api_bp.route("/workflows/<int:wf_id>", methods=["DELETE"])
def delete_workflow(wf_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    # Block template deletion
    all_wf = _shared._db.get_workflows(include_templates=True)
    wf = next((w for w in all_wf if w.get("id") == wf_id), None)
    if wf and wf.get("is_template"):
        return jsonify({"ok": False, "error": "Cannot delete a template workflow"}), 400
    _shared._db.delete_workflow(wf_id)
    _shared._db.log_audit(flask_session.get("username", "system"), "delete_workflow", "workflow", f"Deleted workflow {wf_id}")
    return jsonify({"ok": True})


@api_bp.route("/workflows/<int:wf_id>/clone", methods=["POST"])
def clone_workflow(wf_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    new_name = data.get("name", "New Workflow")
    try:
        new_id = _shared._db.clone_workflow(wf_id, new_name=new_name,
                                     created_by=flask_session.get("username", "system"))
    except Exception as e:
        logger.exception("Failed to clone workflow %d", wf_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    _shared._db.log_audit(flask_session.get("username", "system"), "clone_workflow", "workflow", f"Cloned workflow {wf_id} -> {new_id}")
    return jsonify({"ok": True, "id": new_id})


# Block types in workflow_engine that ultimately reach WinRM and therefore
# require an admin RBAC grant on the targeted server. "wait", "send_email",
# "send_webhook", "log_event", "and_gate", "or_gate", "retry" are ambient and
# need no per-server gate.
_WINRM_BLOCK_TYPES = frozenset({
    "check_service", "check_process", "check_port", "check_disk",
    "restart_service", "start_service", "stop_service",
    "run_powershell", "restart_server", "kill_process", "clear_temp",
    "condition",  # condition can run PS expressions on a server
})


@api_bp.route("/workflows/<int:wf_id>/execute", methods=["POST"])
def execute_workflow_api(wf_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    actor = flask_session.get("username", "anonymous")

    # Feature 2.6: dry-run walks the plan without touching servers or notifying.
    dry_run = bool((request.get_json(silent=True) or {}).get("dry_run", False))

    # Load the workflow + parse canvas to enumerate every server it touches.
    wf = None
    try:
        all_wf = _shared._db.get_workflows(include_templates=True)
        wf = next((w for w in all_wf if w.get("id") == wf_id), None)
    except Exception:
        logger.exception("Failed to load workflow %d for RBAC check", wf_id)
        return jsonify({"ok": False, "error": "Failed to load workflow"}), 500
    if not wf:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404

    from workflow_engine import parse_canvas, execute_workflow
    try:
        canvas_raw = wf.get("canvas_json") or "{}"
        canvas = json.loads(canvas_raw) if isinstance(canvas_raw, str) else canvas_raw
        graph = parse_canvas(canvas if isinstance(canvas, dict) else {})
    except Exception as e:
        logger.exception("Failed to parse workflow %d canvas", wf_id)
        return jsonify({"ok": False, "error": f"Invalid workflow canvas: {e}"}), 400

    # Enumerate (server, node_type) pairs and validate per-server admin RBAC.
    seen_servers = set()
    for node_id, node in (graph.get("nodes") or {}).items():
        node_type = node.get("type", "")
        cfg = node.get("config") or {}
        server_name = (cfg.get("server") or "").strip()

        if not server_name:
            # Empty/missing server: ok for ambient blocks; reject for WinRM blocks.
            if node_type in _WINRM_BLOCK_TYPES:
                _shared._db.log_audit(actor, "rbac_denied_workflow_execute", "rbac",
                                      f"workflow={wf_id} node={node_id} type={node_type} "
                                      f"reason=missing_server")
                return jsonify({
                    "ok": False,
                    "error": (f"Workflow node {node_id} ({node_type}) is misconfigured: "
                              f"server field is empty"),
                }), 400
            continue

        if server_name in seen_servers:
            continue
        seen_servers.add(server_name)

        perm_err = _require_server_permission(server_name, "admin")
        if perm_err:
            _shared._db.log_audit(actor, "rbac_denied_workflow_execute", "rbac",
                                  f"workflow={wf_id} server={server_name} "
                                  f"node={node_id} type={node_type}")
            # Reject the WHOLE execution; do not partially run.
            resp, status = perm_err
            try:
                payload = resp.get_json() or {}
            except Exception:
                payload = {}
            payload["ok"] = False
            payload["error"] = (f"Access denied for server {server_name!r} "
                                f"(workflow node {node_id}, type {node_type}): "
                                f"{payload.get('error', 'admin permission required')}")
            payload["server"] = server_name
            return jsonify(payload), status

    exec_id = execute_workflow(_shared._db, wf_id, _shared._config.get_servers, _shared._config.get_settings(),
                               executed_by=actor,
                               trigger_source="dry-run" if dry_run else "manual", dry_run=dry_run)
    _shared._db.log_audit(actor, "execute_workflow", "workflow",
                          f"{'Dry-ran' if dry_run else 'Executed'} workflow {wf_id} "
                          f"(servers={sorted(seen_servers)})")
    return jsonify({"ok": True, "execution_id": exec_id, "dry_run": dry_run})


@api_bp.route("/workflows/executions", methods=["GET"])
def get_workflow_executions():
    wf_id = request.args.get("workflow_id", None, type=int)
    limit = request.args.get("limit", 50, type=int)
    return jsonify({"ok": True, "executions": _shared._db.get_workflow_executions(wf_id, limit)})


@api_bp.route("/workflows/executions/<int:exec_id>", methods=["GET"])
def get_workflow_execution_detail(exec_id):
    detail = _shared._db.get_workflow_execution_detail(exec_id)
    if not detail:
        return jsonify({"ok": False, "error": "Execution not found"}), 404
    return jsonify({"ok": True, "execution": detail})


@api_bp.route("/workflows/executions/<int:exec_id>/cancel", methods=["POST"])
def cancel_workflow_execution(exec_id):
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    _shared._db.cancel_workflow_execution(exec_id)
    _shared._db.log_audit(flask_session.get("username", "system"), "cancel_workflow_execution", "workflow_execution", f"Cancelled execution {exec_id}")
    return jsonify({"ok": True})


@api_bp.route("/workflows/validate-script", methods=["POST"])
def validate_workflow_script():
    """Run a PowerShell script through the sandbox without executing it.
    Returns (ok, reason) so the workflow editor can preview block validity."""
    err = _require_auth()
    if err:
        return err
    from ps_sandbox import validate_script, get_sandbox_settings, lint_script
    data = request.get_json(silent=True) or {}
    script = data.get("script", "")
    settings = _shared._config.get_settings()
    enabled, extras, max_len = get_sandbox_settings(settings)
    if not isinstance(script, str):
        return jsonify({"ok": False, "error": "script must be a string"}), 400
    if len(script) > max_len:
        return jsonify({"ok": False, "valid": False,
                        "reason": f"Script too long ({len(script)} > {max_len} chars)"})
    ok, reason = validate_script(script, allowed_cmdlets=extras, enabled=enabled)
    # Advisories are reported ALONGSIDE validity, never folded into it. A
    # locale-fragile counter path is a likely mistake, not a policy violation,
    # and an operator targeting an English-locale host is entitled to write one.
    # Emitted even when the sandbox is disabled — the footgun does not depend on
    # the allowlist. See docs/plans/BACKLOG.md B-3.
    return jsonify({"ok": True, "valid": ok, "reason": reason or "OK",
                    "sandbox_enabled": enabled,
                    "warnings": lint_script(script)})
