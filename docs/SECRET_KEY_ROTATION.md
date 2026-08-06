# Flask `SECRET_KEY` Rotation Runbook (S2-1 / BL3)

## When to rotate

Rotate the secret key whenever you need to invalidate **every** active Flask
session in one operation. Typical triggers:

- A signed session cookie has been observed in an attacker's possession.
- The on-disk key file (`data/flask_secret.key`) is suspected to have been
  read by an unauthorized process or copied off the host.
- Routine post-incident hygiene at the end of a containment window.
- A departing admin's session cookie should no longer be valid.

Rotating `SECRET_KEY` is a **fleet-wide logout**: every browser holding a
session cookie gets logged out on its next request. There is no per-session
selectivity here — for that, use `POST /api/admin/kill-session` instead.

## Procedure

1. Generate a new key (32 bytes of entropy, hex-encoded):

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   Copy the 64-character hex string the command prints.

2. Set the environment variable for the Prism service. On Windows
   (PowerShell, persistent for the user account that runs Prism):

   ```powershell
   [Environment]::SetEnvironmentVariable(
       "PRISM_SECRET_KEY",
       "<paste the hex string here>",
       "User"
   )
   ```

   For a Windows service, set it on the service account or via the
   service's Environment registry value.

3. Restart Prism:

   ```powershell
   Restart-Service Prism   # or your service name
   ```

   On startup the log line `Flask secret key loaded from PRISM_SECRET_KEY env var`
   confirms the new key is in effect.

4. Confirm session invalidation: any browser that was logged in before the
   restart should now be redirected to `/login`. The audit log will start
   recording fresh `login_success` rows as people sign back in.

## Notes

- If `PRISM_SECRET_KEY` is unset, Prism falls back to the persisted key in
  `data/flask_secret.key` (the historical default). This means an empty env
  var means "use whatever's on disk" — to truly rotate, set the env var.
- The on-disk file is unchanged by the env var path. To make the rotation
  permanent across env changes, also delete `data/flask_secret.key` so a
  fresh one is generated on the next restart-without-env scenario.
- `PRISM_SECRET_KEY` accepts either a hex string (preferred) or raw bytes.
  Hex is preferred because it's easy to round-trip through env vars and
  config-management systems without encoding hazards.

## Related primitives

- `POST /api/admin/kill-session` — revoke a specific `(username, login_time)`
  without restarting Prism. Use this when you know exactly whose session
  needs to die and you don't want to log everyone out.
- `POST /api/admin/disable-user` — block all future logins (and current
  sessions on next request) for a username.
- `POST /api/admin/enable-user` — undo `disable-user`.

The `SECRET_KEY` rotation is the biggest hammer — use the targeted endpoints
first whenever they fit the situation.
