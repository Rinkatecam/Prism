# SOP-07 — Audit Log Archival

| Field | Value |
|---|---|
| Document ID | SOP-07 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | Quality |
| Implements | URS-078, URS-082 / FS-082 |
| Closes finding | F-AT-2, F-AT-3 |
| Review cadence | **Quarterly** |

## 1. Purpose

Move aged audit material to controlled cold storage so the live `audit_log` table stays a manageable size while preserving every regulated record indefinitely.

`audit_log` is **append-only** (DB triggers + hash chain). This SOP does NOT delete rows. The archive is a **one-way snapshot** for SIEM ingest and / or off-host compliance hand-off.

## 2. Cadence

Every quarter (suggested: last working day of the quarter). Out-of-cycle on regulator request or before a Prism upgrade that could affect the audit schema.

> **Live status:** Last archived [[csv:last_execution.SOP-07]]. Next due [[csv:next_due.SOP-07]]. Audit subsystem health: [[csv:audit_blind]].

## 3. Procedure

### 3.1 Snapshot

```
POST /api/audit-log/archive
{ "older_than_days": 90 }
```

The route:
1. Writes the rows older than the cutoff to `data/audit_archive/audit_<YYYYMMDD_HHMMSS>.jsonl`.
2. Returns `{ "ok": true, "rows": N, "file": "<relative path>" }`.
3. Writes an `audit_archive` audit row capturing who initiated, how many rows, the filename, and the cutoff.

### 3.2 Verify

Compute SHA-256 of the archive file:

```
sha256sum data/audit_archive/audit_*.jsonl
```

Record the hash + file size + row count alongside the SOP execution record. This is the integrity fingerprint for any future tampering check.

### 3.3 Transfer to controlled storage

Move the archive file to the organisation's controlled cold storage:
- Encrypted at rest (storage-layer responsibility).
- Owner-only readable.
- Retention: 7 years (or whatever the regulator's record-retention policy requires).

Verify the file at the destination has the same SHA-256 as recorded in 3.2.

### 3.4 Hash-chain integrity verification

After archival, re-verify the live audit chain to confirm it remains intact:

```python
from database import Database
db = Database('data/prism.db')
print(db.verify_audit_chain())
```

Must return `{"ok": true, ...}`.

## 4. About `audit_mirror.jsonl` (F-AT-3)

The mirror file at `data/audit_mirror.jsonl` is an OS-level append-only sibling of `audit_log`. It is **not** the archive. It's a real-time SIEM feed.

**Rotation policy** (operator's responsibility):
- A platform log shipper (e.g. Filebeat, Datadog Agent, Splunk forwarder) should tail this file and ship lines to the SIEM continuously.
- File rotation must use **copy-then-truncate** (not move-then-create), so Prism's open file handle keeps appending to the same inode while the shipper reads from the rotated copy. The Windows-friendly equivalent is to keep the file open in append-share mode (which Prism does).
- Recommended cadence: weekly rotation; 4-week local retention; older copies move to cold storage alongside the archive.

A misconfigured rotation (e.g. simple `move`) will silently break Prism's append, surfaceable only by `_audit_mirror_failures` counter (F-D-1). Include rotation health in the monthly baseline review (SOP-05).

## 5. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (RBAC-admin) | | | |
| Quality sign-off | | | |

---
*End of SOP.*
