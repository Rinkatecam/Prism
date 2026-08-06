"""Feature 1.1 — repeat-interval throttle.

Ground truth (verified by the integration-design pass): the existing
alert-fatigue gate is a *noise-score threshold* gate, not a time-based
repeat-interval cooldown, and CRITICAL is hard-exempt from it. Transition
dedup already gives "send once while firing". The remaining delta is a
per-channel repeat FLOOR: once we email/webhook for a (server, metric,
event_type), don't re-send until `repeat_interval_hours` has elapsed — while
resolved events always pass.

T1 covers the storage layer: three nullable `last_sent_*` columns on
alert_scores and a COALESCE-merged upsert (same idempotent pattern as
last_fired/last_acked).
"""

from __future__ import annotations

import queue
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import alert_scoring
from collector_v2 import aggregator
from database import Database


@pytest.fixture()
def db():
    return Database(Path(tempfile.mkdtemp()) / "test.db")


def _cols(db) -> set[str]:
    conn = db._get_conn()
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(alert_scores)").fetchall()}
    finally:
        conn.close()


def test_alert_scores_has_last_sent_columns(db):
    assert {"last_sent_email", "last_sent_webhook", "last_resolved"} <= _cols(db)


def test_upsert_persists_and_coalesces_last_sent(db):
    db.upsert_alert_score(
        "srv1", "cpu", "critical", 1, 0, 0, 10.0,
        last_fired="2026-07-01T10:00:00Z",
        last_sent_email="2026-07-01T10:00:00Z",
    )
    row = db.get_alert_score("srv1", "cpu", "critical")
    assert row["last_sent_email"] == "2026-07-01T10:00:00Z"

    # A later upsert that omits last_sent_email must NOT null it (COALESCE-merge),
    # exactly like last_fired/last_acked behave.
    db.upsert_alert_score(
        "srv1", "cpu", "critical", 2, 0, 0, 12.0,
        last_fired="2026-07-01T11:00:00Z",
    )
    row2 = db.get_alert_score("srv1", "cpu", "critical")
    assert row2["last_sent_email"] == "2026-07-01T10:00:00Z"  # preserved
    assert row2["fire_count"] == 2


# ── T2: should_send_repeat() helper ──────────────────────────────────────────

def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _settings(enabled: bool = True, interval: float = 4):
    return {"alert_fatigue": {"enabled": enabled, "repeat_interval_hours": interval}}


def _seed(db, last_sent_email=None, event_type="critical"):
    db.upsert_alert_score(
        "srv1", "cpu", event_type, 1, 0, 0, 10.0, last_sent_email=last_sent_email
    )


def test_repeat_first_send_sends(db):
    # No prior send recorded → always allowed.
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "critical", _settings(), channel="email"
    ) is True


def test_repeat_within_interval_skips(db):
    _seed(db, _iso(1))  # sent 1h ago, interval 4h → suppress
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "critical", _settings(interval=4), channel="email"
    ) is False


def test_repeat_after_interval_sends(db):
    _seed(db, _iso(5))  # sent 5h ago, interval 4h → allow
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "critical", _settings(interval=4), channel="email"
    ) is True


def test_repeat_resolved_always_sends(db):
    # Resolve-once must never be throttled, even if a send was just made.
    _seed(db, _iso(1), event_type="resolved")
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "resolved", _settings(interval=4), channel="email"
    ) is True


def test_repeat_disabled_config_sends(db):
    _seed(db, _iso(1))
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "critical", _settings(enabled=False), channel="email"
    ) is True


def test_repeat_is_per_channel(db):
    # A recent email send must not suppress the (never-sent) webhook channel.
    _seed(db, _iso(1))
    assert alert_scoring.should_send_repeat(
        db, "srv1", "cpu", "critical", _settings(interval=4), channel="webhook"
    ) is True


# ── T3: repeat gate + last_sent stamping wired into _dispatch_alert ───────────

class _FakeServer:
    def __init__(self, name):
        self.name = name


_DISPATCH_SETTINGS = {
    "email": {"enabled": True, "smtp_server": "x", "recipients": ["a@b"],
              "send_on_critical": True},
    "webhooks": {"enabled": False},
    "alert_fatigue": {"enabled": True, "repeat_interval_hours": 4},
}


def _agg(db):
    return aggregator.Aggregator(
        result_queue=queue.Queue(), db=db, get_settings=lambda: _DISPATCH_SETTINGS
    )


def _patched_email():
    """Patch the email send fn (real repeat gate + real db retained) and disable
    the fatigue gate. Returns the send mock for call-count assertions."""
    msend = patch("collector_v2.aggregator._send_alert_email_fn")
    mfat = patch("collector_v2.aggregator._is_throttled_by_fatigue_fn")
    return msend, mfat


def test_dispatch_repeat_suppresses_second_email_within_window(db):
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        send = MagicMock(return_value=True)
        m_send_fn.return_value = send
        m_fat_fn.return_value = MagicMock(return_value=False)  # not fatigue-throttled
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)  # within window
    assert send.call_count == 1, "second email within repeat_interval must be suppressed"


def test_dispatch_repeat_resends_after_window(db):
    db.upsert_alert_score("srv1", "cpu", "critical", 1, 0, 0, 10.0, last_sent_email=_iso(5))
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        send = MagicMock(return_value=True)
        m_send_fn.return_value = send
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
    assert send.call_count == 1, "an alert older than repeat_interval must re-notify"


def test_dispatch_resolved_always_emails_even_if_recent_send(db):
    db.upsert_alert_score("srv1", "cpu", "resolved", 1, 0, 0, 10.0, last_sent_email=_iso(0.5))
    agg = _agg(db)
    evt = ("resolved", "cpu", 5.0, 90.0, "CPU recovered")
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        send = MagicMock(return_value=True)
        m_send_fn.return_value = send
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
    assert send.call_count == 1, "resolved must never be repeat-throttled"


def test_dispatch_repeat_is_per_metric(db):
    agg = _agg(db)
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        send = MagicMock(return_value=True)
        m_send_fn.return_value = send
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), ("critical", "cpu", 95, 90, "cpu"), _DISPATCH_SETTINGS)
        agg._dispatch_alert(_FakeServer("srv1"), ("critical", "ram", 95, 90, "ram"), _DISPATCH_SETTINGS)
    assert send.call_count == 2, "distinct metrics throttle independently"


def test_dispatch_stamps_last_sent_email(db):
    agg = _agg(db)
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        m_send_fn.return_value = MagicMock(return_value=True)
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), ("critical", "cpu", 95, 90, "cpu"), _DISPATCH_SETTINGS)
    row = db.get_alert_score("srv1", "cpu", "critical")
    assert row is not None and row["last_sent_email"], "a sent email must stamp last_sent_email"


# ── Pre-push fix: only stamp last_sent on CONFIRMED delivery ──────────────────

_WEBHOOK_SETTINGS = {
    "email": {"enabled": False},
    "webhooks": {"enabled": True, "teams_webhook_url": "https://x", "send_on_critical": True},
    "alert_fatigue": {"enabled": True, "repeat_interval_hours": 4},
}


def test_failed_email_does_not_stamp_last_sent(db):
    # send_alert_email returns False on delivery failure (it never raises), so a
    # failed send must NOT record last_sent — otherwise the recurring CRITICAL is
    # silently throttled for repeat_interval_hours.
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        m_send_fn.return_value = MagicMock(return_value=False)  # delivery failed
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
    row = db.get_alert_score("srv1", "cpu", "critical")
    assert row is None or not row.get("last_sent_email"), "failed email must not stamp last_sent_email"


def test_failed_email_still_reattempts_next_cycle(db):
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    msend, mfat = _patched_email()
    with msend as m_send_fn, mfat as m_fat_fn:
        send = MagicMock(return_value=False)  # both attempts fail to deliver
        m_send_fn.return_value = send
        m_fat_fn.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _DISPATCH_SETTINGS)
    assert send.call_count == 2, "a failed send must not throttle the next attempt"


def test_failed_webhook_does_not_stamp_last_sent(db):
    # send_teams_webhook returns {"ok": bool} — a truthiness check is NOT enough.
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    with patch("collector_v2.aggregator._send_teams_webhook_fn") as mwh, \
            patch("collector_v2.aggregator._is_throttled_by_fatigue_fn") as mfat:
        mwh.return_value = MagicMock(return_value={"ok": False, "error": "boom"})
        mfat.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _WEBHOOK_SETTINGS)
    row = db.get_alert_score("srv1", "cpu", "critical")
    assert row is None or not row.get("last_sent_webhook"), "failed webhook must not stamp last_sent_webhook"


def test_successful_webhook_stamps_last_sent(db):
    agg = _agg(db)
    evt = ("critical", "cpu", 95.0, 90.0, "CPU high")
    with patch("collector_v2.aggregator._send_teams_webhook_fn") as mwh, \
            patch("collector_v2.aggregator._is_throttled_by_fatigue_fn") as mfat:
        mwh.return_value = MagicMock(return_value={"ok": True})
        mfat.return_value = MagicMock(return_value=False)
        agg._dispatch_alert(_FakeServer("srv1"), evt, _WEBHOOK_SETTINGS)
    row = db.get_alert_score("srv1", "cpu", "critical")
    assert row is not None and row["last_sent_webhook"], "a delivered webhook must stamp last_sent_webhook"
