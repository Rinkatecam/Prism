# 09 — Performance Qualification (PQ) Scenarios

| Field | Value |
|---|---|
| Document ID | CSV-09 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `02_URS.md`, `07_IQ_PROTOCOL.md`, `08_OQ_TEST_INVENTORY.md` |

## Purpose

PQ scenarios demonstrate that Prism, in its validated configuration, can perform the **operator-facing workflows** described in the URS, against the **real production-equivalent infrastructure**, repeatably, with the **expected outcomes**. Each scenario walks end-to-end, exercises one or more URS items, and produces evidence (screenshots, log lines, DB rows) that the system behaves as specified.

PQ is executed:
- After IQ + OQ pass on a new deployment.
- After any major configuration change (re-baselining).
- On a periodic cadence (recommended: quarterly).

## How to read

Each scenario has the shape:
- **PQ-NNN — Title**
- **URS coverage**: list of URS-IDs.
- **Pre-conditions**: state required.
- **Steps**: ordered user actions.
- **Expected outcome**: observable result + evidence.
- **Pass criteria**: explicit acceptance test.

---

## PQ-001 — Steady-state monitoring

**URS coverage**: URS-001, URS-002, URS-005, URS-008, URS-122.

**Pre-conditions**: Prism running; 5+ servers configured; collector has been up ≥ 10 min.

**Steps**:
1. Open the dashboard at `/`.
2. Observe the server-grid tiles refreshing.
3. Observe the topbar pulse / ECG indicator.
4. Click any tile to open the server detail page; observe metric updates.
5. Scroll the history chart to confirm at least 1 h of data is shown.

**Expected outcome**:
- Every configured server shows a non-stale tile (last-update timestamp within `2 × poll_interval_seconds`).
- The ECG strip beats at ~1 Hz with green spikes.
- Counter shows N/N up (where N = configured server count, minus any genuinely offline).
- Server detail page metrics chart renders.

**Pass criteria**: all observable items above. Capture screenshot.

---

## PQ-002 — Threshold breach → alert dispatch

**URS coverage**: URS-002, URS-010.

**Pre-conditions**: A test server whose CPU can be driven to 95% (via a `while($true){}` loop or similar). Email or webhook configured.

**Steps**:
1. On the target server, kick off CPU stress.
2. Wait ≤ 2 × `poll_interval_seconds`.
3. Observe the server tile transition from green → amber → red.
4. Confirm one event row appears in the events feed for the transition.
5. Confirm the configured email/webhook receives one alert with the correct server name + metric.

**Expected outcome**: alert lands in the operator's channel within 2 cycles.

**Pass criteria**: transition logged in `events` table; one alert dispatched; correct metric+threshold cited.

---

## PQ-003 — Anomaly detection + ack/snooze

**URS coverage**: URS-011, URS-013.

**Pre-conditions**: `anomaly_detection.enabled = true`; ≥ 24 h of baseline data per metric.

**Steps**:
1. Drive a target server's CPU well above baseline (but below the threshold) for the configured N-of-M window.
2. Wait for an anomaly event to appear in the server detail page.
3. Click "Acknowledge" with a note.
4. Verify the anomaly does NOT re-fire while the ack is active.
5. Click "Remove ack"; verify the anomaly is reconsidered on the next cycle.

**Expected outcome**: ack lifecycle works; the suppression window is honoured.

**Pass criteria**: row in `anomaly_acknowledgments`; no duplicate event rows during ack window.

---

## PQ-004 — Maintenance-window threshold loosening

**URS coverage**: URS-004.

**Pre-conditions**: Maintenance window configured for the test server, today, current time-of-day, with loosened CPU threshold.

**Steps**:
1. Drive CPU above the normal threshold but below the loosened one.
2. Observe the server stays green/amber, not red.
3. Wait for the window to end (or temporarily edit the schedule).
4. Drive CPU; observe normal thresholds resume.

**Expected outcome**: thresholds switch on the window's start/end boundary.

**Pass criteria**: server stays in expected status during the window.

---

## PQ-005 — Manual restart + lifecycle visualisation

**URS coverage**: URS-030, FS-093, URS-122, URS-074.

**Pre-conditions**: Test server with `admin` permission to the operator.

**Steps**:
1. Open server detail; click **Restart**.
2. Observe the "rebooting" overlay appear.
3. Wait for the server to come back; observe transition to "stabilising" (≤ 60 s) then to normal.
4. Confirm an `audit_log` row was written with action `power:restart`, the operator's username, source IP.
5. Confirm `install_state.json` returned to `{}` for this server.

**Expected outcome**: full lifecycle plays out; audit row present.

**Pass criteria**: dashboard reflects the lifecycle; audit row present.

---

## PQ-006 — Scheduled Windows-update install with auto-restart

**URS coverage**: URS-033, URS-034, URS-036, URS-037, URS-038.

**Pre-conditions**: Test server with pending updates; operator has `admin` permission; tier-0 dual-control approved if applicable.

**Steps**:
1. From server detail, click **Install Updates** with **restart after = true**.
2. Observe the install lifecycle on the dashboard: queued → searching → downloading → installing → restart_required.
3. Confirm auto-restart fires (within 60 s of `restart_required`).
4. Observe rebooting → stabilising → cleared.
5. Confirm `audit_log` has rows for `install_updates` and `auto_restart`.
6. Two days later: confirm the **stuck-state janitor** would have cleaned any leftover row if the cycle had been incomplete (verify by inspecting `install_state.json` is `{}` for this server).

**Expected outcome**: full install + reboot cycle; audit complete; janitor clean.

**Pass criteria**: all observable items + DB state.

---

## PQ-007 — Stale `restart_required` self-clears

**URS coverage**: URS-037.

**Pre-conditions**: Test server with `install_state.status = "restart_required"`; operator has already rebooted the server out-of-band (RDP / manual restart).

**Steps**:
1. Wait ≤ 30 min for the next UPDATES check (or trigger manually via `/api/sync-updates-now`).
2. When the check returns `pending_reboot = False`, confirm `install_state[server]` is popped automatically.
3. Confirm the dashboard tile no longer shows "restart pending".

**Expected outcome**: stale flag cleared without operator action.

**Pass criteria**: `install_state.json` no longer contains the server entry; dashboard updated.

---

## PQ-008 — Workflow with all trigger types

**URS coverage**: URS-050, URS-051, URS-052, URS-060.

**Pre-conditions**: Test workflow created with: Manual trigger fallback + Schedule trigger at +5 min + Event trigger on a controllable condition (e.g. "service stopped").

**Steps (Manual)**:
1. Click **Run** in the workflow page; confirm execution row appears immediately.
2. Inspect step rows; confirm node executors fired in expected order.

**Steps (Schedule)**:
1. Set the schedule trigger to fire at a wallclock time 5 min away.
2. Wait; observe the execution fire within the 2-min window.

**Steps (Event)**:
1. Stop the service named in the event trigger.
2. Wait ≤ 1 min for the workflow scheduler tick.
3. Confirm the workflow fires once on the False → True transition.
4. Re-trigger the condition (start, then stop the service again); confirm a second fire occurs (debounce window respected).

**Pass criteria**: All three trigger types fire correctly; audit rows in `workflow_executions` + `workflow_execution_steps`.

---

## PQ-009 — Workflow PowerShell sandbox refuses dangerous script

**URS coverage**: URS-053, URS-054.

**Pre-conditions**: Workflow with a `Run PowerShell` block.

**Steps**:
1. Set the script to: `Invoke-Expression 'whoami'`
2. Save the workflow.
3. Confirm the validation banner on save flags the sandbox violation.
4. Run the workflow; confirm execution step is marked **failed** with reason citing sandbox HARD_DENY.

**Expected outcome**: sandbox blocks; clear error to operator.

**Pass criteria**: workflow_execution_steps row has `status='failed'` and error mentions sandbox / `Invoke-Expression`.

---

## PQ-010 — Tier-0 dual-control approval

**URS coverage**: URS-076.

**Pre-conditions**: A tier-0 server; two RBAC-admin accounts.

**Steps**:
1. As Admin A, attempt to restart the tier-0 server. Observe the system creates a pending approval row + returns "approval required".
2. As Admin B, view `/api/approvals`; approve the request.
3. As Admin A, retry the restart; confirm it executes.
4. Confirm `audit_log` contains rows for `approval_requested`, `approval_decided`, `tier0_approval_consumed`, and `power:restart`.

**Pass criteria**: dual-control loop closed; full audit trail.

---

## PQ-011 — Audit log integrity verification

**URS coverage**: URS-078, URS-079, URS-080, URS-081.

**Pre-conditions**: Prism has been running with operator actions for at least one day.

**Steps**:
1. Open a Python REPL: `from database import Database; db = Database('data/prism.db'); print(db.verify_audit_chain())`.
2. Confirm the result is `{ok: true, checked: N, first_break_id: None}`.
3. Open `data/audit_mirror.jsonl`; confirm the line count matches the row count in `audit_log`.
4. Attempt: `sqlite3 data/prism.db "DELETE FROM audit_log WHERE id=1;"` — must fail with the trigger's RAISE ABORT message.

**Pass criteria**: chain verifies, mirror line-count matches, trigger blocks deletion.

---

## PQ-012 — RBAC enforcement on destructive endpoints

**URS coverage**: URS-075, FS-075.

**Pre-conditions**: Test operator with **view** permission only on a server.

**Steps**:
1. As the view-only operator, attempt:
   - GET `/api/servers/<n>/history` → expected 200 OK.
   - POST `/api/servers/<n>/power` → expected 403 Forbidden.
   - POST `/api/servers/<n>/install-updates` → expected 403.
   - DELETE `/api/servers/<n>/data` → expected 403.
2. Re-run with **admin** permission; same endpoints; expect 200/202.

**Pass criteria**: 403 vs 200 split is exact as above.

---

## PQ-013 — Backup → wipe → restore round-trip

**URS coverage**: URS-094, URS-124.

**Pre-conditions**: Prism running with a known data state (count of metric rows, audit_log rows, etc.).

**Steps**:
1. Note current `audit_log` row count, `metrics` row count, configured server count.
2. Run `python tools/backup.py --output /tmp/pq013_backup.zip`.
3. Stop Prism.
4. Run `python tools/restore.py --input /tmp/pq013_backup.zip --target /tmp/restored/`.
5. Start Prism pointed at the restored data directory.
6. Confirm row counts and server list match the snapshot.

**Pass criteria**: post-restore state is byte-identical (or row-count-identical) to pre-wipe.

---

## PQ-014 — Locale / timezone display

**URS coverage**: URS-100, URS-101.

**Pre-conditions**: `settings.timezone = "Europe/Berlin"`; `settings.language = "de"`.

**Steps**:
1. Open dashboard; confirm UI labels render in German.
2. Hover any "Last update X seconds ago" tooltip; confirm timestamp is in Berlin time, formatted per `date_format` + `time_format`.
3. Inspect the underlying DB row's timestamp; confirm it is ISO-8601 UTC (the display layer did the conversion).

**Pass criteria**: UI in German; display tz = Berlin; DB tz = UTC.

---

## PQ-015 — Browser reduced-motion respected

**URS coverage**: URS-102.

**Pre-conditions**: Operator with `prefers-reduced-motion: reduce` in their OS / browser settings.

**Steps**:
1. Open dashboard.
2. Confirm the ECG pulse strip is hidden / replaced by a static dot + counter.
3. Confirm no other animated elements (loading spinners, progress bars) flash distractingly.

**Pass criteria**: motion suppressed; counter / static badge still visible.

---

## PQ-016 — Resilience to one bad target

**URS coverage**: URS-122.

**Pre-conditions**: A test server whose WinRM service is stopped (simulated outage).

**Steps**:
1. With the test server unreachable, observe its tile transitions to "offline" within ~2 cycles.
2. Observe the other 29 servers continue to poll on their normal cadence (check pulse widget BPM).
3. Restore WinRM on the test server; confirm it comes back within 1 cycle.

**Pass criteria**: outage isolated to the one server; fleet polling unaffected.

---

## PQ-017 — Failed-login alert spike

**URS coverage**: URS-015.

**Pre-conditions**: `security_alerts.failed_login_tracking = true`; a target with operator-controllable failed-login generation.

**Steps**:
1. From an external machine, issue 15 failed logon attempts (above threshold) against the target server within 15 min.
2. Wait for the next `failed_logins` periodic (5 min).
3. Confirm a `warning` event appears for the server; alert dispatched.
4. Issue 10 more failures (> 2× threshold); confirm `critical` event + alert.

**Pass criteria**: events emitted at warning + critical thresholds; alert channels notified.

---

## PQ-018 — TLS cert expiry warning

**URS coverage**: URS-016.

**Pre-conditions**: A configured TLS cert whose `not_after` will be within `warning_days` (e.g., 25 days).

**Steps**:
1. Wait for next TLS check cycle (1 h, or trigger manually).
2. Confirm dashboard shows the cert in the "expiring" bucket.
3. Confirm a `tls_certificate_expiring` event fires once (not on every cycle).

**Pass criteria**: warning fires; deduplication works.

---

## PQ-019 — Scheduled report delivery

**URS coverage**: URS-019.

**Pre-conditions**: `scheduled_reports.enabled = true`; `scheduled_reports.daily_time = "07:00"`; SMTP working.

**Steps**:
1. Configure schedule for the next 5-minute window.
2. Wait for emission.
3. Confirm email arrives at all configured recipients with the digest.
4. Re-check 24 h later — confirm exactly one digest per day.

**Pass criteria**: exactly-once delivery.

---

## PQ-020 — Self-watchdog records dead thread

**URS coverage**: URS-120.

**Pre-conditions**: Test environment only (do NOT do this in production).

**Steps**:
1. In a test build, deliberately raise an unhandled exception inside the workflow_scheduler loop body.
2. Confirm the thread dies.
3. Within 30 s, confirm an audit_log row appears: `action=watchdog_thread_died, details=thread=workflow-scheduler`.
4. Restart Prism; confirm everything recovers.

**Pass criteria**: dead thread observable, audit row written.

---

## PQ-021 — In-app compliance dashboard

**URS coverage**: complements URS-078 (audit trail), URS-082 (audit export), SOP catalogue from `16_SOP_CATALOGUE.md`.

This scenario exercises the in-app CSV / compliance UI (added during the Wave 6 post-audit work) end-to-end: feature-flag gating, dashboard rendering, SOP rendering with live-data substitution, execution recording, and audit-trail follow-through.

**Pre-conditions**:
- Operator account with RBAC-admin (or backup-admin) privileges.
- Prism running.

**Steps**:

1. **Feature flag off (default)**.
   - With `compliance.enabled` unset or `false`, visit `/compliance`. Expect 404.
   - Visit `/api/system/csv-status`. Expect 404.
   - Confirm the "Compliance" nav item is NOT in the sidebar.

2. **Enable the feature**.
   - As a config-admin, edit `config.json` to add `"compliance": {"enabled": true}` under settings (or POST via the settings endpoint).
   - Wait ≤ 5 s for `ConfigManager` mtime detection (or restart Prism).

3. **Dashboard renders**.
   - Visit `/compliance`. Expect the dashboard with three tiles (readiness, audit telemetry, findings register), then SOP cards, then a CSV-documentation index grouped by category.
   - Confirm the "Compliance" nav item now appears in the sidebar.

4. **Live data is fresh**.
   - The readiness tile shows "ATTENTION — N never executed" because no SOP has been run yet.
   - The audit-telemetry tile shows "✓ Audit subsystem healthy (0 failures)" because nothing has failed.
   - The findings tile shows the live count from `17_FINDINGS_AND_GAPS.md` ("OK — 30+2 of 32 disposed" if the register is in its post-audit state).

5. **Open a SOP**.
   - Click "SOP-05 — Validated-baseline review".
   - Expect the rendered markdown of `05_validated_baseline_review.md` with `[[csv:...]]` placeholders substituted by green pills showing live values (e.g., "Audit subsystem: No", "Last passing test count: 520", "Overall readiness: ATTENTION — 5 never executed").
   - Confirm the right-side panel shows status "Never executed" (red badge) + an empty execution history.

6. **Record an execution**.
   - In the right-side form, choose `result: pass`, type a note ("All checks confirmed clean"), submit.
   - Expect inline `✓ Recorded` confirmation.
   - The right-side status flips to "Current" (green); execution history shows one entry.
   - Reload the page (or the page auto-refreshes its data) — the live `[[csv:last_execution.SOP-05]]` placeholder in the rendered doc now shows today's timestamp.

7. **Audit-trail verification**.
   - Open `/api/audit-log?action=sop_execution_recorded` (via UI or curl with auth).
   - Expect one row: `username=<operator>`, `action=sop_execution_recorded`, `category=compliance`, `details=sop=SOP-05, result=pass, row_id=<N>`.
   - Confirm a corresponding row exists in the `sop_log` table.

8. **View raw markdown**.
   - Click "View raw markdown" in the right-side panel.
   - Expect a new tab opening `/api/sop/SOP-05/raw` showing the source `.md` file (with the literal `[[csv:KEY]]` placeholders intact).

9. **CSV doc browser**.
   - Back on `/compliance`, click "CSV-00 — CSV Readiness Report" under the "Reports" category.
   - Expect the full readiness report rendered in-app with live values for test count, finding counts, audit blind status, etc.
   - Click "View raw markdown" — confirm the source `.md` opens in a new tab.

10. **Feature flag off (rollback test)**.
    - Set `compliance.enabled = false`.
    - Wait ≤ 5 s for mtime detection.
    - Visit `/compliance` again. Expect 404.
    - Confirm the nav item disappears.
    - **Critically**: the `sop_log` and `audit_log` rows from step 6 must still be present (data survives the feature being toggled off — only the UI surface goes away).

11. **Hash-chain integrity**.
    - With the feature off, open a Python REPL and call `Database.verify_audit_chain()`.
    - Expect `{"ok": true, ...}` — the new audit rows from step 6 are part of the chain.

**Pass criteria**:
- All 11 steps complete with the expected outcome.
- Step 7 confirms the SOP execution wrote BOTH `sop_log` and `audit_log` rows.
- Step 10 confirms the feature flag truly gates the UI without affecting persisted data.
- Step 11 confirms the new audit rows are part of the hash-chained sequence.

**Evidence retained**:
- Screenshots of steps 3, 5, 6, 9 (the visible UI states).
- The `audit_log` row from step 7 (exportable via `/api/audit-log/export`).
- The `verify_audit_chain` output from step 11.

---

## PQ acceptance form

| PQ # | URS | Performed | Result | Evidence ref | Date |
|---|---|---|---|---|---|
| PQ-001 | URS-001 | | | | |
| PQ-002 | URS-002, URS-010 | | | | |
| PQ-003 | URS-011, URS-013 | | | | |
| PQ-004 | URS-004 | | | | |
| PQ-005 | URS-030 | | | | |
| PQ-006 | URS-033, URS-034 | | | | |
| PQ-007 | URS-037 | | | | |
| PQ-008 | URS-050–051–052 | | | | |
| PQ-009 | URS-053 | | | | |
| PQ-010 | URS-076 | | | | |
| PQ-011 | URS-078–081 | | | | |
| PQ-012 | URS-075 | | | | |
| PQ-013 | URS-094 | | | | |
| PQ-014 | URS-100, URS-101 | | | | |
| PQ-015 | URS-102 | | | | |
| PQ-016 | URS-122 | | | | |
| PQ-017 | URS-015 | | | | |
| PQ-018 | URS-016 | | | | |
| PQ-019 | URS-019 | | | | |
| PQ-020 | URS-120 | | | | |
| PQ-021 | URS-078, URS-082, SOP catalogue | | | | |

**Approval**:
- Performed by: ______________________ Date: ____________
- Reviewed by:   ______________________ Date: ____________
- Approved by:   ______________________ Date: ____________

---
*End of document.*
