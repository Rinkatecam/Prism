"""Regression tests for GET /api/servers/<name> — the server-detail page's
"Current Metrics" + status feed.

The 2026-07-15 bug: the verdict_detail try/except block was inserted BETWEEN
`if metrics:` and its `else:`, so the `else` rebound to the try. Since the try
almost always succeeds, the else ran on EVERY request and overwrote the real
metrics with status="unknown"/None — a perpetual spinner on every server-detail
page. These pin the corrected behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture()
def flask_client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    now_iso = datetime.now(timezone.utc).isoformat()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "detail_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
    return client


def _cfg(**over):
    base = dict(host="host.example", type="file_server",
                thresholds={"cpu_warning": 75, "cpu_critical": 90})
    base.update(over)
    return SimpleNamespace(**base)


def test_get_server_returns_real_metrics(flask_client):
    """With a metric row present, the endpoint returns the REAL values —
    not clobbered to unknown/None."""
    from routes.api import _shared
    row = {
        "status": "warning", "cpu_percent": 47.0, "ram_percent": 30.0,
        "disk_c_percent": 39.8, "disk_d_percent": 92.8,
        "timestamp": "2026-07-15T11:27:22Z",
    }
    with patch.object(_shared._config, "get_server_by_name", return_value=_cfg()), \
            patch.object(_shared._db, "get_latest_by_server", return_value=row), \
            patch.object(_shared._db, "get_server_events", return_value=[]):
        resp = flask_client.get("/api/servers/FILE01")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "warning"
    assert data["cpu"] == 47.0
    assert data["ram"] == 30.0
    assert data["disk_d"] == 92.8
    assert data["last_check"] == "2026-07-15T11:27:22Z"


def test_get_server_unknown_only_when_no_metrics(flask_client):
    """No metric row → the else-branch legitimately yields unknown/None."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_server_by_name", return_value=_cfg()), \
            patch.object(_shared._db, "get_latest_by_server", return_value=None), \
            patch.object(_shared._db, "get_server_events", return_value=[]):
        resp = flask_client.get("/api/servers/NEVERSEEN")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "unknown"
    assert data["cpu"] is None
    assert data["last_check"] is None


def test_get_server_404_when_not_in_config(flask_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_server_by_name", return_value=None):
        resp = flask_client.get("/api/servers/GHOST")
    assert resp.status_code == 404
