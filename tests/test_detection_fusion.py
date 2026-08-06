"""Truth-table tests for the fused verdict engine — ``detection.evaluate_server``.

Derived from docs/plans/DETECTION_FUSION_PLAN.md §2 (the per-metric truth
table), NOT from the implementation — the point is to pin the intended
semantics so a future refactor that changes behaviour fails loudly.

The three layers under test:
  1. Exhaustion floors (settings.thresholds.exhaustion_*) — hard truth.
  2. Static thresholds, downgradable by a baseline WITH AUTHORITY.
  3. Deviation-from-self raises (below static thresholds), sustained N-of-M.

A ``FakeDB`` stands in for the SQLite layer so these stay fast, pure and
CI-safe (no config.json, no real DB). Each test resets the module-level
per-server rings/caches so ordering can't leak state between cases.
"""

from __future__ import annotations

import pytest

import detection
from detection import evaluate_server


# ── Fixtures / doubles ────────────────────────────────────────────────

DB_THRESHOLDS = {
    "cpu_warning": 75, "cpu_critical": 90,
    "ram_warning": 70, "ram_critical": 85,
    "disk_warning": 80, "disk_critical": 90,
}


class Srv:
    def __init__(self, name="s", thresholds=None):
        self.name = name
        self.thresholds = thresholds or dict(DB_THRESHOLDS)


class FakeDB:
    """Configurable stand-in.

    ``slots`` maps metric short-name -> (avg, stddev, sample_count) for the
    current hour-of-week baseline slot. ``span_days`` / ``age_h`` / ``cov_pct``
    drive the downgrade-authority gate. ``acks`` is a list of ack dicts.
    """

    def __init__(self, slots=None, span_days=99.0, age_h=1.0, cov_pct=100.0, acks=None):
        self.slots = slots or {}
        self.span_days = span_days
        self.age_h = age_h
        self.cov_pct = cov_pct
        self.acks = acks or []

    def get_baseline(self, server, metric, how):
        if metric in self.slots:
            avg, sd, n = self.slots[metric]
            return {"avg_value": avg, "stddev": sd, "sample_count": n}
        return None

    def get_metric_history_span_days(self, server):
        return self.span_days

    def get_baseline_age_hours(self, server):
        return self.age_h

    def get_baseline_coverage(self, server, min_samples=10):
        covered = int(168 * self.cov_pct / 100)
        return {"metrics": {m: {"covered": covered, "total": 168} for m in self.slots}}

    def get_active_acknowledgments(self, server, metric=None):
        return self.acks


def _settings(**over):
    s = {
        "timezone": "Europe/Berlin",
        "thresholds": {"enabled": True, "exhaustion_ram": 98, "exhaustion_disk": 95},
        "baseline_detection": {
            "enabled": True, "allow_downgrade": True, "min_samples": 10,
            "min_span_weeks": 2, "min_coverage_pct": 50,
            "sigma_warning": 2.0, "sigma_critical": 3.0, "min_cycles_warning": 3,
        },
        "anomaly_detection": {
            "enabled": True,
            "cpu_warning_window_cycles": 5, "cpu_warning_consecutive_cycles": 3,
            # Spike gate DISABLED for this file on purpose. These tests exercise
            # the fusion layers (floors, static zones, baseline authority,
            # downgrade, threshold_worst) from a SINGLE sample. The production
            # default is 5 consecutive rounds, which would make every one-sample
            # breach here read as healthy and test nothing about fusion.
            # The gate's own behaviour is covered in tests/test_spike_gate.py.
            "spike_sustain_cycles": 1,
        },
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k] = {**s[k], **v}
        else:
            s[k] = v
    return s


@pytest.fixture(autouse=True)
def _reset_rings():
    """Clear the per-server rings + authority cache before every test."""
    detection._fused_dev_history.clear()
    detection._cpu_warn_history.clear()
    detection._authority_cache.clear()
    yield
    detection._fused_dev_history.clear()
    detection._cpu_warn_history.clear()
    detection._authority_cache.clear()


def _metrics(cpu=40, ram=40, disk_c=40, disk_d=40):
    return {"cpu": cpu, "ram": ram, "disk_c": disk_c, "disk_d": disk_d}


# ── 0. Offline / backward-compat (no baselines) ───────────────────────

def test_offline_when_metrics_none():
    v = evaluate_server(FakeDB(), Srv(), None, _settings())
    assert v.status == "offline"


def test_all_healthy_no_baseline():
    v = evaluate_server(FakeDB(), Srv(), _metrics(), _settings())
    assert v.status == "healthy"
    assert v.detail() == {}


def test_static_critical_stands_without_baseline():
    # RAM 88 >= ram_critical 85, no baseline slot -> critical (as today)
    v = evaluate_server(FakeDB(), Srv(), _metrics(ram=88), _settings())
    assert v.status == "critical"
    assert v.metrics["ram"].elevated_normal is False


def test_ram_warning_is_instant_when_the_spike_gate_is_off():
    """RAM 75 >= ram_warning 70 -> warning on the first sample.

    NOTE: this is no longer the shipped default. Since 2026-08-05 a cpu/ram
    breach must hold for anomaly_detection.spike_sustain_cycles consecutive
    rounds (default 5) before the badge flips, because one-sample spikes were
    generating noise. This file sets the gate to 1, so what's pinned here is
    the ungated static path. The 5-round default is covered by
    tests/test_spike_gate.py::test_breach_is_gated_until_the_fifth_round.
    """
    v = evaluate_server(FakeDB(), Srv(), _metrics(ram=75), _settings())
    assert v.status == "warning"


# ── 1. Exhaustion floors (hard truth) ─────────────────────────────────

def test_ram_floor_overrides_vouching_baseline():
    # RAM 99 >= floor 98 -> critical even though baseline says 97 is normal
    db = FakeDB(slots={"ram": (97.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=99), _settings())
    assert v.status == "critical"
    assert v.metrics["ram"].elevated_normal is False
    assert "floor" in v.metrics["ram"].reason.lower()


def test_disk_floor_at_95():
    db = FakeDB(slots={"disk_c": (94.0, 1.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(disk_c=96), _settings())
    assert v.status == "critical"


def test_cpu_has_no_floor():
    # CPU 99 with a vouching baseline stays downgraded — CPU has no floor.
    db = FakeDB(slots={"cpu": (97.0, 3.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(cpu=99), _settings())
    assert v.status == "healthy"
    assert v.metrics["cpu"].elevated_normal is True


# ── 2. Static zone, baseline may vouch (the SQL01 case) ──────────────

def test_sql01_ram_downgraded_by_baseline():
    # RAM 93 >= critical 85, but baseline 90±2 (authority) says normal.
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)})
    v = evaluate_server(db, Srv("sql01"), _metrics(cpu=10, ram=93), _settings())
    assert v.status == "healthy"
    mv = v.metrics["ram"]
    assert mv.elevated_normal is True
    assert "normal for this server" in mv.reason
    assert v.detail()["ram"]["elevated"] is True


def test_baseline_no_authority_young_span():
    # Same slot, but only 5 days of history (< 2 weeks) -> no authority.
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)}, span_days=5.0)
    v = evaluate_server(db, Srv(), _metrics(ram=93), _settings())
    assert v.status == "critical"
    assert v.metrics["ram"].elevated_normal is False


def test_baseline_no_authority_low_coverage():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)}, cov_pct=20.0)
    v = evaluate_server(db, Srv(), _metrics(ram=93), _settings())
    assert v.status == "critical"


def test_baseline_no_authority_stale_recalc():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)}, age_h=9 * 24.0)  # > 8 days
    v = evaluate_server(db, Srv(), _metrics(ram=93), _settings())
    assert v.status == "critical"


def test_allow_downgrade_master_switch_off():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=93),
                        _settings(baseline_detection={"allow_downgrade": False}))
    assert v.status == "critical"


def test_deviating_metric_not_downgraded():
    # Baseline slot exists but current value deviates far from it -> the
    # baseline does NOT vouch; static critical stands.
    db = FakeDB(slots={"ram": (50.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=93), _settings())
    assert v.status == "critical"
    assert v.metrics["ram"].elevated_normal is False


# ── 3. Deviation-from-self (below static thresholds) ──────────────────

def test_cpu_deviation_raises_after_sustained():
    # Baseline 10±3, current 60 (< warn 75). Needs 3-of-5 sustained.
    db = FakeDB(slots={"cpu": (10.0, 3.0, 60)})
    srv = Srv("web")
    v1 = evaluate_server(db, srv, _metrics(cpu=60), _settings())
    assert v1.status == "healthy"          # 1st sample not yet sustained
    evaluate_server(db, srv, _metrics(cpu=60), _settings())
    v3 = evaluate_server(db, srv, _metrics(cpu=60), _settings())
    assert v3.status == "warning"          # 3rd -> sustained
    assert v3.metrics["cpu"].final_severity == "warning"


def test_deviation_capped_at_warning_never_critical():
    # A huge below-threshold deviation still only reaches warning.
    db = FakeDB(slots={"cpu": (5.0, 2.0, 90)})
    srv = Srv("web")
    for _ in range(5):
        v = evaluate_server(db, srv, _metrics(cpu=70), _settings())
    assert v.status == "warning"


def test_deviation_suppressed_when_acked():
    db = FakeDB(slots={"cpu": (10.0, 3.0, 60)},
               acks=[{"metric": "cpu", "ack_type": "acknowledged"}])
    srv = Srv("web")
    for _ in range(5):
        v = evaluate_server(db, srv, _metrics(cpu=60), _settings())
    assert v.status == "healthy"           # acked metric never elevates


# ── 4. CPU static-warning N-of-M gate is CPU-only ─────────────────────

def test_cpu_warning_gated_single_sample():
    """One CPU sample over threshold must not flip the badge.

    This file disables the spike gate by default (see _settings), so the gate is
    switched back on here explicitly — verdict-level cpu/ram smoothing is the
    spike gate's job as of 2026-08-05.
    """
    v = evaluate_server(FakeDB(), Srv(), _metrics(cpu=80),
                        _settings(anomaly_detection={"spike_sustain_cycles": 5}))
    assert v.status == "healthy"           # 80 >= warn 75 but not sustained


def test_cpu_warning_fires_when_sustained():
    srv = Srv()
    for _ in range(3):
        v = evaluate_server(FakeDB(), srv, _metrics(cpu=80), _settings())
    assert v.status == "warning"


def test_cpu_critical_bypasses_gate():
    # 92 >= cpu_critical 90 -> critical instantly, no N-of-M gate.
    v = evaluate_server(FakeDB(), Srv(), _metrics(cpu=92), _settings())
    assert v.status == "critical"


# ── 5. verdict_detail contract ────────────────────────────────────────

def test_detail_omits_boring_healthy():
    v = evaluate_server(FakeDB(), Srv(), _metrics(), _settings())
    assert v.detail() == {}


def test_deviations_shape_for_event_pipeline():
    db = FakeDB(slots={"ram": (50.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=93), _settings())
    devs = v.deviations()
    assert devs and devs[0]["metric"] == "ram"
    assert {"metric", "value", "baseline_avg", "baseline_stddev",
            "deviation_sigma", "severity", "direction"} <= set(devs[0])


# ── 6. baseline disabled -> pure static ───────────────────────────────

def test_baseline_disabled_is_pure_static():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=93),
                        _settings(baseline_detection={"enabled": False}))
    assert v.status == "critical"          # no downgrade when engine off
    assert v.metrics["ram"].elevated_normal is False


# ── 7. maintenance override loosens thresholds ────────────────────────

def test_offline_shortcircuits_before_metrics():
    v = evaluate_server(FakeDB(), Srv(), None, _settings())
    assert v.status == "offline"
    assert v.metrics == {}


# ── 8. FusedVerdict.threshold_worst / has_floor (review fixes) ─────────

def test_has_floor_flag():
    db = FakeDB(slots={"ram": (97.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=99), _settings())  # floor
    assert v.has_floor() is True
    assert v.metrics["ram"].is_floor is True


def test_threshold_worst_excludes_pure_deviation():
    # A deviation-raised warning (static healthy) must NOT drive a threshold
    # event — the baseline_deviation path owns it. threshold_worst -> None.
    db = FakeDB(slots={"cpu": (10.0, 3.0, 60)})
    srv = Srv("web")
    for _ in range(3):
        v = evaluate_server(db, srv, _metrics(cpu=60), _settings())
    assert v.status == "warning"
    assert v.metrics["cpu"].static_severity == "healthy"   # pure deviation
    assert v.threshold_worst("warning") is None


def test_threshold_worst_picks_floor():
    db = FakeDB(slots={"ram": (97.0, 2.0, 60)})
    v = evaluate_server(db, Srv(), _metrics(ram=99), _settings())
    worst = v.threshold_worst("critical")
    assert worst is not None and worst.metric == "ram" and worst.is_floor


def test_threshold_worst_by_excess_not_raw_value():
    # cpu 79 (warn 60 -> 19 over) vs disk_c 82 (warn 80 -> 2 over): the
    # more-over-threshold metric (cpu) must win, not the higher raw value.
    thr = dict(DB_THRESHOLDS, cpu_warning=60, cpu_critical=95)
    srv = Srv(thresholds=thr)
    # sustain CPU so its N-of-M gate opens (3 samples)
    for _ in range(3):
        v = evaluate_server(FakeDB(), srv, _metrics(cpu=79, disk_c=82), _settings())
    assert v.status == "warning"
    worst = v.threshold_worst("warning")
    assert worst is not None and worst.metric == "cpu"   # 19 over beats 2 over


# ── 9. Authority cache invalidates on a knob change (fix #3) ───────────

def test_authority_cache_invalidated_on_coverage_change():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)}, cov_pct=60.0)
    srv = Srv("sql01")
    m = _metrics(ram=93)
    # min_coverage_pct=50, coverage 60% -> authority -> downgrade to healthy
    v1 = evaluate_server(db, srv, m, _settings(baseline_detection={"min_coverage_pct": 50}))
    assert v1.status == "healthy" and v1.metrics["ram"].elevated_normal
    # Operator tightens to 100%; 60% < 100% -> authority lost immediately,
    # NOT after the 300s TTL. Static critical must stand on the very next call.
    v2 = evaluate_server(db, srv, m, _settings(baseline_detection={"min_coverage_pct": 100}))
    assert v2.status == "critical"
    assert v2.metrics["ram"].elevated_normal is False
