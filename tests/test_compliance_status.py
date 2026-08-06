"""Tests for csv_compliance — SOP catalogue + status computation.

Pins:
  * Status enum coverage: current / due_soon / overdue / never / n_a
  * Boundary cases at the cadence + due-soon window
  * Per-event SOPs (cadence_days=None) always return 'n_a'
  * Overall readiness aggregate
  * Feature flag (is_compliance_enabled)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import csv_compliance as cc


# ── catalogue invariants ──────────────────────────────────────────────

def test_catalogue_has_nine_sops():
    assert len(cc.SOP_CATALOGUE) == 9


def test_catalogue_ids_are_unique():
    ids = [s.id for s in cc.SOP_CATALOGUE]
    assert len(ids) == len(set(ids))


def test_catalogue_id_format():
    """All IDs follow SOP-NN."""
    import re
    for s in cc.SOP_CATALOGUE:
        assert re.match(r"^SOP-\d{2}$", s.id), f"bad id format: {s.id!r}"


def test_catalogue_every_doc_path_exists():
    """Every SOP definition must point at a real file under docs/SOPs/."""
    from pathlib import Path
    project_root = Path(cc.__file__).resolve().parent
    for s in cc.SOP_CATALOGUE:
        p = project_root / s.doc_path
        assert p.exists(), f"SOP {s.id} doc_path {s.doc_path} not found"


def test_get_sop_finds_by_id():
    s = cc.get_sop("SOP-05")
    assert s is not None
    assert s.title == "Validated-baseline review"


def test_get_sop_returns_none_for_unknown():
    assert cc.get_sop("SOP-99") is None


# ── status: never / n_a ───────────────────────────────────────────────

def test_status_never_when_scheduled_sop_has_no_execution():
    sop = cc.get_sop("SOP-05")  # cadence_days=30
    out = cc.compute_sop_status(sop, last_executed_at=None)
    assert out["status"] == cc.STATUS_NEVER
    assert out["days_overdue"] == 0
    assert out["next_due_at"] is None


def test_status_n_a_when_per_event_sop_has_no_execution():
    sop = cc.get_sop("SOP-01")  # cadence_days=None
    out = cc.compute_sop_status(sop, last_executed_at=None)
    assert out["status"] == cc.STATUS_NA
    assert out["next_due_at"] is None


def test_status_n_a_when_per_event_sop_has_old_execution():
    """Per-event SOPs never go overdue regardless of execution age."""
    sop = cc.get_sop("SOP-01")
    ancient = "2020-01-01T00:00:00Z"
    out = cc.compute_sop_status(sop, last_executed_at=ancient)
    assert out["status"] == cc.STATUS_NA


# ── status: current / due_soon / overdue ──────────────────────────────

def _iso(days_ago: int, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_status_current_when_recent():
    sop = cc.get_sop("SOP-05")  # 30-day cadence
    out = cc.compute_sop_status(sop, last_executed_at=_iso(5))
    assert out["status"] == cc.STATUS_CURRENT


def test_status_due_soon_when_within_one_week_of_cadence():
    sop = cc.get_sop("SOP-05")  # 30-day cadence
    # 25 days ago → next due in 5 days → within 7-day window.
    out = cc.compute_sop_status(sop, last_executed_at=_iso(25))
    assert out["status"] == cc.STATUS_DUE_SOON


def test_status_overdue_when_past_cadence():
    sop = cc.get_sop("SOP-05")  # 30-day cadence
    out = cc.compute_sop_status(sop, last_executed_at=_iso(35))
    assert out["status"] == cc.STATUS_OVERDUE
    assert out["days_overdue"] >= 5


def test_status_overdue_days_count_is_positive():
    sop = cc.get_sop("SOP-03")  # 90-day cadence
    out = cc.compute_sop_status(sop, last_executed_at=_iso(100))
    assert out["days_overdue"] >= 10


# ── boundary cases ───────────────────────────────────────────────────

def test_status_at_exact_cadence_is_overdue():
    """31 days after a 30-day SOP → 1 day overdue."""
    sop = cc.get_sop("SOP-05")
    out = cc.compute_sop_status(sop, last_executed_at=_iso(31))
    assert out["status"] == cc.STATUS_OVERDUE


def test_status_just_outside_due_soon_window():
    """Just outside the 7-day due-soon window → still current."""
    sop = cc.get_sop("SOP-05")  # cadence 30
    # 22 days ago → 8 days until due → outside due_soon (7 d) → current.
    out = cc.compute_sop_status(sop, last_executed_at=_iso(22))
    assert out["status"] == cc.STATUS_CURRENT


def test_status_malformed_timestamp_treated_as_never():
    sop = cc.get_sop("SOP-05")
    out = cc.compute_sop_status(sop, last_executed_at="not a timestamp")
    assert out["status"] == cc.STATUS_NEVER


# ── overall readiness ────────────────────────────────────────────────

def test_overall_readiness_all_current():
    """When every scheduled SOP has a recent execution, readiness OK."""
    latest = {
        s.id: {"executed_at": _iso(1)}
        for s in cc.SOP_CATALOGUE if s.cadence_days
    }
    out = cc.get_overall_readiness(latest)
    assert out["ok"] is True
    assert out["overdue"] == 0
    assert out["never"] == 0
    assert out["current"] == out["total"]


def test_overall_readiness_with_overdue():
    """Overdue SOP → not ok."""
    latest = {"SOP-05": {"executed_at": _iso(100)}}
    out = cc.get_overall_readiness(latest)
    assert out["ok"] is False
    assert out["overdue"] >= 1


def test_overall_readiness_empty_means_never():
    """Empty dict → every scheduled SOP is 'never' → not ok."""
    out = cc.get_overall_readiness({})
    assert out["ok"] is False
    assert out["never"] > 0
    assert out["overdue"] == 0
    # Per-event SOPs count under n_a.
    assert out["n_a"] >= 1


def test_overall_readiness_total_excludes_n_a():
    """Per-event SOPs (SOP-01, 02, 04, 09) don't count in 'total'."""
    out = cc.get_overall_readiness({})
    # Catalogue has 5 scheduled (SOP-03, 05, 06, 07, 08) + 4 per-event.
    assert out["total"] == 5
    assert out["n_a"] == 4


# ── feature flag ─────────────────────────────────────────────────────

def test_is_compliance_enabled_default_off():
    assert cc.is_compliance_enabled({}) is False
    assert cc.is_compliance_enabled(None) is False


def test_is_compliance_enabled_when_flag_set():
    assert cc.is_compliance_enabled({"compliance": {"enabled": True}}) is True


def test_is_compliance_enabled_false_when_explicit_false():
    assert cc.is_compliance_enabled({"compliance": {"enabled": False}}) is False


def test_config_manager_default_settings_include_compliance_key():
    """**F-PHD-CONFIG (regression)**: ``ConfigManager.get_settings()``
    only surfaces keys that are present in ``_DEFAULT_SETTINGS``. The
    initial compliance UI ship forgot to add ``compliance`` to the
    defaults, so even with ``"compliance": {"enabled": true}`` in
    config.json, ``get_settings()`` dropped the key and the dashboard
    rendered 404.

    Pin the key's presence so a future refactor that re-shapes
    _DEFAULT_SETTINGS doesn't silently break the feature again."""
    from config_manager import ConfigManager
    assert "compliance" in ConfigManager._DEFAULT_SETTINGS, (
        "F-PHD-CONFIG: ConfigManager._DEFAULT_SETTINGS must include "
        "'compliance' or get_settings() will drop user-set values."
    )
    assert ConfigManager._DEFAULT_SETTINGS["compliance"].get("enabled") is False, (
        "compliance.enabled must default to False so non-regulated "
        "deployments don't see the surface accidentally."
    )


def test_config_manager_passes_compliance_enabled_through_to_settings(tmp_path, monkeypatch):
    """End-to-end: a config file with compliance.enabled=true must
    survive the ConfigManager merge and be readable via get_settings()."""
    import json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({
        "servers": [],
        "settings": {"compliance": {"enabled": True}},
    }), encoding="utf-8")
    from config_manager import ConfigManager
    cm = ConfigManager(config_path=str(cfg))
    s = cm.get_settings()
    assert s.get("compliance", {}).get("enabled") is True
    assert cc.is_compliance_enabled(s) is True
