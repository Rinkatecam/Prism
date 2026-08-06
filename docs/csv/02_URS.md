# 02 — User Requirements Specification (URS)

| Field | Value |
|---|---|
| Document ID | CSV-02 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parent | `01_scope_and_categorisation.md` |

## How to read this document

Each URS item is identified by **URS-NNN** (zero-padded, never re-used). The form is:

> **URS-NNN — Title.** Plain-English statement of what an operator needs the system to do, *not how*. Optional acceptance hint.

Items are grouped by functional area. The traceability matrix in `10_TRACEABILITY_MATRIX.md` links every URS-ID downstream through FS / DS / Risk / Tests.

When a requirement explicitly stems from a regulatory expectation we cite it inline (e.g. *21 CFR Part 11 §11.10(e)* for audit-trail durability).

## A. Monitoring & dashboard

### URS-001 — Continuous fleet monitoring
The operator needs to see, in near-real-time, the operational status of every Windows server in the fleet. The system must collect CPU, RAM, disk-C, and disk-D utilisation on a configurable cadence and surface the result on the dashboard. Acceptance: the dashboard tile for a healthy server updates at least once every 60 s by default.

### URS-002 — Status classification
Each server must be classified into a single visible status — **healthy**, **warning**, **critical**, or **offline** — using configurable thresholds. The classification must be deterministic given the metric input + threshold settings.

### URS-003 — Sustained-spike gating
Brief transient CPU spikes (e.g. one sample at 95 % during a backup script) must NOT immediately push a server into "warning" if it would generate alert noise. The operator can configure an N-of-M consecutive-cycles smoothing gate.

### URS-004 — Threshold loosening in maintenance windows
During a configured maintenance window (per-server, per-weekday, per-time-range), the operator may loosen alert thresholds or fully suppress alerts so planned work does not flood the alert channel.

### URS-005 — Historical metric retention
The operator can view a server's CPU / RAM / disk history for at least the previous 30 days. Retention is configurable; data older than the retention period is automatically purged.

### URS-006 — Topology view
The operator can view a graphical map of server dependencies (which server consumes which other server's services) and can interactively pan/zoom + see the blast radius if a given server goes down.

### URS-007 — Per-server detail page
Clicking a server tile opens a detail page with: real-time metrics, 24 h history chart, Windows event-log excerpts, security posture, anomaly list, capacity forecast, and pending Windows updates.

### URS-008 — Pulse / heartbeat indicator
The operator can see at a glance whether the collector itself is healthy — how many servers are up, whether the aggregator/supervisor/workers are alive, and a live activity strip ("ECG") that beats as results land.

## B. Alerting

### URS-010 — Threshold-based alerting
When a server transitions to a worse status (e.g. healthy → warning, warning → critical), the system must emit an alert through every configured channel (event log, email, Teams webhook).

### URS-011 — Anomaly detection
The operator can opt into per-metric statistical anomaly detection (baseline + standard deviation) that fires alerts when current values deviate beyond a configurable σ from the historical baseline.

### URS-012 — Baseline deviation alerts
The operator can opt into N-of-M sustained baseline deviation (e.g. "RAM has been > 2σ above baseline for 3 of the last 5 cycles") to surface gradual drift that thresholds miss.

### URS-013 — Anomaly acknowledgement & snooze
For each anomaly the operator can acknowledge (suppress for the rest of the incident) or snooze (suppress for N days) the alert with optional free-text notes. Acknowledgement does not affect data collection, only alert dispatch.

### URS-014 — Alert fatigue throttling
Repeatedly-firing alerts of the same kind on the same server are de-prioritised (fatigue score) so the operator's attention is not exhausted by known-noisy alerts. The operator can reset scores at any time.

### URS-015 — Failed-login monitoring
The system collects Windows Event ID 4625 (failed logon) and 4740 (account lockout) and alerts the operator when failure rates exceed a configurable threshold.

### URS-016 — TLS certificate expiry alerts
The system tracks TLS certificates on configured hosts/ports and alerts the operator when a certificate is within warning (default 30 d) or critical (default 7 d) of expiry.

### URS-017 — Health-check probes
The operator can configure TCP / HTTP / HTTPS / UDP / ICMP health-check probes (e.g. "is port 443 on app-server-01 responding?") and receive alerts on probe state changes (up ↔ down).

### URS-018 — Config drift detection
The operator can opt into periodic comparison of server configuration snapshots (services, hotfixes, local admins, scheduled tasks) and receive alerts on detected changes.

### URS-019 — Daily / weekly health digest
The system can deliver a scheduled PDF / e-mail digest summarising fleet health, recent incidents, capacity forecasts, etc.

### URS-020 — Incident correlation
When multiple related alerts fire across the fleet, the system groups them into a single incident record with shared `correlation_id` so the operator triages one entity, not N.

## C. Operations

### URS-030 — Manual server restart
An authenticated operator can restart a server from the UI. The action must be auditable (who/when/what) and the dashboard must show a "rebooting → stabilising → ready" lifecycle visible to other operators.

### URS-031 — Scheduled server restart
The operator can configure a recurring schedule (daily / weekly / monthly at HH:MM) for one or more servers, with optional pre-restart actions and post-restart validation (e.g. "wait until TCP 3389 is responding").

### URS-032 — Wake-on-LAN
For servers configured with a MAC address, the operator can send a magic packet to wake them.

### URS-033 — Windows-update install lifecycle
The operator can initiate a Windows-update install on a server. The system manages the lifecycle (queued → searching → downloading → installing → restart_required → completed / failed) and exposes live progress.

### URS-034 — Auto-restart after update install
At install kickoff, the operator can opt to have the server automatically restarted once Windows reports `restart_required`. The auto-restart watcher must survive a Prism process restart (i.e. resume polling on boot).

### URS-035 — Cancel in-flight update install
The operator can cancel an in-flight install; the scheduled task on the target must be torn down.

### URS-036 — Update status surfaced from target
The dashboard must show the live update status read from the target (not just the Prism-side cache), and refresh on an event-driven cadence so an install completing is visible within seconds.

### URS-037 — Recovery from stuck install state
If a server's `restart_required` flag persists despite reboots (e.g. the install script's stale `update-status.json` is left over), the system must detect this on the next successful `Microsoft.Update.SystemInfo.RebootRequired = false` check and auto-clear the stale state. *Added during this audit's scope, see `collector_v2/aggregator.py:_handle_updates_result`.*

### URS-038 — Stuck-state janitor
If a server gets stuck in `rebooting` or `stabilising` for longer than 20 min (e.g. server never comes back, came back briefly then died), the system must self-clean and let the normal offline/stale badge take over. *Added during this audit's scope, see `collector_v2/periodics._reboot_state_janitor`.*

### URS-039 — Service / process / port live picker
Where a UI form asks for a service / process name / port, the operator can browse a live list of what's actually running on the chosen server (no typing from memory).

### URS-040 — Runbook execution
The operator can execute a pre-defined runbook (a sequence of WinRM commands) against a server and see step-by-step output. Runbook execution is audited.

## D. Workflow automation

### URS-050 — Visual workflow editor
The operator can compose automation workflows by dragging blocks (checks, actions, flow control, notifications, triggers) onto a canvas and connecting them with wires.

### URS-051 — Workflow trigger types
A workflow can be triggered three ways: **Manual** (operator clicks Run), **Schedule** (daily / weekly / monthly), or **Event** (a condition on a server transitions from false to true — e.g. "Spooler service stopped").

### URS-052 — Edge-triggered event firing
Event triggers fire on the *transition* (false → true) only, not on every poll. A chronically-bad condition does not re-fire every cycle.

### URS-053 — PowerShell sandbox
User-authored PowerShell embedded in workflow blocks (`Run PowerShell`, `Condition`) is validated against a default-deny allowlist of cmdlets + a HARD_DENY pattern list before execution. The sandbox can be disabled by an admin, but the default is enabled.

### URS-054 — Parameter binding for structured fields
For service / process / port name fields, the user input is bound as a typed `[string]` parameter to the PowerShell, never concatenated into the script text (RCE prevention).

### URS-055 — Variable substitution between blocks
A downstream notification (email, webhook, log) can include the output of an upstream block via `{{step.<id>.output}}`, `{{step.<id>.success}}`, `{{step.<id>.error}}`, and `{{workflow.name}}` / `{{workflow.id}}` macros.

### URS-056 — Branch-aware outputs
Check blocks have two outputs (success / fail); the connection line is visually coloured (green / red) to make the branch obvious.

### URS-057 — Block enable / disable
The operator can disable a block or a connection without deleting it; the workflow executor skips disabled elements. Useful for parking part of a workflow temporarily.

### URS-058 — Multi-select group operations
The operator can ctrl-click or marquee-select multiple blocks and act on them as a group (delete, enable/disable, clone, arrow-nudge, drag together).

### URS-059 — Right-click context menu
The operator can right-click any block or connection to see actions (enable/disable, clone, delete) without opening the properties panel.

### URS-060 — Workflow execution audit trail
Every workflow execution writes one `workflow_execution` row and one `workflow_execution_step` row per node, capturing executor identity, trigger source, status, duration, output (capped), and any error message.

### URS-061 — Workflow categorisation and tagging
Workflows can be assigned to a colour-coded category for visual filtering. Categories are user-defined; built-in categories cannot be deleted.

### URS-062 — Template library
Built-in workflow templates exist for common patterns (service recovery, port health monitor, restart with validation, process watchdog). Templates can be cloned but the originals are preserved across factory-reset.

## E. Authentication, authorisation, and audit

### URS-070 — Authentication
The system can be configured to require authentication. Two providers are supported: an LDAP/AD bind and a local "backup admin" account stored as a werkzeug hash in the config file.

### URS-071 — Strong backup-admin password policy
The backup-admin password must be ≥ 12 characters, contain ≥ 1 digit and ≥ 1 symbol, and not be in the common-password list.

### URS-072 — Account lockout
Repeated failed login attempts for the same username trigger a temporary lockout (configurable threshold + window + duration).

### URS-073 — Session timeout
Idle sessions expire after a configurable timeout (default 8 h, or 30 d for remember-me; backup-admin enforces a 15-min idle floor regardless of remember-me).

### URS-074 — Forced session termination
A backup-admin (or RBAC-admin) can revoke an active session (`/api/admin/kill-session`) or disable a user account (`/api/admin/disable-user`). Effect must take hold on the next request, not requiring server restart.

### URS-075 — Per-server RBAC
Each authenticated user can be granted **view**, **control**, or **admin** permission on a specific server (or `*` wildcard). When the ACL table is empty, behaviour is permissive (legacy compat); once any row exists, enforcement engages.

### URS-076 — Tier-0 dual-control
For tier-0 (production-most-critical) servers, destructive operations (restart, install_updates, runbook) require a second admin's approval before executing. Approvals expire after 1 h. The consumed approval token is recorded in the audit log.

### URS-077 — Global destructive-action approval
Fleet-wide destructive operations (`/api/data/delete`, `/api/data/factory-reset`) require a server_name=`*` approval that is single-use and audit-logged.

### URS-078 — Audit-log capture
Every user-initiated mutating action writes one row to `audit_log`: `(timestamp, username, action, category, details, source_ip, session_id, request_id)`. The list of audit-bearing actions is enumerated in Appendix B.

### URS-079 — Audit-log durability (append-only)
`audit_log` rows cannot be `UPDATE`d or `DELETE`d (enforced by SQLite triggers). This matches *21 CFR Part 11 §11.10(e)* on accurate, complete, and unalterable records.

### URS-080 — Audit-log integrity (hash chain)
Each `audit_log` row carries a SHA-256 hash linking it to the previous row. The chain can be verified end-to-end (`Database.verify_audit_chain()`); any tampering, deletion, or insertion outside the trigger path is detectable.

### URS-081 — Audit-log mirror to SIEM
Each successful insert is also appended to `data/audit_mirror.jsonl` for out-of-band ingest by an external SIEM. The mirror is the second line of defence if the SQLite DB is tampered with at the file level.

### URS-082 — Audit-log export & archive
Backup-admin / RBAC-admin can export the audit log as CSV (`/api/audit-log/export`) and archive aged rows to a JSONL file under `data/audit_archive/`. Archival is one-way; rows in `audit_log` are never deleted.

### URS-083 — User-visible audit log
Authenticated operators can browse the audit log in the UI (`/api/audit-log`), filtered by user / action / time range.

## F. Data integrity & ALCOA+ alignment

### URS-090 — ISO-8601 UTC timestamps
All persisted timestamps are written as ISO-8601 with `Z` suffix (UTC). The display layer converts to the operator's configured timezone via `zoneinfo`.

### URS-091 — Originating-user attribution
Every operator-initiated record persists the username; system-initiated records use `'system'`. (Attributable / A in ALCOA+.)

### URS-092 — Contemporaneous capture
Timestamps are written by the same code path that performs the action, not retroactively reconstructed. (Contemporaneous / C in ALCOA+.)

### URS-093 — Preservation through restart
Operator-visible state (audit_log, install_state, settings, baselines) survives a Prism process restart. In particular, `install_state.json` is persisted on every mutation and reloaded on boot.

### URS-094 — Backup / restore
The operator can produce a backup of `prism.db`, `config.json`, key files, and `install_state.json` via the `tools/backup.py` utility, and restore from such a backup via `tools/restore.py`. Procedure documented in `docs/BACKUP_AND_RESTORE.md`.

### URS-095 — Secret-key rotation
Encrypted password material in `config.json` can be re-keyed with the `tools/rekey.py` utility without operator data loss. See `docs/KEY_ROTATION.md`.

## G. Localisation & accessibility

### URS-100 — Five-language UI
All operator-facing strings have translations in English, German, French, Spanish, and Japanese. The active language is a setting; fallback is English.

### URS-101 — Operator timezone
All displayed timestamps render in the operator's configured timezone (`settings.timezone`, e.g. `Europe/Berlin`); the DB stores UTC. (Set via `formatTs()` in JS and `fmt_ts()` in Jinja.)

### URS-102 — Reduced motion
For operators with `prefers-reduced-motion`, animated UI elements (e.g. the ECG pulse strip) are hidden and replaced by a static indicator.

## H. Security controls (deferred to dedicated security review)

### URS-110 — CSRF protection
All state-changing endpoints require a valid CSRF token from the same-session form (`Flask-WTF`).

### URS-111 — Content Security Policy
Pages set a strict CSP via response headers; a per-request nonce is injected into inline scripts so they can run while opaque scripts cannot. (Pre-existing CSP test failures unrelated to GxP — see Appendix E.)

### URS-112 — Password masking
Passwords are masked in API responses (`/api/config` returns `********`), and the POST round-trip detects the mask sentinel and preserves the stored encrypted value.

### URS-113 — HTTPS downgrade protection
A server already configured for HTTPS cannot be downgraded to plaintext WinRM unless an RBAC-admin explicitly authorises it (S3-1 from prior audit).

### URS-114 — Tier-0 skip-verify block
TLS certificate verification cannot be disabled on tier-0 servers (S3-12).

### URS-115 — LDAP startup safety
If LDAP authentication is enabled but the LDAP server is unreachable at startup, Prism refuses to boot rather than locking out all operators.

### URS-116 — Deterministic dependency closure
The Python dependency closure is pinned with cryptographic hashes (`requirements.lock`) and installed via `pip install --require-hashes`. Tested in `tests/test_supply_chain.py`.

## I. Operational lifecycle

### URS-120 — Self-watchdog
The Prism process monitors its own background threads (restart_scheduler, workflow_scheduler, v2 collector pipeline, workflow scheduler) and writes an `audit_log` row + critical-error counter if any of them die.

### URS-121 — Health endpoint
`GET /api/system/health` returns a full snapshot of subsystem heartbeats, queue depths, tracked servers, and database health — suitable for an external uptime monitor to poll.

### URS-122 — Graceful degradation
If WinRM to a target fails, Prism marks that server `offline` and continues polling other servers on the normal cadence. A single bad target does not block the fleet.

### URS-123 — Configuration hot-reload
The operator can edit `config.json` / settings via the UI; new settings take effect within one supervisor tick (≤ 5 s for cadences, immediately for thresholds).

### URS-124 — Factory reset (destructive)
A backup-admin (with global approval token) can wipe all monitoring data + servers + config snapshots. Built-in workflow templates and built-in runbooks are preserved across factory-reset.

## J. Out-of-scope (deferred)

These were considered but explicitly *not* part of v1 GAMP scope:

- Electronic signatures on individual operator actions (only audit trail).
- Mobile push notifications (alerts go to email + Teams webhook only).
- Multi-tenancy / customer isolation (single-tenant deployment).
- Off-the-shelf SCIM / OAuth2 integration (LDAP-only).
- Backup encryption-at-rest beyond filesystem-level encryption.

## Summary

Total URS items: **80** (numbered 001–124 with gaps for future expansion).

Distribution by area:
- A. Monitoring & dashboard: 8
- B. Alerting: 11
- C. Operations: 11
- D. Workflow automation: 13
- E. Auth / RBAC / Audit: 14
- F. Data integrity: 6
- G. Localisation: 3
- H. Security: 7
- I. Operational lifecycle: 5
- J. Out-of-scope: explicit deferrals
- K. Compliance / CSV (post-Wave-6): 7

## K. Compliance / CSV in-app surface (added post-audit, 2026-05-22)

These requirements describe the in-app compliance dashboard, SOP execution recording, and CSV-document browser delivered after the original V-model walkthrough. They were retro-added to maintain V-model consistency per the PhD audit (F-PHD-AUDIT-VMODEL).

### URS-200 — Compliance dashboard
An RBAC-admin / authenticated operator can view a single page showing live CSV readiness: SOP statuses, audit-chain integrity, audit-insert/mirror failure counters, findings register summary. Gated behind `compliance.enabled` so non-regulated deployments see zero new surface.

### URS-201 — SOP execution recording
An RBAC-admin can record one execution of a documented SOP via the UI (`/compliance/sop/<sop_id>`). The execution is timestamped, attributed to the operator, written to a dedicated `sop_log` table AND mirrored to `audit_log`. Records are append-only (regulated evidence).

### URS-202 — Live-data substitution in rendered docs
When the operator views a SOP or CSV document in-app, the renderer substitutes `[[csv:KEY]]` placeholders against the live application state (e.g., current ACL count, audit-chain status, last-executed timestamps) so the reader sees fresh values inline. Substitution is skipped inside code spans / fenced code blocks so doc authors can use the syntax in prose examples.

### URS-203 — CSV documentation browser
The operator can browse the V-model + spec documents (`docs/csv/*.md`) from within Prism, grouped by category (reports / spec / risk / verification / trace / data-integrity / process / appendix). Each doc renders to HTML with live placeholders; "View raw markdown" link opens the source `.md`.

### URS-204 — Feature flag for compliance UI
The compliance UI is gated on `settings.compliance.enabled` (default `false`). When off, the nav item is hidden, the view routes 404, and the API endpoints 404. Persisted data (`sop_log`) survives the flag being toggled — only the UI surface changes.

### URS-205 — XSS-safe rendering of regulated documents
The renderer must NOT pass raw HTML from markdown source through to the operator's browser. A doc containing `<script>` or other dangerous tags must render those as escaped text, not as live HTML. Prevents XSS via a compromised commit or accidentally-pasted code snippet.

### URS-206 — Append-only SOP evidence
The `sop_log` table is regulated evidence and must structurally enforce append-only via DB triggers — mirroring the `audit_log` integrity model. If an operator records a typo'd execution, the documented remediation is to record a NEW execution with `result='partial'` and a "supersedes #N" note, not to UPDATE the original.

---
*End of document.*
