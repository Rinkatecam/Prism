"""Aggregator thread for the v2 collector.

The aggregator is the LAST stage of the three-thread collector pipeline
documented in ``docs/COLLECTOR_V2_MIGRATION.md``. It owns no scheduling
and no WinRM — it just consumes Results emitted by the worker pool and
turns them into the same side-effects the legacy ``collector_loop`` used
to produce at end-of-cycle:

  * Persist metrics / logs to the DB (``db.insert_metric`` /
    ``db.insert_logs``).
  * Refresh the dashboard's hot cache via ``state.update_latest_metric``.
  * Carry the recently-added "transient_error / preserve previous good
    payload" semantics for the UPDATES check (collector.py:1305-1380).
  * Detect status transitions (healthy ⇄ warning/critical ⇄ offline) and
    emit one event per transition, with the same maintenance-window and
    alert-fatigue gates the v1 code uses.
  * Run the per-result baseline-deviation N-of-M check (same four gates
    as v1's collector.py:2308-2459, just per-result instead of per-cycle).
  * Tell the supervisor about success / failure via
    ``state.mark_check_completed`` so per-server backoff can apply.
  * Heartbeat the watchdog after every Result via
    ``state.heartbeat_aggregator``.

Design rules (cross-cutting from the plan):

  * Bulletproof outer try/except in ``_loop``. Per-handler inner
    try/except. ONE buggy server's Result must never block the whole
    queue.
  * Per-server state owned at module scope: ``_previous_status``,
    ``_cpu_warn_history`` (read-through from ``collector`` so v1 and v2
    share one ring), ``_baseline_dev_history``. The aggregator is the
    SOLE WRITER of these so no lock is needed for them inside the thread.
  * Late imports of ``collector`` and other heavyweight modules so this
    file is importable in tests without a Flask app or pypsrp.
  * Time-windowed event correlation (replaces v1's cycle-based
    ``cycle_events`` list) — see ``_recent_events`` deque + the 30 s
    rate-limited call to ``analytics.correlate_events``.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from . import state
from .types import CheckType, Result

logger = logging.getLogger("prism.collector_v2.aggregator")


# ─────────────────────────────────────────────────────────────────────────
# Module-level state owned by the aggregator
# ─────────────────────────────────────────────────────────────────────────
#
# The aggregator is the SOLE WRITER of every dict below. The aggregator
# thread is also the SOLE READER of these in the hot path; the watchdog
# only reads counters via ``get_aggregator_health`` (which uses a brief
# lock for internal consistency). We therefore do not need per-dict locks
# for the rings themselves — they live on the aggregator's local heap.

# previous_status: replaces the local `previous_status` dict that v1's
# ``collector_loop`` carried across iterations (collector.py:1696). It
# remembers the last status we OBSERVED per server so we can emit one
# event per transition.
_previous_status: dict[str, str] = {}

# Last classified reason a server was unreachable, keyed by server name.
# Recorded where result.error is in scope (the offline-row synthesis) and read
# where the offline EVENT is built, several layers up, which has no access to
# the original exception. Purely descriptive — nothing branches on it.
_last_unreachable_reason: dict[str, str] = {}

# Per-server, per-metric N-of-M ring for sustained-baseline-deviation
# gating. Same shape as the legacy ``_baseline_dev_history`` at
# collector.py:264, just owned here. We intentionally don't import the v1
# global because (a) it'd cross-couple the two collector engines during
# side-by-side validation, and (b) v2's ring advances per-result not
# per-cycle so v1 cadence assumptions don't apply directly.
_baseline_dev_history: dict[str, dict[str, collections.deque]] = {}

# Recent events for time-windowed correlation (plan §7 — replaces the v1
# per-cycle ``cycle_events`` list). Each entry is a dict in the v1 shape:
#   {"event_at": float (epoch), "server_name", "event_type", "metric",
#    "value", "threshold", "message", "event_id"?}
_recent_events: collections.deque = collections.deque(maxlen=200)

# Elevated-normal marker state (fused verdict): which metrics were flagged
# elevated on the previous sample, and when we last fired the audit INFO
# event per (server, metric). In-memory like _previous_status — a restart
# re-fires at most one info event per elevated metric, which is harmless.
_prev_elevated: dict[str, set] = {}
_elevated_info_last: dict[tuple[str, str], float] = {}
_ELEVATED_INFO_THROTTLE_S = 24 * 3600.0

# Last time correlation ran. Gated to once per 30 s so a busy aggregator
# doesn't run the (O(N²) over `_recent_events`) correlation logic on every
# tick. The plan calls for a "60-second window" — we implement that as the
# sliding window applied INSIDE the correlation function, while limiting
# the cadence of CALLING that function.
_last_correlation_check: float = 0.0
_CORRELATION_INTERVAL_S: float = 30.0
_CORRELATION_WINDOW_S: float = 60.0

# Aggregator-level counters, exposed via ``get_aggregator_health`` and
# ``/api/system/health``.
_stats_lock = threading.Lock()
_total_processed: int = 0
_total_offline_results: int = 0
_total_alerts_dispatched: int = 0
_total_critical_errors: int = 0


def _stats_inc(field: str, delta: int = 1) -> None:
    """Atomic increment of a module-level counter. Cheap enough to call
    from every hot path because the lock is uncontested (the aggregator
    is single-threaded and watchdog reads are O(1)).
    """
    global _total_processed, _total_offline_results
    global _total_alerts_dispatched, _total_critical_errors
    with _stats_lock:
        if field == "total_processed":
            _total_processed += delta
        elif field == "total_offline_results":
            _total_offline_results += delta
        elif field == "total_alerts_dispatched":
            _total_alerts_dispatched += delta
        elif field == "total_critical_errors":
            _total_critical_errors += delta


def get_aggregator_health() -> dict[str, Any]:
    """Read-only snapshot of aggregator runtime stats.

    Consumed by ``/api/system/health`` and the watchdog. The
    ``last_tick_s_ago`` field mirrors the value from ``state`` — we
    surface both so callers don't have to import two modules.
    """
    now = time.time()
    with _stats_lock:
        return {
            "total_processed": _total_processed,
            "total_offline_results": _total_offline_results,
            "total_alerts_dispatched": _total_alerts_dispatched,
            "total_critical_errors": _total_critical_errors,
            "last_tick_s_ago": (now - state.last_aggregator_tick)
            if state.last_aggregator_tick else None,
        }


# Map from internal metric short-name → human label used in event
# messages. Mirrors collector.py:1588 (METRIC_LABELS) — we copy it here so
# this module doesn't have a hard import dependency on v1 at module load.
_METRIC_LABELS: dict[str, str] = {
    "cpu": "CPU",
    "ram": "RAM",
    "disk_c": "Disk C:",
    "disk_d": "Disk D:",
}


# Poison pill the orchestrator pushes onto the result queue to wake a
# blocked _loop after stop() is called. We use None — the queue itself is
# typed ``queue.Queue[Result | None]``.
_POISON_PILL: Any = None


# ─────────────────────────────────────────────────────────────────────────
# Aggregator class
# ─────────────────────────────────────────────────────────────────────────


class Aggregator:
    """Daemon thread that drains the worker pool's result queue.

    Owns:
      * one daemon thread ``prism-collector-v2-aggregator``
      * the four module-level state dicts above (rings, counters)

    Does NOT own:
      * the result_queue (created by the orchestrator)
      * the supervisor (it updates ServerHealth indirectly via
        ``state.mark_check_completed``)
      * any worker thread

    The class is intentionally thin — almost all real work happens in the
    per-CheckType ``_handle_*`` methods so the loop stays short and easy
    to audit.
    """

    def __init__(
        self,
        result_queue: "queue.Queue[Result | None]",
        db: Any,
        get_settings: Callable[[], dict[str, Any]],
    ) -> None:
        """Wire the aggregator to its inputs.

        ``db`` is the live Database instance (same one the supervisor and
        the Flask app use). ``get_settings`` is a zero-arg callable so the
        operator can edit settings.json live and the aggregator picks up
        the change on the very next Result (no restart needed) — matches
        the supervisor's contract.
        """
        self.result_queue = result_queue
        self.db = db
        self.get_settings = get_settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the aggregator daemon thread.

        Idempotent: a second call while already running is a no-op + warn,
        not an error, so the watchdog can call us during recovery without
        worrying about state.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "Aggregator.start() called but thread already running"
            )
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="prism-collector-v2-aggregator",
            daemon=True,
        )
        self._thread.start()
        logger.info("Aggregator thread started")

    def stop(self, timeout: float = 10.0) -> None:
        """Graceful shutdown.

        Sets the stop event AND pushes a poison pill so a thread blocked
        on ``result_queue.get`` wakes promptly. We push the pill even
        though the loop has a 1.0 s timeout because that timeout is the
        worst-case latency — the pill makes shutdown effectively instant.
        """
        self._stop_event.set()
        try:
            self.result_queue.put_nowait(_POISON_PILL)
        except queue.Full:
            # Queue is bounded somewhere upstream — fall back to the
            # event check. Daemon threads die at process exit anyway.
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Aggregator thread did not exit within %.1fs; "
                    "daemon=True so process exit will clean it up.",
                    timeout,
                )

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Forever-loop. Pull one Result, process, repeat.

        The outer ``try/except`` is the bulletproof catch-all required by
        the migration plan (§"Cross-cutting / Error handling"). It is the
        SECOND line of defense — every ``_handle_*`` method has its own
        inner try/except so a bug processing server X cannot block
        processing server Y. This outer block only fires for true
        thread-level catastrophes (logic bugs in _process_result, queue
        anomalies, etc.).
        """
        logger.info("Aggregator loop entering main cycle")
        while not self._stop_event.is_set():
            try:
                result = self._pull_next()
                if result is None:
                    continue
                self._process_result(result)
                state.heartbeat_aggregator()
                self._maybe_run_correlation()
            except Exception:
                # Bulletproof: log CRITICAL + counter + tiny sleep to
                # avoid a tight error loop. NEVER die.
                _stats_inc("total_critical_errors")
                logger.critical(
                    "Aggregator bulletproof catch fired:\n%s",
                    traceback.format_exc(),
                )
                time.sleep(0.5)
        logger.info("Aggregator loop exiting (stop requested)")

    def _pull_next(self) -> Result | None:
        """Block (with 1 s timeout) on the queue.

        Returns None on empty-after-timeout OR on a poison-pill. The
        outer loop handles both the same way: re-check stop_event, sleep,
        loop. The timeout exists so a quiet queue eventually lets stop()
        take effect even if nobody pushed a pill.
        """
        try:
            result = self.result_queue.get(timeout=1.0)
        except queue.Empty:
            return None
        if result is _POISON_PILL:
            return None
        return result

    # ── Per-Result dispatch ───────────────────────────────────────────────

    def _process_result(self, result: Result) -> None:
        """Route one Result to the right handler.

        After the handler runs, ALWAYS update the supervisor's per-server
        bookkeeping via ``state.mark_check_completed`` — backoff math
        depends on it. Even if the handler raised internally, we still
        want the supervisor to see (ok=False) so the next tick applies
        backoff (handlers that fail catastrophically should re-raise to
        the outer bulletproof; handlers that fail "controlled" set their
        own ok=False semantics).
        """
        _stats_inc("total_processed")
        if not result.ok:
            _stats_inc("total_offline_results")

        ct = result.item.check_type
        server_name = result.item.server_name
        logger.debug("processing %s", result)

        try:
            if ct is CheckType.METRICS:
                self._handle_metrics_result(result)
            elif ct is CheckType.LOGS:
                self._handle_logs_result(result)
            elif ct is CheckType.UPDATES:
                self._handle_updates_result(result)
            elif ct is CheckType.HARDWARE:
                self._handle_hardware_result(result)
            else:
                logger.error(
                    "[%s] Unknown CheckType %r — Result dropped", server_name, ct
                )
        except Exception:
            logger.error(
                "[%s] Handler crashed for %s:\n%s",
                server_name, ct.value, traceback.format_exc(),
            )
        finally:
            # Notify supervisor LAST so backoff sees the final ok-state.
            try:
                state.mark_check_completed(
                    server_name, ct, result.ok, result.finished_at
                )
            except Exception:
                logger.error(
                    "[%s] mark_check_completed failed:\n%s",
                    server_name, traceback.format_exc(),
                )
            # Feed the topbar ECG widget. Defensive — pulse instrumentation
            # must never break aggregation, and the result object may have
            # exotic shapes (synthesized offline rows where duration_s is
            # None). Both record_pulse and the int() coercion are guarded.
            try:
                state.record_pulse(
                    result.finished_at.timestamp(),
                    server_name,
                    ct.value,
                    result.ok,
                    int((result.duration_s or 0) * 1000),
                )
            except Exception:
                logger.debug("record_pulse failed (non-fatal)", exc_info=True)

    # ── METRICS handler ───────────────────────────────────────────────────

    def _handle_metrics_result(self, result: Result) -> None:
        """Persist + transition + alert pipeline for one METRICS Result.

        This is the most complex handler — it ports the v1 per-result
        block at collector.py:1900-2084 to a per-Result world. The order
        of operations is preserved verbatim so v1 and v2 produce
        equivalent side-effects during side-by-side validation (plan
        § "Phase 4").
        """
        server_name = result.item.server_name
        server = self._lookup_server(server_name)
        settings = self.get_settings() or {}

        # ── Step (a)/(b): build the metrics row ──
        # Build the metrics row on success; otherwise synthesise a None
        # to drive the rest of the pipeline (status compute, transition
        # detection, offline event) with a consistent shape.
        if result.ok and result.data:
            metrics: dict[str, Any] | None = {
                "cpu": result.data.get("cpu"),
                "ram": result.data.get("ram"),
                "disk_c": result.data.get("disk_c"),
                "disk_d": result.data.get("disk_d"),
                "collection_time_ms": result.data.get("collection_time_ms"),
            }
        else:
            metrics = None

            # ── Preserve previous status on the FIRST transient failure ──
            # v1 had "one bad poll = offline" semantics, which made servers
            # flicker to offline on every WinRM blip (RPC stutter, a slow
            # session-open, a network microcut). With v2 owning the lifecycle
            # we can do better: when the previous metric row is recent and
            # this is the FIRST consecutive failure, keep showing the last
            # known status. The second failure flips to offline.
            #
            # Timeline of a real outage (say, a server actually going down):
            #   T+0    : last good poll, status=healthy
            #   T+60s  : poll #N times out → preserve healthy
            #   T+120s : poll #N+1 times out → flip to offline (consec_fail=1)
            #   T+180s : poll #N+2 times out → stays offline
            #
            # Timeline of a transient blip (WinRM session open took 35s):
            #   T+0    : last good poll, status=healthy
            #   T+60s  : poll #N times out → preserve healthy
            #   T+120s : poll #N+1 succeeds → back to healthy, no UI flicker
            #
            # The dashboard's existing ``is_stale`` badge (views.py) still
            # fires once the timestamp ages past poll_interval + 2 ×
            # cycle_timeout, so the operator isn't misled into thinking
            # the data is fresh — they just don't see the alarming "offline"
            # badge for a transient blip.
            try:
                health = state.server_health.get(server_name)
                prev_failures = 0
                if health is not None:
                    cs = health.checks.get(CheckType.METRICS)
                    if cs is not None:
                        prev_failures = cs.consecutive_failures
                prev_row = state.latest_by_server.get(server_name)
            except Exception:
                prev_failures = 0
                prev_row = None

            # "Recent" = within 5 minutes. Past that, preserving would be
            # misleading: a v1-style offline badge is the more honest signal.
            _RECENT_PRESERVE_S = 300

            def _row_is_recent(row: dict | None) -> bool:
                if not row:
                    return False
                ts_str = row.get("timestamp")
                if not ts_str:
                    return False
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - ts).total_seconds() <= _RECENT_PRESERVE_S
                except (ValueError, TypeError):
                    return False

            if (prev_failures == 0
                    and prev_row is not None
                    and prev_row.get("status") not in (None, "offline", "unknown")
                    and _row_is_recent(prev_row)):
                logger.info(
                    "[%s] metrics check failed (%s); preserving last known "
                    "status=%s (will flip to offline on the next failure). "
                    "Reason: %s",
                    server_name, result.error_kind,
                    prev_row.get("status"),
                    (result.error or "")[:120],
                )
                # Skip the rest of the pipeline. The supervisor's
                # mark_check_completed (called in the finally block of
                # _process_result) will increment consecutive_failures to 1
                # so the next failure flips through to offline. Dashboard's
                # ``is_stale`` flag still fires once the row ages past the
                # threshold — operator gets a "stale" badge instead of
                # "offline" for the transient window.
                return

            # Fall-through to offline-row synthesis.
            if result.error_kind == "offline":
                # Log the CLASSIFIED reason, not just a truncated error string.
                # This line used to be `(result.error or "")[:120]`, which cut
                # STANDALONE01's failure at exactly "(Ca" — the first two characters
                # of "Caused by NameResolutionError". The one substring that
                # identified the fault was the one the truncation removed, and
                # the host stayed mislabelled for a month.
                from .checks import classify_unreachable
                _reason = classify_unreachable(result.error or "")
                _last_unreachable_reason[server_name] = _reason
                logger.info(
                    "[%s] metrics offline (reason=%s) — synthesising offline row: %s",
                    server_name, _reason, (result.error or "")[:300],
                )
            else:
                logger.warning(
                    "[%s] metrics failed (%s): %s",
                    server_name, result.error_kind, (result.error or "")[:200],
                )

        # If the server was deleted from config between enqueue and
        # result, we still want to insert the offline row (so the
        # dashboard doesn't show stale-green) but we can't compute
        # status without thresholds. Use a minimal pseudo-server.
        if server is None:
            logger.info(
                "[%s] server missing from config; storing minimal row",
                server_name,
            )
            self.db.insert_metric(
                server_name=server_name,
                cpu=None, ram=None, disk_c=None, disk_d=None,
                status="offline",
                collection_time_ms=None,
            )
            return

        # ── Step (c): compute the fused verdict ──
        # The three-layer decision (exhaustion floors → static thresholds
        # modulated by baseline authority → deviation-from-self raises)
        # lives in detection.evaluate_server. One verdict per sample; the
        # status, the transition events, the baseline event stream and the
        # UI markers all derive from it so they agree by construction.
        evaluate_server = _get_evaluate_server_fn()
        verdict = evaluate_server(self.db, server, metrics, settings)
        status = verdict.status
        for _mv in verdict.metrics.values():
            if _mv.final_severity != _mv.static_severity or _mv.elevated_normal:
                logger.debug(
                    "[%s] %s fused: static=%s final=%s (%s)",
                    server_name, _mv.metric, _mv.static_severity,
                    _mv.final_severity, _mv.reason or "n-of-m gate",
                )

        # ── Step (d): persist metric row ──
        # v1 collector.py:1941-1949. Field-for-field compatible with the
        # legacy schema so existing endpoints reading metrics.* unchanged.
        self.db.insert_metric(
            server_name=server.name,
            cpu=(metrics.get("cpu") if metrics else None),
            ram=(metrics.get("ram") if metrics else None),
            disk_c=(metrics.get("disk_c") if metrics else None),
            disk_d=(metrics.get("disk_d") if metrics else None),
            status=status,
            collection_time_ms=(
                metrics.get("collection_time_ms") if metrics else None
            ),
        )

        # ── Step (e): refresh dashboard cache ──
        # v1 did this once per cycle at the end (collector.py:2492-2503);
        # in v2 we update the cache per-Result so the dashboard reflects
        # the most recent metric for each server immediately.
        row = {
            "server_name": server.name,
            "cpu_percent": metrics.get("cpu") if metrics else None,
            "ram_percent": metrics.get("ram") if metrics else None,
            "disk_c_percent": metrics.get("disk_c") if metrics else None,
            "disk_d_percent": metrics.get("disk_d") if metrics else None,
            "status": status,
            "collection_time_ms": (
                metrics.get("collection_time_ms") if metrics else None
            ),
            "timestamp": result.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Per-metric fused-verdict evidence for the UI (elevated-normal
            # markers + reasons). Cache-only — DB rows don't carry it, so
            # consumers must treat it as optional (plan §8 data contract).
            "verdict_detail": verdict.detail(),
        }
        state.update_latest_metric(server.name, row)

        # ── Step (e.2): elevated-normal entry markers ──
        # One visible INFO event when a metric first goes "statically
        # breached but baseline vouches" — so the downgrade is auditable
        # in the event feed without paging anyone. Throttled 24 h.
        try:
            self._handle_elevated_markers(server, verdict)
        except Exception:
            logger.debug("elevated-marker hook failed for %s",
                         server.name, exc_info=True)

        # ── Step (e.5): post-reboot lifecycle ──
        # If this server was tracked as ``rebooting`` (kicked off by an
        # update install + auto-restart, or a manual restart), a real
        # metrics arrival means it's back up. Transition the install_state
        # to ``stabilising`` for a 60 s window so the dashboard shows a
        # distinct "settling in" state, then clear it. We only fire on
        # successful metrics; an "offline"-row arrival here would just
        # mean WinRM is still flapping during the reboot.
        if metrics is not None:
            try:
                self._handle_post_reboot(server.name)
            except Exception:
                logger.debug("post-reboot hook failed for %s", server.name, exc_info=True)

        # ── Step (f-h): status transition + event + alert dispatch ──
        # All gated together because they share the maintenance/fatigue
        # gates from v1 collector.py:1962-2083.
        self._handle_status_transition(server, metrics, status, settings, verdict)

        # ── Step (k): baseline deviation N-of-M ──
        # Only run when we have real metrics — there's nothing to
        # baseline-check against a synthesised offline row. Consumes the
        # verdict's per-metric deviations (computed once in step c) so
        # the event stream and the status can never disagree about what
        # deviated this sample.
        if metrics is not None and metrics.get("cpu") is not None:
            try:
                self._run_baseline_deviation(server, metrics, settings, verdict)
            except Exception:
                # Baseline checks are bug-prone (settings drift, missing
                # baselines, etc.). Never let them block alert dispatch.
                logger.error(
                    "[%s] Baseline deviation check failed:\n%s",
                    server.name, traceback.format_exc(),
                )

        # ── Step (l): anomaly + rate-anomaly events (H4 audit fix) ──
        # v1 ran statistical anomaly detection + rate-of-change detection
        # at every Nth cycle and fired `anomaly`/`rate_anomaly` events
        # into the events table when detection succeeded + suppression
        # passed. v2 was missing this entirely. We delegate to a v2
        # helper in collector.py that ports the same logic. Same gating
        # (anomaly_detection.enabled + low_side_only_when_baseline_on +
        # CPU N-of-M + suppression windows + ack/snooze) — see
        # detection.dispatch_anomaly_events_v2 docstring.
        if metrics is not None and metrics.get("cpu") is not None:
            try:
                from detection import dispatch_anomaly_events_v2
                dispatch_anomaly_events_v2(self.db, server, metrics, settings)
            except Exception:
                logger.error(
                    "[%s] v2 anomaly dispatch failed:\n%s",
                    server.name, traceback.format_exc(),
                )

    # Stabilising window — after a server's metrics start flowing again
    # post-reboot, we keep the install_state alive briefly to render a
    # "Stabilising" badge instead of going directly back to whatever the
    # threshold-based status says. Long enough for the operator to see it,
    # short enough that operators stop wondering "why does this server
    # still say stabilising 10 minutes after it came back".
    _STABILISING_WINDOW_S = 60

    def _handle_post_reboot(self, server_name: str) -> None:
        """Lifecycle handler called when a real metrics sample arrives.

        Two transitions matter here:

        1. ``rebooting`` → ``stabilising`` (came back online)
           First successful metrics after we marked the server rebooting.
           We bump the row to ``stabilising`` with a 60 s window. Re-arm
           acceleration so the next minute of polling stays tight.

        2. ``stabilising`` → cleared
           Either the 60 s window elapsed OR enough time has passed since
           ``came_back_at`` that we trust the server. Pop the row so the
           dashboard reverts to the normal metric-based badge.

        States other than ``rebooting`` / ``stabilising`` are ignored —
        a server that's currently ``installing`` should keep showing
        installing even if metrics happen to come in mid-install.
        """
        try:
            from routes.api._shared import (
                _update_install_state,
                _persist_install_state,
            )
        except Exception:
            return  # routes blueprint not loaded (e.g. unit-test env)
        cur = _update_install_state.get(server_name)
        if not cur:
            return
        status_now = cur.get("status")
        if status_now == "rebooting":
            iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _update_install_state[server_name] = {
                **cur,
                "status": "stabilising",
                "message": "Server is back online; settling.",
                "updated_at": iso_now,
                "came_back_at": iso_now,
            }
            _persist_install_state()
            logger.info(
                "[%s] reboot complete — transitioning to stabilising",
                server_name,
            )
            # Keep polling tight during the stabilisation window.
            try:
                from .supervisor import accelerate_server as _accel
                _accel(server_name, duration_s=self._STABILISING_WINDOW_S, reason="post_reboot")
            except Exception:
                logger.debug("[%s] post-reboot acceleration failed", server_name, exc_info=True)
        elif status_now == "stabilising":
            came_back_at_str = cur.get("came_back_at")
            if not came_back_at_str:
                # Defensive: stabilising row without a timestamp is a bug
                # elsewhere; pop it so it doesn't get stuck.
                _update_install_state.pop(server_name, None)
                _persist_install_state()
                return
            try:
                came_back = datetime.fromisoformat(came_back_at_str.replace("Z", "+00:00"))
                if came_back.tzinfo is None:
                    came_back = came_back.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                _update_install_state.pop(server_name, None)
                _persist_install_state()
                return
            elapsed = (datetime.now(timezone.utc) - came_back).total_seconds()
            if elapsed >= self._STABILISING_WINDOW_S:
                _update_install_state.pop(server_name, None)
                _persist_install_state()
                logger.info("[%s] stabilising window expired — install_state cleared", server_name)
                # Force an immediate UPDATES recheck so the "pending reboot"
                # flag is re-evaluated within seconds, not up to 30 min later
                # (the normal UPDATES cadence). Microsoft's COM API tells us
                # whether Windows STILL needs a reboot after the one we just
                # did — common with multi-stage patch sets or stuck CBS
                # transactions. Catching this quickly closes the gap where
                # the operator sees "all clear" but the server is in fact
                # still waiting on a second reboot.
                #
                # We push next_due_at to "now" rather than calling
                # accelerate_server() because we want ONE forced check, not
                # another acceleration window. The supervisor will pick it up
                # on its next 5 s tick, the worker will run PS_CHECK_UPDATES,
                # and the aggregator will surface pending_reboot=True via the
                # normal dashboard path if Windows is still flagged.
                try:
                    self._force_updates_check(server_name)
                except Exception:
                    logger.debug(
                        "[%s] failed to force post-reboot UPDATES recheck",
                        server_name, exc_info=True,
                    )

    def _force_updates_check(self, server_name: str) -> None:
        """Schedule a single UPDATES check for ``server_name`` ASAP.

        Sets that server's UPDATES ``next_due_at`` to "now" so the next
        supervisor tick will enqueue the check. This is the surgical
        equivalent of "Sync Updates Now" but for a single server, without
        extending the acceleration window.

        Safe to call when the server isn't tracked yet — silently no-ops.
        """
        from .types import CheckType
        now = datetime.now(timezone.utc)
        with state._server_health_lock:
            h = state.server_health.get(server_name)
            if h is None:
                return
            cs = h.checks.get(CheckType.UPDATES)
            if cs is None:
                return
            # Don't clobber a check that's already pending — let the in-flight
            # one complete; its result will be the fresh one we wanted anyway.
            if cs.pending:
                return
            cs.next_due_at = now
        logger.info(
            "[%s] forced post-reboot UPDATES recheck — next supervisor tick",
            server_name,
        )

    def _handle_status_transition(
        self,
        server: Any,
        metrics: dict[str, Any] | None,
        status: str,
        settings: dict[str, Any],
        verdict: Any = None,
    ) -> None:
        """Detect status transition, emit event, dispatch alerts.

        Mirrors collector.py:1962-2083 (the post-result event block) but
        operates per-Result rather than per-cycle. The maintenance gate
        STILL updates _previous_status so that when the window ends the
        "resolved" transition fires correctly — v1 has the same
        behaviour at collector.py:1968-1971.

        Post-fusion: the old "smart detector owns warnings" suppression
        (which silenced ALL threshold WARNING events whenever baseline or
        anomaly detection was enabled — status yellow, event stream mute)
        is gone. The fused verdict is the single owner. Gating rules:
          * ``thresholds.enabled`` gates only SIMPLE static-threshold
            warning/critical events (the operator's master switch).
          * Exhaustion-floor criticals ALWAYS fire — a resource actually
            running out is a hard truth no switch may silence.
          * A PURE deviation-from-self raise fires no threshold event here;
            the baseline_deviation path (step k) owns it, so emitting one
            too would just duplicate the row.
          * Offline is an unreachability signal, never threshold-derived —
            it always fires (transition AND first-poll).
        """
        prev = _previous_status.get(server.name)

        # Maintenance gate — applies BEFORE event dispatch (collector.py:1966)
        maint_suppressed = _is_alert_suppressed_by_maintenance(
            server.name, settings,
        )

        thresholds_enabled = settings.get("thresholds", {}).get("enabled", True)

        # The metric that should drive a threshold event (static breach or
        # exhaustion floor); None when the status is purely deviation-driven.
        worst_mv = (
            verdict.threshold_worst(status)
            if verdict is not None and hasattr(verdict, "threshold_worst")
            else None
        )
        # Simple thresholds honour the master switch; floors bypass it.
        fire_threshold = worst_mv is not None and (
            thresholds_enabled or getattr(worst_mv, "is_floor", False)
        )

        event_info: tuple | None = None  # populated below if we emit

        if maint_suppressed:
            # Still track prev so resolved fires post-window. Skip all
            # else (no event, no alert).
            _previous_status[server.name] = status
            return

        if prev is not None and prev != status:
            if status in ("critical", "warning") and fire_threshold:
                event_info = self._fire_threshold_change(server, metrics, status, worst_mv)
            elif status == "healthy" and prev in ("critical", "warning"):
                event_info = self._fire_resolved(server, prev)
            elif status == "offline":
                event_info = self._fire_offline(server)
        elif prev is None and status != "healthy":
            # First-collection-and-already-bad. v1 collector.py:2028-2039.
            if status in ("critical", "warning") and fire_threshold:
                event_info = self._fire_threshold_change(server, metrics, status, worst_mv)
            elif status == "offline":
                # Offline is not threshold-gated — an unreachable server on
                # its very first poll must still page.
                event_info = self._fire_offline(server)

        # ── Alert dispatch (with fatigue gate) ──
        if event_info is not None:
            self._dispatch_alert(server, event_info, settings)

        _previous_status[server.name] = status

    def _fire_threshold_change(
        self, server: Any, metrics: dict[str, Any] | None, status: str,
        worst_mv: Any = None,
    ) -> tuple:
        """Fire a status-driven critical/warning event. Returns event_info.

        v1 equivalent: collector.py:1980-1993.

        Post-fusion the caller (_handle_status_transition) pre-selects the
        driving MetricVerdict (``worst_mv``) — the static/floor breach most
        over its threshold — so the event cites the right metric + reason
        (exhaustion floor / static threshold). ``worst_mv`` is None only for
        verdict-less callers (tests, v1 shims), which fall back to the legacy
        ``_get_worst_metric`` raw-threshold walk.
        """
        metric_name = value = threshold = None
        msg = ""
        if worst_mv is not None:
            metric_name = worst_mv.metric
            value = worst_mv.value
            threshold = worst_mv.threshold_used
            msg = worst_mv.reason
        if metric_name is None:
            get_worst_metric = _get_worst_metric_fn()
            metric_name, value, threshold = get_worst_metric(metrics, server.thresholds)
        if not msg:
            label = _METRIC_LABELS.get(metric_name, metric_name or "unknown")
            if value:
                msg = f"{label} exceeded {threshold}% ({value}%)"
            else:
                msg = f"Server is {status}"
        self.db.insert_event(server.name, status, metric_name, value, threshold, msg)
        try:
            _update_score_on_fire(self.db, server.name, metric_name or "", status)
        except Exception:
            logger.debug("Alert scoring failed for %s", server.name, exc_info=True)
        self._append_recent_event({
            "server_name": server.name,
            "event_type": status,
            "metric": metric_name,
            "value": value,
            "threshold": threshold,
            "message": msg,
        })
        logger.info("[%s] Status transition fired: %s (%s)", server.name, status, msg)
        return (status, metric_name, value, threshold, msg)

    def _fire_resolved(self, server: Any, prev: str) -> tuple:
        """Fire a 'resolved' event. v1: collector.py:1994-2002."""
        msg = f"Server recovered from {prev} to healthy"
        self.db.insert_event(server.name, "resolved", None, None, None, msg)
        self._append_recent_event({
            "server_name": server.name,
            "event_type": "resolved",
            "metric": None, "value": None, "threshold": None,
            "message": msg,
        })
        logger.info("[%s] Resolved event fired (was %s)", server.name, prev)
        return ("resolved", None, None, None, msg)

    def _fire_offline(self, server: Any) -> tuple:
        """Fire an 'offline' event. v1: collector.py:2015-2027.

        NOTE: only fires on the TRANSITION (prev != offline). The caller
        handles that gate; we just emit the event when called.
        """
        # Say WHY, not just "unreachable". Those are different problems with
        # different responses: a name that does not resolve is a DNS or
        # domain-membership issue on a host that may be perfectly healthy, a
        # refusal means the host is up but WinRM is not listening, and a
        # timeout means packets are being dropped — usually a firewall. Two
        # incidents on this fleet were mislabelled for days and for a month
        # respectively because all three read identically as "offline".
        from .checks import REASON_TEXT
        reason = _last_unreachable_reason.get(server.name, "unknown")
        detail = REASON_TEXT.get(reason, REASON_TEXT["unknown"])
        msg = f"Server is unreachable — {detail}"
        self.db.insert_event(server.name, "offline", None, None, None, msg)
        try:
            _update_score_on_fire(self.db, server.name, "", "offline")
        except Exception:
            logger.debug("Alert scoring failed for %s", server.name, exc_info=True)
        self._append_recent_event({
            "server_name": server.name,
            "event_type": "offline",
            "metric": None, "value": None, "threshold": None,
            "message": msg,
        })
        logger.info("[%s] Offline event fired", server.name)
        return ("offline", None, None, None, msg)

    def _dispatch_alert(
        self, server: Any, event_info: tuple, settings: dict[str, Any],
    ) -> None:
        """Send email + Teams webhook for an event_info tuple.

        Applies the alert-fatigue gate (one lookup per channel) and the
        webhooks-enabled gate. v1 equivalent: collector.py:2046-2082.
        """
        evt_type, evt_metric, evt_value, evt_threshold, evt_msg = event_info

        # Fatigue gate — one per channel. v1 calls both channels with the
        # same args (collector.py:2048-2049).
        is_throttled = _is_throttled_by_fatigue_fn()
        try:
            fatigue_email = is_throttled(
                self.db, server.name, evt_metric or "", evt_type, settings,
                channel="email",
            )
        except Exception:
            fatigue_email = False
        try:
            fatigue_webhook = is_throttled(
                self.db, server.name, evt_metric or "", evt_type, settings,
                channel="webhook",
            )
        except Exception:
            fatigue_webhook = False

        # Repeat-interval throttle (feature 1.1) — one per channel. Bounds how
        # often a recurring alert re-notifies once we've already sent; resolved
        # events always pass. Failure biases toward sending (True).
        should_repeat = _should_send_repeat_fn()
        try:
            repeat_email = should_repeat(
                self.db, server.name, evt_metric or "", evt_type, settings,
                channel="email",
            )
        except Exception:
            repeat_email = True
        try:
            repeat_webhook = should_repeat(
                self.db, server.name, evt_metric or "", evt_type, settings,
                channel="webhook",
            )
        except Exception:
            repeat_webhook = True

        # ── Email ──
        send_email = _send_alert_email_fn()
        should_email = _should_send_email_fn()
        if should_email(evt_type, settings) and not fatigue_email and repeat_email:
            try:
                event = {
                    "event_type": evt_type,
                    "metric": evt_metric,
                    "value": evt_value,
                    "threshold": evt_threshold,
                    "message": evt_msg,
                }
                # send_alert_email returns True/False and NEVER raises on a
                # delivery failure, so only stamp the repeat throttle on a
                # CONFIRMED send — otherwise a transient SMTP failure would
                # silently suppress the recurring alert for repeat_interval_hours.
                if send_email(event, server.name, settings):
                    try:
                        self.db.mark_alert_sent(
                            server.name, evt_metric or "", evt_type, "email"
                        )
                    except Exception:
                        pass
                    _stats_inc("total_alerts_dispatched")
                    logger.info(
                        "[%s] Alert email dispatched for %s",
                        server.name, evt_type,
                    )
                else:
                    logger.warning(
                        "[%s] Alert email NOT delivered for %s — will retry next cycle",
                        server.name, evt_type,
                    )
            except Exception:
                logger.error(
                    "[%s] Failed to send alert email:\n%s",
                    server.name, traceback.format_exc(),
                )

        # ── Teams webhook ──
        try:
            webhook_cfg = settings.get("webhooks", {}) or {}
            url = webhook_cfg.get("teams_webhook_url")
            enabled = webhook_cfg.get("enabled")
            if enabled and url and not fatigue_webhook and repeat_webhook:
                should_send = False
                if evt_type == "critical" and webhook_cfg.get("send_on_critical", True):
                    should_send = True
                elif evt_type == "warning" and webhook_cfg.get("send_on_warning", False):
                    should_send = True
                if should_send:
                    send_webhook = _send_teams_webhook_fn()
                    res = send_webhook(
                        url, server.name, evt_type, evt_metric,
                        evt_value, evt_threshold, evt_msg, settings,
                    )
                    # send_teams_webhook returns {"ok": bool} (truthy even on
                    # failure), so inspect the dict — only stamp on confirmed
                    # delivery, matching the email path.
                    ok = res.get("ok") if isinstance(res, dict) else bool(res)
                    if ok:
                        try:
                            self.db.mark_alert_sent(
                                server.name, evt_metric or "", evt_type, "webhook"
                            )
                        except Exception:
                            pass
                        logger.info(
                            "[%s] Teams webhook dispatched for %s",
                            server.name, evt_type,
                        )
                    else:
                        logger.warning(
                            "[%s] Teams webhook NOT delivered for %s: %s",
                            server.name, evt_type,
                            (res.get("error") if isinstance(res, dict) else res),
                        )
        except Exception as e:
            logger.warning("[%s] Teams webhook failed: %s", server.name, e)

    # ── Elevated-normal markers ───────────────────────────────────────────

    def _handle_elevated_markers(self, server: Any, verdict: Any) -> None:
        """Audit-trail INFO event when a metric ENTERS elevated-normal.

        "Elevated-normal" = statically over threshold but downgraded to
        healthy because the baseline (with authority) vouches for it —
        e.g. a SQL box whose learned normal is ~93% RAM. The downgrade
        must stay visible somewhere beyond the card marker, so the first
        entry per (server, metric) logs one info event. No email/webhook,
        not fed to correlation, throttled to one per 24 h.
        """
        current = verdict.elevated_metrics() if verdict is not None else set()
        prev = _prev_elevated.get(server.name, set())
        now = time.time()
        for metric in current - prev:
            key = (server.name, metric)
            last = _elevated_info_last.get(key, 0.0)
            if now - last < _ELEVATED_INFO_THROTTLE_S:
                continue
            mv = verdict.metrics.get(metric)
            if mv is None:
                continue
            self.db.insert_event(
                server.name, "info", metric, mv.value, mv.threshold_used,
                mv.reason or f"{metric} elevated but normal for this server",
            )
            _elevated_info_last[key] = now
            logger.info("[%s] elevated-normal marker: %s (%s)",
                        server.name, metric, mv.reason)
        _prev_elevated[server.name] = current

    # ── Baseline deviation N-of-M ─────────────────────────────────────────

    def _run_baseline_deviation(
        self,
        server: Any,
        metrics: dict[str, Any],
        settings: dict[str, Any],
        verdict: Any = None,
    ) -> None:
        """Per-result baseline-deviation check with the four v1 gates.

        Ports collector.py:2308-2459 — four anti-noise gates:
          1. Severity cap (baseline can't exceed server's critical thr).
          2. N-of-M sustained gating.
          3. Acknowledgment/snooze respect.
          4. Suppression window + re-alert delta.

        Difference from v1: the ring advances per-Result (per-server),
        not per-cycle. The N-of-M counts are interpreted as "the last N
        polls for THIS server" rather than "the last N cycles for the
        whole fleet" — equivalent because each server gets one poll per
        cycle in v1 too.
        """
        baseline_cfg = settings.get("baseline_detection", {}) or {}
        if not baseline_cfg.get("enabled", True):
            return

        # Post-fusion: consume the deviations the verdict already computed
        # (detection.evaluate_server → baseline_engine.assess_metrics) so
        # the event stream can never disagree with the status about what
        # deviated this sample. Verdict-less callers (tests, v1 shims)
        # fall back to running check_deviation directly — same math.
        if verdict is not None and hasattr(verdict, "deviations"):
            devs = verdict.deviations()
        else:
            check_deviation = _check_deviation_fn()
            if check_deviation is None:
                return  # baseline_engine missing — skip silently
            bm = {
                "cpu_percent": metrics.get("cpu"),
                "ram_percent": metrics.get("ram"),
                "disk_c_percent": metrics.get("disk_c"),
                "disk_d_percent": metrics.get("disk_d"),
            }
            devs = check_deviation(
                self.db, server.name, bm,
                settings.get("timezone", "Europe/Berlin"),
                baseline_cfg.get("sigma_warning", 2.0),
                baseline_cfg.get("sigma_critical", 3.0),
                baseline_cfg.get("min_samples", 10),
            )

        bl_supp_hours = baseline_cfg.get("suppression_hours", 4)
        bl_min_warn = baseline_cfg.get("min_cycles_warning", 3)
        bl_min_crit = baseline_cfg.get("min_cycles_critical", 2)
        bl_re_alert_delta = baseline_cfg.get("re_alert_delta", 2.0)
        sev_rank = {"healthy": 0, "warning": 1, "critical": 2}

        thr_for_cap = (server.thresholds or {})
        try:
            acks = self.db.get_active_acknowledgments(server.name)
            acked_metrics = {
                a.get("metric") for a in acks
                if a.get("ack_type") in ("acknowledged", "snoozed")
            }
        except Exception:
            acked_metrics = set()

        deviated_metrics: set[str] = set()

        for d in devs:
            severity = d["severity"]
            metric_name = d.get("metric")
            metric_val = float(d.get("value") or 0)

            # ── GATE 1: Severity cap (v1 collector.py:2347-2355) ──
            crit_thr_key = {
                "cpu": "cpu_critical", "ram": "ram_critical",
                "disk_c": "disk_critical", "disk_d": "disk_critical",
            }.get(metric_name)
            if severity == "critical" and crit_thr_key:
                crit_thr = thr_for_cap.get(crit_thr_key, 90)
                if metric_val < crit_thr:
                    severity = "warning"

            # ── GATE 2: N-of-M sustained (v1 collector.py:2357-2366) ──
            bl_hist = _baseline_dev_history.setdefault(server.name, {})
            ring = bl_hist.setdefault(metric_name, collections.deque(maxlen=5))
            ring.append(True)
            deviated_metrics.add(metric_name)
            sustained = sum(1 for x in ring if x)
            if severity == "warning" and sustained < bl_min_warn:
                continue
            if severity == "critical" and sustained < bl_min_crit:
                continue

            # ── GATE 3: Acknowledgment / snooze (v1:2368-2370) ──
            if metric_name in acked_metrics:
                continue

            # ── GATE 4: Suppression window + re-alert delta (v1:2372-2397) ──
            try:
                supp = self.db.get_anomaly_suppression(
                    server.name, metric_name, direction="baseline",
                )
                if supp and supp.get("last_alert_time"):
                    last_t = datetime.strptime(
                        supp["last_alert_time"], "%Y-%m-%dT%H:%M:%SZ",
                    ).replace(tzinfo=timezone.utc)
                    hours_since = (
                        (datetime.now(timezone.utc) - last_t).total_seconds() / 3600
                    )
                    last_sev_rank = sev_rank.get(supp.get("last_severity", ""), 0)
                    this_sev_rank = sev_rank.get(severity, 0)
                    val_delta = abs(metric_val - float(supp.get("last_value") or 0))
                    if hours_since < bl_supp_hours:
                        if this_sev_rank <= last_sev_rank:
                            continue  # not escalating — suppress
                    elif val_delta < bl_re_alert_delta:
                        self.db.upsert_anomaly_suppression(
                            server.name, metric_name, "baseline",
                            severity, metric_val,
                        )
                        continue
            except Exception:
                pass  # any suppression-check error → fire the event

            # ── All gates passed — fire ──
            msg = (
                f"Baseline deviation: {d['metric']} is {d['value']:.1f}% "
                f"(normal: {d['baseline_avg']:.1f}% ± {d['baseline_stddev']:.1f})"
            )
            self.db.insert_event(
                server.name, severity, "baseline_deviation",
                d["value"], d["baseline_avg"], msg,
            )
            try:
                self.db.upsert_anomaly_suppression(
                    server.name, metric_name, "baseline",
                    severity, metric_val,
                )
            except Exception:
                pass
            logger.info(
                "[%s] Baseline deviation detected: %s sev=%s",
                server.name, metric_name, severity,
            )

            # Email + webhook (same fatigue gate honoured by reusing
            # the threshold dispatch path).
            self._dispatch_alert(
                server,
                (severity, d["metric"], d["value"], d["baseline_avg"], msg),
                settings,
            )

        # Record False for metrics that were checked but didn't deviate —
        # keeps the N-of-M ring accurate (v1 collector.py:2451-2456).
        bl_hist = _baseline_dev_history.setdefault(server.name, {})
        for mn in ("cpu", "ram", "disk_c", "disk_d"):
            if mn not in deviated_metrics and mn in bl_hist:
                bl_hist[mn].append(False)

    # ── LOGS handler ──────────────────────────────────────────────────────

    def _handle_logs_result(self, result: Result) -> None:
        """Persist a logs payload. Failures are dropped (no row written).

        v1 stored logs only on the metrics-collection path
        (collector.py:1952-1953) — in v2 logs are their own check_type
        so we get a dedicated Result. The persistence call is identical;
        only the trigger moved.
        """
        server_name = result.item.server_name
        if result.ok and isinstance(result.data, list):
            try:
                # Pass the ingest controls so Information-level noise is dropped
                # and identical lines are coalesced into log_signatures. See
                # Database.insert_logs — logs were 96% of all rows.
                self.db.insert_logs(
                    server_name, result.data,
                    ingest_cfg=(self.get_settings() or {}).get("log_ingest"),
                )
            except Exception:
                logger.error(
                    "[%s] insert_logs failed:\n%s",
                    server_name, traceback.format_exc(),
                )
            return

        if result.error_kind == "offline":
            logger.info(
                "[%s] logs skipped — target offline (%s)",
                server_name, (result.error or "")[:120],
            )
        else:
            logger.warning(
                "[%s] logs failed (%s): %s",
                server_name, result.error_kind, (result.error or "")[:200],
            )

    # ── UPDATES handler ───────────────────────────────────────────────────

    def _handle_updates_result(self, result: Result) -> None:
        """Persist Windows Update info with the shutdown-noise filter.

        Ports the v1 behaviour at collector.py:1305-1380. Critical: when
        the check failed for an offline-class reason, we PRESERVE the
        previous good payload (count/updates/reboot_required) and only
        bump checked_at + transient_error. This avoids the dashboard
        flipping "23 updates pending" → "Check failed" → "23 updates
        pending" during a reboot cycle (the UI's red banner is keyed off
        ``error`` so we explicitly set it to None).
        """
        server_name = result.item.server_name
        checked_at = result.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        if result.ok and isinstance(result.data, dict):
            pending_reboot = bool(result.data.get("pending_reboot"))
            state.update_server_update_info(server_name, {
                "count": int(result.data.get("count") or 0),
                "updates": result.data.get("updates") or [],
                "reboot_required": bool(result.data.get("reboot_required")),
                "pending_reboot": pending_reboot,
                "error": result.data.get("error"),
                "checked_at": checked_at,
            })
            # Auto-clear stale install_state on a fresh "no pending reboot"
            # — the install must have completed + the server must have
            # been rebooted (by us, the operator, or anyone). Mirrors the
            # logic in /api/servers/<name>/updates so the dashboard
            # doesn't have to be open for the cleanup to happen. Without
            # this, a stale ``update-status.json`` left over on a server
            # that's been rebooted manually keeps re-arming install_state
            # every time someone visits the server detail page — even
            # though the live Windows COM query says no reboot needed.
            if not pending_reboot:
                try:
                    from routes.api._shared import (
                        _update_install_state, _persist_install_state,
                    )
                    cur = _update_install_state.get(server_name) or {}
                    # Only pop the operator-blocking states. Don't touch
                    # ``installing`` / ``downloading`` etc. — an active
                    # install legitimately needs to keep its lifecycle row
                    # even if pending_reboot is briefly False between
                    # phases. ``rebooting`` and ``stabilising`` are
                    # Prism-owned transient states that the aggregator's
                    # own handlers manage (_handle_post_reboot etc.) —
                    # don't race with them here.
                    if cur.get("status") in ("restart_required", "completed", "failed"):
                        _update_install_state.pop(server_name, None)
                        _persist_install_state()
                        logger.info(
                            "[%s] auto-cleared stale install_state "
                            "(status=%s) — UPDATES check confirms no "
                            "pending reboot",
                            server_name, cur.get("status"),
                        )
                except Exception:
                    logger.debug(
                        "[%s] auto-clear of stale install_state failed",
                        server_name, exc_info=True,
                    )
            return

        # Treat both ``offline`` (target rebooting / unreachable / WSMan
        # session torn down mid-call) AND ``timeout`` (worker deadline
        # exceeded — usually a slow WU query during patch Tuesday) as
        # transient. Preserve the previous good payload so the dashboard
        # doesn't flip "23 updates pending" → "Update check failed" →
        # "23 updates pending" between cycles. Real failures (auth
        # rejected, PowerShell exception, malformed JSON, etc.) still
        # fall through to the explicit error path below.
        _TRANSIENT_KINDS = ("offline", "timeout")
        if result.error_kind in _TRANSIENT_KINDS:
            prev = state.server_update_info.get(server_name) or {}
            reason = (
                "server_rebooting_or_unreachable"
                if result.error_kind == "offline"
                else "wu_query_exceeded_deadline"
            )
            state.update_server_update_info(server_name, {
                **prev,
                "checked_at": checked_at,
                "transient_error": True,
                "transient_error_reason": reason,
                "error": None,
            })
            logger.info(
                "[%s] Update check transient-failed (%s): %s",
                server_name, result.error_kind, (result.error or "")[:120],
            )
            return

        # Real failure — overwrite with an error payload so the UI shows
        # "Check failed" instead of stale data.
        state.update_server_update_info(server_name, {
            "count": 0,
            "updates": [],
            "reboot_required": False,
            "pending_reboot": False,
            "error": (result.error or "Update check failed")[:200],
            "checked_at": checked_at,
        })
        logger.warning(
            "[%s] Update check failed (%s): %s",
            server_name, result.error_kind, (result.error or "")[:200],
        )

    # ── HARDWARE handler ──────────────────────────────────────────────────

    def _handle_hardware_result(self, result: Result) -> None:
        """Persist hardware inventory. Failures are STICKY — keep old data.

        Hardware doesn't change between polls (well — disk sizes do, but
        rarely). On a failed check we'd rather show last-known good data
        than wipe the inventory and force the dashboard to render
        "Unknown CPU / Unknown RAM" during a reboot. v1 has the same
        behaviour at collector.py:1397-1402.
        """
        server_name = result.item.server_name
        if result.ok and isinstance(result.data, dict):
            payload = dict(result.data)
            payload["collected_at"] = result.finished_at.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            state.update_server_hardware_info(server_name, payload)
            return

        # Failure — keep the existing entry. INFO for offline, WARNING
        # otherwise (parallels logs handler).
        if result.error_kind == "offline":
            logger.info(
                "[%s] hardware skipped — target offline (sticky old data)",
                server_name,
            )
        else:
            logger.warning(
                "[%s] hardware failed (%s): %s — sticky old data preserved",
                server_name, result.error_kind, (result.error or "")[:200],
            )

    # ── Correlation (plan §7) ─────────────────────────────────────────────

    def _append_recent_event(self, event: dict[str, Any]) -> None:
        """Add an event to the sliding window deque.

        The deque is maxlen=200 so memory is bounded even for a chatty
        fleet. The actual time window (60 s by default) is enforced when
        we evaluate the deque — we keep the timestamp so we can prune at
        evaluation time.
        """
        event = dict(event)  # defensive copy
        event["event_at"] = time.time()
        _recent_events.append(event)

    def _maybe_run_correlation(self) -> None:
        """Run analytics.correlate_events at most every 30 s.

        Replaces the v1 per-cycle correlation block (collector.py:2240-2247).
        The 60 s sliding window matches the migration plan's
        "time-windowed correlation (default 60 s)".
        """
        global _last_correlation_check
        now = time.time()
        if now - _last_correlation_check < _CORRELATION_INTERVAL_S:
            return
        _last_correlation_check = now

        # Prune events older than the window from the LEFT side of the
        # deque. The right side (newest) is always recent enough to keep.
        cutoff = now - _CORRELATION_WINDOW_S
        while _recent_events and _recent_events[0].get("event_at", 0) < cutoff:
            _recent_events.popleft()

        if not _recent_events:
            return

        correlate = _correlate_events_fn()
        if correlate is None:
            return  # analytics module missing — skip silently

        try:
            servers = _list_servers_for_correlation()
            window_events = list(_recent_events)
            correlated = correlate(self.db, window_events, servers)
            if correlated:
                logger.info(
                    "Time-windowed correlation produced %d incidents "
                    "(window=%.0fs, events=%d)",
                    len(correlated), _CORRELATION_WINDOW_S, len(window_events),
                )
        except Exception:
            logger.error(
                "Event correlation failed:\n%s", traceback.format_exc(),
            )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _lookup_server(name: str) -> Any:
        """Return the ServerConfig by name, or None if it was deleted.

        Late-import of routes.api._shared to avoid pulling Flask at module
        load (matters for tests). Mirrors workers._lookup_server.
        """
        try:
            from routes.api import _shared
        except Exception:
            return None
        if _shared._config is None:
            return None
        return _shared._config.get_server_by_name(name)


# ─────────────────────────────────────────────────────────────────────────
# Late-binding accessors for v1 helpers
# ─────────────────────────────────────────────────────────────────────────
#
# These are wrapped in lazy getters for two reasons:
#   1. Importing ``collector`` at module load would pull in pypsrp, the
#      DB, analytics, etc. — too heavy for unit tests of this file.
#   2. Letting tests patch ``aggregator._send_alert_email_fn`` lets them
#      assert dispatch happened without monkey-patching every callsite.
#
# Each getter returns the live function ref so a test that ``patch(
# "collector_v2.aggregator.send_alert_email", ...)`` (via these names)
# sees the patched value at call time.


def _get_status_fns() -> tuple[Callable, Callable]:
    """(compute_status, _effective_status) from detection module.

    Post-R1: these live in ``detection.py`` (not ``collector.py``).
    Late-imported to avoid pulling detection at module-load time, since
    tests can stub it out. ``collector.py`` re-exports the same symbols
    for backcompat — either import path works during the migration.

    Post-fusion: v2's hot path consumes ``_get_evaluate_server_fn``
    instead; this pair remains for v1 compat + tests.
    """
    from detection import compute_status, _effective_status
    return compute_status, _effective_status


def _get_evaluate_server_fn() -> Callable:
    """``detection.evaluate_server`` — the fused three-layer verdict
    (docs/plans/DETECTION_FUSION_PLAN.md). Late-imported like its
    predecessors so tests can stub the detection module."""
    from detection import evaluate_server
    return evaluate_server


def _get_worst_metric_fn() -> Callable:
    from detection import _get_worst_metric
    return _get_worst_metric


def _is_alert_suppressed_by_maintenance(name: str, settings: dict[str, Any]) -> bool:
    """Defensive wrapper around the maintenance-window gate.

    Lives in ``maintenance.py`` (post-R1b). The wrapper is defensive so
    a minimal test env without the maintenance module continues to
    dispatch alerts. Tests patch this name directly.
    """
    try:
        from maintenance import _is_alert_suppressed_by_maintenance as v1_fn
    except Exception:
        return False
    try:
        return bool(v1_fn(name, settings))
    except Exception:
        return False


def _active_level_detector(settings: dict[str, Any]) -> str:
    """Defensive wrapper around the detector-priority helper."""
    try:
        from detection import _active_level_detector as v1_fn
        return v1_fn(settings)
    except Exception:
        # Mirror the v1 fallback chain so tests without `collector`
        # available still produce sensible decisions.
        if settings.get("baseline_detection", {}).get("enabled", True):
            return "baseline"
        if settings.get("anomaly_detection", {}).get("enabled", True):
            return "anomaly"
        return "threshold"


def _send_alert_email_fn() -> Callable:
    from email_alerts import send_alert_email
    return send_alert_email


def _should_send_email_fn() -> Callable:
    from email_alerts import should_send_email
    return should_send_email


def _should_send_repeat_fn() -> Callable:
    """Resolve the feature-1.1 repeat-interval throttle. Patchable by tests."""
    from alert_scoring import should_send_repeat
    return should_send_repeat


def _is_throttled_by_fatigue_fn() -> Callable:
    """Resolve the live ``is_throttled_by_fatigue`` reference.

    The migration plan refers to this as living in ``alert_fatigue.py``;
    the real implementation is in ``alert_scoring.py`` (see v1
    collector.py:113). We import from the real module so v2 inherits the
    same throttling math v1 uses.
    """
    from alert_scoring import is_throttled_by_fatigue
    return is_throttled_by_fatigue


def _update_score_on_fire(db: Any, server_name: str, metric: str,
                          event_type: str) -> None:
    """Wrapper around alert_scoring.update_score_on_fire that swallows
    import errors so the aggregator doesn't die if alert_scoring is
    momentarily unavailable."""
    try:
        from alert_scoring import update_score_on_fire
        update_score_on_fire(db, server_name, metric, event_type)
    except Exception:
        logger.debug(
            "update_score_on_fire failed for %s/%s/%s",
            server_name, metric, event_type, exc_info=True,
        )


def _send_teams_webhook_fn() -> Callable:
    from webhooks import send_teams_webhook
    return send_teams_webhook


def _check_deviation_fn() -> Callable | None:
    try:
        from baseline_engine import check_deviation
        return check_deviation
    except Exception:
        return None


def _correlate_events_fn() -> Callable | None:
    try:
        from analytics import correlate_events
        return correlate_events
    except Exception:
        return None


def _list_servers_for_correlation() -> list[Any]:
    """Get the live servers list for correlation rules that look at roles.

    Best-effort: if the config isn't wired (test env), return empty —
    correlation tolerates this and just skips role-based rules.
    """
    try:
        from routes.api import _shared
        if _shared._config is None:
            return []
        return _shared._config.get_servers()
    except Exception:
        return []
