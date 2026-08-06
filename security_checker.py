"""Security monitoring for Prism — Defender, Firewall, BitLocker, ports, users.

WIRING:
    Settings:    config_manager.py → _DEFAULT_SETTINGS["security_alerts"]
                 (defender_check, firewall_check, bitlocker_check,
                  track_local_users, track_open_ports, defender_max_sig_age_days)
    UI toggles:  templates/monitoring.html  "Security Alerts" sub-section
    Saved via:   templates/monitoring.html  saveMonitoringSettings()
    Called by:   collector.py main loop, every 30 cycles (~30 min)
                 → if settings["security_alerts"]["security_status_check"]:
                      collect_security_status(db, server, settings)
    Storage:     database.py  server_security_status table
                 (upsert_security_status / get_security_status)
    Display:     templates/server_detail.html  loadSecurityStatus() JS
                 → GET /api/servers/<name>/security-status (routes/api.py)
    Manual run:  POST /api/servers/<name>/security-status/check
    Alerts:      db.insert_event(severity, "security_status", ...) +
                 email_alerts.send_alert_email + webhooks.send_teams_webhook
                 (severity is "critical" or "warning" → passes should_send_email
                 allowlist in email_alerts.py — do NOT use other strings here).

DO NOT duplicate this PowerShell logic in collector.py — all security checks
go through this module so toggles + dispatch stay centralized.
"""

import json
import logging

logger = logging.getLogger("prism.security")

# Reusable PowerShell fragments — assembled per-server based on settings
_PS_DEFENDER = r"""
try {
    $mp = Get-MpComputerStatus -ErrorAction Stop
    $sigDate = $mp.AntivirusSignatureLastUpdated
    $sigAge = if ($sigDate) { [int]((Get-Date) - $sigDate).TotalDays } else { 999 }
    $result.defender = @{
        enabled = $mp.AntivirusEnabled
        rt_protection = $mp.RealTimeProtectionEnabled
        sig_age_days = $sigAge
        engine_version = $mp.AMEngineVersion
    }
} catch {
    $result.defender = @{ enabled = $false; rt_protection = $false; sig_age_days = -1; engine_version = "unknown" }
}
"""

_PS_FIREWALL = r"""
try {
    $svc = Get-Service MpsSvc -ErrorAction Stop
    $profiles = Get-NetFirewallProfile -ErrorAction Stop
    $result.firewall = @{
        service_running = ($svc.Status -eq 'Running')
        domain_enabled = ($profiles | Where-Object Name -eq 'Domain').Enabled
        private_enabled = ($profiles | Where-Object Name -eq 'Private').Enabled
        public_enabled = ($profiles | Where-Object Name -eq 'Public').Enabled
    }
} catch {
    $result.firewall = @{ service_running = $false; domain_enabled = $false; private_enabled = $false; public_enabled = $false }
}
"""

_PS_BITLOCKER = r"""
try {
    $bl = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop
    $result.bitlocker = @{
        encrypted_pct = [int]$bl.EncryptionPercentage
        status = "$($bl.VolumeStatus)"
    }
} catch {
    $result.bitlocker = @{ encrypted_pct = -1; status = "Unknown" }
}
"""

_PS_OPEN_PORTS = r"""
try {
    $ports = Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Select-Object LocalPort, @{N='Process';E={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}} |
        Sort-Object LocalPort -Unique
    $result.open_ports = @($ports | ForEach-Object { @{ port = $_.LocalPort; process = "$($_.Process)" } })
} catch {
    $result.open_ports = @()
}
"""

_PS_LOCAL_USERS = r"""
try {
    $users = Get-LocalUser -ErrorAction Stop | Where-Object { $_.Enabled } |
        Select-Object Name, @{N='LastLogon';E={if ($_.LastLogon) { $_.LastLogon.ToString('yyyy-MM-dd') } else { '' }}}
    $result.local_users = @($users | ForEach-Object { @{ name = $_.Name; last_logon = $_.LastLogon } })
} catch {
    $result.local_users = @()
}
"""


def _build_ps_script(sec_cfg: dict) -> str:
    """Assemble the PowerShell script with only the sections enabled in settings."""
    parts = ["$result = @{}\n"]
    if sec_cfg.get("defender_check", True):
        parts.append(_PS_DEFENDER)
    if sec_cfg.get("firewall_check", True):
        parts.append(_PS_FIREWALL)
    if sec_cfg.get("bitlocker_check", True):
        parts.append(_PS_BITLOCKER)
    if sec_cfg.get("track_open_ports", True):
        parts.append(_PS_OPEN_PORTS)
    if sec_cfg.get("track_local_users", True):
        parts.append(_PS_LOCAL_USERS)
    parts.append("\n$result | ConvertTo-Json -Depth 5 -Compress\n")
    return "\n".join(parts)


def collect_security_status(db, server, settings):
    """Run security check on a server via WinRM and update DB."""
    from pypsrp.wsman import WSMan
    from pypsrp.powershell import PowerShell, RunspacePool
    from crypto_utils import decrypt_password

    sec_cfg = settings.get("security_alerts", {})

    # Skip entirely if no checks are enabled
    if not any(sec_cfg.get(k, True) for k in (
        "defender_check", "firewall_check", "bitlocker_check",
        "track_open_ports", "track_local_users",
    )):
        return

    script = _build_ps_script(sec_cfg)

    try:
        from winrm_factory import make_wsman
        wsman = make_wsman(server, connection_timeout=15, read_timeout=30)
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(script)
            output = ps.invoke()
            if ps.had_errors:
                logger.debug("[%s] Security check had errors", server.name)
                return
            stdout = str(output[0]) if output else "{}"
            data = json.loads(stdout)
    except Exception as e:
        logger.debug("[%s] Security check failed: %s", server.name, e)
        return

    defender = data.get("defender", {}) or {}
    firewall = data.get("firewall", {}) or {}
    bitlocker = data.get("bitlocker", {}) or {}

    db.upsert_security_status(
        server_name=server.name,
        defender_enabled=1 if defender.get("enabled") else 0,
        defender_rt_protection=1 if defender.get("rt_protection") else 0,
        defender_sig_age_days=defender.get("sig_age_days", -1),
        defender_engine_version=defender.get("engine_version", ""),
        firewall_service_running=1 if firewall.get("service_running") else 0,
        firewall_domain_enabled=1 if firewall.get("domain_enabled") else 0,
        firewall_private_enabled=1 if firewall.get("private_enabled") else 0,
        firewall_public_enabled=1 if firewall.get("public_enabled") else 0,
        bitlocker_encrypted_pct=bitlocker.get("encrypted_pct", -1),
        bitlocker_status=bitlocker.get("status", "Unknown"),
        open_ports_json=json.dumps(data.get("open_ports", [])),
        local_users_json=json.dumps(data.get("local_users", [])),
    )

    # Generate alerts based on settings
    alerts = []
    if sec_cfg.get("defender_check") and defender.get("enabled"):
        if not defender.get("rt_protection"):
            alerts.append(("critical", "Windows Defender real-time protection is OFF"))
        sig_age = defender.get("sig_age_days", 0)
        max_age = sec_cfg.get("defender_max_sig_age_days", 7)
        if sig_age > max_age:
            alerts.append(("warning", f"Defender signatures are {sig_age} days old (max {max_age})"))

    if sec_cfg.get("firewall_check"):
        if not firewall.get("service_running"):
            alerts.append(("critical", "Windows Firewall service (MpsSvc) is NOT running"))
        for prof in ["domain", "private", "public"]:
            if not firewall.get(f"{prof}_enabled"):
                alerts.append(("warning", f"Firewall {prof.title()} profile is disabled"))

    if sec_cfg.get("bitlocker_check"):
        pct = bitlocker.get("encrypted_pct", -1)
        if pct >= 0 and pct < 100:
            alerts.append(("warning", f"BitLocker encryption: {pct}% (system drive not fully encrypted)"))

    # Insert alert events + dispatch notifications (email + webhook)
    for severity, msg in alerts:
        try:
            db.insert_event(server.name, severity, "security_status", None, None, msg)
        except Exception:
            logger.debug("Failed to insert security event for %s", server.name, exc_info=True)
            continue

        # Email notification
        try:
            from email_alerts import send_alert_email, should_send_email
            if should_send_email(severity, settings):
                event = {
                    "event_type": severity,
                    "metric": "security_status",
                    "value": None,
                    "threshold": None,
                    "message": msg,
                }
                send_alert_email(event, server.name, settings)
                logger.info("[%s] Security alert email sent (%s): %s", server.name, severity, msg)
        except Exception:
            logger.debug("[%s] Security alert email failed", server.name, exc_info=True)

        # Teams webhook notification
        try:
            webhook_cfg = settings.get("webhooks", {}) or {}
            if webhook_cfg.get("enabled") and webhook_cfg.get("teams_webhook_url"):
                should_send = False
                if severity == "critical" and webhook_cfg.get("send_on_critical", True):
                    should_send = True
                elif severity == "warning" and webhook_cfg.get("send_on_warning", False):
                    should_send = True
                if should_send:
                    from webhooks import send_teams_webhook
                    send_teams_webhook(
                        webhook_cfg["teams_webhook_url"],
                        server.name, severity, "security_status",
                        None, None, msg, settings,
                    )
                    logger.info("[%s] Security alert webhook sent (%s)", server.name, severity)
        except Exception:
            logger.debug("[%s] Security alert webhook failed", server.name, exc_info=True)
