# Prism backup and restore

Prism keeps three pieces of state in `data/` that are worth preserving:

| File | Why it matters |
| --- | --- |
| `data/prism.db` | SQLite. Seven years of audit log, every metric sample, every event, baselines, workflow runs. |
| `data/config.json` | Server inventory, LDAP/SMTP config, the backup-admin password hash. |
| `data/prism.key.dpapi` (or `data/prism.key`) | Fernet key that wraps every stored credential -- WinRM, SNMP, SMTP. |

`tools/backup.py` snapshots all three to a timestamped directory.
`tools/restore.py` puts them back. Both are pure-stdlib + (optional)
pywin32 and run on the box, in-process is not required.

## When to back up

- After **any** server-inventory change (add/remove server, credential
  rotation).
- Before any factory-reset or major upgrade.
- Nightly via Windows Scheduled Task. Sample XML below.

## What is NOT backed up

- `data/audit_mirror.jsonl` -- the per-host append-only mirror that ships
  to your SIEM. Restoring it would re-import history the SIEM already
  has. The SIEM is the system of record for that file.
- `data/flask_secret.key` -- a fresh secret post-DR is desirable; it
  invalidates all sessions and forces re-login.
- TLS certificates -- regenerate on the new host.

For SIEM-style audit-log export use the existing `/api/audit-log/archive`
endpoint, not these tools. That endpoint is a SIEM feed, not a DR path.

## CRITICAL: DPAPI host binding

> The Fernet key on disk is wrapped with **DPAPI**, which binds it to the
> Windows user account that ran Prism. Move the file to a different user
> or different host and `unprotect_key` returns garbage *with no error
> message* -- subsequent Fernet decrypts succeed against the wrong key
> and produce malformed plaintext that surfaces as cryptic WinRM
> auth failures days later.

`tools/backup.py` records the source-host SID in `manifest.json`.
`tools/restore.py` refuses to proceed if that SID differs from the
current host's SID, unless you pass `--accept-key-loss` -- which means:

  * The DB and config get restored.
  * Every encrypted credential is unrecoverable.
  * You walk the inventory in the UI and re-enter every WinRM/SNMP/SMTP
    secret from your password manager.

The audit log and historical metrics survive in either case.

## Backup walkthrough

    python tools/backup.py C:\PrismBackups\2026-05-06

Output:

    OK: backup written to C:\PrismBackups\2026-05-06, 5 files, total size 12.40 MB

The directory will contain:

    prism-20260506-031500.db
    config.json
    prism.key.dpapi
    manifest.json
    RESTORE.md

Ship this directory to wherever you keep operator artefacts (file share,
S3, gpg-encrypted tarball, whatever your policy says). Prism does not do
the offsite step -- that is the operator's tooling.

## Restore walkthrough -- same host, same account

    python tools/restore.py C:\PrismBackups\2026-05-06
    # OK: restored from ..., restart Prism: python app.py

If `data/prism.db` already exists the script refuses unless you pass
`--force`. Disaster recovery should not happen by accident.

## Restore walkthrough -- different host or rebuilt account

    python tools/restore.py C:\PrismBackups\2026-05-06
    # ERROR: WARNING: DPAPI key was wrapped under a DIFFERENT Windows account...

You have two options:

1. **Rebuild the original account.** Restore the original profile and
    its password from your AD/system backup, run Prism as that user, the
    DPAPI key unwraps, every credential decrypts.
2. **Accept the loss.** Re-run with `--accept-key-loss`:

       python tools/restore.py C:\PrismBackups\2026-05-06 --accept-key-loss

    Then start Prism, walk the server list, re-enter every credential.

## Sample Windows Scheduled Task -- nightly backup

Save as `prism-nightly-backup.xml`, register with
`schtasks /Create /XML prism-nightly-backup.xml /TN "Prism Nightly Backup"`.

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T03:15:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>DOMAIN\svc-prism</UserId>
      <LogonType>Password</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Prism\.venv\Scripts\python.exe</Command>
      <Arguments>C:\Prism\tools\backup.py C:\PrismBackups\nightly</Arguments>
      <WorkingDirectory>C:\Prism</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

Run the scheduled task **as the same Windows account that runs Prism**
so the DPAPI-wrapped key copies forward cleanly. A different service
account would copy a key that decrypts to garbage on restore.

Rotate old backup directories with your usual retention tooling
(`forfiles /P C:\PrismBackups\nightly /D -30 /C "cmd /c rmdir /s /q @path"`
on a separate task, for example).

## Tampering / partial-copy detection

`manifest.json` carries a SHA-256 per file. `tools/restore.py` recomputes
every hash before touching `data/`. Any mismatch aborts the restore --
if a backup was truncated mid-flight or modified in transit, you find
out before it overwrites your live state.
