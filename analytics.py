"""Anomaly detection and capacity forecasting for Prism monitoring system.

Uses only stdlib (no numpy/scipy). Provides statistical anomaly detection
based on rolling mean/stddev, and simple linear regression for disk forecasting.

Baseline cache (added for collector v2 scalability):
    ``detect_anomalies()`` is called once per metric sample under v2 —
    that's ~1/min/server, scaling linearly with fleet size. The dominant
    cost in there is ``db.get_metric_stats()`` which pulls a 7-day window
    (~10 000 rows). Re-reading that on every sample is wasteful: the
    rolling mean over 7 days barely moves between consecutive samples.

    We cache the *derived* per-(server, segment) stats (mean, stddev,
    count per metric) with a 5-minute TTL. Anomaly detection still runs
    on every sample — but reads from RAM, not SQLite, on cache hits.

    Scaling impact (numbers are ballpark, SSD, default 60s poll):
      *   30 servers — 30 DB reads/min → 6 DB reads/min  (5x)
      *  300 servers — 300 reads/min   → 60 reads/min    (5x)
      * 1000 servers — 1000 reads/min  → 200 reads/min   (5x)
    The savings ratio is `poll_interval / cache_ttl_s` clamped to ≥ 1.

    Invalidation: TTL-based (5 min) and explicit on
    ``/api/baselines/recalculate``. Per-server selective clearing is
    available via ``clear_baseline_cache(server_name=...)``.
"""

import math
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("prism.analytics")

# ─────────────────────────────────────────────────────────────────────
# Baseline cache for detect_anomalies (see module docstring)
# ─────────────────────────────────────────────────────────────────────
# Key:   (server_name, segment_str)  — segment is "" when None
# Value: {"ts": float (epoch), "metrics": {<metric>: {"mean","stddev","count"} | None}}
#
# A metric mapping value of ``None`` records "insufficient data" — we
# cache the *result* of the readiness check too, otherwise a server with
# < 24 samples would re-read the DB on every sample. Re-checked when TTL
# expires.
_BASELINE_CACHE: dict[tuple[str, str], dict] = {}
_BASELINE_CACHE_LOCK = threading.Lock()
BASELINE_CACHE_TTL_S: float = 300.0          # 5 min — far below mean-drift timescale on 7d window
BASELINE_CACHE_MAX_ENTRIES: int = 10000      # ≈ 3000 servers × 3 segments + slack

# Observability counters — read via get_baseline_cache_stats().
_cache_hits: int = 0
_cache_misses: int = 0
_cache_evictions: int = 0


def _baseline_cache_get(server_name: str, segment: str | None) -> dict | None:
    """Return cached per-metric stats dict, or None on miss/expiry.

    Returns the inner ``metrics`` dict directly (no copy) — callers
    treat it as read-only.
    """
    global _cache_hits, _cache_misses
    key = (server_name, segment or "")
    with _BASELINE_CACHE_LOCK:
        entry = _BASELINE_CACHE.get(key)
        if entry is None:
            _cache_misses += 1
            return None
        if time.time() - entry["ts"] > BASELINE_CACHE_TTL_S:
            # Treat expired as miss; let put() refresh
            _cache_misses += 1
            return None
        _cache_hits += 1
        return entry["metrics"]


def _baseline_cache_put(server_name: str, segment: str | None, metrics_stats: dict) -> None:
    """Store derived stats. Enforces a soft cap on entries via oldest-first eviction."""
    global _cache_evictions
    key = (server_name, segment or "")
    with _BASELINE_CACHE_LOCK:
        if len(_BASELINE_CACHE) >= BASELINE_CACHE_MAX_ENTRIES and key not in _BASELINE_CACHE:
            # Evict the oldest entry. O(n) but only runs when we're at the cap,
            # which for any realistic fleet means we have a config glitch — the
            # log line on eviction makes that visible.
            oldest_key = min(_BASELINE_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
            _BASELINE_CACHE.pop(oldest_key, None)
            _cache_evictions += 1
            logger.warning(
                "Baseline cache hit max entries (%d) — evicted %s. "
                "Did the fleet grow unexpectedly, or is server churn unusually high?",
                BASELINE_CACHE_MAX_ENTRIES, oldest_key,
            )
        _BASELINE_CACHE[key] = {"ts": time.time(), "metrics": metrics_stats}


def clear_baseline_cache(server_name: str | None = None) -> int:
    """Invalidate cache entries. Returns count cleared.

    Called from:
      * ``/api/baselines/recalculate`` — operator clicked "Recalculate Now"
      * Tests — to ensure isolation between cases
      * Future: could be wired to "server removed from fleet"

    Passing ``server_name=None`` clears everything (full flush). Passing
    a specific name clears only that server's entries (all segments).
    """
    with _BASELINE_CACHE_LOCK:
        if server_name is None:
            n = len(_BASELINE_CACHE)
            _BASELINE_CACHE.clear()
            return n
        keys_to_drop = [k for k in _BASELINE_CACHE if k[0] == server_name]
        for k in keys_to_drop:
            _BASELINE_CACHE.pop(k, None)
        return len(keys_to_drop)


def get_baseline_cache_stats() -> dict:
    """Observability snapshot for /api/system/health and tests."""
    with _BASELINE_CACHE_LOCK:
        size = len(_BASELINE_CACHE)
    total = _cache_hits + _cache_misses
    hit_rate = (_cache_hits / total) if total > 0 else 0.0
    return {
        "size": size,
        "hits": _cache_hits,
        "misses": _cache_misses,
        "evictions": _cache_evictions,
        "hit_rate": round(hit_rate, 3),
        "ttl_s": BASELINE_CACHE_TTL_S,
        "max_entries": BASELINE_CACHE_MAX_ENTRIES,
    }

# Minimum number of data points required for meaningful analysis
MIN_READINGS = 24

# Minimum R² before a disk forecast is allowed to state a DATE. Set at the
# existing "low confidence" boundary (see the confidence tiers in
# forecast_metric) rather than at a new number, so one threshold governs both
# what we call the fit and whether we act on it. Below it the trend direction
# is still reported; the deadline is not.
FORECAST_MIN_R2_FOR_DEADLINE = 0.4

METRIC_KEYS = ("cpu", "ram", "disk_c", "disk_d")

# Variance floors: minimum stddev per metric to avoid hyper-sensitive alerts on stable metrics
VARIANCE_FLOORS = {"cpu": 2.0, "ram": 3.0, "disk_c": 3.0, "disk_d": 3.0}

# Metrics where low-side anomalies should be detected (CPU/RAM drops may indicate service crashes)
LOW_SIDE_METRICS = {"cpu", "ram"}

# Low-side sigma thresholds (slightly less sensitive than high-side)
LOW_SIGMA_WARNING = 2.5
LOW_SIGMA_CRITICAL = 3.5

# Suggested actions mapped by (metric_prefix, severity, direction_prefix)
# Uses i18n keys for localization
SUGGESTED_ACTIONS = {
    ("cpu", "critical", "above"): "action_cpu_critical_high",
    ("cpu", "warning", "above"): "action_cpu_warning_high",
    ("cpu", "warning", "below"): "action_cpu_low",
    ("cpu", "critical", "below"): "action_cpu_low",
    ("ram", "critical", "above"): "action_ram_critical_high",
    ("ram", "warning", "above"): "action_ram_warning_high",
    ("ram", "warning", "below"): "action_ram_low",
    ("ram", "critical", "below"): "action_ram_low",
    ("disk_c", "critical", "above"): "action_disk_critical",
    ("disk_c", "warning", "above"): "action_disk_warning",
    ("disk_d", "critical", "above"): "action_disk_critical",
    ("disk_d", "warning", "above"): "action_disk_warning",
}

# Maps metric names to their database column names
_DB_COLUMN_MAP = {
    "cpu": "cpu_percent",
    "ram": "ram_percent",
    "disk_c": "disk_c_percent",
    "disk_d": "disk_d_percent",
}


def _mean(values: list[float]) -> float:
    """Calculate arithmetic mean."""
    return sum(values) / len(values)


def _stddev(values: list[float], mean_val: float) -> float:
    """Calculate sample standard deviation (Bessel's correction, n-1)."""
    n = len(values)
    if n < 2:
        return 0.0
    variance = sum((v - mean_val) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def _linear_regression(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Simple linear regression. Returns (slope, intercept, r_squared).

    Uses the least-squares method with stdlib math only.
    """
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    sum_y2 = sum(yi * yi for yi in y)

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return 0.0, _mean(y), 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    # R-squared (coefficient of determination)
    ss_tot = sum_y2 - (sum_y * sum_y) / n
    if ss_tot == 0:
        r_squared = 1.0 if denom == 0 else 0.0
    else:
        ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y))
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    return slope, intercept, r_squared


def _weighted_linear_regression(x, y, weights):
    """Weighted least-squares linear regression. Returns (slope, intercept, r_squared)."""
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0

    w_sum = sum(weights)
    if w_sum == 0:
        return 0.0, _mean(y), 0.0

    w_x = sum(wi * xi for wi, xi in zip(weights, x))
    w_y = sum(wi * yi for wi, yi in zip(weights, y))
    w_xy = sum(wi * xi * yi for wi, xi, yi in zip(weights, x, y))
    w_x2 = sum(wi * xi * xi for wi, xi in zip(weights, x))
    w_y2 = sum(wi * yi * yi for wi, yi in zip(weights, y))

    denom = w_sum * w_x2 - w_x * w_x
    if denom == 0:
        return 0.0, w_y / w_sum, 0.0

    slope = (w_sum * w_xy - w_x * w_y) / denom
    intercept = (w_y - slope * w_x) / w_sum

    ss_tot = w_y2 - (w_y * w_y) / w_sum
    if ss_tot == 0:
        r_squared = 1.0 if denom == 0 else 0.0
    else:
        ss_res = sum(wi * (yi - (slope * xi + intercept)) ** 2 for wi, xi, yi in zip(weights, x, y))
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    return slope, intercept, r_squared


def _classify_segment(timestamp_str: str, timezone_str: str = "Europe/Berlin") -> str:
    """Classify a timestamp as 'business' (Mon-Fri 07:00-19:00 local) or 'off_hours'.

    Uses zoneinfo (Python 3.9+ stdlib) for timezone conversion.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        # Python 3.8 fallback — treat everything as business hours
        return "business"

    try:
        ts = timestamp_str.replace("Z", "+00:00")
        if "+" not in ts and "-" not in ts[10:]:
            ts += "+00:00"
        dt_utc = datetime.fromisoformat(ts)
        dt_local = dt_utc.astimezone(ZoneInfo(timezone_str))

        # Monday=0, Sunday=6
        if dt_local.weekday() >= 5:  # Saturday or Sunday
            return "off_hours"
        if 7 <= dt_local.hour < 19:  # 07:00–18:59
            return "business"
        return "off_hours"
    except Exception:
        return "business"  # Default to business on parse errors


def _get_current_segment(timezone_str: str = "Europe/Berlin") -> str:
    """Get the current time segment."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _classify_segment(now, timezone_str)


def _compute_confidence(reading_count: int) -> str:
    """Compute confidence level based on data volume.

    More data = more reliable statistical analysis.
    """
    if reading_count >= 500:
        return "high"
    elif reading_count >= 100:
        return "medium"
    else:
        return "low"


def _compute_metric_stats(history: list[dict]) -> dict:
    """Compute per-metric baseline stats from a history list of DB rows.

    Returned dict keys are the metrics in ``METRIC_KEYS``. Each value is
    either ``{"mean", "stddev", "count"}`` or ``None`` to record
    "insufficient data" — both forms are safe to cache.

    Variance floor is applied here so the cached stddev is the value
    actually used in sigma comparisons. Keeping the floor inside this
    helper means anomaly detection on cache HIT and cache MISS produce
    identical results — a property the tests rely on.
    """
    out: dict[str, dict | None] = {}
    for metric in METRIC_KEYS:
        db_col = _DB_COLUMN_MAP[metric]
        values = [row[db_col] for row in history
                  if row[db_col] is not None and row[db_col] >= 0]
        if len(values) < MIN_READINGS:
            out[metric] = None
            continue
        mean_val = _mean(values)
        std_val = _stddev(values, mean_val)
        std_val = max(std_val, VARIANCE_FLOORS.get(metric, 2.0))
        out[metric] = {
            "mean": mean_val,
            "stddev": std_val,
            "count": len(values),
        }
    return out


def _build_anomalies_from_stats(current_metrics: dict, stats: dict,
                                 segment: str | None) -> list[dict]:
    """Pure function: emit anomaly dicts by comparing current_metrics
    against the cached/computed per-metric stats. No DB access.

    This is the "hot loop" run on every metric sample under v2 — keeping
    it pure means it can be unit-tested without a DB and the cache hit
    path is a straight RAM read + arithmetic.
    """
    anomalies = []
    for metric in METRIC_KEYS:
        per_metric = stats.get(metric)
        if per_metric is None:
            continue  # insufficient history
        current_val = current_metrics.get(metric)
        # Skip null values or disk=-1 (not present)
        if current_val is None or current_val < 0:
            continue
        mean_val = per_metric["mean"]
        std_val = per_metric["stddev"]
        confidence = _compute_confidence(per_metric["count"])
        deviation = current_val - mean_val
        severity = None
        direction = None
        # HIGH side (all metrics): above mean + N*stddev
        if deviation > 3 * std_val:
            severity = "critical"
            direction = "above_baseline"
        elif deviation > 2 * std_val:
            severity = "warning"
            direction = "above_baseline"
        # LOW side (only CPU/RAM): below mean - N*stddev
        elif metric in LOW_SIDE_METRICS:
            if deviation < -LOW_SIGMA_CRITICAL * std_val:
                severity = "critical"
                direction = "below_baseline"
            elif deviation < -LOW_SIGMA_WARNING * std_val:
                severity = "warning"
                direction = "below_baseline"
        if severity is None:
            continue
        deviation_percent = round((deviation / mean_val) * 100, 1) if mean_val > 0 else 0.0
        anomalies.append({
            "metric": metric,
            "value": round(current_val, 1),
            "mean": round(mean_val, 1),
            "stddev": round(std_val, 1),
            "deviation_percent": deviation_percent,
            "severity": severity,
            "direction": direction,
            "confidence": confidence,
            "segment": segment or "all",
        })
    return anomalies


def detect_anomalies(db, server_name: str, current_metrics: dict | None,
                     hours: int = 168, segment: str = None,
                     timezone_str: str = "Europe/Berlin") -> list[dict]:
    """Detect anomalies by comparing current metrics against historical mean +/- stddev.

    Reads from a 5-minute TTL baseline cache when available; falls back
    to ``db.get_metric_stats()`` on cache miss and re-populates. See
    module docstring for the scaling rationale.

    Args:
        db: Database instance
        server_name: Name of the server to analyze
        current_metrics: Current metric reading (dict with cpu, ram, disk_c, disk_d keys)
        hours: Hours of history to consider (default 168 = 7 days).
               NOTE: cache is keyed on (server_name, segment) only. All
               callers use the default 168 today; if a caller passes a
               different value the cache hit may return stats computed
               from a different window. If you need to pass a custom
               ``hours``, call ``clear_baseline_cache(server_name)`` first
               or rename the helper to a non-cached variant.
        segment: Time segment filter ('business', 'off_hours', or None for all)
        timezone_str: IANA timezone for segment classification (default Europe/Berlin)

    Returns:
        List of anomaly dicts with keys: metric, value, mean, stddev,
        deviation_percent, severity, direction, confidence, segment
    """
    if not current_metrics:
        return []

    # Fast path: hit the cache.
    cached_stats = _baseline_cache_get(server_name, segment)
    if cached_stats is not None:
        return _build_anomalies_from_stats(current_metrics, cached_stats, segment)

    # Slow path: read DB, compute, cache.
    try:
        history = db.get_metric_stats(server_name, hours=hours)
    except Exception:
        logger.exception("Failed to fetch metric stats for %s", server_name)
        return []

    if len(history) < MIN_READINGS:
        logger.debug("[%s] Not enough data for anomaly detection (%d readings)",
                     server_name, len(history))
        # Cache the "not enough data" verdict too — otherwise quiet servers
        # would re-read the DB on every sample. TTL will let new data
        # become visible within 5 minutes.
        _baseline_cache_put(server_name, segment, {m: None for m in METRIC_KEYS})
        return []

    # If segment specified, filter history to matching time segment
    if segment:
        filtered_history = [
            row for row in history
            if _classify_segment(row.get("timestamp", ""), timezone_str) == segment
        ]
        # Fall back to full history if segment filtering yields too few points
        if len(filtered_history) >= MIN_READINGS:
            history = filtered_history
        else:
            logger.debug("[%s] Segment '%s' has only %d readings, using full history",
                         server_name, segment, len(filtered_history))

    stats = _compute_metric_stats(history)
    _baseline_cache_put(server_name, segment, stats)
    return _build_anomalies_from_stats(current_metrics, stats, segment)


def detect_rate_anomalies(db, server_name: str, current_metrics: dict | None,
                          hours: int = 24) -> list[dict]:
    """Detect abnormal rates of change by comparing current metric velocity against historical.

    Computes the first derivative (consecutive differences) of each metric over the
    last 24 hours, then flags if the most recent rate exceeds 3 sigma of the historical
    rate distribution.

    Args:
        db: Database instance
        server_name: Name of the server
        current_metrics: Current metric reading dict
        hours: Hours of rate history to consider (default 24)

    Returns:
        List of rate anomaly dicts with keys: metric, rate, mean_rate, rate_stddev,
        direction ('accelerating' or 'decelerating'), severity
    """
    if not current_metrics:
        return []

    try:
        history = db.get_metric_stats(server_name, hours=hours)
    except Exception:
        logger.exception("Failed to fetch metric stats for rate analysis on %s", server_name)
        return []

    # Need at least 6 readings to compute meaningful rate statistics
    if len(history) < 6:
        return []

    anomalies = []
    RATE_FLOOR = 0.5  # Minimum rate stddev to avoid flagging trivial fluctuations
    RATE_SIGMA = 3.0  # Number of sigma for rate anomaly detection

    for metric in METRIC_KEYS:
        db_col = _DB_COLUMN_MAP[metric]

        # Extract valid sequential values
        values = [row[db_col] for row in history
                  if row[db_col] is not None and row[db_col] >= 0]

        if len(values) < 6:
            continue

        # Compute consecutive differences (first derivative)
        rates = [values[i] - values[i - 1] for i in range(1, len(values))]

        if not rates:
            continue

        # Rate statistics
        rate_mean = sum(rates) / len(rates)
        if len(rates) < 2:
            continue
        rate_variance = sum((r - rate_mean) ** 2 for r in rates) / (len(rates) - 1)
        rate_std = math.sqrt(rate_variance) if rate_variance > 0 else 0.0
        rate_std = max(rate_std, RATE_FLOOR)

        # Current rate is the most recent difference
        current_rate = rates[-1]
        rate_deviation = abs(current_rate - rate_mean)

        if rate_deviation <= RATE_SIGMA * rate_std:
            continue

        # Determine direction and severity
        direction = "accelerating" if current_rate > rate_mean else "decelerating"
        severity = "critical" if rate_deviation > 4.0 * rate_std else "warning"

        anomalies.append({
            "metric": metric,
            "rate": round(current_rate, 2),
            "mean_rate": round(rate_mean, 2),
            "rate_stddev": round(rate_std, 2),
            "direction": direction,
            "severity": severity,
        })

    return anomalies


def forecast_metric(db, server_name: str, metric: str = "disk_c",
                    hours: int = 168, target_percent: float = 100.0,
                    ram_warning: float | None = None,
                    warning_threshold: float | None = None,
                    history: list | None = None) -> dict:
    """Forecast metric usage trend — METRIC-AWARE.

    Disk metrics fill monotonically over time and a linear forecast is
    meaningful ("disk D will be full in 14 days").

    RAM is stationary in normal operation — it oscillates around a
    workload-defined steady state and rarely trends upward unless there
    is a memory leak. Linear extrapolation of RAM noise produces nonsense
    "RAM will hit 90% in 14 days" predictions. We instead:
      1. Compute the baseline range (min / max / mean) over the window.
      2. Run a leak detector that requires sustained upward drift across
         sub-windows AND high R² before flagging anything.
      3. If no leak → kind="stationary", show the range, NO days_until.
      4. If leak → kind="leak", forecast + days_until_threshold like disk.

    Args:
        db: Database instance
        server_name: Name of the server
        metric: Which metric ('disk_c', 'disk_d', or 'ram')
        hours: Hours of history to analyze (default 168 = 7 days)
        target_percent: Target threshold percent (100 for disk, role-based for
            RAM/CPU)
        warning_threshold: This server's warning threshold for the metric being
            forecast (role/type-based — see get_server_analytics). Used ONLY to
            flag a stationary baseline that's sitting at/above the warning band
            (the SQL01 case: 93% is this server's normal, but 93% is still worth
            a visibly different headline than a plain "Normal usage" green).
            Applies to both RAM and CPU. None (default) preserves prior
            behavior — no threshold opinion.
        ram_warning: Deprecated alias for warning_threshold, kept so existing
            RAM callers keep working. Ignored when warning_threshold is given.
        history: Pre-fetched rows from ``db.get_metric_stats(server_name,
            hours)``. When given, the DB read is skipped entirely.

            This exists because callers forecast SEVERAL metrics for the SAME
            server, and each call re-read the identical row set. The capacity
            report did 29 servers x 4 metrics = 116 reads of 720 hours where 29
            would do. Measured: of its 5.03 s, roughly 3.8 s was connection
            churn and repeated reads and only ~0.2 s was the actual regression
            arithmetic. Passing the rows in once per server removes 3/4 of the
            reads outright.

            The caller is responsible for the rows matching ``hours`` — nothing
            here re-validates the window, because the whole point is to avoid
            touching the database.

    Returns:
        Dict with keys:
          - kind: 'growth' (disk) | 'stationary' (RAM/CPU normal) | 'leak'
            (RAM/CPU trending up)
          - current, enough_data, confidence, range_min, range_max, range_avg
          - elevated_but_stable: True when kind='stationary' AND
            range_avg >= warning_threshold
          - For 'growth' and 'leak': trend_per_day, days_until_full, forecast_7d
    """
    result = {
        "kind": None,                # 'growth' | 'stationary' | 'leak'
        "current": None,
        "trend_per_day": None,
        "days_until_full": None,
        "forecast_7d": None,
        "confidence": "low",
        "enough_data": False,
        "range_min": None,
        "range_max": None,
        "range_avg": None,
        "elevated_but_stable": False,
    }

    if history is None:
        try:
            history = db.get_metric_stats(server_name, hours=hours)
        except Exception:
            logger.exception("Failed to fetch metric stats for %s", server_name)
            return result

    db_col = _DB_COLUMN_MAP.get(metric)
    if not db_col:
        logger.warning("Unknown metric: %s", metric)
        return result

    # Filter to valid readings
    data_points = []
    for row in history:
        val = row[db_col]
        ts_str = row["timestamp"]
        if val is not None and val >= 0 and ts_str:
            try:
                ts = ts_str.replace("Z", "+00:00")
                if "+" not in ts and "Z" not in ts:
                    ts += "+00:00"
                dt = datetime.fromisoformat(ts)
                data_points.append((dt, val))
            except (ValueError, TypeError):
                continue

    if len(data_points) < MIN_READINGS:
        logger.debug("[%s] Not enough data for %s forecast (%d points)",
                     server_name, metric, len(data_points))
        return result

    result["enough_data"] = True
    y_values = [dp[1] for dp in data_points]

    # Range stats — meaningful for ALL metrics, especially RAM
    result["current"] = round(y_values[-1], 1)
    result["range_min"] = round(min(y_values), 1)
    result["range_max"] = round(max(y_values), 1)
    result["range_avg"] = round(sum(y_values) / len(y_values), 1)

    # Convert timestamps to hours since first reading (for regression)
    t0 = data_points[0][0]
    x_hours = [(dp[0] - t0).total_seconds() / 3600.0 for dp in data_points]

    # Recency weighting: 0.95^days_ago
    last_time = data_points[-1][0]
    weights = [0.95 ** ((last_time - dp[0]).total_seconds() / 86400.0) for dp in data_points]

    slope_per_hour, intercept, r_squared = _weighted_linear_regression(x_hours, y_values, weights)
    trend_per_day = slope_per_hour * 24.0

    # Confidence based on R-squared (used by both kinds when applicable)
    if r_squared > 0.7:
        confidence = "high"
    elif r_squared > 0.4:
        confidence = "medium"
    else:
        confidence = "low"
    result["confidence"] = confidence

    # ─────────────────────────────────────────────────────────────────
    # Branch on metric type
    # ─────────────────────────────────────────────────────────────────
    is_ram = metric == "ram"
    is_cpu = metric == "cpu"

    if is_ram or is_cpu:
        # STATIONARY metrics — only forecast if we detect a real leak/drift.
        # Leak criteria (all must hold):
        #   - Regression has decent fit (R² > 0.55)
        #   - Trend is ≥ 0.5%/day upward
        #   - Sub-window check: split the window in halves; the second half's
        #     mean must be at least 2% higher than the first half's
        #     (filters out short-lived spikes that bias regression)
        leak = False
        if trend_per_day > 0.5 and r_squared > 0.55:
            mid = len(y_values) // 2
            if mid >= 2:
                first_avg = sum(y_values[:mid]) / mid
                second_avg = sum(y_values[mid:]) / (len(y_values) - mid)
                if second_avg - first_avg >= 2.0:
                    leak = True

        if leak:
            result["kind"] = "leak"
            result["trend_per_day"] = round(trend_per_day, 2)
            # Fitted position, not the last raw sample — same reasoning as the
            # disk branch below. This path already gates on R² > 0.55, so the
            # fit is trustworthy; the inconsistency was only in the numerator.
            current_val = slope_per_hour * x_hours[-1] + intercept
            remaining = target_percent - current_val
            if remaining > 0 and trend_per_day > 0.001:
                result["days_until_full"] = round(remaining / trend_per_day, 1)
            else:
                result["days_until_full"] = 0
            last_x = x_hours[-1]
            forecast_val = slope_per_hour * (last_x + 168.0) + intercept
            result["forecast_7d"] = round(min(max(forecast_val, 0.0), 100.0), 1)
        else:
            # Normal stationary behavior — no meaningful forecast
            result["kind"] = "stationary"
            # Keep trend_per_day as a small informational number but DON'T
            # use it for days_until_full or forecast_7d
            result["trend_per_day"] = round(trend_per_day, 2)
            # Threshold-aware wording (the "93% shows green Normal usage"
            # complaint): a stationary baseline can still be sitting at/above
            # this server's warning band. Applies to CPU as well as RAM — a DC
            # whose cpu_warning is 40 and which averages 45 is "elevated but
            # stable", exactly the same shape of fact as the RAM case, and it
            # is what tells an operator how the box NORMALLY runs.
            warn_at = warning_threshold if warning_threshold is not None else ram_warning
            if ((is_ram or is_cpu) and warn_at is not None
                    and result["range_avg"] is not None
                    and result["range_avg"] >= warn_at):
                result["elevated_but_stable"] = True
        return result

    # DISK metrics — keep the linear forecast (data accumulates monotonically)
    result["kind"] = "growth"
    result["trend_per_day"] = round(trend_per_day, 2)

    # A deadline is only worth as much as the line it comes from, and this
    # branch used to emit one for ANY positive slope — no fit-quality gate at
    # all, while the RAM/CPU leak path above has required R² > 0.55 all along.
    #
    # Measured on the live fleet (2026-08-06), 22 disk forecasts:
    #   R² > 0.4  (12 of them) — moving between the raw last reading and the
    #             fitted value changes the answer by 0-3%.
    #   R² <= 0.4 (10 of them) — the same change moves it by 45-46%.
    # And the instability was not harmless: the two soonest deadlines on the
    # whole fleet, the ones the Reports page put at the top of its capacity
    # column, had R² = 0.032 and R² = 0.234. "Disk D full in 53 days" was noise
    # with a decimal point on it.
    #
    # Below the gate we still report trend_per_day — the direction is real
    # enough to show — but not a date. "We don't know" beats a confident wrong
    # number an operator might schedule downtime around.
    if trend_per_day > 0.001 and r_squared > FORECAST_MIN_R2_FOR_DEADLINE:
        # Divide the fitted slope into the FITTED position, not into the last
        # raw sample: mixing a modelled rate with a single noisy reading is
        # what produced most of that 45% swing.
        fitted_now = slope_per_hour * x_hours[-1] + intercept
        remaining = target_percent - fitted_now
        result["days_until_full"] = round(remaining / trend_per_day, 1) if remaining > 0 else 0
    else:
        result["days_until_full"] = None

    # 7-day forecast: current position on regression line + 7 days of trend
    last_x = x_hours[-1]
    forecast_val = slope_per_hour * (last_x + 168.0) + intercept
    result["forecast_7d"] = round(min(max(forecast_val, 0.0), 100.0), 1)

    return result


def forecast_disk(db, server_name: str, metric: str = "disk_c",
                  hours: int = 168) -> dict:
    """Backward-compatible alias for forecast_metric."""
    return forecast_metric(db, server_name, metric=metric, hours=hours)


def enrich_anomaly(anomaly: dict, forecasts: dict, thresholds: dict,
                   server_type: str = None) -> dict:
    """Add contextual information to an anomaly for human-readable display.

    Enriches each anomaly with trend context, forecast-to-threshold estimate,
    suggested action key, and confidence level.

    Args:
        anomaly: Anomaly dict from detect_anomalies (has metric, value, mean, stddev, severity, direction)
        forecasts: Dict of forecast results (disk_c, disk_d, ram) from forecast_metric
        thresholds: Server thresholds dict
        server_type: Server role type string

    Returns:
        The same anomaly dict with added keys: trend_context, forecast_to_threshold,
        suggested_action, confidence_level
    """
    metric = anomaly.get("metric", "")
    severity = anomaly.get("severity", "warning")
    direction = anomaly.get("direction", "above_baseline")
    dir_key = "above" if "above" in direction else "below"

    # ── Trend context ──
    forecast = forecasts.get(metric, {})
    trend_per_day = forecast.get("trend_per_day")
    if trend_per_day is not None:
        if trend_per_day > 0.05:
            anomaly["trend_context"] = f"+{trend_per_day}%/day"
            anomaly["trend_direction"] = "up"
        elif trend_per_day < -0.05:
            anomaly["trend_context"] = f"{trend_per_day}%/day"
            anomaly["trend_direction"] = "down"
        else:
            anomaly["trend_context"] = "stable"
            anomaly["trend_direction"] = "stable"
    else:
        anomaly["trend_context"] = None
        anomaly["trend_direction"] = None

    # ── Forecast to threshold ──
    days_until = forecast.get("days_until_full")
    if days_until is not None and days_until > 0:
        # Determine which threshold we're heading toward
        if metric.startswith("disk"):
            threshold_val = thresholds.get("disk_critical", 90)
        elif metric == "ram":
            threshold_val = thresholds.get("ram_critical", 90)
        else:
            threshold_val = thresholds.get("cpu_critical", 90)
        anomaly["forecast_to_threshold"] = {
            "days": round(days_until, 0),
            "threshold": threshold_val,
        }
    else:
        anomaly["forecast_to_threshold"] = None

    # ── Suggested action (i18n key) ──
    # Normalize metric for lookup (disk_c/disk_d both map to same action)
    action_key = SUGGESTED_ACTIONS.get((metric, severity, dir_key))
    anomaly["suggested_action"] = action_key

    # ── Confidence level ──
    # Based on the forecast confidence if available, else based on data sufficiency
    fc_confidence = forecast.get("confidence", "low")
    if forecast.get("enough_data"):
        anomaly["confidence_level"] = fc_confidence
    else:
        anomaly["confidence_level"] = "low"

    return anomaly


def correlate_events(db, cycle_events: list[dict], servers: list) -> list[dict]:
    """Detect correlated patterns in events from the current collection cycle.

    Implements correlation rules:
    1. Multi-server offline: 2+ servers offline within the same cycle
    2. Compound stress: single server with 2+ metrics in warning/critical state
    3. Tag-based correlation: 2+ servers sharing a tag with critical events
    Plus: creates incidents for correlated events and auto-resolves stale ones.

    Args:
        db: Database instance
        cycle_events: List of event dicts generated this cycle, each with keys:
            server_name, event_type, metric, value, threshold, message, event_id (optional)
        servers: List of ServerConfig objects (for role information)

    Returns:
        List of correlated event dicts that were inserted
    """
    import uuid
    from collections import Counter, defaultdict

    if not cycle_events:
        # Still run auto-resolution even when no new events
        _auto_resolve_incidents(db)
        return []

    correlated = []

    # Build server role lookup
    server_roles = {s.name: s.type for s in servers}

    # ── Rule 1: Multi-server offline ──
    offline_events = [e for e in cycle_events if e.get("event_type") == "offline"]
    if len(offline_events) >= 2:
        corr_id = f"corr_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # Check if offline servers share a role
        offline_servers = [e["server_name"] for e in offline_events]
        offline_roles = [server_roles.get(s, "other") for s in offline_servers]

        # Group by role to find role-specific impacts
        role_counts = Counter(offline_roles)

        # Build correlation message
        server_list = ", ".join(offline_servers)
        if any(count >= 2 for role, count in role_counts.items() if role == "domain_controller"):
            msg = f"Multiple domain controllers offline ({server_list}) — Active Directory at risk"
        elif any(count >= 2 for role, count in role_counts.items()):
            dominant_role = max(role_counts, key=role_counts.get).replace("_", " ").title()
            msg = f"Multiple {dominant_role} servers offline ({server_list}) — service disruption likely"
        else:
            msg = f"Multiple servers offline simultaneously ({server_list}) — possible infrastructure issue"

        try:
            db.insert_event_correlated(
                offline_servers[0], "correlated", None, None, None, msg, corr_id
            )
            # Backfill correlation_id on constituent events
            for evt in offline_events:
                if evt.get("event_id"):
                    db.update_event_correlation(evt["event_id"], corr_id)
            correlated.append({"correlation_id": corr_id, "message": msg, "rule": "multi_server_offline"})
            logger.info("Correlated incident: %s", msg)

            # F7: Create incident for multi-server offline (dedup: one open
            # incident per ongoing mass-outage, not one per collector cycle).
            try:
                existing_id = db.get_open_incident_id_by_title_prefix("Multiple servers offline")
                if existing_id:
                    for evt in offline_events:
                        if evt.get("event_id"):
                            db.link_event_to_incident(existing_id, evt["event_id"])
                else:
                    incident_id = db.create_incident(
                        title=f"Multiple servers offline ({len(offline_servers)} servers)",
                        severity="critical",
                        root_cause_server=offline_servers[0],
                        description=f"Servers affected: {server_list}"
                    )
                    for evt in offline_events:
                        if evt.get("event_id"):
                            db.link_event_to_incident(incident_id, evt["event_id"])
                    logger.info("Created incident #%d for multi-server offline", incident_id)
            except Exception:
                logger.exception("Failed to create incident for multi-server offline")
        except Exception:
            logger.exception("Failed to create offline correlation event")

    # ── Rule 2: Compound stress (single server, multiple metrics) ──
    server_stress = defaultdict(list)
    for e in cycle_events:
        if e.get("event_type") in ("critical", "warning") and e.get("metric"):
            server_stress[e["server_name"]].append(e)

    for server_name, events in server_stress.items():
        if len(events) >= 2:
            corr_id = f"corr_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

            metrics_list = ", ".join(
                f"{e.get('metric', '?')} ({e.get('event_type', '?')})"
                for e in events
            )
            msg = f"{server_name} under compound stress: {metrics_list}"

            try:
                db.insert_event_correlated(
                    server_name, "correlated", None, None, None, msg, corr_id
                )
                for evt in events:
                    if evt.get("event_id"):
                        db.update_event_correlation(evt["event_id"], corr_id)
                correlated.append({"correlation_id": corr_id, "message": msg, "rule": "compound_stress"})
                logger.info("Compound stress: %s", msg)

                # F7: Create incident for compound stress if any event is critical
                has_critical = any(e.get("event_type") == "critical" for e in events)
                try:
                    existing_id = db.get_open_incident_id_by_title_prefix(
                        f"Compound stress on {server_name}")
                    if existing_id:
                        for evt in events:
                            if evt.get("event_id"):
                                db.link_event_to_incident(existing_id, evt["event_id"])
                    else:
                        incident_id = db.create_incident(
                            title=f"Compound stress on {server_name}",
                            severity="critical" if has_critical else "warning",
                            root_cause_server=server_name,
                            description=f"Metrics under stress: {metrics_list}"
                        )
                        for evt in events:
                            if evt.get("event_id"):
                                db.link_event_to_incident(incident_id, evt["event_id"])
                        logger.info("Created incident #%d for compound stress on %s", incident_id, server_name)
                except Exception:
                    logger.exception("Failed to create incident for compound stress on %s", server_name)
            except Exception:
                logger.exception("Failed to create compound stress correlation for %s", server_name)

    # ── Rule 3: Tag-based correlation ──
    # If 2+ servers sharing a tag have critical events in this cycle, create an incident
    try:
        tag_assignments = db.get_all_tag_assignments()  # {server_name: [{"id":..., "name":...}, ...]}
        # Build tag -> list of critical event servers
        tag_critical_servers = defaultdict(set)
        tag_critical_events = defaultdict(list)
        for e in cycle_events:
            if e.get("event_type") in ("critical", "warning"):
                sname = e["server_name"]
                for tag in tag_assignments.get(sname, []):
                    tag_name = tag.get("name", "")
                    tag_critical_servers[tag_name].add(sname)
                    tag_critical_events[tag_name].append(e)

        for tag_name, affected in tag_critical_servers.items():
            if len(affected) >= 2:
                affected_list = sorted(affected)
                server_list = ", ".join(affected_list)
                try:
                    existing_id = db.get_open_incident_id_by_title_prefix(f"Tag '{tag_name}':")
                    if existing_id:
                        for evt in tag_critical_events[tag_name]:
                            if evt.get("event_id"):
                                db.link_event_to_incident(existing_id, evt["event_id"])
                    else:
                        incident_id = db.create_incident(
                            title=f"Tag '{tag_name}': {len(affected)} servers with issues",
                            severity="critical",
                            root_cause_server=affected_list[0],
                            description=f"Servers in tag '{tag_name}' affected: {server_list}"
                        )
                        for evt in tag_critical_events[tag_name]:
                            if evt.get("event_id"):
                                db.link_event_to_incident(incident_id, evt["event_id"])
                        logger.info("Created tag-based incident #%d for tag '%s'", incident_id, tag_name)
                    correlated.append({
                        "correlation_id": f"tag_{tag_name}",
                        "message": f"Tag '{tag_name}': {len(affected)} servers with issues ({server_list})",
                        "rule": "tag_correlation"
                    })
                except Exception:
                    logger.exception("Failed to create tag-based incident for tag '%s'", tag_name)
    except Exception:
        logger.exception("Tag-based correlation failed")

    # ── Rule 4: Dependency-based cascading failure ──
    # When a server goes critical/offline, check if servers that depend on it are also failing
    try:
        all_deps = db.get_all_dependencies()
        if all_deps:
            # Build map: server -> list of servers that depend ON it
            dependents_map = defaultdict(list)
            for dep in all_deps:
                dependents_map[dep["depends_on"]].append(dep["server_name"])

            # Find critical/offline servers this cycle
            critical_servers = set()
            for e in cycle_events:
                if e.get("event_type") in ("critical", "offline"):
                    critical_servers.add(e["server_name"])

            # For each critical server, check if its dependents are also in trouble
            for upstream in critical_servers:
                downstream_list = dependents_map.get(upstream, [])
                if not downstream_list:
                    continue

                # Which dependents are also failing this cycle?
                affected_downstream = [s for s in downstream_list if s in critical_servers]
                if not affected_downstream:
                    continue

                # Found cascading failure: upstream is down and dependents are failing
                all_affected = [upstream] + sorted(affected_downstream)
                server_list = ", ".join(all_affected)

                # Check if we already created an incident for these servers this cycle (avoid duplicates)
                already_covered = any(
                    c.get("rule") in ("multi_server_offline", "tag_correlation") and
                    upstream in c.get("message", "")
                    for c in correlated
                )
                if already_covered:
                    continue

                try:
                    # Dedup: an ongoing cascade from this upstream reuses its open
                    # incident instead of spawning a fresh one every collector
                    # cycle (root cause of the 295-duplicate pile-up).
                    existing_id = db.get_open_incident_id_by_title_prefix(
                        f"Cascading failure from {upstream}")
                    if existing_id:
                        for evt in cycle_events:
                            if evt.get("server_name") in all_affected and evt.get("event_id"):
                                db.link_event_to_incident(existing_id, evt["event_id"])
                        continue

                    incident_id = db.create_incident(
                        title=f"Cascading failure from {upstream} ({len(affected_downstream)} dependent{'s' if len(affected_downstream) > 1 else ''})",
                        severity="critical",
                        root_cause_server=upstream,
                        description=f"Upstream server {upstream} is down. Dependent servers also failing: {', '.join(affected_downstream)}"
                    )
                    # Link all related events
                    for evt in cycle_events:
                        if evt.get("server_name") in all_affected and evt.get("event_id"):
                            db.link_event_to_incident(incident_id, evt["event_id"])
                    correlated.append({
                        "correlation_id": f"cascade_{upstream}",
                        "message": f"Cascading failure from {upstream}: {server_list}",
                        "rule": "dependency_cascade"
                    })
                    logger.info("Created cascading failure incident #%d: %s -> %s",
                               incident_id, upstream, affected_downstream)
                except Exception:
                    logger.exception("Failed to create cascading failure incident for %s", upstream)
    except Exception:
        logger.exception("Dependency-based correlation failed")

    # ── Auto-resolution check ──
    _auto_resolve_incidents(db)

    return correlated


def _auto_resolve_incidents(db):
    """Check open incidents and auto-resolve those where all constituent servers are now healthy."""
    try:
        # Self-heal any duplicate open incidents (pre-dedup backlog / races) so a
        # recurring situation collapses to a single tracked incident.
        collapsed = db.collapse_duplicate_open_incidents()
        if collapsed:
            logger.info("Collapsed %d duplicate open incident(s)", collapsed)

        open_incidents = db.get_incidents(status="open")
        if not open_incidents:
            return

        # Get current status of all servers
        latest = db.get_latest_all()
        server_status = {m["server_name"]: m["status"] for m in latest}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for inc in open_incidents:
            detail = db.get_incident_detail(inc["id"])
            if not detail:
                continue

            # Collect all unique servers referenced in the incident's events.
            incident_servers = {
                evt["server_name"] for evt in (detail.get("events") or [])
                if evt.get("server_name")
            }
            # Fallback: incidents whose events were purged (or were never linked —
            # e.g. cascade incidents) can still auto-resolve on their recorded
            # root-cause server. Without this they become permanent zombies.
            if not incident_servers and detail.get("root_cause_server"):
                incident_servers = {detail["root_cause_server"]}

            if not incident_servers:
                continue

            # Check if all servers are now healthy
            all_healthy = all(
                server_status.get(s, "unknown") == "healthy"
                for s in incident_servers
            )
            if all_healthy:
                db.update_incident(
                    inc["id"],
                    status="resolved",
                    resolved_by="auto",
                    resolved_at=now,
                    resolution_notes="All affected servers returned to healthy status"
                )
                logger.info("Auto-resolved incident #%d — all servers healthy", inc["id"])
    except Exception:
        logger.exception("Auto-resolution check failed")


def get_server_analytics(db, server_name: str, server_type: str = None,
                         timezone_str: str = "Europe/Berlin",
                         settings: dict = None, thresholds: dict = None) -> dict:
    """Combine anomaly detection and forecasting for a single server.

    Args:
        db: Database instance
        server_name: Name of the server
        server_type: Server type (e.g. 'file_server') for role-based thresholds
        timezone_str: IANA timezone for segment classification (default Europe/Berlin)
        thresholds: Optional per-server threshold overrides (ServerConfig.thresholds
            — cpu_warning, ram_critical, etc). Existing callers don't pass this yet,
            so it falls back to the server_type role defaults below (same behavior
            as before this param existed) when omitted.

    Returns:
        Dict with keys: anomalies (list), forecasts (dict with disk_c, disk_d,
        ram, cpu — ram and cpu are stationary forecasts, disks are linear)
    """
    from models import DEFAULT_THRESHOLDS

    # Get current metrics for anomaly detection
    current = db.get_latest_by_server(server_name)

    current_metrics = None
    if current:
        current_metrics = {
            "cpu": current.get("cpu_percent"),
            "ram": current.get("ram_percent"),
            "disk_c": current.get("disk_c_percent"),
            "disk_d": current.get("disk_d_percent"),
        }

    # Determine current time segment for segmented baseline comparison
    current_segment = _get_current_segment(timezone_str)

    anomalies = detect_anomalies(db, server_name, current_metrics,
                                  segment=current_segment, timezone_str=timezone_str)

    # Filter out anomalies the user has already acknowledged or snoozed.
    # Without this filter, the server overview page would re-display the
    # same anomaly immediately after the user clicks Acknowledge, because
    # detect_anomalies() runs live at render time and is stateless.
    # The collector's dispatch loop has its own ack check at line ~1462,
    # so alerts don't fire twice either.
    try:
        acks = db.get_active_acknowledgments(server_name=server_name)
        acked_metrics = {a["metric"] for a in acks}
        if acked_metrics:
            anomalies = [a for a in anomalies if a.get("metric") not in acked_metrics]
    except Exception:
        logger.debug("Failed to filter acknowledged anomalies for %s", server_name, exc_info=True)

    # CPU N-of-M DISPLAY GATE — respect the same CPU warning smoothing that
    # gates alert dispatch in the collector. Without this, the server overview
    # would show a CPU warning on the first high reading while the collector
    # correctly suppressed the event, leading to the "triggers at 1 anomaly
    # not after 3" complaint.
    #
    # Post-R1 refactor: the CPU gate now lives in ``detection.py``. Late
    # import is still required because detection imports from analytics
    # (this module) — without the late binding we'd have a circular
    # import at module load. The cycle is broken by deferring detection's
    # import until first call, which is fine because the gate is only
    # consulted from a render-path function.
    if settings:
        try:
            from detection import _cpu_gate_passes
            _filtered = []
            for a in anomalies:
                if (a.get("metric") == "cpu"
                        and a.get("severity") == "warning"
                        and not _cpu_gate_passes(server_name, settings)):
                    continue
                _filtered.append(a)
            anomalies = _filtered
        except Exception:
            logger.debug("CPU gate filter failed for %s", server_name, exc_info=True)

    forecasts = {
        "disk_c": forecast_metric(db, server_name, metric="disk_c"),
        "disk_d": forecast_metric(db, server_name, metric="disk_d"),
    }

    # Determine RAM warning/critical thresholds — prefer the caller-supplied
    # per-server overrides (thresholds param), fall back to server-type role
    # defaults otherwise. ram_warning feeds the "elevated but stable" forecast
    # wording (docs/plans/DETECTION_FUSION_PLAN.md §2/§7/§8, task item 3) —
    # a stationary RAM baseline sitting at/above this server's own warning
    # band no longer prints a plain green "Normal usage".
    type_defaults = DEFAULT_THRESHOLDS.get(server_type, DEFAULT_THRESHOLDS.get("_default", {}))
    _th = thresholds or type_defaults
    ram_critical = _th.get("ram_critical", type_defaults.get("ram_critical", 90))
    ram_warning = _th.get("ram_warning", type_defaults.get("ram_warning", 80))

    forecasts["ram"] = forecast_metric(db, server_name, metric="ram",
                                        target_percent=ram_critical,
                                        warning_threshold=ram_warning)

    # CPU gets the same stationary treatment as RAM. CPU is not a resource that
    # fills, so a linear "days until" projection is as meaningless for it as it
    # is for RAM — what an operator actually needs is the range/avg/now band
    # that says how this box normally runs. That matters more now that the spike
    # gate deliberately withholds alarms for short CPU breaches: the card is
    # where you go to see whether 80% was an outlier or the daily norm.
    cpu_critical = _th.get("cpu_critical", type_defaults.get("cpu_critical", 85))
    cpu_warning = _th.get("cpu_warning", type_defaults.get("cpu_warning", 75))
    forecasts["cpu"] = forecast_metric(db, server_name, metric="cpu",
                                        target_percent=cpu_critical,
                                        warning_threshold=cpu_warning)

    # Enrich each anomaly with context
    for anomaly in anomalies:
        enrich_anomaly(anomaly, forecasts,
                       DEFAULT_THRESHOLDS.get(server_type, DEFAULT_THRESHOLDS.get("_default", {})),
                       server_type)

    return {
        "anomalies": anomalies,
        "forecasts": forecasts,
    }


def generate_daily_digest(db, servers) -> dict:
    """Generate a daily health summary for all monitored servers.

    Returns a structured dict suitable for rendering as a dashboard page or email.
    """
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(servers),
        "healthy": 0,
        "warning": 0,
        "critical": 0,
        "offline": 0,
        "servers": [],
        "needs_attention": [],
    }

    for server in servers:
        latest = db.get_latest_by_server(server.name)
        status = latest.get("status", "unknown") if latest else "unknown"

        # Count statuses
        if status == "healthy":
            summary["healthy"] += 1
        elif status == "warning":
            summary["warning"] += 1
        elif status == "critical":
            summary["critical"] += 1
        elif status == "offline":
            summary["offline"] += 1

        # Get analytics for this server
        analytics = get_server_analytics(db, server.name, server_type=server.type)

        server_digest = {
            "name": server.name,
            "type": server.type,
            "status": status,
            "anomalies": len(analytics.get("anomalies", [])),
            "anomaly_details": analytics.get("anomalies", []),
            "forecasts": {},
        }

        # Summarize forecasts
        for metric_key in ["disk_c", "disk_d", "ram"]:
            fc = analytics.get("forecasts", {}).get(metric_key, {})
            if fc.get("enough_data"):
                server_digest["forecasts"][metric_key] = {
                    "current": fc.get("current"),
                    "trend_per_day": fc.get("trend_per_day"),
                    "days_until_full": fc.get("days_until_full"),
                    "confidence": fc.get("confidence"),
                }

        summary["servers"].append(server_digest)

        # Flag servers that need attention
        if status in ("critical", "offline") or analytics.get("anomalies"):
            summary["needs_attention"].append(server_digest)

    return summary


# ─────────────────────────────────────────────────────────────────────────
# Availability / health accounting
#
# Rewritten 2026-08-05. The previous implementation treated ANY non-healthy
# status as downtime and measured outage spans from timestamps, extrapolating
# an ongoing outage to `now` while measuring the total window to the LAST
# READING. That produced results anti-correlated with reality — one server
# reported 94.79% uptime with zero healthy readings ever; another reported
# 42.47% while 98.6% of its readings were healthy. Correcting bad data made
# the number WORSE, which is how we established the formula, not the data,
# was at fault.
#
# What changed, and why (grounded in how this is normally defined — see
# docs/plans/OPS_IMPROVEMENTS.md O-7 for sources):
#
#   1. Availability tracks REACHABILITY, not happiness. A host answering on
#      WinRM while its RAM is at 95% is degraded, not down. Conflating the two
#      makes the number useless for both purposes, which is exactly what
#      happened. So warning/critical are UP-but-degraded.
#   2. `unknown` and the operator-initiated transient states are EXCLUDED from
#      the denominator rather than counted as downtime. Counting no-data as
#      downtime means a Prism outage looks like a fleet outage; excluding
#      planned maintenance from availability is the standard convention
#      (cf. Zabbix "excluded downtimes").
#   3. Interval-weighted counting replaces timestamp-delta stitching. Each
#      reading stands for one poll interval. This cannot exceed the window, so
#      the result is structurally confined to 0-100, and a data gap simply
#      contributes nothing instead of silently swallowing or inventing time.
#   4. Two numbers are reported instead of one. `availability_percent` answers
#      "could I reach it"; `health_percent` answers "was it within thresholds
#      while I could reach it". A single number cannot express both, and
#      pretending otherwise is what broke the old one.
#
# Note on naming: what this computes is an SLI (a measured indicator), not an
# SLA (a contractual agreement) and not an SLO (a target). The UI wording
# follows that distinction.
# ─────────────────────────────────────────────────────────────────────────

# Reachable. warning/critical are DEGRADED but still serving — a threshold
# breach is not an outage.
AVAIL_UP_STATUSES = frozenset({"healthy", "warning", "critical"})

# Genuinely not reachable.
AVAIL_DOWN_STATUSES = frozenset({"offline", "unreachable"})

# Excluded from the denominator entirely:
#   unknown                        -> we have no measurement, not a measurement of failure
#   queued/updating/restarting     -> operator-initiated, i.e. planned maintenance
AVAIL_EXCLUDED_STATUSES = frozenset({"unknown", "queued", "updating", "restarting"})


def classify_availability(status: str) -> str:
    """Map a stored status to 'up' | 'down' | 'excluded'.

    An unrecognised status is EXCLUDED, never counted as down. A new status
    value appearing in the DB should not silently manufacture an outage —
    that is the failure mode this whole rewrite exists to remove.
    """
    if status in AVAIL_UP_STATUSES:
        return "up"
    if status in AVAIL_DOWN_STATUSES:
        return "down"
    return "excluded"


def compute_uptime_stats(db, server_name: str, hours: int = 720,
                         poll_interval_seconds: int = 300,
                         timeline: list | None = None) -> dict:
    """Compute availability + health statistics for one server over a window.

    Interval-weighted: every reading represents ``poll_interval_seconds`` of
    observed time. Availability is up-time over OBSERVED time (up + down);
    excluded intervals are not in either.

        availability_percent = up / (up + down) * 100
        health_percent       = healthy / up * 100        # among reachable time

    Returns None for a percentage when its denominator is zero — "no data" is
    not 100% and not 0%, and reporting either is a lie the old version told.

    Keys are kept backward-compatible with the SLA card in reports.html:
    ``uptime_percent`` is an alias of ``availability_percent``.

    Args:
        timeline: Pre-fetched rows in the shape ``db.get_status_timeline``
            returns — ``[{"timestamp": ..., "status": ...}, ...]`` ascending.
            When given, the DB read is skipped entirely.

            This mirrors ``forecast_metric(history=...)`` and exists for the
            same reason: the fleet report reads every server's window in ONE
            scan and slices it per server, where calling this function 29 times
            would re-read the same table 29 times. ``db`` may be None when a
            timeline is supplied.
    """
    if timeline is None:
        timeline = db.get_status_timeline(server_name, hours=hours)

    interval_min = max(poll_interval_seconds, 1) / 60.0

    result = {
        # primary
        "availability_percent": None,
        "health_percent": None,
        # backward-compatible alias consumed by the existing SLA card
        "uptime_percent": None,
        # accounting, in minutes
        "up_minutes": 0.0,
        "down_minutes": 0.0,
        "degraded_minutes": 0.0,
        "excluded_minutes": 0.0,
        "observed_minutes": 0.0,
        "total_downtime_minutes": 0.0,
        # counts
        "total_readings": len(timeline),
        "healthy_readings": 0,
        "up_readings": 0,
        "down_readings": 0,
        "excluded_readings": 0,
        "outage_count": 0,
        "longest_outage_minutes": 0.0,
        "mttr_minutes": None,
        "outages": [],
        "has_data": False,
        "poll_interval_seconds": poll_interval_seconds,
    }

    if not timeline:
        return result

    severity_rank = {"healthy": 0, "unknown": 1, "warning": 2, "critical": 3,
                     "unreachable": 4, "offline": 5}

    up = down = excluded = healthy = 0
    outages = []          # list of dicts, each a contiguous run of 'down'
    run = None

    for reading in timeline:
        status = reading.get("status")
        kind = classify_availability(status)

        if kind == "up":
            up += 1
            if status == "healthy":
                healthy += 1
            if run is not None:
                run["end_time"] = reading.get("timestamp")
                run["closed"] = True
                outages.append(run)
                run = None
        elif kind == "down":
            down += 1
            if run is None:
                run = {
                    "start_time": reading.get("timestamp"),
                    "end_time": None,
                    "readings": 0,
                    "worst_severity": status,
                    "worst_rank": severity_rank.get(status, 4),
                    "closed": False,
                }
            run["readings"] += 1
            rank = severity_rank.get(status, 4)
            if rank > run["worst_rank"]:
                run["worst_severity"] = status
                run["worst_rank"] = rank
        else:
            excluded += 1
            # An excluded reading does NOT close an outage. A collector hiccup
            # in the middle of a real outage must not split it into two, which
            # is how the old gap-breaking logic inflated outage_count.

    if run is not None:
        # Still open at the end of the window. Its duration is the observed
        # down intervals only — deliberately NOT extrapolated to `now`, which
        # is what previously let downtime exceed the window.
        outages.append(run)

    result["has_data"] = (up + down) > 0
    result["healthy_readings"] = healthy
    result["up_readings"] = up
    result["down_readings"] = down
    result["excluded_readings"] = excluded

    result["up_minutes"] = round(up * interval_min, 1)
    result["down_minutes"] = round(down * interval_min, 1)
    result["degraded_minutes"] = round((up - healthy) * interval_min, 1)
    result["excluded_minutes"] = round(excluded * interval_min, 1)
    result["observed_minutes"] = round((up + down) * interval_min, 1)
    result["total_downtime_minutes"] = result["down_minutes"]

    observed = up + down
    if observed > 0:
        avail = 100.0 * up / observed
        # Clamp is defensive only — interval counting cannot leave 0-100.
        avail = min(100.0, max(0.0, avail))
        result["availability_percent"] = round(avail, 2)
        result["uptime_percent"] = result["availability_percent"]
    if up > 0:
        result["health_percent"] = round(min(100.0, max(0.0, 100.0 * healthy / up)), 2)

    processed = []
    longest = 0.0
    for o in outages:
        dur = round(o["readings"] * interval_min, 1)
        longest = max(longest, dur)
        processed.append({
            "start_time": o["start_time"],
            "end_time": o["end_time"],
            "duration_minutes": dur,
            "worst_severity": o["worst_severity"],
            "ongoing": not o["closed"],
        })
    result["outages"] = processed
    result["outage_count"] = len(processed)
    result["longest_outage_minutes"] = longest

    # MTTR over RESOLVED outages only. An ongoing outage has no recovery time
    # yet, and averaging it in understates the real figure.
    resolved = [o for o in processed if not o["ongoing"]]
    if resolved:
        result["mttr_minutes"] = round(
            sum(o["duration_minutes"] for o in resolved) / len(resolved), 1)

    return result


def compute_fleet_availability(summaries: dict) -> dict:
    """Aggregate per-server stats into one fleet figure.

    Reports BOTH aggregations, because they answer different questions and the
    difference between them is informative:

      fleet_availability_percent  — time-weighted: total up minutes over total
          observed minutes across the fleet. This is the true fleet
          availability, and it is the headline number.
      mean_server_availability_percent — unweighted mean of the per-server
          percentages. This is "the average server", and it is the one that
          misleads: a host observed for two hours counts as much as one
          observed for thirty days, so a newly-added or briefly-seen server can
          swing it hard (a Simpson's-paradox-shaped distortion).

    Servers with no observed time are excluded from both rather than counted as
    100% or 0%.
    """
    out = {
        "fleet_availability_percent": None,
        "mean_server_availability_percent": None,
        "fleet_health_percent": None,
        "servers_counted": 0,
        "servers_no_data": 0,
        "total_up_minutes": 0.0,
        "total_down_minutes": 0.0,
        "total_observed_minutes": 0.0,
        "worst_server": None,
        "worst_availability_percent": None,
    }
    if not summaries:
        return out

    up_sum = down_sum = 0.0
    healthy_sum = 0.0
    per_server = []

    for name, s in summaries.items():
        if not isinstance(s, dict):
            continue
        if not s.get("has_data"):
            out["servers_no_data"] += 1
            continue
        up_sum += s.get("up_minutes") or 0.0
        down_sum += s.get("down_minutes") or 0.0
        # healthy minutes reconstructed from up minus degraded
        healthy_sum += max(0.0, (s.get("up_minutes") or 0.0)
                           - (s.get("degraded_minutes") or 0.0))
        if s.get("availability_percent") is not None:
            per_server.append((name, s["availability_percent"]))

    out["servers_counted"] = len(per_server)
    out["total_up_minutes"] = round(up_sum, 1)
    out["total_down_minutes"] = round(down_sum, 1)
    observed = up_sum + down_sum
    out["total_observed_minutes"] = round(observed, 1)

    if observed > 0:
        out["fleet_availability_percent"] = round(
            min(100.0, max(0.0, 100.0 * up_sum / observed)), 2)
    if up_sum > 0:
        out["fleet_health_percent"] = round(
            min(100.0, max(0.0, 100.0 * healthy_sum / up_sum)), 2)
    if per_server:
        out["mean_server_availability_percent"] = round(
            sum(v for _n, v in per_server) / len(per_server), 2)
        worst = min(per_server, key=lambda kv: kv[1])
        out["worst_server"] = worst[0]
        out["worst_availability_percent"] = worst[1]

    return out


# compute_fleet_uptime_summary was removed on 2026-08-06 with its only caller,
# /api/sla/summary. It looped compute_uptime_stats over the fleet, one DB read
# per server. compute_fleet_report below does the same accounting from a single
# scan — and reuses compute_uptime_stats via its timeline= parameter rather than
# reimplementing the outage-run logic, so the tests that covered this path still
# cover it.


# ─────────────────────────────────────────────────────────────────────────
# Fleet report — see docs/plans/FLEET_REPORT_SPEC.md
#
# Availability does not rank anything on a real fleet. Measured over 720 h on
# the 29-server instance: 28 of 29 servers sit inside a 0.73-POINT band
# (99.41-99.65%), while health ranges 1.03-100% with 15 of 29 below 90%. The
# Reports page rendered the first at text-2xl and the second at 10px, so it led
# with the one number that carries no information.
#
# Everything below is computed from ONE fleet-wide scan.
# ─────────────────────────────────────────────────────────────────────────

FLEET_SPARKLINE_BUCKETS = 24

# Attention rule. Each floor was measured against the live fleet before being
# chosen — see the note on dead clauses below.
ATTENTION_HEALTH_FLOOR = 90.0
ATTENTION_AVAILABILITY_FLOOR = 99.0
ATTENTION_CAPACITY_RISKS = frozenset({"high", "medium"})

# Two clauses were REJECTED because they do not discriminate:
#
#   outage_count >= 1        selects 29 of 29. Every server has 2-6 outages
#                            totalling 9-43 min in runs of 8-11 min — a
#                            fleet-wide baseline (collector restarts), not
#                            signal. Replaced by availability < 99, the "two
#                            nines" boundary availTextClass() in reports.html
#                            already uses, which selects 1 server the health
#                            floor misses.
#
#   capacity risk == high    selects 0 of 29. There are no high-risk rows on
#                            this fleet, so the clause was permanently dead.
#                            Widened to include 'medium' (days_to_threshold
#                            <= 90), which selects 1 server the health floor
#                            misses.
#
# A filter that matches everything and a filter that matches nothing are the
# same bug. If a future change makes a clause select all servers or none,
# tests/test_fleet_report.py fails.

# Order matters: it is the tie-break when two metrics are equally far over
# their thresholds. Matches detection._get_worst_metric's declaration order.
_DRIVER_METRICS = (
    ("cpu", "cpu_percent", "cpu_warning"),
    ("ram", "ram_percent", "ram_warning"),
    ("disk_c", "disk_c_percent", "disk_warning"),
    ("disk_d", "disk_d_percent", "disk_warning"),
)


def _health_sparkline(up_statuses: list[str],
                      buckets: int = FLEET_SPARKLINE_BUCKETS) -> list[float]:
    """Percent-healthy per equal-COUNT bucket of a server's up readings.

    Equal-count, not equal-time. The instance runs at a ~7.8% duty cycle —
    `metrics` spans 667 hours but holds rows in 52 distinct hours — so bucketing
    720 h of wall-clock would produce a sparkline that is ~92% empty and reads
    as an outage. The caller must label this "across N observed readings",
    never as a time span.
    """
    n = len(up_statuses)
    if n == 0:
        return []
    b = min(buckets, n)
    out = []
    for i in range(b):
        chunk = up_statuses[(i * n) // b:((i + 1) * n) // b]
        if not chunk:
            continue
        healthy = sum(1 for s in chunk if s == "healthy")
        out.append(round(100.0 * healthy / len(chunk), 1))
    return out


def _attribute_degradation(degraded_rows: list[dict],
                           thresholds: dict) -> tuple[dict, int]:
    """Attribute degraded readings to the metric furthest over ITS threshold.

    Returns ``(tally, no_breach_readings)``.

    This is a statement about STORED VALUES, not about which detector fired.
    Re-deriving status from static thresholds disagrees with the stored status
    on 7,069 of 67,197 up-readings (89.5% agreement) because baseline_detection,
    anomaly_detection and the security checks also raise warnings. One server
    (APP01) is 96% unexplained by thresholds alone.

    So a reading where nothing crossed a threshold is NOT attributed to a
    guess — it is counted separately, and the caller characterises that bucket
    from the `events` table. Claiming "degraded because CPU" on those readings
    would be confidently wrong for roughly a third of the fleet.
    """
    tally: dict[str, dict] = {}
    no_breach = 0

    for row in degraded_rows:
        best = None
        best_excess = -1.0
        best_value = None
        best_threshold = None
        for key, col, threshold_key in _DRIVER_METRICS:
            value = row.get(col)
            threshold = thresholds.get(threshold_key)
            if value is None or value < 0 or threshold is None:
                continue
            if value >= threshold:
                excess = value - threshold
                # Strict > keeps the FIRST declared metric on a tie.
                if excess > best_excess:
                    best, best_excess = key, excess
                    best_value, best_threshold = value, threshold

        if best is None:
            no_breach += 1
            continue

        entry = tally.setdefault(best, {
            "readings": 0, "sum_value": 0.0, "sum_excess": 0.0,
            "threshold": best_threshold,
        })
        entry["readings"] += 1
        entry["sum_value"] += best_value
        entry["sum_excess"] += best_excess

    return tally, no_breach


def compute_fleet_report(db, servers_cfg: list, hours: int = 720,
                         poll_interval_seconds: int = 300,
                         scope: str = "attention") -> dict:
    """Health-led fleet report: availability, degradation, capacity, in one scan.

    Args:
        db: Database instance.
        servers_cfg: ``ServerConfig`` objects (or dicts) — the authoritative
            fleet list. A server with rows in `metrics` but no config entry is
            NOT reported, matching /api/sla/summary. That is what keeps a
            deleted host (STANDALONE01, removed 2026-08-05) out of the report.
        hours: window, also the window the capacity forecast regresses over.
        scope: 'attention' returns full detail for servers needing attention
            and a stub for the rest; 'all' returns full detail for everything.

    One ``get_fleet_metrics_window`` call feeds all four computations —
    availability (via compute_uptime_stats), degradation attribution, the
    capacity forecast (via forecast_metric) and the sparkline. Measured ~0.58 s
    for 29 servers / 67,786 rows, against 0.84 s for the two endpoints it
    replaces.
    """
    # Late import: reports imports analytics, so a module-level import here
    # would close the cycle.
    from reports import CAPACITY_METRIC_MAP, capacity_row

    rows = db.get_fleet_metrics_window(hours=hours)
    event_counts = db.get_fleet_event_counts(hours=hours)

    by_server: dict[str, list[dict]] = {}
    for row in rows:
        by_server.setdefault(row["server_name"], []).append(row)

    names: list[str] = []
    thresholds_by_name: dict[str, dict] = {}
    for cfg in servers_cfg or []:
        if isinstance(cfg, dict):
            name, thresholds = cfg.get("name"), cfg.get("thresholds")
        else:
            name, thresholds = getattr(cfg, "name", None), getattr(cfg, "thresholds", None)
        if not name:
            continue
        names.append(name)
        thresholds_by_name[name] = thresholds or {}

    servers_out = []
    summaries = {}

    for name in names:
        server_rows = by_server.get(name, [])
        thresholds = thresholds_by_name.get(name, {})

        # compute_uptime_stats owns the outage-run logic and is already tested.
        # db is None here: with a timeline supplied it never touches the DB.
        timeline = [{"timestamp": r["timestamp"], "status": r["status"]}
                    for r in server_rows]
        stats = compute_uptime_stats(None, name, hours=hours,
                                     poll_interval_seconds=poll_interval_seconds,
                                     timeline=timeline)
        summaries[name] = stats

        up_rows = [r for r in server_rows
                   if classify_availability(r["status"]) == "up"]
        degraded_rows = [r for r in up_rows if r["status"] != "healthy"]
        degraded_count = len(degraded_rows)

        tally, no_breach = _attribute_degradation(degraded_rows, thresholds)
        drivers = [{
            "metric": key,
            "readings": e["readings"],
            "percent_of_degraded": round(100.0 * e["readings"] / degraded_count, 1)
                                   if degraded_count else 0.0,
            "avg_value": round(e["sum_value"] / e["readings"], 1),
            "threshold": e["threshold"],
            "avg_excess": round(e["sum_excess"] / e["readings"], 1),
        } for key, e in tally.items()]
        drivers.sort(key=lambda d: (-d["readings"], d["metric"]))

        capacity = []
        for metric_key, (metric_label, db_col) in CAPACITY_METRIC_MAP.items():
            try:
                forecast = forecast_metric(db, name, metric=metric_key,
                                           hours=hours, target_percent=90.0,
                                           history=server_rows)
            except Exception:
                logger.exception("Forecast failed for %s/%s", name, metric_key)
                continue
            current = forecast.get("current")
            if current is None:
                # Below MIN_READINGS: fall back to the last stored value in the
                # slice we already hold, rather than re-reading the table.
                for r in reversed(server_rows):
                    v = r.get(db_col)
                    if v is not None and v >= 0:
                        current = round(v, 1)
                        break
                if current is None:
                    continue
            capacity.append(capacity_row(name, metric_key, metric_label,
                                         forecast, current))

        health = stats["health_percent"]
        availability = stats["availability_percent"]

        reasons = []
        if health is not None and health < ATTENTION_HEALTH_FLOOR:
            reasons.append("health")
        if availability is not None and availability < ATTENTION_AVAILABILITY_FLOOR:
            reasons.append("availability")
        if any(c["risk"] in ATTENTION_CAPACITY_RISKS for c in capacity):
            reasons.append("capacity")

        up_minutes = stats["up_minutes"]
        servers_out.append({
            "name": name,
            "attention": bool(reasons),
            "attention_reasons": reasons,

            "health_percent": health,
            "availability_percent": availability,
            "has_data": stats["has_data"],
            "observed_minutes": stats["observed_minutes"],
            "up_minutes": up_minutes,
            "down_minutes": stats["down_minutes"],
            "degraded_minutes": stats["degraded_minutes"],
            "degraded_percent_of_up": round(100.0 * stats["degraded_minutes"] / up_minutes, 1)
                                      if up_minutes else None,

            "outage_count": stats["outage_count"],
            "total_downtime_minutes": stats["total_downtime_minutes"],
            "longest_outage_minutes": stats["longest_outage_minutes"],
            "mttr_minutes": stats["mttr_minutes"],
            # The full outages[] is unbounded per server. Only the 5 longest
            # are returned; the counts above still describe all of them.
            "outages_top": sorted(stats["outages"],
                                  key=lambda o: o["duration_minutes"],
                                  reverse=True)[:5],

            "drivers": drivers,
            "no_threshold_breach": {
                "readings": no_breach,
                "percent_of_degraded": round(100.0 * no_breach / degraded_count, 1)
                                       if degraded_count else 0.0,
                # Window-scoped characterisation, NOT a per-reading join.
                "event_counts": event_counts.get(name, {}),
            },

            "capacity": capacity,
            "health_sparkline": _health_sparkline([r["status"] for r in up_rows]),
        })

    # Worst-first IS the triage: health ascending, then soonest capacity
    # deadline, then name. No-data servers sort last — they are not "0% healthy".
    def _sort_key(s):
        health = s["health_percent"]
        soonest = min((c["days_to_threshold"] for c in s["capacity"]
                       if c["days_to_threshold"] is not None), default=float("inf"))
        return (health is None, health if health is not None else 0.0, soonest, s["name"])

    servers_out.sort(key=_sort_key)

    fleet_avail = compute_fleet_availability(summaries)
    with_health = [s for s in servers_out if s["health_percent"] is not None]
    worst_health = min(with_health, key=lambda s: s["health_percent"]) if with_health else None

    fleet = {
        "servers_total": len(names),
        "servers_counted": fleet_avail["servers_counted"],
        "servers_no_data": fleet_avail["servers_no_data"],
        "servers_needing_attention": sum(1 for s in servers_out if s["attention"]),
        "fleet_health_percent": fleet_avail["fleet_health_percent"],
        "fleet_availability_percent": fleet_avail["fleet_availability_percent"],
        "mean_server_availability_percent": fleet_avail["mean_server_availability_percent"],
        "total_observed_minutes": fleet_avail["total_observed_minutes"],
        "total_degraded_minutes": round(
            sum(s["degraded_minutes"] for s in servers_out), 1),
        # Worst by HEALTH — the number this report leads with. The
        # availability-worst server is reported separately rather than
        # overloading one "worst" field with two different meanings.
        "worst_server": worst_health["name"] if worst_health else None,
        "worst_health_percent": worst_health["health_percent"] if worst_health else None,
        "worst_availability_server": fleet_avail["worst_server"],
        "worst_availability_percent": fleet_avail["worst_availability_percent"],
    }

    if scope != "all":
        # Stub the servers that do not need attention. This is what bounds the
        # default payload: at 500 servers a full row for every host is the
        # difference between ~40 KB and ~400 KB.
        servers_out = [s if s["attention"] else {
            "name": s["name"],
            "attention": False,
            "health_percent": s["health_percent"],
            "availability_percent": s["availability_percent"],
        } for s in servers_out]

    return {
        "hours": hours,
        "poll_interval_seconds": poll_interval_seconds,
        "scope": scope,
        "fleet": fleet,
        "servers": servers_out,
    }
