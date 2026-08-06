"""Tests for the v2 pulse buffer — the in-memory ring that feeds the
topbar ECG widget.

The buffer is intentionally simple (collections.deque + a small lock),
so the tests focus on the contract:

  * record_pulse → get_recent_pulses round-trips correctly
  * The ``since_ts`` watermark filter is strictly greater-than (so the
    widget never re-renders an event it already drew)
  * The fixed window fallback works when no watermark is given
  * maxlen=1000 evicts oldest, never blocks the writer
  * Concurrent writers don't crash the snapshot reader

The fleet rollup (``get_fleet_status``) is also exercised here because it
lives in the same module and shares the lock; same goes for the in-flight
listing.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture(autouse=True)
def _clean_pulse_buffer():
    """Every test gets a fresh buffer + clean server_health."""
    from collector_v2 import state
    state.clear_pulses()
    # Snapshot + clear server_health so each test starts deterministic.
    with state._server_health_lock:
        saved = dict(state.server_health)
        state.server_health.clear()
    yield
    state.clear_pulses()
    with state._server_health_lock:
        state.server_health.clear()
        state.server_health.update(saved)


# ─────────────────────────────────────────────────────────────────────
# record_pulse + get_recent_pulses round-trip
# ─────────────────────────────────────────────────────────────────────

def test_record_then_read_returns_event_in_window():
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now, "srv1", "METRICS", True, 240)

    out = state.get_recent_pulses(window_s=12.0)
    assert len(out) == 1
    ev = out[0]
    assert ev["server"] == "srv1"
    assert ev["check"] == "METRICS"
    assert ev["ok"] is True
    assert ev["ms"] == 240
    assert ev["ts"] == pytest.approx(now, abs=0.001)


def test_event_older_than_window_is_excluded():
    """Events outside the requested window must NOT be returned, so the
    canvas doesn't keep redrawing ancient beats forever."""
    from collector_v2 import state
    state.record_pulse(time.time() - 30, "srv1", "METRICS", True, 240)
    out = state.get_recent_pulses(window_s=12.0)
    assert out == []


def test_since_watermark_is_strictly_greater_than():
    """The widget passes the last event's ts as the next poll's since.
    The endpoint must NOT return that event again — it would re-draw."""
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now, "srv1", "METRICS", True, 100)
    state.record_pulse(now + 1.0, "srv2", "METRICS", True, 110)

    # Using srv1's ts as the watermark, srv1 must be excluded.
    out = state.get_recent_pulses(since_ts=now)
    assert len(out) == 1
    assert out[0]["server"] == "srv2"


def test_since_watermark_in_the_future_returns_nothing():
    from collector_v2 import state
    state.record_pulse(time.time(), "srv1", "METRICS", True, 100)
    out = state.get_recent_pulses(since_ts=time.time() + 60)
    assert out == []


def test_window_fallback_when_no_watermark():
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now - 2, "srv1", "METRICS", True, 100)
    state.record_pulse(now - 20, "srv2", "METRICS", True, 100)  # outside default window
    out = state.get_recent_pulses(window_s=12.0)
    assert len(out) == 1
    assert out[0]["server"] == "srv1"


def test_failed_pulse_marked_ok_false():
    """Failure spikes drive the red beats on the canvas — preserve the bool."""
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now, "srv1", "METRICS", False, 5000)
    out = state.get_recent_pulses(window_s=12.0)
    assert out[0]["ok"] is False


def test_maxlen_evicts_oldest_silently():
    """The buffer is bounded — writer must never block. Old events fall
    off the left silently. Important so the aggregator hot path stays fast."""
    from collector_v2 import state
    # Push more than maxlen, oldest should be evicted.
    base = time.time()
    for i in range(1200):
        state.record_pulse(base + i * 0.01, f"srv{i % 5}", "METRICS", True, 100)
    # Buffer cap is 1000 → we should see exactly that many in a wide window.
    out = state.get_recent_pulses(window_s=1e9)
    assert len(out) == 1000
    # The earliest 200 should have been evicted — the smallest ts we see
    # should correspond to the 200th insert.
    assert out[0]["ts"] == pytest.approx(base + 200 * 0.01, abs=0.001)


def test_record_pulse_swallows_bad_inputs():
    """The aggregator's hot path must never get a traceback from this.
    A malformed call is silently dropped."""
    from collector_v2 import state
    # None values would crash int()/bool() if we let them through. We
    # don't promise to record them — we promise to not raise.
    try:
        state.record_pulse(None, "srv1", "METRICS", True, 100)  # type: ignore[arg-type]
    except Exception as e:
        pytest.fail(f"record_pulse should swallow bad input, got {e!r}")


def test_concurrent_writers_do_not_crash_reader():
    """30 threads appending while a snapshot reader iterates must not raise
    "deque mutated during iteration"."""
    from collector_v2 import state
    stop = threading.Event()

    def writer(idx: int):
        while not stop.is_set():
            state.record_pulse(time.time(), f"srv{idx}", "METRICS", True, 50)

    threads = [threading.Thread(target=writer, args=(i,), daemon=True)
               for i in range(20)]
    for t in threads:
        t.start()
    try:
        # Hammer the reader from the main thread for ~250ms. Both writer
        # and reader take _pulse_lock, so this exercises lock contention
        # too. Test passes as long as no exception escapes — we don't
        # care about throughput, just correctness.
        deadline = time.time() + 0.25
        snapshot_count = 0
        while time.time() < deadline:
            out = state.get_recent_pulses(window_s=12.0)
            snapshot_count += 1
            assert isinstance(out, list)
        assert snapshot_count >= 1, "reader should have completed at least one snapshot"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=1.0)


# ─────────────────────────────────────────────────────────────────────
# get_fleet_status — drives the up/total counter and silent list
# ─────────────────────────────────────────────────────────────────────

def _make_server_health(name: str, metrics_last_ok_at):
    """Helper: register a ServerHealth in state with a controlled METRICS
    last_ok_at. ``metrics_last_ok_at`` may be a datetime or None."""
    from collector_v2 import state
    from collector_v2.types import ServerHealth, CheckType, CheckState
    now = datetime.now(timezone.utc)
    h = ServerHealth(name=name)
    # ServerHealth keeps a checks dict; we create a METRICS CheckState
    # with the desired last_ok_at so get_fleet_status sees the freshness
    # we want to test.
    h.checks[CheckType.METRICS] = CheckState(
        next_due_at=now,
        last_ok_at=metrics_last_ok_at,
        consecutive_failures=0,
        pending=False,
    )
    state.upsert_server_health(name, h)


def test_fleet_empty_when_no_servers_registered():
    from collector_v2 import state
    fleet = state.get_fleet_status()
    assert fleet == {"total": 0, "up": 0, "silent": []}


def test_fleet_counts_recent_metrics_as_up():
    from collector_v2 import state
    now = datetime.now(timezone.utc)
    _make_server_health("fresh1", now - timedelta(seconds=10))
    _make_server_health("fresh2", now - timedelta(seconds=60))
    fleet = state.get_fleet_status()
    assert fleet["total"] == 2
    assert fleet["up"] == 2
    assert fleet["silent"] == []


def test_fleet_marks_old_metrics_as_silent():
    """Server whose last successful METRICS is > 5 min ago = silent."""
    from collector_v2 import state
    now = datetime.now(timezone.utc)
    _make_server_health("fresh", now - timedelta(seconds=30))
    _make_server_health("stale", now - timedelta(seconds=600))
    fleet = state.get_fleet_status()
    assert fleet["total"] == 2
    assert fleet["up"] == 1
    assert len(fleet["silent"]) == 1
    assert fleet["silent"][0]["name"] == "stale"
    assert fleet["silent"][0]["silent_s"] is not None
    assert fleet["silent"][0]["silent_s"] > 300


def test_fleet_never_reported_server_is_silent_with_none_age():
    """A server registered but never successfully sampled — show as silent
    with silent_s=None so the UI can render "no samples yet" instead of "Xs"."""
    from collector_v2 import state
    _make_server_health("new", None)
    fleet = state.get_fleet_status()
    assert fleet["up"] == 0
    assert fleet["silent"][0] == {"name": "new", "silent_s": None}


def test_fleet_silent_list_sorted_worst_first():
    """UI shows worst offender at the top → list must be sorted by
    silent_s descending, with None (never-reported) first."""
    from collector_v2 import state
    now = datetime.now(timezone.utc)
    _make_server_health("medium", now - timedelta(seconds=400))
    _make_server_health("worst", now - timedelta(seconds=900))
    _make_server_health("never", None)
    fleet = state.get_fleet_status()
    names = [s["name"] for s in fleet["silent"]]
    assert names == ["never", "worst", "medium"]


# ─────────────────────────────────────────────────────────────────────
# get_in_flight — drives the "IN FLIGHT" panel section
# ─────────────────────────────────────────────────────────────────────

def test_in_flight_returns_pending_checks():
    from collector_v2 import state
    from collector_v2.types import ServerHealth, CheckType, CheckState
    now = datetime.now(timezone.utc)
    h = ServerHealth(name="srv1")
    h.checks[CheckType.METRICS] = CheckState(next_due_at=now, pending=True)
    h.checks[CheckType.LOGS] = CheckState(next_due_at=now, pending=False)
    state.upsert_server_health("srv1", h)

    inflight = state.get_in_flight()
    assert len(inflight) == 1
    assert inflight[0] == {"name": "srv1", "check": "metrics"}


def test_in_flight_empty_when_nothing_pending():
    from collector_v2 import state
    from collector_v2.types import ServerHealth, CheckType, CheckState
    now = datetime.now(timezone.utc)
    h = ServerHealth(name="srv1")
    h.checks[CheckType.METRICS] = CheckState(next_due_at=now, pending=False)
    state.upsert_server_health("srv1", h)
    assert state.get_in_flight() == []
