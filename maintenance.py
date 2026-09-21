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

EXPIRY (WP-1, docs/plans/SEVERITY_MODEL_SPEC.md — "every window must
auto-expire, because a forever-mute is how real outages get eaten"):

  * Any window may carry ``expires_at`` — UTC ISO-8601, compared tz-aware
    per the house timezone rule. Past expiry the window NEVER matches.
  * An AD-HOC window is ``servers`` + ``expires_at`` and nothing else:
    "mute these boxes for the next two hours". It applies continuously
    until expiry — no days/times required, because that friction is why
    ad-hoc mutes end up as permanent recurring windows in other tools.
  * The matcher refusing expired windows is the load-bearing half;
    ``sweep_expired_windows`` removing them from settings is hygiene.
    Correctness must not depend on a periodic thread being alive.
  * A MALFORMED ``expires_at`` fails CLOSED — the window does not match.
    Same reasoning as the P15 timezone rule above: an unparseable mute
    that keeps muting is the exact failure expiry exists to end. The sweep
    deliberately leaves the malformed row in place, visibly wrong and
    inert, because hygiene must not destroy the evidence of the mistake.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

logger = logging.getLogger("prism.maintenance")


def _window_expired(window: dict) -> bool:
    """True when ``expires_at`` is present and in the past — or unparseable.

    Fail-closed on parse errors, and loudly: the operator typo'd a
    timestamp on a MUTE, and the safest reading of a mute you cannot
    interpret is "not muting".
    """
    raw = window.get("expires_at")
    if not raw:
        return False
    try:
        exp = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        logger.warning(
            "maintenance window for %s has unparseable expires_at=%r — "
            "treating the window as expired (fail closed).",
            window.get("servers"), raw)
        return True
    return exp <= datetime.datetime.now(datetime.timezone.utc)


def _is_adhoc(window: dict) -> bool:
    """An ad-hoc window has an expiry and no recurring schedule."""
    return bool(window.get("expires_at")) and not window.get("days")


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
        # Expiry beats everything: an expired window never matches, ad-hoc
        # or recurring, swept or not. The read site is the guarantee.
        if _window_expired(window):
            continue
        # Ad-hoc window: continuous until expiry, no schedule to match.
        if _is_adhoc(window):
            return window
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


def is_in_maintenance(server_name: str, settings: dict) -> bool:
    """Public read: is this server inside an active maintenance window NOW?

    The estate fold and the flap latch (WP-1 phases 2+) call this — a
    server in maintenance contributes nothing to the estate score and its
    transitions do not count toward the latch. The underscored matcher
    stays the single implementation; this is its stable name.
    """
    return _get_active_maintenance_window(server_name, settings) is not None


def sweep_expired_windows(config_manager) -> int:
    """Remove expired windows from settings. Returns how many were removed.

    Hygiene, not correctness — the matcher already refuses expired windows.
    Two deliberate asymmetries:

      * PARSEABLE-and-past windows are removed; MALFORMED ones are kept.
        The matcher fails closed on both, but a malformed row is operator
        evidence to show, not to silently delete.
      * No write when nothing expired: a sweep that rewrites settings every
        hour turns the config-change audit trail into noise.
    """
    settings = config_manager.get_settings()
    windows = settings.get("maintenance_windows", []) or []

    def _cleanly_expired(w: dict) -> bool:
        raw = w.get("expires_at")
        if not raw:
            return False
        try:
            exp = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            return False        # malformed: keep visible, matcher fails closed
        return exp <= datetime.datetime.now(datetime.timezone.utc)

    kept = [w for w in windows if not _cleanly_expired(w)]
    removed = len(windows) - len(kept)
    if removed:
        config_manager.save_maintenance_windows(kept)
        logger.info("maintenance sweep removed %d expired window(s)", removed)
    return removed


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
