# 15 — Backup & Recovery Validation

| Field | Value |
|---|---|
| Document ID | CSV-15 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `04_DS.md`, existing `docs/BACKUP_AND_RESTORE.md` (operator-facing how-to) |

## Purpose

Validate that Prism's backup + recovery procedure meets the GAMP 5 / ALCOA+ "Enduring" + "Available" attributes. Cross-reference the existing operator-facing how-to (`docs/BACKUP_AND_RESTORE.md`) and confirm it's accurate against the code.

## A. What's backed up (matches `docs/BACKUP_AND_RESTORE.md`)

| Artefact | Purpose | Backed up? |
|---|---|---|
| `data/prism.db` | SQLite DB — metrics, events, logs, audit_log, all configuration tables | **YES** by `tools/backup.py` |
| `data/prism.db-wal` + `db-shm` | WAL sidecar — backup tool snapshots a checkpoint-consistent copy | **YES** (checkpoint forced before copy) |
| `data/config.json` | Server inventory, LDAP/SMTP config, backup-admin hash | **YES** |
| `data/prism.key.dpapi` (or `.key`) | Fernet key wrapping all credentials; DPAPI-wrapped on Windows | **YES** (with host-SID binding check) |
| `data/install_state.json` | Cross-restart install-state | **YES** (added during audit scope — should be verified) |
| `data/audit_mirror.jsonl` | SIEM-bound mirror | **NO** by design — SIEM is system of record |
| `data/flask_secret.key` | Flask session signing | **NO** by design — DR should rotate it |
| `data/config_backups/*.json` | Auto-snapshots of config | **NO** by design — historical artefact only |
| `data/audit_archive/*.jsonl` | Aged audit material | **NO** by design — should already be in cold storage |

**Finding F-BR-1 (Minor)**: confirm `tools/backup.py` includes `install_state.json` in its manifest; if it doesn't, that's a gap because the install-state survives the DB but is held separately.

## B. Backup procedure

Per `docs/BACKUP_AND_RESTORE.md`:

1. `python tools/backup.py C:\PrismBackups\<date>` — writes a timestamped directory with `manifest.json` + the artefact files.
2. Records source-host Windows SID in `manifest.json` (because DPAPI-wrapped keys are bound to the originating user).
3. Forces a SQLite checkpoint before copying `prism.db` to guarantee the on-disk DB is consistent.

## C. Recovery procedure

1. Stop Prism on the target host.
2. `python tools/restore.py C:\PrismBackups\<date>` — places files into `data/` (atomic temp+rename).
3. `tools/restore.py` checks the manifest's host SID against the current SID.
   - **Match**: proceed; encrypted credentials remain decryptable.
   - **Mismatch**: refuses unless `--accept-key-loss` flag is passed. In that case DB + config restore succeeds, but every encrypted credential becomes unrecoverable (operator must re-enter WinRM/SNMP/SMTP secrets via UI).
4. Start Prism.
5. Re-run IQ-006 through IQ-013 to confirm post-restore health.

## D. Recovery time objective (RTO) & recovery point objective (RPO)

| Parameter | Target | Notes |
|---|---|---|
| RPO | ≤ 24 h | Daily nightly backup via Scheduled Task (sample XML in `docs/BACKUP_AND_RESTORE.md`) |
| RTO | ≤ 30 min | Restore is file-copy bound; ~30 s for a < 100 MB `prism.db` plus IQ smoke |
| Cold-storage retention | 7 years (matches audit-log retention expectation) | Per operator's retention policy |

## E. Backup integrity verification

- `tools/backup.py` writes the manifest with file size + (optional) SHA-256 of each artefact.
- `tools/restore.py` validates the SHA-256 on extraction (if present in manifest).
- **Recommendation (Observation F-BR-2)**: scheduled monthly restore-drill into a non-production environment, with a documented PQ-013 result.

## F. ALCOA+ "Enduring" + "Available" check

| Attribute | Status |
|---|---|
| **Enduring** — backups stored on durable media | OK — operator's responsibility; backup file is a regular zip the operator places on enterprise storage |
| **Available** — can be restored on demand | OK — `tools/restore.py` is straightforward |
| Backup encryption at rest | Out of Prism scope; depends on enterprise storage choice |
| DR test cadence | **Finding F-BR-2 (Observation)**: define quarterly DR test SOP |
| Documentation | `docs/BACKUP_AND_RESTORE.md` is accurate as of 2026-05-22 |

## G. Failure modes & responses

| Failure mode | Detection | Response |
|---|---|---|
| `prism.db` corrupted | `PRAGMA integrity_check` returns non-`ok` | Restore from latest backup; raise an incident |
| Lost Fernet key (host disk lost) | DPAPI-unwrap fails on every credential | Restore with `--accept-key-loss`; re-enter secrets |
| Backup zip damaged | SHA-256 mismatch | Use older backup; investigate storage |
| Audit log hash chain broken | `verify_audit_chain()` reports `first_break_id` | **Incident**: investigate; restore from pre-break backup if needed; document |
| Filesystem full | Prism logs `database is locked` / write errors | Free space; restart |
| Process killed mid-write | WAL replays on next start | OK — designed for this |

## H. Findings

| ID | Severity | Description |
|---|---|---|
| F-BR-1 | Minor | Verify `tools/backup.py` includes `install_state.json` in manifest |
| F-BR-2 | Observation | Define quarterly DR-test SOP |

All findings carried to `17_FINDINGS_AND_GAPS.md`.

---
*End of document.*
