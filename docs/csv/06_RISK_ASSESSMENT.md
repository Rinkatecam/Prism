# 06 — Risk Assessment (ICH Q9, GAMP 5 risk-based)

| Field | Value |
|---|---|
| Document ID | CSV-06 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Parents | `03_FS.md`, `04_DS.md` |

## Methodology

For each FS item, we evaluate three risk dimensions (per ICH Q9):

| Dimension | Levels | Meaning |
|---|---|---|
| **S — Severity** | 1 (Low) → 3 (High) | Patient-/data-integrity / GxP impact if this function fails silently |
| **P — Probability** | 1 (Rare) → 3 (Likely) | How likely the failure mode is, given current controls |
| **D — Detectability** | 1 (Easy) → 3 (Hidden) | How long the failure would go unnoticed in production |

**RPN** (Risk Priority Number) = S × P × D, max 27.

| RPN band | Class | Additional verification required |
|---|---|---|
| 18-27 | **Critical** | Enhanced verification: dedicated test, periodic re-execution, evidence retained ≥ 5 years |
| 9-17 | **High** | Test coverage + audit-log capture mandatory |
| 4-8 | **Moderate** | Standard test coverage; observability via dashboard/log |
| 1-3 | **Low** | Documentation only |

## Risk register

### A. Monitoring & data collection

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-001 | Metrics not collected for a server (silent gap in audit history) | 2 | 2 | 2 | 8 | Moderate | Pulse widget (DS-121) makes gaps visible; exponential backoff + acceleration. Tested: aggregator/worker/supervisor unit tests. |
| FS-001 | Metric value corrupted / wrong server attribution | 3 | 1 | 3 | 9 | High | Per-server WSMan session; PS payload returns server-identifying name; aggregator pairs WorkItem→Result by item. **GAP — no direct test that result attribution can't cross servers; recommend adding one.** |
| FS-002 | `compute_status` returns wrong status (threshold inverted, etc.) | 3 | 1 | 3 | 9 | High | **GAP — no direct unit tests of the 6-phase decision tree.** Finding F-002 in `17_FINDINGS_AND_GAPS.md`. |
| FS-003 | N-of-M gate fires too easily (alert noise) or never fires (silent overshoot) | 2 | 2 | 2 | 8 | Moderate | Configurable thresholds; gap on direct test (Finding F-003). |
| FS-004 | Maintenance window evaluator returns wrong active window (timezone bug) | 3 | 1 | 3 | 9 | High | `zoneinfo` mandatory, refuses naive datetime. **GAP — no direct test (Finding F-004).** |
| FS-005 | Retention cleanup deletes too aggressively / not at all | 3 | 1 | 3 | 9 | High | `cleanup_old_data(retention_days)` parameterised. **GAP — no end-to-end retention test (Finding F-005).** |
| FS-008 | Pulse widget shows stale data | 1 | 2 | 1 | 2 | Low | UI-only; 16+12 tests in `test_pulse_*`. |

### B. Alerting

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-010 | Status-transition event not emitted (no alert sent) | 3 | 2 | 2 | 12 | High | Aggregator path is the sole funnel; `test_collector_v2_aggregator.py` covers transition emission. Fatigue throttle is the failure mode → mitigated by `is_throttled_by_fatigue` configurability + `/api/alert-scores/reset`. |
| FS-010 | Alert sent to wrong server (cross-attribution) | 3 | 1 | 3 | 9 | High | Server identity threaded via `Result.item.server_name`; same gap as FS-001 attribution. |
| FS-011 | Anomaly detection blind spot (cache stale > TTL) | 2 | 1 | 2 | 4 | Moderate | TTL 5 min; tested in `test_analytics_baseline_cache.py`. |
| FS-014 | Fatigue throttle suppresses a real critical (false negative) | 3 | 2 | 2 | 12 | High | Throttle only after N fires + low actionability; **GAP — no test pins critical-doesn't-throttle behaviour (Finding F-014).** |
| FS-015 | Failed-login alert missed → security incident undetected | 3 | 2 | 2 | 12 | High | **GAP — no direct tests (Finding F-015).** Recommend integration test against a fake event-log payload. |
| FS-016 | TLS expiry alert missed → cert lapses → outage | 3 | 1 | 2 | 6 | Moderate | Cadence 1 h; warning_days/critical_days configurable. **GAP — no direct tests (Finding F-016).** |
| FS-020 | Incident correlation creates duplicate incidents or fails to group | 1 | 2 | 2 | 4 | Moderate | **GAP — no direct tests of 4 correlation rules (Finding F-020).** UI-visible side-effect; operator can manually re-group. |

### C. Operations (mutating actions on the fleet)

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-030 | Manual restart fires on wrong server (input mishandling) | **3** | 1 | 1 | 3 | Low | Server name in URL path; auth-gated; UI confirmation; audited as `power:restart`. |
| FS-030 | Restart issued without operator authorisation | **3** | 1 | 2 | 6 | Moderate | Per-server `admin` permission + tier-0 dual-control. Tested in `test_rbac_uniform.py`. |
| FS-031 | Scheduled restart fires twice (overlapping cycles) | 2 | 2 | 1 | 4 | Moderate | Marker file + 2-min schedule window prevents double-fire. **GAP — no direct test of `restart_scheduler.py` (Finding F-031).** |
| FS-031 | Scheduled restart fires at wrong wallclock time (timezone bug) | 2 | 1 | 2 | 4 | Moderate | `zoneinfo`-aware; same logic as FS-004. |
| FS-033 | Update install completes successfully but Prism shows "failed" | 1 | 2 | 1 | 2 | Low | Status read directly from target's `update-status.json`. |
| FS-033 | Update install hangs forever (no terminal state) | 2 | 2 | 1 | 4 | Moderate | Auto-restart watcher 90-min deadline; auto-restart scanner safety net every 60 s. |
| FS-034 | Auto-restart fires when operator did NOT request it | **3** | 1 | 2 | 6 | Moderate | `restart_after` flag explicit; clearable via `cancel-updates`. Audited as `auto_restart`. |
| FS-034 | Auto-restart fails to fire (server stuck pending reboot) | 2 | 1 | 2 | 4 | Moderate | Per-install watcher + periodics safety-net scanner. |
| FS-037 | Stale `restart_required` flag persists after manual reboot → operator confused | 2 | 2 | 2 | 8 | Moderate | **NEW in this audit's scope**: aggregator auto-clears on `pending_reboot=False`. 4 new tests. |
| FS-038 | Server stuck in `stabilising` forever (briefly came back then died) | 2 | 2 | 2 | 8 | Moderate | **NEW in this audit's scope**: janitor GCs after 20 min. 5 new tests. |
| FS-040 | Runbook execution fires wrong commands on wrong server | **3** | 1 | 2 | 6 | Moderate | Server name explicit; per-server `admin` + tier-0 dual-control; audited as `execute_runbook`. **GAP — no direct test of runbook_engine (Finding F-040).** |

### D. Workflow automation

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-053 | Sandbox bypass via known limitation (string concat, char-code, backtick) | **3** | **2** | **3** | **18** | **Critical** | Documented limitations; **structured fields use parameter binding (FS-054) which bypasses the text-time sandbox by design**. PowerShell free-form fields are explicitly admin-only (workflow execute is `_require_auth`). Recommend dedicated red-team test cases for each known limitation; record current behaviour. |
| FS-054 | Parameter binding regressed → user input concatenated → RCE | **3** | 1 | 3 | 9 | High | Tested in `test_workflow_param_binding.py` (11 tests); regression-locked. |
| FS-055 | Variable substitution happens inside `script` field → sandbox bypass | **3** | 1 | 2 | 6 | Moderate | `_substitute_config_variables` skips `script` key. Pinned in `test_workflow_variables.py::test_substitute_config_skips_script_key`. |
| FS-055 | Substitution leaks unsanitised data into webhook URL (SSRF) | 2 | 2 | 2 | 8 | Moderate | Webhook URL validation in `webhooks.py` (tested). |
| FS-057 | Disabled block silently executes anyway | 2 | 1 | 2 | 4 | Moderate | `test_workflow_disabled.py` covers 7 paths. |
| FS-060 | Workflow execution not audited (silent failure) | **3** | 1 | 3 | 9 | High | Audit row written by `execute_workflow`. Tested indirectly. |

### E. Authentication, RBAC, audit

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-070 | Authentication bypass | **3** | 1 | 1 | 3 | Low | Auth gated by `auth.enabled` flag; covered by `test_auth_hardening.py`. |
| FS-072 | Lockout silently disabled (config error) | 2 | 1 | 3 | 6 | Moderate | Tested. |
| FS-074 | Forced session termination does not take effect | 3 | 1 | 2 | 6 | Moderate | `revoked_sessions` checked on every request. Tested. |
| FS-075 | RBAC silently bypassed (route forgets to call `_require_*`) | **3** | 2 | **3** | **18** | **Critical** | `test_rbac_uniform.py` enforces uniform RBAC across destructive endpoints. **Recommendation**: extend the test to enumerate every mutation endpoint via Flask's route introspection so the count never drifts. |
| FS-076 | Tier-0 dual-control bypassed via stale approval | **3** | 1 | 2 | 6 | Moderate | Approvals are single-use (consumed-on-use). 1 h expiry. Tested. |
| FS-077 | Global destructive approval reused | **3** | 1 | 2 | 6 | Moderate | Same single-use semantics; tested. |
| FS-078 | Audit log row missing (silent forensic gap) | **3** | 2 | **3** | **18** | **Critical** | Every mutating endpoint must call `db.log_audit`. **Recommendation**: write a static-analysis test that scans `routes/api/*.py` for state-changing decorators and verifies a `log_audit` call exists. (Currently relies on developer discipline.) |
| FS-079 | Audit log row deleted/modified despite triggers | **3** | 1 | 2 | 6 | Moderate | Triggers prevent in-process; hash chain detects out-of-process. Tested. |
| FS-080 | Hash chain validates as OK when it shouldn't (false negative) | **3** | 1 | **3** | **9** | High | Tested in `test_audit_chain.py`. Recommend adding tampering-injection test. |
| FS-081 | JSONL mirror missing rows (silent forensic gap) | 3 | 1 | 3 | 9 | High | Each insert mirrored; if mirror file write fails, log_audit raises. Tested. |

### F. Data integrity

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-090 | Timestamp written in operator's local time instead of UTC | 2 | 1 | 3 | 6 | Moderate | All DB columns use `strftime('%Y-%m-%dT%H:%M:%SZ','now')`. Verified by schema inspection. |
| FS-091 | User attribution falsified (an action recorded under wrong user) | **3** | 1 | 3 | 9 | High | `log_audit` reads `session['username']` directly; session cookie signed; CSRF prevents replay. |
| FS-093 | `install_state.json` corrupted on power loss → loss of in-flight install context | 2 | 1 | 2 | 4 | Moderate | Atomic write via `os.replace`. |
| FS-094 | Backup tool produces a backup that can't be restored | **3** | 1 | 2 | 6 | Moderate | Tested in `test_backup_tool.py` round-trip. |

### G. Localisation

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-100 | Translation key missing → English shown to non-English operator | 1 | 2 | 1 | 2 | Low | Acceptable; English fallback is explicit. |
| FS-101 | Timestamp displayed in wrong timezone | 2 | 2 | 2 | 8 | Moderate | `zoneinfo` mandatory at all display points; **GAP — no automated test (Finding F-101)**. |
| FS-102 | `prefers-reduced-motion` not respected → vestibular harm | 1 | 1 | 1 | 1 | Low | CSS media query covers it. |

### H. Security

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-110 | CSRF bypass | 2 | 1 | 2 | 4 | Moderate | Flask-WTF used uniformly. |
| FS-111 | XSS via missing CSP | 2 | 2 | 2 | 8 | Moderate | CSP set; 1/3 CSP tests pass; pre-existing failure on nonce in `/login` is unrelated to GxP. Finding F-111 to be remediated. |
| FS-112 | Password disclosed in API response (mask defeated) | **3** | 1 | 2 | 6 | Moderate | Mask sentinel; tested by inspection. **GAP — no automated test (Finding F-112).** |
| FS-115 | Prism boots with LDAP misconfigured → no one can log in (lockout) | 2 | 1 | 1 | 2 | Low | Hard-fail at startup via `assert_ldap_startup_safe`; backup-admin remains accessible. |
| FS-116 | Supply chain (third-party lib swapped for malicious) | **3** | 1 | 2 | 6 | Moderate | `--require-hashes` + Sigstore release verification. Tested in `test_supply_chain.py`. |

### I. Operational lifecycle

| FS | Failure mode | S | P | D | RPN | Class | Controls + verification |
|---|---|---|---|---|---|---|---|
| FS-120 | Daemon thread dies; no operator notified | 2 | 2 | 2 | 8 | Moderate | Watchdog writes `audit_log`; `/api/system/health` exposes it. **GAP — no automated test (Finding F-120).** |
| FS-122 | One bad server's WinRM jams the pool, starves others | 2 | 2 | 1 | 4 | Moderate | Per-check deadlines (30/60/60/120 s); ThreadPoolExecutor one-shot pattern leaks stuck sockets to die naturally. |
| FS-124 | Factory reset accidentally fires | **3** | 1 | 1 | 3 | Low | Requires RBAC-admin + global approval + UI confirmation; built-in templates preserved. |

## Summary

| Class | Count | FS items |
|---|---|---|
| **Critical** (RPN 18-27) | 3 | FS-053 (sandbox), FS-075 (RBAC), FS-078 (audit log capture) |
| **High** (RPN 9-17) | 11 | FS-001 attribution, FS-002, FS-004, FS-005, FS-010 ×2, FS-014, FS-015, FS-054, FS-060, FS-080, FS-091 |
| **Moderate** (RPN 4-8) | 28 | (see register) |
| **Low** (RPN 1-3) | 9 | (see register) |

## Critical-class additional verification commitments

### Critical-1: FS-053 — Sandbox bypass via known limitations

**Required**:
1. Maintain a documented list of known PowerShell sandbox bypass techniques (string-concat, char-code, backtick, alias-FS-read) in `docs/WORKFLOW_SANDBOX.md`.
2. For each technique, add a *negative* test in `test_ps_sandbox.py` that asserts the current behaviour: e.g. "the sandbox currently does NOT block `[char]105+[char]101+[char]120`" — so we have explicit visibility if behaviour changes by accident.
3. The free-form PowerShell field (`Run PowerShell` block, `Condition` block) is gated by `_require_auth` AND requires the operator to have `admin` on the target server. Document that operating posture in the SOP doc.
4. Recommend a periodic review (annual) to expand the allowlist AND HARD_DENY together.

### Critical-2: FS-075 — Uniform RBAC enforcement

**Required**:
1. Maintain `tests/test_rbac_uniform.py` as the gate. It currently introspects routes for known destructive endpoints.
2. **Add** a static-analysis test that fails CI if a new state-changing route is added without an explicit auth decorator. Implementation sketch:
   ```python
   def test_every_mutating_route_has_auth():
       app = create_app()
       for rule in app.url_map.iter_rules():
           if rule.methods & {'POST','PUT','PATCH','DELETE'}:
               view = app.view_functions[rule.endpoint]
               assert _has_auth_decorator(view), f"route {rule} has no auth"
   ```
3. Track this as `Finding F-075` in `17_FINDINGS_AND_GAPS.md`.

### Critical-3: FS-078 — Audit log capture completeness

**Required**:
1. Same pattern as Critical-2: a static-analysis test that scans mutating endpoints for a `db.log_audit(...)` call (or a documented exception).
2. Add Critical-3 to `17_FINDINGS_AND_GAPS.md` as **finding F-078**.

## Residual risk acceptance

After all Critical and High findings are addressed, the residual risk is dominated by:
- (a) Untested modules in operational-but-not-GxP-direct paths (alert email dispatch, scheduled reports, runbook engine) — Moderate.
- (b) Pre-existing CSP nonce failures at `/login` — Moderate; remediable but historically deferred.
- (c) Third-party library vulnerabilities introduced between releases — Moderate; mitigated by `requirements.lock` hash pinning + scheduled rebuild.

These are documented in `17_FINDINGS_AND_GAPS.md` with assigned severity and remediation timelines.

---
*End of document.*
