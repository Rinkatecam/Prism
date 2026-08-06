# 01 — Scope, Intended Use, and GAMP 5 Categorisation

| Field | Value |
|---|---|
| System name | **Prism** — Windows Server Monitoring & Operations |
| Document ID | CSV-01 |
| Version | 1.0 |
| Date | 2026-05-22 |
| Status | **Final — pending Quality sign-off** |
| Author | Audit (Claude/Atlas) |
| Reviewer | _pending_ |
| Approver | _pending_ |

## 1. Purpose of this document

This is the **anchor document** of the Prism GAMP 5 Computer System Validation (CSV) package. It establishes:

- The boundary of the validated system (what is in / out of scope).
- The intended use and whether that use is GxP-impacting.
- The GAMP 5 software category, which determines the depth of V-model documentation and verification required downstream.
- The validation strategy that flows from those determinations.

Every subsequent CSV artifact in `docs/csv/` cites this document for its scope rationale.

## 2. System overview

Prism is a custom-built Flask web application that monitors a fleet of 20–30 Windows servers in real time and provides operational tooling (workflow automation, Windows-update management, scheduled restarts, runbooks, event-log inspection). It is **agentless** — it talks to the servers over WinRM (Windows Remote Management) via the `pypsrp` Python library; no software is installed on the targets.

### High-level architecture (data-flow only — full design in DS doc)

```
            Browser (operators)
                  │  HTTPS + session cookie + CSRF
                  ▼
            Flask app (single process)
            ├── HTTP routes (routes/api/*.py, routes/views/*.py)
            ├── v2 collector threads
            │     ├── supervisor  — decides what to poll, every 5 s
            │     ├── workers     — PowerShell pool, runs the WinRM calls
            │     ├── aggregator  — drains result queue, writes DB + cache
            │     └── periodics   — scheduled jobs (TLS, drift, retention, …)
            ├── workflow scheduler thread — fires scheduled + event triggers
            └── SQLite database (data/prism.db, WAL mode)
                  ▲
                  │  pypsrp / WinRM 5985/5986
                  ▼
            30 Windows servers (read-only by default;
            writes only via explicit operator action)
```

### What Prism does NOT do

- Does not directly manipulate manufacturing / quality / lab equipment.
- Does not store patient data, batch records, or any record that is the primary GxP source-of-truth.
- Does not act as an electronic signature authority for any regulated workflow outside its own audit log.
- Does not connect to LIMS, MES, ERP, or any other GxP system.

These exclusions are load-bearing — they shape the GAMP categorisation in §5.

## 3. System boundary (what's IN scope)

The validated system includes, on the Prism host:

| Component | In scope? | Notes |
|---|---|---|
| Prism Python application (all `.py` files in repo) | YES | Custom code (Cat 5) |
| Jinja2 templates (`templates/`) | YES | UI surface delivering data to operators |
| Static assets — JS/CSS shipped with Prism (`static/`) | YES | Vanilla JS, no module bundler |
| SQLite database (`data/prism.db`) | YES | Append-only audit + mutable monitoring state |
| Config files (`config.json`, `data/settings.toml`, key files) | YES | Configuration parameters |
| `install_state.json` (operational state file) | YES | Cross-restart lifecycle state |
| `requirements.lock` (deterministic dependency closure) | YES | Pinned with hashes |
| Operating system (Windows Server hosting Prism) | OUT | Validated separately; OS qualification is the IT team's responsibility |
| Python 3.13 runtime | OUT | Treated as supplier-validated (Cat 1) |
| Third-party libraries (Flask, pypsrp, APScheduler, …) | OUT | Treated as supplier-validated (Cat 1/3); pinned by `requirements.lock` to ensure reproducibility |
| Monitored Windows servers (the 30 targets) | OUT | Their qualification is the IT team's responsibility; Prism only observes them |
| Network infrastructure (firewalls, AD, DNS) | OUT | IT-managed |
| Browser used by operators | OUT | Generic |

## 4. Intended use & GxP impact assessment

### Operator persona

On-site IT administrator(s) responsible for the day-to-day operation of ~30 Windows servers across the corporate fleet. Primary tasks:

1. Spot servers that are misbehaving (CPU/RAM/disk spikes, services down, drift, failed logins).
2. Apply Windows updates and reboots in a controlled way.
3. Run pre-defined PowerShell workflows (service restarts, log harvesting, etc.).
4. Receive alerts (email, Teams webhook) when thresholds or anomalies fire.

### Is Prism GxP?

Prism is **not the primary source of GxP-regulated data**. The data it produces (CPU%, RAM%, event logs, update status) is operational telemetry, not regulated-product data.

**However**, Prism may be used in a GxP context as **supporting infrastructure monitoring** in a regulated facility. Two scenarios determine its validation depth:

| Scenario | Treatment |
|---|---|
| **A. Pure IT-ops, non-regulated fleet** | Prism does not require formal CSV. This package exists for engineering rigour, not regulatory compliance. |
| **B. Servers hosting GxP applications** (the use-case driving this audit) | Prism's monitoring of those servers is part of the infrastructure qualification. Prism's audit log is the primary evidence that "the operator who restarted server X at time Y was authenticated user Z." That makes Prism **GxP-adjacent** — its uptime and audit-log integrity directly affect the GxP-validated systems it watches over. |

This audit assumes scenario **B** and validates to that depth.

### Specific GxP-relevant functions

| Function | Why GxP-relevant |
|---|---|
| Audit log of administrative actions (restarts, installs, runbook fires, workflow runs) | Establishes who-did-what-when on regulated infrastructure |
| Windows update install lifecycle (`/api/servers/<n>/updates/install` and friends) | Modifies the state of regulated hosts; must be auditable + reversible (history retained) |
| Restart scheduling (`restart_scheduler.py`) | Same — modifies host state |
| Workflow execution (`workflow_engine.py`) | Executes PowerShell on hosts; sandbox is the critical control |
| Alert dispatch (email + Teams) | Operators rely on these to detect incidents; missed alerts could delay GxP-impacting response |
| Backup of `prism.db` | Loss = loss of audit history = loss of the very evidence that justifies validation |

Functions explicitly **not GxP-relevant** (operational only):
- Dashboard appearance / theming / i18n
- Topology visual layout
- The ECG pulse widget (operator awareness, not a record)

## 5. GAMP 5 software categorisation

The GAMP 5 framework defines five categories:

| Cat | Software type | Examples |
|---|---|---|
| 1 | Infrastructure | OS, DB engine, runtime, third-party libraries |
| 2 | (Retired in GAMP 5 2nd ed.) | — |
| 3 | Non-configured COTS | Off-the-shelf, used as-is |
| 4 | Configured COTS | Parameterised, no custom code |
| 5 | Custom-built application | Bespoke development |

### Prism's category

**Prism is GAMP Category 5** — Custom-built application.

Justification:
- The entire Python codebase under `C:\Prism\` is custom written for this organisation.
- ~30 routes, custom monitoring engine, custom workflow engine, custom sandbox, custom UI. There is no off-the-shelf product being configured.
- Third-party libraries embedded in Prism (Flask, pypsrp, APScheduler, etc.) are Category 1 dependencies of Prism, and their qualification is delegated to the vendor's release engineering. Prism pins them in `requirements.lock` with cryptographic hashes (`pip install --require-hashes`) so the closure is byte-deterministic.

### Implication for validation depth

GAMP 5 Category 5 software requires:

- Full V-model documentation: URS → FS → DS, with the corresponding right-side artefacts (IQ, OQ, PQ).
- Code-review evidence (git history + commit messages serve this; we cite specific commits in the change-control doc).
- Comprehensive testing at unit, integration, and system levels (the 352-test pytest suite is OQ evidence; PQ scenarios in `docs/csv/09_PQ_SCENARIOS.md`).
- Risk-based deepening of verification — high-risk functions (sandbox, audit log, install-lifecycle) get extra scrutiny in the risk assessment (`docs/csv/06_RISK_ASSESSMENT.md`).
- Formal traceability matrix linking every URS through to test evidence (`docs/csv/10_TRACEABILITY_MATRIX.md`).
- Change-control governance over the source repository (Phase 9 doc).

This package delivers all of the above.

## 6. Validation strategy

Following ISPE GAMP 5 (2nd ed., 2022) and the risk-based principles of ICH Q9:

1. **Specify** — left side of the V. URS, FS, DS, Config Spec. Already partially exists (`docs/COLLECTOR_V2_GOALS.md`, `docs/COLLECTOR_V2_MIGRATION.md`); we consolidate and extend.
2. **Risk-rank** — every FS item gets an ICH Q9 risk score. High-RPN items get enhanced verification.
3. **Verify & validate** — right side of the V.
   - IQ: install procedure + environment qualification.
   - OQ: the 352-test pytest suite, mapped to FS-IDs.
   - PQ: end-to-end scenarios run against the production-equivalent install.
4. **Trace** — every URS line maps through FS → DS → Risk → Test → Evidence. Gaps are findings.
5. **Govern** — change control, backup/recovery, security, audit trail, periodic review SOPs.

## 7. Acceptance criteria for CSV-readiness

The system is declared **CSV-ready** when:

| # | Criterion |
|---|---|
| 1 | Every URS item has at least one FS item, one DS reference, one risk row, and one test reference. |
| 2 | Every High-RPN (≥9) risk has explicit additional verification documented. |
| 3 | Pytest suite executes 100 % pass (modulo documented pre-existing failures unrelated to Prism's GxP functions). |
| 4 | The ALCOA+ checklist has no Critical or Major gap. |
| 5 | The audit log captures every user-initiated mutating action with user, action, target, timestamp. |
| 6 | A documented + tested backup/restore procedure exists for `prism.db` and key files. |
| 7 | Operational SOPs exist for: user lifecycle, periodic review, incident response, key rotation. |
| 8 | All Critical / Major findings from Phase 12 are remediated; Minor findings have a documented disposition. |

These criteria are evaluated at the end of Phase 14 (`docs/csv/00_CSV_READINESS_REPORT.md`).

## 8. Glossary

| Term | Meaning |
|---|---|
| **ALCOA+** | Data-integrity principle: Attributable, Legible, Contemporaneous, Original, Accurate, Complete, Consistent, Enduring, Available |
| **CSV** | Computer System Validation — the formal demonstration that a computer system does what it's supposed to do |
| **DS** | Design Specification |
| **FS** | Functional Specification |
| **GAMP** | Good Automated Manufacturing Practice (ISPE) |
| **GxP** | Catch-all for regulated practices (GMP, GLP, GCP, GDP, GVP) |
| **ICH Q9** | International Council for Harmonisation guideline on Quality Risk Management |
| **IQ** | Installation Qualification |
| **OQ** | Operational Qualification |
| **PQ** | Performance Qualification |
| **RPN** | Risk Priority Number (Severity × Probability × Detectability) |
| **URS** | User Requirements Specification |
| **WinRM** | Windows Remote Management (the protocol Prism uses) |
| **21 CFR Part 11** | US FDA regulation on electronic records & electronic signatures |

## 9. Document control

| Section | Owner |
|---|---|
| §1–9 (this doc) | Quality / IT-validation |
| Subsequent CSV documents (`02_URS.md` onwards) | Same approver chain |

Change history is tracked via git (see `docs/csv/14_CHANGE_CONTROL.md`).

---
*End of document.*
