"""Correlations — attack shape vs fleet-wide fault. WP-5.

One endpoint over `correlation.py`. The analysis lives in that module so it
can be tested against a database without a Flask app, and so the evidence
report can call it directly rather than through HTTP.
"""

import logging

from flask import jsonify, request

import correlation

from . import _shared
from ._shared import api_bp

logger = logging.getLogger(__name__)

#: A fortnight by default. A day is too short to establish that a burst
#: recurs, and recurrence is most of what distinguishes "a thing that happened"
#: from "a thing that keeps happening".
DEFAULT_HOURS = 24 * 14
MAX_HOURS = 24 * 90


@api_bp.route("/correlations")
def api_correlations():
    """What happened across the fleet, and whether it looks like an attack."""
    try:
        hours = request.args.get("hours", DEFAULT_HOURS, type=int)
        hours = max(1, min(hours, MAX_HOURS))

        # The pooled per-thread connection, same as every other read path.
        # Not a fresh sqlite3.connect: a second handle on a WAL database is
        # how the audit chain forked, and this module only reads.
        conn = _shared._db._get_conn()
        result = correlation.analyse(conn, hours=hours)
        return jsonify({"ok": True, **result})
    except Exception:
        logger.exception("Error building correlations")
        return jsonify({"ok": False, "error": "Failed to build correlations"}), 500
