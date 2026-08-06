"""Report generation for Prism — CSV exports, JSON exports, and PDF reports."""

import csv
import io
import logging
import math
import zoneinfo
from datetime import datetime, timezone
from database import Database

logger = logging.getLogger("prism.reports")

# ── Shared helpers for anomaly detection and data gathering ──

METRIC_KEYS = ["cpu_percent", "ram_percent", "disk_c_percent", "disk_d_percent"]
METRIC_LABELS = {"cpu_percent": "CPU", "ram_percent": "RAM",
                 "disk_c_percent": "Disk C", "disk_d_percent": "Disk D"}
ROLLING_WINDOW = 24  # number of recent values to keep for anomaly detection


def _build_ack_lookup(db: Database) -> dict:
    """Build a lookup dict {(server_name, metric): ack_dict} from active acknowledgments."""
    acks = db.get_active_acknowledgments()
    lookup: dict[tuple[str, str], dict] = {}
    for a in acks:
        key = (a["server_name"], a["metric"])
        # Keep the most recent ack per (server, metric)
        if key not in lookup:
            lookup[key] = a
    return lookup


def _compute_anomaly(rolling: dict, server_name: str, row: dict) -> tuple[int, str, str]:
    """Check if any metric deviates > 2 sigma from the rolling mean.

    Returns (anomaly_flag, anomaly_metric, anomaly_direction).
    rolling is mutated in place to track recent values per server/metric.
    """
    for mk in METRIC_KEYS:
        val = row.get(mk)
        if val is None:
            continue
        buf_key = (server_name, mk)
        buf = rolling.setdefault(buf_key, [])
        if len(buf) >= 2:
            mean = sum(buf) / len(buf)
            variance = sum((x - mean) ** 2 for x in buf) / len(buf)
            std = math.sqrt(variance) if variance > 0 else 0.0
            if std > 0 and abs(val - mean) > 2 * std:
                direction = "above" if val > mean else "below"
                # Append current value to rolling window AFTER detection
                buf.append(val)
                if len(buf) > ROLLING_WINDOW:
                    buf.pop(0)
                return 1, METRIC_LABELS.get(mk, mk), direction
        # Append to rolling window
        buf.append(val)
        if len(buf) > ROLLING_WINDOW:
            buf.pop(0)
    return 0, "", ""


def _compute_rate_of_change(prev_row: dict | None, row: dict) -> str:
    """Max absolute difference between current and previous row across all metrics."""
    if prev_row is None:
        return ""
    max_delta = 0.0
    for mk in METRIC_KEYS:
        cur = row.get(mk)
        prv = prev_row.get(mk)
        if cur is not None and prv is not None:
            max_delta = max(max_delta, abs(cur - prv))
    return f"{max_delta:.2f}"


def _gather_metrics_rows(db: Database, server_name: str | None = None,
                         hours: int = 24) -> list[dict]:
    """Gather metric history rows with anomaly indicators.

    Returns list of dicts with keys: server, timestamp, cpu, ram, disk_c, disk_d,
    status, anomaly_flag, anomaly_metric, anomaly_direction, rate_of_change.
    """
    rolling: dict[tuple[str, str], list[float]] = {}
    prev_rows: dict[str, dict] = {}  # last row per server for rate-of-change
    result = []

    if server_name:
        server_names = [server_name]
    else:
        latest = db.get_latest_all()
        server_names = sorted(m["server_name"] for m in latest)

    for name in server_names:
        rows = db.get_server_history(name, hours=hours)
        for r in rows:
            anomaly_flag, anomaly_metric, anomaly_direction = _compute_anomaly(rolling, name, r)
            rate_of_change = _compute_rate_of_change(prev_rows.get(name), r)
            prev_rows[name] = r

            result.append({
                "server": name,
                "timestamp": r["timestamp"],
                "cpu": r.get("cpu_percent", ""),
                "ram": r.get("ram_percent", ""),
                "disk_c": r.get("disk_c_percent", ""),
                "disk_d": r.get("disk_d_percent", ""),
                "status": r.get("status", ""),
                "anomaly_flag": anomaly_flag,
                "anomaly_metric": anomaly_metric,
                "anomaly_direction": anomaly_direction,
                "rate_of_change": rate_of_change,
            })

    return result


def _gather_events_rows(db: Database, server_name: str | None = None,
                        limit: int = 500) -> list[dict]:
    """Gather event rows with correlation and acknowledgment info.

    Returns list of dicts with keys: server, timestamp, type, metric, value,
    threshold, message, correlation_id, acknowledged_at, snoozed_until.
    """
    if server_name:
        events = db.get_server_events(server_name, limit=limit)
    else:
        events = db.get_recent_events(limit=limit)

    ack_lookup = _build_ack_lookup(db)
    result = []

    for e in events:
        srv = e.get("server_name", "")
        metric = e.get("metric", "")
        ack = ack_lookup.get((srv, metric))

        result.append({
            "server": srv,
            "timestamp": e.get("timestamp", ""),
            "type": e.get("event_type", ""),
            "metric": metric,
            "value": e.get("value", ""),
            "threshold": e.get("threshold", ""),
            "message": e.get("message", ""),
            "correlation_id": e.get("correlation_id", ""),
            "acknowledged_at": ack["created_at"] if ack and ack["ack_type"] == "acknowledged" else "",
            "snoozed_until": ack["snooze_until"] if ack and ack["ack_type"] == "snoozed" and ack.get("snooze_until") else "",
        })

    return result


def generate_csv_metrics(db: Database, server_name: str | None = None,
                         hours: int = 24) -> str:
    """Generate CSV of metric history with anomaly indicators.

    If server_name is None, exports all servers.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Server", "Timestamp", "CPU %", "RAM %", "Disk C %", "Disk D %",
                      "Status", "Anomaly Flag", "Anomaly Metric", "Anomaly Direction",
                      "Rate of Change"])

    for row in _gather_metrics_rows(db, server_name=server_name, hours=hours):
        writer.writerow([
            row["server"], row["timestamp"],
            row["cpu"], row["ram"], row["disk_c"], row["disk_d"],
            row["status"], row["anomaly_flag"], row["anomaly_metric"],
            row["anomaly_direction"], row["rate_of_change"],
        ])

    return output.getvalue()


def generate_csv_events(db: Database, server_name: str | None = None,
                        limit: int = 500) -> str:
    """Generate CSV of events with correlation and acknowledgment info."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Server", "Timestamp", "Type", "Metric", "Value", "Threshold",
                      "Message", "Correlation ID", "Acknowledged At", "Snoozed Until"])

    for row in _gather_events_rows(db, server_name=server_name, limit=limit):
        writer.writerow([
            row["server"], row["timestamp"], row["type"], row["metric"],
            row["value"], row["threshold"], row["message"],
            row["correlation_id"], row["acknowledged_at"], row["snoozed_until"],
        ])

    return output.getvalue()


def _draw_sparkline(values, width=120, height=30, line_color=None):
    """Draw a mini sparkline chart using ReportLab graphics."""
    from reportlab.graphics.shapes import Drawing, PolyLine, Rect
    from reportlab.lib.colors import HexColor

    if not line_color:
        line_color = HexColor("#2563EB")

    d = Drawing(width, height)

    if not values or len(values) < 2:
        return d

    # Background
    d.add(Rect(0, 0, width, height, fillColor=HexColor("#F9FAFB"), strokeColor=None))

    # Normalize values to fit in drawing
    min_val = min(values)
    max_val = max(values)
    val_range = max_val - min_val if max_val != min_val else 1

    padding = 2
    plot_w = width - 2 * padding
    plot_h = height - 2 * padding

    points = []
    step = plot_w / (len(values) - 1) if len(values) > 1 else 0
    for i, v in enumerate(values):
        x = padding + i * step
        y = padding + ((v - min_val) / val_range) * plot_h
        points.append(x)
        points.append(y)

    d.add(PolyLine(points, strokeColor=line_color, strokeWidth=1))
    return d


# ── Brand typeface for the PDF ───────────────────────────────────────────
# ReportLab cannot read WOFF2 — registering static/vendor/fonts/*.woff2 raises
# TTFError: Not a recognized TrueType font. The two .ttf files beside them are
# the SAME faces, converted from those woff2s with fontTools so no new font is
# introduced and the vendored OFL.txt still covers them.
#
# Used ONLY for the title and section headings. Body copy and every table stay
# on Helvetica: the subset is 252 glyphs with no verifiable tnum, and Chakra
# Petch's squared bowls make 0/8, 5/6 and 1/7 confusable at the 8-9pt these
# tables run at — a misread digit in an availability figure is a factual error
# in a document someone signs off on.
_BRAND_FONTS_READY = None


def _register_brand_fonts() -> str:
    """Register the brand display face; return the font name to use for
    headings, falling back to Helvetica-Bold if the TTFs are absent.

    Idempotent — ReportLab keeps a process-global font registry and a second
    registration of the same name is wasteful, so the result is cached.
    """
    global _BRAND_FONTS_READY
    if _BRAND_FONTS_READY is not None:
        return _BRAND_FONTS_READY
    try:
        from pathlib import Path as _P
        from reportlab.pdfbase import pdfmetrics as _pm
        from reportlab.pdfbase.ttfonts import TTFont as _TTF
        base = _P(__file__).parent / "static" / "vendor" / "fonts"
        _pm.registerFont(_TTF("ChakraPetch-SemiBold", str(base / "ChakraPetch-SemiBold.ttf")))
        _pm.registerFont(_TTF("ChakraPetch-Medium", str(base / "ChakraPetch-Medium.ttf")))
        _BRAND_FONTS_READY = "ChakraPetch-SemiBold"
    except Exception:
        # Missing or unreadable font must never break report generation.
        logger.warning("Brand fonts unavailable; PDF headings fall back to Helvetica",
                       exc_info=True)
        _BRAND_FONTS_READY = "Helvetica-Bold"
    return _BRAND_FONTS_READY


def generate_pdf_report(db: Database, config_manager, translations: dict) -> bytes:
    """Generate a PDF management report with server health overview.

    Includes executive summary, status overview, server details, sparklines,
    anomaly summary, capacity forecast, outage log, and recent events.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    )
    from analytics import forecast_metric, compute_uptime_stats

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)

    display_font = _register_brand_fonts()

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PrismTitle", parent=styles["Title"],
                                 fontName=display_font,
                                 fontSize=22, spaceAfter=6,
                                 textColor=colors.HexColor("#5B21B6"))
    subtitle_style = ParagraphStyle("PrismSubtitle", parent=styles["Normal"],
                                    fontSize=10, textColor=colors.grey,
                                    spaceAfter=14)
    h2_style = ParagraphStyle("PrismH2", parent=styles["Heading2"],
                              fontName=display_font,
                              fontSize=14, spaceBefore=16, spaceAfter=8,
                              textColor=colors.HexColor("#5B21B6"))
    normal = styles["Normal"]

    elements = []

    # ── Title — use configured timezone ──
    elements.append(Paragraph("Prism — Server Health Report", title_style))
    _settings = config_manager.get_settings()
    try:
        _tz = zoneinfo.ZoneInfo(_settings.get("timezone", "Europe/Berlin"))
        _now = datetime.now(timezone.utc).astimezone(_tz)
        _df = _settings.get("date_format", "DD.MM.YYYY")
        if _df == "DD.MM.YYYY":
            _ds = _now.strftime("%d.%m.%Y")
        elif _df == "YYYY-MM-DD":
            _ds = _now.strftime("%Y-%m-%d")
        elif _df == "MM/DD/YYYY":
            _ds = _now.strftime("%m/%d/%Y")
        else:
            _ds = _now.strftime("%d/%m/%Y")
        _ts = _now.strftime("%I:%M %p") if _settings.get("time_format") == "12h" else _now.strftime("%H:%M")
        now = f"{_ds} {_ts}"
    except Exception:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    elements.append(Paragraph(f"Generated: {now}", subtitle_style))
    elements.append(HRFlowable(width="100%", color=colors.HexColor("#2563EB"),
                               thickness=1, spaceAfter=12))

    # ── Executive Summary ──
    summary = db.get_status_summary()
    latest = db.get_latest_all()
    total = summary.get("total", 0)
    healthy_count = summary.get("healthy", 0)
    fleet_health = round((healthy_count / total) * 100, 1) if total > 0 else 0.0

    # Count anomalies from recent events
    anomaly_events = db.get_recent_events(limit=500)
    anomaly_count = sum(1 for e in anomaly_events if e.get("event_type") in ("warning", "critical", "anomaly"))

    # Worst uptime across fleet
    server_names_all = sorted(m["server_name"] for m in latest) if latest else []
    worst_uptime = 100.0
    for sn in server_names_all:
        try:
            up_stats = compute_uptime_stats(db, sn, hours=168)  # 7 days
            if up_stats["uptime_percent"] < worst_uptime:
                worst_uptime = up_stats["uptime_percent"]
        except Exception:
            pass

    exec_style = ParagraphStyle("ExecSummary", parent=normal, fontSize=10,
                                spaceAfter=4)
    elements.append(Paragraph("Executive Summary", h2_style))
    elements.append(Paragraph(
        f"<b>Fleet Health Score:</b> {fleet_health}% "
        f"({healthy_count}/{total} servers healthy)", exec_style))
    elements.append(Paragraph(
        f"<b>Active Anomalies (24h):</b> {anomaly_count}", exec_style))
    elements.append(Paragraph(
        f"<b>Worst 7-day Uptime:</b> {worst_uptime:.2f}%", exec_style))
    elements.append(Spacer(1, 12))

    # ── Status Overview ──
    elements.append(Paragraph(translations.get("status_overview", "Status Overview"), h2_style))

    summary_data = [
        [translations.get("total", "Total"),
         translations.get("healthy", "Healthy"),
         translations.get("warning", "Warning"),
         translations.get("critical", "Critical"),
         translations.get("offline", "Offline")],
        [str(summary.get("total", 0)),
         str(summary.get("healthy", 0)),
         str(summary.get("warning", 0)),
         str(summary.get("critical", 0)),
         str(summary.get("offline", 0))],
    ]
    summary_table = Table(summary_data, colWidths=[80, 80, 80, 80, 80])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 12))

    # ── Server Details Table ──
    elements.append(Paragraph(translations.get("servers", "Servers"), h2_style))

    servers_config = {s.name: s for s in config_manager.get_servers()}

    if latest:
        server_data = [["Server", "Host", "Status",
                        translations.get("cpu", "CPU"),
                        translations.get("ram", "RAM"),
                        translations.get("disk_c", "Disk C:"),
                        translations.get("disk_d", "Disk D:")]]
        for m in latest:
            name = m["server_name"]
            cfg = servers_config.get(name)
            status = m.get("status", "unknown")
            server_data.append([
                name,
                cfg.host if cfg else "?",
                status.capitalize(),
                f"{m['cpu_percent']:.1f}%" if m.get("cpu_percent") is not None else "—",
                f"{m['ram_percent']:.1f}%" if m.get("ram_percent") is not None else "—",
                f"{m['disk_c_percent']:.1f}%" if m.get("disk_c_percent") is not None else "—",
                f"{m['disk_d_percent']:.1f}%" if m.get("disk_d_percent") is not None else "—",
            ])

        col_widths = [100, 90, 60, 50, 50, 55, 55]
        server_table = Table(server_data, colWidths=col_widths)

        # Color status cells
        status_colors = {
            "Healthy": colors.HexColor("#10B981"),
            "Warning": colors.HexColor("#F59E0B"),
            "Critical": colors.HexColor("#DC2626"),
            "Offline": colors.HexColor("#6B7280"),
        }
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ]
        for row_idx in range(1, len(server_data)):
            status_text = server_data[row_idx][2]
            if status_text in status_colors:
                style_cmds.append(
                    ("TEXTCOLOR", (2, row_idx), (2, row_idx), status_colors[status_text])
                )

        server_table.setStyle(TableStyle(style_cmds))
        elements.append(server_table)
    else:
        elements.append(Paragraph(translations.get("no_servers", "No servers configured."), normal))

    elements.append(Spacer(1, 16))

    # ── Per-Server Sparklines ──
    elements.append(Paragraph("Resource Trend Sparklines (24h)", h2_style))
    spark_colors = [
        colors.HexColor("#2563EB"),  # CPU - blue
        colors.HexColor("#10B981"),  # RAM - green
        colors.HexColor("#F59E0B"),  # Disk C - amber
        colors.HexColor("#DC2626"),  # Disk D - red
    ]
    spark_metric_keys = ["cpu_percent", "ram_percent", "disk_c_percent", "disk_d_percent"]
    spark_metric_labels = ["CPU", "RAM", "Disk C", "Disk D"]

    for sn in server_names_all:
        try:
            history = db.get_server_history(sn, hours=24)
        except Exception:
            continue
        if not history:
            continue

        spark_header = [sn] + spark_metric_labels
        spark_row = [""]

        for i, mk in enumerate(spark_metric_keys):
            vals = [h[mk] for h in history if h.get(mk) is not None]
            if vals and len(vals) >= 2:
                spark_row.append(_draw_sparkline(vals, width=100, height=25,
                                                 line_color=spark_colors[i]))
            else:
                spark_row.append("—")

        spark_table = Table([spark_header, spark_row],
                            colWidths=[90, 105, 105, 105, 105])
        spark_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        elements.append(spark_table)
        elements.append(Spacer(1, 6))

    elements.append(Spacer(1, 8))

    # ── Anomaly Summary ──
    elements.append(Paragraph("Anomaly Summary", h2_style))
    anomaly_rows = [e for e in anomaly_events
                    if e.get("event_type") in ("warning", "critical", "anomaly")]
    if anomaly_rows:
        anom_header = ["Time", "Server", "Type", "Metric", "Value", "Message"]
        anom_data = [anom_header]
        for e in anomaly_rows[:30]:  # Limit to 30 most recent
            anom_data.append([
                e.get("timestamp", "")[:16],
                e.get("server_name", ""),
                e.get("event_type", ""),
                e.get("metric", "—"),
                f'{e["value"]:.1f}' if e.get("value") is not None else "—",
                (e.get("message", "") or "")[:60],
            ])

        anom_table = Table(anom_data, colWidths=[90, 80, 55, 55, 45, 135])
        anom_style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DC2626")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FEF2F2")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        # Color-code by event type
        for row_idx in range(1, len(anom_data)):
            etype = anom_data[row_idx][2]
            if etype == "critical":
                anom_style_cmds.append(
                    ("TEXTCOLOR", (2, row_idx), (2, row_idx), colors.HexColor("#DC2626")))
            elif etype == "warning":
                anom_style_cmds.append(
                    ("TEXTCOLOR", (2, row_idx), (2, row_idx), colors.HexColor("#F59E0B")))

        anom_table.setStyle(TableStyle(anom_style_cmds))
        elements.append(anom_table)
    else:
        elements.append(Paragraph("No anomalies detected in the recent period.", normal))

    elements.append(Spacer(1, 12))

    # ── Capacity Forecast ──
    elements.append(Paragraph("Capacity Forecast", h2_style))

    forecast_metric_map = {
        "cpu": ("CPU", "cpu_percent"),
        "ram": ("RAM", "ram_percent"),
        "disk_c": ("Disk C", "disk_c_percent"),
        "disk_d": ("Disk D", "disk_d_percent"),
    }
    cap_header = ["Server", "Metric", "Current %", "Trend/day", "Days to 90%", "Risk"]
    cap_data = [cap_header]

    for sn in server_names_all:
        sn_latest = db.get_latest(sn) if hasattr(db, "get_latest") else None
        for metric_key, (metric_label, db_col) in forecast_metric_map.items():
            try:
                fc = forecast_metric(db, sn, metric=metric_key,
                                     hours=720, target_percent=90.0)
            except Exception:
                continue

            current = fc.get("current")
            if current is None:
                # Fall back to latest reading
                if sn_latest and sn_latest.get(db_col) is not None:
                    current = round(sn_latest[db_col], 1)
                else:
                    continue

            trend = fc.get("trend_per_day", 0.0)
            days_to_full = fc.get("days_until_full")

            # Determine risk level
            if current >= 90:
                risk = "HIGH"
                days_to_full = 0
            elif days_to_full is not None and days_to_full <= 30:
                risk = "HIGH"
            elif days_to_full is not None and days_to_full <= 90:
                risk = "MEDIUM"
            else:
                risk = "LOW"

            trend_str = f"{trend:+.2f}" if trend is not None else "—"
            days_str = str(int(days_to_full)) if days_to_full is not None else "—"

            cap_data.append([
                sn, metric_label, f"{current:.1f}",
                trend_str, days_str, risk,
            ])

    if len(cap_data) > 1:
        cap_table = Table(cap_data, colWidths=[90, 60, 60, 60, 65, 55])
        cap_style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ]
        # Color-code risk column
        for row_idx in range(1, len(cap_data)):
            risk_val = cap_data[row_idx][5]
            if risk_val == "HIGH":
                cap_style_cmds.append(
                    ("TEXTCOLOR", (5, row_idx), (5, row_idx), colors.HexColor("#DC2626")))
                cap_style_cmds.append(
                    ("FONTNAME", (5, row_idx), (5, row_idx), "Helvetica-Bold"))
            elif risk_val == "MEDIUM":
                cap_style_cmds.append(
                    ("TEXTCOLOR", (5, row_idx), (5, row_idx), colors.HexColor("#F59E0B")))

        cap_table.setStyle(TableStyle(cap_style_cmds))
        elements.append(cap_table)
    else:
        elements.append(Paragraph("Insufficient data for capacity forecasting.", normal))

    elements.append(Spacer(1, 12))

    # ── Outage Log (last 7 days) ──
    elements.append(Paragraph("Outage Log (7 days)", h2_style))

    all_outages = []
    for sn in server_names_all:
        try:
            up_stats = compute_uptime_stats(db, sn, hours=168)
        except Exception:
            continue
        for outage in up_stats.get("outages", []):
            all_outages.append({
                "server": sn,
                "start": outage.get("start_time", "—"),
                "end": outage.get("end_time") or "Ongoing",
                "duration": outage.get("duration_minutes", 0),
                "severity": outage.get("worst_severity", "unknown"),
            })

    if all_outages:
        # Sort by start time descending
        all_outages.sort(key=lambda o: o["start"], reverse=True)
        outage_header = ["Server", "Start", "End", "Duration (min)", "Severity"]
        outage_data = [outage_header]
        for o in all_outages[:25]:  # Limit to 25
            outage_data.append([
                o["server"],
                o["start"][:16] if o["start"] != "—" else "—",
                o["end"][:16] if o["end"] != "Ongoing" else "Ongoing",
                f'{o["duration"]:.1f}',
                o["severity"].capitalize(),
            ])

        outage_table = Table(outage_data, colWidths=[90, 95, 95, 75, 70])
        outage_style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6B7280")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (3, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ]
        # Color-code severity
        sev_colors = {
            "Critical": colors.HexColor("#DC2626"),
            "Offline": colors.HexColor("#6B7280"),
            "Warning": colors.HexColor("#F59E0B"),
        }
        for row_idx in range(1, len(outage_data)):
            sev_text = outage_data[row_idx][4]
            if sev_text in sev_colors:
                outage_style_cmds.append(
                    ("TEXTCOLOR", (4, row_idx), (4, row_idx), sev_colors[sev_text]))

        outage_table.setStyle(TableStyle(outage_style_cmds))
        elements.append(outage_table)
    else:
        elements.append(Paragraph("No outages recorded in the last 7 days.", normal))

    elements.append(Spacer(1, 16))

    # ── Recent Events ──
    elements.append(Paragraph(translations.get("recent_events", "Recent Events"), h2_style))
    events = db.get_recent_events(limit=30)
    if events:
        event_data = [["Time", "Server", translations.get("type", "Type"),
                       translations.get("message", "Message")]]
        for e in events:
            ts = e.get("timestamp", "")[:16]
            event_data.append([
                ts, e.get("server_name", ""),
                e.get("event_type", ""), e.get("message", "")[:80],
            ])

        event_table = Table(event_data, colWidths=[100, 90, 60, 210])
        event_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.append(event_table)
    else:
        elements.append(Paragraph(translations.get("no_events", "No events."), normal))

    # ── Footer ──
    elements.append(Spacer(1, 24))
    elements.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.5))
    elements.append(Spacer(1, 4))
    footer_style = ParagraphStyle("Footer", parent=normal, fontSize=8,
                                  textColor=colors.grey)
    elements.append(Paragraph("Prism Server Monitoring System", footer_style))

    doc.build(elements)
    return buf.getvalue()


def generate_json_metrics(db: Database, server_name: str | None = None,
                          hours: int = 24) -> list[dict]:
    """Generate JSON array of metric history with anomaly indicators.

    Returns list of dicts, each with: server, timestamp, cpu, ram, disk_c,
    disk_d, status, anomaly_flag, anomaly_metric, anomaly_direction,
    rate_of_change.
    """
    return _gather_metrics_rows(db, server_name=server_name, hours=hours)


def generate_json_events(db: Database, server_name: str | None = None,
                         limit: int = 500) -> list[dict]:
    """Generate JSON array of events with correlation/ack info.

    Returns list of dicts, each with: server, timestamp, type, metric, value,
    threshold, message, correlation_id, acknowledged_at, snoozed_until.
    """
    return _gather_events_rows(db, server_name=server_name, limit=limit)


# Capacity metrics, in the order they are reported. Shared with
# analytics.compute_fleet_report so the two emitters cannot disagree about
# which metrics exist or what they are called.
CAPACITY_METRIC_MAP = {
    "cpu": ("CPU", "cpu_percent"),
    "ram": ("RAM", "ram_percent"),
    "disk_c": ("Disk C", "disk_c_percent"),
    "disk_d": ("Disk D", "disk_d_percent"),
}


def capacity_row(server: str, metric_key: str, metric_label: str,
                 forecast: dict | None, current: float | None) -> dict:
    """Map one ``forecast_metric()`` result onto a capacity-report row.

    Single source of truth for the RENAME that happens here: forecast_metric
    emits ``trend_per_day`` and ``days_until_full``, while every consumer of
    the capacity report reads ``growth_rate`` and ``days_to_threshold``.

    Two of the five defects in docs/plans/HANDOFF.md §3 were a consumer reading
    the pre-rename name and silently getting ``undefined`` — a permanent
    em-dash and an ``Infinity`` sort key that nobody noticed. Adding a second
    emitter of these rows (analytics.compute_fleet_report) is precisely the
    situation where that recurs, so both emitters call this rather than each
    doing the rename themselves.
    """
    growth_rate = forecast.get("trend_per_day", 0.0) if forecast else 0.0
    days_to_threshold = forecast.get("days_until_full") if forecast else None

    # Determine risk level
    if days_to_threshold is not None and days_to_threshold <= 30:
        risk = "high"
    elif days_to_threshold is not None and days_to_threshold <= 90:
        risk = "medium"
    else:
        risk = "low"

    # Override: if current usage already >= 90%, risk is high
    if current is not None and current >= 90:
        risk = "high"
        days_to_threshold = 0

    return {
        "server": server,
        "metric": metric_label,
        "metric_key": metric_key,
        "current": current,
        "growth_rate": growth_rate,
        "days_to_threshold": days_to_threshold,
        "risk": risk,
    }


# generate_capacity_report was removed on 2026-08-06 along with the two routes
# that served it. It read each server's 720-hour history separately (29 reads);
# analytics.compute_fleet_report now derives the same forecasts from the single
# fleet-wide scan it already performs for health and availability.
#
# CAPACITY_METRIC_MAP and capacity_row() above survive it deliberately: they are
# the shared definition of what a capacity row IS, and compute_fleet_report goes
# through them so the field names cannot drift.


def generate_comparison_pdf(db: Database, config_manager, translations: dict,
                            server_names: list[str], hours: int = 24) -> bytes:
    """Generate a PDF comparing selected servers over a time period."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PrismTitle", parent=styles["Title"],
                                 fontSize=20, spaceAfter=6)
    subtitle_style = ParagraphStyle("PrismSubtitle", parent=styles["Normal"],
                                    fontSize=10, textColor=colors.grey, spaceAfter=14)
    h2_style = ParagraphStyle("PrismH2", parent=styles["Heading2"],
                              fontSize=14, spaceBefore=16, spaceAfter=8)
    normal = styles["Normal"]

    elements = []

    # Title
    elements.append(Paragraph("Prism — Server Comparison Report", title_style))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    elements.append(Paragraph(
        f"Generated: {now_str} | Servers: {', '.join(server_names)} | Period: {hours}h",
        subtitle_style))
    elements.append(HRFlowable(width="100%", color=colors.HexColor("#2563EB"),
                               thickness=1, spaceAfter=12))

    # ── Current Metrics Comparison Table ──
    elements.append(Paragraph(
        translations.get("server_comparison", "Server Comparison"), h2_style))

    header = ["Server", "Status", "CPU %", "RAM %", "Disk C %", "Disk D %"]
    table_data = [header]

    for name in server_names:
        latest = db.get_latest(name) if hasattr(db, "get_latest") else None
        if latest:
            status = latest.get("status", "unknown")
            table_data.append([
                name,
                status.capitalize(),
                f'{latest["cpu_percent"]:.1f}' if latest.get("cpu_percent") is not None else "—",
                f'{latest["ram_percent"]:.1f}' if latest.get("ram_percent") is not None else "—",
                f'{latest["disk_c_percent"]:.1f}' if latest.get("disk_c_percent") is not None else "—",
                f'{latest["disk_d_percent"]:.1f}' if latest.get("disk_d_percent") is not None else "—",
            ])
        else:
            table_data.append([name, "Unknown", "—", "—", "—", "—"])

    t = Table(table_data, colWidths=[90, 70, 60, 60, 60, 60])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 12))

    # ── Statistics Comparison ──
    elements.append(Paragraph(
        translations.get("statistics", "Statistics"), h2_style))

    metric_map = {
        "cpu_percent": "CPU",
        "ram_percent": "RAM",
        "disk_c_percent": "Disk C",
        "disk_d_percent": "Disk D",
    }

    for metric_key, metric_label in metric_map.items():
        stats_header = [f"{metric_label}", "Min", "Max", "Mean", "Median", "P95", "StdDev"]
        stats_data = [stats_header]

        for name in server_names:
            history = db.get_server_history(name, hours=hours)
            values = [h[metric_key] for h in history if h.get(metric_key) is not None]

            if values:
                sorted_v = sorted(values)
                n = len(sorted_v)
                mean_v = sum(values) / n
                median_v = sorted_v[n // 2] if n % 2 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2
                p95_idx = min(int(n * 0.95), n - 1)
                variance = sum((x - mean_v) ** 2 for x in values) / n
                stddev = math.sqrt(variance) if variance > 0 else 0.0

                stats_data.append([
                    name,
                    f"{sorted_v[0]:.1f}",
                    f"{sorted_v[-1]:.1f}",
                    f"{mean_v:.1f}",
                    f"{median_v:.1f}",
                    f"{sorted_v[p95_idx]:.1f}",
                    f"{stddev:.1f}",
                ])
            else:
                stats_data.append([name, "—", "—", "—", "—", "—", "—"])

        st = Table(stats_data, colWidths=[90, 50, 50, 50, 55, 50, 55])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6B7280")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(st)
        elements.append(Spacer(1, 8))

    # ── Sparklines for Each Server ──
    elements.append(Paragraph("Trend Sparklines", h2_style))
    spark_colors = [
        colors.HexColor("#2563EB"),
        colors.HexColor("#10B981"),
        colors.HexColor("#F59E0B"),
        colors.HexColor("#DC2626"),
    ]

    for name in server_names:
        history = db.get_server_history(name, hours=hours)
        if not history:
            continue

        spark_header = [name, "CPU", "RAM", "Disk C", "Disk D"]
        spark_row = [""]

        for i, mk in enumerate(["cpu_percent", "ram_percent",
                                 "disk_c_percent", "disk_d_percent"]):
            vals = [h[mk] for h in history if h.get(mk) is not None]
            if vals and len(vals) >= 2:
                spark_row.append(_draw_sparkline(vals, width=100, height=25,
                                                 line_color=spark_colors[i]))
            else:
                spark_row.append("—")

        spark_table = Table([spark_header, spark_row],
                            colWidths=[90, 105, 105, 105, 105])
        spark_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        elements.append(spark_table)
        elements.append(Spacer(1, 6))

    # ── Footer ──
    elements.append(Spacer(1, 24))
    elements.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.5))
    elements.append(Spacer(1, 4))
    footer_style = ParagraphStyle("Footer", parent=normal, fontSize=8,
                                  textColor=colors.grey)
    elements.append(Paragraph(
        "Prism Server Monitoring System — Comparison Report", footer_style))

    doc.build(elements)
    return buf.getvalue()
