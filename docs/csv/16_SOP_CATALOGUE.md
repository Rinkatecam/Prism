# 16 — Operational SOP Catalogue

| Field | Value |
|---|---|
| Document ID | CSV-16 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `13_SECURITY.md`, `14_CHANGE_CONTROL.md`, `15_BACKUP_RECOVERY.md` |

## Purpose

Catalogue the Standard Operating Procedures required for a GAMP 5 / regulated-environment deployment of Prism. Each SOP entry shows: scope, current state (existing doc / gap), and the recommended evidence retention. Existing docs in `docs/` are cited; gaps are flagged as observations in `17_FINDINGS_AND_GAPS.md`.

## SOP catalogue

### SOP-001 — User onboarding

**Scope**: granting a new operator access to Prism.
**Procedure summary**:
1. Verify operator's AD account exists; confirm group memberships.
2. If `auth.enabled=true`, no further app action — operator can log in.
3. As RBAC-admin, grant per-server permissions via `/api/rbac/grant` according to operator's role:
   - `view` — read-only.
   - `control` — restart/WOL allowed (non-tier-0).
   - `admin` — install updates, factory-relevant ops; tier-0 servers require dual-control still.
4. Record the grant in the change log; the route already writes an `audit_log` row (`rbac_grant`).
**Existing doc**: implicit in `routes/api/rbac.py` + `docs/SECURITY.md`.
**Gap**: no standalone SOP doc; **finding F-SOP-1 (Minor)** — promote into `docs/SOPs/user_onboarding.md`.

### SOP-002 — User offboarding

**Scope**: removing an operator's access.
**Procedure summary**:
1. As RBAC-admin, `POST /api/admin/disable-user` — blocks future logins.
2. `POST /api/admin/kill-session` for any active session.
3. `POST /api/rbac/revoke` for every per-server ACL row.
4. Verify with `GET /api/rbac/acl` that no rows remain for that username.
5. Audit rows are auto-written.
**Gap**: same — promote to `docs/SOPs/user_offboarding.md`.

### SOP-003 — Periodic ACL review

**Scope**: quarterly review of who has what permission.
**Procedure**:
1. `GET /api/rbac/acl` — export to CSV.
2. Walk each row with the service owner; confirm continued business need.
3. Revoke any stale row.
4. File the review report (date, reviewer, decisions).
**Cadence**: quarterly.
**Gap**: process not yet codified; recommend SOP doc + calendar reminder.

### SOP-004 — Sandbox-allowlist change

**Scope**: adding or removing a cmdlet from `DEFAULT_ALLOWED_CMDLETS` in `ps_sandbox.py`.
**Procedure**: per `14_CHANGE_CONTROL.md §G` — 2-reviewer PR, justification, sandbox-doc update.
**Existing doc**: `docs/WORKFLOW_SANDBOX.md` (operator-facing description; should add a change-log section).

### SOP-005 — Key rotation

**Scope**: rotating the Fernet key that wraps stored credentials.
**Existing doc**: `docs/KEY_ROTATION.md` and `docs/SECRET_KEY_ROTATION.md`.
**Procedure**: stop Prism → `python tools/rekey.py` → start Prism → verify with `/api/test-connection` on a sample server.
**Cadence**: at least annually; immediately on suspected compromise.

### SOP-006 — Backup operation

**Existing doc**: `docs/BACKUP_AND_RESTORE.md`.
**Procedure**: see `15_BACKUP_RECOVERY.md §B`.
**Cadence**: daily via Windows Scheduled Task.

### SOP-007 — Disaster recovery / restore test

**Procedure**: quarterly DR drill — restore latest backup into a staging instance, run IQ-006..IQ-013, run PQ-001, PQ-005, PQ-011.
**Gap**: not yet codified — finding F-BR-2 in `15_BACKUP_RECOVERY.md`.

### SOP-008 — Incident response

**Scope**: how the IT team responds to a Prism-detected outage / anomaly / failed-login spike.
**Procedure summary**:
1. Triage from dashboard.
2. Acknowledge anomalies via UI.
3. Run the relevant runbook or workflow.
4. If destructive (restart, install), confirm via dual-control if tier-0.
5. Record root-cause analysis in the incident's `resolution_notes` field (`PUT /api/incidents/<id>`).
**Existing doc**: implicit in operations training; recommend documenting.

### SOP-009 — Periodic review of the validated baseline

**Scope**: confirm Prism is still operating in its qualified state.
**Procedure**: monthly health check —
1. `verify_audit_chain()` — confirm `ok: true`.
2. `GET /api/system/health` — confirm all subsystems healthy.
3. Re-run pytest in the deployed environment — confirm 352 passing.
4. Compare current `requirements.lock` and code commit to the validated baseline (recorded at last IQ).
5. File a 1-page review.
**Gap**: process not codified — recommend SOP doc.

### SOP-010 — Patch / dependency update

**Existing doc**: `docs/DEPENDENCIES.md`.
**Procedure**: per `14_CHANGE_CONTROL.md §B`.

### SOP-011 — Audit-log archival

**Scope**: aged audit material moved to cold storage.
**Procedure**:
1. `POST /api/audit-log/archive?before=<date>` — produces a JSONL snapshot.
2. Verify SHA-256 of snapshot file.
3. Move file to immutable cold storage.
4. **Do NOT delete the source rows from `audit_log`** — the archive is one-way snapshot, source remains live.
**Cadence**: quarterly.
**Gap**: F-AT-2 — codify in an SOP doc.

### SOP-012 — Sandbox allowlist + free-form PowerShell governance

**Scope**: who can use `Run PowerShell` block in workflows.
**Procedure**:
1. By default, free-form PS is gated by `_require_auth` AND requires `admin` permission on the target.
2. The sandbox is `enabled=true` by default; if an admin sets `workflows.sandbox.enabled=false`, that is an audited `config_update`.
3. Operators should NOT have `admin` on production tier-0 servers without dual-control.
4. Periodic review (quarterly): grep `workflow_execution_steps` for `run_powershell` executions; spot-check their scripts.
**Gap**: codify as `docs/SOPs/powershell_governance.md`.

### SOP-013 — Pulse / heartbeat anomaly investigation

**Scope**: what to do when the topbar pulse turns amber/red.
**Procedure**:
1. Click the pulse widget for details (in-flight checks, silent servers, subsystem heartbeats).
2. If a single subsystem heartbeat is stale — restart Prism (`POST /api/restart` if RBAC-admin) and capture audit-log evidence.
3. If many silent servers — investigate WinRM / network.
4. If sustained, escalate to L2 with screenshots + `/api/system/health` JSON.

### SOP-014 — Maintenance window scheduling

**Scope**: how to declare a planned maintenance window.
**Procedure**:
1. As RBAC-admin, `POST /api/maintenance-windows` with the schedule (servers, days, hours, threshold override).
2. Verify via dashboard that the window is active during the expected window.
3. Track in the change log.

### SOP-015 — Tier-0 dual-control workflow

**Scope**: how a destructive action on a tier-0 server gets executed.
**Procedure**:
1. Admin A: attempt action → system creates pending approval; UI shows "approval required".
2. Admin B: `GET /api/approvals` → review → `POST /api/approvals/<id>/decide`.
3. Admin A: retry action → executes; approval token consumed.
4. Audit chain: `approval_requested` → `approval_decided` → `tier0_approval_consumed` → the actual action (e.g. `power:restart`).
5. Per CFR §11.10(g), this provides "authority checks".

## SOP retention & control

Each SOP should be a separately versioned document in `docs/SOPs/`. Recommended:
- Version on the doc.
- Effective-from date.
- Owner.
- Review cadence.
- Sign-off page.

## Gap summary

| Finding | Severity | What's missing |
|---|---|---|
| F-SOP-1 | Minor | Standalone user onboarding/offboarding SOPs |
| F-SOP-2 | Minor | Periodic ACL review SOP |
| F-SOP-3 | Minor | Disaster-recovery test SOP (F-BR-2 in backup doc) |
| F-SOP-4 | Minor | Incident-response SOP |
| F-SOP-5 | Minor | Monthly validated-baseline review SOP |
| F-SOP-6 | Minor | Audit-log archival SOP (F-AT-2 in audit doc) |
| F-SOP-7 | Minor | PowerShell governance SOP |

All findings carried to `17_FINDINGS_AND_GAPS.md`.

---
*End of document.*
