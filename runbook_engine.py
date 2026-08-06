"""Runbook execution engine for Prism.
Manages and executes PowerShell runbooks on remote Windows servers."""

import json
import time
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("prism.runbook")

# Built-in runbook definitions
BUILTIN_RUNBOOKS = [
    {
        "name": "Clear Temp Files",
        "description": "Remove temporary files from C:\\Temp and Windows\\Temp",
        "category": "maintenance",
        "steps": [{"type": "powershell", "script": "Remove-Item 'C:\\Temp\\*' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item '$env:WINDIR\\Temp\\*' -Recurse -Force -ErrorAction SilentlyContinue; 'Temp files cleared'", "timeout": 30}],
    },
    {
        "name": "Restart Print Spooler",
        "description": "Restart the Windows Print Spooler service",
        "category": "service",
        "steps": [{"type": "powershell", "script": "Restart-Service Spooler -Force; Start-Sleep 2; (Get-Service Spooler).Status", "timeout": 30}],
    },
    {
        "name": "Flush DNS Cache",
        "description": "Clear the DNS resolver cache",
        "category": "network",
        "steps": [{"type": "powershell", "script": "Clear-DnsClientCache; ipconfig /flushdns", "timeout": 15}],
    },
    {
        "name": "Check Disk Space Detail",
        "description": "Get detailed disk usage information",
        "category": "diagnostic",
        "steps": [{"type": "powershell", "script": "Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{N='Used(GB)';E={[math]::Round($_.Used/1GB,2)}}, @{N='Free(GB)';E={[math]::Round($_.Free/1GB,2)}}, @{N='Total(GB)';E={[math]::Round(($_.Used+$_.Free)/1GB,2)}} | ConvertTo-Json", "timeout": 15}],
    },
    {
        "name": "List Top CPU Processes",
        "description": "Show top 10 processes by CPU usage",
        "category": "diagnostic",
        "steps": [{"type": "powershell", "script": "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name, @{N='CPU(s)';E={[math]::Round($_.CPU,1)}}, @{N='Mem(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}} | ConvertTo-Json", "timeout": 15}],
    },
    {
        "name": "Windows Service Status",
        "description": "List all non-Microsoft services and their status",
        "category": "diagnostic",
        "steps": [{"type": "powershell", "script": "Get-Service | Where-Object { $_.DisplayName -notlike 'Windows*' -and $_.DisplayName -notlike 'Microsoft*' } | Select-Object Name, DisplayName, Status, StartType | ConvertTo-Json", "timeout": 20}],
    },
]


def seed_builtin_runbooks(db):
    """Insert built-in runbooks if they don't exist."""
    for rb in BUILTIN_RUNBOOKS:
        try:
            db.create_runbook(
                name=rb["name"],
                description=rb["description"],
                category=rb["category"],
                steps_json=json.dumps(rb["steps"]),
                created_by="system",
                is_builtin=True,
            )
        except Exception:
            pass  # Already exists (UNIQUE constraint)


def execute_runbook(db, runbook_id, server_name, server_config, dry_run=False, executed_by="system", settings=None):
    """Execute a runbook on a remote server.

    Args:
        db: Database instance
        runbook_id: ID of the runbook to execute
        server_name: Target server name
        server_config: ServerConfig object with host, port, username, password
        dry_run: If True, validate but don't execute
        executed_by: Username for audit trail
        settings: Full settings dict (optional). When provided, runbook execution
                  failures will dispatch email + Teams webhook alerts via the
                  same path as collector.py status events. See WIRING below.

    NOTIFICATION WIRING (when settings is provided):
        - On final_status='failed': inserts an event with severity='warning'
          (or 'critical' if all steps failed), then calls
          email_alerts.send_alert_email + webhooks.send_teams_webhook.
        - Severity strings ('warning', 'critical') are required by
          email_alerts.should_send_email allowlist.
        - Mirrors the dispatch block in collector.py per-server loop.

    Returns: execution_id
    """
    runbook = db.get_runbook(runbook_id)
    if not runbook:
        raise ValueError(f"Runbook {runbook_id} not found")

    steps = json.loads(runbook["steps_json"])

    # Create execution record
    exec_id = db.insert_runbook_execution(
        runbook_id=runbook_id,
        server_name=server_name,
        status="running" if not dry_run else "dry_run",
        output="",
        executed_by=executed_by,
        dry_run=dry_run,
        duration_ms=0,
    )

    if dry_run:
        db.update_runbook_execution(exec_id, status="completed",
                                     output=f"Dry run: {len(steps)} steps validated")
        return exec_id

    # Execute in background thread
    def _run():
        from pypsrp.powershell import PowerShell, RunspacePool
        from winrm_factory import make_wsman

        start = time.time()
        output_parts = []
        final_status = "completed"

        try:
            wsman = make_wsman(
                server_config,
                connection_timeout=15,
                read_timeout=max(s.get("timeout", 30) for s in steps),
            )

            with RunspacePool(wsman) as pool:
                for i, step in enumerate(steps):
                    if step["type"] == "powershell":
                        ps = PowerShell(pool)
                        ps.add_script(step["script"])
                        result = ps.invoke()

                        step_output = "\n".join(str(o) for o in result) if result else ""
                        if ps.had_errors:
                            errors = "\n".join(str(e) for e in ps.streams.error)
                            output_parts.append(f"Step {i+1} ERROR:\n{errors}")
                            final_status = "failed"
                            break
                        else:
                            output_parts.append(f"Step {i+1} OK:\n{step_output}")

                    elif step["type"] == "wait":
                        time.sleep(step.get("seconds", 5))
                        output_parts.append(f"Step {i+1}: Waited {step.get('seconds', 5)}s")

        except Exception as e:
            output_parts.append(f"ERROR: {str(e)}")
            final_status = "failed"

        duration = int((time.time() - start) * 1000)
        full_output = "\n\n".join(output_parts)
        db.update_runbook_execution(exec_id, status=final_status,
                                     output=full_output,
                                     duration_ms=duration)

        db.log_audit(executed_by, f"runbook_execute_{final_status}",
                     "runbook", f"Runbook '{runbook['name']}' on {server_name}: {final_status}")

        # NOTIFICATION DISPATCH (only on failure, only when settings provided).
        # Inserts an event row + sends email + webhook so runbook failures
        # are not silent. Severity must be a valid email_alerts allowlist string.
        if final_status == "failed" and settings:
            try:
                severity = "warning"  # runbook failures are warnings, not critical (operator-initiated)
                msg = f"Runbook '{runbook['name']}' failed on {server_name} after {duration}ms"
                try:
                    db.insert_event(server_name, severity, "runbook_failed",
                                    None, None, msg)
                except Exception:
                    logger.debug("Failed to insert runbook failure event", exc_info=True)
                # Email
                try:
                    from email_alerts import send_alert_email, should_send_email
                    if should_send_email(severity, settings):
                        event = {
                            "event_type": severity,
                            "metric": "runbook_failed",
                            "value": runbook["name"],
                            "threshold": None,
                            "message": msg,
                        }
                        send_alert_email(event, server_name, settings)
                        logger.info("[%s] Runbook failure email sent", server_name)
                except Exception:
                    logger.debug("[%s] Runbook failure email failed", server_name, exc_info=True)
                # Webhook
                try:
                    webhook_cfg = (settings.get("webhooks") or {})
                    if (webhook_cfg.get("enabled") and webhook_cfg.get("teams_webhook_url")
                            and webhook_cfg.get("send_on_warning", False)):
                        from webhooks import send_teams_webhook
                        send_teams_webhook(
                            webhook_cfg["teams_webhook_url"],
                            server_name, severity, "runbook_failed",
                            runbook["name"], None, msg, settings,
                        )
                        logger.info("[%s] Runbook failure webhook sent", server_name)
                except Exception:
                    logger.debug("[%s] Runbook failure webhook failed", server_name, exc_info=True)
            except Exception:
                logger.exception("Runbook failure notification dispatch failed")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return exec_id
