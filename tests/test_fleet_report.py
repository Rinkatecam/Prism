"""Fleet report (analytics.compute_fleet_report) — see docs/plans/FLEET_REPORT_SPEC.md.

The report replaces the Reports page's SLA and Capacity sections, which led
with availability. Measured over 720 h on the 29-server instance, 28 of 29
servers sat inside a 0.73-POINT availability band while health ranged
1.03-100% with 15 of 29 below 90%. Availability cannot rank a fleet; health
can.

Three of the tests here exist because of specific defects recorded in
docs/plans/HANDOFF.md §3:

  * ``test_field_name_contract`` — two defects were a renderer reading a field
    the API never emitted (``days_until_full`` for ``days_to_threshold``,
    ``trend_slope`` for ``trend_per_day``). Both produced a silent em-dash or
    an ``Infinity`` sort key. Nothing failed; nobody noticed.
  * ``test_equivalence_with_compute_uptime_stats`` — the single-scan path must
    agree with the per-server path it replaces, or the ``timeline=`` refactor
    silently changed the numbers.
  * ``test_attention_rule_discriminates`` — the rule originally shipped with
    two clauses that, measured, selected 29 of 29 and 0 of 29. A filter that
    matches everything and one that matches nothing are the same bug.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from analytics import (
    ATTENTION_AVAILABILITY_FLOOR,
    ATTENTION_HEALTH_FLOOR,
    compute_fleet_report,
    compute_uptime_stats,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVAL = 60  # seconds per reading


class Cfg:
    """Stand-in for models.ServerConfig — only name + thresholds are read."""

    def __init__(self, name, thresholds=None):
        self.name = name
        self.thresholds = thresholds or {
            "cpu_warning": 75, "cpu_critical": 90,
            "ram_warning": 80, "ram_critical": 90,
            "disk_warning": 75, "disk_critical": 90,
        }


def _insert(db, server, readings, start_minutes_ago=None):
    """Insert readings at explicit, strictly increasing timestamps.

    ``insert_metric`` stamps rows with 'now', which ties when several land in
    the same second and makes ORDER BY timestamp non-deterministic — that would
    make the outage-run and sparkline assertions flaky rather than wrong.

    Each reading is ``(status, cpu, ram, disk_c, disk_d)``. Rows are placed one
    minute apart ending shortly before now, so they fall inside any window the
    tests use.
    """
    n = len(readings)
    if start_minutes_ago is None:
        start_minutes_ago = n + 5
    conn = db._get_conn()
    for i, (status, cpu, ram, disk_c, disk_d) in enumerate(readings):
        conn.execute(
            """INSERT INTO metrics
               (server_name, timestamp, cpu_percent, ram_percent,
                disk_c_percent, disk_d_percent, status)
               VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now',?), ?, ?, ?, ?, ?)""",
            (server, f"-{start_minutes_ago - i} minutes", cpu, ram, disk_c, disk_d, status),
        )
    conn.commit()


def _report(db, cfgs, **kw):
    kw.setdefault("hours", 720)
    kw.setdefault("poll_interval_seconds", INTERVAL)
    kw.setdefault("scope", "all")
    return compute_fleet_report(db, cfgs, **kw)


def _server(report, name):
    return next(s for s in report["servers"] if s["name"] == name)


# ── the attention rule, clause by clause ─────────────────────────────────

def test_attention_health_clause_fires_alone(tmp_db):
    """Reachable the whole time, degraded half of it."""
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 10
                         + [("warning", 80, 10, 10, 10)] * 10)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["health_percent"] == 50.0
    assert s["availability_percent"] == 100.0
    assert s["attention"] is True
    assert s["attention_reasons"] == ["health"]


def test_attention_availability_clause_fires_alone(tmp_db):
    """Perfectly healthy when up, but down more than 1% of observed time."""
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 90
                         + [("offline", None, None, None, None)] * 10)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["health_percent"] == 100.0
    assert s["availability_percent"] == 90.0
    assert s["attention_reasons"] == ["availability"]


def test_attention_capacity_clause_fires_alone(tmp_db):
    """Healthy and available, but disk C is climbing toward the threshold.

    The clause must be able to fire on its own — a server that is fine right
    now and full in a month is exactly what health alone cannot surface.
    """
    readings = [("healthy", 10, 10, 40 + i * 0.4, 10) for i in range(60)]
    _insert(tmp_db, "SRV", readings)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["health_percent"] == 100.0
    assert s["availability_percent"] == 100.0
    assert s["attention_reasons"] == ["capacity"]
    assert any(c["risk"] in ("high", "medium") for c in s["capacity"])


def test_quiet_server_is_not_flagged(tmp_db):
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 20.0, 10)] * 60)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["attention"] is False
    assert s["attention_reasons"] == []


def test_attention_rule_discriminates(tmp_db):
    """Guards the regression in FLEET_REPORT_SPEC §4.1.

    ``outage_count >= 1`` selected 29 of 29 on the live fleet and
    ``capacity risk == high`` selected 0 of 29. Both were replaced. A rule that
    selects everything or nothing is not a filter, so a mixed fleet must land
    strictly between the two.
    """
    _insert(tmp_db, "SICK", [("critical", 95, 10, 10, 10)] * 40)
    _insert(tmp_db, "FINE1", [("healthy", 10, 10, 20.0, 10)] * 40)
    _insert(tmp_db, "FINE2", [("healthy", 12, 11, 21.0, 10)] * 40)
    cfgs = [Cfg("SICK"), Cfg("FINE1"), Cfg("FINE2")]

    report = _report(tmp_db, cfgs)
    flagged = report["fleet"]["servers_needing_attention"]

    assert 0 < flagged < len(cfgs), f"rule selected {flagged} of {len(cfgs)}"
    assert _server(report, "SICK")["attention"] is True


def test_attention_floors_are_the_documented_ones():
    """The spec quotes these numbers; a silent change would invalidate §4."""
    assert ATTENTION_HEALTH_FLOOR == 90.0
    assert ATTENTION_AVAILABILITY_FLOOR == 99.0


# ── equivalence with the path it replaces ────────────────────────────────

def test_equivalence_with_compute_uptime_stats(tmp_db):
    """One fleet-wide scan must produce what 29 per-server reads produced.

    compute_fleet_report slices a single query and passes each slice to
    compute_uptime_stats(timeline=...). If that refactor drifted, availability
    and health would move without anything failing.
    """
    _insert(tmp_db, "A", [("healthy", 10, 10, 10, 10)] * 20
                       + [("warning", 80, 10, 10, 10)] * 5
                       + [("offline", None, None, None, None)] * 3
                       + [("healthy", 10, 10, 10, 10)] * 12)
    _insert(tmp_db, "B", [("critical", 95, 95, 95, 95)] * 30)

    report = _report(tmp_db, [Cfg("A"), Cfg("B")])

    for name in ("A", "B"):
        got = _server(report, name)
        ref = compute_uptime_stats(tmp_db, name, hours=720,
                                   poll_interval_seconds=INTERVAL)
        for key in ("health_percent", "availability_percent", "outage_count",
                    "total_downtime_minutes", "mttr_minutes",
                    "observed_minutes", "up_minutes", "down_minutes",
                    "degraded_minutes", "longest_outage_minutes"):
            assert got[key] == ref[key], f"{name}.{key}"


# ── degradation attribution ──────────────────────────────────────────────

def test_drivers_attribute_the_metric_furthest_over_threshold(tmp_db):
    """RAM at +15 beats CPU at +5, even though both are over."""
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 10
                         + [("warning", 80, 95, 10, 10)] * 10)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert [d["metric"] for d in s["drivers"]] == ["ram"]
    d = s["drivers"][0]
    assert d["readings"] == 10
    assert d["percent_of_degraded"] == 100.0
    assert d["avg_value"] == 95.0
    assert d["threshold"] == 80
    assert d["avg_excess"] == 15.0
    assert s["no_threshold_breach"]["readings"] == 0


def test_degradation_with_no_threshold_crossed_is_not_attributed(tmp_db):
    """The APP01 case: degraded, but no static threshold explains it.

    Live, that server is 96% unexplained by thresholds — its warnings come from
    baseline_deviation, anomaly and security checks. Guessing a metric here
    would be confidently wrong for roughly a third of the fleet, so these
    readings must land in no_threshold_breach and nowhere else.
    """
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 10
                         + [("warning", 20, 20, 20, 20)] * 10)
    tmp_db.insert_event("SRV", "warning", "baseline_deviation", 20.0, 4.0, "drift")
    tmp_db.insert_event("SRV", "anomaly", "ram", 20.0, None, "spike")

    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["drivers"] == []
    nb = s["no_threshold_breach"]
    assert nb["readings"] == 10
    assert nb["percent_of_degraded"] == 100.0
    assert nb["event_counts"] == {"baseline_deviation": 1, "ram": 1}


def test_driver_tie_breaks_on_declaration_order(tmp_db):
    """CPU and RAM equally over — cpu is declared first, so cpu wins."""
    _insert(tmp_db, "SRV", [("warning", 85, 90, 10, 10)] * 4)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert [d["metric"] for d in s["drivers"]] == ["cpu"]


def test_driver_percentages_are_of_degraded_not_of_window(tmp_db):
    """20 healthy + 10 ram-degraded → ram is 100% of DEGRADED, not 33%."""
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 20
                         + [("warning", 10, 95, 10, 10)] * 10)
    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["drivers"][0]["percent_of_degraded"] == 100.0
    assert s["degraded_percent_of_up"] == pytest.approx(33.3, abs=0.1)


# ── "no data" is not zero ────────────────────────────────────────────────

def test_server_with_no_rows_yields_null_not_zero(tmp_db):
    """A server nobody has polled is not 0% healthy and not 100% available."""
    report = _report(tmp_db, [Cfg("GHOST")])
    s = _server(report, "GHOST")

    assert s["health_percent"] is None
    assert s["availability_percent"] is None
    assert s["degraded_percent_of_up"] is None
    assert s["has_data"] is False
    assert s["attention"] is False, "no data must not be treated as a problem"
    assert report["fleet"]["servers_no_data"] == 1


def test_no_data_server_sorts_last(tmp_db):
    _insert(tmp_db, "SICK", [("critical", 95, 10, 10, 10)] * 20)
    _insert(tmp_db, "OK", [("healthy", 10, 10, 20.0, 10)] * 20)

    report = _report(tmp_db, [Cfg("GHOST"), Cfg("OK"), Cfg("SICK")])

    assert [s["name"] for s in report["servers"]][-1] == "GHOST"
    assert report["servers"][0]["name"] == "SICK"


def test_rows_for_a_server_absent_from_config_are_not_reported(tmp_db):
    """STANDALONE01 was deleted on 2026-08-05 but its rows remain in `metrics`.

    The config list is authoritative, matching /api/sla/summary. A deleted host
    must not reappear in the report just because history survives retention.
    """
    _insert(tmp_db, "DELETED", [("healthy", 10, 10, 10, 10)] * 10)
    _insert(tmp_db, "LIVE", [("healthy", 10, 10, 10, 10)] * 10)

    report = _report(tmp_db, [Cfg("LIVE")])

    assert [s["name"] for s in report["servers"]] == ["LIVE"]
    assert report["fleet"]["servers_total"] == 1


# ── payload shape ────────────────────────────────────────────────────────

def test_scope_attention_stubs_the_rest(tmp_db):
    """The default scope is what bounds the payload at 500 servers."""
    _insert(tmp_db, "SICK", [("critical", 95, 10, 10, 10)] * 20)
    _insert(tmp_db, "FINE", [("healthy", 10, 10, 20.0, 10)] * 20)
    cfgs = [Cfg("SICK"), Cfg("FINE")]

    report = _report(tmp_db, cfgs, scope="attention")
    sick, fine = _server(report, "SICK"), _server(report, "FINE")

    assert sick["attention"] is True
    assert "drivers" in sick and "capacity" in sick and "health_sparkline" in sick

    assert fine["attention"] is False
    assert set(fine) == {"name", "attention", "health_percent", "availability_percent"}
    # The stub still carries availability so the band can compute its spread.
    assert fine["availability_percent"] is not None


def test_scope_all_returns_full_rows_for_everyone(tmp_db):
    _insert(tmp_db, "FINE", [("healthy", 10, 10, 20.0, 10)] * 20)

    fine = _server(_report(tmp_db, [Cfg("FINE")], scope="all"), "FINE")

    assert fine["attention"] is False
    assert "drivers" in fine and "capacity" in fine


def test_outages_top_is_capped_at_five_and_holds_the_longest(tmp_db):
    """outage_count still counts them all; only the array is bounded."""
    readings = []
    for length in (1, 2, 3, 4, 5, 6, 7):
        readings += [("offline", None, None, None, None)] * length
        readings += [("healthy", 10, 10, 10, 10)] * 3
    _insert(tmp_db, "SRV", readings)

    s = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")

    assert s["outage_count"] == 7
    assert len(s["outages_top"]) == 5
    durations = [o["duration_minutes"] for o in s["outages_top"]]
    assert durations == sorted(durations, reverse=True)
    assert durations[0] == 7.0, "the longest outage must survive the cap"


def test_sparkline_uses_equal_count_buckets_of_up_readings(tmp_db):
    """Equal-count, not equal-time — the instance runs at a ~7.8% duty cycle.

    Bucketing 720 h of wall-clock would leave a sparkline ~92% empty, which
    reads as an outage rather than as "not sampled".
    """
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 24
                         + [("warning", 95, 10, 10, 10)] * 24
                         + [("offline", None, None, None, None)] * 50)

    spark = _server(_report(tmp_db, [Cfg("SRV")]), "SRV")["health_sparkline"]

    assert len(spark) == 24, "down readings must not occupy buckets"
    assert spark[0] == 100.0
    assert spark[-1] == 0.0


def test_sparkline_is_empty_when_never_up(tmp_db):
    _insert(tmp_db, "SRV", [("offline", None, None, None, None)] * 10)
    assert _server(_report(tmp_db, [Cfg("SRV")]), "SRV")["health_sparkline"] == []


# ── fleet roll-up ────────────────────────────────────────────────────────

def test_fleet_worst_server_is_worst_by_health_not_availability(tmp_db):
    """The report leads with health, so "worst" must mean worst health.

    Overloading one field with two meanings is how the old page ended up
    ranking by the number that does not move.
    """
    # DEGRADED: always reachable, mostly degraded.
    _insert(tmp_db, "DEGRADED", [("critical", 95, 10, 10, 10)] * 38
                              + [("healthy", 10, 10, 10, 10)] * 2)
    # FLAPPY: healthy whenever up, but down a fifth of the time.
    _insert(tmp_db, "FLAPPY", [("healthy", 10, 10, 10, 10)] * 32
                            + [("offline", None, None, None, None)] * 8)

    fleet = _report(tmp_db, [Cfg("DEGRADED"), Cfg("FLAPPY")])["fleet"]

    assert fleet["worst_server"] == "DEGRADED"
    assert fleet["worst_availability_server"] == "FLAPPY"


def test_fleet_totals_add_up(tmp_db):
    _insert(tmp_db, "A", [("healthy", 10, 10, 10, 10)] * 10
                       + [("warning", 95, 10, 10, 10)] * 10)
    _insert(tmp_db, "B", [("healthy", 10, 10, 10, 10)] * 20)

    report = _report(tmp_db, [Cfg("A"), Cfg("B")])
    fleet = report["fleet"]

    assert fleet["servers_total"] == 2
    assert fleet["servers_counted"] == 2
    assert fleet["total_observed_minutes"] == 40.0
    assert fleet["total_degraded_minutes"] == 10.0
    # 30 healthy minutes of 40 up minutes.
    assert fleet["fleet_health_percent"] == 75.0


# ── contracts that a unit test cannot see ────────────────────────────────

def _collect_keys(node, out):
    """Every dict key appearing anywhere in the response, recursively."""
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            _collect_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_keys(v, out)
    return out


def test_field_name_contract(tmp_db):
    """Every API field templates/reports.html reads must actually be emitted.

    docs/plans/HANDOFF.md §3 records two defects of exactly this shape: the
    renderer read ``days_until_full`` while the API emitted
    ``days_to_threshold``, and read ``trend_slope`` while the API emitted
    ``trend_per_day``. Neither raised — one produced a permanent em-dash, the
    other an Infinity sort key. The page therefore reads every field through a
    single ``const F = {...}`` map, which this test extracts and checks.
    """
    template = (PROJECT_ROOT / "templates" / "reports.html").read_text(encoding="utf-8")
    block = re.search(r"const F = \{(.*?)\n\};", template, re.S)
    assert block, "templates/reports.html must declare the `const F = {...}` field map"

    declared = set(re.findall(r":\s*'([a-z_]+)'", block.group(1)))
    assert len(declared) > 25, f"only found {len(declared)} field names — regex drifted?"

    # A fleet rich enough to populate every optional structure: degraded time
    # with a driver, degraded time without one, an outage, and a disk trend.
    readings = [("healthy", 10, 10, 40 + i * 0.4, 10) for i in range(20)]
    readings += [("warning", 10, 95, 60.0, 10)] * 10      # ram over threshold
    readings += [("warning", 10, 10, 60.0, 10)] * 5       # nothing over threshold
    readings += [("offline", None, None, None, None)] * 3
    readings += [("healthy", 10, 10, 60.0, 10)] * 5
    _insert(tmp_db, "SRV", readings)
    tmp_db.insert_event("SRV", "warning", "baseline_deviation", 1.0, 0.0, "drift")

    report = _report(tmp_db, [Cfg("SRV")], scope="all")

    srv = _server(report, "SRV")
    assert srv["drivers"], "fixture must produce at least one driver"
    assert srv["no_threshold_breach"]["readings"] > 0
    assert srv["capacity"], "fixture must produce capacity rows"
    assert srv["outages_top"], "fixture must produce an outage"

    emitted = _collect_keys(report, set())
    missing = sorted(declared - emitted)
    assert not missing, f"reports.html reads fields the API never emits: {missing}"


def test_fleet_queries_do_not_use_datetime_now():
    """``datetime('now')`` renders with a SPACE separator while timestamps are
    stored with a 'T'. Since ``'T' > ' '`` and the comparison is TEXT, a WHERE
    clause against it silently over-reads by up to a calendar day — measured at
    3.74x on a 2-hour window. All call sites use strftime; these two are new.
    """
    source = (PROJECT_ROOT / "database.py").read_text(encoding="utf-8")
    for method in ("get_fleet_metrics_window", "get_fleet_event_counts"):
        start = source.index(f"def {method}(")
        body = source[start:start + 2000]
        assert "datetime('now'" not in body, f"{method} must not use datetime('now')"
        assert "strftime('%Y-%m-%dT%H:%M:%SZ','now'" in body, \
            f"{method} must bound its window with strftime"


def test_report_is_json_serialisable(tmp_db):
    """The endpoint jsonify()s this directly — no stray sets, Decimals or rows."""
    _insert(tmp_db, "SRV", [("healthy", 10, 10, 10, 10)] * 10
                         + [("warning", 95, 10, 10, 10)] * 5)
    json.dumps(_report(tmp_db, [Cfg("SRV")]))
