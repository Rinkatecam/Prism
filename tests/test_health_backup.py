"""Feature 1.8 — T8: backup age + stale flag surfaced on /api/system/health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


@pytest.fixture()
def flask_client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    now_iso = datetime.now(timezone.utc).isoformat()
    with client.session_transaction() as sess:
        sess["username"] = "backup_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


def _health_json(client):
    with patch("routes.api.health._require_auth", return_value=None):
        r = client.get("/api/system/health")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return r.get_json()


def _iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_health_reports_backup_stale_when_old(flask_client):
    from routes.api import _shared
    _shared._db.set_backup_state(last_success_ts=_iso_ago(30), last_ok=1)
    data = _health_json(flask_client)
    assert "backup" in data, "health must surface a backup block"
    assert data["backup"]["stale"] is True  # 30h > default 26h threshold
    assert data["backup"]["age_hours"] >= 29


def test_health_reports_backup_fresh_when_recent(flask_client):
    from routes.api import _shared
    _shared._db.set_backup_state(last_success_ts=_iso_ago(1), last_ok=1)
    data = _health_json(flask_client)
    assert data["backup"]["stale"] is False
    assert data["backup"]["last_ok"] == 1
    assert data["backup"]["last_success_ts"]
