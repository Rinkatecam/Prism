"""Alert fatigue scoring engine for Prism.
Scores alerts by frequency, recency, and actionability to reduce noise."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("prism.alert_scoring")


def calculate_score(fire_count, ack_count, hours_since_last):
    """Calculate noise score 0-100. Higher = noisier.

    Formula:
    - frequency_norm = min(fire_count / 50, 1.0)  # normalized to 50 fires
    - recency = 1.0 / (1.0 + hours_since_last / 24)  # decays over days
    - actionability = ack_count / max(fire_count, 1)  # ratio of acknowledged
    - noise = frequency_norm * recency * (1 - actionability) * 100
    """
    frequency_norm = min(fire_count / 50, 1.0)
    recency = 1.0 / (1.0 + hours_since_last / 24)
    actionability = min(ack_count / max(fire_count, 1), 1.0)
    noise = frequency_norm * recency * (1.0 - actionability) * 100
    return round(noise, 1)


def update_score_on_fire(db, server_name, metric, event_type):
    """Called when an alert fires. Increments fire_count and recalculates score."""
    existing = db.get_alert_score(server_name, metric, event_type)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if existing:
        fire_count = existing["fire_count"] + 1
        ack_count = existing["ack_count"]
        hours_since = 0  # just fired
        score = calculate_score(fire_count, ack_count, hours_since)
        db.upsert_alert_score(server_name, metric, event_type,
                             fire_count, ack_count, existing.get("suppress_count", 0),
                             score, last_fired=now)
    else:
        score = calculate_score(1, 0, 0)
        db.upsert_alert_score(server_name, metric, event_type,
                             1, 0, 0, score, last_fired=now)


def update_score_on_ack(db, server_name, metric, event_type):
    """Called when an alert is acknowledged. Increments ack_count and recalculates."""
    existing = db.get_alert_score(server_name, metric, event_type)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if existing:
        ack_count = existing["ack_count"] + 1
        fire_count = existing["fire_count"]
        last_fired = existing.get("last_fired", now)
        # Compute hours since last fire
        try:
            dt = datetime.fromisoformat(last_fired.replace("Z", "+00:00"))
            hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            hours = 24
        score = calculate_score(fire_count, ack_count, hours)
        db.upsert_alert_score(server_name, metric, event_type,
                             fire_count, ack_count, existing.get("suppress_count", 0),
                             score, last_acked=now)


def is_throttled_by_fatigue(db, server_name, metric, event_type, settings, channel="email"):
    """Return True if this alert should SKIP the given notification channel due to fatigue.

    Looks up the current noise score for (server, metric, event_type) and
    compares it against the configured threshold. Used by collector_v2 and
    security_checker.py at every email/webhook dispatch site.

    Args:
        db: Database instance
        server_name: Server emitting the alert
        metric: Metric label (e.g. "cpu", "failed_logins", "security_status")
        event_type: Severity ("critical", "warning", etc.)
        settings: Full settings dict
        channel: "email" or "webhook"

    Returns:
        True → caller should SKIP the dispatch.
        False → proceed normally.

    Notes:
        - Honors `alert_fatigue.enabled` (default True). If disabled, never throttles.
        - Returns False on any DB error so safety wins over silence.
        - Increments suppress_count when throttling kicks in (so the noise
          digest can show "suppressed by fatigue" in future iterations).
    """
    cfg = (settings or {}).get("alert_fatigue", {})
    if not cfg.get("enabled", True):
        return False
    # F-014 (this audit's scope) — CRITICAL never gets throttled.
    # Fatigue is a noise-reduction tool; it must not silence genuine
    # incidents. If a (server, metric, critical) alert is scored
    # noisy, that's the operator's problem to tune thresholds, not
    # the system's problem to hide. Callers that pass severity as
    # event_type (the common case in the collector) benefit from
    # this guard automatically. Pinned by tests in
    # tests/test_alert_scoring.py.
    if str(event_type or "").strip().lower().startswith("critical"):
        return False
    score_threshold = cfg.get(f"{channel}_throttle_score", 70 if channel == "email" else 80)
    try:
        existing = db.get_alert_score(server_name, metric, event_type)
        if not existing:
            return False
        if existing.get("score", 0) >= score_threshold:
            # Bump suppress_count for telemetry
            try:
                db.upsert_alert_score(
                    server_name, metric, event_type,
                    existing["fire_count"], existing["ack_count"],
                    existing.get("suppress_count", 0) + 1,
                    existing["score"],
                    last_fired=existing.get("last_fired"),
                )
            except Exception:
                pass
            logger.info("[%s] %s alert throttled by fatigue (score=%.1f >= %d)",
                        server_name, metric, existing["score"], score_threshold)
            return True
    except Exception:
        logger.debug("Fatigue check failed for %s/%s", server_name, metric, exc_info=True)
    return False


def should_send_repeat(db, server_name, metric, event_type, settings, channel="email"):
    """Return True if a *repeat* notification is allowed on ``channel``.

    This is the feature-1.1 repeat-interval throttle — distinct from the noise
    fatigue gate above. It bounds how often a RECURRING alert re-notifies: once
    we've sent on a channel for (server, metric, event_type), don't re-send
    until ``alert_fatigue.repeat_interval_hours`` (default 4h) has elapsed.

    Safety bias — returns True (allow the send) on any of:
      * a ``resolved`` event  (resolve-once must never be throttled),
      * ``alert_fatigue.enabled`` is False, or a non-positive interval,
      * no prior send recorded on this channel,
      * the interval has elapsed,
      * any DB/parse error (prefer a duplicate over a silent drop).

    Returns False (suppress) ONLY when a prior send exists on this channel and
    less than the configured interval has elapsed.
    """
    # Resolve-once bypasses the throttle entirely.
    if str(event_type or "").strip().lower().startswith("resolved"):
        return True

    cfg = (settings or {}).get("alert_fatigue", {})
    if not cfg.get("enabled", True):
        return True
    try:
        interval_h = float(cfg.get("repeat_interval_hours", 4))
    except (ValueError, TypeError):
        interval_h = 4.0
    if interval_h <= 0:
        return True

    try:
        existing = db.get_alert_score(server_name, metric, event_type)
    except Exception:
        logger.debug("Repeat check failed for %s/%s", server_name, metric, exc_info=True)
        return True
    if not existing:
        return True

    last_sent = existing.get(f"last_sent_{channel}")
    if not last_sent:
        return True
    try:
        dt = datetime.fromisoformat(str(last_sent).replace("Z", "+00:00"))
        hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return True  # unparseable timestamp → send rather than drop
    return hours >= interval_h


def get_noise_digest(db, limit=10):
    """Get top N noisiest alerts with suggested threshold adjustments."""
    scores = db.get_alert_scores(limit=limit)
    digest = []
    for s in scores:
        suggestion = None
        if s["fire_count"] > 5 and s["ack_count"] / max(s["fire_count"], 1) < 0.2:
            suggestion = "Consider raising threshold — this alert fires frequently but is rarely actioned"
        digest.append({**s, "suggestion": suggestion})
    return digest
