# Appendix D — SQLite Database Schema Catalogue

*Source: automated inventory pass, 2026-05-22 against `database.py:SCHEMA_SQL` (lines 19-509). Referenced from `docs/csv/04_DS.md`, `docs/csv/11_DATA_INTEGRITY.md`, and `docs/csv/12_AUDIT_TRAIL.md`.*

**DB file**: `C:\Prism\data\prism.db` — SQLite WAL mode, `busy_timeout=5000` ms.
**Audit mirror**: `C:\Prism\data\audit_mirror.jsonl` — append-only for SIEM out-of-band integrity.

## Table catalogue

| # | Table | Class | Retention | Append-only? | Notes |
|---|---|---|---|---|---|
| 1 | `metrics` | Telemetry | 30 d | yes (per-cycle insert) | CPU/RAM/disk samples |
| 2 | `events` | Alert record | 30 d | yes | `event_type` enum (`threshold_breach`, `baseline_deviation`, …); `correlation_id` for groupings |
| 3 | `logs` | Win-EventLog cache | 30 d | yes | High volume — filtered + balanced collection |
| 4 | `anomaly_suppression` | Throttle state | 24 h | mutable | UNIQUE(server, metric, direction); cleaned every retention cycle |
| 5 | `anomaly_acknowledgments` | Operator ack/snooze | 30 d (resolved) | append-only | Snoozes pruned when `snooze_until < now` |
| 6 | `audit_log` | Security record | **unlimited** | **enforced via triggers** | SHA-256 hash chain (`prev_hash` + `row_hash`); forensic columns (`source_ip`, `session_id`, `request_id`); JSONL mirror |
| 7 | `revoked_sessions` | Auth contain | none | append-only | UNIQUE(username, login_time) |
| 8 | `disabled_users` | Auth contain | none | mutable (status flip) | Primary key = `username` (lower-cased) |
| 9 | `auth_failures` | Lockout state | 24 h | append-only | Cleared on successful login |
| 10 | `user_server_acl` | RBAC table | none | mutable | UNIQUE(user, server); wildcard `*`; permissive when empty |
| 11 | `pending_approvals` | Dual-control | 1 h (`expires_at`) | mutable | Tier-0 destructive actions |
| 12 | `restart_log` | Restart audit | 30 d | append-only | `run_id` groups one campaign |
| 13 | `server_tags` | Config | none | mutable | UNIQUE(name) collate NOCASE |
| 14 | `server_tag_assignments` | Junction | none | mutable | Cascade on tag delete |
| 15 | `tls_certificates` | Cert inventory | 30 d (`last_checked`) | mutable (upsert on probe) | UNIQUE(server, host, port) |
| 16 | `health_check_config` | Probe config | none | mutable | UNIQUE(server, type, host, port) |
| 17 | `health_check_results` | Probe history | 30 d | append-only | **High volume**: ~4 M rows/year |
| 18 | `metric_baselines` | Per-hour Z-score | none | mutable (upsert by recalc) | UNIQUE(server, metric, hour_of_week) |
| 19 | `failed_logins` | Win 4625/4740 | 30 d | append-only | UNIQUE(server, ts, account, ip) |
| 20 | `config_snapshots` | Drift base | 30 d | append-only | JSON blob `data_json` |
| 21 | `config_changes` | Drift diffs | 30 d | append-only | added / removed / modified / reordered |
| 22 | `incidents` | Correlation | 30 d (resolved only) | mutable (status transitions) | `status` enum: open / resolved / archived |
| 23 | `incident_events` | Junction | 30 d (cascade) | append-only | UNIQUE(incident_id, event_id) |
| 24 | `alert_scores` | Fatigue scoring | rows with score < 0.1 pruned | mutable | UNIQUE(server, metric, event_type) |
| 25 | `server_dependencies` | Topology | none | mutable | UNIQUE(server, depends_on, type) |
| 26 | `runbooks` | Library | none | mutable | Built-in vs user-created distinction |
| 27 | `runbook_executions` | History | 30 d | append-only | Cascade on runbook delete |
| 28 | `workflow_categories` | UI grouping | none | mutable | UNIQUE(name) NOCASE |
| 29 | `workflows` | Canvas | none | mutable | `is_template` preserved on factory-reset |
| 30 | `workflow_executions` | History | 30 d | append-only | Cascade on workflow delete |
| 31 | `workflow_execution_steps` | Per-node log | 30 d (cascade) | append-only | Cascade on execution delete |
| 32 | `server_security_status` | Posture snapshot | 30 d (`last_checked`) | mutable (upsert) | One row per server |

## `audit_log` — full schema

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `timestamp` | TEXT NOT NULL DEFAULT `strftime('%Y-%m-%dT%H:%M:%SZ','now')` | Auto-UTC ISO 8601 |
| `username` | TEXT NOT NULL DEFAULT `'system'` | Auto-filled from `session['username']` if present |
| `action` | TEXT NOT NULL | Action verb (54 values in use) |
| `category` | TEXT NOT NULL DEFAULT `'general'` | `auth`, `config`, `workflow`, `general`, … |
| `details` | TEXT | JSON or free-form, capped 500 chars at insert |
| `source_ip` | TEXT | Auto-filled from `request.remote_addr` |
| `session_id` | TEXT | SHA-256(`username + login_time`) |
| `request_id` | TEXT | Per-request UUID via `flask.g.request_id` |
| `prev_hash` | TEXT | Previous row's `row_hash` |
| `row_hash` | TEXT | SHA-256 of concatenated row fields |

**Triggers** (defence-in-depth append-only):
```sql
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only — UPDATE not allowed'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only — DELETE not allowed'); END;
```

**Integrity verification**: `Database.verify_audit_chain(limit=None) -> {ok, checked, first_break_id, first_break_reason}` walks the chain and reports the first tampering point.

## Indexes

| Table | Index | Columns | Purpose |
|---|---|---|---|
| `metrics` | `idx_metrics_server_time` | (server_name, timestamp DESC) | Dashboard per-server timelines |
| `metrics` | `idx_metrics_timestamp` | (timestamp DESC) | Global timeline |
| `events` | `idx_events_server_time` | (server_name, timestamp DESC) | Per-server events |
| `events` | `idx_events_timestamp` | (timestamp DESC) | Activity feed |
| `logs` | `idx_logs_server_time` | (server_name, timestamp DESC) | Event-log search |
| `audit_log` | `idx_audit_timestamp` | (timestamp DESC) | Audit-log page |
| `audit_log` | `idx_audit_category` | (category) | Category filter |
| `anomaly_acks` | `idx_ack_server_metric` | (server_name, metric) | Ack lookup |
| `tls_certificates` | `idx_tls_expiry` | (days_remaining ASC) | "What's expiring soonest" |
| `health_check_results` | `idx_hc_results_server` | (server_name) | Per-server health |
| `metric_baselines` | `idx_baselines_server` | (server_name, metric) | Baseline lookup |
| `failed_logins` | `idx_failed_logins_server` | (server_name, timestamp DESC) | Heat-map source |
| `restart_log` | `idx_restart_log_run` | (run_id) | Per-campaign |
| `alert_scores` | `idx_alert_scores` | (score DESC) | "Show me the noisiest" |
| `workflow_executions` | `idx_wf_exec_workflow` | (workflow_id, started_at DESC) | Per-workflow history |
| `incidents` | `idx_incidents_status` | (status, created_at DESC) | Open-first |

(Other indexes listed in `database.py:SCHEMA_SQL`.)

## Retention cleanup (driven by `Database.cleanup_old_data(retention_days=30)`)

Called by the `retention` periodic job once per hour. Wipes from:
`metrics`, `events`, `logs`, `restart_log`, `tls_certificates` (`last_checked`), `health_check_results`, `failed_logins`, `config_snapshots`, `config_changes`, `runbook_executions`, `workflow_executions` (cascades to `workflow_execution_steps`), `server_security_status` (`last_checked`).
Plus: `anomaly_suppression` (24 h via `cleanup_anomaly_suppression`), `auth_failures` (24 h), `pending_approvals` (1 h expires), `alert_scores` rows with `score < 0.1`.

**Never touched by retention**: `audit_log`, `revoked_sessions`, `disabled_users`, `user_server_acl`, `server_tags`, `server_tag_assignments`, `health_check_config`, `metric_baselines`, `server_dependencies`, `runbooks`, `workflow_categories`, `workflows` — these are reference / configuration data.

## Configuration knobs (from `database.py` + caller defaults)

- `DB_PATH` = `data/prism.db`
- `AUDIT_MIRROR_PATH` = `data/audit_mirror.jsonl`
- `PRAGMA journal_mode=WAL`
- `PRAGMA busy_timeout=5000` (5 s)
- Default retention = 30 days (caller-overridable)

---
*End of appendix.*
