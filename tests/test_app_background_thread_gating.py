"""app.py's scheduler/watchdog threads must never start under pytest.

`collector_v2.start_collector_v2` was already correctly gated behind
`_under_pytest = "pytest" in sys.modules`. `restart_thread.start()`,
`workflow_thread.start()` and `watchdog_thread.start()` were not — they ran
unconditionally on `import app`, against the real config.json and the real
data/prism.db, in EVERY test process, including the ~30 test files in this
suite that import `app`. `restart_scheduler_loop` and `workflow_scheduler_loop`
poll real schedules and real workflow triggers and EXECUTE them against the
real 29-server fleet when one is due; the watchdog writes real
`thread_dead_*` / `thread_stuck_*` rows to the real audit_log the moment it
notices either scheduler isn't alive.

Checked live against the production database and config.json before this fix
landed: no restart/workflow audit activity from the day of the fix, and
nothing was currently armed to fire (restart schedule disabled, no
event-triggered workflows) — so this session caused no harm. But the gap was
real and independent of that day's configuration, and the next person to
enable a schedule and then run `pytest` would not be so lucky.

This can only be tested via a subprocess: the interesting behaviour is what
happens at `import app` time depending on whether 'pytest' is already in
sys.modules, which is exactly the condition that is always true inside this
test process itself. `tests/_app_gate_probe.py` does the import in a fresh
subprocess with `config_manager.CONFIG_PATH` / `database.DB_PATH`
monkeypatched to a throwaway tempdir BEFORE `import app` runs — both
constants are anchored to `Path(__file__).parent`, not the process cwd, so
nothing less direct actually keeps the probe off the real files.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBE = pathlib.Path(__file__).resolve().parent / "_app_gate_probe.py"


def _run_probe(mode: str) -> dict:
    # Piped via stdin, not passed as a file argument: the interpreter adds a
    # file argument's own directory to sys.path[0], and a stray same-named
    # module in that directory would silently shadow a stdlib import —
    # exactly what happened with a leftover scratch `dis.py` earlier in this
    # project. Stdin has no such directory to add.
    proc = subprocess.run(
        [sys.executable, "-", mode],
        input=PROBE.read_text(encoding="utf-8"),
        cwd=PROJECT_ROOT,
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        # app.py's own log lines carry em-dashes, and Python's logging
        # StreamHandler on this Windows console writes them in the console's
        # codepage, not UTF-8 — a strict decode intermittently raises inside
        # pytest's own capture-reader thread. This test only reads the single
        # JSON line the probe prints last; a mangled log byte elsewhere in
        # stdout is irrelevant to that, so replace rather than fail on it.
    )
    assert proc.returncode == 0, (
        f"probe subprocess (mode={mode}) exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    # The probe's own stdout carries app.py's real startup log lines ahead of
    # the one JSON line it prints last.
    last_line = proc.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


def test_the_probe_never_touches_the_real_config_or_database():
    """A guard on the guard: if this test's own sandbox were leaking, every
    other assertion in this file would be meaningless — it would be
    measuring the real app's real files, not proving anything about mode."""
    result = _run_probe("pytest")
    assert "prism_gate_probe_" in result["config_path_used"], result
    assert "prism_gate_probe_" in result["db_path_used"], result
    assert str(PROJECT_ROOT) not in result["config_path_used"], (
        f"probe used the REAL config.json: {result}")
    assert str(PROJECT_ROOT) not in result["db_path_used"], (
        f"probe used the REAL data/prism.db: {result}")


def test_no_background_thread_starts_under_pytest():
    """The regression test for the actual defect. Before the fix, all three
    of these were True regardless of mode."""
    result = _run_probe("pytest")
    assert result["restart_alive"] is False, result
    assert result["workflow_alive"] is False, result
    assert result["watchdog_alive"] is False, result


def test_all_three_threads_still_start_outside_pytest():
    """The other half: proves the gate actually branches on the condition
    rather than disabling the threads unconditionally, which would silently
    break real production startup instead of fixing a test hazard."""
    result = _run_probe("direct")
    assert result["restart_alive"] is True, result
    assert result["workflow_alive"] is True, result
    assert result["watchdog_alive"] is True, result
