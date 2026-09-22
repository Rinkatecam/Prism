"""runbook_engine.py's built-in runbook catalogue.

No test file existed for this module before this one -- these tests cover
only the piece this change touches (BUILTIN_RUNBOOKS' shape and seeding),
not the execution engine itself (WinRM-dependent, out of scope here).

Diagnostic context for the "Disable Windows Search Service" entry: found
while investigating repeated RDS session-host freezes traced to WSearch
failing to clean up per-user index data at logoff (event ID 2), which
backs up Winlogon's own notification pipeline (event ID 6005) and leaves
some users with temporary profiles (event ID 1511). Disabling WSearch on
a session host is the standard, documented remediation -- it is not a
role Windows Search needs to play there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runbook_engine import BUILTIN_RUNBOOKS, seed_builtin_runbooks
from ps_sandbox import validate_script


def _find(name):
    matches = [rb for rb in BUILTIN_RUNBOOKS if rb["name"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} runbook, found {len(matches)}"
    return matches[0]


def test_every_builtin_runbook_has_the_required_shape():
    for rb in BUILTIN_RUNBOOKS:
        assert rb["name"].strip(), "runbook name must not be blank"
        assert rb["description"].strip(), f"{rb['name']}: description must not be blank"
        assert rb["category"].strip(), f"{rb['name']}: category must not be blank"
        assert rb["steps"], f"{rb['name']}: must have at least one step"
        for step in rb["steps"]:
            assert step["type"] in ("powershell", "wait"), (
                f"{rb['name']}: unknown step type {step['type']!r}")
            if step["type"] == "powershell":
                assert step["script"].strip(), f"{rb['name']}: empty script"
                assert isinstance(step.get("timeout"), int) and step["timeout"] > 0, (
                    f"{rb['name']}: timeout must be a positive int")


def test_the_new_runbooks_script_passes_the_sandbox():
    """The sandbox allowlist isn't actually wired into the save/execute path
    yet (see the separate follow-up on that gap -- confirmed for real here:
    the PRE-EXISTING "Clear Temp Files" built-in uses Remove-Item, which
    DEFAULT_ALLOWED_CMDLETS does not include, so a blanket "every built-in
    passes" assertion would be false today and is not this change's claim to
    make). This only pins the ONE script this change adds -- Stop-Service/
    Set-Service/Get-Service are all already in DEFAULT_ALLOWED_CMDLETS, so
    there is no reason for the new one to be an exception too."""
    rb = _find("Disable Windows Search Service")
    ok, reason = validate_script(rb["steps"][0]["script"])
    assert ok, f"script fails sandbox validation: {reason}"


def test_disable_windows_search_service_runbook_exists():
    rb = _find("Disable Windows Search Service")
    assert rb["category"] == "service"
    script = rb["steps"][0]["script"]
    assert "WSearch" in script
    assert "Stop-Service" in script
    assert "Set-Service" in script
    assert "Disabled" in script


@pytest.fixture()
def tmp_db(tmp_path):
    from database import Database
    return Database(tmp_path / "runbooks.db")


def test_seeding_inserts_the_new_runbook_alongside_the_existing_ones(tmp_db):
    seed_builtin_runbooks(tmp_db)
    names = {rb["name"] for rb in tmp_db.get_runbooks()}
    assert "Disable Windows Search Service" in names
    # The pre-existing five/six built-ins are untouched by this addition.
    assert "Restart Print Spooler" in names
    assert "Clear Temp Files" in names


def test_seeding_is_idempotent_for_the_new_runbook(tmp_db):
    seed_builtin_runbooks(tmp_db)
    seed_builtin_runbooks(tmp_db)  # second call must not duplicate or raise
    matches = [rb for rb in tmp_db.get_runbooks() if rb["name"] == "Disable Windows Search Service"]
    assert len(matches) == 1
    assert matches[0]["is_builtin"] == 1
