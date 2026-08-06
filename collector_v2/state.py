"""Module-level shared state for the v2 collector.

Pre-R2: this module reached back into ``collector.py`` to reuse v1's
dict objects so legacy API endpoints (``from collector import
latest_by_server``) saw the same data as v2.

Post-R2: shared state lives in the top-level ``state`` module (a
neutral home that neither v1 nor v2 owns). This module re-exports
those references so existing v2 code continues to work, AND so the
``collector_v2.state.latest_by_server`` import path keeps producing
the same dict object as v1's ``collector.latest_by_server`` re-export
and the canonical ``state.latest_by_server``.

The contract is unchanged: these mappings are written by the
aggregator, read by everything else. Workers do NOT mutate these
directly — they only write to the result_queue. The aggregator is the
sole writer so per-key races are impossible.

The ``_state_lock`` is held briefly during reads/writes of the bigger
maps (specifically ``latest_by_server``). Per-check info maps
(``server_update_info``, ``server_hardware_info``, ``server_auth_info``)
are dict-of-dict and read/written without explicit locking because
the GIL makes single-key dict ops atomic and the aggregator is the
sole writer.

When v1 is fully retired (R4), the re-export from ``collector.py``
disappears. This module's references remain valid — they point at
the same neutral ``state`` module that survives.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .types import ServerHealth, CheckType

# ── Public mirrors (read by API endpoints) ───────────────────────────────
# These are the v1-compatible names. Source of truth is the top-level
# ``state`` module — v1's collector.py and v2's aggregator both write to
# the same dict objects there.
import state as _shared_state

latest_by_server = _shared_state.latest_by_server
server_update_info = _shared_state.server_update_info
server_hardware_info = _shared_state.server_hardware_info
server_auth_info = _shared_state.server_auth_info
_state_lock = _shared_state._state_lock

# Heartbeat timestamps for the watchdog. Each thread updates its own.
last_supervisor_tick: float = 0.0
last_aggregator_tick: float = 0.0
# Workers don't get a single heartbeat (they're a pool); instead the
# WorkerPool reports last_worker_activity_at = max of all worker timestamps.
last_worker_activity_at: float = 0.0

# F-AT-1 (CSV-12 / 17 remediation): scheduled audit-chain verifier
# stashes its last result here. Surfaces in /api/system/health so an
# external watcher can poll for tampering without polling the DB
# directly. Shape:
#   {"ts": float (epoch), "ok": bool, "checked": int,
#    "first_break_id": int | None, "first_break_reason": str | None}
last_audit_chain_check: dict | None = None


# ── Per-server health, owned by the supervisor ───────────────────────────
# Stored in this module so periodics.py and the API can read (read-only!).
# Writes happen ONLY from the supervisor thread, except for the failure
# counter / next_due updates which the aggregator does via the public
# update_server_health() helper below.

server_health: dict[str, ServerHealth] = {}
_server_health_lock = threading.Lock()


def get_server_health(name: str) -> ServerHealth | None:
    """Snapshot a server's health entry. Returns None if not tracked yet."""
    with _server_health_lock:
        h = server_health.get(name)
        if h is None:
            return None
        # Return the live object — callers should not mutate it. We don't
        # deep-copy because mutating ServerHealth from outside the supervisor
        # would be a bug and we want it to show up loudly in tests.
        return h


def upsert_server_health(name: str, health: ServerHealth) -> None:
    """Register or replace a server's health entry."""
    with _server_health_lock:
        server_health[name] = health


def remove_server_health(name: str) -> None:
    """Drop a server's health entry (called when config removes a server)."""
    with _server_health_lock:
        server_health.pop(name, None)


# ── Aggregator-side helpers (called from aggregator.py) ──────────────────
# Updates that need to be visible to the supervisor's next tick. We keep
# them here so the supervisor and aggregator don't have to know about each
# other directly.

def mark_check_completed(server_name: str, ct: CheckType,
                          ok: bool, finished_at: datetime) -> None:
    """Aggregator calls this after persisting a Result. Updates the
    per-server health record so the supervisor's NEXT tick decides whether
    to apply backoff or schedule normal cadence."""
    with _server_health_lock:
        h = server_health.get(server_name)
        if h is None:
            return  # supervisor hasn't registered this server yet — ignore
        h.pending[ct] = False
        if ok:
            h.record_success(ct, finished_at)
        else:
            h.record_failure(ct)


def update_latest_metric(server_name: str, row: dict[str, Any]) -> None:
    """Aggregator calls this after persisting a metric row. Refreshes the
    dashboard's hot cache atomically."""
    with _state_lock:
        latest_by_server[server_name] = row


def update_server_update_info(server_name: str, info: dict[str, Any]) -> None:
    """Aggregator calls this after processing an updates Result. Carries
    the `transient_error` semantics the dashboard relies on."""
    # No lock needed — single writer (aggregator).
    server_update_info[server_name] = info


def update_server_hardware_info(server_name: str, info: dict[str, Any]) -> None:
    server_hardware_info[server_name] = info


def heartbeat_supervisor() -> None:
    global last_supervisor_tick
    last_supervisor_tick = time.time()


def heartbeat_aggregator() -> None:
    global last_aggregator_tick
    last_aggregator_tick = time.time()


def heartbeat_worker() -> None:
    global last_worker_activity_at
    last_worker_activity_at = time.time()


def get_v2_health_snapshot() -> dict[str, Any]:
    """Read-only snapshot of v2's runtime health, for /api/system/health."""
    now = time.time()
    fleet = get_fleet_status(now=now)
    return {
        "supervisor_last_tick_s_ago": (now - last_supervisor_tick) if last_supervisor_tick else None,
        "aggregator_last_tick_s_ago": (now - last_aggregator_tick) if last_aggregator_tick else None,
        "workers_last_activity_s_ago": (now - last_worker_activity_at) if last_worker_activity_at else None,
        "tracked_servers": len(server_health),
        "cached_metrics": len(latest_by_server),
        # Fleet rollup — drives the topbar pulse widget's "up/total" counter
        # and "silent server" list, but useful to any external monitor too.
        "servers_total": fleet["total"],
        "servers_up": fleet["up"],
        "silent_servers": fleet["silent"],
    }


# ─────────────────────────────────────────────────────────────────────
# Pulse buffer — feeds the topbar ECG widget.
#
# Every Result the aggregator processes pushes a single tuple here
# (timestamp, server, check_type_name, ok, duration_ms). The /api/collector/pulse
# endpoint snapshots this on each poll and returns events newer than the
# client's watermark, so the steady-state payload is tiny.
#
# Sized for ~10s of headroom at peak rate: 30 servers × 4 check types,
# even if every result landed in the same second, 1000 is comfortable. The
# deque is bounded — old events drop off the left silently. No persistence;
# pulse is intentionally ephemeral.
#
# Thread-safety: deque.append is atomic under CPython's GIL, so the
# aggregator's hot path is lock-free. Snapshot reads take a brief lock to
# avoid a "size changed during iteration" RuntimeError if the buffer is
# being appended to while we copy it out.
# ─────────────────────────────────────────────────────────────────────

_pulse_buffer: deque[tuple[float, str, str, bool, int]] = deque(maxlen=1000)
_pulse_lock = threading.Lock()


def record_pulse(ts: float, server: str, check_type: str,
                 ok: bool, duration_ms: int) -> None:
    """Append a single pulse event. Called from the aggregator's hot path
    after every Result is processed. Defensive — must never raise.

    Takes _pulse_lock for consistency with the documented contract: readers
    snapshot under the lock, so writers MUST also lock or the snapshot's
    safety reduces to "CPython GIL atomicity" which we don't want to
    depend on. The lock is held for one ``deque.append`` (sub-microsecond);
    the aggregator's hot path is not measurably affected.
    """
    try:
        coerced = (float(ts), str(server), str(check_type),
                   bool(ok), int(duration_ms))
    except Exception:
        # Cannot let pulse instrumentation break aggregation. Swallow.
        return
    with _pulse_lock:
        _pulse_buffer.append(coerced)


def get_recent_pulses(since_ts: float | None = None,
                      window_s: float = 12.0) -> list[dict[str, Any]]:
    """Return pulses newer than ``since_ts`` (preferred) or within the last
    ``window_s`` seconds. Output is a list of plain dicts safe to jsonify.

    The endpoint passes a ``since`` watermark on every poll after the first,
    keeping the response tiny in steady state. On the first poll the
    watermark is unset → fall back to a fixed window so the widget can paint
    the initial strip without an empty animation.
    """
    if since_ts is not None:
        cutoff = float(since_ts)
    else:
        cutoff = time.time() - float(window_s)
    with _pulse_lock:
        snapshot = list(_pulse_buffer)
    out: list[dict[str, Any]] = []
    for ts, server, check_type, ok, ms in snapshot:
        if ts <= cutoff:
            continue
        out.append({
            "ts": ts,
            "server": server,
            "check": check_type,
            "ok": ok,
            "ms": ms,
        })
    return out


def clear_pulses() -> None:
    """Test helper — reset the buffer between cases."""
    with _pulse_lock:
        _pulse_buffer.clear()


# ─────────────────────────────────────────────────────────────────────
# Fleet rollup — "how many servers are reporting fresh data right now?"
# Drives both the v2 health snapshot's new fields and the pulse endpoint.
# ─────────────────────────────────────────────────────────────────────

# How long a server can go without a successful METRICS sample before we
# call it "silent". Generous because METRICS cadence is configurable per
# server and can legitimately be up to 5 min; we want a single missed cycle
# not to flag the server.
_SILENT_THRESHOLD_S = 300.0


def get_fleet_status(now: float | None = None) -> dict[str, Any]:
    """Compute current fleet rollup from server_health.

    Returns ``{total, up, silent: [{name, silent_s}]}``. "Up" means the
    server has had a successful METRICS sample within ``_SILENT_THRESHOLD_S``
    seconds. Servers that have never reported are counted as silent with
    ``silent_s=None`` so the UI can show "no samples yet" vs "stopped
    reporting at <time>".

    Thread-safety: snapshot ``server_health`` AND the per-server check
    state under ``_server_health_lock`` in one block. The supervisor's
    ``_ensure_check`` mutates ``h.checks`` (inserts new CheckType keys)
    under the same lock — iterating ``h.checks`` outside the lock could
    raise ``RuntimeError: dictionary changed size during iteration``.
    """
    if now is None:
        now = time.time()
    # Snapshot under lock: (name, metrics_last_ok_at_or_None) tuples.
    # Pulling just the field we need (vs. the whole CheckState) keeps the
    # critical section short and lets the rest run lock-free.
    with _server_health_lock:
        snapshot: list[tuple[str, datetime | None]] = []
        for name, h in server_health.items():
            cs = h.checks.get(CheckType.METRICS)
            snapshot.append((name, cs.last_ok_at if cs is not None else None))

    total = len(snapshot)
    up = 0
    silent: list[dict[str, Any]] = []
    for name, last_ok in snapshot:
        if last_ok is None:
            silent.append({"name": name, "silent_s": None})
            continue
        try:
            age = now - last_ok.timestamp()
        except Exception:
            silent.append({"name": name, "silent_s": None})
            continue
        if age <= _SILENT_THRESHOLD_S:
            up += 1
        else:
            silent.append({"name": name, "silent_s": round(age, 1)})
    # Sort silent list so the UI shows the worst offender at the top:
    # never-reported servers first (None), then longest-silent descending.
    # Two-tuple key: ``(0, 0)`` for None places them before any (1, -age).
    silent.sort(key=lambda r: (0, 0) if r["silent_s"] is None else (1, -r["silent_s"]))
    return {"total": total, "up": up, "silent": silent}


def get_in_flight() -> list[dict[str, str]]:
    """Servers with a currently-pending check. Drives the pulse panel's
    "IN FLIGHT" section. Each entry is ``{name, check}``. A server with
    multiple checks pending shows up multiple times — that's the truth and
    the panel renders fine.

    Walks ``h.checks`` directly (not ``h.pending`` which is a write-only
    view that doesn't expose ``.items()``). Snapshot under the lock to
    avoid "dictionary changed size during iteration" — the supervisor
    inserts new CheckType keys via ``_ensure_check``.
    """
    out: list[dict[str, str]] = []
    with _server_health_lock:
        for name, h in server_health.items():
            try:
                for ct, cs in h.checks.items():
                    if cs.pending:
                        out.append({"name": name, "check": ct.value})
            except Exception:
                continue
    return out
