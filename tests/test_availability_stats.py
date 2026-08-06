"""Availability / health accounting (analytics.compute_uptime_stats).

Rewritten 2026-08-05 because the previous formula produced results
anti-correlated with reality on live data:

  * one server reported 94.79% uptime with ZERO healthy readings out of 2041
  * another reported 42.47% while 98.6% of readings were healthy
  * correcting 1,624 rows of bad data made a server's reported uptime 16 points
    WORSE while its healthy readings went up — the output was not monotonic in
    the health of its own input

Root causes, each pinned by a test below:
  1. `status != "healthy"` counted warning/critical as DOWNTIME, conflating
     availability with health.
  2. `unknown` counted as downtime, so missing data manufactured outages.
  3. An ongoing outage was extrapolated to `now` while the window was measured
     to the last reading, so downtime could exceed the window.
  4. Gap-breaking split single outages into many, inflating outage_count.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from analytics import (
    classify_availability,
    compute_fleet_availability,
    compute_uptime_stats,
)

INTERVAL = 60  # seconds


def _db(statuses, start_min=0):
    """Timeline of readings one minute apart, in order."""
    rows = [
        {"timestamp": f"2026-07-01T{(start_min + i) // 60:02d}:{(start_min + i) % 60:02d}:00Z",
         "status": s}
        for i, s in enumerate(statuses)
    ]
    db = MagicMock()
    db.get_status_timeline.return_value = rows
    return db


def _stats(statuses):
    return compute_uptime_stats(_db(statuses), "srv", hours=720,
                                poll_interval_seconds=INTERVAL)


# ── classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    ("healthy", "up"),
    ("warning", "up"),        # degraded, NOT down — this is defect #1
    ("critical", "up"),       # degraded, NOT down
    ("offline", "down"),
    ("unreachable", "down"),
    ("unknown", "excluded"),  # no measurement, not a measurement of failure
    ("queued", "excluded"),   # operator-initiated = planned
    ("updating", "excluded"),
    ("restarting", "excluded"),
])
def test_status_classification(status, expected):
    assert classify_availability(status) == expected


def test_unrecognised_status_is_excluded_never_down():
    """A new status value must not silently manufacture an outage."""
    assert classify_availability("some_future_state") == "excluded"
    assert classify_availability(None) == "excluded"


# ── defect #1: threshold breaches are not outages ─────────────────────────

def test_permanently_warning_server_is_fully_available_but_zero_health():
    """The FILE01 case. Old formula said 94.79% uptime with 0 healthy
    readings, which is self-contradictory. Correct answer is two numbers."""
    st = _stats(["warning"] * 100)
    assert st["availability_percent"] == 100.0, "always reachable"
    assert st["health_percent"] == 0.0, "never within thresholds"
    assert st["down_minutes"] == 0.0
    assert st["outage_count"] == 0
    assert st["degraded_minutes"] == 100.0


def test_permanently_critical_server_is_still_available():
    st = _stats(["critical"] * 50)
    assert st["availability_percent"] == 100.0
    assert st["health_percent"] == 0.0


def test_mixed_healthy_and_degraded_splits_the_two_numbers():
    st = _stats(["healthy"] * 75 + ["warning"] * 25)
    assert st["availability_percent"] == 100.0
    assert st["health_percent"] == 75.0


# ── defect #2: unknown is excluded, not down ──────────────────────────────

def test_unknown_readings_are_excluded_from_the_denominator():
    st = _stats(["healthy"] * 50 + ["unknown"] * 50)
    assert st["availability_percent"] == 100.0, "unknown must not be downtime"
    assert st["excluded_readings"] == 50
    assert st["observed_minutes"] == 50.0
    assert st["down_minutes"] == 0.0


def test_all_unknown_reports_no_data_not_a_percentage():
    """No data is neither 100% nor 0%. Reporting either is a lie."""
    st = _stats(["unknown"] * 30)
    assert st["has_data"] is False
    assert st["availability_percent"] is None
    assert st["uptime_percent"] is None


def test_planned_states_are_excluded():
    st = _stats(["healthy"] * 10 + ["restarting"] * 5 + ["healthy"] * 10)
    assert st["availability_percent"] == 100.0
    assert st["excluded_readings"] == 5
    assert st["outage_count"] == 0


# ── defect #3: result cannot leave 0-100 ──────────────────────────────────

def test_ongoing_outage_at_end_of_window_stays_in_range():
    st = _stats(["healthy"] * 50 + ["offline"] * 50)
    assert st["availability_percent"] == 50.0
    assert 0.0 <= st["availability_percent"] <= 100.0
    assert st["outage_count"] == 1
    assert st["outages"][0]["ongoing"] is True
    assert st["down_minutes"] == 50.0


def test_downtime_can_never_exceed_the_observed_window():
    st = _stats(["offline"] * 100)
    assert st["availability_percent"] == 0.0
    assert st["down_minutes"] == 100.0
    assert st["observed_minutes"] == 100.0
    assert st["down_minutes"] <= st["observed_minutes"]


def test_health_is_none_when_never_reachable():
    st = _stats(["offline"] * 20)
    assert st["availability_percent"] == 0.0
    assert st["health_percent"] is None, "no reachable time to be healthy in"


# ── defect #4: an excluded gap must not split one outage into two ─────────

def test_excluded_reading_inside_an_outage_does_not_split_it():
    """A collector hiccup mid-outage is not two outages. The old gap-breaking
    logic inflated outage_count exactly this way (135 outages on one server)."""
    st = _stats(["healthy"] * 5 + ["offline"] * 10 + ["unknown"] * 3
                + ["offline"] * 10 + ["healthy"] * 5)
    assert st["outage_count"] == 1, "one contiguous outage, interrupted by no-data"
    assert st["down_minutes"] == 20.0
    assert st["excluded_readings"] == 3


def test_recovery_closes_an_outage_and_a_second_one_counts_separately():
    st = _stats(["healthy"] * 5 + ["offline"] * 5 + ["healthy"] * 5
                + ["offline"] * 5 + ["healthy"] * 5)
    assert st["outage_count"] == 2
    assert st["down_minutes"] == 10.0
    assert all(not o["ongoing"] for o in st["outages"])


def test_mttr_uses_resolved_outages_only():
    st = _stats(["healthy"] + ["offline"] * 4 + ["healthy"]      # 4 min, resolved
                + ["offline"] * 10)                              # ongoing
    assert st["outage_count"] == 2
    assert st["mttr_minutes"] == 4.0, "ongoing outage must not drag MTTR down"


def test_worst_severity_is_recorded_per_outage():
    st = _stats(["healthy"] + ["unreachable"] * 2 + ["offline"] * 2 + ["healthy"])
    assert st["outage_count"] == 1
    assert st["outages"][0]["worst_severity"] == "offline"


# ── edge cases ────────────────────────────────────────────────────────────

def test_empty_timeline_reports_no_data():
    st = compute_uptime_stats(_db([]), "srv", poll_interval_seconds=INTERVAL)
    assert st["has_data"] is False
    assert st["availability_percent"] is None
    assert st["total_readings"] == 0


def test_single_reading_is_usable():
    st = _stats(["healthy"])
    assert st["has_data"] is True
    assert st["availability_percent"] == 100.0
    assert st["observed_minutes"] == 1.0


def test_uptime_percent_is_an_alias_for_availability():
    """reports.html reads uptime_percent; it must not break."""
    st = _stats(["healthy"] * 9 + ["offline"])
    assert st["uptime_percent"] == st["availability_percent"] == 90.0
    # keys the existing SLA card depends on
    for k in ("total_readings", "outage_count", "total_downtime_minutes",
              "mttr_minutes"):
        assert k in st


def test_poll_interval_scales_the_minute_accounting():
    db = _db(["offline"] * 10)
    at_60 = compute_uptime_stats(db, "s", poll_interval_seconds=60)
    at_300 = compute_uptime_stats(db, "s", poll_interval_seconds=300)
    assert at_60["down_minutes"] == 10.0
    assert at_300["down_minutes"] == 50.0
    # the PERCENTAGE must be interval-independent
    assert at_60["availability_percent"] == at_300["availability_percent"] == 0.0


# ── fleet aggregation ─────────────────────────────────────────────────────

def _summ(**servers):
    return {n: _stats(sts) for n, sts in servers.items()}


def test_fleet_is_time_weighted_not_a_naive_mean():
    """A server observed briefly must not count the same as one observed long.
    Here: 1000 healthy minutes vs 10 offline minutes. The naive mean says 50%;
    the truth is ~99%."""
    summ = _summ(big=["healthy"] * 1000, tiny=["offline"] * 10)
    f = compute_fleet_availability(summ)
    assert f["fleet_availability_percent"] == pytest.approx(99.01, abs=0.05)
    assert f["mean_server_availability_percent"] == 50.0
    assert f["fleet_availability_percent"] > f["mean_server_availability_percent"]


def test_fleet_excludes_no_data_servers_from_both_figures():
    summ = _summ(good=["healthy"] * 100, nodata=["unknown"] * 100)
    f = compute_fleet_availability(summ)
    assert f["servers_counted"] == 1
    assert f["servers_no_data"] == 1
    assert f["fleet_availability_percent"] == 100.0
    assert f["mean_server_availability_percent"] == 100.0


def test_fleet_reports_the_worst_server():
    summ = _summ(a=["healthy"] * 100,
                 b=["healthy"] * 50 + ["offline"] * 50,
                 c=["healthy"] * 90 + ["offline"] * 10)
    f = compute_fleet_availability(summ)
    assert f["worst_server"] == "b"
    assert f["worst_availability_percent"] == 50.0


def test_fleet_health_is_separate_from_fleet_availability():
    summ = _summ(a=["warning"] * 100, b=["healthy"] * 100)
    f = compute_fleet_availability(summ)
    assert f["fleet_availability_percent"] == 100.0, "both always reachable"
    assert f["fleet_health_percent"] == 50.0, "half the reachable time was degraded"


def test_empty_fleet_is_none_not_zero():
    f = compute_fleet_availability({})
    assert f["fleet_availability_percent"] is None
    assert f["servers_counted"] == 0
