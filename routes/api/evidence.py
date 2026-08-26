"""The evidence report — WP-5.

Three formats over one `evidence.build()` result. The CSV goes through the
same `_csv_response` helper as every other export, so it carries the UTF-8 BOM
Excel needs and its timestamps convert to the configured timezone.
"""

import logging

from flask import jsonify, request

import evidence as evidence_mod
import reports_evidence

from . import _shared
from ._shared import api_bp
from .reports import _csv_response, _csv_timestamp, _csv_zone_label

logger = logging.getLogger(__name__)

#: A week. Long enough that a weekly pattern shows up, short enough that the
#: PDF stays a document rather than a phone book.
DEFAULT_HOURS = 168
MAX_HOURS = 24 * 90


def _hours() -> int:
    return max(1, min(request.args.get("hours", DEFAULT_HOURS, type=int), MAX_HOURS))


def _build():
    db = _shared._db
    return evidence_mod.build(db._get_conn(), db=db, hours=_hours())


@api_bp.route("/reports/evidence.json")
def evidence_json():
    try:
        return jsonify({"ok": True, **_build()})
    except Exception:
        logger.exception("Error building the evidence report")
        return jsonify({"ok": False, "error": "Failed to build the report"}), 500


@api_bp.route("/reports/evidence.csv")
def evidence_csv():
    try:
        text = reports_evidence.generate_evidence_csv(
            _build(), ts_fmt=_csv_timestamp, ts_label=_csv_zone_label())
        return _csv_response(text, f"prism_evidence_{_hours()}h.csv")
    except Exception:
        logger.exception("Error building the evidence CSV")
        return jsonify({"ok": False, "error": "Failed to build the report"}), 500


@api_bp.route("/reports/evidence.pdf")
def evidence_pdf():
    try:
        from flask import Response
        pdf = reports_evidence.generate_evidence_pdf(_build())
        return Response(pdf, mimetype="application/pdf", headers={
            "Content-Disposition":
                f'attachment; filename="prism_evidence_{_hours()}h.pdf"'})
    except Exception:
        logger.exception("Error building the evidence PDF")
        return jsonify({"ok": False, "error": "Failed to build the report"}), 500
