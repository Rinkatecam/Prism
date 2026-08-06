"""Shared mutable state — the dashboard's hot cache and per-server
ephemeral data.

This module owns the dicts that v1's collector_loop used to keep at
module scope. v2's aggregator + supervisor now write to these same
dicts, and every Flask route reads from them. Centralising the
ownership here breaks the routes' dependency on ``collector.py`` so
the legacy collector can be retired without breaking the API surface.

What lives here:

  * ``latest_by_server`` — the most-recent metric row per server.
    Powers the dashboard tiles, server-overview page, sidebar status
    badges, and the analytics "latest reading" UI elements. Refreshed
    by the aggregator on every successful METRICS Result.

  * ``server_update_info`` — pending Windows Updates per server, with
    the last successful check timestamp. Drives the Updates table in
    the server-detail page.

  * ``server_hardware_info`` — hardware inventory (CPU/RAM/disk
    geometry) per server. Refreshed on the HARDWARE check (every 1h).
    Read by the inventory views.

  * ``server_auth_info`` — the WinRM auth protocol negotiated per
    server (kerberos / ntlm). Diagnostic display only.

  * ``_state_lock`` — the RLock that guards reads/writes to all of
    the above. CPython guarantees ``dict.get()`` is atomic, but any
    iteration, ``.clear()``, ``.update()`` or comprehension MUST hold
    this lock. Same lock for all four dicts because they're touched
    together by the aggregator's per-Result handler.

  * ``last_cycle_completed`` — epoch float of the most recent
    successful end-of-cycle (v1) or per-Result aggregator tick (v2).
    Drives ``/api/collector-status`` and the dashboard auto-refresh
    poller. ``max(v1_ts, v2_ts)`` is computed at the API layer to
    handle "both" mode.

  * ``_accelerated_servers`` — per-server accelerated-polling expiry
    timestamps. Set by ``collector.accelerate_server()`` after admin
    actions (restart, install, cancel) — affected servers poll every
    cycle for the next 10 minutes instead of the normal cadence.

Module dependencies:
  * stdlib only (``threading``).
  * Nothing in this project imports FROM here at module load that
    this module ALSO imports from — guarantees no circular import.

Why this matters for v1 retirement:
  routes/api/*.py used to ``from collector import latest_by_server``.
  Post-R2 they import from ``state`` directly; ``collector.py`` keeps
  re-exports for the migration window so any straggler still works.
"""

from __future__ import annotations

import threading


# ─────────────────────────────────────────────────────────────────────
# Per-server caches (written by aggregator, read by routes)
# ─────────────────────────────────────────────────────────────────────

#: Tracks the negotiated auth protocol per server (e.g. "kerberos" or "ntlm").
#: Diagnostic-only; never used for decision logic.
server_auth_info: dict[str, str] = {}

#: Pending Windows updates per server. Shape:
#:   { server_name: {"updates": [...], "needs_reboot": bool,
#:                   "checked_at": iso_string, "error": str|None} }
server_update_info: dict[str, dict] = {}

#: Hardware specs per server (cached; refreshed on the HARDWARE check).
#: Shape: { server_name: {"cpu_model": str, "ram_gb": float,
#:                        "disks": [{"letter": "C", "size_gb": float}, ...]} }
server_hardware_info: dict[str, dict] = {}

#: Latest metric row per server. Refreshed by the aggregator on every
#: successful METRICS Result. Shape mirrors a DB row from the
#: ``metrics`` table (cpu_percent, ram_percent, disk_c_percent, etc).
latest_by_server: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────
# Locks + scalar state
# ─────────────────────────────────────────────────────────────────────

#: Guards all four dicts above. RLock so the aggregator can call into
#: helpers that re-acquire. Pattern: hold around any non-atomic op
#: (iteration, .clear(), .update(), comprehension); skip for simple
#: O(1) ``dict.get()`` which CPython guarantees atomic.
_state_lock = threading.RLock()

#: Epoch float of the most recent successful collection. Drives the
#: ``/api/collector-status`` endpoint and the dashboard auto-refresh
#: poller. Under v1 this is end-of-cycle; under v2 it's the last
#: aggregator tick. The API layer takes ``max(v1_ts, v2_ts)`` so the
#: dashboard refreshes correctly in either engine.
last_cycle_completed: float = 0.0


# ─────────────────────────────────────────────────────────────────────
# Per-server accelerated polling
# ─────────────────────────────────────────────────────────────────────

#: Per-server accelerated polling expiry timestamps. Set by
#: ``collector.accelerate_server()`` (which also forwards to
#: ``collector_v2.accelerate_server`` when v2 is active). All sub-checks
#: (updates, logs, hardware) run every cycle while the entry is live.
#:
#: Shape: { server_name: expiry_epoch_float }
_accelerated_servers: dict[str, float] = {}

#: Default acceleration window. Bumped from 5 min to 10 min after a
#: production incident where cumulative updates + reboot took longer
#: than 5 min and the dashboard went stale while the install was still
#: running.
_ACCELERATE_DURATION_S: int = 600
