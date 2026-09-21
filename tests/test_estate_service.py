"""The estate service — snapshot building + the stateful fold wiring.

WP-1 phase 2 wiring. `estate_service` is the seam between the collector's
hot cache and the pure `estate_fold`:

  * `build_snapshot(...)` is PURE — cache + config + settings → the
    (servers, services) lists fold() consumes. All the "read a role off a
    config, a status off the cache, is-it-in-maintenance" glue lives here,
    tested without a collector.
  * `recompute(...)` / `tick(...)` / `current_verdict()` wrap it with the
    module-scope FoldState under a lock — the aggregator calls recompute
    at merge, the supervisor calls tick to move timers, views reads the
    verdict. `now` is always injected.

The pre-band GATES are the reader's job, not the fold's, and they live in
`display_severity(...)`: nothing configured → idle, configured but nothing
measured → unmeasured, everything measured is down → flat. Only past all
three do the weighted bands (calm/elevated/urgent) apply. This preserves
the `unmeasured` severity shipped earlier — the fold must never run on an
estate that has not been measured.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import estate_service                    # noqa: E402
from estate_service import build_snapshot, display_severity     # noqa: E402


class _Srv:
    def __init__(self, name, type="file_server", criticality=""):
        self.name = name
        self.type = type
        self.criticality = criticality


def _cache(**status_by_name):
    return {n: {"status": s} for n, s in status_by_name.items()}


# ── build_snapshot: the glue, pure ────────────────────────────────────────

def test_snapshot_resolves_role_and_weight_from_config():
    servers = [_Srv("DC1", type="domain_controller"),
               _Srv("F1", type="file_server"),
               _Srv("P1", type="print_server")]
    cache = _cache(DC1="offline", F1="healthy", P1="healthy")
    snap, _services = build_snapshot(servers, cache, {}, {}, {})
    by = {s["name"]: s for s in snap}
    assert by["DC1"]["role"] == "critical_infrastructure" and by["DC1"]["weight"] == 10
    assert by["F1"]["role"] == "important" and by["F1"]["weight"] == 4
    assert by["P1"]["role"] == "background" and by["P1"]["weight"] == 1


def test_snapshot_marks_maintenance_from_settings():
    servers = [_Srv("S1")]
    cache = _cache(S1="offline")
    # An ad-hoc window far in the future keeps S1 in maintenance.
    settings = {"maintenance_windows": [
        {"servers": ["S1"], "expires_at": "2099-01-01T00:00:00Z"}],
        "timezone": "UTC"}
    snap, _ = build_snapshot(servers, cache, settings, {}, {})
    assert snap[0]["in_maintenance"] is True


def test_snapshot_carries_the_latched_worst_when_present():
    """The latch's worst-recent state (from flap_state, phase-2 persistence)
    reaches the fold through the snapshot, not through the cache."""
    servers = [_Srv("S1")]
    cache = _cache(S1="healthy")            # bounced back up…
    snap, _ = build_snapshot(servers, cache, {}, {"S1": "offline"}, {})
    assert snap[0]["latched_worst"] == "offline"


def test_snapshot_status_defaults_to_unknown_when_not_yet_cached():
    """A configured server with no cache row yet is unknown — contributes
    nothing and reads as unmeasured, never as a silent healthy."""
    servers = [_Srv("S1"), _Srv("S2")]
    cache = _cache(S1="healthy")            # S2 has not reported
    snap, _ = build_snapshot(servers, cache, {}, {}, {})
    by = {s["name"]: s for s in snap}
    assert by["S2"]["status"] == "unknown"


def test_snapshot_builds_services_from_the_health_summary():
    services = build_snapshot([], {}, {}, {},
                              {"per_check": [{"name": "IIS", "server": "W1",
                                              "status": "down"}]})[1]
    assert services and services[0]["status"] == "down"
    assert services[0]["weight"] >= 1


# ── display_severity: the pre-band gates wrap the fold band ───────────────

def test_nothing_configured_is_idle():
    assert display_severity(band="calm", server_count=0,
                            measured=0, down=0, monitored=0) == "idle"


def test_configured_but_nothing_measured_is_unmeasured():
    assert display_severity(band="calm", server_count=29,
                            measured=0, down=0, monitored=29) == "unmeasured"


def test_everything_measured_is_down_is_flat():
    """ok==0 and something is down — the owner's flatline case, tested
    BEFORE the weighted bands so 'everything offline' reads as a flatline,
    not as the fastest possible beat."""
    assert display_severity(band="urgent", server_count=3,
                            measured=3, down=3, monitored=3) == "flat"


def test_a_measured_estate_shows_the_fold_band():
    assert display_severity(band="elevated", server_count=29,
                            measured=29, down=0, monitored=29) == "elevated"
    assert display_severity(band="urgent", server_count=29,
                            measured=29, down=1, monitored=29) == "urgent"


def test_one_survivor_is_not_flat():
    """The negative control for the flat gate: one healthy host out of 30
    is an estate in trouble, not a dead one — the band shows through."""
    assert display_severity(band="urgent", server_count=30,
                            measured=30, down=29, monitored=30) == "urgent"


# ── the stateful wrapper ──────────────────────────────────────────────────

def test_recompute_then_read_round_trips_a_verdict():
    estate_service.reset()
    servers = [_Srv("DC1", type="domain_controller"), _Srv("F1")]
    cache = _cache(DC1="offline", F1="healthy")
    estate_service.recompute(servers, cache, {}, {}, {}, now=1000.0)
    v = estate_service.current_verdict(now=1000.0)
    assert v is not None
    assert v["severity"] == "urgent"        # DC down → rail → urgent
    assert v["rail"] and "DC1" in v["rail"]
    assert v["bpm"] == 132


def test_the_reader_survives_a_cold_cache():
    """Before the first recompute, the reader returns None and the caller
    falls back to its own query path — never a crash, never a fake calm."""
    estate_service.reset()
    assert estate_service.current_verdict(now=1000.0) is None


def test_tick_advances_the_dwell_without_a_recompute():
    """Recovery must ease down on the supervisor clock even if no new
    Result arrives — otherwise a recovered estate with a quiet collector
    would hold urgent forever."""
    estate_service.reset()
    # A DC plus a healthy survivor, so DC-down is urgent (rail), NOT flat —
    # the flat gate correctly owns a fleet where EVERYTHING measured is down,
    # and a one-server-down fleet is exactly that.
    fleet = [_Srv("DC1", type="domain_controller"), _Srv("F1")]
    estate_service.recompute(fleet, _cache(DC1="offline", F1="healthy"),
                             {}, {}, {}, now=0.0)
    assert estate_service.current_verdict(now=0.0)["severity"] == "urgent"
    # DC recovers; recompute sees calm but the dwell holds…
    estate_service.recompute(fleet, _cache(DC1="healthy", F1="healthy"),
                             {}, {}, {}, now=5.0, fresh_counts={"DC1": 2})
    assert estate_service.current_verdict(now=5.0)["severity"] == "urgent"
    # …the tick moves the clock past the dwell and it releases.
    estate_service.tick(now=200.0, fresh_counts={"DC1": 2})
    assert estate_service.current_verdict(now=200.0)["severity"] == "calm"


def test_fleet_freshness_gates_the_dwell_via_cache_timestamps():
    """The PRODUCTION path — no explicit fresh_counts. The dwell must not
    release until the cache's newest metric timestamp has advanced twice
    since it opened, proving the collector is alive rather than the cache
    being frozen at a stale sample."""
    estate_service.reset()
    fleet = [_Srv("DC1", type="domain_controller"), _Srv("F1")]

    def cache(dc, t):
        return {"DC1": {"status": dc, "timestamp": t},
                "F1": {"status": "healthy", "timestamp": t}}

    estate_service.recompute(fleet, cache("offline", "t0"), {}, {}, {}, now=0.0)
    assert estate_service.current_verdict(now=0.0)["severity"] == "urgent"

    # DC recovers, dwell opens. Time passes WELL past 120s, but the cache
    # timestamp is frozen at t1 — a stale/dead collector. Must NOT release.
    estate_service.recompute(fleet, cache("healthy", "t1"), {}, {}, {}, now=10.0)
    estate_service.recompute(fleet, cache("healthy", "t1"), {}, {}, {}, now=300.0)
    assert estate_service.current_verdict(now=300.0)["severity"] == "urgent", (
        "released on a frozen cache — the freshness guard did nothing")

    # Now fresh samples arrive: two distinct newer timestamps. Release.
    estate_service.recompute(fleet, cache("healthy", "t2"), {}, {}, {}, now=305.0)
    estate_service.recompute(fleet, cache("healthy", "t3"), {}, {}, {}, now=310.0)
    assert estate_service.current_verdict(now=310.0)["severity"] == "calm"


def test_the_verdict_payload_shape_is_frozen_for_WP2():
    """WP-2 (the heart monitor) consumes this dict. The keys are a
    contract: band/severity, bpm, rail, contributors, why_not_higher,
    score. Freezing the shape here means WP-2 cannot start against a
    moving target."""
    estate_service.reset()
    estate_service.recompute([_Srv("F1")], _cache(F1="warning"), {}, {}, {},
                             now=1.0)
    v = estate_service.current_verdict(now=1.0)
    assert set(v) >= {"severity", "bpm", "rail", "contributors",
                      "why_not_higher", "score", "ok", "monitored"}


def test_a_stale_verdict_expires_rather_than_being_trusted():
    """A verdict from a collector that stopped publishing must not be
    rendered as a confident answer. The supervisor recomputes every 5s, so
    anything older than the TTL means it is dead, wedged, or never started —
    and the reader must fall back to its own live count-fold instead of
    showing a calm estate nobody is watching. Same stale-evidence principle
    as the dwell's freshness guard, one layer up."""
    estate_service.reset()
    fleet = [_Srv("DC1", type="domain_controller"), _Srv("F1")]
    estate_service.recompute(fleet, _cache(DC1="offline", F1="healthy"),
                             {}, {}, {}, now=1000.0)
    assert estate_service.current_verdict(now=1000.0) is not None
    assert estate_service.current_verdict(now=1050.0) is not None   # 50s: fresh
    assert estate_service.current_verdict(now=1200.0) is None, (
        "a 200s-old verdict was still served — the collector could be dead")


def test_a_served_verdict_reports_its_own_age():
    """The age travels with the payload so a consumer (WP-2's heart, a
    future health endpoint) can show 'as of 12s ago' rather than implying
    the number is live."""
    estate_service.reset()
    estate_service.recompute([_Srv("F1")], _cache(F1="warning"), {}, {}, {},
                             now=500.0)
    v = estate_service.current_verdict(now=512.0)
    assert v["age_s"] == 12.0
