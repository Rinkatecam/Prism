# 11 — Data Integrity Audit (ALCOA+)

| Field | Value |
|---|---|
| Document ID | CSV-11 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `04_DS.md`, `appendix_D_db_schema.md` |

## Purpose

This document evaluates Prism's regulated-data outputs against the ALCOA+ data-integrity framework (MHRA + WHO + FDA). Each of the 9 attributes is checked against Prism's data flows and persistence layer; gaps become findings in `17_FINDINGS_AND_GAPS.md`.

## ALCOA+ at a glance

| Letter | Attribute | Question |
|---|---|---|
| **A** | Attributable | Can each record be traced to the user who created/changed it? |
| **L** | Legible | Are records readable for the full retention period? |
| **C** | Contemporaneous | Were they recorded at the moment of the action? |
| **O** | Original | Is the first capture preserved (no overwrite of source)? |
| **A** | Accurate | Are values correct, free of errors? |
| **+ C** | Complete | Nothing missing? |
| **+ C** | Consistent | Format / units / chronology stable? |
| **+ E** | Enduring | Stored on durable, controlled media? |
| **+ A** | Available | Retrievable when required? |

## Scope of "regulated data" in Prism

Per the scoping doc (CSV-01), Prism is GxP-**adjacent**: its data is operational monitoring + administrative actions. The regulated subset is:

1. **`audit_log`** rows — every user-initiated mutating action.
2. **`workflow_executions` + `workflow_execution_steps`** — record of automated remediation runs.
3. **`runbook_executions`** — record of manual remediation runs.
4. **`restart_log`** — record of scheduled + manual restarts.
5. **`events`** — system-generated incident records (threshold breaches, anomalies, status transitions).
6. **`failed_logins`** — security-relevant records.
7. **`config_snapshots` + `config_changes`** — what changed on a regulated host.
8. **`pending_approvals`** + their consumed/audit traces — dual-control evidence.

The full DB schema is in `appendix_D_db_schema.md`.

## A — Attributable

| Data | How attributed | Status |
|---|---|---|
| `audit_log.username` | Auto-filled from `session['username']` in `log_audit`; `'system'` default for collector-driven rows | **OK** |
| `audit_log.source_ip` | Auto-filled from `request.remote_addr` | **OK** |
| `audit_log.session_id` | SHA-256(`username + login_time`) — stable, doesn't expose credentials | **OK** |
| `audit_log.request_id` | UUID set in `before_request`; ties the action to its HTTP request | **OK** |
| `workflow_executions.executed_by` | Set to `session['username']` when executing | **OK** |
| `runbook_executions.executed_by` | Same | **OK** |
| `restart_log` rows | No `executed_by` column (action initiator not captured directly on the restart row) | **MINOR — Finding F-A-1**: add `actor` column to `restart_log` |
| `events` rows (collector-generated) | No user attribution (these are not user-initiated) | OK by design |
| `failed_logins` rows | `account_name` is the *attempting* user, `source_ip` the source | **OK** |
| `config_snapshots` | No `taken_by` (it's automated) | OK by design |

## L — Legible

| Aspect | Status |
|---|---|
| Text columns are UTF-8 (SQLite default) | **OK** |
| Timestamps are ISO-8601 strings, human-readable | **OK** |
| JSON-encoded blobs (`canvas_json`, `payload_json`, `data_json`, `details` when JSON-shaped) round-trip without corruption | **OK** |
| Workflow/runbook outputs are capped at 20 KB success / 5 KB error to prevent unreadable wall-of-text rows | **OK** (audit decision documented; caps don't lose attribution context) |
| Audit-log `details` field truncated to 500 chars at insert | **OK** but warrants documentation: any payload longer than 500 chars is truncated — caller is responsible for putting the salient info first |

## C — Contemporaneous

| Aspect | Status |
|---|---|
| All `timestamp` columns default to `strftime('%Y-%m-%dT%H:%M:%SZ','now')` — written by the same SQLite statement as the row insert | **OK** |
| `log_audit` is called inside the same request thread that performs the action | **OK** |
| Server clock is NTP-synchronised (IQ-013 verifies this) | **OK** at install |
| No code path back-dates a record | **OK** by inspection |

## O — Original

| Aspect | Status |
|---|---|
| `audit_log` is append-only (DB triggers) — original record cannot be UPDATEd or DELETEd | **OK** |
| `metrics` / `events` rows are inserted per cycle; never overwritten | **OK** |
| `workflow_execution_steps` per-node rows preserved | **OK** |
| Mutable tables (`incidents`, `tls_certificates`, `metric_baselines`) overwrite in place by design — the operator-visible state is intended to reflect current truth, not historical detail. **The events feeding them remain in `events` as the historical source.** | **OK** with documentation |
| `audit_mirror.jsonl` is an append-only OS-level mirror; if `prism.db` is tampered with at file-system level, the mirror catches it | **OK** |

## A — Accurate

| Aspect | Status |
|---|---|
| Threshold computations are pure functions over input metrics → deterministic | **OK** |
| WinRM round-trip carries no implicit conversion (PowerShell emits JSON, Prism parses) | **OK** |
| Status classification has 352 tests (most via aggregator + supervisor + worker suites) | partial — see **Finding F-002** for the missing direct `compute_status` tests |
| `verify_audit_chain()` detects any out-of-process tampering of `audit_log` rows | **OK** |
| Hash chain on `audit_log` makes ANY post-fact alteration detectable | **OK** |

## + Complete

| Aspect | Status |
|---|---|
| Every state-changing endpoint should call `db.log_audit()` | **GAP — Finding F-078**: no static-analysis test enforces this; relies on developer discipline. Risk class: Critical. |
| Every mutation endpoint should have an auth decorator | **GAP — Finding F-075**: same shape; risk class Critical. |
| Workflow execution captures every node's outcome (success / failed / skipped) | **OK** — `_execute_graph` writes one row per node |
| Sandbox-rejected workflows still get a `workflow_execution_steps.status='failed'` row + reason | **OK** |
| Failed `log_audit` insert: log_audit's caller does not see an exception in normal flow (it's logged via `logger.warning`), but the JSONL mirror also fails — net effect: 0 rows in `audit_log`, 0 lines in mirror. A latent DB-down condition would silently lose audit. | **GAP — Finding F-D-1 (Minor)**: consider raising on `log_audit` failure to make missing-audit visible. Alternative: ringbuffer + retry. |

## + Consistent

| Aspect | Status |
|---|---|
| Single timestamp format across all tables (ISO-8601 UTC `Z`) | **OK** |
| Username convention: lower-cased throughout (`auth.py` normalises) | **OK** |
| Action verb vocabulary is enumerated and stable (54 actions) — see Appendix B | **OK** |
| `event_type` values are an implicit enum (`threshold_breach`, `baseline_deviation`, `failed_login`, `status_change`, `resolved`, `critical`, `warning`, `offline`) | **OK** but no DB-level constraint; recommend documenting in a single place (already in `appendix_D_db_schema.md`) |
| `install_state.status` values are an implicit enum (`installing`, `restart_required`, `completed`, `failed`, `rebooting`, `stabilising`) | **OK** |
| Severity vocabulary (`info`, `warning`, `critical`) stable | **OK** |
| `request_id` UUIDs are uniformly generated | **OK** |

## + Enduring

| Aspect | Status |
|---|---|
| SQLite WAL mode + 5 s busy timeout | **OK** |
| Backup procedure documented + tested (`tools/backup.py`, `test_backup_tool.py`) | **OK** |
| Backups cover: `prism.db`, `config.json`, `install_state.json`, key file | **OK** |
| Backup encryption at rest | **Out of Prism scope**; depends on host disk encryption — call out in SOPs |
| Retention period of 30 days for monitoring data | **OK** but `audit_log` is unbounded; recommend defining an archival cadence (already covered by Phase 11 SOP catalogue) |
| `audit_archive/*.jsonl` files for aged audit material | **OK** — already implemented; SOP needs to document the operator's review of archive integrity |

## + Available

| Aspect | Status |
|---|---|
| Audit-log UI in the app (`/api/audit-log` route + view) | **OK** |
| CSV export (`/api/audit-log/export`) | **OK** |
| Direct SQL access by an authorised admin (read-only) | **OK** by design |
| 99.x % uptime target | not formally tracked; recommend defining in operations SOP |

## Findings rolled up

| ID | Severity | Description | Cross-reference |
|---|---|---|---|
| F-002 | High | No direct unit tests for `compute_status` | Risk register, OQ inventory |
| F-075 | Critical | No static-analysis test that all mutating routes are auth-gated | Risk register, FS-075 |
| F-078 | Critical | No static-analysis test that all mutating routes write an audit row | Risk register, FS-078 |
| F-A-1 | Minor | `restart_log` has no operator-attributable column | this doc |
| F-D-1 | Minor | `log_audit` insert failure is silent-by-default | this doc |
| F-D-2 | Minor | `audit_log.details` truncation to 500 chars not documented in caller convention | this doc |

All findings carried to `17_FINDINGS_AND_GAPS.md`.

## Conclusion

Prism's data-integrity posture is **strong**:
- Append-only audit log with hash-chain integrity verification.
- JSONL mirror provides out-of-band evidence.
- Forensic context (IP, session, request UUID) captured automatically.
- ISO-8601 UTC timestamps consistently.

The main gaps are **structural/process** rather than format/content:
- Static-analysis enforcement that audit + RBAC are universally applied (F-075, F-078).
- Operator attribution on `restart_log` (Minor).
- Soft-failure semantics of `log_audit` itself (Minor).

These are tracked in `17_FINDINGS_AND_GAPS.md` for remediation.

---
*End of document.*
