"""Cascade persistence: cycle rejection at write, and incident promotion.

WP-1 phase 3, slice 2. The pure logic lives in `cascade.py`; this is where
it meets the database.

TWO GUARANTEES, both append-only by design:

  * A dependency edge that would close a loop is REJECTED at the write.
    The runtime reducer therefore never walks a graph that can cycle —
    that is what keeps it a lookup instead of a traversal, and it is why
    the round table rejected Tarjan condensation (an algorithm spent on
    user error a clear error message prevents).

  * PROMOTION never rewrites history. `promoted_at` and `subject_server` are
    both nullable additive columns, and `root_cause_server` is NEVER nulled
    because it is the historical fact of how the outage began. A promoted
    child is `subject_server != root_cause_server` with `promoted_at` set —
    structural, not conventional. The trigger and the mechanism live in
    tests/test_cascade_promotion.py; the column contract lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── cycle rejection at the writer ─────────────────────────────────────────

def test_a_cycle_creating_edge_is_refused(tmp_db):
    """A→B exists; B→A must be refused with a message naming the loop, not
    silently accepted and dealt with at read time."""
    tmp_db.add_dependency("APP", "DB")
    with pytest.raises(ValueError) as excinfo:
        tmp_db.add_dependency("DB", "APP")
    assert "cycle" in str(excinfo.value).lower()


def test_an_indirect_cycle_is_refused(tmp_db):
    tmp_db.add_dependency("APP", "DB")
    tmp_db.add_dependency("DB", "DC")
    with pytest.raises(ValueError):
        tmp_db.add_dependency("DC", "APP")


def test_a_self_edge_is_refused(tmp_db):
    with pytest.raises(ValueError):
        tmp_db.add_dependency("APP", "APP")


def test_a_legitimate_edge_still_saves(tmp_db):
    tmp_db.add_dependency("APP", "DB")
    row_id = tmp_db.add_dependency("WEB", "APP")
    assert row_id
    assert len(tmp_db.get_all_dependencies()) == 2


def test_the_rejection_leaves_no_partial_row(tmp_db):
    """A refused write must not land. Otherwise the closure rebuild that
    follows would see the cycle it was supposed to prevent."""
    tmp_db.add_dependency("APP", "DB")
    try:
        tmp_db.add_dependency("DB", "APP")
    except ValueError:
        pass
    assert len(tmp_db.get_all_dependencies()) == 1


# ── the closure cache ─────────────────────────────────────────────────────

def test_the_closure_is_rebuilt_on_write_and_readable(tmp_db):
    """Computed at CRUD time (rare, human-driven), read at runtime as a
    point lookup — never a traversal on the 5s path."""
    tmp_db.add_dependency("APP", "DB")
    tmp_db.add_dependency("DB", "DC")
    closure = tmp_db.get_dependency_closure()
    assert closure["upstream"]["APP"] == {"DB": 1, "DC": 2}


def test_removing_an_edge_rebuilds_the_closure(tmp_db):
    """A stale closure is worse than none: it would mute a server whose
    dependency the operator just deleted."""
    dep_id = tmp_db.add_dependency("APP", "DB")
    assert tmp_db.get_dependency_closure()["upstream"].get("APP")
    tmp_db.remove_dependency(dep_id)
    assert not tmp_db.get_dependency_closure()["upstream"].get("APP")


def test_an_empty_graph_gives_an_empty_closure(tmp_db):
    c = tmp_db.get_dependency_closure()
    assert c == {"downstream": {}, "upstream": {}}


# ── promotion ─────────────────────────────────────────────────

# The promotion MECHANISM and its trigger are tested in
# tests/test_cascade_promotion.py, which is where the design point phase 3
# recorded gets resolved. Only the column contract lives here, next to the
# rest of the schema slice.
#
# WHAT MOVED, and why the tests that used to stand here are gone: phase 3
# promoted by UPDATE-ing an existing child row, guarded on
# `root_cause_server IS NOT NULL AND promoted_at IS NULL`. Under the owner's
# ruling at WP-1's close a child has no row until it is promoted, so there is
# nothing to update — `create_incident(..., promoted_at=...)` is the whole
# mechanism and the child's own row is the idempotence guard. The invariants
# those tests protected did not go away; they moved to the new file and are
# asserted against the new mechanism:
#
#   idempotent            test_promotion_creates_exactly_one_incident_...
#   a root is not a child test_a_root_and_a_promoted_child_are_structurally_...
#   append-only origin    test_the_origin_incident_row_is_untouched_by_promotion


def test_the_incidents_table_carries_promoted_at(tmp_db):
    cols = {r[1] for r in tmp_db._get_conn().execute(
        "PRAGMA table_info(incidents)").fetchall()}
    assert "promoted_at" in cols


def test_an_ordinary_incident_is_not_born_promoted(tmp_db):
    """`promoted_at` is the marker for one specific history. Anything that
    stamps it by default makes every incident look like a promoted orphan and
    the marker stops meaning anything."""
    inc = tmp_db.create_incident(title="DB down", severity="critical",
                                 subject_server="DB", root_cause_server="DB")
    assert tmp_db.get_incident(inc)["promoted_at"] is None


def test_a_promoted_incident_keeps_its_origin(tmp_db):
    """The one-way door. `root_cause_server` is NEVER nulled — it is the
    historical fact of how the outage began, and a promoted child that forgot
    its origin cannot be audited."""
    inc = tmp_db.create_incident(title="APP down", severity="critical",
                                 subject_server="APP",
                                 root_cause_server="DB",
                                 promoted_at="2026-08-19T10:00:00Z")
    row = tmp_db.get_incident(inc)
    assert row["promoted_at"] == "2026-08-19T10:00:00Z"
    assert row["root_cause_server"] == "DB", "the origin was erased"
    assert row["subject_server"] == "APP"


def test_the_identity_of_a_row_is_not_updatable(tmp_db):
    """`update_incident` deliberately does not accept `subject_server`. The
    subject is what the row IS; letting a later write change it would put the
    identity rule back in the hands of whoever calls the setter."""
    inc = tmp_db.create_incident(title="APP down", severity="critical",
                                 subject_server="APP", root_cause_server="DB")
    tmp_db.update_incident(inc, subject_server="SOMETHING_ELSE")
    assert tmp_db.get_incident(inc)["subject_server"] == "APP"
