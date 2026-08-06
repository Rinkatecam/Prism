"""Baseline deviation detection engine for Prism.

Computes per-server, per-metric hourly baselines (168 slots = 7 days x 24 hours)
and detects when current values deviate significantly from normal behaviour.
"""

import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger("prism.baseline")

METRICS = ("cpu_percent", "ram_percent", "disk_c_percent", "disk_d_percent")
METRIC_SHORT = {"cpu_percent": "cpu", "ram_percent": "ram",
                "disk_c_percent": "disk_c", "disk_d_percent": "disk_d"}
SLOTS_PER_WEEK = 168  # 7 * 24


def _hour_of_week(dt):
    """Return 0-167 slot for a datetime (0 = Monday 00:00, 167 = Sunday 23:00)."""
    return dt.weekday() * 24 + dt.hour


def compute_baselines(db, server_name, weeks=4, timezone_str="Europe/Berlin"):
    """Compute hourly baselines for all metrics for a server.

    Returns total count of baseline slots written.
    """
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone_str)
    except Exception:
        tz = timezone.utc

    hours = weeks * 7 * 24
    rows = db.get_metric_history_raw(server_name, hours=hours)
    if not rows:
        return 0

    # Bucket values by (metric, hour_of_week)
    buckets = {}  # (metric, hour_of_week) -> [values]
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            ts_local = ts.astimezone(tz)
        except (ValueError, AttributeError):
            continue

        how = _hour_of_week(ts_local)
        for metric in METRICS:
            val = row.get(metric)
            if val is not None:
                key = (metric, how)
                buckets.setdefault(key, []).append(float(val))

    # Compute stats and upsert
    count = 0
    for (metric, how), values in buckets.items():
        n = len(values)
        if n < 2:
            continue
        avg = sum(values) / n
        variance = sum((v - avg) ** 2 for v in values) / (n - 1)
        stddev = math.sqrt(variance) if variance > 0 else 0.0
        db.upsert_baseline(server_name, METRIC_SHORT[metric], how, avg, stddev, n)
        count += 1

    logger.info("[%s] Computed %d baseline slots from %d readings", server_name, count, len(rows))
    return count


def assess_metrics(db, server_name, current_metrics, timezone_str="Europe/Berlin",
                   sigma_warning=2.0, sigma_critical=3.0, min_samples=10):
    """Classify EVERY current metric against its hour-of-week baseline slot.

    Unlike ``check_deviation`` (which only reports metrics that deviate),
    this returns an assessment for every metric that has a value — the
    fused-verdict engine needs to know "normal for this server" as a
    positive signal (downgrade authority), not just the absence of a
    deviation. See docs/plans/DETECTION_FUSION_PLAN.md §2.

    Returns:
        dict short_name -> {
            "state": "no-slot" | "normal" | "deviating",
            "severity": "warning"|"critical"|None,   (deviating only)
            "direction": "high"|"low"|None,
            "value": float,
            "baseline_avg": float,      (rounded 1dp; only when slot exists)
            "baseline_stddev": float,   (floored + rounded 1dp)
            "deviation_sigma": float,   (rounded 2dp)
        }
        Metrics with no reading are omitted. "no-slot" entries carry only
        state + value (no baseline numbers to report).
    """
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone_str)
    except Exception:
        tz = timezone.utc

    now = datetime.now(tz)
    how = _hour_of_week(now)

    out = {}
    metric_map = {"cpu": "cpu_percent", "ram": "ram_percent",
                  "disk_c": "disk_c_percent", "disk_d": "disk_d_percent"}

    for short_name, full_name in metric_map.items():
        value = current_metrics.get(full_name)
        if value is None:
            continue

        baseline = db.get_baseline(server_name, short_name, how)
        if not baseline or baseline.get("sample_count", 0) < min_samples:
            out[short_name] = {"state": "no-slot", "severity": None,
                               "direction": None, "value": float(value)}
            continue

        avg = baseline["avg_value"]
        stddev = baseline["stddev"]

        # Per-metric stddev floor. Real metric noise is rarely below these
        # values in normal operation; a tiny computed stddev (e.g. 0.5) makes
        # Z-scores explode and fire false criticals on trivial deviations.
        # CPU and RAM naturally jitter more than disk.
        stddev_floors = {"cpu": 5.0, "ram": 3.0, "disk_c": 2.0, "disk_d": 2.0}
        stddev = max(stddev, stddev_floors.get(short_name, 1.0))

        delta = abs(float(value) - avg)
        z_score = delta / stddev

        # Absolute deviation floors — the value must also be meaningfully
        # different from baseline in absolute terms, not just statistically
        # significant. Prevents e.g. a 34% CPU reading from firing critical
        # just because baseline happens to be 30% with low variance.
        #   min_warn = 7 percentage points different
        #   min_crit = 15 percentage points different
        MIN_ABS_WARN = 7.0
        MIN_ABS_CRIT = 15.0

        severity = None
        if z_score >= sigma_critical and delta >= MIN_ABS_CRIT:
            severity = "critical"
        elif z_score >= sigma_warning and delta >= MIN_ABS_WARN:
            severity = "warning"
        # If the statistical gate passes but the absolute delta is small,
        # we deliberately do NOT flag — this is normal variance amplified
        # by tight stddev, not a real deviation worth acting on.

        out[short_name] = {
            "state": "deviating" if severity else "normal",
            "severity": severity,
            "direction": ("high" if float(value) > avg else "low") if severity else None,
            "value": float(value),
            "baseline_avg": round(avg, 1),
            "baseline_stddev": round(stddev, 1),
            "deviation_sigma": round(z_score, 2),
        }

    return out


def check_deviation(db, server_name, current_metrics, timezone_str="Europe/Berlin",
                    sigma_warning=2.0, sigma_critical=3.0, min_samples=10):
    """Check if current metrics deviate from baseline.

    Thin filter over ``assess_metrics`` — kept for the event pipeline and
    all pre-fusion callers. Return shape unchanged.

    Args:
        current_metrics: dict with cpu_percent, ram_percent, etc.

    Returns:
        list of deviation dicts for metrics that exceed thresholds.
    """
    assessments = assess_metrics(
        db, server_name, current_metrics, timezone_str,
        sigma_warning, sigma_critical, min_samples,
    )
    return [
        {
            "metric": name,
            "value": a["value"],
            "baseline_avg": a["baseline_avg"],
            "baseline_stddev": a["baseline_stddev"],
            "deviation_sigma": a["deviation_sigma"],
            "severity": a["severity"],
            "direction": a["direction"],
        }
        for name, a in assessments.items()
        if a["state"] == "deviating"
    ]


def nightly_baseline_job(db, get_servers, settings):
    """Recalculate baselines for all servers. Called once per day."""
    servers = get_servers()
    tz = settings.get("timezone", "Europe/Berlin")
    baseline_cfg = settings.get("baseline_detection", {})
    weeks = baseline_cfg.get("history_weeks", 4)

    total = 0
    for srv in servers:
        try:
            n = compute_baselines(db, srv.name, weeks=weeks, timezone_str=tz)
            total += n
        except Exception:
            logger.exception("Failed to compute baselines for %s", srv.name)

    logger.info("Nightly baseline job: %d slots for %d servers", total, len(servers))
    return total
