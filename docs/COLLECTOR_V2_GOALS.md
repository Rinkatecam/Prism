# Collector v2 — Goals & Implementation Map

**Read this first** if you're picking up the v2 migration work. The
other docs (`COLLECTOR_V2_MIGRATION.md`, `_AUDIT.md`, `_ROLLBACK.md`)
cover the HOW; this one covers the WHY. Every decision in v2 traces
back to one of the five goals here. When extending or changing v2,
ask: "which goal does this preserve, and which one might it break?"

---

## Why we rebuilt the collector

In one sentence: **the v1 collector couldn't scale because it treated
the entire fleet as a single batch with a shared deadline.** A 90-second
cycle budget was shared by all 30 servers. A single slow server could
blow the whole budget. Every fix we tried (staggering cadences, sharding
the WU check across cycles) was a workaround for that single coupling —
not a fix.

The user's exact words when commissioning v2:

> "I need the best way to collect so we don't have problems and have
> consistent and good analytics for each servers. and it can grow with
> the servers that are configured."

That's the bar. v2 either meets it or we go back.

---

## The 5 goals (in priority order)

### Goal 1 — Server independence
**One slow server must not affect any other server's data freshness.**

In v1 a single hung WinRM call could push the cycle past 90 s and cause
all unfinished servers to be marked offline. v2 must guarantee that
SRV06 taking 60 s to respond doesn't change anything about how often
SRV02 gets polled or how fresh its metrics are.

**Pre-flip acceptance test (NOT YET EXECUTED):** if you `sleep(180)`
inside one server's check function, every other server's metrics-row
insertion cadence stays at ~60 s.

### Goal 2 — Predictable per-server cadence
**Each server's check intervals (metrics, logs, updates, hardware) must
be honored regardless of fleet size.**

If `update_check_interval_minutes` is set to 30 in settings, EVERY server
gets its WU check every 30 min — whether there are 30 servers or 300.
The per-server cadence does NOT degrade as the fleet grows.

**Pre-flip acceptance test (NOT YET EXECUTED):** at 30, 100, and 500
servers, the average gap between WU checks per server stays within
±5% of the configured value. **Caveat:** the supervisor's 5 s tick
imposes a ±5 s jitter floor regardless of fleet size. For the default
60 s metrics interval that's ±8% — strictly outside the ±5% target.
The test should use a 300 s interval (or longer) to test the cadence
claim cleanly.

### Goal 3 — Scale headroom
**Adding servers to the config must not require any code or design changes
up to ~200 servers; the architecture should bend gracefully to ~500.**

Specifically: no O(N²) loops anywhere in the hot path, no shared resource
that grows linearly with N, no scheduling decision that has to fan out
to all servers atomically.

**Pre-flip acceptance test (NOT YET EXECUTED, NEEDS LOAD HARNESS):**
the supervisor's tick latency at N=500 servers is still under 100 ms.
The work queue never has more than `num_workers × 4` items at steady
state. **Today's evidence:** measured at N=30 only (smoke test 2026-05-19).
Behavior at N>30 is design-supported, not behavior-validated.

### Goal 4 — Consistent analytics per server
**Status transitions, anomaly detection, baseline checks, alert
dispatch, and event correlation must produce the same results in v2 as
in v1 for the same input data.**

This is the "no regression" goal. v1's hard-won logic for CPU N-of-M
gates, alert fatigue, maintenance windows, baseline ring buffers,
suppression windows — all of it must work in v2. The user shouldn't see
a behavior change just because the engine flipped.

**Pre-flip acceptance test (NOT YET EXECUTED):** run v1 and v2 in
`"both"` mode against the same fleet for 1 hour. Compare metric rows,
event rows, baseline checks, and alert dispatches. v2 should match v1
±1% (jitter only from the per-server scheduling stagger). **Caveat:**
v2 re-implemented baseline detection and status-transition handling
from scratch rather than reusing v1's inline blocks. Anomaly detection
reuses v1 via `dispatch_anomaly_events_v2`. The re-implemented pieces
are the highest risk for divergence — focus the side-by-side check
there.

### Goal 5 — Debuggable, observable, recoverable
**When something breaks, the operator must be able to tell what broke,
why, and what to do about it — without reading source code.**

Every component (supervisor, workers, aggregator, periodics) maintains
its own heartbeat, error counter, and health snapshot. The watchdog
surfaces stuck threads via CRITICAL log + audit row. The
`/api/system/health` endpoint exposes everything an operator needs in
one JSON response. No silent failures.

**Pre-flip acceptance test (NOT YET EXECUTED):** if you `kill -9` one
worker thread manually, the operator sees it within ≤2 minutes via the
watchdog and can identify which component without reading the codebase.
**Today's evidence:** the audit (separate doc) confirmed the
`/api/system/health` endpoint surface, heartbeat plumbing, and watchdog
wiring all exist and work as designed. No manual kill-test executed
yet.

---

## Pre-flip checklist

v2 stays opt-in (`collector_engine="legacy"` is the default) until all
of these are true. **Do not flip the default before checking these
off.** If you're tempted to flip early, re-read the AUDIT doc — the
gaps are real.

```
[ ] All 5 acceptance tests above executed and passing
[ ] 1-hour side-by-side validation (`collector_engine="both"`)
    showing v1↔v2 parity within 1% — metric counts, event counts,
    alert dispatches
[ ] MEDIUM findings 1-7 from the internal collector-v2 audit closed (or
    explicitly accepted with rationale in the audit doc)
[ ] Startup heartbeat-0 issue fixed (no more "9999s stale" CRITICAL
    flap on Prism boot)
[ ] Operator has read COLLECTOR_V2_ROLLBACK.md and knows how to
    switch back to legacy in <2 minutes
[ ] PS-script parity test (test_collector_v2_scripts_parity.py)
    in CI and green
[ ] Health endpoint test (test_collector_v2_health_endpoint.py)
    in CI and green
[ ] An operator has run v2 in production for ≥1 week with zero
    CRITICAL audit-log entries from the watchdog
```

When the last box is ticked, change `_DEFAULT_SETTINGS["collector_engine"]`
in `config_manager.py` to `"v2"` and ship.

---

## How v2 achieves each goal

### Goal 1 → Per-WorkItem deadlines (not per-cycle)

Each check has its own deadline:
- METRICS: 30 s
- LOGS: 60 s
- UPDATES: 120 s (acknowledging WU is genuinely slow)
- HARDWARE: 30 s

A worker enforces this via `concurrent.futures.ThreadPoolExecutor`
wrapping the WinRM call. When the deadline expires, the future is
cancelled, the worker emits a Result with `error_kind="timeout"`, and
moves on to the next item. No shared budget; no straggler-rebuild
logic; no cycle-wide cascade.

**File: `collector_v2/workers.py::_invoke_with_deadline`**

### Goal 2 → Per-server health state owned by the supervisor

Each server has a `ServerHealth` record with:
- `next_metrics_at`, `next_logs_at`, `next_updates_at`, `next_hardware_at`
- `consecutive_failures` per check type (drives exponential backoff)
- `accelerated_until` (overrides cadence when set)
- `pending[check_type]` (prevents double-enqueue while a check is in flight)

The supervisor ticks every 5 s. For each server × check, it asks:
"is `now >= next_X_at` AND `not pending[X]`?" — if yes, enqueue. After
enqueueing, it bumps `next_X_at` by the configured interval. The
aggregator clears `pending[X]` when the Result lands, so the cycle
repeats.

Fleet size doesn't enter the decision. Each server is scheduled
independently from a dict lookup.

**File: `collector_v2/supervisor.py::_schedule_server`**

### Goal 3 → O(N) scheduling, bounded worker pool, bounded queue

- Supervisor iteration is `O(N servers × 4 check types)` per 5 s tick.
  At N=500 that's 2,000 dict ops — microseconds.
- Worker pool is fixed at 15 (tunable via `collector_v2_num_workers`).
  Adding servers does NOT add threads. Workers process whatever's in
  the queue.
- Work queue is bounded at `num_workers × 4 = 60` items by default.
  When full, the supervisor reschedules the server (push `next_X_at`
  forward 30 s) instead of blocking. **Backpressure is the design.**
- Result queue is unbounded — dropping a result would lose data. If
  the aggregator falls behind, the watchdog catches the slow heartbeat.

The hot path is independent of N for the same reason a database B-tree
lookup is: we structured the work to be inherently per-item.

**Files: `collector_v2/__init__.py::start_collector_v2` (queue sizing),
`workers.py::WorkerPool` (worker count), `supervisor.py::_schedule_server`
(O(N) tick).**

### Goal 4 → Reuse v1's analytics logic, not rewrite it

The analytics-layer functions are the load-bearing pieces of Prism that
have been tested and tuned over many sprints. v2 explicitly does NOT
rewrite them. Instead:

- **Status decision** — `_effective_status` from `collector.py` is
  imported and called by the aggregator.
- **Anomaly detection** — `dispatch_anomaly_events_v2` is the v2
  entrypoint, but the underlying `detect_anomalies` and
  `detect_rate_anomalies` calls into `analytics.py` are unchanged.
- **Alert fatigue gates** — `alert_scoring.is_throttled_by_fatigue` is
  imported and called from the aggregator's `_dispatch_alert`.
- **Maintenance windows** — `_is_alert_suppressed_by_maintenance` from
  `collector.py` is imported by the aggregator.
- **CPU N-of-M gate** — `_cpu_gate_record` / `_cpu_gate_passes` from
  `collector.py` are imported.
- **Baseline N-of-M ring** — re-implemented in `aggregator.py` (the
  state was tightly coupled to cycle-counting, so re-implementing was
  cleaner than refactoring v1). Logic is line-by-line ported.
- **Periodics** — TLS / drift / failed-logins / scheduled reports /
  retention all call into v1's existing helpers (`_check_tls_certificates`
  etc.) from `collector_v2/periodics.py`.

The v1↔v2 parity test (audit's H4 finding fix) is: same fleet, same
metrics inputs, same decisions, same DB writes. We expect ±1% jitter
from the staggered scheduling — no behavioral divergence.

**Files: anywhere v2 imports from `collector` or `analytics` or
`alert_scoring` — search `from collector import` inside `collector_v2/`.**

### Goal 5 → Heartbeats + counters + health endpoint

Each thread:
1. Writes a heartbeat timestamp at the end of its work iteration.
2. Maintains a critical-error counter (incremented when the bulletproof
   catch-all fires).
3. Exposes a `get_X_health()` function returning current stats.

The aggregated `collector_v2.get_health_snapshot()` is exposed via
`/api/system/health` (audit H2 fix). It returns per-component data so
the operator can pinpoint a stuck component without reading source.

The watchdog (extended in `app.py`) monitors all three v2 threads. When
a heartbeat goes stale (> 5× the thread's expected interval), it
CRITICAL-logs + writes an audit row. One alert per transition (not per
60 s tick).

**Files: `state.py::heartbeat_*` (heartbeats), `supervisor.py::get_supervisor_health`
(per-component snapshot), `routes/api/health.py` (HTTP surface),
`app.py::_watchdog_loop` (monitoring).**

---

## What "done" means for each goal

The grading rubric below is strict on purpose. **A `✓` means "tested
under realistic conditions and passing."** A `◐` means the design
supports it but the corresponding test hasn't been executed. A `✗`
means there's a known gap. Be honest — promoting `◐` to `✓` without
running the actual test is how teams ship overconfident systems.

| Goal | Built | Tested in unit suite | Behavior-tested against real fleet | Default-on |
|---|---|---|---|---|
| 1 — Server independence | ✓ | ◐ (per-component tests assume the architecture; no integration test running two simultaneous slow checks) | ◐ (smoke 2026-05-19 on N=30 happy-path only) | ✗ |
| 2 — Predictable per-server cadence | ✓ | ✓ (`test_collector_v2_supervisor.py`) | ◐ (smoke 2026-05-19, single fleet size) | ✗ |
| 3 — Scale headroom | ✓ | ✗ (no load tests in suite) | ✗ (no N>30 measurement) | ✗ |
| 4 — Consistent analytics | mostly — baseline + transitions re-implemented in v2; anomaly via `dispatch_anomaly_events_v2`; maintenance, alert fatigue, CPU N-of-M all import from v1 | ◐ (per-handler tests, no v1↔v2 parity test) | ✗ (1-hour `"both"` mode never run) | ✗ |
| 5 — Debuggable + recoverable | ✓ | ◐ (heartbeats and counters tested; kill-thread scenario untested) | ◐ (`/api/system/health` now has a regression test from the audit fixes; manual kill-test not yet executed) | ✗ |

**Reading the table:** the column-1 "Built" status is `✓` for all 5
goals because the code exists and parses. The validation columns
honestly carry mostly `◐` — the design implements each goal, but the
"and we ran the test that proves it" step is in the pre-flip
checklist above. Don't grade up without doing the test.

---

## Architectural principles to preserve

When extending v2, these are the load-bearing invariants. Breaking any
of them undoes the corresponding goal.

1. **Workers do not write to the DB.** Only the aggregator persists.
   This serializes mutation and avoids races. If you find yourself
   wanting a worker to write, the answer is "put it on the result
   queue and let the aggregator handle it." (verify: grep
   `db.insert_` in `collector_v2/workers.py` — should be zero hits)

2. **The supervisor does not call WinRM.** Only workers do. The
   supervisor's job is fast scheduling decisions; if it ever blocks on
   I/O the whole pipeline stalls. (verify: grep `make_wsman\|RunspacePool`
   in `collector_v2/supervisor.py` — should be zero hits)

3. **The aggregator does not enqueue work.** Only the supervisor does.
   This keeps the "what's due" decision in one place. (verify: grep
   `work_queue.put` in `collector_v2/aggregator.py` — should be zero
   hits)

4. **`pending[ct]` is set by the supervisor and cleared by the
   aggregator.** Both sides must always update it. If a code path
   exists where the supervisor enqueues but the aggregator never
   clears (e.g., a worker drops the item without emitting a Result),
   that server's check gets stuck forever. **The defensive
   emit-on-exception in `workers.py::_execute_one` closes the worst
   gap here** (audit M3 fix — there's a regression test in
   `test_collector_v2_workers.py::test_execute_one_emits_result_even_when_inner_raises`).

5. **Bulletproof catch-all at the outer loop of every thread.** No
   thread may die from an `Exception`. The watchdog cannot catch
   `BaseException` (segfault, KeyboardInterrupt), but everything else
   must be contained within the thread. Three places to check:
   `supervisor._loop`, `WorkerPool._worker_loop`, `Aggregator._loop`.

6. **Heartbeats are pulse-of-life.** A thread that's working hard but
   not heartbeating looks dead to the watchdog. Make sure every
   meaningful loop iteration calls the appropriate `state.heartbeat_*`.
   **Known operator-visibility regression:** v2 does NOT advance
   `collector.last_cycle_completed` (there's no "cycle" in v2).
   Operators with external log-parsing or external monitoring keyed on
   that variable will see it stuck at the moment v2 took over. The
   replacement is `last_aggregator_tick` (via `/api/system/health`).
   If you're flipping the default, audit any external tooling first.

7. **Acceleration is idempotent.** Calling `accelerate_server(name,
   600)` twice in quick succession should NOT add 1200 s of
   acceleration — it should reset to 600 s from the second call. The
   current implementation matches this; preserve it.

8. **Settings are read each supervisor tick.** Don't cache them at
   module-load. Operators expect to edit `config.json` and have the
   collector pick up the new poll interval / cadences within one tick.

9. **PowerShell scripts are sacred.** `scripts.py` is a verbatim copy
   of v1's. Don't edit them in v2 without also editing v1 (and vice
   versa) until v1 is fully retired. Mismatch means side-by-side
   validation lies.

10. **Default to `"legacy"` until proven.** The `_DEFAULT_SETTINGS`
    in `config_manager.py` keeps `collector_engine="legacy"` so
    operators opt in explicitly. We flip the default only after a
    quarter of clean v2 runs on real fleets.

---

## How to add a new check type to v2

The supervisor / worker / aggregator architecture absorbs new check
types cheaply because per-check state lives in a single
`dict[CheckType, CheckState]` (audit M2 refactor). No switch statements
to edit; just register the enum member and the right handlers.

Concrete recipe (5 files, no dataclass surgery):

1. **`collector_v2/types.py`** — add a member to `CheckType` enum.
   Add an entry to `DEFAULT_DEADLINES_S` and `DEFAULT_INTERVALS_S`.
   *Nothing else in this file changes* — `ServerHealth` stores state
   in `self.checks[CheckType]` so the new key is picked up
   automatically.
2. **`collector_v2/scripts.py`** — add `PS_<NAME>_SCRIPT`. If v1
   also needs the check, mirror to `collector.py` (the parity test
   in `tests/test_collector_v2_scripts_parity.py` will fail until
   you do — that's intentional).
3. **`collector_v2/checks.py`** — add `check_<name>(server, pool) ->
   (ok, data, err, kind)` function. Use one of the existing checks
   as a template.
4. **`collector_v2/workers.py`** — add the new `CheckType` → check
   function entry to `_CHECK_DISPATCH`. The deadline lookup goes
   through `DEFAULT_DEADLINES_S` automatically.
5. **`collector_v2/aggregator.py`** — add a `_handle_<name>_result`
   method and add a case to `_process_result`'s dispatch.
6. **`tests/`** — add one test in each of the supervisor / workers /
   aggregator suites covering the new check.

Note: `supervisor.py` doesn't need any change for the new check type
— its loop already iterates `for ct in CheckType` and uses
`DEFAULT_INTERVALS_S[ct]` for cadence. You only touch the supervisor
if you want a different initial stagger offset.

That's it. No fleet-wide cadence math, no shared budget to negotiate,
no risk of starving existing checks. The architecture absorbs the
new work because each check is its own self-contained item.

---

## How to extend v2 to a new fleet size

The rows below are **design estimates**, not measured operating points.
Only the first row (30 servers, default config) has been smoke-tested
end-to-end. The architecture is structured to scale per the table, but
the constant factors at each row need actual measurement before you
trust them in production.

| Target | Estimated change needed | Status |
|---|---|---|
| Up to 50 servers | None expected. Default 15 workers + 60-item queue is plenty. | **Smoke-tested at 30** (2026-05-19) |
| 50–200 servers | Bump `collector_v2_num_workers` to 25-30. Verify queue depth stays under 80%. | Untested; design supports |
| 200–500 servers | Workers to 40-50. Consider raising work_queue capacity (currently `num_workers × 4`). Validate supervisor tick latency stays under 100 ms — if it grows, the dict iteration is becoming a bottleneck and you'd want to shard the supervisor. | Untested; design supports |
| 500–2,000 servers | Replace pypsrp's sync WinRM with an async-capable wrapper. Each worker becomes an asyncio task. Threading model unchanged on the supervisor + aggregator side; only the workers go async. | Speculative |
| 2,000+ servers | Multi-process. Shard the fleet across N collector processes, each running a full v2 stack. DB is shared. | Speculative |

**Before any fleet expansion**, spin up a load test (even a mocked one —
30 fake `ServerConfig` entries pointing at unreachable hosts is enough
to measure the supervisor's tick latency and the queue's steady-state
depth at the target N). The architecture is designed to absorb the
load; "designed to" is not the same as "verified to."

---

## What was deliberately NOT built

Things that were considered and skipped, with the reasoning. Future
implementers, read this so you don't re-litigate decisions:

- **Pure async (asyncio + aiohttp)** — pypsrp is sync-only. Migrating
  to async would require replacing or wrapping pypsrp, which is a much
  bigger refactor than the per-thread model. Not worth it under
  ~500 servers.

- **Auto-restart of stuck threads** — the watchdog detects but does
  not auto-restart. A silently-restarted thread can mask recurring
  bugs (e.g., a deadlock that the watchdog "fixes" every 30 s would
  never get debugged). Operator-paged investigation is the
  intended behavior. See audit M-MEDIUM-2.

- **Stuck-pending TTL recovery** — the audit flagged that if a worker
  drops an item without emitting a Result, `pending[ct]` stays True
  forever for that server. We did NOT add a TTL on `pending` because
  the fix should be at the source. **Closed by audit M3 fix:**
  `_execute_one` now has a defensive `except BaseException` that
  ALWAYS returns a `Result(ok=False, error_kind="exception")` so the
  aggregator always clears `pending[ct]`. A TTL fallback would mask
  bugs; this guarantees correct behavior instead. Regression test:
  `test_execute_one_emits_result_even_when_inner_raises`.

- **DB write through workers** — would parallelize DB writes but
  risks row-level races on `latest_by_server` cache updates and
  status-transition detection. The aggregator's single-writer model
  is simpler and easier to reason about. At 30 servers × ~5 results/s
  the aggregator is nowhere near saturated.

- **Per-server worker pinning** — would let us guarantee per-server
  ordering of checks. Not needed because the aggregator processes
  results in arrival order and per-server state updates are serial
  there anyway. Adding pinning would make load-balancing harder.

- **Adaptive worker pool sizing** — auto-scaling the pool based on
  observed queue depth was discussed and skipped. Simpler to expose
  `collector_v2_num_workers` as a setting and let operators tune. We
  may add this if we see operator-tuning happen often in practice.

---

## Anti-patterns to refuse

If a future change request asks for any of these, push back:

- **"Let workers update the dashboard cache directly to avoid the
  aggregator round-trip"** — breaks Goal 5 (you can no longer trace
  who wrote what when) AND opens race-condition vulnerabilities.

- **"Add a fast-path that bypasses the queue for urgent checks"** —
  breaks Goal 1 (the "urgent" path will starve regular checks under
  load). Use acceleration instead — that's what it's for.

- **"Let the supervisor inline the check itself for small fleets"** —
  removes the deadline isolation that's the whole point. The 5-s
  supervisor tick is the only thing that needs to be instant.

- **"Stop heartbeating during quiet periods to save CPU"** — defeats
  the watchdog. Heartbeats are nearly free anyway (one atomic write
  per loop iteration).

- **"Let's ship v2 as default; we'll fix the M-findings later"** —
  the audit (the internal collector-v2 audit) found 7 MEDIUM-severity gaps
  that aren't individually blocking but add up to real risk. The
  pre-flip checklist above exists to gate exactly this temptation.
  Close them first, OR explicitly accept each one in the audit doc
  with a rationale. Don't silently defer.

- **"Skip the v1↔v2 parity test because manual smoke worked"** —
  manual smoke verifies "v2 runs"; parity verifies "v2 produces the
  same DB writes as v1." Different goals, different evidence. The
  re-implemented baseline + transition logic in the aggregator
  (audit's Goal 4 caveat) is exactly the kind of code that passes a
  smoke test and silently produces 5%-different alert dispatch
  counts.

---

## Where to look for things

| If you're trying to... | Read first |
|---|---|
| Understand WHY v2 exists | This file |
| Understand the architecture | `COLLECTOR_V2_MIGRATION.md` |
| Find a specific bug or gap | the internal collector-v2 audit |
| Switch engines / troubleshoot operationally | `COLLECTOR_V2_ROLLBACK.md` |
| Add a new check type | This file, "How to add a new check type" |
| Scale beyond current fleet | This file, "How to extend v2" |
| Run the tests | `python -m pytest tests/test_collector_v2_*.py -v` |
| Validate against real fleet | Set `collector_engine="both"` for 1 hour |
