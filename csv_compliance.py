"""Compliance / CSV domain module.

Single source of truth for:

  * The canonical SOP catalogue (id, title, cadence, owner, doc path).
  * Status computation: ``current`` / ``due_soon`` / ``overdue`` /
    ``never`` / ``n_a`` for each SOP given its last-execution timestamp.
  * Overall fleet-of-SOPs readiness aggregate.
  * The feature-flag check that gates the entire compliance UI surface.

The module has NO side effects — pure data + pure functions. State
lives in ``database.sop_log``; settings live in ``config.json``. Every
caller (API endpoint, view route, periodic, test) reads through these
functions so the SOP catalogue stays in one place.

Why this lives at the repo top-level (not under collector_v2/ or
routes/): compliance is a cross-cutting concern that the API layer,
the view layer, AND tests all read. Putting it in any one subsystem
would force the others to import sideways.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

# ─── Canonical SOP catalogue ──────────────────────────────────────────
#
# Order is the display order on the compliance dashboard. Cadence
# semantics:
#   * Integer days  → fixed cadence; status = current / due_soon /
#                     overdue / never.
#   * None         → per-event SOP (e.g., onboarding fires per-hire,
#                     not on a schedule); status is always 'n_a'.
#
# ``owner_role`` is informational; the actual permission check is
# enforced in the API layer (``_require_rbac_admin`` for execute).
#
# Adding a new SOP: append here, add the docs/SOPs/NN_*.md file, and
# the dashboard picks it up automatically. No DB migration needed.


@dataclass(frozen=True)
class SopDef:
    id: str
    title: str
    cadence_days: int | None  # None = per-event, not scheduled
    owner_role: str
    doc_path: str             # repo-relative
    purpose: str              # one-line summary for the card


SOP_CATALOGUE: tuple[SopDef, ...] = (
    SopDef("SOP-01", "User onboarding",          None, "RBAC-admin",
           "docs/SOPs/01_user_onboarding.md",
           "Grant Prism access to a new operator with documented role"),
    SopDef("SOP-02", "User offboarding",         None, "RBAC-admin",
           "docs/SOPs/02_user_offboarding.md",
           "Revoke a leaving operator's access on the same business day"),
    SopDef("SOP-03", "Periodic ACL review",      90,   "RBAC-admin",
           "docs/SOPs/03_periodic_acl_review.md",
           "Confirm continued business need for every ACL row"),
    SopDef("SOP-04", "Incident response",        None, "IT operations",
           "docs/SOPs/04_incident_response.md",
           "Triage playbook for Prism-detected outages"),
    SopDef("SOP-05", "Validated-baseline review", 30,  "IT-validation",
           "docs/SOPs/05_validated_baseline_review.md",
           "Confirm Prism is still operating in its qualified state"),
    SopDef("SOP-06", "PowerShell governance",    90,   "Security engineering",
           "docs/SOPs/06_powershell_governance.md",
           "Quarterly review of free-form PS usage and sandbox allowlist"),
    SopDef("SOP-07", "Audit log archival",       90,   "Quality",
           "docs/SOPs/07_audit_log_archival.md",
           "Move aged audit material to controlled cold storage"),
    SopDef("SOP-08", "Disaster recovery test",   90,   "IT operations",
           "docs/SOPs/08_disaster_recovery_test.md",
           "Prove a Prism backup can actually be restored"),
    SopDef("SOP-09", "Audit mirror rotation",    None, "IT operations",
           "docs/SOPs/09_audit_mirror_rotation.md",
           "OS-level rotation of audit_mirror.jsonl"),
)

# ─── CSV document catalogue ──────────────────────────────────────────
#
# The V-model + supporting docs that live under docs/csv/. Same idea as
# SOPs but without execution cadence — these are reference / specification
# documents. The in-app browser renders them with [[csv:KEY]] live-data
# substitution so readers see fresh values inline (e.g., live test count
# in the readiness report).

@dataclass(frozen=True)
class CsvDoc:
    id: str             # "CSV-00", "CSV-01", ..., "APX-A"
    title: str
    doc_path: str       # repo-relative
    category: str       # "report" | "spec" | "risk" | "verification" | "trace" | "data_integrity" | "process" | "appendix"
    short_desc: str     # one-line operator-facing description


CSV_DOC_CATALOGUE: tuple[CsvDoc, ...] = (
    # Cover + scope
    CsvDoc("CSV-00", "CSV Readiness Report",
           "docs/csv/00_CSV_READINESS_REPORT.md", "report",
           "Cover page — read first; aggregates the verdict and links every artifact"),
    CsvDoc("CSV-01", "Scope & GAMP Categorisation",
           "docs/csv/01_scope_and_categorisation.md", "spec",
           "System boundary, intended use, GAMP 5 Category 5 justification"),
    # V-model left side
    CsvDoc("CSV-02", "User Requirements (URS)",
           "docs/csv/02_URS.md", "spec",
           "78 user requirements anchoring the traceability chain"),
    CsvDoc("CSV-03", "Functional Specification (FS)",
           "docs/csv/03_FS.md", "spec",
           "78 functional items mapping each URS to a module"),
    CsvDoc("CSV-04", "Design Specification (DS)",
           "docs/csv/04_DS.md", "spec",
           "23 design items: three-thread collector, sandbox, audit hash chain"),
    CsvDoc("CSV-05", "Configuration Specification",
           "docs/csv/05_CONFIG_SPEC.md", "spec",
           "Every configurable parameter, default, valid range"),
    # Risk
    CsvDoc("CSV-06", "Risk Assessment (ICH Q9)",
           "docs/csv/06_RISK_ASSESSMENT.md", "risk",
           "Per-FS RPN ranking; identifies Critical / High items"),
    # V-model right side
    CsvDoc("CSV-07", "IQ Protocol",
           "docs/csv/07_IQ_PROTOCOL.md", "verification",
           "15 installation-qualification tests for a new deploy"),
    CsvDoc("CSV-08", "OQ Test Inventory",
           "docs/csv/08_OQ_TEST_INVENTORY.md", "verification",
           "Pytest suite mapped to FS items (operational qualification)"),
    CsvDoc("CSV-09", "PQ Scenarios",
           "docs/csv/09_PQ_SCENARIOS.md", "verification",
           "20+ end-to-end operator scenarios with expected outcomes"),
    # Trace
    CsvDoc("CSV-10", "Traceability Matrix",
           "docs/csv/10_TRACEABILITY_MATRIX.md", "trace",
           "URS ↔ FS ↔ DS ↔ Risk ↔ Test ↔ PQ in one table"),
    # Data integrity + audit
    CsvDoc("CSV-11", "Data Integrity Audit (ALCOA+)",
           "docs/csv/11_DATA_INTEGRITY.md", "data_integrity",
           "Per-record check of Attributable/Legible/Contemporaneous/etc."),
    CsvDoc("CSV-12", "Audit Trail + 21 CFR Part 11",
           "docs/csv/12_AUDIT_TRAIL.md", "data_integrity",
           "Coverage, integrity, Part 11 applicability evaluation"),
    # Process
    CsvDoc("CSV-13", "Security & Access Control",
           "docs/csv/13_SECURITY.md", "process",
           "Auth, RBAC, CSRF, CSP, secrets, supply chain"),
    CsvDoc("CSV-14", "Change Control",
           "docs/csv/14_CHANGE_CONTROL.md", "process",
           "How changes flow once the system is validated"),
    CsvDoc("CSV-15", "Backup & Recovery",
           "docs/csv/15_BACKUP_RECOVERY.md", "process",
           "What's backed up, how to restore, RTO/RPO targets"),
    CsvDoc("CSV-16", "SOP Catalogue",
           "docs/csv/16_SOP_CATALOGUE.md", "process",
           "Index of operational SOPs; mirrors docs/SOPs/"),
    # Findings
    CsvDoc("CSV-17", "Findings & Gap Analysis",
           "docs/csv/17_FINDINGS_AND_GAPS.md", "report",
           "Consolidated register: 32 findings, disposition, remediation evidence"),
    # Appendices (reference data)
    CsvDoc("APX-A", "Appendix A — collector_v2 inventory",
           "docs/csv/appendix_A_collector_inventory.md", "appendix",
           "Three-thread pipeline + shared state, owners, locks"),
    CsvDoc("APX-B", "Appendix B — HTTP API surface",
           "docs/csv/appendix_B_api_surface.md", "appendix",
           "All Flask endpoints, auth gates, audit calls"),
    CsvDoc("APX-C", "Appendix C — Core modules",
           "docs/csv/appendix_C_core_modules.md", "appendix",
           "Top-level Python modules: purpose, threading, dependencies"),
    CsvDoc("APX-D", "Appendix D — DB schema",
           "docs/csv/appendix_D_db_schema.md", "appendix",
           "Every SQLite table, retention, integrity controls"),
    CsvDoc("APX-E", "Appendix E — Test inventory",
           "docs/csv/appendix_E_test_inventory.md", "appendix",
           "Test files, categorisation, coverage gaps"),
)


def get_csv_doc(doc_id: str) -> CsvDoc | None:
    for d in CSV_DOC_CATALOGUE:
        if d.id == doc_id:
            return d
    return None


def list_csv_docs() -> list[CsvDoc]:
    return list(CSV_DOC_CATALOGUE)


def csv_docs_by_category() -> dict[str, list[CsvDoc]]:
    """Group docs by category, preserving the catalogue order within each
    group. Display order: report → spec → risk → verification → trace →
    data_integrity → process → appendix."""
    order = ["report", "spec", "risk", "verification", "trace",
             "data_integrity", "process", "appendix"]
    out: dict[str, list[CsvDoc]] = {k: [] for k in order}
    for d in CSV_DOC_CATALOGUE:
        out.setdefault(d.category, []).append(d)
    # Drop empty categories.
    return {k: v for k, v in out.items() if v}


# Status constants — single source so callers can switch over them.
STATUS_CURRENT = "current"
STATUS_DUE_SOON = "due_soon"   # within 7 days of cadence
STATUS_OVERDUE = "overdue"
STATUS_NEVER = "never"          # cadence set but no execution ever recorded
STATUS_NA = "n_a"               # per-event SOP, no schedule

DUE_SOON_WINDOW_DAYS = 7


# ─── Feature flag ─────────────────────────────────────────────────────

def is_compliance_enabled(settings: dict | None) -> bool:
    """Read the ``compliance.enabled`` setting; default off.

    When False the API endpoints under /api/sop/* and /api/system/csv-status
    return 404 and the /compliance view route 404s as well — so a
    non-regulated deployment has zero UI surface and looks identical to
    a Prism instance that doesn't have these features. This protects
    the IT-shop scenario (CSV-01 Scenario A).
    """
    if not settings:
        return False
    cfg = settings.get("compliance") or {}
    return bool(cfg.get("enabled", False))


# ─── Catalogue access ────────────────────────────────────────────────

def get_sop(sop_id: str) -> SopDef | None:
    """Lookup by id."""
    for s in SOP_CATALOGUE:
        if s.id == sop_id:
            return s
    return None


def list_sops() -> list[SopDef]:
    """Return the catalogue as a list (preserves display order)."""
    return list(SOP_CATALOGUE)


# ─── Status computation ───────────────────────────────────────────────

def _parse_iso_utc(ts: str) -> datetime | None:
    """Best-effort parse of the ISO-8601 UTC timestamps the DB writes.
    Returns ``None`` on malformed input (the row is still listable;
    status just reads as 'never')."""
    if not ts:
        return None
    try:
        t = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def compute_sop_status(
    sop: SopDef,
    last_executed_at: str | None,
    now: datetime | None = None,
) -> dict:
    """Return a status dict for one SOP.

    Shape::

        {
          "id": "SOP-05",
          "status": "current" | "due_soon" | "overdue" | "never" | "n_a",
          "last_executed_at": "2026-04-15T08:00:00Z" | None,
          "next_due_at": "2026-05-15T08:00:00Z" | None,
          "days_overdue": int (positive if overdue, else 0),
          "cadence_days": int | None,
        }

    The ``next_due_at`` field is ``None`` for per-event SOPs. Callers
    render "(no fixed schedule)" in that case.
    """
    now = now or datetime.now(timezone.utc)

    # Per-event SOPs are never overdue. The dashboard shows them as
    # "n/a" with a note like "executes per-hire / per-departure".
    if sop.cadence_days is None:
        return {
            "id": sop.id,
            "status": STATUS_NA,
            "last_executed_at": last_executed_at,
            "next_due_at": None,
            "days_overdue": 0,
            "cadence_days": None,
        }

    if not last_executed_at:
        return {
            "id": sop.id,
            "status": STATUS_NEVER,
            "last_executed_at": None,
            "next_due_at": None,
            "days_overdue": 0,
            "cadence_days": sop.cadence_days,
        }

    last = _parse_iso_utc(last_executed_at)
    if last is None:
        # Malformed timestamp in the DB — treat as never so the
        # operator notices and re-executes.
        return {
            "id": sop.id,
            "status": STATUS_NEVER,
            "last_executed_at": last_executed_at,
            "next_due_at": None,
            "days_overdue": 0,
            "cadence_days": sop.cadence_days,
        }

    next_due = last + timedelta(days=sop.cadence_days)
    delta = (next_due - now).total_seconds() / 86400.0  # days

    if delta < 0:
        status = STATUS_OVERDUE
        days_overdue = int(-delta) + 1
    elif delta <= DUE_SOON_WINDOW_DAYS:
        status = STATUS_DUE_SOON
        days_overdue = 0
    else:
        status = STATUS_CURRENT
        days_overdue = 0

    return {
        "id": sop.id,
        "status": status,
        "last_executed_at": last_executed_at,
        "next_due_at": next_due.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_overdue": days_overdue,
        "cadence_days": sop.cadence_days,
    }


def get_overall_readiness(
    latest_by_sop: dict[str, dict],
    now: datetime | None = None,
) -> dict:
    """Aggregate readiness across the whole SOP catalogue.

    Args:
        latest_by_sop: dict from ``db.get_all_latest_sop_executions()``
            shape ``{sop_id: {executed_at, ...}}``. SOPs absent from
            this dict are treated as "never executed".
        now: optional reference time (mostly for tests).

    Returns::

        {
          "total":     int,   # SOPs with a fixed cadence
          "current":   int,
          "due_soon":  int,
          "overdue":   int,
          "never":     int,
          "n_a":       int,   # per-event SOPs (excluded from "total")
          "ok":        bool,  # True iff overdue == 0 AND never == 0
        }
    """
    counts = {
        STATUS_CURRENT: 0, STATUS_DUE_SOON: 0, STATUS_OVERDUE: 0,
        STATUS_NEVER: 0, STATUS_NA: 0,
    }
    for sop in SOP_CATALOGUE:
        last = latest_by_sop.get(sop.id) or {}
        ts = last.get("executed_at")
        s = compute_sop_status(sop, ts, now=now)["status"]
        counts[s] += 1

    scheduled_total = sum(counts[k] for k in (
        STATUS_CURRENT, STATUS_DUE_SOON, STATUS_OVERDUE, STATUS_NEVER,
    ))
    return {
        "total": scheduled_total,
        "current": counts[STATUS_CURRENT],
        "due_soon": counts[STATUS_DUE_SOON],
        "overdue": counts[STATUS_OVERDUE],
        "never": counts[STATUS_NEVER],
        "n_a": counts[STATUS_NA],
        "ok": counts[STATUS_OVERDUE] == 0 and counts[STATUS_NEVER] == 0,
    }
