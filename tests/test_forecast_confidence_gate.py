"""A capacity forecast may not state a date it cannot support.

The disk branch of ``forecast_metric`` used to emit ``days_until_full`` for any
positive slope, with no fit-quality gate — while the RAM/CPU leak path beside it
had required R² > 0.55 all along.

Measured on the live fleet (2026-08-06), 22 disk forecasts:

  * R² > 0.4  (12) — swapping the raw last reading for the fitted value moved
    the answer by 0-3%.
  * R² <= 0.4 (10) — the same swap moved it by 45-46%.

That was not a harmless wobble. The two soonest deadlines on the entire fleet,
the ones the Reports page put at the top of its capacity column, had R² = 0.032
and R² = 0.234. "Disk D full in 53 days" was noise with a decimal point on it,
and two runs seconds apart moved one server between risk bands.
"""

from __future__ import annotations

import math

import pytest

from analytics import FORECAST_MIN_R2_FOR_DEADLINE, forecast_metric

HOURS = 720


def _history(values, minutes_apart=60):
    """Readings one hour apart, oldest first, as get_metric_stats returns them.

    Every column carries the same series so a test can point any `metric` at it.
    An earlier version pinned cpu/ram to a constant and varied only the disks —
    which made the RAM tests assert against a flat line, so "no leak detected"
    passed for a reason that had nothing to do with leak detection.
    """
    return [
        {"timestamp": f"2026-07-{1 + (i * minutes_apart) // 1440:02d}T"
                      f"{((i * minutes_apart) // 60) % 24:02d}:"
                      f"{(i * minutes_apart) % 60:02d}:00Z",
         "cpu_percent": v, "ram_percent": v,
         "disk_c_percent": v, "disk_d_percent": v}
        for i, v in enumerate(values)
    ]


def _forecast(values, metric="disk_c", target=90.0):
    return forecast_metric(None, "SRV", metric=metric, hours=HOURS,
                           target_percent=target, history=_history(values))


def test_clean_upward_trend_still_gets_a_deadline():
    """The gate must not silence the forecasts that were always trustworthy."""
    values = [50.0 + i * 0.05 for i in range(200)]      # tidy line, R² ~ 1

    result = _forecast(values)

    assert result["kind"] == "growth"
    assert result["confidence"] == "high"
    assert result["days_until_full"] is not None
    assert result["days_until_full"] > 0


def test_noisy_flat_disk_reports_a_trend_but_no_deadline():
    """A sawtooth with a faint upward bias: direction is arguable, a date is not.

    This is the shape that topped the live capacity column — R² of 0.032, yet
    the old code divided the remaining headroom by that slope and printed the
    result to one decimal.
    """
    values = [70.0 + (8.0 if i % 2 else -8.0) + i * 0.001 for i in range(200)]

    result = _forecast(values)

    assert result["confidence"] == "low"
    assert result["days_until_full"] is None, "a date must not survive a bad fit"
    assert result["trend_per_day"] is not None, "the direction is still reported"


def test_the_gate_is_the_documented_threshold():
    assert FORECAST_MIN_R2_FOR_DEADLINE == 0.4


def test_deadline_uses_the_fitted_position_not_the_last_reading():
    """Mixing a modelled rate with one raw sample caused most of the swing.

    Same underlying trend, but the final reading is a 6-point spike. The
    deadline must barely move, because the line has not moved.
    """
    base = [60.0 + i * 0.05 for i in range(200)]
    spiked = list(base)
    spiked[-1] = base[-1] + 6.0

    clean = _forecast(base)["days_until_full"]
    noisy = _forecast(spiked)["days_until_full"]

    assert clean is not None and noisy is not None
    drift = abs(clean - noisy) / clean * 100
    assert drift < 10, (
        f"one outlying sample moved the deadline by {drift:.0f}% "
        f"({clean} -> {noisy}); the fitted value should absorb it")


def test_a_falling_disk_gets_no_deadline():
    values = [80.0 - i * 0.05 for i in range(200)]
    assert _forecast(values)["days_until_full"] is None


def test_disk_already_past_target_reports_zero_days():
    """Being over the line is a fact about the present, not a forecast."""
    values = [88.0 + i * 0.05 for i in range(200)]

    result = _forecast(values, target=90.0)

    assert result["days_until_full"] == 0


def test_insufficient_history_yields_no_forecast():
    result = _forecast([50.0, 51.0, 52.0])
    assert result["enough_data"] is False
    assert result["days_until_full"] is None


@pytest.mark.parametrize("metric", ["disk_c", "disk_d"])
def test_both_disks_are_gated(metric):
    values = [70.0 + (8.0 if i % 2 else -8.0) + i * 0.001 for i in range(200)]
    assert _forecast(values, metric=metric)["days_until_full"] is None


def test_stationary_ram_never_produced_a_deadline_and_still_does_not():
    """Regression guard on the branch that was already correct."""
    values = [60.0 + (3.0 if i % 3 else -3.0) for i in range(200)]

    result = _forecast(values, metric="ram")

    assert result["kind"] == "stationary"
    assert result["days_until_full"] is None


def test_a_real_ram_leak_still_forecasts():
    """The leak path must keep working — the change there was the numerator
    only, and it already gated on R² > 0.55."""
    values = [40.0 + i * 0.06 for i in range(300)]      # ~1.4%/day, clean

    result = _forecast(values, metric="ram")

    assert result["kind"] == "leak"
    assert result["days_until_full"] is not None
    assert math.isfinite(result["days_until_full"])
