"""Maintenance-window gating — schedule-aware suppression and threshold
loosening for servers undergoing planned work.

A maintenance window has three effects on a server during its active
hours:

  1. **Threshold loosening** — per-window ``thresholds`` dict overrides
     the per-server values inside ``compute_status``. Used to let CPU /
     RAM / disk run hotter during patching without firing warnings.
  2. **Full alert suppression** — when the window's ``suppress_alerts``
     flag is set, ALL alert dispatch sites (status-change, baseline
     deviation, anomaly, rate-anomaly, security check, failed-login
     spike, TLS) must skip event insertion + email + webhook.
  3. **Detection-result demotion** — handled by ``detection.compute_status``,
     which late-imports ``_get_maintenance_thresholds`` from here.

The functions live here (rather than in ``collector.py``) because v2
needs them too — the periodics thread, the aggregator, and the
supervisor all consult maintenance state, and reaching back into the
legacy module for shared logic blocks v1 retirement.

Module dependencies are strictly downward:
  this module imports nothing from the project except logging.
  ``detection.py`` and ``collector.py`` import FROM here.

Schedule matching algorithm — a window applies when ALL of:
  * ``server_name`` ∈ ``window["servers"]``
  * today (in the configured timezone) ∈ ``window["days"]`` (0=Mon..6=Sun)
  * the current HH:MM is inside ``[start_time, end_time]`` — with
    overnight wrap handled (e.g. 22:00 → 06:00 spans midnight).
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

logger = logging.getLogger("prism.maintenance")


def _get_active_maintenance_window(server_name: str, settings: dict) -> dict | None:
    """Return the currently-active maintenance window dict for this server,
    or ``None`` if no window applies right now.

    Used by:
      * ``_get_maintenance_thresholds`` — threshold loosening
      * ``_is_alert_suppressed_by_maintenance`` — full alert suppression
      * ``collector_v2.supervisor`` — schedule decisions
      * ``routes.views`` — UI badge for "in maintenance"
    """
    windows = settings.get("maintenance_windows", [])
    if not windows:
        return None

    tz_name = settings.get("timezone", "Europe/Berlin")
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.datetime.now(tz)
    except Exception:
        # P15 from AUDIT-2026-05: do NOT fall back to a naive datetime.now().
        # On a misconfigured system (Berlin tz config but server clock in UTC),
        # naive comparisons against schedule HH:MM strings fire windows at the
        # wrong wallclock hour, suppressing or failing-to-suppress alerts at
        # the wrong time. Refusing here means maintenance windows are simply
        # not evaluated until the operator fixes their tz config — which is
        # the right behaviour given the project's timezone rule (all timestamps
        # must use configured timezone; never silent-fall-back to naive).
        logger.warning(
            "zoneinfo failed for tz='%s' — maintenance windows will not be evaluated "
            "this check. Fix the timezone setting.",
            tz_name,
        )
        return None

    current_day = now.weekday()
    current_time = now.strftime("%H:%M")

    for window in windows:
        if server_name not in window.get("servers", []):
            continue
        if current_day not in window.get("days", []):
            continue
        start = window.get("start_time", "00:00")
        end = window.get("end_time", "23:59")
        if start <= end:
            if start <= current_time <= end:
                return window
        else:
            # Overnight window (e.g. 22:00–06:00)
            if current_time >= start or current_time <= end:
                return window
    return None


def _is_alert_suppressed_by_maintenance(server_name: str, settings: dict) -> bool:
    """Return True if alerts should be SUPPRESSED ENTIRELY for this server right now.

    Driven by the per-window ``suppress_alerts`` boolean (default False —
    the legacy behaviour is threshold loosening only). When True, every
    notification dispatch site MUST check this and skip db.insert_event
    + email + webhook.

    Wired into:
      * status-change events (``compute_status`` path)
      * failed-login spike + lockout (``collector._collect_all_failed_logins``)
      * baseline_engine deviation events
      * analytics anomaly + rate_anomaly events
      * security_checker (the caller must check; the checker itself is unaware)
      * tls_certificate alerts (``_check_tls_certificates``)
      * v2 aggregator's status-transition emit path
    """
    win = _get_active_maintenance_window(server_name, settings)
    return bool(win and win.get("suppress_alerts", False))


def _get_maintenance_thresholds(server_name: str, settings: dict) -> dict | None:
    """Return overridden threshold dict if server is in an active window with
    a ``thresholds`` override, else ``None``.

    Called from ``detection.compute_status`` via a late import to keep
    the maintenance ↔ detection dependency one-way at import time.
    """
    win = _get_active_maintenance_window(server_name, settings)
    return win.get("thresholds", {}) if win else None
