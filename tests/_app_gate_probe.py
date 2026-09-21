"""Helper for test_app_background_thread_gating.py — NOT collected by pytest
(no test_ prefix). Run standalone in a subprocess: `python - <mode>` with this
file piped to stdin (stdin, not a file argument, so the interpreter never adds
this directory to sys.path[0] — a stray same-named module there would shadow
a stdlib import exactly like a leftover scratch `dis.py` once did in this
project).

Imports app.py in a subprocess with config_manager.CONFIG_PATH and
database.DB_PATH monkeypatched to a fresh tempdir BEFORE `import app` runs,
so the module-level `db = Database()` / `config = ConfigManager()` in app.py
resolve to throwaway files no matter what. Both constants are anchored to
Path(__file__).parent in their own modules, not the process cwd, so nothing
short of patching the constants themselves keeps app.py off the real
config.json / data/prism.db.

mode "pytest": fakes sys.modules['pytest'] before import — restart_thread,
  workflow_thread and watchdog_thread must all stay unstarted.
mode "direct": does not — they must all start (only against the fake
  config/db this script builds), proving the gate actually branches both
  ways rather than disabling the threads unconditionally.
"""
import json
import sys
import pathlib
import tempfile
import types

mode = sys.argv[1]

if mode == "pytest":
    sys.modules["pytest"] = types.ModuleType("pytest")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="prism_gate_probe_"))
cfg_path = tmp / "config.json"
db_path = tmp / "data" / "prism.db"

cfg_path.write_text(json.dumps({
    "servers": [],
    "settings": {
        "scheduled_server_restarts": [],
        "scheduled_server_restart_schedule": {"enabled": False},
        "scheduled_flask_restart": {"enabled": False},
        "collector_v2_num_workers": 2,
    },
}), encoding="utf-8")

import config_manager
import database
config_manager.CONFIG_PATH = cfg_path
database.DB_PATH = db_path

import app  # noqa: E402  (the module-level side effects are what's under test)

print(json.dumps({
    "mode": mode,
    "config_path_used": str(app.config.config_path),
    "db_path_used": str(app.db.db_path),
    "restart_alive": app.restart_thread.is_alive(),
    "workflow_alive": app.workflow_thread.is_alive(),
    "watchdog_alive": app.watchdog_thread.is_alive(),
}))
sys.exit(0)
