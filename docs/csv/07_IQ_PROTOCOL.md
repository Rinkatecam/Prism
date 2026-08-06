# 07 — Installation Qualification (IQ) Protocol

| Field | Value |
|---|---|
| Document ID | CSV-07 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `04_DS.md`, `05_CONFIG_SPEC.md` |

## Purpose

The Installation Qualification (IQ) phase verifies that Prism has been installed correctly into the validated environment. It is executed once per deployment (and once per major upgrade) and produces dated, signed evidence that the right code + right dependencies + right configuration are in place.

## A. Prerequisites (verified before IQ-001)

| # | Prereq | How verified |
|---|---|---|
| P-1 | Windows Server 2019+ or compatible Windows host with PowerShell 5.1+ | `(Get-Item 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').GetValue('ProductName')` |
| P-2 | Python 3.13.5 available on PATH | `python --version` |
| P-3 | `pip` available + version ≥ 24 | `pip --version` |
| P-4 | Service account that will own the Prism process (NOT a domain administrator) | OS user list |
| P-5 | Network: WinRM 5985/5986 reachable on every target Windows server | `Test-WSMan <target>` |
| P-6 | LDAP server reachable from Prism host (if `auth.enabled=true`) | `Test-NetConnection <ldap_host> -Port 389` |
| P-7 | SMTP server reachable (if `email.enabled=true`) | `Test-NetConnection <smtp_host> -Port 587` |

## B. IQ test steps

### IQ-001 — Code present at expected path

**Steps**:
1. `cd C:\Prism`
2. `git rev-parse HEAD` (or — if installed from release tarball — confirm Sigstore signature via `tools/verify_release.ps1` per `docs/RELEASE_VERIFICATION.md`)

**Pass criteria**:
- Repository root contains `app.py`, `database.py`, `config.json`, `requirements.lock`, `collector_v2/`, `routes/`, `tests/`.
- Commit hash recorded in IQ result form.
- If release tarball: Sigstore verification reports SUCCESS.

### IQ-002 — Deterministic dependency closure

**Steps**:
1. `pip install --require-hashes -r requirements.lock`

**Pass criteria**:
- Command exits 0.
- No "package hash mismatch" warnings.
- `pip freeze` output matches `requirements.lock` (modulo hash-only differences).

**Evidence**: capture `pip freeze > evidence/IQ-002_pip_freeze.txt`.

### IQ-003 — Python version recorded

**Steps**:
1. `python --version > evidence/IQ-003_python_version.txt`

**Pass criteria**:
- Recorded version is **3.13.5** (the version this audit was performed against; later patch versions of 3.13 are acceptable; 3.12 or 3.14 should re-trigger validation).

### IQ-004 — Directory layout & permissions

**Steps**:
1. Verify directories exist: `data/`, `data/config_backups/`, `data/audit_archive/`.
2. Verify ownership: all writable paths owned by the Prism service account.
3. Verify `data/.key` is mode `0400` (owner-read-only).

**Pass criteria**: all checks pass.

### IQ-005 — `config.json` present and parseable

**Steps**:
1. `python -c "import json; print(json.load(open('config.json')))"` — must not raise.
2. Check that `servers` is a list (possibly empty).
3. Check that `settings` is an object.

**Pass criteria**: command exits 0; JSON parses; structure present.

### IQ-006 — Database initialises cleanly

**Steps**:
1. If a fresh install: `data/prism.db` does not exist before this step.
2. Start the application: `python app.py` (foreground, capture stdout/stderr).
3. Wait for log line `Listening on http://...:5000`.
4. Verify `data/prism.db` is now present.
5. Verify schema by running `sqlite3 data/prism.db ".schema"` and confirming all 32 tables in `appendix_D_db_schema.md` are present.

**Pass criteria**: app starts without exception; all expected tables exist.

### IQ-007 — Background threads start

**Steps**:
1. With Prism running from IQ-006, hit `GET /api/system/health` (with appropriate authentication if `auth.enabled=true`).
2. Inspect the response:

**Pass criteria**:
- `collector_v2.supervisor.ok` = true
- `collector_v2.aggregator.ok` = true
- `collector_v2.workers.ok` = true
- `collector_v2.periodics.ok` = true
- `restart_scheduler.ok` = true
- `workflow_scheduler.ok` = true
- Heartbeat ages are all < 60 s.

### IQ-008 — Database integrity at install time

**Steps**:
1. `sqlite3 data/prism.db "PRAGMA integrity_check;"` returns `ok`.
2. `sqlite3 data/prism.db "PRAGMA journal_mode;"` returns `wal`.
3. `sqlite3 data/prism.db "PRAGMA busy_timeout;"` returns `5000`.

**Pass criteria**: all three values match.

### IQ-009 — Audit-log triggers active

**Steps**:
1. `sqlite3 data/prism.db ".schema audit_log"` — confirm both `audit_log_no_update` and `audit_log_no_delete` triggers are present.
2. Attempt an UPDATE: `sqlite3 data/prism.db "UPDATE audit_log SET username='x' WHERE id=1;"` — must fail with the `RAISE(ABORT, ...)` message.

**Pass criteria**: trigger blocks the UPDATE.

### IQ-010 — Audit JSONL mirror writable

**Steps**:
1. Through the running app, perform any action that writes audit (e.g. log in as backup-admin).
2. Verify `data/audit_mirror.jsonl` exists and contains at least one line.
3. Verify the file is appended to, not truncated, on subsequent actions.

**Pass criteria**: file present, monotonically growing.

### IQ-011 — WinRM reachability for every configured server

**Steps**:
1. For each entry in `config.json.servers`:
   - From Prism host: `Test-WSMan <host> -Authentication Default -Credential <creds>`.
   - **OR** trigger `POST /api/test-connection` for the server via the UI and confirm `inventory_ok: true`.

**Pass criteria**: every configured server returns `ok=true`.

### IQ-012 — Test suite passes against installed version

**Steps**:
1. `cd C:\Prism`
2. `python -m pytest tests/ -q --tb=no` (Prism need not be running for tests).

**Pass criteria** (as of 2026-05-22):
- 352 tests pass.
- 2 tests are pre-existing failures (`test_csp_nonce_present_in_rendered_html`, `test_csp_nonce_per_request`) — both at `/login`, unrelated to GxP functions. These are documented in `17_FINDINGS_AND_GAPS.md` finding F-111 and accepted as known.

**Evidence**: capture `python -m pytest tests/ --tb=line -v > evidence/IQ-012_pytest_output.txt`.

### IQ-013 — Time synchronisation

**Steps**:
1. `w32tm /query /status` (Windows) — confirm Prism host is NTP-synchronised.
2. Verify time drift < 1 s.

**Pass criteria**: synchronised, drift acceptable. Critical because every audit-log row's timestamp is `strftime('%Y-%m-%dT%H:%M:%SZ','now')` against the OS clock.

### IQ-014 — Backup of pre-install state (rollback safety)

**Steps**:
1. If this is an upgrade (not a fresh install), before starting:
   - `python tools/backup.py --output evidence/IQ-014_pre_upgrade_backup.zip`
   - Confirm backup file exists and is non-empty.

**Pass criteria**: backup archive present.

### IQ-015 — Secret-key rotation evidence

**Steps**:
1. Confirm `data/.key` exists.
2. Confirm `data/.key` ownership and permissions.
3. Confirm `PRISM_SECRET_KEY` is either set (env) or not (file-based).

**Pass criteria**: at least one of the two key sources is configured; permissions correct.

## C. IQ acceptance form

| IQ # | Test | Pass / Fail | Evidence file | Tested by | Date |
|---|---|---|---|---|---|
| IQ-001 | Code present | | | | |
| IQ-002 | Deps installed | | | | |
| IQ-003 | Python version | | | | |
| IQ-004 | Directory layout | | | | |
| IQ-005 | config.json | | | | |
| IQ-006 | DB initialises | | | | |
| IQ-007 | Threads up | | | | |
| IQ-008 | DB integrity | | | | |
| IQ-009 | Audit triggers | | | | |
| IQ-010 | Audit mirror | | | | |
| IQ-011 | WinRM reachable | | | | |
| IQ-012 | Test suite | | | | |
| IQ-013 | Time sync | | | | |
| IQ-014 | Pre-install backup | (N/A for fresh install) | | | |
| IQ-015 | Key present | | | | |

**Approval**:
- Performed by: ______________________ Date: ____________
- Reviewed by:   ______________________ Date: ____________
- Approved by:   ______________________ Date: ____________

---
*End of document.*
