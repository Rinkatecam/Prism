"""The estate service: the seam between the hot cache and the pure fold.

WP-1 phase 2 wiring (docs/plans/SEVERITY_MODEL_SPEC.md). This module holds
the only mutable estate state in the app — one FoldState — and three ways
to touch it:

    recompute(...)      the aggregator calls this after each merge
    tick(...)           the supervisor calls this every 5s to move timers
    current_verdict()   views reads this; None until the first recompute

`build_snapshot` and `display_severity` are PURE and carry all the glue and
all the gates, so the interesting logic is tested without a collector. The
stateful trio is a thin lock-guarded wrapper.

Everything takes `now` as a parameter. The estate's damping IS a clock, and
the repo's two flaky tests both came from wall-time buried in logic — so
wall-time lives only at the two call sites (aggregator, supervisor), never
in here.
"""

from __future__ import annotations

import logging
import threading

from severity_roles import resolve_role, weight_for
from estate_fold import fold, advance, FoldState, RawVerdict

logger = logging.getLogger("prism.estate")

# Estate display words. The three weighted bands come from the fold; the
# three pre-band gates wrap it. bpm matches the shipped _VITALS_BPM exactly
# so the heart monitor's existing contract is untouched.
_BPM = {"calm": 60, "elevated": 96, "urgent": 132,
        "flat": 0, "idle": 0, "unmeasured": 0}

# Service severity → points is the fold's job; here we only classify the
# health-summary shape into up/down/unknown for the snapshot.
_SERVICE_DOWN = {"down", "critical", "offline"}


def build_snapshot(servers, cache, settings, latched_worst, health_summary,
                   closure: dict | None = None, groups: dict | None = None,
                   now: float | None = None):
    """Pure: (servers, cache, settings, latch, health) → (server_units, service_units).

    `servers`   iterable of ServerConfig-likes (name, type, criticality)
    `cache`     state.latest_by_server — {name: {"status": ...}}
    `settings`  for role overrides (severity_model) and maintenance windows
    `latched_worst`  {name: worst_recent_status} from flap_state (may be {})
    `health_summary` the health-check summary dict, or {} — its "per_check"
                     list, if present, becomes weighted service units
    `closure`   the dependency closure (WP-1 phase 3). When given, the
                cascade reducer runs and a server whose failure is explained
                by a failed upstream is marked `impacted` — contributing
                0.15 instead of 1.0, so three machines down BECAUSE of one
                root do not fold as four outages. Absent ⇒ pure no-op.
    `groups`    redundancy groups ({group_name: [members]}), e.g. the seeded
                Domain Services group.
    `now`       epoch seconds, for the business-hours weight profile. Passed
                rather than read: this function is pure and the estate's
                damping IS a clock, so wall-time lives at the call sites only.
                Absent ⇒ base weights, which is the honest answer when the
                caller has no clock to offer.
    """
    from maintenance import is_in_maintenance
    from cascade import effective_severity

    raw_status = {}
    for s in servers:
        row = cache.get(s.name) or {}
        raw_status[s.name] = (latched_worst.get(s.name)
                              or row.get("status") or "unknown")

    server_units = []
    for s in servers:
        role, _source = resolve_role(s, settings)
        status = raw_status[s.name]
        reason_code = None
        impacted_by = None
        if closure:
            state, reason_code, root = effective_severity(
                s.name, status, closure, raw_status, groups)
            if state == "impacted":
                # The fold reads `status`, so the substitution happens here
                # rather than in the fold — one place decides what a server
                # IS, and the fold only ever weighs it.
                status = "impacted"
                impacted_by = root
        server_units.append({
            "name": s.name,
            "status": status,
            "role": role,
            "weight": weight_for(role, settings, now=now),
            "in_maintenance": _safe_in_maintenance(is_in_maintenance, s.name, settings),
            "latched_worst": latched_worst.get(s.name),
            "reason_code": reason_code,
            "impacted_by": impacted_by,
        })

    # Services: each configured health check is a unit weighted by its bound
    # server's role (or a per-check override, once WP-4 adds one). Absent a
    # per_check breakdown we contribute nothing — a summary of counts has no
    # per-unit weight to fold.
    service_units = []
    role_by_server = {u["name"]: u["role"] for u in server_units}
    for check in (health_summary or {}).get("per_check", []):
        status = (check.get("status") or "unknown").lower()
        status = "down" if status in _SERVICE_DOWN else (
            "up" if status in ("up", "ok", "healthy") else "unknown")
        bound = check.get("server")
        role = check.get("criticality") or role_by_server.get(bound) or "important"
        service_units.append({
            "name": check.get("name") or f"check on {bound}",
            "status": status,
            "weight": weight_for(role, settings, now=now)
            if role in ("critical_infrastructure", "important", "background")
            else 4,
        })
    return server_units, service_units


def _safe_in_maintenance(fn, name, settings) -> bool:
    try:
        return bool(fn(name, settings))
    except Exception:
        logger.debug("maintenance check failed for %s", name, exc_info=True)
        return False


def display_severity(band: str, server_count: int, measured: int,
                     down: int, monitored: int) -> str:
    """Wrap the fold's weighted band in the three pre-band gates.

    Order matters and is the same order the shipped `_estate_vitals` used:
    idle (nothing to watch) → unmeasured (watching, no answer yet) → flat
    (everything answered is down) → the weighted band. The fold must not
    speak for an estate that has not been measured, which is exactly what
    the `unmeasured` severity shipped earlier protects.
    """
    if server_count <= 0 and monitored <= 0:
        return "idle"
    if measured <= 0:
        return "unmeasured"
    if down >= measured and down > 0:
        return "flat"
    return band


# ── the one piece of mutable state ────────────────────────────────────────

_lock = threading.RLock()
_fold_state = FoldState()
_last_raw: RawVerdict | None = None
_last_counts: dict = {}
_verdict: dict | None = None
_verdict_at: float = 0.0
_poll_interval: int = 60

# Freshness accounting for the dwell's evidence guard. The guard's stated
# purpose (SEVERITY_MODEL_SPEC §"The three clocks") is that the estate must
# not calm down on STALE data — "a 60s dwell can expire having seen only ONE
# fresh sample". We defend that risk at the FLEET level: track the newest
# metric timestamp across the cache, and count how many distinct newer
# timestamps have arrived since the current dwell opened. Two = the collector
# is demonstrably alive and producing fresh data, which is what "answered
# twice" was protecting. Fleet-level rather than per-server on purpose: it is
# simpler, it needs no contributor bookkeeping, and it defends the exact
# failure named — a dead collector cannot advance the fleet's newest sample.
_fleet_token: str | None = None      # newest metric timestamp seen
_fleet_fresh: int = 0                # distinct newer tokens since dwell open
_dwell_marker: float | None = None   # the dwell_opened_at we reset _fleet_fresh for


def reset() -> None:
    """Test hook: forget all estate state. Never called in production."""
    global _fold_state, _last_raw, _last_counts, _verdict, _verdict_at
    global _fleet_token, _fleet_fresh, _dwell_marker
    with _lock:
        _fold_state = FoldState()
        _last_raw = None
        _last_counts = {}
        _verdict = None
        _verdict_at = 0.0
        _fleet_token = None
        _fleet_fresh = 0
        _dwell_marker = None


def recompute(servers, cache, settings, latched_worst, health_summary, *,
              now: float, poll_interval: int = 60,
              fresh_counts: dict | None = None,
              closure: dict | None = None,
              groups: dict | None = None) -> None:
    """Fold the whole fleet, advance the display state, publish the verdict.

    Called from the supervisor tick every 5s. O(fleet); zero queries —
    everything it needs is already in the hot cache and the config. Running
    every tick means this one call both folds the current truth AND advances
    the dwell/freeze timers, so no separate timer pass is needed.

    `fresh_counts`, if given, overrides the internal fleet-freshness signal —
    used by tests to drive the dwell deterministically. Production leaves it
    None and the fleet token accounting supplies it.
    """
    global _fold_state, _last_raw, _last_counts, _verdict, _verdict_at
    global _poll_interval, _fleet_token, _fleet_fresh, _dwell_marker
    server_units, service_units = build_snapshot(
        servers, cache, settings, latched_worst or {}, health_summary or {},
        closure=closure, groups=groups, now=now)
    raw = fold(server_units, service_units, settings)

    measured = sum(1 for u in server_units if u["status"] != "unknown")
    # `impacted` deliberately does NOT count as down: those machines are
    # explained by a root, and counting them would trip the flat gate
    # ("everything measured is down") on a cascade that has one real cause.
    down = sum(1 for u in server_units
               if (u["latched_worst"] or u["status"]) in ("offline", "down"))
    counts = {"server_count": len(server_units), "measured": measured,
              "down": down, "monitored": raw.monitored_weight,
              "ok": sum(1 for u in server_units if u["status"] == "healthy")}

    with _lock:
        _poll_interval = poll_interval
        # Advance the fleet-freshness counter from the cache's newest sample.
        newest = _newest_token(cache)
        if newest is not None and newest != _fleet_token:
            _fleet_token = newest
            _fleet_fresh += 1
        fc = fresh_counts if fresh_counts is not None else {"fleet": _fleet_fresh}

        _fold_state = advance(_fold_state, raw, now=now,
                              poll_interval=poll_interval, fresh_counts=fc)

        # If the dwell just (re)opened, reset the freshness counter so the
        # "twice" is counted from the dwell's opening, not from process start.
        if _fold_state.dwell_opened_at != _dwell_marker:
            _dwell_marker = _fold_state.dwell_opened_at
            _fleet_fresh = 0

        _last_raw = raw
        _last_counts = counts
        _verdict = _render(_fold_state, raw, counts)
        _verdict_at = now


def tick(now: float, fresh_counts: dict | None = None) -> None:
    """Advance the display state against the LAST raw verdict, no re-fold.

    Retained for callers that want pure timer movement without a cache read
    (and for the freshness test). Production drives everything through
    `recompute` on the 5s tick; this is a no-op before the first recompute.
    """
    global _fold_state, _verdict, _verdict_at
    with _lock:
        if _last_raw is None:
            return
        fc = fresh_counts if fresh_counts is not None else {"fleet": _fleet_fresh}
        _fold_state = advance(_fold_state, _last_raw, now=now,
                              poll_interval=_poll_interval, fresh_counts=fc)
        _verdict = _render(_fold_state, _last_raw, _last_counts)
        _verdict_at = now


def _newest_token(cache: dict) -> str | None:
    """The newest metric timestamp across the cache — the fleet-freshness
    token. Absent timestamps are ignored; a fleet with none yet returns None
    and freshness simply does not advance (correct: nothing has reported)."""
    tokens = [row.get("timestamp") for row in cache.values()
              if isinstance(row, dict) and row.get("timestamp")]
    return max(tokens) if tokens else None


# How long a published verdict stays trustworthy without a refresh. The
# supervisor recomputes every 5s, so anything older than this means the
# supervisor is dead, wedged, or never started — and a verdict from a
# collector that stopped is exactly the stale-evidence failure the dwell's
# freshness guard exists to prevent, one layer up. Expiring to None makes
# the reader fall back to its own live count-fold rather than render a
# confident answer about a fleet nobody is watching any more.
_VERDICT_TTL_S = 60.0


def current_verdict(now: float | None = None) -> dict | None:
    """The published estate verdict, or None if there is none to trust.

    None means "no answer" in two cases: nothing folded yet (cold start),
    and the last fold is older than _VERDICT_TTL_S (the supervisor stopped
    publishing). Both make the reader fall back to its own count-fold —
    never a stale confident answer. `now` is injectable for tests.
    """
    import time as _time
    now = _time.time() if now is None else now
    with _lock:
        if _verdict is None:
            return None
        age = now - _verdict_at
        if age > _VERDICT_TTL_S:
            return None
        out = dict(_verdict)
        out["age_s"] = round(age, 1)
        return out


def _render(state: FoldState, raw: RawVerdict, counts: dict) -> dict:
    severity = display_severity(
        band=state.displayed_band,
        server_count=counts.get("server_count", 0),
        measured=counts.get("measured", 0),
        down=counts.get("down", 0),
        monitored=int(counts.get("monitored", 0)))
    return {
        "severity": severity,
        "bpm": _BPM.get(severity, 0),
        "band": state.displayed_band,
        "settling": state.settling,
        "rail": raw.rail,
        "contributors": raw.contributors,
        "why_not_higher": raw.why_not_higher,
        "score": raw.score,
        "ok": counts.get("ok", 0),
        "monitored": counts.get("server_count", 0),
        "excluded_maintenance": raw.excluded_maintenance,
    }
