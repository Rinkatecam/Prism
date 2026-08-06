# Collector v1 retirement — done

> **Status (2026-05-20, fully complete):** v1 has been retired. R1
> through R5 are all complete; ``collector.py`` is deleted and the
> ``collector_engine`` and ``anomaly_detection.check_every_cycles``
> settings are gone. This document is kept as the historical record
> of the migration.

## Final state (post-retirement)

* ``collector.py`` — **deleted**. All callers migrated to canonical
  homes (``state``, ``detection``, ``maintenance``, ``tls_monitor``,
  ``healthchecks``, ``drift``, ``failed_logins``, ``scheduled_reports``).
* ``app.py`` starts v2 unconditionally (with a pytest guard to keep
  test runs from spawning 15 background threads each). The watchdog
  monitors v2's three components + the restart/workflow schedulers.
* ``config_manager.py`` no longer carries ``collector_engine`` or
  ``anomaly_detection.check_every_cycles``. Stale values in old
  settings.json files are ignored; ``app.py`` logs a one-line warning
  for the engine key.
* ``templates/settings.html`` shows only the worker-pool-size knob in
  the Collector section.
* ``collector_v2/scripts.py`` is the sole home for the 5 PowerShell
  scripts.
* Routes call ``collector_v2.sync_now()`` / ``sync_logs_now()`` /
  ``sync_updates_now()`` directly. The ``_V2ForwardingEvent`` shim is
  gone with ``collector.py``.
* 184 tests pass (was 187 pre-retirement; -2 from the deleted parity
  test, -1 from the deleted "legacy engine returns null" test).

The R1–R5 plan below remains as historical context for what
specifically was moved and why.

## TL;DR

v1 isn't just `collector.py`'s `collector_loop()` — it has become the
**home for ~14 helper functions and ~9 shared-state objects** that v2
and the Flask routes both depend on. Deleting `collector.py` today
would break:

* every API route (they import shared state dicts)
* `collector_v2/periodics.py` (calls into v1 helpers)
* `collector_v2/aggregator.py` (calls into v1 helpers)
* parts of `analytics.py` and `routes/views.py`

The work isn't hard — it's mostly mechanical extraction of stateless
helpers into purpose-built modules. The reason it hasn't happened is
that during the v2 migration we deliberately left shared code in
collector.py so import sites kept working unchanged ("compatibility
shim" mode). Retirement = un-doing that shim mode.

**Estimated effort:** 2-3 focused days of work, in 4 phases.

**Recommended timing:** *after* v2 has run as default for ~1 week with
zero CRITICAL audit findings.

---

## What still depends on v1

### Category A — Helper functions v2 calls into v1 for

These are pure-logic helpers that v2 modules import. They could live
anywhere — they're in `collector.py` only because that's where the v1
loop happened to define them. **All are stateless or near-stateless.**

| Function | Used by | Lives in |
|---|---|---|
| `_check_tls_certificates` | `collector_v2/periodics.py` | `collector.py` |
| `_run_health_checks` | `collector_v2/periodics.py` | `collector.py` |
| `_collect_all_failed_logins` | `collector_v2/periodics.py` | `collector.py` |
| `_collect_drift_snapshots` | `collector_v2/periodics.py` | `collector.py` |
| `_check_scheduled_reports` | `collector_v2/periodics.py` | `collector.py` |
| `_generate_scheduled_report` | called by the above | `collector.py` |
| `_is_alert_suppressed_by_maintenance` | `collector_v2/periodics.py` + `aggregator.py` | `collector.py` |
| `_get_active_maintenance_window` | `collector_v2/supervisor.py` + `routes/views.py` | `collector.py` |
| `_get_maintenance_thresholds` | indirectly via `compute_status` | `collector.py` |
| `_active_level_detector` | `collector_v2/aggregator.py` + `analytics.py` | `collector.py` |
| `_get_worst_metric` | `collector_v2/aggregator.py` | `collector.py` |
| `compute_status` | `collector_v2/aggregator.py` | `collector.py` |
| `_effective_status` | `collector_v2/aggregator.py` | `collector.py` |
| `dispatch_anomaly_events_v2` | `collector_v2/aggregator.py` | `collector.py` |
| `_cpu_gate_record`, `_cpu_gate_passes` | `analytics.py` + `aggregator.py` (via `_cpu_warn_history`) | `collector.py` |

**Blocker rating:** all of these block retirement, but extraction is
trivial — they're pure functions with no upward dependency back into
`collector_loop`.

### Category B — Shared mutable state

These are the dicts that v1 owned and v2 + every API route reads from.
`collector_v2/state.py` deliberately re-uses the **same dict objects**
(not copies) so existing `from collector import X` import sites kept
working during the migration.

| Object | Type | Used by |
|---|---|---|
| `latest_by_server` | `dict[str, dict]` | routes/views.py, routes/api/misc.py (dotted), v2/state.py |
| `server_update_info` | `dict[str, dict]` | routes/views.py, v2/state.py |
| `server_hardware_info` | `dict[str, dict]` | v2/state.py (no current readers — kept for parity) |
| `server_auth_info` | `dict[str, str]` | v2/state.py |
| `_state_lock` | `threading.RLock` | routes/views.py, routes/api/misc.py, v2/state.py |
| `_accelerated_servers` | `dict[str, float]` | `accelerate_server()` |
| `_cpu_warn_history` | `dict[str, deque]` | `_cpu_gate_*`, aggregator tests |
| `_baseline_dev_history` | `dict[str, dict[str, deque]]` | v1's loop (private), can move with collector_loop |
| `last_cycle_completed` | `float` | `routes/api/misc.py` `/api/collector-status` |
| `force_log_collection` | `_V2ForwardingEvent` | `routes/api/misc.py` |
| `force_update_collection` | `_V2ForwardingEvent` | `routes/api/misc.py` |

**Blocker rating:** these block retirement because of the route
imports. Every importer has to be repointed to a new location, then
the originals deleted.

### Category C — Constants

| Constant | Used by | Why it stays where it is |
|---|---|---|
| `METRIC_LABELS` | error messages in aggregator | Could move to `models.py` or `detection.py` |
| `_SEVERITY_RANK` | `_max_severity` | Lives with the helpers it serves |
| `_OFFLINE_MARKERS` | inside `collect_server` (v1 probe) | Already mirrored in v2/checks.py |
| `PS_COLLECT_SCRIPT` and 4 siblings | duplicated in collector_v2/scripts.py with parity test | Duplication collapses on v1 delete |

### Category D — Control-point shims (v1 cooperatively forwards to v2)

These exist in `collector.py` today because the legacy collector ran
the action, and v2 needed to participate. With v1 gone, they collapse
into v2 native.

| Shim | What it does today | After v1 retires |
|---|---|---|
| `accelerate_server(name, ttl_s)` | Writes both `_accelerated_servers` (v1) and `state.server_health[name].accelerated_until` (v2) | Pure v2 call |
| `force_log_collection` (`_V2ForwardingEvent`) | `.set()` flips v1's flag AND calls v2's `force_sync_now(type="logs")` | Removed; routes call v2 directly |
| `force_update_collection` | Same pattern as logs | Same |
| `set_collector_config_ref(config)` | Sets `_COLLECTOR_CONFIG_REF` for the LDAP probe | Move to v2 startup |

### Category E — Routes coupled to v1 module

Every API route file imports `import collector as _collector_module` at
the top and uses dotted access. The grep output:

```
analytics.py:1122            import collector as _collector_mod  (anomaly N-of-M check)
analytics.py:1127            _collector_mod._cpu_gate_passes(...)
routes/api/health.py:11      import collector as _collector_module
routes/api/health.py:12      from collector import ...
routes/api/misc.py:80        _collector_module.last_cycle_completed
routes/api/misc.py:895       _collector_mod._state_lock
routes/api/misc.py:896       _collector_mod.latest_by_server
... (same pattern in metrics/servers/updates/workflows/reports/rbac/power/config)
routes/views.py:58           from collector import _get_active_maintenance_window
routes/views.py:161,206      from collector import latest_by_server, _state_lock
routes/views.py:357          from collector import server_update_info
```

**11 route modules total** all import shared state or helpers from
`collector`. Retirement requires updating every one of them.

### Category F — Tests

```
tests/test_collector_v2_aggregator.py:541,584  from collector import _cpu_warn_history
```

Two test cases reach into v1's CPU ring buffer. Moves with the
extraction.

---

## What "v1" actually is

It's tempting to read "retire v1" as "delete `collector.py`". That's
not accurate. What we'd retire is:

* The `collector_loop()` function (the per-cycle batch scheduler)
* The `collect_server()` per-server probe (v2 has its own in
  `collector_v2/checks.py`)
* The cycle math (`cycle_count % _log_cycles == ...` etc.)
* The 5-thread `ThreadPoolExecutor` inside the loop
* `_V2ForwardingEvent` once routes talk to v2 directly
* The legacy mode of the `collector_engine` setting

What *survives* the retirement (extracted to other modules):

* All Category A helpers (status computation, anomaly dispatch,
  maintenance gating, TLS checks, drift, scheduled reports, …)
* All Category B state dicts (live somewhere neutral)
* All Category C constants
* All Category E import sites (updated to point at new homes)

---

## Retirement plan

### Phase R1 — Extract pure-logic helpers (no behaviour change)

Goal: every Category A helper lives somewhere other than
`collector.py`. `collector.py` re-exports them via `from new_home import
*` so existing import sites keep working. **Reversible** at any
checkpoint.

| New module | Functions to move |
|---|---|
| `detection.py` (new) | `_active_level_detector`, `compute_status`, `_effective_status`, `_max_severity`, `_get_worst_metric`, `_cpu_gate_record`, `_cpu_gate_passes`, `dispatch_anomaly_events_v2`, `METRIC_LABELS`, `_SEVERITY_RANK` |
| `maintenance.py` (new) | `_get_active_maintenance_window`, `_is_alert_suppressed_by_maintenance`, `_get_maintenance_thresholds` |
| `tls_monitor.py` (new) | `_check_tls_certificates`, `_send_cert_alert` |
| `healthchecks.py` (new) | `_run_health_checks` |
| `drift.py` (new) or `security_checker.py` (existing) | `_collect_drift_snapshots` |
| `security_checker.py` (existing) | `_collect_all_failed_logins` |
| `reports.py` (new) or `routes/api/reports.py` (existing) | `_check_scheduled_reports`, `_generate_scheduled_report` |

Each move:
1. Cut the function from `collector.py`.
2. Paste into the new module.
3. Add `from <new_module> import <symbol>` at the top of `collector.py`
   so `from collector import X` keeps working during transition.
4. Run the full test suite.

**Estimated:** 1 day. All tests should keep passing throughout.

### Phase R2 — Move shared state to a neutral module

Goal: routes import shared state from a `state.py` module that doesn't
care which collector engine is running.

Concrete plan:
1. Create new top-level `state.py` (or repurpose `collector_v2/state.py`
   — slight name clash risk, prefer the top-level location).
2. Move definitions: `latest_by_server`, `server_update_info`,
   `server_hardware_info`, `server_auth_info`, `_state_lock`,
   `_accelerated_servers`, `last_cycle_completed`.
3. Migrate `_cpu_warn_history` and `_baseline_dev_history` to live
   alongside their consumers in `detection.py`.
4. Update all 11 route files to import from the new locations.
5. Leave back-compat re-exports in `collector.py` (e.g.
   `latest_by_server = state.latest_by_server`) so any straggler
   import keeps working.
6. Verify by running the app under both engines and confirming the
   dashboard refreshes correctly.

**Estimated:** 1 day. The route updates are mechanical but there are 11
of them.

**Risk:** missing an import site means a dashboard widget shows stale
data silently. Catch via integration test: spin up Flask, hit
`/api/collector-status` and `/api/system/health`, verify the values
change in response to v2 activity.

### Phase R3 — Retire `collector_loop` and v1-only functions

After R1 + R2, the only thing keeping `collector.py` alive is the
legacy loop itself.

1. Delete `collector_loop()`.
2. Delete `collect_server()` (v1's per-server probe — v2's
   `checks.py` has its own).
3. Delete the `_V2ForwardingEvent` class. Update
   `routes/api/misc.py`'s sync-now endpoints to call
   `collector_v2.force_sync_now(...)` directly.
4. Move `set_collector_config_ref` to v2 startup; routes that called it
   get pointed at v2's equivalent.
5. Delete the `"legacy"` and `"both"` values from `collector_engine`
   setting; force the value to `"v2"` at startup with a one-time
   warning in the log.
6. Delete `collector_v2/scripts.py` ↔ `collector.py` parity test
   (it has nothing to compare against).

**Estimated:** half day.

### Phase R4 — Delete `collector.py`

After R1-R3, `collector.py` should be either empty or a tiny back-compat
shim with re-exports. Decision time:

* **Option A — keep the shim** for one release as a soft landing for
  external scripts that might `from collector import latest_by_server`.
  Reduces support burden but leaves a confusingly-named file in the
  repo.
* **Option B — delete outright.** Cleanest. Forces any external script
  to update.

I'd recommend Option B with a `CHANGELOG.md` entry.

**Estimated:** 1 hour.

---

## Things to rethink during retirement

These are decisions that were made under "compatibility mode" pressure
and deserve a fresh look:

### 1. The `_V2ForwardingEvent` shim is clever but wrong

It makes `force_log_collection.set()` work as a single call that flips
both engines' flags. Clever during the dual-running phase — but with
v2 as the sole engine, the indirection serves no purpose. Replace
with direct calls: `collector_v2.force_sync_now(type="logs")`.

### 2. `_COLLECTOR_CONFIG_REF` is a global

The LDAP health probe reads it because the probe was inline in v1's
loop and needed a config handle. v2's periodics shouldn't have to know
about this. Pass the config object to the probe explicitly, kill the
global.

### 3. `analytics.py` reaching into `collector._cpu_gate_passes`

`analytics.py:1127` does `_collector_mod._cpu_gate_passes(...)` to
check the CPU N-of-M gate from the server-detail page render path.
That's a circular dependency: analytics → collector → analytics. After
R1 moves `_cpu_gate_passes` to `detection.py`, this becomes
analytics → detection, which is one-way and clean.

### 4. The shared dict pattern itself

`latest_by_server` is a `dict[str, dict]` written by the collector and
read by the dashboard with a single global lock. It works fine for 30
servers but doesn't scale gracefully:
* Reads block on the write lock.
* No bounded-history semantics — every read sees "whatever was there
  last".
* Persisted nowhere — restart = empty cache until first cycle.

Worth considering: replace with a small SQLite-backed view or an
explicit `LastMetricsCache` class with read-mostly semantics (RWLock
or copy-on-write).

This is NOT a retirement blocker — but R2 is the natural moment to
rethink the shape, because we're already moving the code.

### 5. The "engine" abstraction itself

After v1 dies, `collector_engine` setting only ever has one value:
"v2". The setting becomes vestigial. Two paths:

* **Delete the setting** — cleaner.
* **Keep it as a stub** for a future v3 — speculative; YAGNI suggests
  delete.

I'd delete it, deleting the SECTION 2.5 settings.html row we just
added. The UI cleanup is small and the setting being permanently `v2`
is more confusing than just having no setting at all.

---

## When to do the retirement

**Pre-conditions (in order):**

1. ☐ v2 has been the default (`collector_engine = "v2"`) in production
   for ≥1 week.
2. ☐ Operations dashboard shows zero CRITICAL events caused by v2
   itself (worker pool starvation, aggregator crashes, etc.).
3. ☐ The 5 acceptance tests in `docs/COLLECTOR_V2_GOALS.md` have all
   passed.
4. ☐ At least one Prism restart has happened on v2 without operator
   intervention (proves the startup path is solid).
5. ☐ No outstanding HIGH-severity findings in
   the internal collector-v2 audit.

**Once all five tick:** R1-R4 can be done in a single 2-3-day block. No
need to spread across releases — the changes are mechanical, the test
suite covers the seams, and rollback is "git revert" while v2 keeps
running.

**Until then:** don't touch retirement. The current shim mode is
working; the dual-import safety net is worth its weight.

---

## What this document is NOT

* Not a commitment to retire v1 by any specific date.
* Not a list of bugs in v1 (there aren't outstanding v1 bugs that
  retirement would fix).
* Not the place to track v2 stability — that's the internal collector-v2 audit.

It exists so that when someone says "let's retire v1", the entire
scope is visible in one place and nobody has to re-derive it.
