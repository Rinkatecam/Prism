"""Email alert module for Prism. Sends HTML email notifications on server events."""

import smtplib
import logging
import zoneinfo
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger("prism.email")


def _format_email_timestamp(settings: dict) -> str:
    """Format current time using configured timezone and date/time format."""
    try:
        tz = zoneinfo.ZoneInfo(settings.get("timezone", "Europe/Berlin"))
        dt = datetime.now(timezone.utc).astimezone(tz)
        date_fmt = settings.get("date_format", "DD.MM.YYYY")
        if date_fmt == "DD.MM.YYYY":
            date_str = dt.strftime("%d.%m.%Y")
        elif date_fmt == "YYYY-MM-DD":
            date_str = dt.strftime("%Y-%m-%d")
        elif date_fmt == "MM/DD/YYYY":
            date_str = dt.strftime("%m/%d/%Y")
        else:
            date_str = dt.strftime("%d/%m/%Y")
        if settings.get("time_format", "24h") == "12h":
            time_str = dt.strftime("%I:%M %p")
        else:
            time_str = dt.strftime("%H:%M")
        return f"{date_str} {time_str}"
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def should_send_email(event_type: str, settings: dict) -> bool:
    """Check whether an email should be sent for the given event type.

    Args:
        event_type: One of 'critical', 'warning', 'resolved', 'offline', 'info'.
        settings: Full settings dict (must contain 'email' sub-dict).

    Returns:
        True if an email should be sent.
    """
    email_cfg = settings.get("email", {})
    if not email_cfg.get("enabled", False):
        return False
    if not email_cfg.get("recipients"):
        return False
    if not email_cfg.get("smtp_server"):
        return False

    if event_type == "info":
        return False
    if event_type in ("critical", "offline") and email_cfg.get("send_on_critical", True):
        return True
    if event_type == "warning" and email_cfg.get("send_on_warning", False):
        return True
    if event_type == "resolved" and email_cfg.get("send_on_critical", True):
        # Send resolved notifications if critical alerts are enabled
        return True

    return False


def send_alert_email(event: dict, server_name: str, settings: dict) -> bool:
    """Send an HTML alert email for a server event.

    Args:
        event: Dict with keys: event_type, metric, value, threshold, message.
        server_name: Name of the server.
        settings: Full settings dict (must contain 'email' sub-dict).

    Returns:
        True if the email was sent successfully.
    """
    email_cfg = settings.get("email", {})
    recipients = email_cfg.get("recipients", [])
    if not recipients:
        logger.warning("No email recipients configured")
        return False

    event_type = event.get("event_type", "info")
    message = event.get("message", "")
    metric = event.get("metric")
    value = event.get("value")
    threshold = event.get("threshold")
    dashboard_url = email_cfg.get("dashboard_url", "http://localhost:5000")
    timestamp = _format_email_timestamp(settings)

    # Color scheme per event type
    colors = {
        "critical": {"bg": "#DC2626", "label": "CRITICAL"},
        "offline":  {"bg": "#DC2626", "label": "OFFLINE"},
        "warning":  {"bg": "#F59E0B", "label": "WARNING"},
        "resolved": {"bg": "#10B981", "label": "RESOLVED"},
    }
    color = colors.get(event_type, {"bg": "#6B7280", "label": event_type.upper()})

    subject = f"[Prism] {color['label']}: {server_name}"

    # Build metric detail row
    metric_html = ""
    if metric and value is not None:
        metric_html = f"""
        <tr>
          <td style="padding:8px 16px;color:#6B7280;font-size:13px;">Metric</td>
          <td style="padding:8px 16px;font-size:13px;font-weight:600;">{metric.upper()} &mdash; {value}% (threshold: {threshold}%)</td>
        </tr>"""

    html_body = f"""\
<html>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F3F4F6;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <!-- Header -->
        <tr>
          <td style="background:{color['bg']};padding:20px 24px;">
            <span style="color:#FFFFFF;font-size:20px;font-weight:700;">Prism Monitor</span><br>
            <span style="color:rgba(255,255,255,0.9);font-size:14px;margin-top:4px;display:inline-block;">{color['label']}: {server_name}</span>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:24px;">
            <p style="margin:0 0 16px;font-size:14px;color:#374151;">{message}</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;border-radius:6px;border:1px solid #E5E7EB;">
              <tr>
                <td style="padding:8px 16px;color:#6B7280;font-size:13px;">Server</td>
                <td style="padding:8px 16px;font-size:13px;font-weight:600;">{server_name}</td>
              </tr>
              <tr>
                <td style="padding:8px 16px;color:#6B7280;font-size:13px;">Status</td>
                <td style="padding:8px 16px;font-size:13px;font-weight:600;color:{color['bg']};">{color['label']}</td>
              </tr>{metric_html}
              <tr>
                <td style="padding:8px 16px;color:#6B7280;font-size:13px;">Time</td>
                <td style="padding:8px 16px;font-size:13px;">{timestamp}</td>
              </tr>
            </table>
            <p style="margin:20px 0 0;text-align:center;">
              <a href="{dashboard_url}" style="display:inline-block;padding:10px 24px;background:#2563EB;color:#FFFFFF;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;">View Dashboard</a>
            </p>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:16px 24px;border-top:1px solid #E5E7EB;text-align:center;">
            <span style="font-size:11px;color:#9CA3AF;">Sent by Prism Server Monitor</span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_address", "prism@localhost")
    msg["To"] = ", ".join(recipients)

    # Plain text fallback
    plain = f"{color['label']}: {server_name}\n\n{message}\n\nTime: {timestamp}\nDashboard: {dashboard_url}"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        smtp_server = email_cfg.get("smtp_server", "")
        smtp_port = int(email_cfg.get("smtp_port", 587))
        use_tls = email_cfg.get("use_tls", True)
        username = email_cfg.get("username", "")
        password = email_cfg.get("password", "")

        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)

        if username and password:
            server.login(username, password)

        server.sendmail(msg["From"], recipients, msg.as_string())
        server.quit()
        logger.info("Alert email sent to %s for %s [%s]", ", ".join(recipients), server_name, event_type)
        return True

    except Exception:
        logger.exception("Failed to send alert email for %s [%s]", server_name, event_type)
        return False


def send_test_email(settings: dict) -> tuple[bool, str]:
    """Send a test email to verify SMTP configuration.

    Args:
        settings: Full settings dict (must contain 'email' sub-dict).

    Returns:
        Tuple of (success: bool, message: str).
    """
    email_cfg = settings.get("email", {})
    recipients = email_cfg.get("recipients", [])
    if not recipients:
        return False, "No recipients configured"
    if not email_cfg.get("smtp_server"):
        return False, "No SMTP server configured"

    test_event = {
        "event_type": "info",
        "metric": "cpu",
        "value": 42.0,
        "threshold": 90.0,
        "message": "This is a test email from Prism Server Monitor. If you received this, your email configuration is working correctly.",
    }

    # Temporarily build the email using the same logic
    dashboard_url = email_cfg.get("dashboard_url", "http://localhost:5000")
    timestamp = _format_email_timestamp(settings)

    html_body = f"""\
<html>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#F3F4F6;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr>
          <td style="background:#2563EB;padding:20px 24px;">
            <span style="color:#FFFFFF;font-size:20px;font-weight:700;">Prism Monitor</span><br>
            <span style="color:rgba(255,255,255,0.9);font-size:14px;margin-top:4px;display:inline-block;">Test Email</span>
          </td>
        </tr>
        <tr>
          <td style="padding:24px;">
            <p style="margin:0 0 16px;font-size:14px;color:#374151;">{test_event['message']}</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#F9FAFB;border-radius:6px;border:1px solid #E5E7EB;">
              <tr>
                <td style="padding:8px 16px;color:#6B7280;font-size:13px;">SMTP Server</td>
                <td style="padding:8px 16px;font-size:13px;">{email_cfg.get('smtp_server', '')}</td>
              </tr>
              <tr>
                <td style="padding:8px 16px;color:#6B7280;font-size:13px;">Time</td>
                <td style="padding:8px 16px;font-size:13px;">{timestamp}</td>
              </tr>
            </table>
            <p style="margin:20px 0 0;text-align:center;">
              <a href="{dashboard_url}" style="display:inline-block;padding:10px 24px;background:#2563EB;color:#FFFFFF;text-decoration:none;border-radius:6px;font-size:13px;font-weight:600;">View Dashboard</a>
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 24px;border-top:1px solid #E5E7EB;text-align:center;">
            <span style="font-size:11px;color:#9CA3AF;">Sent by Prism Server Monitor</span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "[Prism] Test Email"
    msg["From"] = email_cfg.get("from_address", "prism@localhost")
    msg["To"] = ", ".join(recipients)

    plain = f"Prism Test Email\n\n{test_event['message']}\n\nTime: {timestamp}"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        smtp_server = email_cfg.get("smtp_server", "")
        smtp_port = int(email_cfg.get("smtp_port", 587))
        use_tls = email_cfg.get("use_tls", True)
        username = email_cfg.get("username", "")
        password = email_cfg.get("password", "")

        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)

        if username and password:
            server.login(username, password)

        server.sendmail(msg["From"], recipients, msg.as_string())
        server.quit()
        logger.info("Test email sent successfully to %s", ", ".join(recipients))
        return True, f"Test email sent to {', '.join(recipients)}"

    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed. Check username and password."
    except smtplib.SMTPConnectError:
        return False, f"Cannot connect to SMTP server {smtp_server}:{smtp_port}"
    except TimeoutError:
        return False, f"Connection to {smtp_server}:{smtp_port} timed out"
    except Exception as e:
        logger.exception("Test email failed")
        return False, f"Failed to send: {type(e).__name__}: {e}"


def send_report_email(digest_data: dict | None, pdf_bytes: bytes | None,
                      settings: dict, report_type: str = "daily") -> bool:
    """Send a scheduled report email with digest summary and optional PDF attachment.

    Args:
        digest_data: Daily digest dict (from generate_daily_digest) or None
        pdf_bytes: PDF report bytes or None (attached if present)
        settings: App settings dict (contains email config)
        report_type: "daily" or "weekly"

    Returns:
        True if email sent successfully, False otherwise
    """
    email_cfg = settings.get("email", {})
    if not email_cfg.get("enabled") or not email_cfg.get("smtp_server"):
        logger.warning("Email not configured, skipping report email")
        return False

    recipients = email_cfg.get("recipients", [])
    if not recipients:
        logger.warning("No email recipients configured")
        return False

    import email.mime.application
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Build HTML body from digest data
    title = f"Prism {'Daily' if report_type == 'daily' else 'Weekly'} Report"
    ts_str = _format_email_timestamp(settings)

    html = f'<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">'
    html += f'<div style="background:#2563EB;color:white;padding:16px 20px;border-radius:8px 8px 0 0;">'
    html += f'<h2 style="margin:0;font-size:18px;">{title}</h2>'
    html += f'<p style="margin:4px 0 0;font-size:12px;opacity:0.8;">{ts_str}</p></div>'

    if digest_data:
        total = digest_data.get("total", 0)
        healthy = digest_data.get("healthy", 0)
        warning = digest_data.get("warning", 0)
        critical = digest_data.get("critical", 0)
        offline = digest_data.get("offline", 0)

        html += '<div style="padding:20px;background:#f9fafb;border:1px solid #e5e7eb;">'
        html += '<h3 style="margin:0 0 12px;font-size:14px;color:#374151;">Fleet Status</h3>'
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        html += f'<tr><td style="padding:4px 8px;">Total Servers</td><td style="padding:4px 8px;font-weight:bold;">{total}</td></tr>'

        if healthy > 0:
            html += f'<tr><td style="padding:4px 8px;">Healthy</td><td style="padding:4px 8px;color:#10B981;font-weight:bold;">{healthy}</td></tr>'
        if warning > 0:
            html += f'<tr><td style="padding:4px 8px;">Warning</td><td style="padding:4px 8px;color:#F59E0B;font-weight:bold;">{warning}</td></tr>'
        if critical > 0:
            html += f'<tr><td style="padding:4px 8px;">Critical</td><td style="padding:4px 8px;color:#DC2626;font-weight:bold;">{critical}</td></tr>'
        if offline > 0:
            html += f'<tr><td style="padding:4px 8px;">Offline</td><td style="padding:4px 8px;color:#6B7280;font-weight:bold;">{offline}</td></tr>'
        html += '</table>'

        # Needs attention
        needs_attention = digest_data.get("needs_attention", [])
        if needs_attention:
            html += '<h3 style="margin:16px 0 8px;font-size:14px;color:#DC2626;">Needs Attention</h3>'
            html += '<table style="width:100%;border-collapse:collapse;font-size:12px;">'
            html += '<tr style="background:#e5e7eb;"><th style="padding:6px 8px;text-align:left;">Server</th><th style="padding:6px 8px;text-align:left;">Status</th><th style="padding:6px 8px;text-align:left;">Anomalies</th></tr>'
            for s in needs_attention:
                status_color = "#DC2626" if s["status"] == "critical" else "#F59E0B" if s["status"] == "warning" else "#6B7280"
                html += f'<tr><td style="padding:4px 8px;font-weight:bold;">{s["name"]}</td>'
                html += f'<td style="padding:4px 8px;color:{status_color};font-weight:bold;">{s["status"]}</td>'
                html += f'<td style="padding:4px 8px;">{s.get("anomalies", 0)}</td></tr>'
            html += '</table>'
        else:
            html += '<p style="color:#10B981;font-size:13px;margin-top:12px;">All servers healthy.</p>'

        html += '</div>'

    dashboard_url = email_cfg.get("dashboard_url", "http://localhost:5000")
    html += f'<div style="padding:12px 20px;background:#f1f5f9;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 8px 8px;text-align:center;">'
    html += f'<a href="{dashboard_url}" style="color:#2563EB;font-size:12px;">Open Prism Dashboard</a></div></div>'

    # Build email
    msg = MIMEMultipart()
    msg["Subject"] = title + " — " + ts_str
    msg["From"] = email_cfg.get("from_address", "prism@localhost")
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    # Attach PDF if provided
    if pdf_bytes:
        pdf_part = email.mime.application.MIMEApplication(pdf_bytes, Name="prism_report.pdf")
        pdf_part["Content-Disposition"] = 'attachment; filename="prism_report.pdf"'
        msg.attach(pdf_part)

    # Send
    try:
        import smtplib
        if email_cfg.get("use_tls", True):
            server = smtplib.SMTP(email_cfg["smtp_server"], email_cfg.get("smtp_port", 587))
            server.starttls()
        else:
            server = smtplib.SMTP(email_cfg["smtp_server"], email_cfg.get("smtp_port", 25))

        if email_cfg.get("username") and email_cfg.get("password"):
            server.login(email_cfg["username"], email_cfg["password"])

        server.sendmail(msg["From"], recipients, msg.as_string())
        server.quit()
        logger.info("Scheduled %s report email sent to %d recipients", report_type, len(recipients))
        return True
    except Exception:
        logger.exception("Failed to send scheduled report email")
        return False
