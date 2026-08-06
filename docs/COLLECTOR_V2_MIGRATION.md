# Collector v2 Migration Plan

**Status:** Phase 0 — scaffolding
**Started:** 2026-05-19
**Reason:** The current collector cannot scale past ~30 servers because of a
single shared 90 s cycle budget. We've patched three spike points
(startup, updates, soon logs) but the underlying coupling between servers
is structural. Building a per-check-deadline supervisor + worker pool +
aggregator architecture so Prism scales cleanly to 200-500 servers.

---

## The current architecture (what we have)

```
┌─ collector_loop (1 daemon thread in collector.py) ─────────┐
│  while True:                                                │
│    cycle_count += 1                                         │
│    decide global flags: collect_logs, check_updates, ...    │
│    for server in 30 servers:                                │
│      futures.append(pool.submit(collect_server, server))    │
│    wait up to 90s for ALL futures                           │
│    mark stragglers as offline                               │
│    run baseline checks                                      │
│    refresh latest_by_server cache                           │
│    sleep until poll_interval elapsed                        │
└──────────────────────────────────────────────────────────────┘
```

**Coupling that hurts us:**
- One shared wall-clock budget for the whole fleet
- Cycle is the unit of time for everything (cadences, alerts, baselines)
- No per-server health state — no backoff, no retry policy
- 5-worker thread pool is undersized for 30 servers × 30 s WU calls

---

## The target architecture (what we're building)

```
┌─ Supervisor (1 thread) ──────────────────────────────────┐
│  Every 5s tick:                                           │
│    for each server, for each check_type:                  │
│      if next_X_at < now → enqueue WorkItem               │
│    apply backoff for consecutive failures                 │
│    apply maintenance suppression                          │
│    heartbeat                                              │
└──────────┬───────────────────────────────────────────────┘
           │ work_queue (bounded)
           ▼
┌─ Worker pool (15-20 threads) ────────────────────────────┐
│  For each WorkItem:                                       │
│    open WinRM session                                     │
│    run check (metrics / logs / updates / hardware)        │
│    enforce per-CHECK deadline (not fleet-wide)            │
│    put Result on result_queue                             │
│    heartbeat per item                                     │
└──────────┬───────────────────────────────────────────────┘
           │ result_queue
           ▼
┌─ Aggregator (1 thread) ──────────────────────────────────┐
│  For each Result:                                         │
│    persist to DB                                          │
│    update latest_by_server cache                          │
│    detect status transitions                              │
│    feed baseline ring buffer                              │
│    dispatch alerts (with fatigue + maintenance gates)     │
│    notify supervisor of success/failure                   │
│    heartbeat                                              │
└──────────────────────────────────────────────────────────┘
           │
┌─ Watchdog (existing, supervises all 3 + restart_scheduler) ─┐
└──────────────────────────────────────────────────────────────┘
```

---

## What we KEEP (verbatim from collector.py — battle-tested)

| Item | Where it lives now | Where it goes |
|---|---|---|
| pypsrp WinRM bindings | `collect_server` | `collector_v2/checks.py` |
| The 6 `PS_*` PowerShell scripts | `collector.py` | `collector_v2/scripts.py` |
| `_is_offline_error` + `_OFFLINE_MARKERS` | `collect_server` inner scope | `collector_v2/checks.py` (module-level) |
| `compute_status` | `collector.py` | `collector_v2/checks.py` (called by checks) |
| `_effective_status` | `collector.py` | `collector_v2/aggregator.py` (called from result processing) |
| `_get_active_maintenance_window`, `_is_alert_suppressed_by_maintenance`, `_get_maintenance_thresholds` | `collector.py` | `collector_v2/aggregator.py` (gate dispatch) |
| `_cpu_gate_record`, `_cpu_gate_passes` | `collector.py` | `collector_v2/aggregator.py` (gate alerts) |
| Baseline-deviation N-of-M ring buffer (`_baseline_dev_history`) | `collector.py` | `collector_v2/aggregator.py` (state managed there) |
| `_check_tls_certificates`, `_run_health_checks`, `_collect_drift_snapshots`, `_collect_all_failed_logins`, `_check_scheduled_reports` | `collector.py` | `collector_v2/periodics.py` (new file, scheduled by supervisor at lower cadence) |
| `_LEVEL_NORMALIZE` map + `_normalize_level` | `database.py` | Stays |
| `_OFFLINE_MARKERS` (recently expanded with shutdown + socket-reset + German variants) | `collect_server` | `collector_v2/checks.py` |
| `server_update_info`, `server_auth_info`, `server_hardware_info`, `_accelerated_servers`, `_baseline_dev_history`, `_cpu_warn_history`, `latest_by_server`, `_state_lock` | `collector.py` (module globals) | Module globals in `collector_v2/state.py` — same interface, importable from API endpoints unchanged |
| Bulletproof catch-all per loop iteration | `collector_loop` | Replicated in supervisor + worker + aggregator loops |
| Watchdog pattern | `app.py` | Extended to supervise 3 threads instead of 1 |
| S1-6 straggler-as-offline semantics | `collector_loop` | Replaced by per-check timeout → "this check failed, mark this server as offline FOR THIS CYCLE" via result.error |
| S3-15 cache-correctness signal (`last_cycle_completed` only advances when cache refresh OK) | `collector_loop` | Aggregator advances `last_aggregator_tick` on every batch of results successfully persisted |
| Acceleration mechanism (`accelerate_server`, `_ACCELERATE_DURATION_S`, accelerated_until per server) | `collector.py` | Supervisor reads accelerated_until; same API for callers |
| All settings (`poll_interval_seconds`, `log_collection_interval_minutes`, `update_check_interval_minutes`) | `config_manager.py` defaults | Same settings, same defaults — supervisor reads them per tick |

## What we REMOVE / REPLACE

| Item | Why | Replacement |
|---|---|---|
| `collector_loop` while-True with shared 90 s budget | Source of every fleet-wide spike | Supervisor tick + worker queue |
| `ThreadPoolExecutor(max_workers=5)` as the main pool | Undersized + cycle-coupled | `WorkerPool` class with 15 workers + per-item deadlines |
| Cycle-modulo gating (`cycle_count % N == M`) | Couples cadence to a single time-axis | Per-server `next_X_at` timestamps in `ServerHealth` |
| `_server_due_for_updates` (today's WU shard) | Made obsolete by per-server scheduling | Per-server `next_updates_at` in `ServerHealth` |
| Executor rebuild on straggler timeout (`executor.shutdown` + new pool) | No longer needed — workers handle their own deadlines | Workers self-recover via WinRM exceptions |
| `last_cycle_completed` semantic ("a cycle finished") | No longer meaningful — there is no cycle | `last_aggregator_tick` (aggregator processed a result successfully) |
| Per-cycle event correlation (`cycle_events` list in `collector_loop`) | Tied to the cycle concept | Time-windowed correlation in aggregator (default: 60 s window) |
| `sync_now_event` semantics (wakes the cycle loop) | No loop to wake | Supervisor's tick is 5 s; force-sync just sets every server's `next_metrics_at = now` |
| `force_log_collection`, `force_update_check` events | Same as above | Supervisor sets all servers' `next_logs_at = now` / `next_updates_at = now` |

## What we ADD

| Item | Purpose |
|---|---|
| `collector_v2/types.py` | `ServerHealth`, `WorkItem`, `Result`, `CheckType` enum |
| `collector_v2/state.py` | Module-level shared state (replaces `collector.py` globals) |
| `collector_v2/scripts.py` | All PowerShell scripts, extracted untouched |
| `collector_v2/checks.py` | Per-check functions: `check_metrics`, `check_logs`, `check_updates`, `check_hardware` + `_is_offline_error` |
| `collector_v2/supervisor.py` | Scheduling thread + ServerHealth tracking + backoff |
| `collector_v2/workers.py` | Worker pool + per-item execution + deadline enforcement |
| `collector_v2/aggregator.py` | Result processing + DB writes + alert dispatch + status transitions |
| `collector_v2/periodics.py` | Low-cadence periodics (TLS, drift, health checks, scheduled reports) scheduled by supervisor |
| `collector_v2/__init__.py` | Public API: `start_collector_v2`, `accelerate_server`, etc. |
| `tests/test_collector_v2_types.py` | Data-model tests |
| `tests/test_collector_v2_supervisor.py` | Supervisor scheduling correctness, backoff math |
| `tests/test_collector_v2_workers.py` | Worker pool, deadline enforcement, error mapping |
| `tests/test_collector_v2_aggregator.py` | Result processing, transition events, alert gates |
| `tests/test_collector_v2_integration.py` | End-to-end with mocked WinRM |
| Feature flag `settings['collector_engine']` | `"legacy"` (default during rollout) or `"v2"` |
| `/api/system/collector-engine` endpoint | Reports which engine is running + health |

---

## Cross-cutting requirements (every component)

### Error handling
Every thread (supervisor / worker / aggregator) has a **bulletproof outer
try/except** that catches `Exception` (not `BaseException`), logs CRITICAL
with traceback, increments an internal error counter, and continues. The
thread MUST NEVER die from a runtime exception. This is the same pattern
the legacy `collector_loop` uses today and it has proven essential.

Per-component error counters are exposed in `/api/system/health` so the
operator can see when something is being repeatedly caught silently.

### Logging
- INFO: supervisor tick start, worker pool size changes, aggregator batch processed, server health transitions
- DEBUG: per-WorkItem dispatch, per-Result process, per-server schedule decisions
- WARNING: per-check failures, retries, backoff applied, queue near capacity
- ERROR: unexpected exceptions caught by inner handlers
- CRITICAL: bulletproof catch-all triggered (a thread almost died)

Every log line includes the component name (`prism.collector_v2.supervisor`, etc.) and the server name when applicable.

### Heartbeats
Each of the 3 threads (+ existing watchdog) maintains a heartbeat timestamp
at module level. The existing `_watchdog_loop` in `app.py` reads these and
audits transitions. The threshold for "stuck": > 5× the thread's tick
interval since last heartbeat.

| Thread | Tick interval | Stuck threshold |
|---|---|---|
| Supervisor | 5 s | 25 s |
| Worker (each) | per-WorkItem (variable) | 180 s (longest legitimate WU call) |
| Aggregator | per-Result (variable) | 30 s (no results for that long = something broke) |

### Notifications
On CRITICAL events (thread death, queue backlog > 80%, error rate spike):
- Audit log row (existing `_db.log_audit` mechanism)
- Console log at CRITICAL level (existing log sink, picked up by any log aggregator the operator has)
- Optionally: webhook fired if `settings['webhooks']['collector_alerts']` is configured

### Queue management
- Work queue: bounded at `max_workers * 4 = 60` items. When full, supervisor logs WARNING and DOES NOT enqueue more — server's `next_X_at` is rescheduled to now + 30 s. This is backpressure.
- Result queue: unbounded (results MUST flow through; dropping a result would lose data). If the aggregator is falling behind, the watchdog will catch it.

### Acceleration semantics (preserved)
The existing `accelerate_server(name, duration_s, reason)` API still works.
It sets `health.accelerated_until = now + duration_s`. The supervisor
checks this each tick: if accelerated, ALL pending check types fire on
the next tick (overriding their normal cadence).

### Feature-flag rollout
- `settings['collector_engine'] = "legacy"` (default): old `collector_loop` runs
- `settings['collector_engine'] = "v2"`: new three-thread system runs
- `settings['collector_engine'] = "both"`: BOTH run, results compared; v2 writes to a parallel DB column for validation. (Optional, for the side-by-side validation phase.)

The operator sets this in `settings.json` and restarts Prism. No downtime.

---

## Phase plan + dependencies

```
PHASE 0 — scaffolding (sequential, ~2 hours)
  ├─ Write this doc                               (you are here)
  ├─ Create collector_v2/ package
  ├─ Extract types into types.py
  └─ Extract shared state into state.py

PHASE 1 — extraction (sequential, ~2 hours)
  ├─ Move PS scripts to scripts.py
  └─ Move check functions to checks.py
     - Tests pass against extracted functions

PHASE 2 — components (3 PARALLEL agents, ~4 hours wall-clock)
  ├─ Agent A: supervisor.py + test_supervisor.py
  ├─ Agent B: workers.py + test_workers.py
  └─ Agent C: aggregator.py + test_aggregator.py
     All three depend ONLY on types.py + state.py + checks.py

PHASE 3 — wire-up (sequential, ~2 hours)
  ├─ Add feature flag to settings + config_manager
  ├─ Modify app.py to pick which engine to start
  ├─ Adapt watchdog to monitor 3 threads when v2 is on
  └─ Compatibility shims for the API endpoints

PHASE 4 — validation (parallel where possible, ~3 hours)
  ├─ Run integration tests
  ├─ Run side-by-side mode on real fleet for one full poll cycle
  └─ Compare metric/event/log inserts: v2 should match legacy ± fraction

PHASE 5 — audit + ship (~1 hour)
  ├─ Deep review of v2 (use /deep-review skill on the diff)
  ├─ Document rollback procedure
  └─ Flip default to v2; keep legacy code in repo for 1 sprint
```

## Done criteria

The migration is **done** when:

1. All Phase 4 tests pass on the real fleet for a 30-minute sustained run
2. Side-by-side mode shows v2 producing equivalent results to legacy
   (metric counts within 1%, no missed offlines, no missed alerts)
3. v2 handles cycle-1-like load (everything-due-at-once) without any
   server being marked offline due to fleet-wide budget pressure
4. Watchdog correctly detects + audits a deliberately stuck supervisor
   thread (test scenario)
5. Deep-review passes with no HIGH-severity findings
6. The dashboard refreshes correctly with v2 (no regression in the
   `prismRefresh` event flow)
