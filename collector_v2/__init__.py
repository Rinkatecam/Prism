"""Prism Collector v2 — supervisor + worker pool + aggregator architecture.

The v1 collector (`collector.py`) runs a single cycle loop with a shared
90 s wall-clock budget over all servers. The v2 architecture replaces that
with three cooperating threads:

  * Supervisor — decides what work is due, enqueues WorkItems
  * Workers   — pull WorkItems, execute checks against target servers
  * Aggregator — processes Results, persists to DB, fires alerts

Each component is independently bounded by its own deadlines and has its
own heartbeat. No single slow server can block the rest of the fleet.

Public API (called by ``app.py`` at startup and by route handlers):
  * ``start_collector_v2(get_servers, get_settings, db, num_workers=15)``
  * ``accelerate_server(name, duration_s, reason)``
  * ``sync_now()`` / ``sync_logs_now()`` / ``sync_updates_now()``
  * ``get_health_snapshot()`` — for ``/api/system/health``

History: v1 (a single-threaded ``collector_loop``) was retired in
``docs/COLLECTOR_V1_RETIREMENT.md``; the ``collector_engine`` feature
flag and the ``"both"`` shadow mode are gone with it. See
``docs/COLLECTOR_V2_MIGRATION.md`` for the original migration plan and
``docs/COLLECTOR_V2_GOALS.md`` for the design goals v2 still serves.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from . import state, types
from .types import CheckType, WorkItem, Result, ServerHealth  # noqa: F401 — re-export

logger = logging.getLogger("prism.collector_v2")

# Module-level singletons for the three threads. None until start_collector_v2().
_supervisor = None
_worker_pool = None
_aggregator = None
_work_queue: queue.Queue | None = None
_result_queue: queue.Queue | None = None
_started = False
_start_lock = threading.Lock()


def start_collector_v2(get_servers, get_settings, db, *, num_workers: int = 15) -> None:
    """Boot the three-thread v2 collector.

    Idempotent — calling twice is a no-op (logs a warning). Designed to be
    called once from app.py at process start when settings['collector_engine']
    is "v2" or "both".

    Args:
        get_servers: Callable returning the current list[ServerConfig].
                     Reads config_manager.ConfigManager.get_servers().
        get_settings: Callable returning the current settings dict.
        db: Database instance.
        num_workers: Worker pool size. Default 15 (was 5 in v1 — bigger because
                     each worker can sit on a slow WU call for up to 120s).

    The three threads start in this order:
      1. Aggregator (must be ready to receive Results before workers produce them)
      2. Worker pool (must be ready before supervisor enqueues WorkItems)
      3. Supervisor (last — once it ticks, the pipeline is live)

    Stop order is the reverse: supervisor stops enqueueing, workers drain,
    aggregator drains. Stop is not exposed publicly today (the process just
    exits at shutdown) but the methods exist for future graceful-restart work.
    """
    global _supervisor, _worker_pool, _aggregator, _work_queue, _result_queue, _started

    from .supervisor import Supervisor
    from .workers import WorkerPool
    from .aggregator import Aggregator

    with _start_lock:
        if _started:
            logger.warning("start_collector_v2 called twice — ignoring second call")
            return

        # Bounded work queue: backpressure if the supervisor outpaces workers.
        #
        # Sized from the FLEET, not the pool. It was num_workers * 4, which is
        # 120 at the default 30 workers — fine for 29 servers, but a single
        # metrics sweep of a 500-server fleet enqueues 500 items and would
        # overflow every cycle. Overflow is not data loss (the supervisor
        # reschedules the check by _QUEUE_FULL_RESCHEDULE_S), but chronic
        # overflow silently stretches the effective poll cadence beyond what is
        # configured, which is a monitoring tool lying about its own freshness.
        #
        # One full sweep of every server and check type, doubled for headroom,
        # with the old formula as the floor for small fleets.
        try:
            _fleet_size = len(get_servers())
        except Exception:
            _fleet_size = 0
        _queue_size = max(num_workers * 4, _fleet_size * 2 * len(CheckType))
        _work_queue = queue.Queue(maxsize=_queue_size)
        # Result queue: unbounded. Dropping a Result would lose data the
        # aggregator needs for status transitions / alerts. If the aggregator
        # falls behind we want unbounded growth + a watchdog alert, not silent
        # data loss.
        _result_queue = queue.Queue()

        # 1) Aggregator first (must drain results)
        _aggregator = Aggregator(_result_queue, db, get_settings)
        _aggregator.start()

        # 2) Workers second (must execute work)
        _worker_pool = WorkerPool(_work_queue, _result_queue, num_workers=num_workers)
        _worker_pool.start()

        # 3) Supervisor last (drives the pipeline)
        _supervisor = Supervisor(get_servers, get_settings, _work_queue)
        _supervisor.start()

        # 4) Periodics — TLS / drift / failed-logins / retention / scheduled
        # reports / health-checks. These don't share the supervisor's per-
        # server fan-out shape, so they live in their own daemon thread.
        # The functions themselves live in the extracted helper modules —
        # detection.py, maintenance.py, tls_monitor.py, healthchecks.py,
        # drift.py, failed_logins.py, scheduled_reports.py — all imported by
        # periodics.py. v2 only changes how they're scheduled. (They used to
        # live in collector.py, which was deleted when v1 was retired; see
        # docs/COLLECTOR_V1_RETIREMENT.md.)
        from . import periodics
        periodics.start_periodics(get_servers, get_settings, db)

        _started = True
        logger.info(
            "Collector v2 started: 1 supervisor + %d workers + 1 aggregator "
            "+ 1 periodics (work_queue cap=%d, result_queue=unbounded)",
            num_workers, _work_queue.maxsize,
        )


def stop_collector_v2() -> None:
    """Graceful stop of all three threads. Not called by app.py today (the
    process just exits) but exposed for tests + future restart-without-pid
    workflows."""
    global _started
    if not _started:
        return
    logger.info("Stopping collector v2 threads...")
    try:
        if _supervisor is not None:
            _supervisor.stop()
    except Exception:
        logger.exception("Failed to stop supervisor")
    try:
        if _worker_pool is not None:
            _worker_pool.stop()
    except Exception:
        logger.exception("Failed to stop worker pool")
    try:
        if _aggregator is not None:
            _aggregator.stop()
    except Exception:
        logger.exception("Failed to stop aggregator")
    try:
        from . import periodics
        periodics.stop_periodics()
    except Exception:
        logger.exception("Failed to stop periodics")
    _started = False


# ── V1-compatible public surface ─────────────────────────────────────────
# These mirror the names the rest of the codebase imports from `collector`.
# When v2 is the active engine, the `collector` module re-exports these so
# `from collector import accelerate_server` keeps working unchanged.

def accelerate_server(name: str, duration_s: int = 600, reason: str = "") -> None:
    """V1-compatible API. Set a server to accelerated polling for `duration_s`
    seconds. Reads through to supervisor.accelerate_server."""
    from .supervisor import accelerate_server as _accel
    _accel(name, duration_s, reason)


def sync_now() -> None:
    """V1-compatible API equivalent of `sync_now_event.set()`. Tells the
    supervisor to fire metrics for every server on the next tick (5 s)."""
    if _supervisor is not None:
        _supervisor.force_sync_all()


def sync_logs_now() -> None:
    """V1-compatible API equivalent of `force_log_collection.set()`."""
    if _supervisor is not None:
        _supervisor.force_logs_all()


def sync_updates_now() -> None:
    """V1-compatible API equivalent of `force_update_check.set()`."""
    if _supervisor is not None:
        _supervisor.force_updates_all()


def get_health_snapshot() -> dict[str, Any]:
    """Read-only snapshot of v2's runtime health. Used by /api/system/health
    and the watchdog."""
    snap = state.get_v2_health_snapshot()
    # Per-component health if available
    try:
        from .supervisor import get_supervisor_health
        snap["supervisor"] = get_supervisor_health()
    except Exception:
        snap["supervisor"] = {"error": "unavailable"}
    try:
        from .workers import get_worker_pool_health
        snap["workers"] = get_worker_pool_health()
    except Exception:
        snap["workers"] = {"error": "unavailable"}
    try:
        from .aggregator import get_aggregator_health
        snap["aggregator"] = get_aggregator_health()
    except Exception:
        snap["aggregator"] = {"error": "unavailable"}
    try:
        from .periodics import get_periodics_health
        snap["periodics"] = get_periodics_health()
    except Exception:
        snap["periodics"] = {"error": "unavailable"}
    snap["started"] = _started
    return snap


__all__ = [
    "start_collector_v2",
    "stop_collector_v2",
    "accelerate_server",
    "sync_now",
    "sync_logs_now",
    "sync_updates_now",
    "get_health_snapshot",
    "state",
    "types",
    "CheckType",
    "WorkItem",
    "Result",
    "ServerHealth",
]
