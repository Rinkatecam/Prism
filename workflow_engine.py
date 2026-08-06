"""Workflow execution engine for Prism.

Parses Drawflow canvas JSON, resolves execution graph, and runs blocks
on remote Windows servers via WinRM. Supports sequential and parallel
execution with conditional branching (success/failure paths).
"""

import json
import time
import logging
import threading
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("prism.workflow")

# Marker directory for scheduled workflow deduplication
DATA_DIR = Path(__file__).parent / "data"

# Polling interval for the scheduler loop
SCHEDULER_INTERVAL = 30  # seconds

# S2-11 (P10) heartbeat — workflow scheduler tick freshness. app.py's
# watchdog reads this and treats >5×SCHEDULER_INTERVAL as "dead".
_last_heartbeat: float = 0.0


# ---------------------------------------------------------------------------
# WinRM helpers (patterns reused from runbook_engine / restart_scheduler)
# ---------------------------------------------------------------------------

def _connect_winrm(server_config):
    """Create WinRM connection. server_config is a ServerConfig object.

    Delegates to winrm_factory.make_wsman so HTTPS/cert flags on the
    ServerConfig are honoured uniformly across the app.
    """
    from winrm_factory import make_wsman
    return make_wsman(server_config, connection_timeout=15, read_timeout=30)


def _run_ps(wsman, script):
    """Execute PowerShell script via WinRM, return (success, output).

    NOTE: This entry point sends a raw script string to PowerShell. It is
    only safe when the script body is fully developer-controlled (no user
    input concatenated in) OR when the caller has run the body through
    `ps_sandbox.validate_script`. For any executor that takes user input
    (service name, process name, drive letter, etc.) prefer
    ``_run_ps_builder`` which binds values as typed parameters.

    S3-7 (BL7): every script now gets a correlation-ID prelude so the
    target's PowerShell session has $Global:PrismCorrId set and emits a
    Write-Information record carrying [PrismCorrId=<id>]. Operators can
    grep the on-target event-log + Prism's audit_log + restart_log by the
    same ID to reconstruct cross-system action timelines.

    Output capture: gathers ALL PowerShell streams (success / warning /
    information / verbose / error), not just the pipeline return value.
    This is what makes ``Write-Host "Hello"`` visible in the workflow
    modal — without it, info-stream lines are silently dropped and
    operators see "nothing" for scripts that only use Write-Host.
    """
    try:
        from pypsrp.powershell import PowerShell, RunspacePool
    except ImportError:
        return False, "pypsrp not installed"
    from winrm_factory import correlation_id_prelude

    try:
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            # Wrap the user's script in a script-block piped through
            # ``Out-String`` so PowerShell formats objects to text the way
            # a console would.
            #
            # Without this, ``ps.invoke()`` returns deserialized objects
            # whose ``str()`` is the .NET type name. Concrete example —
            # the 2026-05-22 report:
            #
            #   user wrote:  Start-ADSyncSyncCycle
            #   we showed:   Microsoft.IdentityManagement.PowerShell.
            #                ObjectModel.SchedulerOperationStatus
            #   pwsh shows:  Success
            #
            # ``& { … } | Out-String -Width 500`` runs the user's body in
            # a script block (preserves multi-line / multi-statement) and
            # pipes the pipeline output through PowerShell's formatter so
            # the success stream now contains pre-formatted text. Other
            # streams (Write-Host → information, Write-Warning, …) are
            # not affected by Out-String and continue to be captured by
            # ``_format_ps_output`` directly.
            wrapped = "& {\n" + script + "\n} | Out-String -Width 500"
            ps.add_script(correlation_id_prelude() + wrapped)
            result = ps.invoke()
            output = _format_ps_output(result, ps)
            if ps.had_errors:
                return False, output or "(no output)"
            return True, output
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _format_ps_output(result, ps) -> str:
    """Combine all PowerShell streams into a single human-readable string.

    Order: success → information (Write-Host) → warning → verbose → error.
    Each non-success stream is prefixed so the operator can tell which is
    which when reading the workflow modal. Empty streams are skipped.

    ``ps.streams`` shape comes from pypsrp:
      * .error        — list of ErrorRecord
      * .warning      — list of WarningRecord (Write-Warning)
      * .information  — list of InformationRecord (Write-Host, PS 5.1+)
      * .verbose      — list of VerboseRecord (Write-Verbose, only if -Verbose)
      * .debug        — list of DebugRecord (skipped — almost never useful here)
    """
    parts: list[str] = []

    # Success stream — pipeline output. Usually the "main" content.
    # Note: ``_run_ps`` wraps user scripts in ``| Out-String``, so the
    # objects here are already pre-formatted strings. ``str()`` on those
    # is a no-op. For callers that bypass the Out-String wrap (none
    # currently — every entry point uses ``_run_ps`` or ``_run_ps_builder``),
    # this falls back to str() of the live object.
    if result:
        success_text = "\n".join(
            str(o).rstrip() for o in result if o is not None and str(o).strip()
        )
        if success_text:
            parts.append(success_text)

    # Information stream — Write-Host / Write-Information land here on
    # PS 5.1+. Crucially this is where most ad-hoc operator scripts emit
    # status updates, so we MUST surface it.
    #
    # ``str(InformationRecord)`` is unreliable — depending on pypsrp
    # version it may return "None", a type name, or the actual text.
    # Walk the record's attributes instead, preferring ``message_data``
    # (the pypsrp.complex_objects.InformationRecord field that holds the
    # original payload). Fall back to other common spellings, then
    # finally to str() with a "is it useful?" check.
    try:
        info_lines = []
        for r in (ps.streams.information or []):
            if r is None:
                continue
            text = _extract_record_text(r)
            if text and "PrismCorrId=" not in text:
                info_lines.append(text)
        info_text = "\n".join(info_lines).strip()
        if info_text:
            parts.append(info_text if not parts else "[info]\n" + info_text)
    except Exception:
        pass

    # Warning stream — Write-Warning. Always prefix since these need to
    # stand out from the main output.
    try:
        warn_lines = [
            t for t in (_extract_record_text(r)
                        for r in (ps.streams.warning or []))
            if t
        ]
        warn_text = "\n".join(warn_lines).strip()
        if warn_text:
            parts.append("[warning]\n" + warn_text)
    except Exception:
        pass

    # Verbose stream — only present if the script set $VerbosePreference
    # or someone passed -Verbose to a cmdlet. Useful diagnostic when
    # operators are debugging.
    try:
        verbose_lines = [
            t for t in (_extract_record_text(r)
                        for r in (ps.streams.verbose or []))
            if t
        ]
        verbose_text = "\n".join(verbose_lines).strip()
        if verbose_text:
            parts.append("[verbose]\n" + verbose_text)
    except Exception:
        pass

    # Error stream — non-terminating errors. Even on a "successful" call
    # (had_errors == False) there can be warnings worth showing; for an
    # actual failure path the caller will mark success=False but still
    # want to render these.
    try:
        err_lines = [
            t for t in (_extract_record_text(r)
                        for r in (ps.streams.error or []))
            if t
        ]
        err_text = "\n".join(err_lines).strip()
        if err_text:
            parts.append("[error]\n" + err_text)
    except Exception:
        pass

    return "\n".join(parts)


def _extract_record_text(record) -> str:
    """Best-effort string extraction from a pypsrp stream record.

    pypsrp's ``InformationRecord`` / ``WarningRecord`` / ``ErrorRecord``
    don't have a universal ``__str__`` — depending on version it may
    yield the original message, the type name, or literally ``"None"``
    if ``MessageData`` is unset. The 2026-05-22 report saw the latter:
    ``Start-ADSyncSyncCycle`` emitted an empty InformationRecord and the
    modal showed ``[info]\\nNone``.

    Walk known attribute names in priority order, returning the first
    one that yields non-empty text. Fall through to ``str()`` only as a
    last resort, and only if it produces something other than literal
    "None" / a type-name placeholder.
    """
    if record is None:
        return ""

    # pypsrp field names — these are the documented spellings across
    # versions 0.x and 1.x. ``message_data`` for InformationRecord;
    # ``message`` for WarningRecord / VerboseRecord; ErrorRecord usually
    # has a ``.exception`` and a ``.error_details.message``.
    for attr in ("message_data", "message", "Message", "MessageData",
                 "informational_record", "InformationalRecord"):
        try:
            val = getattr(record, attr, None)
        except Exception:
            continue
        if val is None:
            continue
        text = str(val).strip()
        if text and text.lower() != "none":
            return text

    # ErrorRecord fast-path
    try:
        details = getattr(record, "error_details", None)
        if details is not None:
            msg = getattr(details, "message", None)
            if msg:
                return str(msg).strip()
        exc = getattr(record, "exception", None)
        if exc is not None:
            msg = getattr(exc, "message", None) or str(exc)
            if msg:
                msg = msg.strip()
                if msg and msg.lower() != "none":
                    return msg
    except Exception:
        pass

    # Final fallback — str() of the record itself. Filter the common
    # useless outputs (literal "None", a type name like "Microsoft.X.Y").
    try:
        text = str(record).strip()
        if not text or text.lower() == "none":
            return ""
        # A plain dotted type name is almost certainly not what the user
        # wanted to see; better to show nothing than confusing noise.
        if text.startswith("Microsoft.") and " " not in text and "\n" not in text:
            return ""
        return text
    except Exception:
        return ""


def _run_ps_builder(wsman, builder):
    """Execute a PowerShell pipeline built via add_cmdlet/add_parameter.

    ``builder`` is a callable that receives a fresh ``PowerShell`` instance
    and is expected to populate it via ``add_cmdlet`` / ``add_parameter`` /
    ``add_script(...).add_parameter(...)``. Parameter binding does NOT pass
    user input through the PowerShell parser as code — values are typed and
    bound, so attacker-controlled service/process/drive strings cannot escape
    a quoted argument and inject new statements.

    Returns (success: bool, output: str), shape-compatible with ``_run_ps``.
    """
    try:
        from pypsrp.powershell import PowerShell, RunspacePool
    except ImportError:
        return False, "pypsrp not installed"
    from winrm_factory import correlation_id_prelude

    try:
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            # S3-7: prelude is appended as its own pipeline stage so the
            # builder's structured cmdlet/parameter calls stay un-touched.
            # The prelude only assigns a global + emits a Write-Information
            # record; no parameter-binding interaction.
            ps.add_script(correlation_id_prelude())
            builder(ps)
            result = ps.invoke()
            output = _format_ps_output(result, ps)
            if ps.had_errors:
                return False, output or "(no output)"
            return True, output
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Block executor functions — each returns (success: bool, output: str)
# ---------------------------------------------------------------------------

def _exec_check_service(config, server_map, db, settings):
    """Check if a Windows service is running via WinRM."""
    server_name = config.get("server", "")
    # Block-def field key is ``service_name`` (matches the visible
    # label and the Browse-picker handler). Accept the legacy ``service``
    # key so workflows authored against earlier executor revisions still
    # run — the canonical key is ``service_name``.
    service = (config.get("service_name") or config.get("service") or "").strip()
    if not service:
        return False, "No service name specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    # Parameter binding: `service` is bound as a typed [string] argument to
    # Get-Service -Name, never re-parsed as PowerShell code.
    script = (
        "param([string]$Name)\n"
        "$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue | "
        "Select-Object Name, Status | ConvertTo-Json -Compress; $svc"
    )

    def _build(ps):
        ps.add_script(script).add_parameter("Name", service)

    success, output = _run_ps_builder(wsman, _build)
    if not success:
        return False, output

    try:
        data = json.loads(output.strip()) if output.strip() else {}
        # Status enum: 4 = Running
        status = data.get("Status")
        if status == 4 or str(status).lower() == "running":
            return True, f"Service '{service}' is running"
        return False, f"Service '{service}' status: {status}"
    except (json.JSONDecodeError, TypeError):
        if "running" in output.lower():
            return True, f"Service '{service}' is running"
        return False, f"Service '{service}' status unknown: {output[:200]}"


def _exec_check_process(config, server_map, db, settings):
    """Check if a process is running via WinRM."""
    server_name = config.get("server", "")
    # Canonical key is ``process_name`` (matches block-def + Browse).
    # Fall back to ``process`` for older saved workflows.
    process = (config.get("process_name") or config.get("process") or "").strip()
    if not process:
        return False, "No process name specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    # Parameter binding: `process` is bound as a typed [string] argument.
    script = (
        "param([string]$Name)\n"
        "Get-Process -Name $Name -ErrorAction SilentlyContinue | "
        "Select-Object Name, Id -First 1 | ConvertTo-Json -Compress"
    )

    def _build(ps):
        ps.add_script(script).add_parameter("Name", process)

    success, output = _run_ps_builder(wsman, _build)
    if not success:
        return False, f"Process '{process}' not found: {output}"

    if output.strip():
        return True, f"Process '{process}' is running"
    return False, f"Process '{process}' not found"


def _exec_check_port(config, server_map, db, settings):
    """Check if a TCP port is reachable using health_checker.tcp_probe."""
    from health_checker import tcp_probe

    server_name = config.get("server", "")
    port = config.get("port")
    if not port:
        return False, "No port specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    result = tcp_probe(server_cfg.host, int(port), timeout=10)
    if result["status"] == "up":
        return True, f"Port {port} is open ({result['response_time_ms']:.0f}ms)"
    return False, f"Port {port} is not reachable: {result.get('error', 'unknown')}"


def _exec_check_url(config, server_map, db, settings):
    """Check a URL using health_checker.http_check."""
    from health_checker import http_check
    import urllib.parse

    url = config.get("url", "")
    expected_status = int(config.get("expected_status", 200))
    if not url:
        return False, "No URL specified"

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    use_ssl = parsed.scheme == "https"

    result = http_check(host, port, path=path, use_ssl=use_ssl,
                        expected_status=expected_status, timeout=15)
    if result["status"] == "up":
        return True, f"URL {url} returned {result.get('http_status')} ({result['response_time_ms']:.0f}ms)"
    return False, f"URL check failed: {result.get('error', 'unknown')}"


def _exec_restart_service(config, server_map, db, settings):
    """Restart a Windows service via WinRM."""
    server_name = config.get("server", "")
    # Accept both canonical ``service_name`` (block-def field key) and
    # legacy ``service`` for older workflows.
    service = (config.get("service_name") or config.get("service") or "").strip()
    if not service:
        return False, "No service name specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    script = (
        "param([string]$Name)\n"
        "Restart-Service -Name $Name -Force; Start-Sleep 2; "
        "(Get-Service -Name $Name).Status"
    )

    def _build(ps):
        ps.add_script(script).add_parameter("Name", service)

    success, output = _run_ps_builder(wsman, _build)
    if success:
        return True, f"Service '{service}' restarted: {output.strip()}"
    return False, f"Failed to restart '{service}': {output}"


def _exec_start_service(config, server_map, db, settings):
    """Start a Windows service via WinRM."""
    server_name = config.get("server", "")
    service = (config.get("service_name") or config.get("service") or "").strip()
    if not service:
        return False, "No service name specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    script = (
        "param([string]$Name)\n"
        "Start-Service -Name $Name; Start-Sleep 2; "
        "(Get-Service -Name $Name).Status"
    )

    def _build(ps):
        ps.add_script(script).add_parameter("Name", service)

    success, output = _run_ps_builder(wsman, _build)
    if success:
        return True, f"Service '{service}' started: {output.strip()}"
    return False, f"Failed to start '{service}': {output}"


def _exec_stop_service(config, server_map, db, settings):
    """Stop a Windows service via WinRM."""
    server_name = config.get("server", "")
    service = (config.get("service_name") or config.get("service") or "").strip()
    if not service:
        return False, "No service name specified"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    script = (
        "param([string]$Name)\n"
        "Stop-Service -Name $Name -Force; Start-Sleep 1; "
        "(Get-Service -Name $Name).Status"
    )

    def _build(ps):
        ps.add_script(script).add_parameter("Name", service)

    success, output = _run_ps_builder(wsman, _build)
    if success:
        return True, f"Service '{service}' stopped: {output.strip()}"
    return False, f"Failed to stop '{service}': {output}"


def _exec_run_powershell(config, server_map, db, settings):
    """Run a custom PowerShell script on a remote server.

    Scripts pass through the ps_sandbox allowlist. Operators can extend the
    allowlist via Settings → Workflows → "Allowed cmdlets" or disable the
    sandbox entirely (NOT recommended) with `workflows.sandbox.enabled = false`.
    """
    from ps_sandbox import validate_script, get_sandbox_settings

    server_name = config.get("server", "")
    script = config.get("script", "")
    if not script:
        return False, "No script specified"

    enabled, extras, max_len = get_sandbox_settings(settings)
    if len(script) > max_len:
        return False, f"Script too long ({len(script)} > {max_len} chars)"
    ok, reason = validate_script(script, allowed_cmdlets=extras, enabled=enabled)
    if not ok:
        logger.warning("Sandbox rejected run_powershell on %s: %s", server_name, reason)
        return False, f"Blocked by PowerShell sandbox: {reason}"

    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    wsman = _connect_winrm(server_cfg)
    return _run_ps(wsman, script)


def _exec_restart_server(config, server_map, db, settings):
    """Restart a remote server via WinRM."""
    server_name = config.get("server", "")
    server_cfg = server_map.get(server_name)
    if not server_cfg:
        return False, f"Server '{server_name}' not found"

    force = config.get("force", True)
    wsman = _connect_winrm(server_cfg)
    script = "Restart-Computer -Force" if force else "Restart-Computer"
    success, output = _run_ps(wsman, script)
    if success:
        return True, f"Restart command sent to '{server_name}'"
    return False, f"Failed to restart '{server_name}': {output}"


def _exec_wait(config, server_map, db, settings):
    """Sleep for a configured duration. Always succeeds."""
    duration = int(config.get("duration", 5))
    time.sleep(duration)
    return True, f"Waited {duration}s"


def _exec_flow_placeholder(config, server_map, db, settings):
    """and_gate / or_gate / retry are evaluated by the graph executor
    (_execute_graph) BEFORE the per-node executor lookup. This is reached only
    if one is somehow executed out of graph context — fail loud rather than
    silently succeed (the old _exec_wait no-op behaviour)."""
    return False, "flow-control block executed outside graph context"


def _exec_send_email(config, server_map, db, settings):
    """Send an alert email using email_alerts module.

    Reads ``body`` (the canvas field name) but falls back to the older
    ``message`` key so workflows saved before 2026-05 still work. The
    subject defaults to "Workflow notification" if not set. Both subject
    and body have already had ``{{step.X.output}}``-style variables
    substituted by ``_execute_graph`` before we get here.
    """
    subject = config.get("subject") or "Workflow notification"
    body = config.get("body") or config.get("message") or subject
    try:
        from email_alerts import send_alert_email
        event = {
            "event_type": "info",
            "metric": None,
            "value": None,
            "threshold": None,
            # email_alerts.send_alert_email composes the email from these
            # fields; ``message`` IS the body in that module's model.
            "message": body,
            "subject_override": subject,
        }
        ok = send_alert_email(event, "Workflow Engine", settings)
        if ok:
            return True, "Email sent"
        return False, "Email sending failed (check SMTP config)"
    except Exception as e:
        return False, f"Email error: {e}"


def _exec_send_webhook(config, server_map, db, settings):
    """Send a Teams webhook notification. ``message`` field carries the
    body (with {{step.X.output}}-style variables already substituted)."""
    message = config.get("message", "Workflow notification")
    try:
        from webhooks import send_teams_webhook
        webhook_cfg = settings.get("webhooks", {})
        # Operator can override the global webhook URL via the block's
        # ``url`` field — useful for one-workflow-to-different-channel
        # setups. Falls back to the global if the block didn't set one.
        url = (config.get("url") or "").strip() or webhook_cfg.get("teams_webhook_url", "")
        if not url:
            return False, "No webhook URL configured (set on the block or in Settings → Webhooks)"
        result = send_teams_webhook(url, "Workflow Engine", "workflow_info",
                                    None, None, None, message, settings)
        if result.get("ok"):
            return True, "Webhook sent"
        return False, f"Webhook failed: {result.get('error', 'unknown')}"
    except Exception as e:
        return False, f"Webhook error: {e}"


def _exec_log_event(config, server_map, db, settings):
    """Insert an event into the database."""
    server_name = config.get("server", "Workflow")
    message = config.get("message", "Workflow event")
    severity = config.get("severity", "info")
    try:
        db.insert_event(server_name, severity, "workflow", None, None, message)
        return True, f"Event logged: [{severity}] {message}"
    except Exception as e:
        return False, f"Failed to log event: {e}"


# ── New block executors (additions) ──

def _exec_check_disk(config, server_map, db, settings):
    """Check if a drive has enough free space."""
    server = server_map.get(config.get("server"))
    if not server:
        return False, f"Server '{config.get('server')}' not found"
    drive = config.get("drive", "C")
    min_free = float(config.get("min_free_pct", 10))
    try:
        wsman = _connect_winrm(server)
        # Parameterised: drive name is bound as a typed [string]; the script
        # body itself contains no user input.
        script = (
            "param([string]$Drive)\n"
            "$d = Get-PSDrive -Name $Drive -ErrorAction Stop; "
            "$total = $d.Used + $d.Free; "
            "$pct = [math]::Round($d.Free / $total * 100, 1); "
            "@{ FreePct = $pct; FreeGB = [math]::Round($d.Free/1GB,1); "
            "TotalGB = [math]::Round($total/1GB,1) } | ConvertTo-Json"
        )

        def _build(ps):
            ps.add_script(script).add_parameter("Drive", drive)

        success, output = _run_ps_builder(wsman, _build)
        if not success:
            return False, output
        import json as _j
        data = _j.loads(output)
        free_pct = data.get("FreePct", 0)
        if free_pct >= min_free:
            return True, f"Drive {drive}: {free_pct}% free ({data.get('FreeGB', '?')} GB / {data.get('TotalGB', '?')} GB)"
        else:
            return False, f"Drive {drive}: only {free_pct}% free (threshold: {min_free}%)"
    except Exception as e:
        return False, f"Disk check failed: {e}"


def _exec_kill_process(config, server_map, db, settings):
    """Kill a process by name."""
    server = server_map.get(config.get("server"))
    if not server:
        return False, f"Server '{config.get('server')}' not found"
    proc = config.get("process_name", "").strip()
    if not proc:
        return False, "No process name specified"
    try:
        wsman = _connect_winrm(server)
        # Parameterised: `proc` is bound as a typed [string] -- the
        # half-baked single-quote-doubling escape from the prior version is
        # unnecessary because the value never enters the script body as text.
        script = (
            "param([string]$Name)\n"
            "Stop-Process -Name $Name -Force -ErrorAction Stop; "
            "\"Process $Name killed\""
        )

        def _build(ps):
            ps.add_script(script).add_parameter("Name", proc)

        return _run_ps_builder(wsman, _build)
    except Exception as e:
        return False, f"Kill process failed: {e}"


def _exec_clear_temp(config, server_map, db, settings):
    """Clear temp files on a server."""
    server = server_map.get(config.get("server"))
    if not server:
        return False, f"Server '{config.get('server')}' not found"
    try:
        wsman = _connect_winrm(server)
        script = (
            "Remove-Item 'C:\\Temp\\*' -Recurse -Force -ErrorAction SilentlyContinue; "
            "Remove-Item \"$env:WINDIR\\Temp\\*\" -Recurse -Force -ErrorAction SilentlyContinue; "
            "'Temp files cleared'"
        )
        return _run_ps(wsman, script)
    except Exception as e:
        return False, f"Clear temp failed: {e}"


def _exec_condition(config, server_map, db, settings):
    """Evaluate a PowerShell expression as boolean condition.

    Expressions go through the same allowlist as run_powershell. We wrap the
    expression in `if (..) { 'TRUE' } else { 'FALSE' }` only AFTER it has
    been validated, so the boolean wrapper itself can never be smuggled past
    the sandbox.
    """
    from ps_sandbox import validate_script, get_sandbox_settings

    server = server_map.get(config.get("server"))
    if not server:
        return False, f"Server '{config.get('server')}' not found"
    expr = config.get("expression", "").strip()
    if not expr:
        return False, "No expression specified"

    enabled, extras, max_len = get_sandbox_settings(settings)
    if len(expr) > max_len:
        return False, f"Expression too long ({len(expr)} > {max_len} chars)"
    ok, reason = validate_script(expr, allowed_cmdlets=extras, enabled=enabled)
    if not ok:
        logger.warning("Sandbox rejected condition expression: %s", reason)
        return False, f"Blocked by PowerShell sandbox: {reason}"

    try:
        wsman = _connect_winrm(server)
        script = f"if ({expr}) {{ 'TRUE' }} else {{ 'FALSE' }}"
        success, output = _run_ps(wsman, script)
        if not success:
            return False, output
        result = output.strip().upper()
        return result == "TRUE", f"Expression result: {output.strip()}"
    except Exception as e:
        return False, f"Condition check failed: {e}"


# ─────────────────────────────────────────────────────────────────────
# Variable substitution — {{step.<id>.<field>}} / {{workflow.<field>}}
# ─────────────────────────────────────────────────────────────────────
#
# Operators can reference output of upstream blocks in notification text
# fields. Substitution happens at execute time, AFTER the prior step has
# run, so the actual output (not a literal placeholder) flows through.
#
# Syntax:
#   {{step.<node_id>.output}}    — text output of step N
#   {{step.<node_id>.success}}   — "true" or "false"
#   {{step.<node_id>.error}}     — error message (empty on success)
#   {{workflow.name}}            — workflow name
#   {{workflow.id}}              — workflow id
#
# We deliberately skip the ``script`` config key — variables inside a
# PowerShell script would bypass the ps_sandbox.validate_script gate
# (the sandbox sees the template, not the substituted text). Operators
# who need dynamic PS scripts should compose the script entirely from
# constants + use parameter binding in their own workflow design.

import re as _re_sub  # local alias so callers can also import re without conflict

_VAR_PATTERN = _re_sub.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_NO_SUBSTITUTE_KEYS = {"script"}  # see safety note above


def _substitute_variables(text, executed: dict, workflow: dict | None = None) -> str:
    """Replace ``{{...}}`` placeholders in a single string.

    Unknown references render as ``<unknown ...>`` rather than the raw
    placeholder so the substituted message visibly carries the error to
    the operator instead of silently shipping a literal ``{{step.99.output}}``.
    """
    if not text or "{{" not in text:
        return text

    def _replace(m):
        path = m.group(1).strip()
        if "." not in path:
            return m.group(0)
        head, _, rest = path.partition(".")
        if head == "step":
            # rest is "<node_id>.<field>"
            if "." not in rest:
                return f"<malformed step ref: {path}>"
            node_id, _, field = rest.partition(".")
            entry = executed.get(node_id) or executed.get(str(node_id))
            if entry is None:
                return f"<unknown step {node_id}>"
            success, output = entry
            if field == "output":
                return str(output or "")
            if field == "success":
                return "true" if success else "false"
            if field == "error":
                return "" if success else str(output or "")
            return f"<unknown field {field}>"
        if head == "workflow":
            field = rest
            if workflow and field in workflow:
                return str(workflow[field])
            return f"<unknown workflow.{field}>"
        return m.group(0)

    return _VAR_PATTERN.sub(_replace, text)


def _substitute_config_variables(config: dict, executed: dict,
                                  workflow: dict | None = None) -> dict:
    """Return a shallow-copied config dict with variables resolved in
    every string value (except keys in ``_NO_SUBSTITUTE_KEYS``).

    Non-string values pass through untouched. Returns a NEW dict so the
    original (which is stored on the canvas node and persisted) is not
    mutated by execution-time substitution.
    """
    if not isinstance(config, dict):
        return config
    out = {}
    for k, v in config.items():
        if k in _NO_SUBSTITUTE_KEYS or not isinstance(v, str):
            out[k] = v
        else:
            out[k] = _substitute_variables(v, executed, workflow)
    return out


def _exec_trigger(config, server_map, db, settings):
    """No-op executor for trigger blocks (manual / schedule / event).

    Trigger blocks live on the canvas as metadata that describes WHEN
    the workflow runs. Their type+config gets mirrored to the workflow
    row's trigger_type / trigger_config columns at save time (see
    routes/api/workflows.py:_sync_trigger_from_canvas) and the
    workflow_scheduler_loop reads those columns to decide which
    workflows to fire.

    During graph execution the trigger block is just a pass-through —
    it always "succeeds" and routes its single output to whatever's
    wired downstream. The trigger has already done its job by the time
    we get here; we just need to not block the graph.
    """
    return True, ""


# ── Block Executor Registry ──
BLOCK_EXECUTORS = {
    # Triggers — no-op pass-throughs (see _exec_trigger docstring)
    "trigger_manual": _exec_trigger,
    "trigger_schedule": _exec_trigger,
    "trigger_event": _exec_trigger,
    "check_service": _exec_check_service,
    "check_process": _exec_check_process,
    "check_port": _exec_check_port,
    "check_url": _exec_check_url,
    "check_disk": _exec_check_disk,
    "restart_service": _exec_restart_service,
    "start_service": _exec_start_service,
    "stop_service": _exec_stop_service,
    "run_powershell": _exec_run_powershell,
    "restart_server": _exec_restart_server,
    "kill_process": _exec_kill_process,
    "clear_temp": _exec_clear_temp,
    "wait": _exec_wait,
    "and_gate": _exec_flow_placeholder,  # evaluated in _execute_graph (feature 2.5)
    "or_gate": _exec_flow_placeholder,   # evaluated in _execute_graph (feature 2.5)
    "condition": _exec_condition,
    "retry": _exec_flow_placeholder,     # evaluated in _execute_graph (feature 2.5)
    "send_email": _exec_send_email,
    "send_webhook": _exec_send_webhook,
    "log_event": _exec_log_event,
}


# Dry-run pass-throughs: the only blocks that still "run" in dry-run (instant
# no-op triggers). Every other block is recorded as a dry_run step instead of
# executing — no WinRM, no notifications, no waits.
_DRY_RUN_PASSTHROUGH = frozenset({"trigger_manual", "trigger_schedule", "trigger_event"})


# Verify-after (feature 2.6): remediation block type -> the check to re-run
# against the SAME config (service_name/server) after a successful remediation.
# Both remediations here should leave the service RUNNING, which check_service
# confirms. (restart_server is intentionally excluded — the box is rebooting.)
_VERIFY_AFTER_MAP = {
    "restart_service": "check_service",
    "start_service": "check_service",
}


# ---------------------------------------------------------------------------
# Drawflow canvas parser
# ---------------------------------------------------------------------------

def parse_canvas(canvas_json: dict) -> dict:
    """Parse Drawflow JSON into an execution graph.

    Drawflow format:
        canvas_json["drawflow"]["Home"]["data"] = {
            "1": {
                "id": 1, "name": "check_service", "data": {config...},
                "class": "...", "html": "...",
                "inputs": {"input_1": {"connections": [{"node": "2", "input": "output_1"}]}},
                "outputs": {
                    "output_1": {"connections": [...]},  # success path
                    "output_2": {"connections": [...]},  # failure path
                },
                ...
            },
            ...
        }

    Returns:
        {
            "nodes": {
                node_id: {
                    "type": str,
                    "config": dict,
                    "label": str,
                    "outputs": {"success": [node_ids], "fail": [node_ids]},
                }
            },
            "start_nodes": [node_ids with no inputs],
        }
    """
    nodes = {}
    has_incoming = set()
    parents_map = {}  # target node_id -> [source node_ids] (reverse adjacency)

    # Navigate Drawflow structure
    drawflow = canvas_json.get("drawflow", canvas_json)
    if isinstance(drawflow, dict):
        # Standard Drawflow: drawflow.Home.data or drawflow.data
        home = drawflow.get("Home", drawflow)
        raw_nodes = home.get("data", {})
    else:
        raw_nodes = {}

    for node_id_str, node_data in raw_nodes.items():
        node_id = str(node_data.get("id", node_id_str))
        node_type = node_data.get("name", "unknown")
        config = node_data.get("data", {})
        label = config.get("label", node_data.get("html", node_type))

        # Per-block disable state. Operator can right-click a node and
        # choose "Disable" — the canvas grays it out and the executor
        # treats it as a no-op pass-through (status "skipped", success
        # path followed). Lets you temporarily take a step out of a
        # workflow without rewiring connections.
        disabled = bool(config.get("disabled"))

        # Per-connection disable state lives on the SOURCE node as
        # ``_disabled_conns`` — a list of signatures
        # ``"<output_class>:<target_node>:<input_class>"`` identifying
        # connections that should be skipped during execution. Storing
        # this on the source node (rather than on the connection itself,
        # which Drawflow's serialization doesn't expose) keeps the JSON
        # round-trip clean.
        disabled_conns = set(config.get("_disabled_conns", []) or [])

        # Parse output connections
        outputs_raw = node_data.get("outputs", {})

        # output_1 = success path, output_2 = failure path
        success_targets = []
        fail_targets = []

        out1 = outputs_raw.get("output_1", {})
        for conn in out1.get("connections", []):
            target = str(conn.get("node", ""))
            target_input = conn.get("input", "input_1")
            sig = f"output_1:{target}:{target_input}"
            if target and sig not in disabled_conns:
                success_targets.append(target)
                has_incoming.add(target)
                parents_map.setdefault(target, []).append(node_id)

        out2 = outputs_raw.get("output_2", {})
        for conn in out2.get("connections", []):
            target = str(conn.get("node", ""))
            target_input = conn.get("input", "input_1")
            sig = f"output_2:{target}:{target_input}"
            if target and sig not in disabled_conns:
                fail_targets.append(target)
                has_incoming.add(target)
                parents_map.setdefault(target, []).append(node_id)

        nodes[node_id] = {
            "type": node_type,
            "config": config,
            "label": label,
            "disabled": disabled,
            "outputs": {
                "success": success_targets,
                "fail": fail_targets,
            },
        }

    # Attach reverse-adjacency (parents) — used by and_gate/or_gate/retry.
    for nid, n in nodes.items():
        n["parents"] = parents_map.get(nid, [])

    # Start nodes = nodes with no incoming connections
    start_nodes = [nid for nid in nodes if nid not in has_incoming]

    # If no clear start nodes, fall back to first node
    if not start_nodes and nodes:
        start_nodes = [min(nodes.keys(), key=lambda x: int(x) if x.isdigit() else 0)]

    return {"nodes": nodes, "start_nodes": start_nodes}


# ---------------------------------------------------------------------------
# Graph execution
# ---------------------------------------------------------------------------

def _run_retry(target_node, retry_config, forward_success, server_map, db,
               settings, executed, workflow_info):
    """Re-execute the retry block's upstream parent up to max_attempts TOTAL.

    The forward pass already ran the parent once (attempt 1 = ``forward_success``);
    this performs up to ``max_attempts - 1`` further attempts with ``delay``
    seconds between, stopping at the first success. Returns (success, output).
    """
    try:
        max_attempts = max(1, int(retry_config.get("max_attempts",
                                                    retry_config.get("attempts", 3))))
    except (TypeError, ValueError):
        max_attempts = 3
    try:
        delay = max(0, int(retry_config.get("delay", retry_config.get("delay_seconds", 5))))
    except (TypeError, ValueError):
        delay = 5

    if forward_success:
        return True, f"retry: upstream already succeeded (attempt 1/{max_attempts})"
    if target_node is None:
        return False, "retry: no upstream block to retry"
    ttype = target_node.get("type", "")
    texec = BLOCK_EXECUTORS.get(ttype)
    if texec is None or ttype in ("and_gate", "or_gate", "retry"):
        return False, f"retry: upstream block '{ttype}' cannot be retried"
    tconfig = _substitute_config_variables(target_node.get("config", {}), executed, workflow_info)

    last = "failed"
    for attempt in range(2, max_attempts + 1):
        if delay:
            time.sleep(delay)
        try:
            success, output = texec(tconfig, server_map, db, settings)
        except Exception as e:
            success, output = False, f"Exception: {type(e).__name__}: {e}"
        last = output
        if success:
            return True, f"retry: '{ttype}' succeeded on attempt {attempt}/{max_attempts}"
    return False, f"retry: '{ttype}' failed after {max_attempts} attempts — {last}"


def _execute_graph(db, exec_id, graph, server_map, settings, workflow_info=None,
                   dry_run=False):
    """Walk the graph and execute nodes in order, following success/fail
    paths. ``workflow_info`` is an optional dict providing context for
    ``{{workflow.name}}``-style variable substitution (name, id).

    When ``dry_run`` is True, side-effecting blocks (WinRM actions/checks,
    notifications, waits) are NOT executed — each is recorded status="dry_run"
    and treated as success so the operator sees the plan without acting."""
    nodes = graph["nodes"]
    executed = {}  # node_id -> (success, output)
    defer_counts = {}           # gate node_id -> times re-enqueued (deferral cap)
    max_defers = len(nodes) + 1

    queue = list(graph["start_nodes"])

    while queue:
        node_id = queue.pop(0)
        if node_id in executed:
            continue

        node = nodes.get(node_id)
        if not node:
            continue

        node_type = node["type"]
        config = node.get("config", {})
        label = node.get("label", node_type)
        server_name = config.get("server", "")

        # Gate deferral (feature 2.5): a gate must wait until ALL its parents
        # have resolved. Re-enqueue up to max_defers times (bounded so a parent
        # on a never-taken branch can't spin); then evaluate with what resolved.
        # Done BEFORE the step record so a deferred gate doesn't spam step rows.
        if (node_type in ("and_gate", "or_gate")
                and not node.get("disabled") and not dry_run):
            _gate_parents = node.get("parents", [])
            if _gate_parents and any(p not in executed for p in _gate_parents):
                dc = defer_counts.get(node_id, 0) + 1
                defer_counts[node_id] = dc
                if dc <= max_defers:
                    queue.append(node_id)
                    continue

        # Insert step record
        step_id = db.insert_workflow_step(exec_id, node_id, node_type, label, server_name)
        db.update_workflow_step(step_id, status="running",
                                started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Disabled blocks: skip execution entirely. Treat as a successful
        # no-op so the workflow continues along the SUCCESS path. The
        # step row gets ``status="skipped"`` so operators can tell the
        # difference between a real run and a disabled-bypass run.
        if node.get("disabled"):
            db.update_workflow_step(
                step_id,
                status="skipped",
                output="(block disabled — skipped)",
                error="",
                completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            executed[node_id] = (True, "(disabled — skipped)")
            logger.info("Node %s [%s] SKIPPED (disabled)", node_id, node_type)
            # Propagate to success path only; disabled blocks don't fail.
            queue.extend(node.get("outputs", {}).get("success", []))
            continue

        # Resolve {{step.X.output}} / {{workflow.name}} variables in the
        # block's config BEFORE handing it to the executor. Substitution
        # is per-string-value so non-text fields (numbers, server names)
        # pass through. The ``script`` key is skipped to keep the
        # ps_sandbox guarantee — see _NO_SUBSTITUTE_KEYS.
        config = _substitute_config_variables(config, executed, workflow_info)

        # Dry-run: don't execute side-effecting blocks. Record the step and
        # follow the success path so the operator sees the plan without acting.
        # Pure pass-through triggers still "run" (instant no-ops).
        if dry_run and node_type not in _DRY_RUN_PASSTHROUGH:
            db.update_workflow_step(
                step_id, status="dry_run",
                output="(dry-run — not executed)", error="",
                completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            executed[node_id] = (True, "(dry-run — not executed)")
            logger.info("Node %s [%s] DRY-RUN (not executed)", node_id, node_type)
            queue.extend(node.get("outputs", {}).get("success", []))
            continue

        # Flow-control blocks (feature 2.5) — evaluated by the graph executor,
        # not a per-node executor.
        if node_type in ("and_gate", "or_gate"):
            parents = node.get("parents", [])
            resolved = [p for p in parents if p in executed]
            oks = [executed[p][0] for p in resolved]
            if node_type == "and_gate":
                gate_ok = bool(parents) and len(resolved) == len(parents) and all(oks)
            else:  # or_gate
                gate_ok = any(oks)
            gout = f"{node_type}: {sum(1 for x in oks if x)}/{len(parents)} parents succeeded"
            db.update_workflow_step(
                step_id, status="completed" if gate_ok else "failed",
                output=gout, error="" if gate_ok else "gate condition not met",
                completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            executed[node_id] = (gate_ok, gout)
            logger.info("Node %s [%s] %s: %s", node_id, node_type,
                        "PASS" if gate_ok else "BLOCK", gout)
            queue.extend(node["outputs"]["success"] if gate_ok else node["outputs"]["fail"])
            continue

        if node_type == "retry":
            parents = node.get("parents", [])
            parent_id = parents[0] if parents else None
            forward_success = executed.get(parent_id, (False, ""))[0] if parent_id else False
            r_success, r_output = _run_retry(
                nodes.get(parent_id), config, forward_success,
                server_map, db, settings, executed, workflow_info,
            )
            db.update_workflow_step(
                step_id, status="completed" if r_success else "failed",
                output=r_output, error="" if r_success else r_output,
                completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            executed[node_id] = (r_success, r_output)
            logger.info("Node %s [retry] %s: %s", node_id,
                        "OK" if r_success else "FAIL", (r_output or "")[:100])
            queue.extend(node["outputs"]["success"] if r_success else node["outputs"]["fail"])
            continue

        # Execute block
        executor = BLOCK_EXECUTORS.get(node_type)
        if executor:
            try:
                success, output = executor(config, server_map, db, settings)
            except Exception as e:
                success, output = False, f"Exception: {type(e).__name__}: {e}"
                logger.exception("Block %s (node %s) raised an exception", node_type, node_id)
        else:
            success, output = False, f"Unknown block type: {node_type}"

        # Verify-after (feature 2.6): after a SUCCESSFUL remediation whose config
        # opts in via ``verify_after``, re-check the same target. If the follow-up
        # check fails, the remediation is treated as failed (routes to fail), so a
        # "restarted but still down" outcome isn't reported green. Own step row.
        # Skipped in dry-run (would open a real WinRM connection).
        if (success and not dry_run and config.get("verify_after")
                and node_type in _VERIFY_AFTER_MAP):
            verify_type = _VERIFY_AFTER_MAP[node_type]
            verify_exec = BLOCK_EXECUTORS.get(verify_type)
            if verify_exec is not None:
                vstep = db.insert_workflow_step(
                    exec_id, f"{node_id}:verify", verify_type,
                    f"verify: {label}", server_name)
                db.update_workflow_step(vstep, status="running",
                                        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                try:
                    v_ok, v_out = verify_exec(config, server_map, db, settings)
                except Exception as e:
                    v_ok, v_out = False, f"Exception: {type(e).__name__}: {e}"
                db.update_workflow_step(
                    vstep, status="completed" if v_ok else "failed",
                    output=v_out, error="" if v_ok else v_out,
                    completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                logger.info("Node %s [%s] verify-after %s: %s", node_id, node_type,
                            "OK" if v_ok else "FAIL", (v_out or "")[:100])
                if not v_ok:
                    success = False
                    output = f"{output} | verify-after FAILED: {v_out}"

        # Update step record. Cap output at a reasonable size so a chatty
        # Get-WinEvent / Get-ChildItem doesn't blow up the DB row — but
        # large enough that most real scripts fit (5 000 was too tight,
        # operators were seeing their actual answer truncated).
        _OUTPUT_CAP = 20_000
        _ERROR_CAP = 5_000

        def _cap(s: str, cap: int) -> str:
            if not s:
                return ""
            if len(s) <= cap:
                return s
            return s[:cap] + f"\n…(truncated — {len(s) - cap} more chars; raise the cap in workflow_engine.py if you need them)"

        db.update_workflow_step(
            step_id,
            status="completed" if success else "failed",
            output=_cap(output, _OUTPUT_CAP),
            error="" if success else _cap(output, _ERROR_CAP),
            completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        executed[node_id] = (success, output)
        logger.info("Node %s [%s] %s: %s", node_id, node_type,
                     "OK" if success else "FAIL", (output or "")[:100])

        # Follow appropriate output connections
        if success:
            queue.extend(node.get("outputs", {}).get("success", []))
        else:
            fail_targets = node.get("outputs", {}).get("fail", [])
            if fail_targets:
                queue.extend(fail_targets)
            # If no failure path, the branch effectively stops here


# ---------------------------------------------------------------------------
# Execution circuit breaker (feature 2.6) — the loop-stopper.
#
# In-memory + per workflow_id (a process restart clears it, matching
# _event_trigger_state). Suppresses ONLY auto-fires (scheduled/event) after N
# consecutive failed executions; manual runs always proceed (an audited
# override so an operator is never locked out of their own remediation during
# an incident). A success resets it; after a cooldown it half-opens for one
# trial. Configurable under settings["workflows"]["circuit_breaker"].
# ---------------------------------------------------------------------------

_breaker_lock = threading.Lock()
_breaker: dict[int, dict] = {}  # workflow_id -> {"fails": int, "opened_at": float|None}
_AUTO_TRIGGER_SOURCES = frozenset({"scheduled", "event"})


def _breaker_config(settings):
    cfg = ((settings or {}).get("workflows", {}) or {}).get("circuit_breaker", {}) or {}
    enabled = cfg.get("enabled", True)
    try:
        max_failures = max(1, int(cfg.get("max_consecutive_failures", 3)))
    except (TypeError, ValueError):
        max_failures = 3
    try:
        cooldown_s = max(0.0, float(cfg.get("cooldown_seconds", 1800)))
    except (TypeError, ValueError):
        cooldown_s = 1800.0
    return enabled, max_failures, cooldown_s


def _breaker_allows(workflow_id, trigger_source, settings, now=None):
    """True if this execution may proceed. Only auto-fires are ever blocked."""
    if trigger_source not in _AUTO_TRIGGER_SOURCES:
        return True
    enabled, _max_failures, cooldown_s = _breaker_config(settings)
    if not enabled:
        return True
    now = time.time() if now is None else now
    with _breaker_lock:
        st = _breaker.get(workflow_id)
        if not st or st.get("opened_at") is None:
            return True  # breaker closed
        # Open: allow a single half-open trial once the cooldown has elapsed.
        return (now - st["opened_at"]) >= cooldown_s


def _breaker_record(workflow_id, success, settings, now=None):
    """Record an execution outcome. Success resets; failure increments and opens
    the breaker at the configured threshold."""
    enabled, max_failures, _cooldown_s = _breaker_config(settings)
    if not enabled:
        return
    now = time.time() if now is None else now
    with _breaker_lock:
        st = _breaker.setdefault(workflow_id, {"fails": 0, "opened_at": None})
        if success:
            st["fails"] = 0
            st["opened_at"] = None
        else:
            st["fails"] += 1
            if st["fails"] >= max_failures:
                st["opened_at"] = now


def _should_record_breaker(trigger_source, dry_run):
    """Only real auto-fires feed the breaker: manual runs are an override (must
    never OPEN it and lock the operator out of their own remediation) and a
    dry-run must never RESET an open breaker for a still-broken workflow."""
    return (not dry_run) and (trigger_source in _AUTO_TRIGGER_SOURCES)


# Per-workflow concurrency lock (feature 2.6) — never run two instances of the
# same workflow at once. In-memory; a restart clears it.
_inflight_lock = threading.Lock()
_inflight: set[int] = set()


def _try_acquire_inflight(workflow_id) -> bool:
    with _inflight_lock:
        if workflow_id in _inflight:
            return False
        _inflight.add(workflow_id)
        return True


def _release_inflight(workflow_id) -> None:
    with _inflight_lock:
        _inflight.discard(workflow_id)


# ---------------------------------------------------------------------------
# Public API: execute a workflow
# ---------------------------------------------------------------------------

def execute_workflow(db, workflow_id, get_servers, settings,
                     executed_by="system", trigger_source="manual", dry_run=False):
    """Execute a workflow in a background thread.

    Returns execution_id immediately. The thread updates DB as it progresses.

    Args:
        db: Database instance.
        workflow_id: ID of the workflow to execute.
        get_servers: Callable returning list of ServerConfig objects.
        settings: Current settings dict.
        executed_by: Username for audit trail.
        trigger_source: "manual", "scheduled", or "api".

    Returns:
        int: The execution ID.
    """
    workflow = db.get_workflow(workflow_id)
    if not workflow:
        raise ValueError(f"Workflow {workflow_id} not found")

    # Circuit breaker (feature 2.6): suppress auto-fires while open so a failing
    # unattended remediation can't re-fire in a loop. Manual runs always proceed.
    if not _breaker_allows(workflow_id, trigger_source, settings):
        logger.warning(
            "Workflow %d %s fire suppressed — circuit breaker open after "
            "repeated failures", workflow_id, trigger_source,
        )
        try:
            db.log_audit(
                executed_by, "workflow_suppressed_breaker", "workflow",
                f"Workflow '{workflow.get('name', workflow_id)}' {trigger_source} "
                f"fire suppressed — circuit breaker open after repeated failures",
            )
        except Exception:
            pass
        # Record a terminal "suppressed" execution row so the operator can SEE
        # that unattended remediation is paused — otherwise it stops silently.
        try:
            sup_id = db.create_workflow_execution(workflow_id, trigger_source, executed_by)
            db.update_workflow_execution(
                sup_id, status="suppressed",
                summary="Auto-fire suppressed — circuit breaker open after repeated failures",
                completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_ms=0,
            )
            return sup_id
        except Exception:
            logger.debug("Failed to record suppressed execution row", exc_info=True)
            return None

    # Per-workflow concurrency lock (feature 2.6): never run two instances of
    # the same workflow at once (kills scheduler+event double-fire and
    # overlapping remediation of the same servers).
    if not _try_acquire_inflight(workflow_id):
        logger.warning(
            "Workflow %d %s fire skipped — a run is already in flight",
            workflow_id, trigger_source,
        )
        try:
            db.log_audit(
                executed_by, "workflow_skipped_inflight", "workflow",
                f"Workflow '{workflow.get('name', workflow_id)}' {trigger_source} "
                f"fire skipped — a run is already in flight",
            )
        except Exception:
            pass
        return None

    try:
        exec_id = db.create_workflow_execution(workflow_id, trigger_source, executed_by)
    except Exception:
        _release_inflight(workflow_id)
        raise

    def _run():
        start_time = time.time()
        try:
            canvas = json.loads(workflow["canvas_json"])
            graph = parse_canvas(canvas)
            servers = get_servers()
            server_map = {s.name: s for s in servers}

            db.update_workflow_execution(exec_id, status="running")
            logger.info("Workflow '%s' (id=%d) execution %d started",
                        workflow["name"], workflow_id, exec_id)

            # Execute the graph. Pass workflow context for variable
            # substitution: ``{{workflow.name}}`` / ``{{workflow.id}}``
            # in notification fields resolve to the values here.
            _execute_graph(
                db, exec_id, graph, server_map, settings,
                workflow_info={
                    "id": workflow_id,
                    "name": workflow.get("name", ""),
                    "trigger": trigger_source,
                    "executed_by": executed_by,
                },
                dry_run=dry_run,
            )

            # Check results
            steps = db.get_workflow_steps(exec_id)
            failed = [s for s in steps if s["status"] == "failed"]

            status = "failed" if failed else "completed"
            summary = f"{len(steps)} steps, {len(failed)} failed"

        except Exception as e:
            status = "failed"
            summary = f"Engine error: {str(e)}"
            logger.exception("Workflow %d execution failed", workflow_id)

        # Feed the outcome to the circuit breaker (feature 2.6) — but ONLY for
        # real auto-fires: a manual failure must not open it (manual is an
        # override) and a dry-run must not reset it (it would re-arm a broken
        # remediation).
        if _should_record_breaker(trigger_source, dry_run):
            _breaker_record(workflow_id, status == "completed", settings)

        duration = int((time.time() - start_time) * 1000)
        db.update_workflow_execution(
            exec_id,
            status=status,
            summary=summary,
            completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_ms=duration,
        )
        db.log_audit(executed_by, f"workflow_{status}", "workflow",
                     f"Workflow '{workflow['name']}': {summary}")
        logger.info("Workflow '%s' execution %d finished: %s (%dms)",
                     workflow["name"], exec_id, status, duration)

    def _run_guarded():
        try:
            _run()
        finally:
            _release_inflight(workflow_id)

    thread = threading.Thread(target=_run_guarded, daemon=True)
    thread.start()
    return exec_id


# ---------------------------------------------------------------------------
# Scheduled trigger loop
# ---------------------------------------------------------------------------

def _should_trigger_scheduled(trigger_config, tz_name, workflow_id=0):
    """Determine whether a scheduled workflow should fire right now.

    Reuses the same time-window + file-marker approach from restart_scheduler.

    trigger_config keys:
        schedule: "daily" | "weekly" | "monthly"
        time: "HH:MM"
        day_of_week: 0-6 (Mon-Sun) -- for weekly
        day_of_month: 1-31          -- for monthly

    Returns True if the workflow should run now and hasn't already run
    in this schedule period.
    """
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid timezone '%s', falling back to UTC", tz_name)
        tz = timezone.utc

    now = datetime.now(tz)
    sched_time_str = trigger_config.get("time", "03:00")
    try:
        sched_hour, sched_minute = map(int, sched_time_str.split(":"))
    except (ValueError, AttributeError):
        logger.warning("Invalid schedule time '%s'", sched_time_str)
        return False

    # Only trigger within a 2-minute window of the scheduled time
    if now.hour != sched_hour or abs(now.minute - sched_minute) > 1:
        return False

    sched_type = trigger_config.get("schedule", "daily")

    if sched_type == "weekly":
        target_day = trigger_config.get("day_of_week", 0)
        if isinstance(target_day, str):
            target_day = int(target_day)
        if now.weekday() != target_day:
            return False
    elif sched_type == "monthly":
        target_dom = trigger_config.get("day_of_month", 1)
        if isinstance(target_dom, str):
            target_dom = int(target_dom)
        if now.day != target_dom:
            return False
    # "daily" fires every day

    # Marker check only — do NOT write here (S2-5 / P4 from AUDIT-2026-05).
    # Old behaviour wrote the marker inside this should-trigger function,
    # which meant a workflow whose execute thread crashed before doing real
    # work was permanently skipped until the next period. The caller now
    # writes the marker after kicking off execute_workflow (which spawns
    # its own thread that returns immediately).
    marker_key = f"wf_{workflow_id}_{sched_type}"
    if _already_ran_marker(marker_key, now, sched_type):
        return False

    return True


def _mark_workflow_started(workflow_id: int, sched_type: str, now=None):
    """Called by the scheduler loop right after kicking off execute_workflow.
    Splitting the marker-write from the trigger-check is what S2-5 fixed."""
    if now is None:
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
    marker_key = f"wf_{workflow_id}_{sched_type}"
    _write_run_marker(marker_key, now)


def _already_ran_marker(marker_key, now, sched_type):
    """Return True if the marker shows we already ran in this period."""
    marker_file = DATA_DIR / f"last_wf_{marker_key}.txt"
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
        return last_run.isocalendar()[:2] == now.isocalendar()[:2]
    elif sched_type == "monthly":
        return (last_run.year, last_run.month) == (now.year, now.month)
    return False


def _write_run_marker(marker_key, now):
    """Persist the current timestamp so we know we already ran."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker_file = DATA_DIR / f"last_wf_{marker_key}.txt"
        marker_file.write_text(now.isoformat())
    except OSError:
        logger.exception("Failed to write workflow run marker")


# Per-workflow event-trigger state.
#
#   key   = workflow id (int)
#   value = {
#     "last_value": bool | None  — what the condition evaluated to last time
#                                  (None = never checked → first check seeds
#                                  state without firing, so a condition that
#                                  was ALREADY true at create time doesn't
#                                  trigger an immediate workflow run)
#     "last_check_at": float     — unix ts of the last evaluation
#     "last_fire_at":  float     — unix ts of the last actual workflow fire
#                                  (used for debounce so an oscillating
#                                  condition doesn't fire 30× per minute)
#   }
#
# Module-scope dict — ephemeral on purpose. A Prism restart resets every
# trigger's last_value to None, which means the first evaluation after
# startup seeds without firing. Restarts should not trigger workflows.
_event_trigger_state: dict[int, dict] = {}


def _evaluate_event_trigger(trigger_cfg: dict, server_map: dict,
                             db, settings: dict) -> tuple[bool, str]:
    """Return ``(condition_met, message)`` for an event-trigger block.

    The "met" semantic differs per event_type — for ``service_stopped``
    "met" means the service is NOT running, for ``service_running`` it
    means the service IS running, etc. Each branch maps the event-type
    to one of the existing check executors (so the WinRM payload is
    exactly the same as what the operator would write in a Check block)
    and inverts the result where needed.

    For ``metric_threshold`` we DON'T hit WinRM — we read the in-memory
    metrics cache (``state.latest_by_server``) that the v2 collector
    already maintains. Per-poll WinRM cost would be silly for metrics
    Prism already collects on its own cadence.
    """
    event_type = (trigger_cfg.get("event_type") or "").strip()
    server_name = trigger_cfg.get("server", "")
    target = str(trigger_cfg.get("target", "")).strip()

    server = server_map.get(server_name) if server_name else None

    # Helpers — keep the branches readable
    def _via_check_service():
        return _exec_check_service(
            {"server": server_name, "service_name": target},
            server_map, db, settings)

    def _via_check_process():
        return _exec_check_process(
            {"server": server_name, "process_name": target},
            server_map, db, settings)

    def _via_check_port():
        # port can come through as a string from the canvas form
        try:
            port = int(target)
        except (TypeError, ValueError):
            return False, f"invalid port: {target!r}"
        return _exec_check_port(
            {"server": server_name, "port": port},
            server_map, db, settings)

    if event_type == "service_stopped":
        ok, msg = _via_check_service()
        return (not ok), msg
    if event_type == "service_running":
        ok, msg = _via_check_service()
        return bool(ok), msg
    if event_type == "process_not_running":
        ok, msg = _via_check_process()
        return (not ok), msg
    if event_type == "process_running":
        ok, msg = _via_check_process()
        return bool(ok), msg
    if event_type == "port_closed":
        ok, msg = _via_check_port()
        return (not ok), msg
    if event_type == "port_open":
        ok, msg = _via_check_port()
        return bool(ok), msg
    if event_type == "metric_threshold":
        # Read the live metrics cache — no WinRM round-trip; the v2
        # aggregator updates this every metrics cycle.
        if not server_name:
            return False, "no server"
        try:
            from state import latest_by_server
        except Exception:
            return False, "metrics state unavailable"
        latest = latest_by_server.get(server_name) or {}
        metric_key = (trigger_cfg.get("metric") or "cpu").strip().lower()
        # Map the canvas-friendly short names to the cache's column names
        column_map = {
            "cpu": "cpu_percent",
            "ram": "ram_percent",
            "disk_c": "disk_c_percent",
            "disk_d": "disk_d_percent",
        }
        col = column_map.get(metric_key, metric_key)
        val = latest.get(col)
        if val is None:
            return False, f"no metric {metric_key} for {server_name}"
        try:
            threshold = float(trigger_cfg.get("threshold", 90))
            current = float(val)
        except (TypeError, ValueError):
            return False, "non-numeric threshold or value"
        op = (trigger_cfg.get("operator") or ">=").strip()
        ops = {
            ">=": lambda a, b: a >= b,
            "<=": lambda a, b: a <= b,
            ">":  lambda a, b: a >  b,
            "<":  lambda a, b: a <  b,
            "==": lambda a, b: a == b,
        }
        cmp = ops.get(op, ops[">="])
        return cmp(current, threshold), f"{metric_key}={current} {op} {threshold}"

    return False, f"unknown event_type: {event_type!r}"


def workflow_scheduler_loop(get_settings, db, get_servers):
    """Daemon thread that checks for scheduled workflows to run.

    Args:
        get_settings: Callable returning the current settings dict.
        db: Database instance.
        get_servers: Callable returning list of ServerConfig objects.
    """
    logger.info("Workflow scheduler started")
    while True:
        try:
            settings = get_settings()
            workflows = db.get_workflows(include_templates=False)
            for wf in workflows:
                if not wf.get("enabled"):
                    continue

                # ── Event-triggered workflows ──
                # Edge-triggered: fire only on False → True transitions
                # (a chronically-stopped service shouldn't re-fire every
                # poll). Per-workflow poll cadence taken from the trigger
                # config's ``poll_seconds`` field, with a hard floor of
                # SCHEDULER_INTERVAL since we can't check more often than
                # the loop ticks anyway.
                if wf.get("trigger_type") == "event":
                    try:
                        trigger_cfg = json.loads(wf.get("trigger_config", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    poll_seconds = max(
                        SCHEDULER_INTERVAL,
                        int(trigger_cfg.get("poll_seconds", 60) or 60),
                    )
                    st = _event_trigger_state.get(wf["id"], {})
                    now_ts = time.time()
                    if now_ts - st.get("last_check_at", 0) < poll_seconds:
                        continue
                    server_map = {s.name: s for s in get_servers()}
                    try:
                        met, msg = _evaluate_event_trigger(
                            trigger_cfg, server_map, db, settings
                        )
                    except Exception:
                        logger.warning(
                            "Event trigger evaluation failed for wf %d",
                            wf["id"], exc_info=True,
                        )
                        # Still bump last_check_at so we don't hot-loop
                        # on a broken config — operator fixes the trigger
                        # then it picks up next poll cycle.
                        _event_trigger_state[wf["id"]] = {
                            **st, "last_check_at": now_ts,
                        }
                        continue

                    prev_value = st.get("last_value")
                    fire = (met and prev_value is False)
                    _event_trigger_state[wf["id"]] = {
                        "last_value": bool(met),
                        "last_check_at": now_ts,
                        "last_fire_at": st.get("last_fire_at"),
                    }
                    if fire:
                        # Debounce: after a fire, suppress further fires
                        # for 5× poll_seconds. Stops an oscillating
                        # condition from creating a workflow storm.
                        last_fire = st.get("last_fire_at") or 0
                        if now_ts - last_fire < poll_seconds * 5:
                            continue
                        _event_trigger_state[wf["id"]]["last_fire_at"] = now_ts
                        logger.info(
                            "Event-triggered workflow '%s' (id=%d) fired: %s",
                            wf["name"], wf["id"], msg,
                        )
                        try:
                            execute_workflow(
                                db, wf["id"], get_servers, settings,
                                executed_by="event_trigger",
                                trigger_source="event",
                            )
                        except Exception:
                            logger.exception(
                                "Failed to execute event-triggered workflow '%s'",
                                wf["name"],
                            )
                    continue

                if wf.get("trigger_type") != "scheduled":
                    continue
                try:
                    trigger_cfg = json.loads(wf.get("trigger_config", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                tz_name = settings.get("timezone", "Europe/Berlin")
                if _should_trigger_scheduled(trigger_cfg, tz_name, workflow_id=wf["id"]):
                    logger.info("Scheduled workflow '%s' (id=%d) triggered", wf["name"], wf["id"])
                    # S2-7 (P6): execute_workflow spawns its own internal
                    # daemon thread and returns immediately, so this loop
                    # iteration is already non-blocking even for long
                    # workflows. The scheduler tick continues firing every
                    # SCHEDULER_INTERVAL seconds.
                    sched_type = trigger_cfg.get("schedule", "daily")
                    # S3-5 (B8): stamp the workflow's owner / last-modified-by
                    # in the executed_by field instead of the literal string
                    # "scheduler". An insider who plants a workflow as a
                    # scheduled time-bomb and later leaves the company would
                    # otherwise show up only as "scheduler" in audit_log; with
                    # this change the audit row preserves provenance even when
                    # the trigger is automated.
                    owner = (wf.get("last_modified_by")
                             or wf.get("created_by")
                             or "scheduler")
                    actor_label = (f"scheduler:{owner}" if owner not in (None, "", "scheduler")
                                   else "scheduler")
                    try:
                        execute_workflow(db, wf["id"], get_servers, settings,
                                         executed_by=actor_label, trigger_source="scheduled")
                        # S2-5 (P4): write the marker AFTER successful kickoff.
                        # If execute_workflow itself raises (validation, missing
                        # workflow, etc.) we skip the marker so the next tick
                        # within the 2-min window retries.
                        _mark_workflow_started(wf["id"], sched_type)
                    except Exception:
                        logger.exception("Failed to execute scheduled workflow '%s'", wf["name"])
        except Exception:
            logger.exception("Workflow scheduler error")
        global _last_heartbeat
        _last_heartbeat = time.time()
        time.sleep(SCHEDULER_INTERVAL)


# ---------------------------------------------------------------------------
# Built-in workflow templates with valid Drawflow canvas JSON
# ---------------------------------------------------------------------------

def _make_drawflow(nodes_spec):
    """Build a valid Drawflow JSON structure from a simplified node spec.

    nodes_spec: list of dicts with keys:
        id, name, label, data, x, y,
        success_to (list of node ids), fail_to (list of node ids),
        inputs_from (list of node ids)
    """
    data = {}
    for ns in nodes_spec:
        nid = ns["id"]
        # Build inputs
        inputs = {}
        inputs_from = ns.get("inputs_from", [])
        if inputs_from:
            connections = []
            for src_id in inputs_from:
                # Determine which output of the source connects here
                src_spec = next((n for n in nodes_spec if n["id"] == src_id), None)
                if src_spec:
                    if nid in src_spec.get("success_to", []):
                        connections.append({"node": str(src_id), "output": "output_1"})
                    elif nid in src_spec.get("fail_to", []):
                        connections.append({"node": str(src_id), "output": "output_2"})
                    else:
                        connections.append({"node": str(src_id), "output": "output_1"})
            inputs["input_1"] = {"connections": connections}

        # Build outputs
        outputs = {}
        success_to = ns.get("success_to", [])
        fail_to = ns.get("fail_to", [])
        out1_conns = [{"node": str(t), "input": "input_1"} for t in success_to]
        out2_conns = [{"node": str(t), "input": "input_1"} for t in fail_to]
        outputs["output_1"] = {"connections": out1_conns}
        outputs["output_2"] = {"connections": out2_conns}

        node_data = ns.get("data", {})
        node_data["label"] = ns.get("label", ns["name"])

        data[str(nid)] = {
            "id": nid,
            "name": ns["name"],
            "data": node_data,
            "class": ns["name"],
            "html": ns.get("label", ns["name"]),
            "typenode": False,
            "inputs": inputs,
            "outputs": outputs,
            "pos_x": ns.get("x", 100),
            "pos_y": ns.get("y", 100),
        }

    return json.dumps({"drawflow": {"Home": {"data": data}}})


# -- Template 1: Service Recovery --
_TEMPLATE_SERVICE_RECOVERY = _make_drawflow([
    {"id": 1, "name": "check_service", "label": "Check Service Status",
     "data": {"server": "", "service": "Spooler"},
     "x": 80, "y": 200,
     "success_to": [4], "fail_to": [2]},
    {"id": 2, "name": "restart_service", "label": "Restart Service",
     "data": {"server": "", "service": "Spooler"},
     "x": 350, "y": 300,
     "success_to": [3], "fail_to": [5],
     "inputs_from": [1]},
    {"id": 3, "name": "wait", "label": "Wait 10s",
     "data": {"duration": 10},
     "x": 620, "y": 300,
     "success_to": [6], "fail_to": [],
     "inputs_from": [2]},
    {"id": 4, "name": "log_event", "label": "Log Service OK",
     "data": {"server": "", "message": "Service is running normally", "severity": "info"},
     "x": 350, "y": 80,
     "success_to": [], "fail_to": [],
     "inputs_from": [1]},
    {"id": 5, "name": "send_webhook", "label": "Notify Restart Failed",
     "data": {"message": "Service restart failed - manual intervention needed"},
     "x": 620, "y": 450,
     "success_to": [], "fail_to": [],
     "inputs_from": [2]},
    {"id": 6, "name": "check_service", "label": "Verify Service Recovered",
     "data": {"server": "", "service": "Spooler"},
     "x": 890, "y": 300,
     "success_to": [7], "fail_to": [5],
     "inputs_from": [3]},
    {"id": 7, "name": "log_event", "label": "Log Recovery Success",
     "data": {"server": "", "message": "Service successfully recovered after restart", "severity": "info"},
     "x": 1160, "y": 300,
     "success_to": [], "fail_to": [],
     "inputs_from": [6]},
])

# -- Template 2: Port Health Monitor --
_TEMPLATE_PORT_MONITOR = _make_drawflow([
    {"id": 1, "name": "check_port", "label": "Check Primary Port",
     "data": {"server": "", "port": 443, "protocol": "tcp"},
     "x": 80, "y": 200,
     "success_to": [2], "fail_to": [3]},
    {"id": 2, "name": "check_url", "label": "Verify HTTP Response",
     "data": {"url": "https://localhost/health", "expected_status": 200},
     "x": 350, "y": 100,
     "success_to": [5], "fail_to": [4],
     "inputs_from": [1]},
    {"id": 3, "name": "restart_service", "label": "Restart Web Service",
     "data": {"server": "", "service": "W3SVC"},
     "x": 350, "y": 350,
     "success_to": [6], "fail_to": [7],
     "inputs_from": [1]},
    {"id": 4, "name": "send_webhook", "label": "Notify HTTP Failure",
     "data": {"message": "Port open but HTTP health check failed"},
     "x": 620, "y": 100,
     "success_to": [], "fail_to": [],
     "inputs_from": [2]},
    {"id": 5, "name": "log_event", "label": "Log All Healthy",
     "data": {"server": "", "message": "Port and HTTP checks passed", "severity": "info"},
     "x": 620, "y": 10,
     "success_to": [], "fail_to": [],
     "inputs_from": [2]},
    {"id": 6, "name": "wait", "label": "Wait 15s",
     "data": {"duration": 15},
     "x": 620, "y": 300,
     "success_to": [8], "fail_to": [],
     "inputs_from": [3]},
    {"id": 7, "name": "send_email", "label": "Escalate via Email",
     "data": {"message": "Web service restart failed - immediate attention needed"},
     "x": 620, "y": 450,
     "success_to": [], "fail_to": [],
     "inputs_from": [3]},
    {"id": 8, "name": "check_port", "label": "Verify Port After Restart",
     "data": {"server": "", "port": 443, "protocol": "tcp"},
     "x": 890, "y": 300,
     "success_to": [], "fail_to": [7],
     "inputs_from": [6]},
])

# -- Template 3: Server Restart with Validation --
_TEMPLATE_SERVER_RESTART = _make_drawflow([
    {"id": 1, "name": "log_event", "label": "Log Restart Start",
     "data": {"server": "", "message": "Scheduled server restart initiated", "severity": "info"},
     "x": 80, "y": 200,
     "success_to": [2], "fail_to": []},
    {"id": 2, "name": "stop_service", "label": "Stop App Service",
     "data": {"server": "", "service": "MyAppService"},
     "x": 350, "y": 200,
     "success_to": [3], "fail_to": [3],
     "inputs_from": [1]},
    {"id": 3, "name": "restart_server", "label": "Restart Server",
     "data": {"server": "", "force": True},
     "x": 620, "y": 200,
     "success_to": [4], "fail_to": [7],
     "inputs_from": [2]},
    {"id": 4, "name": "wait", "label": "Wait for Reboot (60s)",
     "data": {"duration": 60},
     "x": 890, "y": 200,
     "success_to": [5], "fail_to": [],
     "inputs_from": [3]},
    {"id": 5, "name": "check_port", "label": "Check RDP Port",
     "data": {"server": "", "port": 3389, "protocol": "tcp"},
     "x": 1160, "y": 200,
     "success_to": [6], "fail_to": [7],
     "inputs_from": [4]},
    {"id": 6, "name": "log_event", "label": "Log Restart Success",
     "data": {"server": "", "message": "Server restart completed successfully", "severity": "info"},
     "x": 1430, "y": 100,
     "success_to": [], "fail_to": [],
     "inputs_from": [5]},
    {"id": 7, "name": "send_email", "label": "Alert: Restart Problem",
     "data": {"message": "Server restart encountered issues - please verify manually"},
     "x": 1160, "y": 400,
     "success_to": [], "fail_to": [],
     "inputs_from": [3, 5]},
])

# -- Template 4: Process Watchdog --
_TEMPLATE_PROCESS_WATCHDOG = _make_drawflow([
    {"id": 1, "name": "check_process", "label": "Check Critical Process",
     "data": {"server": "", "process": "sqlservr"},
     "x": 80, "y": 200,
     "success_to": [2], "fail_to": [3]},
    {"id": 2, "name": "log_event", "label": "Log Process OK",
     "data": {"server": "", "message": "Critical process is running", "severity": "info"},
     "x": 350, "y": 80,
     "success_to": [], "fail_to": [],
     "inputs_from": [1]},
    {"id": 3, "name": "start_service", "label": "Start Related Service",
     "data": {"server": "", "service": "MSSQLSERVER"},
     "x": 350, "y": 320,
     "success_to": [4], "fail_to": [6],
     "inputs_from": [1]},
    {"id": 4, "name": "wait", "label": "Wait 10s",
     "data": {"duration": 10},
     "x": 620, "y": 320,
     "success_to": [5], "fail_to": [],
     "inputs_from": [3]},
    {"id": 5, "name": "check_process", "label": "Verify Process Running",
     "data": {"server": "", "process": "sqlservr"},
     "x": 890, "y": 320,
     "success_to": [7], "fail_to": [6],
     "inputs_from": [4]},
    {"id": 6, "name": "send_email", "label": "Escalate - Process Down",
     "data": {"message": "Critical process could not be recovered - manual action required"},
     "x": 890, "y": 500,
     "success_to": [], "fail_to": [],
     "inputs_from": [3, 5]},
    {"id": 7, "name": "send_webhook", "label": "Notify Recovery OK",
     "data": {"message": "Critical process recovered after service restart"},
     "x": 1160, "y": 320,
     "success_to": [], "fail_to": [],
     "inputs_from": [5]},
])


BUILTIN_TEMPLATES = [
    {
        "name": "Service Recovery",
        "description": "Check if a service is running, restart if down, verify recovery, and notify on failure",
        "canvas_json": _TEMPLATE_SERVICE_RECOVERY,
        "is_template": True,
    },
    {
        "name": "Port Health Monitor",
        "description": "Check port availability and HTTP health, restart web service if down, escalate on failure",
        "canvas_json": _TEMPLATE_PORT_MONITOR,
        "is_template": True,
    },
    {
        "name": "Server Restart with Validation",
        "description": "Gracefully stop services, restart server, wait for reboot, verify connectivity",
        "canvas_json": _TEMPLATE_SERVER_RESTART,
        "is_template": True,
    },
    {
        "name": "Process Watchdog",
        "description": "Monitor a critical process, attempt service restart if missing, escalate if recovery fails",
        "canvas_json": _TEMPLATE_PROCESS_WATCHDOG,
        "is_template": True,
    },
]


def seed_workflow_templates(db):
    """Insert built-in workflow templates if they don't exist."""
    # Check which templates already exist (by name + is_template)
    existing = db.get_workflows(include_templates=True)
    existing_names = {w["name"] for w in existing if w.get("is_template")}

    seeded = 0
    for tmpl in BUILTIN_TEMPLATES:
        if tmpl["name"] in existing_names:
            continue  # Already exists, skip
        try:
            db.create_workflow(
                name=tmpl["name"],
                description=tmpl["description"],
                category_id=None,
                trigger_type="manual",
                trigger_config="{}",
                canvas_json=tmpl["canvas_json"],
                created_by="system",
                is_template=True,
            )
            seeded += 1
        except Exception:
            pass
    if seeded:
        logger.info("Seeded %d new workflow templates", seeded)
    else:
        logger.debug("All workflow templates already exist")
