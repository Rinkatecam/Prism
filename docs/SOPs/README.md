# Prism Operational SOPs

This directory holds the Standard Operating Procedures for running Prism in a regulated environment. Each SOP is a controlled document — version-controlled in git, with an effective date and an owner.

| SOP file | Topic | Owner | Cadence |
|---|---|---|---|
| `01_user_onboarding.md` | Grant Prism access to a new operator | RBAC-admin | per-hire |
| `02_user_offboarding.md` | Revoke a leaving operator's access | RBAC-admin | per-departure |
| `03_periodic_acl_review.md` | Confirm continued business need for every ACL row | RBAC-admin + service owners | Quarterly |
| `04_incident_response.md` | What to do when Prism flags an outage | IT operations | per-incident |
| `05_validated_baseline_review.md` | Confirm Prism is operating in its qualified state | IT-validation | Monthly |
| `06_powershell_governance.md` | Who can use free-form PS in workflows | Security engineering | Quarterly review |
| `07_audit_log_archival.md` | Cold-storage handling of audit-log material | Quality | Quarterly |
| `08_disaster_recovery_test.md` | Periodic restore drill into staging | IT operations | Quarterly |
| `09_audit_mirror_rotation.md` | OS-level rotation of `audit_mirror.jsonl` | IT operations | Continuous |

Each SOP cites the URS / FS / finding it implements so the change-control audit trail is preserved.

The two existing operator-facing how-to docs in `docs/` are retained for engineering reference and are referenced from the SOPs that need them:

- `docs/BACKUP_AND_RESTORE.md` — backup tool usage
- `docs/KEY_ROTATION.md` — `tools/rekey.py` usage
- `docs/WORKFLOW_SANDBOX.md` — sandbox limitations + allowlist change log

---
*The SOPs are draft v1.0 (2026-05-22). Sign-off pages at the bottom of each SOP.*
