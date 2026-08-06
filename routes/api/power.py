"""Power endpoints — split out from the original routes/api.py."""

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


@api_bp.route("/restart", methods=["POST"])
def restart_server():
    """Restart the Flask/waitress process. Used after security settings change.

    Closes audit finding R6 (S2-3 from AUDIT-2026-05). Bouncing the Flask
    process takes the dashboard offline and stops the collector — repeated
    calls keep monitoring blank, which an attacker can use to mask other
    activity. Restricted to backup admin / wildcard-admin and rate-limited
    at 5/hour so it can't be hammered.
    """
    auth_err = _require_rbac_admin()
    if auth_err:
        return auth_err

    import os
    import sys
    import threading

    actor = _current_actor()
    logger.info("Server restart requested via API by %s", actor)
    try:
        _shared._db.log_audit(actor, "flask_restart", "system",
                              "Flask/waitress process restart via /api/restart")
    except Exception:
        logger.debug("audit log write failed for flask_restart")

    def _do_restart():
        """Give the response time to send, then restart."""
        import time
        time.sleep(1)
        logger.info("Restarting server process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Server restarting..."})


def _clear_reboot_state(name: str, action: str):
    """When we successfully send a restart to a server:

      * clear pending-reboot / reboot-required flags on ``server_update_info``
        (the install completed when the user kicked this; or the user is
        rebooting for unrelated reasons — either way the WU truth changes
        on the next post-restart WU check)
      * transition ``_update_install_state`` to ``status="rebooting"`` so the
        dashboard tile keeps showing "Rebooting" through the unreachable
        window. The aggregator clears it when metrics return; a janitor
        gives up after 20 min.
      * arm accelerated polling for 20 min so the came-back-online moment
        is detected within seconds.
    """
    if action == "restart":
        from .updates import _set_rebooting_state
        _set_rebooting_state(name, actor="user:manual_restart")
        info = server_update_info.get(name)
        if info:
            info.pop("pending_reboot", None)
            info.pop("reboot_required", None)
            info.pop("last_install", None)
        # Long acceleration window — covers the reboot itself plus a buffer
        # for stabilising metrics to flow. Re-armed by the aggregator when
        # the server actually comes back.
        accelerate_server(name, duration_s=20 * 60, reason="manual_restart")
        logger.debug("Marked %s as rebooting after manual restart command", name)


@api_bp.route("/servers/<name>/power", methods=["POST"])
def server_power_action(name: str):
    """Execute a power action (restart/shutdown) on a remote server via WinRM."""
    auth_err = _require_server_permission(name, "admin")
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        data = request.get_json(silent=True) or {}
        action = data.get("action", "").strip().lower()

        if action not in ("restart", "shutdown"):
            return jsonify({"ok": False, "error": "Invalid action. Use 'restart' or 'shutdown'."}), 400

        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman
        wsman = make_wsman(cfg, connection_timeout=15, read_timeout=15)

        # Use shutdown.exe with a 5-second delay. Restart-Computer -Force and
        # Stop-Computer -Force both slam the machine down SO fast that the
        # WinRM session dies mid-invoke — pypsrp then throws a socket error
        # and the endpoint returns 500 even though the power action actually
        # succeeded. shutdown.exe /t 5 gives us a 5-second grace window to
        # return the HTTP response before the system actually goes down.
        if action == "restart":
            script = r"shutdown.exe /r /t 5 /f /c 'Prism: scheduled restart'"
        else:
            script = r"shutdown.exe /s /t 5 /f /c 'Prism: scheduled shutdown'"

        # Swallow the "session ended" class of errors — they're the expected
        # outcome when the target actually goes down during our call.
        _EXPECTED_DEATHS = (
            "winrm", "session", "pipeline", "runspace",
            "connection was forcibly closed", "existing connection was forcibly closed",
            "transport connection", "closed unexpectedly",
        )
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(script)
                ps.invoke()

                if ps.had_errors:
                    err_msgs = [str(e) for e in ps.streams.error]
                    err_str = "; ".join(err_msgs)[:300]
                    # If the error is one of the expected "session died" kinds,
                    # treat it as success — shutdown.exe definitely ran.
                    if any(phrase in err_str.lower() for phrase in _EXPECTED_DEATHS):
                        logger.info("Power action '%s' sent to %s (session closed as expected)", action, name)
                        _clear_reboot_state(name, action)
                        _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
                        _shared._db.log_audit(_audit_user, f"power:{action}", "server", f"{name} (session closed as expected)")
                        return jsonify({"ok": True, "action": action, "server": name, "note": "WinRM session closed by reboot"})
                    logger.warning("Power action '%s' on %s failed: %s", action, name, err_str)
                    return jsonify({"ok": False, "error": err_str}), 500
        except Exception as invoke_err:
            err_str = str(invoke_err)
            if any(phrase in err_str.lower() for phrase in _EXPECTED_DEATHS):
                logger.info("Power action '%s' sent to %s (connection closed as expected)", action, name)
                _clear_reboot_state(name, action)
                _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
                _shared._db.log_audit(_audit_user, f"power:{action}", "server", f"{name} (session closed as expected)")
                return jsonify({"ok": True, "action": action, "server": name, "note": "WinRM session closed by reboot"})
            raise

        logger.info("Power action '%s' sent to %s", action, name)
        _clear_reboot_state(name, action)
        _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
        _shared._db.log_audit(_audit_user, f"power:{action}", "server", f"{name}")
        return jsonify({"ok": True, "action": action, "server": name})

    except Exception as e:
        logger.exception("Error executing power action on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/wol", methods=["POST"])
def server_wake_on_lan(name: str):
    """Send a Wake-on-LAN magic packet to power on a server."""
    auth_err = _require_server_permission(name, "control")
    if auth_err:
        return auth_err
    # F-078 remediation: Wake-on-LAN is a remote power-state action and
    # belongs in the audit trail alongside restart/shutdown.
    _shared._db.log_audit(
        username=flask_session.get("username", "system"),
        action="power:wol",
        category="power",
        details=f"server={name}",
    )
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        mac = getattr(cfg, "mac_address", None) or ""
        if not mac:
            # Try from config dict
            for s in _shared._config.get_config().get("servers", []):
                if s.get("name") == name:
                    mac = s.get("mac_address", "")
                    break

        if not mac:
            return jsonify({"ok": False, "error": "No MAC address configured for this server"}), 400

        # Clean MAC address
        mac_clean = mac.replace(":", "").replace("-", "").replace(".", "").strip()
        if len(mac_clean) != 12:
            return jsonify({"ok": False, "error": "Invalid MAC address format"}), 400

        import socket
        import struct
        # Build magic packet: 6x 0xFF + 16x MAC
        mac_bytes = bytes.fromhex(mac_clean)
        magic = b'\xff' * 6 + mac_bytes * 16
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, ('<broadcast>', 9))
        sock.close()

        logger.info("Wake-on-LAN packet sent for %s (MAC: %s)", name, mac)
        return jsonify({"ok": True, "server": name, "mac": mac})

    except Exception as e:
        logger.exception("Error sending WoL for %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
