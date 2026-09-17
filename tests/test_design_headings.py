"""The heading ladder -- DESIGN_SYSTEM_SPEC.md Part II §2 and §8.1 (H-1..H-16).

WP-6 step 13. Steps 11-12 already landed the topbar H1 chip (`#page-title`,
`page_title`/`page_icon` blocks, the `_anchor_for` narrowing to `<h2\\b`) and
swept every page's own `<h1>` into those blocks. THIS step touches no
template at all (see DESIGN_SYSTEM_SPEC.md's own step-13 "Risk: None to the
running app") -- it is purely the test suite that will hold the ladder in
place while steps 14-20 (card conversion, which is also the H2 migration --
C25) and 25-26 (moving explanatory prose into tooltips) do the actual
conversion work.

CANONICAL STRINGS. Part II §2.1/§4.2 supersede Part I §3.3 wherever they
disagree (Part IV's own conflict register says so explicitly): H2 here is
`text-base font-bold text-brand ...`, not Part I's `text-lg font-semibold`
with no colour rule. H1 is the one exception written as the FULL, literal,
already-implemented chip string (`templates/base.html`'s `#page-title`, §3.1)
rather than the bare ladder row `text-lg font-bold text-ink flex items-center
gap-2 min-w-0` -- there is exactly one H1 in the whole tree (H-7 pins this),
it is a chip (`h-9 px-3 rounded-md bg-page border border-line`) wrapping the
ladder classes, not a bare heading inside a card, and H-6/H-7/H-8/H-10/H-11
already govern its correctness structurally. Treating the abstract ladder row
as H1's "canonical string" would flag the one, finished, spec-compliant H1 as
permanent, unfixable debt forever -- which is not what a ratchet is for.

RATCHETS vs FLAT ASSERTS. The spec names exactly two ratchets for this file,
H-4 and H-13 (`HEADING_BASELINE` and `HEADING_UNDERTEXT_BASELINE`, following
the exact `LITERAL_BASELINE`/`LITERAL_TOTAL` shape in
tests/test_design_tokens.py: a per-file dict, a `_TOTAL` companion written
out rather than computed, and the three standing companion tests -- never
rises, not left behind, no new file). Both were seeded by RUNNING the
detectors below against this tree, immediately after step 12, never by
copying the spec's own rough expectations (~112 headings / 26 files; 11-18
undertext sites) -- those are a sanity range, not a target. See each
BASELINE dict's own header comment for the real measured numbers and how
they compare to the expectation.

THREE MORE ratchets were added beyond what the spec named for this file --
H-5, H-15 and H-16 -- each only after the flat assert was tried first,
found genuinely failing against the real tree, and traced to a cause
outside this step's own scope (never a template edit; step 13 touches zero
templates). Each is flagged prominently in this step's own report, and each
follows the precedent this house already set for the identical situation:
tests/test_design_roles.py's H2_ICON_BASELINE/H3_ICON_BASELINE, whose own
comment says outright that "card conversion (steps 14-20) hadn't happened
yet".

  * H-15 (`test_no_settings_page_repeats_its_theme_name_as_a_heading`,
    `SETTINGS_THEME_REPEAT_BASELINE`) -- NINE pre-existing violations:
    general/collector/servers/detection/alerts/operations/security/
    compliance/notifications each carry a page-level heading whose text is
    identical to the section's own name, the same "H2 nested under H2" /
    page-level-wrapper shape C9 names and schedules for removal in the
    Settings-family card conversion (step 15) and the D4 nav-dropdown work
    (steps 21-23). The FIRST implementation attempt (against
    search_index.build()'s DEDUPED output) passed with zero violations --
    not because the templates are clean, but because build()'s own dedup
    step structurally discards exactly this shape of entry (see
    _settings_theme_repeats' own docstring). A vacuous pass is a worse
    outcome than a ratchet, so this was rebuilt against the pre-dedup
    extraction before being accepted as a real measurement.
  * H-5 (`test_no_heading_carries_a_top_margin`, `HEADING_TOP_MARGIN_
    BASELINE`) -- FOUR pre-existing violations, all in reports.html, all
    JS-built, all named BY LINE NUMBER in step 16's own brief ("removing
    every mt-3/mt-4").
  * H-16 (`test_no_heading_level_is_skipped`, `HEADING_LEVEL_SKIP_
    BASELINE`) -- ONE pre-existing violation: /topology jumps h1 -> h3
    with no h2 anywhere on the page. topology.html sits in step 18's own
    Files list alongside network.html and scan.html, both of which
    already carry the §2.3 "group label" h2 step 18 describes and
    consequently do not skip -- topology.html is the one sibling that has
    not yet gained one.

WHAT THIS FILE CANNOT SEE:

  * A heading built via `document.createElement('hN')` followed by a
    SEPARATE `.className = '...'` statement, where no `<hN ...>` substring
    exists anywhere in the file. One site today: server_detail.html:1970's
    restart-overlay `<h2>`, styled entirely by a bespoke CSS class
    (`restart-overlay-title`, not a Tailwind utility) rather than the
    ladder -- invisible to both this file's own tag scan AND
    `dt.class_scopes()` (whose script-body pass requires the string to
    carry an actual colour utility; a bare custom class name has none).
    Named here rather than chased: it is a full-screen system takeover, not
    a page heading in the sense this file governs.
  * `[role="dialog"]`-scoped subtrees, which H-16's own spec description
    names as the mechanism for excluding a dialog's title from the page's
    heading outline. `role="dialog"` does not exist ANYWHERE in this tree
    yet (grepped: zero hits) -- it is step 19's job ("every dialog declares
    itself"). H-16 below strips it anyway, for forward compatibility, but
    the mechanism doing the real work today is the §2.6 `data-role`
    carve-out (`dialog-title`/`chrome`/`verdict`), which base.html's modal
    title already carries. See H-16's own comment.
  * Whether a rung a human would call "a heading" was AUTHORED as one.
    `monitoring.html:23`'s `<p class="text-xs text-muted">` doing a
    heading's job (Part I §7.4's own example) is invisible to every test
    here that scans `<h1>`-`<h4>` tags, because it is not one.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt                        # noqa: E402
from tests.test_design_tokens import _code_only, contrast     # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"
BASE = TEMPLATES / "base.html"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
LUCIDE = PROJECT_ROOT / "static" / "vendor" / "lucide-0.344.0.js"


# ══════════════════════════════════════════════════════════════════════════
# Canonical class strings -- DESIGN_SYSTEM_SPEC.md Part II §2.1 / §4.2.
# ══════════════════════════════════════════════════════════════════════════

# The one and only H1 in the tree (base.html's #page-title chip, §3.1) --
# see the module docstring for why this is the literal chip string and not
# the bare ladder row.
H1 = ("inline-flex items-center gap-2 min-w-0 max-w-[14rem] lg:max-w-[22rem] "
      "h-9 px-3 rounded-md bg-page border border-line text-lg font-bold text-ink")
H1_ICON = "w-5 h-5 flex-shrink-0 text-brand"
H2 = "text-base font-bold text-brand flex items-center gap-2 min-w-0"
H2_ICON = "w-5 h-5 flex-shrink-0 text-brand"
H3 = "text-sm font-semibold text-muted flex items-center gap-2 mb-3"
# §2.1: "Without an icon, an H3 drops the <i> AND the flex items-center
# gap-2" -- two legal H3 strings, not one.
H3_NO_ICON = "text-sm font-semibold text-muted mb-3"
H3_ICON = "w-4 h-4 flex-shrink-0 text-muted"
H4 = "text-xs font-semibold uppercase tracking-wide text-faint mb-2"

_CANONICAL_BY_LEVEL: dict[str, set[str]] = {
    "1": {H1}, "2": {H2}, "3": {H3, H3_NO_ICON}, "4": {H4},
}

# §2.6 -- a heading that is not a rung says so by a data-role attribute, so
# the lint keys on an attribute rather than a naming convention that drifts.
# Independent of tests/test_design_roles.py's identically-named set: that
# file's own comment explains why the roles rule and its migrator carry two
# separate copies rather than one shared import -- a carve-out enforced by
# code that shares nothing with what it is checking is worth more than one
# that could drift silently with a shared source.
_HEADING_CARVE_OUTS = {"dialog-title", "chrome", "verdict"}


def _norm(cls: str) -> str:
    return " ".join(cls.split())


# ── the shared heading scanner -- source-level, quote-aware ───────────────
#
# Mirrors tests/test_design_tooltip.py's _TAG_OPEN/_attrs_of: quote-aware so
# a Jinja expression inside an attribute value containing '>' does not end
# the tag match early. Matching the raw (comment-blanked) file TEXT rather
# than only markup is what makes a heading built by JS string concatenation
# visible for free -- reports.html and workflows.html both build several
# h4s as `'<h4 class="...">' + expr + '</h4>'`, and the tag's characters are
# literally present in the file regardless of the JS/Jinja quoting around
# them. Cross-checked against dt.class_scopes() directly in
# test_the_heading_scan_sees_a_js_built_heading below -- the spec's own
# instruction ("JS string bodies included via dt.class_scopes") turned into
# a runnable assertion that both mechanisms agree, rather than a second,
# independent implementation that could silently drift from this one.
_TAG_OPEN = re.compile(r"<([a-zA-Z][\w:-]*)\b((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>", re.S)
_ATTR_VALUE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _attrs_of(attr_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_VALUE.finditer(attr_text):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _headings_in(text: str) -> list[dict]:
    """Every <h1>-<h4> OPEN tag in `text`, as {level, attrs, class, start}.
    `text` should already be comment-blanked (_code_only)."""
    out = []
    for m in _TAG_OPEN.finditer(text):
        tag = m.group(1).lower()
        if tag not in ("h1", "h2", "h3", "h4"):
            continue
        attrs = _attrs_of(m.group(2))
        out.append({
            "level": tag[1],
            "attrs": attrs,
            "class": _norm(attrs.get("class", "")),
            "start": m.start(),
        })
    return out


def _is_carve_out(attrs: dict) -> bool:
    return attrs.get("data-role") in _HEADING_CARVE_OUTS


def test_the_heading_scan_sees_a_js_built_heading():
    """Positive control, and the spec's 'JS string bodies included via
    dt.class_scopes' instruction turned into a runnable cross-check.
    reports.html/workflows.html build several headings by string
    concatenation, not static Jinja -- a scan blind to this would silently
    exempt them from every rule in this file."""
    sample = ('html += \'<h4 class="text-xs font-semibold mt-3 mb-1">\' '
              "+ e(x) + '</h4>';")
    found = _headings_in(sample)
    assert len(found) == 1 and found[0]["level"] == "4", (
        "the heading scan no longer sees a heading built by JS string "
        "concatenation")
    assert found[0]["class"] == "text-xs font-semibold mt-3 mb-1"
    scopes = dt.class_scopes(sample)
    assert any(sample[a:b] == "text-xs font-semibold mt-3 mb-1" for a, b in scopes), (
        "dt.class_scopes() no longer recognises this JS-embedded class list "
        "-- the cross-check the spec's own instruction calls for no longer "
        "holds")


# ── fixtures -- same idiom as tests/test_jump_search.py ───────────────────

@pytest.fixture(scope="module")
def app_obj():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app


@pytest.fixture(scope="module")
def client(app_obj):
    return app_obj.test_client()


def _page_routes() -> list[str]:
    """Same derivation as tests/test_pages_render.py: every GET route from
    the app's own URL map, minus API/static/partial fragments and the auth
    flows that redirect rather than render (login redirects to '/' whenever
    auth is disabled, which is the default under pytest -- see auth.py)."""
    import app as prism_app
    out = []
    for rule in prism_app.app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        path = rule.rule
        if path.startswith(("/api/", "/static/", "/partials/")):
            continue
        if path in ("/logout", "/login", "/setup"):
            continue
        if "<" in path:
            continue
        out.append(path)
    return sorted(set(out))


def _settings_section_routes() -> list[str]:
    from routes.views import _SETTINGS_SECTIONS
    return [f"/settings/{n}" for n in _SETTINGS_SECTIONS]


def _server_detail_route() -> str | None:
    import app as prism_app
    try:
        servers = prism_app.config.get_servers()
    except Exception:
        return None
    return f"/server/{servers[0].name}" if servers else None


@pytest.fixture(scope="module")
def rendered_pages(client) -> dict[str, str]:
    routes = _page_routes() + _settings_section_routes()
    extra = _server_detail_route()
    if extra:
        routes = routes + [extra]
    bodies = {}
    for path in routes:
        r = client.get(path)
        if r.status_code == 200:
            bodies[path] = r.get_data(as_text=True)
    return bodies


# ══════════════════════════════════════════════════════════════════════════
# H-1..H-3, H-9 -- the ladder is a property of the TOKENS, not of markup.
# Flat asserts: these are about tools/design_tokens.TOKENS, which does not
# depend on any template having been converted.
# ══════════════════════════════════════════════════════════════════════════

def test_the_ladder_is_monotone_in_both_themes():
    """H-1. contrast(ink,page) > contrast(brand,card) > contrast(muted,card)
    > contrast(faint,card), in both theme indices, read from dt.TOKENS
    (§2.2: 18.41/8.98/7.58/4.76 light, 18.38/9.81/8.41/5.37 dark). A palette
    move that inverts a rung fails here, not in review."""
    for index_, theme in ((0, "light"), (1, "dark")):
        ink_on_page = contrast(dt.TOKENS["ink"][index_], dt.TOKENS["page"][index_])
        brand_on_card = contrast(dt.TOKENS["brand"][index_], dt.TOKENS["card"][index_])
        muted_on_card = contrast(dt.TOKENS["muted"][index_], dt.TOKENS["card"][index_])
        faint_on_card = contrast(dt.TOKENS["faint"][index_], dt.TOKENS["card"][index_])
        assert ink_on_page > brand_on_card > muted_on_card > faint_on_card, (
            f"{theme}: the ladder is not monotone: ink/page={ink_on_page:.2f} "
            f"brand/card={brand_on_card:.2f} muted/card={muted_on_card:.2f} "
            f"faint/card={faint_on_card:.2f}")


def test_every_rung_clears_aa_on_the_surface_it_sits_on():
    """H-2. H1 (ink) sits on page inside the bar's cut-out; H2/H3/H4
    (brand/muted/faint) sit on card. All >= 4.5 in both themes."""
    for index_, theme in ((0, "light"), (1, "dark")):
        checks = (
            ("H1 ink/page", dt.TOKENS["ink"][index_], dt.TOKENS["page"][index_]),
            ("H2 brand/card", dt.TOKENS["brand"][index_], dt.TOKENS["card"][index_]),
            ("H3 muted/card", dt.TOKENS["muted"][index_], dt.TOKENS["card"][index_]),
            ("H4 faint/card", dt.TOKENS["faint"][index_], dt.TOKENS["card"][index_]),
        )
        for label, fg, bg in checks:
            ratio = contrast(fg, bg)
            assert ratio >= 4.5, f"{theme} {label} is {ratio:.2f}:1, below AA"


def test_h4_is_the_reason_headings_stay_on_the_card():
    """H-3. Positive control: faint fails AA on both raised and page in
    LIGHT mode (§2.3: 4.04 and 4.34 -- both < 4.5; dark mode's 4.95/5.97
    already clear it, which is why every §2.3 citation of this failure in
    the spec is a light-mode number). If the palette ever makes faint safe
    on both surfaces in light mode too, THIS fails, and §2.3's 'H4 sits
    only on card' rule must be relaxed deliberately rather than left
    stale -- not silently made permanently vacuous."""
    on_raised = contrast(dt.TOKENS["faint"][0], dt.TOKENS["raised"][0])
    on_page = contrast(dt.TOKENS["faint"][0], dt.TOKENS["page"][0])
    assert on_raised < 4.5, f"light faint-on-raised is {on_raised:.2f}:1 -- no longer a failure"
    assert on_page < 4.5, f"light faint-on-page is {on_page:.2f}:1 -- no longer a failure"


_SIZE_RE = re.compile(r"\btext-(lg|base|sm|xs)\b")
_WEIGHT_RE = re.compile(r"\bfont-(bold|semibold|medium|normal)\b")


def _size_weight(cls: str) -> tuple[str | None, str | None]:
    size = _SIZE_RE.search(cls)
    weight = _WEIGHT_RE.search(cls)
    return (size.group(1) if size else None, weight.group(1) if weight else None)


def test_the_ladder_is_not_carried_by_colour_alone():
    """H-9. §0 rule 1: colour is never the only signal of heading level.
    With the colour token out of the picture, extracting just (size,
    weight) from each rung's canonical string must still distinguish H2
    from H3 from H4 -- the CVD constraint, as code. §2.5 already documents
    that H2->H3 is carried mostly by size (8.98->7.58 is a weak colour
    step), which is exactly what this guards: deleting weight OR size from
    a rung's constant collapses two levels onto the same non-colour
    signature."""
    h2, h3, h4 = _size_weight(H2), _size_weight(H3), _size_weight(H4)
    assert all(h2), f"H2's canonical string is missing a size or weight token: {H2!r}"
    assert all(h3), f"H3's canonical string is missing a size or weight token: {H3!r}"
    assert all(h4), f"H4's canonical string is missing a size or weight token: {H4!r}"
    assert h2 != h3, f"H2 and H3 share size+weight {h2} -- colour would be the only signal"
    assert h3 != h4, f"H3 and H4 share size+weight {h3} -- colour would be the only signal"
    assert h2 != h4, f"H2 and H4 share size+weight {h2} -- colour would be the only signal"


# ══════════════════════════════════════════════════════════════════════════
# H-4 -- RATCHET. HEADING_BASELINE / HEADING_TOTAL.
# ══════════════════════════════════════════════════════════════════════════

def _heading_violations() -> dict[str, int]:
    """A heading (not a §2.6 carve-out) whose own class string is not one
    of its level's canonical forms."""
    counts: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = 0
        for h in _headings_in(text):
            if _is_carve_out(h["attrs"]):
                continue
            if h["class"] not in _CANONICAL_BY_LEVEL[h["level"]]:
                n += 1
        if n:
            counts[rel] = n
    return counts


# Measured 2026-09-03 by RUNNING _heading_violations() against this tree,
# immediately after WP-6 step 12 (the H1 sweep) -- per C28, never copied
# from the spec's own "~112 headings across 26 files" expectation. The
# total landed EXACTLY on the expectation's number (112); the file count
# (30, not "~26") does not, which is not a sign of a miscounting detector --
# the spec's own file count was always the rougher of its two numbers (a
# hand-estimate rather than something re-derived from a re-run), and 30
# genuine files each holding at least one un-migrated heading is consistent
# with "every H2/H3/H4 in the app is still pre-ladder except base.html's H1"
# (steps 14-20/the card conversion is what converts them, file by file).
HEADING_BASELINE: dict[str, int] = {
    "compliance.html": 2,
    "dashboard.html": 3,
    "monitoring.html": 1,
    "network.html": 1,
    "operations.html": 6,
    "partials/_runbook_form.html": 1,
    "partials/active_actions.html": 1,
    "partials/critical_issues.html": 1,
    "partials/server_comparison.html": 9,
    "partials/server_grid.html": 1,
    "partials/services_table.html": 1,
    "partials/settings/_compliance.html": 1,
    "partials/settings/_dependencies.html": 1,
    "partials/settings/_health_checks.html": 1,
    "partials/settings/_maintenance.html": 2,
    "partials/settings/_rbac.html": 4,
    "partials/settings/_restarts.html": 1,
    "partials/settings/_server_config.html": 4,
    "partials/settings/_servers.html": 1,
    "partials/settings/_tls.html": 1,
    "partials/updates_overview.html": 1,
    "partials/verdict_header.html": 2,
    "reports.html": 13,
    "scan.html": 1,
    "server_detail.html": 17,
    "servers.html": 3,
    "settings.html": 13,
    "setup.html": 1,
    "topology.html": 2,
    "workflows.html": 16,
}

HEADING_TOTAL = 112


def test_every_heading_uses_its_level_s_canonical_classes():
    """H-4. Ratchet. §2.1's canonical strings are the spec; a heading not
    yet converted is expected debt until its file's card-conversion step
    (14-20) or heading-specific step lands, not a build failure today."""
    counts = _heading_violations()
    grew = [f"{f}: {n} (baseline {HEADING_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > HEADING_BASELINE.get(f, 0)]
    assert not grew, (
        "non-canonical heading class string(s) increased -- use the §2.1 "
        "constant for the heading's level:\n  " + "\n  ".join(grew))


def test_the_heading_baseline_is_not_left_behind_when_headings_convert():
    counts = _heading_violations()
    slack = {f: (b, counts.get(f, 0))
             for f, b in HEADING_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER non-canonical headings than the "
        "baseline; lower it:\n  "
        + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_heading_class_violation_outside_the_templates_that_already_have_one():
    new = sorted(set(_heading_violations()) - set(HEADING_BASELINE))
    assert not new, f"new template(s) with a non-canonical heading: {new}"


def test_the_total_number_of_heading_class_violations_never_rises():
    counts = _heading_violations()
    total = sum(counts.values())
    assert total <= HEADING_TOTAL, (
        f"total non-canonical headings rose to {total} (was {HEADING_TOTAL}):\n  "
        + "\n  ".join(f"{f}: {n} (baseline {HEADING_BASELINE.get(f, 0)})"
                      for f, n in sorted(counts.items())
                      if n != HEADING_BASELINE.get(f, 0)))
    assert total == HEADING_TOTAL, (
        f"total fell to {total}; lower HEADING_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# H-5 -- JUDGEMENT CALL: ratchet, not hard zero. See the test's own
# docstring immediately below.
# ══════════════════════════════════════════════════════════════════════════

def _top_margin_violations() -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = sum(1 for h in _headings_in(text)
                if any(w.startswith("mt-") for w in h["class"].split()))
        if n:
            counts[rel] = n
    return counts


# Measured 2026-09-03 by RUNNING _top_margin_violations() against this tree.
# FOUR sites, all in reports.html, all JS-built: the two h4s at (current)
# lines 1278/1291 (`text-xs font-semibold mt-3 mb-1`) and the two h3s at
# 1383/1393 (`text-sm font-semibold mt-4 mb-1[...]`). This is not a
# surprise discovery -- it is the SAME defect §2.1's own prose names by
# example ("reports.html currently has mt-3, mt-4 and nothing at all on
# three siblings that mean the same thing") and step 16 ("Batch C -
# Reports, Monitoring, Operations") explicitly schedules fixing BY LINE
# NUMBER: "Restyle the four JS-built h3s (1232/1373/1384/1394) and two h4s
# (1279/1292), removing every mt-3/mt-4." (the line numbers have drifted by
# a handful since that text was written, which is expected -- the FOUR
# mt-*-bearing headings and the instruction to remove them are exactly
# reproduced by this baseline). Step 13's own file list touches no
# template, so this cannot reach zero here.
HEADING_TOP_MARGIN_BASELINE: dict[str, int] = {
    "reports.html": 4,
}

HEADING_TOP_MARGIN_TOTAL = 4


def test_no_heading_carries_a_top_margin():
    """H-5.

    JUDGEMENT CALL -- flagged prominently per this step's own instructions.
    The spec's own text lists this as a flat, hard-zero assert ("§2.1: 'No
    heading carries a top margin, ever.'"). Measured: NOT zero -- see
    HEADING_TOP_MARGIN_BASELINE's own comment for the exact four sites and
    why fixing them is explicitly step 16's job, by line number, not this
    one's. Converted to a ratchet for that reason, seeded by measurement.

    Vertical separation belongs to the container (the head row's mb-4, or a
    future subblock/subblock(rule=true) wrapper -- C8), never the heading.
    """
    counts = _top_margin_violations()
    grew = {f: (HEADING_TOP_MARGIN_BASELINE.get(f, 0), n)
            for f, n in counts.items() if n > HEADING_TOP_MARGIN_BASELINE.get(f, 0)}
    assert not grew, (
        "heading carrying a top margin, beyond the measured baseline -- "
        "vertical rhythm belongs to the container, not the heading (C8):\n  "
        + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in grew.items()))


def test_the_top_margin_baseline_is_not_left_behind():
    counts = _top_margin_violations()
    slack = {f: (b, counts.get(f, 0))
             for f, b in HEADING_TOP_MARGIN_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER top-margin headings than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_top_margin_heading_outside_the_templates_that_already_have_one():
    new = sorted(set(_top_margin_violations()) - set(HEADING_TOP_MARGIN_BASELINE))
    assert not new, f"new template with a top-margin heading: {new}"


def test_the_total_number_of_top_margin_headings_never_rises():
    counts = _top_margin_violations()
    total = sum(counts.values())
    assert total <= HEADING_TOP_MARGIN_TOTAL, (
        f"total top-margin headings rose to {total} (was {HEADING_TOP_MARGIN_TOTAL})")
    assert total == HEADING_TOP_MARGIN_TOTAL, (
        f"total fell to {total}; lower HEADING_TOP_MARGIN_TOTAL to match, or "
        "the headroom just won is silently available to spend again")


def test_the_top_margin_scan_actually_catches_a_violation():
    """Positive control -- proves the word-boundary check does not also
    match, say, a hypothetical 'mt-auto' or a longer utility by accident,
    and that it DOES fire on the real shape."""
    sample = '<h3 class="text-sm font-semibold text-muted mt-3 mb-3">Title</h3>'
    found = _headings_in(sample)
    assert found and any(w.startswith("mt-") for w in found[0]["class"].split()), (
        "the mt- scan no longer catches a heading with a top margin")


# ══════════════════════════════════════════════════════════════════════════
# H-6, H-7 -- exactly one h1, and it lives only in base.html.
# ══════════════════════════════════════════════════════════════════════════

def _crawlable_and_settings_routes() -> list[str]:
    import search_index
    from routes.views import _SETTINGS_SECTIONS
    return list(search_index._CRAWLABLE) + [f"/settings/{n}" for n in _SETTINGS_SECTIONS]


def test_exactly_one_h1_exists_and_it_is_the_top_bar_one(client):
    """H-6. Every crawlable route (search_index._CRAWLABLE -- the module's
    own named scan list) plus all 11 settings sections, through the test
    client: exactly one <h1>, carrying id="page-title". Deliberately
    excludes login/404/500/setup: D8's page_chrome exemption renders ZERO
    h1 there by design (test_pages_render.py's own
    test_the_chrome_page_exemption_is_exhaustive pins the four-file set),
    and the spec's own H-6 wording is scoped to 'every CRAWLABLE route',
    which is search_index.py's own defined term, not 'every route'."""
    routes = _crawlable_and_settings_routes()
    assert len(routes) == 21, f"expected 10 crawlable + 11 settings = 21 routes, got {len(routes)}"
    violations = []
    for path in routes:
        r = client.get(path)
        if r.status_code != 200:
            violations.append(f"{path}: status {r.status_code}")
            continue
        html = r.get_data(as_text=True)
        h1s = re.findall(r"<h1\b[^>]*>", html)
        if len(h1s) != 1:
            violations.append(f"{path}: {len(h1s)} <h1> element(s), expected 1")
        elif 'id="page-title"' not in h1s[0]:
            violations.append(f"{path}: h1 does not carry id=\"page-title\": {h1s[0]!r}")
    assert not violations, "\n  ".join(violations)


def test_only_base_html_contains_an_h1():
    """H-7. Source-level companion to H-6: catches a reintroduction before
    the page is ever rendered."""
    sites = [p.name for p in sorted(TEMPLATES.rglob("*.html"))
             if p.name != "base.html"
             and re.search(r"<h1\b", _code_only(p.read_text(encoding="utf-8")))]
    assert not sites, f"<h1> found outside base.html: {sites}"


# ══════════════════════════════════════════════════════════════════════════
# H-8 -- every template extending base.html declares page_title.
# ══════════════════════════════════════════════════════════════════════════

_EXTENDS_BASE = re.compile(r'\{%\s*extends\s+"base\.html"\s*%\}')
_PAGE_TITLE_BLOCK = re.compile(r"\{%\s*block\s+page_title\s*%\}(.*?)\{%\s*endblock\s*%\}", re.S)


def test_every_template_extending_base_declares_a_page_title():
    """H-8. All 19 templates with {% extends "base.html" %} define a
    non-empty {% block page_title %} -- true even for the four page_chrome
    pages, whose CHIP is suppressed but whose <title> tag still needs the
    string (base.html:6-13's own comment: 'the STRING is still needed for
    the tab even where the chip itself is suppressed')."""
    extending = 0
    missing = []
    for p in sorted(TEMPLATES.glob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        if not _EXTENDS_BASE.search(text):
            continue
        extending += 1
        m = _PAGE_TITLE_BLOCK.search(text)
        if not m or not m.group(1).strip():
            missing.append(p.name)
    assert extending >= 19, f"only {extending} templates extend base.html -- the scan has drifted"
    assert not missing, f"template(s) extending base.html with no page_title: {missing}"


# ══════════════════════════════════════════════════════════════════════════
# H-10, H-11 -- the suppression rule (§3.2/§3.3).
# ══════════════════════════════════════════════════════════════════════════

_BRAND_ANCHOR = re.compile(r'<a href="([^"]*)"[^>]*>(?:(?!</a>).)*?prism-wordmark', re.S)
_DATA_HOME = re.compile(r"data-home=\"\{\{\s*'true'\s*if\s*request\.path\s*==\s*'([^']*)'")


def test_the_suppression_rule_matches_the_brand_link():
    """H-10. The brand anchor's href and the literal request.path is
    compared against for data-home are the SAME string -- makes 'the page
    the brand mark already names' a rule rather than two hardcoded '/'
    literals that could drift apart."""
    text = _code_only(BASE.read_text(encoding="utf-8"))
    brand = _BRAND_ANCHOR.search(text)
    home = _DATA_HOME.search(text)
    assert brand, "the brand anchor (the prism-wordmark link) was not found"
    assert home, "the h1's data-home computation was not found"
    assert brand.group(1) == home.group(1), (
        f"brand anchor href {brand.group(1)!r} != the path literal "
        f"data-home compares against {home.group(1)!r}")


def test_the_page_title_is_hidden_by_an_id_rule_not_a_utility():
    """H-11. app.css carries the #page-title[data-home="true"] rule and the
    639.98px media-query clip (§3.3); the H1's own class list carries no
    sr-only/not-sr-only -- not-sr-only emits padding:0, which fights px-3
    at equal specificity and resolves on sheet order (memory: measure the
    cascade, not the palette)."""
    css = APP_CSS.read_text(encoding="utf-8")
    assert '#page-title[data-home="true"]' in css, (
        "the id-selector suppression rule is gone from app.css")
    assert "639.98px" in css, "the sub-640px clip media query is gone from app.css"
    h1_text = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"<h1\b[^>]*>", h1_text)
    assert m, "the topbar h1 is gone"
    assert "sr-only" not in m.group(0), (
        f"the h1 carries an sr-only/not-sr-only utility: {m.group(0)!r}")


# ══════════════════════════════════════════════════════════════════════════
# H-12 -- every lucide name in the templates exists in the vendored bundle.
# ══════════════════════════════════════════════════════════════════════════

_ICON_ATTR = re.compile(r'data-lucide="([a-z0-9-]+)"')
_PAGE_ICON_BLOCK = re.compile(r"\{%\s*block\s+page_icon\s*%\}([a-z0-9-]+)\{%\s*endblock\s*%\}")


def _kebab_to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("-"))


def _all_icon_names() -> set[str]:
    names: set[str] = set()
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        names.update(_ICON_ATTR.findall(text))
        # page_icon blocks carry a literal icon name but not inside a
        # data-lucide="..." attribute -- base.html's H1 reads
        # data-lucide="{% block page_icon %}circle-dot{% endblock %}",
        # a Jinja expression, not a literal, so _ICON_ATTR cannot see any
        # page's resolved icon this way (test_design_roles.py hit the same
        # wall for the icon COLOUR and solved it with a dedicated pattern;
        # here the block body itself already IS the literal name).
        names.update(_PAGE_ICON_BLOCK.findall(text))
    return names


# Real runtime resolution, not just a grep for the top-level export.
#
# A first version of this test checked `exports.<PascalName>` and failed on
# `edit-3` (base.html:1425, workflows.html:1157) -- there is genuinely no
# `exports.Edit3` anywhere in this bundle. But the icon is NOT broken: the
# bundle's own `toPascalCase` (line 50, byte-for-byte the same transform as
# `_kebab_to_pascal` above -- verified by hand) feeds `createIcons` from
# `iconAndAliases` (line 17229), a frozen object holding BOTH the primary
# icon set AND lucide's deprecated-name aliases -- `Edit3: PenLine` (line
# 17739) is one such entry. `createIcons`'s own default
# (`icons = iconAndAliases`, line 18745) is what the app's actual runtime
# call uses, so `iconAndAliases` -- not the bare `exports.` table -- is the
# complete, correct set of names that will actually resolve to a rendered
# icon. Checking only `exports.` would have reported a real, working icon
# as broken -- the wrong direction for this test to be wrong in, since a
# false positive here teaches the next person to "fix" something that was
# never failing.
_ICON_ALIAS_ENTRY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*:", re.M)


def _bundle_icon_names(bundle: str) -> set[str]:
    start = bundle.index("var iconAndAliases")
    end = bundle.index("const createIcons", start)
    return set(_ICON_ALIAS_ENTRY.findall(bundle[start:end]))


def test_every_lucide_name_in_the_templates_exists_in_the_bundle():
    """H-12. kebab -> CamelCase -> resolved against the bundle's real
    icon+alias table (see _bundle_icon_names' comment for why that is the
    correct table, not a bare `exports.` grep). A typo'd icon renders as
    nothing at all; this is the only check that sees it."""
    bundle = LUCIDE.read_text(encoding="utf-8")
    available = _bundle_icon_names(bundle)
    assert len(available) >= 1000, (
        f"only {len(available)} name(s) resolved from iconAndAliases -- "
        "the block boundaries or the entry regex have drifted")
    names = _all_icon_names()
    assert len(names) >= 100, (
        f"only {len(names)} distinct icon name(s) found -- "
        "test_design_roles.py measured 139; this scan has drifted")
    missing = [n for n in sorted(names) if _kebab_to_pascal(n) not in available]
    assert not missing, (
        "icon name(s) with no matching entry in the bundle's icon+alias "
        "table -- renders as nothing at all:\n  " + "\n  ".join(missing))


def test_the_icon_bundle_check_would_catch_a_real_typo():
    """Positive control: a name that is not, and has never been, a lucide
    icon must still be rejected -- proving the widened check (bare exports
    -> the full alias table) did not accidentally start accepting
    anything."""
    bundle = LUCIDE.read_text(encoding="utf-8")
    available = _bundle_icon_names(bundle)
    assert "NotArealLucideIconXyz" not in available


def test_the_icon_bundle_scan_can_see_a_digit_in_the_name():
    """Positive control, same shape as test_design_roles.py's own: prove
    'settings-2' (a real, present icon; test_design_roles.py measured 11
    digit-bearing names across 34 sites) both appears in the scan's output
    and converts to an export the bundle actually has."""
    names = _all_icon_names()
    assert "settings-2" in names, "settings-2 (templates/settings.html) is missing from the scan"
    assert _kebab_to_pascal("settings-2") == "Settings2"
    bundle = LUCIDE.read_text(encoding="utf-8")
    assert "exports.Settings2" in bundle


# ══════════════════════════════════════════════════════════════════════════
# H-13 -- RATCHET. HEADING_UNDERTEXT_BASELINE / HEADING_UNDERTEXT_TOTAL.
# D3's structural half: "No title of any level carries a description line
# beneath it." §7.5's now-superseded "Subtitles" idiom (`text-sm text-muted
# mt-1 max-w-2xl` under an h1; `text-xs text-muted mb-4` under an h2 or as a
# card's first child) is exactly this shape -- D3 REVERSES §7.5 rather than
# refining it, so both count.
# ══════════════════════════════════════════════════════════════════════════

# Same "looks like a caption" recipe as tests/test_design_tooltip.py's
# DESC_LINE_BASELINE (a small, muted/faint-coloured element carrying either
# a single Jinja expression or >=5 words of prose) -- deliberately reused
# rather than reinvented, since D3's undertext IS a description line; the
# only difference here is POSITIONAL: anchored to a heading's very next
# sibling, not searched for anywhere on the page.
_UNDERTEXT_TAG = re.compile(
    r"</h[1-4]>\s*<(?P<tag>p|div|span|small|li|dd)\b[^>]*class=\"(?P<cls>[^\"]*)\"[^>]*>"
    r"(?P<body>.*?)</(?P=tag)>", re.S)
_UNDERTEXT_SMALL = r"text-xs|text-sm|text-\[1[01]px\]"
_UNDERTEXT_MUTED = r"text-muted|text-faint"
_UNDERTEXT_SINGLE_JINJA = re.compile(r"^\s*\{\{[^{}]*\}\}\s*$", re.S)
_UNDERTEXT_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def _is_undertext(cls: str, body: str) -> bool:
    if not (re.search(_UNDERTEXT_SMALL, cls) and re.search(_UNDERTEXT_MUTED, cls)):
        return False
    if _UNDERTEXT_SINGLE_JINJA.match(body.strip()):
        return True
    words = _UNDERTEXT_WORD.findall(re.sub(r"<[^>]*>", " ", body))
    return len(words) >= 5


def _undertext_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        if rel == "partials/_empty_state.html":
            continue
        text = _code_only(p.read_text(encoding="utf-8"))
        n = 0
        for m in _UNDERTEXT_TAG.finditer(text):
            if "data-empty-state" in m.group(0):
                continue
            if _is_undertext(m.group("cls"), m.group("body")):
                n += 1
        if n:
            out[rel] = n
    return out


def test_the_undertext_scan_actually_catches_a_violation():
    """Positive control: §7.5's own now-superseded subtitle idiom, the
    exact shape D3 reverses."""
    sample = ('<h2 class="text-lg font-semibold">Title</h2>\n'
              '<p class="text-xs text-muted mb-4">One full sentence '
              'describing scope, with a terminal period.</p>')
    assert _undertext_counts  # exists
    matches = list(_UNDERTEXT_TAG.finditer(sample))
    assert matches and _is_undertext(matches[0].group("cls"), matches[0].group("body")), (
        "the undertext scan no longer catches §7.5's own subtitle idiom")


# Measured 2026-09-03 by RUNNING _undertext_counts() against this tree,
# immediately after WP-6 step 12 -- per C28, never copied from the spec's
# "18 (headings area) / 11 (cards area) -- different detectors" expectation,
# which is a sanity range from two now-superseded, independently-built
# detectors, not a target.
#
# Measured: 7, across 7 files -- below both prior estimates. Checked by
# hand rather than accepted blindly (a low outlier is exactly the shape a
# too-narrow detector produces): a broader "heading, then a small-muted
# element somewhere in the next ~250 characters" scan finds exactly THREE
# more near-misses, and all three are legitimately excluded --
# base.html's is `#ps-tooltip`'s OWN internal `.tip-desc` styling (not a
# page heading's undertext at all), server_detail.html's sits inside a
# wrapper `<div id="recent-failed-logins">` (a real intervening container,
# not "directly under"), and partials/updates_overview.html's is gated by
# `{% if last_checked %}` and reads "Checked: <timestamp>" -- which is
# D3's own STATUS half ("any measured value" stays inline), not the
# explanatory half D3 moves to a tooltip, so excluding it is not just
# defensible but CORRECT. The strict "immediately adjacent" definition
# this file uses is doing real filtering work, not just missing cases.
HEADING_UNDERTEXT_BASELINE: dict[str, int] = {
    "dashboard.html": 1,
    "network.html": 1,
    "partials/active_actions.html": 1,
    "partials/server_comparison.html": 1,
    "partials/services_table.html": 1,
    "scan.html": 1,
    "setup.html": 1,
}

HEADING_UNDERTEXT_TOTAL = 7


def test_no_prose_sits_directly_under_a_heading():
    """H-13. Ratchet. Not driven to 0 here -- steps 25-26 (moving
    explanatory prose into tooltips) are what lower this, per the spec's
    own step-13 "Blocks" line."""
    counts = _undertext_counts()
    grew = [f"{f}: {n} (baseline {HEADING_UNDERTEXT_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > HEADING_UNDERTEXT_BASELINE.get(f, 0)]
    assert not grew, (
        "new prose appeared directly under a heading -- D3 says explanatory "
        "text becomes a tooltip, not a subtitle:\n  " + "\n  ".join(grew))


def test_the_undertext_baseline_is_not_left_behind_when_lines_move_to_tooltips():
    counts = _undertext_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in HEADING_UNDERTEXT_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER undertext sites than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_undertext_outside_the_templates_that_already_have_one():
    new = sorted(set(_undertext_counts()) - set(HEADING_UNDERTEXT_BASELINE))
    assert not new, f"new template(s) with prose directly under a heading: {new}"


def test_the_total_number_of_undertext_sites_never_rises():
    counts = _undertext_counts()
    total = sum(counts.values())
    assert total <= HEADING_UNDERTEXT_TOTAL, (
        f"total undertext sites rose to {total} (was {HEADING_UNDERTEXT_TOTAL}):\n  "
        + "\n  ".join(f"{f}: {n} (baseline {HEADING_UNDERTEXT_BASELINE.get(f, 0)})"
                      for f, n in sorted(counts.items())
                      if n != HEADING_UNDERTEXT_BASELINE.get(f, 0)))
    assert total == HEADING_UNDERTEXT_TOTAL, (
        f"total fell to {total}; lower HEADING_UNDERTEXT_TOTAL to match, or "
        "the headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# H-14 -- flat assert, hard zero.
# ══════════════════════════════════════════════════════════════════════════

def test_a_heading_carries_no_hand_written_tip_attribute():
    """H-14. No data-tip-* written directly on an h1-h4; a heading's tip (if
    it ever needs one -- §5.6/§8's authoring surface, a later step) comes
    from the macro, never hand-written on the heading tag itself. Measured
    zero in this tree today (grepped directly), matching 'hard zero after
    step 12'."""
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        for h in _headings_in(text):
            bad = [k for k in h["attrs"] if k.startswith("data-tip-")]
            if bad:
                violations.append(f"{rel}: <h{h['level']}> carries {bad}")
    assert not violations, (
        "hand-written data-tip-* on a heading -- heading tips come from the "
        "macro:\n  " + "\n  ".join(violations))


def test_the_tip_attribute_scan_actually_catches_a_violation():
    """Positive control: proves this would fire if it were ever untrue."""
    sample = '<h3 class="text-sm font-semibold" data-tip-title="x">Title</h3>'
    found = _headings_in(sample)
    assert found and any(k.startswith("data-tip-") for k in found[0]["attrs"]), (
        "the data-tip-* scan no longer catches a heading carrying one")


# ══════════════════════════════════════════════════════════════════════════
# H-15 -- see the judgement-call comment directly above the ratchet.
# ══════════════════════════════════════════════════════════════════════════

def _settings_theme_repeats(client) -> dict[str, int]:
    """For every settings section, every h1/h2 (search_index._headings --
    the SAME extraction the real jump index uses) whose text equals the
    section's own slug-derived name (`slug.replace('_',' ').title()`,
    search_index.build()'s own pre-dedup computation of a settings page's
    name).

    NOT built from search_index.build()'s own output -- see this file's
    docstring and this rule's own test docstring for why that is a real
    trap here, not a style choice: build()'s dedup step keys on
    (label.lower(), url), and its FIRST loop already adds a `kind:
    "settings"` entry with exactly `{label: slug.title(), url:
    f"/settings/{slug}"}` for every section, before the page-crawl loop
    ever runs. A genuine "heading repeats the theme name" case produces a
    `kind: "heading"` entry with the IDENTICAL (label, url) pair, which the
    dedup step then silently drops in favour of the settings-kind entry
    that got there first -- so testing the deduped index (as an earlier
    draft of this test did) finds exactly ZERO of these, always, by
    construction, regardless of how much redundancy the templates carry."""
    import search_index
    from routes.views import _SETTINGS_SECTIONS
    counts: dict[str, int] = {}
    for slug in _SETTINGS_SECTIONS:
        r = client.get(f"/settings/{slug}")
        if r.status_code != 200:
            continue
        html = r.get_data(as_text=True)
        page_name = slug.replace("_", " ").title()
        for _lvl, text in search_index._headings(html):
            if text.strip().casefold() == page_name.casefold():
                counts[slug] = counts.get(slug, 0) + 1
    return counts


def test_no_settings_page_repeats_its_theme_name_as_a_heading(client):
    """H-15.

    JUDGEMENT CALL -- flagged prominently per this step's own instructions.

    The spec's own text lists this as a flat, hard-zero assert
    ("consequence of C9 -- the theme name is the chip's job and the index
    label's job, not a heading's"). Implemented against the real mechanism
    C9 names -- search_index._headings(), the SAME extraction the real jump
    index runs (see _settings_theme_repeats' own docstring for why the
    DEDUPED index itself is the wrong thing to test against): for every
    settings section, does any heading's text equal the section's own
    slug-derived name?

    Measured against this tree today: NOT zero. NINE of the eleven
    sections fail, one violation each -- general, collector, servers
    (via a NESTED include: `_servers.html`'s own heading is "Tag
    Management", but it also `{% include %}`s `_server_config.html`,
    whose own h2 says "Servers" verbatim), detection, alerts, operations,
    security, compliance, notifications. Only rbac (headings: "Permissions",
    "My permissions", "Grant access", "Current ACLs", "Pending approvals
    (tier-0)") and display (heading: "Display Preferences") do not trip it.

    This is not new debt introduced by anything in WP-6; it is C9's own
    named example -- literally the same page-level-wrapper-H2 shape as the
    "Permissions" heading at settings.html:597 (`_rbac.html`'s four h2s
    nested under it -- the "H2 nested under H2" defect C9 exists to name),
    except in general/collector/detection/alerts/operations/security/
    compliance/notifications the wrapper heading's text happens to be
    IDENTICAL to the section name rather than a different word like
    "Permissions". C9 schedules removing this shape for the Settings-family
    CARD CONVERSION (step 15) and the D4 nav-DROPDOWN work (steps 21-23),
    neither of which has happened yet -- step 13's own file list touches no
    template.

    So: converted to a ratchet, seeded by measurement, exactly like the
    precedent this house already set for the same reason
    (tests/test_design_roles.py's H2_ICON_BASELINE/H3_ICON_BASELINE, whose
    own comment says outright that "card conversion (steps 14-20) hadn't
    happened yet"). This is the ONE test in this file where the flat assert
    was tried first (twice -- the first attempt against the deduped index
    passed VACUOUSLY, for the structural reason above, which is a worse
    outcome than a ratchet: a test that cannot fail is not a test) and
    found genuinely unsatisfiable for an out-of-scope reason -- not a
    default reached without trying.
    """
    counts = _settings_theme_repeats(client)
    grew = {s: (SETTINGS_THEME_REPEAT_BASELINE.get(s, 0), n)
            for s, n in counts.items() if n > SETTINGS_THEME_REPEAT_BASELINE.get(s, 0)}
    assert not grew, (
        "a settings section repeats its own name as a heading, beyond the "
        "measured baseline -- see this test's own docstring:\n  "
        + "\n  ".join(f"{s}: {b} -> {n}" for s, (b, n) in grew.items()))


def test_the_settings_theme_repeat_scan_is_not_vacuous():
    """Positive control -- the exact failure mode this ratchet's own history
    warns about (see test_no_settings_page_repeats_its_theme_name_as_a_
    heading's docstring): a detector that always finds zero is
    indistinguishable from a broken one without this. Confirms the real,
    measured floor (>= 9) rather than merely ">= 1", so a future change
    that silently drops most of the sections is still caught here even if
    it happens to leave one violation standing."""
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    counts = _settings_theme_repeats(prism_app.app.test_client())
    assert sum(counts.values()) >= 9, (
        f"only {sum(counts.values())} settings theme-repeat(s) found across "
        f"{counts} -- measured 9 across 9 sections; the scan may have "
        "regressed to the deduped-index trap this test exists to catch")


# Measured 2026-09-03 by RUNNING _settings_theme_repeats() against this
# tree. Keyed by settings SLUG rather than by template file -- every one of
# these lives in settings.html today (or a partial it includes), so a
# per-file key would collapse most of them into one entry and hide which
# sections still need it. See the test's own docstring for the full
# reasoning on why this is a ratchet at all.
SETTINGS_THEME_REPEAT_BASELINE: dict[str, int] = {
    "general": 1,
    "collector": 1,
    "servers": 1,
    "detection": 1,
    "alerts": 1,
    "operations": 1,
    "security": 1,
    "compliance": 1,
    "notifications": 1,
}

SETTINGS_THEME_REPEAT_TOTAL = 9


def test_the_settings_theme_repeat_baseline_is_not_left_behind(client):
    """Companion to H-15's ratchet -- same shape as every other baseline in
    this house: if the real count has dropped (steps 15/21-23 removing a
    wrapper heading), the baseline must come down with it."""
    counts = _settings_theme_repeats(client)
    slack = {s: (b, counts.get(s, 0))
             for s, b in SETTINGS_THEME_REPEAT_BASELINE.items() if counts.get(s, 0) < b}
    assert not slack, (
        "these settings section(s) now repeat their name as a heading FEWER "
        "times than the baseline; lower it:\n  "
        + "\n  ".join(f"{s}: {b} -> {n}" for s, (b, n) in slack.items()))


def test_no_settings_theme_repeat_outside_the_sections_that_already_have_one(client):
    counts = _settings_theme_repeats(client)
    new = sorted(set(counts) - set(SETTINGS_THEME_REPEAT_BASELINE))
    assert not new, f"new settings section repeating its own name: {new}"


def test_the_total_settings_theme_repeats_never_rises(client):
    counts = _settings_theme_repeats(client)
    total = sum(counts.values())
    assert total <= SETTINGS_THEME_REPEAT_TOTAL, (
        f"total settings theme-name repeats rose to {total} "
        f"(was {SETTINGS_THEME_REPEAT_TOTAL})")
    assert total == SETTINGS_THEME_REPEAT_TOTAL, (
        f"total fell to {total}; lower SETTINGS_THEME_REPEAT_TOTAL to match, "
        "or the headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# H-16 -- JUDGEMENT CALL: ratchet, not hard zero. See the test's own
# docstring immediately below.
# ══════════════════════════════════════════════════════════════════════════

def _heading_level_skips(rendered_pages: dict[str, str]) -> dict[str, int]:
    """{route: skip-count}. See test_no_heading_level_is_skipped's own
    docstring for the [role="dialog"]-strip and carve-out reasoning."""
    out: dict[str, int] = {}
    for path, html in rendered_pages.items():
        text = _code_only(html)
        # Forward-compatible with step 19; a no-op today (see docstring).
        text = re.sub(r'<[a-zA-Z][\w:-]*\b[^>]*\brole="dialog"[^>]*>.*?</[a-zA-Z][\w:-]*>',
                      " ", text, flags=re.S)
        levels = [int(h["level"]) for h in _headings_in(text) if not _is_carve_out(h["attrs"])]
        n = sum(1 for prev, cur in zip(levels, levels[1:]) if cur - prev > 1)
        if n:
            out[path] = n
    return out


# Measured 2026-09-03 by RUNNING _heading_level_skips() against every route
# this file's rendered_pages fixture reaches. ONE violation: /topology jumps
# h1 -> h3 (templates/topology.html:115/132's "Legend" and node-details
# panels are both h3, and the page has no h2 anywhere -- grepped directly).
# Out of scope for step 13 for the same reason as H-5's ratchet below:
# topology.html is explicitly in step 18's ("Batch E - fleet and overview
# pages") own Files list, the step whose Files list also names
# network.html and scan.html -- both of which ALREADY carry the §2.3
# "group label" h2 step 18 describes (grepped: network.html:54,
# scan.html:42, both `text-sm font-semibold text-muted uppercase
# tracking-wider`) and consequently do NOT skip. topology.html is the one
# sibling page in that batch that has not yet gained its own h2, which is
# the single, precise, out-of-scope reason this fails today.
HEADING_LEVEL_SKIP_BASELINE: dict[str, int] = {
    "/topology": 1,
}

HEADING_LEVEL_SKIP_TOTAL = 1


def test_no_heading_level_is_skipped(rendered_pages):
    """H-16.

    JUDGEMENT CALL -- flagged prominently per this step's own instructions.
    The spec's own text lists this as a flat, hard-zero assert. Measured:
    NOT zero -- /topology jumps h1 -> h3 with no h2 in between (see
    HEADING_LEVEL_SKIP_BASELINE's own comment for the full account and why
    it is step 18's job, not this one's). Converted to a ratchet for that
    reason, seeded by measurement.

    In document order per rendered page, no jump greater than +1.
    [role="dialog"] subtrees are stripped per the spec's own description --
    a NO-OP today (grepped: role="dialog" exists nowhere in this tree yet;
    it is step 19's job, "every dialog declares itself"). What actually
    keeps this test honest today is excluding any heading carrying a §2.6
    data-role carve-out (dialog-title/chrome/verdict) from the sequence --
    which is precisely what stops base.html's ALWAYS-PRESENT
    #prism-modal-title <h3 data-role="dialog-title"> (emitted on every
    page, unconditionally, regardless of page_chrome) from corrupting the
    outline on login.html/404.html/500.html: those three pages carry NO
    heading of their own at all (D8 suppresses even the h1), so without the
    carve-out exclusion the modal's h3 would be the page's ONLY heading --
    an h1-less jump straight to h3. See
    test_a_carve_out_heading_does_not_count_as_a_level_jump below for a
    direct, synthetic proof of exactly this scenario (the live routes
    cannot exercise it: GET /login redirects to '/' whenever auth is
    disabled, which is the default under pytest, so login.html never
    actually renders here)."""
    counts = _heading_level_skips(rendered_pages)
    grew = {p: (HEADING_LEVEL_SKIP_BASELINE.get(p, 0), n)
            for p, n in counts.items() if n > HEADING_LEVEL_SKIP_BASELINE.get(p, 0)}
    assert not grew, (
        "a heading level is skipped, beyond the measured baseline:\n  "
        + "\n  ".join(f"{p}: {b} -> {n}" for p, (b, n) in grew.items()))


def test_the_heading_level_skip_baseline_is_not_left_behind(rendered_pages):
    counts = _heading_level_skips(rendered_pages)
    slack = {p: (b, counts.get(p, 0))
             for p, b in HEADING_LEVEL_SKIP_BASELINE.items() if counts.get(p, 0) < b}
    assert not slack, (
        "these route(s) now skip FEWER heading levels than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{p}: {b} -> {n}" for p, (b, n) in slack.items()))


def test_no_heading_level_skip_outside_the_routes_that_already_have_one(rendered_pages):
    new = sorted(set(_heading_level_skips(rendered_pages)) - set(HEADING_LEVEL_SKIP_BASELINE))
    assert not new, f"new route with a skipped heading level: {new}"


def test_the_total_number_of_heading_level_skips_never_rises(rendered_pages):
    counts = _heading_level_skips(rendered_pages)
    total = sum(counts.values())
    assert total <= HEADING_LEVEL_SKIP_TOTAL, (
        f"total heading level skips rose to {total} (was {HEADING_LEVEL_SKIP_TOTAL})")
    assert total == HEADING_LEVEL_SKIP_TOTAL, (
        f"total fell to {total}; lower HEADING_LEVEL_SKIP_TOTAL to match, or "
        "the headroom just won is silently available to spend again")


def test_a_carve_out_heading_does_not_count_as_a_level_jump():
    """Positive control for H-16's carve-out handling, covering the one
    live shape the real routes cannot reach (see the test above): a page
    with NO heading but the modal's h3."""
    only_modal = '<h3 data-role="dialog-title" class="text-base font-bold text-ink">x</h3>'
    levels = [int(h["level"]) for h in _headings_in(only_modal) if not _is_carve_out(h["attrs"])]
    assert levels == [], "a lone carve-out heading should not appear in the sequence at all"

    # Without the carve-out marker, the SAME lone h3 (no h1/h2 before it)
    # would be exactly this kind of violation -- proving the exclusion
    # above is load-bearing, not just accidentally never triggered.
    without_carve_out = '<h3 class="text-base font-bold text-ink">x</h3>'
    bare_levels = [int(h["level"]) for h in _headings_in(without_carve_out)
                   if not _is_carve_out(h["attrs"])]
    assert bare_levels == [3], (
        "the carve-out scan is filtering out an h3 that does not even "
        "carry the data-role attribute -- it is over-matching")
    # A real page-outline check (H-16's own loop) starting from an empty
    # "headings seen so far" list and landing directly on h3 would report
    # nothing wrong by itself (there is no PRIOR heading to jump FROM) --
    # the actual protection is that base.html's h1 chip is what normally
    # supplies that prior heading, and h1 -> h3 (skipping h2) IS caught:
    combined = ['1', '3']
    jumps = [int(b) - int(a) for a, b in zip(combined, combined[1:])]
    assert any(j > 1 for j in jumps), "h1 -> h3 must read as a skip of +2"
