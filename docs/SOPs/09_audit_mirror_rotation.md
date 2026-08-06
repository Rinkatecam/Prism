# SOP-09 — Audit Mirror Rotation

| Field | Value |
|---|---|
| Document ID | SOP-09 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | IT operations |
| Implements | URS-081 / FS-081 |
| Closes finding | F-AT-3 |
| Review cadence | Continuous (monitored by SOP-05) |

## 1. Purpose

Define the rotation policy for `data/audit_mirror.jsonl` so the file does not grow unbounded but also doesn't lose append continuity for Prism's open file handle.

## 2. Why this matters

`audit_mirror.jsonl` is Prism's out-of-band copy of `audit_log`. It's the second line of defence against in-process or DB-level audit-log tampering, and it's the primary feed for any SIEM the operator has bolted on. Three failure modes to guard against:

1. **File grows forever** → disk full → Prism aborts audit inserts (counter `_audit_mirror_failures` rises).
2. **Operator rotates with `mv`** → Prism's open file handle still points at the moved inode, **but the mirror appears empty in the new location**. SIEM stops getting fresh lines.
3. **Operator rotates with `>`** (truncate) → Prism continues to append from the original cursor position, leaving the file looking sparse with leading nulls or zero-length first lines.

## 3. Recommended rotation policy

### Windows (production deployment)

The shipper (Filebeat / Datadog Agent / Splunk forwarder) should:
- **Tail** the mirror file with the equivalent of `tail -f` (continuous read, follows inode).
- **NOT** rotate the source file. Let it grow.
- The shipper itself rotates its OWN spool / output queue after shipping.

A periodic compaction job (e.g. weekly via Windows Task Scheduler):

```powershell
# Inside a maintenance window — Prism still running
$src = "C:\Prism\data\audit_mirror.jsonl"
$arch = "C:\Prism\data\audit_archive\audit_mirror_$(Get-Date -Format yyyyMMdd_HHmmss).jsonl"

# Copy-then-truncate: preserves Prism's open file handle on the source.
Copy-Item -Path $src -Destination $arch -Force
# Truncate the source while the open append-share handle is preserved.
[System.IO.File]::WriteAllText($src, "")

# Verify
if ((Get-Item $arch).Length -eq 0) {
    Write-Error "Archive is empty — something went wrong"
}
```

**Do NOT use `Move-Item`** (it changes the inode; on Windows it changes the file identity in a way Prism's open append handle doesn't tolerate).

### Linux (if Prism were deployed there)

Use `logrotate` with `copytruncate`:

```
/var/lib/prism/data/audit_mirror.jsonl {
    weekly
    rotate 4
    copytruncate
    compress
    notifempty
    missingok
}
```

`copytruncate` does the same dance as the PowerShell above.

## 4. Verification

After every rotation:

```python
import os
size = os.path.getsize("C:/Prism/data/audit_mirror.jsonl")
print(f"mirror is now {size} bytes")

# Trigger an audit row, then re-check size.
# Run any small mutating action via the UI; mirror should grow by ~500 bytes.
```

Also check `_audit_mirror_failures` (via the health endpoint, once F-AT-1's surfacing is wired in or via Python repl):

```python
from database import Database
db = Database("data/prism.db")
print("mirror failures since boot:", db._audit_mirror_failures)
```

Should remain at 0 after rotation. Any non-zero value indicates the rotation broke continuity.

## 5. Reference

- `database.py:log_audit` — appends to the mirror after each successful DB insert.
- `database.py:AUDIT_MIRROR_PATH` — the path constant (data/audit_mirror.jsonl).
- `tests/test_audit_chain.py` — exercises mirror writes end-to-end.

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Owner (IT ops) | | | |

---
*End of SOP.*
