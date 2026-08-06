"""API endpoints for the in-app CSV / compliance dashboard.

All endpoints are gated on ``compliance.enabled`` in settings. When
disabled they return 404 so a non-regulated deployment has zero
surface — the feature is invisible.

When enabled:

  * Read endpoints require authentication (``_require_auth``).
  * The execute endpoint additionally requires RBAC-admin
    (``_require_rbac_admin``) — recording a SOP execution is a
    quality / validation act, not a routine operator task.
  * Every execution writes one ``sop_log`` row AND one ``audit_log``
    row with action ``sop_execution_recorded``.
"""

from __future__ import annotations

import logging

from flask import jsonify, request
from flask import session as flask_session

from . import _shared
from ._shared import api_bp, _require_auth, _require_rbac_admin

import csv_compliance
import compliance_renderer

logger = logging.getLogger("prism.api.compliance")


def _ensure_compliance_enabled():
    """Gate every endpoint. Returns (None) when enabled; (response, 404)
    tuple when disabled — caller short-circuits with ``return err``."""
    try:
        settings = _shared._config.get_settings()
    except Exception:
        settings = {}
    if not csv_compliance.is_compliance_enabled(settings):
        return jsonify({"ok": False, "error": "compliance feature disabled"}), 404
    return None


# ─── readiness summary ───────────────────────────────────────────────

@api_bp.route("/system/csv-status")
def csv_status():
    """Return the overall CSV readiness aggregate + per-SOP status.

    Shape::

        {
          "ok": true,
          "enabled": true,
          "readiness": {total, current, due_soon, overdue, never, n_a, ok},
          "sops": [{id, title, status, last_executed_at, next_due_at, ...}],
          "audit": {audit_blind, insert_failures, mirror_failures, last_chain_check}
        }
    """
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth

    db = _shared._db
    try:
        latest = db.get_all_latest_sop_executions()
    except Exception:
        latest = {}

    sops_out = []
    for sop in csv_compliance.list_sops():
        last_row = latest.get(sop.id) or {}
        last_ts = last_row.get("executed_at")
        status = csv_compliance.compute_sop_status(sop, last_ts)
        sops_out.append({
            "id": sop.id, "title": sop.title,
            "owner_role": sop.owner_role,
            "purpose": sop.purpose,
            "doc_path": sop.doc_path,
            **status,
            "last_executed_by": last_row.get("executed_by"),
            "last_result": last_row.get("result"),
        })

    audit_block = {
        "insert_failures": getattr(db, "_audit_insert_failures", 0),
        "mirror_failures": getattr(db, "_audit_mirror_failures", 0),
    }
    audit_block["audit_blind"] = (
        audit_block["insert_failures"] > 0
        or audit_block["mirror_failures"] > 0
    )
    try:
        from collector_v2 import state as _v2_state
        audit_block["last_chain_check"] = getattr(_v2_state, "last_audit_chain_check", None)
    except Exception:
        audit_block["last_chain_check"] = None

    # Findings counts — reuse the renderer's context builder so the
    # dashboard sees the same numbers any rendered doc would show.
    findings_block = _extract_findings_counts()

    return jsonify({
        "ok": True,
        "enabled": True,
        "readiness": csv_compliance.get_overall_readiness(latest),
        "sops": sops_out,
        "audit": audit_block,
        "findings": findings_block,
    })


def _parse_findings_totals(md: str) -> dict:
    """Pure-string parser for the totals row of the findings register.

    Returns ``{total, closed, risk_accepted, open, ok}`` (all ``None``
    if the totals row can't be located). Factored out of
    ``_extract_findings_counts`` so unit tests can pass arbitrary
    markdown without monkeypatching file I/O — caught me in CI when
    the recursive monkeypatch on ``Path.read_text`` stack-overflowed.
    """
    import re
    counts: dict = {
        "total": None, "closed": None,
        "risk_accepted": None, "open": None,
    }
    m = re.search(r"^\|\s*\*?\*?Total\*?\*?\s*\|(.+?)\|\s*$", md, re.M)
    if m:
        cells = [c.strip() for c in m.group(1).split("|")]
        nums: list[int] = []
        for c in cells:
            bare = re.sub(r"[*\s]", "", c)
            if bare.isdigit():
                nums.append(int(bare))
        if len(nums) >= 6:
            # Current shape: pre | phd | smoke | closed | risk | open.
            counts["total"] = nums[0] + nums[1] + nums[2]
            counts["closed"] = nums[3]
            counts["risk_accepted"] = nums[4]
            counts["open"] = nums[5]
        elif len(nums) == 4:
            # Legacy shape: total | closed | risk | open.
            counts["total"] = nums[0]
            counts["closed"] = nums[1]
            counts["risk_accepted"] = nums[2]
            counts["open"] = nums[3]
    counts["ok"] = (counts["open"] == 0) if counts["open"] is not None else None
    return counts


def _extract_findings_counts() -> dict:
    """Pull the findings-register summary out of ``17_FINDINGS_AND_GAPS.md``
    so the dashboard tile always reflects the latest written register.

    The totals row in the doc has SIX numeric columns since the
    PhD audit + smoke-catch additions:

        | **Total** | <pre-audit> | <phd> | <smoke> | <closed> | <risk-acc> | <open> |

    The reported counts:
      * ``total``         = pre-audit + phd + smoke (the universe).
      * ``closed``        = column 4 (everything pinned by code/test/SOP evidence).
      * ``risk_accepted`` = column 5 (everything signed off with rationale).
      * ``open``          = column 6 (must be 0 for compliance OK).

    For forward-compatibility ``_parse_findings_totals`` also accepts
    the historical 4-column shape so docs from prior audit cycles
    still render correctly.

    **F-PHD-FINDINGS** (regression caught in op-use): the previous
    extractor matched only 4 capture groups and so silently mapped
    the new doc's ``closed=37`` into the ``open`` slot, leaving the
    dashboard tile screaming "37 OPEN FINDINGS" when in fact zero
    are open. Pinned by ``test_extract_findings_counts_handles_six_column_totals``.
    """
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent.parent
    try:
        md = (project_root / "docs" / "csv" / "17_FINDINGS_AND_GAPS.md").read_text(
            encoding="utf-8", errors="ignore",
        )
    except Exception:
        return {
            "total": None, "closed": None,
            "risk_accepted": None, "open": None, "ok": None,
        }
    return _parse_findings_totals(md)


# ─── SOP listing / detail ────────────────────────────────────────────

@api_bp.route("/sop")
def list_sops():
    """List every SOP in the catalogue with its current status."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth

    db = _shared._db
    try:
        latest = db.get_all_latest_sop_executions()
    except Exception:
        latest = {}
    out = []
    for sop in csv_compliance.list_sops():
        last_row = latest.get(sop.id) or {}
        ts = last_row.get("executed_at")
        out.append({
            "id": sop.id, "title": sop.title,
            "cadence_days": sop.cadence_days,
            "owner_role": sop.owner_role,
            "doc_path": sop.doc_path,
            "purpose": sop.purpose,
            **csv_compliance.compute_sop_status(sop, ts),
            "last_executed_by": last_row.get("executed_by"),
            "last_result": last_row.get("result"),
        })
    return jsonify({"ok": True, "sops": out})


@api_bp.route("/sop/<sop_id>")
def get_sop(sop_id: str):
    """Single SOP detail + last execution + computed status."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    sop = csv_compliance.get_sop(sop_id)
    if not sop:
        return jsonify({"ok": False, "error": "unknown sop_id"}), 404
    db = _shared._db
    try:
        last = db.get_latest_sop_execution(sop_id)
    except Exception:
        last = None
    ts = last.get("executed_at") if last else None
    return jsonify({
        "ok": True,
        "id": sop.id, "title": sop.title,
        "cadence_days": sop.cadence_days,
        "owner_role": sop.owner_role,
        "doc_path": sop.doc_path,
        "purpose": sop.purpose,
        **csv_compliance.compute_sop_status(sop, ts),
        "last_execution": last,
    })


@api_bp.route("/sop/<sop_id>/history")
def get_sop_history(sop_id: str):
    """Execution history for one SOP (most-recent-first)."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    if not csv_compliance.get_sop(sop_id):
        return jsonify({"ok": False, "error": "unknown sop_id"}), 404
    limit = int(request.args.get("limit", 50))
    try:
        rows = _shared._db.get_sop_execution_history(sop_id, limit=limit)
    except Exception:
        rows = []
    return jsonify({"ok": True, "history": rows})


@api_bp.route("/sop/<sop_id>/render")
def render_sop(sop_id: str):
    """Server-rendered SOP HTML with live ``[[csv:KEY]]`` substitution."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    sop = csv_compliance.get_sop(sop_id)
    if not sop:
        return jsonify({"ok": False, "error": "unknown sop_id"}), 404
    try:
        body = compliance_renderer.render_doc(
            sop.doc_path, _shared._db, _shared._config.get_settings(),
        )
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    # ``render_doc`` returns {"html", "toc"} since Wave-B; expose both
    # so the template can render the side-nav TOC.
    return jsonify({
        "ok": True,
        "id": sop.id, "title": sop.title,
        "doc_path": sop.doc_path,
        "html": body["html"],
        "toc": body["toc"],
    })


@api_bp.route("/sop/<sop_id>/raw")
def raw_sop(sop_id: str):
    """Raw markdown source — used by the "view raw .md in another tab"
    link in the compliance UI."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    sop = csv_compliance.get_sop(sop_id)
    if not sop:
        return jsonify({"ok": False, "error": "unknown sop_id"}), 404
    try:
        text = compliance_renderer.read_raw_doc(sop.doc_path)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    from flask import Response
    return Response(text, mimetype="text/markdown; charset=utf-8")


# ─── CSV document browser ────────────────────────────────────────────
# Parallel surface to the SOP endpoints, but for the V-model + spec
# documents under docs/csv/. Read-only — there is no "execute" for a
# spec doc; the equivalent action is the periodic re-review (covered
# by SOP-05) which already has its own audit trail.


@api_bp.route("/csv-doc")
def list_csv_docs_endpoint():
    """List every CSV document grouped by category."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    by_cat = csv_compliance.csv_docs_by_category()
    out = {}
    for cat, docs in by_cat.items():
        out[cat] = [{
            "id": d.id, "title": d.title,
            "doc_path": d.doc_path, "short_desc": d.short_desc,
        } for d in docs]
    return jsonify({"ok": True, "by_category": out})


@api_bp.route("/csv-doc/<doc_id>")
def get_csv_doc_endpoint(doc_id: str):
    """Detail block for one CSV doc."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    doc = csv_compliance.get_csv_doc(doc_id)
    if not doc:
        return jsonify({"ok": False, "error": "unknown doc_id"}), 404
    return jsonify({
        "ok": True,
        "id": doc.id, "title": doc.title,
        "doc_path": doc.doc_path,
        "category": doc.category,
        "short_desc": doc.short_desc,
    })


@api_bp.route("/csv-doc/<doc_id>/render")
def render_csv_doc_endpoint(doc_id: str):
    """Server-rendered CSV-doc HTML with live ``[[csv:KEY]]`` substitution.

    Same renderer as SOPs (compliance_renderer.render_doc). All keys in
    the live context — including findings counts and audit-chain status
    — are substituted, so the readiness report shows fresh numbers
    every time it's rendered.
    """
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    doc = csv_compliance.get_csv_doc(doc_id)
    if not doc:
        return jsonify({"ok": False, "error": "unknown doc_id"}), 404
    try:
        body = compliance_renderer.render_doc(
            doc.doc_path, _shared._db, _shared._config.get_settings(),
        )
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    return jsonify({
        "ok": True,
        "id": doc.id, "title": doc.title,
        "doc_path": doc.doc_path,
        "category": doc.category,
        "html": body["html"],
        "toc": body["toc"],
    })


@api_bp.route("/csv-doc/<doc_id>/raw")
def raw_csv_doc_endpoint(doc_id: str):
    """Raw markdown source — used by the "view raw .md in another tab"
    link in the CSV-doc browser."""
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_auth()
    if auth:
        return auth
    doc = csv_compliance.get_csv_doc(doc_id)
    if not doc:
        return jsonify({"ok": False, "error": "unknown doc_id"}), 404
    try:
        text = compliance_renderer.read_raw_doc(doc.doc_path)
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    from flask import Response
    return Response(text, mimetype="text/markdown; charset=utf-8")


# ─── Execute (RBAC-admin gated, audited) ─────────────────────────────

@api_bp.route("/sop/<sop_id>/execute", methods=["POST"])
def execute_sop(sop_id: str):
    """Record one execution of an SOP. RBAC-admin only.

    Body (JSON)::
        { "result": "pass" | "fail" | "partial",
          "notes": "Free-form text",
          "evidence_ref": "optional pointer (filename / ticket id)" }

    Writes ``sop_log`` row + ``audit_log`` row (the latter via
    ``Database.insert_sop_execution``).
    """
    err = _ensure_compliance_enabled()
    if err:
        return err
    auth = _require_rbac_admin()
    if auth:
        return auth
    sop = csv_compliance.get_sop(sop_id)
    if not sop:
        return jsonify({"ok": False, "error": "unknown sop_id"}), 404

    data = request.get_json(silent=True) or {}
    result = (data.get("result") or "pass").strip().lower()
    if result not in ("pass", "fail", "partial"):
        return jsonify({"ok": False, "error": "invalid result"}), 400
    notes = data.get("notes") or None
    evidence_ref = data.get("evidence_ref") or None
    user = flask_session.get("username", "system")

    try:
        row_id = _shared._db.insert_sop_execution(
            sop_id=sop_id,
            executed_by=user,
            result=result,
            notes=notes,
            evidence_ref=evidence_ref,
        )
    except Exception as exc:
        logger.exception("Failed to record SOP execution for %s", sop_id)
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500

    return jsonify({"ok": True, "row_id": row_id})
