"""Detection logic — the per-server status decision tree, the CPU
N-of-M smoothing gate, and the anomaly-event dispatcher.

This module exists because the same logic is consumed by BOTH the
legacy collector (``collector.collector_loop``) and the v2 aggregator
(``collector_v2.aggregator.AggregatorThread``). During the v1→v2
migration these helpers lived in ``collector.py`` and v2 reached back
into v1 with late imports; extracting them here flattens that
dependency and unblocks v1 retirement.

What lives here (and why):

  * ``_SEVERITY_RANK`` / ``_max_severity`` — the severity ordering
    used everywhere a worst-of-many decision is made.
  * ``METRIC_LABELS`` — the human-readable name for each metric key.
    Lives next to the dispatcher that uses it in alert message text.
  * ``_active_level_detector`` — picks which of baseline / anomaly /
    threshold owns warning-level cpu/ram/disk events. The single
    source of truth gate.
  * ``compute_status`` — the *threshold-only* decision: cpu/ram/disk
    vs per-server warning/critical numbers. Returns the ground-truth
    severity that smart detectors can elevate by at most one level.
  * ``_get_worst_metric`` — picks the worst metric for alert message
    construction. Pure read of metrics + thresholds.
  * ``_cpu_warn_history`` + ``_baseline_dev_history`` — the per-server
    rings backing N-of-M smoothing. The aggregator and analytics both
    read these.
  * ``_cpu_gate_record`` / ``_cpu_gate_passes`` — write + read of the
    CPU ring. Single-writer (called once per sample per server from
    ``_effective_status``); arbitrary readers downstream.
  * ``_effective_status`` — the full 6-phase decision tree that turns
    metrics + thresholds + smart-detector verdicts + the CPU gate +
    the raw-critical override into a single status string. Stored in
    ``metrics.status`` and read by every status badge.
  * ``dispatch_anomaly_events_v2`` — the per-sample anomaly +
    rate-anomaly event firing path. Honours suppression windows,
    acks/snoozes, and the CPU N-of-M gate.

What does NOT live here:

  * Maintenance window helpers — see ``maintenance.py``.
  * The PowerShell scripts and the per-server WinRM probe — see
    ``collector.py`` (v1) and ``collector_v2/checks.py`` (v2).
  * Baseline math itself — see ``baseline_engine.py``.
  * Rolling mean / sigma anomaly math — see ``analytics.py``.

Import direction is strictly upward: this module imports from
``analytics``, ``baseline_engine``, ``alert_scoring``, and (lazily)
``maintenance``. Nothing imports from this module that this module
imports from in the other direction, so the historical
``analytics → collector → analytics`` cycle is broken.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from analytics import (
    _get_current_segment,
    detect_anomalies,
    detect_rate_anomalies,
)
from alert_scoring import update_score_on_fire

logger = logging.getLogger("prism.detection")


# ─────────────────────────────────────────────────────────────────────
# Severity merging
# ─────────────────────────────────────────────────────────────────────
# Higher number = more severe. Lower-priority detectors can only RAISE
# the status, never lower it (an offline server stays offline even if
# baseline says CPU is fine).
_SEVERITY_RANK = {"healthy": 0, "warning": 1, "critical": 2, "offline": 3}


# ─────────────────────────────────────────────────────────────────────
# Metric label table (for alert message text)
# ─────────────────────────────────────────────────────────────────────
METRIC_LABELS = {
    "cpu": "CPU",
    "ram": "RAM",
    "disk_c": "Disk C:",
    "disk_d": "Disk D:",
}


# ─────────────────────────────────────────────────────────────────────
# CPU N-of-M gate state — per-server ring of recent CPU warning bits.
# `_cpu_gate_record` is the SINGLE WRITER (called from _effective_status
# once per sample per server). All other consumers must use
# `_cpu_gate_passes`, a pure read.
# ─────────────────────────────────────────────────────────────────────
_cpu_warn_history: dict[str, collections.deque] = {}

# Per-server, per-metric ring for sustained-baseline-deviation gating.
# v1 wrote here from collector_loop; v2's aggregator owns its own
# parallel ring. Kept here so legacy v1 code paths still find it
# during the deprecation period.
_baseline_dev_history: dict[str, dict[str, collections.deque]] = {}

# Per-server, per-metric ring for the STATIC-breach spike gate. A CPU or RAM
# value that crosses its threshold for one or two samples is a spike, not a
# condition — alarming on it is pure noise. The badge only flips once the breach
# has held for `spike_sustain_cycles` consecutive collector rounds.
#
# Written once per sample per server from evaluate_server (single writer), the
# same contract as _cpu_warn_history.
_static_spike_history: dict[str, dict[str, collections.deque]] = {}

# Only spiky, instantaneous metrics are gated. Disk is deliberately NOT: it moves
# monotonically, so a disk breach is real on the first sample and delaying it by
# five rounds would just slow down the one metric where filling up matters.
_SPIKE_GATED_METRICS = ("cpu", "ram")
_SPIKE_SUSTAIN_DEFAULT = 5


def _spike_sustain_cycles(settings: dict) -> int:
    """How many consecutive rounds a cpu/ram static breach must hold.

    1 disables the gate (alarm on the first sample, pre-2026-08-05 behaviour
    for RAM). Clamped to 1..20 so a fat-fingered value can't make a metric
    permanently unalarmable.
    """
    anom_cfg = (settings or {}).get("anomaly_detection", {}) or {}
    try:
        n = int(anom_cfg.get("spike_sustain_cycles", _SPIKE_SUSTAIN_DEFAULT))
    except (TypeError, ValueError):
        n = _SPIKE_SUSTAIN_DEFAULT
    return max(1, min(20, n))


def _cpu_gate_record(server_name: str, in_warning: bool, settings: dict) -> bool:
    """Push the current sample's CPU warning state into the history ring.

    Returns True if the gate currently passes (N-of-M holds). The
    ring's ``maxlen`` is resized if the operator changed
    ``cpu_warning_window_cycles`` — old data is discarded on resize.
    """
    anom_cfg = (settings or {}).get("anomaly_detection", {}) or {}
    window = max(1, int(anom_cfg.get("cpu_warning_window_cycles", 5)))
    consec = max(1, int(anom_cfg.get("cpu_warning_consecutive_cycles", 3)))
    history = _cpu_warn_history.get(server_name)
    if history is None or history.maxlen != window:
        history = collections.deque(maxlen=window)
        _cpu_warn_history[server_name] = history
    history.append(bool(in_warning))
    return sum(history) >= consec


def _cpu_gate_passes(server_name: str, settings: dict) -> bool:
    """Pure read — does the CPU warning gate currently allow events to fire?

    Returns True if the gate is disabled (consec ≤ 1), the history is
    empty (first warning gets through), or N-of-M holds. Safe to call
    from any thread; doesn't touch the ring.
    """
    anom_cfg = (settings or {}).get("anomaly_detection", {}) or {}
    consec = max(1, int(anom_cfg.get("cpu_warning_consecutive_cycles", 3)))
    if consec <= 1:
        return True  # smoothing disabled — fire on every sample
    history = _cpu_warn_history.get(server_name)
    if not history:
        return True  # no history yet — let the first warning through
    return sum(history) >= consec


def _max_severity(*statuses: str) -> str:
    """Return the worst status among the inputs (highest rank wins)."""
    worst = "healthy"
    for s in statuses:
        if s and _SEVERITY_RANK.get(s, 0) > _SEVERITY_RANK.get(worst, 0):
            worst = s
    return worst


def _active_level_detector(settings: dict) -> str:
    """Return the highest-priority enabled level detector:
    'baseline', 'anomaly' or 'threshold'.

    Used as a gate to ensure ONE detector owns warning-level cpu/ram/disk
    events at a time. Critical events always fire regardless of which
    detector is on top — that's a hard safety net.
    """
    if settings.get("baseline_detection", {}).get("enabled", True):
        return "baseline"
    if settings.get("anomaly_detection", {}).get("enabled", True):
        return "anomaly"
    return "threshold"


def compute_status(metrics: dict | None, thresholds: dict, server_name: str = "",
                   settings: dict | None = None) -> str:
    """Determine server status from metrics and thresholds. Worst metric wins.

    Maintenance windows can OVERRIDE thresholds for individual servers —
    this is checked via a late import to avoid a circular dependency on
    ``maintenance.py`` (which itself uses nothing from this module).

    NOTE: a single disk_warning/disk_critical pair applies to ALL disks on
    the server. Each collected drive is evaluated against the same shared
    threshold; missing drives (value None or < 0) are skipped.
    """
    if metrics is None:
        return "offline"

    # Maintenance-window threshold overrides — late import to keep the
    # detection ↔ maintenance dependency one-way at import time.
    if server_name and settings:
        try:
            from maintenance import _get_maintenance_thresholds
            maint_thresholds = _get_maintenance_thresholds(server_name, settings)
            if maint_thresholds:
                thresholds = {**thresholds, **maint_thresholds}
        except ImportError:
            # During very early bootstrap (e.g. tests that import
            # detection before maintenance exists) we just use the raw
            # thresholds. Production code paths always have both modules.
            pass

    values = {
        "cpu": (metrics.get("cpu"), thresholds.get("cpu_warning"), thresholds.get("cpu_critical")),
        "ram": (metrics.get("ram"), thresholds.get("ram_warning"), thresholds.get("ram_critical")),
        "disk_c": (metrics.get("disk_c"), thresholds.get("disk_warning"), thresholds.get("disk_critical")),
        "disk_d": (metrics.get("disk_d"), thresholds.get("disk_warning"), thresholds.get("disk_critical")),
    }

    # Check critical first
    for name, (value, warn, crit) in values.items():
        if value is not None and value >= 0 and crit is not None and value >= crit:
            return "critical"

    # Then warning
    for name, (value, warn, crit) in values.items():
        if value is not None and value >= 0 and warn is not None and value >= warn:
            return "warning"

    return "healthy"


def _get_worst_metric(metrics: dict | None, thresholds: dict) -> tuple[str | None, float | None, float | None]:
    """Find the metric most over its threshold. Returns (metric_name, value, threshold)
    or (None, None, None).

    "Most over" = largest positive excess above the FIRST threshold the
    metric crosses (critical preferred over warning when both fire).
    Used to construct alert messages — "the worst thing happening" gets
    surfaced first.
    """
    if metrics is None:
        return None, None, None

    worst_name = None
    worst_excess = -1
    worst_value = None
    worst_threshold = None

    checks = [
        ("cpu", metrics.get("cpu"), thresholds.get("cpu_critical"), thresholds.get("cpu_warning")),
        ("ram", metrics.get("ram"), thresholds.get("ram_critical"), thresholds.get("ram_warning")),
        ("disk_c", metrics.get("disk_c"), thresholds.get("disk_critical"), thresholds.get("disk_warning")),
        ("disk_d", metrics.get("disk_d"), thresholds.get("disk_critical"), thresholds.get("disk_warning")),
    ]

    for name, value, crit, warn in checks:
        if value is None or value < 0:
            continue
        if crit is not None and value >= crit:
            excess = value - crit
            if excess > worst_excess:
                worst_excess = excess
                worst_name, worst_value, worst_threshold = name, value, crit
        elif warn is not None and value >= warn:
            excess = value - warn
            if excess > worst_excess:
                worst_excess = excess
                worst_name, worst_value, worst_threshold = name, value, warn

    return worst_name, worst_value, worst_threshold


def _effective_status(db, server, metrics, threshold_status, settings):
    """Compute the server's EFFECTIVE status. The single source of truth.

    See ``docs/STATUS_FLOW.md`` for the audited decision tree. The phase
    ordering here is:

        Phase 0  OFFLINE short-circuit (never demote)
        Phase 1  threshold_status as ground truth (already computed by caller)
        Phase 2  smart detectors (baseline + anomaly) raise extra_severity
        Phase 3  severity cap — smart can elevate by AT MOST one level
        Phase 4  CPU N-of-M gate — demote brief CPU-only warnings
        Phase 5  raw-critical sanity check — no critical without a raw metric at crit

    Offline + Critical always win as safety nets. Maintenance-window
    threshold overrides are applied BEFORE this function runs (inside
    ``compute_status``).
    """
    # ═══ Phase 0: OFFLINE short-circuit ═══
    if not metrics or threshold_status == "offline":
        return threshold_status

    # ═══ Phase 2: Smart detectors ═══
    extra_severity = "healthy"
    baseline_cfg = settings.get("baseline_detection", {}) or {}
    anom_cfg = settings.get("anomaly_detection", {}) or {}

    # Build the metric dict in the shape baseline_engine expects
    bm = {
        "cpu_percent": metrics.get("cpu"),
        "ram_percent": metrics.get("ram"),
        "disk_c_percent": metrics.get("disk_c"),
        "disk_d_percent": metrics.get("disk_d"),
    }

    if baseline_cfg.get("enabled", True):
        try:
            from baseline_engine import check_deviation
            devs = check_deviation(
                db, server.name, bm,
                settings.get("timezone", "Europe/Berlin"),
                baseline_cfg.get("sigma_warning", 2.0),
                baseline_cfg.get("sigma_critical", 3.0),
                baseline_cfg.get("min_samples", 10),
            )
            for d in devs:
                extra_severity = _max_severity(extra_severity, d.get("severity", "warning"))
        except Exception:
            logger.debug("[%s] baseline check in _effective_status failed", server.name, exc_info=True)

    if anom_cfg.get("enabled", True):
        try:
            tz = settings.get("timezone", "Europe/Berlin")
            seg = _get_current_segment(tz)
            anomalies = detect_anomalies(db, server.name, {
                "cpu": metrics.get("cpu"),
                "ram": metrics.get("ram"),
                "disk_c": metrics.get("disk_c"),
                "disk_d": metrics.get("disk_d"),
            }, segment=seg, timezone_str=tz)
            # When baseline is active, anomaly only contributes low-side
            # (crash detection) — baseline owns high-side.
            low_only = (
                baseline_cfg.get("enabled", True)
                and anom_cfg.get("low_side_only_when_baseline_on", True)
            )
            for a in anomalies:
                if low_only and a.get("direction") != "below_baseline":
                    continue
                extra_severity = _max_severity(extra_severity, a.get("severity", "warning"))
        except Exception:
            logger.debug("[%s] anomaly check in _effective_status failed", server.name, exc_info=True)

    # ═══ Phase 3: Severity cap ═══
    # Smart detectors can elevate by at most ONE level above threshold_status.
    _CAP_MAP = {
        "healthy":  "warning",    # smart can push healthy → warning, no further
        "warning":  "critical",   # smart can push warning → critical
        "critical": "critical",   # already at max
        "offline":  "offline",    # offline is never contested
        "resolved": "resolved",
    }
    max_allowed = _CAP_MAP.get(threshold_status, "critical")
    if _SEVERITY_RANK.get(extra_severity, 0) > _SEVERITY_RANK.get(max_allowed, 2):
        extra_severity = max_allowed
    merged = _max_severity(threshold_status, extra_severity)

    # ═══ Phase 4: CPU N-of-M warning gate ═══
    # Filter brief CPU spikes. Only applies when:
    #   - merged is exactly "warning" (not critical or offline)
    #   - current CPU is below cpu_critical (safety net bypass)
    #   - CPU is the ONLY reason we're at warning (RAM/disk are healthy)
    #   - the N-of-M ring buffer doesn't have enough warning-state samples
    cpu_val = metrics.get("cpu")
    if cpu_val is not None and cpu_val >= 0:
        thresholds = server.thresholds or {}
        cpu_warn_thr = thresholds.get("cpu_warning", 75)
        cpu_crit_thr = thresholds.get("cpu_critical", 90)
        # ALWAYS record the current state — this is the single writer to history
        gate_open = _cpu_gate_record(server.name, cpu_val >= cpu_warn_thr, settings)

        if merged == "warning" and cpu_val < cpu_crit_thr and not gate_open:
            # Is CPU the only reason we're at warning?
            ram_val = metrics.get("ram") or -1
            disk_c_val = metrics.get("disk_c") or -1
            disk_d_val = metrics.get("disk_d") or -1
            ram_warn_thr = thresholds.get("ram_warning", 80)
            disk_warn_thr = thresholds.get("disk_warning", 80)
            ram_is_warning = ram_val >= 0 and ram_val >= ram_warn_thr
            disk_is_warning = (
                (disk_c_val >= 0 and disk_c_val >= disk_warn_thr)
                or (disk_d_val >= 0 and disk_d_val >= disk_warn_thr)
            )
            if not (ram_is_warning or disk_is_warning):
                logger.debug("[%s] CPU warning gated by N-of-M (cpu=%.1f)", server.name, cpu_val)
                merged = "healthy"

    # ═══ Phase 5: Raw-critical sanity check ═══
    # FINAL OVERRIDE: `critical` can ONLY be returned if at least one raw
    # metric is actually at or above its OWN per-server critical threshold.
    # Smart detectors firing "critical" on stale baseline data used to
    # bypass this. Never again.
    if merged == "critical":
        thresholds = server.thresholds or {}
        raw_critical = False
        checks = [
            ("cpu",    metrics.get("cpu"),    thresholds.get("cpu_critical", 90)),
            ("ram",    metrics.get("ram"),    thresholds.get("ram_critical", 90)),
            ("disk_c", metrics.get("disk_c"), thresholds.get("disk_critical", 90)),
            ("disk_d", metrics.get("disk_d"), thresholds.get("disk_critical", 90)),
        ]
        for _name, _val, _crit in checks:
            if _val is not None and _val >= 0 and _val >= _crit:
                raw_critical = True
                break
        if not raw_critical:
            logger.debug("[%s] CRITICAL demoted → warning (no raw metric at critical threshold)", server.name)
            merged = "warning"

    # ═══ Phase 6: Return ═══
    return merged


# ─────────────────────────────────────────────────────────────────────
# Fused verdict engine — docs/plans/DETECTION_FUSION_PLAN.md
#
# Replaces the compute_status → _effective_status pair for the v2
# aggregator. Three layers, evaluated per metric:
#
#   Layer 1  Exhaustion floors (settings.thresholds.exhaustion_*) —
#            the hard truth. A finite resource nearly gone is critical
#            no matter what any baseline says.
#   Layer 2  Static per-server thresholds. A baseline WITH AUTHORITY
#            (enough history + coverage + freshness) that says "this is
#            normal for this server" downgrades warning/critical to
#            healthy, flagged elevated_normal so the UI keeps a marker.
#   Layer 3  Deviation-from-self below the static thresholds raises
#            healthy → warning (sustained N-of-M, capped at warning).
#
# ``compute_status`` / ``_effective_status`` above remain for the v1
# collector and existing unit tests; v2 consumes ``evaluate_server``.
# ─────────────────────────────────────────────────────────────────────

_METRIC_THRESHOLD_KEYS = {
    "cpu": ("cpu_warning", "cpu_critical"),
    "ram": ("ram_warning", "ram_critical"),
    "disk_c": ("disk_warning", "disk_critical"),
    "disk_d": ("disk_warning", "disk_critical"),
}

# Which exhaustion floor governs each metric. CPU deliberately has NO
# floor — pegged CPU is workload, not a finite resource running out;
# static thresholds + the N-of-M gate own it (owner decision, plan §7).
_METRIC_FLOOR_KEYS = {
    "ram": "exhaustion_ram",
    "disk_c": "exhaustion_disk",
    "disk_d": "exhaustion_disk",
}
_FLOOR_DEFAULTS = {"exhaustion_ram": 98, "exhaustion_disk": 95}


@dataclass
class MetricVerdict:
    """Per-metric outcome of the fused evaluation."""
    metric: str
    value: float
    static_severity: str            # healthy|warning|critical (static zone)
    baseline_state: str             # off|no-authority|normal|deviating-high|deviating-low
    final_severity: str             # healthy|warning|critical
    elevated_normal: bool = False   # statically breached but baseline vouches
    reason: str = ""                # operator-readable justification
    threshold_used: float | None = None  # the number that drove the verdict
    deviation: dict | None = None   # check_deviation-shaped dict when deviating
    is_floor: bool = False          # severity came from the exhaustion floor (hard truth)
    # Which Layer-3 gate blocked a sustained deviation from raising the badge:
    # "" (not blocked) | "direction" | "authority" | "proximity". The deviation
    # itself is still recorded in ``deviation`` — this marks it as an
    # observation rather than an alarm, so the UI can show it without alerting.
    deviation_suppressed: str = ""
    # True when a STATIC threshold breach was held back because it hasn't lasted
    # spike_sustain_cycles rounds yet. The value really is over its threshold —
    # it just hasn't earned an alarm. Surfaced as a quiet observation, not amber.
    spike_gated: bool = False


@dataclass
class FusedVerdict:
    """One server's fused status + the per-metric evidence behind it."""
    status: str                     # healthy|warning|critical|offline
    metrics: dict[str, MetricVerdict] = field(default_factory=dict)

    def has_floor(self) -> bool:
        """True if any metric hit its exhaustion floor. The floor is a hard
        truth that must alert even when 'Thresholds (Simple)' is switched off."""
        return any(mv.is_floor for mv in self.metrics.values())

    def threshold_worst(self, status: str) -> "MetricVerdict | None":
        """The metric that should drive a THRESHOLD-transition event at
        ``status`` — i.e. one whose severity comes from a static breach or the
        exhaustion floor, NOT a pure deviation-from-self raise (that is owned
        by the baseline_deviation event path, so firing a threshold event too
        would duplicate it). Picked by largest excess over the threshold that
        drove it (matching v1's 'most over threshold' semantics), not raw value.
        Returns None when the status is purely deviation-driven.
        """
        cands = [mv for mv in self.metrics.values()
                 if mv.final_severity == status
                 and (mv.is_floor or mv.static_severity != "healthy")]
        if not cands:
            return None

        def _excess(mv: "MetricVerdict") -> float:
            if mv.threshold_used is not None:
                return mv.value - mv.threshold_used
            return mv.value
        return max(cands, key=_excess)

    def detail(self) -> dict:
        """JSON-safe ``{metric: {elevated, reason, kind, ...}}`` for UI caches.

        Only metrics with something to say. Healthy-and-boring is omitted so the
        cache row stays small.

        ``kind`` tells the UI how loudly to render the entry — the dashboard
        previously showed a bare severity badge with no reason at all, so an
        operator could not tell a real threshold breach from a statistical blip
        without opening the server:

          ``floor``      value hit an exhaustion floor — a hard truth
          ``breach``     value crossed its configured static threshold
          ``deviation``  static zone was healthy; Layer 3 raised it because the
                         value differs from this host's own baseline. Same
                         information, must read much quieter than a breach.
          ``elevated``   statically over threshold but the baseline vouches for
                         it (normal for this server) — renders as healthy
          ``suppressed`` a sustained deviation that a raise gate filtered out.
                         Carries ``gate`` (direction|authority|proximity).
                         Must render as an almost-silent observation, never an
                         alarm — the server still counts as healthy everywhere.
          ``spike``      a static cpu/ram breach that has not lasted
                         spike_sustain_cycles rounds yet. Genuinely over
                         threshold, but transient. Same quiet treatment as
                         ``suppressed``; the server counts as healthy.

        ``elevated`` and ``reason`` are unchanged for backwards compatibility.
        """
        out = {}
        for name, mv in self.metrics.items():
            if mv.elevated_normal:
                entry = {"elevated": True, "reason": mv.reason, "kind": "elevated"}
            elif mv.final_severity != "healthy" and mv.reason:
                if mv.is_floor:
                    kind = "floor"
                elif mv.static_severity == "healthy":
                    kind = "deviation"
                else:
                    kind = "breach"
                entry = {"elevated": False, "reason": mv.reason, "kind": kind,
                         "value": mv.value, "threshold": mv.threshold_used}
            elif mv.spike_gated and mv.reason:
                entry = {"elevated": False, "reason": mv.reason,
                         "kind": "spike", "gate": "sustain",
                         "value": mv.value}
            elif mv.deviation_suppressed and mv.reason:
                entry = {"elevated": False, "reason": mv.reason,
                         "kind": "suppressed", "gate": mv.deviation_suppressed}
            else:
                continue
            out[name] = entry
        return out

    def deviations(self) -> list[dict]:
        """check_deviation-shaped dicts for the baseline event pipeline."""
        return [mv.deviation for mv in self.metrics.values() if mv.deviation]

    def elevated_metrics(self) -> set[str]:
        return {n for n, mv in self.metrics.items() if mv.elevated_normal}


# Per-server, per-metric sustain ring backing the fused verdict's
# deviation-from-self raise. Single writer: ``evaluate_server`` (once per
# sample per server). The CPU static-warning gate reuses the legacy
# ``_cpu_warn_history`` ring so ``_cpu_gate_passes`` consumers (event
# dispatch, UI display filters) keep seeing the same state.
_fused_dev_history: dict[str, dict[str, collections.deque]] = {}

# Baseline downgrade-authority cache: (checked_at_epoch, knobs_sig, {metric: bool}).
# Coverage/span/freshness move slowly — re-checking every sample would be
# 3 queries × 30 servers × 1/min for data that changes daily. The knobs
# signature is part of the entry so that tightening min_coverage_pct /
# min_span_weeks / min_samples takes effect on the NEXT sample instead of
# lingering for up to the TTL (an operator raising the bar to unmask a real
# critical must not keep seeing the stale 'baseline vouches' result).
_authority_cache: dict[str, tuple[float, tuple, dict]] = {}
_AUTHORITY_TTL_S = 300.0


def _sustain_record(store: dict, server_name: str, metric: str,
                    hit: bool, window: int, need: int) -> bool:
    """Push one bit into a per-server/per-metric ring; True if N-of-M holds."""
    rings = store.setdefault(server_name, {})
    ring = rings.get(metric)
    if ring is None or ring.maxlen != window:
        ring = collections.deque(maxlen=window)
        rings[metric] = ring
    ring.append(bool(hit))
    return sum(ring) >= need


_DEV_GATE_DEFAULTS = {
    "deviation_direction": "high",
    "deviation_min_pct_of_warning": 80,
    "deviation_requires_authority": True,
}


def _deviation_may_raise(b_state: str, value: float, warn, authority_ok: bool,
                         baseline_cfg: dict) -> tuple[bool, str]:
    """May a Layer-3 deviation-from-self raise the badge to warning?

    Three independent gates, each individually configurable. Every one of them
    existed as a defect before: the old condition was simply ``deviating and
    dev_sustained and name not in acked``, which meant a fleet of 30 produced 9
    warnings where only 4 had a real threshold breach.

      A1 ``deviation_direction`` (high|both, default high)
          Only a deviation ABOVE the baseline may raise. A disk that is
          *emptier* than its learned normal is good news, not a fault, and it
          was raising amber badges. Direction was previously computed only to
          pick the wording ("above"/"below").

      A2 ``deviation_min_pct_of_warning`` (0-100, default 80)
          The value must have reached this percentage of the metric's own
          warning threshold. Without it, RAM at 38% warned because it is
          "usually 22%" while the warning threshold was 80% — contradicting the
          adopted principle that anomaly alone never pages
          (docs/plans/DETECTION_FUSION_PLAN.md §1). 0 disables the gate; a
          metric with no configured warning threshold is never blocked by it,
          since there is nothing to be proximate to.

      A3 ``deviation_requires_authority`` (default True)
          A raise now needs the same baseline maturity/coverage/freshness that
          a DOWNGRADE already required. Previously the baseline was trusted to
          create warnings from an immature or stale baseline but not to clear
          them — the asymmetry that turns a post-cleanup disk into an alert.

    Returns ``(may_raise, blocked_by)``; ``blocked_by`` names the first failing
    gate for logging, and is "" when the deviation is allowed through.

    Legacy behaviour is reproducible exactly with
    ``{"deviation_direction": "both", "deviation_min_pct_of_warning": 0,
    "deviation_requires_authority": False}`` — so this is reversible from the
    Monitoring page with no code change.
    """
    direction = str(baseline_cfg.get(
        "deviation_direction", _DEV_GATE_DEFAULTS["deviation_direction"])).strip().lower()
    if direction not in ("high", "both"):
        direction = _DEV_GATE_DEFAULTS["deviation_direction"]
    if direction == "high" and b_state != "deviating-high":
        return False, "direction"

    if baseline_cfg.get("deviation_requires_authority",
                        _DEV_GATE_DEFAULTS["deviation_requires_authority"]):
        if not authority_ok:
            return False, "authority"

    try:
        pct = float(baseline_cfg.get(
            "deviation_min_pct_of_warning",
            _DEV_GATE_DEFAULTS["deviation_min_pct_of_warning"]))
    except (TypeError, ValueError):
        pct = float(_DEV_GATE_DEFAULTS["deviation_min_pct_of_warning"])
    pct = max(0.0, min(100.0, pct))
    if pct > 0 and warn is not None:
        try:
            if value < float(warn) * pct / 100.0:
                return False, "proximity"
        except (TypeError, ValueError):
            pass

    return True, ""


def _baseline_authority(db, server_name: str, settings: dict) -> dict[str, bool]:
    """Per-metric: may the baseline DOWNGRADE static verdicts for this server?

    Authority requires ALL of (plan §2 "warm-up before power"):
      * baseline_detection.enabled and .allow_downgrade
      * metric history span ≥ min_span_weeks
      * baseline slots fresh (recomputed within 8 days)
      * ≥ min_coverage_pct of the 168 hour-of-week slots armed
    Raises (deviation-from-self) need only the per-slot min_samples bar,
    which ``assess_metrics`` already enforces — this gate is downgrade-only.
    """
    baseline_cfg = (settings or {}).get("baseline_detection", {}) or {}
    if not (baseline_cfg.get("enabled", True) and baseline_cfg.get("allow_downgrade", True)):
        return {}

    min_samples = int(baseline_cfg.get("min_samples", 10))
    min_cov = float(baseline_cfg.get("min_coverage_pct", 50))
    min_span_days = float(baseline_cfg.get("min_span_weeks", 2)) * 7.0
    knobs_sig = (min_samples, min_cov, min_span_days)

    now = time.time()
    cached = _authority_cache.get(server_name)
    if cached and cached[1] == knobs_sig and now - cached[0] < _AUTHORITY_TTL_S:
        return cached[2]

    out: dict[str, bool] = {}
    try:
        span_ok = db.get_metric_history_span_days(server_name) >= min_span_days
        age_h = db.get_baseline_age_hours(server_name)
        fresh_ok = age_h is not None and age_h <= 8 * 24.0
        if span_ok and fresh_ok:
            cov = db.get_baseline_coverage(server_name, min_samples=min_samples)
            for metric, c in (cov.get("metrics", {}) or {}).items():
                total = c.get("total") or 168
                pct = 100.0 * c.get("covered", 0) / total
                out[metric] = pct >= min_cov
    except Exception:
        logger.debug("[%s] baseline authority check failed", server_name, exc_info=True)
        out = {}

    _authority_cache[server_name] = (now, knobs_sig, out)
    return out


def evaluate_server(db, server, metrics: dict | None, settings: dict) -> FusedVerdict:
    """Compute the fused three-layer verdict for one server sample.

    The single status decision for the v2 pipeline: the aggregator stores
    ``verdict.status`` in ``metrics.status``, fires transition events from
    it, and threads the per-metric verdicts to the UI cache and the
    baseline event path so every consumer agrees by construction.
    """
    if metrics is None:
        return FusedVerdict(status="offline")

    settings = settings or {}
    thresholds = dict(server.thresholds or {})
    # Maintenance-window overrides — same rule (and same late import) as
    # compute_status, so both entry points loosen identically in-window.
    try:
        from maintenance import _get_maintenance_thresholds
        maint = _get_maintenance_thresholds(server.name, settings)
        if maint:
            thresholds.update(maint)
    except ImportError:
        pass

    thr_cfg = settings.get("thresholds", {}) or {}
    baseline_cfg = settings.get("baseline_detection", {}) or {}
    anom_cfg = settings.get("anomaly_detection", {}) or {}
    window = max(1, int(anom_cfg.get("cpu_warning_window_cycles", 5)))
    dev_need = max(1, int(baseline_cfg.get("min_cycles_warning", 3)))

    # Hour-of-week baseline assessment for every metric (the sole smart
    # engine voting on status — the rolling analytics detector stays an
    # insights/event stream, plan §2 "engine consolidation").
    assessments: dict[str, dict] = {}
    if baseline_cfg.get("enabled", True):
        try:
            from baseline_engine import assess_metrics
            assessments = assess_metrics(
                db, server.name,
                {
                    "cpu_percent": metrics.get("cpu"),
                    "ram_percent": metrics.get("ram"),
                    "disk_c_percent": metrics.get("disk_c"),
                    "disk_d_percent": metrics.get("disk_d"),
                },
                settings.get("timezone", "Europe/Berlin"),
                baseline_cfg.get("sigma_warning", 2.0),
                baseline_cfg.get("sigma_critical", 3.0),
                baseline_cfg.get("min_samples", 10),
            )
        except Exception:
            logger.debug("[%s] baseline assessment failed", server.name, exc_info=True)
    authority = _baseline_authority(db, server.name, settings) if assessments else {}

    # Acked/snoozed metrics never ELEVATE the verdict (fixes the "snoozed
    # metric still yellows the UI" split); downgrades ignore acks.
    try:
        acks = db.get_active_acknowledgments(server.name)
        acked = {a.get("metric") for a in acks
                 if a.get("ack_type") in ("acknowledged", "snoozed")}
    except Exception:
        acked = set()

    verdicts: dict[str, MetricVerdict] = {}
    for name, (warn_key, crit_key) in _METRIC_THRESHOLD_KEYS.items():
        raw = metrics.get(name)
        if raw is None or raw < 0:
            continue
        value = float(raw)
        warn = thresholds.get(warn_key)
        crit = thresholds.get(crit_key)

        static_sev = "healthy"
        if crit is not None and value >= crit:
            static_sev = "critical"
        elif warn is not None and value >= warn:
            static_sev = "warning"

        floor = None
        floor_key = _METRIC_FLOOR_KEYS.get(name)
        if floor_key:
            try:
                floor = float(thr_cfg.get(floor_key, _FLOOR_DEFAULTS[floor_key]))
            except (TypeError, ValueError):
                floor = float(_FLOOR_DEFAULTS[floor_key])

        a = assessments.get(name)
        if not baseline_cfg.get("enabled", True):
            b_state = "off"
        elif not a or a["state"] == "no-slot":
            b_state = "no-authority"
        elif a["state"] == "deviating":
            b_state = "deviating-high" if a["direction"] == "high" else "deviating-low"
        elif authority.get(name):
            b_state = "normal"
        else:
            b_state = "no-authority"   # slot says normal but baseline too young/stale/sparse

        # ── Sustain rings (recorded EVERY sample — single writer) ──
        # A static breach on CPU or RAM must hold for spike_sustain_cycles
        # CONSECUTIVE rounds before it flips the badge (window == need, so a
        # single good sample resets it). Both warning and critical are gated —
        # a one-sample jump to 95% CPU is noise at either severity. The
        # exhaustion floor is NOT gated: it is evaluated before this in Layer 1
        # and stays instant, so genuine resource exhaustion still pages at once.
        #
        # Disk is excluded on purpose (see _SPIKE_GATED_METRICS): it climbs
        # monotonically, so gating it would only delay the one metric where
        # running out actually matters.
        in_warn_zone = warn is not None and value >= warn
        # The legacy CPU ring remains the single writer for _cpu_gate_passes
        # consumers (analytics event-display filter, per-server ack reset), so it
        # is still fed every sample even though the verdict now uses the spike
        # gate below. Its return value is intentionally unused here.
        if name == "cpu":
            _cpu_gate_record(server.name, in_warn_zone, settings)
        if name in _SPIKE_GATED_METRICS:
            spike_need = _spike_sustain_cycles(settings)
            warn_gate_open = _sustain_record(
                _static_spike_history, server.name, name,
                in_warn_zone, spike_need, spike_need)
        else:
            spike_need = 1
            warn_gate_open = True
        # Deviation-from-self is a NEW signal → require it sustained before
        # it raises the badge, for every metric.
        deviating = b_state.startswith("deviating")
        dev_sustained = _sustain_record(
            _fused_dev_history, server.name, name, deviating, window, dev_need)

        label = METRIC_LABELS.get(name, name)
        mv = MetricVerdict(metric=name, value=value, static_severity=static_sev,
                           baseline_state=b_state, final_severity=static_sev)

        if floor is not None and value >= floor:
            # ── Layer 1: exhaustion floor — the hard truth ──
            mv.final_severity = "critical"
            mv.is_floor = True
            mv.threshold_used = floor
            mv.reason = f"{label} {value:.0f}% at exhaustion floor ({floor:.0f}%)"
        elif static_sev in ("warning", "critical"):
            # ── Layer 2: static zone — baseline with authority may vouch ──
            if b_state == "normal":
                mv.final_severity = "healthy"
                mv.elevated_normal = True
                mv.threshold_used = a["baseline_avg"]
                mv.reason = (f"{label} {value:.0f}% — normal for this server "
                             f"(baseline {a['baseline_avg']:.0f}% ± {a['baseline_stddev']:.0f})")
            elif not warn_gate_open:
                # Brief cpu/ram spike — over threshold, but not for long enough
                # to be a condition. Applies to critical as well as warning; the
                # exhaustion floor above is never gated. Recorded as an
                # observation so the UI can show it quietly instead of silently
                # discarding a real (if short-lived) breach.
                mv.final_severity = "healthy"
                mv.spike_gated = True
                mv.reason = (f"{label} {value:.0f}% over threshold but not sustained "
                             f"({spike_need} rounds required) — not alerting")
                logger.debug("[%s] %s %s spike gated (%.1f, needs %d rounds)",
                             server.name, name, static_sev, value, spike_need)
            else:
                thr_used = crit if static_sev == "critical" else warn
                mv.threshold_used = float(thr_used) if thr_used is not None else None
                dev_note = " and deviates from its baseline" if deviating else ""
                mv.reason = f"{label} exceeded {thr_used:.0f}% ({value:.0f}%){dev_note}"
        else:
            # ── Layer 3: below static thresholds — deviation-from-self ──
            if deviating and dev_sustained and name not in acked:
                may_raise, blocked_by = _deviation_may_raise(
                    b_state, value, warn, bool(authority.get(name)), baseline_cfg)
                direction_word = "above" if b_state == "deviating-high" else "below"
                if may_raise:
                    mv.final_severity = "warning"    # capped: never critical alone
                    mv.threshold_used = a["baseline_avg"]
                    mv.reason = (f"{label} {value:.0f}% is {direction_word} its baseline "
                                 f"({a['baseline_avg']:.0f}% ± {a['baseline_stddev']:.0f})")
                else:
                    # Stays healthy, but the deviation is NOT discarded: mv.deviation
                    # is still populated below, so the UI can show it as an
                    # observation instead of an alarm. Silently dropping it is how
                    # trust in a monitoring tool dies.
                    mv.deviation_suppressed = blocked_by
                    # Observation wording, deliberately different from the alarm
                    # wording above ("differs from" not "is above/below"), so the
                    # tooltip cannot be mistaken for a warning. Safe to set on a
                    # healthy metric: every mv.reason consumer in the aggregator is
                    # gated on final_severity != healthy or elevated_normal, so this
                    # never reaches event dispatch or an alert message.
                    mv.reason = (f"{label} {value:.0f}% differs from its usual "
                                 f"{a['baseline_avg']:.0f}% — not alerting")
                    logger.debug(
                        "[%s] %s deviation %s baseline suppressed by %s gate "
                        "(value=%.1f warn=%s authority=%s)",
                        server.name, name, direction_word, blocked_by,
                        value, warn, bool(authority.get(name)))

        if deviating and a:
            mv.deviation = {
                "metric": name,
                "value": value,
                "baseline_avg": a["baseline_avg"],
                "baseline_stddev": a["baseline_stddev"],
                "deviation_sigma": a["deviation_sigma"],
                "severity": a["severity"],
                "direction": a["direction"],
            }
        verdicts[name] = mv

    overall = "healthy"
    for mv in verdicts.values():
        overall = _max_severity(overall, mv.final_severity)
    return FusedVerdict(status=overall, metrics=verdicts)


def dispatch_anomaly_events_v2(db, server, metrics: dict, settings: dict) -> int:
    """Run anomaly + rate-anomaly detection for ONE server's metric result
    and dispatch events. Returns the number of events fired.

    Called by ``collector_v2.aggregator`` once per sample per server.
    Detection runs on every sample — the expensive DB read inside
    ``detect_anomalies`` is memoised by the analytics baseline cache
    (see ``analytics.py`` module docstring) so the marginal cost per
    cached sample is pure arithmetic.

    Alert storms are prevented by the suppression-window + ack/snooze
    layers downstream, not by skipping detection itself. Operators who
    want quieter alerts should raise ``anomaly_detection.suppression_hours``.

    Args:
        db: Database instance
        server: ServerConfig
        metrics: dict with cpu/ram/disk_c/disk_d
        settings: full settings dict

    Returns:
        Count of events fired (for caller's logging / event correlation).
    """
    if metrics is None:
        return 0

    anom_cfg = settings.get("anomaly_detection", {})
    if not anom_cfg.get("enabled", True):
        return 0

    level_detector = _active_level_detector(settings)
    anom_low_only = (
        level_detector == "baseline"
        and anom_cfg.get("low_side_only_when_baseline_on", True)
    )

    events_fired = 0

    # ── Statistical anomaly detection (per-segment baseline+sigma) ─────
    try:
        tz = settings.get("timezone", "Europe/Berlin")
        current_segment = _get_current_segment(tz)
        anomalies = detect_anomalies(db, server.name, {
            "cpu": metrics.get("cpu"),
            "ram": metrics.get("ram"),
            "disk_c": metrics.get("disk_c"),
            "disk_d": metrics.get("disk_d"),
        }, segment=current_segment, timezone_str=tz)
        for anomaly in anomalies:
            direction = anomaly.get("direction", "above_baseline")
            # Priority gate: baseline owns high-side, anomaly owns low-side
            if anom_low_only and direction != "below_baseline":
                continue
            # CPU N-of-M gate: warning-level CPU anomalies need sustained
            if (anomaly.get("metric") == "cpu"
                    and anomaly.get("severity") == "warning"
                    and not _cpu_gate_passes(server.name, settings)):
                continue
            # Acknowledged/snoozed?
            acks = db.get_active_acknowledgments(server.name, anomaly["metric"])
            if acks:
                continue
            # Suppression window
            suppression = db.get_anomaly_suppression(server.name, anomaly["metric"], direction)
            if suppression:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    last_time = _dt.fromisoformat(suppression["last_alert_time"].replace("Z", "+00:00"))
                    hours_since = (_dt.now(_tz.utc) - last_time).total_seconds() / 3600.0
                    SUPPRESSION_WINDOW_HOURS = float(anom_cfg.get("suppression_hours", 4))
                    sev_order = {"warning": 1, "critical": 2}
                    last_sev = sev_order.get(suppression["last_severity"], 0)
                    curr_sev = sev_order.get(anomaly.get("severity"), 0)
                    if (hours_since < SUPPRESSION_WINDOW_HOURS
                            and curr_sev <= last_sev
                            and abs(anomaly["value"] - suppression["last_value"]) < anomaly.get("stddev", 5.0)):
                        continue
                except (ValueError, TypeError):
                    pass
            # Fire the event
            label = METRIC_LABELS.get(anomaly["metric"], anomaly["metric"])
            if direction == "below_baseline":
                msg = (f"{label} below baseline: {anomaly['value']}% "
                       f"(expected {anomaly['mean']}% +/- {anomaly['stddev']}%, "
                       f"{anomaly['deviation_percent']}% below normal)")
            else:
                msg = (f"{label} anomaly: {anomaly['value']}% "
                       f"(expected {anomaly['mean']}% +/- {anomaly['stddev']}%, "
                       f"{anomaly['deviation_percent']}% above normal)")
            db.insert_event(
                server.name, "anomaly", anomaly["metric"],
                anomaly["value"], anomaly["mean"], msg
            )
            try:
                update_score_on_fire(db, server.name, anomaly["metric"], "anomaly")
            except Exception:
                pass
            db.upsert_anomaly_suppression(
                server.name, anomaly["metric"], direction,
                anomaly.get("severity", "warning"), anomaly["value"]
            )
            events_fired += 1
        if anomalies:
            logger.info("[%s] v2 detected %d anomalies, fired %d events",
                        server.name, len(anomalies), events_fired)
    except Exception:
        logger.exception("[%s] v2 anomaly detection failed", server.name)

    # ── Rate-of-change anomaly (opt-in) ─────────────────────────────────
    if anom_cfg.get("rate_detection_enabled", False):
        try:
            rate_anomalies = detect_rate_anomalies(db, server.name, {
                "cpu": metrics.get("cpu"),
                "ram": metrics.get("ram"),
                "disk_c": metrics.get("disk_c"),
                "disk_d": metrics.get("disk_d"),
            })
            for ra in rate_anomalies:
                if (ra.get("metric") == "cpu"
                        and ra.get("severity", "warning") == "warning"
                        and not _cpu_gate_passes(server.name, settings)):
                    continue
                rate_direction = f"rate_{ra['direction']}"
                suppression = db.get_anomaly_suppression(server.name, ra["metric"], rate_direction)
                if suppression:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        last_time = _dt.fromisoformat(suppression["last_alert_time"].replace("Z", "+00:00"))
                        hours_since = (_dt.now(_tz.utc) - last_time).total_seconds() / 3600.0
                        if hours_since < float(anom_cfg.get("rate_suppression_hours", 2)):
                            continue
                    except (ValueError, TypeError):
                        pass
                label = METRIC_LABELS.get(ra["metric"], ra["metric"])
                direction_text = "accelerating" if ra["direction"] == "accelerating" else "decelerating"
                msg = (f"{label} rate anomaly ({direction_text}): "
                       f"{ra['rate']:+.2f}%/interval "
                       f"(normal: {ra['mean_rate']:.2f} ± {ra['rate_stddev']:.2f})")
                db.insert_event(
                    server.name, "rate_anomaly", ra["metric"],
                    ra["rate"], ra["mean_rate"], msg
                )
                db.upsert_anomaly_suppression(
                    server.name, ra["metric"], rate_direction,
                    ra["severity"], ra["rate"]
                )
                events_fired += 1
        except Exception:
            logger.exception("[%s] v2 rate anomaly detection failed", server.name)

    return events_fired
