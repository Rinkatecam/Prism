"""Misc endpoints — split out from the original routes/api.py."""

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


@api_bp.route("/logs/search")
def search_logs():
    """Search log messages across all servers."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"error": "Missing 'q' query parameter"}), 400
        if len(query) > 200:
            return jsonify({"error": "Query too long (max 200 chars)"}), 400
        server = request.args.get("server", "").strip() or None
        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)
        # Cap result set to prevent DB DoS via wildcard searches
        limit = min(request.args.get("limit", 500, type=int), 2000)

        logs = _shared._db.get_log_search(query, server_name=server, hours=hours)[:limit]
        return jsonify({"query": query, "hours": hours, "logs": logs})
    except Exception:
        logger.exception("Error in GET /api/logs/search")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/collector-status")
def collector_status():
    """Lightweight endpoint returning the last "fresh data" timestamp.

    Drives base.html's prismRefresh poller: when the returned value
    advances, every HTMX partial on the dashboard refreshes.

    Under the legacy collector the value is collector.last_cycle_completed.
    Under v2 there are no cycles — the aggregator processes one Result at
    a time and bumps last_aggregator_tick on each one. We return whichever
    is fresher so the dashboard refreshes correctly in both modes (and in
    "both" mode, where both engines are advancing their own clocks).

    Returning the max of the two avoids any case where the dashboard
    stops refreshing because the operator flipped engines mid-session.
    """
    # Post-R2: read directly from state module. The collector.py __getattr__
    # proxy makes `collector.last_cycle_completed` still work too, but
    # going to the source avoids one extra hop.
    import state as _state_module
    v1_ts = _state_module.last_cycle_completed or 0
    v2_ts = 0
    try:
        from collector_v2 import state as _v2_state
        v2_ts = _v2_state.last_aggregator_tick or 0
    except Exception:
        pass
    return jsonify({"last_cycle": max(v1_ts, v2_ts)})


# /sla/summary was removed on 2026-08-06. It ran 29 x get_status_timeline to
# serve a page that led with availability — a number measured to vary by 0.73
# points across 28 of 29 servers. GET /api/reports/fleet replaces it and
# returns the same availability figures alongside health, from one scan.


@api_bp.route("/ldap/query", methods=["POST"])
def ldap_query():
    """Query AD via LDAP for users or groups.

    Used by the "Query AD" button in Settings → Security & Access → LDAP
    to populate the allowed_users textarea with real sAMAccountName /
    group CN values — avoids typo hell.

    Body (all optional — blank/masked falls back to saved config):
      {
        "kind": "users" | "groups",
        "search": "string",           # substring filter (sAMAccountName or cn)
        "ldap_url": "ldap://...",
        "base_dn": "DC=ad,DC=example,DC=com",
        "bind_user": "admin@ad.example.com",
        "bind_password": "..."          # PASSWORD_MASK = use saved value
      }

    Returns:
      {ok: true, results: [{dn, name, sam, upn, description}, ...], count: N}
    """
    # S3-2 (R8): rbac admin only. Was _require_auth — any LDAP user borrowed
    # the configured service account's directory-read rights and could
    # enumerate the whole AD 200 records at a time. Useful prerequisite for
    # spear-phishing or Kerberoasting target selection.
    auth_err = _shared._require_rbac_admin()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "users").strip().lower()
    if kind not in ("users", "groups"):
        return jsonify({"ok": False, "error": "kind must be 'users' or 'groups'"}), 400

    search = (data.get("search") or "").strip()
    # Sanity: escape LDAP special chars in search
    def _escape(s):
        return (s.replace("\\", r"\5c").replace("*", r"\2a")
                 .replace("(", r"\28").replace(")", r"\29").replace("\0", r"\00"))

    # Pull fallbacks from saved settings (allows the user to test BEFORE saving
    # too — form values win; saved values fill the gaps / handle the password mask).
    saved_auth = _shared._config.get_settings().get("auth") or {}
    ldap_url = (data.get("ldap_url") or "").strip() or saved_auth.get("ldap_url", "")
    base_dn = (data.get("base_dn") or "").strip() or saved_auth.get("ldap_base_dn", "")
    bind_user = (data.get("bind_user") or "").strip() or saved_auth.get("ldap_bind_user", "")
    bind_password = (data.get("bind_password") or "").strip()
    if not bind_password or is_password_masked(bind_password):
        # Use the saved (encrypted) bind password
        bind_password = saved_auth.get("ldap_bind_password", "")
        if bind_password:
            try:
                bind_password = decrypt_password(bind_password)
            except Exception:
                bind_password = ""

    if not ldap_url:
        return jsonify({"ok": False, "error": "LDAP server URL is required"}), 400
    if not base_dn:
        return jsonify({"ok": False, "error": "Base DN is required"}), 400
    if not bind_user or not bind_password:
        return jsonify({"ok": False, "error": "Bind credentials are required"}), 400

    try:
        import ldap3
        from ldap3 import Server, Connection, SIMPLE, SYNC
    except ImportError:
        return jsonify({"ok": False, "error": "ldap3 library not installed on server"}), 500

    # Build filter
    esc = _escape(search) if search else ""
    if kind == "users":
        # AD user: objectCategory=person + objectClass=user, skip disabled/computer accounts
        if esc:
            ldap_filter = (
                f"(&(objectCategory=person)(objectClass=user)"
                f"(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
                f"(|(sAMAccountName=*{esc}*)(displayName=*{esc}*)(userPrincipalName=*{esc}*)))"
            )
        else:
            ldap_filter = (
                "(&(objectCategory=person)(objectClass=user)"
                "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
            )
        attrs = ["sAMAccountName", "displayName", "userPrincipalName", "distinguishedName", "description"]
    else:  # groups
        if esc:
            ldap_filter = f"(&(objectCategory=group)(|(sAMAccountName=*{esc}*)(cn=*{esc}*)))"
        else:
            ldap_filter = "(objectCategory=group)"
        attrs = ["sAMAccountName", "cn", "distinguishedName", "description"]

    try:
        server = Server(ldap_url, get_info=ldap3.NONE, connect_timeout=10)
        conn = Connection(
            server,
            user=bind_user,
            password=bind_password,
            authentication=SIMPLE,
            client_strategy=SYNC,
            auto_bind=False,
            raise_exceptions=False,
            read_only=True,
            receive_timeout=15,
        )
        if not conn.bind():
            detail = str(conn.result.get("description", "bind failed"))
            logger.warning("LDAP query bind failed: %s", detail)
            return jsonify({"ok": False, "error": f"Bind failed: {detail}"}), 401

        # Cap results to 200 — prevents dumping 50k-user domains to the browser
        conn.search(
            search_base=base_dn,
            search_filter=ldap_filter,
            search_scope=ldap3.SUBTREE,
            attributes=attrs,
            size_limit=200,
            time_limit=15,
        )

        results = []
        for entry in conn.entries[:200]:
            e = entry.entry_attributes_as_dict
            sam = (e.get("sAMAccountName") or [""])[0]
            dn = str(entry.entry_dn)
            if kind == "users":
                results.append({
                    "sam": sam,
                    "name": (e.get("displayName") or [sam])[0] or sam,
                    "upn": (e.get("userPrincipalName") or [""])[0],
                    "dn": dn,
                    "description": (e.get("description") or [""])[0],
                })
            else:
                results.append({
                    "sam": sam,
                    "name": (e.get("cn") or [sam])[0] or sam,
                    "upn": "",
                    "dn": dn,
                    "description": (e.get("description") or [""])[0],
                })
        # Sort alphabetically by name for a stable UX
        results.sort(key=lambda r: (r.get("name") or "").lower())
        conn.unbind()
        return jsonify({"ok": True, "results": results, "count": len(results), "kind": kind})
    except Exception as e:
        logger.exception("LDAP query failed")
        # Sanitize: never echo the bind password in the error
        msg = str(e)[:200]
        if bind_password and bind_password in msg:
            msg = msg.replace(bind_password, "***")
        return jsonify({"ok": False, "error": msg}), 500


@api_bp.route("/maintenance-windows")
def get_maintenance_windows():
    auth = _require_auth()
    if auth: return auth
    windows = _shared._config.get_maintenance_windows()
    return jsonify({"ok": True, "windows": windows})


@api_bp.route("/maintenance-windows", methods=["POST"])
def save_maintenance_window():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json(force=True)
    window = data.get("window")
    index = data.get("index", -1)  # -1 means add new
    if not window:
        return jsonify({"ok": False, "error": "window data required"}), 400

    # Validate required fields
    if not window.get("name"):
        return jsonify({"ok": False, "error": "Window name required"}), 400
    if not window.get("servers") or not isinstance(window.get("servers"), list):
        return jsonify({"ok": False, "error": "At least one server must be selected"}), 400
    if not window.get("days") or not isinstance(window.get("days"), list):
        return jsonify({"ok": False, "error": "At least one day must be selected"}), 400
    # Coerce and clamp threshold values 0..100 (no warning >= critical check —
    # keep maintenance flexible since a window may intentionally raise both).
    th = window.get("thresholds") or {}
    for k, v in list(th.items()):
        try:
            th[k] = max(0, min(100, int(v)))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"Invalid threshold value for {k}"}), 400
    window["thresholds"] = th
    window["suppress_alerts"] = bool(window.get("suppress_alerts", False))

    windows = _shared._config.get_maintenance_windows()
    if index >= 0 and index < len(windows):
        windows[index] = window
    else:
        windows.append(window)

    _shared._config.save_maintenance_windows(windows)

    try:
        username = flask_session.get("username", "anonymous")
        _shared._db.log_audit(username, f"{'Updated' if index >= 0 else 'Added'} maintenance window: {window.get('name', '')}", "maintenance")
    except Exception:
        pass

    return jsonify({"ok": True, "windows": windows})


@api_bp.route("/maintenance-windows/<int:index>", methods=["DELETE"])
def delete_maintenance_window(index):
    auth = _require_auth()
    if auth: return auth
    windows = _shared._config.get_maintenance_windows()
    if index < 0 or index >= len(windows):
        return jsonify({"ok": False, "error": "Invalid index"}), 400

    removed = windows.pop(index)
    _shared._config.save_maintenance_windows(windows)

    try:
        username = flask_session.get("username", "anonymous")
        _shared._db.log_audit(username, f"Deleted maintenance window: {removed.get('name', '')}", "maintenance")
    except Exception:
        pass

    return jsonify({"ok": True, "windows": windows})


def _consume_global_destructive_approval(action_name: str):
    """Consume a tier-0-style approval token for a global destructive op
    (factory_reset / data_delete). The approval must have been created with
    server_name='*' and a matching action; consume_approval() enforces
    single-use semantics. Returns None on success, (jsonify, status) on
    failure."""
    actor = flask_session.get("username", "anonymous")
    approval_id_raw = request.args.get("approval_id") if request else None
    if not approval_id_raw:
        return jsonify({
            "ok": False,
            "error": (f"{action_name} requires ?approval_id from a second admin "
                      f"(tier-0 destructive op)"),
            "approval_required": True,
        }), 403
    try:
        approval_id = int(approval_id_raw)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "approval_id must be an integer"}), 400
    consumed = _shared._db.consume_approval(approval_id)
    if not consumed:
        return jsonify({"ok": False, "error": "Approval not found, expired, or already used"}), 403
    if consumed.get("server_name") != "*":
        return jsonify({"ok": False, "error": "Approval is not a global ('*') approval"}), 403
    if consumed.get("action") != action_name:
        return jsonify({"ok": False,
                        "error": f"Approval action mismatch: got {consumed.get('action')!r}, "
                                 f"need {action_name!r}"}), 403
    # S3-6 (BL5): embed the payload (truncated) in the audit row so
    # post-incident reconstruction can answer 'what was approved?' from
    # the append-only hash-chained log alone.
    _payload = (consumed.get("payload_json") or "")[:500]
    _shared._db.log_audit(actor, "tier0_global_approval_consumed", "rbac",
                          f"Approval #{approval_id} consumed for global {action_name}; "
                          f"payload={_payload}")
    return None


@api_bp.route("/data/clean", methods=["POST"])
def clean_all_data():
    """Retention cleanup. Admin RBAC required; no approval token (this is
    just retention pruning, not full data deletion)."""
    auth = _require_rbac_admin()
    if auth:
        actor = flask_session.get("username", "anonymous")
        try:
            _shared._db.log_audit(actor, "rbac_denied_data_clean", "rbac",
                                  "Non-admin attempted retention cleanup")
        except Exception:
            pass
        return auth
    try:
        counts = _shared._db.clean_all_data()
        _shared._db.log_audit(flask_session.get("username", "system"), "clean_data", "system", f"Cleaned all monitoring data: {counts}")
        logger.info("All monitoring data cleaned by %s", flask_session.get("username", "system"))
        return jsonify({"ok": True, "counts": counts})
    except Exception as e:
        logger.exception("Failed to clean data")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/data/delete", methods=["POST"])
def delete_all_data():
    """Wipe all data + servers from config. Requires admin RBAC AND a
    consumed global approval token (server_name='*', action='data_delete')."""
    actor = flask_session.get("username", "anonymous")
    auth = _require_rbac_admin()
    if auth:
        try:
            _shared._db.log_audit(actor, "rbac_denied_data_delete", "rbac",
                                  "Non-admin attempted /api/data/delete")
        except Exception:
            pass
        return auth
    approval_err = _consume_global_destructive_approval("data_delete")
    if approval_err:
        try:
            _shared._db.log_audit(actor, "rbac_denied_data_delete_approval", "rbac",
                                  "Admin attempted /api/data/delete without valid approval token")
        except Exception:
            pass
        return approval_err
    try:
        counts = _shared._db.delete_all_data_and_servers()
        # Remove all servers from config
        import json
        config_path = _shared._config.config_path
        _shared._config.create_backup()
        raw = _shared._config._get_raw_config()
        raw["servers"] = []
        with _shared._config._lock:
            with open(config_path, "w") as f:
                json.dump(raw, f, indent=2)
            _shared._config._cache = None
            _shared._config._cache_mtime = 0.0
        _shared._db.log_audit(flask_session.get("username", "system"), "delete_all", "system", f"Deleted all data and servers: {counts}")
        logger.info("All data and servers deleted by %s", flask_session.get("username", "system"))
        return jsonify({"ok": True, "counts": counts})
    except Exception as e:
        logger.exception("Failed to delete data")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/data/factory-reset", methods=["POST"])
def factory_reset():
    """Wipe ALL state: every DB table, config.json, and on-disk config backups.

    Requires admin RBAC AND a consumed global approval token
    (server_name='*', action='factory_reset').

    Audit log is written BEFORE the DB wipe so it survives — the wiped row is
    irrelevant since the audit_log table is also blown away. We log to the
    application logger as well as a permanent breadcrumb.
    """
    actor = flask_session.get("username", "anonymous")
    auth = _require_rbac_admin()
    if auth:
        try:
            _shared._db.log_audit(actor, "rbac_denied_factory_reset", "rbac",
                                  "Non-admin attempted factory reset")
        except Exception:
            pass
        return auth
    approval_err = _consume_global_destructive_approval("factory_reset")
    if approval_err:
        try:
            _shared._db.log_audit(actor, "rbac_denied_factory_reset_approval", "rbac",
                                  "Admin attempted factory reset without valid approval token")
        except Exception:
            pass
        return approval_err
    user = flask_session.get("username", "system")
    try:
        # Audit FIRST (the table will be wiped, but logger.info persists to disk)
        try:
            _shared._db.log_audit(user, "factory_reset", "system", "Factory reset initiated — wiping all data")
        except Exception:
            pass
        logger.warning("FACTORY RESET initiated by %s — wiping ALL state", user)

        counts = _shared._db.factory_reset()

        # Reset config.json
        import json
        config_path = _shared._config.config_path
        raw = {"servers": [], "settings": {}}
        with _shared._config._lock:
            with open(config_path, "w") as f:
                json.dump(raw, f, indent=2)
            _shared._config._cache = None
            _shared._config._cache_mtime = 0.0

        # Wipe on-disk config backups — a real factory reset should not leave
        # restorable snapshots behind. Delete the directory contents only,
        # not the directory itself.
        try:
            import os
            backup_dir = os.path.join(os.path.dirname(config_path), "config_backups")
            if os.path.isdir(backup_dir):
                removed = 0
                for name in os.listdir(backup_dir):
                    fp = os.path.join(backup_dir, name)
                    if os.path.isfile(fp):
                        try:
                            os.remove(fp)
                            removed += 1
                        except Exception:
                            logger.warning("Failed to remove backup %s", fp, exc_info=True)
                logger.info("Factory reset removed %d config backup files", removed)
        except Exception:
            logger.exception("Failed to wipe config_backups directory")

        logger.warning("Factory reset COMPLETED by %s", user)
        return jsonify({"ok": True, "counts": counts})
    except Exception as e:
        logger.exception("Failed to perform factory reset")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/scheduled-restarts", methods=["GET"])
def get_scheduled_restarts():
    settings = _shared._config.get_settings()
    return jsonify({
        "ok": True,
        "flask_restart": settings.get("scheduled_flask_restart", {"enabled": False, "schedule": "daily", "time": "03:00", "day": "sunday"}),
        "server_restart_schedule": settings.get("scheduled_server_restart_schedule", {"enabled": False, "schedule": "weekly", "time": "03:00", "day": "6", "month_day": 1}),
        "server_restarts": settings.get("scheduled_server_restarts", []),
        "delay_between_seconds": settings.get("restart_delay_between_seconds", 60)
    })


@api_bp.route("/scheduled-restarts", methods=["POST"])
def save_scheduled_restarts():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data provided"}), 400
    actor = flask_session.get("username", "anonymous")
    # Per-server admin RBAC for every entry being scheduled. Skip if the
    # caller is only updating flask_restart / schedule shape and not the
    # per-server list.
    if "server_restarts" in data:
        entries = data.get("server_restarts") or []
        if not isinstance(entries, list):
            return jsonify({"ok": False, "error": "server_restarts must be a list"}), 400
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            srv = (entry.get("server") or entry.get("server_name") or "").strip()
            if not srv:
                continue
            perm_err = _require_server_permission(srv, "admin")
            if perm_err:
                try:
                    _shared._db.log_audit(actor, "rbac_denied_scheduled_restart", "rbac",
                                          f"server={srv}")
                except Exception:
                    pass
                return perm_err
    try:
        import json
        config_path = _shared._config.config_path
        _shared._config.create_backup()
        raw = _shared._config._get_raw_config()
        settings = raw.setdefault("settings", {})
        if "flask_restart" in data:
            settings["scheduled_flask_restart"] = data["flask_restart"]
        if "server_restart_schedule" in data:
            settings["scheduled_server_restart_schedule"] = data["server_restart_schedule"]
        if "server_restarts" in data:
            settings["scheduled_server_restarts"] = data["server_restarts"]
        if "delay_between_seconds" in data:
            settings["restart_delay_between_seconds"] = data["delay_between_seconds"]
        with _shared._config._lock:
            with open(config_path, "w") as f:
                json.dump(raw, f, indent=2)
            _shared._config._cache = None
            _shared._config._cache_mtime = 0.0
        _shared._db.log_audit(flask_session.get("username", "system"), "update_scheduled_restarts", "settings", f"Updated scheduled restart config")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to save scheduled restarts")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/restart-log", methods=["GET"])
def get_restart_log():
    """Get restart schedule execution log."""
    limit = request.args.get("limit", 50, type=int)
    run_id = request.args.get("run_id", None)
    entries = _shared._db.get_restart_log(limit=limit, run_id=run_id)
    return jsonify({"ok": True, "entries": entries})


@api_bp.route("/restart-status", methods=["GET"])
def get_restart_status():
    """Get the latest restart schedule run status."""
    import restart_scheduler
    results = restart_scheduler.get_last_run_results()
    latest_run = _shared._db.get_latest_restart_run()
    return jsonify({"ok": True, "results": results, "latest_run": latest_run})


@api_bp.route("/restart-now", methods=["POST"])
def trigger_restart_now():
    """Manually trigger the server restart schedule.

    Per-server admin RBAC is enforced for every server in the schedule. If
    any server fails the check, the whole trigger is rejected.
    """
    auth = _require_auth()
    if auth: return auth
    actor = flask_session.get("username", "anonymous")
    settings = _shared._config.get_settings()
    scheduled = settings.get("scheduled_server_restarts") or []
    seen = set()
    for entry in scheduled:
        if not isinstance(entry, dict):
            continue
        srv = (entry.get("server") or entry.get("server_name") or "").strip()
        if not srv or srv in seen:
            continue
        seen.add(srv)
        perm_err = _require_server_permission(srv, "admin")
        if perm_err:
            try:
                _shared._db.log_audit(actor, "rbac_denied_restart_now", "rbac",
                                      f"server={srv}")
            except Exception:
                pass
            return perm_err
    import restart_scheduler
    servers = _shared._config.get_raw_servers()
    # Run in background thread
    import threading
    t = threading.Thread(target=restart_scheduler.execute_server_restarts, args=(settings, _shared._db, servers), daemon=True, name="manual-restart")
    t.start()
    _shared._db.log_audit(actor, "manual_restart_trigger", "restart_schedule",
                          f"Manually triggered restart schedule (servers={sorted(seen)})")
    return jsonify({"ok": True, "message": "Restart schedule triggered"})


@api_bp.route("/tags", methods=["GET"])
def get_tags():
    """Return all tags."""
    return jsonify({"ok": True, "tags": _shared._db.get_all_tags()})


@api_bp.route("/tags", methods=["POST"])
def create_tag():
    """Create a new tag."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Tag name is required"}), 400
    color = data.get("color")
    try:
        new_id = _shared._db.create_tag(name, color)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "create_tag", "tags", f"Created tag '{name}' (id={new_id})")
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        logger.exception("Failed to create tag")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/tags/<int:tag_id>", methods=["PUT"])
def update_tag(tag_id):
    """Update an existing tag."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    name = data.get("name")
    color = data.get("color")
    try:
        _shared._db.update_tag(tag_id, name, color)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "update_tag", "tags", f"Updated tag id={tag_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to update tag")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/tags/<int:tag_id>", methods=["DELETE"])
def delete_tag(tag_id):
    """Delete a tag."""
    auth = _require_auth()
    if auth: return auth
    try:
        _shared._db.delete_tag(tag_id)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "delete_tag", "tags", f"Deleted tag id={tag_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to delete tag")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/failed-logins/summary")
def get_failed_logins_summary():
    """Cross-server summary of failed logins in last 24h."""
    conn = _shared._db._get_conn()
    try:
        rows = conn.execute(
            """SELECT server_name, COUNT(*) as count
               FROM failed_logins
               WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', '-24 hours')
               GROUP BY server_name ORDER BY count DESC"""
        ).fetchall()
        return jsonify({"ok": True, "servers": [dict(r) for r in rows]})
    finally:
        conn.close()


@api_bp.route("/servers/<name>/config-changes")
def get_server_config_changes(name):
    """Config changes for a single server."""
    hours = request.args.get("hours", 168, type=int)
    snap_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int)
    changes = _shared._db.get_config_changes(server_name=name, hours=hours,
                                     limit=limit, snapshot_type=snap_type)
    return jsonify({"ok": True, "changes": changes})


@api_bp.route("/servers/<name>/config-snapshot/<snap_type>")
def get_server_config_snapshot(name, snap_type):
    """Latest config snapshot for a server + type."""
    snapshot = _shared._db.get_latest_snapshot(name, snap_type)
    if not snapshot:
        return jsonify({"ok": True, "snapshot": None})
    # Parse data_json for the response
    import json as _json
    try:
        snapshot["data"] = _json.loads(snapshot.get("data_json", "[]"))
    except (ValueError, _json.JSONDecodeError):
        snapshot["data"] = []
    snapshot.pop("data_json", None)
    return jsonify({"ok": True, "snapshot": snapshot})


@api_bp.route("/servers/<name>/config-snapshot", methods=["POST"])
def trigger_config_snapshot(name):
    """Trigger an immediate config snapshot for a server (auth required)."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    server = _shared._config.get_server_by_name(name)
    if not server:
        return jsonify({"ok": False, "error": "Server not found"}), 404

    settings = _shared._config.get_settings()
    drift_cfg = settings.get("drift_detection", {})
    enabled_types = drift_cfg.get("snapshot_types", ["services", "hotfixes", "local_admins"])
    redaction_patterns = drift_cfg.get("redaction_patterns", [])

    try:
        from pypsrp.wsman import WSMan
        from pypsrp.powershell import RunspacePool
        from crypto_utils import decrypt_password
        import drift_detector
        import json as _json

        from winrm_factory import make_wsman
        wsman = make_wsman(server, connection_timeout=15, read_timeout=30)
        total_changes = 0
        with RunspacePool(wsman) as pool:
            snapshots = drift_detector.collect_all_snapshots(pool, enabled_types)
            for snap_type, new_data in snapshots.items():
                if not new_data:
                    continue
                _script, key_field = drift_detector.SNAPSHOT_TYPES.get(snap_type, (None, "Name"))
                prev = _shared._db.get_latest_snapshot(name, snap_type)
                prev_data = []
                if prev:
                    try:
                        prev_data = _json.loads(prev.get("data_json", "[]"))
                    except (ValueError, _json.JSONDecodeError):
                        prev_data = []
                changes = drift_detector.diff_snapshots(prev_data, new_data, key_field)
                if redaction_patterns:
                    for c in changes:
                        if c.get("old_value"):
                            c["old_value"] = drift_detector.redact_sensitive(c["old_value"], redaction_patterns)
                        if c.get("new_value"):
                            c["new_value"] = drift_detector.redact_sensitive(c["new_value"], redaction_patterns)
                data_json = _json.dumps(new_data, default=str)
                _shared._db.insert_config_snapshot(name, snap_type, data_json)
                if changes:
                    _shared._db.insert_config_changes(name, snap_type, changes)
                    total_changes += len(changes)

        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "manual_snapshot", "config",
                      f"Manual snapshot for {name}: {total_changes} changes")
        return jsonify({"ok": True, "changes_found": total_changes,
                        "types_collected": list(snapshots.keys())})
    except Exception as e:
        logger.exception("Manual snapshot failed for %s", name)
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/dependencies")
def get_dependencies():
    """Return all dependency records."""
    try:
        deps = _shared._db.get_all_dependencies()
        return jsonify({"ok": True, "dependencies": deps})
    except Exception:
        logger.exception("Error fetching dependencies")
        return jsonify({"ok": False, "error": "Failed to fetch dependencies"}), 500


@api_bp.route("/dependencies", methods=["POST"])
def add_dependency():
    """Add a new dependency relationship."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        data = request.get_json(force=True)
        server_name = (data.get("server_name") or "").strip()
        depends_on = (data.get("depends_on") or "").strip()
        dependency_type = (data.get("dependency_type") or "service").strip()
        custom_type_name = (data.get("custom_type_name") or "").strip() or None
        target_mode = (data.get("target_mode") or "port").strip()
        port = data.get("port")
        service_name = (data.get("service_name") or "").strip() or None
        process_name = (data.get("process_name") or "").strip() or None
        description = (data.get("description") or "").strip() or None

        if not server_name or not depends_on:
            return jsonify({"ok": False, "error": "server_name and depends_on are required"}), 400
        if server_name == depends_on:
            return jsonify({"ok": False, "error": "A server cannot depend on itself"}), 400

        if port is not None:
            try:
                port = int(port)
            except (ValueError, TypeError):
                port = None

        dep_id = _shared._db.add_dependency(
            server_name=server_name,
            depends_on=depends_on,
            dependency_type=dependency_type,
            port=port,
            description=description,
            custom_type_name=custom_type_name,
            target_mode=target_mode,
            service_name=service_name,
            process_name=process_name,
        )
        _shared._db.log_audit(flask_session.get("username", "system"), "add_dependency",
                      "server_dependencies", f"{server_name} -> {depends_on} ({dependency_type})")
        return jsonify({"ok": True, "id": dep_id})
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return jsonify({"ok": False, "error": "This dependency already exists"}), 409
        logger.exception("Error adding dependency")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/dependencies/<int:dep_id>", methods=["DELETE"])
def delete_dependency(dep_id):
    """Remove a dependency by id."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        _shared._db.remove_dependency(dep_id)
        _shared._db.log_audit(flask_session.get("username", "system"), "remove_dependency",
                      "server_dependencies", f"Removed dependency #{dep_id}")
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Error removing dependency %d", dep_id)
        return jsonify({"ok": False, "error": "Failed to remove dependency"}), 500


@api_bp.route("/topology/data")
def topology_data():
    """Return the interactive-canvas topology payload: nodes, edges, layout
    positions, live status, and per-node dependency lists. This is what
    static/js/topology.js calls on page load and on every prismRefresh.

    Reads from the in-memory latest_by_server cache (populated by the
    collector at the end of every cycle) instead of hitting SQLite, so the
    request is cheap enough to poll frequently.
    """
    try:
        from topology import build_topology_data
        # Post-R2: import shared state directly from its canonical home.
        import state as _state_module
        deps = _shared._db.get_all_dependencies()
        servers_config = {s.name: s for s in _shared._config.get_servers()}
        # S2-4 / P3 from AUDIT-2026-05: read latest_by_server under _state_lock
        # and snapshot to a local dict before passing to build_topology_data.
        # Concurrent writers can otherwise produce an intermittent 500
        # ("topology pane is sometimes empty") when build_topology_data
        # iterates a dict that's being .update()'d.
        with _state_module._state_lock:
            latest_cache = dict(_state_module.latest_by_server or {})
        # Cold-cache fallback: if the collector hasn't populated latest_by_server
        # yet (e.g. right after a Prism restart), fall back to a one-shot DB read.
        if not latest_cache:
            try:
                rows = _shared._db.get_latest_all()
                latest_cache = {r["server_name"]: r for r in rows}
            except Exception:
                latest_cache = {}
        payload = build_topology_data(deps, servers_config, latest_cache)
        return jsonify({"ok": True, **payload})
    except Exception:
        logger.exception("Error building topology data")
        return jsonify({"ok": False, "error": "Internal error"}), 500


@api_bp.route("/topology/svg")
def topology_svg():
    """Return SVG dependency map."""
    try:
        from topology import generate_dependency_svg
        deps = _shared._db.get_all_dependencies()
        # Build server statuses from latest metrics
        latest = _shared._db.get_latest_all()
        statuses = {m["server_name"]: m["status"] for m in latest}
        highlight = request.args.get("highlight")
        dark = request.args.get("dark", "0") == "1"
        svg = generate_dependency_svg(deps, statuses, highlight_server=highlight, dark_mode=dark)
        return Response(svg, mimetype="image/svg+xml")
    except Exception:
        logger.exception("Error generating topology SVG")
        return Response('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="60">'
                        '<text x="10" y="35" fill="red" font-size="14">Error generating map</text></svg>',
                        mimetype="image/svg+xml")


@api_bp.route("/topology/blast-radius/<server_name>")
def blast_radius(server_name):
    """Return list of servers in the blast radius of a given server."""
    try:
        from topology import get_blast_radius
        deps = _shared._db.get_all_dependencies()
        affected = get_blast_radius(deps, server_name)
        return jsonify({"ok": True, "server": server_name, "affected": affected})
    except Exception:
        logger.exception("Error computing blast radius for %s", server_name)
        return jsonify({"ok": False, "error": "Failed to compute blast radius"}), 500
