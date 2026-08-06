# SOP-01 — User Onboarding

| Field | Value |
|---|---|
| Document ID | SOP-01 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | RBAC-admin |
| Implements | URS-070, URS-075 / FS-070, FS-075 |
| Closes finding | F-SOP-1 |
| Review cadence | Annual or on Prism upgrade |

## 1. Purpose

Grant Prism access to a new operator in a controlled, auditable way. Ensures their permissions are appropriate to their role and that the grant is recorded in the audit trail.

## 2. Scope

Applies to every new operator who needs to access Prism — IT engineers, on-call staff, auditors. Does NOT apply to service accounts (covered by `06_powershell_governance.md`).

## 3. Prerequisites

- The operator has an Active Directory account in the organisation's directory.
- A signed access-request form (out-of-band) authorising the role.
- The requesting manager has named which servers / tier the operator needs.

## 4. Procedure

### 4.1 Confirm AD account
1. Confirm the operator's `sAMAccountName` exists in AD.
2. Confirm any required group memberships are in place (per the LDAP `user_filter` in `config.json`).
3. If `auth.enabled = true`, no further app-level action is required for the *login* — they can log in immediately. Login attempts are auto-audited via `auth_failures` + `audit_log`.

### 4.2 Determine permission level

| Level | Capabilities | Typical role |
|---|---|---|
| `view` | Read metrics, history, logs. No mutations. | On-call observer, auditor |
| `control` | View + restart / WOL on the listed servers. | L1 IT |
| `admin` | View + control + install updates + delete data on the listed servers. | L2/L3 IT |

For **tier-0** servers (production-critical), the operator additionally needs to be part of the dual-control pool — any destructive action requires a second admin's approval.

### 4.3 Grant ACL rows

For each server (or `*` wildcard) the operator needs access to:

```
POST /api/rbac/grant
{
  "username": "alice",         # lower-cased sAMAccountName
  "server_name": "WEBSRV01",   # or "*"
  "permission": "control"      # view | control | admin
}
```

The route auto-writes `rbac_grant` to the audit log with the granting admin's identity. Verify by reading `/api/audit-log?action=rbac_grant` after the grant.

### 4.4 Verify

The operator should be asked to log in once and confirm they see only the servers they should. Their first-login event lands in `audit_log` and `auth_failures` (cleared on successful login) — both verifiable.

## 5. Record-keeping

The grant + verification login are auto-audited; no out-of-band paperwork beyond the original access-request form is required by Prism. Quality may require a copy of the request form attached to the SOP execution record.

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (RBAC-admin) | | | |
| Reviewed by (manager) | | | |

---
*End of SOP.*
