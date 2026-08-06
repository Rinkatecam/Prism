"""Servers endpoints — split out from the original routes/api.py."""

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


@api_bp.route("/servers")
def get_servers():
    """Latest metrics for all servers, merged with config data."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        servers_config = {s.name: s for s in _shared._config.get_servers()}
        latest_metrics = _shared._db.get_latest_all()

        result = []
        # Include servers that have metrics
        seen = set()
        for m in latest_metrics:
            name = m["server_name"]
            seen.add(name)
            cfg = servers_config.get(name)
            result.append({
                "name": name,
                "host": cfg.host if cfg else "unknown",
                "type": cfg.type if cfg else "unknown",
                "status": m["status"],
                "cpu": m["cpu_percent"],
                "ram": m["ram_percent"],
                "disk_c": m["disk_c_percent"],
                "disk_d": m["disk_d_percent"],
                "last_check": m["timestamp"],
                "collection_time_ms": m["collection_time_ms"],
                "auth_protocol": server_auth_info.get(name, "unknown"),
            })

        # Include configured servers that haven't been collected yet
        for name, cfg in servers_config.items():
            if name not in seen:
                result.append({
                    "name": name,
                    "host": cfg.host,
                    "type": cfg.type,
                    "status": "unknown",
                    "cpu": None, "ram": None, "disk_c": None, "disk_d": None,
                    "last_check": None,
                    "collection_time_ms": None,
                    "auth_protocol": server_auth_info.get(name, "unknown"),
                })

        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /api/servers")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/<name>")
def get_server(name: str):
    """Latest metrics + thresholds for a specific server."""
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"error": f"Server '{name}' not found in config"}), 404

        metrics = _shared._db.get_latest_by_server(name)
        events = _shared._db.get_server_events(name, limit=50)

        result = {
            "name": name,
            "host": cfg.host,
            "type": cfg.type,
            "thresholds": cfg.thresholds,
            "events": events,
        }

        if metrics:
            result.update({
                "status": metrics["status"],
                "cpu": metrics["cpu_percent"],
                "ram": metrics["ram_percent"],
                "disk_c": metrics["disk_c_percent"],
                "disk_d": metrics["disk_d_percent"],
                "last_check": metrics["timestamp"],
            })
        else:
            result.update({
                "status": "unknown",
                "cpu": None, "ram": None, "disk_c": None, "disk_d": None,
                "last_check": None,
            })

        # Fused-verdict evidence (elevated-normal markers + reasons) lives
        # only in the aggregator's hot cache, not the DB row — optional by
        # contract (DETECTION_FUSION_PLAN §8); absent on a cold cache.
        # NB: keep this AFTER the if/else above — an earlier version placed
        # this try between `if metrics:` and its `else:`, which rebound the
        # else to the try and clobbered real metrics with unknown on every
        # request (broke every server-detail page).
        try:
            from state import latest_by_server, _state_lock
            with _state_lock:
                cached = latest_by_server.get(name)
            if cached and cached.get("verdict_detail"):
                result["verdict_detail"] = cached["verdict_detail"]
        except Exception:
            pass

        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /api/servers/%s", name)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/history")
def get_server_history(name: str):
    """Time-series metric data for a server."""
    try:
        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)  # Cap at 30 days

        history = _shared._db.get_server_history(name, hours=hours)
        restart_events = _shared._db.get_server_restart_events(name, hours=hours)
        return jsonify({
            "name": name, "hours": hours, "data": history,
            "restart_events": restart_events,
        })
    except Exception:
        logger.exception("Error in GET /api/servers/%s/history", name)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/sparklines")
def get_fleet_sparklines():
    """Downsampled 24h 'worst resource %' series per server for card sparklines.

    ONE grouped query for the whole fleet; the servers page fetches this on a
    slow interval and injects a tiny SVG polyline into each card.
    """
    try:
        hours = min(request.args.get("hours", 24, type=int) or 24, 168)
        buckets = min(max(request.args.get("buckets", 24, type=int) or 24, 4), 96)
        series = _shared._db.get_fleet_sparklines(hours=hours, buckets=buckets)
        return jsonify({"ok": True, "hours": hours, "buckets": buckets, "series": series})
    except Exception:
        logger.exception("Error in GET /api/servers/sparklines")
        return jsonify({"ok": False, "error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/restart-readiness")
def get_server_restart_readiness(name: str):
    """Probe a server to determine whether it's *fully* back from a restart.

    The old restart overlay said "back online" as soon as WinRM responded.
    On a cumulative-update reboot the server may answer WinRM at the login
    screen, then reboot AGAIN to apply stage 2. The overlay would lie and
    the user would walk away.

    A server is fully back when ALL of these are true:
      1. WinRM responds (we can run a script).
      2. No pending-reboot flag in the registry (CBS / WindowsUpdate / file
         rename pending / packages pending).
      3. The PrismInstallUpdates scheduled task is NOT currently running.
      4. The server has been continuously responsive for ≥60 s (caller-side
         debouncing — we just report uptime_seconds and let the overlay's
         stability window logic decide).

    Returns (always 200 unless server not configured, so the client doesn't
    have to handle two error shapes):
      {
        "ok": bool,
        "winrm_ok": bool,
        "pending_reboot": bool,
        "pending_reasons": list[str],   # which keys flagged
        "install_task_running": bool,
        "uptime_seconds": int | null,   # null if WinRM couldn't read it
        "ready": bool,                  # all four hold + uptime >= 60s
        "error": str | null,            # populated when winrm_ok is false
      }
    """
    auth_err = _require_server_permission(name, "read")
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman

        probe_script = r"""
$ErrorActionPreference = 'SilentlyContinue'

# 1) Pending-reboot signals — read each key independently so we can tell
#    the user WHICH gate is open (helps diagnose stuck reboots).
$reasons = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
    $reasons += 'cbs_reboot_pending'
}
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\PackagesPending') {
    $reasons += 'cbs_packages_pending'
}
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
    $reasons += 'windows_update_reboot_required'
}
$pfro = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name 'PendingFileRenameOperations' -ErrorAction SilentlyContinue).PendingFileRenameOperations
if ($pfro -and $pfro.Count -gt 0) {
    $reasons += 'pending_file_rename_operations'
}
# Domain join sometimes leaves a stage flag pending a reboot
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\PostRebootReporting') {
    $reasons += 'cbs_post_reboot_reporting'
}

# 2) PrismInstallUpdates task — Running means stage 2 (or another stage)
#    is still active, regardless of pending-reboot flags
$taskState = 'Unknown'
try {
    $taskInfo = Get-ScheduledTask -TaskName 'PrismInstallUpdates' -ErrorAction SilentlyContinue
    if ($taskInfo) { $taskState = [string]$taskInfo.State }
} catch {}

# 3) Uptime — the stability-window denominator
$boot = (Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue).LastBootUpTime
$uptime_s = $null
if ($boot) { $uptime_s = [int]((Get-Date) - $boot).TotalSeconds }

@{
    pending_reasons = $reasons
    pending_reboot = ($reasons.Count -gt 0)
    install_task_state = $taskState
    install_task_running = ($taskState -eq 'Running')
    uptime_seconds = $uptime_s
} | ConvertTo-Json -Compress -Depth 3
"""
        result = {
            "ok": True,
            "winrm_ok": False,
            "pending_reboot": True,  # pessimistic default when we can't read
            "pending_reasons": [],
            "install_task_running": False,
            "uptime_seconds": None,
            "ready": False,
            "error": None,
        }
        try:
            wsman = make_wsman(cfg, connection_timeout=10, read_timeout=15)
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(probe_script)
                out = ps.invoke()
                if ps.had_errors:
                    err_msg = "; ".join(str(e) for e in ps.streams.error)[:300]
                    result["error"] = err_msg or "PowerShell stream had errors"
                    return jsonify(result)
                raw = (str(out[0]) if out and out[0] is not None else "").strip()
                if not raw:
                    result["error"] = "Empty WinRM response"
                    return jsonify(result)
                payload = json.loads(raw)
                # Unwrap the {"value":[...]} PowerShell-array-of-hash wrapper
                # (same quirk as the event-log JSON pipeline).
                if isinstance(payload, dict) and set(payload.keys()) == {"value", "Count"}:
                    payload = payload["value"]
                result["winrm_ok"] = True
                result["pending_reboot"] = bool(payload.get("pending_reboot"))
                result["pending_reasons"] = list(payload.get("pending_reasons") or [])
                result["install_task_running"] = bool(payload.get("install_task_running"))
                uptime = payload.get("uptime_seconds")
                result["uptime_seconds"] = int(uptime) if uptime is not None else None
                result["ready"] = (
                    result["winrm_ok"]
                    and not result["pending_reboot"]
                    and not result["install_task_running"]
                    and result["uptime_seconds"] is not None
                    and result["uptime_seconds"] >= 60
                )
        except Exception as e:
            # Any failure here means the server isn't reachable yet — that
            # IS the answer the overlay is asking about. Return winrm_ok=false
            # so the overlay shows "still rebooting" instead of an error toast.
            result["error"] = str(e)[:200]
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /api/servers/%s/restart-readiness", name)
        return jsonify({"ok": False, "error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/logs")
def get_server_logs(name: str):
    """Get Windows event logs for a specific server with optional filters."""
    try:
        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)
        source = request.args.get("source", "").strip() or None
        level = request.args.get("level", "").strip() or None
        limit = request.args.get("limit", 100, type=int)
        limit = min(limit, 500)

        logs = _shared._db.get_server_logs(name, hours=hours, source=source, level=level, limit=limit)
        return jsonify({"name": name, "hours": hours, "logs": logs})
    except Exception:
        logger.exception("Error in GET /api/servers/%s/logs", name)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/analytics")
def get_server_analytics_endpoint(name: str):
    """Anomaly detection + disk capacity forecasts for a specific server."""
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"error": f"Server '{name}' not found"}), 404

        settings = _shared._config.get_settings()
        analytics = get_server_analytics(_shared._db, name, server_type=cfg.type,
                                          timezone_str=settings.get("timezone", "Europe/Berlin"),
                                          settings=settings, thresholds=cfg.thresholds)
        return jsonify({"name": name, **analytics})
    except Exception:
        logger.exception("Error in GET /api/servers/%s/analytics", name)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/data", methods=["DELETE"])
def delete_server_data(name: str):
    """Wipe ALL historical data for a server (metrics, events, logs, anomaly
    suppression, dependencies, etc.) from every related table. The audit_log
    is preserved (append-only, historical record of who did what).

    Also clears the server's entries from in-memory caches so the dashboard
    stops showing it as offline immediately. Called by the frontend BEFORE
    saving the config without that server, so the cleanup happens atomically
    from the user's perspective.

    Note: this does NOT remove the server from config.json — that's a
    separate operation owned by /api/config. This endpoint only wipes the
    DB rows + cache entries tied to a server name.
    """
    auth_err = _require_server_permission(name, "admin")
    if auth_err:
        return auth_err
    try:
        # Wipe DB
        deleted = _shared._db.delete_server_data(name)

        # Wipe in-memory caches so the dashboard reflects the deletion
        # immediately. Post-retirement: shared state lives in ``state``,
        # detection rings live in ``detection``.
        from state import (
            latest_by_server, server_update_info, server_hardware_info,
            server_auth_info, _accelerated_servers, _state_lock,
        )
        from detection import _baseline_dev_history, _cpu_warn_history
        with _state_lock:
            latest_by_server.pop(name, None)
        server_update_info.pop(name, None)
        server_hardware_info.pop(name, None)
        server_auth_info.pop(name, None)
        _accelerated_servers.pop(name, None)
        _baseline_dev_history.pop(name, None)
        _cpu_warn_history.pop(name, None)
        _shared._update_install_state.pop(name, None)

        # Audit (this WILL be preserved — append-only)
        _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
        _shared._db.log_audit(_audit_user, "delete_server_data", "server",
                      f"{name}: wiped {sum(deleted.values())} rows across {len(deleted)} tables")

        return jsonify({"ok": True, "server": name, "deleted": deleted,
                        "total_rows": sum(deleted.values())})
    except Exception as e:
        logger.exception("Error wiping data for server %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/hardware")
def get_server_hardware(name: str):
    """Get hardware specs for a server (cached from collector)."""
    try:
        info = server_hardware_info.get(name)
        if info is None:
            return jsonify({"name": name, "available": False})
        return jsonify({"name": name, "available": True, **info})
    except Exception:
        logger.exception("Error in GET /api/servers/%s/hardware", name)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/<name>/services")
def get_server_services(name: str):
    """Query running and stopped services on a remote server via WinRM."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman

        wsman = make_wsman(cfg, connection_timeout=30, read_timeout=30)

        script = r"Get-Service | Select-Object Name, DisplayName, Status | ConvertTo-Json"

        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(script)
            output = ps.invoke()

            if ps.had_errors:
                err_msgs = [str(e) for e in ps.streams.error]
                return jsonify({"ok": False, "error": "; ".join(err_msgs)[:200]}), 500

            stdout = str(output[0]) if output else "[]"
            if not stdout.strip():
                return jsonify({"ok": True, "services": []})

            try:
                data = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed JSON from services query on %s: %s", name, stdout[:200])
                return jsonify({"ok": False, "error": "Malformed response from server"}), 502
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return jsonify({"ok": True, "services": []})

            # Status is returned as an integer enum: 1=Stopped, 2=StartPending,
            # 3=StopPending, 4=Running, 5=ContinuePending, 6=PausePending, 7=Paused
            _status_map = {1: "Stopped", 2: "StartPending", 3: "StopPending",
                           4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused"}
            services = [
                {
                    "name": svc.get("Name", ""),
                    "display_name": svc.get("DisplayName", ""),
                    "status": _status_map.get(svc.get("Status"), str(svc.get("Status", ""))),
                }
                for svc in data
                if isinstance(svc, dict)
            ]

        return jsonify({"ok": True, "services": services})

    except Exception as e:
        logger.exception("Error querying services on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/processes")
def get_server_processes(name: str):
    """Query running processes on a remote server via WinRM."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman

        wsman = make_wsman(cfg, connection_timeout=30, read_timeout=30)

        script = r"Get-Process | Select-Object Name, Id, @{N='MemoryMB';E={[math]::Round($_.WorkingSet64/1MB,1)}} | Sort-Object Name -Unique | ConvertTo-Json"

        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(script)
            output = ps.invoke()

            if ps.had_errors:
                err_msgs = [str(e) for e in ps.streams.error]
                return jsonify({"ok": False, "error": "; ".join(err_msgs)[:200]}), 500

            stdout = str(output[0]) if output else "[]"
            if not stdout.strip():
                return jsonify({"ok": True, "processes": []})

            try:
                data = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed JSON from processes query on %s: %s", name, stdout[:200])
                return jsonify({"ok": False, "error": "Malformed response from server"}), 502
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return jsonify({"ok": True, "processes": []})

            processes = [
                {
                    "name": proc.get("Name", ""),
                    "id": proc.get("Id", 0),
                    "memory_mb": proc.get("MemoryMB", 0.0),
                }
                for proc in data
                if isinstance(proc, dict)
            ]

        return jsonify({"ok": True, "processes": processes})

    except Exception as e:
        logger.exception("Error querying processes on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/ports")
def get_server_listening_ports(name: str):
    """List TCP ports currently in LISTEN state on a remote server.

    Drives the "Browse ports" button on the Event Trigger block — lets
    operators pick a port that the server is actually listening on
    rather than typo'ing one. Returns ``{ports: [{port, process, pid}]}``.
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman

        wsman = make_wsman(cfg, connection_timeout=30, read_timeout=30)

        # Get-NetTCPConnection -State Listen is the modern API and works
        # on PS 5.1+. Resolve PID → process name via Get-Process so the
        # picker can show "5985 — wsmprovhost" instead of "5985 — 1234".
        # Distinct by port (LocalPort) so we don't show duplicates from
        # IPv4 + IPv6 + multiple local IP bindings.
        script = r"""
$conns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue
$procs = Get-Process | Select-Object Id, Name
$procMap = @{}
foreach ($p in $procs) { $procMap[[int]$p.Id] = $p.Name }
$rows = $conns | ForEach-Object {
    [pscustomobject]@{
        Port = [int]$_.LocalPort
        PID  = [int]$_.OwningProcess
        Process = if ($procMap.ContainsKey([int]$_.OwningProcess)) { $procMap[[int]$_.OwningProcess] } else { '' }
        Address = [string]$_.LocalAddress
    }
} | Sort-Object Port -Unique
,$rows | ConvertTo-Json -Compress
"""
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(script)
            output = ps.invoke()

            if ps.had_errors:
                err_msgs = [str(e) for e in ps.streams.error]
                return jsonify({"ok": False, "error": "; ".join(err_msgs)[:200]}), 500

            stdout = str(output[0]) if output else "[]"
            if not stdout.strip():
                return jsonify({"ok": True, "ports": []})

            try:
                data = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Malformed JSON from ports query on %s: %s", name, stdout[:200])
                return jsonify({"ok": False, "error": "Malformed response from server"}), 502
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                return jsonify({"ok": True, "ports": []})

            ports = [
                {
                    "name": str(p.get("Port", "")),     # match the modal's "name" field convention
                    "display_name": p.get("Process", ""),
                    "port": p.get("Port", 0),
                    "process": p.get("Process", ""),
                    "pid": p.get("PID", 0),
                    "address": p.get("Address", ""),
                }
                for p in data
                if isinstance(p, dict) and p.get("Port")
            ]

        return jsonify({"ok": True, "ports": ports})

    except Exception as e:
        logger.exception("Error querying ports on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/sla")
def get_server_sla(name):
    """Get SLA/uptime stats for a single server."""
    hours = min(int(request.args.get("hours", 720)), 8760)  # Max 1 year
    poll_interval = _shared._config.get_settings().get("poll_interval_seconds", 300)
    from analytics import compute_uptime_stats
    stats = compute_uptime_stats(_shared._db, name, hours=hours, poll_interval_seconds=poll_interval)
    return jsonify({"server": name, "hours": hours, **stats})


@api_bp.route("/servers/export-csv")
def export_servers_csv():
    auth = _require_auth()
    if auth: return auth
    import csv as _csv
    import io as _io
    servers = _shared._config.get_servers()
    output = _io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["name", "host", "type", "username", "port",
                     "cpu_warning", "cpu_critical", "ram_warning", "ram_critical",
                     "disk_warning", "disk_critical"])
    for s in servers:
        t = s.thresholds
        writer.writerow([s.name, s.host, s.type, s.username, s.port,
                        t.get("cpu_warning", 75), t.get("cpu_critical", 90),
                        t.get("ram_warning", 80), t.get("ram_critical", 90),
                        t.get("disk_warning", 75), t.get("disk_critical", 90)])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=prism_servers.csv"}
    )


@api_bp.route("/servers/import-csv", methods=["POST"])
def import_servers_csv():
    auth = _require_auth()
    if auth: return auth
    import csv as _csv
    import io as _io
    data = request.get_json(force=True)
    csv_text = data.get("csv", "")
    if not csv_text:
        return jsonify({"ok": False, "error": "No CSV data provided"}), 400

    reader = _csv.DictReader(_io.StringIO(csv_text))
    existing_servers = _shared._config.get_raw_servers()
    existing_names = {s.get("name", "").lower() for s in existing_servers}

    imported = 0
    skipped = 0
    errors = []

    for i, row in enumerate(reader, 1):
        name = row.get("name", "").strip()
        host = row.get("host", "").strip()
        if not name or not host:
            errors.append(f"Row {i}: missing name or host")
            continue
        if name.lower() in existing_names:
            skipped += 1
            continue
        server = {
            "name": name,
            "host": host,
            "type": row.get("type", "file_server").strip(),
            "username": row.get("username", "administrator").strip(),
            "password": "",
            "port": int(row.get("port", 5985)),
            "thresholds": {
                "cpu_warning": int(row.get("cpu_warning", 75)),
                "cpu_critical": int(row.get("cpu_critical", 90)),
                "ram_warning": int(row.get("ram_warning", 80)),
                "ram_critical": int(row.get("ram_critical", 90)),
                "disk_warning": int(row.get("disk_warning", 75)),
                "disk_critical": int(row.get("disk_critical", 90)),
            },
        }
        existing_servers.append(server)
        existing_names.add(name.lower())
        imported += 1

    if imported > 0:
        _shared._config.save_config(existing_servers)
        try:
            username = flask_session.get("username", "anonymous")
            _shared._db.log_audit(username, f"Imported {imported} servers via CSV", "servers")
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
    })


@api_bp.route("/servers/bulk-thresholds", methods=["POST"])
def bulk_thresholds():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json(force=True)
    server_names = data.get("server_names", [])
    thresholds = data.get("thresholds", {})
    if not server_names or not thresholds:
        return jsonify({"ok": False, "error": "server_names and thresholds required"}), 400

    servers = _shared._config.get_raw_servers()
    updated = 0
    name_set = set(server_names)
    for s in servers:
        if s.get("name") in name_set:
            if "thresholds" not in s:
                s["thresholds"] = {}
            s["thresholds"].update(thresholds)
            updated += 1

    if updated > 0:
        _shared._config.save_config(servers)
        try:
            username = flask_session.get("username", "anonymous")
            _shared._db.log_audit(username, f"Bulk threshold update for {updated} servers", "servers")
        except Exception:
            pass

    return jsonify({"ok": True, "updated": updated})


@api_bp.route("/servers/duplicate", methods=["POST"])
def duplicate_server():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json(force=True)
    index = data.get("index")
    if index is None:
        return jsonify({"ok": False, "error": "index required"}), 400

    servers = _shared._config.get_raw_servers()
    if index < 0 or index >= len(servers):
        return jsonify({"ok": False, "error": "Invalid server index"}), 400

    import copy
    original = copy.deepcopy(servers[index])
    original["name"] = original["name"] + "-copy"
    original["password"] = ""  # Don't copy password
    servers.append(original)
    _shared._config.save_config(servers)

    try:
        username = flask_session.get("username", "anonymous")
        _shared._db.log_audit(username, f"Duplicated server {servers[index].get('name', '')}", "servers")
    except Exception:
        pass

    return jsonify({"ok": True, "new_index": len(servers) - 1})


@api_bp.route("/servers/<name>/tags", methods=["GET"])
def get_server_tags(name):
    """Return tags assigned to a server."""
    return jsonify({"ok": True, "tags": _shared._db.get_tags_for_server(name)})


@api_bp.route("/servers/<name>/tags", methods=["POST"])
def assign_server_tag(name):
    """Assign a tag to a server."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    tag_id = data.get("tag_id")
    if tag_id is None:
        return jsonify({"ok": False, "error": "tag_id is required"}), 400
    try:
        _shared._db.assign_tag(name, tag_id)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "assign_tag", "tags", f"Assigned tag {tag_id} to server '{name}'")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to assign tag")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/servers/<name>/tags/<int:tag_id>", methods=["DELETE"])
def remove_server_tag(name, tag_id):
    """Remove a tag from a server."""
    auth = _require_auth()
    if auth: return auth
    try:
        _shared._db.remove_tag_assignment(name, tag_id)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "remove_tag", "tags", f"Removed tag {tag_id} from server '{name}'")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to remove tag assignment")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/servers/<name>/failed-logins")
def get_server_failed_logins(name):
    hours = request.args.get("hours", 24, type=int)
    limit = request.args.get("limit", 100, type=int)
    return jsonify({"ok": True, "logins": _shared._db.get_failed_logins_recent(name, hours, limit)})


@api_bp.route("/servers/<name>/failed-logins/heatmap")
def get_failed_login_heatmap(name):
    """Failed-login heatmap data, bucketed by (day-of-week, hour) in the
    configured timezone.

    Query params:
      hours:       total lookback window in hours (default 672 = 4 weeks).
      week_offset: optional integer (0..3). If provided, narrows the result
                   to a single 168-hour week ending `week_offset * 168` hours
                   ago. 0 = this week, 1 = last week, 2 = two weeks ago, etc.
                   When omitted, the full `hours` window is used.
    """
    hours = request.args.get("hours", 672, type=int)
    week_offset = request.args.get("week_offset", default=None, type=int)
    settings = _shared._config.get_settings()
    tz_name = settings.get("timezone", "Europe/Berlin")

    # Get raw login timestamps and compute heatmap in the configured timezone
    raw = _shared._db.get_failed_logins_recent(name, hours=hours, limit=20000)
    if not raw:
        return jsonify({"ok": True, "data": [], "max_count": 0})

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone
        tz = timezone.utc

    from datetime import datetime, timedelta, timezone as _tzmod
    # Compute the [start, end] UTC window when week_offset is set.
    # week_offset=0 -> last 168h (this week)
    # week_offset=1 -> 168..336h ago (last week)
    # ...
    win_start = None
    win_end = None
    if week_offset is not None and week_offset >= 0:
        now_utc = datetime.now(_tzmod.utc)
        win_end = now_utc - timedelta(hours=168 * week_offset)
        win_start = win_end - timedelta(hours=168)

    grid = {}  # (dow, hour) -> count
    for r in raw:
        try:
            ts = r.get("timestamp", "")
            dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # Apply week-offset filter (UTC comparison)
            if win_start is not None and not (win_start <= dt_utc <= win_end):
                continue
            dt = dt_utc.astimezone(tz)
            key = (dt.weekday(), dt.hour)  # weekday: 0=Mon, hour: 0-23
            grid[key] = grid.get(key, 0) + 1
        except (ValueError, AttributeError):
            continue

    # Convert to list format matching JS expectations (dow uses isoweekday-style for Mon-Sun mapping)
    # JS dowMap expects SQLite %w format: 0=Sun,1=Mon...6=Sat -> remap Python weekday (0=Mon) to that
    py_to_sqlite_dow = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
    data = [{"dow": py_to_sqlite_dow[k[0]], "hour": k[1], "count": v} for k, v in grid.items()]
    max_count = max((d["count"] for d in data), default=0)
    return jsonify({"ok": True, "data": data, "max_count": max_count})


@api_bp.route("/servers/<name>/security-status", methods=["GET"])
def get_security_status(name):
    status = _shared._db.get_security_status(name)
    if not status:
        return jsonify({"ok": True, "status": None})
    return jsonify({"ok": True, "status": status})


@api_bp.route("/servers/<name>/security-status/check", methods=["POST"])
def trigger_security_check(name):
    """Manual trigger for security check."""
    from security_checker import collect_security_status
    cfg = _shared._config.get_server_by_name(name)
    if not cfg:
        return jsonify({"ok": False, "error": "Server not found"}), 404
    settings = _shared._config.get_settings()
    try:
        collect_security_status(_shared._db, cfg, settings)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "status": _shared._db.get_security_status(name)})
