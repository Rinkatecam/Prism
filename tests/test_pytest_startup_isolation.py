"""Regression test for the compliance-test production-DB-write bug.

`tests/test_compliance_phd_audit.py`'s `app_client_compliance_on` fixture
does `from app import app`, which runs app.py's module-level `db =
Database()` / `config = ConfigManager()` and wires `routes/api/_shared._db`
to the result. Before this fix, `Database()` / `ConfigManager()` with no
explicit path always fell back to `database.DB_PATH` / `config_manager.
CONFIG_PATH`, both hardcoded to `Path(__file__).parent / ...` — the real
project's `data/prism.db` and `config.json`, with no pytest-awareness at all.
`test_notes_with_html_returned_verbatim_by_api` then called
`_shared._db.insert_sop_execution(...)` directly, writing real rows to the
real `audit_log` / `sop_log` tables on every full-suite run. Confirmed
damage, disclosed and permanent: ids 1850-1871 / 834-855 (see
docs/plans/HANDOFF.md, 2026-09-03 contamination note).

The fix: `database.py` / `config_manager.py` now honour `PRISM_DB_PATH` /
`PRISM_CONFIG_PATH` (already documented in docs/csv/05_CONFIG_SPEC.md §D,
but never actually implemented until now), and `tests/conftest.py` sets both
unconditionally, at its own module level, before pytest collects any test
module. This file is the "guard on the guard": if conftest.py's redirect
were ever removed, weakened, or reordered, these tests fail loudly instead
of silently going back to writing production data.
"""
from __future__ import annotations

import pathlib

import database
import config_manager

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_database_default_path_is_redirected_under_pytest():
    assert "prism_pytest_session_" in str(database.DB_PATH), database.DB_PATH
    assert str(PROJECT_ROOT) not in str(database.DB_PATH), (
        f"database.DB_PATH still resolves under the real project root: {database.DB_PATH}")
    assert database.DB_PATH != PROJECT_ROOT / "data" / "prism.db"


def test_config_default_path_is_redirected_under_pytest():
    assert "prism_pytest_session_" in str(config_manager.CONFIG_PATH), config_manager.CONFIG_PATH
    assert str(PROJECT_ROOT) not in str(config_manager.CONFIG_PATH), (
        f"config_manager.CONFIG_PATH still resolves under the real project root: "
        f"{config_manager.CONFIG_PATH}")
    assert config_manager.CONFIG_PATH != PROJECT_ROOT / "config.json"


def test_bare_database_construction_does_not_touch_the_real_db():
    """No explicit path, exactly like app.py's `db = Database()`."""
    db = database.Database()
    assert str(PROJECT_ROOT) not in str(db.db_path), (
        f"Database() with no explicit path resolved to the real DB: {db.db_path}")


def test_bare_config_manager_construction_does_not_touch_the_real_config():
    """No explicit path, exactly like app.py's `config = ConfigManager()`."""
    cfg = config_manager.ConfigManager()
    assert str(PROJECT_ROOT) not in str(cfg.config_path), (
        f"ConfigManager() with no explicit path resolved to the real config: {cfg.config_path}")


def test_app_module_db_is_not_the_real_database():
    """app.py's own module-level `db = Database()`, the exact object
    `register_api_routes()` first wires into `routes.api._shared._db` at
    app-construction time. Checked directly on `app.db` rather than via
    `_shared._db` — several test files (test_rbac_uniform.py and six others)
    call `register_api_routes()` again with their own fixture db/config,
    reassigning that global, so which instance `_shared._db` currently holds
    depends on test collection order. `app.db` itself does not: it is set
    once, at import."""
    from app import db as app_db
    assert app_db.db_path == database.DB_PATH
    assert str(PROJECT_ROOT) not in str(app_db.db_path), (
        f"app.py's module-level db is wired to the REAL database: {app_db.db_path}")


def test_app_module_config_is_not_the_real_config():
    from app import config as app_config
    assert app_config.config_path == config_manager.CONFIG_PATH
    assert str(PROJECT_ROOT) not in str(app_config.config_path), (
        f"app.py's module-level config is wired to the REAL config.json: {app_config.config_path}")


def test_shared_db_is_never_the_real_database():
    """Whatever routes.api._shared._db currently points to — app.py's own
    instance, or a later test's fixture instance that re-called
    register_api_routes() — must never be the real production database. This
    is the exact global test_compliance_phd_audit.py's
    app_client_compliance_on fixture writes through."""
    from routes.api import _shared
    assert _shared._db is not None
    assert str(PROJECT_ROOT) not in str(_shared._db.db_path), (
        f"routes.api._shared._db is wired to the REAL database: {_shared._db.db_path}")


def test_shared_config_is_never_the_real_config():
    from routes.api import _shared
    assert _shared._config is not None
    assert str(PROJECT_ROOT) not in str(_shared._config.config_path), (
        f"routes.api._shared._config is wired to the REAL config.json: {_shared._config.config_path}")
