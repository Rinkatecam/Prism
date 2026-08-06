"""Worker pool for the v2 collector.

Pulls WorkItems off the supervisor's queue, opens a WinRM session against
the target server, dispatches to the appropriate per-check function in
``checks.py``, enforces a per-check deadline, and emits a Result on the
aggregator's queue.

Design rules (see docs/COLLECTOR_V2_MIGRATION.md, esp. §"Cross-cutting
requirements"):

  * Bulletproof outer try/except in ``_worker_loop`` — worker threads
    must NEVER die. Logged CRITICAL with traceback + counter.
  * Per-CHECK deadline enforced via a ``concurrent.futures.ThreadPoolExecutor``
    submit + ``future.result(timeout=...)``. This isolates the deadline
    math from pypsrp's own (unreliable on hung sessions) internal
    timeouts. (Plan §"Worker pool".)
  * Stale-in-queue check — if ``item.is_stale`` we DROP the item without
    making a WinRM call; the supervisor will reschedule. (Plan §"Queue
    management".)
  * Error categorisation matches v1: ``_is_offline_error`` lives in
    checks.py and we forward its kind into the Result. Offline-class
    errors log at INFO, not WARNING — they're routine during reboots.
  * Stuck-WinRM recovery: when ``future.result(timeout)`` raises, the
    nested-future thread may still be holding a socket. We do NOT
    rebuild any executor — each worker owns its own one-shot executor
    and the leaked thread dies when the WinRM socket finally errors.
    (Equivalent to v1's S1-5 fix, just per-worker.)
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from . import state
from .checks import (
    _is_offline_error,
    check_hardware,
    check_logs,
    check_metrics,
    check_updates,
)
from .types import CheckType, Result, WorkItem

logger = logging.getLogger("prism.collector_v2.workers")

# The Windows Update COM search is the heaviest WinRM call and occasionally
# trips a transient WSMan transport fault (HTTP 500, empty body). UPDATES
# reschedules on the long update_check_interval, so a one-off blip would leave
# update state stale for a full interval — we retry it ONCE, with a fresh
# connection, on a transport-class failure. Backoff before the retry lets the
# target's WSMan settle without eating much of the deadline.
_TRANSIENT_RETRY_BACKOFF_S = 1.5


# ── Module-level health counters (read by /api/system/health) ────────────
# All mutations go through ``_stats_lock`` so the snapshot the API returns
# is internally consistent even when many workers are completing items.

_stats_lock = threading.Lock()
_active_count: int = 0
_total_processed: int = 0
_total_offline: int = 0
_total_timeouts: int = 0
_total_critical_errors: int = 0


def _stats_inc(field: str, delta: int = 1) -> None:
    """Atomic increment of a module-level counter. Called from hot paths."""
    global _active_count, _total_processed, _total_offline
    global _total_timeouts, _total_critical_errors
    with _stats_lock:
        if field == "active_count":
            _active_count += delta
        elif field == "total_processed":
            _total_processed += delta
        elif field == "total_offline":
            _total_offline += delta
        elif field == "total_timeouts":
            _total_timeouts += delta
        elif field == "total_critical_errors":
            _total_critical_errors += delta


def get_worker_pool_health() -> dict[str, Any]:
    """Read-only snapshot of worker-pool runtime stats, for /api/system/health.

    The supervisor + aggregator have their own counters; this is the
    workers' contribution. The watchdog reads this every few seconds.
    """
    with _stats_lock:
        return {
            "active_workers": _active_count,
            "total_processed": _total_processed,
            "total_offline": _total_offline,
            "total_timeouts": _total_timeouts,
            "total_critical_errors": _total_critical_errors,
            "num_workers": _registered_num_workers,
        }


# Set by ``WorkerPool.start`` so ``get_worker_pool_health`` can report it
# without each caller having to know which pool is "the" pool. Defaults to
# 0 — the API will show 0 workers until the pool starts, which is correct.
_registered_num_workers: int = 0


# ── Check dispatch table ─────────────────────────────────────────────────
# Maps CheckType → callable that returns the right check function. We
# store WRAPPERS rather than direct refs so tests can ``patch(
# "collector_v2.workers.check_metrics", ...)`` and have the patched name
# resolve at call time. A direct ref would capture the unpatched function
# at module-load time.

_CHECK_DISPATCH: dict[CheckType, Callable[..., tuple]] = {
    CheckType.METRICS: lambda srv, pool: check_metrics(srv, pool),
    CheckType.LOGS: lambda srv, pool: check_logs(srv, pool),
    CheckType.UPDATES: lambda srv, pool: check_updates(srv, pool),
    CheckType.HARDWARE: lambda srv, pool: check_hardware(srv, pool),
}


# ─────────────────────────────────────────────────────────────────────────
# WorkerPool
# ─────────────────────────────────────────────────────────────────────────


class WorkerPool:
    """Pool of daemon threads draining the supervisor's work queue.

    Why a class instead of bare functions: each test (and production
    instance) needs its own queues + stop signal. A class makes the
    plumbing explicit. The class itself owns no state past start/stop —
    per-item state lives on the stack of ``_execute_one`` and is GC'd
    immediately after the Result is emitted.

    Default worker count is 15 (3x v1's 5). The plan calls for 15–20
    workers; we expose ``num_workers`` so production can tune up to 20
    without code changes. (Plan §"Worker pool".)
    """

    # Sentinel value pushed onto the work queue at stop() time so blocking
    # queue.get() calls return promptly even when no real work is queued.
    # One sentinel per worker so each one gets exactly one wake-up.
    _POISON_PILL: Any = None

    def __init__(
        self,
        work_queue: "queue.Queue[WorkItem | None]",
        result_queue: "queue.Queue[Result]",
        num_workers: int = 15,
    ) -> None:
        self.work_queue = work_queue
        self.result_queue = result_queue
        self.num_workers = num_workers
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn N daemon worker threads. Each runs ``_worker_loop`` until
        stop() is called. Idempotent in the sense that re-calling start()
        after stop() will spawn a fresh set of threads."""
        if self._threads:
            # Calling start() twice without stop() is a bug; loudly refuse
            # rather than silently double up.
            raise RuntimeError(
                "WorkerPool.start called while threads are already running"
            )
        self._stop_event.clear()
        global _registered_num_workers
        _registered_num_workers = self.num_workers
        for i in range(self.num_workers):
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                name=f"prism-collector-v2-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        logger.info("WorkerPool started with %d workers", self.num_workers)

    def stop(self, *, join_timeout_s: float = 5.0) -> None:
        """Graceful shutdown. Sets the stop event then pushes one poison
        pill per worker so blocking ``queue.get`` calls return immediately.
        Joins each worker with a short timeout — daemon threads are
        cleaned up on process exit anyway, so a missed join is harmless
        but uncommon enough to warrant a WARNING log."""
        self._stop_event.set()
        # One poison pill per worker — workers exit on None. We push them
        # AFTER setting the event so a worker that wakes between event
        # set and pill push still exits via the event check.
        for _ in self._threads:
            try:
                self.work_queue.put_nowait(self._POISON_PILL)
            except queue.Full:
                # Queue full → workers are already busy; they'll see the
                # event when they pull their next item. Don't block here.
                pass
        for t in self._threads:
            t.join(timeout=join_timeout_s)
            if t.is_alive():
                logger.warning(
                    "Worker %s did not exit within %.1fs; daemon=True so "
                    "process exit will clean it up.",
                    t.name,
                    join_timeout_s,
                )
        self._threads.clear()
        global _registered_num_workers
        _registered_num_workers = 0
        logger.info("WorkerPool stopped")

    # ── per-thread loop ───────────────────────────────────────────────

    def _worker_loop(self, worker_id: int) -> None:
        """Pull WorkItems from the queue, execute, emit Results. Forever.

        Bulletproof: any exception escaping the inner try/except is
        re-caught here so the worker thread NEVER dies. The plan and v1
        both treat this as a hard requirement — a single uncaught
        exception in a worker would silently shrink the pool and
        eventually starve the fleet.
        """
        logger.debug("Worker %d starting", worker_id)
        # Sentinel returned by _pull_next_item on a TIMEOUT (queue empty
        # for 1s). Distinct from None which means "poison pill / stop".
        # Earlier this code conflated both via None and that killed all
        # workers as soon as the queue was briefly empty (startup race
        # before the supervisor's first tick produced any items).
        _EMPTY = self._QUEUE_EMPTY_SENTINEL
        while not self._stop_event.is_set():
            try:
                item = self._pull_next_item()
                if item is _EMPTY:
                    # Just a poll timeout — re-check stop event and pull again.
                    continue
                if item is None:
                    # Real exit: poison pill received OR stop_event already set
                    # and we drained the queue. Either way, leave the loop.
                    break

                # Heartbeat per pull so the watchdog sees the pool is alive
                # even when no work is happening. (Plan §"Heartbeats".)
                state.heartbeat_worker()

                logger.debug(
                    "worker %d executing W=%r", worker_id, item
                )
                result = self._execute_one(item)
                self._emit_result(result, worker_id)
            except Exception:
                # Bulletproof: catch ANY exception that escaped _execute_one
                # or _emit_result. Increment the critical-error counter so
                # the watchdog notices, sleep briefly to avoid a tight
                # error loop, then continue. Daemon thread must never die.
                _stats_inc("total_critical_errors")
                logger.critical(
                    "Worker %d bulletproof catch fired:\n%s",
                    worker_id,
                    traceback.format_exc(),
                )
                time.sleep(0.5)
        logger.debug("Worker %d exiting", worker_id)

    # ── helpers used by the loop ──────────────────────────────────────

    # Sentinel for "queue was empty for the poll timeout window". Distinct
    # from None (which means stop/poison-pill). Picked as a private object
    # to guarantee `is` comparisons in the loop.
    _QUEUE_EMPTY_SENTINEL: Any = object()

    def _pull_next_item(self) -> WorkItem | None | Any:
        """Block (with timeout) on the queue.

        Returns one of three values:
          * a WorkItem — execute it
          * None — poison pill received (stop the worker)
          * self._QUEUE_EMPTY_SENTINEL — 1s elapsed with no item, caller
            should re-check ``_stop_event`` and pull again (do NOT exit)

        The empty-vs-stop distinction is load-bearing: conflating them
        used to kill the worker pool whenever the queue went briefly
        idle, e.g. during the few seconds between WorkerPool.start() and
        the supervisor's first tick that produced items.
        """
        try:
            item = self.work_queue.get(timeout=1.0)
        except queue.Empty:
            return self._QUEUE_EMPTY_SENTINEL
        if item is self._POISON_PILL:
            return None
        return item

    def _emit_result(self, result: Result, worker_id: int) -> None:
        """Push the Result to the aggregator. Updates the per-Result
        stats and decides on the right log level for failures (offline
        errors are routine during reboots and log at INFO, real failures
        at WARNING)."""
        self.result_queue.put(result)
        _stats_inc("total_processed")
        if result.ok:
            return

        kind = result.error_kind or "unknown"
        if kind == "timeout":
            _stats_inc("total_timeouts")
            # Deadline expiries are rare and worth flagging.
            logger.info(
                "worker %d deadline timeout: %s after %.1fs",
                worker_id,
                result.item,
                result.duration_s,
            )
        elif kind == "offline":
            _stats_inc("total_offline")
            # Offline-class errors during reboot windows are noise at
            # WARNING — log at DEBUG. The v1 collector previously logged
            # these at WARNING which spammed the operator's logs every
            # reboot.
            logger.debug(
                "worker %d offline (%s): %s — %s",
                worker_id,
                result.item.server_name,
                result.item.check_type.value,
                (result.error or "")[:160],
            )
        else:
            logger.warning(
                "worker %d failure (%s): %s — %s",
                worker_id,
                kind,
                result.item,
                (result.error or "")[:200],
            )

    # ── per-item execution ────────────────────────────────────────────

    def _execute_one(self, item: WorkItem) -> Result:
        """Run a single WorkItem: deadline check, config lookup, WinRM
        session, dispatch, Result.

        **Guarantees a Result is returned in EVERY case** — including
        unhandled exceptions inside the inner try. This guarantee is
        load-bearing: the supervisor sets ``pending[ct] = True`` when it
        enqueues, and the aggregator clears it when a Result arrives.
        If we ever escaped this function without producing a Result, the
        server's ``pending[ct]`` would be stuck True forever and the
        supervisor would never enqueue another check for that
        (server, check_type) pair. Audit M3 from
        ``docs/COLLECTOR_V2_AUDIT.md`` flagged this risk; this defensive
        emit closes it by treating "anything that escapes" as a real
        Result with ``error_kind='exception'``.
        """
        started_at = datetime.now(timezone.utc)
        _stats_inc("active_count")
        try:
            try:
                # 1. Stale-item drop — supervisor will reschedule (plan
                #    §"Queue management"). NO WinRM call attempted.
                if item.is_stale:
                    return Result(
                        item=item,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        ok=False,
                        error="Item exceeded queue wait time",
                        error_kind="timeout",
                    )

                # 2. Look up the server config. Late-import so we don't
                #    pull Flask in at module load — workers must be usable
                #    in tests without a Flask app context.
                server = self._lookup_server(item.server_name)
                if server is None:
                    return Result(
                        item=item,
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                        ok=False,
                        error=(
                            f"Server {item.server_name!r} not in config "
                            "(deleted while queued)"
                        ),
                        error_kind="config_missing",
                    )

                # 3. Run the check with deadline enforcement.
                ok, data, err, kind = self._invoke_with_deadline(server, item)
                return Result(
                    item=item,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    ok=ok,
                    data=data,
                    error=err,
                    error_kind=kind,
                )
            except BaseException as e:  # noqa: BLE001 — defensive: ALL escapes
                # Anything that gets here (NameError in check code, a bug
                # in _invoke_with_deadline, a typo, MemoryError on a huge
                # WU response, etc.) becomes a Result with error_kind
                # 'exception' instead of being swallowed by the worker
                # loop's bulletproof catch. Without this, pending[ct] for
                # this server would stay True forever and the supervisor
                # would never schedule another check for it.
                _stats_inc("total_critical_errors")
                logger.exception(
                    "[%s] %s: unhandled exception in _execute_one — "
                    "emitting defensive Result so supervisor's pending "
                    "flag clears",
                    item.server_name,
                    item.check_type.value,
                )
                return Result(
                    item=item,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                    ok=False,
                    error=f"{type(e).__name__}: {str(e)[:300]}",
                    error_kind="exception",
                )
        finally:
            _stats_inc("active_count", -1)

    # ── server lookup ─────────────────────────────────────────────────

    def _lookup_server(self, name: str):
        """Find the ServerConfig by name. Late-imports ``_shared`` to
        avoid loading Flask at module-import time (matters for tests).
        Returns None if the server was deleted while queued."""
        from routes.api import _shared  # late import: avoids Flask at load
        if _shared._config is None:
            # Shouldn't happen in production (start_collector_v2 wires it
            # in) but during tests the config may not be set. Treat as
            # "server missing" so the caller emits a config_missing
            # Result and the test can assert on it.
            return None
        return _shared._config.get_server_by_name(name)

    # ── deadline-bounded execution ────────────────────────────────────

    def _invoke_with_deadline(
        self, server: Any, item: WorkItem
    ) -> tuple[bool, Any, str | None, str | None]:
        """Open a WSMan + RunspacePool and run the check, enforcing
        ``item.deadline_s`` via a one-shot ThreadPoolExecutor.

        Why a per-item executor: pypsrp's internal timeouts are
        unreliable on hung sessions (a TCP RST mid-script can leave the
        Python side blocked indefinitely on a socket read). Wrapping the
        whole open+run in a future with our own timeout gives us a hard
        wall-clock guarantee.

        Returns the (ok, data, err, kind) tuple the check function
        produces, OR a synthesised timeout tuple when the deadline fires.

        STUCK-SOCKET HANDLING: when ``future.result(timeout)`` raises
        ``TimeoutError``, the underlying pypsrp call may still be
        holding a socket. v1 handled this at the fleet level by
        rebuilding its ThreadPoolExecutor (collector.py ~line 1748).
        v2 each worker has its own one-shot executor for this single
        item — so we DON'T rebuild anything. The leaked nested-future
        thread will die when the WinRM socket finally errors out, the
        same fix v1 uses, just scoped to one item. (Plan §"Stuck WinRM
        recovery".)
        """
        # connection_timeout=15: TCP/WinRM handshake bound. Short enough
        # that an offline server fails fast.
        # read_timeout=deadline_s + 5: lets WS-Man's internal channel
        # wait for the PS script, with a small margin over OUR deadline
        # so our timeout fires first (giving us cleaner errors than
        # pypsrp's mid-read explosions).
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"prism-v2-winrm-{item.server_name}",
        )
        try:
            future = executor.submit(
                self._open_session_and_check, server, item
            )
            try:
                return future.result(timeout=item.deadline_s)
            except concurrent.futures.TimeoutError:
                # Try to cancel — almost always a no-op because the
                # nested future has already started running. The thread
                # will die naturally when the underlying socket errors.
                future.cancel()
                return (
                    False,
                    None,
                    f"Check exceeded deadline of {item.deadline_s}s",
                    "timeout",
                )
        finally:
            # wait=False: don't block on the (possibly still-running)
            # nested future. cancel_futures=True is best-effort.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover — Python <3.9 fallback
                executor.shutdown(wait=False)

    def _open_session_and_check(
        self, server: Any, item: WorkItem
    ) -> tuple[bool, Any, str | None, str | None]:
        """Open the WSMan + RunspacePool and dispatch to the right check.

        Runs INSIDE the per-item executor's thread. Any exception is
        caught here and converted to the (ok, data, err, kind) tuple so
        the outer ``future.result(...)`` only ever raises TimeoutError.
        """
        dispatch_fn = _CHECK_DISPATCH.get(item.check_type)
        if dispatch_fn is None:
            return (
                False,
                None,
                f"Unknown check_type {item.check_type!r}",
                "exception",
            )

        # Defer the WSMan/RunspacePool resolution through module-level
        # names (``_make_wsman``, ``_get_runspace_pool_cls``) so tests
        # can patch them without owning the pypsrp dep. Both helpers
        # raise on missing pypsrp; we map that to the same
        # ``exception`` error_kind v1 used.
        try:
            wsman_factory = _make_wsman
            runspace_cls = _get_runspace_pool_cls()
        except RuntimeError as e:  # pypsrp missing
            return False, None, str(e), "exception"

        # UPDATES gets ONE extra attempt on a transient transport ('winrm')
        # fault — a fresh WSMan/pool each time, since a 500 may have killed the
        # target's shell. Deterministic failures (offline/ps/parse) and success
        # return immediately. The whole method is bounded by the per-item
        # executor's ``item.deadline_s``, so a genuinely slow retry just yields
        # a clean timeout instead of a floor-crossing hang; in practice a 500
        # fails fast, leaving room to recover a one-off blip in-cycle.
        max_attempts = 2 if item.check_type == CheckType.UPDATES else 1
        result: tuple[bool, Any, str | None, str | None] = (
            False, None, "no attempt made", "exception",
        )
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.info(
                    "[%s] %s hit a transient WinRM fault (%s) — retrying once",
                    server.name, item.check_type.value, result[2],
                )
                time.sleep(_TRANSIENT_RETRY_BACKOFF_S)
            try:
                wsman = wsman_factory(
                    server,
                    connection_timeout=15,
                    read_timeout=item.deadline_s + 5,
                )
                with runspace_cls(wsman) as pool:
                    result = dispatch_fn(server, pool)
            except Exception as e:
                # Categorise per v1: offline-class errors are noise during
                # reboot windows; everything else is a real winrm/transport
                # error worth alerting on.
                kind = "offline" if _is_offline_error(e) else "winrm"
                result = (
                    False,
                    None,
                    f"{type(e).__name__}: {str(e)[:300]}",
                    kind,
                )
            # Success, or a non-transient (deterministic) failure → done.
            if result[0] or result[3] != "winrm":
                return result
        return result


# ── Module-level WinRM accessors (patch points for tests) ────────────────
# Tests patch ``collector_v2.workers.make_wsman`` and
# ``collector_v2.workers.RunspacePool`` (via _get_runspace_pool_cls). Routing
# through module-level names keeps the imports inside the function (no
# pypsrp at module-load time) while still giving tests one stable place
# to patch.


def _make_wsman(server: Any, **kwargs: Any) -> Any:
    """Thin wrapper around winrm_factory.make_wsman so tests can patch
    ``collector_v2.workers.make_wsman``. (See module docstring for why.)"""
    return make_wsman(server, **kwargs)


def _get_runspace_pool_cls() -> Any:
    """Return ``pypsrp.powershell.RunspacePool`` or raise RuntimeError
    when pypsrp isn't installed. Tests patch
    ``collector_v2.workers.RunspacePool`` to control the with-block."""
    if RunspacePool is None:
        raise RuntimeError("pypsrp not installed")
    return RunspacePool


# Import-time resolution. If pypsrp/winrm_factory aren't importable
# (test env without the dep) we fall back to None / a stub. ``make_wsman``
# being a name in this module's namespace is what lets tests patch it
# at ``collector_v2.workers.make_wsman``.

try:
    from winrm_factory import make_wsman  # type: ignore[no-redef]
except Exception:  # pragma: no cover — exercised only when dep missing
    def make_wsman(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        raise RuntimeError("winrm_factory not importable in this env")

try:
    from pypsrp.powershell import RunspacePool  # type: ignore[no-redef]
except Exception:  # pragma: no cover — exercised only when dep missing
    RunspacePool = None  # type: ignore[assignment]
