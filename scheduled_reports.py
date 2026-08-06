"""Scheduled report generation — daily / weekly digests emailed and
saved to disk.

Settings (``scheduled_reports`` section):
  * ``enabled``           — master switch
  * ``daily_enabled``     + ``daily_time`` (HH:MM)
  * ``weekly_enabled``    + ``weekly_day`` (lowercased weekday name)
                          + ``weekly_time`` (HH:MM)
  * ``email_report``      — send the digest as email
  * ``include_pdf``       — generate and attach a PDF

The "did we already run today?" check uses module-level state
(``_last_daily_report_date`` / ``_last_weekly_report_date``) so a
restart picks up running yet-to-be-run reports correctly. The v2
periodics thread calls ``_check_scheduled_reports`` once a minute and
the state-based guard prevents duplicate sends.

This module exists separately from ``reports.py`` (which generates the
PDF itself) because the PDF builder is heavy (WeasyPrint + Pillow) and
the scheduler should not pull it in until a report is actually being
generated.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timezone

logger = logging.getLogger("prism.scheduled_reports")


# Module-level "already sent today" guards. Each holds a YYYY-MM-DD
# string (or None at process start). Compared against today's date in
# the configured timezone to enforce one-send-per-day semantics.
_last_daily_report_date: str | None = None
_last_weekly_report_date: str | None = None


def _check_scheduled_reports(db, servers, settings: dict) -> None:
    """Check if a daily or weekly scheduled report is due; generate/send it.

    Called once per minute from ``collector_v2/periodics.py``. Returns
    immediately when ``scheduled_reports.enabled`` is False.

    The day comparison is done in the configured timezone — relying on
    UTC dates would shift the "today" boundary at the wrong wallclock
    moment for non-UTC operators.
    """
    global _last_daily_report_date, _last_weekly_report_date

    sched = settings.get("scheduled_reports", {})
    if not sched.get("enabled", False):
        return

    tz_str = settings.get("timezone", "Europe/Berlin")
    try:
        now_local = datetime.now(zoneinfo.ZoneInfo(tz_str))
    except (ImportError, Exception):
        now_local = datetime.now(timezone.utc)

    today_str = now_local.strftime("%Y-%m-%d")
    current_time = now_local.strftime("%H:%M")
    current_weekday = now_local.strftime("%A").lower()

    # Daily report check
    if (sched.get("daily_enabled", True)
            and _last_daily_report_date != today_str
            and current_time >= sched.get("daily_time", "07:00")):
        _last_daily_report_date = today_str
        logger.info("Generating daily scheduled report")
        _generate_scheduled_report(db, servers, settings, "daily")

    # Weekly report check
    if (sched.get("weekly_enabled", False)
            and _last_weekly_report_date != today_str
            and current_weekday == sched.get("weekly_day", "monday")
            and current_time >= sched.get("weekly_time", "07:00")):
        _last_weekly_report_date = today_str
        logger.info("Generating weekly scheduled report")
        _generate_scheduled_report(db, servers, settings, "weekly")


def _generate_scheduled_report(db, servers, settings: dict, report_type: str) -> None:
    """Generate and optionally email/persist a scheduled report.

    Each step is independently try/excepted so a PDF failure doesn't
    block the email send (and vice versa). The function is "best-effort
    delivery, never crash the scheduler" — exceptions are logged.
    """
    try:
        from analytics import generate_daily_digest
        from reports import generate_pdf_report
        from i18n import get_translations

        # Generate digest data
        digest = generate_daily_digest(db, servers)

        # Generate PDF if configured
        pdf_bytes = None
        if settings.get("scheduled_reports", {}).get("include_pdf", True):
            lang = settings.get("language", "en")
            translations = get_translations(lang)

            # Use a temporary ConfigManager-like object for PDF generation.
            # The PDF builder calls .get_servers() / .get_settings() /
            # .get_raw_servers() — we provide just those.
            class _SettingsProxy:
                def get_servers(self_inner):  # noqa: N805
                    return servers

                def get_settings(self_inner):  # noqa: N805
                    return settings

                def get_raw_servers(self_inner):  # noqa: N805
                    return [{"name": s.name, "host": s.host, "type": s.type} for s in servers]

            try:
                pdf_bytes = generate_pdf_report(db, _SettingsProxy(), translations)
            except Exception:
                logger.exception("Failed to generate PDF for scheduled report")

        # Send email if configured
        if settings.get("scheduled_reports", {}).get("email_report", True):
            from email_alerts import send_report_email
            send_report_email(digest, pdf_bytes, settings, report_type)

        # Save to disk (best-effort, never crash the email send if this fails)
        try:
            from pathlib import Path
            report_dir = Path(__file__).parent / "data" / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)

            if pdf_bytes:
                tz_name = settings.get("timezone", "Europe/Berlin")
                try:
                    _tz = zoneinfo.ZoneInfo(tz_name)
                except Exception:
                    _tz = timezone.utc
                filename = (
                    f"prism_{report_type}_"
                    f"{datetime.now(timezone.utc).astimezone(_tz).strftime('%Y%m%d')}.pdf"
                )
                (report_dir / filename).write_bytes(pdf_bytes)
                logger.info("Saved %s report to %s", report_type, report_dir / filename)
        except Exception:
            logger.exception("Failed to save scheduled report to disk")

        logger.info("Scheduled %s report completed", report_type)
    except Exception:
        logger.exception("Failed to generate scheduled %s report", report_type)
