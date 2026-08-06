"""TLS certificate monitoring — periodic expiry checks and alert dispatch.

Polls every configured certificate at the cadence set by
``tls_monitoring.check_interval_cycles`` (translated to seconds by the
v2 periodics thread; see ``collector_v2/periodics.py``). Stores results
in the ``tls_certificates`` table and fires expiry alerts via email +
Teams webhook when a certificate crosses the warning or critical
day-threshold.

Why this is its own module:
  * The v2 periodics thread calls ``_check_tls_certificates`` directly
    (one of the 8 jobs in ``collector_v2/periodics.py``). Keeping the
    function in ``collector.py`` would force periodics to drag the
    legacy collector module in at import time — exactly the cross-
    coupling we're removing in R1.
  * The actual cert inspection (TCP connect + parse X.509) lives in
    ``tls_checker.py``. This module orchestrates the scheduling /
    storage / alerting *around* that primitive.

Module dependencies:
  * ``tls_checker.check_certificate`` (TCP probe + cert parse)
  * ``email_alerts`` (send_alert_email / should_send_email)
  * ``webhooks`` (send_teams_webhook)
  * ``database`` (upsert_tls_certificate + insert_event)

Nothing in ``collector_v2/`` imports from here at module load — the
periodics thread late-imports per job. Safe to add features here without
touching the supervisor / aggregator / worker pool.
"""

from __future__ import annotations

import logging

from email_alerts import send_alert_email, should_send_email

logger = logging.getLogger("prism.tls_monitor")


def _check_tls_certificates(db, settings: dict) -> None:
    """Check all configured TLS certificates and update DB.

    Called from ``collector_v2/periodics.py`` at the cadence resolved
    from ``tls_monitoring.check_interval_cycles`` (default 1h under v2).
    No-op when ``tls_monitoring.enabled`` is False.

    Fires up to one event per cert per call — the suppression of
    repeated alerts within the warning window is handled by the
    event/alert layer downstream (suppression_hours + ack/snooze).
    """
    from tls_checker import check_certificate

    tls_cfg = settings.get("tls_monitoring", {})
    if not tls_cfg.get("enabled", False):
        return

    certs_to_check = tls_cfg.get("certificates", [])
    warning_days = tls_cfg.get("warning_days", 30)
    critical_days = tls_cfg.get("critical_days", 7)

    for cert_cfg in certs_to_check:
        host = cert_cfg.get("host", "")
        port = cert_cfg.get("port", 443)
        server_name = cert_cfg.get("server_name", host)
        if not host:
            continue

        result = check_certificate(host, port, timeout=10, expiry_threshold=warning_days)

        db.upsert_tls_certificate(
            server_name=server_name,
            host=host,
            port=port,
            subject=result.get("subject", ""),
            issuer=result.get("issuer", ""),
            not_before=result.get("not_before", ""),
            not_after=result.get("not_after", ""),
            days_remaining=result.get("days_remaining", -1),
            status=result.get("status", "error"),
            error=result.get("error"),
        )

        # Fire alerts on expiring/expired certs
        days = result.get("days_remaining", -1)
        if days >= 0 and days <= critical_days:
            db.insert_event(
                server_name, "critical", "tls_certificate", days, critical_days,
                f"TLS certificate for {host}:{port} expires in {days} days",
            )
            _send_cert_alert(settings, server_name, host, port, days, "critical")
        elif days >= 0 and days <= warning_days:
            db.insert_event(
                server_name, "warning", "tls_certificate", days, warning_days,
                f"TLS certificate for {host}:{port} expires in {days} days",
            )
            _send_cert_alert(settings, server_name, host, port, days, "warning")

    logger.info("TLS certificate check completed for %d endpoints", len(certs_to_check))


def _send_cert_alert(settings: dict, server_name: str, host: str, port: int,
                     days: int, severity: str) -> None:
    """Send email and Teams webhook alerts for TLS certificate expiry.

    The event dict mirrors the shape the rest of Prism's alerting code
    expects (event_type / metric / value / threshold / message) so the
    same downstream formatters render TLS alerts the same way they
    render anomaly / threshold alerts.
    """
    msg = f"TLS certificate for {host}:{port} expires in {days} days"
    event = {
        "event_type": severity,
        "metric": "tls_certificate",
        "value": days,
        "threshold": settings.get("tls_monitoring", {}).get(
            "critical_days" if severity == "critical" else "warning_days",
            7 if severity == "critical" else 30,
        ),
        "message": msg,
    }

    # Email alert
    if should_send_email(severity, settings):
        try:
            send_alert_email(event, server_name, settings)
            logger.info("[%s] TLS cert alert email sent (%s)", server_name, severity)
        except Exception:
            logger.exception("[%s] Failed to send TLS cert alert email", server_name)

    # Teams webhook alert — separate try/except so a webhook failure
    # doesn't break the email path or vice versa.
    try:
        webhook_cfg = settings.get("webhooks", {})
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
                    server_name, severity, "tls_certificate",
                    days, event["threshold"], msg, settings,
                )
                logger.info("[%s] TLS cert Teams webhook sent (%s)", server_name, severity)
    except Exception:
        logger.warning("[%s] TLS cert Teams webhook failed", server_name)
