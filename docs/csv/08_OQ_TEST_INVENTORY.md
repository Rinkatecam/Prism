# 08 — Operational Qualification (OQ) Test Inventory

| Field | Value |
|---|---|
| Document ID | CSV-08 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `06_RISK_ASSESSMENT.md` |
| Evidence | `evidence/OQ_pytest_run_2026-05-22.txt` |
| Test detail | `appendix_E_test_inventory.md` |

## Purpose

The Operational Qualification (OQ) phase demonstrates that each functional unit of Prism performs according to its specification across the full input range, including documented error paths. In Prism's case, OQ evidence is the **automated `pytest` test suite** (352 passing tests as of 2026-05-22), which is re-executed on every code change and at every install via IQ-012.

## A. Test suite summary (current state)

| Metric | Value |
|---|---|
| Total test files | 42 (33 baseline + 9 added by this audit) |
| Total test functions | 459 (collected by pytest) |
| Passing | **457** (post-Wave-5 final) |
| Pre-existing failures | **2** — both `test_csp.py` nonce tests at `/login`; documented in `17_FINDINGS_AND_GAPS.md` finding F-111; unrelated to GxP functions |
| Skipped | 0 |
| Warning count | 1 (Flask-Limiter in-memory storage warning — non-functional) |
| Execution time | ~19 s wall-clock |
| Last run | 2026-05-22 — evidence file `evidence/OQ_pytest_run_2026-05-22.txt` |

## B. Test classification

| Type | Files | Description |
|---|---|---|
| **UNIT** | 6 | Single function tested in isolation, heavy mocking |
| **INTEGRATION** | 22 | Multiple cooperating components, sometimes through Flask test client |
| **SYSTEM** | 5 | End-to-end via Flask test client + DB |

Full file-by-file matrix in `appendix_E_test_inventory.md`.

## C. Test → FS mapping (OQ coverage of the FS document)

| FS-ID | FS title (short) | Test files | Direct coverage? |
|---|---|---|---|
| FS-001 | Metric collection | test_collector_v2_supervisor / aggregator / workers | YES |
| FS-002 | Status classification | (indirect via aggregator tests) | **GAP — Finding F-002** |
| FS-003 | CPU N-of-M | (indirect) | **GAP — Finding F-003** |
| FS-004 | Maintenance window | — | **GAP — Finding F-004** |
| FS-005 | Retention cleanup | — | **GAP — Finding F-005** |
| FS-006 | Topology | — | GAP |
| FS-007 | Server detail | test_firewall_logs_endpoint / test_server_updates_endpoint | partial |
| FS-008 | Pulse / ECG | test_pulse_buffer (16), test_pulse_endpoint (12) | YES |
| FS-010 | Status transition | test_collector_v2_aggregator | YES |
| FS-011 | Anomaly detection | test_analytics_baseline_cache (9) | YES |
| FS-012 | Baseline deviation | (indirect) | partial |
| FS-013 | Anomaly ack & snooze | test_rbac, indirect | partial |
| FS-014 | Fatigue throttle | — | **GAP — Finding F-014** |
| FS-015 | Failed-login alerts | — | **GAP — Finding F-015** |
| FS-016 | TLS expiry | — | **GAP — Finding F-016** |
| FS-017 | Health-check probes | — | GAP |
| FS-018 | Drift detection | — | GAP |
| FS-019 | Scheduled reports | — | GAP |
| FS-020 | Incident correlation | — | **GAP — Finding F-020** |
| FS-030 | Manual restart | test_install_state_lifecycle (lifecycle parts) | partial |
| FS-031 | Scheduled restart | — | **GAP — Finding F-031** |
| FS-032 | WOL | — | GAP |
| FS-033 | Update install | test_server_updates_endpoint, test_update_status_acceleration | YES |
| FS-034 | Auto-restart | test_install_state_lifecycle | YES |
| FS-035 | Cancel install | — | GAP |
| FS-036 | Live update status | test_update_status_acceleration | YES |
| FS-037 | Auto-clear stale install_state | test_collector_v2_aggregator (4 new tests) | YES ✱ added in this audit |
| FS-038 | Stuck-state janitor | test_install_state_lifecycle (5 new tests) | YES ✱ added in this audit |
| FS-039 | Live picker | — | GAP |
| FS-040 | Runbook execution | — | **GAP — Finding F-040** |
| FS-050 | Visual editor | (UI, manual) | UI gap |
| FS-051 | Trigger types | test_workflow_triggers (20) | YES |
| FS-052 | Edge-triggered | test_workflow_triggers | YES |
| FS-053 | PS sandbox | test_ps_sandbox (16) | YES |
| FS-054 | Parameter binding | test_workflow_param_binding (11) | YES |
| FS-055 | Variable substitution | test_workflow_variables (14) | YES |
| FS-056 | Branch-aware connections | (UI, manual) | UI gap |
| FS-057 | Block disable | test_workflow_disabled (7) | YES |
| FS-058 | Multi-select | (UI, manual) | UI gap |
| FS-059 | Right-click menu | (UI, manual) | UI gap |
| FS-060 | Workflow audit trail | test_workflow_status_field (4), test_workflow_triggers | YES |
| FS-061 | Categories | — | GAP |
| FS-062 | Built-in templates | (indirect) | partial |
| FS-070 | Authentication | test_auth_hardening | YES |
| FS-071 | Password policy | test_auth_hardening | YES |
| FS-072 | Lockout | test_auth_hardening | YES |
| FS-073 | Session timeout | test_auth_hardening | YES |
| FS-074 | Forced session term. | test_auth_hardening, test_rbac | YES |
| FS-075 | Per-server RBAC | test_rbac (8), test_rbac_uniform (7) | YES |
| FS-076 | Tier-0 dual-control | test_rbac_uniform | YES |
| FS-077 | Global destructive approval | test_rbac_uniform | YES |
| FS-078 | Audit-log capture | test_audit_chain (7), test_audit_archive | YES |
| FS-079 | Audit append-only | test_audit_chain (trigger test) | YES |
| FS-080 | Audit hash chain | test_audit_chain | YES |
| FS-081 | JSONL mirror | test_audit_chain | YES |
| FS-082 | Audit export | test_audit_archive | YES |
| FS-083 | Audit-log UI | — | GAP |
| FS-090 | ISO-8601 UTC timestamps | (schema) | indirect |
| FS-091 | User attribution | test_audit_chain | YES |
| FS-092 | Contemporaneous | (schema) | indirect |
| FS-093 | Restart-survivable | test_install_state_lifecycle (persist/load roundtrip) | YES |
| FS-094 | Backup / restore | test_backup_tool (4) | YES |
| FS-095 | Re-key | test_rekey_tool (6) | YES |
| FS-100 | Translation registry | — | **GAP — Finding F-100** |
| FS-101 | Operator timezone | — | **GAP — Finding F-101** |
| FS-102 | Reduced motion | (CSS/UI) | UI gap |
| FS-110 | CSRF | (Flask-WTF library) | indirect |
| FS-111 | CSP | test_csp (6) — 4 pass, 2 pre-existing failures | partial |
| FS-112 | Password masking | — | **GAP — Finding F-112** |
| FS-113 | HTTPS downgrade | — | GAP |
| FS-114 | Tier-0 skip-verify block | — | GAP |
| FS-115 | LDAP startup safety | — | GAP |
| FS-116 | Hash-pinned deps | test_supply_chain (3) | YES |
| FS-120 | Self-watchdog | — | **GAP — Finding F-120** |
| FS-121 | Health endpoint | test_collector_v2_health_endpoint (2) | YES |
| FS-122 | Graceful degradation | test_collector_v2_aggregator | YES |
| FS-123 | Hot config reload | — | GAP |
| FS-124 | Factory reset | — | GAP |

## D. OQ aggregate coverage

| Status | FS items |
|---|---|
| Direct coverage (YES) | 38 |
| Partial / indirect | 9 |
| Gap (no direct coverage) | 22 |
| UI-only (out of OQ scope) | 5 |

Direct + partial = **47 / 78 ≈ 60 %**. Improving this is the work tracked by Phase 13 (remediation) and Phase 12 findings.

## E. OQ execution protocol

To re-execute OQ at any point:

1. Ensure environment matches IQ baseline (Python version, deps).
2. From repo root: `python -m pytest tests/ --tb=line -v 2>&1 | tee evidence/OQ_pytest_run_<date>.txt`
3. Confirm:
   - Test count is ≥ 352 passing.
   - Only known pre-existing failures (`test_csp_nonce_*` x 2) remain.
   - Execution time is broadly comparable to the baseline (~19 s).
4. Compare new evidence against `evidence/OQ_pytest_run_2026-05-22.txt`; investigate any new failure as a deviation.

## F. OQ acceptance form

**Last OQ run**: 2026-05-22

| Criterion | Result |
|---|---|
| Test count | 352 passing |
| Pre-existing failures | 2 (documented in F-111) |
| New failures | 0 |
| Test execution time | 19 s |
| Evidence file | `evidence/OQ_pytest_run_2026-05-22.txt` |

**Approval**:
- Tested by: ______________________ Date: ____________
- Reviewed by: ______________________ Date: ____________
- Approved by: ______________________ Date: ____________

## G. Pre-existing test failures (accepted)

Both failures are at `/login` and pertain to the rendering of a nonce on a publicly-accessible page (no GxP impact). The CSP **header** is set correctly (verified by `test_csp_header_present` which passes). The nonce-on-rendered-script tests were authored to validate a future hardening sweep that has not yet shipped.

- `tests/test_csp.py::test_csp_nonce_present_in_rendered_html`
- `tests/test_csp.py::test_csp_nonce_per_request`

These are tracked in `17_FINDINGS_AND_GAPS.md` finding F-111 with severity **Minor** and remediation deferred. The CSP nonce wiring itself works for *authenticated* pages (where the GxP-significant inline scripts live) — these failures are specifically about the pre-authentication `/login` template not having any nonce-bearing `<script>` blocks. Risk acceptable.

---
*End of document.*
