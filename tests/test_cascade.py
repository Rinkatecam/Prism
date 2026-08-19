"""The cascade: closure, cycle rejection, the reducer, root election.

WP-1 phase 3 (docs/plans/SEVERITY_MODEL_SPEC.md). Pure logic, no database,
no clock reads — the DB slice stores what these functions compute.

THE OUTCOME THIS EXISTS FOR: an N-deep dependency chain going down must
produce ONE incident naming the root, with the downstream machines marked
Impacted — muted, NEVER hidden — instead of N sibling incidents that each
page somebody.

Four pieces:

  build_closure(edges)      who is downstream of whom, and how deep
  would_create_cycle(...)   write-time rejection, so runtime never checks
  effective_severity(...)   the reducer: (own, closure, states) → (state, reason)
  elect_roots(...)          which failed node OWNS the incident

The redundancy group is the day-one activation clause: every domain member
gets an assumed edge to the "Domain Services" group, and a GROUP is failed
only when ALL its members are. Two DCs, one down: nobody is muted. That is
what makes a seeded edge safe to ship without asking.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cascade import (              # noqa: E402
    build_closure,
    would_create_cycle,
    effective_severity,
    elect_roots,
    GROUP_PREFIX,
)


def _edges(*pairs):
    """(dependent, upstream) pairs — 'A depends on B'."""
    return [{"server_name": a, "depends_on": b} for a, b in pairs]


# ── closure ───────────────────────────────────────────────────────────────

def test_a_two_hop_chain_has_both_depths():
    """APP → DB → DC. The closure records DC's reach at depth 1 (DB) and
    depth 2 (APP), which is what lets root election name DC once."""
    cl = build_closure(_edges(("APP", "DB"), ("DB", "DC")))
    assert cl["downstream"]["DC"] == {"DB": 1, "APP": 2}
    assert cl["downstream"]["DB"] == {"APP": 1}
    assert cl["upstream"]["APP"] == {"DB": 1, "DC": 2}


def test_an_empty_graph_yields_an_empty_closure():
    """Zero edges ⇒ the whole cascade is a no-op. Every fresh install
    starts here, so this is the case that must never misbehave."""
    cl = build_closure([])
    assert cl["downstream"] == {} and cl["upstream"] == {}


def test_unequal_paths_record_the_SHORTEST_depth():
    """A depends on D directly AND through B→C→D. D therefore reaches A at
    depth 1 and at depth 3; the recorded depth must be 1.

    The legs must be UNEQUAL for this to test anything. The first version
    used a symmetric diamond where both routes were depth 2 — shortest and
    longest are the same number there, so a longest-path implementation
    passed it (caught by the harness)."""
    cl = build_closure(_edges(("A", "D"), ("C", "D"), ("B", "C"), ("A", "B")))
    assert cl["downstream"]["D"]["A"] == 1, "recorded the long way round"
    assert cl["upstream"]["A"]["D"] == 1


def test_closure_ignores_a_self_edge_rather_than_looping():
    """A self-edge is dropped at ingest, leaving the closure entirely empty.

    Asserted as `== {}` on the whole closure, not `.get("A", {}) == {}` —
    the latter returns {} whether the key is absent OR present-but-empty,
    so it could not see the edge being ingested at all (caught by the
    harness)."""
    cl = build_closure(_edges(("A", "A")))
    assert cl["downstream"] == {}, "the self-edge was ingested"
    assert cl["upstream"] == {}


# ── cycle rejection at write time ─────────────────────────────────────────

def test_a_direct_cycle_is_rejected():
    """A→B exists; B→A would close a loop. Rejected at the WRITE, so the
    runtime reducer never has to check — the round table chose rejection
    over Tarjan condensation precisely to keep the hot path a lookup."""
    assert would_create_cycle(_edges(("A", "B")), "B", "A") is True


def test_an_indirect_cycle_is_rejected():
    assert would_create_cycle(_edges(("A", "B"), ("B", "C")), "C", "A") is True


def test_a_self_edge_is_rejected():
    assert would_create_cycle([], "A", "A") is True


def test_a_legitimate_edge_is_allowed():
    assert would_create_cycle(_edges(("A", "B")), "C", "A") is False
    assert would_create_cycle(_edges(("A", "B"), ("B", "C")), "A", "C") is False


# ── the reducer ───────────────────────────────────────────────────────────

def _states(**kw):
    return dict(kw)


def test_a_failing_server_with_a_failed_upstream_is_impacted():
    cl = build_closure(_edges(("APP", "DB")))
    state, reason, root = effective_severity(
        "APP", "offline", cl, _states(APP="offline", DB="offline"))
    assert state == "impacted"
    assert reason == "impacted_by"
    assert root == "DB"


def test_a_failing_server_with_a_HEALTHY_upstream_owns_its_failure():
    """The negative control, and the one that matters most: muting exists
    for noise, never for real failures. APP down while DB is fine is APP's
    own outage and must page."""
    cl = build_closure(_edges(("APP", "DB")))
    state, reason, root = effective_severity(
        "APP", "offline", cl, _states(APP="offline", DB="healthy"))
    assert state == "down"
    assert reason == "own_down"
    assert root is None


def test_the_deepest_failed_upstream_is_named_not_the_nearest():
    """APP → DB → DC, all down. APP is impacted BY DC — the root — not by
    DB, which is itself impacted. Naming the nearest would produce a chain
    of blame instead of one root."""
    cl = build_closure(_edges(("APP", "DB"), ("DB", "DC")))
    _s, _r, root = effective_severity(
        "APP", "offline", cl, _states(APP="offline", DB="offline", DC="offline"))
    assert root == "DC"


def test_a_healthy_server_is_never_impacted():
    """Impacted describes a FAILING machine whose failure is explained. A
    healthy server under a dead upstream is just healthy — reporting it as
    impacted would invent a problem."""
    cl = build_closure(_edges(("APP", "DB")))
    state, _r, _root = effective_severity(
        "APP", "healthy", cl, _states(APP="healthy", DB="offline"))
    assert state == "healthy"


def test_with_no_edges_the_reducer_returns_own_state():
    cl = build_closure([])
    state, reason, root = effective_severity(
        "APP", "offline", cl, _states(APP="offline"))
    assert (state, reason, root) == ("down", "own_down", None)


def test_a_warning_downstream_of_a_dead_upstream_stays_degraded():
    """Only FAILED downstreams are muted. A server merely over its warning
    threshold has a real, independent condition — suppressing it would hide
    a genuine signal behind an unrelated outage."""
    cl = build_closure(_edges(("APP", "DB")))
    state, _r, _root = effective_severity(
        "APP", "warning", cl, _states(APP="warning", DB="offline"))
    assert state == "degraded"


# ── redundancy groups: the day-one activation clause ──────────────────────

def test_a_group_upstream_fails_only_when_every_member_is_down():
    """Two DCs, one down. The group is UP, so nothing downstream is muted —
    this is what makes shipping a seeded assumed edge safe."""
    group = GROUP_PREFIX + "domain"
    cl = build_closure(_edges(("APP", group)))
    groups = {group: ["DC1", "DC2"]}
    state, _r, _root = effective_severity(
        "APP", "offline", cl, _states(APP="offline", DC1="offline", DC2="healthy"),
        groups=groups)
    assert state == "down", "one DC down must mute nobody"


def test_a_group_upstream_fails_when_all_members_are_down():
    group = GROUP_PREFIX + "domain"
    cl = build_closure(_edges(("APP", group)))
    groups = {group: ["DC1", "DC2"]}
    state, reason, root = effective_severity(
        "APP", "offline", cl, _states(APP="offline", DC1="offline", DC2="offline"),
        groups=groups)
    assert state == "impacted" and reason == "impacted_by"
    assert root == group


def test_an_empty_group_never_fails():
    """A group with no members must not read as 'all members down' — the
    vacuous-truth bug that would mute the entire fleet on a fresh install
    where the group exists but no DC has been classified yet."""
    group = GROUP_PREFIX + "domain"
    cl = build_closure(_edges(("APP", group)))
    state, _r, _root = effective_severity(
        "APP", "offline", cl, _states(APP="offline"), groups={group: []})
    assert state == "down"


# ── root election ─────────────────────────────────────────────────────────

def test_the_chain_elects_one_root_and_attaches_the_rest():
    """THE headline outcome: a 3-deep chain down produces ONE root and two
    children — one incident, two listed casualties."""
    cl = build_closure(_edges(("APP", "DB"), ("DB", "DC")))
    states = _states(APP="offline", DB="offline", DC="offline")
    roots = elect_roots(cl, states, onsets={"APP": 3.0, "DB": 2.0, "DC": 1.0},
                        weights={"APP": 4, "DB": 4, "DC": 10})
    assert roots == {"DC": ["APP", "DB"]} or roots == {"DC": ["DB", "APP"]}


def test_two_independent_failures_elect_two_roots():
    """Unrelated outages must not be collapsed into one incident just
    because they overlap in time — that is the retrospective 60s-window
    heuristic's failure mode, which this replaces."""
    cl = build_closure(_edges(("APP", "DB")))
    states = _states(APP="offline", DB="offline", PRINT="offline")
    roots = elect_roots(cl, states, onsets={"APP": 2.0, "DB": 1.0, "PRINT": 5.0},
                        weights={})
    assert set(roots) == {"DB", "PRINT"}
    assert roots["PRINT"] == []


def test_the_earliest_onset_wins_a_tie_between_two_candidate_roots():
    """Two failed servers, neither downstream of the other, both feeding
    one dependent. The one that failed FIRST is the cause."""
    cl = build_closure(_edges(("APP", "DB"), ("APP", "DC")))
    states = _states(APP="offline", DB="offline", DC="offline")
    roots = elect_roots(cl, states, onsets={"DB": 5.0, "DC": 1.0}, weights={})
    assert "DC" in roots and "APP" in roots["DC"]


def test_weight_breaks_a_tie_when_onsets_match():
    """Same instant — the more important machine is the more likely cause,
    and it is the one an operator should be sent to first."""
    cl = build_closure(_edges(("APP", "DB"), ("APP", "DC")))
    states = _states(APP="offline", DB="offline", DC="offline")
    roots = elect_roots(cl, states, onsets={"DB": 1.0, "DC": 1.0},
                        weights={"DB": 4, "DC": 10})
    assert "DC" in roots


def test_a_healthy_fleet_elects_nothing():
    cl = build_closure(_edges(("APP", "DB")))
    assert elect_roots(cl, _states(APP="healthy", DB="healthy"),
                       onsets={}, weights={}) == {}


def test_a_missing_onset_does_not_crash_election():
    """Onsets come from observation and a just-restarted process may not
    have one for every failure. Absent onset sorts last, never raises."""
    cl = build_closure(_edges(("APP", "DB")))
    roots = elect_roots(cl, _states(APP="offline", DB="offline"),
                        onsets={}, weights={})
    assert roots == {"DB": ["APP"]}
