"""Tests for the scheduled baseline-recalculation periodic job.

settings.baseline_detection.recalc_hour was saved by the Monitoring page UI
but read by nothing — baseline_engine.nightly_baseline_job() was only
reachable via the manual POST /api/baselines/recalculate button. This file
locks down collector_v2.periodics.run_baseline_recalc_if_due(), which wires
the "nightly" job to an actual daily schedule:

  * once per LOCAL calendar day (settings.timezone), at/after
    settings.baseline_detection.recalc_hour
  * "already ran today" is derived from the DB (metric_baselines.updated_at),
    so it's correct across process restarts within the same day
  * a one-shot startup catch-up when baselines are stale (>=25h) or the
    table is empty but metric history exists
  * skipped entirely when baseline_detection.enabled is false

Uses the ``tmp_db`` fixture from tests/conftest.py (file-backed SQLite in a
tmp dir) — no config.json involved, so this runs on the Linux CI image too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from collector_v2 import periodics


@pytest.fixture(autouse=True)
def _reset_module_state():
    """run_baseline_recalc_if_due() keeps a one-shot "did we evaluate startup
    catch-up yet" flag at module scope (matches the in-memory convention the
    rest of periodics.py uses, e.g. _last_heartbeat). Reset it — and the
    log-once flag — before every test so tests don't leak state into each
    other."""
    periodics._baseline_recalc_startup_checked = False
    periodics._baseline_recalc_bad_hour_logged = False
    periodics._baseline_recalc_last_run_date = None
    yield
    periodics._baseline_recalc_startup_checked = False
    periodics._baseline_recalc_bad_hour_logged = False
    periodics._baseline_recalc_last_run_date = None


def _servers(*names):
    """run_baseline_recalc_if_due() takes get_servers as a zero-arg callable
    (matching baseline_engine.nightly_baseline_job's own contract), not a
    list — mirror that here rather than passing a list directly."""
    server_list = [SimpleNamespace(name=n) for n in names]
    return MagicMock(return_value=server_list)


def _settings(**baseline_overrides):
    cfg = {"enabled": True, "recalc_hour": "02:00", "history_weeks": 4}
    cfg.update(baseline_overrides)
    return {"timezone": "UTC", "baseline_detection": cfg}


def _patch_job(monkeypatch, return_value=3):
    """Patch baseline_engine.nightly_baseline_job (imported lazily inside
    run_baseline_recalc_if_due, so patching the source module works) and
    analytics.clear_baseline_cache, returning both mocks."""
    job_mock = MagicMock(return_value=return_value)
    cache_mock = MagicMock(return_value=0)
    monkeypatch.setattr("baseline_engine.nightly_baseline_job", job_mock)
    monkeypatch.setattr("analytics.clear_baseline_cache", cache_mock)
    return job_mock, cache_mock


# ── registration ──────────────────────────────────────────────────────────

def test_baseline_recalc_job_registered():
    get_servers = MagicMock(return_value=[])
    get_settings = MagicMock(return_value={"poll_interval_seconds": 60})
    db = MagicMock()
    jobs = periodics._build_jobs(get_servers, get_settings, db)
    names = {j.name for j in jobs}
    assert "baseline_recalc" in names


# ── runs at/after recalc_hour, not yet run today ─────────────────────────

def test_runs_when_local_time_passes_recalc_hour(tmp_db, monkeypatch):
    job_mock, cache_mock = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="02:00")
    now = datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc)  # UTC == local (tz=UTC)

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1", "srv2"), settings, now_utc=now,
    )

    assert ran is True
    job_mock.assert_called_once()
    cache_mock.assert_called_once()
    # Audit trail mirrors the manual /api/baselines/recalculate endpoint.
    audit = tmp_db.get_audit_log(limit=5)
    assert any(row.get("action") == "recalculate_baselines" for row in audit)


def test_does_not_run_before_recalc_hour(tmp_db, monkeypatch):
    job_mock, _ = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="02:00")
    now = datetime(2026, 7, 14, 1, 0, 0, tzinfo=timezone.utc)  # before 02:00, no stale data

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=now,
    )

    assert ran is False
    job_mock.assert_not_called()


# ── once per day ──────────────────────────────────────────────────────────

def test_does_not_run_twice_same_day(tmp_db, monkeypatch):
    job_mock, _ = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="02:00")

    first = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc),
    )
    second = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 14, 20, 0, 0, tzinfo=timezone.utc),
    )

    assert first is True
    assert second is False
    job_mock.assert_called_once()


def test_runs_again_next_day(tmp_db, monkeypatch):
    """Sanity check on the other side of the "once per day" gate — a new
    local calendar day must be eligible again."""
    job_mock, _ = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="02:00")

    day1 = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc),
    )
    day2 = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 15, 3, 0, 0, tzinfo=timezone.utc),
    )

    assert day1 is True
    assert day2 is True
    assert job_mock.call_count == 2


# ── disabled ──────────────────────────────────────────────────────────────

def test_skips_when_disabled(tmp_db, monkeypatch):
    job_mock, cache_mock = _patch_job(monkeypatch)
    settings = _settings(enabled=False, recalc_hour="02:00")
    now = datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc)  # well past recalc_hour

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=now,
    )

    assert ran is False
    job_mock.assert_not_called()
    cache_mock.assert_not_called()


# ── startup catch-up ───────────────────────────────────────────────────────

def test_startup_catchup_when_stale(tmp_db, monkeypatch):
    """Baselines exist but are >=25h old — catch up immediately rather than
    waiting for the next recalc_hour, even before recalc_hour today."""
    job_mock, _ = _patch_job(monkeypatch)
    now = datetime(2026, 7, 14, 0, 30, 0, tzinfo=timezone.utc)  # before recalc_hour
    tmp_db.upsert_baseline("srv1", "cpu", 0, 50.0, 5.0, 20)
    # Backdate relative to the SAME fixed `now` the assertion uses below —
    # not real wall-clock time — so the 30h staleness is exact regardless
    # of when the test suite actually runs.
    stale_ts = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = tmp_db._get_conn()
    conn.execute("UPDATE metric_baselines SET updated_at=?", (stale_ts,))
    conn.commit()
    conn.close()

    settings = _settings(recalc_hour="02:00")

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=now,
    )

    assert ran is True
    job_mock.assert_called_once()


def test_startup_catchup_when_empty_but_history_exists(tmp_db, monkeypatch):
    """metric_baselines is empty (never computed) but there IS metric
    history to build from — catch up immediately."""
    job_mock, _ = _patch_job(monkeypatch)
    tmp_db.insert_metric("srv1", 50.0, 40.0, 30.0, 20.0, "online")

    settings = _settings(recalc_hour="02:00")
    now = datetime(2026, 7, 14, 0, 30, 0, tzinfo=timezone.utc)  # before recalc_hour

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=now,
    )

    assert ran is True
    job_mock.assert_called_once()


def test_no_catchup_when_empty_and_no_history(tmp_db, monkeypatch):
    """Nothing collected yet at all — don't fire early; wait for recalc_hour
    like a normal day."""
    job_mock, _ = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="02:00")
    now = datetime(2026, 7, 14, 0, 30, 0, tzinfo=timezone.utc)  # before recalc_hour

    ran = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=now,
    )

    assert ran is False
    job_mock.assert_not_called()


def test_startup_catchup_only_fires_once_per_process(tmp_db, monkeypatch):
    """Even if metric_baselines stays empty after a run (e.g. not enough
    samples per hour-of-week slot yet), the catch-up check must not
    retrigger on every tick — it's a one-shot per process lifetime."""
    job_mock, _ = _patch_job(monkeypatch, return_value=0)  # simulate 0 slots written
    tmp_db.insert_metric("srv1", 50.0, 40.0, 30.0, 20.0, "online")
    settings = _settings(recalc_hour="02:00")
    before_hour = datetime(2026, 7, 14, 0, 30, 0, tzinfo=timezone.utc)

    first = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings, now_utc=before_hour,
    )
    second = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=before_hour + timedelta(minutes=5),
    )

    assert first is True
    assert second is False
    job_mock.assert_called_once()


# ── malformed recalc_hour ──────────────────────────────────────────────────

def test_malformed_recalc_hour_falls_back_to_0200(tmp_db, monkeypatch):
    job_mock, _ = _patch_job(monkeypatch)
    settings = _settings(recalc_hour="not-a-time")

    # 01:00 — before the 02:00 fallback — must NOT run.
    ran_early = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 14, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert ran_early is False
    job_mock.assert_not_called()

    # 03:00 — after the 02:00 fallback — must run.
    ran_late = periodics.run_baseline_recalc_if_due(
        tmp_db, _servers("srv1"), settings,
        now_utc=datetime(2026, 7, 14, 3, 0, 0, tzinfo=timezone.utc),
    )
    assert ran_late is True
    job_mock.assert_called_once()
