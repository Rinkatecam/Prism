"""Health endpoints — split out from the original routes/api.py."""

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
from collector_v2 import state as _v2_state
from collector_v2.periodics import get_periodics_health as _v2_periodics_health
from state import (
    server_auth_info,
    server_update_info,
    server_hardware_info,
)

# Canonical heartbeat-stale thresholds, sourced from each subsystem so the
# pulse endpoint stays honest if cadences are retuned elsewhere. Each value
# is the subsystem's nominal tick interval in seconds; we flag stale when
# the recorded heartbeat age exceeds 5× this number (the same multiplier
# the watchdog uses, see app.py's watchdog block).
_SUBSYSTEM_INTERVAL_S = {
    "supervisor": 5,   # supervisor tick=5s (collector_v2/supervisor.py)
    "aggregator": 5,   # aggregator inner-loop period
    "workers":    10,  # worker activity heartbeat (less frequent)
    "periodics":  30,  # periodics outer loop cadence
}
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


@api_bp.route("/system/health")
def system_health():
    auth = _require_auth()
    if auth: return auth
    import sys
    import time as _time
    db_stats = _shared._db.get_db_stats()
    settings = _shared._config.get_settings()
    now = _time.time()
    # Post-v1-retirement: the "fresh data" signal is the v2 aggregator's
    # last tick (it bumps every time a Result is persisted). External
    # monitors and the operations-page badge read ``last_cycle`` here,
    # so we keep the field name even though there's no cycle anymore.
    try:
        from collector_v2 import state as _v2_state
        _last_data_ts = _v2_state.last_aggregator_tick or 0
    except Exception:
        _last_data_ts = 0
    collector_info = {
        "last_cycle": _last_data_ts,
        "poll_interval": settings.get("poll_interval_seconds", 60),
    }
    # S2-11 (P10) — surface scheduler thread heartbeats so the operator can
    # see at a glance whether all three workhorse threads are ticking. The
    # watchdog in app.py logs CRITICAL + writes an audit row on transition,
    # but a glance at /api/system/health is the fastest manual check.
    def _hb_summary(hb_attr_module, attr_name, interval_s):
        hb = getattr(hb_attr_module, attr_name, 0) or 0
        if hb <= 0:
            return {"last_tick": None, "age_s": None, "stale": False}
        age = now - hb
        return {"last_tick": hb, "age_s": round(age, 1),
                "stale": age > interval_s * 5}
    try:
        import restart_scheduler as _rs_mod
        import workflow_engine as _wf_mod
        # Wrap the v2 aggregator tick in a tiny namespace so _hb_summary's
        # attribute-access signature works without a special case.
        from types import SimpleNamespace as _NS
        _collector_hb_src = _NS(last_tick=_last_data_ts)
        threads_info = {
            "collector": _hb_summary(_collector_hb_src, "last_tick",
                                     settings.get("poll_interval_seconds", 60)),
            "restart_scheduler": _hb_summary(_rs_mod, "_last_heartbeat",
                                             getattr(_rs_mod, "LOOP_INTERVAL", 30)),
            "workflow_scheduler": _hb_summary(_wf_mod, "_last_heartbeat",
                                              getattr(_wf_mod, "SCHEDULER_INTERVAL", 30)),
        }
    except Exception:
        threads_info = {}
    # B9 (low) — surface the backup-admin password age. The Sprint-3 nag is
    # to show a banner at >90 days; this is the data the banner reads.
    backup_admin_age_days = None
    try:
        ba = settings.get("auth", {}).get("backup_admin", {}) or {}
        ts = ba.get("password_set_at")
        if ts:
            from datetime import datetime as _dt, timezone as _tz
            t = _dt.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_tz.utc)
            backup_admin_age_days = (_dt.now(_tz.utc) - t).days
    except Exception:
        backup_admin_age_days = None

    # Feature 1.8: backup freshness — age of the last successful DB backup and a
    # stale flag (age > database_backup.stale_after_hours). tz-aware UTC parse,
    # mirroring the backup_admin_age pattern above.
    backup_info = {"last_success_ts": None, "age_hours": None,
                   "stale": False, "last_ok": None}
    try:
        _bst = _shared._db.get_backup_state()
        if _bst:
            backup_info["last_ok"] = _bst.get("last_ok")
            _lst = _bst.get("last_success_ts")
            if _lst:
                from datetime import datetime as _dt3, timezone as _tz3
                _bt = _dt3.fromisoformat(str(_lst).replace("Z", "+00:00"))
                if _bt.tzinfo is None:
                    _bt = _bt.replace(tzinfo=_tz3.utc)
                _age_h = (_dt3.now(_tz3.utc) - _bt).total_seconds() / 3600
                backup_info["last_success_ts"] = _lst
                backup_info["age_hours"] = round(_age_h, 1)
                try:
                    _stale_after = float(
                        settings.get("database_backup", {}).get("stale_after_hours", 26))
                except (TypeError, ValueError):
                    _stale_after = 26.0
                backup_info["stale"] = _age_h > _stale_after
    except Exception:
        pass

    # Per-component health snapshot for the v2 collector. ``error`` block
    # on failure rather than ``None`` so operators can distinguish "v2
    # crashed at start" from "v2 not running at all".
    try:
        import collector_v2  # noqa: PLC0415
        v2_health = collector_v2.get_health_snapshot()
    except Exception as _v2e:
        v2_health = {"error": f"v2 health unavailable: {_v2e}"}

    # Anomaly baseline cache stats — useful for operators monitoring how
    # the cache is performing as fleet size grows.
    try:
        from analytics import get_baseline_cache_stats  # noqa: PLC0415
        baseline_cache_stats = get_baseline_cache_stats()
    except Exception:
        baseline_cache_stats = None

    # F-AT-1 (CSV-12 / 17 remediation): surface the hourly audit-chain
    # verifier's latest result so an external monitor (or the monthly
    # SOP-05 review) can confirm "audit chain ok in the last hour"
    # without having to call verify_audit_chain() manually.
    #
    # Also surface the audit-insert and mirror failure counters (F-D-1)
    # so a DB outage that prevented audit rows from landing is visible
    # rather than silent. ``audit_blind`` is the alert-worthy summary —
    # any non-zero value means we've lost at least one regulated record
    # and the operator must investigate.
    try:
        from collector_v2 import state as _v2_state2
        last_chain = getattr(_v2_state2, "last_audit_chain_check", None)
        if last_chain and last_chain.get("ts"):
            last_chain = {
                **last_chain,
                "age_s": round(now - last_chain["ts"], 1),
            }
    except Exception:
        last_chain = None
    audit_telemetry = {
        "last_chain_check": last_chain,
        "insert_failures": getattr(_shared._db, "_audit_insert_failures", 0),
        "mirror_failures": getattr(_shared._db, "_audit_mirror_failures", 0),
    }
    audit_telemetry["audit_blind"] = (
        audit_telemetry["insert_failures"] > 0
        or audit_telemetry["mirror_failures"] > 0
    )

    return jsonify({
        "ok": True,
        # ``collector_engine`` is kept in the response for one release
        # so any external dashboard that reads it doesn't break. Value
        # is always "v2" post-retirement — v1 no longer exists.
        "collector_engine": "v2",
        "collector": collector_info,
        "collector_v2": v2_health,
        "baseline_cache": baseline_cache_stats,
        "threads": threads_info,
        "db": {
            "size_mb": round(db_stats.get("size_bytes", 0) / (1024 * 1024), 2),
            "size_bytes": db_stats.get("size_bytes", 0),
            "metrics_count": db_stats.get("table_counts", {}).get("metrics", 0),
            "events_count": db_stats.get("table_counts", {}).get("events", 0),
            "audit_count": db_stats.get("table_counts", {}).get("audit_log", 0),
            # What the log ingest filter discarded this process lifetime.
            # Reported rather than silent: a monitoring tool that quietly throws
            # data away is one you stop trusting. Pair these with the
            # settings.log_ingest allow-list to see what the filter is doing.
            "logs_dropped_information": type(_shared._db).logs_dropped_information,
            "logs_kept_by_allowlist": type(_shared._db).logs_kept_by_allowlist,
            "oldest_record": db_stats.get("oldest_record"),
            "newest_record": db_stats.get("newest_record"),
        },
        "backup": backup_info,
        "auth": {
            "backup_admin_password_age_days": backup_admin_age_days,
            "backup_admin_rotation_due": (
                backup_admin_age_days is not None and backup_admin_age_days > 90
            ),
        },
        "audit": audit_telemetry,
        "app": {
            "python_version": sys.version.split()[0],
        },
    })


@api_bp.route("/collector/pulse")
def collector_pulse():
    """Live heartbeat feed for the topbar ECG widget.

    Returns the events that landed in the aggregator since the client's
    watermark (``?since=<unix_ts>``) plus a small "right now" snapshot:

      * ``fleet``      — up / total / silent
      * ``active``     — checks currently in flight
      * ``subsystems`` — supervisor / aggregator / workers / periodics
                         heartbeats with derived ok-flag
      * ``bpm``        — events per minute in the last 30s (smoothed)

    Steady-state payload is tiny (~1-3 events per poll at 1.5s cadence).
    First poll has no watermark and gets ~12s of backfill so the widget
    can paint an initial strip without an empty animation.
    """
    auth = _require_auth()
    if auth: return auth

    now = time.time()
    since_raw = request.args.get("since", type=float)

    # 1. Pulses — read the buffer ONCE, derive both ``events`` (since the
    # watermark) and ``bpm`` (last 30s) from the same snapshot. Saves a
    # lock round-trip and a redundant list-comprehension per request.
    try:
        recent_30s = _v2_state.get_recent_pulses(window_s=30.0)
    except Exception:
        logger.exception("get_recent_pulses failed")
        recent_30s = []
    cutoff = (since_raw if since_raw is not None else (now - 12.0))
    events = [e for e in recent_30s if e["ts"] > cutoff]
    bpm = round(len(recent_30s) * 2)  # events per 30s → per 60s

    # 2. Fleet + 3. Subsystems — reuse the single ``get_v2_health_snapshot``
    # which already calls ``get_fleet_status`` internally. Calling
    # ``get_fleet_status`` separately would iterate ``server_health`` twice
    # per request, and at 1.5s polling cadence × N tabs that adds up.
    fleet = {"total": 0, "up": 0, "silent": []}
    subsystems: dict = {}
    try:
        snap = _v2_state.get_v2_health_snapshot()
        fleet = {
            "total": snap.get("servers_total", 0),
            "up": snap.get("servers_up", 0),
            "silent": snap.get("silent_servers", []),
        }
        subsystems["supervisor"] = _hb_status(snap.get("supervisor_last_tick_s_ago"),
                                              _SUBSYSTEM_INTERVAL_S["supervisor"])
        subsystems["aggregator"] = _hb_status(snap.get("aggregator_last_tick_s_ago"),
                                              _SUBSYSTEM_INTERVAL_S["aggregator"])
        subsystems["workers"]    = _hb_status(snap.get("workers_last_activity_s_ago"),
                                              _SUBSYSTEM_INTERVAL_S["workers"])
    except Exception:
        logger.exception("v2 health snapshot failed")

    # 4. In-flight checks (drives the panel's "IN FLIGHT" section). Not
    # part of the snapshot to keep that function's contract tight.
    try:
        active = _v2_state.get_in_flight()
    except Exception:
        logger.exception("get_in_flight failed")
        active = []

    # 5. Periodics heartbeat — separate from get_v2_health_snapshot because
    # periodics is a sibling subsystem, not part of the v2 supervisor.
    try:
        ph = _v2_periodics_health()
        subsystems["periodics"] = _hb_status(ph.get("last_heartbeat_s_ago"),
                                             _SUBSYSTEM_INTERVAL_S["periodics"])
    except Exception:
        subsystems["periodics"] = {"age_s": None, "ok": False}

    # Pulse data is real-time — don't let intermediate caches/proxies hold
    # it for any duration, otherwise the watermark protocol breaks.
    resp = make_response(jsonify({
        "ok": True,
        "now": now,
        "events": events,
        "fleet": fleet,
        "active": active,
        "subsystems": subsystems,
        "bpm": bpm,
    }))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _hb_status(age_s, interval_s: float) -> dict:
    """Convert a heartbeat age into ``{age_s, ok}``. Flags stale at
    5× the subsystem's nominal interval — matches the watchdog threshold."""
    if age_s is None:
        return {"age_s": None, "ok": False}
    return {"age_s": round(float(age_s), 1),
            "ok": float(age_s) < interval_s * 5}


@api_bp.route("/system/vacuum", methods=["POST"])
def vacuum_database():
    auth = _require_auth()
    if auth: return auth
    user = flask_session.get("username", "system")
    try:
        result = _shared._db.vacuum_db()
        saved = result["old_size"] - result["new_size"]
        try:
            _shared._db.log_audit(user, "vacuum_db", "system",
                         f"VACUUM completed: saved {round(saved/(1024*1024),2)} MB")
        except Exception:
            pass
        logger.info("Database VACUUM by %s: saved %d bytes", user, saved)
        return jsonify({
            "ok": True,
            "old_size_mb": round(result["old_size"] / (1024 * 1024), 2),
            "new_size_mb": round(result["new_size"] / (1024 * 1024), 2),
            "saved_bytes": saved,
        })
    except Exception as e:
        logger.exception("VACUUM failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/tls-certificates", methods=["GET"])
def get_tls_certificates():
    """Return all stored TLS certificate records."""
    return jsonify({"ok": True, "certificates": _shared._db.get_all_tls_certificates()})


@api_bp.route("/tls-certificates/expiring", methods=["GET"])
def get_expiring_certificates():
    """Return certificates expiring within a threshold (days query param, default 30)."""
    threshold = request.args.get("days", 30, type=int)
    return jsonify({"ok": True, "certificates": _shared._db.get_expiring_certificates(threshold)})


@api_bp.route("/tls-certificates/check", methods=["POST"])
def check_tls_certificate():
    """Probe a TLS certificate on a remote host and optionally store the result."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    host = (data.get("host") or "").strip()
    if not host:
        return jsonify({"ok": False, "error": "host is required"}), 400
    port = data.get("port", 443)
    server_name = data.get("server_name")
    try:
        import tls_checker
        result = tls_checker.check_certificate(host, port)
        if server_name:
            # NOTE: upsert_tls_certificate signature is (server_name, host, port,
            # subject, issuer, not_before, not_after, days_remaining, status, error)
            # — see database.py:1572. Map result dict explicitly.
            _shared._db.upsert_tls_certificate(
                server_name=server_name,
                host=host,
                port=int(port),
                subject=result.get("subject"),
                issuer=result.get("issuer"),
                not_before=result.get("not_before"),
                not_after=result.get("not_after"),
                days_remaining=result.get("days_remaining"),
                status=result.get("status", "unknown"),
                error=result.get("error"),
            )
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "check_tls", "tls", f"Checked TLS cert for {host}:{port}")
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        logger.exception("TLS certificate check failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/tls-certificates/<int:cert_id>", methods=["DELETE"])
def delete_tls_certificate(cert_id):
    """Delete a TLS certificate record."""
    auth = _require_auth()
    if auth: return auth
    try:
        _shared._db.delete_tls_certificate(cert_id)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "delete_tls_cert", "tls", f"Deleted TLS certificate id={cert_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to delete TLS certificate")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/health-checks", methods=["GET"])
def get_health_check_results():
    """Return health check results, optionally filtered by server query param."""
    server = request.args.get("server", None)
    return jsonify({"ok": True, "results": _shared._db.get_health_check_results(server)})


@api_bp.route("/health-checks/<server_name>", methods=["GET"])
def get_server_health_checks(server_name):
    """Return health check results for a specific server."""
    return jsonify({"ok": True, "results": _shared._db.get_health_check_results(server_name)})


@api_bp.route("/health-checks/config", methods=["GET"])
def get_health_check_config():
    """Return health check configurations, optionally filtered by server query param."""
    server = request.args.get("server", None)
    return jsonify({"ok": True, "config": _shared._db.get_health_check_config(server)})


def _verify_tls_from_payload(data: dict) -> bool:
    """Whether an HTTPS health check should validate its certificate.

    ABSENT MEANS VERIFY. Only an explicit false turns validation off, so a
    client that predates this field — or one that simply does not send it —
    cannot weaken a check by omission. `bool(data.get("verify_tls"))` reads
    identically and does the opposite: a missing key becomes `None` becomes
    `False`, and every check created by an older client silently stops
    checking certificates. That is the exact defect this setting was added to
    remove, reintroduced one layer up.

    A free function rather than inline in the route so it can be tested
    without importing `app`, which starts the collector against the real
    configuration.
    """
    value = data.get("verify_tls")
    return True if value is None else bool(value)


@api_bp.route("/health-checks/config", methods=["POST"])
def save_health_check_config():
    """Create or update a health check configuration."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    server_name = (data.get("server_name") or data.get("server") or "").strip()
    check_type = (data.get("check_type") or "").strip()
    target_host = (data.get("target_host") or data.get("host") or "").strip()
    target_port = data.get("target_port") if data.get("target_port") is not None else data.get("port")
    if check_type == "icmp" and target_port is None:
        target_port = 0
    if not server_name or not check_type or not target_host or target_port is None:
        return jsonify({"ok": False, "error": "server_name, check_type, target_host, and target_port are required"}), 400
    http_path = data.get("http_path")
    expected_status = data.get("expected_status")
    # ABSENT is not the same as EMPTY, and collapsing them defeated the
    # COALESCE in save_health_check_config: `(data.get("name") or "").strip()`
    # turns a missing key into "", which is not NULL, so the upsert wrote it
    # over the stored name. Caught by a live round trip, not by the unit test —
    # the test called the DB method directly and never saw this coercion.
    # None  -> key absent, keep what is stored.
    # ""    -> caller explicitly cleared it.
    _raw_name = data.get("name")
    name = _raw_name.strip() if isinstance(_raw_name, str) else None
    verify_tls = _verify_tls_from_payload(data)
    try:
        new_id = _shared._db.save_health_check_config(
            server_name=server_name,
            check_type=check_type,
            target_host=target_host,
            target_port=target_port,
            http_path=http_path,
            expected_status=expected_status,
            name=name,
            verify_tls=verify_tls,
        )
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "save_health_check_config", "health_checks", f"Saved health check config for '{server_name}' ({check_type} -> {target_host}:{target_port})")
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        logger.exception("Failed to save health check config")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/health-checks/config/<int:config_id>", methods=["DELETE"])
def delete_health_check_config(config_id):
    """Delete a health check configuration."""
    auth = _require_auth()
    if auth: return auth
    try:
        _shared._db.delete_health_check_config(config_id)
        username = flask_session.get("username", "system")
        _shared._db.log_audit(username, "delete_health_check_config", "health_checks", f"Deleted health check config id={config_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.exception("Failed to delete health check config")
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/health-checks/probe", methods=["POST"])
def probe_health_check():
    """Run an ad-hoc health check probe (TCP or HTTP)."""
    auth = _require_auth()
    if auth: return auth
    data = request.get_json() or {}
    host = (data.get("host") or "").strip()
    port = data.get("port")
    check_type = (data.get("check_type") or "").strip()
    if not host or check_type not in ("tcp", "http", "https", "udp", "icmp"):
        return jsonify({"ok": False, "error": "host and valid check_type ('tcp', 'http', 'https', 'udp', 'icmp') are required"}), 400
    if check_type != "icmp" and port is None:
        return jsonify({"ok": False, "error": "port is required for non-ICMP checks"}), 400
    http_path = data.get("http_path", "/")
    try:
        import health_checker
        if check_type == "tcp":
            result = health_checker.tcp_probe(host, port)
        elif check_type == "http":
            result = health_checker.http_check(host, port, path=http_path, use_ssl=False)
        elif check_type == "https":
            # Same default and the same guard as the saved check. If the Test
            # button verified when the stored check would not (or the reverse),
            # an operator would tune the endpoint against one behaviour and
            # deploy the other — and "it worked when I tested it" is the least
            # debuggable complaint a monitoring tool can produce.
            result = health_checker.http_check(
                host, port, path=http_path, use_ssl=True,
                verify_tls=_verify_tls_from_payload(data))
        elif check_type == "udp":
            result = health_checker.udp_probe(host, port)
        elif check_type == "icmp":
            result = health_checker.icmp_ping(host)
        else:
            result = health_checker.tcp_probe(host, port)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        logger.exception("Health check probe failed")
        return jsonify({"ok": False, "error": str(e)}), 500
