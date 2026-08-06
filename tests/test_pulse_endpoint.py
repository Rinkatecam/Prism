"""Tests for /api/collector/pulse — the topbar ECG widget's data feed.

Contract:
  * Authenticated only
  * Returns ``events``, ``fleet``, ``active``, ``subsystems``, ``bpm``, ``now``
  * ``?since=<ts>`` filters events to strictly after the watermark
  * Empty buffer + no servers → all fields present, sane defaults

These tests use Flask's test_client + a pre-authenticated session, the
same pattern used in test_collector_v2_health_endpoint.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def flask_client():
    """Authenticated Flask test client."""
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    now_iso = datetime.now(timezone.utc).isoformat()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "pulse_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


@pytest.fixture(autouse=True)
def _clean_pulse_state():
    """Wipe pulse buffer + server_health before every test."""
    from collector_v2 import state
    state.clear_pulses()
    with state._server_health_lock:
        saved = dict(state.server_health)
        state.server_health.clear()
    yield
    state.clear_pulses()
    with state._server_health_lock:
        state.server_health.clear()
        state.server_health.update(saved)


def _pulse_json(client, qs: str = ""):
    with patch("routes.api.health._require_auth", return_value=None):
        r = client.get("/api/collector/pulse" + qs)
    assert r.status_code == 200, (
        f"endpoint returned {r.status_code} — body: "
        f"{r.get_data(as_text=True)[:200]}"
    )
    return r.get_json()


def _add_server(name: str, last_metrics_age_s: float | None = 5.0,
                pending: bool = False):
    """Register a ServerHealth with controlled freshness + pending flag."""
    from collector_v2 import state
    from collector_v2.types import ServerHealth, CheckType, CheckState
    now = datetime.now(timezone.utc)
    last_ok = (now - timedelta(seconds=last_metrics_age_s)
               if last_metrics_age_s is not None else None)
    h = ServerHealth(name=name)
    h.checks[CheckType.METRICS] = CheckState(
        next_due_at=now, last_ok_at=last_ok,
        consecutive_failures=0, pending=pending,
    )
    state.upsert_server_health(name, h)


# ─────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────

def test_endpoint_requires_auth():
    """With auth enforced and no session, the endpoint must reject (not 200).

    The default test config has auth DISABLED, which makes _require_auth a
    pass-through — so we deterministically enforce auth here (rather than relying
    on some other test's leftover state) and assert the gate actually rejects.
    """
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    # _require_auth (routes.api._shared) reads _config.get_settings()["auth"].
    fake_cfg = MagicMock()
    fake_cfg.get_settings.return_value = {"auth": {"enabled": True}}
    with patch("routes.api._shared._config", fake_cfg):
        r = client.get("/api/collector/pulse")
    # auth enabled + no session → 401 (or a 302 redirect). Just not 200.
    assert r.status_code != 200


# ─────────────────────────────────────────────────────────────────────
# Shape
# ─────────────────────────────────────────────────────────────────────

def test_response_shape_with_empty_state(flask_client):
    """First-ever poll with empty buffer + no servers — all keys present,
    sane defaults. The widget must be able to render in this state."""
    data = _pulse_json(flask_client)
    assert data["ok"] is True
    assert "now" in data and data["now"] > 0
    assert data["events"] == []
    assert data["fleet"] == {"total": 0, "up": 0, "silent": []}
    assert data["active"] == []
    assert "subsystems" in data
    for key in ("supervisor", "aggregator", "workers", "periodics"):
        assert key in data["subsystems"]
        assert "ok" in data["subsystems"][key]
        assert "age_s" in data["subsystems"][key]
    assert data["bpm"] == 0


def test_events_include_recorded_pulses(flask_client):
    """A pulse recorded just now must appear in the response, with the
    fields the canvas needs (ts, server, check, ok, ms)."""
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now, "srv1", "METRICS", True, 240)
    state.record_pulse(now + 0.1, "srv2", "LOGS", False, 5000)

    data = _pulse_json(flask_client)
    assert len(data["events"]) == 2
    srv1 = next(e for e in data["events"] if e["server"] == "srv1")
    assert srv1["check"] == "METRICS"
    assert srv1["ok"] is True
    assert srv1["ms"] == 240
    srv2 = next(e for e in data["events"] if e["server"] == "srv2")
    assert srv2["ok"] is False


def test_since_filter_excludes_older_events(flask_client):
    """The widget passes the last event's ts on the next poll; the endpoint
    must NOT return that event again."""
    from collector_v2 import state
    now = time.time()
    state.record_pulse(now, "old", "METRICS", True, 100)
    state.record_pulse(now + 1.0, "new", "METRICS", True, 100)

    data = _pulse_json(flask_client, f"?since={now}")
    servers = [e["server"] for e in data["events"]]
    assert servers == ["new"]


# ─────────────────────────────────────────────────────────────────────
# Fleet rollup in the response
# ─────────────────────────────────────────────────────────────────────

def test_fleet_counts_reflect_server_health(flask_client):
    _add_server("fresh1", last_metrics_age_s=10)
    _add_server("fresh2", last_metrics_age_s=30)
    _add_server("stale",  last_metrics_age_s=600)

    data = _pulse_json(flask_client)
    fleet = data["fleet"]
    assert fleet["total"] == 3
    assert fleet["up"] == 2
    assert len(fleet["silent"]) == 1
    assert fleet["silent"][0]["name"] == "stale"


def test_active_lists_pending_checks(flask_client):
    _add_server("busy", last_metrics_age_s=5, pending=True)
    _add_server("idle", last_metrics_age_s=5, pending=False)
    data = _pulse_json(flask_client)
    names = [a["name"] for a in data["active"]]
    assert names == ["busy"]
    assert data["active"][0]["check"] == "metrics"


# ─────────────────────────────────────────────────────────────────────
# BPM derivation
# ─────────────────────────────────────────────────────────────────────

def test_bpm_zero_when_no_events(flask_client):
    data = _pulse_json(flask_client)
    assert data["bpm"] == 0


def test_bpm_independent_of_since_filter(flask_client):
    """BPM reads the FULL buffer (last 30s), not just events newer than
    ``since``. Otherwise the displayed BPM would drift with poll cadence
    rather than reflecting actual collector throughput."""
    from collector_v2 import state
    base = time.time() - 5
    # 30 events over the last 5 seconds → projected to 60s ≈ 360 BPM, but
    # we compute it as len(last_30s) * 2, so 30 events → 60 BPM.
    for i in range(30):
        state.record_pulse(base + i * 0.1, f"srv{i}", "METRICS", True, 100)

    data = _pulse_json(flask_client, f"?since={time.time()}")  # excludes all events from response
    assert data["events"] == []
    assert data["bpm"] == 60


# ─────────────────────────────────────────────────────────────────────
# Subsystem health derivation
# ─────────────────────────────────────────────────────────────────────

def test_subsystem_ok_flag_reflects_heartbeat_age(flask_client):
    """A fresh heartbeat (age_s < 5×interval) → ok=True. A stale one → False."""
    from collector_v2 import state
    # Force supervisor heartbeat to "just now"; aggregator to "long ago".
    state.last_supervisor_tick = time.time()
    state.last_aggregator_tick = time.time() - 1000

    data = _pulse_json(flask_client)
    assert data["subsystems"]["supervisor"]["ok"] is True
    assert data["subsystems"]["aggregator"]["ok"] is False


# ─────────────────────────────────────────────────────────────────────
# Audit-fix regression tests
# ─────────────────────────────────────────────────────────────────────

def test_response_has_no_store_cache_header(flask_client):
    """Audit C6 fix — pulse data must not be cached by intermediates,
    otherwise the watermark protocol breaks (the next poll's ``since``
    would refer to events the client never actually received)."""
    with patch("routes.api.health._require_auth", return_value=None):
        r = flask_client.get("/api/collector/pulse")
    assert r.status_code == 200
    assert "no-store" in (r.headers.get("Cache-Control") or "").lower()


def test_fleet_in_response_matches_snapshot(flask_client):
    """Audit C6 fix — the endpoint now derives fleet from
    get_v2_health_snapshot() rather than calling get_fleet_status()
    separately. Verify the values still surface correctly."""
    _add_server("fresh", last_metrics_age_s=10)
    _add_server("stale", last_metrics_age_s=600)
    data = _pulse_json(flask_client)
    assert data["fleet"]["total"] == 2
    assert data["fleet"]["up"] == 1
    assert {s["name"] for s in data["fleet"]["silent"]} == {"stale"}


def test_endpoint_survives_concurrent_aggregator_check_insertion():
    """Audit C3 fix — the endpoint reads h.checks under
    _server_health_lock. Simulate a concurrent supervisor-style mutation
    (insert a new CheckType while the endpoint iterates) and confirm
    no RuntimeError leaks out.
    """
    import threading
    from collector_v2 import state
    from collector_v2.types import ServerHealth, CheckType, CheckState
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # Pre-load 5 servers with METRICS only.
    for i in range(5):
        h = ServerHealth(name=f"srv{i}")
        h.checks[CheckType.METRICS] = CheckState(next_due_at=now, last_ok_at=now)
        state.upsert_server_health(f"srv{i}", h)
    try:
        stop = threading.Event()
        def mutator():
            # Continuously insert/remove a NEW check type to provoke
            # "dictionary changed size during iteration" if the endpoint
            # ever drops the lock.
            while not stop.is_set():
                for i in range(5):
                    with state._server_health_lock:
                        h = state.server_health.get(f"srv{i}")
                        if h is not None:
                            h.checks[CheckType.HARDWARE] = CheckState(next_due_at=now)
                            h.checks.pop(CheckType.HARDWARE, None)
        t = threading.Thread(target=mutator, daemon=True)
        t.start()
        try:
            deadline = time.time() + 0.5
            calls = 0
            while time.time() < deadline:
                fleet = state.get_fleet_status()
                inflight = state.get_in_flight()
                assert isinstance(fleet["total"], int)
                assert isinstance(inflight, list)
                calls += 1
            assert calls > 10, "should have completed many lock-protected calls"
        finally:
            stop.set()
            t.join(timeout=1.0)
    finally:
        with state._server_health_lock:
            state.server_health.clear()
