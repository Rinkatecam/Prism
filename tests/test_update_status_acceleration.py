"""Tests for the /api/servers/<name>/update-status endpoint's acceleration
policy.

The original bug (SRV01, 2026-05-21): every poll of this endpoint while
a server was in ``restart_required`` re-armed 600 s of accelerated polling.
With the browser polling every 5 s, the acceleration window was
continuously reset → the supervisor enqueued every check every 5 s
forever, even though the install had completed 2 days ago and nothing
was happening on the server.

These tests pin the corrected behaviour:
  * Active progress states (queued/searching/downloading/installing) DO
    re-arm acceleration every poll — install progress should feel live.
  * ``restart_required`` does NOT re-arm on repeated polls. It only
    accelerates ONCE, on the transition INTO it.
  * Terminal states (``completed``, ``failed``) never accelerate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def flask_client(monkeypatch):
    """Authenticated Flask client with a fake server config in place."""
    from app import app as flask_app
    from routes.api import _shared
    flask_app.config["TESTING"] = True

    # Stub the config so .get_server_by_name("srv1") doesn't return None
    fake_cfg = MagicMock()
    fake_cfg.name = "srv1"
    fake_cfg.host = "srv1.example.com"
    monkeypatch.setattr(
        _shared._config, "get_server_by_name", lambda n: fake_cfg if n == "srv1" else None
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "accel_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    return client


@pytest.fixture(autouse=True)
def _clean_install_state(tmp_path, monkeypatch):
    """Isolate the test from the production install_state.json file.

    The endpoint under test calls ``_persist_install_state()`` whenever it
    writes a status row. Without monkeypatching the persistence path, every
    test run would leak its fake ``srv1`` entry into ``data/install_state.json``
    — that's how this fixture got written wrong the first time and how a
    stray ``srv1`` entry ended up next to SRV01 in production.

    Point ``_install_state_path`` at a tmp file AND clear the in-memory
    dict on entry and exit so the test never touches real state.
    """
    from routes.api import _shared
    monkeypatch.setattr(_shared, "_install_state_path", tmp_path / "test_install_state.json")
    _shared._update_install_state.clear()
    yield
    _shared._update_install_state.clear()


class _FakePowerShell:
    """Minimal PowerShell stand-in. Returns whatever ``payload`` was set by
    the test on the parent RunspacePool stub."""

    def __init__(self, pool):
        self._pool = pool

    def add_script(self, script):
        pass

    def invoke(self):
        return [json.dumps(self._pool._payload)]


class _FakeRunspacePool:
    """Context-manager stub for pypsrp's RunspacePool. The ``payload``
    attribute is what the WinRM read returns — set it in the test."""

    def __init__(self, wsman):
        self._payload: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hit_endpoint(client, payload: dict):
    """Drive the endpoint with a controlled WinRM payload and return the
    list of accelerate_server call args. Patches both the WinRM read AND
    accelerate_server so the test never hits the network or the supervisor."""
    fake_pool = _FakeRunspacePool(None)
    fake_pool._payload = payload

    accel_mock = MagicMock()
    # RunspacePool / PowerShell are imported INSIDE the route handler, so
    # we patch the source module (pypsrp.powershell) rather than the
    # updates module's namespace.
    with patch("routes.api.updates._wu_make_wsman"), \
         patch("pypsrp.powershell.RunspacePool",
               side_effect=lambda wsman: fake_pool), \
         patch("pypsrp.powershell.PowerShell",
               side_effect=lambda pool: _FakePowerShell(pool)), \
         patch("routes.api.updates.accelerate_server", accel_mock):
        r = client.get("/api/servers/srv1/update-status")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return accel_mock


# ─────────────────────────────────────────────────────────────────────
# 1. The original bug: repeated polls in restart_required must NOT accelerate
# ─────────────────────────────────────────────────────────────────────


def test_repeated_polls_at_restart_required_do_not_accelerate(flask_client):
    """The SRV01 incident, refined. A server that's been sitting in
    restart_required for hours/days must not get acceleration re-armed on
    any browser poll — including the very first one. Importing a stale
    remote file's terminal status from ``prev=None`` is not a real
    transition; the install isn't in flight, so there's nothing to
    accelerate. Only transitions OUT OF an active progress state count.
    """
    payload = {
        "status": "restart_required",
        "message": "Installed 3 update(s) — restart required",
        "installed_count": 3,
    }

    # First poll = transition from "no state" to "restart_required".
    # Under the new policy this is NOT a meaningful transition — the
    # install isn't active. So acceleration must NOT fire.
    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 0, (
        "First import of a stale terminal-state file must NOT accelerate. "
        "Acceleration is only for in-flight installs."
    )

    # Subsequent polls = no transition, no progress, no acceleration.
    for _ in range(5):
        accel = _hit_endpoint(flask_client, payload)
        assert accel.call_count == 0, (
            "Polls while already in restart_required must NOT re-arm "
            "acceleration — this is the loop that bombards stuck servers"
        )


# ─────────────────────────────────────────────────────────────────────
# 2. Active progress states still accelerate every poll
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["queued", "searching", "downloading", "installing"])
def test_polls_during_active_install_keep_accelerating(flask_client, state):
    """While an install is actually progressing, repeated polls SHOULD
    re-arm acceleration. These states are short-lived and the UI feels
    sluggish if acceleration expires mid-download."""
    payload = {"status": state, "message": f"In state {state}"}

    # First poll — transition INTO this state, so accelerates (count=1).
    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 1

    # Repeat polls — no transition, but state is active → must still accelerate.
    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 1, f"Active state {state!r} must re-arm acceleration on every poll"


# ─────────────────────────────────────────────────────────────────────
# 3. Terminal states never accelerate (except the transition burst)
# ─────────────────────────────────────────────────────────────────────


def test_terminal_completed_does_not_accelerate(flask_client):
    """Importing a terminal `completed` state from a stale file does NOT
    accelerate — install isn't active."""
    payload = {"status": "completed", "message": "Done"}

    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 0

    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 0


def test_terminal_failed_does_not_accelerate(flask_client):
    """The SRV03 case — a `failed` state from yesterday's attempt must
    NOT cause acceleration on first import or subsequent polls."""
    payload = {"status": "failed", "error": "boom"}

    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 0  # was 1 (transition burst) — now suppressed

    accel = _hit_endpoint(flask_client, payload)
    assert accel.call_count == 0


# ─────────────────────────────────────────────────────────────────────
# 4. Transitions in/out of restart_required still get one acceleration each
# ─────────────────────────────────────────────────────────────────────


def test_transition_installing_to_restart_required_accelerates(flask_client):
    """First we're installing (active progress), then we hit
    restart_required. The transition OUT of an active state must
    accelerate so the badge appears within one supervisor tick — this is
    the legitimate post-install-completion burst."""
    accel = _hit_endpoint(flask_client, {"status": "installing"})
    assert accel.call_count == 1  # active state, always accelerates

    accel = _hit_endpoint(flask_client, {"status": "restart_required"})
    assert accel.call_count == 1, (
        "Transition installing → restart_required is the post-install "
        "completion moment — accelerate so the badge appears immediately"
    )


def test_new_install_kickoff_from_restart_required_accelerates(flask_client):
    """An active install starting (queued) is always accelerated, no
    matter what the previous state was."""
    # Park in restart_required — no acceleration on import
    accel = _hit_endpoint(flask_client, {"status": "restart_required"})
    assert accel.call_count == 0

    # Repeat polls — no acceleration
    accel = _hit_endpoint(flask_client, {"status": "restart_required"})
    assert accel.call_count == 0

    # Now a NEW install kicks off → status flips to queued (active)
    accel = _hit_endpoint(flask_client, {"status": "queued"})
    assert accel.call_count == 1, (
        "A new install starting must always accelerate — queued is an "
        "active progress state"
    )
