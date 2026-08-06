"""Tests for ``detection.compute_status`` — F-002 remediation.

The threshold-based status decision tree is the single source of truth
for "what status is this server in?" used by every alert, every
dashboard tile, every status-change event. Until now it was only
exercised indirectly through aggregator tests. This file pins each
input class directly.

The 6-phase decision tree lives in ``_effective_status``; this file
tests the pure threshold layer (``compute_status``), which is the
ground-truth Phase 1 input.
"""

from __future__ import annotations

import pytest

from detection import compute_status


def _thresholds(cpu_w=80, cpu_c=90, ram_w=80, ram_c=90, disk_w=80, disk_c=90):
    return {
        "cpu_warning": cpu_w, "cpu_critical": cpu_c,
        "ram_warning": ram_w, "ram_critical": ram_c,
        "disk_warning": disk_w, "disk_critical": disk_c,
    }


# ── 1. offline path ───────────────────────────────────────────────────

def test_offline_when_metrics_is_none():
    """No metrics dict means the collector got no answer — offline."""
    assert compute_status(None, _thresholds()) == "offline"


# ── 2. healthy when everything is below warning ───────────────────────

def test_healthy_when_all_metrics_below_warning():
    metrics = {"cpu": 50, "ram": 60, "disk_c": 50, "disk_d": 70}
    assert compute_status(metrics, _thresholds()) == "healthy"


def test_healthy_when_metric_exactly_zero():
    """Boundary case: a value of 0 must still produce healthy."""
    metrics = {"cpu": 0, "ram": 0, "disk_c": 0, "disk_d": 0}
    assert compute_status(metrics, _thresholds()) == "healthy"


# ── 3. warning when one metric crosses warning ────────────────────────

def test_warning_when_cpu_crosses_warning():
    metrics = {"cpu": 85, "ram": 50, "disk_c": 50, "disk_d": 50}
    assert compute_status(metrics, _thresholds()) == "warning"


def test_warning_when_ram_crosses_warning():
    metrics = {"cpu": 50, "ram": 85, "disk_c": 50, "disk_d": 50}
    assert compute_status(metrics, _thresholds()) == "warning"


def test_warning_at_exact_threshold_boundary():
    """``>=`` semantics: at the exact warning value, status flips."""
    metrics = {"cpu": 80, "ram": 50, "disk_c": 50, "disk_d": 50}
    assert compute_status(metrics, _thresholds(cpu_w=80)) == "warning"


# ── 4. critical when one metric crosses critical ──────────────────────

def test_critical_when_cpu_crosses_critical():
    metrics = {"cpu": 95, "ram": 50, "disk_c": 50, "disk_d": 50}
    assert compute_status(metrics, _thresholds()) == "critical"


def test_critical_at_exact_threshold_boundary():
    """``>=`` semantics: at the exact critical value, status is critical."""
    metrics = {"cpu": 90, "ram": 50, "disk_c": 50, "disk_d": 50}
    assert compute_status(metrics, _thresholds(cpu_c=90)) == "critical"


def test_critical_wins_over_warning_on_different_metrics():
    """If one metric is warning and another is critical, critical wins."""
    metrics = {"cpu": 85, "ram": 95, "disk_c": 50, "disk_d": 50}  # cpu=warn, ram=crit
    assert compute_status(metrics, _thresholds()) == "critical"


# ── 5. multiple disks share a single threshold ────────────────────────

def test_disk_c_drives_status():
    metrics = {"cpu": 10, "ram": 10, "disk_c": 95, "disk_d": 10}
    assert compute_status(metrics, _thresholds()) == "critical"


def test_disk_d_drives_status():
    metrics = {"cpu": 10, "ram": 10, "disk_c": 10, "disk_d": 95}
    assert compute_status(metrics, _thresholds()) == "critical"


# ── 6. missing / negative metric values are skipped ───────────────────

def test_missing_disk_d_is_ignored():
    """A server with no D: drive should NOT be marked critical
    because the disk_d field is None or absent."""
    metrics = {"cpu": 10, "ram": 10, "disk_c": 10, "disk_d": None}
    assert compute_status(metrics, _thresholds()) == "healthy"


def test_negative_disk_value_is_ignored():
    """The PowerShell collector reports -1 for non-existent drives.
    These must not register as 'over the threshold'."""
    metrics = {"cpu": 10, "ram": 10, "disk_c": 10, "disk_d": -1}
    assert compute_status(metrics, _thresholds()) == "healthy"


def test_missing_cpu_does_not_crash():
    """Defensive: an absent CPU value should not cause an exception."""
    metrics = {"ram": 10, "disk_c": 10}
    # Should be healthy (everything present is below warning).
    assert compute_status(metrics, _thresholds()) == "healthy"


# ── 7. partially-configured thresholds ────────────────────────────────

def test_no_thresholds_returns_healthy():
    """Edge case: a brand-new server with no thresholds yet should be
    healthy (never critical) — there's nothing to compare against."""
    metrics = {"cpu": 99, "ram": 99, "disk_c": 99, "disk_d": 99}
    assert compute_status(metrics, {}) == "healthy"


def test_only_warning_threshold_set():
    """If only the warning threshold is set, the critical branch is
    effectively disabled (never fires)."""
    metrics = {"cpu": 99, "ram": 10, "disk_c": 10, "disk_d": 10}
    thresholds = {"cpu_warning": 80}  # no cpu_critical
    assert compute_status(metrics, thresholds) == "warning"


# ── 8. maintenance-window threshold override ──────────────────────────

def test_maintenance_window_loosens_threshold(monkeypatch):
    """When a maintenance window provides a loosened threshold for a
    metric, the loosened value must be used instead of the per-server
    default. Verified by patching the late-imported helper."""
    # Patch the maintenance helper to claim cpu_critical=99 for srv1.
    import maintenance
    monkeypatch.setattr(
        maintenance,
        "_get_maintenance_thresholds",
        lambda name, settings: {"cpu_critical": 99} if name == "srv1" else {},
    )
    metrics = {"cpu": 92, "ram": 10, "disk_c": 10, "disk_d": 10}
    base = _thresholds(cpu_c=90)
    # Note: ``settings`` must be truthy for the maintenance branch to
    # consult ``_get_maintenance_thresholds`` (see ``compute_status``
    # guard: ``if server_name and settings``). An empty dict is falsy
    # in Python and would silently bypass the helper.
    settings = {"some_setting": True}
    # Without maintenance: critical. With maintenance loosening to 99:
    # warning (92 is still above the 80 warning floor).
    assert compute_status(metrics, base, server_name="srv1", settings=settings) == "warning"
    # A different server gets the un-loosened threshold.
    assert compute_status(metrics, base, server_name="srv2", settings=settings) == "critical"
