"""The estate-vitals severity model — `routes.views._estate_vitals`.

This function decides what the dashboard's centre circle says and how fast it
beats. It is the one piece of the redesign whose output is a judgement rather
than a count, so every branch is pinned here rather than left to be inferred
from the docstring.

The bucket rules are the interesting part. Three of them are choices that a
plausible alternative would get wrong in a way nobody would notice:

  * `unknown` (configured, never measured) counts toward `monitored` and
    against `percent`. Folding it into `ok` reports a clean bill of health
    for hosts that have never answered; folding it into `bad` raises an
    alarm for something that has not failed.
  * `flat` is tested BEFORE `urgent`, so "everything is down" reads as a
    flatline rather than as a fast beat.
  * `flat` does NOT require every host to be offline — `ok == 0` is the test.
    One healthy host out of thirty is an estate in trouble, not a dead one,
    and the difference decides whether the trace moves at all.

WHAT THESE ARE BLIND TO:

  * Whether the SOURCE numbers are right. `get_status_summary` and
    `get_health_check_summary` are tested separately; this only folds them.
  * Whether the resulting tempo reads as calm or alarming to a human. That
    is a design judgement and 60/96/132 bpm is the recorded decision, not a
    derived value.
  * Anything about network and scan. They contribute nothing today by
    design; when they arrive, these tests will keep passing while silently
    stopping short of the composite the circle claims to measure.
"""

from __future__ import annotations

import pytest

from routes.views import _VITALS_BPM, _estate_vitals


def _summary(total=0, healthy=0, warning=0, critical=0, offline=0):
    return {"total": total, "healthy": healthy, "warning": warning,
            "critical": critical, "offline": offline}


def _services(total=0, up=0, down=0, unknown=0):
    return {"total": total, "up": up, "down": down, "unknown": unknown}


# ── severity ─────────────────────────────────────────────────────────────

def test_nothing_configured_is_idle_and_has_no_percentage():
    v = _estate_vitals(0, _summary(), _services())
    assert v["severity"] == "idle"
    assert v["monitored"] == 0
    assert v["percent"] is None, (
        "0% would be a verdict on an estate nobody has asked Prism to watch; "
        "the readout shows a dash for None")
    assert v["bpm"] == 0


def test_a_wholly_healthy_estate_is_calm():
    v = _estate_vitals(3, _summary(total=3, healthy=3), _services(total=2, up=2))
    assert v["severity"] == "calm"
    assert (v["ok"], v["monitored"], v["percent"]) == (5, 5, 100)


def test_a_warning_alone_is_elevated():
    v = _estate_vitals(3, _summary(total=3, healthy=2, warning=1), _services())
    assert v["severity"] == "elevated"


def test_anything_critical_is_urgent():
    v = _estate_vitals(3, _summary(total=3, healthy=2, critical=1), _services())
    assert v["severity"] == "urgent"


def test_an_offline_server_is_urgent_too():
    """Offline is at least as bad as critical — a host reachable by nothing
    tells you less than one that is merely over a threshold, not more."""
    v = _estate_vitals(3, _summary(total=3, healthy=2, offline=1), _services())
    assert v["severity"] == "urgent"


def test_a_failing_service_is_urgent_even_with_every_server_healthy():
    """The circle measures everything monitored, not just servers. A dead
    IIS on a host whose CPU and RAM are fine is exactly the case health
    checks exist for, and the whole reason this card was wired up."""
    v = _estate_vitals(3, _summary(total=3, healthy=3), _services(total=2, up=1, down=1))
    assert v["severity"] == "urgent"
    assert v["bad"] == 1


def test_everything_down_is_a_flatline_not_a_fast_beat():
    v = _estate_vitals(3, _summary(total=3, offline=3), _services())
    assert v["severity"] == "flat"
    assert v["bpm"] == 0, "a flatline that still beats is not a flatline"


def test_flat_is_tested_before_urgent():
    """Both conditions hold when everything is critical. The ORDER is the
    behaviour: reversing the two branches turns the owner's flatline case
    into the fastest possible beat, which is the opposite reading."""
    v = _estate_vitals(2, _summary(total=2, critical=2), _services())
    assert v["severity"] == "flat"


def test_one_healthy_host_out_of_thirty_is_urgent_not_flat():
    """The negative control for the rule above, and the one that stops `flat`
    from swallowing every bad estate. A single survivor means there is still
    a signal to show."""
    v = _estate_vitals(30, _summary(total=30, healthy=1, offline=29), _services())
    assert v["severity"] == "urgent"
    assert v["bpm"] > 0


def test_an_estate_that_has_never_been_measured_is_neither_flat_nor_calm():
    """All-unknown is not all-down, and it is not well either.

    `flat` requires something actually bad or dead, so this case would
    wrongly read as "the estate is dead" for the first poll interval after a
    restart. But the branch it used to fall through to was `calm`, which
    labelled 29 hosts that had never answered as **"Stable" beside 0%** —
    three individually true statements that together say nothing. Both
    plausible existing severities lie about it, which is what earns a sixth.
    """
    v = _estate_vitals(29, _summary(), _services())
    assert v["unknown"] == 29
    assert v["severity"] == "unmeasured"
    assert v["percent"] is None, (
        "0% is a verdict on hosts that have never reported; the readout "
        "renders a dash for None, the same as `idle`")
    assert v["bpm"] == 0, "nothing is happening on the estate, so nothing beats"


def test_one_host_that_HAS_answered_takes_the_estate_out_of_unmeasured():
    """The negative control, and the reason the trigger is narrow.

    A single measurement makes the estate measurable, so the ordinary
    severities apply again and the score is a real number. Widening the
    trigger to "mostly unknown" would hide that real number behind a dash
    and would need a threshold nobody could defend.
    """
    v = _estate_vitals(29, _summary(total=1, healthy=1), _services())
    assert v["unknown"] == 28
    assert v["severity"] == "calm"
    assert v["percent"] == 3


def test_unmeasured_does_not_swallow_the_empty_estate():
    """`idle` still owns "nothing is configured". The two states render
    identically — a dash and a still trace — so a mix-up would be invisible
    on screen, while the labels say opposite things: "Nothing monitored"
    is a statement about the config, "Awaiting data" a statement about the
    collector."""
    v = _estate_vitals(0, _summary(), _services())
    assert v["severity"] == "idle"
    assert v["monitored"] == 0


def test_an_unprobed_service_can_put_the_estate_in_unmeasured_too():
    """The circle measures everything monitored, so "nothing has reported"
    has to include health checks. An estate with no servers and three probes
    that have not yet run is exactly as unmeasured as one with no metrics."""
    v = _estate_vitals(0, _summary(), _services(total=3, unknown=3))
    assert v["severity"] == "unmeasured"
    assert v["monitored"] == 3


def test_only_the_two_never_measured_states_withhold_a_percentage():
    """"No beat" and "no number" are different questions, and the tempting
    simplification conflates them.

    `flat` beats at 0 and still HAS a percentage: every host is down, so 0%
    healthy is a measured fact and the single most useful figure on the
    circle. The dash belongs to the two states where no measurement exists
    at all — nothing configured, and nothing reported. Keying the dash off
    the tempo instead would blank the readout on a flatlined estate.
    """
    flat = _estate_vitals(3, _summary(total=3, offline=3), _services())
    assert flat["bpm"] == 0
    assert flat["percent"] == 0, "everything down is a real 0%, not an absence"

    for name, v in (("idle", _estate_vitals(0, _summary(), _services())),
                    ("unmeasured", _estate_vitals(29, _summary(), _services()))):
        assert v["percent"] is None, f"{name} must render a dash, not a score"


def test_the_tempo_rises_with_the_severity():
    """"The worse it gets the faster it beats" as an assertion rather than a
    comment. Also pins that the two silent states are silent."""
    assert _VITALS_BPM["calm"] < _VITALS_BPM["elevated"] < _VITALS_BPM["urgent"]
    assert _VITALS_BPM["flat"] == _VITALS_BPM["idle"] == 0
    assert _VITALS_BPM["unmeasured"] == 0, (
        "an estate nothing has reported on has no tempo to report; a beat "
        "here would animate a claim about data that does not exist")


@pytest.mark.parametrize("severity",
                         ["calm", "elevated", "urgent", "flat", "idle", "unmeasured"])
def test_every_severity_the_model_can_return_has_a_tempo(severity):
    """`_VITALS_BPM[severity]` is a bare subscript — a severity added to the
    branch ladder without a tempo raises KeyError and takes the whole
    dashboard down with a 500, rather than degrading."""
    assert severity in _VITALS_BPM


# ── buckets ──────────────────────────────────────────────────────────────

def test_a_configured_server_with_no_metrics_row_is_unknown():
    """`get_status_summary().total` is the sum of the status buckets, so a
    host that has never been collected is absent from it entirely. The gap
    against the configured count is the only thing that surfaces it."""
    v = _estate_vitals(29, _summary(total=27, healthy=27), _services())
    assert v["unknown"] == 2
    assert v["monitored"] == 29
    assert v["percent"] == 93, "the two unmeasured hosts count against the score"


def test_the_unknown_gap_never_goes_negative():
    """The configured count and the metrics count come from different
    places, and a just-deleted server leaves its metrics row behind for a
    retention cycle. Without the clamp, `monitored` would fall below the
    real fleet size and `percent` would exceed 100."""
    v = _estate_vitals(2, _summary(total=5, healthy=5), _services())
    assert v["unknown"] == 0
    assert v["percent"] == 100


def test_an_unprobed_service_is_unknown_not_up():
    v = _estate_vitals(0, _summary(), _services(total=3, up=1, unknown=2))
    assert (v["ok"], v["unknown"], v["monitored"]) == (1, 2, 3)
    assert v["percent"] == 33


def test_the_servers_card_totals_the_configured_fleet_not_the_measured_one():
    """Otherwise the card reads "27 / 27" on a fleet of 29 and the two
    unmeasured hosts vanish from the one place counting them."""
    v = _estate_vitals(29, _summary(total=27, healthy=26, warning=1), _services())
    assert v["servers"]["total"] == 29
    assert v["servers"]["unknown"] == 2


def test_the_servers_card_never_shows_a_total_below_its_own_parts():
    """The same drift as above from the other side: more metrics rows than
    configured servers must not produce "27 / 25"."""
    v = _estate_vitals(25, _summary(total=27, healthy=27), _services())
    assert v["servers"]["total"] == 27
    assert v["servers"]["total"] >= v["servers"]["healthy"]


def test_missing_data_degrades_instead_of_raising():
    """Both reads are defended in `_vitals_context`, so this function gets
    None whenever a query failed or a table is missing on an old database.
    A dashboard that 500s because health checks are unavailable is a worse
    outcome than one that reports what it knows."""
    v = _estate_vitals(0, None, None)
    assert v["severity"] == "idle" and v["percent"] is None
    v = _estate_vitals(4, None, _services(total=1, up=1))
    assert v["monitored"] == 5 and v["unknown"] == 4


def test_the_buckets_account_for_everything_monitored():
    """`monitored` is a sum of the five buckets, not a separate count. If a
    future status fell outside all of them the percentage would be computed
    against a denominator that does not match the cards beside it."""
    v = _estate_vitals(30, _summary(total=28, healthy=20, warning=3, critical=4, offline=1),
                       _services(total=6, up=4, down=1, unknown=1))
    assert v["ok"] + v["warn"] + v["bad"] + v["dead"] + v["unknown"] == v["monitored"]
    assert v["monitored"] == 30 + 6
