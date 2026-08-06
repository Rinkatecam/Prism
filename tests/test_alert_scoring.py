"""Tests for ``alert_scoring`` — F-014 remediation.

The fatigue-throttle gate exists to silence noisy alerts. The critical
property under test: **it must never silence a CRITICAL alert**, even
if that (server, metric, critical) tuple is scored extremely noisy.

The guard was added in alert_scoring.py during this audit's scope. These
tests pin its behaviour for the future.
"""

from __future__ import annotations

import pytest

from alert_scoring import is_throttled_by_fatigue, calculate_score


# ── score formula sanity ──────────────────────────────────────────────

def test_calculate_score_zero_fires_returns_zero():
    """No fires at all → no noise score."""
    assert calculate_score(fire_count=0, ack_count=0, hours_since_last=0) == 0


def test_calculate_score_low_actionability_high_recency_high_score():
    """Many fires + few acks + recently → high score (noisy alert)."""
    score = calculate_score(fire_count=50, ack_count=1, hours_since_last=0)
    assert score > 80, f"expected high noise score, got {score}"


def test_calculate_score_high_actionability_lowers_score():
    """Same fire count but mostly acked → low score (signal-worthy)."""
    noisy = calculate_score(fire_count=50, ack_count=1, hours_since_last=0)
    useful = calculate_score(fire_count=50, ack_count=50, hours_since_last=0)
    assert useful < noisy / 4, "high ack-ratio must significantly lower score"


def test_calculate_score_old_alert_lowers_score():
    """Even noisy alerts decay over time."""
    fresh = calculate_score(fire_count=50, ack_count=1, hours_since_last=0)
    old = calculate_score(fire_count=50, ack_count=1, hours_since_last=24 * 30)
    assert old < fresh / 2, "30-day-old alert should be a fraction of fresh score"


# ── F-014: critical events never throttled ───────────────────────────

class _StubDB:
    """Minimal DB stub that returns a high score for any lookup."""
    def __init__(self, score=99.0, fire_count=100, ack_count=0, suppress_count=0):
        self._row = {
            "score": score, "fire_count": fire_count,
            "ack_count": ack_count, "suppress_count": suppress_count,
            "last_fired": None,
        }
    def get_alert_score(self, *_, **__):
        return dict(self._row)
    def upsert_alert_score(self, *_, **__):
        return None


def test_critical_event_is_not_throttled_even_with_high_score():
    """The load-bearing F-014 invariant: a 'critical' alert with score
    well above the throttle threshold must NEVER be suppressed."""
    db = _StubDB(score=99.0)
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    assert is_throttled_by_fatigue(db, "srv1", "cpu", "critical", settings, "email") is False


def test_critical_event_case_insensitive():
    """Case-insensitive: 'CRITICAL' / 'Critical' / 'critical' all bypass."""
    db = _StubDB(score=99.0)
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    for variant in ("CRITICAL", "Critical", "critical", "Critical:1"):
        assert is_throttled_by_fatigue(db, "srv1", "cpu", variant, settings, "email") is False, (
            f"variant {variant!r} must not throttle"
        )


def test_warning_event_can_be_throttled_when_score_high():
    """Warning still respects the throttle — the F-014 guard is critical-
    only. This pins the inverse of the protection."""
    db = _StubDB(score=99.0)
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    # Warning above the threshold → throttle.
    assert is_throttled_by_fatigue(db, "srv1", "cpu", "warning", settings, "email") is True


def test_warning_event_not_throttled_when_score_below_threshold():
    """Inverse: a warning with low noise score must not throttle."""
    db = _StubDB(score=10.0)
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    assert is_throttled_by_fatigue(db, "srv1", "cpu", "warning", settings, "email") is False


# ── fatigue disabled = never throttles ────────────────────────────────

def test_fatigue_disabled_never_throttles_any_severity():
    """Master kill-switch."""
    db = _StubDB(score=99.0)
    settings = {"alert_fatigue": {"enabled": False}}
    for sev in ("critical", "warning", "info"):
        assert is_throttled_by_fatigue(db, "srv1", "cpu", sev, settings, "email") is False


# ── no row in DB = never throttles ────────────────────────────────────

class _EmptyDB:
    def get_alert_score(self, *_, **__):
        return None
    def upsert_alert_score(self, *_, **__):
        return None


def test_no_score_row_means_no_throttle():
    """A brand-new (server, metric, event_type) combination has no row;
    must NOT throttle (default-allow)."""
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    assert is_throttled_by_fatigue(_EmptyDB(), "srv1", "cpu", "warning", settings, "email") is False


# ── safety: DB error never silently throttles ─────────────────────────

class _BrokenDB:
    def get_alert_score(self, *_, **__):
        raise RuntimeError("simulated DB outage")
    def upsert_alert_score(self, *_, **__):
        return None


def test_db_error_does_not_throttle():
    """If the fatigue lookup fails, fall open (alert), not closed (silent)."""
    settings = {"alert_fatigue": {"enabled": True, "email_throttle_score": 70}}
    assert is_throttled_by_fatigue(_BrokenDB(), "srv1", "cpu", "warning", settings, "email") is False
