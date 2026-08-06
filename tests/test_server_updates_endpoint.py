"""Tests for /api/servers/<name>/updates — the endpoint that drives the
"Pending Windows Updates" panel on the server-detail page.

This endpoint has a "clear install_state when Windows says no reboot
needed" path. The 2026-05-21 SRV01 bombardment incident traced to
this path mistakenly trusting ``pending_reboot=False`` values that were
preserved-but-stale from before the install (the aggregator's
transient-error path bumps ``checked_at`` while keeping the previous
good payload). These tests pin the corrected behaviour: transient-error
payloads are NOT trusted to clear install_state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


@pytest.fixture()
def flask_client(monkeypatch):
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    now_iso = datetime.now(timezone.utc).isoformat()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "updates_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Same isolation trick as test_install_state_lifecycle: redirect
    persistence to a tmp file so we never touch data/install_state.json."""
    from routes.api import _shared
    monkeypatch.setattr(_shared, "_install_state_path", tmp_path / "test_install_state.json")
    _shared._update_install_state.clear()
    # Also wipe server_update_info so collector data doesn't leak across tests
    from state import server_update_info
    saved_info = dict(server_update_info)
    server_update_info.clear()
    yield
    _shared._update_install_state.clear()
    server_update_info.clear()
    server_update_info.update(saved_info)


def _fetch_updates(client, name: str):
    return client.get(f"/api/servers/{name}/updates")


# ─────────────────────────────────────────────────────────────────────
# The SRV01 regression — transient-preserved data must NOT pop
# ─────────────────────────────────────────────────────────────────────


def test_transient_error_preserved_data_does_not_pop_install_state(flask_client):
    """The SRV01 bombardment loop reproducer.

    Scenario: server was offline since an install. The collector cache
    has ``pending_reboot=False`` (from a successful check BEFORE the
    install) but ``transient_error=True`` because every UPDATES check
    since has hit "offline" and gone through the preserve path. The
    install_state has the real ``restart_required``. The endpoint MUST
    NOT pop install_state — that's what kept making the /update-status
    poll appear as a fresh transition + fire acceleration over and over.
    """
    from routes.api import _shared
    from state import server_update_info

    server_update_info["SRV01"] = {
        "count": 0,
        "updates": [],
        "checked_at": "2026-05-21T10:00:00Z",     # bumped on every transient failure
        "pending_reboot": False,                   # ← stale from BEFORE the install
        "transient_error": True,                   # ← the giveaway
        "transient_error_reason": "server_rebooting_or_unreachable",
        "error": None,
    }
    _shared._update_install_state["SRV01"] = {
        "status": "restart_required",
        "installed_count": 3,
        "completed_at": "2026-05-19T10:11:58Z",
        "reboot_required": True,
    }

    r = _fetch_updates(flask_client, "SRV01")
    assert r.status_code == 200

    # The install_state survives — the dashboard's restart-required badge
    # must remain. Popping here was the bug.
    assert "SRV01" in _shared._update_install_state, (
        "transient-error data must NOT cause install_state to be popped — "
        "this was the loop that bombarded SRV01 every 5 s with "
        "acceleration re-arms"
    )


def test_fresh_no_reboot_data_DOES_pop_install_state(flask_client):
    """Inverse test: a genuinely fresh UPDATES check (no transient_error)
    that says ``pending_reboot=False`` SHOULD pop install_state. This is
    the legitimate "the reboot fired, we're clean now" path."""
    from routes.api import _shared
    from state import server_update_info

    server_update_info["srv-clean"] = {
        "count": 0,
        "updates": [],
        "checked_at": "2026-05-21T10:30:00Z",
        "pending_reboot": False,
        # NO transient_error flag — this is a real successful check
        "error": None,
    }
    _shared._update_install_state["srv-clean"] = {
        "status": "restart_required",
        "installed_count": 3,
        "completed_at": "2026-05-21T10:00:00Z",
    }

    r = _fetch_updates(flask_client, "srv-clean")
    assert r.status_code == 200
    assert "srv-clean" not in _shared._update_install_state, (
        "Fresh check confirming no reboot needed SHOULD clear install_state"
    )


def test_fresh_with_pending_reboot_keeps_install_state(flask_client):
    """If the fresh UPDATES check confirms reboot is STILL needed, leave
    install_state alone — Windows agrees with us."""
    from routes.api import _shared
    from state import server_update_info

    server_update_info["srv-pending"] = {
        "count": 0,
        "updates": [],
        "checked_at": "2026-05-21T10:30:00Z",
        "pending_reboot": True,    # ← Windows still wants a reboot
        "error": None,
    }
    _shared._update_install_state["srv-pending"] = {
        "status": "restart_required",
        "installed_count": 3,
    }

    r = _fetch_updates(flask_client, "srv-pending")
    assert r.status_code == 200
    assert "srv-pending" in _shared._update_install_state


def test_collector_error_does_not_pop_install_state(flask_client):
    """If the latest UPDATES check has an explicit error, that's NOT
    authoritative either. Keep install_state."""
    from routes.api import _shared
    from state import server_update_info

    server_update_info["srv-err"] = {
        "count": 0,
        "updates": [],
        "checked_at": "2026-05-21T10:30:00Z",
        "pending_reboot": False,
        "error": "PowerShell script raised",  # ← explicit error
    }
    _shared._update_install_state["srv-err"] = {
        "status": "restart_required",
    }

    r = _fetch_updates(flask_client, "srv-err")
    assert r.status_code == 200
    assert "srv-err" in _shared._update_install_state, (
        "explicit error in collector cache must not cause install_state pop"
    )
