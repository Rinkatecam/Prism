# 05 — Configuration Specification

| Field | Value |
|---|---|
| Document ID | CSV-05 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `04_DS.md` |

## Purpose

This document enumerates every configurable parameter in Prism: where it lives, what it does, valid range, default, operational impact, and validation reference. A GAMP 5 install requires that the configuration set used in the validated environment be documented and change-controlled.

## A. Locations of configuration

| Location | What lives here | Format |
|---|---|---|
| `config.json` (repo root) | Server inventory + global settings (formerly two-file) | JSON |
| `data/settings.toml` (optional, if used) | Some settings can live here per deployment choice | TOML |
| `data/.key` | Fernet key for password encryption | binary |
| Env vars (`PRISM_*`) | Overrides for `config.json` and runtime knobs | strings |
| Database `settings` mechanism | Not currently used — all settings via files | — |

The single source of truth is **`config.json`**. The `data/settings.toml` path exists for deployments that want to keep server inventory separate from runtime settings; both shapes are supported by `config_manager.ConfigManager`.

## B. Per-server configuration (`config.json.servers[]`)

Each entry:

| Field | Type | Default | Range / Notes |
|---|---|---|---|
| `name` | string | required | Display name; used as primary key everywhere |
| `host` | string | required | FQDN or IP |
| `username` | string | required | Service account |
| `password` | string | required | **Encrypted** at rest via `crypto_utils.encrypt_password` |
| `domain` | string | "" | Optional, prepended to username if non-empty |
| `port` | int | 5985 (HTTP) / 5986 (HTTPS) | WinRM port |
| `use_https` | bool | false | If true, port auto-flips to 5986 unless overridden |
| `https_skip_verify` | bool | false | Tier-0 servers cannot set this true (FS-114) |
| `tier` | int | 1 | 0 = production-critical (dual-control); 1 = staging; 2 = dev |
| `type` | string | "generic" | Role hint for threshold defaults: `web_server`, `database`, `domain_controller`, `application`, etc. |
| `thresholds` | object | from `type` defaults | Per-metric `*_warning` / `*_critical` |
| `mac_address` | string | null | For Wake-on-LAN (URS-032) |
| `tags` | array[string] | [] | UI grouping |

### thresholds sub-object

| Field | Type | Default | Notes |
|---|---|---|---|
| `cpu_warning` | int 0-100 | 75 | % |
| `cpu_critical` | int 0-100 | 90 | % |
| `ram_warning` | int 0-100 | 80 | % |
| `ram_critical` | int 0-100 | 90 | % |
| `disk_warning` | int 0-100 | 80 | % |
| `disk_critical` | int 0-100 | 90 | % |

## C. Global settings (`config.json.settings`)

### Collector

| Key | Type | Default | Notes / range |
|---|---|---|---|
| `poll_interval_seconds` | int | 60 | Floor 30, cap 3600. Drives METRICS cadence (FS-001) |
| `collector_v2_num_workers` | int | 15 | Number of worker threads in the pool |

### Detection

| Key | Type | Default | Notes |
|---|---|---|---|
| `baseline_detection.enabled` | bool | false | Activates Z-score baseline alerts (FS-012) |
| `baseline_detection.sigma_warning` | float | 2.0 | Standard deviations |
| `baseline_detection.sigma_critical` | float | 3.0 | |
| `anomaly_detection.enabled` | bool | false | Activates `detect_anomalies` (FS-011) |
| `anomaly_detection.cpu_warning_window_cycles` | int | 5 | M of N-of-M gate (FS-003) |
| `anomaly_detection.cpu_warning_consecutive_cycles` | int | 3 | N of N-of-M gate |
| `anomaly_detection.suppression_hours` | int | 4 | Per-(server, metric, direction) cooldown |
| `alert_fatigue_threshold` | float | 50.0 | Score above this throttles email/webhook (FS-014) |

### Auth

| Key | Type | Default | Notes |
|---|---|---|---|
| `auth.enabled` | bool | false | Activates the login wall (FS-070) |
| `auth.type` | string | "ldap" | "ldap" or "backup_admin_only" |
| `auth.ldap_url` | string | "" | `ldap://host:389` or `ldaps://host:636` |
| `auth.ldap_base_dn` | string | "" | e.g. `DC=EXAMPLE,DC=COM` |
| `auth.ldap_user_filter` | string | "(sAMAccountName={username})" | LDAP filter |
| `auth.ldap_bind_user` | string | "" | Service account |
| `auth.ldap_bind_password` | string | "" | Encrypted at rest |
| `auth.session_timeout_minutes` | int | 480 | 8 h default |
| `auth.remember_me_timeout_minutes` | int | 43200 | 30 d |
| `auth.lockout_threshold` | int | 10 | Failed attempts in window |
| `auth.lockout_window_minutes` | int | 30 | |
| `auth.lockout_duration_minutes` | int | 15 | |
| `auth.backup_admin.password_hash` | string | (set on install) | werkzeug hash |
| `auth.allowed_users` | array[string] | [] | Allowlist (per-environment policy) |

### Workflows

| Key | Type | Default | Notes |
|---|---|---|---|
| `workflows.sandbox.enabled` | bool | true | FS-053 sandbox toggle |
| `workflows.sandbox.allowed_cmdlets` | array[string] | [] | Adds to default 92-cmdlet allowlist |
| `workflows.sandbox.max_script_chars` | int | 10000 | Hard cap on user PS length |

### Maintenance & alerts

| Key | Type | Default | Notes |
|---|---|---|---|
| `maintenance_windows` | array[obj] | [] | See FS-004 / DS-112 |
| `email.enabled` | bool | false | Activates email dispatch |
| `email.smtp_host`, `email.smtp_port`, `email.username`, `email.password` | string/int | — | `password` encrypted |
| `email.use_tls` | bool | true | |
| `email.from_address`, `email.to_addresses[]` | string/array | — | |
| `webhooks.teams_webhook_url` | string | "" | Default Teams webhook |
| `webhooks.enabled_alerts` | array[string] | ["critical","warning"] | Which severities go to webhook |

### TLS & drift

| Key | Type | Default | Notes |
|---|---|---|---|
| `tls_monitoring.enabled` | bool | false | FS-016 |
| `tls_monitoring.warning_days` | int | 30 | |
| `tls_monitoring.critical_days` | int | 7 | |
| `tls_monitoring.check_interval_cycles` | int | — | Period in collector cycles |
| `drift_detection.enabled` | bool | false | FS-018 |
| `drift_detection.snapshot_types` | array[string] | ["services","hotfixes","local_admins"] | |
| `drift_detection.check_interval_cycles` | int | — | |

### Health checks

| Key | Type | Default | Notes |
|---|---|---|---|
| `health_checks.enabled` | bool | false | FS-017 master switch |
| `health_checks[]` | array[obj] | [] | Per-probe config (target host/port, type, interval) |

### Security alerts

| Key | Type | Default | Notes |
|---|---|---|---|
| `security_alerts.failed_login_tracking` | bool | false | FS-015 |
| `security_alerts.login_failure_threshold` | int | 10 | In trailing 15 min |

### Restart scheduler

| Key | Type | Default | Notes |
|---|---|---|---|
| `scheduled_server_restart_schedule.enabled` | bool | false | |
| `scheduled_server_restart_schedule.schedule` | string | "weekly" | "daily" / "weekly" / "monthly" |
| `scheduled_server_restart_schedule.day_of_week` | int | 2 | 0=Mon..6=Sun (weekly) |
| `scheduled_server_restart_schedule.day_of_month` | int | 1 | 1-31 (monthly) |
| `scheduled_server_restart_schedule.time` | string | "03:00" | HH:MM local |
| `scheduled_server_restart_schedule.install_windows_updates` | bool | false | |
| `scheduled_server_restarts[]` | array[obj] | [] | Per-server overrides |

### Scheduled reports

| Key | Type | Default | Notes |
|---|---|---|---|
| `scheduled_reports.enabled` | bool | false | |
| `scheduled_reports.daily_enabled` | bool | true | |
| `scheduled_reports.daily_time` | string | "07:00" | |
| `scheduled_reports.weekly_enabled` | bool | false | |
| `scheduled_reports.weekly_day` | string | "monday" | |
| `scheduled_reports.weekly_time` | string | "07:00" | |
| `scheduled_reports.email_report` | bool | true | |
| `scheduled_reports.include_pdf` | bool | false | Requires WeasyPrint runtime |

### Locale

| Key | Type | Default | Notes |
|---|---|---|---|
| `timezone` | string | "Europe/Berlin" | Any valid IANA zone |
| `language` | string | "en" | en / de / fr / es / ja |
| `date_format` | string | "DD.MM.YYYY" | |
| `time_format` | string | "24h" | 24h / 12h |

### Retention

| Key | Type | Default | Notes |
|---|---|---|---|
| `retention_days` | int | 30 | Applied to most tables (see appendix D) |
| Auth-failures window | hard-coded | 24 h | Tighter than general retention |

## D. Environment variables

| Var | Purpose | Notes |
|---|---|---|
| `PRISM_SECRET_KEY` | Flask session signing key | If unset, generated on first boot and persisted; 32+ bytes |
| `PRISM_PASSWORD_KEY` | Fernet key for password encryption | If unset, falls back to `data/.key` |
| `PRISM_CONFIG_PATH` | Override `config.json` path | Default repo root |
| `PRISM_DB_PATH` | Override DB path | Default `data/prism.db` |

## E. Key files outside `config.json`

| Path | Purpose | Permissions |
|---|---|---|
| `data/prism.db` | Main DB (WAL mode) | rw for service account only |
| `data/prism.db-wal` / `db-shm` | WAL sidecar files | same |
| `data/audit_mirror.jsonl` | SIEM mirror of audit log | append-only at OS level |
| `data/audit_archive/*.jsonl` | Aged audit archives | append-only |
| `data/install_state.json` | Cross-restart install state | atomic write via tempfile + rename |
| `data/.key` | Password encryption key | 0400 owner-read-only |
| `data/config_backups/*.json` | Auto + manual config backups | owner-only |

## F. Change-control posture

- Configuration changes via the UI (`POST /api/config`) write a backup snapshot to `data/config_backups/` and write `config_update` to `audit_log`.
- Manual edits to `config.json` on disk are detected on next 5-second `ConfigManager` mtime check.
- A re-key (`tools/rekey.py`) writes a new `data/.key` and re-encrypts in place — must be run while Prism is stopped.
- The full list of audit-bearing config actions is in Appendix B's "Audit-event vocabulary" section.

## G. Validation of configuration

| Aspect | Validation |
|---|---|
| Server passwords encrypted at rest | `crypto_utils` Fernet; tested by `test_rekey_tool.py` |
| Password-mask round-trip safe | `routes/api/config.py:save_config` preserves stored encrypted value on masked-input save |
| Sensitive `auth.*` fields cannot be set by non-RBAC-admin POST | Strip filter in `save_config` |
| LDAP unreachable at boot | `auth.py:assert_ldap_startup_safe` (SystemExit) |
| Timezone valid | `maintenance.py` refuses naive datetime if invalid |
| Workflow sandbox cannot be silently disabled | Default `enabled=true`; flipping it is an audited config change |
| Tier-0 cannot disable cert verification | `routes/api/config.py` S3-12 guard |

## H. Recommended production-baseline configuration

For a tier-1 production deployment monitoring a regulated infrastructure fleet:

| Setting | Recommended |
|---|---|
| `auth.enabled` | **true** |
| `auth.type` | **ldap** (with backup-admin as fallback) |
| `auth.session_timeout_minutes` | **480** (8 h) |
| `auth.lockout_*` | defaults (10 / 30 / 15) |
| `workflows.sandbox.enabled` | **true** |
| `baseline_detection.enabled` | **true** |
| `tls_monitoring.enabled` | **true** |
| `drift_detection.enabled` | **true** |
| `security_alerts.failed_login_tracking` | **true** |
| `scheduled_reports.enabled` | **true** (daily) |
| `retention_days` | **30** minimum; longer if regulator requires |
| `audit_log` | append-only triggers active (default); JSONL mirror active |

This baseline should be locked in the change-controlled configuration register for the validated environment.

---
*End of document.*
