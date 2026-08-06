# SOP-06 — PowerShell Workflow Governance

| Field | Value |
|---|---|
| Document ID | SOP-06 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | Security engineering |
| Implements | URS-053, URS-054 / FS-053, FS-054 |
| Closes finding | F-SOP-7 |
| Review cadence | Quarterly + per-change |

## 1. Purpose

Define who can use the `Run PowerShell` and `Condition` workflow blocks (free-form PowerShell), what governance applies to additions to the sandbox allowlist, and how the audit trail is preserved.

Cross-reference: `docs/WORKFLOW_SANDBOX.md` (operator-facing description of the sandbox).

## 2. Who can use free-form PowerShell

| Block | Required permission |
|---|---|
| `Run PowerShell` | Authenticated user with `admin` on the target server |
| `Condition` | Same — the block runs the user-supplied script to compute a boolean |
| Service / process / port live picker (Browse) | `_require_auth` — read-only on the target |

For **tier-0** servers, the workflow execute additionally requires a dual-control approval (`pending_approvals` table). See SOP-04 §"escalation" and the audit-log row `tier0_approval_consumed`.

## 3. Sandbox default posture

`workflows.sandbox.enabled = true` (default in `config.json`). The sandbox enforces:

1. A default-deny cmdlet allowlist (~92 cmdlets in `ps_sandbox.py:DEFAULT_ALLOWED_CMDLETS`).
2. A HARD_DENY pattern list (Invoke-Expression, Add-Type, Start-Process, backtick escapes, `&`, `iex`, char-code reconstruction).
3. A length cap (`workflows.sandbox.max_script_chars`, default 10 000).

The sandbox CANNOT be disabled silently — flipping `enabled` to `false` writes a `config_update` row to `audit_log` and is a controlled change. Disabling it requires a documented business reason in the SOP execution record.

## 4. Sandbox allowlist change procedure

Adding a cmdlet to `DEFAULT_ALLOWED_CMDLETS` is a security-significant change. Procedure:

1. **Request**: operator describes the use case + the cmdlet they need.
2. **Threat-model review**: Security engineering examines:
   - Can the cmdlet read or write outside the operator's intended scope?
   - Can it spawn child processes? Reach the network?
   - Are there safer alternatives via the structured-field path (parameter binding, FS-054)?
3. **Decision**: two engineers (at least one Security) approve via PR review.
4. **Implementation**: PR adds the cmdlet to `DEFAULT_ALLOWED_CMDLETS` AND adds a positive + negative test in `tests/test_ps_sandbox.py`.
5. **Documentation**: append an entry to the change log in `docs/WORKFLOW_SANDBOX.md`:
   ```
   ## Change log
   - 2026-05-22 — Added `Start-ADSyncSyncCycle` for AAD-Connect sync workflows.
     Justification: cmdlet only writes to AAD; no fs/network reach. Reviewed by …
   ```

## 5. Quarterly review

Every quarter, Security engineering grep's `workflow_execution_steps` for `node_type = 'run_powershell'`:

```sql
SELECT step.script, COUNT(*) FROM workflow_execution_steps step
JOIN workflows w ON …
WHERE step.node_type = 'run_powershell'
  AND step.started_at > date('now', '-90 days')
GROUP BY step.script;
```

Spot-check 5 random scripts. Confirm:
- No HARD_DENY patterns slipped through (they couldn't — but verify).
- No suspicious-looking script reaches outside its declared scope.
- The script's audit row identifies the operator, the server, and a clear intent.

File the review record under `data/sop_records/ps_governance_<YYYYQ#>.md`.

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Owner (Security engineering) | | | |
| Quality sign-off | | | |

---
*End of SOP.*
