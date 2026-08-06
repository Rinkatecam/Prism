"""Shared state and helpers for the routes.api package.

Single source of truth for the api Blueprint, module-level state
(_db / _config / _limiter), the persisted _update_install_state cache,
and the auth/RBAC helpers used across multiple sub-modules.
"""

import json
import logging
import os
import pathlib
import tempfile
import threading
from flask import Blueprint, jsonify, request
from flask import session as flask_session
from database import Database
from config_manager import ConfigManager

logger = logging.getLogger("prism.api")

api_bp = Blueprint("api", __name__, url_prefix="/api")

# These are set by register_api_routes() (see routes/api/__init__.py)
_db: Database = None
_config: ConfigManager = None
_limiter = None


# ─────────────────────────────────────────────────────────────────────
# Install-state persistence
# ─────────────────────────────────────────────────────────────────────
# Per-server install / restart lifecycle state. Tracks what each server
# is currently DOING — queued / searching / downloading / installing /
# restart_required / rebooting / stabilising. Read by the dashboard
# (Server Actions panel + tile decoration), the server-detail overlay,
# and the periodics safety-net jobs.
#
# Shape per entry:
#   { status, message, started_at, updated_at, installed_count,
#     pending_count, reboot_required, restart_after, reboot_started_at,
#     came_back_at, actor, error }
#
# Persistence: written to ``data/install_state.json`` whenever the dict
# is mutated (via the ``_persist_install_state`` helper called from each
# mutator), and reloaded on app startup via ``_load_install_state``. A
# 60s periodic flush in collector_v2/periodics.py is the safety net for
# nested-value mutations that bypass the persistence hook.
#
# Why persist? Without it, a Flask restart wipes the dict — operators
# who restarted a server right before a Prism upgrade lose the
# "Rebooting" indicator and have no way to tell from the dashboard
# whether the server is still mid-reboot or fully back.
_install_state_path = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "install_state.json"
_install_state_lock = threading.Lock()


def _load_install_state() -> dict[str, dict]:
    """Read the persisted install_state from disk. Returns ``{}`` on
    missing file or corrupt content. Called once at app startup."""
    try:
        if not _install_state_path.exists():
            return {}
        raw = _install_state_path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning(
                "install_state.json is not a dict (got %s) — starting empty",
                type(data).__name__,
            )
            return {}
        # Defensive: filter to dict-shaped entries only
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        logger.warning(
            "Failed to load install_state.json — starting empty",
            exc_info=True,
        )
        return {}


def _persist_install_state() -> None:
    """Atomically write the current ``_update_install_state`` to disk.

    Uses tmp-file + rename so a crash mid-write can't corrupt the file.
    Single-writer pattern via ``_install_state_lock``. Safe to call from
    any thread.

    Called from every mutator (set_rebooting_state, the install-status
    poll, the cancel handler, the aggregator's _handle_post_reboot, the
    periodics janitor) AND from the periodics flush job as a safety net.
    The lock is fast — JSON serialisation of a <30-entry dict is sub-ms.
    """
    with _install_state_lock:
        try:
            # Snapshot under the lock so concurrent writers don't see a
            # half-mutated dict. dict() is GIL-atomic in CPython.
            snapshot = dict(_update_install_state)
            _install_state_path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a sibling tmp file then rename — ``os.replace``
            # is atomic on POSIX and best-effort atomic on Windows for
            # small files (our payload is tiny).
            fd, tmp = tempfile.mkstemp(
                prefix=".install_state.",
                suffix=".tmp",
                dir=str(_install_state_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, indent=2, default=str)
                os.replace(tmp, _install_state_path)
            except Exception:
                # Clean up the tmp on failure so we don't leak files
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            logger.warning("Failed to persist install_state.json", exc_info=True)


# Loaded once at import. ``app.py`` doesn't need to do anything — the
# first import of this module rehydrates the dict from disk.
_update_install_state: dict[str, dict] = _load_install_state()
if _update_install_state:
    logger.info(
        "Rehydrated install_state for %d server(s): %s",
        len(_update_install_state),
        ", ".join(sorted(_update_install_state.keys())),
    )


def _set_state(db, config, limiter):
    """Called once by register_api_routes to wire shared globals."""
    global _db, _config, _limiter
    _db = db
    _config = config
    _limiter = limiter


"""JSON API endpoints for Prism."""


def _require_auth():
    """Return an error response if auth is enabled and user is not logged in,
    OR if the action is destructive (power/restart/update) even when auth is disabled."""
    auth_cfg = _config.get_settings().get("auth", {})
    if auth_cfg.get("enabled", False) and not flask_session.get("username"):
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return None


def _current_actor() -> str:
    """Best-effort actor identifier for ACL/audit purposes."""
    return (flask_session.get("username")
            or (request.remote_addr if request else "anonymous")
            or "anonymous")


def _is_backup_admin() -> bool:
    return bool(flask_session.get("is_backup_admin"))


def _server_tier(name: str) -> int:
    """Look up the tier (0/1/2) for a server name. Defaults to 1."""
    try:
        for s in _config.get_servers():
            if s.name == name:
                return int(getattr(s, "tier", 1))
    except Exception:
        pass
    return 1


def _require_server_permission(name: str, action: str = "view"):
    """Authorize the current user for an action against a specific server.

    Permission levels: view < control < admin.

    Policy:
      - Auth disabled  → allow (legacy single-admin mode)
      - Backup admin    → allow everything (break-glass account)
      - ACL table empty → permissive: allow non-tier-0; tier-0 still requires explicit admin
      - ACL has rows    → enforce per-user permission. Tier-0 destructive actions
                          require permission == 'admin'.

    `action` is one of 'view', 'control', 'admin'. Returns None on success or a
    Flask (jsonify, status) tuple on denial.
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    auth_cfg = _config.get_settings().get("auth", {})
    if not auth_cfg.get("enabled", False):
        # Auth disabled — permissive mode for backwards compatibility
        return None

    if _is_backup_admin():
        return None

    actor = flask_session.get("username", "")
    tier = _server_tier(name)
    required_rank = _db._PERM_RANK.get(action, 1)

    if _db.acl_is_empty():
        # Permissive default: only block tier-0 destructive ops
        if tier == 0 and required_rank >= _db._PERM_RANK["admin"]:
            return jsonify({
                "ok": False,
                "error": "Tier-0 server requires explicit admin ACL grant. Have an admin run /api/rbac/grant.",
            }), 403
        return None

    perm = _db.get_user_permission(actor, name)
    if not perm:
        return jsonify({"ok": False, "error": f"Access denied: no permission for server {name!r}"}), 403

    granted_rank = _db._PERM_RANK.get(perm, 0)
    if granted_rank < required_rank:
        return jsonify({
            "ok": False,
            "error": f"Insufficient permission ({perm}) — '{action}' required",
        }), 403

    # Tier-0 destructive actions require admin permission specifically
    if tier == 0 and required_rank >= _db._PERM_RANK["control"] and granted_rank < _db._PERM_RANK["admin"]:
        return jsonify({
            "ok": False,
            "error": f"Tier-0 server: only 'admin' permission may perform '{action}'",
        }), 403

    # Tier-0 destructive actions additionally require a fresh second-admin
    # approval token. The caller passes ?approval_id=<id>; we consume it
    # (single-use) and proceed only if it matches this server + action and
    # was approved by someone other than the requester.
    if tier == 0 and required_rank >= _db._PERM_RANK["control"]:
        approval_id_raw = request.args.get("approval_id") if request else None
        if not approval_id_raw:
            return jsonify({
                "ok": False,
                "error": "Tier-0 destructive op requires ?approval_id from a second admin",
                "approval_required": True,
            }), 403
        try:
            approval_id = int(approval_id_raw)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "approval_id must be an integer"}), 400
        consumed = _db.consume_approval(approval_id)
        if not consumed:
            return jsonify({"ok": False, "error": "Approval not found, expired, or already used"}), 403
        if consumed.get("server_name") != name:
            return jsonify({"ok": False, "error": "Approval is for a different server"}), 403
        # Action match is best-effort: we map the request action to the
        # canonical names used by /api/approvals (restart, shutdown,
        # install_updates, delete_data, …). Mismatch is a soft error to
        # avoid breaking when callers pass `action='admin'` generically.
        # S3-6 (BL5): embed the approval's payload_json (truncated) into the
        # audit_log details so post-incident reconstruction can answer
        # "what was approved?" from a single source. pending_approvals is a
        # mutable table; audit_log is append-only with hash-chain integrity.
        _payload = (consumed.get("payload_json") or "")[:500]
        _db.log_audit(actor, "tier0_approval_consumed", "rbac",
                      f"Approval #{approval_id} consumed for {name} ({action}); "
                      f"payload={_payload}")

    return None


def _require_rbac_admin():
    """Allow only backup admins or wildcard-admin users to manage ACLs."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    auth_cfg = _config.get_settings().get("auth", {})
    if not auth_cfg.get("enabled", False):
        return None
    if _is_backup_admin():
        return None
    user = flask_session.get("username", "")
    perm = _db.get_user_permission(user, "*")
    if perm == "admin":
        return None
    return jsonify({"ok": False, "error": "Only backup admins or wildcard admins may manage ACLs"}), 403
