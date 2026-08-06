"""Tests for compliance_renderer — markdown rendering + [[csv:KEY]] substitution.

Pins:
  * The placeholder regex catches valid keys + ignores other ``[[...]]`` text.
  * Unknown keys render as a visible "missing" marker (doc-author signal).
  * HTML escaping prevents injection from live-data values.
  * Path-traversal guard refuses paths outside docs/.
  * Markdown rendering produces clean HTML.
  * The context builder doesn't crash if subsystems are unavailable.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── placeholder substitution ─────────────────────────────────────────

def test_substitute_known_key_emits_bold():
    """F-PHD-1 remediation: live values are emitted as ``**value**``
    markdown rather than raw HTML, so a doc containing arbitrary HTML
    can't smuggle the markup through (the renderer has html=False)."""
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders("test_count = [[csv:test_count]]",
                                    {"test_count": "459"})
    assert "459" in out
    assert "**459**" in out


def test_substitute_unknown_key_emits_visible_marker():
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders("foo = [[csv:nope]]", {})
    assert "unknown" in out
    assert "`[[csv:nope" in out


def test_substitute_value_with_bold_marker_does_not_break_boundary():
    """If a live value happens to contain ``**`` characters they get
    normalised so the bold delimiter isn't accidentally terminated."""
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders("x = [[csv:x]]", {"x": "a**b"})
    # Original ``**`` collapsed to ``*`` so the value is still inside
    # the bold delimiter.
    assert "**a*b**" in out


def test_substitute_empty_value_renders_em_dash():
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders("[[csv:x]]", {"x": ""})
    assert "—" in out


def test_substitute_skips_inline_code_spans():
    """F-PHD-4: doc authors write ``[[csv:KEY]]`` inside backtick code
    spans as literal examples explaining the syntax. The renderer
    must NOT substitute placeholders inside those — otherwise the
    operator sees the substituted value (or 'unknown' marker) where
    a literal example was intended."""
    from compliance_renderer import _substitute_placeholders
    source = "Use `[[csv:test_count]]` to embed live test count."
    out = _substitute_placeholders(source, {"test_count": "459"})
    # The placeholder inside backticks is preserved verbatim.
    assert "[[csv:test_count]]" in out
    # The literal "459" should NOT appear (no substitution happened).
    assert "459" not in out


def test_substitute_skips_fenced_code_blocks():
    """Same shape for fenced code blocks (``` …```)."""
    from compliance_renderer import _substitute_placeholders
    source = "Example:\n```\nfoo = [[csv:test_count]]\n```\n"
    out = _substitute_placeholders(source, {"test_count": "459"})
    assert "[[csv:test_count]]" in out
    assert "459" not in out


def test_substitute_in_prose_still_works_alongside_code_spans():
    """Substitution in prose continues to work even when code spans
    appear in the same doc."""
    from compliance_renderer import _substitute_placeholders
    source = "Currently [[csv:test_count]] tests pass; placeholder syntax is `[[csv:KEY]]`."
    out = _substitute_placeholders(source, {"test_count": "459"})
    # Prose value substituted (bold).
    assert "**459**" in out
    # Code-span literal preserved.
    assert "[[csv:KEY]]" in out


def test_renderer_drops_raw_script_from_source():
    """F-PHD-1: the renderer is configured with ``html=False`` so any
    raw ``<script>`` in the markdown source comes out HTML-escaped,
    not as live JavaScript. This is the conservative defence against
    a malicious or accidentally-pasted dangerous tag in a CSV doc."""
    from compliance_renderer import _md, _substitute_placeholders
    source = "# Hello\n\n<script>alert(1)</script>\n\nPlain text."
    # Run through the same pipeline as render_doc.
    substituted = _substitute_placeholders(source, {})
    html_out = _md.render(substituted)
    assert "<script>" not in html_out, (
        f"F-PHD-1: raw <script> must be escaped; got: {html_out!r}"
    )
    # CommonMark escapes the < > characters.
    assert "&lt;script&gt;" in html_out or "alert(1)" in html_out  # one or the other
    # And the legitimate content survives.
    assert "Plain text." in html_out


def test_substitute_leaves_non_csv_brackets_alone():
    """A markdown link like [[Wiki]] or other bracket usage must not
    be matched — the regex requires the ``csv:`` prefix."""
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders("see [[other]] for details", {})
    assert "[[other]]" in out  # unchanged


def test_substitute_handles_multiple_placeholders():
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders(
        "tests=[[csv:t]] findings=[[csv:f]]",
        {"t": "459", "f": "30 of 32 closed"},
    )
    assert "459" in out
    assert "30 of 32 closed" in out


def test_substitute_supports_dotted_keys():
    from compliance_renderer import _substitute_placeholders
    out = _substitute_placeholders(
        "[[csv:last_execution.SOP-05]]",
        {"last_execution.SOP-05": "2026-05-22T08:00:00Z"},
    )
    assert "2026-05-22T08:00:00Z" in out


# ── path-traversal guard ─────────────────────────────────────────────

def test_validate_doc_path_rejects_traversal(tmp_path):
    from compliance_renderer import _validate_doc_path
    with pytest.raises(FileNotFoundError):
        _validate_doc_path("../etc/passwd")
    with pytest.raises(FileNotFoundError):
        _validate_doc_path("docs/../app.py")


def test_validate_doc_path_rejects_non_markdown():
    from compliance_renderer import _validate_doc_path
    with pytest.raises(FileNotFoundError):
        # Pick a non-markdown file we know exists.
        _validate_doc_path("docs/csv/00_CSV_READINESS_REPORT.md.txt")


def test_validate_doc_path_accepts_real_sop_md():
    """The actual SOPs are valid."""
    from compliance_renderer import _validate_doc_path
    p = _validate_doc_path("docs/SOPs/05_validated_baseline_review.md")
    assert p.exists()
    assert p.suffix == ".md"


# ── full render pipeline ─────────────────────────────────────────────

def test_render_doc_produces_html():
    """Smoke test: an existing SOP renders to HTML."""
    from compliance_renderer import render_doc
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    out = render_doc(
        "docs/SOPs/01_user_onboarding.md",
        db, settings={"compliance": {"enabled": True}},
    )
    # Wave-B shape change: render_doc returns {"html", "toc"}.
    assert isinstance(out, dict)
    assert "html" in out and "toc" in out
    html = out["html"]
    assert "<h1" in html or "<h2" in html  # heading_open carries attrs
    assert "SOP-01" in html


def test_render_doc_substitutes_live_placeholders(tmp_path):
    """A doc with [[csv:test_count]] should have it replaced."""
    from compliance_renderer import render_doc
    # Create a temporary doc under docs/ for this test.
    project_root = Path(__file__).resolve().parent.parent
    doc = project_root / "docs" / "_test_csv_render.md"
    doc.write_text(
        "# Test\n\nThe test count is [[csv:test_count]].\n",
        encoding="utf-8",
    )
    try:
        db = MagicMock()
        db.get_acl_rows.return_value = []
        db._audit_insert_failures = 0
        db._audit_mirror_failures = 0
        db.get_all_latest_sop_executions.return_value = {}
        out = render_doc("docs/_test_csv_render.md", db,
                         settings={"compliance": {"enabled": True}})
        html = out["html"]
        # F-PHD-1: live values now render as <strong> (from markdown
        # bold), not raw HTML spans.
        assert "<strong>" in html
        # Either the test_count was found in evidence file or a fallback shown.
        assert "test_count" in html or "see latest pytest run" in html or "<strong>" in html
    finally:
        doc.unlink(missing_ok=True)


# ── Wave-B rendering refresh ─────────────────────────────────────────

def test_render_doc_emits_toc_from_h2_h3_and_h4():
    """Wave-B: render_doc returns a TOC built from H2 + H3 + H4
    headings.  H4 is included because the findings register
    (``17_FINDINGS_AND_GAPS``) uses H4 for every finding card; without
    H4 in the TOC, operators have to scroll through 40k of HTML to
    find a specific finding ID."""
    from compliance_renderer import render_doc
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    out = render_doc(
        "docs/SOPs/05_validated_baseline_review.md",
        db, settings={"compliance": {"enabled": True}},
    )
    toc = out["toc"]
    assert len(toc) >= 4
    # Levels are restricted to 2, 3, 4 (H1 is the title; H5+ too deep).
    assert all(item["level"] in (2, 3, 4) for item in toc)
    titles = [item["text"] for item in toc]
    assert any("Purpose" in t for t in titles)
    ids = [item["id"] for item in toc]
    assert "purpose" in ids


def test_render_doc_findings_register_lists_each_finding_in_toc():
    """The findings register (``17_FINDINGS_AND_GAPS.md``) has one H4
    per finding card (F-075, F-078, F-002, …). Each must appear in
    the TOC so operators can jump straight to a finding by ID. The
    PhD-audit findings (F-PHD-1 …) and the summary lists are
    deliberately tabular, not headings, so they're NOT in the TOC —
    that's a documentation-shape choice, not a renderer gap."""
    from compliance_renderer import render_doc
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    out = render_doc(
        "docs/csv/17_FINDINGS_AND_GAPS.md",
        db, settings={"compliance": {"enabled": True}},
    )
    toc = out["toc"]
    ids = {item["id"] for item in toc}
    # Sample several H4 finding cards from the body of the register.
    assert "f-075" in ids, "expected F-075 (Critical RBAC enforcement) in TOC"
    assert "f-078" in ids, "expected F-078 in TOC"
    assert "f-002" in ids, "expected F-002 (High/Major compute_status) in TOC"
    # Each finding card is at H4 level.
    for finding_id in ("f-075", "f-078", "f-002"):
        item = next(t for t in toc if t["id"] == finding_id)
        assert item["level"] == 4, f"expected {finding_id} at level 4"
    # The findings doc has at least 20 finding cards — assert the TOC
    # actually grew accordingly (regression guard for "what if someone
    # disables H4 again").
    h4_count = sum(1 for t in toc if t["level"] == 4)
    assert h4_count >= 20, f"expected ≥20 H4 finding cards in TOC, got {h4_count}"


def test_render_doc_anchors_headings_with_ids():
    """Every H2/H3 in the rendered HTML carries an id matching the TOC."""
    from compliance_renderer import render_doc
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    out = render_doc(
        "docs/SOPs/05_validated_baseline_review.md",
        db, settings={"compliance": {"enabled": True}},
    )
    for item in out["toc"]:
        # The id appears on the <h2>/<h3> open tag.
        marker = f'id="{item["id"]}"'
        assert marker in out["html"], f"missing anchor id for {item}"


def test_render_doc_renders_pipe_tables():
    """SOP metadata tables (Document ID / Version / …) used to come
    out as a single paragraph because commonmark mode disables tables.
    Wave-B enables the ``table`` rule explicitly so the metadata table
    at the top of every SOP renders as a proper ``<table>``."""
    from compliance_renderer import render_doc
    db = MagicMock()
    db.get_acl_rows.return_value = []
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    db.get_all_latest_sop_executions.return_value = {}
    out = render_doc(
        "docs/SOPs/05_validated_baseline_review.md",
        db, settings={"compliance": {"enabled": True}},
    )
    assert "<table>" in out["html"]
    assert "<th>" in out["html"]


def test_render_doc_highlights_code_blocks():
    """Pygments runs over fenced code blocks. A ``` ```python ``` block
    in a SOP comes out with pygments span classes (Python's ``import``
    keyword becomes a span)."""
    from compliance_renderer import render_doc
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent
    doc = project_root / "docs" / "_test_csv_highlight.md"
    doc.write_text(
        "# Code\n\n```python\nimport os\nprint(os.getcwd())\n```\n",
        encoding="utf-8",
    )
    try:
        db = MagicMock()
        db.get_acl_rows.return_value = []
        db._audit_insert_failures = 0
        db._audit_mirror_failures = 0
        db.get_all_latest_sop_executions.return_value = {}
        out = render_doc("docs/_test_csv_highlight.md", db,
                         settings={"compliance": {"enabled": True}})
        html = out["html"]
        # Pygments emits span classes for tokens.
        assert "doc-code" in html
        # The code body is preserved.
        assert "import" in html
    finally:
        doc.unlink(missing_ok=True)


def test_render_doc_tags_callout_blockquotes():
    """A blockquote that starts with ``Note:`` / ``Warning:`` / etc.
    picks up a ``callout`` CSS class so the template can paint it
    differently from a normal quote."""
    from compliance_renderer import render_doc
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent
    doc = project_root / "docs" / "_test_csv_callout.md"
    doc.write_text(
        "# Callouts\n\n"
        "> Note: this is informational.\n\n"
        "> Warning: this is dangerous.\n\n"
        "> Just a quote, no callout.\n",
        encoding="utf-8",
    )
    try:
        db = MagicMock()
        db.get_acl_rows.return_value = []
        db._audit_insert_failures = 0
        db._audit_mirror_failures = 0
        db.get_all_latest_sop_executions.return_value = {}
        out = render_doc("docs/_test_csv_callout.md", db,
                         settings={"compliance": {"enabled": True}})
        html = out["html"]
        assert "callout-note" in html
        assert "callout-warning" in html
        # A plain quote is still a <blockquote> but without a callout class.
        # Cheap way to check: the "Just a quote" blockquote shouldn't get one.
        assert html.count('class="callout') == 2
    finally:
        doc.unlink(missing_ok=True)


def test_slugify_strips_numbering_prefix():
    """``_slugify`` drops a leading ``4.1`` style prefix so renumbering
    a SOP section doesn't break inbound anchor links."""
    from compliance_renderer import _slugify
    assert _slugify("4.1 Restore into staging") == "restore-into-staging"
    assert _slugify("1. Purpose") == "purpose"
    assert _slugify("Some Heading") == "some-heading"
    # Empty / punctuation-only fallback.
    assert _slugify("") == "section"
    assert _slugify("   ---   ") == "section"


def test_slugify_short_circuits_on_structured_id_prefix():
    """Findings register / URS / FS-style headings start with a
    structured ID. ``_slugify`` uses the ID as the slug verbatim so
    URLs stay short and stable even if the title text changes."""
    from compliance_renderer import _slugify
    assert _slugify("F-075 — No static-analysis enforcement of universal RBAC") == "f-075"
    assert _slugify("F-PHD-1 — XSS via raw HTML") == "f-phd-1"
    assert _slugify("URS-200 — Compliance UI surface") == "urs-200"
    assert _slugify("SOP-05 — Validated baseline review") == "sop-05"
    # Non-prefix headings still go through the regular slug path.
    assert _slugify("Findings register") == "findings-register"


def test_slugify_dedupes_identical_headings_in_toc():
    """If two headings share the same text the second gets a -2 suffix
    so anchor ids stay unique."""
    from compliance_renderer import _annotate_headings_and_extract_toc
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark")
    tokens = md.parse("## Procedure\n\nx\n\n## Procedure\n\ny")
    toc = _annotate_headings_and_extract_toc(tokens)
    ids = [t["id"] for t in toc]
    assert ids == ["procedure", "procedure-2"]


# ── context builder ──────────────────────────────────────────────────

def test_build_context_handles_missing_db_gracefully():
    """If get_acl_rows isn't implemented, context still builds."""
    from compliance_renderer import build_csv_context
    db = MagicMock(spec=[])  # no methods at all
    ctx = build_csv_context(db, settings={})
    assert "test_count" in ctx  # always present
    # Audit-blind defaults to "unknown" or "No" depending on attr access.
    assert "audit_blind" in ctx


def test_build_context_includes_per_sop_keys():
    """[[csv:last_execution.SOP-NN]] keys exist for every SOP."""
    from compliance_renderer import build_csv_context
    import csv_compliance
    db = MagicMock()
    db.get_all_latest_sop_executions.return_value = {}
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    ctx = build_csv_context(db, settings={})
    for sop in csv_compliance.SOP_CATALOGUE:
        assert f"last_execution.{sop.id}" in ctx
        assert f"next_due.{sop.id}" in ctx
        assert f"status.{sop.id}" in ctx


def test_build_context_overall_readiness_field():
    from compliance_renderer import build_csv_context
    db = MagicMock()
    db.get_all_latest_sop_executions.return_value = {}
    db._audit_insert_failures = 0
    db._audit_mirror_failures = 0
    ctx = build_csv_context(db, settings={})
    assert "overall_readiness" in ctx
    # With no executions, every scheduled SOP is "never" → attention.
    assert "ATTENTION" in ctx["overall_readiness"] or "never" in ctx["overall_readiness"].lower()
