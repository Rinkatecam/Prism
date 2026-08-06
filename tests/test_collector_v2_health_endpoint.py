"""Tests for the v2 surface on /api/system/health.

The endpoint MUST always return a ``collector_engine`` field (value
``"v2"`` post-retirement) and a populated ``collector_v2`` block so
operators can see per-component health at a glance.

Audit fix M5 from docs/COLLECTOR_V2_AUDIT.md — the H2 fix added the
endpoint surface; these tests guard against regression. The
historical "legacy" mode is gone (see
``docs/COLLECTOR_V1_RETIREMENT.md``).
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture()
def flask_client():
    """Real Flask test client with an authenticated session pre-set.

    Auth is enforced by Flask before_request middleware in auth.py — it
    checks for ``session["username"]``. We set it via session_transaction
    so subsequent requests pass the gate. Imports app.py lazily so the
    module-level daemon-thread launch only happens for tests that need it.
    """
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    from datetime import datetime, timezone
    client = flask_app.test_client()
    # Use a login_time that's "now" so the session-timeout check passes
    # regardless of when the test runs.
    now_iso = datetime.now(timezone.utc).isoformat()
    with client.session_transaction() as sess:
        sess["username"] = "audit_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


def _health_json(client):
    """Hit /api/system/health and return JSON. Auth is satisfied by the
    fixture's pre-set session."""
    # The endpoint also calls _require_auth() inside the handler as a
    # second-layer check. Mock it to short-circuit any session-revocation
    # / disabled-user lookups that would need a real DB row.
    with patch("routes.api.health._require_auth", return_value=None):
        r = client.get("/api/system/health")
    assert r.status_code == 200, (
        f"health endpoint returned {r.status_code} — response: "
        f"{r.get_data(as_text=True)[:200]}"
    )
    return r.get_json()


def test_health_includes_collector_engine_key(flask_client):
    """Every response — legacy or v2 — must report which engine is active."""
    data = _health_json(flask_client)
    assert "collector_engine" in data, (
        "Operators rely on this key to distinguish legacy vs v2 mode. "
        "Don't remove it."
    )
    # Default install runs legacy
    assert data["collector_engine"] in ("legacy", "v2", "both")


def test_health_collector_v2_block_present_when_engine_is_v2(flask_client):
    """Audit M5: when v2 is the active engine, /api/system/health MUST
    include a populated collector_v2 block with per-component health.
    Without this, operators have no programmatic way to see v2 status."""

    # Force the settings to report v2 — we don't actually start v2
    # threads here (that would be an integration test), we just verify
    # the endpoint contract.
    fake_settings = {
        "collector_engine": "v2",
        "poll_interval_seconds": 60,
        "auth": {},
    }
    fake_snapshot = {
        "started": True,
        "supervisor_last_tick_s_ago": 1.2,
        "aggregator_last_tick_s_ago": 0.3,
        "workers_last_activity_s_ago": 0.8,
        "tracked_servers": 30,
        "cached_metrics": 30,
        "supervisor": {"last_tick_s_ago": 1.2, "queue_depth": 4,
                       "tracked_servers": 30, "critical_errors_total": 0},
        "workers": {"active_workers": 2, "total_processed": 1234,
                    "total_offline": 12, "total_timeouts": 3,
                    "total_critical_errors": 0, "num_workers": 15},
        "aggregator": {"last_tick_s_ago": 0.3, "total_processed": 1234,
                        "critical_errors_total": 0,
                        "total_alerts_dispatched": 7},
        "periodics": {"last_heartbeat_s_ago": 12.0,
                      "critical_errors_total": 0, "last_run": {}},
    }

    with patch("routes.api.health._shared._config.get_settings",
               return_value=fake_settings), \
            patch("collector_v2.get_health_snapshot", return_value=fake_snapshot):
        data = _health_json(flask_client)

    assert data["collector_engine"] == "v2"
    v2 = data.get("collector_v2")
    assert v2 is not None, (
        "collector_v2 block missing when engine='v2'. The HTTP surface "
        "for v2 health was the H2 audit fix; this regression test "
        "guards against removing it."
    )
    # Spot-check shape — operators read these keys, don't rename
    # without coordinating a UI update.
    for key in ("started", "supervisor", "workers", "aggregator", "periodics"):
        assert key in v2, f"v2 health snapshot missing key: {key}"
    assert v2["started"] is True
    # Per-component sub-shapes carry the counters the operator needs
    assert "total_processed" in v2["workers"]
    assert "critical_errors_total" in v2["supervisor"]


# test_health_collector_v2_is_null_when_engine_is_legacy was removed when
# v1 was retired. There's no longer a "legacy" engine; the endpoint always
# reports collector_engine="v2" and always populates the collector_v2
# block. The two remaining tests above cover the surviving contract.


# ─── F-AT-1 / F-D-1: audit telemetry surfacing on /api/system/health ───

def test_health_includes_audit_telemetry_block(flask_client):
    """F-AT-1 + F-D-1 (CSV remediation): the health endpoint must expose
    the audit-chain verifier's latest result AND the insert/mirror
    failure counters, so an external monitor (or SOP-05) can detect
    tampering OR audit-blind state without calling internal APIs."""
    data = _health_json(flask_client)
    assert "audit" in data, (
        "F-AT-1: /api/system/health must include an `audit` block"
    )
    audit = data["audit"]
    # Shape: must contain all three CSV-relevant signals.
    assert "last_chain_check" in audit
    assert "insert_failures" in audit
    assert "mirror_failures" in audit
    assert "audit_blind" in audit
    # On a fresh DB, no failures have occurred — audit_blind is False.
    assert audit["insert_failures"] == 0
    assert audit["mirror_failures"] == 0
    assert audit["audit_blind"] is False


def test_health_audit_blind_flag_flips_when_insert_failure_recorded(flask_client):
    """If a log_audit insert ever fails, the health endpoint must show
    audit_blind=True. This is the F-D-1 visibility contract."""
    from routes.api import _shared
    # Simulate a prior insert failure (the counter is on the live DB
    # object the endpoint uses).
    original = getattr(_shared._db, "_audit_insert_failures", 0)
    _shared._db._audit_insert_failures = 5
    try:
        data = _health_json(flask_client)
        assert data["audit"]["audit_blind"] is True
        assert data["audit"]["insert_failures"] == 5
    finally:
        _shared._db._audit_insert_failures = original
