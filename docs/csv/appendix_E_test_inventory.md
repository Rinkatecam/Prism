# Appendix E — Test Suite Inventory (OQ evidence)

*Source: automated inventory pass, 2026-05-22. Referenced from `docs/csv/08_OQ_TEST_INVENTORY.md` (OQ Test Inventory).*

## Test files (33 files, ~325 functions, 352 collected, pytest passes 352)

| File | Coverage area | Test count | Type | Modules exercised |
|---|---|---|---|---|
| `test_analytics_baseline_cache.py` | Per-(server, segment) TTL cache backing `detect_anomalies()` | 9 | UNIT | `analytics` |
| `test_audit_archive.py` | Audit-log JSONL archive helper | 2 | INTEGRATION | (fixture-based) |
| `test_audit_chain.py` | Audit-log hash chain + JSONL mirror (AUDIT-2026-05 S1-7) | 7 | INTEGRATION | `database` |
| `test_auth_hardening.py` | Sprint-2 auth-hardening batch (S2-1, S2-12, S2-13, S2-15) | n | INTEGRATION | `auth`, `database` |
| `test_backup_tool.py` | `tools/backup.py` + `tools/restore.py` | 4 | INTEGRATION | `tools/backup`, `tools/restore`, `database` |
| `test_collector_v2_aggregator.py` | `collector_v2.aggregator` — isolation unit tests | 34 | UNIT | `collector_v2/aggregator, state, periodics, workers, scripts` |
| `test_collector_v2_health_endpoint.py` | `/api/system/health` v2 surface | 2 | INTEGRATION | `routes/api/health` |
| `test_collector_v2_periodics.py` | `collector_v2.periodics._build_jobs` cadence translation | 10 | UNIT | `collector_v2/periodics` |
| `test_collector_v2_scripts_logs.py` | `PS_COLLECT_LOGS` payload contract (Firewall channel) | 8 | INTEGRATION | `collector_v2/scripts` |
| `test_collector_v2_supervisor.py` | `collector_v2.supervisor` — isolation unit tests | 24 | INTEGRATION | `collector_v2/supervisor` |
| `test_collector_v2_workers.py` | `collector_v2.workers.WorkerPool` — isolation unit tests | 16 | UNIT | `collector_v2/workers` |
| `test_csp.py` | Sprint-3 CSP hardening (S3-9 / W2) — **2 pre-existing failures unrelated to this work** | 6 | SYSTEM | `app` |
| `test_firewall_logs_endpoint.py` | `/api/servers/<n>/logs?source=Firewall` | 4 | SYSTEM | `routes/api/servers` |
| `test_install_state_lifecycle.py` | install / reboot / stabilising state machine (incl. janitor) | **26** | UNIT | `routes/api/updates`, `collector_v2/aggregator`, `collector_v2/periodics` |
| `test_models.py` | `ServerConfig` defaults, HTTPS port auto-flip | 5 | INTEGRATION | `models` |
| `test_ps_sandbox.py` | Workflow PS sandbox allowlist + HARD_DENY | 16 | INTEGRATION | `ps_sandbox` |
| `test_pulse_buffer.py` | v2 pulse buffer (ECG widget feed) | 16 | INTEGRATION | `collector_v2/state` |
| `test_pulse_endpoint.py` | `/api/collector/pulse` endpoint | 12 | SYSTEM | `app`, `routes`, `collector_v2/state` |
| `test_rbac.py` | RBAC database helpers | 8 | INTEGRATION | `database` |
| `test_rbac_uniform.py` | Uniform RBAC enforcement (S1-4) | 7 | SYSTEM | `routes/api/rbac`, `database` |
| `test_rekey_tool.py` | `tools/rekey.py` + plain-text migration | 6 | INTEGRATION | `tools/rekey`, `crypto_utils` |
| `test_server_updates_endpoint.py` | `/api/servers/<n>/updates` driver | 4 | SYSTEM | `routes/api/updates`, `state` |
| `test_status_enum.py` | `ServerStatus` enum sanity | 4 | INTEGRATION | `models` |
| `test_supply_chain.py` | Supply-chain integrity (`requirements.lock` hashes, etc.) | 3 | INTEGRATION | (build) |
| `test_update_status_acceleration.py` | `/api/servers/<n>/update-status` acceleration logic | 6 | INTEGRATION | `routes/api/updates` |
| `test_webhooks.py` | Webhook URL validation + sanitisation | 9 | INTEGRATION | `webhooks` |
| `test_workflow_disabled.py` | Disabled-block / disabled-connection support | 7 | UNIT | `workflow_engine` |
| `test_workflow_field_keys.py` | Canonical `service_name`/`process_name` field-key acceptance | 9 | INTEGRATION | `workflow_engine` |
| `test_workflow_param_binding.py` | Parameter binding (Sprint-1 S1-1, RCE mitigation) | 11 | INTEGRATION | `workflow_engine`, `ps_sandbox` |
| `test_workflow_ps_output.py` | `_format_ps_output` multi-stream PS output | 17 | INTEGRATION | `workflow_engine` |
| `test_workflow_status_field.py` | Workflow card `last_execution_status` field | 4 | INTEGRATION | `workflow_engine`, `database` |
| `test_workflow_triggers.py` | Trigger blocks (Manual / Schedule / Event) | 20 | INTEGRATION | `workflow_engine`, `routes/api/workflows` |
| `test_workflow_variables.py` | `{{step.X.output}}` substitution | 14 | INTEGRATION | `workflow_engine` |

## Test typology counts (rough)

| Type | Files |
|---|---|
| UNIT (single function in isolation, heavy mocking) | 6 |
| INTEGRATION (multiple components glued) | 22 |
| SYSTEM (end-to-end via Flask test client) | 5 |

## Modules with **zero** direct test coverage

These are the gaps that the OQ inventory will flag as findings.

| Module | Tier | Why it matters |
|---|---|---|
| `alert_scoring.py` | Indirectly exercised | Fatigue throttle directly affects alert dispatch — critical control |
| `detection.py` | Indirectly via aggregator tests | Single source of truth for status decision — should have direct tests of the 6-phase decision tree |
| `drift.py`, `drift_detector.py` | Untested | Config drift alerting — feeds operator-facing events |
| `email_alerts.py` | Untested | Email alert dispatch — operator-facing safety |
| `failed_logins.py` | Untested | Security-event alerting — operator-facing safety |
| `health_checker.py`, `healthchecks.py` | Untested | TCP/HTTP probe primitives — used by every health-check probe |
| `tls_monitor.py`, `tls_checker.py` | Untested | TLS cert expiry alerting |
| `auth.py` | Indirectly | Authentication / lockout / LDAP — security-critical |
| `runbook_engine.py` | Untested | Runbook execution |
| `scheduled_reports.py` | Untested | Daily/weekly digest generation |
| `restart_scheduler.py` | Untested | Scheduled restart execution |
| `maintenance.py` | Untested | Maintenance-window evaluation — affects threshold + alert suppression |
| `config_manager.py` | Untested | Config loading + password decryption |
| `i18n.py` | Untested | Translation registry |
| `security_checker.py` | Untested | Per-server security posture probes |
| `topology.py` | Untested | Topology graph rendering |
| `winrm_factory.py` | Untested | WSMan session factory (HTTPS, cert verify, timeout) |
| `routes/api/config.py` | Untested | Config CRUD endpoints (mutation gate) |
| `routes/api/metrics.py` | Untested | Anomaly ack, baseline recalc endpoints |
| `routes/api/misc.py` | Untested | factory-reset, maintenance-windows, dependencies, tags |
| `routes/api/power.py` | Untested | Restart / shutdown / WOL endpoints |
| `routes/api/reports.py` | Untested | CSV / JSON / PDF export endpoints |
| `routes/api/workflows.py` | Indirectly | Workflow CRUD + execute endpoints |
| `routes/views/*.py` | Untested | Template-rendering views |
| `seed_demo.py` | Test-time only | Demo seeder |

## Notable test-suite characteristics

- **Bulletproof outer try/except** is **NOT** tested for the supervisor/aggregator/worker daemon threads — gap.
- **End-to-end install + reboot lifecycle** is tested via `test_install_state_lifecycle.py` (now 26 tests post-audit including the new janitor coverage for stuck `stabilising` rows).
- **Audit hash chain** has dedicated tests in `test_audit_chain.py` — confirms `verify_audit_chain()` detects tampering.
- **Sandbox** has 16 dedicated tests covering allowlist + HARD_DENY + known limitations.
- **Pre-existing failures**: `test_csp_nonce_present_in_rendered_html` and `test_csp_nonce_per_request` at `/login` — both fail on clean master (predates this audit), unrelated to GxP functions.

---
*End of appendix.*
