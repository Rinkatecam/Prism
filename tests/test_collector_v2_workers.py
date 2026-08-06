"""Unit tests for collector_v2.workers.

Exercise the WorkerPool in isolation — no real WinRM, no database, no
supervisor thread. We mock ``make_wsman`` + the check functions and feed
WorkItems straight onto the test queue. Each test asserts on the Result
emitted to the result queue (or on the absence thereof).

Run standalone:
    python -m pytest tests/test_collector_v2_workers.py -v
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from collector_v2 import state, workers
from collector_v2.types import CheckType, Result, WorkItem


# ─────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class FakeServer:
    """Minimal ServerConfig stand-in. The check functions and make_wsman
    are mocked so we don't need real fields."""

    name: str
    host: str = "10.0.0.1"
    username: str = "admin"
    password: str = "x"
    use_https: bool = False
    https_skip_verify: bool = False
    port: int = 5985


class FakeConfig:
    """Stand-in for ConfigManager — only needs get_server_by_name."""

    def __init__(self, servers: list[FakeServer]) -> None:
        self._by_name = {s.name: s for s in servers}

    def get_server_by_name(self, name: str) -> FakeServer | None:
        return self._by_name.get(name)


@pytest.fixture(autouse=True)
def _reset_worker_stats():
    """Reset module-level counters between tests so a leaked state from a
    prior test never causes a false pass/fail."""
    with workers._stats_lock:
        workers._active_count = 0
        workers._total_processed = 0
        workers._total_offline = 0
        workers._total_timeouts = 0
        workers._total_critical_errors = 0
    workers._registered_num_workers = 0
    # Reset the worker activity heartbeat as well so we can detect new
    # advances. The state module owns this global.
    state.last_worker_activity_at = 0.0
    yield


@contextmanager
def _patched_config(servers: list[FakeServer]):
    """Install a fake ConfigManager into routes.api._shared._config for
    the duration of the test. Restores the prior value on exit so a
    test that doesn't use this fixture isn't affected."""
    from routes.api import _shared

    prev = _shared._config
    _shared._config = FakeConfig(servers)
    try:
        yield
    finally:
        _shared._config = prev


class _FakePool:
    """Stand-in for pypsrp's RunspacePool — supports the ``with`` protocol
    and returns itself as the bound name. The mocked check functions don't
    actually call any pypsrp methods on it; they ignore the value."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_runspace_pool_cls(wsman):
    """Constructor stub that returns a fresh _FakePool. Used as the
    ``RunspacePool`` class via patch."""
    return _FakePool()


def _fresh_item(
    server_name: str = "srv1",
    check_type: CheckType = CheckType.METRICS,
    deadline_s: int = 10,
    max_queue_wait_s: int = 60,
    enqueued_at: datetime | None = None,
) -> WorkItem:
    """Build a non-stale WorkItem with sensible defaults."""
    if enqueued_at is None:
        enqueued_at = datetime.now(timezone.utc)
    return WorkItem(
        server_name=server_name,
        check_type=check_type,
        enqueued_at=enqueued_at,
        deadline_s=deadline_s,
        max_queue_wait_s=max_queue_wait_s,
    )


# ─────────────────────────────────────────────────────────────────────────
# _execute_one tests — exercised directly without spinning up threads
# ─────────────────────────────────────────────────────────────────────────


def test_stale_item_returns_timeout_without_winrm():
    """An item past max_queue_wait_s should NEVER touch WinRM. The
    supervisor will reschedule it; trying to collect would waste a worker."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    stale = _fresh_item(
        enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        max_queue_wait_s=60,
    )
    assert stale.is_stale is True

    with patch("collector_v2.workers.make_wsman") as mwsman, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(stale)

    assert result.ok is False
    assert result.error_kind == "timeout"
    assert "queue" in (result.error or "").lower()
    # The crucial assertion: no WinRM call attempted.
    mwsman.assert_not_called()


def test_successful_check_emits_ok_result_with_timing():
    """Happy path: check function returns ok, Result preserves data and
    has sensible started_at/finished_at."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()
    mock_data = {"cpu": 12.5, "ram": 40.0}

    # We patch:
    #   - make_wsman so no real network call happens
    #   - RunspacePool so the with-block returns a stub pool
    #   - check_metrics so we control the return value
    with patch("collector_v2.workers.check_metrics",
               return_value=(True, mock_data, None, None)) as mcheck, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman") as mwsman, \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is True
    assert result.data == mock_data
    assert result.error is None
    assert result.duration_s >= 0
    mcheck.assert_called_once()
    mwsman.assert_called_once()


def test_check_offline_error_kind_preserved():
    """If the check function returns error_kind='offline', the Result
    MUST carry it through unmodified — the aggregator gates banner
    display on this exact value."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()
    with patch("collector_v2.workers.check_metrics",
               return_value=(False, None, "Shell not found", "offline")), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "offline"
    assert "Shell not found" in (result.error or "")


def test_deadline_expires_returns_timeout_with_duration():
    """A check that blocks past deadline_s should be cut off and yield a
    Result with error_kind='timeout' and duration_s >= deadline_s."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(deadline_s=1)  # 1s deadline

    def slow_check(server, ps_pool):
        time.sleep(3)  # Way over deadline
        return True, {"cpu": 0}, None, None

    with patch("collector_v2.workers.check_metrics", side_effect=slow_check), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "timeout"
    # Duration should be approximately the deadline — the future wakes
    # within a small fraction of a second of the deadline firing.
    assert result.duration_s >= 1.0
    assert result.duration_s < 3.0  # We didn't wait for the full sleep


def test_server_not_in_config_returns_config_missing():
    """Server deleted from config while item was queued → config_missing
    Result. NO WinRM call."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(server_name="ghost-server")

    with patch("collector_v2.workers.make_wsman") as mwsman, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            _patched_config([FakeServer("srv1")]):
        # ghost-server is NOT in the fake config — only srv1 is.
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "config_missing"
    assert "ghost-server" in (result.error or "")
    mwsman.assert_not_called()


def test_winrm_open_failure_categorised_as_offline_when_marker_matches():
    """If make_wsman / RunspacePool raises with an offline-marker pattern
    in the message, the Result should be classified 'offline'."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()

    # The string "connection refused" is in checks._OFFLINE_MARKERS.
    boom = ConnectionError("connection refused")

    with patch("collector_v2.workers.make_wsman", side_effect=boom), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "offline"


def test_winrm_open_failure_categorised_as_winrm_when_unknown():
    """Errors that DON'T match offline markers go to error_kind='winrm'."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()
    boom = RuntimeError("something genuinely unexpected")

    with patch("collector_v2.workers.make_wsman", side_effect=boom), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "winrm"


# ─────────────────────────────────────────────────────────────────────────
# UPDATES transient-transport retry — a one-off WSMan 500 on the heavy
# Windows Update COM search recovers in-cycle (fresh connection) instead of
# waiting a full update_check_interval.
# ─────────────────────────────────────────────────────────────────────────


def test_updates_retries_once_on_transient_winrm():
    """A 'winrm' transport fault on UPDATES is retried once, with a fresh
    WSMan/pool, and a second-attempt success is returned."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(check_type=CheckType.UPDATES)
    payload = {"count": 2, "updates": [], "reboot_required": False}
    with patch("collector_v2.workers.check_updates",
               side_effect=[(False, None, "WinRMTransportError: Code 500", "winrm"),
                            (True, payload, None, None)]) as mcheck, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman") as mwsman, \
            patch("collector_v2.workers.time.sleep"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is True
    assert result.data == payload
    assert mcheck.call_count == 2       # retried once
    assert mwsman.call_count == 2       # a FRESH connection per attempt


def test_updates_no_retry_on_deterministic_failure():
    """A non-transport failure kind (e.g. 'parse') is NOT retried."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(check_type=CheckType.UPDATES)
    with patch("collector_v2.workers.check_updates",
               return_value=(False, None, "Bad updates JSON", "parse")) as mcheck, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            patch("collector_v2.workers.time.sleep"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "parse"
    assert mcheck.call_count == 1


def test_updates_retry_exhausted_returns_winrm_error():
    """Both attempts transient → the winrm error is returned after 2 tries."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(check_type=CheckType.UPDATES)
    with patch("collector_v2.workers.check_updates",
               return_value=(False, None, "WinRMTransportError: Code 500", "winrm")) as mcheck, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            patch("collector_v2.workers.time.sleep"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "winrm"
    assert mcheck.call_count == 2


def test_non_updates_check_not_retried_on_winrm():
    """Only UPDATES gets the extra attempt — METRICS fails once and returns."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item(check_type=CheckType.METRICS)
    with patch("collector_v2.workers.check_metrics",
               return_value=(False, None, "WinRMTransportError: Code 500", "winrm")) as mcheck, \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            patch("collector_v2.workers.time.sleep"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert mcheck.call_count == 1       # no retry for non-UPDATES


# ─────────────────────────────────────────────────────────────────────────
# Loop / threading / lifecycle tests
# ─────────────────────────────────────────────────────────────────────────


def test_worker_pool_starts_n_threads():
    """start() spawns exactly num_workers threads, all with the expected
    name prefix and daemon=True."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=4)

    try:
        pool.start()
        # Count threads with the right name prefix to avoid false matches
        # against pytest/internal threads.
        worker_threads = [
            t for t in threading.enumerate()
            if t.name.startswith("prism-collector-v2-worker-")
        ]
        assert len(worker_threads) == 4
        assert all(t.daemon for t in worker_threads)
        assert workers._registered_num_workers == 4
    finally:
        pool.stop()


def test_poison_pill_lets_worker_exit_cleanly():
    """stop() pushes a sentinel that wakes a blocked queue.get and the
    worker exits without processing it as a real item."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=2)

    pool.start()
    # No work has been enqueued — workers are blocking on queue.get.
    pool.stop(join_timeout_s=5.0)

    # All worker threads gone — pool._threads cleared.
    assert pool._threads == []
    # No result emitted (no real work was done).
    assert rq.empty()


def test_workers_survive_empty_queue_timeout():
    """Regression: when queue.get times out (queue.Empty), workers must NOT
    exit. They must loop and try again. The original implementation
    conflated the timeout case with poison-pill (both returned None from
    `_pull_next_item`), so all workers exited as soon as the queue went
    briefly idle — which happens at every startup before the supervisor's
    first tick. This test ensures the fix doesn't regress.

    Setup: start the pool with NO items in the queue, wait ~2s (long enough
    for the 1.0s poll timeout to fire at least twice on each worker), then
    push an item. Workers should still be alive and consume it.
    """
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=3)
    pool.start()
    try:
        # Wait long enough for the 1s queue.get(timeout=1.0) to fire several
        # times on every worker. If the regression resurfaces, all threads
        # exit during this window.
        time.sleep(2.5)
        # All worker threads are still alive
        assert all(t.is_alive() for t in pool._threads), (
            "Workers exited during empty-queue window (the original bug)"
        )
        # Now push an item — workers should pick it up
        with patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
                patch("collector_v2.workers.make_wsman"), \
                patch("collector_v2.workers.check_metrics",
                      return_value=(True, {"cpu": 1.0, "ram": 1.0, "disk_c": 1.0, "disk_d": -1.0,
                                            "collection_time_ms": 5}, None, None)), \
                _patched_config([FakeServer("SRV02")]):
            wq.put(_fresh_item("SRV02"))
            result = rq.get(timeout=3.0)
        assert result.ok
        assert result.item.server_name == "SRV02"
    finally:
        pool.stop(join_timeout_s=2.0)


def test_heartbeat_advances_on_each_pull():
    """state.heartbeat_worker() should be called on every pull. We watch
    the global timestamp advance after enqueueing an item."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()
    state.last_worker_activity_at = 0.0

    with patch("collector_v2.workers.check_metrics",
               return_value=(True, {"cpu": 1}, None, None)), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):

        pool.start()
        wq.put(item)
        # Wait briefly for the worker to pull + process.
        deadline = time.time() + 3.0
        while time.time() < deadline and rq.empty():
            time.sleep(0.05)
        pool.stop(join_timeout_s=2.0)

    assert not rq.empty(), "Worker never produced a Result"
    assert state.last_worker_activity_at > 0.0, \
        "heartbeat_worker should have advanced the timestamp"


def test_stats_counters_increment_correctly():
    """Verify total_processed, total_offline, total_timeouts increment
    in the expected directions across a small batch."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    # Item 1: success
    ok_item = _fresh_item(server_name="srv1")
    # Item 2: offline-classified result
    off_item = _fresh_item(server_name="srv1")
    # Item 3: stale → timeout (synthesised by us)
    stale_item = _fresh_item(
        server_name="srv1",
        enqueued_at=datetime.now(timezone.utc) - timedelta(seconds=200),
        max_queue_wait_s=60,
    )

    call_count = {"n": 0}

    def fake_check(server, ps_pool):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return True, {"cpu": 1}, None, None
        return False, None, "shell was not found", "offline"

    with patch("collector_v2.workers.check_metrics", side_effect=fake_check), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):

        pool.start()
        wq.put(ok_item)
        wq.put(off_item)
        wq.put(stale_item)
        deadline = time.time() + 5.0
        while time.time() < deadline and rq.qsize() < 3:
            time.sleep(0.05)
        pool.stop(join_timeout_s=2.0)

    h = workers.get_worker_pool_health()
    assert h["total_processed"] == 3
    assert h["total_offline"] >= 1
    assert h["total_timeouts"] >= 1


def test_bulletproof_catch_keeps_worker_alive():
    """If _execute_one raises an uncaught exception, the worker loop's
    bulletproof outer catch must increment the critical counter and
    continue processing further items. The worker thread must NOT die."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item1 = _fresh_item(server_name="boom-srv")
    item2 = _fresh_item(server_name="srv1")

    # Patch _execute_one on the instance so item1 explodes but item2
    # falls through to the real implementation.
    real_execute = pool._execute_one
    call_count = {"n": 0}

    def buggy_execute(item):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("deliberate explosion")
        return real_execute(item)

    pool._execute_one = buggy_execute  # type: ignore[method-assign]

    with patch("collector_v2.workers.check_metrics",
               return_value=(True, {"cpu": 1}, None, None)), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):

        pool.start()
        wq.put(item1)
        wq.put(item2)
        # Wait for the second item to produce a Result — that's how we
        # know the worker survived the first explosion.
        deadline = time.time() + 5.0
        while time.time() < deadline and rq.empty():
            time.sleep(0.05)
        pool.stop(join_timeout_s=2.0)

    # item2 produced a Result → worker survived the bulletproof catch.
    assert not rq.empty(), "Worker died after the deliberate exception"
    h = workers.get_worker_pool_health()
    assert h["total_critical_errors"] >= 1


def test_start_twice_raises():
    """Calling start() twice without stop() should be loud, not silent."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)
    try:
        pool.start()
        with pytest.raises(RuntimeError):
            pool.start()
    finally:
        pool.stop(join_timeout_s=2.0)


def test_execute_one_emits_result_even_when_inner_raises():
    """Regression for audit M3: any exception escaping the inner try
    inside _execute_one MUST become a Result with error_kind='exception'
    rather than propagating up to the worker loop's bulletproof catch.

    Without this guarantee, the supervisor's pending[ct] flag for that
    (server, check_type) stays True forever and the supervisor never
    enqueues another check for that pair.

    Setup: patch _invoke_with_deadline to raise on first call, then
    confirm _execute_one still returns a Result that the aggregator can
    use to clear pending.
    """
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated bug deep in check code")

    with patch.object(pool, "_invoke_with_deadline", side_effect=_explode), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    # The function must return a Result, NOT propagate the exception.
    assert isinstance(result, Result), (
        "Unhandled exception in _execute_one escaped the function — "
        "this would strand supervisor's pending flag forever (audit M3)"
    )
    assert result.ok is False
    assert result.error_kind == "exception"
    assert "RuntimeError" in (result.error or "")
    # The critical-error counter advanced so the watchdog notices.
    assert workers.get_worker_pool_health()["total_critical_errors"] >= 1


def test_unknown_check_type_returns_exception():
    """If somehow an unknown CheckType arrives (e.g. enum drift), the
    worker should return a Result with error_kind='exception' rather
    than crashing."""
    wq: queue.Queue = queue.Queue()
    rq: queue.Queue = queue.Queue()
    pool = workers.WorkerPool(wq, rq, num_workers=1)

    item = _fresh_item()
    # Patch out the dispatch table to simulate an unknown check_type.
    with patch.dict(workers._CHECK_DISPATCH, {}, clear=True), \
            patch("collector_v2.workers.RunspacePool", _fake_runspace_pool_cls), \
            patch("collector_v2.workers.make_wsman"), \
            _patched_config([FakeServer("srv1")]):
        result = pool._execute_one(item)

    assert result.ok is False
    assert result.error_kind == "exception"
