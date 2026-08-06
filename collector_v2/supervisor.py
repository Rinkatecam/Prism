"""Supervisor thread for the v2 collector.

The supervisor is the SCHEDULER of the three-thread architecture described
in docs/COLLECTOR_V2_MIGRATION.md. It owns no work itself — it merely
decides, on every 5-second tick, which (server, check_type) pairs are due
for collection and pushes WorkItems onto a bounded queue that the worker
pool drains.

Design rules (from the migration plan):
  * Bulletproof outer try/except — this thread must NEVER die. (Plan §
    "Cross-cutting requirements / Error handling".)
  * Per-server `next_X_at` timestamps in ServerHealth — replaces the legacy
    cycle-modulo gating that coupled cadence to a single wall-clock axis.
    (Plan § "What we REMOVE / REPLACE".)
  * Backpressure: when the queue is full, do NOT block. Push the server's
    `next_X_at` out by 30 s. (Plan § "Cross-cutting / Queue management".)
  * Hardware checks are hardcoded to 60 min — the cadence rarely changes.
    (See DEFAULT_INTERVALS_S in types.py.)
  * Re-read settings every tick so the operator can change cadences live
    without restarting Prism.
  * Per-server hash-shard offsets on first scheduling — the structural fix
    that replaces the legacy v1 stagger/shard patches. Spreads the fleet
    so a "logs every 5 min" cadence doesn't fire on all 30 servers in the
    same tick.
  * Acceleration semantics preserved verbatim from v1: when accelerated,
    ALL pending check types fire on the next tick. (Plan § "Acceleration
    semantics (preserved)".)
  * Maintenance windows with suppress_alerts=True: we STILL collect
    metrics + logs (logs are essential for post-window incident review),
    but we skip the heavy checks (updates, hardware) so install probes
    don't run while the operator is mid-maintenance.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import state
from .types import (
    DEFAULT_INTERVALS_S,
    CheckType,
    ServerHealth,
    WorkItem,
    backoff_delay_s,
)

logger = logging.getLogger("prism.collector_v2.supervisor")

# Tick cadence — every 5 s the supervisor reconsiders the whole fleet. The
# migration plan pins this at 5 s; the watchdog "stuck" threshold (25 s)
# is computed as 5× this.
_TICK_INTERVAL_S: float = 5.0

# Acceleration default — matches the legacy collector's _ACCELERATE_DURATION_S.
_ACCELERATE_DURATION_S: int = 600

# Hard ceiling on a single accelerate_server call. Acceleration runs every
# check on every 5 s supervisor tick — at 1200 s (20 min) that's already
# ~240 forced cycles. Anything beyond this is almost certainly a bug (a
# caller passing seconds-per-day by mistake, an over-eager retry) and would
# bombard the target's WinRM endpoint with no benefit. Callers that
# legitimately need longer windows (none currently) can chain calls; each
# call resets the expiry, which is the documented behaviour.
_ACCELERATE_MAX_DURATION_S: int = 1200

# Startup stagger so cycle-1 doesn't look like the v1 startup spike: metrics
# fire immediately, then logs at +30 s, updates at +60 s, hardware at +90 s.
# Within each check type we ALSO apply a per-server hash offset (see
# `_initial_due_at`) so 30 servers don't all hit logs at exactly +30 s.
_STARTUP_OFFSETS_S: dict[CheckType, int] = {
    CheckType.METRICS: 0,
    CheckType.LOGS: 30,
    CheckType.UPDATES: 60,
    CheckType.HARDWARE: 90,
}

# When the queue is full we push the offending server's next_X_at this far
# into the future. 30 s is short enough that backpressure clears quickly
# but long enough that we don't busy-spin re-enqueueing every tick.
_QUEUE_FULL_RESCHEDULE_S: int = 30

# Module-level bookkeeping (read by `get_supervisor_health` + watchdog).
_critical_error_count: int = 0
_last_tick_at: float = 0.0

# Cumulative count of checks deferred because the work queue was full, and the
# monotonic time of the last WARNING about it. The per-server WARNING is
# rate-limited because at fleet scale a saturated queue produces one line per
# server per check type per tick — a flood that buries every other log entry.
_queue_full_deferrals: int = 0
_queue_full_last_warned_at: float = 0.0
_QUEUE_FULL_WARN_EVERY_S: float = 60.0
_singleton_lock = threading.Lock()
_singleton: "Supervisor | None" = None


# ─────────────────────────────────────────────────────────────────────────
# Module-level helpers (v1-compatible API surface)
# ─────────────────────────────────────────────────────────────────────────


def accelerate_server(
    name: str, duration_s: int | None = None, reason: str = ""
) -> None:
    """Put a server into accelerated-polling mode.

    V1-compatible API — same signature, same semantics as
    `collector.accelerate_server`. Callers across the codebase
    (action_kickoff, restart watcher, came-back-online flow) already import
    THIS name; they don't need to know which engine is running.

    Idempotent: each call resets the expiry, so any "flag event" can re-arm
    the window without bookkeeping in the caller.
    """
    dur = duration_s if duration_s is not None else _ACCELERATE_DURATION_S
    # Clamp to the safety ceiling. A future caller passing a silly value
    # (e.g. seconds-per-day, or a unit-confusion bug) would otherwise keep a
    # server in accelerated polling for hours — bombarding WinRM with no
    # benefit. Log+clamp rather than reject so the caller's intent (re-arm
    # acceleration) still takes effect.
    if dur > _ACCELERATE_MAX_DURATION_S:
        logger.warning(
            "accelerate_server(%s) called with duration_s=%s, clamping to %s "
            "(callers must never bombard a single server beyond this window)",
            name, dur, _ACCELERATE_MAX_DURATION_S,
        )
        dur = _ACCELERATE_MAX_DURATION_S
    elif dur < 0:
        # Negative durations are nonsensical; treat as "stop accelerating".
        dur = 0
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=dur)
    with state._server_health_lock:
        h = state.server_health.get(name)
        if h is None:
            # The server isn't tracked yet (supervisor hasn't seen it in a
            # tick). Stash a sentinel that the supervisor will pick up on
            # the next tick when it materialises the ServerHealth row.
            _pending_acceleration[name] = (until, reason)
            logger.info(
                "Accelerated polling queued for unknown server %s (%.0fs)%s",
                name,
                dur,
                f" [reason: {reason}]" if reason else "",
            )
            return
        h.accelerated_until = until
        h.accelerated_reason = reason
    logger.info(
        "Accelerated polling enabled for %s (%.0fs)%s",
        name,
        dur,
        f" [reason: {reason}]" if reason else "",
    )


# Bridging dict for acceleration requests that arrive BEFORE the supervisor
# has seen the server. The supervisor drains this on every tick.
_pending_acceleration: dict[str, tuple[datetime, str]] = {}


def get_supervisor_health() -> dict[str, Any]:
    """Read-only snapshot for the watchdog and /api/system/health endpoint."""
    now = time.time()
    with _singleton_lock:
        sup = _singleton
    queue_depth = 0
    if sup is not None and sup.work_queue is not None:
        try:
            queue_depth = sup.work_queue.qsize()
        except NotImplementedError:
            # qsize() not reliable on some platforms; treat as unknown.
            queue_depth = -1
    queue_capacity = 0
    if sup is not None and sup.work_queue is not None:
        queue_capacity = sup.work_queue.maxsize
    return {
        "last_tick_s_ago": (now - _last_tick_at) if _last_tick_at else None,
        "tracked_servers": len(state.server_health),
        "queue_depth": queue_depth,
        "queue_capacity": queue_capacity,
        # Cumulative count of checks deferred because the work queue was full.
        # A non-zero and RISING value means the pipeline cannot keep up and the
        # effective poll cadence is longer than configured — previously visible
        # only as scattered per-server WARNING lines, which at fleet scale is a
        # flood nobody reads rather than a signal.
        "checks_deferred_queue_full": _queue_full_deferrals,
        "critical_errors_total": _critical_error_count,
    }


# ─────────────────────────────────────────────────────────────────────────
# Supervisor class
# ─────────────────────────────────────────────────────────────────────────


class Supervisor:
    """Per-tick scheduler that decides which checks to enqueue.

    Owns:
      * a daemon thread `prism-collector-v2-supervisor`
      * the per-server scheduling state in `state.server_health`
      * the bounded work_queue (created by the orchestrator and passed in)

    Does NOT own:
      * the workers (they pull from work_queue independently)
      * the aggregator (it updates ServerHealth via state.mark_check_completed)
      * any DB writes (writes flow through the aggregator)

    The class is intentionally tiny — almost all behaviour lives in helper
    methods so `_loop` stays under the 80-line ceiling required by the
    plan's write-style rules.
    """

    def __init__(
        self,
        get_servers: Callable[[], list[Any]],
        get_settings: Callable[[], dict[str, Any]],
        work_queue: "queue.Queue[WorkItem]",
    ) -> None:
        self.get_servers = get_servers
        self.get_settings = get_settings
        self.work_queue = work_queue
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # For the periodic INFO summary line.
        self._tick_count: int = 0
        self._enqueued_since_summary: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the supervisor daemon thread.

        Idempotent: a second start() is a no-op so the watchdog can call us
        without fear during recovery attempts.
        """
        global _singleton
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Supervisor.start() called but thread already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="prism-collector-v2-supervisor",
            daemon=True,
        )
        with _singleton_lock:
            _singleton = self
        self._thread.start()
        logger.info("Supervisor thread started (tick=%ss)", _TICK_INTERVAL_S)

    def stop(self, timeout: float = 10.0) -> None:
        """Request graceful shutdown and wait up to `timeout` for the thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ── Force-sync entrypoints (replace v1's *_event mechanisms) ──────────

    def force_sync_all(self) -> int:
        """Reset every tracked server's next_metrics_at to now.

        Replaces the v1 `sync_now_event.set()` pattern. Called from the
        `/api/sync-now` endpoint. Returns the count of servers affected
        so the endpoint can include it in the response.
        """
        return self._force_check_all(CheckType.METRICS)

    def force_logs_all(self) -> int:
        """Force a logs collection on every server on the next tick.

        Replaces v1's `force_log_collection.set()`.
        """
        return self._force_check_all(CheckType.LOGS)

    def force_updates_all(self) -> int:
        """Force a Windows Update check on every server on the next tick.

        Replaces v1's `force_update_check.set()`. Note: in v1 this was a
        "all servers in one cycle" hammer that caused the WU spike. In v2
        the bounded queue + per-server scheduling absorbs the load — but
        operators should still expect it to take a couple of minutes for
        all 30 servers to drain through the worker pool.
        """
        return self._force_check_all(CheckType.UPDATES)

    def _force_check_all(self, ct: CheckType) -> int:
        now = datetime.now(timezone.utc)
        n = 0
        with state._server_health_lock:
            for h in state.server_health.values():
                h.set_next_due_for(ct, now)
                n += 1
        logger.info("Force-sync requested: %s on %d servers", ct.value, n)
        return n

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """The supervisor's forever-loop.

        Kept short: each tick delegates to focused helpers. The outer
        try/except is the bulletproof catch-all required by the migration
        plan (§ "Error handling"). Mirrors the legacy pattern at
        collector.py line ~2527.
        """
        global _critical_error_count, _last_tick_at
        logger.info("Supervisor loop entering main cycle")
        while not self._stop_event.is_set():
            tick_start = time.time()
            try:
                self._run_one_tick()
            except Exception:
                # Bulletproof. NEVER die from a runtime exception. Log with
                # traceback, increment counter, recover.
                _critical_error_count += 1
                logger.critical(
                    "Supervisor tick crashed (#%d) — recovering:\n%s",
                    _critical_error_count,
                    traceback.format_exc(),
                )
            finally:
                _last_tick_at = time.time()
                state.heartbeat_supervisor()
            elapsed = time.time() - tick_start
            sleep_for = max(0.0, _TICK_INTERVAL_S - elapsed)
            # Use the stop event's wait() so stop() interrupts the sleep.
            self._stop_event.wait(timeout=sleep_for)
        logger.info("Supervisor loop exiting (stop requested)")

    # ── Per-tick helpers ──────────────────────────────────────────────────

    def _run_one_tick(self) -> None:
        """Single tick: refresh tracked-servers set, schedule, enqueue."""
        self._tick_count += 1
        servers = self.get_servers() or []
        settings = self.get_settings() or {}
        intervals = self._compute_intervals(settings)

        live_names = {s.name for s in servers}
        self._materialise_health(servers, intervals)
        self._drain_pending_acceleration(live_names)
        self._drop_removed(live_names)

        now = datetime.now(timezone.utc)
        enqueued = 0
        for srv in servers:
            enqueued += self._schedule_server(srv, settings, intervals, now)
        self._enqueued_since_summary += enqueued
        self._maybe_log_summary(len(servers))

    @staticmethod
    def _compute_intervals(settings: dict[str, Any]) -> dict[CheckType, int]:
        """Read live settings into a CheckType→seconds map.

        Falls back to DEFAULT_INTERVALS_S on missing / malformed values so
        an operator typo in settings.json never starves the scheduler.
        """
        out = dict(DEFAULT_INTERVALS_S)
        try:
            out[CheckType.METRICS] = max(
                5, int(settings.get("poll_interval_seconds", 60))
            )
        except (TypeError, ValueError):
            pass
        try:
            out[CheckType.LOGS] = max(
                30, int(settings.get("log_collection_interval_minutes", 5)) * 60
            )
        except (TypeError, ValueError):
            pass
        try:
            out[CheckType.UPDATES] = max(
                60,
                int(settings.get("update_check_interval_minutes", 30)) * 60,
            )
        except (TypeError, ValueError):
            pass
        # HARDWARE stays at 3600 — hardcoded per the migration plan.
        return out

    def _materialise_health(
        self,
        servers: list[Any],
        intervals: dict[CheckType, int],
    ) -> None:
        """Create ServerHealth entries for servers we haven't seen yet."""
        now = datetime.now(timezone.utc)
        with state._server_health_lock:
            for srv in servers:
                if srv.name in state.server_health:
                    continue
                # Build via the new dict-based shape (audit M2 refactor).
                # ServerHealth's compat properties (next_metrics_at etc.)
                # still work, but constructing via `checks=` is the
                # forward-looking idiom: adding a check type doesn't
                # require touching this loop.
                from .types import CheckState
                health = ServerHealth(
                    name=srv.name,
                    checks={
                        ct: CheckState(
                            next_due_at=_initial_due_at(srv.name, ct, intervals, now)
                        )
                        for ct in CheckType
                    },
                )
                state.server_health[srv.name] = health
                logger.info(
                    "Tracking new server %s "
                    "(metrics@%ss logs@%ss updates@%ss hw@%ss)",
                    srv.name,
                    int((health.next_metrics_at - now).total_seconds()),
                    int((health.next_logs_at - now).total_seconds()),
                    int((health.next_updates_at - now).total_seconds()),
                    int((health.next_hardware_at - now).total_seconds()),
                )

    @staticmethod
    def _drain_pending_acceleration(live_names: set[str]) -> None:
        """Apply any acceleration that was requested before the server existed.

        `accelerate_server()` is called by code paths (e.g. action_kickoff)
        that don't know whether the supervisor has materialised the
        ServerHealth row yet. Those calls stash into `_pending_acceleration`;
        we drain here once the row exists.
        """
        if not _pending_acceleration:
            return
        with state._server_health_lock:
            for name in list(_pending_acceleration.keys()):
                if name not in live_names:
                    continue
                h = state.server_health.get(name)
                if h is None:
                    continue
                until, reason = _pending_acceleration.pop(name)
                h.accelerated_until = until
                h.accelerated_reason = reason

    @staticmethod
    def _drop_removed(live_names: set[str]) -> None:
        """Garbage-collect health entries for servers no longer in config."""
        with state._server_health_lock:
            stale = [n for n in state.server_health if n not in live_names]
            for n in stale:
                state.server_health.pop(n, None)
                logger.info("Stopped tracking removed server %s", n)

    def _schedule_server(
        self,
        srv: Any,
        settings: dict[str, Any],
        intervals: dict[CheckType, int],
        now: datetime,
    ) -> int:
        """Enqueue any due checks for one server. Returns count enqueued."""
        health = state.server_health.get(srv.name)
        if health is None:
            return 0  # raced with removal — skip, will retry next tick

        maint_skip_heavy = _heavy_checks_suppressed_by_maintenance(srv.name, settings)
        is_accel = health.is_accelerated()
        enqueued = 0
        for ct in CheckType:
            if maint_skip_heavy and ct in (CheckType.UPDATES, CheckType.HARDWARE):
                # Plan: during suppress_alerts maintenance we keep
                # metrics+logs (logs feed post-window incident review) but
                # skip the heavy probes that would generate install activity
                # during downtime.
                if health.pending.get(ct):
                    continue
                # H3 fix from audit: only log + push next-due if the next-due
                # is currently in the PAST (i.e. this check is actually
                # eligible to fire and we're suppressing it). Without this,
                # the supervisor re-evaluated every 5 s for the whole
                # maintenance window even though next_due was already pushed
                # forward by the FIRST suppression → log lines piled up at
                # 2 events/s × 30 servers × window length.
                if now < health.next_due_for(ct):
                    continue
                logger.warning(
                    "[%s] %s suppressed by active maintenance window",
                    srv.name,
                    ct.value,
                )
                # Push the next-due past the maintenance window's typical
                # length so we don't re-evaluate on every 5 s tick.
                health.set_next_due_for(ct, now + timedelta(seconds=300))
                continue
            if health.pending.get(ct):
                continue
            if not (is_accel or now >= health.next_due_for(ct)):
                continue
            if self._enqueue(srv.name, ct, now, is_accel):
                enqueued += 1
                health.pending[ct] = True
                self._advance_next_due(health, ct, intervals, now)
            else:
                # Queue was full — backpressure. Reschedule soon and bail.
                health.set_next_due_for(
                    ct, now + timedelta(seconds=_QUEUE_FULL_RESCHEDULE_S)
                )
                global _queue_full_deferrals, _queue_full_last_warned_at
                _queue_full_deferrals += 1
                # Rate-limited: a saturated queue otherwise emits one line per
                # server per check type per tick. At 500 servers that is
                # thousands of lines a minute, which hides the condition rather
                # than reporting it. The counter above is the real signal and is
                # exposed on /api/system/health.
                mono = time.monotonic()
                if mono - _queue_full_last_warned_at >= _QUEUE_FULL_WARN_EVERY_S:
                    _queue_full_last_warned_at = mono
                    logger.warning(
                        "Work queue full (capacity %d) — deferring checks by %ds; "
                        "%d deferrals so far. Most recent: [%s] %s",
                        self.work_queue.maxsize,
                        _QUEUE_FULL_RESCHEDULE_S,
                        _queue_full_deferrals,
                        srv.name,
                        ct.value,
                    )
        return enqueued

    def _enqueue(
        self,
        server_name: str,
        ct: CheckType,
        now: datetime,
        is_accel: bool,
    ) -> bool:
        """Put a WorkItem on the queue without blocking. Returns success."""
        reason = "accelerated" if is_accel else "schedule"
        item = WorkItem(
            server_name=server_name,
            check_type=ct,
            enqueued_at=now,
            deadline_s=_default_deadline_for(ct),
            reason=reason,
        )
        try:
            self.work_queue.put_nowait(item)
        except queue.Full:
            return False
        logger.debug("enqueue %s", item)
        return True

    @staticmethod
    def _advance_next_due(
        health: ServerHealth,
        ct: CheckType,
        intervals: dict[CheckType, int],
        now: datetime,
    ) -> None:
        """After enqueueing, push the next-due timestamp out by the cadence.

        If the server has been failing this check repeatedly, apply
        exponential backoff (see types.backoff_delay_s). The failure
        counter is owned by the aggregator (`state.mark_check_completed`),
        so we just READ it here.
        """
        base = intervals[ct]
        failures = health.consecutive_failures.get(ct, 0)
        if failures > 1:
            delay = backoff_delay_s(failures, base_s=base)
            logger.warning(
                "[%s] backoff applied: %s failures=%d → next in %ds",
                health.name,
                ct.value,
                failures,
                delay,
            )
        else:
            delay = base
        health.set_next_due_for(ct, now + timedelta(seconds=delay))

    def _maybe_log_summary(self, server_count: int) -> None:
        """INFO-level summary every 12 ticks (1 minute at 5 s tick)."""
        if self._tick_count % 12 != 0:
            return
        try:
            depth = self.work_queue.qsize()
        except NotImplementedError:
            depth = -1
        logger.info(
            "Supervisor tick #%d: enqueued %d items in last minute, "
            "%d servers tracked, queue_depth=%d",
            self._tick_count,
            self._enqueued_since_summary,
            server_count,
            depth,
        )
        self._enqueued_since_summary = 0


# ─────────────────────────────────────────────────────────────────────────
# Free helpers
# ─────────────────────────────────────────────────────────────────────────


def _stable_hash(name: str) -> int:
    """Deterministic positive int from a server name.

    Python's builtin `hash()` is salted per-process (PEP 456) — same name,
    different processes, different shard. We want the schedule offset to
    be the same across restarts so an operator watching telemetry over
    days sees a consistent pattern, not reshuffling on every restart.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _initial_due_at(
    server_name: str,
    ct: CheckType,
    intervals: dict[CheckType, int],
    now: datetime,
) -> datetime:
    """First scheduling slot for this (server, check) pair.

    Combines:
      1. The startup offset for this check type (metrics=0, logs=30,
         updates=60, hardware=90) — avoids the v1 cycle-1 spike.
      2. A per-server hash-shard offset within the check's interval —
         spreads the fleet so a 5-min logs cadence on 30 servers fires
         ~6 servers per minute, not all 30 in one tick.

    The migration plan calls this out as "the structural fix that replaces
    the v1 stagger/shard patches" (Requirement #5 of supervisor.py).
    """
    base_offset = _STARTUP_OFFSETS_S[ct]
    interval = intervals[ct]
    shard = _stable_hash(server_name) % max(1, interval)
    return now + timedelta(seconds=base_offset + shard)


def _default_deadline_for(ct: CheckType) -> int:
    """Per-check worker deadline. Imported here so tests don't have to."""
    from .types import DEFAULT_DEADLINES_S

    return DEFAULT_DEADLINES_S[ct]


def _heavy_checks_suppressed_by_maintenance(
    server_name: str, settings: dict[str, Any]
) -> bool:
    """True when an active maintenance window asks us to skip heavy probes.

    Reads ``_get_active_maintenance_window`` from ``maintenance.py``
    (post-R1b) — the single source of truth for window matching.

    The decision: when ``suppress_alerts=True`` is set on the active
    window we DO continue to collect metrics + logs (logs are essential
    for the post-window incident review) but we SKIP updates + hardware.
    Operators almost never want install/probe activity during a
    maintenance burst, and we'd rather see fresh-but-quiet metrics than
    a gap in the chart.
    """
    try:
        from maintenance import _get_active_maintenance_window
    except Exception:
        return False
    try:
        win = _get_active_maintenance_window(server_name, settings)
    except Exception:
        # Defensive: a bad timezone setting should not stop scheduling.
        return False
    return bool(win and win.get("suppress_alerts", False))
