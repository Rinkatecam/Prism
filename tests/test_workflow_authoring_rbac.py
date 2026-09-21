"""Authoring a WinRM-reaching workflow needs the permission to run it.

Collector audit finding 3 (HIGH), the structural half. The auditor's finding was
not "a regex sandbox is imperfect" — it was that **a limited-RBAC operator who
can author a workflow gets RCE as the service account**.

THE PATH THAT MADE IT REAL, and why the gate has to be at authoring. Executing a
workflow by hand already required per-server ADMIN on every server its blocks
touch. But a workflow can also fire from a SCHEDULE, and the scheduler executes
with no user present — so there is no permission to check and none was checked.
The whole escalation was therefore:

    1. create a workflow with a `run_powershell` block   (needed only a login)
    2. set its trigger to `scheduled`                    (needed only a login)
    3. wait

No amount of sandbox hardening closes that, because the sandbox is about WHAT
the script may do and this is about WHO may cause it to run at all. The fix is
one rule: **you may author what you may run.** Creating, updating or cloning a
workflow containing a block that reaches WinRM requires the same per-server
admin grant that manual execution requires — checked at the only moment a user
is present.

Two consequences worth stating because they are easy to get wrong:

  * UPDATE must be gated too, or "create an empty workflow, then fill it in" is
    the bypass, and it is the obvious one.
  * CLONE must be gated too, because a clone copies somebody else's blocks into
    a workflow the cloner owns and can schedule.

An EMPTY server field is allowed at authoring. A canvas under construction has
half-configured nodes, and rejecting those would make the editor unusable; the
permission is checked against whatever server is actually named, and naming one
later is an update, which is checked then.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask


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
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "wf_authoring_rbac.db")
    cfg = _StubConfig([_StubServer("WEB01"), _StubServer("DC01", tier=0)],
                      settings={"auth": {"enabled": True}})
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)
    shared._db = db
    shared._config = cfg
    return app, app.test_client(), db, cfg


def _login_as(client, username, is_backup_admin=False):
    with client.session_transaction() as sess:
        sess["username"] = username
        sess["is_backup_admin"] = is_backup_admin


def _canvas(node_type: str, server: str) -> dict:
    return {"drawflow": {"Home": {"data": {
        "1": {"id": 1, "name": node_type,
              "data": {"server": server, "script": "Get-Service -Name Spooler"},
              "html": node_type, "inputs": {},
              "outputs": {"output_1": {"connections": []}}},
    }}}}


def _open_workflows(db):
    return db.get_workflows(include_templates=True)


# ── create ────────────────────────────────────────────────────────────────

def test_creating_a_powershell_workflow_without_admin_is_denied(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "pwn", "canvas_json": _canvas("run_powershell", "WEB01"),
        "trigger_type": "scheduled"})

    assert r.status_code == 403, r.get_data(as_text=True)
    assert "WEB01" in (r.get_json() or {}).get("error", "")
    assert not _open_workflows(db), "the workflow was created despite the denial"


def test_creating_a_powershell_workflow_with_admin_is_allowed(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "admin", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "diagnose", "canvas_json": _canvas("run_powershell", "WEB01")})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert len(_open_workflows(db)) == 1


def test_control_permission_is_not_enough_to_author(app_client):
    """The gate asks for ADMIN, matching execution exactly. `control` is the
    level that restarts a service through the UI; it must not also be the level
    that plants an arbitrary script on a timer. Tested separately from the
    `view` case because `view` fails either way — only a `control` user can tell
    an admin check apart from a control check.
    """
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "control", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "escalate", "canvas_json": _canvas("run_powershell", "WEB01")})

    assert r.status_code == 403, r.get_data(as_text=True)
    assert not _open_workflows(db)


def test_the_scheduled_trigger_escalation_is_closed(app_client):
    """The audit's path, end to end. The scheduler runs with no user, so this
    is the last moment anything can be checked."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "control", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "time-bomb",
        "canvas_json": _canvas("run_powershell", "WEB01"),
        "trigger_type": "scheduled",
        "trigger_config": {"schedule": "daily", "time": "03:00"}})

    assert r.status_code == 403
    assert not _open_workflows(db)


def test_an_ambient_block_needs_no_server_permission(app_client):
    """`send_email` and `wait` never touch WinRM. Gating them would make the
    rule about workflows rather than about remote execution."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "notify-only", "canvas_json": _canvas("send_email", "WEB01")})

    assert r.status_code == 200, r.get_data(as_text=True)


def test_a_node_with_no_server_yet_is_allowed(app_client):
    """A canvas under construction has half-configured nodes. Rejecting them
    would make the editor unusable, and naming a server later is an update —
    which is checked."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    _login_as(client, "alice")

    r = client.post("/api/workflows", json={
        "name": "wip", "canvas_json": _canvas("run_powershell", "")})

    assert r.status_code == 200, r.get_data(as_text=True)


# ── update: the obvious bypass ────────────────────────────────────────────

def test_updating_a_workflow_to_add_a_powershell_block_is_denied(app_client):
    """Without this, "create it empty and fill it in" is the whole bypass."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    wf_id = db.create_workflow(name="wip", description="", category_id=None,
                               trigger_type="manual", trigger_config="{}",
                               canvas_json="{}")
    _login_as(client, "alice")

    r = client.put(f"/api/workflows/{wf_id}", json={
        "name": "wip", "canvas_json": _canvas("run_powershell", "WEB01")})

    assert r.status_code == 403, r.get_data(as_text=True)
    row = next(w for w in _open_workflows(db) if w["id"] == wf_id)
    assert "run_powershell" not in (row.get("canvas_json") or "")


def test_updating_with_admin_is_allowed(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "admin", granted_by="root")
    wf_id = db.create_workflow(name="wip", description="", category_id=None,
                               trigger_type="manual", trigger_config="{}",
                               canvas_json="{}")
    _login_as(client, "alice")

    r = client.put(f"/api/workflows/{wf_id}", json={
        "name": "wip", "canvas_json": _canvas("run_powershell", "WEB01")})

    assert r.status_code == 200, r.get_data(as_text=True)


# ── clone: somebody else's blocks, your schedule ──────────────────────────

def test_cloning_a_powershell_workflow_without_admin_is_denied(app_client):
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    src = db.create_workflow(
        name="admins-tool", description="", category_id=None,
        trigger_type="manual", trigger_config="{}",
        canvas_json=json.dumps(_canvas("run_powershell", "WEB01")))
    _login_as(client, "alice")

    r = client.post(f"/api/workflows/{src}/clone")

    assert r.status_code == 403, r.get_data(as_text=True)
    assert len(_open_workflows(db)) == 1, "the clone was created anyway"


# ── the denial has to be auditable ────────────────────────────────────────

def test_a_denied_authoring_attempt_is_audited(app_client):
    """An attempt to plant a scheduled script is exactly the event a later
    investigation needs to find."""
    app, client, db, cfg = app_client
    db.grant_acl("alice", "WEB01", "view", granted_by="root")
    _login_as(client, "alice")

    client.post("/api/workflows", json={
        "name": "pwn", "canvas_json": _canvas("run_powershell", "WEB01"),
        "trigger_type": "scheduled"})

    rows = db.get_audit_log(limit=50)
    hits = [r for r in rows
            if "rbac_denied" in (r.get("action") or "")
            and "WEB01" in (r.get("details") or "")]
    assert hits, f"no audit row for the denial; saw {[r.get('action') for r in rows]}"
