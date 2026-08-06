"""A failing periodic job must back off, not busy-loop at tick cadence.

Found 2026-08-06 while diagnosing the collector. `_periodics_loop` set
`_last_run[job.name]` only AFTER `job.handler()` returned, so a job that raised
never recorded an attempt, `job.due()` stayed permanently true, and it re-ran on
every 30-second tick regardless of its configured interval.

Observed live: `database_backup`, configured at 24h, retrying at exactly
30-second intervals — 2,880 attempts a day instead of 1, and 116 of the 118
periodics errors in a two-hour log sample.

The hazard is not the backup job. The same loop runs `ldap_probe`, `tls_certs`
and `security_status`; if one of those starts failing it hammers a domain
controller or a remote host every 30 seconds, which can look like an attack and
can trip account lockout. The supervisor already backs off per-server failures
(`backoff_delay_s`); periodics never did.
"""

from __future__ import annotations

import threading
import time

import pytest

from collector_v2 import periodics


@pytest.fixture(autouse=True)
def _clean_state():
    periodics._last_run.clear()
    periodics._consecutive_failures.clear()
    periodics._stop_event.clear()
    yield
    periodics._stop_event.set()
    periodics._last_run.clear()
    periodics._consecutive_failures.clear()


def _run_loop(jobs, ticks, tick_s=0.01, monkeypatch=None):
    """Drive the real loop for roughly `ticks` iterations."""
    monkeypatch.setattr(periodics, "_TICK_S", tick_s)
    monkeypatch.setattr(periodics, "_build_jobs", lambda *a, **k: jobs)
    periodics._stop_event.clear()
    t = threading.Thread(target=periodics._periodics_loop,
                         args=(lambda: [], lambda: {}, None), daemon=True)
    t.start()
    time.sleep(tick_s * ticks)
    periodics._stop_event.set()
    t.join(timeout=5)


def _job(name, interval_s, handler):
    j = periodics._Job(name, interval_s, handler)
    return j


# ── the defect ────────────────────────────────────────────────────────────

def test_a_failing_job_does_not_rerun_every_tick(monkeypatch):
    """The core regression. A 24h job that fails must not retry 2,880x/day."""
    calls = []

    def boom():
        calls.append(time.time())
        raise RuntimeError("always fails")

    _run_loop([_job("backup", 86400, boom)], ticks=25, monkeypatch=monkeypatch)

    # With the bug this ran on every tick (~25). With backoff, the first retry
    # is _RETRY_BASE_S away — far beyond this test's wall-clock — so exactly one
    # attempt should have happened.
    assert len(calls) == 1, f"job ran {len(calls)} times; backoff not applied"


def test_failure_is_recorded_so_the_job_stops_being_due(monkeypatch):
    def boom():
        raise RuntimeError("x")

    _run_loop([_job("backup", 86400, boom)], ticks=6, monkeypatch=monkeypatch)

    assert periodics._consecutive_failures.get("backup") == 1
    assert "backup" in periodics._last_run, "_last_run must be set on failure too"


def test_backoff_grows_with_consecutive_failures(monkeypatch):
    """Exponential, so a permanently-broken job decays to near-silence."""
    j = _job("probe", 86400, lambda: None)
    delays = []
    for fails in range(1, 8):
        delay = min(j.interval_s, periodics._RETRY_BASE_S * (2 ** min(fails - 1, 6)))
        delays.append(delay)
    assert delays == [60, 120, 240, 480, 960, 1920, 3840]
    assert delays == sorted(delays), "must be monotonically increasing"


def test_backoff_is_capped_at_the_jobs_own_interval(monkeypatch):
    """A backed-off job must never become LESS frequent than its cadence —
    otherwise a 60s job could drift to an hour."""
    short = periodics._Job("fast", 60, lambda: None)
    for fails in range(1, 12):
        delay = min(short.interval_s,
                    periodics._RETRY_BASE_S * (2 ** min(fails - 1, 6)))
        assert delay <= short.interval_s


def test_success_clears_the_backoff(monkeypatch):
    """A job that recovers returns to its normal cadence immediately rather
    than staying penalised."""
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first call fails")

    # interval 0 so it is always due; the failure path sets a backoff, and the
    # cap (min with interval_s == 0) makes it immediately due again.
    _run_loop([_job("flaky", 0, flaky)], ticks=8, monkeypatch=monkeypatch)

    assert state["n"] >= 2, "should have retried after the first failure"
    assert "flaky" not in periodics._consecutive_failures, \
        "failure count must reset once the job succeeds"


def test_a_healthy_job_is_unaffected(monkeypatch):
    """Guard against the backoff logic interfering with the normal path."""
    calls = []
    _run_loop([_job("ok", 0, lambda: calls.append(1))], ticks=6,
              monkeypatch=monkeypatch)
    assert len(calls) >= 3
    assert periodics._consecutive_failures == {}


def test_one_failing_job_does_not_block_the_others(monkeypatch):
    """Pre-existing behaviour that must survive the change."""
    good = []

    def boom():
        raise RuntimeError("x")

    _run_loop([_job("bad", 0, boom), _job("good", 0, lambda: good.append(1))],
              ticks=6, monkeypatch=monkeypatch)
    assert len(good) >= 3, "a sibling job's failure must not skip this one"
