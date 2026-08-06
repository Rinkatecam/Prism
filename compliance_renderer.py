"""Markdown rendering for SOP / CSV documents with live-data injection.

A SOP is a markdown file that may contain ``[[csv:KEY]]`` placeholders
referencing live application state (e.g., the current ACL row count,
the audit-blind status, when SOP-N was last executed). The renderer:

  1. Reads the markdown source from disk.
  2. Substitutes every ``[[csv:KEY]]`` against a context dict built
     from db + state + settings.
  3. Renders to HTML via ``markdown_it`` (already in the deps), with:
       * slug IDs + anchor links on every H2/H3/H4 heading
       * a table-of-contents extracted from H2/H3 (returned alongside
         the HTML so the template can render a sticky sidebar)
       * pygments-highlighted code blocks with language hints
       * "callout" blockquotes (Note: / Warning: / Tip: / …) that pick
         up coloured left-borders and icons via CSS

The substitution is intentionally STRING-LEVEL — we don't pipe through
Jinja because Jinja's ``{{ }}`` syntax conflicts with the
``{{step.X.output}}`` examples already present in the CSV docs.

Keys are case-sensitive and namespaced::

    [[csv:test_count]]
    [[csv:audit_blind]]
    [[csv:audit_chain_last_check]]
    [[csv:acl_count]]
    [[csv:last_execution.SOP-05]]
    [[csv:next_due.SOP-05]]
    [[csv:overdue_sops]]
    [[csv:findings_total]]    [[csv:findings_closed]]    [[csv:findings_open]]
    [[csv:overall_readiness]]

Unknown keys render as ``<code>[[csv:KEY — unknown]]</code>`` so
doc authors see typos. Empty values render as ``—`` (em-dash).

Security: the markdown source is repo-controlled (not user-editable
through the app); even so the renderer is configured with
``html=False`` so any inline ``<script>`` snuck into a doc is escaped
rather than executed. Live values are emitted as markdown ``**bold**``
(rendered to ``<strong>``) so substitution never crosses the
HTML boundary. The pygments output is well-formed and produced by a
trusted library, so it's emitted via the markdown_it highlight hook
(escaping handled by pygments itself).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path

# markdown_it is already in requirements.lock — no new dep.
from markdown_it import MarkdownIt

# pygments ships with the broader dependency set (used elsewhere for
# code rendering) and is already in requirements.lock.
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

import csv_compliance


_PROJECT_ROOT = Path(__file__).resolve().parent
_PLACEHOLDER_RE = re.compile(r"\[\[csv:([A-Za-z0-9_.-]+)\]\]")


# ─── Context builder ─────────────────────────────────────────────────

def build_csv_context(db, settings: dict | None) -> dict[str, str]:
    """Build the {key: replacement_text} mapping the renderer uses.

    All values are pre-formatted strings (already HTML-escaped where
    necessary) so the renderer can do a single text-level substitution.
    """
    ctx: dict[str, str] = {}

    # Test count — read from the most recent pytest evidence file if
    # available. Falls back to a literal so the doc still renders.
    try:
        evidence_dir = _PROJECT_ROOT / "docs" / "csv" / "evidence"
        latest = sorted(evidence_dir.glob("OQ_pytest_run_*.txt"))
        if latest:
            txt = latest[-1].read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"(\d+)\s+passed", txt)
            if m:
                ctx["test_count"] = m.group(1)
    except Exception:
        pass
    ctx.setdefault("test_count", "(see latest pytest run)")

    # Audit telemetry: pull straight from the live DB counters + the
    # last-chain-check slot on collector_v2.state.
    try:
        insert_fails = int(getattr(db, "_audit_insert_failures", 0) or 0)
        mirror_fails = int(getattr(db, "_audit_mirror_failures", 0) or 0)
        audit_blind = insert_fails > 0 or mirror_fails > 0
        ctx["audit_blind"] = (
            f"YES — investigate ({insert_fails} insert, {mirror_fails} mirror failures)"
            if audit_blind else "No"
        )
        ctx["audit_insert_failures"] = str(insert_fails)
        ctx["audit_mirror_failures"] = str(mirror_fails)
    except Exception:
        ctx["audit_blind"] = "(unknown)"

    try:
        from collector_v2 import state as _v2_state
        lcc = getattr(_v2_state, "last_audit_chain_check", None)
        if lcc and lcc.get("ts"):
            age = max(0, int(datetime.now(timezone.utc).timestamp() - lcc["ts"]))
            mins = age // 60
            ago = f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"
            status = "OK" if lcc.get("ok") else f"FAIL ({lcc.get('first_break_reason')})"
            ctx["audit_chain_last_check"] = f"{ago}, {status}"
        else:
            ctx["audit_chain_last_check"] = "(not yet run — the hourly verifier checks back within 1 h of boot)"
    except Exception:
        ctx["audit_chain_last_check"] = "(unavailable)"

    # ACL row count.
    try:
        acl_rows = db.get_acl_rows() if hasattr(db, "get_acl_rows") else []
        ctx["acl_count"] = str(len(acl_rows) if acl_rows else 0)
    except Exception:
        # Fall back to raw count via the table.
        try:
            import sqlite3 as _sql
            with _sql.connect(str(db.db_path)) as c:
                ctx["acl_count"] = str(c.execute(
                    "SELECT COUNT(*) FROM user_server_acl"
                ).fetchone()[0])
        except Exception:
            ctx["acl_count"] = "(unavailable)"

    # Per-SOP last-execution and next-due timestamps + overall readiness.
    # F-PHD-5 (post-audit fix): compute_sop_status is called ONCE per SOP
    # and the result reused for per-SOP keys, the overdue list, AND the
    # readiness aggregate. Previous shape called it ~3× per SOP (9 → 27
    # calls per render).
    try:
        latest = db.get_all_latest_sop_executions() if hasattr(db, "get_all_latest_sop_executions") else {}
    except Exception:
        latest = {}

    per_sop_status: dict[str, dict] = {}
    overdue_sop_ids: list[str] = []
    for sop in csv_compliance.SOP_CATALOGUE:
        last_row = latest.get(sop.id) or {}
        last_ts = last_row.get("executed_at") or ""
        ctx[f"last_execution.{sop.id}"] = last_ts or "(never)"
        status = csv_compliance.compute_sop_status(sop, last_ts or None)
        per_sop_status[sop.id] = status
        ctx[f"next_due.{sop.id}"] = status.get("next_due_at") or "(no fixed schedule)"
        ctx[f"status.{sop.id}"] = status["status"]
        if status["status"] == csv_compliance.STATUS_OVERDUE:
            overdue_sop_ids.append(sop.id)
    ctx["overdue_sops"] = ", ".join(overdue_sop_ids) if overdue_sop_ids else "(none)"

    # Overall readiness — get_overall_readiness recomputes statuses
    # internally, which is duplication we accept rather than re-shape
    # the public API. Most renders process ≤ 9 SOPs so it's still
    # cheap (microseconds, no I/O).
    try:
        readiness = csv_compliance.get_overall_readiness(latest)
        if readiness["ok"]:
            ctx["overall_readiness"] = (
                f"OK — {readiness['current']} of {readiness['total']} "
                f"scheduled SOPs current"
            )
        else:
            ctx["overall_readiness"] = (
                f"ATTENTION — {readiness['overdue']} overdue, "
                f"{readiness['never']} never executed, "
                f"{readiness['due_soon']} due within 7 days"
            )
    except Exception:
        ctx["overall_readiness"] = "(unavailable)"

    # Findings register — read counts from the findings doc itself if
    # we can find the summary line, else fall back to the values frozen
    # in the package at audit-completion time.
    #
    # F-PHD-FINDINGS: the totals row now has SIX numeric columns
    # (pre-audit / phd / smoke / closed / risk / open) since the PhD
    # audit cycle. Parse all of them and compute total = pre+phd+smoke
    # so the [[csv:findings_total]] placeholder reflects the real
    # universe (39, not 32). Falls back to the legacy 4-column shape
    # if the doc is reverted to an older format.
    try:
        findings_md = (_PROJECT_ROOT / "docs" / "csv" / "17_FINDINGS_AND_GAPS.md").read_text(
            encoding="utf-8", errors="ignore",
        )
        m = re.search(r"^\|\s*\*?\*?Total\*?\*?\s*\|(.+?)\|\s*$", findings_md, re.M)
        if m:
            cells = [c.strip() for c in m.group(1).split("|")]
            nums: list[int] = []
            for c in cells:
                bare = re.sub(r"[*\s]", "", c)
                if bare.isdigit():
                    nums.append(int(bare))
            if len(nums) >= 6:
                ctx["findings_total"] = str(nums[0] + nums[1] + nums[2])
                ctx["findings_closed"] = str(nums[3])
                ctx["findings_risk_accepted"] = str(nums[4])
                ctx["findings_open"] = str(nums[5])
            elif len(nums) == 4:
                ctx["findings_total"] = str(nums[0])
                ctx["findings_closed"] = str(nums[1])
                ctx["findings_risk_accepted"] = str(nums[2])
                ctx["findings_open"] = str(nums[3])
    except Exception:
        pass
    ctx.setdefault("findings_total", "(see 17_FINDINGS_AND_GAPS.md)")
    ctx.setdefault("findings_closed", "(see register)")
    ctx.setdefault("findings_open", "(see register)")
    ctx.setdefault("findings_risk_accepted", "(see register)")

    return ctx


# ─── Substitution ────────────────────────────────────────────────────

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
# Inline code spans: CommonMark allows N backticks to open + N to close,
# letting authors embed single backticks inside double-backtick spans.
# The doc 17_FINDINGS_AND_GAPS.md uses ``` `` `[[csv:KEY]]` `` ``` to
# render a literal backtick-wrapped placeholder. We catch both 1- and
# 2-backtick spans with a backreference.
_INLINE_CODE_RE = re.compile(r"(`{1,2})(?:(?!\1).)+?\1", re.DOTALL)


def _substitute_placeholders(text: str, ctx: dict[str, str]) -> str:
    """Replace ``[[csv:KEY]]`` markers with values from ctx.

    The output is plain markdown (NOT raw HTML) — see the rationale in
    the comment above ``_md``. Live values render as ``**bold**`` so
    they stand out visually after markdown rendering converts them to
    ``<strong>``. Unknown keys render as backtick-inline-code so doc
    authors see typos at render time.

    **F-PHD-4 (post-audit fix)**: substitution is SKIPPED inside code
    spans (``` `[[csv:KEY]]` ``) and fenced code blocks (``` ```...```
    ```). Doc authors routinely use ``[[csv:KEY]]`` in prose as a
    literal example explaining the placeholder syntax; before this
    fix, those examples got substituted and the operator saw the
    "unknown key" marker instead of the literal example. We protect
    code regions by replacing them with sentinels, substituting in the
    plain prose, then restoring.

    The text-level substitution is regex-injection-proof: the value is
    placed inside ``**...**`` which markdown_it treats as content (it
    converts to ``<strong>VALUE</strong>``). Any ``**`` characters
    within the value are escaped to ``*`` to avoid breaking the bold
    boundary.
    """
    def _escape_md(s: str) -> str:
        return str(s).replace("**", "*").replace("\\", "\\\\")

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx:
            val = ctx[key]
            if val in ("", None):
                return "—"
            return f"**{_escape_md(val)}**"
        return f"`[[csv:{_escape_md(key)} — unknown]]`"

    # Step 1: stash fenced code blocks first (greedy, so they take
    # precedence over inline code that might appear inside them).
    stash: list[str] = []
    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00CSV_STASH_{len(stash) - 1}\x00"

    text = _FENCED_CODE_RE.sub(_stash, text)
    # Step 2: stash inline code spans (single-backtick `…`).
    text = _INLINE_CODE_RE.sub(_stash, text)

    # Step 3: now run the real substitution on the plain prose.
    text = _PLACEHOLDER_RE.sub(_repl, text)

    # Step 4: restore stashed code regions verbatim.
    def _unstash(m: re.Match) -> str:
        return stash[int(m.group(1))]
    text = re.sub(r"\x00CSV_STASH_(\d+)\x00", _unstash, text)

    return text


# ─── Slug + TOC machinery ────────────────────────────────────────────
#
# Generating slugs ourselves keeps the renderer self-contained (one less
# dependency to lock and audit) and lets us emit identical IDs in both
# the heading element and the TOC anchor — markdown_it's built-in
# anchors plugin is not in the dep set and we don't need its full
# feature surface.

_SLUG_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
# Heading-leading ID pattern — catches ``F-075``, ``F-PHD-1``,
# ``URS-200``, ``SOP-05``, ``F-BR-2``, etc.  When a heading starts with
# one of these, we use just the ID as the slug so anchor links stay
# short and readable (``#f-075`` instead of
# ``#f-075-no-static-analysis-enforcement-of-universal-rbac``).
_ID_PREFIX_RE = re.compile(r"^\s*([A-Z]+(?:-[A-Z0-9]+)+)\b")


def _slugify(text: str) -> str:
    """Convert a heading's text into a kebab-case anchor id.

    Two short-circuits for stability and readability:
      * If the heading STARTS with a structured ID like ``F-075``,
        ``F-PHD-1``, or ``URS-200``, use just that ID as the slug.
        This is critical for the findings register where every H4 is
        a finding card — operators want ``#f-075``, not the full
        title rolled into the URL.
      * Otherwise strip a leading numbering prefix (``"4.1 "``) so a
        renumbered SOP section keeps the same slug.

    Falls back to ``section`` if a heading is empty or punctuation-only.
    """
    # Short-circuit: leading structured ID.
    m = _ID_PREFIX_RE.match(text)
    if m:
        return m.group(1).lower()

    s = text.strip().lower()
    # Drop a leading numbering prefix ("4.", "4.1 ", "4.1.2 ").
    s = re.sub(r"^\s*(\d+(?:\.\d+)*)[\s.\-]*", "", s)
    s = _SLUG_NON_WORD_RE.sub("-", s).strip("-")
    return s or "section"


def _annotate_headings_and_extract_toc(tokens) -> list[dict]:
    """Walk the parsed markdown_it token stream, assign each heading a
    stable id (deduplicated with ``-2`` / ``-3`` suffixes), and return a
    flat TOC list ``[{level, text, id}, ...]`` covering H2 + H3 + H4.

    Why H2 + H3 + H4:
      * H1 is the doc title — already visible at the top of the article,
        repeating it in the TOC adds clutter.
      * H2/H3 covers the typical SOP shape (purpose / cadence / procedure
        with numbered sub-sections).
      * H4 is essential for register-style docs (``17_FINDINGS_AND_GAPS``
        has 26 H4 finding cards; without H4 the TOC has 8 entries and
        navigating to a specific finding requires scrolling through
        ~40k of HTML by eye). Including H4 in the SOP-shaped docs adds
        a few extra entries which is fine — the template indents H4
        deeper than H3 so the visual hierarchy reads clearly.

    The CSS in ``_compliance_doc_styles.html`` caps the TOC sidebar's
    height with ``overflow: auto`` so even a 30-entry list doesn't push
    everything else off the page.
    """
    toc: list[dict] = []
    seen_ids: dict[str, int] = {}
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        level_tag = tok.tag  # "h2", "h3", "h4", …
        # The text content lives in the next token (type="inline").
        text_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        text = (text_tok.content if text_tok is not None else "") or ""
        slug = _slugify(text)
        # Dedupe — markdown allows duplicate heading text, but the
        # browser doesn't allow duplicate ids.
        n = seen_ids.get(slug, 0)
        seen_ids[slug] = n + 1
        unique_id = slug if n == 0 else f"{slug}-{n + 1}"
        tok.attrSet("id", unique_id)
        tok.attrJoin("class", f"doc-heading doc-{level_tag}")
        if level_tag in ("h2", "h3", "h4"):
            toc.append({"level": int(level_tag[1:]), "text": text, "id": unique_id})
    return toc


# ─── Callout-blockquote detection ────────────────────────────────────
#
# Authors routinely write ``> Note: ...`` or ``> Warning: ...`` in SOPs
# and CSV docs. Plain ``<blockquote>`` flattens that into "italic-ish
# grey text", which is a poor signal-to-noise match for what's almost
# always a critical operational caveat. We sniff the first text token
# inside each blockquote for a leading keyword and tag the wrapper.

_CALLOUT_KEYWORDS = {
    "note": "callout-note",
    "info": "callout-note",
    "tip": "callout-tip",
    "important": "callout-important",
    "warning": "callout-warning",
    "caution": "callout-warning",
    "danger": "callout-danger",
}


def _tag_callout_blockquotes(tokens) -> None:
    """Mutate the token stream so any blockquote whose first paragraph
    starts with ``Note:`` / ``Warning:`` / etc. gets an extra CSS class
    on the ``<blockquote>`` open tag."""
    depth = 0
    for i, tok in enumerate(tokens):
        if tok.type != "blockquote_open":
            continue
        # Find the first inline token nested inside this blockquote.
        for j in range(i + 1, len(tokens)):
            inner = tokens[j]
            if inner.type == "blockquote_close":
                break
            if inner.type != "inline" or not inner.content:
                continue
            head = inner.content.strip().lower()
            m = re.match(r"([a-z]+)\s*[:\-]", head)
            if not m:
                break
            cls = _CALLOUT_KEYWORDS.get(m.group(1))
            if cls:
                tok.attrJoin("class", f"callout {cls}")
            break


# ─── Public render API ───────────────────────────────────────────────

# F-PHD-1 (post-Wave-6 audit remediation): see the previous version of
# this comment in git history. Short version — ``html=False`` so any
# stray ``<script>`` in a doc is escaped rather than executed.
#
# Wave-B follow-up: we explicitly enable the ``table`` rule. The CSV
# docs are wall-to-wall pipe-tables (every SOP starts with a metadata
# table; the traceability matrix is a 30-row table). Commonmark mode
# would render those as a single paragraph with literal pipes, which
# was a bug masked by the previous ``html=True`` config (raw HTML
# tables passed through). With ``html=False`` we MUST parse the
# markdown table syntax to get a usable doc view.
_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable("table")
)


def _highlight_code(code: str, lang: str, _attrs) -> str:
    """markdown_it ``highlight`` hook — runs every fenced code block
    through pygments. Returns a ready-to-insert ``<pre>`` (markdown_it
    skips its own wrapper when the highlight callback returns non-empty
    HTML).

    Unknown languages render through the ``TextLexer`` (no
    highlighting) — that's the same fallback the renderer used before,
    so plain ``` fenced blocks keep working.
    """
    try:
        lexer = get_lexer_by_name(lang.strip(), stripall=False) if lang else None
    except ClassNotFound:
        lexer = None
    if lexer is None:
        # Plain pre/code with the language hint kept as a data-attribute
        # for the copy-to-clipboard button in the template.
        safe_lang = html.escape(lang or "")
        safe_code = html.escape(code)
        return (
            f'<pre class="doc-code" data-lang="{safe_lang}">'
            f"<code>{safe_code}</code></pre>"
        )
    # Pygments emits ``<div class="highlight"><pre>...</pre></div>``;
    # we strip the outer div so our own CSS can style ``<pre>`` directly.
    formatter = HtmlFormatter(nowrap=False, cssclass="doc-code")
    inner = highlight(code, lexer, formatter)
    # ``inner`` looks like:  <div class="doc-code"><pre>…</pre></div>
    # Replace the wrapping div with a ``<pre class="doc-code">`` that
    # carries the language hint for the copy-button.
    m = re.search(r"<pre[^>]*>(.*?)</pre>", inner, re.DOTALL)
    body = m.group(1) if m else html.escape(code)
    safe_lang = html.escape(lang or "")
    return f'<pre class="doc-code" data-lang="{safe_lang}"><code>{body}</code></pre>'


_md.options["highlight"] = _highlight_code


def render_doc(doc_relpath: str, db, settings: dict | None) -> dict:
    """Render a SOP or CSV markdown file to HTML with live placeholders.

    ``doc_relpath`` is relative to the repo root (e.g.
    ``docs/SOPs/05_validated_baseline_review.md`` or
    ``docs/csv/17_FINDINGS_AND_GAPS.md``).

    Returns ``{"html": str, "toc": [{"level", "text", "id"}, ...]}``.
    The shape change from "returns a string" → "returns a dict" is the
    Wave-B rendering refresh: the template now wants both the rendered
    body AND a navigation TOC. Existing callers that only need the
    body should pull ``["html"]``.

    Raises FileNotFoundError if the path doesn't exist or is outside
    the repo root (path traversal guard).
    """
    safe_path = _validate_doc_path(doc_relpath)
    raw = safe_path.read_text(encoding="utf-8")
    ctx = build_csv_context(db, settings)
    substituted = _substitute_placeholders(raw, ctx)

    # Parse → annotate → render. The two-step lets us mutate tokens
    # (heading ids + callout classes) before HTML generation.
    env: dict = {}
    tokens = _md.parse(substituted, env)
    toc = _annotate_headings_and_extract_toc(tokens)
    _tag_callout_blockquotes(tokens)
    html_out = _md.renderer.render(tokens, _md.options, env)
    return {"html": html_out, "toc": toc}


def read_raw_doc(doc_relpath: str) -> str:
    """Return the raw markdown source (for the 'view source' tab)."""
    safe_path = _validate_doc_path(doc_relpath)
    return safe_path.read_text(encoding="utf-8")


def _validate_doc_path(doc_relpath: str) -> Path:
    """Resolve to an absolute path under the repo root + verify it
    actually lives under ``docs/``. Path-traversal guard."""
    p = (_PROJECT_ROOT / doc_relpath).resolve()
    # Must be under docs/ to be a valid SOP/CSV doc.
    try:
        rel = p.relative_to(_PROJECT_ROOT / "docs")
    except ValueError:
        raise FileNotFoundError(
            f"refusing to render doc outside docs/: {doc_relpath!r}"
        )
    if not p.exists():
        raise FileNotFoundError(f"doc not found: {doc_relpath!r}")
    if not p.is_file() or p.suffix != ".md":
        raise FileNotFoundError(f"not a markdown file: {doc_relpath!r}")
    return p
