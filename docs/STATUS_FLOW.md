# Server Status Flow — Reference Documentation

This is the complete, authoritative explanation of how a Prism server ends up
with its `status` (healthy / warning / critical / offline). If there's ever a
question like "why is this server showing critical when nothing is critical?"
or "what's the right place to add a new detector?", the answer is here.

Last audited: 2026-04-15. Pipeline is clean (see "No-conflict guarantee" below).

---

## 1. The four states

Every server has **exactly one status** at any given moment. It is computed
by `_effective_status()` in `collector.py` and stored in the `metrics.status`
column of SQLite on every collection cycle.

| State | Plain rule |
|---|---|
| **HEALTHY** | Every collected metric (cpu/ram/disk_c/disk_d) is below its own per-server warning threshold, AND no smart detector has flagged anything that slipped through the cap. |
| **WARNING** | At least one metric is in the `[warning_threshold, critical_threshold)` band, OR a smart detector (baseline/anomaly) flagged an unusual value that the severity cap allows to elevate a healthy server by one level. "Worth watching, not an emergency." |
| **CRITICAL** | **At least one raw metric is at or above its own critical threshold.** No exceptions. Smart detectors alone cannot promote a healthy server to critical — the per-server critical threshold is the ground truth. |
| **OFFLINE** | Metrics could not be collected on the last attempt for this server (WinRM error, timeout, host unreachable, auth failure). Offline is never demoted by any gate. |

Per-server thresholds live on each `ServerConfig` (stored in `config.json`
under each server's `thresholds` key). Defaults come from `models.py`
`DEFAULT_THRESHOLDS` per server type (file_server, app_server, domain_controller, etc).

---

## 2. The decision tree — 7 phases in `_effective_status`

Every phase is clearly labeled with a `═══ Phase N ═══` comment in
`collector.py`. Read top to bottom — first match wins unless noted.

```
Phase 0  OFFLINE short-circuit
         → metrics is None OR threshold_status == "offline" → return "offline"
         → offline is never contested by any later phase.

Phase 1  Threshold baseline (ground truth)
         → threshold_status is already computed by compute_status() using
           the per-server cpu/ram/disk_warning/critical values.
         → This is the authoritative "what does warning and critical
           actually mean on this server" answer.
         → Maintenance windows override these thresholds BEFORE this phase
           runs — see _get_maintenance_thresholds.

Phase 2  Smart detectors (supplementary signal)
         → If baseline_detection.enabled: run baseline_engine.check_deviation,
           collect the highest severity into extra_severity.
         → If anomaly_detection.enabled: run analytics.detect_anomalies,
           collect the highest severity into extra_severity.
             · When baseline is on, anomaly only contributes LOW-SIDE
               (crash detection) — baseline owns high-side.

Phase 3  Severity cap (anti-nonsense)
         → Smart detectors can elevate the merged status by AT MOST ONE LEVEL
           above what the threshold check says:
               threshold=healthy  + smart=critical  → capped to warning
               threshold=healthy  + smart=warning   → warning
               threshold=warning  + smart=critical  → critical (1 level up, OK)
               threshold=critical + smart=any       → critical
         → Prevents: "CPU at 47% is CRITICAL because baseline thinks normal
           is 9%." Smart detectors add context but cannot override the ground
           truth defined by the per-server threshold.

Phase 4  Max severity merge
         → merged = max(threshold_status, capped_extra_severity)
         → Uses _SEVERITY_RANK to order: healthy < warning < critical < offline.

Phase 5  CPU N-of-M gate (anti-noise, CPU only)
         → Record current cycle's CPU state in _cpu_warn_history ring buffer.
         → If merged == "warning" AND CPU is the ONLY reason for it AND the
           ring doesn't have at least N-of-M cycles in warning state:
               demote warning → healthy
         → Critical CPU (>= cpu_critical) bypasses this gate entirely.
         → Configurable via anomaly_detection.cpu_warning_consecutive_cycles
           (default 3) and cpu_warning_window_cycles (default 5).

Phase 6  Raw critical override (hard rule)
         → FINAL check: if merged == "critical", verify at least one raw
           metric is actually ≥ its own critical threshold. If not,
           demote to "warning".
         → This is the hard-coded sanity check that enforces the rule
           "critical must mean a real metric at critical level, period."
         → No smart detector can circumvent this.

Return   merged status (one of: healthy | warning | critical | offline)
```

---

## 3. The complete data flow diagram

```
                     ┌──────────────────────────────────────────┐
                     │          COLLECTION CYCLE                │
                     │            (collector.py)                │
                     └──────────────────┬───────────────────────┘
                                        │
                    WinRM fetch  →  metrics dict (cpu/ram/disk)
                                        │
                                        ▼
                     ┌──────────────────────────────────────────┐
                     │ _threshold_status = compute_status(...)  │
                     │    (per-server thresholds; maintenance   │
                     │     window overrides applied here)       │
                     └──────────────────┬───────────────────────┘
                                        │
                                        ▼
    ┌────────────────────────────────────────────────────────────────────┐
    │  _effective_status(db, server, metrics, threshold_status, settings)│
    │                                                                    │
    │   0. offline short-circuit                                         │
    │   1. threshold baseline     ← compute_status result                │
    │   2. smart_boost            ← baseline_engine.check_deviation      │
    │                             ← analytics.detect_anomalies           │
    │   3. severity cap (smart ≤ +1 level above threshold)               │
    │   4. max(threshold, capped_smart)                                  │
    │   5. cpu n-of-m gate (demote warning → healthy if not sustained)   │
    │   6. raw critical override (critical needs real metric at thresh)  │
    └────────────────────────────────────┬───────────────────────────────┘
                                         │  one string
                                         ▼
                     ┌──────────────────────────────────────────┐
                     │  db.insert_metric(status=status, ...)    │
                     │       THE ONLY WRITER OF status          │
                     └──────────────────┬───────────────────────┘
                                        │
                                        ▼
                             ┌──────────────────────┐
                             │   metrics.status     │   ← single source of truth
                             │   (SQLite column)    │
                             └──────────┬───────────┘
                                        │ reads only
                ┌───────────────────────┼─────────────────────────┐
                │                       │                         │
                ▼                       ▼                         ▼
    get_status_summary()      get_latest_all()           /api/servers/<name>
           │                        │                           │
           ▼                        ▼                           ▼
    partials/                partials/                 templates/
    status_overview.html     server_card.html          server_detail.html
    (dashboard top boxes)    (server grid badges)      renderStatusBadge()

    SIDE STREAMS — never touch metrics.status:
      events table          ← insert_event (threshold/baseline/anomaly/rate)
      security_status       ← security_checker.collect_security_status
      tls_certificates      ← tls_checker.check_certificate
      incidents             ← correlate_events
      workflow_executions   ← workflow_engine.execute_workflow
      restart_log           ← restart_scheduler
      health_check_results  ← health_checker.run_health_checks
```

---

## 4. What is and is NOT allowed to modify status

### ✅ ONLY these things can write `metrics.status`
1. **`db.insert_metric(status=...)`** called from the main collector loop in
   `collector.py`. That call passes the result of `_effective_status()`.
   This is the only production writer.
2. `seed_demo.py` writes hardcoded statuses for dev-only seeded data.
   Not part of the production path.

### ❌ NEVER do any of these
- Don't call `db.insert_metric(status="critical")` from anywhere other than
  the main collector loop.
- Don't add an `UPDATE metrics SET status = ...` SQL anywhere. (Grep confirms
  zero exist as of the audit.)
- Don't compute status client-side in JS and override the stored value.
  `metricColor()` in `server_detail.html` is fine — it only colors individual
  metric cells, never the overall badge.
- Don't add a new detector that writes to `metrics.status` directly. Add it
  to `_effective_status` as a new phase, so it goes through the severity cap
  and raw-critical override.
- Don't add a new status field (e.g. "degraded") without updating every
  reader: `partials/status_overview.html`, `partials/server_card.html`,
  `renderStatusBadge()`, topology SVG coloring, dashboard filters.
- **Don't hardcode per-metric color thresholds in templates** (e.g.
  `if ram >= 70 then amber`). Always use per-server thresholds passed in
  from the view: `{% set _ram_w = issue.thresholds.get('ram_warning', 80) %}`.
  This bit us once — `critical_issues.html` had hardcoded 70/85 values that
  contradicted the stored per-server thresholds and gave the user a
  "warning for RAM on a healthy server" bug.

### ⚠️ Adjacent namespaces that LOOK like status but are disjoint
- `security_status` table — checked by `security_checker.py`, has its own
  schema with defender/firewall/bitlocker columns. Does not affect
  `metrics.status`.
- `tls_certificates.status` — per-cert state (healthy/warning/critical/expired).
  Does not affect server `metrics.status`.
- `incidents.status` — lifecycle of a correlated incident (open/investigating/
  resolved). Does not affect `metrics.status`.
- `workflow_executions.status` / `runbook_executions.status` — execution
  lifecycle of automated jobs. Unrelated.
- `restart_log.status` — restart job outcome. Unrelated.

---

## 5. Events table vs metrics.status

They are **two independent systems** driven by different writers:

| Thing | Writer | Purpose |
|---|---|---|
| `metrics.status` | `_effective_status` → `insert_metric` | Current badge shown to user |
| `events` | `insert_event` from many sources | Historical log of alerts |

An event row can exist with severity "critical" for a server that is currently
"warning" in metrics.status — that's OK. The event is a point-in-time record
of something that happened. The status is always "right now".

### Events sources (each calls `insert_event` independently)
- Status-change events (collector loop, when status transitions)
- baseline_deviation (baseline event loop in collector)
- anomaly (analytics anomaly loop in collector)
- rate_anomaly (analytics rate loop in collector, OFF by default)
- failed_logins / account_lockout (collector `_collect_all_failed_logins`)
- security_status (security_checker)
- tls_certificate (collector `_check_tls_certificates`)
- health_check (collector `_run_health_checks`)
- config_drift (collector `_collect_drift_snapshots`)
- runbook_failed (runbook_engine)

All event sources share the same severity allowlist enforced by
`email_alerts.should_send_email`: **critical | warning | offline | resolved | info**.
Using any other severity string silently drops notifications.

---

## 6. No-conflict guarantee (audit 2026-04-15)

The audit found:

- Grep for `UPDATE metrics SET status`: **0 hits**
- Grep for `insert_metric(.*status=`: **1 hit** (the collector loop)
- `compute_status` callers: **1** (`_effective_status` only)
- `_get_worst_metric` callers: **2** (collector status-change event emission
  for message formatting only, never for status computation)
- Every API endpoint and template that touches status reads `metrics.status`
  back — none recompute.

**Pipeline is clean.**

### Minor maintenance hazards (non-blocking, document-only)
- The baseline event-firing loop in `collector.py` duplicates the severity
  cap logic (to cap event severities for logged events). If the cap rules
  in `_effective_status` change, the baseline event loop must be updated in
  parallel. Extracting a shared `_cap_severity()` helper would eliminate
  this drift risk. Not a bug today.
- `seed_demo.py` L61 writes hardcoded statuses bypassing the pipeline. Only
  runs on dev databases. Not a production concern.

---

## 7. Quick "why is this server showing X" debugging recipe

1. **Check the raw metric values** vs the per-server thresholds (visible as
   chips in the server overview header):
   ```
   cpu: 63 / warn 75 / crit 90     → healthy for CPU
   ram: 76 / warn 92 / crit 96     → healthy for RAM
   disk_c: 80 / warn 70 / crit 85  → warning for disk_c
   ```
2. **Result:** Phase 1 returns `warning` (disk_c in warning band). No smart
   detector can override this. No raw metric is ≥ critical → not critical.
3. **Expected status:** warning ✓

If the badge disagrees with this recipe, one of:
- `_cpu_warn_history` has stale state (restart the collector)
- Baseline data hasn't been refreshed after a config change (hit
  "Recalculate Now" in Monitoring → Baseline Detection)
- Stale event rows in the UI (those are historical, not current status)
- An unexpected new writer to `metrics.status` was added — run the audit
  grep again (section 6) to find it

---

## 8. Key file locations (as of this audit)

| Thing | File | Function / area |
|---|---|---|
| Module docstring with status contract | `collector.py` | top of file |
| The decision tree | `collector.py` | `_effective_status()` |
| Threshold math | `collector.py` | `compute_status()` + `_get_worst_metric()` |
| Maintenance overrides | `collector.py` | `_get_active_maintenance_window()` |
| CPU N-of-M ring buffer | `collector.py` | `_cpu_warn_history`, `_cpu_gate_record`, `_cpu_gate_passes` |
| Baseline math | `baseline_engine.py` | `check_deviation()` |
| Anomaly math | `analytics.py` | `detect_anomalies()`, `detect_rate_anomalies()` |
| Stored column | `database.py` | `metrics.status` column in SCHEMA_SQL |
| Only writer | `database.py` | `insert_metric()` |
| Dashboard summary | `database.py` | `get_status_summary()` |
| Topology colors | `routes/api.py` | topology SVG endpoint |
| Topology interactive | `topology.py` | `build_topology_data()` + `static/js/topology.js` |
| Per-server API | `routes/api.py` | `/api/servers/<name>` |
| Server detail render | `templates/server_detail.html` | `renderStatusBadge()` + header badge |
| Server card render | `templates/partials/server_card.html` | stored `server.status` |
| Windows Update check | `collector.py` | `PS_CHECK_UPDATES` (ServerSelection=2) |
| Windows Update install | `routes/api.py` | `_wu_ps_script_body()` + scheduled task |
| Update dashboard widget | `routes/views.py` | `partial_updates_overview()` |
| Update status polling | `routes/api.py` | `/api/servers/<name>/update-status` |
| Accelerated polling | `collector.py` | `_accelerated_servers` + `accelerate_server()` |
| CSRF token refresh | `routes/api.py` + `base.html` | `/api/csrf-token` + 30-min JS refresh |
| Smooth DOM refresh | `base.html` | Idiomorph morph extension + `morphHTML()` |

---

## 9. Collector sub-check cadence

Each sub-check runs on a different cycle modulo, configurable from Settings:

| Sub-check | Default interval | Setting key | Cycle gate |
|---|---|---|---|
| Metrics (CPU/RAM/Disk) | Every cycle (~60s) | `poll_interval_seconds` | Always |
| Windows event logs | 5 min | `log_collection_interval_minutes` | `cycle % _log_cycles == 1` |
| Windows Update check | 30 min | `update_check_interval_minutes` | `cycle % _upd_cycles == 1` |
| Hardware specs | ~1 hour | — | `cycle % 60 == 1` |
| Security status | ~30 min | — | `cycle % 30 == 7` |
| TLS certificates | Configurable | `tls_check_interval_minutes` | `cycle % _tls_interval == 5` |
| Config drift | Configurable | `drift_interval_minutes` | `cycle % drift_interval == 15` |

Each gate uses a different modulo remainder (1, 5, 7, 15) so heavy checks don't
pile onto the same cycle.

### Per-server accelerated polling

When an admin triggers an action (restart, install updates, cancel updates),
`accelerate_server(name)` puts that server into fast mode for 5 minutes.
ALL sub-checks run every cycle for that server regardless of the global gates.
This ensures the UI reflects the action's result within ~60s instead of waiting
30 min for the next WU check.

Entries live in `_accelerated_servers: dict[str, float]` (name → expiry epoch)
and are cleaned up at the start of each cycle.

---

## 10. Windows Update flow

### Check (collector)

`PS_CHECK_UPDATES` runs via WinRM with `ServerSelection = 2` (public Microsoft
Update catalog) to match what the Windows Settings UI shows. WSUS-managed servers
with the default source (0) often show fewer updates because WSUS only surfaces
approved ones.

Results stored in `server_update_info[name]` (in-memory dict). The dashboard
widget (`/partials/updates-overview`) and server detail page (`/api/servers/
<name>/updates`) both read from this dict.

### Install (scheduled task)

The WU COM API refuses `Download()` / `Install()` from remote WinRM sessions
(`0x80070005 E_ACCESSDENIED`). Prism works around this:

1. Script encoded as UTF-16LE base64 → `-EncodedCommand` arg (bypasses AppLocker
   blocking `.ps1` files in `C:\ProgramData\`)
2. Scheduled task runs as `NT AUTHORITY\SYSTEM` at `Highest` privilege
3. Task writes progress to `C:\ProgramData\Prism\update-status.json`
4. UI polls `/api/servers/<name>/update-status` every 5s
5. Auto-retry on `WU_E_UH_NEEDANOTHERDOWNLOAD` (`0x8024200D`) — re-downloads
   then retries install once
6. Per-update `ResultCode` + `HResult` inspection — partial failures reported
   individually, never masked by aggregate success

### Display chain

```
PS_CHECK_UPDATES (ServerSelection=2)
     │
     ▼
server_update_info[name]     ← collector writes every 30 min
     │                          (or every ~60s during accelerated polling)
     ├──► /api/servers/<name>/updates
     │         │
     │         ├──► server_detail.html renderUpdates()
     │         │    (shows: pending count, error, restart needed)
     │         │
     │         └──► /partials/updates-overview (dashboard widget)
     │              (shows: pending + active installs + errors)
     │
_update_install_state[name]  ← install endpoint writes
     │
     └──► merged into both endpoints above when install is active
```

### Why ServerSelection=2 everywhere

- `collector.py PS_CHECK_UPDATES`: ServerSelection=2
- `routes/api.py _wu_ps_script_body()`: ServerSelection=2
- `routes/api.py install_server_updates_direct()`: ServerSelection=2

All three must match. If they use different sources, the collector shows updates
that the install task can't find ("No updates available" even though the dashboard
says 1 pending). This was a bug fixed in commit `17bb68b`.

---

## 11. Balanced Windows Event Log collection

`PS_COLLECT_LOGS` fetches 200 events per log (System / Application / Security),
classifies each by severity, and picks a balanced sample:

- **Up to 15** Critical + Error events (or Audit Failure for Security)
- **Up to 10** Warning events
- **Fill remaining slots** with Information events (up to 30 total per log)

Why not use `-FilterHashtable @{Level=1,2}`? Because `Get-WinEvent` throws a
**terminating error** when no events match the filter, even with
`-ErrorAction SilentlyContinue`. The balanced approach fetches a wide batch
and sorts in PowerShell, which is robust regardless of whether errors exist.

The **Security log** doesn't use the `Level` field — all events are Level 0
(LogAlways). Audit failures are flagged via the `Keywords` bitmask
(`0x10000000000000`), not the Level enum.

---

## 12. Smooth UI refresh (Idiomorph)

Dashboard and server detail pages use **Idiomorph** (HTMX DOM-diffing extension)
instead of plain `innerHTML` replacement. This means:

- Numbers update in-place (no flash)
- Icons stay stable (no re-render)
- CSS transitions animate value changes (250ms)
- Server grid doesn't jump on refresh

Every HTMX partial uses `hx-swap="morph:innerHTML"`. Server detail JS sections
use `morphHTML(container, html)` which calls `Idiomorph.morph()` when available.

The `prismRefresh` event (fired globally from `base.html` every ~5s when a
new collector cycle completes) drives all page refreshes. Moved from
dashboard-only to global so every page reacts to new data.

---

## 13. If you ever need to add a new detector

1. Implement the detector as a pure function returning `(severity, details)`
   per metric. Put it in `baseline_engine.py`, `analytics.py`, or a new
   module — NOT in `collector.py`.
2. Call it from `_effective_status` inside Phase 2. Feed its severity into
   `extra_severity` via `_max_severity()`.
3. The Phase 3 severity cap and Phase 6 raw-critical override will apply
   automatically. No other changes needed.
4. If the detector should also fire independent log events, add an event
   loop in `collector.py` AFTER `_effective_status`, using `insert_event`
   with a severity from the allowlist (critical/warning/info/resolved/offline).
   Consult `_cpu_gate_passes()` if the detector is for CPU.
5. If you want UI toggles for the new detector, add a setting to
   `config_manager.py _DEFAULT_SETTINGS` and expose it in
   `templates/monitoring.html` Detection Mode card.

**Do not** add a new writer to `metrics.status`. The invariant is: one writer,
one decision tree, every reader sees the same answer.
