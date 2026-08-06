# Appendix B — HTTP API Surface Inventory

*Source: automated inventory pass, 2026-05-22. Cited from `docs/csv/03_FS.md` (Functional Specification) and `docs/csv/12_AUDIT_TRAIL.md` (Audit Trail).*

## Summary table

| File | Lines | Auth gates | Audit calls | DB tables touched |
|------|-------|------------|-------------|--------------------|
| `_shared.py` | 286 | (defines all) | — | — |
| `__init__.py` | 51 | — | — | — |
| `workflows.py` | 540 | `_require_auth`, per-server RBAC, tier-0 approval | 12 | workflows, runbook_executions, audit_log, workflow_categories |
| `updates.py` | 1656 | per-server `admin` | 7 | _update_install_state (mem + JSON), audit_log |
| `servers.py` | 885 | mixed (some open read, mutations gated) | 8 | servers, metrics, events, logs, tags, failed_logins, audit_log |
| `health.py` | 458 | `_require_auth` | 5 | health_checks, tls_certificates, audit_log |
| `config.py` | 866 | `_require_rbac_admin` for mutations | 8 | settings rows, config snapshots, audit_log |
| `metrics.py` | 285 | mixed | 3 | baselines, acknowledgments, incidents, audit_log |
| `power.py` | 228 | per-server `admin`, `_require_rbac_admin` for Flask restart | 4 | server state, audit_log |
| `rbac.py` | 287 | `_require_rbac_admin` for mutations | 5 | acl, audit_log, approvals, sessions |
| `misc.py` | 949 | mixed; global approval for `factory_reset`/`delete_all` | 11 | all tables, audit_log |
| `reports.py` | 491 | open read | 0 | metrics, events (read-only) |

**Total routes**: 81 functional endpoints (plus internal helpers).
**Total audit event types**: 54 distinct `action` values.

## Route catalogue (by file)

### workflows.py

| Method | Path | Auth | Audit |
|---|---|---|---|
| GET | `/api/runbooks` | none | — |
| POST | `/api/runbooks` | `_require_auth` | `create_runbook` |
| PUT | `/api/runbooks/<id>` | `_require_auth` | `update_runbook` |
| DELETE | `/api/runbooks/<id>` | `_require_auth` | `delete_runbook` |
| POST | `/api/runbooks/<id>/execute` | server:admin + tier-0 approval | `execute_runbook` |
| GET | `/api/runbooks/executions` | none | — |
| GET | `/api/runbooks/executions/<id>` | none | — |
| GET | `/api/workflow-categories` | none | — |
| POST | `/api/workflow-categories` | `_require_auth` | `create_workflow_category` |
| PUT | `/api/workflow-categories/<id>` | `_require_auth` | `update_workflow_category` |
| DELETE | `/api/workflow-categories/<id>` | `_require_auth` | `delete_workflow_category` |
| GET | `/api/workflows` | none | — |
| GET | `/api/workflows/templates` | none | — |
| POST | `/api/workflows` | `_require_auth` | `create_workflow` |
| PUT | `/api/workflows/<id>` | `_require_auth` | `update_workflow` |
| DELETE | `/api/workflows/<id>` | `_require_auth` | `delete_workflow` |
| POST | `/api/workflows/<id>/clone` | `_require_auth` | `clone_workflow` |
| POST | `/api/workflows/<id>/execute` | `_require_auth` + per-server RBAC | `execute_workflow` / `rbac_denied_workflow_execute` |
| GET | `/api/workflows/executions` | none | — |
| GET | `/api/workflows/executions/<id>` | none | — |
| POST | `/api/workflows/executions/<id>/cancel` | `_require_auth` | — |
| POST | `/api/workflows/validate-script` | `_require_auth` | — |

### updates.py

| Method | Path | Auth | Audit |
|---|---|---|---|
| POST | `/api/sync-now` | none | — |
| POST | `/api/sync-updates-now` | none | — |
| POST | `/api/sync-logs-now` | none | — |
| POST | `/api/servers/<n>/install-updates` | server:admin | `install_updates` |
| POST | `/api/servers/<n>/install-updates-direct` | server:admin | `install_updates_direct` |
| POST | `/api/servers/<n>/cancel-updates` | server:admin | `cancel_updates` |
| GET | `/api/servers/<n>/update-task-info` | none | — |
| GET | `/api/servers/<n>/update-status` | none | — |
| GET | `/api/servers/<n>/updates` | none | — |
| *(auto-restart watcher spawned by install endpoint)* | — | — | `auto_restart` |

### servers.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/servers` | `_require_auth` |
| GET | `/api/servers/<n>` | none |
| GET | `/api/servers/<n>/history` | none |
| GET | `/api/servers/<n>/restart-readiness` | server:read |
| GET | `/api/servers/<n>/logs` | none |
| GET | `/api/servers/<n>/analytics` | none |
| DELETE | `/api/servers/<n>/data` | server:admin |
| GET | `/api/servers/<n>/hardware` | none |
| GET | `/api/servers/<n>/services` | `_require_auth` |
| GET | `/api/servers/<n>/processes` | `_require_auth` |
| GET | `/api/servers/<n>/ports` | `_require_auth` |
| GET | `/api/servers/<n>/sla` | none |
| GET | `/api/servers/export-csv` | `_require_auth` |
| POST | `/api/servers/import-csv` | `_require_auth` |
| POST | `/api/servers/bulk-thresholds` | `_require_auth` |
| POST | `/api/servers/duplicate` | `_require_auth` |
| GET | `/api/servers/<n>/tags` | none |
| POST | `/api/servers/<n>/tags` | `_require_auth` |
| DELETE | `/api/servers/<n>/tags/<id>` | `_require_auth` |
| GET | `/api/servers/<n>/failed-logins` | none |
| GET | `/api/servers/<n>/failed-logins/heatmap` | none |
| GET | `/api/servers/<n>/security-status` | none |
| POST | `/api/servers/<n>/security-status/check` | none |

### health.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/system/health` | `_require_auth` |
| GET | `/api/collector/pulse` | `_require_auth` |
| POST | `/api/system/vacuum` | `_require_auth` |
| GET | `/api/tls-certificates[/expiring]` | none |
| POST | `/api/tls-certificates/check` | `_require_auth` |
| DELETE | `/api/tls-certificates/<id>` | `_require_auth` |
| GET | `/api/health-checks[/<server>/config]` | none |
| POST | `/api/health-checks/config` | `_require_auth` |
| DELETE | `/api/health-checks/config/<id>` | `_require_auth` |
| POST | `/api/health-checks/probe` | `_require_auth` |

### config.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/config` | none (passwords masked) |
| POST | `/api/config` | `_require_rbac_admin` (strips sensitive `auth.*` keys) |
| POST | `/api/test-email` | none |
| POST | `/api/test-connection` | `_require_auth` |
| GET | `/api/cert-info` | `_require_auth` |
| GET | `/api/csrf-token` | none |
| GET / POST | `/api/config/backups[/restore /download /upload]` | `_require_auth` |
| POST | `/api/test-webhook` | `_require_auth` |
| POST | `/api/discover-servers` | `_require_rbac_admin` |
| GET | `/api/config-changes` | none |

### metrics.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/status` / `/api/analytics/summary` / `/api/digest` | none |
| GET / POST / DELETE | `/api/anomalies/acknowledge[/<id>]` | none |
| GET | `/api/anomalies/acknowledgments` | none |
| GET | `/api/baselines/<n>[/coverage]/<metric>` | none |
| POST | `/api/baselines/recalculate` | `_require_auth` |
| GET / PUT | `/api/incidents[/<id>/open/count]` | mixed |
| GET / POST | `/api/alert-scores[/digest /reset]` | mixed |

### power.py

| Method | Path | Auth |
|---|---|---|
| POST | `/api/restart` (Flask) | `_require_rbac_admin` + rate-limit |
| POST | `/api/servers/<n>/power` | server:admin |
| POST | `/api/servers/<n>/wol` | server:control |

### rbac.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/audit-log[/export]` | `_require_auth` |
| GET / POST | `/api/rbac/acl /grant /revoke /me` | `_require_rbac_admin` (mutations) |
| GET / POST | `/api/approvals[/<id>/decide]` | mixed |
| POST | `/api/audit-log/archive` | `_require_rbac_admin` |
| POST | `/api/admin/kill-session /disable-user /enable-user` | `_require_rbac_admin` |
| GET | `/api/admin/active-sessions` | `_require_rbac_admin` |
| GET | `/api/system/ldap-health` | `_require_auth` |

### misc.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/logs/search` | `_require_auth` |
| GET | `/api/collector-status / /api/sla/summary` | none |
| POST | `/api/ldap/query` | `_require_rbac_admin` |
| GET / POST / DELETE | `/api/maintenance-windows[/<idx>]` | `_require_auth` |
| POST | `/api/data/clean` | `_require_rbac_admin` |
| POST | `/api/data/delete` | `_require_rbac_admin` + global approval |
| POST | `/api/data/factory-reset` | `_require_rbac_admin` + global approval |
| GET | `/api/scheduled-restarts` / `/api/restart-log` / `/api/restart-status` | none |
| POST | `/api/scheduled-restarts` / `/api/restart-now` | `_require_auth` + per-server RBAC |
| GET / POST / PUT / DELETE | `/api/tags[/<id>]` | mixed |
| GET | `/api/failed-logins/summary` | none |
| GET | `/api/servers/<n>/config-changes / config-snapshot/<type>` | none |
| POST | `/api/servers/<n>/config-snapshot` | `_require_auth` |
| GET / POST / DELETE | `/api/dependencies[/<id>]` | mixed |
| GET | `/api/topology/data / svg / blast-radius/<server>` | none |

### reports.py

| Method | Path | Auth |
|---|---|---|
| GET | `/api/reports/csv/metrics events capacity` | none |
| GET | `/api/reports/json/metrics events capacity` | none |
| GET | `/api/reports/pdf[/comparison]` | none |
| GET | `/api/servers/compare[-events stats segmented]` | none |

## Background threads spawned from request handlers

| Spawner | What it does | Risk-relevant notes |
|---|---|---|
| `updates._spawn_auto_restart_watcher` | Polls remote `update-status.json` every 30 s for up to 90 min; fires restart on `restart_required` | Daemon thread; survives Flask restart only via the periodics `auto_restart_scanner` safety net |
| `misc.restart_now` | Background thread runs `restart_scheduler.execute_server_restarts` | Daemon |
| `power.restart_server` (Flask self-restart) | 1 s sleep then `os.execv` | Daemon |

## Audit-event vocabulary (54 actions)

Used by `audit_log.action` column — see `Appendix C` for the table schema.

`create_runbook, update_runbook, delete_runbook, execute_runbook, create_workflow_category, update_workflow_category, delete_workflow_category, create_workflow, update_workflow, delete_workflow, clone_workflow, execute_workflow, rbac_denied_workflow_execute, install_updates, install_updates_direct, cancel_updates, auto_restart, delete_server_data, assign_tag, remove_tag, vacuum_db, check_tls, delete_tls_cert, save_health_check_config, delete_health_check_config, recalculate_baselines, update_incident, reset_alert_scores, flask_restart, power:restart, power:shutdown, power:wol, rbac_grant, rbac_revoke, session_killed, user_disabled, user_enabled, approval_requested, approval_decided, tier0_approval_consumed, tier0_global_approval_consumed, audit_archive, clean_data, delete_all, factory_reset, update_scheduled_restarts, restart_now, create_tag, update_tag, delete_tag, add_dependency, remove_dependency, manual_snapshot, config_update`.

---
*End of appendix.*
