# 12 — Audit Trail & 21 CFR Part 11 Evaluation

| Field | Value |
|---|---|
| Document ID | CSV-12 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `04_DS.md`, `11_DATA_INTEGRITY.md`, `appendix_D_db_schema.md` |

## Purpose

This document audits Prism's audit-trail implementation and assesses applicability of US 21 CFR Part 11 (Electronic Records / Electronic Signatures). Two parallel goals:

1. Demonstrate that the audit trail is **trustworthy** — captures everything regulated, is tamper-evident, is retrievable.
2. Determine which Part 11 controls apply to Prism and whether they are satisfied.

## A. Audit-trail design recap

`audit_log` table schema is in `appendix_D_db_schema.md`. Key properties:

| Property | Implementation |
|---|---|
| Append-only | SQLite triggers `audit_log_no_update` + `audit_log_no_delete` raise `ABORT` |
| Tamper-evident | SHA-256 hash chain (`prev_hash`, `row_hash`); `verify_audit_chain()` validates |
| Out-of-band mirror | Every successful insert appended to `data/audit_mirror.jsonl` |
| Forensic context | Each row carries `username`, `source_ip`, `session_id` (SHA-256(user+login_time)), `request_id` (per-request UUID) |
| Retention | **Unlimited** by default — never auto-purged; manual archive to `data/audit_archive/*.jsonl` |
| User-visible | `/api/audit-log` (paginated), `/api/audit-log/export` (CSV), `/api/audit-log/archive` (one-way snapshot) |

## B. Coverage of mutating actions

Per `appendix_B_api_surface.md` there are **54** distinct audit `action` values. They span:

- **Authentication**: (implicit — failed logins go to `auth_failures` + `events`; successful logins generate session_id used in downstream rows)
- **RBAC**: `rbac_grant`, `rbac_revoke`, `session_killed`, `user_disabled`, `user_enabled`
- **Approvals**: `approval_requested`, `approval_decided`, `tier0_approval_consumed`, `tier0_global_approval_consumed`
- **Workflows**: `create_workflow`, `update_workflow`, `delete_workflow`, `clone_workflow`, `execute_workflow`, `rbac_denied_workflow_execute`, `create_workflow_category`, `update_workflow_category`, `delete_workflow_category`
- **Runbooks**: `create_runbook`, `update_runbook`, `delete_runbook`, `execute_runbook`
- **Updates**: `install_updates`, `install_updates_direct`, `cancel_updates`, `auto_restart`
- **Power**: `power:restart`, `power:shutdown`, `power:wol`, `flask_restart`
- **Data lifecycle**: `clean_data`, `delete_all`, `factory_reset`, `delete_server_data`
- **Config**: `config_update`, `save_health_check_config`, `delete_health_check_config`
- **Tags**: `create_tag`, `update_tag`, `delete_tag`, `assign_tag`, `remove_tag`
- **Dependencies**: `add_dependency`, `remove_dependency`
- **Maintenance / Misc**: `vacuum_db`, `check_tls`, `delete_tls_cert`, `recalculate_baselines`, `reset_alert_scores`, `update_scheduled_restarts`, `restart_now`, `manual_snapshot`, `update_incident`
- **Archive**: `audit_archive`

**Completeness gap**: there is **no static-analysis test** that ensures every newly-added mutating endpoint also writes an audit row. This relies on developer discipline + code review.

**Finding F-078 (Critical)**: add a test that introspects Flask routes and verifies a `db.log_audit(...)` call exists in the handler for every state-changing route, OR a documented exception is recorded.

## C. Integrity verification

| Check | Method | Frequency |
|---|---|---|
| Hash chain | `Database.verify_audit_chain()` walks every row, recomputes hashes, compares | On demand (no scheduled run today) |
| JSONL mirror line count = `audit_log` row count | Manual inspection | On demand |
| Append-only enforcement | SQLite triggers (verified at IQ-009) | Every DB transaction |

**Recommendation**: schedule `verify_audit_chain()` as a daily periodic job; surface the result in `/api/system/health`. This becomes a finding (F-AT-1 Minor).

## D. Retention & archival

| Aspect | Current state | Note |
|---|---|---|
| `audit_log` retention | Unlimited (never deleted by `cleanup_old_data`) | Aligns with "preserve original" GxP principle |
| `audit_archive/*.jsonl` | Manual via `POST /api/audit-log/archive` | One-way snapshot; does NOT delete rows |
| `audit_mirror.jsonl` | Append-only | Grows forever; recommend OS-level log rotation that copies-not-truncates |

**Finding F-AT-2 (Minor)**: document a quarterly archival SOP — operator runs `archive`, verifies the snapshot, retains under controlled storage.

## E. 21 CFR Part 11 applicability

Part 11 applies to electronic records that are required by predicate rules + electronic signatures used in lieu of handwritten signatures. For Prism:

| Subpart | Requirement | Applicable? | Status |
|---|---|---|---|
| §11.10(a) Validation | "Validation of systems to ensure accuracy, reliability, consistent intended performance" | YES | This CSV package |
| §11.10(b) Output review | "Ability to generate accurate and complete copies of records" | YES | `/api/audit-log/export` (CSV); SIEM JSONL mirror |
| §11.10(c) Record protection | "Protection of records to enable their accurate and ready retrieval throughout the records retention period" | YES | Append-only triggers + hash chain + JSONL mirror |
| §11.10(d) Limited access | "Limiting system access to authorized individuals" | YES | RBAC + LDAP + lockout + session management |
| §11.10(e) Audit trail | "Computer-generated, time-stamped audit trails to independently record the date and time of operator entries and actions that create, modify, or delete electronic records" | YES | `audit_log` (every mutating action) |
| §11.10(f) Operational checks | "Operational system checks to enforce permitted sequencing of steps and events" | YES | Workflow engine state machine; install-state lifecycle |
| §11.10(g) Authority checks | "Authority checks to ensure that only authorized individuals can use the system" | YES | RBAC + tier-0 dual-control + approval tokens |
| §11.10(h) Device checks | "Device (e.g., terminal) checks" | Limited applicability | Source-IP captured in `audit_log`; no specific device fingerprint |
| §11.10(i) Personnel quals | (out of scope for the tool itself) | n/a | Covered by operator SOP, not the system |
| §11.10(j) Holding accountability | (out of scope) | n/a | Same |
| §11.10(k) Documentation | "Use of appropriate controls over systems documentation including distribution and revisions" | YES | Git history + this CSV package + change-control SOP (`14_CHANGE_CONTROL.md`) |
| **§11.50 Manifestations** | "Signed electronic records shall contain information associated with the signing" | **NOT APPLICABLE** | Prism does not implement electronic signatures (out of scope per URS-J) |
| **§11.70 Signature/record linking** | "Electronic signatures and handwritten signatures executed to electronic records shall be linked to their respective electronic records" | **NOT APPLICABLE** | Same |
| **§11.200/300 Electronic-signature controls** | Various | **NOT APPLICABLE** | Same |

### What Part 11 controls would Prism need to grow to support electronic signatures?

If the organisation ever wants Prism to support electronic-signature workflows (e.g. "this restart was authorised by signed approval from Dr. X"), it would need:
- Two-factor authentication (or equivalent) for the signature event.
- A `signatures` table with `signed_by`, `signed_at`, `meaning` ("Approved"), and a cryptographic binding to the record being signed.
- A non-repudiation property: the user cannot deny they signed.
- A printable / exportable form that shows the signature meaning.

None of this is currently in scope. The current dual-control approval workflow (`pending_approvals`) is functionally similar to a 2-person signature but is **not a Part 11 signature** because the consumed-approval token is the only artefact and it doesn't carry the "signature meaning" field.

## F. Findings

| ID | Severity | Description |
|---|---|---|
| F-075 | Critical | No static-analysis enforcement of universal RBAC (also in Risk doc) |
| F-078 | Critical | No static-analysis enforcement of universal audit logging |
| F-AT-1 | Minor | `verify_audit_chain()` not run on a schedule; should surface in `/api/system/health` |
| F-AT-2 | Minor | Quarterly archival SOP undocumented |
| F-AT-3 | Observation | `audit_mirror.jsonl` rotation policy undocumented |

All findings carried to `17_FINDINGS_AND_GAPS.md`.

## G. Compliance posture summary

| Part 11 area | Status |
|---|---|
| Validation (§11.10(a)) | **In progress** (this CSV package) |
| Record protection (§11.10(c)) | **Strong** |
| Audit trail (§11.10(e)) | **Strong** (modulo F-078) |
| Limited access (§11.10(d), §11.10(g)) | **Strong** (modulo F-075) |
| Output review (§11.10(b)) | **OK** |
| Operational checks (§11.10(f)) | **OK** |
| Documentation (§11.10(k)) | **OK** (this package + git) |
| Electronic signatures (§11.50/70/200/300) | **N/A** (deferred) |

Prism is **defensibly close to Part 11 §11.10 compliance for electronic records**; remediating F-075 and F-078 closes the most material gaps. Electronic signatures are explicitly out of scope.

---
*End of document.*
