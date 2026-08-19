"""Accelerated polling has to STOP when the machine is back.

FOUND BY THE OWNER, watching a real domain controller. A manual restart armed
twenty minutes of accelerated polling, which at the supervisor's 5 s tick is
roughly 240 forced WinRM checks of one machine. The host rebooted in fifty
seconds. Counted from the metrics table: 184 samples in twenty minutes against
21 for a comparable server in the same window — nine times the load, and almost
all of it after the machine was healthy again.

THERE WAS AN EARLY RELEASE, AND IT COULD NEVER FIRE. It hangs off the
update-install state machine's stabilising window, and a manual restart never
creates an install-state row — so for the one code path an operator triggers by
hand, the only exit was the twenty-minute timer. A mechanism that exists, is
correct, and is unreachable from the case that needs it: the same shape as
OPS-LEARNINGS #34.

THE SAFETY PROPERTY, and it is the reason this is a new primitive rather than a
call to `accelerate_server` with a small number: `settle_acceleration` SHORTENS an
active window and NEVER ARMS one. Arming on "the server just came back" would
start hammering any machine that briefly blipped and recovered — turning a fix
for excessive polling into a cause of it. Every test below that says "does not
arm" is protecting that.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector_v2 import state                                  # noqa: E402
from collector_v2.supervisor import settle_acceleration         # noqa: E402
from collector_v2.types import ServerHealth                     # noqa: E402


@pytest.fixture(autouse=True)
def _clean_health():
    with state._server_health_lock:
        state.server_health.clear()
    yield
    with state._server_health_lock:
        state.server_health.clear()


def _track(name: str, accelerated_for: float | None, reason: str = "manual_restart"):
    h = ServerHealth(name=name)
    if accelerated_for is not None:
        h.accelerated_until = (datetime.now(timezone.utc)
                               + timedelta(seconds=accelerated_for))
        h.accelerated_reason = reason
    with state._server_health_lock:
        state.server_health[name] = h
    return h


def _remaining(name: str) -> float:
    with state._server_health_lock:
        h = state.server_health[name]
        if h.accelerated_until is None:
            return 0.0
        return (h.accelerated_until - datetime.now(timezone.utc)).total_seconds()


# ── it shortens ───────────────────────────────────────────────────────────

def test_a_long_window_is_cut_to_the_settle_window():
    """The fix: twenty minutes becomes one, the moment the host reports back."""
    _track("SRV", accelerated_for=20 * 60)
    assert settle_acceleration("SRV", duration_s=60) is True
    assert 0 < _remaining("SRV") <= 61


def test_the_server_is_still_accelerated_briefly_afterwards():
    """Not a hard stop: the metrics still need to settle, which is what the
    stabilising window is for. Cutting straight to zero would drop back to the
    60 s cadence while a freshly booted server's CPU is still spiking."""
    _track("SRV", accelerated_for=20 * 60)
    settle_acceleration("SRV", duration_s=60)
    with state._server_health_lock:
        assert state.server_health["SRV"].is_accelerated() is True


# ── it never arms: the property that makes this safe ──────────────────────

def test_a_server_that_was_never_accelerated_is_not_accelerated_now():
    """The whole reason this is not `accelerate_server(name, 60)`. Every server
    that blips offline and comes back would otherwise start being polled every
    five seconds — a fix for excessive polling that causes it."""
    _track("SRV", accelerated_for=None)
    assert settle_acceleration("SRV", duration_s=60) is False
    with state._server_health_lock:
        assert state.server_health["SRV"].accelerated_until is None
        assert state.server_health["SRV"].is_accelerated() is False


def test_an_untracked_server_is_not_created():
    """No row, no acceleration, and no row invented either — a sentinel here
    would be picked up by the supervisor on its next tick and start polling a
    machine nobody asked about."""
    assert settle_acceleration("NEVER-SEEN", duration_s=60) is False
    with state._server_health_lock:
        assert "NEVER-SEEN" not in state.server_health


def test_an_expired_window_is_not_revived():
    """An expired window means acceleration already ended. Re-stamping it would
    restart the polling this function exists to stop."""
    h = _track("SRV", accelerated_for=-30)          # ended thirty seconds ago
    before = h.accelerated_until
    assert settle_acceleration("SRV", duration_s=60) is False
    with state._server_health_lock:
        assert state.server_health["SRV"].accelerated_until == before
        assert state.server_health["SRV"].is_accelerated() is False


def test_a_shorter_window_is_never_lengthened():
    """The stabilising window is a CEILING, not a target. A server with twenty
    seconds left must not be given sixty."""
    _track("SRV", accelerated_for=20)
    assert settle_acceleration("SRV", duration_s=60) is False
    assert _remaining("SRV") <= 21


def test_zero_ends_it_immediately_without_arming():
    _track("SRV", accelerated_for=20 * 60)
    assert settle_acceleration("SRV", duration_s=0) is True
    with state._server_health_lock:
        assert state.server_health["SRV"].is_accelerated() is False


def test_a_negative_duration_is_treated_as_zero_not_as_the_past():
    """A negative value must not stamp a time in the PAST. The lower bound is
    the assertion that matters: `<= 1` alone passed while the code stamped
    now-500s, because a time in the past is also a time under one second away.
    A stamp in the past is a different state from "just ended" to anything that
    later compares timestamps."""
    _track("SRV", accelerated_for=20 * 60)
    assert settle_acceleration("SRV", duration_s=-500) is True
    remaining = _remaining("SRV")
    # The window is stamped a few microseconds before it is read back, so the
    # lower bound is -1 rather than 0. It still catches the defect it is for:
    # an unclamped -500 lands five hundred seconds the wrong side of it.
    assert -1 <= remaining <= 1, f"stamped {remaining:.0f}s from now"


# ── the wiring: it has to be called where the comeback is observed ────────

def test_the_aggregator_releases_on_the_transition_into_healthy():
    """A helper nobody calls is this repo's most-repeated failure, and here it
    would leave the twenty-minute hammer exactly as it was."""
    import ast
    src = (PROJECT_ROOT / "collector_v2" / "aggregator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_handle_status_transition")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_settle" in called, (
        "_handle_status_transition does not release acceleration, so a manual "
        "restart still polls the host every five seconds for its whole window")


def test_the_release_is_not_behind_the_maintenance_gate():
    """A patch window is exactly when a machine is most likely to be restarting,
    so it is exactly when the release must not be suppressed. The maintenance
    gate returns early; anything after it does not run for a suppressed server."""
    src = (PROJECT_ROOT / "collector_v2" / "aggregator.py").read_text(encoding="utf-8")
    body = src[src.index("def _handle_status_transition"):]
    body = body[:body.index("\n    def ")]
    release = body.index("settle_acceleration")
    gate = body.index("if maint_suppressed:")
    assert release < gate, (
        "the acceleration release sits after the maintenance gate's early "
        "return, so a server restarting inside a patch window keeps being "
        "hammered for the full window")


def test_a_manual_restart_does_not_arm_the_ceiling():
    """It armed 20*60, which IS the supervisor's safety ceiling — and the comment
    beside that ceiling says such a value is "almost certainly a bug"."""
    import re
    from collector_v2.supervisor import _ACCELERATE_MAX_DURATION_S
    src = (PROJECT_ROOT / "routes" / "api" / "power.py").read_text(encoding="utf-8")
    m = re.search(r'accelerate_server\(name,\s*duration_s=([^,]+),\s*reason="manual_restart"\)', src)
    assert m, "the manual-restart acceleration call moved"
    seconds = eval(m.group(1), {"__builtins__": {}}, {})   # noqa: S307 - a literal
    assert seconds < _ACCELERATE_MAX_DURATION_S, (
        f"a manual restart arms {seconds}s, which is the safety ceiling "
        f"({_ACCELERATE_MAX_DURATION_S}s) the supervisor warns about")
    assert seconds <= 10 * 60, (
        f"{seconds}s of five-second polling on one host is more than a reboot "
        "needs; the release handles the normal case, so this is only the bound "
        "for a machine that never returns")
