"""The business-hours weight profile: a flag, not a calendar engine.

Parked in `docs/plans/SEVERITY_MODEL_SPEC.md` and un-parked by the owner at
WP-1's close. All three sysadmin personas asked for it independently, and
all three asked for the same small thing: outside working hours, the
line-of-business machines should not fold as hard. Nobody asked for
holidays, on-call rotas or per-server schedules, and the spec is explicit
that adding them would be the wrong shape.

What it therefore is: one optional `severity_model.business_hours` block
that swaps in a second WEIGHT table outside the window. It reuses the
existing per-site weights-override mechanism, so there is no new concept in
the fold — the same arithmetic, different numbers.

Two invariants the tests below pin, because they are what make it safe:

  * A DOWN server is still announced. The weight only scales ACCUMULATION;
    the fold's rails are independent of weight, so an Important server going
    down outside hours still forces `elevated`. Overnight damping must never
    become overnight silence.
  * The clock stays a PARAMETER. `weight_for` reads no clock; with no `now`
    it returns the base weights. Both of this repo's flaky tests came from
    wall-time buried inside logic.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from severity_roles import is_business_hours, weight_for      # noqa: E402


def _at(y, m, d, hh, mm=0) -> float:
    """A UTC instant as an epoch float. Berlin is UTC+2 in August, so these
    read two hours later in the configured zone — which is the point of the
    timezone tests below."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


# Wednesday 2026-08-19; asserted rather than assumed, so a wrong weekday
# fails loudly instead of quietly testing the wrong branch.
_WEDNESDAY = datetime(2026, 8, 19, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 8, 22, tzinfo=timezone.utc)


def test_the_fixture_dates_are_the_weekdays_they_claim():
    assert _WEDNESDAY.weekday() == 2
    assert _SATURDAY.weekday() == 5


_ON = {
    "timezone": "Europe/Berlin",
    "severity_model": {"business_hours": {"enabled": True,
                                          "start_hour": 8, "end_hour": 18}},
}


# ── the window ────────────────────────────────────────────────────────────

def test_a_weekday_morning_is_inside_the_window():
    assert is_business_hours(_at(2026, 8, 19, 7), _ON) is True     # 09:00 Berlin


def test_a_weekday_night_is_outside_the_window():
    assert is_business_hours(_at(2026, 8, 19, 22), _ON) is False   # 00:00 Berlin


def test_the_configured_timezone_decides_and_not_utc():
    """06:30 UTC is 08:30 in Berlin — inside the window there, outside it in
    UTC. Every timestamp in this app is displayed in the configured zone and
    the severity model is not an exception."""
    assert is_business_hours(_at(2026, 8, 19, 6, 30), _ON) is True
    assert is_business_hours(_at(2026, 8, 19, 6, 30),
                             {**_ON, "timezone": "UTC"}) is False


def test_the_evening_boundary_uses_the_configured_zone_too():
    """17:00 UTC is 19:00 Berlin — outside there, inside in UTC."""
    assert is_business_hours(_at(2026, 8, 19, 17), _ON) is False
    assert is_business_hours(_at(2026, 8, 19, 17),
                             {**_ON, "timezone": "UTC"}) is True


def test_the_weekend_is_outside_the_window():
    assert is_business_hours(_at(2026, 8, 22, 10), _ON) is False


def test_the_working_days_are_configurable():
    """A shop that runs Saturdays says so. `days` is python weekday numbers,
    Monday 0 — the same convention the rest of the app schedules on."""
    weekend_shop = {**_ON, "severity_model": {
        "business_hours": {"enabled": True, "start_hour": 8, "end_hour": 18,
                           "days": [5, 6]}}}
    assert is_business_hours(_at(2026, 8, 22, 10), weekend_shop) is True
    assert is_business_hours(_at(2026, 8, 19, 10), weekend_shop) is False


def test_start_is_inclusive_and_end_is_exclusive():
    """08:00 is a working hour; 18:00 is not. Stated because an off-by-one
    here is a silent hour of the wrong weights every single day."""
    assert is_business_hours(_at(2026, 8, 19, 6), _ON) is True     # 08:00
    assert is_business_hours(_at(2026, 8, 19, 16), _ON) is False   # 18:00
    assert is_business_hours(_at(2026, 8, 19, 15, 59), _ON) is True


def test_a_window_that_wraps_midnight_is_honoured():
    """A night-shift site configures 22 → 06. Without wrap handling that
    window reads as "never inside" and damps every weight around the clock —
    a silent misconfiguration rather than a loud one."""
    night = {"timezone": "UTC", "severity_model": {
        "business_hours": {"enabled": True, "start_hour": 22, "end_hour": 6,
                           "days": [0, 1, 2, 3, 4, 5, 6]}}}
    assert is_business_hours(_at(2026, 8, 19, 23), night) is True
    assert is_business_hours(_at(2026, 8, 19, 2), night) is True
    assert is_business_hours(_at(2026, 8, 19, 12), night) is False


def test_the_window_is_off_unless_it_is_switched_on():
    off = {"timezone": "Europe/Berlin", "severity_model": {}}
    assert is_business_hours(_at(2026, 8, 19, 22), off) is None


def test_an_unparseable_zone_degrades_to_no_opinion():
    """config.json is hand-editable. A typo in the zone must not take the
    dashboard down and must not silently pick a different zone — it returns
    "no opinion", and the weights stay at their base values."""
    broken = {**_ON, "timezone": "Mars/Olympus_Mons"}
    assert is_business_hours(_at(2026, 8, 19, 22), broken) is None


def test_a_garbage_window_degrades_to_no_opinion():
    broken = {"timezone": "Europe/Berlin", "severity_model": {
        "business_hours": {"enabled": True, "start_hour": "elevenish"}}}
    assert is_business_hours(_at(2026, 8, 19, 22), broken) is None


# ── the weights ───────────────────────────────────────────────────────────

def test_weights_are_unchanged_inside_the_window():
    now = _at(2026, 8, 19, 7)
    assert weight_for("important", _ON, now=now) == 4
    assert weight_for("critical_infrastructure", _ON, now=now) == 10


def test_an_important_server_weighs_less_outside_the_window():
    now = _at(2026, 8, 19, 22)
    assert weight_for("important", _ON, now=now) == 2


def test_critical_infrastructure_never_weighs_less_outside_the_window():
    """The default profile's whole argument: a domain controller at 3am is
    exactly as much of an emergency as at 3pm. Only the line-of-business
    tier is allowed to soften."""
    now = _at(2026, 8, 19, 22)
    assert weight_for("critical_infrastructure", _ON, now=now) == 10


def test_background_cannot_soften_below_the_floor():
    now = _at(2026, 8, 19, 22)
    assert weight_for("background", _ON, now=now) == 1


def test_the_outside_profile_is_configurable():
    site = {"timezone": "Europe/Berlin", "severity_model": {
        "business_hours": {"enabled": True, "start_hour": 8, "end_hour": 18,
                           "outside_weights": {"important": 3}}}}
    assert weight_for("important", site, now=_at(2026, 8, 19, 22)) == 3


def test_without_a_clock_the_weights_are_the_base_weights():
    """`weight_for` must never read a clock: the callers that have `now`
    pass it, and the ones that do not get the undamped answer. A function
    that quietly consults wall-time is how both flaky tests in this repo
    were born."""
    assert weight_for("important", _ON) == 4


def test_the_site_wide_weight_override_still_wins_inside_the_window():
    site = {"timezone": "Europe/Berlin", "severity_model": {
        "weights": {"important": 7},
        "business_hours": {"enabled": True, "start_hour": 8, "end_hour": 18}}}
    assert weight_for("important", site, now=_at(2026, 8, 19, 7)) == 7


def test_the_outside_profile_overrides_the_site_wide_weight(tmp_path):
    """Both are per-site knobs; the more specific one — the one that names
    the hours — is the one that wins outside them."""
    site = {"timezone": "Europe/Berlin", "severity_model": {
        "weights": {"important": 7},
        "business_hours": {"enabled": True, "start_hour": 8, "end_hour": 18,
                           "outside_weights": {"important": 2}}}}
    assert weight_for("important", site, now=_at(2026, 8, 19, 22)) == 2


def test_an_unreadable_window_leaves_the_weights_alone(tmp_path):
    """"No opinion" must not be read as "outside hours". A typo in `timezone`
    would otherwise damp every weight in the estate at every hour of the day —
    a misconfiguration that makes the product quieter and says nothing."""
    broken = {**_ON, "timezone": "Mars/Olympus_Mons"}
    assert weight_for("important", broken, now=_at(2026, 8, 19, 22)) == 4


def test_a_zero_outside_weight_is_floored_not_honoured():
    """Weight 0 removes the unit from the DENOMINATOR too, so a machine
    weighted zero stops counting as monitored at all — invisible rather than
    quieter. The floor is what keeps "softer" from becoming "gone"."""
    site = {"timezone": "Europe/Berlin", "severity_model": {
        "business_hours": {"enabled": True, "start_hour": 8, "end_hour": 18,
                           "outside_weights": {"background": 0}}}}
    assert weight_for("background", site, now=_at(2026, 8, 19, 22)) == 1


def test_the_estate_snapshot_applies_the_profile():
    """The seam, not just the function. `build_snapshot` has to pass its clock
    down or the profile is configured, documented, and doing nothing — this
    repo's most-repeated failure."""
    import estate_service

    class _Srv:
        def __init__(self, name, type):
            self.name, self.type, self.criticality = name, type, ""

    servers = [_Srv("FILE01", "file_server")]
    cache = {"FILE01": {"status": "healthy", "timestamp": "t"}}
    units, _ = estate_service.build_snapshot(
        servers, cache, _ON, {}, {}, now=_at(2026, 8, 19, 22))
    assert units[0]["weight"] == 2
    day, _ = estate_service.build_snapshot(
        servers, cache, _ON, {}, {}, now=_at(2026, 8, 19, 7))
    assert day[0]["weight"] == 4


# ── the invariant that makes it safe ──────────────────────────────────────

def test_an_important_server_down_outside_hours_is_still_announced():
    """Damping must never become silence. The weight change lowers the
    SCORE, but the fold's rails do not read weight — so the estate still
    reads elevated and still names the machine."""
    from estate_fold import fold
    now = _at(2026, 8, 19, 22)
    servers = [{"name": "FILE01", "status": "offline", "role": "important",
                "weight": weight_for("important", _ON, now=now)}]
    servers += [{"name": f"OK{i}", "status": "healthy", "role": "important",
                 "weight": weight_for("important", _ON, now=now)}
                for i in range(20)]
    verdict = fold(servers, [], _ON)
    assert verdict.rail == "FILE01 is down"
    assert verdict.band == "elevated"


def test_a_dc_down_outside_hours_is_still_urgent():
    from estate_fold import fold
    now = _at(2026, 8, 19, 22)
    servers = [{"name": "DC01", "status": "offline",
                "role": "critical_infrastructure",
                "weight": weight_for("critical_infrastructure", _ON, now=now)}]
    verdict = fold(servers, [], _ON)
    assert verdict.band == "urgent"


def test_the_same_warning_load_scores_lower_outside_hours():
    """The measurable effect, stated as a comparison rather than a magic
    number: identical trouble on identical fleets folds to a smaller score
    at night."""
    from estate_fold import fold

    def _score(now):
        w = weight_for("important", _ON, now=now)
        units = [{"name": "A", "status": "warning", "role": "important",
                  "weight": w}]
        units += [{"name": f"OK{i}", "status": "healthy",
                   "role": "critical_infrastructure",
                   "weight": weight_for("critical_infrastructure", _ON, now=now)}
                  for i in range(3)]
        return fold(units, [], _ON).score

    assert _score(_at(2026, 8, 19, 22)) < _score(_at(2026, 8, 19, 7))
