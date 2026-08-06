"""Pytest fixtures shared across the suite.

Each test gets a fresh Database backed by an in-memory SQLite file (created
in a tmp dir) so tests are fully isolated. The collector thread is NEVER
started — tests stub server data directly via the DB API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the project root importable when running `pytest` from repo root or tests/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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
