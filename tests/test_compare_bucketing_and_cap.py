"""Server-comparison payload bucketing and the selection cap.

Background (2026-08-05): all four /servers/compare* endpoints hard-rejected more
than 6 servers, while the frontend offered checkboxes for all 29 AND shipped an
"All" button that checked every one of them — so "All" was a guaranteed 400. The
owner reported it as "I can only select some not all".

The cap itself was the wrong lever. It existed because get_server_history returns
EVERY row, so the payload grew linearly with the selection (measured: 330 KB for
one server over 720h, ~9.6 MB for 29). Bucketing the chart series makes the
response a function of the bucket count instead, which is what allows the cap to
cover the whole fleet.

Deliberate asymmetry pinned below: the STATS endpoint keeps using full history,
because p95/stddev computed over pre-averaged buckets would be wrong — averaging
destroys exactly the spread those statistics measure.
"""

from __future__ import annotations

import sqlite3

import pytest

from database import Database
from routes.api.reports import COMPARE_CHART_BUCKETS, MAX_COMPARE_SERVERS


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    return d


def _insert(db, server, rows):
    """rows = [(timestamp, cpu, ram, disk_c, disk_d, status)]"""
    conn = sqlite3.connect(db.db_path)
    conn.executemany(
        """INSERT INTO metrics (server_name, timestamp, cpu_percent, ram_percent,
                                disk_c_percent, disk_d_percent, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(server, *r) for r in rows],
    )
    conn.commit()
    conn.close()


def _dense(hours_back_from, count, cpu=10.0, disk_d=-1.0):
    """`count` readings one minute apart, ending `hours_back_from` hours ago."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc) - timedelta(hours=hours_back_from)
    return [
        ((now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         cpu, 50.0, 40.0, disk_d, "healthy")
        for i in range(count)
    ]


# ── bucketing ─────────────────────────────────────────────────────────────

def test_bucketing_bounds_the_output_regardless_of_row_count(db):
    _insert(db, "s1", _dense(0.1, 3000))
    raw = db.get_server_history("s1", hours=720)
    buck = db.get_server_history_bucketed("s1", hours=720, buckets=240)
    assert len(raw) == 3000
    assert len(buck) <= 240, "output must be bounded by the bucket count"
    assert len(buck) < len(raw) / 10, "should be a big reduction"


def test_bucketed_output_is_oldest_to_newest_like_the_raw_version(db):
    _insert(db, "s1", _dense(0.1, 600))
    buck = db.get_server_history_bucketed("s1", hours=720, buckets=240)
    stamps = [r["timestamp"] for r in buck]
    assert stamps == sorted(stamps), "must be chronological, matching get_server_history"


def test_absent_metric_sentinel_is_preserved_not_averaged(db):
    """disk_d = -1 means 'no such drive'. Averaging it in would produce a
    meaningless fractional value; NULLIF + re-emit keeps it as -1."""
    _insert(db, "s1", _dense(0.1, 300, disk_d=-1.0))
    buck = db.get_server_history_bucketed("s1", hours=720, buckets=240)
    assert buck, "expected buckets"
    assert {r["disk_d_percent"] for r in buck} == {-1.0}


def test_a_present_metric_is_averaged_within_its_bucket(db):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [((now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             float(v), 50.0, 40.0, 20.0, "healthy")
            for i, v in enumerate([10, 20, 30, 40])]
    _insert(db, "s1", rows)
    buck = db.get_server_history_bucketed("s1", hours=1, buckets=1)
    assert len(buck) == 1
    assert buck[0]["cpu_percent"] == pytest.approx(25.0)
    assert buck[0]["samples"] == 4


def test_empty_buckets_are_omitted_so_a_gap_stays_a_gap(db):
    """Two dense clusters 100 hours apart must NOT be joined by invented
    zero/null points in between."""
    _insert(db, "s1", _dense(0.1, 120))     # recent cluster
    _insert(db, "s1", _dense(100, 120))     # cluster ~100h ago
    buck = db.get_server_history_bucketed("s1", hours=720, buckets=240)
    assert 0 < len(buck) <= 240
    # no bucket may carry zero samples — absence is absence
    assert all(r["samples"] > 0 for r in buck)


def test_buckets_below_one_is_clamped(db):
    _insert(db, "s1", _dense(0.1, 60))
    assert db.get_server_history_bucketed("s1", hours=24, buckets=0) is not None
    assert db.get_server_history_bucketed("s1", hours=24, buckets=-5) is not None


def test_no_data_returns_empty_list_not_an_error(db):
    assert db.get_server_history_bucketed("ghost", hours=720, buckets=240) == []


def test_raw_history_is_untouched_by_the_new_method(db):
    """get_server_history has 9 other callers (PDF/CSV reports, server detail).
    Its behaviour must not change."""
    _insert(db, "s1", _dense(0.1, 50))
    raw = db.get_server_history("s1", hours=720)
    assert len(raw) == 50
    assert set(raw[0]) == {"timestamp", "cpu_percent", "ram_percent",
                           "disk_c_percent", "disk_d_percent", "status"}


# ── the cap ───────────────────────────────────────────────────────────────

def test_cap_is_large_enough_for_a_whole_fleet():
    """The point of the change: 'All' must be a legal request. 6 was not."""
    assert MAX_COMPARE_SERVERS >= 30, (
        "cap must comfortably exceed the ~29-server fleet, else 'All' 400s again")
    assert MAX_COMPARE_SERVERS <= 200, "not unbounded either"


def test_bucket_count_is_finer_than_any_realistic_chart_width():
    assert COMPARE_CHART_BUCKETS >= 120


def test_all_four_compare_endpoints_share_one_cap():
    """The original defect was structural — four copies of the number, and a
    frontend that knew none of them. Assert there is exactly one constant and
    no stray literal caps left."""
    import re
    from pathlib import Path
    src = Path("routes/api/reports.py").read_text(encoding="utf-8")
    # every compare endpoint's length guard must reference the constant
    guards = re.findall(r"len\(server_(?:list|names)\)\s*>\s*(\S+?)[\):]", src)
    assert guards, "expected length guards in the compare endpoints"
    assert all(g == "MAX_COMPARE_SERVERS" for g in guards), (
        f"a hardcoded cap survived: {guards}")


def test_the_cap_reaches_the_template_from_a_single_source():
    """The frontend must receive the SAME number the API enforces.

    Since the comparison UI moved to a shared partial (2026-08-06) that any view
    may include, the cap is injected by the GLOBAL context processor rather than
    plumbed through one view — otherwise a second host page would silently get
    no cap and the '400 on All' bug would return by a different route.
    """
    from pathlib import Path
    app_src = Path("app.py").read_text(encoding="utf-8")
    assert "max_compare_servers" in app_src, \
        "the context processor must expose the cap to every template"
    assert "MAX_COMPARE_SERVERS" in app_src, \
        "and it must come from the API's constant, not a literal"

    tpl = Path("templates/partials/server_comparison.html").read_text(encoding="utf-8")
    assert "const MAX_COMPARE = {{ max_compare_servers" in tpl, \
        "partial must consume the injected cap, never a hardcoded number"


def test_comparison_partial_is_included_by_monitoring_not_reports():
    """The move itself: /monitoring hosts it, /reports no longer does."""
    from pathlib import Path
    mon = Path("templates/monitoring.html").read_text(encoding="utf-8")
    rep = Path("templates/reports.html").read_text(encoding="utf-8")
    assert "partials/server_comparison.html" in mon
    assert "partials/server_comparison.html" not in rep
    # and no orphaned comparison code left behind in reports
    for leftover in ("compare-server-checkboxes", "loadComparison",
                     "MAX_COMPARE", "CHART_COLORS"):
        assert leftover not in rep, f"{leftover} still in reports.html"
