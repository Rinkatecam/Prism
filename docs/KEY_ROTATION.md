# Prism Fernet key rotation

Prism wraps every stored credential (server WinRM passwords, the SMTP
password, the LDAP bind password) with a single symmetric Fernet key.
That key is itself wrapped with Windows DPAPI under
`data/prism.key.dpapi`, scoped to the user account that runs Prism.

This document covers:

1. When to rotate the key.
2. The rehearsed rotation procedure (`tools/rekey.py`).
3. The plain-text migration escape hatch and how to disable it.
4. DPAPI host-binding warnings (cross-link with `BACKUP_AND_RESTORE.md`).

---

## 1. When to rotate

Rotate the Fernet key:

* **Annually**, as a baseline hygiene drill. Schedule a Q1 maintenance
  window each year.
* **Immediately after any suspected compromise** of the Prism host,
  the service account profile, or any backup containing
  `prism.key.dpapi`. DPAPI is "the user's password applied as a KDF" —
  a stolen profile + password is enough to decrypt every credential
  offline. Rotating invalidates that material.
* **Before changing the Prism service account.** DPAPI is per-user
  scope; if you switch the account first, the old DPAPI blob is
  unreadable and you've lost every credential. Sequence is:
  rekey → stop Prism → change service account → start Prism → re-enter
  any credentials touched between rekey and stop (ideally none).
* **When an admin who knew the service-account password leaves.**
  Prism inherits whatever trust the operator placed in the account;
  if the trust boundary moved, the key should too.

Do **not** wait for compromise to be confirmed. The rotation is cheap
(seconds), the recovery from "key was leaked six months ago and we
didn't rotate" is manual re-entry of every server credential.

---

## 2. Procedure

The tool lives at `tools/rekey.py`. It reads every encrypted password
in `config.json`, decrypts under the OLD key, generates a NEW Fernet
key, re-encrypts under the new key, and atomically swaps the key file
plus the config.

### Rehearsal (no writes)

    python tools/rekey.py --dry-run

Output:

    DRY-RUN OK: would rekey N credential(s) (skipped=M failed=0)

`failed=0` is required. If any field fails to decrypt, the OLD key is
already broken for that credential — fix that first (re-enter the
password through the UI) before running the live rotation.

### Live rotation

    1. Stop Prism (Windows service, scheduled task, or `python app.py`).
    2. Run:

           python tools/rekey.py --in-place

       Expected output:

           OK: rekeyed N credentials, old key archived to
           data/prism.key.dpapi.20260506-143022.bak

    3. Verify Prism starts cleanly and a sample server reports metrics
       (the metric collector immediately decrypts a stored credential).
    4. Once you've confirmed the new key works against the live fleet,
       move the `.bak` file off-host (or delete it). Until then it is
       your rollback path.

`tools/rekey.py` writes a forensic log to `data/rekey.log` and an
`audit_log` row with action `fernet_key_rotated` if `prism.db` is
reachable. The `.bak` archive lets you roll back manually:

    move data\prism.key.dpapi.20260506-143022.bak data\prism.key.dpapi
    # ...and restore the previous config.json from data/config_backups/

### Order of operations (why it matters)

The tool writes in this order:

1. Copy old `prism.key.dpapi` to `prism.key.dpapi.<ts>.bak`.
2. Write new `prism.key.dpapi`.
3. Write new `config.json` (write-tmp + rename).
4. Remove any leftover plain-text key.

If a crash happens between (2) and (3), the new key is on disk but the
config still references old ciphertext — Prism would fail to decrypt on
start. Recover by restoring the `.bak` over the new key file. The old
config is intact because we use atomic write-and-rename.

---

## 3. Plain-text migration escape hatch

`crypto_utils._load_or_create_key()` historically supported a silent
migration path: if `data/prism.key` (plain-text) was present and
`data/prism.key.dpapi` was missing, Prism would on next start adopt
the plain-text key and re-wrap it under DPAPI.

This is a **key-injection escape hatch**. An attacker who can write
into `data/` can plant a `prism.key` file with a Fernet key they chose,
delete `prism.key.dpapi`, and from next start onward Prism encrypts
every new credential with the attacker's key.

As of the 2026-05 hardening, the migration path is **gated by an
explicit environment variable**:

    set PRISM_ALLOW_PLAINTEXT_MIGRATION=1
    python app.py
    REM ... after first start, unset:
    set PRISM_ALLOW_PLAINTEXT_MIGRATION=

Without the env var, `_load_or_create_key()` raises:

    RuntimeError: Refusing to migrate plain-text key without
    PRISM_ALLOW_PLAINTEXT_MIGRATION=1 (see docs/KEY_ROTATION.md)

Operators with a legitimate plain-text-key install (e.g., upgrading
from a pre-DPAPI revision) set the env var **once**, restart Prism so
the migration happens, then unset it. Steady-state production never
has this variable set.

The gate fires only when DPAPI is available and no `.dpapi` file
exists yet. On Linux/macOS dev boxes, where DPAPI is unavailable,
plain-text is the supported steady state — no env var needed.

---

## 4. DPAPI host binding (cross-reference)

The Fernet key is wrapped under DPAPI with **current-user scope**, not
LOCAL_MACHINE. Three implications, also covered in
`docs/BACKUP_AND_RESTORE.md`:

* A backup of `prism.key.dpapi` plus the service-account profile
  (NTUSER.DAT, master keys under
  `AppData\Roaming\Microsoft\Protect\<SID>`) decrypts every credential
  offline given the service-account password (or, on a domain-joined
  box, the Domain Backup Key — i.e., a compromised DC).
* Restoring the wrapped key file on a different host or under a
  different service account silently decrypts to garbage. There is no
  error; every WinRM auth simply fails. `tools/restore.py` warns when
  the source SID differs from the current one.
* If the wrapping account is permanently lost (forensic image, host
  rebuild), there is no recovery — every credential must be re-entered.
  `tools/rekey.py` does NOT solve this case (it requires the OLD key to
  be readable). Plan for it via off-host credential storage (a
  password manager that's not the Prism service account).

For backup/restore semantics specifically, see
[BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).
