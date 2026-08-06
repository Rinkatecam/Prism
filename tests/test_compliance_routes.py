"""Integration tests for the compliance API endpoints.

Pins:
  * All endpoints 404 when compliance.enabled is false (feature gate).
  * Read endpoints require authentication.
  * POST /api/sop/<id>/execute requires RBAC-admin.
  * Execute writes both sop_log and audit_log.
  * /api/system/csv-status returns the documented JSON shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


@pytest.fixture()
def app_client():
    """Real Flask client with compliance ENABLED + a logged-in admin session.

    CSRF is disabled in test mode (matches other route-level tests in the
    suite) so POST endpoints can be exercised without round-tripping a
    token through Flask-WTF.
    """
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"
        sess["login_time"] = datetime.now(timezone.utc).isoformat()
        sess["last_activity"] = datetime.now(timezone.utc).isoformat()
    return client


def _settings_with_compliance(enabled: bool):
    return {"compliance": {"enabled": enabled}, "auth": {}, "timezone": "Europe/Berlin"}


# ── feature flag gating ──────────────────────────────────────────────

def test_csv_status_404s_when_compliance_disabled(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(False)):
        r = app_client.get("/api/system/csv-status")
    assert r.status_code == 404


def test_sop_list_404s_when_compliance_disabled(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(False)):
        r = app_client.get("/api/sop")
    assert r.status_code == 404


def test_sop_execute_404s_when_compliance_disabled(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(False)):
        r = app_client.post(
            "/api/sop/SOP-05/execute",
            data=json.dumps({"result": "pass", "notes": "n"}),
            content_type="application/json",
        )
    assert r.status_code == 404


# ── auth gating ──────────────────────────────────────────────────────

def test_csv_status_returns_documented_shape(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/system/csv-status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["enabled"] is True
    # The documented shape.
    assert "readiness" in data
    rd = data["readiness"]
    assert set(rd.keys()) >= {"total", "current", "due_soon", "overdue",
                               "never", "n_a", "ok"}
    assert "sops" in data
    assert isinstance(data["sops"], list)
    assert "audit" in data


def test_sop_list_returns_nine_sops(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/sop")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["sops"]) == 9


def test_sop_detail_404s_for_unknown_id(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/sop/SOP-99")
    assert r.status_code == 404


def test_sop_render_produces_html(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/sop/SOP-01/render")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "html" in data
    # H1/H2 now carry id attrs from the Wave-B anchor pass, so the
    # substring is ``<h1 ...>`` rather than the bare opening tag.
    assert "<h1" in data["html"] or "<h2" in data["html"]
    # Wave-B: the renderer also returns a TOC alongside the HTML.
    assert "toc" in data
    assert isinstance(data["toc"], list)


def test_sop_raw_returns_markdown(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/sop/SOP-01/raw")
    assert r.status_code == 200
    assert "text/markdown" in r.content_type
    body = r.get_data(as_text=True)
    assert body.startswith("# ") or "SOP-01" in body


# ── execute endpoint ─────────────────────────────────────────────────

def test_execute_requires_rbac_admin(app_client):
    """Execute is RBAC-admin gated — non-admin session is rejected."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_rbac_admin",
               return_value=({"ok": False, "error": "rbac-admin required"}, 403)):
        r = app_client.post(
            "/api/sop/SOP-05/execute",
            data=json.dumps({"result": "pass", "notes": "n"}),
            content_type="application/json",
        )
    assert r.status_code == 403


def test_execute_records_sop_and_audit_row(app_client):
    """The happy path writes both sop_log and audit_log."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_rbac_admin", return_value=None):
        r = app_client.post(
            "/api/sop/SOP-05/execute",
            data=json.dumps({"result": "pass", "notes": "monthly review"}),
            content_type="application/json",
        )
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["ok"] is True
    assert isinstance(data["row_id"], int)
    # Verify the sop_log row + audit_log row exist.
    last = _shared._db.get_latest_sop_execution("SOP-05")
    assert last is not None
    assert last["notes"] == "monthly review"


def test_execute_rejects_invalid_result(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_rbac_admin", return_value=None):
        r = app_client.post(
            "/api/sop/SOP-05/execute",
            data=json.dumps({"result": "bogus"}),
            content_type="application/json",
        )
    assert r.status_code == 400


# ─── CSV doc endpoints (Phase A additions) ───────────────────────────

def test_csv_doc_list_404s_when_compliance_disabled(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(False)):
        r = app_client.get("/api/csv-doc")
    assert r.status_code == 404


def test_csv_doc_list_returns_grouped_categories(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/csv-doc")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "by_category" in data
    cats = data["by_category"]
    # Every CSV doc has a category; we expect at least these groups.
    assert "report" in cats
    assert "spec" in cats
    assert "verification" in cats
    assert "appendix" in cats
    # CSV-00 is the readiness report.
    assert any(d["id"] == "CSV-00" for d in cats["report"])


def test_csv_doc_detail_404s_for_unknown_id(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/csv-doc/CSV-99")
    assert r.status_code == 404


def test_csv_doc_render_produces_html_with_live_data(app_client):
    """CSV-17 (findings register) contains a 'Total' row matched by the
    findings-count regex — confirms live data injection works for CSV
    docs, not just SOPs."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/csv-doc/CSV-17/render")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    # Heading tags carry id attrs after the Wave-B anchor pass.
    assert "<h1" in data["html"] or "<h2" in data["html"]
    # Wave-B: TOC is co-emitted with the HTML.
    assert "toc" in data


# ── F-PHD-FINDINGS: dashboard tile must read the 6-column totals row ──

def test_extract_findings_counts_handles_six_column_totals():
    """**F-PHD-FINDINGS** (regression caught in op-use): the totals row
    in ``17_FINDINGS_AND_GAPS.md`` grew from 4 to 6 numeric columns
    when the PhD-audit + smoke-catch sections were added. The old
    regex matched only the first 4 numbers and mislabelled them — the
    'closed' count landed in the 'open' slot, so the dashboard tile
    screamed '37 OPEN FINDINGS' when in fact zero are open.

    Pin the correct interpretation:
      * ``total`` = pre-audit + phd + smoke (the universe).
      * ``closed`` = column 4, ``risk_accepted`` = column 5, ``open`` = column 6.
      * ``ok`` = True iff ``open == 0``.
    """
    from routes.api.compliance import _extract_findings_counts
    counts = _extract_findings_counts()
    # Total is the sum of the three "added" columns.
    assert counts["total"] == 39, (
        f"expected total=39 (32 pre + 6 phd + 1 smoke), got {counts['total']}"
    )
    assert counts["closed"] == 37, (
        f"expected closed=37, got {counts['closed']} — likely reading wrong column"
    )
    assert counts["risk_accepted"] == 2, (
        f"expected risk_accepted=2, got {counts['risk_accepted']}"
    )
    assert counts["open"] == 0, (
        f"expected open=0 (compliance OK), got {counts['open']} — "
        "this is the 'screaming-37' bug F-PHD-FINDINGS"
    )
    assert counts["ok"] is True


def test_extract_findings_counts_falls_back_to_four_column_legacy():
    """Legacy 4-column totals rows (from earlier audit cycles) should
    still parse so an operator restoring an older doc revision doesn't
    see a blank tile."""
    from routes.api.compliance import _parse_findings_totals
    legacy_md = (
        "# Findings\n\n"
        "| Sev | Total | Closed | Risk | Open |\n"
        "|---|---|---|---|---|\n"
        "| Critical | 1 | 1 | 0 | 0 |\n"
        "| **Total** | **20** | **18** | **2** | **0** |\n"
    )
    counts = _parse_findings_totals(legacy_md)
    assert counts["total"] == 20
    assert counts["closed"] == 18
    assert counts["risk_accepted"] == 2
    assert counts["open"] == 0
    assert counts["ok"] is True


def test_extract_findings_counts_returns_nones_when_no_totals_row():
    """Defensive: a doc without a totals row yields all Nones, not a crash."""
    from routes.api.compliance import _parse_findings_totals
    counts = _parse_findings_totals("# Findings\n\nNo totals row here.")
    assert counts["total"] is None
    assert counts["closed"] is None
    assert counts["open"] is None
    # ok is None too (we don't know).
    assert counts["ok"] is None


def test_extract_findings_counts_open_nonzero_marks_not_ok():
    """If the totals row shows any open finding the tile must flip
    to NOT OK so the operator sees the red badge."""
    from routes.api.compliance import _parse_findings_totals
    md = (
        "| Sev | Pre | PhD | Smoke | Closed | Risk | Open |\n"
        "|---|---|---|---|---|---|---|\n"
        "| **Total** | 20 | 5 | 0 | 23 | 1 | **1** |\n"
    )
    counts = _parse_findings_totals(md)
    assert counts["open"] == 1
    assert counts["ok"] is False


def test_csv_doc_raw_returns_markdown_source(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/csv-doc/CSV-00/raw")
    assert r.status_code == 200
    assert "text/markdown" in r.content_type
    body = r.get_data(as_text=True)
    # Be loose — the title prose may evolve. Just verify it's the right
    # doc by checking the CSV-00 identifier appears.
    assert "CSV-00" in body


# ─── Findings counts surface on csv-status (Phase B) ─────────────────

def test_csv_status_includes_findings_counts(app_client):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_with_compliance(True)), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client.get("/api/system/csv-status")
    assert r.status_code == 200
    data = r.get_json()
    assert "findings" in data
    f = data["findings"]
    # Parsed from 17_FINDINGS_AND_GAPS.md — at audit baseline:
    # total=32, closed=30, risk_accepted=2, open=0.
    assert f.get("total") is not None
    assert f.get("closed") is not None
    assert f.get("open") is not None
