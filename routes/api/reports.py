"""Reports endpoints — split out from the original routes/api.py."""

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


@api_bp.route("/reports/csv/metrics")
def download_csv_metrics():
    """Download metric history as CSV."""
    try:
        server = request.args.get("server", "").strip() or None
        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)

        csv_data = generate_csv_metrics(_shared._db, server_name=server, hours=hours)
        filename = f"prism_metrics_{server or 'all'}_{hours}h.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception:
        logger.exception("Error generating CSV metrics report")
        return jsonify({"error": "Failed to generate report"}), 500


@api_bp.route("/reports/csv/events")
def download_csv_events():
    """Download events as CSV."""
    try:
        server = request.args.get("server", "").strip() or None
        csv_data = generate_csv_events(_shared._db, server_name=server)
        filename = f"prism_events_{server or 'all'}.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception:
        logger.exception("Error generating CSV events report")
        return jsonify({"error": "Failed to generate report"}), 500


@api_bp.route("/reports/pdf")
def download_pdf_report():
    """Download PDF management report."""
    try:
        settings = _shared._config.get_settings()
        lang = settings.get("language", "en")
        translations = get_translations(lang)
        pdf_bytes = generate_pdf_report(_shared._db, _shared._config, translations)
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=prism_report.pdf"},
        )
    except Exception:
        logger.exception("Error generating PDF report")
        return jsonify({"error": "Failed to generate PDF report"}), 500


# ── Server-comparison limits ─────────────────────────────────────────────
# Single source of truth for all four /servers/compare* endpoints AND for the
# frontend, which receives it via the reports view so the UI can make the 400
# unreachable instead of discovering it after the click.
#
# The old cap was 6, justified by payload size: get_server_history returns EVERY
# row, so a 720-hour window is thousands of rows per server and the response
# grew linearly with the selection. That made the existing "All" button — which
# checks all 29 boxes — a guaranteed 400.
#
# The chart series are now time-bucketed (COMPARE_CHART_BUCKETS), so payload no
# longer scales with the selection and the cap can cover the whole fleet. 50 is
# chosen to leave headroom above today's 29 without being unbounded; the
# remaining constraint at that size is chart legibility, not cost.
MAX_COMPARE_SERVERS = 50

# Points per metric series returned to the chart. 240 is finer than any
# realistic chart width in CSS pixels, so downsampling is invisible.
COMPARE_CHART_BUCKETS = 240


@api_bp.route("/servers/compare")
def compare_servers():
    """Get metric history for multiple servers for side-by-side comparison."""
    try:
        names = request.args.get("servers", "").strip()
        if not names:
            return jsonify({"error": "Missing 'servers' parameter (comma-separated)"}), 400

        server_list = [n.strip() for n in names.split(",") if n.strip()]
        if len(server_list) < 2:
            return jsonify({"error": "Need at least 2 servers to compare"}), 400
        if len(server_list) > MAX_COMPARE_SERVERS:
            return jsonify({
                "error": f"Maximum {MAX_COMPARE_SERVERS} servers for comparison"
            }), 400

        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)

        result = {}
        for name in server_list:
            cfg = _shared._config.get_server_by_name(name)
            if not cfg:
                continue
            # Bucketed, so the payload is a function of COMPARE_CHART_BUCKETS
            # rather than of the window x server count. This is what makes
            # comparing the whole fleet affordable; see the docstring on
            # Database.get_server_history_bucketed for why stats do NOT use it.
            history = _shared._db.get_server_history_bucketed(
                name, hours=hours, buckets=COMPARE_CHART_BUCKETS)
            latest = _shared._db.get_latest_by_server(name)
            result[name] = {
                "host": cfg.host,
                "type": cfg.type,
                "latest": {
                    "cpu": latest.get("cpu_percent") if latest else None,
                    "ram": latest.get("ram_percent") if latest else None,
                    "disk_c": latest.get("disk_c_percent") if latest else None,
                    "disk_d": latest.get("disk_d_percent") if latest else None,
                    "status": latest.get("status", "unknown") if latest else "unknown",
                } if latest else None,
                "history": history,
            }

        return jsonify({"hours": hours, "servers": result})
    except Exception:
        logger.exception("Error in GET /api/servers/compare")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/compare-events")
def compare_server_events():
    """Compare Windows event logs across multiple servers.

    Returns events grouped by event_id+source, showing which servers have
    each event and which don't.
    """
    try:
        names = request.args.get("servers", "").strip()
        if not names:
            return jsonify({"error": "Missing 'servers' parameter"}), 400

        server_list = [n.strip() for n in names.split(",") if n.strip()]
        if len(server_list) < 2:
            return jsonify({"error": "Need at least 2 servers to compare"}), 400
        if len(server_list) > MAX_COMPARE_SERVERS:
            return jsonify({"error": f"Maximum {MAX_COMPARE_SERVERS} servers"}), 400

        hours = request.args.get("hours", 24, type=int)
        hours = min(hours, 720)

        # Fetch ALL logs for each server (no limit) so we don't miss Error/Warning events
        event_map = {}  # key: (event_id, source) -> {level, message, servers: {name: count}}
        for name in server_list:
            logs = _shared._db.get_server_logs(name, hours=hours, limit=5000)
            for log in logs:
                key = (log.get("event_id"), log.get("log_source", ""))
                if key not in event_map:
                    event_map[key] = {
                        "event_id": log.get("event_id"),
                        "source": log.get("log_source", ""),
                        "level": log.get("level", ""),
                        "message_short": (log.get("message", "")[:150]),
                        "message_full": log.get("message", ""),
                        "servers": {},
                        "server_details": {},  # per-server: latest timestamp, count
                    }
                entry = event_map[key]
                # Keep the worst level
                level_order = {"Critical": 4, "Error": 3, "Warning": 2, "Information": 1}
                if level_order.get(log.get("level", ""), 0) > level_order.get(entry["level"], 0):
                    entry["level"] = log.get("level", "")
                entry["servers"][name] = entry["servers"].get(name, 0) + 1
                # Track per-server details
                if name not in entry["server_details"]:
                    entry["server_details"][name] = {
                        "count": 0,
                        "latest": log.get("timestamp", ""),
                        "level": log.get("level", ""),
                    }
                sd = entry["server_details"][name]
                sd["count"] += 1
                ts = log.get("timestamp", "")
                if ts > sd["latest"]:
                    sd["latest"] = ts

        # Split into common (on all servers) and unique (not on all)
        common = []
        unique = []
        for key, entry in event_map.items():
            entry["present_on"] = list(entry["servers"].keys())
            entry["missing_on"] = [n for n in server_list if n not in entry["servers"]]
            if len(entry["present_on"]) == len(server_list):
                common.append(entry)
            else:
                unique.append(entry)

        # Sort: unique by number of servers desc, then event_id; common by count desc
        unique.sort(key=lambda e: (-len(e["present_on"]), e.get("event_id") or 0))
        common.sort(key=lambda e: (-sum(e["servers"].values()), e.get("event_id") or 0))

        return jsonify({
            "hours": hours,
            "server_list": server_list,
            "common": common,
            "unique": unique,
        })
    except Exception:
        logger.exception("Error in GET /api/servers/compare-events")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/servers/compare-stats")
def compare_server_stats():
    """Get aggregated statistics for server comparison."""
    server_names = [s.strip() for s in request.args.get("servers", "").split(",") if s.strip()]
    hours = min(max(int(request.args.get("hours", 24)), 1), 720)

    if len(server_names) < 2 or len(server_names) > MAX_COMPARE_SERVERS:
        return jsonify({
            "error": f"Provide 2-{MAX_COMPARE_SERVERS} server names"
        }), 400

    from analytics import _linear_regression

    # Configured collector cadence, not an assumption. One reading == one poll
    # interval, so this is what converts a per-reading slope into per-day.
    poll_interval = _shared._config.get_settings().get("poll_interval_seconds", 300)

    def compute_metric_stats(values):
        """Pure Python stats: min, max, mean, median, p95, stddev, trend_slope."""
        if not values:
            return None
        n = len(values)
        sorted_vals = sorted(values)
        mean_val = sum(values) / n

        # Median
        if n % 2 == 0:
            median_val = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        else:
            median_val = sorted_vals[n // 2]

        # P95
        p95_idx = int(n * 0.95)
        p95_val = sorted_vals[min(p95_idx, n - 1)]

        # Stddev (sample)
        if n > 1:
            import math
            variance = sum((v - mean_val) ** 2 for v in values) / (n - 1)
            stddev_val = math.sqrt(variance)
        else:
            stddev_val = 0.0

        # Trend slope (per hour, then convert to per day)
        x = list(range(n))
        slope, _, _ = _linear_regression(x, values)
        # Each x unit is one reading, i.e. one poll interval. Convert to per-day:
        #   readings_per_day = 86400 / poll_interval_seconds
        # This was hardcoded to 300, but the collector has polled at 60s since
        # the v2 migration, so every trend_per_day was understated 5x. Read the
        # configured value, with 300 kept only as the historical fallback.
        readings_per_day = 86400 / max(poll_interval, 1)
        trend_per_day = slope * readings_per_day

        return {
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "mean": round(mean_val, 1),
            "median": round(median_val, 1),
            "p95": round(p95_val, 1),
            "stddev": round(stddev_val, 1),
            "trend_per_day": round(trend_per_day, 2),
        }

    result = {"hours": hours, "servers": {}}
    metric_cols = {
        "cpu": "cpu_percent",
        "ram": "ram_percent",
        "disk_c": "disk_c_percent",
        "disk_d": "disk_d_percent",
    }

    for name in server_names:
        history = _shared._db.get_server_history(name, hours=hours)
        if not history:
            continue

        server_stats = {}
        for metric, col in metric_cols.items():
            values = [r[col] for r in history if r.get(col) is not None and r[col] >= 0]
            server_stats[metric] = compute_metric_stats(values)

        result["servers"][name] = server_stats

    return jsonify(result)


@api_bp.route("/servers/compare-segmented")
def compare_server_segmented():
    """Get business-hours vs off-hours stats + fleet annotations."""
    server_names = [s.strip() for s in request.args.get("servers", "").split(",") if s.strip()]
    hours = min(max(int(request.args.get("hours", 24)), 1), 720)

    if len(server_names) < 2 or len(server_names) > MAX_COMPARE_SERVERS:
        return jsonify({
            "error": f"Provide 2-{MAX_COMPARE_SERVERS} server names"
        }), 400

    from analytics import _classify_segment
    import math

    settings = _shared._config.get_settings()
    tz = settings.get("timezone", "Europe/Berlin")

    metric_cols = {
        "cpu": "cpu_percent",
        "ram": "ram_percent",
        "disk_c": "disk_c_percent",
        "disk_d": "disk_d_percent",
    }

    result = {"hours": hours, "servers": {}, "annotations": []}

    # Collect per-server, per-segment averages
    all_means = {}  # {metric: {server: mean}} for annotation computation

    for name in server_names:
        history = _shared._db.get_server_history(name, hours=hours)
        if not history:
            continue

        biz = []
        off = []
        for r in history:
            seg = _classify_segment(r.get("timestamp", ""), tz)
            if seg == "business":
                biz.append(r)
            else:
                off.append(r)

        server_segs = {}
        for metric, col in metric_cols.items():
            biz_vals = [r[col] for r in biz if r.get(col) is not None and r[col] >= 0]
            off_vals = [r[col] for r in off if r.get(col) is not None and r[col] >= 0]

            biz_mean = round(sum(biz_vals) / len(biz_vals), 1) if biz_vals else None
            off_mean = round(sum(off_vals) / len(off_vals), 1) if off_vals else None

            server_segs[metric] = {
                "business_hours": {"mean": biz_mean, "count": len(biz_vals)},
                "off_hours": {"mean": off_mean, "count": len(off_vals)},
            }

            # Collect for fleet annotation
            if metric not in all_means:
                all_means[metric] = {}
            if biz_mean is not None:
                all_means[metric][name] = biz_mean

        result["servers"][name] = server_segs

    # Compute fleet annotations using MAD (median absolute deviation)
    for metric, server_means in all_means.items():
        if len(server_means) < 2:
            continue

        values = list(server_means.values())
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        fleet_median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2

        # MAD
        abs_devs = sorted([abs(v - fleet_median) for v in values])
        mad = abs_devs[len(abs_devs) // 2] if abs_devs else 0
        mad = max(mad, 1.0)  # Floor to avoid division by zero

        for server_name, mean_val in server_means.items():
            deviation = (mean_val - fleet_median) / mad
            if abs(deviation) > 2.0:
                direction = "above" if deviation > 0 else "below"
                result["annotations"].append({
                    "server": server_name,
                    "metric": metric,
                    "direction": direction,
                    "value": mean_val,
                    "fleet_median": round(fleet_median, 1),
                    "deviation_mad": round(abs(deviation), 1),
                })

    return jsonify(result)


@api_bp.route("/reports/json/metrics")
def api_json_metrics():
    """JSON export of metric history with anomaly indicators."""
    from reports import generate_json_metrics
    db = _shared._db
    server = request.args.get("server", "").strip() or None
    hours = int(request.args.get("hours", "24"))
    hours = max(1, min(hours, 8760))
    data = generate_json_metrics(db, server_name=server, hours=hours)
    # Return as downloadable JSON file
    output = json.dumps(data, indent=2, default=str)
    resp = make_response(output)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f'attachment; filename="prism_metrics_{server or "all"}_{hours}h.json"'
    return resp


@api_bp.route("/reports/json/events")
def api_json_events():
    """JSON export of events with correlation/ack info."""
    from reports import generate_json_events
    db = _shared._db
    server = request.args.get("server", "").strip() or None
    limit = int(request.args.get("limit", "500"))
    limit = max(1, min(limit, 10000))
    data = generate_json_events(db, server_name=server, limit=limit)
    output = json.dumps(data, indent=2, default=str)
    resp = make_response(output)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f'attachment; filename="prism_events_{server or "all"}.json"'
    return resp


# /reports/capacity and /reports/csv/capacity were removed on 2026-08-06,
# together with reports.generate_capacity_report. They served the Capacity
# Planning section, which the Fleet Report absorbed: the same forecasts now
# come from the same single scan that produces health and availability, so
# keeping a second emitter meant two code paths computing one answer.
# /api/reports/fleet and /api/reports/csv/fleet replace both.


@api_bp.route("/reports/fleet")
def api_fleet_report():
    """Health-led fleet report — see docs/plans/FLEET_REPORT_SPEC.md.

    Replaces the two requests the Reports page used to make (/api/sla/summary
    and /api/reports/capacity, 58 queries over the same rows) with one scan.
    """
    try:
        hours = int(request.args.get("hours", 720))
    except (TypeError, ValueError):
        hours = 720
    hours = max(1, min(hours, 8760))

    scope = (request.args.get("scope") or "attention").strip().lower()
    if scope not in ("attention", "all"):
        scope = "attention"

    try:
        from analytics import compute_fleet_report
        config = _shared._config
        settings = config.get_settings() if config else {}
        servers_cfg = config.get_servers() if config else []
        data = compute_fleet_report(
            _shared._db, servers_cfg, hours=hours,
            poll_interval_seconds=settings.get("poll_interval_seconds", 300),
            scope=scope,
        )
        return jsonify(data)
    except Exception as e:
        logger.exception("Fleet report generation failed")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/reports/csv/fleet")
def api_csv_fleet():
    """CSV of the fleet report — one row per server, always full scope.

    Exports what the section shows plus the fields that only fit in the
    expanded row, so the download is not a subset of the screen.
    """
    import csv as csv_mod
    from analytics import compute_fleet_report

    try:
        hours = int(request.args.get("hours", 720))
    except (TypeError, ValueError):
        hours = 720
    hours = max(1, min(hours, 8760))

    config = _shared._config
    settings = config.get_settings() if config else {}
    servers_cfg = config.get_servers() if config else []
    data = compute_fleet_report(
        _shared._db, servers_cfg, hours=hours,
        poll_interval_seconds=settings.get("poll_interval_seconds", 300),
        scope="all",
    )

    output = io.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow([
        "Server", "Needs attention", "Attention reasons",
        "Health %", "Availability %", "Observed hours", "Degraded % of up",
        "Outages", "Downtime minutes", "MTTR minutes",
        "Top driver", "Top driver % of degraded", "Top driver avg %",
        "Top driver threshold", "No threshold breach %",
        "Soonest capacity metric", "Days to threshold", "Capacity risk",
    ])
    for s in data["servers"]:
        driver = s["drivers"][0] if s["drivers"] else None
        soonest = min(
            (c for c in s["capacity"] if c["days_to_threshold"] is not None),
            key=lambda c: c["days_to_threshold"], default=None)
        writer.writerow([
            s["name"],
            "yes" if s["attention"] else "no",
            " ".join(s["attention_reasons"]),
            "" if s["health_percent"] is None else f'{s["health_percent"]:.2f}',
            "" if s["availability_percent"] is None else f'{s["availability_percent"]:.2f}',
            f'{s["observed_minutes"] / 60:.1f}',
            "" if s["degraded_percent_of_up"] is None else f'{s["degraded_percent_of_up"]:.1f}',
            s["outage_count"],
            f'{s["total_downtime_minutes"]:.0f}',
            "" if s["mttr_minutes"] is None else f'{s["mttr_minutes"]:.0f}',
            driver["metric"] if driver else "",
            f'{driver["percent_of_degraded"]:.1f}' if driver else "",
            f'{driver["avg_value"]:.1f}' if driver else "",
            driver["threshold"] if driver else "",
            f'{s["no_threshold_breach"]["percent_of_degraded"]:.1f}',
            soonest["metric"] if soonest else "",
            f'{soonest["days_to_threshold"]:.0f}' if soonest else "",
            soonest["risk"] if soonest else "",
        ])

    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="prism_fleet_report_{hours}h.csv"'
    return resp


@api_bp.route("/reports/pdf/comparison")
def api_pdf_comparison():
    """Download comparison PDF for selected servers."""
    from reports import generate_comparison_pdf
    db = _shared._db
    config = _shared._config
    lang = _shared._config.get_settings().get("language", "en")
    t = get_translations(lang)

    servers_param = request.args.get("servers", "")
    hours = int(request.args.get("hours", "24"))
    hours = max(1, min(hours, 8760))
    server_names = [s.strip() for s in servers_param.split(",") if s.strip()]

    if len(server_names) < 2:
        return jsonify({"error": "Select at least 2 servers"}), 400

    pdf_bytes = generate_comparison_pdf(db, config, t, server_names, hours)
    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="prism_comparison_{hours}h.pdf"'
    return resp
