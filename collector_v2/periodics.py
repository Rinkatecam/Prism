"""Periodic helpers for collector v2 — fills the gap v1's collector_loop
filled "for free" by running at the tail of every cycle.

The v1 collector ran several side jobs at low cadence as part of the same
loop that pulled metrics: TLS cert checks, scheduled reports, failed-login
collection, drift snapshots, health-check probes, and DB retention
cleanup. v2's supervisor / worker / aggregator pipeline doesn't include
these because they don't share the per-server fan-out shape — they're
fleet-wide jobs that benefit from a single owning thread.

This module runs them in their own daemon thread (`prism-collector-v2-
periodics`) at the same cadences v1 used:

| Job                       | Cadence    | v1 source                          |
| ------------------------- | ---------- | ---------------------------------- |
| TLS certificate checks    | every 1h   | `_check_tls_certificates`          |
| Health-check probes       | every 5min | `_run_health_checks`               |
| Failed-login collection   | every 5min | `_collect_all_failed_logins`       |
| Drift snapshots           | every 60min| `_collect_drift_snapshots`         |
| Scheduled-report check    | every 1min | `_check_scheduled_reports`         |
| DB retention cleanup      | every 1h   | inline in `collector_loop`         |

We DON'T reimplement these — we call the existing v1 functions. They're
already well-tested and battle-hardened; the only thing v2 changes is
how they're scheduled.

Audit reference: this closes H1 from docs/COLLECTOR_V2_AUDIT.md.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("prism.collector_v2.periodics")

# Per-job state — last successful run time + lock to make sure two threads
# don't race on the same job. The supervisor (and the periodics thread)
# can both be reading these for health snapshots; the lock guards writes.
_last_run: dict[str, float] = {}

# Consecutive-failure count per job, driving the retry backoff in the loop.
# Reset to 0 on the next success so a job that recovers returns to its normal
# cadence immediately rather than staying penalised.
_consecutive_failures: dict[str, int] = {}

# First retry delay after a failure; doubles per consecutive failure, capped at
# the job's own interval (see _periodics_loop). 60s is one metrics cycle — long
# enough to stop a permanently-broken job busy-looping, short enough that a
# transient blip still recovers promptly.
_RETRY_BASE_S = 60.0

# Loop tick. Long enough to be cheap, short enough that the 60s
# scheduled_reports job runs roughly on time.
_TICK_S = 30.0
_critical_error_count: int = 0
_last_heartbeat: float = 0.0
_lock = threading.Lock()
_stop_event = threading.Event()


class _Job:
    """One periodic job. The handler is called with whatever args it needs,
    bound at registration time so the loop itself stays generic."""

    def __init__(self, name: str, interval_s: int, handler: Callable[[], None]):
        self.name = name
        self.interval_s = interval_s
        self.handler = handler

    def due(self, now: float) -> bool:
        last = _last_run.get(self.name, 0.0)
        return (now - last) >= self.interval_s


def _heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.time()


def get_periodics_health() -> dict[str, Any]:
    """Snapshot for /api/system/health."""
    now = time.time()
    return {
        "last_heartbeat_s_ago": (now - _last_heartbeat) if _last_heartbeat else None,
        "critical_errors_total": _critical_error_count,
        "last_run": {
            name: round(now - t, 1) for name, t in _last_run.items()
        },
    }


def _build_jobs(get_servers, get_settings, db) -> list[_Job]:
    """Bind the periodics-job functions to the current Prism state.

    Each handler is a zero-arg closure that calls into the appropriate
    module-level function with the right args. We do this so the loop's
    dispatch is uniform and a failure in one job doesn't poison the
    others.

    Post-R1 retirement: each helper lives in its own module
    (``tls_monitor``, ``healthchecks``, ``drift``, ``failed_logins``,
    ``scheduled_reports``). All imports are deferred to the closure
    bodies so this function stays cheap to import in tests that don't
    exercise the actual jobs.
    """
    def _tls():
        from tls_monitor import _check_tls_certificates
        _check_tls_certificates(db, get_settings())

    def _health_checks():
        from healthchecks import _run_health_checks
        _run_health_checks(db, get_settings())

    def _failed_logins():
        # Only fire if security_alerts.failed_login_tracking is on,
        # mirroring v1's gating (collector.py:2438).
        settings = get_settings()
        sec_cfg = settings.get("security_alerts", {})
        if sec_cfg.get("failed_login_tracking", True):
            from failed_logins import _collect_all_failed_logins
            _collect_all_failed_logins(db, get_servers(), settings)

    def _drift():
        settings = get_settings()
        drift_cfg = settings.get("drift_detection", {})
        if drift_cfg.get("enabled", False):
            from drift import _collect_drift_snapshots
            _collect_drift_snapshots(db, get_servers(), settings)

    def _scheduled_reports():
        from scheduled_reports import _check_scheduled_reports
        _check_scheduled_reports(db, get_servers(), get_settings())

    def _retention():
        settings = get_settings()
        retention_days = settings.get("retention_days", 30)
        logger.info("Running retention cleanup (keeping %d days)", retention_days)
        db.cleanup_old_data(retention_days,
                            per_table=settings.get("retention"))
        db.cleanup_anomaly_suppression(hours=24)
        db.cleanup_expired_snoozes()
        # Refresh the query planner's statistics after the row counts have just
        # changed. Without sqlite_stat1 the planner full-SCANs
        # idx_metrics_server_time for per-server aggregates; with it, it uses a
        # skip-scan (SEARCH ... ANY(server_name) AND timestamp>?). Measured on
        # the live DB: a 24h fleet ledger goes 12.4ms -> 5.5ms, a 2.2x win for
        # one statement, and the gap widens with row count. The live database
        # had NO sqlite_stat1 at all. Costs 321ms at 64k rows.
        db.analyze()

    def _database_backup():
        settings = get_settings()
        if not settings.get("database_backup", {}).get("enabled", True):
            return
        run_scheduled_backup(db, settings)

    def _baseline_recalc():
        # settings.baseline_detection.recalc_hour was saved by the Monitoring
        # page UI but never wired to anything — the "nightly" baseline job
        # was only reachable via the manual POST /api/baselines/recalculate
        # button. This ties it to an actual daily schedule; see
        # run_baseline_recalc_if_due() below for the full semantics
        # (once/day at recalc_hour local time + startup catch-up).
        run_baseline_recalc_if_due(db, get_servers, get_settings())

    def _auto_restart_scanner():
        """Safety-net: scan ``_update_install_state`` for any server stuck
        in ``restart_required`` with ``restart_after=True`` and fire the
        restart. Pairs with the per-install watcher thread spawned at
        install kickoff (see ``routes/api/updates._spawn_auto_restart_watcher``).

        Failure modes this guards against:
          * Watcher thread crashed (uncaught exception)
          * Flask process restarted between install kickoff and
            restart_required — the watcher dies, but if the operator
            re-triggers the install_state from the remote
            ``update-status.json`` (via the status-poll endpoint),
            ``restart_after`` is rehydrated and this scanner picks it up.
          * Watcher hit its 90-min deadline without firing (rare)
          * Two watchers running for the same server (idempotent — the
            restart command itself is idempotent and the install_state
            is popped/transitioned only once)
        """
        try:
            from routes.api._shared import _update_install_state
            from routes.api.updates import _trigger_server_restart_internal
        except Exception:
            return  # routes blueprint not loaded (test env / cold start)
        # Snapshot to avoid mutation during iteration; the request threads
        # can write to this dict while we read.
        for name in list(_update_install_state.keys()):
            entry = _update_install_state.get(name) or {}
            if entry.get("status") != "restart_required":
                continue
            if not entry.get("restart_after"):
                continue
            logger.info(
                "[%s] auto-restart scanner firing restart "
                "(restart_required + restart_after=True, watcher may have died)",
                name,
            )
            try:
                _trigger_server_restart_internal(
                    name, actor="system:auto_restart_scanner",
                )
            except Exception:
                logger.warning(
                    "[%s] auto-restart scanner failed to fire restart", name,
                    exc_info=True,
                )

    def _reboot_state_janitor():
        """Clean up install_state rows that have outstayed their welcome.

        Three lifecycle states get garbage-collected here, each with a
        different timeout calibrated to the operational expectation
        for that state:

          * ``rebooting`` / ``stabilising`` — Prism owns the transition;
            normal path is the aggregator's ``_handle_post_reboot``
            clearing the row when metrics resume. Stuck after 20 min
            means the server didn't come back. Without this janitor the
            dashboard would show "Rebooting" or "Stabilising" forever —
            actively misleading. Clear the row; the normal
            offline/stale badges take over.

          * ``restart_required`` — Prism installed updates and signalled
            the operator (or auto-restart watcher) to reboot. Normal
            path is the aggregator's auto-clear in
            ``_handle_updates_result`` — a successful UPDATES check
            returning ``pending_reboot=False`` pops the row. Failure
            mode that triggered SRV01's >7-day stale row: the target
            never sent a successful UPDATES result that confirmed the
            reboot happened. Reasons range from "server permanently
            unreachable" to "credentials rotated" to "Windows still
            reports a different pending reboot from a separate WU
            install we didn't do". After 48 h the install_state row is
            outliving any useful signal — operator has been notified
            for two full operating days; if a reboot is still needed,
            the live ``server_update_info.reboot_required`` flag is
            the truthful source. Clear the install_state row; the
            "needs restart" pill on the dashboard still fires from the
            live UPDATES result if Windows still says reboot is needed.

        Why 48 h for restart_required and not less? Operators
        legitimately delay reboots inside change windows (e.g., monthly
        patch Tuesday → wait for weekend reboot). 24 h would conflict
        with that. 7 days would let stale rows fester (SRV01 was a
        week old). 48 h is the operational sweet spot: long enough to
        clear a normal change cycle, short enough to catch genuinely
        stale rows.

        We use the row's ``reboot_started_at`` if present (set on the
        rebooting transition and preserved through stabilising), else
        ``completed_at`` (set when the install finishes), else
        ``updated_at`` so a row that somehow lost its primary anchor
        still gets garbage-collected eventually.
        """
        REBOOT_TIMEOUT_S = 20 * 60                # rebooting / stabilising
        RESTART_REQUIRED_TIMEOUT_S = 48 * 60 * 60  # restart_required (48h)
        STUCK_STATUSES = {"rebooting", "stabilising", "restart_required"}
        try:
            from routes.api._shared import (
                _update_install_state,
                _persist_install_state,
            )
        except Exception:
            return  # routes not loaded; nothing to clean
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        # Snapshot keys so we don't mutate while iterating (the route
        # handlers can write to this dict from request threads).
        for name in list(_update_install_state.keys()):
            entry = _update_install_state.get(name)
            if not entry:
                continue
            status = entry.get("status")
            if status not in STUCK_STATUSES:
                continue
            # Anchor for staleness — different states record different
            # canonical timestamps, so fall through in order of accuracy:
            #   reboot_started_at  (rebooting/stabilising, set at trigger)
            #   completed_at       (restart_required, set when install finished)
            #   updated_at         (last write, always present after any
            #                       transition — safety net)
            ts_str = (
                entry.get("reboot_started_at")
                or entry.get("completed_at")
                or entry.get("updated_at")
            )
            if not ts_str:
                continue
            try:
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
            except (ValueError, TypeError):
                continue
            timeout = (
                RESTART_REQUIRED_TIMEOUT_S
                if status == "restart_required"
                else REBOOT_TIMEOUT_S
            )
            if (now - ts).total_seconds() >= timeout:
                _update_install_state.pop(name, None)
                _persist_install_state()
                logger.warning(
                    "[%s] %s state exceeded %ds (anchor=%s) — install_state "
                    "cleared so dashboard shows the truthful state from the "
                    "live UPDATES check instead of a stale snapshot",
                    name, status, timeout, ts_str,
                )

    def _audit_chain_verifier():
        """F-AT-1 (CSV-12 / 17 remediation): periodically verify the
        audit_log hash chain so tampering surfaces within hours, not at
        the next on-demand check.

        ``Database.verify_audit_chain(limit=N)`` walks the most recent N
        rows; we use 5 000 here — enough to catch a recent break, cheap
        enough to run hourly without DB load. The result is stashed in
        ``state.last_audit_chain_check`` for the health endpoint to read,
        and a tampering finding writes a Critical audit row about itself.
        """
        try:
            from collector_v2 import state as _state
        except Exception:
            return
        try:
            result = db.verify_audit_chain(limit=5000)
        except Exception:
            logger.debug("verify_audit_chain raised", exc_info=True)
            return
        _state.last_audit_chain_check = {
            "ts": time.time(),
            "ok": bool(result.get("ok")),
            "checked": int(result.get("checked") or 0),
            "first_break_id": result.get("first_break_id"),
            "first_break_reason": result.get("first_break_reason"),
        }
        if not result.get("ok"):
            # This is a CRITICAL finding: hash chain integrity is one
            # of the load-bearing CSV controls. Surface immediately.
            try:
                db.log_audit(
                    username="system",
                    action="audit_chain_tamper_detected",
                    category="security",
                    details=(
                        f"first_break_id={result.get('first_break_id')}, "
                        f"reason={result.get('first_break_reason')}"
                    ),
                )
            except Exception:
                logger.error(
                    "audit_chain_tamper_detected — could not log it to audit "
                    "(this is also a finding): %s",
                    result,
                )
            logger.critical(
                "AUDIT CHAIN TAMPERING DETECTED: %s",
                result,
            )

    def _ldap_probe():
        # Originally inline in v1's collector_loop — fires every 5 min when
        # auth.enabled. State transitions get an audit row inside
        # auth.ldap_health_probe(). Gated to avoid noise when auth is off.
        #
        # Post-R3: the config handle that used to be in
        # ``collector._COLLECTOR_CONFIG_REF`` (set by
        # ``collector.set_collector_config_ref(config)`` at app startup)
        # now lives on the ``collector_v2`` module itself —
        # ``app.py`` writes it after ``start_collector_v2()``.
        settings = get_settings()
        if not settings.get("auth", {}).get("enabled", False):
            return
        try:
            from auth import ldap_health_probe as _probe
            import collector_v2 as _v2_mod
            cfg = getattr(_v2_mod, "_COLLECTOR_CONFIG_REF", None)
            if cfg is not None:
                _probe(cfg)
        except Exception:
            logger.debug("ldap probe failed", exc_info=True)

    def _security_status():
        # v1 inline at collector.py:2658-2667 — every 30 cycles (~30 min)
        # when security_alerts.security_status_check is on. Per-server
        # call to security_checker.collect_security_status. Maintenance
        # window suppression applied per-server inside the loop.
        settings = get_settings()
        sec_cfg = settings.get("security_alerts", {})
        if not sec_cfg.get("security_status_check"):
            return
        try:
            from security_checker import collect_security_status
            from maintenance import _is_alert_suppressed_by_maintenance
            for server in get_servers():
                if _is_alert_suppressed_by_maintenance(server.name, settings):
                    continue
                try:
                    collect_security_status(db, server, settings)
                except Exception:
                    logger.exception(
                        "[%s] security_status check failed", server.name
                    )
        except ImportError:
            logger.debug("security_checker module not available", exc_info=True)

    # Honour the v1-era cycle-based knobs by translating them to seconds.
    # The knobs are not exposed in the UI; power users edit settings.json
    # directly. Both settings name "cycles" — under v1 a cycle was the
    # poll_interval (default 60s). We multiply by the current poll_interval
    # so an operator who sets `check_interval_cycles=30` at poll=60 gets
    # the same 1800s cadence they got under v1.
    #
    # Bounds:
    #   * minimum 60s — any cadence faster than the periodics tick (30s)
    #     would be meaningless; the floor stays double the tick for safety.
    #   * maximum 86400s (24h) — a sanity cap, not a meaningful policy.
    #
    # Defaults (when the setting is absent) preserve v2's previous behaviour:
    #   * tls_certs: 3600s (1h) — close to v1's default 30*60s = 1800s; we
    #     deliberately stay at 1h when the operator hasn't set anything,
    #     because TLS certs change on a daily-to-weekly scale and 1h is
    #     friendlier on slow CAs.
    #   * drift: 3600s — matches v1's default 60*60s exactly.
    settings_snap = get_settings() or {}
    poll_s = max(5, int(settings_snap.get("poll_interval_seconds", 60)))

    def _cycles_to_seconds(cfg_section: str, default_seconds: int) -> int:
        cfg = settings_snap.get(cfg_section, {}) or {}
        cycles = cfg.get("check_interval_cycles")
        if cycles is None:
            return default_seconds
        try:
            seconds = int(cycles) * poll_s
            return max(60, min(seconds, 86400))
        except (TypeError, ValueError):
            logger.warning(
                "%s.check_interval_cycles is not an integer (%r), using default %ds",
                cfg_section, cycles, default_seconds,
            )
            return default_seconds

    tls_interval_s = _cycles_to_seconds("tls_monitoring", 3600)
    drift_interval_s = _cycles_to_seconds("drift_detection", 3600)
    # Feature 1.8: backup cadence from interval_hours (default 24h), floored to
    # a 1h minimum so a hand-edited 0/garbage value can't hammer the DB.
    try:
        backup_interval_s = max(3600, int(
            get_settings().get("database_backup", {}).get("interval_hours", 24)) * 3600)
    except (TypeError, ValueError):
        backup_interval_s = 86400
    logger.info(
        "Periodics cadences resolved: tls=%ds drift=%ds (poll_interval=%ds)",
        tls_interval_s, drift_interval_s, poll_s,
    )

    return [
        # Order matters only for first-tick race-resolution. Quick jobs first
        # so a stuck slow one (e.g. TLS probe hung on a slow CA) doesn't
        # delay the lighter housekeeping.
        _Job("scheduled_reports", 60, _scheduled_reports),
        # Reboot-state cleanup ticks every minute — fast enough that a
        # stuck "Rebooting" badge doesn't linger noticeably past the 20 min
        # timeout, cheap enough that it costs nothing on the common path.
        _Job("reboot_state_janitor", 60, _reboot_state_janitor),
        # F-AT-1: hourly audit-chain integrity verification.
        _Job("audit_chain_verifier", 3600, _audit_chain_verifier),
        # Auto-restart safety-net — fires any pending restart that the
        # per-install watcher thread missed (Flask restart, watcher crash,
        # 90-min timeout). Runs every minute so worst-case latency from
        # restart_required to actual restart is ~60 s.
        _Job("auto_restart_scanner", 60, _auto_restart_scanner),
        _Job("ldap_probe", 300, _ldap_probe),
        _Job("health_checks", 300, _health_checks),
        _Job("failed_logins", 300, _failed_logins),
        _Job("tls_certs", tls_interval_s, _tls),
        _Job("drift", drift_interval_s, _drift),
        _Job("security_status", 1800, _security_status),
        _Job("retention", 3600, _retention),
        # Feature 1.8: scheduled online DB backup with rotation + freshness.
        _Job("database_backup", backup_interval_s, _database_backup),
        # Checked every 60s like the other minute-cadence jobs above; the
        # 60s interval only gates how often we *look*, not how often the
        # (expensive, fleet-wide) recalc actually runs — that's governed
        # entirely by run_baseline_recalc_if_due()'s own once-per-day logic.
        _Job("baseline_recalc", 60, _baseline_recalc),
    ]


# ── Scheduled baseline recalculation ─────────────────────────────────────────
#
# baseline_engine.nightly_baseline_job() computes hour-of-week baselines for
# every server. Historically it was ONLY reachable via the manual
# POST /api/baselines/recalculate button — the settings.baseline_detection
# .recalc_hour knob the Monitoring page UI lets an operator set was saved
# but read by nothing. This section closes that gap.
#
# Scheduling semantics:
#   * Runs at most once per LOCAL calendar day (local = settings.timezone,
#     default Europe/Berlin), at/after settings.baseline_detection
#     .recalc_hour (HH:MM, local). "Already ran today" is derived from the
#     DB (newest metric_baselines.updated_at), not an in-memory flag, so
#     it's correct across app restarts within the same day.
#   * Startup catch-up: the very first check after the periodics thread
#     starts also asks "are baselines stale (newest updated_at >= 25h old)
#     or missing entirely (empty table) with metric history available to
#     build them from?" — if so it runs immediately instead of waiting for
#     recalc_hour. This only fires once per process lifetime
#     (_baseline_recalc_startup_checked), so a pathological case where
#     nightly_baseline_job legitimately writes zero rows (e.g. a
#     brand-new server with <2 samples per hour-of-week slot) can't cause
#     it to retrigger on every tick.
#   * Skips entirely when settings.baseline_detection.enabled is false.
#   * Malformed recalc_hour falls back to 02:00, logged once.

_BASELINE_RECALC_DEFAULT_HOUR = "02:00"
_BASELINE_RECALC_STALE_HOURS = 25.0

# Module-level, in-memory, process-lifetime state — matches the convention
# other one-shot-ish periodics gates use (e.g. _last_heartbeat above).
_baseline_recalc_startup_checked = False
_baseline_recalc_bad_hour_logged = False
# Authoritative "did we already run today" marker, set unconditionally right
# after we invoke nightly_baseline_job — independent of whether that run
# actually wrote any metric_baselines rows (e.g. a brand-new fleet with <2
# samples per hour-of-week slot still writes zero rows; without this we'd
# reattempt the recalc on every subsequent tick for the rest of the day).
# The DB-derived signal below (metric_baselines.updated_at) is the primary,
# restart-safe check; this is the belt-and-suspenders backstop for the
# common case, in-process, where the DB signal alone wouldn't catch it.
_baseline_recalc_last_run_date: str | None = None


def _resolve_baseline_tz(settings: dict):
    tz_name = (settings or {}).get("timezone") or "Europe/Berlin"
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _parse_recalc_hour(raw) -> tuple[int, int]:
    """Parse 'HH:MM' defensively. Malformed input falls back to 02:00 and
    logs a warning exactly once (settings.json can be hand-edited)."""
    global _baseline_recalc_bad_hour_logged
    try:
        hh_s, mm_s = str(raw).split(":", 1)
        hh, mm = int(hh_s), int(mm_s)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except (ValueError, TypeError, AttributeError):
        pass
    if not _baseline_recalc_bad_hour_logged:
        logger.warning(
            "baseline_detection.recalc_hour is malformed (%r) — falling back to %s",
            raw, _BASELINE_RECALC_DEFAULT_HOUR,
        )
        _baseline_recalc_bad_hour_logged = True
    hh_s, mm_s = _BASELINE_RECALC_DEFAULT_HOUR.split(":")
    return int(hh_s), int(mm_s)


def run_baseline_recalc_if_due(db, get_servers, settings, *, now_utc=None) -> bool:
    """Run nightly_baseline_job() if it's due; returns True iff it ran.

    ``now_utc`` is injectable for tests; production callers (the periodics
    loop) always pass None and get the real clock.
    """
    global _baseline_recalc_startup_checked, _baseline_recalc_last_run_date

    baseline_cfg = (settings or {}).get("baseline_detection", {}) or {}
    if not baseline_cfg.get("enabled", True):
        return False

    tz = _resolve_baseline_tz(settings)
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_str = now_local.strftime("%Y-%m-%d")

    # Track whether the DB read SUCCEEDED — a raised read must NOT be
    # conflated with "table empty" (which would trigger a needless recompute
    # or, worse, burn the one-shot startup catch-up on a transient lock).
    last_updated_raw = None
    read_ok = True
    try:
        last_updated_raw = db.get_baselines_last_updated()
    except Exception:
        read_ok = False
        logger.debug("baseline_recalc: get_baselines_last_updated failed", exc_info=True)

    already_ran_today = _baseline_recalc_last_run_date == today_str
    age_h = None
    if last_updated_raw:
        try:
            last_dt = datetime.fromisoformat(str(last_updated_raw).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if last_dt.astimezone(tz).strftime("%Y-%m-%d") == today_str:
                already_ran_today = True
            age_h = (now_utc - last_dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            logger.debug("baseline_recalc: unparsable updated_at %r", last_updated_raw)

    # Startup catch-up — evaluated once per process lifetime, on the first
    # tick where the DB read SUCCEEDED. Gating on read_ok (rather than
    # unconditionally consuming the flag) means a transient DB error on the
    # very first tick doesn't burn the catch-up or misread an existing-but-
    # unreadable baseline set as an empty table.
    do_catchup = False
    if not _baseline_recalc_startup_checked and read_ok:
        _baseline_recalc_startup_checked = True
        if last_updated_raw is None:
            try:
                do_catchup = bool(db.has_metric_history())
            except Exception:
                # Couldn't confirm history — leave the catch-up un-consumed so
                # a later tick can retry rather than silently skipping it.
                _baseline_recalc_startup_checked = False
                logger.debug("baseline_recalc: has_metric_history failed", exc_info=True)
        elif age_h is not None and age_h >= _BASELINE_RECALC_STALE_HOURS:
            do_catchup = True

    if already_ran_today:
        return False

    hh, mm = _parse_recalc_hour(baseline_cfg.get("recalc_hour", _BASELINE_RECALC_DEFAULT_HOUR))
    time_reached = (now_local.hour, now_local.minute) >= (hh, mm)

    if not (time_reached or do_catchup):
        return False

    # The recalc itself must not permanently consume the startup catch-up if
    # it raises before completing (e.g. get_servers() throws on a concurrent
    # config write) — reset the one-shot flag on failure so a later tick
    # retries instead of waiting for the next recalc_hour.
    try:
        from baseline_engine import nightly_baseline_job
        servers = get_servers()
        count = nightly_baseline_job(db, lambda: servers, settings)
    except Exception:
        if do_catchup:
            _baseline_recalc_startup_checked = False
        logger.error("baseline_recalc: recalculation run failed", exc_info=True)
        return False

    # Replicate what POST /api/baselines/recalculate does on a manual click
    # (routes/api/metrics.py recalculate_baselines) so a scheduled run
    # behaves identically: flush the analytics rolling-mean/sigma cache too,
    # and leave an audit trail.
    try:
        from analytics import clear_baseline_cache
        clear_baseline_cache()
    except Exception:
        logger.debug("baseline_recalc: failed to clear analytics baseline cache", exc_info=True)

    try:
        db.log_audit(
            "system", "recalculate_baselines", "baselines",
            f"Scheduled recalculation: {count} baseline slots for {len(servers)} servers",
        )
    except Exception:
        logger.debug("baseline_recalc: failed to write audit log", exc_info=True)

    _baseline_recalc_last_run_date = today_str
    logger.info(
        "baseline recalc: %d servers, %d slots updated (trigger=%s)",
        len(servers), count, "catchup" if (do_catchup and not time_reached) else "scheduled",
    )
    return True


# ── Scheduled DB backup (feature 1.8) ────────────────────────────────────────

def run_scheduled_backup(db, settings, *, data_dir=None, backups_root=None, now=None):
    """Run one scheduled online DB backup.

    Reuses ``tools.backup.run()`` unchanged, into a per-run subdir
    ``data/backups/<ts>/``, then prunes whole subdirs to ``keep``. Persists the
    outcome to ``backup_state`` and fires a SINGLE deduped stale-backup event
    when the last success is older than ``stale_after_hours`` (re-armed on the
    next success). Re-raises on backup failure so the periodic loop logs it.
    """
    import shutil  # noqa: F401  (used by _rotate_backups)
    from datetime import datetime, timezone
    from pathlib import Path
    import tools.backup as _backup

    cfg = (settings or {}).get("database_backup", {})
    try:
        keep = int(cfg.get("keep", 14))
    except (TypeError, ValueError):
        keep = 14
    try:
        stale_after_h = float(cfg.get("stale_after_hours", 26))
    except (TypeError, ValueError):
        stale_after_h = 26.0
    severity = cfg.get("alert_severity", "warning")

    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if backups_root is not None:
        root = Path(backups_root)
    else:
        root = Path(__file__).resolve().parent.parent / "data" / "backups"
    root.mkdir(parents=True, exist_ok=True)

    # Deduped stale-backup alert — evaluated BEFORE this run so a persistently
    # failing/absent backup surfaces exactly once per staleness episode.
    _maybe_alert_stale_backup(db, now_dt, now_iso, stale_after_h, severity)

    subdir = root / now_dt.strftime("%Y%m%dT%H%M%SZ")
    subdir.mkdir(parents=True, exist_ok=True)
    try:
        # Pass the config path explicitly from the SINGLE SOURCE OF TRUTH rather
        # than letting backup.run() guess. It used to derive it as
        # data_dir/config.json, which is not where config.json lives, so every
        # run raised FileNotFoundError before writing anything.
        _cfg_path = None
        try:
            from config_manager import ConfigManager
            _cfg_path = ConfigManager().config_path
        except Exception:
            # Fall back to backup.run()'s own repo-root default.
            logger.debug("Could not resolve ConfigManager.config_path", exc_info=True)
        _backup.run(subdir, data_dir, config_path=_cfg_path)
    except Exception as e:
        db.set_backup_state(last_ok=0, last_error=str(e)[:500])
        logger.error("Scheduled DB backup failed: %s", e)
        # Remove the partial/empty subdir this run created so rotation (which
        # keeps the lexically-newest N) can't later evict a GOOD older backup
        # in favour of a failed run. Guarded to stay inside `root`.
        try:
            if subdir.is_dir() and root.resolve() in subdir.resolve().parents:
                shutil.rmtree(subdir, ignore_errors=True)
        except Exception:
            pass
        raise
    # Success — record it and clear error/alert state so staleness re-arms.
    db.set_backup_state(last_success_ts=now_iso, last_ok=1, last_path=str(subdir),
                        last_error="", last_alerted_ts="")
    _rotate_backups(root, keep)
    logger.info("Scheduled DB backup ok: %s", subdir)
    return subdir


def _maybe_alert_stale_backup(db, now_dt, now_iso, stale_after_h, severity):
    """Fire ONE 'backup_age' event when the last success is stale and we have
    not already alerted for this episode. No-op before the first-ever backup."""
    from datetime import datetime
    try:
        st = db.get_backup_state()
    except Exception:
        return
    if not st or not st.get("last_success_ts"):
        return  # never backed up yet → don't alarm pre-first-backup
    if st.get("last_alerted_ts"):
        return  # already alerted for this staleness episode
    try:
        dt = datetime.fromisoformat(str(st["last_success_ts"]).replace("Z", "+00:00"))
        age_h = (now_dt - dt).total_seconds() / 3600
    except Exception:
        return
    if age_h < stale_after_h:
        return
    msg = (f"Database backup is stale: last successful backup was {age_h:.1f}h "
           f"ago (threshold {stale_after_h:.0f}h).")
    try:
        db.insert_event("prism", severity, "backup_age", round(age_h, 1),
                        float(stale_after_h), msg)
    except Exception:
        logger.warning("Failed to insert stale-backup event", exc_info=True)
    try:
        db.set_backup_state(last_alerted_ts=now_iso)
    except Exception:
        pass


def _rotate_backups(root, keep):
    """Prune oldest backup subdirs, keeping the newest ``keep``. Whole-subdir
    rmtree, guarded to stay inside ``root``."""
    import shutil
    from pathlib import Path
    if not keep or keep <= 0:
        return
    root = Path(root)
    try:
        subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    except Exception:
        return
    for old in subdirs[:-keep]:
        try:
            if root.resolve() in old.resolve().parents:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            logger.warning("Backup rotation failed to prune %s", old, exc_info=True)


def _periodics_loop(get_servers, get_settings, db) -> None:
    """Run the registered jobs at their cadences. Bulletproof.

    The loop ticks every 30s — long enough to be cheap, short enough that
    the 60s `scheduled_reports` job runs roughly on time. Each due job is
    invoked inside its own try/except so a crash in one doesn't skip the
    others on the same tick.
    """
    global _critical_error_count
    jobs = _build_jobs(get_servers, get_settings, db)
    logger.info("Periodics loop started: %d jobs registered (%s)",
                len(jobs), ", ".join(j.name for j in jobs))

    while not _stop_event.is_set():
        try:
            now = time.time()
            for job in jobs:
                if not job.due(now):
                    continue
                try:
                    logger.debug("Running periodic job: %s", job.name)
                    t0 = time.time()
                    job.handler()
                    elapsed = time.time() - t0
                    with _lock:
                        _last_run[job.name] = now
                        # Recovered — drop any accumulated backoff so the job
                        # resumes its configured cadence on the next tick.
                        _consecutive_failures.pop(job.name, None)
                    if elapsed > 5.0:
                        logger.info("Periodic job %s took %.1fs", job.name, elapsed)
                except Exception:
                    # Record the attempt even though it failed, then back off.
                    #
                    # This used to leave _last_run untouched, so job.due() stayed
                    # permanently true and a FAILING job re-ran on EVERY 30s
                    # tick regardless of its configured cadence. Observed live:
                    # database_backup, configured at 24h, retrying at exactly
                    # 30-second intervals — 2,880 attempts a day instead of 1,
                    # and 116 of the 118 periodics errors in a two-hour sample.
                    #
                    # "Retry next tick" is right for a transient failure and
                    # catastrophic for a permanent one, and the hazard is not
                    # the backup job. The same loop runs ldap_probe, tls_certs
                    # and security_status: if one of those starts failing it
                    # hammers a domain controller or a remote host every 30s,
                    # which can look like an attack and can trip account
                    # lockout. The supervisor already backs off per-server
                    # failures; periodics simply never did.
                    with _lock:
                        fails = _consecutive_failures[job.name] = (
                            _consecutive_failures.get(job.name, 0) + 1)
                        # Exponential, capped at the job's own interval so a
                        # backed-off job can never become LESS frequent than
                        # its configured cadence.
                        delay = min(job.interval_s,
                                    _RETRY_BASE_S * (2 ** min(fails - 1, 6)))
                        # Pretend the run happened `interval - delay` ago, so
                        # the next attempt lands `delay` from now.
                        _last_run[job.name] = now - max(0.0, job.interval_s - delay)
                    logger.exception(
                        "Periodic job %s failed (attempt %d, next retry in %ds)",
                        job.name, fails, int(delay),
                    )
            _heartbeat()
        except Exception:
            _critical_error_count += 1
            logger.critical(
                "Periodics bulletproof catch fired (#%d) — recovering",
                _critical_error_count,
                exc_info=True,
            )
        # Wait one tick (interruptible by stop). Module-level so tests can
        # shrink it — the retry-backoff behaviour is otherwise untestable
        # without a 30s wait per tick.
        _stop_event.wait(_TICK_S)


_thread: threading.Thread | None = None


def start_periodics(get_servers, get_settings, db) -> None:
    """Spawn the periodics daemon thread. Idempotent — second call no-ops."""
    global _thread
    if _thread is not None and _thread.is_alive():
        logger.warning("start_periodics called twice — ignoring")
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_periodics_loop,
        args=(get_servers, get_settings, db),
        daemon=True,
        name="prism-collector-v2-periodics",
    )
    _thread.start()
    logger.info("Periodics thread started")


def stop_periodics() -> None:
    """Graceful stop. Mostly for tests / future restart-without-pid work."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
