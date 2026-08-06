# 14 — Change Control

| Field | Value |
|---|---|
| Document ID | CSV-14 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `04_DS.md`, `07_IQ_PROTOCOL.md`, `08_OQ_TEST_INVENTORY.md` |

## Purpose

Document how changes to Prism are proposed, reviewed, tested, deployed, and rolled back. CSV requires that the validated system not change uncontrolled; this is the policy that demonstrates control.

## A. Repository governance

- Source-of-truth: git repository at `C:\Prism\` (private; mirror to remote for backup).
- Branch model: trunk-based with `master`. Feature branches for non-trivial changes.
- Commit messages: imperative, descriptive, body explains *why*. The commit history is itself part of the audit trail for the codebase (see Phase 12 audit cross-references).
- The release tarball is Sigstore-signed (cosign keyless via GitHub Actions OIDC); verified at IQ-001 / IQ-002.

## B. Change types & path

| Change type | Examples | Required path |
|---|---|---|
| **Critical security fix** | RCE, auth bypass, audit-log tamper | Hot-fix branch; immediate review; deploy after pytest + smoke; backport into validated baseline if applicable |
| **Bug fix** | UI mis-render, transient WinRM handling | Standard PR; pytest must pass; reviewer signs off |
| **New feature** | A new check type, new workflow block, new endpoint | New URS item drafted; FS + DS updated; tests added; reviewer signs off |
| **Library upgrade** | Bumping pypsrp, Flask, ldap3 | Update `requirements.lock` deterministically (`pip-compile --generate-hashes`); re-run pytest; smoke-test; document in `docs/DEPENDENCIES.md` |
| **Config change** | Threshold tuning, schedule edit | Through `POST /api/config`; auto-backup to `data/config_backups/`; audit row written |
| **Documentation** | This package; runbooks | PR; reviewer signs off; no functional impact |

## C. Pre-merge gates

Every change must pass before merge:

1. **Tests green**: `pytest tests/ -q` returns 352 passing (modulo documented F-111 failures).
2. **Static linting** (where applicable; pre-existing): no new warnings.
3. **Code review** by a second engineer.
4. **Risk-class check**: if the change touches a Critical-RPN area (sandbox, RBAC, audit log) AND is not a hot-fix, two reviewers required.

## D. Deployment procedure

| Step | Action |
|---|---|
| 1 | Backup current state (`tools/backup.py`) |
| 2 | Verify backup is restorable (spot-check on a staging instance, ideally) |
| 3 | Pull new code (`git pull` or replace from signed tarball + verify with `tools/verify_release.ps1`) |
| 4 | Re-install deps: `pip install --require-hashes -r requirements.lock` |
| 5 | Stop Prism cleanly (Ctrl-C; wait for sub-threads to flush) |
| 6 | Start Prism (`python app.py`) |
| 7 | Re-run IQ-006 through IQ-013 (post-install smoke) |
| 8 | Re-run any PQ scenario affected by the change |

## E. Rollback procedure

| Step | Action |
|---|---|
| 1 | Stop the new Prism instance |
| 2 | `git checkout <previous-tag>` (or restore from signed tarball of previous release) |
| 3 | If DB schema changed: restore `prism.db` from pre-deployment backup |
| 4 | Re-install deps for the older version |
| 5 | Start the older Prism (`python app.py`) |
| 6 | Re-run IQ-006 through IQ-013 |
| 7 | Document the rollback in the change-control log + write an `audit_log` row (`category='lifecycle'`, `action='rollback'`, details with from→to commit hashes) |

## F. Configuration change control

- Config edits in the UI go through `POST /api/config`.
- The route:
  1. Validates the new config (HTTPS-downgrade gate, tier-0 skip-verify gate, sensitive-field strip filter).
  2. Writes a JSON snapshot to `data/config_backups/<timestamp>.json` (auto backup).
  3. Writes the new `config.json` atomically (tempfile + rename).
  4. Writes an `audit_log` row with `action='config_update'` and the diff (truncated) in `details`.
- `ConfigManager` re-reads on next mtime check (≤ 5 s).
- Manual file edits are tolerated but should be documented in the change log if performed.

## G. Sandbox-allowlist change control

The PowerShell sandbox allowlist (`DEFAULT_ALLOWED_CMDLETS` in `ps_sandbox.py`) is a security boundary. Any addition or removal:

1. Pull request review by two engineers (one must understand the threat model).
2. Justification in the PR description: which user request prompts this, what's the worst-case if an attacker uses this cmdlet, what mitigation.
3. Cross-reference the change in `docs/WORKFLOW_SANDBOX.md` change log.
4. Add a test if the new cmdlet introduces a new pattern (e.g. ADSync triggers a sub-test).

## H. Validated-baseline management

The "validated baseline" is the (code commit + dep lock + config) tuple that was qualified by the most recent IQ+OQ+PQ pass.

| Artefact | Stored as |
|---|---|
| Code commit hash | Git tag + recorded in IQ-001 result |
| `requirements.lock` | File in repo at the tag |
| `config.json` snapshot | Backup zip from `tools/backup.py` |
| IQ / OQ / PQ evidence | `docs/csv/evidence/` directory |

Re-qualification is required if any of these change in a way that exceeds the "minor" threshold:

- Patch-level Python upgrade: minor — re-run OQ.
- Minor-level Python upgrade: full IQ + OQ.
- Major-level Python upgrade: full IQ + OQ + PQ.
- Library upgrade (security patch): OQ + smoke PQ.
- Library upgrade (feature): full IQ + OQ + relevant PQ.
- Code change touching Critical-RPN areas: re-run all High/Critical PQ scenarios.
- Code change in operational/UI areas: OQ pass sufficient.

## I. Change log (since CSV process started)

| Date | Change | Author | IQ/OQ/PQ re-run? |
|---|---|---|---|
| 2026-05-22 | This CSV package authored; 9 new tests added for janitor + auto-clear; +4 tests for aggregator | Audit | OQ re-run: 352 passing |

(Subsequent entries appended here.)

## J. Audit Trail (for change control itself)

| Change-control action | Captured where? |
|---|---|
| Config edit via UI | `audit_log` row `config_update` |
| Manual config edit on disk | **Not audited** by Prism — operator must document externally |
| Code change | git log |
| Test re-run | `evidence/` directory |
| Sandbox allowlist edit | git log + `docs/WORKFLOW_SANDBOX.md` change log |

---
*End of document.*
