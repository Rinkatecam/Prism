"""Regression tests for the runbook PowerShell-sandbox enforcement gap.

Confirmed gap (this branch's starting point): routes/api/workflows.py's
create_runbook() only checked that ``steps_json`` was well-formed JSON --
it never ran the embedded PowerShell through ps_sandbox.validate_script().
runbook_engine.py's execute_runbook() then ran each step's script directly
via pypsrp against the real WinRM target, with no allowlist check at
execution time either. workflow_engine.py, by contrast, already calls
validate_script()/get_sandbox_settings() for its own free-form blocks
(``run_powershell``, ``condition``) -- see _exec_run_powershell and
_exec_condition -- so this gap was scoped to the runbook path only.

This file proves:
  1. create_runbook() rejects a disallowed cmdlet with 400, and nothing is
     persisted (routes/api/workflows.py::create_runbook, ::_validate_runbook_steps).
  2. update_runbook() closes the same "create clean, then PUT a bad script
     over it" bypass.
  3. A runbook whose script uses only allowed cmdlets is still accepted,
     unchanged behaviour from before this change.
  4. execute_runbook() re-validates immediately before opening the WinRM
     connection (defense in depth) and fails the run safely -- no pypsrp
     call is ever made for a blocked script.
  5. Every entry in runbook_engine.BUILTIN_RUNBOOKS keeps working, via the
     is_builtin exemption documented on execute_runbook() -- proved by
     showing (a) some of them would actually fail raw validate_script (so
     the exemption is load-bearing, not vacuous), and (b) executing them
     with is_builtin=True never hits the sandbox gate at all.
  6. The sandbox's enabled/allowed_cmdlets settings (via get_sandbox_settings)
     are honoured identically to how the workflow editor's preview endpoint
     already honours them -- sandbox disabled means anything is allowed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from ps_sandbox import validate_script, get_sandbox_settings
from runbook_engine import BUILTIN_RUNBOOKS, execute_runbook


# ---------------------------------------------------------------------------
# Flask test-client fixture -- copied from tests/test_workflow_authoring_rbac.py
# ---------------------------------------------------------------------------

class _StubConfig:
    def __init__(self, settings=None):
        self._settings = settings or {"auth": {"enabled": True}}

    def get_servers(self):
        return []

    def get_server_by_name(self, name):
        return None

    def get_settings(self):
        return self._settings

    def get_raw_servers(self):
        return []

    def get_maintenance_windows(self):
        return []


@pytest.fixture()
def app_client(tmp_path):
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "runbook_sandbox.db")
    cfg = _StubConfig(settings={"auth": {"enabled": True}})
    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)
    shared._db = db
    shared._config = cfg
    return app, app.test_client(), db, cfg


def _login_as(client, username):
    with client.session_transaction() as sess:
        sess["username"] = username


DISALLOWED_STEPS = json.dumps([
    {"type": "powershell", "script": "Remove-Item C:\\Temp\\*", "timeout": 30},
])
ALLOWED_STEPS = json.dumps([
    {"type": "powershell", "script": "Get-Service Spooler", "timeout": 30},
])


# ---------------------------------------------------------------------------
# 1 & 3 -- create_runbook() save-time gate
# ---------------------------------------------------------------------------

def test_create_runbook_rejects_disallowed_cmdlet(app_client):
    app, client, db, cfg = app_client
    _login_as(client, "alice")

    r = client.post("/api/runbooks", json={
        "name": "evil", "steps_json": DISALLOWED_STEPS})

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is False
    assert "sandbox" in body["error"].lower()
    assert not any(rb["name"] == "evil" for rb in db.get_runbooks()), (
        "rejected runbook must not be persisted")


def test_create_runbook_accepts_allowed_cmdlets(app_client):
    """Unchanged behaviour: a script built entirely from allowlisted
    cmdlets is still accepted and persisted."""
    app, client, db, cfg = app_client
    _login_as(client, "alice")

    r = client.post("/api/runbooks", json={
        "name": "benign", "steps_json": ALLOWED_STEPS})

    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    rows = [rb for rb in db.get_runbooks() if rb["name"] == "benign"]
    assert len(rows) == 1
    assert json.loads(rows[0]["steps_json"]) == json.loads(ALLOWED_STEPS)


# ---------------------------------------------------------------------------
# 2 -- update_runbook() closes the "create clean, PUT dirty" bypass
# ---------------------------------------------------------------------------

def test_update_runbook_rejects_disallowed_cmdlet(app_client):
    app, client, db, cfg = app_client
    _login_as(client, "alice")

    r = client.post("/api/runbooks", json={
        "name": "later-evil", "steps_json": ALLOWED_STEPS})
    assert r.status_code == 201
    rid = r.get_json()["id"]

    r2 = client.put(f"/api/runbooks/{rid}", json={"steps_json": DISALLOWED_STEPS})

    assert r2.status_code == 400, r2.get_data(as_text=True)
    assert "sandbox" in r2.get_json()["error"].lower()
    row = db.get_runbook(rid)
    assert json.loads(row["steps_json"]) == json.loads(ALLOWED_STEPS), (
        "the disallowed update must not have overwritten the saved script")


# ---------------------------------------------------------------------------
# 6 -- sandbox enabled/allowed_cmdlets settings are honoured at save time
# ---------------------------------------------------------------------------

def test_create_runbook_allows_anything_when_sandbox_disabled(app_client):
    app, client, db, cfg = app_client
    cfg._settings = {"auth": {"enabled": True},
                      "workflows": {"sandbox": {"enabled": False}}}
    _login_as(client, "alice")

    r = client.post("/api/runbooks", json={
        "name": "disabled-sandbox", "steps_json": DISALLOWED_STEPS})

    assert r.status_code == 201, r.get_data(as_text=True)
    assert any(rb["name"] == "disabled-sandbox" for rb in db.get_runbooks())


def test_create_runbook_honours_allowed_cmdlets_extras(app_client):
    """A cmdlet that is merely absent from DEFAULT_ALLOWED_CMDLETS (not
    hard-denied) can be permitted via the per-deployment
    workflows.sandbox.allowed_cmdlets setting, same as the workflow
    editor's preview endpoint (validate_workflow_script)."""
    app, client, db, cfg = app_client
    cfg._settings = {"auth": {"enabled": True},
                      "workflows": {"sandbox": {"enabled": True,
                                                 "allowed_cmdlets": ["Clear-DnsClientCache"]}}}
    _login_as(client, "alice")

    r = client.post("/api/runbooks", json={
        "name": "flush-dns", "steps_json": json.dumps([
            {"type": "powershell", "script": "Clear-DnsClientCache", "timeout": 15},
        ])})

    assert r.status_code == 201, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 4 -- execute_runbook() defense-in-depth gate
# ---------------------------------------------------------------------------

class _SyncThread:
    """Runs the target synchronously so the test can assert immediately
    after execute_runbook() returns, without a real background thread."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


def test_execute_runbook_blocks_disallowed_script_before_winrm(monkeypatch):
    """A runbook whose script contains a disallowed cmdlet must fail safely
    at execute time -- and never reach pypsrp/WinRM at all."""
    import runbook_engine
    monkeypatch.setattr(runbook_engine.threading, "Thread", _SyncThread)

    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 1, "name": "evil", "is_builtin": 0,
        "steps_json": json.dumps([
            {"type": "powershell", "script": "Remove-Item C:\\Temp\\*", "timeout": 30},
        ]),
    }
    db.insert_runbook_execution.return_value = 7

    with patch("winrm_factory.make_wsman") as m_make, \
         patch("pypsrp.powershell.PowerShell") as m_ps, \
         patch("pypsrp.powershell.RunspacePool") as m_pool:
        exec_id = execute_runbook(
            db, runbook_id=1, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=False, executed_by="alice", settings={},
        )

    assert exec_id == 7
    assert m_make.call_count == 0, "blocked script must never open a WinRM connection"
    assert m_ps.call_count == 0
    assert m_pool.call_count == 0

    # Final status must be a real failure, not a silent skip/success.
    final_call = db.update_runbook_execution.call_args
    assert final_call.kwargs["status"] == "failed"
    assert "sandbox" in final_call.kwargs["output"].lower()
    assert "Step 1" in final_call.kwargs["output"]


def test_execute_runbook_allows_allowed_script(monkeypatch):
    """Regression: an allowed script must still actually execute."""
    import runbook_engine
    monkeypatch.setattr(runbook_engine.threading, "Thread", _SyncThread)

    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 2, "name": "benign", "is_builtin": 0,
        "steps_json": json.dumps([
            {"type": "powershell", "script": "Get-Service Spooler", "timeout": 30},
        ]),
    }
    db.insert_runbook_execution.return_value = 8

    mock_ps_instance = MagicMock()
    mock_ps_instance.invoke.return_value = ["Running"]
    mock_ps_instance.had_errors = False
    mock_ps_instance.streams = SimpleNamespace(error=[])

    mock_pool_cm = MagicMock()
    mock_pool_cm.__enter__.return_value = MagicMock()
    mock_pool_cm.__exit__.return_value = False

    with patch("winrm_factory.make_wsman", return_value=MagicMock()) as m_make, \
         patch("pypsrp.powershell.PowerShell", return_value=mock_ps_instance) as m_ps, \
         patch("pypsrp.powershell.RunspacePool", return_value=mock_pool_cm):
        execute_runbook(
            db, runbook_id=2, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=False, executed_by="alice", settings={},
        )

    assert m_make.call_count == 1, "an allowed script must still connect and run"
    final_call = db.update_runbook_execution.call_args
    assert final_call.kwargs["status"] == "completed"


# ---------------------------------------------------------------------------
# 5 -- every BUILTIN_RUNBOOKS entry keeps working
# ---------------------------------------------------------------------------

def test_some_builtins_would_fail_raw_validation_so_exemption_is_load_bearing():
    """Prove the is_builtin exemption is actually doing work, not vacuous.

    Confirmed directly: "Clear Temp Files" uses Remove-Item, which is in
    ps_sandbox.HARD_DENY (checked before, and independent of, the
    allowlist) -- so no allowed_cmdlets setting could ever make it pass.
    "Restart Print Spooler" (Start-Sleep) and "Flush DNS Cache"
    (Clear-DnsClientCache) are merely absent from DEFAULT_ALLOWED_CMDLETS.
    """
    enabled, extras, max_len = get_sandbox_settings({})
    results = {}
    for rb in BUILTIN_RUNBOOKS:
        for step in rb["steps"]:
            if step["type"] == "powershell":
                ok, reason = validate_script(step["script"], allowed_cmdlets=extras,
                                              enabled=enabled)
                results[rb["name"]] = (ok, reason)

    assert results["Clear Temp Files"][0] is False
    assert "Remove-Item" in results["Clear Temp Files"][1]


@pytest.mark.parametrize("rb", BUILTIN_RUNBOOKS, ids=[rb["name"] for rb in BUILTIN_RUNBOOKS])
def test_every_builtin_runbook_executes_despite_the_sandbox_gate(monkeypatch, rb):
    """Every entry in BUILTIN_RUNBOOKS -- whatever it contains -- must still
    run when is_builtin=True, proving the exemption covers all of them
    (rather than assuming it does)."""
    import runbook_engine
    monkeypatch.setattr(runbook_engine.threading, "Thread", _SyncThread)

    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 99, "name": rb["name"], "is_builtin": 1,
        "steps_json": json.dumps(rb["steps"]),
    }
    db.insert_runbook_execution.return_value = 42

    mock_ps_instance = MagicMock()
    mock_ps_instance.invoke.return_value = ["ok"]
    mock_ps_instance.had_errors = False
    mock_ps_instance.streams = SimpleNamespace(error=[])

    mock_pool_cm = MagicMock()
    mock_pool_cm.__enter__.return_value = MagicMock()
    mock_pool_cm.__exit__.return_value = False

    with patch("winrm_factory.make_wsman", return_value=MagicMock()) as m_make, \
         patch("pypsrp.powershell.PowerShell", return_value=mock_ps_instance), \
         patch("pypsrp.powershell.RunspacePool", return_value=mock_pool_cm):
        execute_runbook(
            db, runbook_id=99, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=False, executed_by="system", settings={},
        )

    assert m_make.call_count == 1, (
        f"built-in runbook {rb['name']!r} must run despite the sandbox gate "
        f"(is_builtin exemption)")
    final_call = db.update_runbook_execution.call_args
    assert final_call.kwargs["status"] == "completed"
    assert "BLOCKED by PowerShell sandbox" not in final_call.kwargs["output"]


# ---------------------------------------------------------------------------
# 6 (execute-time half) -- sandbox settings honoured at execute time too
# ---------------------------------------------------------------------------

def test_execute_runbook_allows_disallowed_script_when_sandbox_disabled(monkeypatch):
    import runbook_engine
    monkeypatch.setattr(runbook_engine.threading, "Thread", _SyncThread)

    db = MagicMock()
    db.get_runbook.return_value = {
        "id": 3, "name": "evil-but-unsandboxed", "is_builtin": 0,
        "steps_json": json.dumps([
            {"type": "powershell", "script": "Remove-Item C:\\Temp\\*", "timeout": 30},
        ]),
    }
    db.insert_runbook_execution.return_value = 9

    mock_ps_instance = MagicMock()
    mock_ps_instance.invoke.return_value = ["done"]
    mock_ps_instance.had_errors = False
    mock_ps_instance.streams = SimpleNamespace(error=[])

    mock_pool_cm = MagicMock()
    mock_pool_cm.__enter__.return_value = MagicMock()
    mock_pool_cm.__exit__.return_value = False

    settings = {"workflows": {"sandbox": {"enabled": False}}}

    with patch("winrm_factory.make_wsman", return_value=MagicMock()) as m_make, \
         patch("pypsrp.powershell.PowerShell", return_value=mock_ps_instance), \
         patch("pypsrp.powershell.RunspacePool", return_value=mock_pool_cm):
        execute_runbook(
            db, runbook_id=3, server_name="srv1",
            server_config=SimpleNamespace(host="srv1", username="u", password="p"),
            dry_run=False, executed_by="alice", settings=settings,
        )

    assert m_make.call_count == 1, "sandbox disabled must allow the script through"
    final_call = db.update_runbook_execution.call_args
    assert final_call.kwargs["status"] == "completed"
