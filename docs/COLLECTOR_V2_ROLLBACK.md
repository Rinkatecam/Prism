# Collector v2 — Operator Rollback Procedure

The v2 collector is **opt-in** via `settings.collector_engine`. The default
is `"legacy"`. To switch engines, edit `config.json` and restart Prism.
There is no migration script and no DB-schema change to undo — both
engines write to the same tables.

## Switching engines

### Enable v2

```jsonc
{
  "settings": {
    "collector_engine": "v2",        // "legacy" | "v2" | "both"
    "collector_v2_num_workers": 15,  // optional; tune for fleet size
    // ... rest of settings
  }
}
```

Then restart Prism: `python app.py` (or the systemd / service unit).

Verify it started: `tail -20 prism.log | grep "Collector v2 started"`
should show:
```
Collector v2 started: 1 supervisor + 15 workers + 1 aggregator
+ 1 periodics (work_queue cap=60, result_queue=unbounded)
```

### Roll back to legacy

```jsonc
{
  "settings": {
    "collector_engine": "legacy",
    // ... rest of settings
  }
}
```

Restart. No data migration. Existing metrics in the DB are unchanged.

## What runs in each mode

| Component | `legacy` | `v2` | `both` |
|---|---|---|---|
| `collector_loop` (v1) | ✓ | – | ✓ |
| Supervisor + workers + aggregator (v2) | – | ✓ | ✓ |
| Periodics (TLS, drift, failed-logins, retention, scheduled reports) | inline in v1 | dedicated thread | both run (potential double-fire — only for validation) |
| Restart scheduler | ✓ | ✓ | ✓ |
| Workflow scheduler | ✓ | ✓ | ✓ |
| Watchdog | ✓ | ✓ (monitors v2 threads too) | ✓ |

**`"both"` mode is for side-by-side validation only.** It double-writes
to the DB and is NOT a long-term steady state.

## Health monitoring

`GET /api/system/health` now includes a `collector_v2` block when v2 is
active:

```jsonc
{
  "collector_engine": "v2",
  "collector_v2": {
    "started": true,
    "supervisor_last_tick_s_ago": 4.2,
    "aggregator_last_tick_s_ago": 1.8,
    "workers_last_activity_s_ago": 2.1,
    "tracked_servers": 30,
    "cached_metrics": 30,
    "supervisor": { /* per-component health */ },
    "workers": { /* per-component health */ },
    "aggregator": { /* per-component health */ },
    "periodics": {
      "last_heartbeat_s_ago": 12.4,
      "critical_errors_total": 0,
      "last_run": {
        "scheduled_reports": 8.2,
        "health_checks": 280.1,
        "failed_logins": 285.3,
        "tls_certs": 3550.0,
        "drift": 3585.7,
        "retention": 3590.0
      }
    }
  }
}
```

The watchdog also monitors v2's three threads and audits transitions
the same way it does for legacy (CRITICAL log + audit row per state
change). One known startup quirk: heartbeats begin at `0` so the watchdog
logs CRITICAL for `9999s stale` for the first 5 s before the supervisor's
first tick. Acceptable noise during boot; gone within one tick.

## Known differences (operator-visible)

These are NOT bugs — they're intentional design changes.

1. **`last_cycle_completed` doesn't move in pure-v2 mode.** v2 has no
   "cycle" — work is per-server-per-check. The dashboard's
   `prismRefresh` poller reads `last_aggregator_tick` instead, which
   advances on every Result processed (every few seconds in steady
   state). UI behavior unchanged.

2. **Force-sync is near-immediate but not instantaneous.** v1's
   `sync_now_event.set()` broke the cycle's sleep within ~milliseconds.
   v2's `sync_now()` sets every server's `next_metrics_at = now`, picked
   up on the supervisor's next 5 s tick. Worst-case latency: 5 s.

3. **Per-server scheduling staggers Windows-Update checks.** Instead of
   "all 30 servers do their WU check on cycle 4," each server gets its
   WU slot via `hash(server_name) % update_cycles`. The 30-min cadence
   per server is unchanged; the per-cycle load drops from 30 to ~1.

4. **Backoff for chronically-failing servers.** A server that fails 3+
   consecutive checks gets its next attempt pushed out
   exponentially (60 s → 2 min → 4 min, capped at 1 h). The dashboard
   shows the server as offline; the backoff just reduces WinRM noise
   against a known-bad host.

## If you see problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard tiles all "stale" | Aggregator stuck — check `/api/system/health` for v2 heartbeat age | If `aggregator_last_tick_s_ago > 30s`, restart Prism. The watchdog will also CRITICAL-log this. |
| Workers showing `total_critical_errors > 0` | A bug in a check function or pypsrp | Check `prism.log` for "Worker N bulletproof catch fired" stacktraces. File a bug. |
| Queue full warnings ("queue full — pushing X to +30s") | Worker pool too small for fleet | Increase `collector_v2_num_workers` (default 15). Each worker is one thread; safe up to ~50. |
| Anomaly events not firing in v2 (legacy fired them) | H4 audit fix not deployed | Verify `collector.py` has `dispatch_anomaly_events_v2` function (added 2026-05-19). |
| Periodic jobs not running (TLS / retention / scheduled reports) | H1 audit fix not deployed | Verify `collector_v2/periodics.py` exists. Look for "Periodics loop started" in the log. |

## Rollback safety

The v2 wire-up is **non-destructive**:

- No DB schema change
- No state migration
- Existing API endpoints work identically (compatibility shims in
  `collector.py` ensure `from collector import X` resolves to the same
  dict objects in both modes)
- The v1 collector code in `collector.py` is unchanged and continues to
  work when `collector_engine="legacy"`

You can switch between engines as often as you like with no data loss.

## Pending items (post-rollout follow-ups)

From the internal collector-v2 audit:

* **MEDIUM findings** (7 items): stuck-pending if `_execute_one` raises
  before emit; watchdog 9999-sentinel CRITICAL flap on startup;
  `_pending_acceleration` leak for typo'd names; unbounded result_queue
  with no overflow alarm; per-WorkItem ThreadPoolExecutor leaks a thread
  on stuck WinRM; `latest_by_server.clear()` race in "both" mode;
  watchdog `_Alive` proxy hides actually-dead threads. None blocking.
* **LOW findings** (9 items): polish — log levels, unused fields,
  missing GC on server removal, etc.

These are tracked but not blocking the rollout. Each one is small enough
to address in a follow-up patch.

## Recommended rollout sequence

1. **Today** (2026-05-19): keep `collector_engine = "legacy"` (default).
   v2 is built + tested but operator confidence comes from running it.
2. **This week**: switch to `"v2"` on one shift, observe `/api/system/health`,
   look for warnings in the log, verify the dashboard refreshes correctly.
3. **Next sprint**: if v2 is clean for a week, change the default in
   `config_manager.py:_DEFAULT_SETTINGS['collector_engine']` to `"v2"`.
4. **In one quarter**: assess whether to remove v1's `collector_loop` and
   the compatibility shims. The plan calls this the "v1 retirement"
   milestone; revisit when v2 has run smoothly for ~90 days.
