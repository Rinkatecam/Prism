"""Tests for the CPU stationary forecast card (analytics.forecast_metric).

Owner request: the server overview should show for CPU what it already showed
for RAM — the range / avg / now band that says how the box NORMALLY runs, not a
fake "days until full" projection. CPU is not a resource that fills, so a linear
forecast is as meaningless for it as it is for RAM.

This matters more since the spike gate: a CPU breach now has to hold 5 collector
rounds before it alarms, so the card is where an operator goes to answer "was
that 80% an outlier or is that just Tuesday?".

Test plan:
  1. CPU takes the stationary branch and reports range/avg/now.
  2. elevated_but_stable fires when the average sits at/above this server's
     cpu_warning, and not when it sits below. Boundary (avg == warning) included
     because the production code uses >=.
  3. A genuinely climbing CPU baseline still reports kind='leak' (the template
     renders that as "Sustained CPU climb", not "Memory leak suspected").
  4. A spiky-but-flat CPU is NOT a climb — this is the case the whole feature
     exists for, and the one a naive regression gets wrong.
  5. warning_threshold is metric-agnostic, and the old ram_warning kwarg still
     works so existing RAM callers are unaffected.
  6. get_server_analytics exposes forecasts['cpu'] and feeds it the CPU
     thresholds, not the RAM ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import analytics


def _rows(cpu_values, step_hours: float = 1.0):
    """History rows with the given CPU series. Hourly spacing by default so
    trend-per-day is a realistic number rather than an artefact of cramming
    24 samples into 20 minutes."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return [
        {
            "timestamp": (base + timedelta(hours=step_hours * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cpu_percent": v,
            "ram_percent": 50.0,
            "disk_c_percent": 40.0,
            "disk_d_percent": -1,
        }
        for i, v in enumerate(cpu_values)
    ]


def _db(rows):
    db = MagicMock()
    db.get_metric_stats.return_value = rows
    return db


# ── 1. CPU takes the stationary branch ────────────────────────────────────

def test_cpu_reports_stationary_with_range_avg_and_current():
    # 48 samples oscillating 10..30, no drift
    values = [20 + ((i % 5) - 2) * 5 for i in range(48)]
    fc = analytics.forecast_metric(_db(_rows(values)), "srv1", metric="cpu")

    assert fc["enough_data"] is True
    assert fc["kind"] == "stationary"
    assert fc["range_min"] == 10.0
    assert fc["range_max"] == 30.0
    assert fc["range_avg"] == round(sum(values) / len(values), 1)
    assert fc["current"] == float(values[-1])
    # A stationary metric must NOT claim a days-until projection.
    assert fc["days_until_full"] is None
    assert fc["forecast_7d"] is None


def test_cpu_below_min_readings_reports_not_enough_data():
    fc = analytics.forecast_metric(_db(_rows([20.0] * 5)), "srv1", metric="cpu")
    assert fc["enough_data"] is False
    assert fc["kind"] is None


# ── 2. elevated_but_stable, including the >= boundary ─────────────────────

def test_cpu_elevated_but_stable_when_avg_at_or_above_warning():
    """A DC whose cpu_warning is 40 and which averages 45 is 'elevated but
    stable' — the same shape of fact as the RAM/SQL01 case."""
    fc = analytics.forecast_metric(_db(_rows([45.0] * 48)), "dc1",
                                   metric="cpu", warning_threshold=40)
    assert fc["kind"] == "stationary"
    assert fc["elevated_but_stable"] is True


def test_cpu_not_elevated_when_avg_below_warning():
    fc = analytics.forecast_metric(_db(_rows([18.0] * 48)), "dc1",
                                   metric="cpu", warning_threshold=40)
    assert fc["kind"] == "stationary"
    assert fc["elevated_but_stable"] is False


def test_cpu_elevated_exactly_at_warning_boundary():
    """Production uses >=, so avg exactly on the threshold must flag."""
    fc = analytics.forecast_metric(_db(_rows([40.0] * 48)), "dc1",
                                   metric="cpu", warning_threshold=40)
    assert fc["range_avg"] == 40.0
    assert fc["elevated_but_stable"] is True


def test_cpu_without_warning_threshold_expresses_no_opinion():
    """Omitting the threshold preserves the pre-existing behaviour: report the
    band, don't editorialise about it."""
    fc = analytics.forecast_metric(_db(_rows([95.0] * 48)), "dc1", metric="cpu")
    assert fc["kind"] == "stationary"
    assert fc["elevated_but_stable"] is False


# ── 3. A real climb is still reported ─────────────────────────────────────

def test_cpu_sustained_climb_reports_leak_kind():
    """Steady rise 20 -> 68 over 48h: trend > 0.5%/day, high R^2, and the
    second half's mean clears the first half's by >= 2. That IS worth
    surfacing, and the template titles it 'Sustained CPU climb'."""
    values = [20.0 + i for i in range(48)]
    fc = analytics.forecast_metric(_db(_rows(values)), "srv1", metric="cpu",
                                   target_percent=85, warning_threshold=75)
    assert fc["kind"] == "leak"
    assert fc["trend_per_day"] > 0.5
    assert fc["days_until_full"] is not None
    assert fc["forecast_7d"] is not None


# ── 4. The case the feature exists for ───────────────────────────────────

def test_cpu_spiky_but_flat_is_not_a_climb():
    """The FILE01 shape: CPU swings the full 0..100 range with no drift.
    Regression alone can be fooled by where the spikes land; the sub-window
    check is what keeps this 'stationary'. If this ever flips to 'leak', every
    bursty file server starts crying wolf."""
    values = [0.0, 100.0, 5.0, 95.0, 10.0, 90.0] * 8  # 48 samples, mean 50
    fc = analytics.forecast_metric(_db(_rows(values)), "fil10", metric="cpu",
                                   warning_threshold=75)
    assert fc["kind"] == "stationary"
    assert fc["range_min"] == 0.0
    assert fc["range_max"] == 100.0
    # mean is 50 -> below a 75 warning, so no amber headline either
    assert fc["elevated_but_stable"] is False


# ── 5. Threshold plumbing is metric-agnostic and backward compatible ──────

def test_ram_warning_kwarg_still_honoured_for_ram():
    """Existing RAM callers pass ram_warning=. That must keep working."""
    rows = [
        {"timestamp": r["timestamp"], "cpu_percent": 5.0, "ram_percent": 93.0,
         "disk_c_percent": 40.0, "disk_d_percent": -1}
        for r in _rows([5.0] * 48)
    ]
    fc = analytics.forecast_metric(_db(rows), "sql01", metric="ram",
                                   ram_warning=80)
    assert fc["kind"] == "stationary"
    assert fc["elevated_but_stable"] is True


def test_warning_threshold_takes_precedence_over_ram_warning_alias():
    fc = analytics.forecast_metric(_db(_rows([45.0] * 48)), "dc1", metric="cpu",
                                   ram_warning=90, warning_threshold=40)
    assert fc["elevated_but_stable"] is True, "warning_threshold=40 should win"

    fc2 = analytics.forecast_metric(_db(_rows([45.0] * 48)), "dc1", metric="cpu",
                                    ram_warning=40, warning_threshold=90)
    assert fc2["elevated_but_stable"] is False, "warning_threshold=90 should win"


def test_disk_is_unaffected_and_keeps_linear_growth():
    """Guard against the stationary branch swallowing disks."""
    rows = [
        {"timestamp": r["timestamp"], "cpu_percent": 5.0, "ram_percent": 50.0,
         "disk_c_percent": 40.0 + i * 0.1, "disk_d_percent": -1}
        for i, r in enumerate(_rows([5.0] * 48))
    ]
    fc = analytics.forecast_metric(_db(rows), "srv1", metric="disk_c")
    assert fc["kind"] == "growth"
    assert fc["days_until_full"] is not None


# ── 6. get_server_analytics wiring ───────────────────────────────────────

def test_get_server_analytics_exposes_cpu_forecast():
    rows = _rows([45.0] * 48)
    db = _db(rows)
    db.get_latest_by_server.return_value = {
        "cpu_percent": 45.0, "ram_percent": 50.0,
        "disk_c_percent": 40.0, "disk_d_percent": -1,
    }
    db.get_active_acknowledgments.return_value = []

    out = analytics.get_server_analytics(db, "dc1", server_type="domain_controller")

    assert "cpu" in out["forecasts"], "CPU forecast must be present"
    cpu = out["forecasts"]["cpu"]
    assert cpu["kind"] == "stationary"
    # domain_controller defaults are cpu_warning=40 / cpu_critical=60, so an
    # average of 45 must read elevated. If this ever picks up the RAM
    # thresholds (80/90) instead, it would silently read "Normal usage".
    assert cpu["elevated_but_stable"] is True


def test_get_server_analytics_cpu_uses_per_server_threshold_overrides():
    """thresholds= from ServerConfig must beat the role defaults."""
    db = _db(_rows([45.0] * 48))
    db.get_latest_by_server.return_value = {
        "cpu_percent": 45.0, "ram_percent": 50.0,
        "disk_c_percent": 40.0, "disk_d_percent": -1,
    }
    db.get_active_acknowledgments.return_value = []

    out = analytics.get_server_analytics(
        db, "dc1", server_type="domain_controller",
        thresholds={"cpu_warning": 70, "cpu_critical": 90,
                    "ram_warning": 80, "ram_critical": 90},
    )
    # 45 avg against an overridden 70 warning is NOT elevated, even though the
    # domain_controller role default of 40 would have flagged it.
    assert out["forecasts"]["cpu"]["elevated_but_stable"] is False
