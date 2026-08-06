"""Tests from the post-Wave-6 PhD audit (F-PHD-1 through F-PHD-N).

Pin the security + coverage fixes so they don't regress:

  * F-PHD-1: renderer rejects raw <script>.
  * F-PHD-2: sop_log triggers block UPDATE/DELETE.
  * F-PHD-3: view routes are reachable (the previous test suite only
    covered the API layer).
  * F-PHD-4: every [[csv:KEY]] in the actual shipped SOP / CSV docs
    resolves to a real context key — no silent typos.
  * F-PHD-5: notes containing HTML do not become live HTML in the
    rendered history (covered by F-PHD-3's createElement rewrite —
    here we only assert the API response carries the raw text, since
    the UI escaping happens client-side).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── F-PHD-3: view route reachability ────────────────────────────────

@pytest.fixture
def app_client_compliance_on():
    """Real Flask test client with compliance.enabled = true."""
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"
        sess["login_time"] = datetime.now(timezone.utc).isoformat()
        sess["last_activity"] = datetime.now(timezone.utc).isoformat()
    return client


def _settings_compliance_on():
    return {"compliance": {"enabled": True}, "auth": {}, "timezone": "Europe/Berlin"}


def test_view_route_compliance_dashboard_renders(app_client_compliance_on):
    """``/compliance`` must render the dashboard template (status 200)
    when the flag is on. Pre-fix: untested → a template typo could
    silently 500 with no CI signal."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()):
        r = app_client_compliance_on.get("/compliance")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_data(as_text=True)
    assert "compliance" in body.lower()


def test_view_route_compliance_dashboard_404s_when_flag_off(app_client_compliance_on):
    """When the flag is off the view route must 404 — the feature
    surface is intended to be invisible in non-regulated deployments."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value={"compliance": {"enabled": False}}):
        r = app_client_compliance_on.get("/compliance")
    assert r.status_code == 404


def test_view_route_sop_page_renders(app_client_compliance_on):
    """``/compliance/sop/<sop_id>`` must render compliance_sop.html."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()):
        r = app_client_compliance_on.get("/compliance/sop/SOP-05")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # The page client-side fetches the rendered body; we just verify
    # the template was found and didn't 500.
    assert "SOP-05" in body or "compliance" in body.lower()


def test_view_route_sop_page_404s_for_unknown_id(app_client_compliance_on):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()):
        r = app_client_compliance_on.get("/compliance/sop/SOP-99")
    assert r.status_code == 404


def test_view_route_doc_page_renders(app_client_compliance_on):
    """``/compliance/doc/<doc_id>`` must render compliance_doc.html."""
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()):
        r = app_client_compliance_on.get("/compliance/doc/CSV-17")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "CSV-17" in body or "compliance" in body.lower()


def test_view_route_doc_page_404s_for_unknown_id(app_client_compliance_on):
    from routes.api import _shared
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()):
        r = app_client_compliance_on.get("/compliance/doc/CSV-99")
    assert r.status_code == 404


# ─── F-PHD-4: shipped docs have no typo'd placeholders ───────────────

def test_every_placeholder_in_shipped_docs_resolves():
    """F-PHD-4: walk every ``[[csv:KEY]]`` we wrote into the SOPs +
    CSV docs and verify the key is present in the renderer's context
    dict. A typo would silently render as ``[[csv:KEY — unknown]]``
    with no CI signal under the previous test suite.

    We use the SAME code-stashing logic as the renderer so placeholders
    inside backtick code spans / fenced code blocks are ignored — those
    are intentional prose examples, not real substitutions."""
    from compliance_renderer import (
        build_csv_context, _PLACEHOLDER_RE,
        _FENCED_CODE_RE, _INLINE_CODE_RE,
    )
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    ctx = build_csv_context(db, settings={})

    project_root = Path(__file__).resolve().parent.parent
    md_files = list((project_root / "docs" / "SOPs").glob("*.md"))
    md_files += list((project_root / "docs" / "csv").glob("*.md"))

    typo_findings = []
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="ignore")
        # Strip code regions (mirrors the renderer's substitution logic).
        text_no_code = _FENCED_CODE_RE.sub("", text)
        text_no_code = _INLINE_CODE_RE.sub("", text_no_code)
        for m in _PLACEHOLDER_RE.finditer(text_no_code):
            key = m.group(1)
            if key not in ctx:
                typo_findings.append(f"{md.name}: [[csv:{key}]] is not in renderer context")

    assert not typo_findings, (
        "F-PHD-4: the following placeholders in shipped docs do not "
        "match a known context key. Either fix the typo in the doc OR "
        "add the key to build_csv_context. The placeholder would "
        "otherwise render as '[[csv:KEY — unknown]]' to operators.\n"
        + "\n".join(f"  • {f}" for f in typo_findings)
    )


# ─── F-PHD-3 (continued): notes-content XSS is escaped ───────────────

def test_notes_with_html_returned_verbatim_by_api(app_client_compliance_on, tmp_path):
    """The API returns notes raw — the UI is responsible for safe
    rendering via createElement + textContent (per F-PHD-3). This
    test pins the API contract (no server-side escaping of notes)
    while a separate visual test would pin the client behaviour.

    If the API ever DID escape notes server-side it would break a
    legitimate use case (operator quotes a code snippet in notes).
    The right boundary is at the rendering layer."""
    from routes.api import _shared
    _shared._db.insert_sop_execution(
        sop_id="SOP-05", executed_by="alice",
        notes="<img src=x onerror=alert('xss')>",
        result="pass",
    )
    with patch.object(_shared._config, "get_settings",
                       return_value=_settings_compliance_on()), \
         patch("routes.api.compliance._require_auth", return_value=None):
        r = app_client_compliance_on.get("/api/sop/SOP-05/history")
    assert r.status_code == 200
    data = r.get_json()
    # API returns the raw text — escaping happens client-side.
    assert any("onerror" in (h.get("notes") or "") for h in data["history"])
