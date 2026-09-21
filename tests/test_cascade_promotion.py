"""Promotion: the child whose root recovered while it stayed down.

WP-1's last slice. The OPEN DESIGN POINT recorded in
`docs/plans/SEVERITY_MODEL_SPEC.md` is resolved here by the owner's ruling:
incidents carry a SUBJECT (which server the incident is about) separately
from `root_cause_server` (which server caused it), and a child gets no
incident row at all until it is promoted.

That makes the identity rule STRUCTURAL rather than a convention:

    root incident      subject_server == root_cause_server
    promoted ex-child  subject_server != root_cause_server, promoted_at set

and it keeps the package's headline outcome — one incident per outage, not
one per impacted machine — true by construction rather than by filtering
children out of every listing.

The trigger, from the spec: the root's server recovered AND this child is
still failed at its next FRESH poll. Fresh is load-bearing and fails
CLOSED: promoting on a stale reading pages someone for a machine that may
already be back, and a first-and-only notification cannot be taken back.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cascade import promotion_candidates            # noqa: E402


# A three-deep chain: APP01 → SQL01 → DC01 (each depends on the next).
_CHAIN = {
    "upstream": {"SQL01": {"DC01": 1},
                 "APP01": {"SQL01": 1, "DC01": 2}},
    "downstream": {"DC01": {"SQL01": 1, "APP01": 2},
                   "SQL01": {"APP01": 1}},
}


# ── the pure candidate rule ───────────────────────────────────────────────

def test_a_child_whose_root_recovered_is_a_promotion_candidate():
    """DC01 came back; SQL01 is still down and no longer explained by
    anything. It owns its failure now, and it has never been paged for."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "offline", "APP01": "healthy"},
        open_subjects={"DC01"},
        fresh={"SQL01"})
    assert got == {"SQL01": "DC01"}


def test_a_child_is_not_promoted_while_its_root_is_still_down():
    """The whole point of the mute: SQL01 is down BECAUSE DC01 is. Paging
    for it would be the N-incidents-for-one-outage failure returning."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "offline", "SQL01": "offline"},
        open_subjects={"DC01"},
        fresh={"SQL01"})
    assert got == {}


def test_a_child_with_a_second_failed_upstream_is_not_promoted():
    """APP01 depends on both SQL01 and (transitively) DC01. DC01 recovered
    but SQL01 has not, so APP01 is still explained — now by SQL01."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "offline", "APP01": "offline"},
        open_subjects={"DC01"},
        fresh={"SQL01", "APP01"})
    assert "APP01" not in got
    assert got == {"SQL01": "DC01"}


def test_a_child_that_already_owns_an_incident_is_not_promoted_again():
    """Promotion is the child's FIRST AND ONLY notification. Once it owns a
    row, the row is the guard — there is no second stamp to make."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "offline"},
        open_subjects={"DC01", "SQL01"},
        fresh={"SQL01"})
    assert got == {}


def test_promotion_is_fail_closed_when_the_child_is_not_fresh():
    """"At its next FRESH poll" is the spec's wording and it is load-bearing.
    A stale `offline` may describe a machine that is already back; a
    first-and-only page cannot be un-sent."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "offline"},
        open_subjects={"DC01"},
        fresh=set())
    assert got == {}


def test_promotion_is_fail_closed_when_freshness_is_unknown():
    """No freshness signal at all is not the same as "everything is fresh".
    A caller that forgets to wire it must promote NOTHING, not everything."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "offline"},
        open_subjects={"DC01"},
        fresh=None)
    assert got == {}


def test_a_failure_with_no_incident_owning_upstream_is_not_a_promotion():
    """PRINT01 just went down on its own. It gets an ordinary incident from
    root election — calling that a promotion would put a false origin on
    it, and the origin is the audit trail."""
    got = promotion_candidates(
        {"upstream": {}, "downstream": {}},
        states={"PRINT01": "offline"},
        open_subjects=set(),
        fresh={"PRINT01"})
    assert got == {}


def test_a_recovered_child_is_never_promoted():
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "healthy"},
        open_subjects={"DC01"},
        fresh={"SQL01"})
    assert got == {}


def test_the_deepest_incident_owning_upstream_is_named_as_the_origin():
    """Same rule as the reducer: name the ROOT of the cascade, not the
    nearest hop, so the promoted incident's origin matches the incident the
    child was muted under. SQL01 is at depth 1, DC01 at depth 2 — and both
    happen to own incidents (two overlapping outages)."""
    got = promotion_candidates(
        _CHAIN,
        states={"DC01": "healthy", "SQL01": "healthy", "APP01": "offline"},
        open_subjects={"DC01", "SQL01"},
        fresh={"APP01"})
    assert got == {"APP01": "DC01"}, "the nearest hop was blamed, not the root"


def test_a_group_upstream_that_recovered_can_be_an_origin_without_an_incident():
    """A redundancy group is not a server and never owns an incident row, so
    a member fleet that recovers leaves the child with no incident-owning
    upstream — and therefore no promotion, just an ordinary failure."""
    closure = {"upstream": {"FILE01": {"group:domain-services": 1}},
               "downstream": {"group:domain-services": {"FILE01": 1}}}
    got = promotion_candidates(
        closure,
        states={"FILE01": "offline", "DC01": "healthy"},
        open_subjects=set(),
        fresh={"FILE01"},
        groups={"group:domain-services": ["DC01"]})
    assert got == {}


# ── the subject column ────────────────────────────────────────────────────

def test_the_incidents_table_carries_subject_server(tmp_db):
    cols = {r[1] for r in tmp_db._get_conn().execute(
        "PRAGMA table_info(incidents)").fetchall()}
    assert "subject_server" in cols


def test_an_open_incident_is_found_by_its_subject(tmp_db):
    inc = tmp_db.create_incident(title="SQL01 down", severity="critical",
                                 subject_server="SQL01",
                                 root_cause_server="DC01")
    found = tmp_db.get_open_incident_by_subject("SQL01")
    assert found and found["id"] == inc


def test_a_promoted_incident_is_not_found_under_its_origin(tmp_db):
    """The dedup key is the SUBJECT. If the origin matched, one promoted
    child would block every sibling promoted from the same root — five
    orphans of one dead DC would share a single incident."""
    tmp_db.create_incident(title="SQL01 down", severity="critical",
                           subject_server="SQL01", root_cause_server="DC01")
    assert tmp_db.get_open_incident_by_subject("DC01") is None


def test_a_legacy_incident_without_a_subject_is_found_by_its_root(tmp_db):
    """Rows written before the column existed meant subject == root. The
    lookup must read them that way or every pre-upgrade cascade incident
    becomes invisible to dedup and gets duplicated on the next tick."""
    with tmp_db._write_lock:
        conn = tmp_db._get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO incidents (title, severity, root_cause_server) "
                "VALUES ('legacy', 'critical', 'DC01')")
            conn.commit()
            legacy = cur.lastrowid
        finally:
            conn.close()
    found = tmp_db.get_open_incident_by_subject("DC01")
    assert found and found["id"] == legacy


def test_a_resolved_incident_is_not_found_by_subject(tmp_db):
    inc = tmp_db.create_incident(title="SQL01 down", severity="critical",
                                 subject_server="SQL01")
    tmp_db.update_incident(inc, status="resolved")
    assert tmp_db.get_open_incident_by_subject("SQL01") is None


# ── election writes the subject, and promotion writes both ────────────────

def test_a_root_incident_carries_itself_as_its_subject(tmp_db):
    from analytics import cascade_incidents
    made = cascade_incidents(tmp_db, {"PRINT01": "offline"}, onsets={},
                             weights={})
    inc = tmp_db.get_incident(made[0]["incident_id"])
    assert inc["subject_server"] == "PRINT01"
    assert inc["root_cause_server"] == "PRINT01"
    assert inc["promoted_at"] is None


def test_a_promoted_incident_names_the_child_and_keeps_the_origin(tmp_db):
    """The one-way door: subject is the child (who is down), root_cause is
    the origin (how it began), and the origin is NEVER erased."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    tmp_db.create_incident(title="DC01 down", severity="critical",
                           subject_server="DC01", root_cause_server="DC01")
    made = cascade_incidents(
        tmp_db, {"DC01": "healthy", "SQL01": "offline"},
        onsets={}, weights={}, fresh={"SQL01"}, now="2026-08-19T10:00:00Z")
    assert len(made) == 1 and made[0]["promoted_from"] == "DC01"
    inc = tmp_db.get_incident(made[0]["incident_id"])
    assert inc["subject_server"] == "SQL01"
    assert inc["root_cause_server"] == "DC01", "the origin was erased"
    assert inc["promoted_at"] == "2026-08-19T10:00:00Z"


def test_a_root_and_a_promoted_child_are_structurally_distinguishable(tmp_db):
    """The resolved design point. No convention, no overloaded column: the
    two rows differ in whether subject and root_cause agree."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    root_id = tmp_db.create_incident(title="DC01 down", severity="critical",
                                     subject_server="DC01",
                                     root_cause_server="DC01")
    made = cascade_incidents(
        tmp_db, {"DC01": "healthy", "SQL01": "offline"},
        onsets={}, weights={}, fresh={"SQL01"}, now="2026-08-19T10:00:00Z")
    root = tmp_db.get_incident(root_id)
    child = tmp_db.get_incident(made[0]["incident_id"])
    assert root["subject_server"] == root["root_cause_server"]
    assert child["subject_server"] != child["root_cause_server"]


def test_promotion_creates_exactly_one_incident_however_many_passes(tmp_db):
    """A re-stamped promotion is a duplicate page at 3am. Here the guard is
    the child's own row — the second pass finds it and does nothing."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    tmp_db.create_incident(title="DC01 down", severity="critical",
                           subject_server="DC01", root_cause_server="DC01")
    states = {"DC01": "healthy", "SQL01": "offline"}
    first = cascade_incidents(tmp_db, states, onsets={}, weights={},
                              fresh={"SQL01"}, now="2026-08-19T10:00:00Z")
    second = cascade_incidents(tmp_db, states, onsets={}, weights={},
                               fresh={"SQL01"}, now="2026-08-19T11:00:00Z")
    assert len(first) == 1
    assert second == [], "a second pass promoted the same child twice"
    subjects = [i["subject_server"] for i in tmp_db.get_incidents(status="open")]
    assert subjects.count("SQL01") == 1


def test_the_origin_incident_row_is_untouched_by_promotion(tmp_db):
    """Append-only. Promotion adds a row; it never rewrites the history of
    the outage it came from."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    root_id = tmp_db.create_incident(title="DC01 down", severity="critical",
                                     subject_server="DC01",
                                     root_cause_server="DC01")
    before = tmp_db.get_incident(root_id)
    cascade_incidents(tmp_db, {"DC01": "healthy", "SQL01": "offline"},
                      onsets={}, weights={}, fresh={"SQL01"},
                      now="2026-08-19T10:00:00Z")
    after = tmp_db.get_incident(root_id)
    assert after == before


def test_a_promoted_incident_appends_a_marker_event_carrying_a_correlation_id(tmp_db):
    """The marker is how the child's timeline says "this began as someone
    else's outage", and the correlation_id is what ties it back."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    tmp_db.create_incident(title="DC01 down", severity="critical",
                           subject_server="DC01", root_cause_server="DC01")
    cascade_incidents(tmp_db, {"DC01": "healthy", "SQL01": "offline"},
                      onsets={}, weights={}, fresh={"SQL01"},
                      now="2026-08-19T10:00:00Z")
    rows = tmp_db._get_conn().execute(
        "SELECT * FROM events WHERE server_name = 'SQL01'").fetchall()
    markers = [dict(r) for r in rows if dict(r).get("event_type") == "promoted"]
    assert len(markers) == 1
    assert markers[0]["correlation_id"]
    assert "DC01" in (markers[0]["message"] or "")


def test_an_unpromoted_cascade_writes_no_child_row(tmp_db):
    """The headline outcome, held by construction: while the cascade is
    intact there is ONE row for the whole outage."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("SQL01", "DC01")
    tmp_db.add_dependency("APP01", "SQL01")
    cascade_incidents(tmp_db, {"DC01": "offline", "SQL01": "offline",
                               "APP01": "offline"},
                      onsets={}, weights={}, fresh={"SQL01", "APP01"})
    assert len(tmp_db.get_incidents(status="open")) == 1


# ── auto-resolution follows the subject ───────────────────────────────────

def test_auto_resolution_follows_the_subject_not_the_origin(tmp_db):
    """The bug the subject column exists to prevent. A promoted child whose
    row names DC01 as the origin must NOT resolve because DC01 is healthy —
    the child itself is still down, which is the entire reason it was
    promoted."""
    from analytics import _auto_resolve_incidents
    tmp_db.insert_metric("DC01", 10, 20, 30, None, "healthy")
    tmp_db.insert_metric("SQL01", None, None, None, None, "offline")
    child = tmp_db.create_incident(
        title="SQL01 down", severity="critical", subject_server="SQL01",
        root_cause_server="DC01", promoted_at="2026-08-19T10:00:00Z")
    _auto_resolve_incidents(tmp_db)
    assert tmp_db.get_incident(child)["status"] == "open"


def test_a_promoted_child_resolves_when_the_child_itself_recovers(tmp_db):
    from analytics import _auto_resolve_incidents
    tmp_db.insert_metric("DC01", 10, 20, 30, None, "healthy")
    tmp_db.insert_metric("SQL01", 10, 20, 30, None, "healthy")
    child = tmp_db.create_incident(
        title="SQL01 down", severity="critical", subject_server="SQL01",
        root_cause_server="DC01", promoted_at="2026-08-19T10:00:00Z")
    _auto_resolve_incidents(tmp_db)
    assert tmp_db.get_incident(child)["status"] == "resolved"


# ── the retired rule ──────────────────────────────────────────────────────

def test_the_sixty_second_window_cascade_rule_is_gone():
    """`cascade_incidents` was written in phase 3 and never called: the old
    rule was still the one running. This walks the AST rather than grepping,
    because a text scan fires on the very comment that explains the fix —
    that has happened five times in this repo (OPS-LEARNINGS §2.2)."""
    tree = ast.parse((PROJECT_ROOT / "analytics.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "correlate_events")
    titles = [n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("Cascading failure from" in t for t in titles), \
        "the retrospective window rule is still building incidents"


def _calls_within(tree, func_name: str) -> set:
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_correlate_events_reaches_the_closure_driven_election():
    """The replacement must actually be WIRED, not merely written. Phase 3
    added `cascade_incidents` and left the old rule running — a fix that is
    installed and not doing the work, which is this repo's most-repeated
    failure (OPS-LEARNINGS §2.2). This walks the call chain end to end so a
    future refactor cannot quietly unhook it again."""
    tree = ast.parse((PROJECT_ROOT / "analytics.py").read_text(encoding="utf-8"))
    assert "_cascade_election" in _calls_within(tree, "correlate_events")
    assert "cascade_incidents" in _calls_within(tree, "_cascade_election")


def test_the_quiet_pass_elects_too():
    """A recovery is the ABSENCE of an event, so the pass that first sees a
    root come back usually carries no events at all. If the no-events path
    returned early, promotion would only ever fire by coincidence."""
    tree = ast.parse((PROJECT_ROOT / "analytics.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "correlate_events")
    # The early-return branch for "no cycle_events" must contain the call.
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    early = [g for g in guards
             if any(isinstance(r, ast.Return) for r in ast.walk(g))
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == "_cascade_election" for c in ast.walk(g))]
    assert early, "the no-events path returns without electing"


def test_the_aggregator_runs_the_correlation_pass_on_a_quiet_fleet(monkeypatch):
    """The seam that makes the quiet pass reachable at all.

    Found live, not by reading: the app was running against the real fleet with
    the election wired and creating nothing, because the aggregator returned
    early whenever no events were pending — so the pass that promotion depends
    on was never invoked. A recovery is the absence of a failure; the pass that
    must notice it is the quietest one there is.
    """
    from types import SimpleNamespace
    from collector_v2 import aggregator

    calls = []

    def _fake_correlate(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(aggregator, "_correlate_events_fn",
                        lambda: _fake_correlate)
    monkeypatch.setattr(aggregator, "_list_servers_for_correlation", lambda: [])
    aggregator._recent_events.clear()
    aggregator._last_correlation_check = 0.0

    agg = SimpleNamespace(db=object(), get_settings=lambda: {})
    aggregator.Aggregator._maybe_run_correlation(agg)

    assert calls, "a quiet fleet skipped the correlation pass entirely"


def test_the_correlation_pass_promotes_before_it_auto_resolves(tmp_db):
    """Ordering is load-bearing and this pins it. Auto-resolution closes the
    root's incident the moment the root is healthy; if it ran first, the
    origin would already be gone and the orphan would get an ordinary
    incident with no record of where it came from."""
    from analytics import correlate_events
    tmp_db.add_dependency("SQL01", "DC01")
    tmp_db.insert_metric("DC01", 10, 20, 30, None, "healthy")
    tmp_db.insert_metric("SQL01", None, None, None, None, "offline")
    tmp_db.create_incident(title="DC01 down", severity="critical",
                           subject_server="DC01", root_cause_server="DC01")
    correlate_events(tmp_db, [{"server_name": "SQL01",
                               "event_type": "offline",
                               "message": "down"}], servers=[])
    child = tmp_db.get_open_incident_by_subject("SQL01")
    assert child is not None, "the orphan got no incident at all"
    assert child["root_cause_server"] == "DC01", \
        "the orphan lost its origin — auto-resolution ran first"
    assert child["promoted_at"] is not None
