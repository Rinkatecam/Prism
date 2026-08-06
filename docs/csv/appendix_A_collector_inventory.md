# Appendix A — `collector_v2/` Module Inventory

*Source: automated inventory pass, 2026-05-22. Cited from `docs/csv/04_DS.md` (Design Specification).*

## Summary

| File | Lines | Purpose | Threading model |
|------|-------|---------|------|
| `__init__.py` | 229 | Orchestrator: starts/stops the three-thread pipeline | Synchronous startup; delegates to threads |
| `state.py` | 328 | Shared state — server health dict, latest-metric cache, pulse deque, heartbeats | Multi-reader; aggregator is the writer |
| `types.py` | 367 | Data model — `CheckType` enum, `WorkItem`, `Result`, `ServerHealth`, `CheckState` | Pure dataclasses (no mutable globals) |
| `supervisor.py` | 645 | 5 s tick scheduler — chooses what's due, enqueues `WorkItem`s | One daemon thread |
| `workers.py` | 590 | Worker pool — executes PS scripts under per-check deadlines | N (=15) daemon threads |
| `aggregator.py` | 1469 | Drains result queue, writes DB, computes transitions, dispatches alerts, records pulse | One daemon thread (sole writer to module-level dicts) |
| `checks.py` | 288 | Per-check functions (metrics / logs / updates / hardware) — runs PS via the pool | Library (called from workers) |
| `periodics.py` | 428 | Periodic fleet-wide jobs (TLS, drift, retention, auto-restart, reboot janitor, …) | One daemon thread |
| `scripts.py` | 372 | PowerShell payload constants (single source of truth) | Pure constants |

## Public API of `collector_v2.__init__`

- `start_collector_v2(get_servers, get_settings, db, num_workers=15)`
- `accelerate_server(name, duration_s, reason)`
- `sync_now()`, `sync_logs_now()`, `sync_updates_now()`
- `get_health_snapshot()`
- `stop_collector_v2()` *(not called in production; provided for tests)*

## Critical shared mutable state (referenced by FS / DS / Risk docs)

| State | Where | Owner(s) | Reader(s) | Lock |
|---|---|---|---|---|
| `server_health: dict[str, ServerHealth]` | `state.py` | supervisor (writes); aggregator updates via `mark_check_completed` | API endpoints, watchdog | `state._server_health_lock` |
| `latest_by_server: dict[str, dict]` | `state.py` | aggregator | API + dashboard | `state._state_lock` |
| `_pulse_buffer: deque` (maxlen 1000) | `state.py` | aggregator (appends) | API (snapshots) | `state._pulse_lock` |
| `_previous_status: dict` | `aggregator.py` | aggregator only | — | none needed (single thread) |
| `_baseline_dev_history: dict[str, dict[str, deque]]` | `aggregator.py` | aggregator only | — | none |
| `_recent_events: deque` (maxlen 200) | `aggregator.py` | aggregator only | — | none |
| `_work_queue: Queue` | `__init__.py` | supervisor puts; workers get | — | (`Queue` is thread-safe) |
| `_result_queue: Queue` | `__init__.py` | workers put; aggregator gets | — | (`Queue` is thread-safe) |
| `_pending_acceleration: dict` | `supervisor.py` | external `accelerate_server` (writer); supervisor (drains) | — | `state._server_health_lock` on apply |
| Worker stats (`_active_count`, `_total_processed`, `_total_timeouts`, `_total_offline`, `_total_critical_errors`) | `workers.py` | workers (increment) | watchdog (reads) | `workers._stats_lock` |
| Aggregator stats (`_total_processed`, `_total_alerts_dispatched`, `_total_critical_errors`) | `aggregator.py` | aggregator | watchdog | `aggregator._stats_lock` |
| Periodics state (`_last_run`, `_last_heartbeat`) | `periodics.py` | periodics thread | watchdog | `periodics._lock` |
| Heartbeat timestamps (`last_supervisor_tick`, `last_aggregator_tick`, `last_worker_activity_at`) | `state.py` | each thread (own field only) | watchdog | none (field-level int assignment is atomic in CPython) |

## Periodic jobs registered by `periodics._build_jobs`

| Job | Interval | Handler | Notes |
|---|---|---|---|
| `tls_certs` | 1 h (configurable) | `tls_monitor._check_tls_certificates` | Per-cert WinRM probe |
| `health_checks` | 5 min | `healthchecks._run_health_checks` | TCP/HTTP/ICMP probes per `health_check_config` |
| `failed_logins` | 5 min | `failed_logins._collect_all_failed_logins` | Gated on `security_alerts.failed_login_tracking` |
| `drift` | 1 h (configurable) | `drift._collect_drift_snapshots` | Gated on `drift_detection.enabled` |
| `scheduled_reports` | 60 s | `scheduled_reports._check_scheduled_reports` | Internal cadence |
| `retention` | 1 h | `db.cleanup_old_data` + suppression/snooze cleanup | 30 d default retention |
| `auto_restart_scanner` | 60 s | safety net: fires restart for stuck `restart_required` + `restart_after=True` rows | |
| `reboot_state_janitor` | 60 s | GCs stuck `rebooting` AND `stabilising` install_state rows after 20 min — **extended in this audit's scope** | |
| `ldap_probe` | 5 min | `auth.ldap_health_probe` | Gated on `auth.enabled` |

## Per-check defaults (from `types.DEFAULT_INTERVALS_S` + `DEFAULT_DEADLINES_S`)

| Check | Interval | Deadline | Description |
|---|---|---|---|
| METRICS | 60 s | 30 s | CPU / RAM / disk via WMI |
| LOGS | 5 min | 60 s | Event Log scrape |
| UPDATES | 30 min | 120 s | Windows Update COM query |
| HARDWARE | 60 min | 60 s | CIM hardware inventory |

## Constants
- `_TICK_INTERVAL_S = 5.0`
- `_ACCELERATE_DURATION_S = 600` (10 min default)
- `_ACCELERATE_MAX_DURATION_S = 1200` (20 min ceiling — added during this audit's scope)
- `_QUEUE_FULL_RESCHEDULE_S = 30`

---
*End of appendix.*
