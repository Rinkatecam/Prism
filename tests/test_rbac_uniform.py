"""Tests for uniform RBAC enforcement across destructive endpoints (S1-4).

Covers:
  - workflow execute denied without per-server admin
  - runbook execute denied without admin
  - factory-reset denied without admin
  - factory-reset denied without approval token even with admin
  - factory-reset succeeds with admin + valid approval

The Flask client is built fresh per test so we can wire a fresh Database
and a stub ConfigManager without polluting global state.
"""

from __future__ import annotations

import json
import pytest
from flask import Flask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _StubServer:
    def __init__(self, name: str, tier: int = 1):
        self.name = name
        self.tier = tier
        self.host = "127.0.0.1"
        self.port = 5985
        self.username = "u"
        self.password = ""
        self.use_https = False
        self.https_skip_verify = False
        self.mac_address = ""


class _StubConfig:
    def __init__(self, servers, settings=None):
        self._servers = servers
        self._settings = settings or {"auth": {"enabled": True}}

    def get_servers(self):
        return self._servers

    def get_server_by_name(self, name):
        return next((s for s in self._servers if s.name == name), None)

    def get_settings(self):
        return self._settings

    def get_raw_servers(self):
        return [s.__dict__ for s in self._servers]

    def get_maintenance_windows(self):
        return []


@pytest.fixture()
def app_client(tmp_path):
    """Fresh Flask app + Database wired to the api blueprint."""
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "rbac_uniform.db")
    servers = [_StubServer("WEB01", tier=1), _StubServer("DC01", tier=0)]
    cfg = _StubConfig(servers, settings={"auth": {"enabled": True}})

    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)

    # Ensure shared globals point at our fresh fixtures even if a previous
    # test wired something else.
    shared._db = db
    shared._config = cfg

    client = app.test_client()
    return app, client, db, cfg


def _login_as(client, username, is_backup_admin=False):
    with client.session_transaction() as sess:
        sess["username"] = username
        sess["is_backup_admin"] = is_backup_admin


# ---------------------------------------------------------------------------
# Workflow execute
# ---------------------------------------------------------------------------

def _drawflow_with_node(node_type: str, server: str):
    """Build a minimal Drawflow canvas with a single node."""
    return json.dumps({
        "drawflow": {"Home": {"data": {
            "1": {
                "id": 1,
                "name": node_type,
                "data": {"server": server, "service": "Spooler"},
                "html": node_type,
                "inputs": {},
                "outputs": {"output_1": {"connections": []}},
            }
        }}}
    })


def test_workflow_execute_denied_without_admin(app_client):
    app, client, db, cfg = app_client
    # Grant alice only 'view' on WEB01 (so she's authenticated and ACL is
    # non-empty, but lacks admin).
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    db.grant_acl("admin", "*", "admin", granted_by="root")

    wf_id = db.create_workflow(
        name="restart-spooler",
        description="",
        category_id=None,
        trigger_type="manual",
        trigger_config="{}",
        canvas_json=_drawflow_with_node("restart_service", "WEB01"),
    )

    _login_as(client, "alice")
    r = client.post(f"/api/workflows/{wf_id}/execute")
    assert r.status_code == 403, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("ok") is False
    assert "WEB01" in body.get("error", "")


def test_workflow_execute_allowed_with_admin(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "admin", granted_by="root")

    wf_id = db.create_workflow(
        name="check-spooler",
        description="",
        category_id=None,
        trigger_type="manual",
        trigger_config="{}",
        canvas_json=_drawflow_with_node("check_service", "WEB01"),
    )

    _login_as(client, "alice")
    r = client.post(f"/api/workflows/{wf_id}/execute")
    # Should pass the RBAC gate. The actual workflow execution is async but
    # the endpoint returns 200 with execution_id once the gate is cleared.
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("ok") is True
    assert "execution_id" in body


def test_workflow_execute_rejects_winrm_node_with_empty_server(app_client):
    """A misconfigured workflow (winrm-block, empty server) must be rejected."""
    app, client, db, cfg = app_client
    db.grant_acl("admin", "*", "admin", granted_by="root")

    wf_id = db.create_workflow(
        name="bad-wf",
        description="",
        category_id=None,
        trigger_type="manual",
        trigger_config="{}",
        canvas_json=_drawflow_with_node("restart_service", ""),
    )

    _login_as(client, "admin")
    r = client.post(f"/api/workflows/{wf_id}/execute")
    assert r.status_code == 400
    assert "misconfigured" in r.get_json().get("error", "").lower() \
        or "empty" in r.get_json().get("error", "").lower()


# ---------------------------------------------------------------------------
# Runbook execute
# ---------------------------------------------------------------------------

def test_runbook_execute_denied_without_admin(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "control", granted_by="root")  # not admin
    db.grant_acl("admin", "*", "admin", granted_by="root")

    rid = db.create_runbook(
        name="rb-1", description="", category="general",
        steps_json='[]', created_by="admin", is_builtin=False,
    )

    _login_as(client, "alice")
    r = client.post(f"/api/runbooks/{rid}/execute",
                    json={"server_name": "WEB01", "dry_run": True})
    assert r.status_code == 403
    assert r.get_json().get("ok") is False


# ---------------------------------------------------------------------------
# Factory reset
# ---------------------------------------------------------------------------

def test_factory_reset_denied_without_admin(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("admin", "*", "admin", granted_by="root")
    db.grant_acl("alice", "WEB01", "admin", granted_by="root")  # per-server, not wildcard

    _login_as(client, "alice")
    r = client.post("/api/data/factory-reset")
    assert r.status_code == 403
    assert r.get_json().get("ok") is False


def test_factory_reset_denied_without_approval_token(app_client):
    """Even an admin must present a valid approval_id."""
    app, client, db, cfg = app_client
    # Make alice a wildcard admin
    db.grant_acl("alice", "*", "admin", granted_by="root")

    _login_as(client, "alice")
    r = client.post("/api/data/factory-reset")
    assert r.status_code == 403
    body = r.get_json()
    assert body.get("approval_required") is True


def test_factory_reset_succeeds_with_admin_and_approval(app_client):
    """Happy path: admin presents a consumed approval token."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "*", "admin", granted_by="root")

    # Bob (different user) requests, alice approves — but the approval flow
    # disallows self-approval, so we have alice request and bob approve.
    aid = db.create_approval_request(
        requested_by="alice", server_name="*", action="factory_reset",
        payload_json="{}",
    )
    assert db.decide_approval(aid, approver="bob", approved=True) is True

    # Patch the destructive bits so the test doesn't actually wipe anything.
    import routes.api._shared as shared
    shared._db.factory_reset = lambda: {"audit_log": 0}  # type: ignore[assignment]

    # Replace ConfigManager helpers used by the endpoint
    class _DummyLock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    cfg.config_path = str(__import__("pathlib").Path(app.instance_path) / "x.json")
    cfg._lock = _DummyLock()
    cfg._cache = None
    cfg._cache_mtime = 0.0
    import os
    os.makedirs(app.instance_path, exist_ok=True)
    with open(cfg.config_path, "w") as f:
        f.write("{}")

    _login_as(client, "alice")
    r = client.post(f"/api/data/factory-reset?approval_id={aid}")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("ok") is True

    # Approval is single-use: a second call with the same id must be rejected.
    r2 = client.post(f"/api/data/factory-reset?approval_id={aid}")
    assert r2.status_code == 403
