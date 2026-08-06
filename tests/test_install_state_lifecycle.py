"""Tests for the install / reboot / stabilising lifecycle.

This covers the cooperation between:

  * ``routes.api.updates._set_rebooting_state`` — transitions an entry
    from any install status to ``rebooting`` when a restart fires
    (instead of popping the entry, which used to make the dashboard
    show "offline" during the 3-5 min reboot window).
  * ``collector_v2.aggregator.Aggregator._handle_post_reboot`` — the
    came-back-online detector. Transitions ``rebooting`` → ``stabilising``
    on the first successful METRICS sample, then clears the row after
    the stabilising window elapses.
  * ``collector_v2.periodics._build_jobs._reboot_state_janitor`` — the
    20 min reboot-timeout safety net.

What we deliberately do NOT test here:
  * End-to-end install kickoff (covered by manual smoke + production)
  * The PowerShell payloads (those are in collector_v2/scripts.py and
    don't have round-trip changes here).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────
# Shared install-state fixture — every test mutates the same dict, so
# we wipe it between cases to avoid bleed-through.
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_install_state(tmp_path, monkeypatch):
    """Isolate the test from production install_state.json.

    ``_set_rebooting_state`` and ``_handle_post_reboot`` both call
    ``_persist_install_state()`` on every mutation. Without redirecting the
    persistence path, our fake server names ("srv1", "clean-srv", etc.) end
    up in ``data/install_state.json`` for real — confused operators next
    time they look at the dashboard.
    """
    from routes.api import _shared
    monkeypatch.setattr(_shared, "_install_state_path", tmp_path / "test_install_state.json")
    _shared._update_install_state.clear()
    yield
    _shared._update_install_state.clear()


# ─────────────────────────────────────────────────────────────────────
# 1. _set_rebooting_state — used by both updates.py (auto-restart) and
#    power.py (manual restart) post-R5.
# ─────────────────────────────────────────────────────────────────────

def test_set_rebooting_preserves_pre_reboot_install_count():
    """An update install that finishes with 7 patches and triggers a
    reboot should keep the 7 visible during the reboot window — so the
    post-reboot tile can still say "Just installed 7 updates"."""
    from routes.api._shared import _update_install_state
    from routes.api.updates import _set_rebooting_state
    _update_install_state["srv1"] = {
        "status": "restart_required",
        "started_at": "2026-05-20T10:00:00Z",
        "installed_count": 7,
        "pending_count": 0,
        "restart_after": True,
    }

    _set_rebooting_state("srv1", actor="user:test")

    cur = _update_install_state["srv1"]
    assert cur["status"] == "rebooting"
    assert cur["installed_count"] == 7
    assert cur["started_at"] == "2026-05-20T10:00:00Z", (
        "Original install start time must be preserved for the post-reboot summary"
    )
    assert "reboot_started_at" in cur
    assert cur["restart_after"] is False, (
        "Intent satisfied — clearing prevents a second restart from being scheduled"
    )


def test_set_rebooting_on_empty_state_creates_minimal_row():
    """If a manual restart is invoked on a server with no prior install
    activity, we still want a rebooting row so the dashboard shows it."""
    from routes.api._shared import _update_install_state
    from routes.api.updates import _set_rebooting_state

    _set_rebooting_state("clean-srv", actor="user:manual")

    cur = _update_install_state["clean-srv"]
    assert cur["status"] == "rebooting"
    assert cur["installed_count"] == 0
    assert cur["actor"] == "user:manual"


# ─────────────────────────────────────────────────────────────────────
# 2. Aggregator's _handle_post_reboot — came-back-online detection.
# ─────────────────────────────────────────────────────────────────────

class _FakeAggregator:
    """Just enough surface to call ``_handle_post_reboot`` in isolation.

    The real Aggregator carries queues, DB handles, settings callables —
    none of which matter for this transition. We import the method off
    the class and bind it to a stand-in.
    """
    _STABILISING_WINDOW_S = 60

    def __init__(self):
        from collector_v2.aggregator import Aggregator
        # Bind the real methods to this fake instance via __get__.
        # _handle_post_reboot can dispatch to _force_updates_check at the
        # stabilising→cleared transition, so both need to resolve.
        self._handle_post_reboot = Aggregator._handle_post_reboot.__get__(self)
        self._force_updates_check = Aggregator._force_updates_check.__get__(self)


def test_post_reboot_transitions_rebooting_to_stabilising():
    """First successful metrics after a reboot flips the row to
    ``stabilising`` and re-arms acceleration."""
    from routes.api._shared import _update_install_state
    _update_install_state["srv1"] = {
        "status": "rebooting",
        "reboot_started_at": "2026-05-20T10:00:00Z",
        "installed_count": 3,
    }

    agg = _FakeAggregator()
    with patch("collector_v2.supervisor.accelerate_server") as fake_accel:
        agg._handle_post_reboot("srv1")

    cur = _update_install_state["srv1"]
    assert cur["status"] == "stabilising"
    assert "came_back_at" in cur
    assert cur["installed_count"] == 3, "Pre-reboot context preserved"
    # Acceleration is re-armed for the stabilising window
    fake_accel.assert_called_once()
    args, kwargs = fake_accel.call_args
    assert args[0] == "srv1"
    assert kwargs["duration_s"] == 60


def test_post_reboot_clears_stabilising_after_window_elapses():
    """A second metrics sample arriving > 60 s after came_back_at clears
    the install_state entirely — the dashboard reverts to the normal
    metric-based badge."""
    from routes.api._shared import _update_install_state
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _update_install_state["srv1"] = {
        "status": "stabilising",
        "came_back_at": long_ago,
    }

    agg = _FakeAggregator()
    agg._handle_post_reboot("srv1")

    assert "srv1" not in _update_install_state


def test_post_reboot_clears_stabilising_forces_updates_recheck():
    """When the stabilising window expires, the aggregator must force a
    one-off UPDATES check for that server so Windows' pending_reboot flag
    is re-evaluated within seconds — closing the up-to-30-min gap where
    the dashboard could say "all clear" while Windows still wants a 2nd
    reboot."""
    from routes.api._shared import _update_install_state
    from collector_v2 import state as v2_state
    from collector_v2.types import ServerHealth, CheckType, CheckState

    long_ago_dt = datetime.now(timezone.utc) - timedelta(seconds=120)
    long_ago = long_ago_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _update_install_state["srv1"] = {
        "status": "stabilising",
        "came_back_at": long_ago,
    }
    # Seed a ServerHealth row with UPDATES due 25 min from now (normal
    # cadence). After the forced recheck, next_due_at must be in the past
    # or "now", proving the supervisor will enqueue it on the next tick.
    far_future = datetime.now(timezone.utc) + timedelta(minutes=25)
    h = ServerHealth(name="srv1")
    h.checks[CheckType.UPDATES] = CheckState(
        next_due_at=far_future, pending=False,
    )
    v2_state.upsert_server_health("srv1", h)
    try:
        agg = _FakeAggregator()
        agg._handle_post_reboot("srv1")

        # install_state was cleared (the existing behaviour)
        assert "srv1" not in _update_install_state
        # AND the UPDATES check is now due immediately
        cs = v2_state.server_health["srv1"].checks[CheckType.UPDATES]
        assert cs.next_due_at <= datetime.now(timezone.utc), (
            "UPDATES next_due_at must be pulled forward to 'now' so the "
            "supervisor enqueues the recheck on its next tick"
        )
    finally:
        with v2_state._server_health_lock:
            v2_state.server_health.pop("srv1", None)


def test_post_reboot_recheck_skips_when_updates_pending():
    """If an UPDATES check is already in flight when stabilising clears,
    don't clobber its ``next_due_at`` — let the in-flight one complete
    (its result IS the fresh data we wanted)."""
    from routes.api._shared import _update_install_state
    from collector_v2 import state as v2_state
    from collector_v2.types import ServerHealth, CheckType, CheckState

    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _update_install_state["srv1"] = {
        "status": "stabilising",
        "came_back_at": long_ago,
    }
    pinned = datetime.now(timezone.utc) + timedelta(minutes=10)
    h = ServerHealth(name="srv1")
    h.checks[CheckType.UPDATES] = CheckState(
        next_due_at=pinned, pending=True,  # currently running
    )
    v2_state.upsert_server_health("srv1", h)
    try:
        agg = _FakeAggregator()
        agg._handle_post_reboot("srv1")

        # The pending check's next_due_at is untouched
        cs = v2_state.server_health["srv1"].checks[CheckType.UPDATES]
        assert cs.next_due_at == pinned, (
            "Must not clobber next_due_at while a check is already pending"
        )
    finally:
        with v2_state._server_health_lock:
            v2_state.server_health.pop("srv1", None)


def test_post_reboot_recheck_silent_when_server_untracked():
    """If the server isn't tracked yet (edge case: aggregator running
    before supervisor materialises the ServerHealth row), the recheck
    must be a silent no-op, not crash."""
    from routes.api._shared import _update_install_state
    from collector_v2 import state as v2_state

    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _update_install_state["srv-unknown"] = {
        "status": "stabilising",
        "came_back_at": long_ago,
    }
    # Make sure srv-unknown is NOT in server_health
    with v2_state._server_health_lock:
        v2_state.server_health.pop("srv-unknown", None)

    agg = _FakeAggregator()
    # Must not raise
    agg._handle_post_reboot("srv-unknown")
    assert "srv-unknown" not in _update_install_state


def test_post_reboot_keeps_stabilising_within_window():
    """Don't clear stabilising too early — the operator should see the
    badge for at least the full 60 s window."""
    from routes.api._shared import _update_install_state
    just_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _update_install_state["srv1"] = {
        "status": "stabilising",
        "came_back_at": just_now,
    }

    agg = _FakeAggregator()
    agg._handle_post_reboot("srv1")

    assert "srv1" in _update_install_state
    assert _update_install_state["srv1"]["status"] == "stabilising"


def test_post_reboot_ignores_non_reboot_statuses():
    """A server mid-install must NOT have its install_state altered just
    because the (unrelated) metrics check happened to land. Only the
    reboot/stabilising transitions are owned here."""
    from routes.api._shared import _update_install_state
    _update_install_state["srv1"] = {
        "status": "installing",
        "started_at": "2026-05-20T10:00:00Z",
    }

    agg = _FakeAggregator()
    agg._handle_post_reboot("srv1")

    assert _update_install_state["srv1"]["status"] == "installing"


def test_post_reboot_no_op_when_no_install_state():
    """The hook fires for every metrics sample — most servers have no
    install_state at all, and the hook must be a fast no-op for them."""
    from routes.api._shared import _update_install_state
    agg = _FakeAggregator()
    agg._handle_post_reboot("untracked-srv")
    assert "untracked-srv" not in _update_install_state


def test_post_reboot_handles_malformed_came_back_at():
    """Defensive: if ``came_back_at`` somehow got corrupted (operator
    edited it, or an upgrade migration left it in a weird shape), don't
    crash the aggregator — just clear the row and move on."""
    from routes.api._shared import _update_install_state
    _update_install_state["srv1"] = {
        "status": "stabilising",
        "came_back_at": "not-a-date",
    }

    agg = _FakeAggregator()
    agg._handle_post_reboot("srv1")

    assert "srv1" not in _update_install_state


# ─────────────────────────────────────────────────────────────────────
# 3. Dashboard wiring — covered by manual smoke testing.
#    The partial_server_grid view in routes/views.py reads
#    _update_install_state and attaches it to each server entry. A unit
#    test here would have to stub the entire Flask app + ConfigManager +
#    DB + latest_by_server cache — value-to-fragility ratio is bad. The
#    behaviour is straightforward (dict lookup + merge) and is covered
#    by clicking through the UI while a real install is in flight.
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# 4. Persistence — the install_state must survive a Flask restart.
#
# Without persistence, restarting Prism mid-reboot wipes the in-memory
# dict and operators lose the "Rebooting" indicator on the dashboard.
# The fix is a JSON-file backed store loaded on app startup; these tests
# pin the contract.
# ─────────────────────────────────────────────────────────────────────

def test_persist_writes_atomic_json(tmp_path, monkeypatch):
    """``_persist_install_state`` must write the dict to disk as JSON
    via tmp-file + rename so a crash mid-write can't corrupt the file."""
    from routes.api import _shared
    target = tmp_path / "install_state.json"
    monkeypatch.setattr(_shared, "_install_state_path", target)

    _shared._update_install_state["srv1"] = {
        "status": "rebooting",
        "reboot_started_at": "2026-05-20T10:00:00Z",
        "installed_count": 7,
    }
    _shared._persist_install_state()

    assert target.exists(), "persist must create the file"
    import json
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["srv1"]["status"] == "rebooting"
    assert loaded["srv1"]["installed_count"] == 7


def test_load_returns_empty_on_missing_file(tmp_path, monkeypatch):
    """First-ever app start: no install_state.json exists. The loader
    must return an empty dict, NOT raise."""
    from routes.api import _shared
    monkeypatch.setattr(_shared, "_install_state_path", tmp_path / "missing.json")
    assert _shared._load_install_state() == {}


def test_load_returns_empty_on_corrupt_file(tmp_path, monkeypatch):
    """Partial-write recovery scenario. If the file is malformed JSON,
    we don't want Prism to crash on startup — start fresh instead."""
    from routes.api import _shared
    target = tmp_path / "corrupt.json"
    target.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setattr(_shared, "_install_state_path", target)
    assert _shared._load_install_state() == {}


def test_load_filters_non_dict_entries(tmp_path, monkeypatch):
    """Defensive: if someone hand-edited the file and put non-dict values
    in, drop them rather than crashing later when code expects entry.get()."""
    from routes.api import _shared
    import json
    target = tmp_path / "mixed.json"
    target.write_text(json.dumps({
        "srv1": {"status": "rebooting"},
        "srv2": "not a dict",  # garbage
        "srv3": ["also not a dict"],  # garbage
        "srv4": {"status": "installing"},
    }), encoding="utf-8")
    monkeypatch.setattr(_shared, "_install_state_path", target)
    loaded = _shared._load_install_state()
    assert set(loaded.keys()) == {"srv1", "srv4"}


def test_persist_load_roundtrip(tmp_path, monkeypatch):
    """End-to-end: write the dict, clear it, load from disk, verify
    contents match. This is the actual flow that runs at Flask restart."""
    from routes.api import _shared
    target = tmp_path / "roundtrip.json"
    monkeypatch.setattr(_shared, "_install_state_path", target)

    _shared._update_install_state["srvA"] = {
        "status": "rebooting",
        "reboot_started_at": "2026-05-20T10:00:00Z",
        "installed_count": 3,
        "restart_after": False,
    }
    _shared._update_install_state["srvB"] = {
        "status": "installing",
        "started_at": "2026-05-20T10:05:00Z",
    }
    _shared._persist_install_state()

    # Simulate Flask restart: empty the in-memory dict, then load
    _shared._update_install_state.clear()
    loaded = _shared._load_install_state()

    assert set(loaded.keys()) == {"srvA", "srvB"}
    assert loaded["srvA"]["status"] == "rebooting"
    assert loaded["srvA"]["installed_count"] == 3
    assert loaded["srvB"]["status"] == "installing"


# ─────────────────────────────────────────────────────────────────────
# 4. _reboot_state_janitor — the 20-min safety net that clears stuck
#    rebooting AND stabilising rows. The aggregator's stabilising-clear
#    only fires on a SUCCESSFUL metrics sample; if the server stays
#    offline, the row would be stuck forever without this janitor.
# ─────────────────────────────────────────────────────────────────────


def _run_janitor():
    """Helper: locate and invoke the _reboot_state_janitor closure.

    The janitor is one of the ``_Job`` instances returned by
    ``_build_jobs``. We construct the jobs list and call the one named
    ``reboot_state_janitor`` directly. ``_build_jobs`` takes
    (get_servers, get_settings, db) callables/objects so we pass cheap
    stubs.
    """
    from collector_v2 import periodics
    from unittest.mock import MagicMock
    jobs = periodics._build_jobs(
        get_servers=lambda: [],
        get_settings=lambda: {},
        db=MagicMock(),
    )
    janitor = next(j for j in jobs if j.name == "reboot_state_janitor")
    return janitor.handler()


def test_janitor_clears_stuck_rebooting_row():
    """A row in ``rebooting`` for >20 min with no metrics arrival must
    get cleaned up. This is the pre-existing behaviour we want to keep."""
    from routes.api import _shared
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-stuck"] = {
        "status": "rebooting",
        "reboot_started_at": long_ago,
    }
    _run_janitor()
    assert "srv-stuck" not in _shared._update_install_state


def test_janitor_clears_stuck_stabilising_row():
    """A row in ``stabilising`` for >20 min with no successful metrics
    must get cleaned up. Regression: before this fix the stabilising
    state was stuck forever because ``_handle_post_reboot`` only fires
    on metrics-not-None arrivals (offline samples don't count). When
    a server briefly comes back, transitions to stabilising, then goes
    offline again, the dashboard would show "Stabilising…" indefinitely."""
    from routes.api import _shared
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-stab-stuck"] = {
        "status": "stabilising",
        "reboot_started_at": long_ago,
        "came_back_at": long_ago,
        "updated_at": long_ago,
    }
    _run_janitor()
    assert "srv-stab-stuck" not in _shared._update_install_state, (
        "stabilising rows older than 20 min must be GC'd by the janitor; "
        "otherwise a server that briefly came back then went offline "
        "again would show 'Stabilising…' forever"
    )


def test_janitor_keeps_fresh_stabilising_row():
    """A row that JUST transitioned to stabilising (well within the 20
    min window) must NOT be touched — the aggregator may still clear
    it normally on the next successful metrics arrival."""
    from routes.api import _shared
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-fresh"] = {
        "status": "stabilising",
        "reboot_started_at": recent,
        "came_back_at": recent,
        "updated_at": recent,
    }
    _run_janitor()
    assert "srv-fresh" in _shared._update_install_state, (
        "fresh stabilising rows must survive the janitor — only stale "
        "(>20 min) rows get cleaned up"
    )


def test_janitor_uses_updated_at_when_reboot_started_at_missing():
    """Defense in depth: a row missing ``reboot_started_at`` should
    fall back to ``updated_at`` so it can still be GC'd. Without this
    fallback, a malformed row would stay forever."""
    from routes.api import _shared
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-no-reboot-ts"] = {
        "status": "stabilising",
        # reboot_started_at missing — only updated_at is set
        "updated_at": long_ago,
    }
    _run_janitor()
    assert "srv-no-reboot-ts" not in _shared._update_install_state


def test_janitor_skips_non_reboot_statuses():
    """Rows in ``installing``, ``downloading``, etc. must NOT be
    cleared by the janitor — those have their own lifecycles (watcher
    threads, /update-status endpoint).

    ``restart_required`` is now janitored too but on a much longer
    (48 h) timeout — see ``test_janitor_clears_very_stale_restart_required``."""
    from routes.api import _shared
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-installing"] = {
        "status": "installing",
        "reboot_started_at": long_ago,
        "updated_at": long_ago,
    }
    _run_janitor()
    assert "srv-installing" in _shared._update_install_state


def test_janitor_clears_very_stale_restart_required():
    """**SRV01 regression**: a row stuck in ``restart_required`` for
    longer than 48 h must get garbage-collected. Real-world trigger:
    Prism installed updates → flagged reboot required → operator never
    rebooted (or rebooted externally but UPDATES check never confirmed
    pending_reboot=False because the target was unreachable). The stale
    install_state then drove the dashboard banner indefinitely, even
    though the live ``server_update_info.reboot_required`` flag was
    the truthful source.

    48 h is the threshold — long enough to span a typical monthly
    patch-Tuesday → weekend-reboot operator workflow, short enough to
    catch genuinely dead rows."""
    from routes.api import _shared
    very_old = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-stale-restart"] = {
        "status": "restart_required",
        "completed_at": very_old,
        "updated_at": very_old,
        "reboot_required": True,
        "pending_count": 3,
        "installed_count": 3,
    }
    _run_janitor()
    assert "srv-stale-restart" not in _shared._update_install_state, (
        "restart_required rows older than 48 h must be GC'd — they were "
        "the SRV01 stuck-banner symptom; live UPDATES result is the "
        "truthful source after that long"
    )


def test_janitor_keeps_recent_restart_required():
    """A ``restart_required`` row that's a few hours old must NOT be
    cleared — operators legitimately delay reboots inside a change
    window. The 48 h threshold gives plenty of room for normal patch
    cycles (Tuesday install → weekend reboot)."""
    from routes.api import _shared
    recent = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-recent-restart"] = {
        "status": "restart_required",
        "completed_at": recent,
        "updated_at": recent,
        "reboot_required": True,
        "pending_count": 2,
        "installed_count": 2,
    }
    _run_janitor()
    assert "srv-recent-restart" in _shared._update_install_state, (
        "fresh restart_required rows (<48 h old) must survive — the "
        "operator may still be in their change window"
    )


def test_janitor_restart_required_uses_completed_at_anchor():
    """A ``restart_required`` row without ``reboot_started_at`` must
    fall back to ``completed_at`` (set when the install finished) for
    the staleness check. Without this fallback, install_state rows
    written by the updates route would never be GC'd because they
    don't carry a ``reboot_started_at`` (which is only set when Prism
    triggers a reboot itself)."""
    from routes.api import _shared
    very_old = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _shared._update_install_state["srv-completed-only"] = {
        "status": "restart_required",
        # No reboot_started_at — install never triggered a Prism reboot.
        "completed_at": very_old,
        # Also no updated_at (extreme case) — completed_at is the only anchor.
    }
    _run_janitor()
    assert "srv-completed-only" not in _shared._update_install_state
