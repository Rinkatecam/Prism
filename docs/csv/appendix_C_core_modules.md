# Appendix C — Core Module Inventory (excluding collector_v2/ and routes/)

*Source: automated inventory pass, 2026-05-22. Referenced from `docs/csv/04_DS.md` (Design Specification).*

| Module | Lines | Purpose | Threading | DB tables | Notes |
|---|---|---|---|---|---|
| `app.py` | 563 | Flask app factory; spawns daemons; CSP/CSRF middleware; watchdog | Main + 4 daemons | `audit_log` (watchdog writes) | The boot script; wires everything together |
| `database.py` | 3487 | SQLite schema + DAO; audit hash-chain; JSONL audit mirror | `_write_lock` serialises writes; WAL for readers | 32 tables | Append-only triggers on `audit_log` |
| `ps_sandbox.py` | 278 | Regex allowlist + HARD_DENY for user PowerShell | Stateless | — | Critical control on workflow_engine |
| `workflow_engine.py` | 1821 | Canvas execution; trigger blocks; var substitution; scheduler loop | Daemon scheduler + per-execution daemon threads | `workflows`, `workflow_executions`, `workflow_execution_steps`, `events`, `audit_log` | `_event_trigger_state` (dict, no lock; atomic dict ops on CPython) |
| `analytics.py` | 1397 | Baseline detection, rate anomaly, disk forecast, correlation rules | `_BASELINE_CACHE_LOCK` for cache | `metric_baselines`, `anomaly_suppression`, `incidents` | 5-min TTL baseline cache, ≤10 000 entries |
| `detection.py` | 547 | Status decision tree (6 phases); CPU N-of-M gate; anomaly dispatch | Per-server CPU history deque (single writer per server) | `anomaly_suppression`, `events`, `alert_scores` | Single source of truth for "what status is this server in?" |
| `maintenance.py` | 180 | Maintenance-window evaluation, threshold loosening, alert suppression | Stateless | — | Uses `zoneinfo.ZoneInfo` — refuses naive datetime |
| `tls_monitor.py` | 120+ | TLS cert expiry probe + alerts | Stateless (called from periodics) | `tls_certificates`, `events` | Configurable warning/critical day thresholds |
| `healthchecks.py` | 130+ | TCP/HTTP/UDP/ICMP probes, state-change events | Stateless (called from periodics) | `health_check_config`, `health_check_results`, `events` | — |
| `drift.py` | 180+ | Per-server snapshot diff (services / hotfixes / local admins) | Stateless | `config_snapshots`, `config_changes`, `events` | Gated on `drift_detection.enabled` |
| `failed_logins.py` | 200+ | Event 4625/4740 collection + threshold alerts | Stateless | `failed_logins`, `events`, `alert_scores` | Calls `email_alerts` + `webhooks` on threshold |
| `scheduled_reports.py` | 100+ | Daily / weekly PDF digest emission | Module-level `_last_daily_report_date` / `_last_weekly_report_date` | reads all tables | Re-fire-safe across restart via date marker |
| `alert_scoring.py` | 120+ | Fatigue noise score + throttle gate | Stateless | `alert_scores` | Used by `detection.py` + `failed_logins.py` + `tls_monitor.py` |
| `auth.py` | 300+ | LDAP/AD + backup-admin login, session, lockout | Flask before_request middleware | `revoked_sessions`, `disabled_users`, `auth_failures` | Backup admin requires ≥12 chars, ≥1 digit, ≥1 symbol |
| `restart_scheduler.py` | 300+ | Scheduled per-server restart; optional WU install; post-restart validation | Daemon scheduler + per-fleet-run daemon thread | `restart_log`, `events`, `audit_log` | 2-min schedule window; marker file gates double-fire |
| `health_checker.py` | 150+ | Low-level TCP/HTTP/HTTPS/UDP/ICMP probe primitives | Stateless | — | Pure socket library; no DB |
| `i18n.py` | 500+ | Translation registry (en/de/fr/es/ja) + format helpers | Stateless | — | 5 languages, ~500 keys |
| `config_manager.py` | 300+ | `config.json` + `data/settings.toml` loading, password encryption, mtime cache | `_lock` for cache refresh | — | Decrypts passwords on each `get_servers()` call |

## Module-level globals/locks (critical for thread-safety review)

| Where | Name | Type | Purpose |
|---|---|---|---|
| `database.py` | `_write_lock` | `threading.Lock` | Serialises all writes |
| `analytics.py` | `_BASELINE_CACHE_LOCK` | `threading.Lock` | Guards `_BASELINE_CACHE` |
| `analytics.py` | `_BASELINE_CACHE` | dict | (server, segment) → (ts, metrics) |
| `analytics.py` | `_cache_hits` / `_cache_misses` / `_cache_evictions` | int | Cache telemetry |
| `detection.py` | `_cpu_warn_history` | dict[str, deque] | Per-server CPU N-of-M ring |
| `workflow_engine.py` | `_event_trigger_state` | dict[int, dict] | Per-workflow event-trigger debounce state |
| `workflow_engine.py` | `_last_heartbeat` | float | Watchdog heartbeat |
| `restart_scheduler.py` | `_last_heartbeat`, `last_run_results`, `_lock` | various | Watchdog + UI state |
| `config_manager.py` | `_lock` | `threading.Lock` | Guards cache reload |
| `app.py` | `_watchdog_state` | dict | Thread-name → health |

## Daemon threads spawned in `app.py` at startup

1. **restart-scheduler** — `restart_scheduler.restart_scheduler_loop`
2. **workflow-scheduler** — `workflow_engine.workflow_scheduler_loop`
3. **collector-v2** — pipeline of supervisor + aggregator + 15 workers + periodics (started in one call)
4. **watchdog** — monitors 1-3 and `log_audit`s on death

---
*End of appendix.*
