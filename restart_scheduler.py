"""Restart scheduler engine for Prism. Runs as a daemon thread to execute
scheduled server restarts with optional Windows Update installation."""

import json
import time
import logging
import threading
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("prism.restart_scheduler")

# Shared state
last_run_results: list[dict] = []  # Most recent restart schedule execution results
_lock = threading.Lock()

DATA_DIR = Path(__file__).parent / "data"
LAST_RUN_MARKER = DATA_DIR / "last_restart_run.txt"

# Polling constants
LOOP_INTERVAL = 30          # seconds between schedule checks

# S2-11 (P10) heartbeat. Updated at the end of every loop iteration.
# app.py's watchdog reads this and treats >5×LOOP_INTERVAL as "dead".
_last_heartbeat: float = 0.0
CONDITION_POLL_INTERVAL = 10  # seconds between condition re-checks


def _safe_int(val, default: int) -> int:
    """Convert val to int, returning *default* on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_last_run_results() -> list[dict]:
    """Returns a copy of the most recent restart execution results."""
    with _lock:
        return list(last_run_results)


def restart_scheduler_loop(get_settings, db, get_servers):
    """Main loop function. Intended to be run in a daemon thread.

    Every LOOP_INTERVAL seconds, checks whether any configured restart schedule
    should fire right now.  If so, executes the restart sequence.

    Args:
        get_settings: Callable returning the current settings dict.
        db: Database instance.
        get_servers: Callable returning the current list of ServerConfig objects.
    """
    logger.info("Restart scheduler started")
    # S2-11 (P10) heartbeat: app.py's watchdog reads this to detect a dead
    # scheduler thread. Updated at the end of every loop tick.
    global _last_heartbeat

    # S2-6 (P5): execute_server_restarts can take many minutes on a 30-server
    # fleet. Running it synchronously inside the scheduler loop blocks the
    # 30s tick — meaning the loop can miss its own 2-minute trigger window
    # if a fleet pass overruns. We now spawn each fleet run on its own
    # daemon thread; the loop continues to tick every 30s.
    import threading as _threading

    def _do_fleet_restart(fleet_schedule, tz_name, settings_snapshot):
        """Body of the spawned fleet-restart thread.

        Writes the run marker FIRST so concurrent scheduler ticks during the
        long pass don't try to spawn a second fleet thread (S2-5 / P4).
        On crash, the marker is still written — this is intentional, because
        we don't want to retry an already-partially-started fleet restart in
        the same 2-minute window. Operators recover via the manual restart
        UI; restart_log shows per-server status.
        """
        try:
            _mark_run_started(fleet_schedule, tz_name)
        except Exception:
            logger.exception("Failed to write run marker — proceeding anyway")
        try:
            servers = get_servers()
            server_overrides = settings_snapshot.get("scheduled_server_restarts", [])
            for so in server_overrides:
                if "server" in so and "name" not in so:
                    so["name"] = so["server"]
            execute_server_restarts(settings_snapshot, db, servers, server_overrides)
        except Exception:
            logger.exception("Fleet restart thread failed")

    while True:
        try:
            settings = get_settings()
            # Read the global server restart schedule
            srs = settings.get("scheduled_server_restart_schedule", {})
            if srs.get("enabled", False):
                tz_name = settings.get("timezone", "Europe/Berlin")

                # Map UI fields to _should_run_now format
                day_map = {"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6}
                fleet_schedule = {
                    "type": srs.get("schedule", "weekly"),
                    "time": srs.get("time", "03:00"),
                    "day_of_week": day_map.get(str(srs.get("day", "6")), 6),
                    "day_of_month": _safe_int(srs.get("month_day", 1), 1),
                }

                if _should_run_now(fleet_schedule, tz_name):
                    logger.info("Server restart schedule triggered (spawning thread)")
                    _threading.Thread(
                        target=_do_fleet_restart,
                        args=(fleet_schedule, tz_name, settings),
                        daemon=True,
                        name="prism-fleet-restart",
                    ).start()

        except Exception:
            logger.exception("Unhandled error in restart scheduler loop")

        _last_heartbeat = time.time()
        time.sleep(LOOP_INTERVAL)


# ---------------------------------------------------------------------------
# Schedule check
# ---------------------------------------------------------------------------

def _marker_key(schedule_config: dict, marker_suffix: str = "") -> str:
    """Compute the marker key for a given schedule. Public so the loop can
    pass the same key to _mark_run_started after `_should_run_now` agrees."""
    sched_type = schedule_config.get("type", "weekly")
    return f"{sched_type}_{marker_suffix}" if marker_suffix else sched_type


def _should_run_now(schedule_config: dict, tz_name: str, marker_suffix: str = "") -> bool:
    """Determine whether *schedule_config* should fire right now.

    schedule_config keys:
        type: "daily" | "weekly" | "monthly"
        time: "HH:MM"
        day_of_week: 0-6 (Mon-Sun) -- for weekly
        day_of_month: 1-31          -- for monthly

    Read-only: this function does NOT write the marker. The caller is
    responsible for calling _mark_run_started() at the moment work actually
    begins (S2-5 / P4 from AUDIT-2026-05). Old behaviour was to write the
    marker here, before any work — which meant a crash between this check
    and the actual restart left the marker burned and the schedule didn't
    retry until the next period.
    """
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid timezone '%s', falling back to UTC", tz_name)
        tz = timezone.utc

    now = datetime.now(tz)
    sched_time_str = schedule_config.get("time", "03:00")
    try:
        sched_hour, sched_minute = map(int, sched_time_str.split(":"))
    except (ValueError, AttributeError):
        logger.warning("Invalid schedule time '%s'", sched_time_str)
        return False

    # Only trigger within a 2-minute window of the scheduled time
    if now.hour != sched_hour or abs(now.minute - sched_minute) > 1:
        return False

    sched_type = schedule_config.get("type", "weekly")

    if sched_type == "weekly":
        target_day = schedule_config.get("day_of_week", 6)  # default Sunday
        if now.weekday() != target_day:
            return False
    elif sched_type == "monthly":
        target_dom = schedule_config.get("day_of_month", 1)
        if now.day != target_dom:
            return False
    # "daily" fires every day -- no extra check needed

    # Marker check only — do NOT write here.
    marker_key = _marker_key(schedule_config, marker_suffix)
    if _already_ran_marker(marker_key, now, sched_type):
        return False

    return True


def _mark_run_started(schedule_config: dict, tz_name: str, marker_suffix: str = ""):
    """Atomic-ish 'this scheduled run has begun' write. Called from the
    spawned execution thread BEFORE the actual work. Prevents the next
    scheduler tick (30s later) from re-triggering during a long fleet pass.
    """
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    marker_key = _marker_key(schedule_config, marker_suffix)
    _write_run_marker(marker_key, now)


def _already_ran_marker(marker_key: str, now: datetime, sched_type: str) -> bool:
    """Return True if the marker file shows we already ran in this period."""
    marker_file = DATA_DIR / f"last_restart_{marker_key}.txt"
    if not marker_file.exists():
        return False

    try:
        content = marker_file.read_text().strip()
        last_run = datetime.fromisoformat(content)
    except (ValueError, OSError):
        return False

    if sched_type == "daily":
        return last_run.date() == now.date()
    elif sched_type == "weekly":
        # Same ISO week
        return last_run.isocalendar()[:2] == now.isocalendar()[:2]
    elif sched_type == "monthly":
        return (last_run.year, last_run.month) == (now.year, now.month)

    return False


def _write_run_marker(marker_key: str, now: datetime):
    """Persist the current timestamp so we know we already ran."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker_file = DATA_DIR / f"last_restart_{marker_key}.txt"
        marker_file.write_text(now.isoformat())
    except OSError:
        logger.exception("Failed to write restart run marker")


# ---------------------------------------------------------------------------
# Restart execution
# ---------------------------------------------------------------------------

def execute_server_restarts(settings: dict, db, servers, server_overrides: list[dict]):
    """Execute the restart sequence for the given server override configs.

    Args:
        settings: Full settings dict.
        db: Database instance.
        servers: List of ServerConfig objects (from config_manager.get_servers).
        server_overrides: List of per-server restart config dicts.  Each must
            have at least a ``name`` key matching a ServerConfig.
    """
    global last_run_results

    # Global delay/timeout defaults from settings
    default_delay = settings.get("restart_delay_between_seconds", 60)

    # Build a name -> ServerConfig lookup
    server_map = {s.name: s for s in servers}

    # Sort overrides by their ``order`` field (lower = first)
    sorted_overrides = sorted(server_overrides, key=lambda s: s.get("order", 999))

    results: list[dict] = []

    for srv_override in sorted_overrides:
        server_name = srv_override.get("name", "unknown")
        result = {
            "server": server_name,
            "status": "pending",
            "install_updates": srv_override.get("install_updates", False),
            "updates_installed": 0,
            "conditions_met": False,
            "condition_details": [],
            "error": "",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        server_cfg = server_map.get(server_name)
        if server_cfg is None:
            result["status"] = "skipped"
            result["error"] = f"Server '{server_name}' not found in config"
            logger.warning("Restart skipped: server '%s' not in config", server_name)
            db.log_audit("system", f"Restart skipped for {server_name}: not found in config",
                         category="restart_schedule")
            db.insert_event(server_name, "info", "restart", None, None,
                            f"Scheduled restart skipped: server not found in config")
            results.append(result)
            continue

        # Build a connection-friendly dict from ServerConfig
        srv_conn = {
            "host": server_cfg.host,
            "port": server_cfg.port,
            "username": server_cfg.username,
            "password": server_cfg.password,  # already decrypted by ConfigManager
            "name": server_cfg.name,
        }

        logger.info("[%s] Starting restart sequence", server_name)
        db.log_audit("system", f"Restart sequence started for {server_name}",
                     category="restart_schedule")

        # --- Step 1: Optional Windows Update install ---
        if srv_override.get("install_updates", False):
            logger.info("[%s] Installing Windows updates before restart", server_name)
            db.log_audit("system", f"Installing Windows updates on {server_name}",
                         category="restart_schedule")
            try:
                wsman = _connect_winrm(srv_conn)
                success, output = _install_windows_updates(wsman)
                if success:
                    try:
                        update_data = json.loads(output) if output.strip() else []
                        if isinstance(update_data, list):
                            result["updates_installed"] = len(update_data)
                        elif isinstance(update_data, dict):
                            result["updates_installed"] = update_data.get("UpdateCount", 0)
                    except (json.JSONDecodeError, TypeError):
                        result["updates_installed"] = 0
                    logger.info("[%s] Windows updates completed: %s updates",
                                server_name, result["updates_installed"])
                else:
                    logger.warning("[%s] Windows update failed: %s", server_name, output[:300])
                    db.log_audit("system",
                                 f"Windows update failed on {server_name}: {output[:200]}",
                                 category="restart_schedule")
            except Exception as e:
                logger.exception("[%s] Windows update error", server_name)
                db.log_audit("system",
                             f"Windows update error on {server_name}: {e}",
                             category="restart_schedule")

        # --- Step 2: Send restart command ---
        try:
            wsman = _connect_winrm(srv_conn)
            success, output = _restart_server(wsman)
            if not success:
                result["status"] = "failed"
                result["error"] = f"Restart command failed: {output[:300]}"
                logger.error("[%s] Restart command failed: %s", server_name, output[:300])
                db.log_audit("system",
                             f"Restart command failed on {server_name}: {output[:200]}",
                             category="restart_schedule")
                db.insert_event(server_name, "critical", "restart", None, None,
                                f"Scheduled restart failed: {output[:200]}")
                results.append(result)
                continue

            logger.info("[%s] Restart command sent successfully", server_name)
            db.log_audit("system", f"Restart command sent to {server_name}",
                         category="restart_schedule")
        except Exception as e:
            result["status"] = "failed"
            result["error"] = f"WinRM error: {e}"
            logger.exception("[%s] Failed to send restart command", server_name)
            db.log_audit("system",
                         f"Restart WinRM error on {server_name}: {e}",
                         category="restart_schedule")
            db.insert_event(server_name, "critical", "restart", None, None,
                            f"Scheduled restart failed: WinRM error: {e}")
            results.append(result)
            continue

        # --- Step 3: Wait for configured delay ---
        delay_seconds = srv_override.get("delay_seconds", default_delay)
        logger.info("[%s] Waiting %d seconds before condition checks", server_name, delay_seconds)
        time.sleep(delay_seconds)

        # --- Step 4: Check post-restart conditions ---
        conditions = srv_override.get("conditions", [])
        timeout_seconds = srv_override.get("timeout_seconds", 300)
        all_conditions_met = True

        for condition in conditions:
            cond_result = _check_condition(srv_conn, condition, timeout_seconds)
            result["condition_details"].append(cond_result)
            if not cond_result.get("met", False):
                all_conditions_met = False

        result["conditions_met"] = all_conditions_met or len(conditions) == 0

        if result["conditions_met"]:
            result["status"] = "success"
            logger.info("[%s] Restart completed successfully", server_name)
        else:
            result["status"] = "timeout"
            failed_conds = [c["type"] for c in result["condition_details"] if not c.get("met")]
            result["error"] = f"Conditions not met: {', '.join(failed_conds)}"
            logger.warning("[%s] Restart conditions not met: %s", server_name, result["error"])

        result["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.log_audit("system",
                     f"Restart {result['status']} for {server_name} "
                     f"(updates={result['updates_installed']}, conditions_met={result['conditions_met']})",
                     category="restart_schedule",
                     details=json.dumps(result, default=str))

        # Insert event so it appears on the server detail page
        if result["status"] == "success":
            msg = "Scheduled restart completed successfully"
            if result["updates_installed"]:
                msg += f" ({result['updates_installed']} updates installed)"
            db.insert_event(server_name, "info", "restart", None, None, msg)
        elif result["status"] == "timeout":
            failed_conds = [c["type"] for c in result["condition_details"] if not c.get("met")]
            details = ", ".join(failed_conds) if failed_conds else "unknown"
            db.insert_event(server_name, "warning", "restart", None, None,
                            f"Scheduled restart completed but conditions not met: {details}")

        results.append(result)

    # Store results globally
    with _lock:
        last_run_results.clear()
        last_run_results.extend(results)

    # --- Step 5: Send notifications ---
    _send_restart_notifications(settings, db, results)

    logger.info("Restart schedule completed: %d servers processed", len(results))
    return results


# ---------------------------------------------------------------------------
# WinRM helpers
# ---------------------------------------------------------------------------

def _connect_winrm(server: dict):
    """Create a WSMan connection object for the given server dict.

    The server dict must contain: host, port, username, password.
    Password should already be in plaintext (decrypted by ConfigManager).
    Honours `use_https` / `https_skip_verify` flags when present so the
    scheduler matches the rest of the app's transport policy.
    """
    try:
        from pypsrp.wsman import WSMan
    except ImportError:
        raise RuntimeError("pypsrp not installed. Run: pip install pypsrp")

    host = server["host"]
    use_https = bool(server.get("use_https", False))
    skip_verify = bool(server.get("https_skip_verify", False))
    default_port = 5986 if use_https else 5985
    port = server.get("port", default_port)
    username = server["username"]
    password = server["password"]

    kwargs = dict(
        port=port,
        username=username,
        password=password,
        ssl=use_https,
        auth="negotiate",
        connection_timeout=25,
        read_timeout=30,
    )
    if use_https:
        kwargs["cert_validation"] = not skip_verify
    return WSMan(host, **kwargs)


def _run_powershell(wsman, script: str) -> tuple[bool, str]:
    """Run a PowerShell script via WinRM and return (success, output_text)."""
    try:
        from pypsrp.powershell import PowerShell, RunspacePool
    except ImportError:
        return False, "pypsrp not installed"

    try:
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(script)
            output = ps.invoke()

            stdout = str(output[0]) if output else ""
            if ps.had_errors:
                err_msgs = [str(e) for e in ps.streams.error]
                error_text = "; ".join(err_msgs)[:500]
                # Return output even on partial error
                if stdout:
                    return True, stdout
                return False, error_text

            return True, stdout
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _install_windows_updates(wsman) -> tuple[bool, str]:
    """Install pending Windows updates via WinRM.

    Tries PSWindowsUpdate module first, falls back to COM-based approach.
    """
    # Primary: PSWindowsUpdate module
    ps_primary = r"""
try {
    Import-Module PSWindowsUpdate -ErrorAction Stop
    $updates = Get-WindowsUpdate -Install -AcceptAll -AutoReboot:$false -IgnoreReboot 2>$null
    if ($updates) { $updates | Select-Object Title, Result | ConvertTo-Json -Compress } else { '[]' }
} catch {
    'FALLBACK_NEEDED'
}
"""
    success, output = _run_powershell(wsman, ps_primary)
    if success and output.strip() != "FALLBACK_NEEDED":
        return True, output

    # Fallback: COM-based Windows Update
    logger.info("PSWindowsUpdate not available, using COM fallback")
    ps_fallback = r"""
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$results = $searcher.Search("IsInstalled=0 and Type='Software'")
if ($results.Updates.Count -eq 0) {
    @{UpdateCount=0; ResultCode=0} | ConvertTo-Json -Compress
} else {
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $results.Updates
    $downloader.Download() | Out-Null
    $installer = $session.CreateUpdateInstaller()
    $installer.Updates = $results.Updates
    $installResult = $installer.Install()
    @{UpdateCount=$results.Updates.Count; ResultCode=$installResult.ResultCode} | ConvertTo-Json -Compress
}
"""
    return _run_powershell(wsman, ps_fallback)


def _restart_server(wsman) -> tuple[bool, str]:
    """Send Restart-Computer -Force via WinRM."""
    return _run_powershell(wsman, "Restart-Computer -Force")


# ---------------------------------------------------------------------------
# Post-restart condition checks
# ---------------------------------------------------------------------------

def _check_condition(server_conn: dict, condition: dict, timeout_seconds: int = 300) -> dict:
    """Check a single post-restart condition by polling until met or timeout.

    condition dict keys:
        type: "wait_online" | "wait_service" | "wait_process"
        value: service name or process name (not needed for wait_online)

    Returns dict: {type, value, met: bool, elapsed: float, error: str}
    """
    cond_type = condition.get("type", "wait_online")
    cond_value = condition.get("value", "")
    start = time.time()

    result = {
        "type": cond_type,
        "value": cond_value,
        "met": False,
        "elapsed": 0.0,
        "error": "",
    }

    logger.info("[%s] Checking condition: %s %s (timeout=%ds)",
                server_conn.get("name", "?"), cond_type, cond_value, timeout_seconds)

    while (time.time() - start) < timeout_seconds:
        try:
            if cond_type == "wait_online":
                met, detail = _check_online(server_conn)
            elif cond_type == "wait_service":
                met, detail = _check_service(server_conn, cond_value)
            elif cond_type == "wait_process":
                met, detail = _check_process(server_conn, cond_value)
            else:
                result["error"] = f"Unknown condition type: {cond_type}"
                break

            if met:
                result["met"] = True
                result["elapsed"] = round(time.time() - start, 1)
                logger.info("[%s] Condition %s met after %.1fs",
                            server_conn.get("name", "?"), cond_type, result["elapsed"])
                return result

        except Exception as e:
            # Expected during reboot -- server is unreachable
            logger.debug("[%s] Condition check attempt failed (expected during reboot): %s",
                         server_conn.get("name", "?"), e)

        time.sleep(CONDITION_POLL_INTERVAL)

    result["elapsed"] = round(time.time() - start, 1)
    result["error"] = f"Timed out after {timeout_seconds}s"
    logger.warning("[%s] Condition %s timed out after %ds",
                   server_conn.get("name", "?"), cond_type, timeout_seconds)
    return result


def _check_online(server_conn: dict) -> tuple[bool, str]:
    """Try a WinRM connection. Returns (True, '') if successful."""
    wsman = _connect_winrm(server_conn)
    success, output = _run_powershell(wsman, "'online'")
    if success and "online" in output.lower():
        return True, ""
    return False, output


def _check_service(server_conn: dict, service_name: str) -> tuple[bool, str]:
    """Check that Windows service(s) are running.

    service_name may be comma-separated (e.g. "wuauserv, Spooler") when the
    picker multi-selects.  ALL listed services must be Running for the check
    to pass.
    """
    names = [n.strip() for n in service_name.split(",") if n.strip()]
    if not names:
        return False, "No service name specified"

    wsman = _connect_winrm(server_conn)
    # Query all requested services in a single call (escape single quotes)
    name_array = ", ".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in names)
    script = (
        f"Get-Service -Name {name_array} -ErrorAction SilentlyContinue "
        f"| Select-Object Name, Status | ConvertTo-Json -Compress"
    )
    success, output = _run_powershell(wsman, script)
    if not success:
        return False, output.strip()

    try:
        data = json.loads(output.strip()) if output.strip() else []
        if isinstance(data, dict):
            data = [data]
    except (json.JSONDecodeError, TypeError):
        return False, f"Bad JSON from service query: {output[:200]}"

    # Status enum: 4 = Running
    running = {
        (svc.get("Name") or "").lower()
        for svc in data
        if svc.get("Status") == 4
        or str(svc.get("Status", "")).lower() == "running"
    }
    missing = [n for n in names if n.lower() not in running]
    if missing:
        return False, f"Not running: {', '.join(missing)}"
    return True, "All running"


def _check_process(server_conn: dict, process_name: str) -> tuple[bool, str]:
    """Check that process(es) are running.

    process_name may be comma-separated (e.g. "svchost, explorer") when the
    picker multi-selects.  ALL listed processes must be present for the check
    to pass.
    """
    names = [n.strip() for n in process_name.split(",") if n.strip()]
    if not names:
        return False, "No process name specified"

    wsman = _connect_winrm(server_conn)
    # Query all requested processes in a single call (escape single quotes)
    name_array = ", ".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in names)
    script = (
        f"Get-Process -Name {name_array} -ErrorAction SilentlyContinue "
        f"| Select-Object Name -Unique | ConvertTo-Json -Compress"
    )
    success, output = _run_powershell(wsman, script)
    if not success:
        return False, output.strip()

    try:
        data = json.loads(output.strip()) if output.strip() else []
        if isinstance(data, dict):
            data = [data]
    except (json.JSONDecodeError, TypeError):
        return False, f"Bad JSON from process query: {output[:200]}"

    found = {(p.get("Name") or "").lower() for p in data}
    missing = [n for n in names if n.lower() not in found]
    if missing:
        return False, f"Not found: {', '.join(missing)}"
    return True, "All found"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _format_local_time(settings: dict) -> str:
    """Format current time in the configured timezone for display."""
    tz_name = settings.get("timezone", "Europe/Berlin") if settings else "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(timezone.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M')


def _send_restart_notifications(settings: dict, db, results: list[dict]):
    """Send email and/or Teams webhook with a summary of restart results."""
    if not results:
        return

    # Build summary text
    total = len(results)
    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] in ("failed", "timeout"))
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    total_updates = sum(r.get("updates_installed", 0) for r in results)

    summary_lines = [
        f"Restart schedule completed: {success_count}/{total} successful",
        f"  Failed/Timeout: {failed_count}",
        f"  Skipped: {skipped_count}",
        f"  Total updates installed: {total_updates}",
        "",
    ]
    for r in results:
        status_icon = {"success": "[OK]", "failed": "[FAIL]", "timeout": "[TIMEOUT]",
                       "skipped": "[SKIP]"}.get(r["status"], "[?]")
        line = f"  {status_icon} {r['server']}"
        if r.get("updates_installed"):
            line += f" ({r['updates_installed']} updates)"
        if r.get("error"):
            line += f" - {r['error']}"
        summary_lines.append(line)

    summary_text = "\n".join(summary_lines)
    logger.info("Restart summary:\n%s", summary_text)

    # Teams webhook
    try:
        webhook_cfg = settings.get("webhooks", {})
        if webhook_cfg.get("enabled") and webhook_cfg.get("teams_webhook_url"):
            from webhooks import send_teams_webhook
            overall_status = "success" if failed_count == 0 else "critical"
            send_teams_webhook(
                webhook_cfg["teams_webhook_url"],
                "Scheduled Restart",
                f"restart_{overall_status}",
                None,   # metric
                None,   # value
                None,   # threshold
                summary_text,
                settings,
            )
            logger.info("Restart summary sent to Teams webhook")
    except Exception:
        logger.exception("Failed to send restart Teams webhook")

    # Email notification
    try:
        email_cfg = settings.get("email", {})
        if email_cfg.get("enabled") and email_cfg.get("recipients") and email_cfg.get("smtp_server"):
            _send_restart_email(email_cfg, settings, results, summary_text)
            logger.info("Restart summary email sent")
    except Exception:
        logger.exception("Failed to send restart summary email")


def _send_restart_email(email_cfg: dict, settings: dict, results: list[dict], summary_text: str):
    """Send an HTML email with the restart summary."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    total = len(results)
    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] in ("failed", "timeout"))

    subject = f"Prism Restart Report: {success_count}/{total} successful"
    if failed_count > 0:
        subject = f"[ALERT] Prism Restart Report: {failed_count} failed"

    # Build HTML body
    rows_html = ""
    for r in results:
        color = {"success": "#28a745", "failed": "#dc3545", "timeout": "#ffc107",
                 "skipped": "#6c757d"}.get(r["status"], "#6c757d")
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 12px'>{r['server']}</td>"
            f"<td style='padding:6px 12px;color:{color};font-weight:bold'>{r['status'].upper()}</td>"
            f"<td style='padding:6px 12px'>{r.get('updates_installed', 0)}</td>"
            f"<td style='padding:6px 12px'>{'Yes' if r.get('conditions_met') else 'No'}</td>"
            f"<td style='padding:6px 12px'>{r.get('error', '')}</td>"
            f"</tr>"
        )

    html = f"""
    <html><body style="font-family:Segoe UI,sans-serif;color:#333">
    <h2>Prism Scheduled Restart Report</h2>
    <p>Completed at {_format_local_time(settings)}</p>
    <table style="border-collapse:collapse;width:100%" border="1" cellpadding="0">
    <tr style="background:#f0f0f0">
        <th style="padding:8px 12px;text-align:left">Server</th>
        <th style="padding:8px 12px;text-align:left">Status</th>
        <th style="padding:8px 12px;text-align:left">Updates</th>
        <th style="padding:8px 12px;text-align:left">Conditions Met</th>
        <th style="padding:8px 12px;text-align:left">Error</th>
    </tr>
    {rows_html}
    </table>
    <p style="color:#888;font-size:12px;margin-top:20px">Sent by Prism Monitoring</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_address", "prism@localhost")
    msg["To"] = ", ".join(email_cfg.get("recipients", []))
    msg.attach(MIMEText(summary_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    smtp_server = email_cfg["smtp_server"]
    smtp_port = email_cfg.get("smtp_port", 587)
    use_tls = email_cfg.get("use_tls", True)

    with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
        if use_tls:
            server.starttls()
        username = email_cfg.get("username")
        password = email_cfg.get("password")
        if username and password:
            server.login(username, password)
        server.sendmail(
            email_cfg.get("from_address", "prism@localhost"),
            email_cfg.get("recipients", []),
            msg.as_string(),
        )
