"""Tests for ``collector_v2.periodics._build_jobs`` cadence translation.

The interesting behaviour to lock down here is the v1→v2 translation
of cycle-based settings (``tls_monitoring.check_interval_cycles`` and
``drift_detection.check_interval_cycles``). Operators with existing
settings.json edits must continue to get the cadence they configured
under v1, even though the v2 periodics thread has no concept of cycle.

Why these knobs aren't UI-exposed and we still test them: they're
"power user" settings — edited by hand. The only contract we owe is
that the value they put in keeps producing the same effective cadence
across an engine flip. Without these tests, a future refactor of
``_build_jobs`` could silently change that without anyone noticing
until a TLS cert expired unexpectedly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from collector_v2 import periodics


def _build_jobs_with(settings: dict) -> dict:
    """Helper: call _build_jobs with stub callables, return job-name → interval."""
    get_servers = MagicMock(return_value=[])
    get_settings = MagicMock(return_value=settings)
    db = MagicMock()
    jobs = periodics._build_jobs(get_servers, get_settings, db)
    return {j.name: j.interval_s for j in jobs}


def test_default_cadences_when_settings_silent():
    """No tls/drift cycles configured → 1h defaults (v2 baseline)."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    assert intervals["tls_certs"] == 3600
    assert intervals["drift"] == 3600


def test_tls_cycles_translate_to_seconds():
    """v1 operator set tls_monitoring.check_interval_cycles=30, poll=60s.
    Expected v2 cadence: 30 * 60 = 1800s (same as v1's effective cadence)."""
    intervals = _build_jobs_with({
        "poll_interval_seconds": 60,
        "tls_monitoring": {"check_interval_cycles": 30},
    })
    assert intervals["tls_certs"] == 1800


def test_drift_cycles_translate_to_seconds():
    intervals = _build_jobs_with({
        "poll_interval_seconds": 60,
        "drift_detection": {"check_interval_cycles": 60},
    })
    assert intervals["drift"] == 3600


def test_translation_respects_non_default_poll_interval():
    """If poll_interval is 30s, the cycle multiplier follows it.
    Operator set cycles=20 expecting 600s (10min) under v1's 30s poll —
    v2 should produce the same."""
    intervals = _build_jobs_with({
        "poll_interval_seconds": 30,
        "tls_monitoring": {"check_interval_cycles": 20},
    })
    assert intervals["tls_certs"] == 600


def test_minimum_floor_60s():
    """Sub-minute cadences are clamped — the periodics tick is 30s and
    anything faster than 60s is meaningless."""
    intervals = _build_jobs_with({
        "poll_interval_seconds": 5,
        "tls_monitoring": {"check_interval_cycles": 1},  # 5s — too fast
    })
    assert intervals["tls_certs"] == 60


def test_maximum_cap_24h():
    """An absurd setting (operator typo) gets clamped to 24h to avoid
    multi-day silence. This is a safety net, not a meaningful policy."""
    intervals = _build_jobs_with({
        "poll_interval_seconds": 60,
        "drift_detection": {"check_interval_cycles": 100000},
    })
    assert intervals["drift"] == 86400


def test_garbage_value_falls_back_to_default():
    """A non-numeric value shouldn't crash _build_jobs; it should log
    and use the default. Defensive: settings.json could be hand-edited."""
    intervals = _build_jobs_with({
        "poll_interval_seconds": 60,
        "tls_monitoring": {"check_interval_cycles": "thirty"},
    })
    assert intervals["tls_certs"] == 3600


def test_all_expected_jobs_present():
    """Regression: removing a periodic job is a silent and serious
    bug — test certs would stop, retention would stop, etc. Pin the
    full set so any accidental deletion fails CI."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    expected = {
        "scheduled_reports", "reboot_state_janitor", "auto_restart_scanner",
        "ldap_probe", "health_checks", "failed_logins", "tls_certs",
        "drift", "security_status", "retention",
        # F-AT-1 (CSV-12 / 17 remediation): hourly audit-chain integrity
        # verifier registered as a periodic job.
        "audit_chain_verifier",
        # Feature 1.8: scheduled online DB backup with rotation + freshness.
        "database_backup",
        # Nightly baseline recalculation, now actually wired to
        # settings.baseline_detection.recalc_hour (see
        # run_baseline_recalc_if_due).
        "baseline_recalc",
    }
    assert set(intervals.keys()) == expected


def test_database_backup_job_registered():
    """Feature 1.8: the 12th periodic is registered at 24h by default, floored
    to a 1h minimum so a misconfigured interval can't hammer the DB."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    assert intervals["database_backup"] == 86400


def test_database_backup_interval_floored():
    intervals = _build_jobs_with({"database_backup": {"interval_hours": 0}})
    assert intervals["database_backup"] == 3600


def test_reboot_state_janitor_runs_every_minute():
    """The janitor's job is to clear stuck ``rebooting`` install_state
    rows after 20 min. It needs to tick often enough that a stuck
    badge isn't visible for a noticeable extra delay past the timeout.
    Once per minute is the right tradeoff (cheap + responsive)."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    assert intervals["reboot_state_janitor"] == 60


def test_auto_restart_scanner_runs_every_minute():
    """Safety-net for the per-install watcher thread that fires the
    actual auto-restart. The scanner picks up any pending restart
    within 60 s if the watcher died (Flask restart, exception, etc.).
    Cadence must be ≤ a minute or operators notice the lag."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    assert intervals["auto_restart_scanner"] == 60


def test_baseline_recalc_checked_every_minute():
    """The 60s cadence only gates how often we *check* whether the daily
    recalc is due — the actual once-per-day gating lives in
    run_baseline_recalc_if_due(). See test_baseline_recalc_job.py for that
    behaviour."""
    intervals = _build_jobs_with({"poll_interval_seconds": 60})
    assert intervals["baseline_recalc"] == 60
