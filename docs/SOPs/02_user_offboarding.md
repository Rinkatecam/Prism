# SOP-02 — User Offboarding

| Field | Value |
|---|---|
| Document ID | SOP-02 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | RBAC-admin |
| Implements | URS-074, URS-075 / FS-074, FS-075 |
| Closes finding | F-SOP-1 |
| Review cadence | Annual |

## 1. Purpose

Revoke a leaving operator's access to Prism on the same business day they leave, in a way that is immediate, auditable, and complete.

## 2. Scope

Every operator who leaves the organisation, transfers to a role no longer requiring Prism, or whose access must be terminated for cause.

## 3. Prerequisites

- HR notification of the departure (out-of-band, normally via the corporate offboarding ticket).
- The RBAC-admin performing the action is NOT the leaving operator.

## 4. Procedure

### 4.1 Disable login

```
POST /api/admin/disable-user
{ "username": "alice" }
```

Blocks all future logins for this user. Writes `user_disabled` to the audit log.

### 4.2 Kill any active session

```
POST /api/admin/kill-session
{ "username": "alice", "login_time": "<from /api/admin/active-sessions>" }
```

Forces immediate logout. Writes `session_killed` to the audit log. Necessary because `disable-user` only blocks *future* logins; an already-active session has to be revoked separately.

### 4.3 Revoke every ACL row

For each server the operator had access to:

```
POST /api/rbac/revoke
{ "username": "alice", "server_name": "WEBSRV01" }
```

Repeat for `*` (wildcard) if it was granted. Writes `rbac_revoke` per row.

### 4.4 Verify removal

```
GET /api/rbac/acl?username=alice
```

Must return an empty list.

```
GET /api/admin/active-sessions
```

Must NOT show the operator.

### 4.5 Preserve the audit trail

Do NOT delete the operator's historical `audit_log` rows — they're append-only by design (`audit_log_no_update`/`audit_log_no_delete` triggers) and they're the regulatory evidence of every action the operator ever performed.

## 5. Compensating control

If for any reason the offboarding cannot be performed in the app (e.g. Prism is down), AD account disable is the failsafe — Prism's `_require_auth` consults LDAP on every request, so a disabled AD account is denied login regardless of Prism's ACL table.

## 6. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (RBAC-admin) | | | |
| Reviewed by | | | |

---
*End of SOP.*
