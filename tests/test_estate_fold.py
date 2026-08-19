"""The weighted estate fold — `estate_fold`. WP-1 phase 2, the core.

Everything in `estate_fold` is a pure function of its arguments; the clock
is a parameter, never a read. That is not style — the repo's two flaky
tests both trace to wall-clock inside logic, and this module's dwell and
freeze ARE clocks. Tests pass `now` explicitly and time never elapses for
real anywhere in this file.

The ratified behaviour under test (docs/plans/SEVERITY_MODEL_SPEC.md):

  score    S = Σ(w·p)/Σw   p: critical/down 1.0, warning 0.35,
                              impacted 0.15, healthy/unknown 0.0
  bands    calm < elevated (S≥0.03) < urgent (S≥0.25)
  rails    any host down            ⇒ at least elevated
           critical-infra host critical-or-down ⇒ urgent
  dwell    upward moves instant; downward only after
           max(2×poll_interval, 60s) AND every recovering contributor
           has answered ≥2 times since the dwell opened
  freeze   3rd displayed-band change in a rolling 30min ⇒ hold the
           HIGHEST band, marked settling; rails pierce upward;
           release after the raw band is stable for 10min
  latch    ≥6 transitions/15min ⇒ unsteady (contributes worst recent);
           unlatch at ≤1 transition/15min; maintenance transitions
           never count
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from estate_fold import (       # noqa: E402
    SEVERITY_POINTS,
    BANDS,
    fold,
    FoldState,
    advance,
    latch_should_latch,
    latch_should_release,
)


def _srv(name="S", status="healthy", role="important", weight=4,
         in_maintenance=False, latched_worst=None):
    return {"name": name, "status": status, "role": role, "weight": weight,
            "in_maintenance": in_maintenance, "latched_worst": latched_worst}


def _fleet(n_important=19, n_background=7, n_ci=3):
    """The live fleet's shape: Σw = 19·4 + 7·1 + 3·10 = 113."""
    out = []
    out += [_srv(f"IMP{i}", role="important", weight=4) for i in range(n_important)]
    out += [_srv(f"BG{i}", role="background", weight=1) for i in range(n_background)]
    out += [_srv(f"DC{i}", role="critical_infrastructure", weight=10) for i in range(n_ci)]
    return out


# ── the score and the bands ───────────────────────────────────────────────

def test_an_all_healthy_fleet_is_calm_at_score_zero():
    v = fold(_fleet(), [], {})
    assert v.band == "calm"
    assert v.score == 0.0


def test_the_owner_complaint_one_background_critical_is_calm():
    """The literal original complaint: one of 29 at 95% disk (floor-critical,
    host still up) must not alarm the estate. 1·1.0/113 ≈ 0.009 < 0.03."""
    servers = _fleet()
    servers[19]["status"] = "critical"          # a background box
    v = fold(servers, [], {})
    assert v.band == "calm"
    assert 0 < v.score < 0.03
    # …but the dossier must NAME it — refusing to panic is not hiding.
    assert any(c["name"] == "BG0" for c in v.contributors)


def test_one_important_server_critical_is_elevated():
    servers = _fleet()
    servers[0]["status"] = "critical"           # important, w=4: 4/113 ≈ 0.035
    v = fold(servers, [], {})
    assert v.band == "elevated"


def test_warnings_accumulate_where_criticals_alone_would_not():
    """Eleven Background warnings: 11·1·0.35/113 ≈ 0.034 — accumulation is
    what the SUM grades (the veteran's concession that kept the formula)."""
    servers = _fleet()
    for i in range(19, 26):
        servers[i]["status"] = "warning"
    for i in range(0, 2):
        servers[i]["status"] = "warning"
    v = fold(servers, [], {})
    assert v.band == "elevated"


def test_band_boundaries_are_inclusive():
    """S == 0.03 IS elevated and S == 0.25 IS urgent — the spec writes
    bands as ≥, and an off-by-one here silently reclassifies real fleets."""
    # 2 servers: w=3 and w=97 → one warning on w=3... construct exactly.
    servers = [_srv("A", role="important", weight=3, status="critical"),
               _srv("B", role="important", weight=97)]
    v = fold(servers, [], {})
    assert abs(v.score - 0.03) < 1e-9
    assert v.band == "elevated"

    servers = [_srv("A", role="important", weight=25, status="critical"),
               _srv("B", role="important", weight=75)]
    v = fold(servers, [], {})
    assert abs(v.score - 0.25) < 1e-9
    assert v.band == "urgent"


# ── the rails ─────────────────────────────────────────────────────────────

def test_any_host_down_rails_to_at_least_elevated():
    """Print server down: 1/113 ≈ 0.009 — below every band. The RAIL is
    what delivers the promised 'print down ⇒ estate warning'."""
    servers = _fleet()
    servers[19]["status"] = "offline"
    v = fold(servers, [], {})
    assert v.band == "elevated"
    assert v.rail is not None and "BG0" in v.rail


def test_a_critical_infrastructure_host_down_rails_to_urgent():
    """DC down: 10/113 ≈ 0.088 — below urgent on the sum. The rail is the
    answer machine; the sum only grades between rails."""
    servers = _fleet()
    servers[26]["status"] = "offline"           # DC0
    v = fold(servers, [], {})
    assert v.band == "urgent"
    assert "DC0" in v.rail


def test_a_critical_infrastructure_host_critical_but_up_also_rails_urgent():
    servers = _fleet()
    servers[26]["status"] = "critical"
    v = fold(servers, [], {})
    assert v.band == "urgent"


def test_a_background_host_critical_but_up_fires_no_rail():
    """Rails are down OR critical-infrastructure — a critical-but-up
    Background box is the owner-approved refuse-to-panic case."""
    servers = _fleet()
    servers[19]["status"] = "critical"
    v = fold(servers, [], {})
    assert v.rail is None


def test_maintenance_excludes_a_server_from_score_AND_rails():
    """Patch night: a down server inside its window contributes nothing and
    fires no rail — otherwise every reboot is a cardiac event. The window
    auto-expires (phase 1), so this can never become a forever-mute."""
    servers = _fleet()
    servers[26]["status"] = "offline"
    servers[26]["in_maintenance"] = True
    v = fold(servers, [], {})
    assert v.band == "calm"
    assert v.rail is None
    # …and the exclusion is visible, not silent:
    assert v.excluded_maintenance == 1


# ── services: units weighted by their carrier ─────────────────────────────

def test_a_down_service_weighs_its_bound_servers_role():
    """Owner-approved decision #1: a down check on an Important server is
    4·1.0/(113+4) ≈ 0.034 → elevated, NOT today's instant urgent."""
    services = [{"name": "IIS on IMP0", "status": "down", "weight": 4}]
    v = fold(_fleet(), services, {})
    assert v.band == "elevated"
    assert v.rail is None, "services get no rail — rails are host-scoped"


def test_an_unknown_service_is_excluded_from_the_denominator():
    services = [{"name": "probe", "status": "unknown", "weight": 4}]
    v = fold(_fleet(), services, {})
    assert v.score == 0.0 and v.band == "calm"


# ── the dossier data ──────────────────────────────────────────────────────

def test_contributors_are_ranked_by_weighted_points_top_three():
    servers = _fleet()
    servers[26]["status"] = "critical"    # 10.0
    servers[0]["status"] = "critical"     # 4.0
    servers[19]["status"] = "warning"     # 0.35
    servers[20]["status"] = "warning"     # 0.35
    v = fold(servers, [], {})
    names = [c["name"] for c in v.contributors]
    assert names[0] == "DC0" and names[1] == "IMP0"
    assert len(v.contributors) == 3


def test_why_not_higher_names_the_distance_or_the_unarmed_rail():
    """The anti-mistrust clause: the calmer needle explains itself. For a
    calm estate with a critical Background box, the reason the band is not
    higher is that no rail is armed and the score is short of 0.03."""
    servers = _fleet()
    servers[19]["status"] = "critical"
    v = fold(servers, [], {})
    assert v.why_not_higher, "why_not_higher must never be empty when S > 0"
    assert "0.03" in v.why_not_higher or "rail" in v.why_not_higher.lower()


# ── dwell: enter instant, exit guarded ────────────────────────────────────

def _tick(state, band_or_verdict, now, poll=60, fresh=None):
    class _V:                      # minimal raw-verdict stand-in
        def __init__(self, band): self.band = band
    v = band_or_verdict if hasattr(band_or_verdict, "band") else _V(band_or_verdict)
    return advance(state, v, now=now, poll_interval=poll,
                   fresh_counts=fresh or {})


def test_an_upward_move_displays_the_same_tick():
    """Seeded FIRST: a fresh state's first tick exercises the seeding path,
    not the upward branch — a mutation making upward wait on the dwell
    passed this test until the seed was added (caught by the harness)."""
    st = _tick(FoldState(), "calm", now=999.0)          # seed
    st = _tick(st, "urgent", now=1000.0)
    assert st.displayed_band == "urgent"


def test_a_downward_move_waits_for_the_dwell():
    st = _tick(FoldState(), "urgent", now=0.0)
    st = _tick(st, "calm", now=1.0)              # recovery begins
    assert st.displayed_band == "urgent", "no instant de-escalation"
    st = _tick(st, "calm", now=90.0, fresh={"DC0": 2})
    assert st.displayed_band == "urgent", "60s < 2×poll_interval(60)=120"
    st = _tick(st, "calm", now=125.0, fresh={"DC0": 2})
    assert st.displayed_band == "calm", "dwell satisfied: time AND answers"


def test_the_dwell_freshness_guard_blocks_on_one_answer():
    """'The estate calms down only after every recovering server has
    answered twice' — one fresh sample is a coincidence of shard phase,
    not evidence."""
    st = _tick(FoldState(), "urgent", now=0.0)
    st = _tick(st, "calm", now=1.0)
    st = _tick(st, "calm", now=200.0, fresh={"DC0": 1})
    assert st.displayed_band == "urgent"
    st = _tick(st, "calm", now=205.0, fresh={"DC0": 2})
    assert st.displayed_band == "calm"


def test_a_relapse_during_the_dwell_cancels_it():
    """The relapse must cross a BAND CHANGE, and the timing is the test.

    A relapse back to the same displayed band is cleared by the agreement
    branch — two earlier versions of this test exercised only that path and
    a keep-the-stale-dwell mutation in the change path sailed through both
    (caught by the harness twice). So: displayed elevated, dwell toward
    calm opens at t=1, relapse UP to urgent at t=30 (a real band change),
    recovery restarts at t=60. At t=130 a stale t=1 dwell has banked
    129s ≥ 120 and would calm; only a correctly restarted t=60 dwell still
    holds. The two histories diverge exactly there.
    """
    st = _tick(FoldState(), "calm", now=-100.0)     # seed
    st = _tick(st, "elevated", now=0.0)             # change 1
    st = _tick(st, "calm", now=1.0)                 # dwell opens at t=1
    st = _tick(st, "urgent", now=30.0)              # relapse THROUGH a change
    assert st.displayed_band == "urgent"
    st = _tick(st, "calm", now=60.0)                # recovery restarts at t=60
    st = _tick(st, "calm", now=130.0, fresh={"DC0": 2})
    assert st.displayed_band == "urgent", (
        "a stale pre-relapse dwell was honoured: 129s banked from t=1 "
        "instead of 70s from t=60")
    st = _tick(st, "calm", now=185.0, fresh={"DC0": 2})
    assert st.displayed_band == "calm", "the restarted dwell eventually clears"


# ── freeze: the net for sub-latch oscillators ─────────────────────────────

def _oscillate(st, times):
    """Drive displayed-band changes at the given timestamps by alternating
    urgent/calm with an always-satisfied dwell (fresh answers supplied)."""
    band = "urgent"
    for t in times:
        if band == "urgent":
            st = _tick(st, "urgent", now=t)
        else:
            st = _tick(st, "calm", now=t, fresh={"X": 2})
        band = "calm" if band == "urgent" else "urgent"
    return st


def test_the_third_band_change_in_30min_freezes_at_the_highest():
    st = FoldState()
    # SEED first: a fresh FoldState adopts its first verdict silently (a
    # process start is not the estate changing its mind — ratified), so the
    # oscillation must begin from a seeded calm state for changes to count.
    st = _tick(st, "calm", now=-100.0)
    # change 1: calm→urgent at t=0; change 2: urgent→calm at t=260
    st = _tick(st, "urgent", now=0.0)
    st = _tick(st, "calm", now=130.0, fresh={"X": 2})     # dwell opened…
    st = _tick(st, "calm", now=260.0, fresh={"X": 2})     # …change 2 lands
    # change 3: calm→urgent at t=300 — trips the freeze
    st = _tick(st, "urgent", now=300.0)
    assert st.frozen is True
    assert st.displayed_band == "urgent", "frozen at the HIGHEST band"
    assert st.settling is True


def test_while_frozen_a_calm_raw_band_does_not_lower_the_display():
    st = _frozen_state()
    st = _tick(st, "calm", now=400.0, fresh={"X": 2})
    st = _tick(st, "calm", now=800.0, fresh={"X": 2})
    assert st.displayed_band == "urgent", "the freeze holds the cautious answer"


def test_rails_pierce_the_freeze_upward():
    """A frozen-at-elevated estate must still jump to urgent the merge a DC
    dies — the freeze can only ever over-warn, never under-warn."""
    st = FoldState()
    st = _tick(st, "calm", now=-100.0)          # seed (see the freeze test)
    st = _tick(st, "elevated", now=0.0)
    st = _tick(st, "calm", now=130.0, fresh={"X": 2})
    st = _tick(st, "calm", now=260.0, fresh={"X": 2})
    st = _tick(st, "elevated", now=300.0)                 # freeze trips at elevated
    assert st.frozen
    st = _tick(st, "urgent", now=310.0)
    assert st.displayed_band == "urgent"


def test_the_freeze_releases_after_ten_minutes_of_raw_stability():
    st = _frozen_state()
    for t in (400.0, 700.0, 950.0):
        st = _tick(st, "calm", now=t, fresh={"X": 2})
    assert st.frozen is True, "not yet 10min of stable raw"
    st = _tick(st, "calm", now=400.0 + 601.0, fresh={"X": 2})
    assert st.frozen is False
    assert st.displayed_band == "calm"


def _frozen_state():
    """(Also used as a fixture builder above.) Two clean changes then a
    third inside 30min — the documented trip condition, reproduced."""
    st = FoldState()
    st = _tick(st, "calm", now=-100.0)          # seed (see the freeze test)
    st = _tick(st, "urgent", now=0.0)
    st = _tick(st, "calm", now=130.0, fresh={"X": 2})
    st = _tick(st, "calm", now=260.0, fresh={"X": 2})
    st = _tick(st, "urgent", now=300.0)
    assert st.frozen is True
    return st


def test_a_restart_reseeds_from_raw_without_carrying_ghosts():
    """Module state does not survive restart, ratified as acceptable: a
    fresh FoldState adopts the first raw verdict instantly and starts
    counting from there."""
    st = _tick(FoldState(), "elevated", now=5000.0)
    assert st.displayed_band == "elevated"
    assert st.frozen is False


# ── the latch (pure logic; persistence is the aggregator's) ───────────────

def test_six_transitions_in_fifteen_minutes_latch():
    now = 10_000.0
    transitions = [now - 60 * m for m in (1, 3, 5, 7, 9, 11)]
    assert latch_should_latch(transitions, now) is True


def test_five_transitions_do_not_latch():
    now = 10_000.0
    transitions = [now - 60 * m for m in (1, 3, 5, 7, 9)]
    assert latch_should_latch(transitions, now) is False


def test_old_transitions_age_out_of_the_window():
    now = 10_000.0
    transitions = [now - 60 * m for m in (1, 3, 20, 25, 30, 40)]
    assert latch_should_latch(transitions, now) is False


def test_the_latch_releases_only_when_the_window_is_quiet():
    now = 50_000.0
    assert latch_should_release([now - 300.0], now) is True      # 1 in 15min
    assert latch_should_release([now - 300.0, now - 200.0], now) is False


def test_a_latched_server_contributes_its_worst_recent_state():
    """Unsteady is honest about severity even while the word is stable:
    the fold reads latched_worst instead of the bouncing stored status."""
    servers = _fleet()
    servers[26]["status"] = "healthy"                 # bounced back up…
    servers[26]["latched_worst"] = "offline"          # …but latched at worst
    v = fold(servers, [], {})
    assert v.band == "urgent", "the rail fires on the latched worst"


# ── the contract constants ────────────────────────────────────────────────

def test_severity_points_carry_the_ratified_values():
    assert SEVERITY_POINTS["critical"] == 1.0
    assert SEVERITY_POINTS["offline"] == 1.0
    assert SEVERITY_POINTS["warning"] == 0.35
    assert SEVERITY_POINTS["impacted"] == 0.15    # phase 3's reducer plugs in here
    assert SEVERITY_POINTS["healthy"] == 0.0


def test_bands_are_ordered_and_inclusive_thresholds_recorded():
    assert BANDS == (("urgent", 0.25), ("elevated", 0.03), ("calm", 0.0))
