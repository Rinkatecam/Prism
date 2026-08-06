# 10 — Traceability Matrix (URS ↔ FS ↔ DS ↔ Risk ↔ Test ↔ PQ)

| Field | Value |
|---|---|
| Document ID | CSV-10 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `02_URS.md`, `03_FS.md`, `04_DS.md`, `06_RISK_ASSESSMENT.md`, `08_OQ_TEST_INVENTORY.md`, `09_PQ_SCENARIOS.md` |

## Purpose

The traceability matrix is the **single load-bearing artefact** for CSV. For each URS item it shows the complete chain: which functional specification implements it, which design satisfies that FS, which risks attach to it, which tests verify it, and which PQ scenario exercises it end-to-end.

**Empty cells = gaps.** Every gap is a finding in `17_FINDINGS_AND_GAPS.md`.

## Legend

- `URS-NNN` — User Requirements Spec item (`02_URS.md`)
- `FS-NNN` — Functional Spec item (`03_FS.md`)
- `DS-NNN` — Design Spec item (`04_DS.md`)
- `RPN-cls` — Risk class from `06_RISK_ASSESSMENT.md`: C=Critical / H=High / M=Moderate / L=Low
- `test_*` — pytest file (from `08_OQ_TEST_INVENTORY.md`)
- `PQ-NNN` — PQ scenario (`09_PQ_SCENARIOS.md`)

## Matrix

| URS | URS title (short) | FS | DS | Risk | OQ test files | PQ |
|---|---|---|---|---|---|---|
| URS-001 | Continuous fleet monitoring | FS-001 | DS-100, DS-101, DS-102, DS-117 | M+H | test_collector_v2_supervisor, test_collector_v2_aggregator, test_collector_v2_workers | PQ-001 |
| URS-002 | Status classification | FS-002 | DS-100 | **H — Finding F-002** | (indirect) | PQ-002 |
| URS-003 | Sustained-spike gating | FS-003 | DS-100 | M | (indirect — Finding F-003) | (covered by PQ-002 flow) |
| URS-004 | Maintenance-window threshold loosening | FS-004 | DS-112 | **H — Finding F-004** | — | PQ-004 |
| URS-005 | Historical metric retention | FS-005 | DS-100, DS-120 | **H — Finding F-005** | — | (n/a — operational period) |
| URS-006 | Topology view | FS-006 | DS-118 | L | — | (UI, manual) |
| URS-007 | Server detail page | FS-007 | DS-116, DS-118 | L | test_firewall_logs_endpoint, test_server_updates_endpoint | PQ-001 |
| URS-008 | Pulse / ECG widget | FS-008 | DS-102, DS-121 | L | test_pulse_buffer, test_pulse_endpoint | PQ-001 |
| URS-010 | Threshold-based alerting | FS-010 | DS-100 | **H** | test_collector_v2_aggregator | PQ-002 |
| URS-011 | Statistical anomaly detection | FS-011 | DS-111 | M | test_analytics_baseline_cache | PQ-003 |
| URS-012 | Baseline deviation alerts | FS-012 | DS-111 | M | (indirect) | (PQ-003 flow) |
| URS-013 | Anomaly acknowledge & snooze | FS-013 | DS-110 | M | (indirect) | PQ-003 |
| URS-014 | Alert fatigue throttle | FS-014 | DS-100 | **H — Finding F-014** | — | (UI smoke) |
| URS-015 | Failed-login monitoring | FS-015 | DS-100, DS-117 | **H — Finding F-015** | — | PQ-017 |
| URS-016 | TLS expiry alerts | FS-016 | DS-100 | M | — Finding F-016 | PQ-018 |
| URS-017 | Health-check probes | FS-017 | DS-100 | M | — | (UI smoke) |
| URS-018 | Config drift detection | FS-018 | DS-100 | M | — | (UI smoke) |
| URS-019 | Daily/weekly digest | FS-019 | DS-100 | L | — | PQ-019 |
| URS-020 | Incident correlation | FS-020 | DS-100 | M | — Finding F-020 | (UI smoke) |
| URS-030 | Manual restart | FS-030 | DS-107, DS-109, DS-118 | M | test_install_state_lifecycle | PQ-005 |
| URS-031 | Scheduled restart | FS-031 | DS-100 | M | — Finding F-031 | (out of cycle) |
| URS-032 | Wake-on-LAN | FS-032 | DS-118 | L | — | (manual) |
| URS-033 | Windows-update install | FS-033 | DS-107, DS-118 | M | test_server_updates_endpoint, test_update_status_acceleration | PQ-006 |
| URS-034 | Auto-restart after install | FS-034 | DS-107 | M | test_install_state_lifecycle | PQ-006 |
| URS-035 | Cancel install | FS-035 | DS-107 | M | — | (out of cycle) |
| URS-036 | Live update status | FS-036 | DS-107, DS-118 | M | test_update_status_acceleration | PQ-006 |
| URS-037 | Recovery from stuck install state ✱ | FS-037 | DS-107 | M | test_collector_v2_aggregator (4 new tests) | PQ-007 |
| URS-038 | Stuck-state janitor ✱ | FS-038 | DS-107 | M | test_install_state_lifecycle (5 new tests) | PQ-006 |
| URS-039 | Live picker (services / processes / ports) | FS-039 | DS-118 | L | — | (UI smoke) |
| URS-040 | Runbook execution | FS-040 | DS-118 | M | — Finding F-040 | (out of cycle) |
| URS-050 | Visual workflow editor | FS-050 | DS-106, DS-116 | L | (UI, manual) | PQ-008 |
| URS-051 | Workflow trigger types | FS-051 | DS-115 | M | test_workflow_triggers | PQ-008 |
| URS-052 | Edge-triggered event firing | FS-052 | DS-115 | M | test_workflow_triggers | PQ-008 |
| URS-053 | PowerShell sandbox | FS-053 | DS-103, DS-104 | **C** | test_ps_sandbox, test_workflow_param_binding | PQ-009 |
| URS-054 | Parameter binding | FS-054 | DS-104 | **H** | test_workflow_param_binding | PQ-009 |
| URS-055 | Variable substitution | FS-055 | DS-105 | M | test_workflow_variables | PQ-008 |
| URS-056 | Branch-aware connections | FS-056 | DS-116 | L | (UI, manual) | (UI smoke) |
| URS-057 | Block / connection enable/disable | FS-057 | DS-106, DS-116 | M | test_workflow_disabled | (PQ-008 flow) |
| URS-058 | Multi-select & group ops | FS-058 | DS-116 | L | (UI, manual) | (UI smoke) |
| URS-059 | Right-click context menu | FS-059 | DS-116 | L | (UI, manual) | (UI smoke) |
| URS-060 | Workflow execution audit trail | FS-060 | DS-106, DS-110 | **H** | test_workflow_status_field, test_workflow_triggers | PQ-008 |
| URS-061 | Workflow categorisation | FS-061 | DS-118 | L | — | (UI smoke) |
| URS-062 | Built-in templates | FS-062 | DS-106 | L | (indirect) | (UI smoke) |
| URS-070 | Authentication | FS-070 | DS-108 | L | test_auth_hardening | PQ-005, PQ-010 |
| URS-071 | Backup-admin password policy | FS-071 | DS-108 | M | test_auth_hardening | (PQ-010 setup) |
| URS-072 | Lockout | FS-072 | DS-108 | M | test_auth_hardening | (PQ smoke) |
| URS-073 | Session timeout | FS-073 | DS-108 | M | test_auth_hardening | (manual) |
| URS-074 | Forced session termination | FS-074 | DS-108 | M | test_auth_hardening, test_rbac | PQ-005 (audit) |
| URS-075 | Per-server RBAC | FS-075 | DS-109 | **C — Finding F-075 (uniform-enforcement test extension)** | test_rbac, test_rbac_uniform | PQ-012 |
| URS-076 | Tier-0 dual-control | FS-076 | DS-109 | M | test_rbac_uniform | PQ-010 |
| URS-077 | Global destructive approval | FS-077 | DS-109 | M | test_rbac_uniform | PQ-013 |
| URS-078 | Audit-log capture | FS-078 | DS-110, DS-118 | **C — Finding F-078 (static-analysis test)** | test_audit_chain, test_audit_archive | PQ-011, PQ-005, PQ-010 |
| URS-079 | Append-only audit | FS-079 | DS-110, DS-120 | M | test_audit_chain | PQ-011 |
| URS-080 | Hash chain | FS-080 | DS-110 | **H** | test_audit_chain | PQ-011 |
| URS-081 | JSONL mirror | FS-081 | DS-110 | **H** | test_audit_chain | PQ-011 |
| URS-082 | Export & archive | FS-082 | DS-118 | M | test_audit_archive | (manual) |
| URS-083 | User-visible audit log | FS-083 | DS-118 | L | — | (UI smoke) |
| URS-090 | ISO-8601 UTC | FS-090 | DS-120 | M | (schema) | (manual inspection) |
| URS-091 | User attribution | FS-091 | DS-110 | **H** | test_audit_chain | PQ-005, PQ-011 |
| URS-092 | Contemporaneous | FS-092 | DS-120 | M | (schema) | (manual) |
| URS-093 | Restart-survivable state | FS-093 | DS-107 | M | test_install_state_lifecycle (persist/load roundtrip) | PQ-005 |
| URS-094 | Backup / restore | FS-094 | DS-122 | M | test_backup_tool | PQ-013 |
| URS-095 | Re-key | FS-095 | DS-122 | M | test_rekey_tool | (manual) |
| URS-100 | Five-language UI | FS-100 | DS-119 | L | — Finding F-100 | PQ-014 |
| URS-101 | Operator timezone | FS-101 | DS-116, DS-119 | M | — Finding F-101 | PQ-014 |
| URS-102 | Reduced motion | FS-102 | DS-116 | L | (CSS) | PQ-015 |
| URS-110 | CSRF | FS-110 | DS-118 | M | (Flask-WTF library) | (manual) |
| URS-111 | CSP | FS-111 | DS-118 | M | test_csp (4/6 pass; F-111) | (manual) |
| URS-112 | Password masking | FS-112 | DS-114 | M | — Finding F-112 | (manual) |
| URS-113 | HTTPS downgrade protection | FS-113 | DS-118 | M | — | (manual) |
| URS-114 | Tier-0 skip-verify block | FS-114 | DS-118 | M | — | (manual) |
| URS-115 | LDAP startup safety | FS-115 | DS-108 | L | — | (boot-test) |
| URS-116 | Hash-pinned dependencies | FS-116 | DS-100 | M | test_supply_chain | IQ-002 |
| URS-120 | Self-watchdog | FS-120 | DS-113 | M | — Finding F-120 | PQ-020 |
| URS-121 | Health endpoint | FS-121 | DS-118 | L | test_collector_v2_health_endpoint | (manual smoke) |
| URS-122 | Graceful degradation | FS-122 | DS-100, DS-101 | M | test_collector_v2_aggregator | PQ-016 |
| URS-123 | Hot config reload | FS-123 | DS-114 | M | — | (manual) |
| URS-124 | Factory reset | FS-124 | DS-118 | L | — | (test environment) |
| URS-200 ✱ | Compliance dashboard | FS-200, FS-204 | (new surface; see DS-100/118 patterns) | M | test_compliance_routes, test_compliance_phd_audit (view route tests) | PQ-021 |
| URS-201 ✱ | SOP execution recording | FS-201, FS-206 | (new) | M | test_compliance_db, test_compliance_routes::test_execute_* | PQ-021 |
| URS-202 ✱ | Live-data substitution | FS-202, FS-205 | (new) | M | test_compliance_renderer (substitution + code-span skip) | PQ-021 |
| URS-203 ✱ | CSV documentation browser | FS-203 | (new) | L | test_compliance_routes::test_csv_doc_* | PQ-021 |
| URS-204 ✱ | Feature flag gating | FS-204 | (new) | L | test_compliance_status::test_is_compliance_enabled_*, test_compliance_routes::test_*_404s | PQ-021 |
| URS-205 ✱ | XSS-safe rendering | FS-205 | (new; risk-class H) | **H** | test_compliance_renderer::test_renderer_drops_raw_script_from_source | (security smoke) |
| URS-206 ✱ | Append-only SOP evidence | FS-206 | (new) | M | test_compliance_db::test_sop_log_*_blocked_by_trigger | (DR-test SOP-08 covers integrity preservation) |

✱ added post-Wave-6 audit to maintain V-model self-consistency for the in-app compliance UI.

✱ = added during this audit's scope (URS-037, URS-038).

## Completeness summary

| Coverage stage | Count | Total | % |
|---|---|---|---|
| URS items | 78 | 78 | 100 % |
| URS → FS link | 78 | 78 | 100 % |
| URS → DS link | 78 | 78 | 100 % |
| URS → Risk assessment | 78 | 78 | 100 % |
| URS → OQ test (direct) | 38 | 78 | **49 %** |
| URS → PQ scenario | 60 | 78 | **77 %** |

## Gaps escalated to `17_FINDINGS_AND_GAPS.md`

Items where the OQ column is empty + the FS-level risk is **High** or **Critical**:

| URS | FS | Risk | Finding |
|---|---|---|---|
| URS-002 | FS-002 | H | F-002 — add direct unit tests for `compute_status` 6-phase decision tree |
| URS-004 | FS-004 | H | F-004 — add direct tests for `_get_active_maintenance_window` |
| URS-005 | FS-005 | H | F-005 — add end-to-end retention cleanup test |
| URS-014 | FS-014 | H | F-014 — pin "fatigue throttle never suppresses critical" behaviour |
| URS-015 | FS-015 | H | F-015 — add direct tests for failed-login spike alerting |
| URS-040 | FS-040 | M (gap-of-concern) | F-040 — add tests for runbook_engine |
| URS-075 | FS-075 | **C** | F-075 — static-analysis test: every mutating route has an auth decorator |
| URS-078 | FS-078 | **C** | F-078 — static-analysis test: every mutating route writes an audit row |
| URS-120 | FS-120 | M | F-120 — add test for watchdog audit_log emission on dead thread |

Also captured as findings, severity Minor, no immediate gating:

| URS | Finding |
|---|---|
| URS-100 | F-100 — automated i18n key-presence sanity test |
| URS-101 | F-101 — automated tz-conversion test |
| URS-111 | F-111 — pre-existing CSP-nonce-on-`/login` failure |
| URS-112 | F-112 — automated test that masked password round-trip preserves stored value |

## Reading the matrix

To verify CSV-readiness for any single URS, follow the row left-to-right:
1. Read URS-NNN in `02_URS.md` to understand the requirement.
2. Read each FS-NNN in `03_FS.md` to understand the functional realisation.
3. Read each DS-NNN in `04_DS.md` to understand the design choice.
4. Read the risk class in `06_RISK_ASSESSMENT.md` for the failure-mode analysis.
5. Open the cited test file(s) under `tests/` — these are the OQ evidence.
6. Read the cited PQ scenario in `09_PQ_SCENARIOS.md` for the end-to-end exercise.
7. If any column is empty: the corresponding finding in `17_FINDINGS_AND_GAPS.md` documents its remediation plan.

---
*End of document.*
