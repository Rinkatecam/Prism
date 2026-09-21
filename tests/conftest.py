"""Pytest fixtures shared across the suite.

Each test gets a fresh Database backed by an in-memory SQLite file (created
in a tmp dir) so tests are fully isolated. The collector thread is NEVER
started — tests stub server data directly via the DB API.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Make the project root importable when running `pytest` from repo root or tests/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Redirect the WHOLE pytest session off the real production database and
# config.json before any test module gets a chance to import database.py /
# config_manager.py / app.py. Both modules compute a module-level DB_PATH /
# CONFIG_PATH constant at first import, anchored to Path(__file__).parent —
# so app.py's `db = Database()` / `config = ConfigManager()` bind to whatever
# these env vars say the very first time ANYTHING imports app. This must run
# here, at conftest.py's own module level (collection time), not inside a
# fixture — fixtures run too late, after test modules are already imported.
#
# This is what closes the gap that let tests/test_compliance_phd_audit.py's
# app_client_compliance_on fixture (`from app import app`) write real rows to
# the production audit_log/sop_log (ids 1850-1871 / 834-855, disclosed and
# permanent — see docs/plans/HANDOFF.md). Unconditional assignment, not
# setdefault: test isolation must not depend on trusting whatever an ambient
# shell environment happens to have set.
_session_tmp = Path(tempfile.mkdtemp(prefix="prism_pytest_session_"))
os.environ["PRISM_CONFIG_PATH"] = str(_session_tmp / "config.json")
os.environ["PRISM_DB_PATH"] = str(_session_tmp / "data" / "prism_test.db")
atexit.register(shutil.rmtree, _session_tmp, ignore_errors=True)


@pytest.fixture()
def tmp_db(tmp_path):
    """Fresh Database in a temp directory (file-based so triggers run)."""
    from database import Database
    db_path = tmp_path / "prism_test.db"
    return Database(db_path)


@pytest.fixture()
def fresh_config(tmp_path, monkeypatch):
    """A ConfigManager pointing at an empty config.json under tmp_path."""
    from config_manager import ConfigManager
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"servers": [], "settings": {}}', encoding="utf-8")
    return ConfigManager(cfg_file)


@pytest.fixture(autouse=True)
def _isolate_estate_state():
    """Reset the estate service's module state around EVERY test.

    `estate_service` publishes one process-wide verdict, and
    `routes.views._estate_vitals` reads it — so a test that folds a fleet
    leaks a severity into every later test that renders vitals. That is
    exactly what happened: six tests in test_estate_vitals.py passed alone
    and failed in the full suite, depending on file order.

    Autouse rather than opt-in: the leak crosses FILES, so any test that
    forgets the fixture is a test that can be poisoned by a neighbour. The
    reset runs before AND after — before so a test starts clean, after so a
    test never leaves a verdict behind for whatever pytest-randomly puts
    next.
    """
    try:
        import estate_service
    except Exception:
        yield
        return
    estate_service.reset()
    yield
    estate_service.reset()
