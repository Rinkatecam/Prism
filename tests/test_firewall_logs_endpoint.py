"""Tests for /api/servers/<name>/logs?source=Firewall — the data feed for
the new Firewall Logs panel in server_detail.html.

The endpoint itself is already used by the Windows Logs panel; this is the
same code path with a different ``source`` query value. These tests pin
the contract: rows tagged ``log_source='Firewall'`` are returned, rows
with other sources are excluded, and the ``hours`` / ``level`` filters
compose correctly with the source filter.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture()
def flask_client_with_logs(tmp_db, monkeypatch):
    """Flask test client wired to a temp DB that's been seeded with a
    mixed bag of log rows so we can verify filtering."""
    from app import app as flask_app
    from routes.api import _shared
    flask_app.config["TESTING"] = True
    # Point the API blueprint at our fresh DB
    monkeypatch.setattr(_shared, "_db", tmp_db)

    now = datetime.now(timezone.utc)

    def _ts(seconds_ago: int) -> str:
        return (now - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")

    # Seed: System + Application + Security + Firewall rows
    # These tests exercise the ENDPOINT's source/level filtering, so they opt
    # out of the ingest filter — several fixtures below are Information level
    # and would otherwise be dropped before they ever reach the endpoint,
    # testing the wrong layer. Ingest filtering has its own tests.
    tmp_db.insert_logs("testsrv01", ingest_cfg={"drop_information": False,
                                                "coalesce_signatures": False},
                       logs_list=[
        {"source": "System",      "time": _ts(60),  "level": "Error",       "event_id": 7001, "message": "Service failed to start"},
        {"source": "Application", "time": _ts(50),  "level": "Information", "event_id": 1000, "message": "App started"},
        {"source": "Security",    "time": _ts(40),  "level": "Information", "event_id": 4624, "message": "Login successful"},
        {"source": "Firewall",    "time": _ts(30),  "level": "Information", "event_id": 2004, "message": "Firewall rule added"},
        {"source": "Firewall",    "time": _ts(20),  "level": "Warning",     "event_id": 5031, "message": "Application blocked"},
        {"source": "Firewall",    "time": _ts(10),  "level": "Error",       "event_id": 5025, "message": "Firewall service stopped"},
    ])

    # Authenticated session
    now_iso = now.isoformat()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "fw_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


def _fetch(client, qs: str):
    # Session fixture already sets sess['username'] which is what
    # the @api_bp before_request gate checks.
    return client.get(f"/api/servers/testsrv01/logs?{qs}")


def test_source_firewall_returns_only_firewall_rows(flask_client_with_logs):
    r = _fetch(flask_client_with_logs, "hours=1&source=Firewall&limit=50")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    data = r.get_json()
    assert "logs" in data
    sources = {log["log_source"] for log in data["logs"]}
    assert sources == {"Firewall"}, (
        f"Expected only Firewall rows but got sources: {sources}"
    )
    assert len(data["logs"]) == 3


def test_no_source_filter_returns_all_sources(flask_client_with_logs):
    """Sanity check the inverse: without ?source=, all 4 sources surface.
    This is how the existing Windows Logs panel's "All" option works."""
    r = _fetch(flask_client_with_logs, "hours=1&limit=50")
    assert r.status_code == 200
    data = r.get_json()
    sources = {log["log_source"] for log in data["logs"]}
    assert sources == {"System", "Application", "Security", "Firewall"}


def test_firewall_source_composes_with_level_filter(flask_client_with_logs):
    """Combining ?source=Firewall&level=Error must return only the
    intersection — 1 row in our seed (event 5025 "service stopped")."""
    r = _fetch(flask_client_with_logs, "hours=1&source=Firewall&level=Error&limit=50")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["logs"]) == 1
    row = data["logs"][0]
    assert row["log_source"] == "Firewall"
    assert row["level"] == "Error"
    assert row["event_id"] == 5025


def test_firewall_source_with_no_matches_returns_empty_list(flask_client_with_logs):
    """source=Firewall + level=Critical → no rows. Endpoint must return
    an empty list, not 404."""
    r = _fetch(flask_client_with_logs, "hours=1&source=Firewall&level=Critical&limit=50")
    assert r.status_code == 200
    assert r.get_json()["logs"] == []
