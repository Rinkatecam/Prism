"""Guardrails for `Database.get_health_check_overview()`.

The Services card counts probes; `/services` lists them. Those two numbers
are read from two different methods and an operator who clicks the card
expects them to agree, so most of this file is about the ways they can
silently stop agreeing.

`get_health_check_summary` already argues at length why the aggregate must
be driven from `health_check_config` rather than from the append-only
`health_check_results` history. The overview inherits every one of those
traps and adds two of its own:

  * it must show DISABLED probes (the summary excludes them), because a
    probe someone switched off is a thing the operator configured and
    cannot otherwise see — but it must not let them into the counts, or
    the page and the card disagree by exactly the number of switched-off
    probes;
  * it returns one row per probe, so "the latest result wins" has to hold
    per probe rather than globally. A join that resolves the newest row in
    the whole table would give every probe the same status and look
    perfectly plausible on an estate where one probe is failing.

WHAT THESE ARE BLIND TO:

  * Whether the probes are right. `health_checker.py` decides up/down;
    this only reports what it stored.
  * Presentation. Whether `/services` renders these rows correctly is
    tests/test_design_services_page.py's job, not this file's.
  * Clocks. Latest-per-probe is resolved by `MAX(id)`, which is monotonic
    and needs no timezone. `last_checked` is carried through as data and
    never compared here — if the resolution ever moves to the timestamp,
    these tests keep passing and stop covering it.
"""

from __future__ import annotations

import inspect
import re


def _probe(db, server, host, port, check_type="tcp", name=""):
    return db.save_health_check_config(server, check_type, host, port, name=name)


def _disable(db, cfg_id):
    """Switch a probe off the way the operator's UI does — a row update.

    Written against the table rather than through a helper method because
    there is no such method: the enabled flag is set by the health-check
    settings route. Mirrors test_health_check_summary.py's helper so the
    two files build the same situation the same way."""
    conn = db._get_conn()
    try:
        conn.execute("UPDATE health_check_config SET enabled = 0 WHERE id = ?", (cfg_id,))
        conn.commit()
    finally:
        conn.close()


def _overview_sql() -> str:
    """The SQL the method actually runs, read out of its own source.

    Read from the method rather than restated here, for the reason recorded
    as #12 in docs/OPS-LEARNINGS.md: a test that reads its expectation from
    a constant declared beside it goes green over SQL the application does
    not execute."""
    from database import Database
    src = inspect.getsource(Database.get_health_check_overview)
    m = re.search(r'conn\.execute\("""(.*?)"""\)', src, re.S)
    assert m, "the method no longer runs a single triple-quoted statement"
    return m.group(1)


# ── what it returns ──────────────────────────────────────────────────────

def test_an_empty_estate_returns_no_rows(tmp_db):
    assert tmp_db.get_health_check_overview() == []


def test_a_configured_probe_appears_before_it_has_ever_run(tmp_db):
    """The gap between adding a probe and the first periodics tick is real,
    and a page that shows nothing during it looks broken rather than new."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445, name="SMB")

    rows = tmp_db.get_health_check_overview()
    assert len(rows) == 1
    assert rows[0]["server_name"] == "file-01"
    assert rows[0]["target_port"] == 445
    assert rows[0]["status"] == "unknown"
    assert rows[0]["last_checked"] is None


def test_never_probed_is_distinguishable_from_probed_with_no_verdict(tmp_db):
    """Both fold to `unknown` — that is deliberate, and it is why the row
    also has to carry enough to tell them apart. `last_checked` is that
    discriminator: a probe that has answered has a timestamp whatever it
    answered."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    _probe(tmp_db, "file-01", "10.0.0.10", 446)
    tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 446,
                                      "something-else", 4.0)

    by_port = {r["target_port"]: r for r in tmp_db.get_health_check_overview()}
    assert by_port[445]["status"] == "unknown"
    assert by_port[445]["last_checked"] is None
    assert by_port[446]["status"] == "unknown"
    assert by_port[446]["last_checked"] is not None


def test_the_latest_result_wins_per_probe_not_across_the_table(tmp_db):
    """The failure this exists to make impossible: a probe whose newest row
    is `down` reading as `up` because some OTHER probe answered later.

    Written so the newest row in the whole table belongs to the healthy
    probe — a resolution that ignores the probe key gives both rows `up`
    and looks entirely reasonable on a green page."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    _probe(tmp_db, "web-01", "10.0.0.20", 443, check_type="https")

    tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 445, "up", 3.0)
    tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 445, "down",
                                      None, "connection refused")
    tmp_db.upsert_health_check_result("web-01", "https", "10.0.0.20", 443, "up", 12.0)

    by_server = {r["server_name"]: r for r in tmp_db.get_health_check_overview()}
    assert by_server["file-01"]["status"] == "down"
    assert by_server["file-01"]["error"] == "connection refused"
    assert by_server["web-01"]["status"] == "up"


def test_one_probe_is_one_row_however_often_it_ran(tmp_db):
    """`health_check_results` is append-only — one probe on the five-minute
    cadence writes ~288 rows a day. A page listing result rows would grow a
    row per poll and read as a fleet that keeps getting bigger."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    for _ in range(12):
        tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 445, "up", 3.0)

    assert len(tmp_db.get_health_check_overview()) == 1


def test_a_disabled_probe_is_listed_and_flagged(tmp_db):
    """The summary excludes disabled probes deliberately. The page must
    still show them — an operator who switched one off cannot otherwise
    find it — which means the row has to say so."""
    cfg_id = _probe(tmp_db, "file-01", "10.0.0.10", 445)
    _disable(tmp_db, cfg_id)

    rows = tmp_db.get_health_check_overview()
    assert len(rows) == 1
    assert rows[0]["enabled"] == 0


# ── the invariant that matters most: the page agrees with the card ───────

def test_the_enabled_rows_reproduce_the_summary_exactly(tmp_db):
    """The card links to the page. If the page's own arithmetic disagrees
    with the card's, the operator has to work out which one lied.

    So this does not check "roughly the same": it rebuilds the summary's
    four numbers out of the overview's rows and demands equality."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    _probe(tmp_db, "web-01", "10.0.0.20", 443, check_type="https")
    _probe(tmp_db, "dc-01", "10.0.0.30", 389, check_type="tcp")
    old_id = _probe(tmp_db, "old-01", "10.0.0.40", 80, check_type="http")

    tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 445, "up", 3.0)
    tmp_db.upsert_health_check_result("web-01", "https", "10.0.0.20", 443, "down",
                                      None, "timeout")
    # dc-01 never probed. old-01 probed and then switched off.
    tmp_db.upsert_health_check_result("old-01", "http", "10.0.0.40", 80, "down",
                                      None, "404")
    _disable(tmp_db, old_id)

    rows = [r for r in tmp_db.get_health_check_overview() if r["enabled"]]
    rebuilt = {
        "total": len(rows),
        "up": sum(1 for r in rows if r["status"] == "up"),
        "down": sum(1 for r in rows if r["status"] == "down"),
        "unknown": sum(1 for r in rows if r["status"] == "unknown"),
    }
    assert rebuilt == tmp_db.get_health_check_summary()
    # And the disabled one is present in the unfiltered list, so the page
    # can show it without it ever having reached the arithmetic above.
    assert len(tmp_db.get_health_check_overview()) == len(rows) + 1


def test_two_probes_on_one_host_stay_two(tmp_db):
    """The probe's identity is the config table's UNIQUE tuple, not the
    server name. Resolving by server alone collapses a host's probes into
    one row and the page under-reports the estate."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    _probe(tmp_db, "file-01", "10.0.0.10", 139)

    assert len(tmp_db.get_health_check_overview()) == 2


# ── the shape of the query itself ────────────────────────────────────────

def test_the_query_is_driven_from_the_config_side(tmp_db):
    """`FROM health_check_results` as the OUTER table is the 2,000x-slower
    form `get_health_check_summary`'s docstring measures and rejects, and on
    a page that also refreshes it is the one that shows.

    Asserted against the first FROM in the statement — the outer one — rather
    than against a timing, because a timing on a dozen probes is noise."""
    sql = _overview_sql().lower()
    first_from = re.search(r"\bfrom\s+(\w+)", sql)
    assert first_from, sql
    assert first_from.group(1) == "health_check_config", (
        f"the outer query starts FROM {first_from.group(1)}")


def test_the_probe_key_is_matched_on_all_four_columns(tmp_db):
    """Dropping any one of them makes two distinct probes look like one.
    Port is the one that gets forgotten, because most hosts have a single
    probe and the bug is invisible until one does not.

    The behavioural version of this is
    `test_two_probes_on_one_host_stay_two` above; this one names the columns
    so a rewrite that happens to keep two probes apart for some other reason
    does not read as coverage of the key."""
    sql = _overview_sql().lower()
    for column in ("server_name", "check_type", "target_host", "target_port"):
        assert re.search(rf"\.{column}\s*=\s*\w+\.{column}", sql), (
            f"{column} is not part of the probe key in the query")


def test_the_overview_does_not_filter_on_enabled(tmp_db):
    """The summary's `WHERE c.enabled = 1` must NOT be carried over — the
    page's job is to show the probe that was switched off. Guarded because
    copying the summary's query and adjusting it is the obvious way to write
    this method, and the filter is the part that looks harmless."""
    sql = " ".join(_overview_sql().lower().split())
    assert "enabled = 1" not in sql, (
        "the overview must return disabled probes too")
