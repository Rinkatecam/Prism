"""Rbac endpoints — split out from the original routes/api.py."""

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


@api_bp.route("/audit-log")
def get_audit_log():
    auth = _require_auth()
    if auth: return auth
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    category = request.args.get("category", "").strip() or None
    entries = _shared._db.get_audit_log(limit=limit, offset=offset, category=category)
    return jsonify({"ok": True, "entries": entries})


@api_bp.route("/audit-log/export")
def export_audit_log():
    auth = _require_auth()
    if auth: return auth
    category = request.args.get("category", "").strip() or None
    csv_data = _shared._db.export_audit_csv(category=category)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"}
    )


@api_bp.route("/rbac/acl", methods=["GET"])
def list_rbac_acl():
    err = _require_rbac_admin()
    if err:
        return err
    return jsonify({"ok": True, "acl": _shared._db.list_acl(), "permissive_mode": _shared._db.acl_is_empty()})


@api_bp.route("/rbac/grant", methods=["POST"])
def grant_rbac_acl():
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    server = (data.get("server_name") or "").strip()
    perm = (data.get("permission") or "").strip().lower()
    if not username or not server or perm not in ("view", "control", "admin"):
        return jsonify({"ok": False, "error": "username, server_name, permission(view|control|admin) required"}), 400
    actor = _current_actor()
    rec_id = _shared._db.grant_acl(username, server, perm, granted_by=actor)
    _shared._db.log_audit(actor, "rbac_grant", "rbac",
                  f"Granted '{perm}' on '{server}' to '{username}' (id={rec_id})")
    return jsonify({"ok": True, "id": rec_id})


@api_bp.route("/rbac/revoke", methods=["POST"])
def revoke_rbac_acl():
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    server = (data.get("server_name") or "").strip()
    if not username or not server:
        return jsonify({"ok": False, "error": "username and server_name required"}), 400
    n = _shared._db.revoke_acl(username, server)
    actor = _current_actor()
    _shared._db.log_audit(actor, "rbac_revoke", "rbac",
                  f"Revoked ACL on '{server}' for '{username}' ({n} row(s))")
    return jsonify({"ok": True, "removed": n})


@api_bp.route("/rbac/me")
def rbac_self():
    """Return the current user's effective permissions across all servers."""
    err = _require_auth()
    if err:
        return err
    user = flask_session.get("username", "")
    auth_enabled = _shared._config.get_settings().get("auth", {}).get("enabled", False)
    permissive = _shared._db.acl_is_empty()
    perms = {}
    try:
        for s in _shared._config.get_servers():
            tier = int(getattr(s, "tier", 1))
            if not auth_enabled:
                eff = "admin"
            elif _is_backup_admin():
                eff = "admin"
            elif permissive:
                eff = "control" if tier == 0 else "admin"
            else:
                eff = _shared._db.get_user_permission(user, s.name) or "none"
            perms[s.name] = {"permission": eff, "tier": tier}
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "user": user,
        "is_backup_admin": _is_backup_admin(),
        "auth_enabled": auth_enabled,
        "permissive_mode": permissive,
        "permissions": perms,
    })


@api_bp.route("/approvals", methods=["GET"])
def list_approvals():
    err = _require_auth()
    if err:
        return err
    include_decided = request.args.get("all") == "1"
    return jsonify({"ok": True, "approvals": _shared._db.list_pending_approvals(include_decided)})


@api_bp.route("/approvals", methods=["POST"])
def create_approval():
    err = _require_auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    server = (data.get("server_name") or "").strip()
    action = (data.get("action") or "").strip()
    payload = json.dumps(data.get("payload", {}))
    if not server or not action:
        return jsonify({"ok": False, "error": "server_name and action required"}), 400
    actor = _current_actor()
    aid = _shared._db.create_approval_request(actor, server, action, payload)
    _shared._db.log_audit(actor, "approval_requested", "rbac", f"Approval #{aid}: {action} on {server}")
    return jsonify({"ok": True, "id": aid})


@api_bp.route("/audit-log/archive", methods=["POST"])
def archive_audit_log():
    """Append the audit_log to a JSONL file under data/audit_archive/.

    Audit rows are NEVER deleted (append-only triggers); this is a one-way
    snapshot for SIEM ingest or compliance hand-off. Subsequent calls only
    append rows newer than the last archive (caller may pass `since_days`
    to limit the window).
    """
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    older = data.get("older_than_days")
    older_int = int(older) if older not in (None, "", 0) else None
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    archive_dir = _Path(__file__).resolve().parent.parent / "data" / "audit_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    fname = archive_dir / f"audit_{_dt.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl"
    n = _shared._db.export_audit_log_jsonl(fname, older_than_days=older_int)
    # F-AT-X (this audit's scope): the archive action itself is
    # audit-worthy. Snapshotting the audit log moves regulated material
    # to cold storage; we must record who did it and how many rows.
    _shared._db.log_audit(
        username=flask_session.get("username", "system"),
        action="audit_archive",
        category="security",
        details=f"rows={n}, file={fname.name}, older_than_days={older_int}",
    )
    return jsonify({"ok": True, "rows": n, "file": str(fname.relative_to(_Path(__file__).resolve().parent.parent))})


# ── S2-1 (BL3): containment endpoints — kill session, disable/enable user ──
@api_bp.route("/admin/kill-session", methods=["POST"])
def admin_kill_session():
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    login_time = (data.get("login_time") or "").strip()
    if not username or not login_time:
        return jsonify({"ok": False, "error": "username and login_time required"}), 400
    actor = _current_actor()
    _shared._db.revoke_session(username, login_time, by=actor)
    _shared._db.log_audit(actor, "session_killed", "auth",
                          f"target={username} login_time={login_time}")
    return jsonify({"ok": True})


@api_bp.route("/admin/disable-user", methods=["POST"])
def admin_disable_user():
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    reason = (data.get("reason") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    actor = _current_actor()
    _shared._db.disable_user(username, by=actor, reason=reason)
    _shared._db.log_audit(actor, "user_disabled", "auth",
                          f"target={username} reason={reason[:120]}")
    return jsonify({"ok": True})


@api_bp.route("/admin/enable-user", methods=["POST"])
def admin_enable_user():
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    actor = _current_actor()
    n = _shared._db.enable_user(username)
    _shared._db.log_audit(actor, "user_enabled", "auth",
                          f"target={username} removed={n}")
    return jsonify({"ok": True, "removed": n})


@api_bp.route("/admin/active-sessions", methods=["GET"])
def admin_active_sessions():
    """Best-effort: Flask doesn't expose an active-session registry, so we
    return the list of revoked sessions + disabled users + recent failures
    for operator visibility. Sprint 2 ships this as a placeholder; a real
    registry needs server-side session storage (Sprint 3+)."""
    err = _require_rbac_admin()
    if err:
        return err
    return jsonify({
        "ok": True,
        "active": [],  # No registry available without server-side session store
        "revoked": _shared._db.list_revoked_sessions(limit=200),
        "disabled": _shared._db.list_disabled_users(),
    })


# ── S2-15 (W7): LDAP health snapshot ──────────────────────────────────────
@api_bp.route("/system/ldap-health", methods=["GET"])
def system_ldap_health():
    err = _require_auth()
    if err:
        return err
    try:
        from auth import get_ldap_health
        snap = get_ldap_health()
    except Exception:
        snap = {"ok": False, "last_check": None, "last_error": "probe unavailable", "url": ""}
    return jsonify({"ok": True, **snap})


@api_bp.route("/approvals/<int:approval_id>/decide", methods=["POST"])
def decide_approval_endpoint(approval_id: int):
    err = _require_rbac_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    approved = bool(data.get("approved", False))
    actor = _current_actor()
    ok = _shared._db.decide_approval(approval_id, actor, approved)
    if not ok:
        return jsonify({"ok": False, "error": "Cannot decide (already decided, expired, or self-approval)"}), 400
    _shared._db.log_audit(actor, "approval_decided", "rbac",
                  f"Approval #{approval_id}: {'approved' if approved else 'rejected'}")
    return jsonify({"ok": True})
