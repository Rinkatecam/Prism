"""Guardrails for `Database.get_health_check_summary()`.

The dashboard's Services card is the first thing in the application to read
health-check state in aggregate, and every plausible-looking shortcut for
computing it is wrong in a way that produces a believable number:

  * counting rows in `health_check_results` reports how often the probes
    have RUN (append-only history, ~288 rows per probe per day) — which on
    a small estate looks exactly like a service count;
  * counting them without the enabled filter reports probes nobody is
    watching as down;
  * defaulting a never-probed row to `up` reports a clean bill of health
    for a probe that has never once answered.

So each of those is asserted directly rather than left to the SQL being
read carefully. Every test here builds the situation and checks the number.

WHAT THESE ARE BLIND TO:

  * Whether the probes themselves are right. `health_checker.py` decides
    up/down; this only aggregates what it stored.
  * Timezones. `last_checked` is never compared against anything here —
    latest-per-probe is resolved by `MAX(id)`, which is monotonic and
    needs no clock. If the resolution ever moves to the timestamp, these
    tests will keep passing and will stop covering it.
"""

from __future__ import annotations

import inspect
import re


def _probe(db, server, host, port, check_type="tcp"):
    return db.save_health_check_config(server, check_type, host, port)


def _summary_sql() -> str:
    """The SQL `get_health_check_summary` runs, read out of its own source.

    Taken from the method rather than copied into this file so a plan
    assertion cannot go green over SQL the application does not execute — the
    failure recorded as #12 in docs/OPS-LEARNINGS.md was a test reading its
    expectation from a constant declared beside it."""
    from database import Database
    src = inspect.getsource(Database.get_health_check_summary)
    m = re.search(r'conn\.execute\("""(.*?)"""\)', src, re.S)
    assert m, "the method no longer runs a single triple-quoted statement"
    return m.group(1)


def test_an_empty_estate_reports_zeroes(tmp_db):
    assert tmp_db.get_health_check_summary() == {
        "total": 0, "up": 0, "down": 0, "unknown": 0}


def test_each_configured_probe_counts_once_however_often_it_ran(tmp_db):
    """The history table is append-only. Counting result ROWS is the
    mistake this exists to make impossible: one probe polled twelve times
    is one service, not twelve."""
    _probe(tmp_db, "file-01", "10.0.0.10", 445)
    for _ in range(12):
        tmp_db.upsert_health_check_result("file-01", "tcp", "10.0.0.10", 445, "up", 3.0)

    s = tmp_db.get_health_check_summary()
    assert s == {"total": 1, "up": 1, "down": 0, "unknown": 0}


def test_the_latest_result_wins_not_the_first(tmp_db):
    """A probe that was up and is now down must read as down. Resolved by
    MAX(id) — insertion order — so this also pins that the resolution is
    monotonic and does not depend on `last_checked` string comparison."""
    _probe(tmp_db, "web-01", "10.0.0.20", 443, "https")
    tmp_db.upsert_health_check_result("web-01", "https", "10.0.0.20", 443, "up", 40.0)
    tmp_db.upsert_health_check_result("web-01", "https", "10.0.0.20", 443, "down", 0.0,
                                      "Connection refused")

    s = tmp_db.get_health_check_summary()
    assert s == {"total": 1, "up": 0, "down": 1, "unknown": 0}


def test_two_probes_on_one_host_stay_two(tmp_db):
    """The join key is the config table's whole UNIQUE tuple. Grouping by
    server_name alone — the shortest thing that looks right — collapses a
    host's HTTPS and SMB probes into one, and the count silently becomes a
    host count."""
    _probe(tmp_db, "app-01", "10.0.0.30", 443, "https")
    _probe(tmp_db, "app-01", "10.0.0.30", 445, "tcp")
    tmp_db.upsert_health_check_result("app-01", "https", "10.0.0.30", 443, "up", 12.0)
    tmp_db.upsert_health_check_result("app-01", "tcp", "10.0.0.30", 445, "down", 0.0)

    s = tmp_db.get_health_check_summary()
    assert s == {"total": 2, "up": 1, "down": 1, "unknown": 0}


def test_a_disabled_probe_is_not_counted_at_all(tmp_db):
    """Not down, not unknown — absent. A switched-off probe reported as
    down puts a red number on the dashboard for something the operator
    deliberately stopped watching."""
    cfg_id = _probe(tmp_db, "standalone-01", "10.0.0.40", 3389)
    tmp_db.upsert_health_check_result("standalone-01", "tcp", "10.0.0.40", 3389, "down", 0.0)
    conn = tmp_db._get_conn()
    try:
        conn.execute("UPDATE health_check_config SET enabled = 0 WHERE id = ?", (cfg_id,))
        conn.commit()
    finally:
        conn.close()

    assert tmp_db.get_health_check_summary() == {
        "total": 0, "up": 0, "down": 0, "unknown": 0}


def test_a_probe_with_no_result_yet_is_unknown(tmp_db):
    """Configured and never answered. `up` would be a lie that reads as
    good news; `down` would be an alert for something that has not failed.
    The window is real — the periodics cadence is five minutes, so every
    restart passes through it."""
    _probe(tmp_db, "dc-01", "10.0.0.5", 389, "tcp")

    assert tmp_db.get_health_check_summary() == {
        "total": 1, "up": 0, "down": 0, "unknown": 1}


def test_a_result_with_no_config_is_not_counted(tmp_db):
    """History outlives configuration: deleting a probe leaves its result
    rows behind (`delete_health_check_config` touches only the config
    table). Driving the count from the results side would keep reporting a
    service the operator removed — and reporting it as DOWN, since nothing
    probes it any more."""
    tmp_db.upsert_health_check_result("GONE01", "tcp", "10.0.0.99", 445, "down", 0.0)

    assert tmp_db.get_health_check_summary() == {
        "total": 0, "up": 0, "down": 0, "unknown": 0}


def test_the_summary_never_scans_the_whole_history_table(tmp_db):
    """The cost characteristic, asserted as a QUERY PLAN rather than a clock.

    This runs on the dashboard's five-second refresh path, and
    `health_check_results` is append-only history — one row per probe per five
    minutes, so ~131,000 rows at the 30-day retention default with a dozen
    probes. The readable form of this query (GROUP BY the results table to
    find each MAX(id), then join back) touches all of them to answer a
    question about twelve, and was measured at 82.64 ms against 0.033 ms for
    the form that drives from the config side. Both return the same numbers,
    so no behavioural test here can tell them apart — this is the only check
    that can.

    A plan assertion rather than a timing assertion on purpose: a timing test
    on a 12-row fixture measures nothing, and on a 131,000-row fixture it is
    slow, machine-dependent and flaky. The plan is deterministic.

    WHAT THIS IS BLIND TO: it reads the plan for the query as written here,
    re-derived from the same SQL the method runs. It cannot notice the method
    being pointed at different SQL, which is what the coverage test below is
    for.
    """
    for i in range(3):
        tmp_db.save_health_check_config(f"h{i}", "tcp", f"10.0.0.{i}", 443 + i)

    sql = _summary_sql()
    conn = tmp_db._get_conn()
    try:
        plan = [r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        conn.close()

    scans = [p for p in plan
             if p.startswith("SCAN") and "health_check_results" in p]
    assert not scans, (
        "the summary scans the history table; at the retention default that "
        "is ~131,000 rows every five seconds:\n  " + "\n  ".join(plan))
    assert any("idx_hc_results_probe" in p for p in plan), (
        "the probe index is not being used, so each probe's newest row is "
        f"found by scanning:\n  " + "\n  ".join(plan))


def test_the_plan_check_is_reading_the_query_the_method_actually_runs():
    """`_summary_sql()` extracts the SQL from the method's own source, so the
    plan test cannot pass while the method runs something else. Without this,
    the check above would be asserting about a copy — the shape that produced
    finding #12 in docs/OPS-LEARNINGS.md, a test that asserted on a constant
    declared in the test file."""
    sql = _summary_sql()
    assert "health_check_config" in sql and "health_check_results" in sql
    assert "ORDER BY r.id DESC" in sql, (
        "the extracted SQL no longer resolves the newest row per probe by id")
    assert "c.enabled = 1" in sql


def test_the_buckets_always_sum_to_the_total(tmp_db):
    """`total` is not a separate query, and nothing may fall between the
    buckets. A status the probes do not currently emit — a future check
    type reporting `degraded`, say — must land in `unknown` rather than
    being dropped, or the card shows a total larger than its parts and
    reads as a rendering bug."""
    _probe(tmp_db, "A", "10.0.0.1", 80, "http")
    _probe(tmp_db, "B", "10.0.0.2", 80, "http")
    _probe(tmp_db, "C", "10.0.0.3", 80, "http")
    tmp_db.upsert_health_check_result("A", "http", "10.0.0.1", 80, "up", 5.0)
    tmp_db.upsert_health_check_result("B", "http", "10.0.0.2", 80, "degraded", 5.0)
    # C is left unprobed.

    s = tmp_db.get_health_check_summary()
    assert s["total"] == s["up"] + s["down"] + s["unknown"] == 3
    assert s["up"] == 1 and s["down"] == 0 and s["unknown"] == 2
