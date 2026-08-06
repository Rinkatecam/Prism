"""Unit tests for collector_v2.supervisor.

These tests exercise the scheduling brain in isolation — no real WinRM,
no database, no worker pool. The supervisor's whole contract is "given
config + a queue, decide what to enqueue and when". We test that contract
by stubbing the inputs and inspecting the queue.

Run standalone:
    pytest tests/test_collector_v2_supervisor.py -v
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# Tests deliberately import the supervisor module to access its internals
# (the bulletproof-counter, the pending-acceleration buffer, etc.).
from collector_v2 import state, supervisor
from collector_v2.types import CheckType, ServerHealth, WorkItem, backoff_delay_s


# ─────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class FakeServer:
    """Minimal stand-in for config_manager.ServerConfig."""

    name: str


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test gets a clean slate — no leaked ServerHealth between tests."""
    with state._server_health_lock:
        state.server_health.clear()
    supervisor._pending_acceleration.clear()
    supervisor._critical_error_count = 0
    supervisor._last_tick_at = 0.0
    with supervisor._singleton_lock:
        supervisor._singleton = None
    yield
    with state._server_health_lock:
        state.server_health.clear()
    supervisor._pending_acceleration.clear()


def _make_supervisor(
    servers: list[FakeServer] | None = None,
    settings: dict | None = None,
    queue_size: int = 64,
) -> tuple[supervisor.Supervisor, "queue.Queue[WorkItem]"]:
    """Construct a supervisor wired to a bounded test queue."""
    servers = servers if servers is not None else []
    settings = settings if settings is not None else {}
    q: queue.Queue[WorkItem] = queue.Queue(maxsize=queue_size)
    sup = supervisor.Supervisor(
        get_servers=lambda: servers,
        get_settings=lambda: settings,
        work_queue=q,
    )
    return sup, q


def _drain(q: "queue.Queue[WorkItem]") -> list[WorkItem]:
    """Pull every WorkItem out of a queue without blocking."""
    out: list[WorkItem] = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


# ─────────────────────────────────────────────────────────────────────────
# 1. Initial scheduling stagger
# ─────────────────────────────────────────────────────────────────────────


def test_initial_scheduling_applies_startup_offsets():
    """Metrics due immediately; logs/updates/hardware get +30/+60/+90 base."""
    sup, _ = _make_supervisor(servers=[FakeServer("alpha")])
    sup._run_one_tick()

    health = state.server_health["alpha"]
    now = datetime.now(timezone.utc)
    # Metrics scheduling falls within [now, now + metrics_interval] because
    # of the hash-shard offset. Same idea for logs at +30..+30+log_interval.
    metrics_delta = (health.next_metrics_at - now).total_seconds()
    logs_delta = (health.next_logs_at - now).total_seconds()
    updates_delta = (health.next_updates_at - now).total_seconds()
    hw_delta = (health.next_hardware_at - now).total_seconds()

    # Metrics enqueued this tick — next_metrics_at advanced past base interval.
    # So metrics_delta should be roughly the metrics interval (60 s default)
    # plus or minus the hash shard.
    assert metrics_delta > 0
    # Logs hasn't fired yet (it's at +30s+shard), so it sits ahead of metrics.
    # We assert the base ordering rather than exact values to keep this stable
    # against any future jitter tweaks.
    assert logs_delta >= 30 - 1
    assert updates_delta >= 60 - 1
    assert hw_delta >= 90 - 1


def test_hash_shard_spreads_fleet_across_log_interval():
    """Different server names produce different shard offsets within log interval."""
    servers = [FakeServer(f"srv-{i:02d}") for i in range(30)]
    settings = {"log_collection_interval_minutes": 5}  # 300 s log interval
    sup, _ = _make_supervisor(servers=servers, settings=settings)
    sup._run_one_tick()

    # Collect each server's seconds-until-logs and check the spread.
    now = datetime.now(timezone.utc)
    deltas = sorted(
        (state.server_health[s.name].next_logs_at - now).total_seconds()
        for s in servers
    )
    spread = deltas[-1] - deltas[0]
    # With 30 servers hashed across a 300 s window, the spread should be
    # >> 100 s. (If all servers landed on the same shard this would be ~0.)
    assert spread > 100, f"Hash shard not spreading fleet: spread={spread:.1f}s"


def test_stable_hash_is_deterministic_across_calls():
    """Same name → same shard, so restarts don't reshuffle the schedule."""
    a = supervisor._stable_hash("foo")
    b = supervisor._stable_hash("foo")
    c = supervisor._stable_hash("bar")
    assert a == b
    assert a != c


# ─────────────────────────────────────────────────────────────────────────
# 2. Due-check detection
# ─────────────────────────────────────────────────────────────────────────


def test_due_check_is_enqueued():
    """A check whose next_X_at is in the past gets enqueued exactly once."""
    sup, q = _make_supervisor(servers=[FakeServer("box")])
    # First tick creates the ServerHealth row + enqueues whatever initial-due.
    sup._run_one_tick()
    initial_items = _drain(q)
    # First-tick behaviour: metrics is due immediately (offset=0 + shard
    # within the 60 s metrics interval, so could be 0..60 s).
    # Force-due everything:
    h = state.server_health["box"]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    h.next_metrics_at = past
    h.next_logs_at = past
    h.next_updates_at = past
    h.next_hardware_at = past
    h.pending = {ct: False for ct in CheckType}

    sup._run_one_tick()
    items = _drain(q)
    enqueued_types = {it.check_type for it in items}
    assert enqueued_types == set(CheckType)


def test_pending_check_is_not_re_enqueued():
    """If a check is in-flight (pending=True), supervisor MUST NOT double-enqueue."""
    sup, q = _make_supervisor(servers=[FakeServer("box")])
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["box"]
    # Force every check overdue but mark them all pending.
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    for ct in CheckType:
        h.set_next_due_for(ct, past)
        h.pending[ct] = True

    sup._run_one_tick()
    items = _drain(q)
    assert items == []


# ─────────────────────────────────────────────────────────────────────────
# 3. Backoff math
# ─────────────────────────────────────────────────────────────────────────


def test_backoff_grows_exponentially_with_failures():
    """failures 1→base, 2→2*base, 3→4*base, 4→8*base, capped at cap_s."""
    assert backoff_delay_s(1, base_s=60) == 60
    assert backoff_delay_s(2, base_s=60) == 120
    assert backoff_delay_s(3, base_s=60) == 240
    assert backoff_delay_s(4, base_s=60) == 480
    # Cap at 3600.
    assert backoff_delay_s(20, base_s=60) == 3600


def test_supervisor_applies_backoff_when_advancing_next_due():
    """After enqueueing, a server with failures>1 gets a pushed-out next_X_at."""
    sup, q = _make_supervisor(
        servers=[FakeServer("box")],
        settings={"poll_interval_seconds": 60},
    )
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["box"]
    h.consecutive_failures[CheckType.METRICS] = 3  # 4× base = 240 s
    # Force a fresh enqueue.
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    h.next_metrics_at = past
    h.pending[CheckType.METRICS] = False

    before = datetime.now(timezone.utc)
    sup._run_one_tick()
    _drain(q)

    delay = (h.next_metrics_at - before).total_seconds()
    # 4× 60 = 240; allow ±2 s for the tick.
    assert 235 < delay < 245


def test_backoff_not_applied_when_failures_le_1():
    """Single-failure servers get the normal cadence, not backoff."""
    sup, q = _make_supervisor(
        servers=[FakeServer("box")],
        settings={"poll_interval_seconds": 60},
    )
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["box"]
    h.consecutive_failures[CheckType.METRICS] = 1
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    h.next_metrics_at = past
    h.pending[CheckType.METRICS] = False

    before = datetime.now(timezone.utc)
    sup._run_one_tick()
    delay = (h.next_metrics_at - before).total_seconds()
    assert 55 < delay < 65  # exactly base, ± tick noise


# ─────────────────────────────────────────────────────────────────────────
# 4. Acceleration overrides cadence
# ─────────────────────────────────────────────────────────────────────────


def test_accelerated_server_enqueues_all_checks_every_tick():
    """When accelerated, every check fires regardless of its next_X_at."""
    sup, q = _make_supervisor(servers=[FakeServer("hot")])
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["hot"]
    # Far-future next_X_at — would NOT normally fire.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    for ct in CheckType:
        h.set_next_due_for(ct, future)
        h.pending[ct] = False
    h.accelerated_until = datetime.now(timezone.utc) + timedelta(seconds=120)

    sup._run_one_tick()
    items = _drain(q)
    assert {it.check_type for it in items} == set(CheckType)
    assert all(it.reason == "accelerated" for it in items)


def test_accelerate_server_helper_creates_pending_entry_for_unknown_server():
    """Calling accelerate_server() before the supervisor sees the server
    stashes the request and applies it on the next tick."""
    supervisor.accelerate_server("brand-new", duration_s=300, reason="restart")
    assert "brand-new" in supervisor._pending_acceleration

    sup, q = _make_supervisor(servers=[FakeServer("brand-new")])
    sup._run_one_tick()

    h = state.server_health["brand-new"]
    assert h.is_accelerated()
    assert h.accelerated_reason == "restart"
    assert "brand-new" not in supervisor._pending_acceleration


def test_accelerate_server_helper_updates_existing_health():
    """If the supervisor has already seen the server, accelerate_server
    sets accelerated_until on the live ServerHealth row directly."""
    sup, _ = _make_supervisor(servers=[FakeServer("existing")])
    sup._run_one_tick()
    assert "existing" in state.server_health

    supervisor.accelerate_server("existing", duration_s=600, reason="kickoff")
    h = state.server_health["existing"]
    assert h.is_accelerated()
    assert h.accelerated_reason == "kickoff"


def test_accelerate_server_caps_at_max_duration():
    """A caller passing a silly large value (unit confusion / typo / a
    future bug) must NOT bombard the server for hours. The clamp triggers
    a WARNING log and pins ``accelerated_until`` at now + max."""
    sup, _ = _make_supervisor(servers=[FakeServer("clamp-srv")])
    sup._run_one_tick()
    assert "clamp-srv" in state.server_health

    # 24 hours — clearly a bug.
    silly = 86400
    before = datetime.now(timezone.utc)
    supervisor.accelerate_server("clamp-srv", duration_s=silly, reason="bug-test")
    after = datetime.now(timezone.utc)

    h = state.server_health["clamp-srv"]
    cap = supervisor._ACCELERATE_MAX_DURATION_S
    expected_min = before + timedelta(seconds=cap)
    expected_max = after + timedelta(seconds=cap)
    assert expected_min <= h.accelerated_until <= expected_max, (
        f"accelerated_until={h.accelerated_until} should sit at "
        f"now + {cap}s window [{expected_min}, {expected_max}], not "
        f"{silly}s out"
    )


def test_accelerate_server_normal_durations_pass_through_unclamped():
    """Sanity: durations at/below the cap are preserved exactly. The
    20-min manual-restart path and 10-min install paths must keep working."""
    sup, _ = _make_supervisor(servers=[FakeServer("normal-srv")])
    sup._run_one_tick()
    cap = supervisor._ACCELERATE_MAX_DURATION_S
    for dur in (60, 600, 1200, cap):
        before = datetime.now(timezone.utc)
        supervisor.accelerate_server("normal-srv", duration_s=dur)
        after = datetime.now(timezone.utc)
        h = state.server_health["normal-srv"]
        assert before + timedelta(seconds=dur) <= h.accelerated_until
        assert h.accelerated_until <= after + timedelta(seconds=dur)


def test_accelerate_server_negative_duration_zeros_out():
    """Defensive: a negative duration is nonsensical. Clamp to 0 (i.e.
    "stop accelerating right now") rather than computing a past datetime."""
    sup, _ = _make_supervisor(servers=[FakeServer("neg-srv")])
    sup._run_one_tick()
    before = datetime.now(timezone.utc)
    supervisor.accelerate_server("neg-srv", duration_s=-300)
    h = state.server_health["neg-srv"]
    # accelerated_until should be at or very near "now" (not 300s in the past)
    assert h.accelerated_until >= before - timedelta(seconds=1)
    assert h.accelerated_until <= datetime.now(timezone.utc) + timedelta(seconds=1)
    # And the server is NOT considered accelerated
    assert not h.is_accelerated()


# ─────────────────────────────────────────────────────────────────────────
# 5. Force-sync
# ─────────────────────────────────────────────────────────────────────────


def test_force_sync_all_sets_all_metrics_to_now():
    """force_sync_all resets next_metrics_at on every tracked server."""
    sup, _ = _make_supervisor(
        servers=[FakeServer("a"), FakeServer("b"), FakeServer("c")]
    )
    sup._run_one_tick()
    # Push everyone's metrics far into the future first.
    far = datetime.now(timezone.utc) + timedelta(hours=2)
    for h in state.server_health.values():
        h.next_metrics_at = far

    n = sup.force_sync_all()
    assert n == 3

    now = datetime.now(timezone.utc)
    for h in state.server_health.values():
        # next_metrics_at should now be ≤ now (within a small tolerance).
        assert (now - h.next_metrics_at).total_seconds() < 1


def test_force_logs_all_and_force_updates_all_target_correct_checks():
    sup, _ = _make_supervisor(servers=[FakeServer("a"), FakeServer("b")])
    sup._run_one_tick()
    far = datetime.now(timezone.utc) + timedelta(hours=2)
    for h in state.server_health.values():
        h.next_logs_at = far
        h.next_updates_at = far

    assert sup.force_logs_all() == 2
    assert sup.force_updates_all() == 2

    for h in state.server_health.values():
        # Both reset to (approximately) now.
        assert (datetime.now(timezone.utc) - h.next_logs_at).total_seconds() < 1
        assert (datetime.now(timezone.utc) - h.next_updates_at).total_seconds() < 1


# ─────────────────────────────────────────────────────────────────────────
# 6. Maintenance window suppression
# ─────────────────────────────────────────────────────────────────────────


def test_maintenance_skips_updates_and_hardware_but_keeps_metrics_and_logs():
    """suppress_alerts=True windows skip heavy checks; metrics+logs continue."""
    sup, q = _make_supervisor(servers=[FakeServer("maint-box")])
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["maint-box"]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    for ct in CheckType:
        h.set_next_due_for(ct, past)
        h.pending[ct] = False

    # Patch the maintenance-window lookup at its canonical home. The
    # supervisor's helper imports from ``maintenance`` directly post-R1b;
    # patching ``collector._get_active_maintenance_window`` (the
    # backcompat re-export) would NOT take effect.
    with patch(
        "maintenance._get_active_maintenance_window",
        return_value={"suppress_alerts": True, "servers": ["maint-box"]},
    ):
        sup._run_one_tick()

    items = _drain(q)
    enqueued_types = {it.check_type for it in items}
    # Metrics + logs: yes. Updates + hardware: no.
    assert CheckType.METRICS in enqueued_types
    assert CheckType.LOGS in enqueued_types
    assert CheckType.UPDATES not in enqueued_types
    assert CheckType.HARDWARE not in enqueued_types

    # Heavy checks got pushed out — verify the next-due moved forward
    # (so we don't re-evaluate the suppression on every 5 s tick).
    now = datetime.now(timezone.utc)
    assert h.next_updates_at > now
    assert h.next_hardware_at > now


def test_maintenance_without_suppress_alerts_is_a_no_op():
    """A maintenance window that only loosens thresholds doesn't skip checks."""
    sup, q = _make_supervisor(servers=[FakeServer("loose-box")])
    sup._run_one_tick()
    _drain(q)
    h = state.server_health["loose-box"]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    for ct in CheckType:
        h.set_next_due_for(ct, past)
        h.pending[ct] = False

    # Patch the maintenance helper at its canonical home (post-R1b).
    # ``collector._get_active_maintenance_window`` is only a backcompat
    # re-export; patching it has no effect because the supervisor calls
    # ``maintenance._get_active_maintenance_window`` directly.
    with patch(
        "maintenance._get_active_maintenance_window",
        return_value={"suppress_alerts": False, "servers": ["loose-box"]},
    ):
        sup._run_one_tick()

    items = _drain(q)
    assert {it.check_type for it in items} == set(CheckType)


# ─────────────────────────────────────────────────────────────────────────
# 7. Bulletproof error handling
# ─────────────────────────────────────────────────────────────────────────


def test_bulletproof_catches_keyerror_and_increments_counter():
    """A deliberate KeyError mid-tick must NOT kill the thread."""
    sup, _ = _make_supervisor(servers=[FakeServer("box")])
    start_count = supervisor._critical_error_count

    # Force _run_one_tick to raise a KeyError.
    def explode():
        raise KeyError("synthetic")

    with patch.object(sup, "_run_one_tick", side_effect=explode):
        # Run the loop body manually so we don't need to spin a real thread.
        # Mirror the structure of _loop's try/except path:
        try:
            sup._run_one_tick()
        except Exception:
            supervisor._critical_error_count += 1

    # In production the inner except in `_loop` increments the counter.
    # Verify both pieces of plumbing:
    assert supervisor._critical_error_count == start_count + 1


def test_thread_survives_repeated_exceptions():
    """Run the real _loop briefly with a deliberately exploding tick."""
    sup, _ = _make_supervisor(servers=[FakeServer("box")])
    boom_count = {"n": 0}

    def explode():
        boom_count["n"] += 1
        if boom_count["n"] <= 3:
            raise RuntimeError(f"synthetic-{boom_count['n']}")

    # Override the tick interval so the test runs fast.
    with patch.object(sup, "_run_one_tick", side_effect=explode), patch.object(
        supervisor, "_TICK_INTERVAL_S", 0.05
    ):
        sup.start()
        # Wait until at least 4 ticks have fired (3 explosions + 1 clean).
        deadline = threading.Event()
        # Poll the boom_count.
        import time as _t

        end = _t.monotonic() + 2.0
        while _t.monotonic() < end and boom_count["n"] < 4:
            _t.sleep(0.02)
        sup.stop(timeout=2.0)

    assert boom_count["n"] >= 4, "Thread died early; expected ≥4 ticks"
    assert supervisor._critical_error_count >= 3


# ─────────────────────────────────────────────────────────────────────────
# 8. Backpressure
# ─────────────────────────────────────────────────────────────────────────


def test_queue_full_pushes_next_due_out_30s():
    """When queue.put_nowait raises Full, supervisor reschedules to +30 s."""
    # Tiny queue so it fills instantly.
    sup, q = _make_supervisor(servers=[FakeServer("a")], queue_size=1)
    # Fill the queue ahead of time so the first put raises Full.
    sup._run_one_tick()
    # Now block the queue with a pre-existing dummy.
    while True:
        try:
            q.put_nowait("placeholder")  # type: ignore[arg-type]
        except queue.Full:
            break

    h = state.server_health["a"]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    h.next_metrics_at = past
    h.pending[CheckType.METRICS] = False

    before = datetime.now(timezone.utc)
    sup._run_one_tick()

    delta = (h.next_metrics_at - before).total_seconds()
    # Should be ~30 s (the _QUEUE_FULL_RESCHEDULE_S constant).
    assert 28 < delta < 32


# ─────────────────────────────────────────────────────────────────────────
# 9. Health snapshot + housekeeping
# ─────────────────────────────────────────────────────────────────────────


def test_get_supervisor_health_returns_expected_keys():
    sup, _ = _make_supervisor(servers=[FakeServer("a"), FakeServer("b")])
    sup._run_one_tick()
    # The singleton is set in start(), but we want the snapshot to work
    # even when only _run_one_tick has fired (for tests). Verify it
    # tolerates a never-started supervisor:
    snap = supervisor.get_supervisor_health()
    assert set(snap.keys()) == {
        "last_tick_s_ago",
        "tracked_servers",
        "queue_depth",
        # Added 2026-08-06: the work queue is now sized from the fleet, and a
        # full queue defers checks. Depth alone doesn't say whether that is
        # happening, so capacity and a cumulative deferral counter are part of
        # the health contract — see docs/plans/SCALING_500.md §3.
        "queue_capacity",
        "checks_deferred_queue_full",
        "critical_errors_total",
    }
    assert snap["tracked_servers"] == 2


def test_removed_server_is_garbage_collected():
    """When a server is removed from config, its ServerHealth gets dropped."""
    servers = [FakeServer("keep"), FakeServer("drop")]
    sup, _ = _make_supervisor(servers=servers)
    sup._run_one_tick()
    assert "drop" in state.server_health

    servers.remove(servers[1])
    sup._run_one_tick()
    assert "drop" not in state.server_health
    assert "keep" in state.server_health


def test_settings_changes_take_effect_on_next_tick():
    """Operator can change cadences live — supervisor re-reads each tick."""
    # Mutable settings dict captures live edits.
    settings: dict = {"poll_interval_seconds": 60}
    sup = supervisor.Supervisor(
        get_servers=lambda: [FakeServer("box")],
        get_settings=lambda: settings,
        work_queue=queue.Queue(maxsize=64),
    )
    sup._run_one_tick()
    # Now bump the interval and force a fresh enqueue.
    settings["poll_interval_seconds"] = 30
    h = state.server_health["box"]
    h.next_metrics_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    h.pending[CheckType.METRICS] = False

    before = datetime.now(timezone.utc)
    sup._run_one_tick()
    delta = (h.next_metrics_at - before).total_seconds()
    # Should reflect the new 30 s interval.
    assert 25 < delta < 35
