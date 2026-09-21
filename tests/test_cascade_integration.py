"""Phase 3 integration: seeded domain group, the reducer in the fold, and
closure-driven incident election replacing the 60-second-window rule.

Three seams, all testable without a collector:

  seeded_domain_edges(...)   the day-one activation clause — every domain
                             member gets an ASSUMED edge to a Domain
                             Services group made of the DCs, severable
                             per server
  estate_service snapshot    an Impacted server contributes 0.15, not 1.0,
                             so a cascade does not fold as N outages
  cascade_incidents(...)     one incident per elected root, children
                             attached by root_cause_server — replacing
                             analytics' retrospective 60s-window rule
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cascade import GROUP_PREFIX, seeded_domain_edges, DOMAIN_GROUP   # noqa: E402


class _Srv:
    def __init__(self, name, type="file_server", criticality=""):
        self.name = name
        self.type = type
        self.criticality = criticality


def _fleet():
    return [_Srv("DC01", type="domain_controller"),
            _Srv("DC02", type="domain_controller"),
            _Srv("FILE01", type="file_server"),
            _Srv("APP01", type="app_server")]


# ── the seeded domain group ───────────────────────────────────────────────

def test_domain_controllers_become_the_group_members():
    edges, groups = seeded_domain_edges(_fleet(), {})
    assert groups[DOMAIN_GROUP] == ["DC01", "DC02"]


def test_every_non_dc_gets_an_assumed_edge_to_the_group():
    edges, _ = seeded_domain_edges(_fleet(), {})
    deps = {e["server_name"]: e["depends_on"] for e in edges}
    assert deps == {"FILE01": DOMAIN_GROUP, "APP01": DOMAIN_GROUP}


def test_a_dc_never_depends_on_the_group_it_belongs_to():
    """Otherwise the last DC standing would be muted by its own group the
    moment the others failed — a self-referential cascade."""
    edges, _ = seeded_domain_edges(_fleet(), {})
    assert not any(e["server_name"].startswith("DC") for e in edges)


def test_assumed_edges_are_labelled_as_assumed():
    """Every Impacted-via-assumed-edge tooltip must be able to name the
    assumption. An edge the operator never drew must never look like one
    they did."""
    edges, _ = seeded_domain_edges(_fleet(), {})
    assert all(e.get("assumed") is True for e in edges)
    assert all("domain" in (e.get("reason") or "").lower() for e in edges)


def test_a_severed_server_gets_no_assumed_edge():
    """The one-click escape hatch, which ships in the same phase as the
    seeding — non-negotiable, because the seeding is an opinion applied
    without asking."""
    settings = {"severity_model": {"severed_assumed_edges": ["APP01"]}}
    edges, _ = seeded_domain_edges(_fleet(), settings)
    assert {e["server_name"] for e in edges} == {"FILE01"}


def test_seeding_can_be_switched_off_entirely():
    settings = {"severity_model": {"seed_domain_group": False}}
    edges, groups = seeded_domain_edges(_fleet(), settings)
    assert edges == [] and groups == {}


def test_a_fleet_with_no_domain_controller_seeds_nothing():
    """No DC means no domain to depend on. Seeding an empty group would
    create edges to something that can never fail — noise with no signal."""
    edges, groups = seeded_domain_edges(
        [_Srv("FILE01"), _Srv("APP01", type="app_server")], {})
    assert edges == [] and groups == {}


def test_a_single_dc_fleet_still_seeds():
    """One DC is the common small-shop case, and it is exactly where a
    cascade is most useful — everything depends on the only DC."""
    edges, groups = seeded_domain_edges(
        [_Srv("DC01", type="domain_controller"), _Srv("FILE01")], {})
    assert groups[DOMAIN_GROUP] == ["DC01"]
    assert len(edges) == 1


def test_the_group_name_is_prefixed_so_it_can_never_collide_with_a_server():
    assert DOMAIN_GROUP.startswith(GROUP_PREFIX)


# ── the reducer reaches the fold ──────────────────────────────────────────

def test_an_impacted_server_contributes_less_than_a_failed_one():
    """THE point of the cascade at estate level: three machines down
    BECAUSE of one root must not fold as four outages. Impacted scores
    0.15; down scores 1.0."""
    import estate_service
    servers = [_Srv("DC01", type="domain_controller"), _Srv("APP01"),
               _Srv("FILE01")]
    cache = {n: {"status": "offline", "timestamp": "t"} for n in
             ("DC01", "APP01", "FILE01")}
    closure = {"upstream": {"APP01": {"DC01": 1}, "FILE01": {"DC01": 1}},
               "downstream": {"DC01": {"APP01": 1, "FILE01": 1}}}
    units, _ = estate_service.build_snapshot(servers, cache, {}, {}, {},
                                             closure=closure)
    by = {u["name"]: u for u in units}
    assert by["DC01"]["status"] == "offline"
    assert by["APP01"]["status"] == "impacted"
    assert by["FILE01"]["status"] == "impacted"


def test_without_a_closure_every_failure_stands_alone():
    """Zero edges ⇒ the reducer is a no-op and nothing is muted. Every
    fresh install is here."""
    import estate_service
    servers = [_Srv("DC01", type="domain_controller"), _Srv("APP01")]
    cache = {n: {"status": "offline", "timestamp": "t"} for n in ("DC01", "APP01")}
    units, _ = estate_service.build_snapshot(servers, cache, {}, {}, {})
    assert all(u["status"] == "offline" for u in units)


def test_the_impacted_reason_travels_with_the_unit():
    """The tooltip needs the root's name; carrying it on the unit means the
    renderer never re-derives it and the two cannot disagree."""
    import estate_service
    servers = [_Srv("DC01", type="domain_controller"), _Srv("APP01")]
    cache = {n: {"status": "offline", "timestamp": "t"} for n in ("DC01", "APP01")}
    closure = {"upstream": {"APP01": {"DC01": 1}},
               "downstream": {"DC01": {"APP01": 1}}}
    units, _ = estate_service.build_snapshot(servers, cache, {}, {}, {},
                                             closure=closure)
    app = [u for u in units if u["name"] == "APP01"][0]
    assert app["reason_code"] == "impacted_by"
    assert app["impacted_by"] == "DC01"


# ── incident election ─────────────────────────────────────────────────────

def test_a_chain_creates_one_incident_naming_the_root(tmp_db):
    from analytics import cascade_incidents
    tmp_db.add_dependency("APP01", "SQL01")
    tmp_db.add_dependency("SQL01", "DC01")
    states = {"DC01": "offline", "SQL01": "offline", "APP01": "offline"}
    made = cascade_incidents(tmp_db, states, onsets={}, weights={})
    assert len(made) == 1
    inc = tmp_db.get_incident(made[0]["incident_id"])
    assert inc["root_cause_server"] == "DC01"
    assert "APP01" in inc["description"] and "SQL01" in inc["description"]


def test_two_independent_outages_create_two_incidents(tmp_db):
    from analytics import cascade_incidents
    tmp_db.add_dependency("APP01", "DC01")
    states = {"DC01": "offline", "APP01": "offline", "PRINT01": "offline"}
    made = cascade_incidents(tmp_db, states, onsets={}, weights={})
    roots = {m["root"] for m in made}
    assert roots == {"DC01", "PRINT01"}


def test_an_ongoing_cascade_reuses_its_incident(tmp_db):
    """The 295-duplicate pile-up must not come back. Re-running with the
    same failure set reuses the open incident instead of spawning one per
    collector cycle — now keyed on root_cause_server, not a title prefix."""
    from analytics import cascade_incidents
    tmp_db.add_dependency("APP01", "DC01")
    states = {"DC01": "offline", "APP01": "offline"}
    first = cascade_incidents(tmp_db, states, onsets={}, weights={})
    second = cascade_incidents(tmp_db, states, onsets={}, weights={})
    assert len(first) == 1
    assert second == [], "a second cycle opened a duplicate incident"
    assert len(tmp_db.get_incidents(status="open")) == 1


def test_a_healthy_fleet_creates_nothing(tmp_db):
    from analytics import cascade_incidents
    tmp_db.add_dependency("APP01", "DC01")
    assert cascade_incidents(tmp_db, {"DC01": "healthy", "APP01": "healthy"},
                             onsets={}, weights={}) == []


def test_a_lone_failure_with_no_dependents_still_gets_its_incident(tmp_db):
    """A root with no children is still an outage. Only creating incidents
    for multi-server cascades would silently drop single failures — which
    is what the old rule did (it required an affected dependent)."""
    from analytics import cascade_incidents
    made = cascade_incidents(tmp_db, {"PRINT01": "offline"}, onsets={},
                             weights={})
    assert len(made) == 1 and made[0]["root"] == "PRINT01"
