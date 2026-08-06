# SOP-03 — Periodic ACL Review

| Field | Value |
|---|---|
| Document ID | SOP-03 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | RBAC-admin + service owners |
| Implements | URS-075 / FS-075 |
| Closes finding | F-SOP-2 |
| Review cadence | **Quarterly** |

## 1. Purpose

Confirm that every Prism user with persistent access still needs it, with the role appropriate to their current job. Detects "creeping privilege" — operators who collected admin grants over time without ever giving any back.

> **Live status:** ACL currently has **[[csv:acl_count]]** rows. Last reviewed: [[csv:last_execution.SOP-03]]. Next due: [[csv:next_due.SOP-03]].

## 2. Trigger

- Quarterly (suggested: first Monday of each quarter).
- Out-of-cycle if a significant org change (re-org, RIF, acquisition) materially changes the access landscape.

## 3. Procedure

### 3.1 Snapshot the current ACL

```
GET /api/rbac/acl
```

Export to a CSV that you keep alongside the SOP execution record.

### 3.2 Per row, ask the service owner

For each `(username, server_name, permission)` row, ask the service owner (or the operator's current line manager):

- Does **alice** still need **admin** on **DBSRV02**? Y / N → if N: revoke.
- Is **admin** still the right level, or has the job changed? → adjust.

For wildcard `*` grants (fleet-wide admin), require explicit sign-off — these are the most powerful permissions and should be reviewed by Quality.

### 3.3 Apply changes

Use SOP-01 (grant) and SOP-02 (revoke) for any row that needs adjusting. Each change auto-audits.

### 3.4 File the review

Save the CSV + the decisions list under `data/sop_records/acl_review_<YYYYQ#>.csv` (out-of-app, owner-only readable). At minimum it must contain:

- Reviewer name + date.
- For each row: keep / revoke / adjust.
- Service-owner sign-off for each kept row.

## 4. Exit criteria

- Every persistent ACL row has been positively confirmed.
- The audit log shows a balanced set of grants + revokes for the cycle.
- No `*`-wildcard grant exists without explicit Quality sign-off recorded.

## 5. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (RBAC-admin) | | | |
| Service-owner sign-off | | | |
| Quality sign-off | | | |

---
*End of SOP.*
