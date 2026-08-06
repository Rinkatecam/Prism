"""Metrics endpoints — split out from the original routes/api.py."""

import re
import time
import io
import json
from pathlib import Path
from flask import jsonify, request, Response, make_response, current_app
from flask import session as flask_session
from crypto_utils import is_password_masked, decrypt_password, PASSWORD_MASK
import collector_v2 as _collector_v2
from collector_v2 import (
    accelerate_server,
    sync_now as _v2_sync_now,
    sync_logs_now as _v2_sync_logs_now,
    sync_updates_now as _v2_sync_updates_now,
)
from state import (
    server_auth_info,
    server_update_info,
    server_hardware_info,
)
from email_alerts import send_test_email
from analytics import get_server_analytics, forecast_disk, forecast_metric
from reports import generate_csv_metrics, generate_csv_events, generate_pdf_report
from i18n import get_translations

from . import _shared
from ._shared import (
    api_bp,
    logger,
    _require_auth,
    _current_actor,
    _is_backup_admin,
    _server_tier,
    _require_server_permission,
    _require_rbac_admin,
)


@api_bp.route("/status")
def get_status():
    """Quick summary: counts by status + recent events."""
    try:
        summary = _shared._db.get_status_summary()
        events = _shared._db.get_recent_events(limit=20)
        summary["recent_events"] = events
        return jsonify(summary)
    except Exception:
        logger.exception("Error in GET /api/status")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/analytics/summary")
def get_analytics_summary():
    """Disk capacity forecasts for ALL servers (dashboard overview)."""
    try:
        servers = _shared._config.get_servers()
        result = []
        for srv in servers:
            forecasts = {
                "disk_c": forecast_disk(_shared._db, srv.name, metric="disk_c"),
                "disk_d": forecast_disk(_shared._db, srv.name, metric="disk_d"),
            }
            forecasts["ram"] = forecast_metric(_shared._db, srv.name, metric="ram")
            result.append({
                "name": srv.name,
                "forecasts": forecasts,
            })
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /api/analytics/summary")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/anomalies/acknowledge", methods=["POST"])
def acknowledge_anomaly():
    """Acknowledge or snooze an anomaly condition."""
    data = request.get_json(force=True)
    server_name = data.get("server_name")
    metric = data.get("metric")
    ack_type = data.get("ack_type", "acknowledged")  # "acknowledged" or "snoozed"
    snooze_days = data.get("snooze_days", 7)
    notes = data.get("notes", "")

    if not server_name or not metric:
        return jsonify({"ok": False, "error": "server_name and metric are required"}), 400

    snooze_until = None
    if ack_type == "snoozed":
        from datetime import datetime, timezone, timedelta
        snooze_until = (datetime.now(timezone.utc) + timedelta(days=int(snooze_days))).strftime("%Y-%m-%dT%H:%M:%SZ")

    ack_id = _shared._db.add_acknowledgment(server_name, metric, ack_type, snooze_until, notes)

    # Update alert fatigue score on acknowledgment
    try:
        from alert_scoring import update_score_on_ack
        update_score_on_ack(_shared._db, server_name, metric, "anomaly")
    except Exception:
        logger.debug("Alert scoring ack update failed", exc_info=True)

    return jsonify({"ok": True, "id": ack_id})


@api_bp.route("/anomalies/acknowledge/<int:ack_id>", methods=["DELETE"])
def remove_acknowledgment(ack_id):
    """Remove an acknowledgment."""
    _shared._db.remove_acknowledgment(ack_id)
    return jsonify({"ok": True})


@api_bp.route("/anomalies/acknowledgments")
def list_acknowledgments():
    """List all active acknowledgments."""
    server = request.args.get("server")
    metric = request.args.get("metric")
    acks = _shared._db.get_active_acknowledgments(server_name=server, metric=metric)
    return jsonify(acks)


@api_bp.route("/digest")
def get_daily_digest():
    """Return daily health digest summary."""
    from analytics import generate_daily_digest
    servers = _shared._config.get_servers()
    digest = generate_daily_digest(_shared._db, servers)
    return jsonify(digest)


@api_bp.route("/baselines/<name>")
def get_baselines(name):
    return jsonify({"ok": True, "baselines": _shared._db.get_all_baselines(name)})


@api_bp.route("/baselines/<name>/coverage")
def get_baseline_coverage(name):
    """P14 from AUDIT-2026-05: report what fraction of baseline slots have
    enough samples for the smart detector to be effective. Useful for the
    dashboard tile so an operator can see at a glance which servers are
    still warming up after being added."""
    settings = _shared._config.get_settings()
    min_samples = int(settings.get("baseline_detection", {}).get("min_samples", 10))
    coverage = _shared._db.get_baseline_coverage(name, min_samples=min_samples)
    return jsonify({"ok": True, **coverage})


@api_bp.route("/baselines/<name>/<metric>")
def get_baseline_metric(name, metric):
    baselines = _shared._db.get_all_baselines(name)
    filtered = [b for b in baselines if b.get("metric") == metric]
    return jsonify({"ok": True, "baselines": filtered})


@api_bp.route("/baselines/recalculate", methods=["POST"])
def recalculate_baselines():
    auth = _require_auth()
    if auth:
        return auth
    try:
        from baseline_engine import nightly_baseline_job
        settings = _shared._config.get_settings()
        count = nightly_baseline_job(_shared._db, _shared._config.get_servers, settings)
        # Operator clicked "Recalculate Now" — flush the analytics
        # baseline cache too, even though the cache and the nightly job
        # are technically independent subsystems (cache backs the rolling
        # mean/sigma detector; nightly_baseline_job populates the
        # hour-of-week Z-score table). When an operator hits this button
        # they typically want "refresh everything detection-related" —
        # this honours that intent at a negligible cost (next sample
        # rebuilds the cache from DB in ~1s).
        try:
            from analytics import clear_baseline_cache
            cleared = clear_baseline_cache()
            logger.info("Baseline cache flushed by manual recalc: %d entries", cleared)
        except Exception:
            logger.debug("Failed to clear analytics baseline cache", exc_info=True)
        _shared._db.log_audit(flask_session.get("username", "system"), "recalculate_baselines",
                      "baselines", f"Recalculated {count} baseline slots")
        return jsonify({"ok": True, "slots": count})
    except Exception as e:
        logger.exception("Baseline recalculation failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/incidents")
def get_incidents():
    """List incidents, optional ?status=open filter."""
    status = request.args.get("status")
    limit = request.args.get("limit", 50, type=int)
    incidents = _shared._db.get_incidents(status=status, limit=limit)
    return jsonify({"ok": True, "incidents": incidents})


@api_bp.route("/incidents/open/count")
def get_open_incident_count():
    """Return count of open incidents for badge display."""
    count = _shared._db.get_open_incident_count()
    return jsonify({"ok": True, "count": count})


@api_bp.route("/incidents/<int:incident_id>")
def get_incident_detail(incident_id):
    """Get a single incident with linked events."""
    detail = _shared._db.get_incident_detail(incident_id)
    if not detail:
        return jsonify({"ok": False, "error": "Incident not found"}), 404
    return jsonify({"ok": True, "incident": detail})


@api_bp.route("/incidents/<int:incident_id>", methods=["PUT"])
def update_incident(incident_id):
    """Update an incident (resolve, add notes, change severity). Auth required."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    detail = _shared._db.get_incident_detail(incident_id)
    if not detail:
        return jsonify({"ok": False, "error": "Incident not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed_fields = {"status", "severity", "resolution_notes", "resolved_by", "resolved_at",
                      "description", "root_cause_server", "title"}
    updates = {k: v for k, v in data.items() if k in allowed_fields}

    # If resolving, auto-fill resolved_at and resolved_by
    if updates.get("status") == "resolved":
        from datetime import datetime, timezone
        if "resolved_at" not in updates:
            updates["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if "resolved_by" not in updates:
            updates["resolved_by"] = flask_session.get("username", "manual")

    if not updates:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400

    _shared._db.update_incident(incident_id, **updates)
    _shared._db.log_audit(flask_session.get("username", "system"), "update_incident",
                  "incidents", f"Updated incident #{incident_id}: {list(updates.keys())}")
    return jsonify({"ok": True, "updated": list(updates.keys())})


@api_bp.route("/alert-scores")
def get_alert_scores():
    """Get all alert scores sorted by noise, with optional server filter."""
    try:
        server = request.args.get("server")
        if server:
            scores = _shared._db.get_alert_scores_for_server(server)
        else:
            scores = _shared._db.get_alert_scores(limit=100)
        return jsonify(scores)
    except Exception:
        logger.exception("Failed to get alert scores")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/alert-scores/digest")
def get_alert_scores_digest():
    """Get noise digest with threshold adjustment suggestions."""
    try:
        from alert_scoring import get_noise_digest
        limit = request.args.get("limit", 10, type=int)
        digest = get_noise_digest(_shared._db, limit=limit)
        return jsonify(digest)
    except Exception:
        logger.exception("Failed to get alert scores digest")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/alert-scores/reset", methods=["POST"])
def reset_alert_scores():
    """Reset all alert scores. Requires auth."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    try:
        _shared._db.reset_alert_scores()
        _shared._db.log_audit(flask_session.get("username", "system"), "reset_alert_scores",
                      "alert_scoring", "All alert fatigue scores reset")
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to reset alert scores")
        return jsonify({"error": "Internal server error"}), 500
