# SOP-08 — Disaster Recovery Test

| Field | Value |
|---|---|
| Document ID | SOP-08 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | IT operations |
| Implements | URS-094 / FS-094 |
| Closes finding | F-BR-2, F-SOP-3 |
| Review cadence | **Quarterly** |

## 1. Purpose

Periodically prove that a Prism backup can actually be restored. An untested backup is theatre.

## 2. Cadence

Quarterly. Out-of-cycle after any change to `tools/backup.py` / `tools/restore.py` or to the schema migration logic in `database.py`.

## 3. Prerequisites

- A staging host (Windows) separate from production Prism.
- The most recent production backup zip (see `docs/BACKUP_AND_RESTORE.md`).
- Python 3.13.5 + `requirements.lock` installed on staging.

## 4. Procedure

### 4.1 Capture production state fingerprints

On production Prism, BEFORE backup:

```python
import sqlite3
con = sqlite3.connect('data/prism.db')
print('audit_log rows:', con.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0])
print('metrics rows:',   con.execute('SELECT COUNT(*) FROM metrics').fetchone()[0])
print('servers:',         con.execute('SELECT COUNT(*) FROM workflows').fetchone()[0])  # any reference table
```

Record these counts in the SOP execution record.

### 4.2 Take the backup

```
python tools/backup.py C:\PrismBackups\<date>
```

Capture the manifest:
```
type C:\PrismBackups\<date>\manifest.json
```

Verify the manifest includes:
- `prism-<ts>.db` (role: database)
- `config.json` (role: config)
- `prism.key.dpapi` or `prism.key` (role: fernet_key_*)
- `install_state.json` (role: install_state) — **NEW since F-BR-1 fix**

### 4.3 Restore into staging

On the staging host:

```
python tools/restore.py C:\PrismBackups\<date>
```

If the staging host has a different Windows SID than production, the DPAPI-wrapped key cannot be unwrapped — pass `--accept-key-loss` and accept that encrypted credentials in `config.json` will need to be re-entered. The DB and config schema still come across.

### 4.4 Bring staging Prism up

```
cd C:\Prism-staging
python app.py
```

### 4.5 Verify restored state

Re-run the fingerprint counts:

```python
import sqlite3
con = sqlite3.connect('data/prism.db')
print('audit_log rows:', con.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0])
print('metrics rows:',   con.execute('SELECT COUNT(*) FROM metrics').fetchone()[0])
```

Must match the production counts (modulo a small delta if production wrote between snapshot and backup completion — that's expected; document the delta).

Verify audit chain on the restored DB:

```python
from database import Database
db = Database('data/prism.db')
print(db.verify_audit_chain())
```

Must return `{"ok": true, ...}` — restoring a backup with a broken chain is a finding.

### 4.6 Smoke test

Run IQ-006 through IQ-013 (see `docs/csv/07_IQ_PROTOCOL.md`) on the staging instance. Run PQ-001 (steady-state monitoring) and PQ-005 (manual restart with audit). Both must pass.

### 4.7 Tear down staging

After verification, stop the staging Prism + delete the staging data directory. The point of the test is to prove restore works, not to keep two parallel installs.

### 4.8 File the test record

Under `data/sop_records/dr_test_<YYYY-MM-DD>.md`:
- Production fingerprints (4.1)
- Backup manifest (4.2)
- Restored fingerprints (4.5)
- PQ-001 + PQ-005 result (4.6)
- Total time elapsed (RTO measurement)
- Any deviations + their resolution

## 5. Exit criteria

- Restored Prism boots cleanly.
- Audit chain verifies intact.
- Fingerprint counts match (modulo expected snapshot delta).
- IQ + the 2 PQ scenarios pass.
- Total RTO ≤ 30 min (per `15_BACKUP_RECOVERY.md`).

Any FAIL is a finding; remediate via `14_CHANGE_CONTROL.md`.

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (IT ops) | | | |
| Quality sign-off (annual) | | | |

---
*End of SOP.*
