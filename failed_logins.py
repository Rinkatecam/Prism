"""Failed-login + account-lockout collection from Windows Security log.

Polls every monitored server at the v2 periodics cadence (default 5
min) and:

  1. Pulls events 4625 (failed logon) + 4740 (account locked out) from
     the Security log in the last 15 minutes.
  2. Inserts each event row into ``failed_logins`` for the UI.
  3. Fires a ``critical`` event for any 4740 (lockout immediately
     alerts).
  4. Counts 4625 events in the trailing 15 minutes; fires a
     ``warning`` or ``critical`` ``failed_logins`` event when the count
     crosses ``security_alerts.login_failure_threshold``
     (``critical`` at 2× the threshold).

Maintenance windows with ``suppress_alerts=True`` SKIP this collection
entirely — both the WinRM round-trip and the alert dispatch. This is
intentional: during planned work the operator typically expects login
storms (e.g. service-account reconfiguration), and we'd rather see no
data than a wall of false alarms.

The PowerShell script (``PS_COLLECT_FAILED_LOGINS``) does the heavy
lifting — it parses 4625 XML payloads, enriches with calling-process
or LogonProcessName fallbacks, and returns a JSON array. It lives here
(rather than in ``collector.py``) because this is its only caller and
keeping them together makes the contract obvious.
"""

from __future__ import annotations

import json as _json
import logging

from alert_scoring import update_score_on_fire
from collector_v2.scripts import PS_COLLECT_FAILED_LOGINS
from email_alerts import send_alert_email, should_send_email
from maintenance import _is_alert_suppressed_by_maintenance

logger = logging.getLogger("prism.failed_logins")

# PS_COLLECT_FAILED_LOGINS lives in collector_v2/scripts.py — the single
# source of truth for all PowerShell payloads. Pre-retirement this file
# carried its own copy guarded by a parity test; with v1 gone we collapsed
# the duplication and import the script directly. Re-exported so any
# straggler doing ``from failed_logins import PS_COLLECT_FAILED_LOGINS``
# keeps working.
__all__ = ("_collect_all_failed_logins", "PS_COLLECT_FAILED_LOGINS")


def _collect_all_failed_logins(db, servers, settings: dict) -> None:
    """Collect failed login + lockout events from all servers via WinRM.

    Per server (each in its own try/except so one bad server doesn't
    block the fleet):
      1. Skip if a ``suppress_alerts=True`` maintenance window is active.
      2. Open a WinRM RunspacePool and run PS_COLLECT_FAILED_LOGINS.
      3. Parse the JSON result; insert all events into ``failed_logins``.
      4. For each 4740 (lockout): fire a ``critical`` event +
         email + Teams webhook (independently try/excepted).
      5. Count 4625 events in the last 15 min; if ≥ threshold, fire a
         ``warning`` (or ``critical`` at 2× threshold) ``failed_logins``
         event + email + webhook + alert-score bump.
    """
    from pypsrp.powershell import PowerShell, RunspacePool

    sec_cfg = settings.get("security_alerts", {})
    threshold = sec_cfg.get("login_failure_threshold", 10)

    for server in servers:
        # MAINTENANCE GATE: skip failed-login alerting entirely when in a
        # suppress_alerts window. Collection is also skipped to save WinRM time.
        if _is_alert_suppressed_by_maintenance(server.name, settings):
            continue
        try:
            from winrm_factory import make_wsman
            wsman = make_wsman(server, connection_timeout=15, read_timeout=15)
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(PS_COLLECT_FAILED_LOGINS)
                output = ps.invoke()
                if ps.had_errors:
                    continue
                stdout = str(output[0]) if output else "[]"
                if not stdout.strip():
                    continue
                data = _json.loads(stdout)
                if isinstance(data, dict):
                    data = [data]
                if data:
                    db.insert_failed_logins(server.name, data)

                    # Account lockout detection (Event ID 4740) — fires
                    # critical immediately, never throttled, never gated.
                    if sec_cfg.get("lockout_alert", True):
                        lockouts = [e for e in data if str(e.get("event_id")) == "4740"]
                        for lk in lockouts:
                            acct = lk.get("account_name", "?")
                            msg = f"Account lockout: '{acct}' was locked out"
                            try:
                                db.insert_event(
                                    server.name, "critical", "account_lockout",
                                    None, None, msg,
                                )
                            except Exception:
                                logger.debug("[%s] lockout event insert failed", server.name, exc_info=True)
                                continue
                            # Email
                            try:
                                if should_send_email("critical", settings):
                                    event = {
                                        "event_type": "critical",
                                        "metric": "account_lockout",
                                        "value": acct,
                                        "threshold": None,
                                        "message": msg,
                                    }
                                    send_alert_email(event, server.name, settings)
                            except Exception:
                                logger.debug("[%s] lockout email failed", server.name, exc_info=True)
                            # Webhook
                            try:
                                webhook_cfg = settings.get("webhooks", {}) or {}
                                if (webhook_cfg.get("enabled")
                                        and webhook_cfg.get("teams_webhook_url")
                                        and webhook_cfg.get("send_on_critical", True)):
                                    from webhooks import send_teams_webhook
                                    send_teams_webhook(
                                        webhook_cfg["teams_webhook_url"],
                                        server.name, "critical", "account_lockout",
                                        None, None, msg, settings,
                                    )
                            except Exception:
                                logger.debug("[%s] lockout webhook failed", server.name, exc_info=True)
                            logger.warning("[%s] Account lockout: %s", server.name, acct)

                    # Spike detection on 4625 events — count last 15 min
                    count = db.get_failed_login_count(server.name, minutes=15)
                    if count >= threshold:
                        # Severity: critical if 2x threshold, otherwise warning
                        severity = "critical" if count >= 2 * threshold else "warning"
                        msg = f"{count} failed login attempts in 15 minutes (threshold: {threshold})"
                        db.insert_event(
                            server.name, severity, "failed_logins",
                            count, threshold, msg,
                        )
                        try:
                            update_score_on_fire(db, server.name, "failed_logins", severity)
                        except Exception:
                            logger.debug("Alert scoring failed for %s", server.name, exc_info=True)
                        logger.warning("[%s] Failed login spike: %d in 15min (%s)",
                                       server.name, count, severity)

                        # Email notification
                        try:
                            if should_send_email(severity, settings):
                                event = {
                                    "event_type": severity,
                                    "metric": "failed_logins",
                                    "value": count,
                                    "threshold": threshold,
                                    "message": msg,
                                }
                                send_alert_email(event, server.name, settings)
                                logger.info("[%s] Failed login email sent (%s)", server.name, severity)
                        except Exception:
                            logger.debug("[%s] Failed login email failed", server.name, exc_info=True)

                        # Teams webhook
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
                                        server.name, severity, "failed_logins",
                                        count, threshold, msg, settings,
                                    )
                                    logger.info("[%s] Failed login webhook sent (%s)", server.name, severity)
                        except Exception:
                            logger.debug("[%s] Failed login webhook failed", server.name, exc_info=True)
        except Exception:
            logger.debug("[%s] Failed login collection skipped", server.name)
