"""Regression tests for the watchdog phantom-CRITICAL bug (council audit P0).

Before the fix, app.py coerced an un-ticked v2 heartbeat to a fake 9999s-stale
timestamp (`... or 9999`), so at cold start the watchdog logged a false
CRITICAL "thread alive but stuck" AND wrote a bogus thread_stuck audit row into
the tamper-evident audit_log. A real 0-seconds-ago tick was ALSO mis-coerced
(0 is falsy → 9999).

The fix extracts two pure helpers so the decision is unit-testable without
running the infinite watchdog loop, and treats:
  * None  -> 0.0  (no heartbeat yet: SKIP the stale check, never "stuck")
  * 0     -> now  (ticked this instant: fresh, never "stuck")
  * n     -> now-n (real age; "stuck" only if older than stale_factor×interval)
"""

from __future__ import annotations

import app


NOW = 1_000_000.0
INTERVAL = 36  # v2_workers interval used in app.py


def test_none_heartbeat_maps_to_skip_sentinel():
    # Un-ticked heartbeat (cold start) must NOT become a fake-stale timestamp.
    assert app._hb_ago_to_ts(None, NOW) == 0.0
    assert app._is_stuck(True, app._hb_ago_to_ts(None, NOW), INTERVAL, NOW) is False


def test_zero_seconds_ago_is_fresh_not_stale():
    # Ticked this instant → treated as `now`, not coerced to 9999s stale.
    hb = app._hb_ago_to_ts(0, NOW)
    assert hb == NOW
    assert app._is_stuck(True, hb, INTERVAL, NOW) is False


def test_genuinely_stale_heartbeat_is_stuck():
    # 10000s old with interval 36 (5×36 = 180s threshold) → genuinely stuck.
    hb = app._hb_ago_to_ts(10_000, NOW)
    assert app._is_stuck(True, hb, INTERVAL, NOW) is True


def test_dead_thread_not_reported_stuck_via_heartbeat():
    # A dead thread is handled by the is_alive() branch, not the stale branch.
    assert app._is_stuck(False, NOW, INTERVAL, NOW) is False
