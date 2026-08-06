# SOP-05 — Validated Baseline Review

| Field | Value |
|---|---|
| Document ID | SOP-05 |
| Version | 1.0 |
| Effective from | 2026-05-22 |
| Owner | IT-validation |
| Implements | URS-080, URS-120, URS-121 / FS-080, FS-120, FS-121 |
| Closes finding | F-SOP-5, F-AT-1 |
| Review cadence | **Monthly** |

> **Live status (rendered from live application state):**
>
> | Signal | Current value |
> |---|---|
> | Audit subsystem | [[csv:audit_blind]] |
> | Audit chain last verified | [[csv:audit_chain_last_check]] |
> | Last passing test count | [[csv:test_count]] |
> | SOP-05 last executed | [[csv:last_execution.SOP-05]] |
> | SOP-05 next due | [[csv:next_due.SOP-05]] |
> | Overall readiness | [[csv:overall_readiness]] |
>
> *These cells refresh every time the page loads.*

## 1. Purpose

Confirm Prism is still operating in the qualified state described by the most recent CSV sign-off. Detects drift before it accumulates into a finding.

## 2. Trigger

Monthly (suggested: first Tuesday of each month) AND after any change-controlled deployment.

## 3. Procedure

### 3.1 Audit chain integrity

```python
from database import Database
db = Database('data/prism.db')
result = db.verify_audit_chain()
print(result)
```

Expected: `{"ok": true, "checked": N, "first_break_id": None, "first_break_reason": None}`.

The scheduled audit-chain verifier (F-AT-1) runs this hourly; the latest result lives in `/api/system/health` → `last_audit_chain_check`. The monthly SOP also runs it interactively and signs off the result.

**If `ok=false`**: stop. Open an incident; do NOT make any operator-visible config changes until the chain is investigated. Follow the org's data-integrity playbook.

### 3.2 Subsystem health

```
GET /api/system/health
```

Confirm all of:
- `collector_v2.supervisor.ok = true`
- `collector_v2.aggregator.ok = true`
- `collector_v2.workers.ok = true`
- `collector_v2.periodics.ok = true`
- `restart_scheduler.ok = true`
- `workflow_scheduler.ok = true`
- `last_audit_chain_check.ok = true` (within last hour)
- `audit_insert_failures` counter has not jumped since last review (per F-D-1 telemetry)

### 3.3 Test suite

```
cd C:\Prism
python -m pytest tests/ --tb=line -q
```

Expected: same count as the last sign-off (e.g. ≥ 457 passing, 2 deselected). New failures or sudden test-count drops are findings.

Save the output to `docs/csv/evidence/OQ_pytest_run_<YYYY-MM-DD>.txt`.

### 3.4 Baseline drift

Compare the current code commit + `requirements.lock` against the last IQ-recorded baseline:

```
git rev-parse HEAD          # current code commit
sha256sum requirements.lock # current dep lock fingerprint
```

If either has changed since the last IQ:
- A new IQ + OQ (and possibly PQ) is required per `14_CHANGE_CONTROL.md`. If the previous re-qualification was already performed, link to its evidence here.

### 3.5 Findings register currency

Read `docs/csv/17_FINDINGS_AND_GAPS.md`. Confirm that:
- All Critical / Major findings remain CLOSED.
- Moderate / Minor follow-ups are progressing per their target dates.
- No newly-introduced finding is unaccounted for.

### 3.6 File the review

Save under `data/sop_records/baseline_review_<YYYY-MM-DD>.md` with:

- Reviewer name + date.
- Each step result: PASS / FAIL / N/A.
- Any deviations + their resolution.
- Sign-off.

## 4. Exit criteria

All sub-steps PASS. Any FAIL becomes an immediate finding, raised through the change-control system per `14_CHANGE_CONTROL.md`.

## 5. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Performed by (IT-validation) | | | |
| Quality sign-off (quarterly) | | | |

---
*End of SOP.*
