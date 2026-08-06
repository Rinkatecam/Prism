"""Layer-3 deviation-from-self RAISE gates.

docs/plans/ALERT_NOISE_AND_VERDICT_UX_PLAN.md §3. Measured on a live 30-server
fleet, the ungated Layer 3 produced 9 warnings where only 4 had a real threshold
breach. Three defects in one condition (`deviating and dev_sustained and name not
in acked`), each now its own configurable gate:

  A1 direction  — a metric BELOW its baseline raised an amber badge. A disk with
                  more free space than usual is good news.
  A2 proximity  — RAM at 38% warned because it is "usually 22%", though its
                  warning threshold is 80%. Contradicted the adopted principle
                  that anomaly alone never pages (DETECTION_FUSION_PLAN §1).
  A3 authority  — raising required no baseline maturity while DOWNGRADING did.
                  A stale baseline could invent alerts but not clear them.

Legacy behaviour must remain exactly reproducible from config, so the fix is
reversible from the Monitoring page without a code change.
"""

from __future__ import annotations

import pytest

from detection import _DEV_GATE_DEFAULTS, _deviation_may_raise

LEGACY = {
    "deviation_direction": "both",
    "deviation_min_pct_of_warning": 0,
    "deviation_requires_authority": False,
}


def _cfg(**over):
    base = dict(_DEV_GATE_DEFAULTS)
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# A1 — direction
# ---------------------------------------------------------------------------

def test_rising_deviation_may_raise():
    ok, blocked = _deviation_may_raise("deviating-high", 90.0, 100, True, _cfg())
    assert ok is True and blocked == ""


def test_falling_deviation_is_blocked_by_default():
    ok, blocked = _deviation_may_raise("deviating-low", 10.0, 100, True, _cfg())
    assert ok is False and blocked == "direction"


def test_falling_deviation_allowed_when_direction_both():
    ok, blocked = _deviation_may_raise(
        "deviating-low", 90.0, 100, True, _cfg(deviation_direction="both"))
    assert ok is True and blocked == ""


@pytest.mark.parametrize("bad", ["", "sideways", None, 5, "HIGH "])
def test_unrecognised_direction_falls_back_to_high(bad):
    """A malformed value must fail SAFE (high-only), never open the gate."""
    ok, blocked = _deviation_may_raise(
        "deviating-low", 10.0, 100, True, _cfg(deviation_direction=bad))
    assert ok is False and blocked == "direction"


def test_direction_high_is_case_and_space_insensitive():
    ok, _ = _deviation_may_raise(
        "deviating-low", 90.0, 100, True, _cfg(deviation_direction="  BOTH  "))
    assert ok is True


# ---------------------------------------------------------------------------
# A2 — proximity to the warning threshold
# ---------------------------------------------------------------------------

def test_value_below_proximity_gate_is_blocked():
    # warn=80, gate=80% -> 64. Value 50 is nowhere near the bar.
    ok, blocked = _deviation_may_raise("deviating-high", 50.0, 80, True, _cfg())
    assert ok is False and blocked == "proximity"


def test_value_exactly_at_the_proximity_gate_passes():
    # warn=80, 80% -> 64.0 exactly. Boundary must be inclusive.
    ok, blocked = _deviation_may_raise("deviating-high", 64.0, 80, True, _cfg())
    assert ok is True and blocked == ""


def test_value_just_under_the_gate_is_blocked():
    ok, blocked = _deviation_may_raise("deviating-high", 63.9, 80, True, _cfg())
    assert ok is False and blocked == "proximity"


def test_zero_pct_disables_the_proximity_gate():
    ok, blocked = _deviation_may_raise(
        "deviating-high", 1.0, 80, True, _cfg(deviation_min_pct_of_warning=0))
    assert ok is True and blocked == ""


def test_missing_warning_threshold_does_not_block():
    """Nothing to be proximate to — the gate must not silently swallow the alert."""
    ok, blocked = _deviation_may_raise("deviating-high", 5.0, None, True, _cfg())
    assert ok is True and blocked == ""


@pytest.mark.parametrize("bad", ["lots", None, [], {}])
def test_non_numeric_pct_falls_back_to_the_default(bad):
    # Falls back to 80 -> gate 64 -> a value of 50 stays blocked.
    ok, blocked = _deviation_may_raise(
        "deviating-high", 50.0, 80, True, _cfg(deviation_min_pct_of_warning=bad))
    assert ok is False and blocked == "proximity"


@pytest.mark.parametrize("pct,expected_ok", [(-50, True), (150, False)])
def test_out_of_range_pct_is_clamped(pct, expected_ok):
    """-50 clamps to 0 (gate off); 150 clamps to 100 (value must reach warn)."""
    ok, _ = _deviation_may_raise(
        "deviating-high", 50.0, 80, True, _cfg(deviation_min_pct_of_warning=pct))
    assert ok is expected_ok


def test_non_numeric_warning_threshold_does_not_crash():
    ok, blocked = _deviation_may_raise("deviating-high", 50.0, "eighty", True, _cfg())
    assert ok is True and blocked == ""


# ---------------------------------------------------------------------------
# A3 — authority
# ---------------------------------------------------------------------------

def test_no_authority_blocks_by_default():
    ok, blocked = _deviation_may_raise("deviating-high", 90.0, 100, False, _cfg())
    assert ok is False and blocked == "authority"


def test_no_authority_allowed_when_flag_off():
    ok, blocked = _deviation_may_raise(
        "deviating-high", 90.0, 100, False, _cfg(deviation_requires_authority=False))
    assert ok is True and blocked == ""


# ---------------------------------------------------------------------------
# Gate precedence — the reported gate is the first one that fails
# ---------------------------------------------------------------------------

def test_direction_is_reported_before_authority_and_proximity():
    ok, blocked = _deviation_may_raise("deviating-low", 1.0, 100, False, _cfg())
    assert ok is False and blocked == "direction"


def test_authority_is_reported_before_proximity():
    ok, blocked = _deviation_may_raise("deviating-high", 1.0, 100, False, _cfg())
    assert ok is False and blocked == "authority"


# ---------------------------------------------------------------------------
# Reversibility — legacy config must reproduce the old behaviour exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("b_state,value,warn,authority", [
    ("deviating-low", 28.0, 70, True),    # was: disk D below baseline
    ("deviating-low", 66.0, 70, True),
    ("deviating-low", 10.0, 75, False),
    ("deviating-high", 45.0, 75, True),
    ("deviating-high", 50.0, 80, True),
    ("deviating-high", 38.0, 80, True),
])
def test_legacy_settings_still_raise_everything(b_state, value, warn, authority):
    """Instant rollback path: every case the new defaults suppress must still
    raise under the legacy triple, proving no behaviour is lost, only gated."""
    ok, blocked = _deviation_may_raise(b_state, value, warn, authority, LEGACY)
    assert ok is True, f"legacy config must not block ({blocked})"


# ---------------------------------------------------------------------------
# Regression: the six real readings that were noise on the live fleet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,b_state,value,warn,authority,expect_gate", [
    ("app-d  disk D 28% below baseline 48%",  "deviating-low",  28.0, 70, True,  "direction"),
    ("db-a   disk D 66% below baseline 84%",  "deviating-low",  66.0, 70, True,  "direction"),
    ("wsus-a disk C 10% below baseline 53%",  "deviating-low",  10.0, 75, True,  "direction"),
    ("rds-a  disk C 45% above baseline 37%",  "deviating-high", 45.0, 75, True,  "proximity"),
    ("rds-a  RAM    50% above baseline 24%",  "deviating-high", 50.0, 80, True,  "proximity"),
    ("rds-b  RAM    38% above baseline 22%",  "deviating-high", 38.0, 80, True,  "proximity"),
])
def test_real_noise_cases_are_suppressed(label, b_state, value, warn, authority, expect_gate):
    ok, blocked = _deviation_may_raise(b_state, value, warn, authority, _cfg())
    assert ok is False, f"{label} should no longer warn"
    assert blocked == expect_gate, f"{label} blocked by {blocked}, expected {expect_gate}"


def test_a_disk_genuinely_climbing_still_warns():
    """The gates must not blind the tool to a real approach to capacity:
    a file server at 70% against an 85% warning bar clears the 68% gate."""
    ok, blocked = _deviation_may_raise("deviating-high", 70.0, 85, True, _cfg())
    assert ok is True and blocked == ""


# ---------------------------------------------------------------------------
# Declared defaults must match what the plan committed to
# ---------------------------------------------------------------------------

def test_declared_defaults_match_the_approved_plan():
    from config_manager import ConfigManager
    b = ConfigManager._DEFAULT_SETTINGS["baseline_detection"]
    assert b["deviation_direction"] == "high"
    assert b["deviation_min_pct_of_warning"] == 80
    assert b["deviation_requires_authority"] is True
    # detection.py's fallbacks must agree with config_manager's declarations,
    # or a config written before this change behaves differently from a fresh one.
    assert _DEV_GATE_DEFAULTS["deviation_direction"] == b["deviation_direction"]
    assert _DEV_GATE_DEFAULTS["deviation_min_pct_of_warning"] == b["deviation_min_pct_of_warning"]
    assert _DEV_GATE_DEFAULTS["deviation_requires_authority"] == b["deviation_requires_authority"]
