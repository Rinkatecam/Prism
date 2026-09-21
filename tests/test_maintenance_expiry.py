"""Expiring maintenance windows — WP-1 phase 1, track 3.

The round table's M6, and the sentence that IS the feature: **every window
must auto-expire, because a forever-mute is how real outages get eaten.**
Every monitoring veteran has a story about the downtime somebody set in
2019 that swallowed a real outage in 2021.

Two window shapes after this change:

  * RECURRING (exists today): days + start/end HH:MM in the configured
    timezone. Gains an optional `expires_at` — an absolute UTC instant
    after which the schedule stops applying (a 3-month migration window
    that must not outlive the migration).
  * AD-HOC (new): `servers` + `expires_at` and nothing else. "Mute these
    boxes for the next two hours." Applies continuously from now until
    expiry, then never again.

Expired windows are also SWEPT from settings by a periodic job — but the
matcher refusing them is the load-bearing half: the sweep is hygiene, the
refusal is correctness, and the refusal must not depend on the sweep
having run (a sweep is a periodic thread, and periodic threads die).

Timestamps: `expires_at` is stored UTC ISO-8601, compared tz-aware —
the house timezone rule. The recurring schedule keeps matching in the
configured timezone exactly as before.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from maintenance import (       # noqa: E402
    _get_active_maintenance_window,
    is_in_maintenance,
    sweep_expired_windows,
)


def _utc(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings(*windows) -> dict:
    return {"maintenance_windows": list(windows), "timezone": "UTC"}


def _recurring(server="S1", **extra) -> dict:
    """A window that matches RIGHT NOW in UTC, whatever now is."""
    w = {"servers": [server], "days": list(range(7)),
         "start_time": "00:00", "end_time": "23:59"}
    w.update(extra)
    return w


# ── ad-hoc windows ────────────────────────────────────────────────────────

def test_an_adhoc_window_applies_until_it_expires():
    s = _settings({"servers": ["S1"], "expires_at": _utc(hours=2)})
    assert is_in_maintenance("S1", s) is True
    assert is_in_maintenance("S2", s) is False


def test_an_expired_adhoc_window_never_matches_even_before_the_sweep():
    """THE load-bearing assertion. The matcher must refuse expired windows
    on its own — correctness cannot depend on a periodic thread being
    alive. This is the 'forever-mute eats a real outage' failure, closed
    at the read site."""
    s = _settings({"servers": ["S1"], "expires_at": _utc(hours=-1)})
    assert is_in_maintenance("S1", s) is False


def test_an_adhoc_window_needs_no_schedule_fields():
    """'Mute for the next two hours' must not require inventing days and
    times — that friction is why ad-hoc mutes end up as permanent recurring
    windows in every tool that lacks them."""
    s = _settings({"servers": ["S1"], "expires_at": _utc(minutes=30),
                   "suppress_alerts": True})
    win = _get_active_maintenance_window("S1", s)
    assert win is not None
    assert win.get("suppress_alerts") is True


# ── recurring windows gain an optional expiry ─────────────────────────────

def test_a_recurring_window_still_matches_exactly_as_before():
    """No regression for every window that exists today (no expires_at)."""
    assert is_in_maintenance("S1", _settings(_recurring())) is True


def test_an_expired_recurring_window_stops_matching():
    s = _settings(_recurring(expires_at=_utc(days=-1)))
    assert is_in_maintenance("S1", s) is False


def test_a_future_expiry_leaves_a_recurring_window_active():
    s = _settings(_recurring(expires_at=_utc(days=90)))
    assert is_in_maintenance("S1", s) is True


def test_a_malformed_expiry_disables_the_window_not_the_feature():
    """Fail CLOSED, per the tz rule's own precedent (P15): a window whose
    expiry cannot be parsed must not match — an unparseable mute that keeps
    muting is the exact failure this feature exists to end."""
    s = _settings({"servers": ["S1"], "expires_at": "not-a-timestamp"})
    assert is_in_maintenance("S1", s) is False


# ── the sweep ─────────────────────────────────────────────────────────────

class _FakeConfig:
    def __init__(self, settings):
        self._settings = settings
        self.saved = None

    def get_settings(self):
        return self._settings

    def save_maintenance_windows(self, windows):
        self.saved = windows
        self._settings["maintenance_windows"] = windows


def test_the_sweep_removes_only_expired_windows():
    cfg = _FakeConfig(_settings(
        {"servers": ["S1"], "expires_at": _utc(hours=-2)},          # expired ad-hoc
        _recurring(expires_at=_utc(days=-1)),                       # expired recurring
        {"servers": ["S3"], "expires_at": _utc(hours=2)},           # live ad-hoc
        _recurring(server="S4"),                                    # eternal recurring
    ))
    removed = sweep_expired_windows(cfg)
    assert removed == 2
    kept = cfg.saved
    assert len(kept) == 2
    assert kept[0]["servers"] == ["S3"]
    assert kept[1]["servers"] == ["S4"]


def test_the_sweep_is_a_noop_when_nothing_expired():
    """No write when there is nothing to remove — a sweep that rewrites
    settings every hour turns config_changes into noise."""
    cfg = _FakeConfig(_settings(_recurring()))
    assert sweep_expired_windows(cfg) == 0
    assert cfg.saved is None


def test_the_sweep_leaves_malformed_expiries_in_place_but_they_do_not_match():
    """Hygiene must not destroy evidence: an unparseable expires_at is an
    operator mistake to SHOW (the window sits there visibly wrong and
    inert), not to silently delete. The matcher refusing it (fail closed)
    is what protects correctness meanwhile."""
    cfg = _FakeConfig(_settings({"servers": ["S1"],
                                 "expires_at": "not-a-timestamp"}))
    assert sweep_expired_windows(cfg) == 0
    assert cfg.saved is None
    assert is_in_maintenance("S1", cfg.get_settings()) is False
