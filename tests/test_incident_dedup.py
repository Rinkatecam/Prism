"""Regression tests for the incident-spam bug (295 duplicate cascade incidents).

Root causes fixed here:
  B1  correlate_events created a fresh incident every collector cycle for the
      same ongoing situation (no dedup against existing open incidents).
  B2  _auto_resolve_incidents skipped any incident with no linked events, so
      cascade incidents (whose events get purged) became permanent zombies.
  +   a collapse pass to clear the pre-fix backlog.

(The resolve-button CSRF fix is frontend-only and verified live, not here.)
"""

from __future__ import annotations

from types import SimpleNamespace

from analytics import correlate_events, _auto_resolve_incidents


def _srv(name, typ="other"):
    return SimpleNamespace(name=name, type=typ)


def _offline(name):
    return {"server_name": name, "event_type": "offline", "metric": None,
            "value": None, "threshold": None, "message": f"{name} offline"}


def _critical(name):
    # A bare critical event (no metric) so only the dependency-cascade rule
    # fires — multi-offline needs 2 *offline* events and would otherwise
    # legitimately cover (suppress) the cascade via its already_covered guard.
    return {"server_name": name, "event_type": "critical", "metric": None,
            "value": None, "threshold": None, "message": f"{name} critical"}


# ── B1: dedup on creation ─────────────────────────────────────────────────
def test_get_open_incident_id_by_title_prefix(tmp_db):
    db = tmp_db
    iid = db.create_incident(title="Cascading failure from APPSRV06 (1 dependent)",
                             severity="critical", root_cause_server="APPSRV06")
    assert db.get_open_incident_id_by_title_prefix("Cascading failure from APPSRV06") == iid
    # A different upstream must NOT match.
    assert db.get_open_incident_id_by_title_prefix("Cascading failure from APPSRV99") is None
    # Resolved incidents are ignored (so a recovered-then-refailed server re-opens).
    db.update_incident(iid, status="resolved")
    assert db.get_open_incident_id_by_title_prefix("Cascading failure from APPSRV06") is None
    # LIKE wildcards in the prefix (tag names with % or _) are matched literally.
    tid = db.create_incident(title="Tag '50%_off': 2 servers with issues",
                             severity="critical", root_cause_server="X")
    assert db.get_open_incident_id_by_title_prefix("Tag '50%_off':") == tid
    assert db.get_open_incident_id_by_title_prefix("Tag 'ZZ") is None


def test_cascade_incident_is_not_duplicated_across_cycles(tmp_db):
    db = tmp_db
    db.add_dependency("APPSRV07", "APPSRV06")  # APP07 depends on APP06
    servers = [_srv("APPSRV06"), _srv("APPSRV07")]
    events = [_critical("APPSRV06"), _critical("APPSRV07")]

    # Three identical collector cycles with the same ongoing outage.
    for _ in range(3):
        correlate_events(db, [dict(e) for e in events], servers)

    cascades = [i for i in db.get_incidents(status="open")
                if i["title"].startswith("Cascading failure from APPSRV06")]
    assert len(cascades) == 1, f"expected 1 open cascade incident, got {len(cascades)}"


def test_multi_offline_incident_is_not_duplicated(tmp_db):
    db = tmp_db
    servers = [_srv("A"), _srv("B")]
    events = [_offline("A"), _offline("B")]
    for _ in range(3):
        correlate_events(db, [dict(e) for e in events], servers)
    multi = [i for i in db.get_incidents(status="open")
             if i["title"].startswith("Multiple servers offline")]
    assert len(multi) == 1, f"expected 1 multi-offline incident, got {len(multi)}"


# ── B2: auto-resolve falls back to root_cause_server ──────────────────────
def test_auto_resolve_uses_root_cause_when_no_events(tmp_db):
    db = tmp_db
    iid = db.create_incident(title="Cascading failure from APPSRV06 (1 dependent)",
                             severity="critical", root_cause_server="APPSRV06")
    # No events linked (the zombie condition). Root-cause server is now healthy.
    db.insert_metric("APPSRV06", 10.0, 20.0, 30.0, 40.0, "healthy")

    _auto_resolve_incidents(db)

    detail = db.get_incident_detail(iid)
    assert detail["status"] == "resolved"
    assert detail["resolved_by"] == "auto"


def test_auto_resolve_leaves_open_when_root_cause_unhealthy(tmp_db):
    db = tmp_db
    iid = db.create_incident(title="Cascading failure from APPSRV06 (1 dependent)",
                             severity="critical", root_cause_server="APPSRV06")
    db.insert_metric("APPSRV06", 99.0, 99.0, 99.0, 99.0, "critical")

    _auto_resolve_incidents(db)

    assert db.get_incident_detail(iid)["status"] == "open"


# ── Collapse the pre-fix backlog ──────────────────────────────────────────
def test_collapse_duplicate_open_incidents(tmp_db):
    db = tmp_db
    # 5 byte-identical cascade incidents → collapse to 1.
    for _ in range(5):
        db.create_incident(title="Cascading failure from APPSRV06 (1 dependent)",
                           severity="critical", root_cause_server="APPSRV06")
    # Same ongoing mass-outage, fluctuating count in the title → still collapse
    # to 1 (the trailing "(N servers)" parenthetical is normalized away).
    for n in (2, 5, 29):
        db.create_incident(title=f"Multiple servers offline ({n} servers)",
                           severity="critical", root_cause_server="X")
    keep = db.create_incident(title="Compound stress on DBSRV01",
                              severity="warning", root_cause_server="DBSRV01")

    collapsed = db.collapse_duplicate_open_incidents()

    assert collapsed == 6  # 4 cascade + 2 multi-offline collapsed
    open_titles = [i["title"] for i in db.get_incidents(status="open")]
    assert open_titles.count("Cascading failure from APPSRV06 (1 dependent)") == 1
    assert sum(t.startswith("Multiple servers offline") for t in open_titles) == 1
    assert "Compound stress on DBSRV01" in open_titles
    # The kept unique incident is untouched and still open.
    assert db.get_incident_detail(keep)["status"] == "open"
