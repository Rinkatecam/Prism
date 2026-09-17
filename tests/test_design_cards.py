"""The card -- DESIGN_SYSTEM_SPEC.md Part II §4 and §8.2 (C-1..C-11).

WP-6 step 14. Creates templates/partials/_card.html (the card()/subblock()/
subhead() macros and the §4.2 constants) and this test suite. Per the
step's own text: "Convert ZERO call sites -- the suite must be green at the
end of this step." Nothing in templates/ (other than _card.html itself) is
touched by this step -- steps 15-20 are the batch-by-batch conversions,
each of which is also its file's H2 migration (C25).

RATCHETS vs FLAT ASSERTS -- and FIVE judgement calls, flagged prominently
per this step's own instructions (the same way test_design_headings.py's
own module docstring flagged its three extra ratchets one step prior):

The spec names exactly TWO ratchets for this file in its own registry
(§9): `CARD_EXCEPTIONS`+`CARD_EXCEPTION_TOTAL` (C-2) and
`SECTION_HEADER_BASELINE` (C-11). Both were seeded by RUNNING their
detectors against this tree as it stands after step 13, never by copying
the spec's own rough expectations (131 sites / 57 strings / 19 rows) --
see each dict's own header comment for the measured numbers, the
investigation done, and how they compare.

THREE MORE ratchets were added beyond what the spec named for this file,
following the exact precedent test_design_headings.py set one step ago
(its own H-5/H-15/H-16): a flat, hard-zero assert was tried first for each,
found genuinely failing against the real (pre-step-14, pre-conversion)
tree, traced to a cause explicitly OUT OF SCOPE for this step (a template
edit, which this step's own text forbids -- "Convert ZERO call sites"),
and converted to a ratchet seeded by measurement instead:

  * C-5's second clause (`test_the_only_shadow_names_used_are_the_ones_
    the_config_maps`, `SHADOW_NAME_BASELINE`) -- 21 real sites across 10
    files use a banned shadow-(xl|2xl|sm|inner|none) utility TODAY
    (measured by running the detector; cross-checked by hand against
    `grep -rEn 'class="[^"]*\\bshadow-(xl|2xl|sm|inner|none)\\b' templates/`,
    which independently returns the same 21 lines). Every one of them is a
    MODAL PANEL (`shadow-xl`/`shadow-2xl`, 19 sites) or a plain page card
    (`shadow-sm`, login.html and setup.html, 2 sites) -- exactly the debt
    §4.6 names by name ("All 12 modal panels currently lose their edge in
    dark mode; this is a real defect the cleanup fixes") and schedules for
    step 19 ("Batch F - overlays collapse onto shadow-lg"). The spec's own
    text states this half of C-5 as a flat assert with no ratchet in its
    §9 registry; measured reality disagrees, and fixing 21 sites across 10
    files by editing modal markup is squarely a template conversion this
    step's own text forbids ("Convert ZERO call sites").
  * C-6 (`test_no_card_in_page_flow_carries_a_static_shadow`,
    `STATIC_SHADOW_BASELINE`) -- 2 real sites (login.html:13, setup.html:16,
    both `bg-card rounded-lg border border-line p-6 shadow-sm`, a plain
    page card with no overlay ancestor). Every OTHER shadow-bearing
    card-shaped element in the tree (all 19 modal panels above) DOES sit
    inside a `fixed`/`absolute` + z-bearing ancestor within a short
    backward scan -- checked by hand for a representative sample (the two
    operations.html `data-action-modal` panels, whose OWN class carries
    only `relative`, with `fixed inset-0 z-[70]` two levels up) -- so the
    detector is not simply failing to see overlay context; these two are
    genuinely different in kind, and genuinely a two-site debt, not a
    detector bug.
  * C-9's second clause (`test_the_lowest_step_is_the_reason_an_inset_
    surface_is_forbidden`, `TILE_FAINT_BASELINE`) -- 2 real sites,
    settings.html:453 and :535, both a `bg-page rounded-lg p-3 border
    border-line` box (the pre-tile "nested box" idiom, Part I §7.7) with a
    `text-faint` descendant. §4.4's own rule is WHY these two are already
    latent contrast failures (faint-on-page is 4.34:1 light, faint-on-
    raised 4.04:1 -- both < 4.5) waiting for the day someone converts this
    box to the pinned TILE string, which is exactly the day this ratchet
    starts mattering.

Each of the three above is a two-, two-, and twenty-one-site ratchet
respectively -- small next to CARD_EXCEPTIONS' 133, but each is a real
measured fact about the tree today, not a rounding choice, and each is
called out by name in this docstring for the same reason step 13's were:
so a reviewer sees the reasoning rather than a silently-widened test.

The contrast HALF of C-9 (pin contrast(faint, raised) in 4.03-4.05 and
< 4.5) stays a flat assert -- it is a property of `tools.design_tokens.
TOKENS`, unaffected by which templates have converted, exactly like
test_design_headings.py's H-1/H-2/H-3/H-9. C-4 (a raw CARD/CARD_FLUSH
literal that already has a heading inside it) measures ZERO against the
real tree today and is also a flat assert with no ratchet: nothing in this
tree currently hand-types the exact canonical string AND puts a heading
inside it. C-1, C-3, C-7, C-8 and C-10 govern the MACRO itself and
`{% call card( %}` call sites, of which there are exactly zero after this
step by design -- each is necessarily vacuous against the real tree today
(the same honest situation test_design_headings.py's H-16 documented for
`role="dialog"`, which also does not exist anywhere in this tree yet) and
is proven instead by a positive control rendering or scanning a SYNTHETIC
example, per this house's standing convention that every regex/detector
carries proof it can actually fire.

TWO CONSTANTS beyond §4.2's own eighteen were added to _card.html, both
documented in that file's own header comment and repeated here:
`H3_NO_ICON` (§2.1's "without an icon, an H3 drops the <i> AND `flex
items-center gap-2`" -- test_design_headings.py already carries the
identical string under the identical name) and `DIALOG_TITLE` (§2.6's
carve-out table, needed to implement §4.3's `level` parameter -- there is
no other place in Part II this literal string is pinned).

`TIP_BTN` is declared in _card.html exactly as §4.2 lists it, but is NOT
wired into card()/subhead()'s rendered output -- flagged prominently here
and in this step's own report. _card.html's tip button/mirror come
entirely from partials/_tip.html's tip_button()/tip_mirror() (per §4.3's
own instruction: "it imports tip() from partials/_tip.html rather than
defining its own"), whose ACTUAL button class
(`ps-tip inline-flex items-center justify-center w-8 h-8 -m-2 text-muted
hover:text-accent`) differs from TIP_BTN's string in exactly the ways
_tip.html's own header comment argues for BY NAME: no `rounded` (a
cascade-order argument: Tailwind's sheet loads after _tip.html's inline
`<style>`, so a `rounded-*` utility would silently overwrite the
`border-radius: 0.35rem` `[data-tip-title]` already sets), no
`transition-colors` (same cascade-order argument, for `transition:
box-shadow`), and no `focus-visible:text-accent`/`flex-shrink-0` (not
argued against explicitly, simply not part of the shipped, tested,
already-adopted implementation). Re-deriving a SECOND tip-button class
string in this file and asserting _card.html matches IT would just be
grading _card.html against the wrong answer; the right one is the
already-shipped, already-tested _tip.html mechanism, which is what
card()/subhead() actually call. TIP_BTN is kept as a real, exact,
spec-literal `{% set %}` in _card.html (this step's own instructions:
copy §4.2's constants exactly) and is simply unreferenced -- Jinja does
not warn about an unused `{% set %}`, so this costs nothing at runtime and
is called out here so it is a documented decision, not a silent one.

HOUSE STYLE reused directly, not reinvented, from the three files this
step's own instructions named: `_code_only`/`contrast` (imported from
tests.test_design_tokens), `dt.class_scopes()` (tools.design_tokens, so a
JS-built class string is in scope exactly as C-4b names), the
`app_obj`/`client` module-scoped pytest fixtures and `_page_routes`/
`_settings_section_routes`/`_server_detail_route` route helpers (imported
from tests.test_design_headings rather than re-implemented -- that file
already imports `_code_only`/`contrast` from test_design_tokens.py under
the identical "reuse, do not reinvent" rationale), and the
LITERAL_BASELINE/LITERAL_TOTAL four-part ratchet shape (grew / baseline-
not-left-behind / no-new-file / total-never-rises) for every ratchet below.

WHAT THIS FILE CANNOT SEE:

  * Anything about a `{% call card(...) %}` site's REAL-WORLD correctness
    beyond what a regex over Jinja source or a rendered-HTML string can
    see -- there are none in this tree yet, so every such check here is
    proven only against synthetic examples. Steps 15-20 are what will
    finally exercise C-3/C-7/C-8's static halves against real markup.
  * A card assembled ENTIRELY by client-side JavaScript string
    concatenation and injected via `innerHTML` after the initial response
    (server_detail.html's log-viewer builders, e.g. the `bg-card` shell at
    (current) line 1460 and the `bg-card rounded p-3` message box nested
    inside it at line 1499 -- a real, pre-existing bg-card-in-bg-card
    instance, but one that never appears in ANY server-rendered HTML the
    test client can request, because it is built by a `let html = '...'`
    JS function that only runs in a browser after a button click). C-8's
    rendered-DOM half is widened to include
    `/partials/server-analytics/<name>` specifically because THAT nesting
    (partials/server_analytics.html:31 containing :96) *is* real,
    server-rendered, static Jinja markup -- see that test's own comment --
    but the JS-built ones above remain permanently outside what a
    test-client-based scan can ever see, by the nature of the mechanism.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import jinja2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt                                  # noqa: E402
from tests.test_design_tokens import _code_only, contrast              # noqa: E402
from tests.test_design_headings import (                               # noqa: E402
    _page_routes, _settings_section_routes, _server_detail_route,
)

TEMPLATES = PROJECT_ROOT / "templates"
CARD_HTML = TEMPLATES / "partials" / "_card.html"
BASE = TEMPLATES / "base.html"


# ══════════════════════════════════════════════════════════════════════════
# Canonical strings -- DESIGN_SYSTEM_SPEC.md Part II §4.1/§4.2. Independent
# copies maintained in THIS file, the same convention test_design_headings.py
# uses for H1..H4: C-1 compares a RENDERED macro's output against a literal
# living here, which is what makes it a real test rather than "the constant
# equals itself" (this file's own docstring, and C-1's own spec wording,
# both name that exact tautology by name).
# ══════════════════════════════════════════════════════════════════════════

CARD = "bg-card rounded-lg border border-line p-5"
CARD_FLUSH = "bg-card rounded-lg border border-line overflow-hidden"
HEAD = "flex flex-wrap items-start justify-between gap-3 mb-4"
HEAD_FLUSH = "flex flex-wrap items-start justify-between gap-3 px-5 py-4 border-b border-line"
H2 = "text-base font-bold text-brand flex items-center gap-2 min-w-0"
H2_ICON = "w-5 h-5 flex-shrink-0 text-brand"
CONTROLS = "flex items-center gap-2 flex-shrink-0"
SUBBLOCK = "mt-5 first:mt-0"
SUBBLOCK_RULE = "mt-5 pt-5 border-t border-line first:mt-0 first:pt-0 first:border-t-0"
H3 = "text-sm font-semibold text-muted flex items-center gap-2 mb-3"
H3_ICON = "w-4 h-4 flex-shrink-0 text-muted"
H4 = "text-xs font-semibold uppercase tracking-wide text-faint mb-2"
FOOTER = "mt-5 pt-4 border-t border-line flex items-center justify-end gap-2"
FOOTER_FLUSH = "px-5 py-4 border-t border-line flex items-center justify-end gap-2"
TILE = "bg-raised rounded border border-line p-3"
TILE_ON_PAGE = "bg-card rounded border border-line p-3"
TILE_LABEL = "text-xs font-medium text-muted"
TIP_BTN = ("ps-tip inline-flex items-center justify-center align-middle w-8 h-8 -m-2 "
           "rounded text-muted hover:text-accent focus-visible:text-accent flex-shrink-0 "
           "transition-colors duration-fast ease-standard")

# Not one of §4.2's eighteen -- see the module docstring's "TWO CONSTANTS"
# paragraph. §2.6's carve-out table.
DIALOG_TITLE = "text-base font-bold text-ink flex items-center gap-2"

# §4.5 -- the five things that are not cards.
NOT_CARD_IDIOMS = {
    "code_shell": "bg-raised rounded border border-line overflow-hidden",
    "notice": "rounded border border-line bg-raised p-4 flex items-start gap-3",
    "doorway": "card-clickable bg-card rounded-lg border border-line p-5 flex items-start gap-3",
    "menu": "bg-card rounded border border-line shadow-lg overflow-hidden py-1",
    "doc_surface": "bg-card rounded-lg border border-line p-6 lg:p-8",
}
_NOTICE_SUFFIXES = ("border-dashed", "border-warning", "border-critical")
_DOORWAY_SUFFIXES = ("border-dashed",)
_ALLOWED_EXACT = {CARD, CARD_FLUSH, TILE, TILE_ON_PAGE} | set(NOT_CARD_IDIOMS.values())


def _norm(cls: str) -> str:
    return " ".join(cls.split())


def _is_allowed_card_string(cls: str) -> bool:
    """C-2's allowlist: exactly one of the card/tile constants, or one of
    the five §4.5 idioms (optionally + one named status/dashed border
    utility -- §4.5's own "(+ border-dashed / border-warning /
    border-critical)" annotation on notice/doorway)."""
    n = _norm(cls)
    if n in _ALLOWED_EXACT:
        return True
    words = n.split()
    for base, suffixes in ((NOT_CARD_IDIOMS["notice"], _NOTICE_SUFFIXES),
                            (NOT_CARD_IDIOMS["doorway"], _DOORWAY_SUFFIXES)):
        base_words = base.split()
        for suf in suffixes:
            for sv in (suf, suf + "/30", suf + "/40", suf + "/50"):
                if sorted(words) == sorted(base_words + [sv]):
                    return True
    return False


# ── the shared "is this card-shaped" detector -- C-2's own literal recipe:
# "anything matching bg-(card|raised|page) + rounded + (p-\\d or
# overflow-hidden)". (?<!:) on each half excludes a Tailwind VARIANT
# (hover:bg-raised, dark:hover:bg-line) -- a variant-prefixed utility only
# paints on interaction/theme, and several real icon-button treatments in
# this tree (`p-1 rounded hover:bg-raised dark:hover:bg-line`) would
# otherwise false-positive as card-shaped purely because a variant and its
# base utility share the same token spelling. `inline-flex` is excluded
# for the same reason a card is never inline: it is what a small
# segmented-toggle/pill-group control (`inline-flex rounded border
# border-line p-0.5 bg-page`, two real sites: server_detail.html's
# heatmap week-selector, settings.html's LDAP-picker mode toggle) uses to
# coincidentally satisfy bg-(page)+rounded+p-\\d without being remotely a
# card. Both exclusions were found by hand while seeding CARD_EXCEPTIONS
# below -- see that dict's own header comment. ────────────────────────────
_BG = re.compile(r"(?<!:)\bbg-(card|raised|page)\b")
_ROUNDED = re.compile(
    r"(?<!:)\brounded(?:-(?:t|r|b|l|tl|tr|bl|br))?(?:-(?:none|sm|md|lg|xl|2xl|3xl|full))?\b")
_PAD = re.compile(r"(?<!:)\bp-\d+\b")
_OVERFLOW_HIDDEN = re.compile(r"(?<!:)\boverflow-hidden\b")


def _is_card_shaped(cls: str) -> bool:
    if "inline-flex" in cls.split():
        return False
    return bool(_BG.search(cls) and _ROUNDED.search(cls)
                and (_PAD.search(cls) or _OVERFLOW_HIDDEN.search(cls)))


def _is_panel_shaped(cls: str) -> bool:
    """Broader than _is_card_shaped: bg-(card|raised|page) + rounded,
    WITHOUT requiring padding/overflow-hidden. Used only to detect "is
    there an enclosing box here at all" (C-11's backward guard, C-9's tile
    scan) -- a flush modal panel (`bg-card rounded-lg shadow-xl border
    border-line ... flex flex-col relative`, no p-\\d anywhere on the outer
    shell because padding sits on its inner header/body/footer zones
    instead, e.g. settings.html's #ldap-picker-modal) is exactly the shape
    _is_card_shaped is CORRECTLY blind to under C-2's own literal detector
    recipe, but must not also be invisible to "is this heading already
    inside some box" -- settings.html:982's LDAP-picker dialog <h3> was
    the concrete case that exposed this gap while seeding
    SECTION_HEADER_BASELINE below."""
    if "inline-flex" in cls.split():
        return False
    return bool(_BG.search(cls) and _ROUNDED.search(cls))


def _card_shaped_spans(text: str) -> list[tuple[int, int, str]]:
    """Every card-shaped class list, markup AND JavaScript --
    dt.class_scopes() covers both (C-4b)."""
    return [(a, b, text[a:b]) for a, b in dt.class_scopes(text) if _is_card_shaped(text[a:b])]


def _div_extent(text: str, open_end: int) -> int:
    """End offset of the </div> matching a <div> whose opening tag ends at
    `open_end`. A plain depth walk over <div>/</div> tokens -- not a real
    parser, but every element this file scans is built from plain <div>s
    (verified while measuring: no scanned card/tile/popover in this tree
    uses <section>/<article> as its own shell)."""
    depth = 1
    for m in re.finditer(r"<div\b|</div>", text[open_end:]):
        depth += -1 if m.group(0).startswith("</div") else 1
        if depth <= 0:
            return open_end + m.end()
    return len(text)


# ══════════════════════════════════════════════════════════════════════════
# Jinja rendering harness. A REAL jinja2.Environment over templates/, so
# card()'s output is the macro EXECUTING (C-1's own point), not its source
# text being read. Standalone -- no Flask/app.py import for any test in
# this section: only C-8's and C-10's rendered-DOM confirmations need the
# real test client (per the spec's own text for both), and those use the
# app_obj/client fixtures below, copied from tests/test_jump_search.py's
# pattern exactly per this step's safety instructions (import app INSIDE
# the fixture body, so app.py's `_under_pytest` gate suppresses the real
# collector/scheduler -- see this step's own report for why that matters).
# ══════════════════════════════════════════════════════════════════════════

def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)


def _make_next_tip_id():
    """Standalone stand-in for app.py's _next_tip_id -- same shape (a
    counter that mints a fresh id per call), no flask.g involved."""
    state = {"n": 0}

    def _next():
        state["n"] += 1
        return f"ps-test-{state['n']}"
    return _next


def render(source: str, **ctx) -> str:
    """Render a synthetic template SOURCE against a fake `t`/`next_tip_id`
    context -- the two context-processor globals _tip.html's macros read
    (its own header comment: "both are context-processor globals, not
    macro parameters"). `t={}` is enough: dict.get(key, fallback) returns
    fallback for every key, which is all `tip_button`/`tip_mirror` need
    from translations for these tests."""
    tmpl = _jinja_env().from_string(source)
    base_ctx = {"t": {}, "next_tip_id": _make_next_tip_id()}
    base_ctx.update(ctx)
    return tmpl.render(**base_ctx)


CARD_IMPORT = '{% from "partials/_card.html" import card, subblock, subhead with context %}'


# ══════════════════════════════════════════════════════════════════════════
# C-1 -- test_the_canonical_card_is_one_string_and_the_macro_is_where_it_lives
# ══════════════════════════════════════════════════════════════════════════

def test_the_canonical_card_is_one_string_and_the_macro_is_where_it_lives():
    """C-1. Render card() through a REAL Jinja Environment and compare the
    EMITTED class attribute to CARD -- not read the constant out of the
    template source. Asserting a literal in this test equals a literal in
    _card.html would be the exact tautology C-1's own spec wording warns
    against ("test_the_scale_has_only_two_distinct_values had to be
    rescued from" -- test_design_radii.py's own history)."""
    html = render(CARD_IMPORT + "{% call card() %}body-marker{% endcall %}")
    m = re.search(r'<div class="([^"]*)">', html)
    assert m, f"card() did not render an outer <div class=\"...\">: {html!r}"
    assert m.group(1) == CARD, f"rendered card() class {m.group(1)!r} != CARD {CARD!r}"
    assert "body-marker" in html, "caller() content is missing from the rendered card"


def test_the_flush_variant_is_the_other_pinned_string():
    """Companion to C-1: flush=True must emit CARD_FLUSH, character for
    character -- the ONLY other legal card shell (§4.1: "There is no third
    answer")."""
    html = render(CARD_IMPORT + "{% call card(flush=True) %}x{% endcall %}")
    m = re.search(r'<div class="([^"]*)">', html)
    assert m and m.group(1) == CARD_FLUSH, (
        f"rendered flush card() class {m.group(1) if m else None!r} != CARD_FLUSH {CARD_FLUSH!r}")


def test_extra_is_appended_after_the_canonical_string_not_folded_into_it():
    """`extra=` (§4.3) composes onto the end of CARD; the base string
    itself never changes shape."""
    html = render(CARD_IMPORT + '{% call card(extra="mb-6") %}x{% endcall %}')
    m = re.search(r'<div class="([^"]*)">', html)
    assert m and m.group(1) == CARD + " mb-6", f"got {m.group(1) if m else None!r}"


# ══════════════════════════════════════════════════════════════════════════
# C-2 -- test_every_card_shaped_class_string_is_one_of_the_pinned_ones
# RATCHET. CARD_EXCEPTIONS / CARD_EXCEPTION_TOTAL.
# ══════════════════════════════════════════════════════════════════════════

def _card_exception_counts() -> tuple[dict[str, int], set[str]]:
    per_file: dict[str, int] = {}
    strings: set[str] = set()
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = 0
        for _a, _b, cls in _card_shaped_spans(text):
            if not _is_allowed_card_string(cls):
                n += 1
                strings.add(_norm(cls))
        if n:
            per_file[rel] = n
    return per_file, strings


def test_the_card_shape_scan_actually_catches_a_violation():
    """Positive control. A near-miss (one extra utility beyond the
    canonical string) must be caught; the canonical string itself, and
    each of the five §4.5 idioms (+ their one named suffix variant), must
    NOT be."""
    assert _is_card_shaped('bg-card rounded-lg border border-line p-5 mb-2')
    assert not _is_allowed_card_string('bg-card rounded-lg border border-line p-5 mb-2')
    assert _is_allowed_card_string(CARD)
    assert _is_allowed_card_string(CARD_FLUSH)
    assert _is_allowed_card_string(TILE)
    assert _is_allowed_card_string(TILE_ON_PAGE)
    for idiom in NOT_CARD_IDIOMS.values():
        assert _is_allowed_card_string(idiom), f"§4.5 idiom rejected: {idiom!r}"
    assert _is_allowed_card_string(NOT_CARD_IDIOMS["notice"] + " border-warning")
    assert _is_allowed_card_string(NOT_CARD_IDIOMS["doorway"] + " border-dashed")
    assert not _is_allowed_card_string(NOT_CARD_IDIOMS["notice"] + " border-brand")


def test_a_hover_or_dark_variant_is_not_mistaken_for_a_static_card_surface():
    """Positive control for the (?<!:) guard -- see _is_card_shaped's own
    comment. Real shape: `p-1(.5) rounded(-md) hover:bg-raised dark:hover:
    bg-line` -- 8 real sites across 4 files (base.html's three sidebar/
    theme-toggle icon buttons, workflows.html's three modal-close buttons,
    one each in partials/settings/_servers.html and _server_config.html),
    which satisfies bg-(raised|page)+rounded+p-N ONLY if the hover: prefix
    is ignored."""
    assert not _is_card_shaped("p-1 rounded hover:bg-raised dark:hover:bg-line text-muted")
    assert not _is_card_shaped("p-2 rounded hover:bg-page dark:hover:bg-page/50")


def test_an_inline_flex_segmented_control_is_not_mistaken_for_a_card():
    """Positive control for the inline-flex guard -- see _is_card_shaped's
    own comment. Real shape: server_detail.html's heatmap week-selector,
    `inline-flex rounded border border-line p-0.5 bg-page text-[10px]`."""
    assert not _is_card_shaped("inline-flex rounded border border-line p-0.5 bg-page text-[10px]")


# Seeded 2026-09-17 by RUNNING _card_exception_counts() against this tree
# as it stands after WP-6 step 13 (6ce42dc) -- per C28, never copied from
# the spec's own "131 sites / 57 strings" expectation, which Part II-a's
# own C28 bullet already names as one of two DIFFERENT measurements
# (124 vs 131) taken by different detectors in different spec drafts.
#
# Measured: 133 sites / 67 distinct strings across 35 files -- roughly 2%
# over the site count and roughly 18% over the string count. Investigated
# by hand rather than accepted blindly (the same standard step 13's own
# HEADING_UNDERTEXT_BASELINE was held to for landing outside ITS
# expectation): an early pass measured 143 sites / 75 strings and turned
# out to include two real detector bugs, both fixed before this baseline
# was taken --
#   (1) `hover:bg-raised`/`dark:hover:bg-line` on a handful of icon
#       buttons (base.html's sidebar-collapse controls) satisfied
#       bg-(raised)+rounded+p-N by coincidence of a Tailwind VARIANT
#       sharing its base utility's spelling -- fixed with the (?<!:)
#       guard now on _BG/_ROUNDED/_PAD/_OVERFLOW_HIDDEN (8 sites, 6
#       strings removed);
#   (2) two `inline-flex` segmented-toggle controls (server_detail.html's
#       heatmap week-selector, settings.html's LDAP-picker mode toggle)
#       satisfied bg-(page)+rounded+p-\\d the same coincidental way --
#       fixed by excluding `inline-flex` (2 sites, 2 strings removed).
# The residual ~2%/~18% overshoot against the spec's own rough numbers is
# consistent with what C28 already documents about this exact
# measurement (two prior detectors landed on 124 and 131 respectively for
# what was nominally the same count) plus 13 further implementation steps
# of markup changes since the addendum's own number was taken. Every
# entry below was produced by the detector, not hand-adjusted.
# Lowered 2026-09-17 by WP-6 step 15 (Batch B, the Settings family) --
# RE-RUN, not hand-computed, against the tree after converting settings.html
# + partials/settings/*.html to {% call card(...) %}. Every card SHELL this
# step converted reached the exact canonical CARD/CARD_FLUSH string (the
# pre-conversion violation in each case was purely ORDER -- "bg-card
# rounded-lg p-5 border border-line" instead of the pinned "...border
# border-line p-5" -- never an extra utility), so each conversion removes
# exactly one exception. Four entries below reached zero and are deleted
# rather than kept at 0 (_compliance.html, _maintenance.html, _rbac.html,
# _tls.html), matching this ratchet's own established convention. Five
# entries stay NONZERO because this step deliberately did not touch a
# second, DIFFERENT card-shaped site still living in the same file --
# _detection.html's two `bg-page` grid boxes (Thresholds/Anomaly),
# _restarts.html's and _health_checks.html's/_dependencies.html's own
# `bg-page`-recessed rows/forms, and _servers.html's one JS-built tag-pill
# row -- none of which this step's own text named for conversion (see this
# step's own report for the tile-conversion scope decision).
# Lowered 2026-09-17 by WP-6 step 16 (Batch C -- Reports, Monitoring,
# Operations) -- RE-RUN, not hand-computed. monitoring.html (1 -> 0),
# partials/active_actions.html (1 -> 0) and partials/updates_overview.html
# (1 -> 0) reached exactly zero and are deleted, matching this ratchet's own
# convention. reports.html falls from 8 to 1: seven `bg-card p-6` sections
# converted to {% call card(...) %} (now invisible to this source-text
# scan, same "moved behind the macro" effect CARD_EXCEPTIONS already
# documents for step 15); the eighth exception -- the JS-filled #fleet-band,
# converted from a hand-rolled `bg-page` shape to TILE plus an appended
# `mb-4` layout margin -- stays counted because `mb-4` is not part of the
# pinned TILE string, exactly as C-2's own allowlist is written (an exact
# string match, not "TILE plus anything"). operations.html falls from 15 to
# 4: the Runbook Library, System & Tools and Data Management card shells
# convert (macro-invisible, -3), the seven bg-page System Health tiles
# reach the exact TILE string (-7), and the runbook-run-modal's code shell
# is fixed from `bg-page` to the pinned `bg-raised` idiom (-1) -- eleven
# sites resolved. The four that remain: the three Danger Zone action boxes
# (Clean/Delete/Factory Reset), each of which keeps a real `<button>` and so
# is deliberately NOT converted to the TILE idiom (§4.4: "if it needs a
# button, it is a card and belongs at the page level") -- only their
# bg-page/dark:bg-page/50 -> bg-raised theme-token fix and rounded-lg ->
# rounded radius step landed, which does not change their exception COUNT
# (still non-canonical before and after, for the same underlying shape);
# and one pre-existing, untouched JS-built status box (`rounded p-2 text-sm
# bg-raised dark:bg-line text-muted`, the config-upload result banner) this
# step's own brief never named.
# Lowered 2026-09-17 by WP-6 step 17 (Batch D -- server_detail, server_card,
# analytics, comparison) -- RE-RUN, not hand-computed.
# partials/server_analytics.html reaches exactly zero (all four sites --
# the per-anomaly tile, the no-anomalies notice, the two forecast tiles --
# converted to card()) and is deleted, matching this ratchet's own
# convention. partials/server_comparison.html also reaches zero (its one
# p-6 shell converted, plus its seven internal <h3> sub-titles moved to
# subhead() so C-7's static scanner does not misread a real sub-heading as
# the card's own) and is deleted too. partials/server_card.html stays at 1
# (its one server-card grid tile is unchanged in shape -- see this step's
# own report for why the spec's "doorway" wording was not applied there).
# server_detail.html falls from 19 to 13: the metrics-container spinner
# (now empty_state(card=true), macro-invisible), the 24h Trend Chart card,
# the Config Changes flush card, the brand-bordered runbook-output panel,
# and the two hand-rolled notice banners in renderUpdates() all convert (-6
# card-shaped exceptions -- the notices were TWO of the 19 despite one
# having an arbitrary-hex background invisible to _is_card_shaped, because
# the OTHER, bg-page one WAS visible; converting both to the canonical
# bg-raised notice string removes that one visible exception and leaves
# the other, now-visible-but-compliant, uncounted either way). The Security
# and Dependencies p-4 cards (5 sites) are UNCHANGED and remain counted --
# see this step's own report for the C-7/signal-icon conflict that left
# them as hand-rolled divs rather than {% call card(...) %}.
# Lowered 2026-09-17 by WP-6 step 18 (Batch E -- fleet and overview pages) --
# RE-RUN, not hand-computed. dashboard.html, network.html, scan.html,
# topology.html and partials/services_table.html all reach exactly zero and
# are deleted, matching this ratchet's own convention:
#   dashboard.html (5 -> 0): the onboarding p-10 dashed box converts to
#     empty_state(card=true) (macro-invisible, -1); all four doorway tiles
#     reach the exact §4.5 doorway string once rounded-md->rounded-lg and
#     p-4->p-5 land (-4).
#   network.html / scan.html (2 -> 0 each): the rounded-md notice-shaped box
#     converts to card(extra='flex items-start gap-3 mb-4') -- its RENDERED
#     class string still carries extra utilities beyond CARD's own (it would
#     still count if hand-typed), but a {% call card( site has no literal
#     class="..." text in the TEMPLATE SOURCE at all -- class_scopes() has
#     nothing to find, so the site is invisible to this source-level scan,
#     the identical "moved behind the macro" effect every prior step's H2
#     conversions already rely on, just exercised here for extra= instead of
#     heading=. The second site, the "What Prism watches today" card,
#     dissolves entirely rather than converting: its heading moves bare onto
#     `page` per §2.3 and its links become their own compliant doorway
#     cards, so there is no card shell left at all. Both of the file's two
#     pre-existing exceptions are gone, for two different reasons.
#   topology.html (3 -> 0): the Legend and Blast Radius p-4 boxes convert to
#     card(heading=...) (both macro-invisible, -2); the canvas wrapper's
#     stray mb-4 moves onto the following grid's mt-4 instead, so the shell
#     itself becomes the exact canonical CARD_FLUSH string (-1).
#   partials/services_table.html (7 -> 0): the four p-4 stat tiles reach the
#     exact TILE_ON_PAGE string (-4); both table shells fix rounded-md unde
#     rounded-lg and reach exact CARD_FLUSH (-2); the unreadable-panel
#     notice converts to card(extra='border-warning flex items-start
#     gap-3') (macro-invisible, -1).
# Two entries LOWER but not to zero:
#   compliance.html (4 -> 1): the readiness-card and audit-card p-4 readouts
#     reach the exact TILE_ON_PAGE string (-2); the findings-card doorway
#     conversion (card-clickable, p-5, flex items-start gap-3, an exact
#     §4.5 doorway match) also resolves (-1) -- see this step's own report
#     for why treating findings-card as the third "clickable group" is a
#     flagged judgement call, not a certainty. The one remaining exception
#     is the JS-built SOP-grid card (`card.className = 'bg-card rounded-lg
#     border border-line p-3 flex flex-col gap-2'`) -- untouched, out of
#     this step's own named scope (only "the four bg-card p-4 readouts,
#     plus two more in compliance.html" were named, and this is neither).
#   servers.html (5 -> 1): all five bg-raised code-shell blocks in the
#     WinRM setup guide gain `border border-line`, reaching the pinned
#     "bg-raised rounded border border-line overflow-hidden" code-shell
#     idiom (§4.5). Four resolve exactly; the fifth (the troubleshooting
#     section's Get-Credential snippet) keeps a trailing `mt-1` for the
#     small gap after its preceding sentence, so it stays one exception --
#     the same "canonical string + a necessary layout utility" shape this
#     ratchet's own history already accepts for step 16's #fleet-band mb-4.
CARD_EXCEPTIONS: dict[str, int] = {
    "base.html": 4,
    "compliance.html": 1,
    "compliance_doc.html": 2,
    "compliance_sop.html": 3,
    "login.html": 1,
    "operations.html": 4,
    "partials/_empty_state.html": 1,
    "partials/_skeletons.html": 4,
    "partials/critical_issues.html": 1,
    "partials/server_card.html": 1,
    "partials/settings/_dependencies.html": 1,
    "partials/settings/_detection.html": 2,
    "partials/settings/_health_checks.html": 1,
    "partials/settings/_restarts.html": 1,
    "partials/settings/_servers.html": 1,
    "reports.html": 1,
    "server_detail.html": 13,
    "servers.html": 1,
    "settings.html": 8,
    "setup.html": 1,
    "workflows.html": 5,
}

CARD_EXCEPTION_TOTAL = 57


def test_every_card_shaped_class_string_is_one_of_the_pinned_ones():
    """C-2. Ratchet. A card-shaped string not yet one of the pinned
    constants/idioms is expected debt until its file's card-conversion
    step (15-20) lands, not a build failure today."""
    counts, _strings = _card_exception_counts()
    grew = [f"{f}: {n} (baseline {CARD_EXCEPTIONS.get(f, 0)})"
            for f, n in counts.items() if n > CARD_EXCEPTIONS.get(f, 0)]
    assert not grew, (
        "card-shaped class string(s) increased -- use the card()/subblock() "
        "macro or one of the §4.5 idioms:\n  " + "\n  ".join(grew))


def test_the_card_exception_baseline_is_not_left_behind():
    counts, _strings = _card_exception_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in CARD_EXCEPTIONS.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER card-shape exceptions than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_card_exception_outside_the_templates_that_already_have_one():
    counts, _strings = _card_exception_counts()
    new = sorted(set(counts) - set(CARD_EXCEPTIONS))
    assert not new, f"new template(s) with a non-canonical card-shaped string: {new}"


def test_the_total_number_of_card_exceptions_never_rises():
    counts, _strings = _card_exception_counts()
    total = sum(counts.values())
    assert total <= CARD_EXCEPTION_TOTAL, (
        f"total card-shape exceptions rose to {total} (was {CARD_EXCEPTION_TOTAL}):\n  "
        + "\n  ".join(f"{f}: {n} (baseline {CARD_EXCEPTIONS.get(f, 0)})"
                       for f, n in sorted(counts.items()) if n != CARD_EXCEPTIONS.get(f, 0)))
    assert total == CARD_EXCEPTION_TOTAL, (
        f"total fell to {total}; lower CARD_EXCEPTION_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# C-3 -- test_no_card_padding_radius_shadow_or_surface_is_set_at_a_call_site
# Vacuous over the real tree today (zero {% call card( sites); proven by a
# positive control against synthetic `extra=` values.
# ══════════════════════════════════════════════════════════════════════════

_CALL_CARD_OPEN = re.compile(r"\{%-?\s*call\s+card\((.*?)\)\s*-?%\}", re.S)
_CALL_OPEN_TAG = re.compile(r"\{%-?\s*call\b")
_CALL_END_TAG = re.compile(r"\{%-?\s*endcall\s*-?%\}")
_CALL_TAG = re.compile(f"(?:{_CALL_OPEN_TAG.pattern})|(?:{_CALL_END_TAG.pattern})")

_EXTRA_ARG = re.compile(r"""\bextra\s*=\s*(['"])(?P<val>(?:(?!\1).)*)\1""", re.S)
# Allowed (§4.3/C-3): grid/flex utilities, hidden, sticky, mb-*, and
# border-<status>(/alpha). Anything else is forbidden outright, so this is
# a DENYLIST check on every space-separated token, not an allowlist regex
# applied to the whole string -- the whole-string form cannot say WHICH
# token was the offender.
#
# "brand" added 2026-09-17 (WP-6 step 17, Batch D): the runbook-output
# panel in server_detail.html is the brand-bordered flush card this step's
# own text names explicitly -- card(flush=true, extra='border-brand/30'),
# replacing dark:border-[#8B5CF6]/30. brand is not one of the four
# health-status colours, but it is already used exactly like one elsewhere
# in this tree (LIFECYCLE_COLOUR_BUCKET in tests/test_design_tokens.py
# buckets the install/reboot/restart-required lifecycle onto brand,
# alongside accent and healthy) -- "an action is in progress / waiting on
# a human", the same role border-critical/-warning play for a health
# state. First real {% call card( site in this tree to exercise C-3 for
# real (every prior one was vacuous), so this widening is what makes that
# first exercise pass rather than fail on a legitimate, spec-named colour
# C-3's own allowlist had never had reason to include yet.
_STATUS = ("critical", "warning", "healthy", "info", "brand")
_EXTRA_TOKEN_OK = re.compile(
    r"^(hidden|sticky|top-\d+(\.\d+)?|mb-\d+(\.\d+)?|"
    r"grid[a-z0-9-]*|(row|col)(-[a-z0-9-]+)?|"
    r"flex(-[a-z0-9-]+)?|items-[a-z]+|justify-[a-z]+|content-[a-z]+|gap-[a-z0-9.]+|"
    r"order-[a-z0-9]+|w-[a-z0-9\[\]/.%]+|max-w-[a-z0-9\[\]/.%]+|"
    r"border-(?:" + "|".join(_STATUS) + r")(?:/\d+)?)$")


def _card_call_blocks(text: str) -> list[tuple[str, str, int, int]]:
    """[(args_str, body, body_start, body_end)] for every top-level
    {% call card(...) %}...{% endcall %} in `text`. Depth-walks Jinja's OWN
    {% call %}/{% endcall %} tokens (not just card('s own), so a NESTED
    {% call subblock() %} inside the body does not end the match early."""
    out = []
    for m in _CALL_CARD_OPEN.finditer(text):
        depth = 1
        body_end = None
        for tm in _CALL_TAG.finditer(text, m.end()):
            if _CALL_END_TAG.match(tm.group(0)):
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                body_end = tm.start()
                break
        if body_end is not None:
            out.append((m.group(1), text[m.end():body_end], m.end(), body_end))
    return out


def _extra_violations(text: str) -> list[str]:
    violations = []
    for args, _body, _s, _e in _card_call_blocks(text):
        em = _EXTRA_ARG.search(args)
        if not em:
            continue
        bad = [tok for tok in em.group("val").split() if not _EXTRA_TOKEN_OK.match(tok)]
        if bad:
            violations.append(f"extra={em.group('val')!r} carries forbidden token(s) {bad}")
    return violations


def test_no_card_padding_radius_shadow_or_surface_is_set_at_a_call_site():
    """C-3. Real tree today: zero {% call card( sites, so this is vacuous
    -- see test_the_extra_argument_scan_actually_catches_a_violation for
    proof the mechanism works."""
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        vs = _extra_violations(text)
        if vs:
            violations.append(f"{p.relative_to(TEMPLATES).as_posix()}: {vs}")
    assert not violations, "\n".join(violations)


def test_the_extra_argument_scan_actually_catches_a_violation():
    """Positive control. Each forbidden category (padding, radius, shadow,
    surface colour, dark: variant) must be caught individually; the
    allowed vocabulary (status border, hidden/sticky, mb-*, flex/grid) must
    not be."""
    for bad_extra in ("p-8", "rounded-full", "shadow-lg", "bg-critical/10",
                       "dark:bg-page", "text-critical"):
        sample = '{%% call card(extra="%s") %%}x{%% endcall %%}' % bad_extra
        assert _extra_violations(sample), f"extra={bad_extra!r} was not flagged"
    good_extra = "border-critical/30 mb-4 hidden sticky top-4 flex-1 grid-cols-2"
    good_sample = '{%% call card(extra="%s") %%}x{%% endcall %%}' % good_extra
    assert not _extra_violations(good_sample), (
        f"a fully-legal extra= value was rejected: {_extra_violations(good_sample)}")

    # border-brand -- the real server_detail.html runbook-output-panel site
    # (WP-6 step 17), proving the 2026-09-17 _STATUS widening actually fires.
    brand_sample = '{% call card(extra="border-brand/30") %}x{% endcall %}'
    assert not _extra_violations(brand_sample), (
        f"border-brand/30 was rejected even though _STATUS now includes brand: "
        f"{_extra_violations(brand_sample)}")


# ══════════════════════════════════════════════════════════════════════════
# C-4 -- test_a_card_with_a_heading_comes_from_the_macro
# Flat assert: measures ZERO against the real tree today.
# ══════════════════════════════════════════════════════════════════════════

_DIV_OPEN_CLASS = re.compile(r'<div\b[^>]*?\bclass="([^"]*)"', re.S)
_HEADING_OPEN_TAG = re.compile(r"<h[1-4]\b")


def _raw_card_literal_with_heading(text: str) -> list[str]:
    hits = []
    for m in _DIV_OPEN_CLASS.finditer(text):
        if _norm(m.group(1)) not in (CARD, CARD_FLUSH):
            continue
        body = text[m.end():_div_extent(text, m.end())]
        if _HEADING_OPEN_TAG.search(body):
            hits.append(f"offset {m.start()}: {_norm(m.group(1))}")
    return hits


def test_a_card_with_a_heading_comes_from_the_macro():
    """C-4. A raw CARD/CARD_FLUSH literal is legal only where no heading
    follows inside the element (skeletons, canvas shells). Measured ZERO
    against the real tree today: nothing currently hand-types the exact
    canonical string AND puts a heading inside it -- the templates that DO
    have a heading-then-card shape (CARD_EXCEPTIONS/SECTION_HEADER_BASELINE
    above) universally use a NON-canonical string (extra utilities,
    different padding/order), which is exactly why they show up in THOSE
    ratchets instead of failing this flat assert."""
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        hits = _raw_card_literal_with_heading(text)
        if hits:
            violations.append(f"{p.relative_to(TEMPLATES).as_posix()}: {hits}")
    assert not violations, "\n".join(violations)


def test_the_raw_literal_scan_actually_catches_a_violation():
    """Positive control."""
    bad = f'<div class="{CARD}"><h2 class="...">Title</h2></div>'
    assert _raw_card_literal_with_heading(bad), "a raw CARD literal with a heading was not caught"
    ok_no_heading = f'<div class="{CARD}"><p>no heading here</p></div>'
    assert not _raw_card_literal_with_heading(ok_no_heading)
    ok_not_canonical = '<div class="bg-card rounded-lg border border-line p-4"><h2>X</h2></div>'
    assert not _raw_card_literal_with_heading(ok_not_canonical), (
        "a NON-canonical string was flagged -- C-4 is only about the exact "
        "canonical literal, everything else is C-2's job")


def test_a_card_built_from_javascript_uses_the_same_string():
    """C-4b. Via dt.class_scopes() -- "the class-string scanner's
    blindness to el.className = '...' is why 629 literals survived the
    token migration." Two proofs: (1) a synthetic JS-built card string is
    visible to _card_shaped_spans (which is built on class_scopes()) the
    same way a markup one is; (2) a REAL JS-built card in this tree today
    (server_detail.html's log-table shell, `let html = '<div class="...">'`)
    is found by the same mechanism, cross-checking the synthetic proof
    against a real, already-known site rather than only a fabricated one."""
    js_sample = "let html = '<div class=\"bg-card rounded-lg border border-line p-5 mb-3\">';"
    spans = _card_shaped_spans(js_sample)
    assert spans, "a JS-built (el.className / string-literal) card was not found by class_scopes()"
    assert not _is_allowed_card_string(spans[0][2]), (
        "the JS-built sample's class does not match the fixture's own intent (must be non-canonical)")

    real_text = _code_only((TEMPLATES / "server_detail.html").read_text(encoding="utf-8"))
    real_spans = [cls for _a, _b, cls in _card_shaped_spans(real_text)
                  if "overflow-hidden" in cls and "let html" not in cls]
    assert any("bg-card rounded-lg border border-line overflow-hidden" == _norm(c)
               for c in real_spans), (
        "server_detail.html's known JS-built table-shell card "
        "('bg-card rounded-lg border border-line overflow-hidden') was not "
        "found via class_scopes() -- the cross-check against real code no "
        "longer holds")


# ══════════════════════════════════════════════════════════════════════════
# C-5 -- test_the_only_shadow_names_used_are_the_ones_the_config_maps
# First clause: flat assert (the config). Second clause: RATCHET
# (SHADOW_NAME_BASELINE) -- see this file's own module docstring for why.
# ══════════════════════════════════════════════════════════════════════════

ALLOWED_SHADOW_NAMES = frozenset({"sm", "md", "lg"})


def _config_shadow_names() -> dict[str, str]:
    block = re.search(r"boxShadow:\s*\{(.*?)\}", BASE.read_text(encoding="utf-8"), re.S)
    assert block, "boxShadow is missing from tailwind.config in base.html"
    return dict(re.findall(r"'(\w+)':\s*'([^']+)'", block.group(1)))


def test_the_shadow_config_maps_exactly_sm_md_lg():
    """C-5, first clause. Flat assert -- a property of base.html's own
    tailwind.config, not affected by which templates have converted."""
    names = _config_shadow_names()
    assert set(names) == ALLOWED_SHADOW_NAMES, (
        f"boxShadow maps {sorted(names)}, expected exactly {sorted(ALLOWED_SHADOW_NAMES)}")
    for name in ALLOWED_SHADOW_NAMES:
        assert names[name] == f"var(--shadow-{name})", f"boxShadow.{name} = {names[name]!r}"


_BANNED_SHADOW_USAGE = re.compile(r"(?<!:)\bshadow-(xl|2xl|sm|inner|none)\b")


def test_the_shadow_name_allowlist_does_not_derive_from_the_config():
    """C-5's positive control, encoded so it cannot silently pass for the
    wrong reason: ALLOWED_SHADOW_NAMES is a FIXED literal, not computed
    from _config_shadow_names(), so even a config that also mapped `xl`
    would not make `shadow-xl` legal. The manual half of this proof --
    actually editing base.html's boxShadow to add an `xl` entry and
    confirming usage is STILL flagged -- is this step's own Verify line
    and is done by hand, not automated here (mutating the real config
    inside an automated test would be exactly the kind of self-mutating
    test this house does not write)."""
    mutated = dict(_config_shadow_names())
    mutated["xl"] = "var(--shadow-lg)"  # simulates the manual base.html edit
    assert "xl" in mutated and "xl" not in ALLOWED_SHADOW_NAMES, (
        "the allowlist must stay {sm, md, lg} regardless of what the config maps")
    assert _BANNED_SHADOW_USAGE.search('class="bg-card rounded-lg shadow-xl"'), (
        "shadow-xl usage is not detected even though it is never legal")


def _shadow_name_counts() -> dict[str, int]:
    per_file: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = sum(len(_BANNED_SHADOW_USAGE.findall(text[a:b])) for a, b in dt.class_scopes(text))
        if n:
            per_file[rel] = n
    return per_file


# Seeded 2026-09-17 by RUNNING _shadow_name_counts() against this tree.
# Cross-checked against `grep -rEn 'class="[^"]*\\bshadow-(xl|2xl|sm|inner|
# none)\\b' templates/*.html templates/partials/*.html templates/partials/
# **/*.html`, which independently returns the same 21 lines (one further
# raw `grep -rn shadow-sm` hit, base.html:101, is `var(--shadow-sm)` inside
# the boxShadow CONFIG block itself -- a CSS custom property name, not a
# Tailwind class, and correctly outside any dt.class_scopes() span).
# Every site is a modal panel (shadow-xl/2xl, 19 of the 21) or a plain page
# card (shadow-sm, the remaining 2 -- login.html and setup.html, also
# STATIC_SHADOW_BASELINE's own two sites below) -- see this file's module
# docstring for the full account of why this is a ratchet and not the flat
# assert the spec's own text describes.
#
# 21 -> 5 with WP-6 step 19 (Batch F, overlays collapse onto shadow-lg):
# all six files this step touches (base.html, both settings partials,
# server_detail.html, settings.html, workflows.html) drop to zero --
# every modal panel/popover in scope moved shadow-xl/2xl -> shadow-lg,
# the one legal name this ratchet does not ban. The five remaining sites
# (login.html/setup.html's shadow-sm, operations.html/servers.html's
# leftover shadow-xl/2xl) are untouched, out of this step's own Files
# list -- steps 19 named exactly six files, not these four.
SHADOW_NAME_BASELINE: dict[str, int] = {
    "login.html": 1,
    "operations.html": 2,
    "servers.html": 1,
    "setup.html": 1,
}

SHADOW_NAME_TOTAL = 5


def test_no_shadow_xl_2xl_sm_inner_or_none_class_appears_in_any_template():
    """C-5, second clause. Ratchet (see module docstring)."""
    counts = _shadow_name_counts()
    grew = [f"{f}: {n} (baseline {SHADOW_NAME_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > SHADOW_NAME_BASELINE.get(f, 0)]
    assert not grew, (
        "a banned shadow name increased -- only shadow-sm/md/lg... no, only "
        "shadow-md/lg are legal (shadow-sm is ALSO banned; see "
        "ALLOWED_SHADOW_NAMES):\n  " + "\n  ".join(grew))


def test_the_shadow_name_baseline_is_not_left_behind():
    counts = _shadow_name_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in SHADOW_NAME_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now use FEWER banned shadow names than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_banned_shadow_name_outside_the_templates_that_already_have_one():
    new = sorted(set(_shadow_name_counts()) - set(SHADOW_NAME_BASELINE))
    assert not new, f"new template(s) with a banned shadow name: {new}"


def test_the_total_number_of_banned_shadow_names_never_rises():
    counts = _shadow_name_counts()
    total = sum(counts.values())
    assert total <= SHADOW_NAME_TOTAL, f"total rose to {total} (was {SHADOW_NAME_TOTAL})"
    assert total == SHADOW_NAME_TOTAL, (
        f"total fell to {total}; lower SHADOW_NAME_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# C-6 -- test_no_card_in_page_flow_carries_a_static_shadow
# RATCHET (STATIC_SHADOW_BASELINE) -- see module docstring.
# ══════════════════════════════════════════════════════════════════════════

_FIXED_OR_ABS = re.compile(r"(?<!:)\b(fixed|absolute)\b")
_Z_SIGNAL = re.compile(r"(?<!:)\bz-\[?\d|z-index\s*:\s*\d")
_STATIC_SHADOW = re.compile(r"(?<![:\w-])shadow(?:-([a-z0-9]+))?\b")


def _static_shadow_names(cls: str) -> list[str]:
    return [m.group(1) or "DEFAULT" for m in _STATIC_SHADOW.finditer(cls)]


def _static_shadow_on_card_in_flow(text: str) -> dict[str, list[str]]:
    hits: list[str] = []
    for a, b in dt.class_scopes(text):
        cls = text[a:b]
        if not (_BG.search(cls) and _ROUNDED.search(cls)):
            continue
        shadows = _static_shadow_names(cls)
        if not shadows:
            continue
        if _FIXED_OR_ABS.search(cls) and _Z_SIGNAL.search(cls):
            continue  # overlay positioning on the SAME element
        back = text[max(0, a - 500):a]
        if _FIXED_OR_ABS.search(back) and _Z_SIGNAL.search(back):
            continue  # overlay positioning on a near ancestor
        hits.append(f"{shadows}: {cls}")
    return hits


def test_the_static_shadow_scan_actually_catches_a_violation():
    """Positive control: a plain page card with an unconditional shadow
    and no overlay ancestor must be caught; the same card inside a
    `fixed ... z-[N]` overlay (same class, or an ancestor within the
    backward scan) must not be; a `hover:shadow-lg` (conditional, not
    static) must not be either."""
    bad = '<div class="bg-card rounded-lg border border-line p-6 shadow-sm">x</div>'
    assert _static_shadow_on_card_in_flow(bad), "a static shadow in page flow was not caught"

    ok_same_element = '<div class="fixed z-[70] bg-card rounded-lg border border-line p-6 shadow-lg">x</div>'
    assert not _static_shadow_on_card_in_flow(ok_same_element)

    ok_ancestor = ('<div class="fixed inset-0 z-[70] flex items-center justify-center">'
                   '<div class="bg-card rounded-lg shadow-xl border border-line relative">x</div>'
                   '</div>')
    assert not _static_shadow_on_card_in_flow(ok_ancestor), (
        "a card shadow inside a nearby fixed+z overlay ancestor was flagged")

    ok_hover_only = '<div class="bg-card rounded-lg border border-line overflow-hidden hover:shadow-lg">x</div>'
    assert not _static_shadow_on_card_in_flow(ok_hover_only), (
        "a hover:-conditional shadow was treated as static")


def _static_shadow_counts() -> dict[str, int]:
    per_file: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = len(_static_shadow_on_card_in_flow(text))
        if n:
            per_file[rel] = n
    return per_file


# Seeded 2026-09-17 by RUNNING _static_shadow_counts() against this tree.
# TWO sites, both `bg-card rounded-lg border border-line p-6 shadow-sm`
# with no overlay ancestor of any kind: login.html:13 and setup.html:16,
# the two chrome pages' own centred content card. Verified by hand: every
# OTHER shadow-bearing card-shaped element in the tree (the 19
# shadow-xl/2xl modal panels SHADOW_NAME_BASELINE also counts) sits inside
# a `fixed`/`absolute` + z-bearing ancestor within the 500-char backward
# scan -- spot-checked directly against operations.html's two
# `data-action-modal`/confirmation-dialog panels (whose own class carries
# only `relative`, with `fixed inset-0 z-[70]` two <div> levels up) and
# three of workflows.html's modals (template-modal/add-category-modal/
# wf-browse-modal), each the same shape one or two <div> levels up. (Two
# `_Z_SIGNAL` forms are supported -- a Tailwind `z-[n]`/`z-n` class and an
# inline `style="z-index: n"` -- for workflows.html's chrome-panel
# overlays elsewhere in the same file, which set z-index that way; none of
# TODAY's STATIC_SHADOW-adjacent sites happen to need the inline-style
# form, since they all use a real z-[n] class, but the detector supports
# both so a future card-shaped overlay styled either way is still seen.)
STATIC_SHADOW_BASELINE: dict[str, int] = {
    "login.html": 1,
    "setup.html": 1,
}

STATIC_SHADOW_TOTAL = 2


def test_no_card_in_page_flow_carries_a_static_shadow():
    """C-6. Ratchet (see module docstring)."""
    counts = _static_shadow_counts()
    grew = [f"{f}: {n} (baseline {STATIC_SHADOW_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > STATIC_SHADOW_BASELINE.get(f, 0)]
    assert not grew, "a card in page flow gained a static shadow:\n  " + "\n  ".join(grew)


def test_the_static_shadow_baseline_is_not_left_behind():
    counts = _static_shadow_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in STATIC_SHADOW_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER static-shadow-in-flow cards than the "
        "baseline; lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_static_shadow_outside_the_templates_that_already_have_one():
    new = sorted(set(_static_shadow_counts()) - set(STATIC_SHADOW_BASELINE))
    assert not new, f"new template with a static card shadow in page flow: {new}"


def test_the_total_number_of_static_shadows_never_rises():
    counts = _static_shadow_counts()
    total = sum(counts.values())
    assert total <= STATIC_SHADOW_TOTAL, f"total rose to {total} (was {STATIC_SHADOW_TOTAL})"
    assert total == STATIC_SHADOW_TOTAL, (
        f"total fell to {total}; lower STATIC_SHADOW_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# C-7 -- test_a_cards_first_heading_is_an_h2
# Real-tree scan is vacuous (zero {% call card( sites); proven live via
# actual Jinja rendering PLUS a positive control against synthetic source.
# ══════════════════════════════════════════════════════════════════════════

def test_a_cards_first_heading_is_an_h2():
    """C-7. Rendered proof (not just a source-text claim) of both branches
    card() itself supports: the default head-row heading is a real `<h2`,
    and level!=2 (the flush-dialog-panel escape hatch, §4.3) switches to
    the dialog-title carve-out instead."""
    html = render(CARD_IMPORT + '{% call card(heading="Title", icon="server") %}x{% endcall %}')
    m = re.search(r"<h(\d)\b[^>]*>", html)
    assert m and m.group(1) == "2", f"card()'s default head-row heading is not h2: {html!r}"
    assert 'data-role="dialog-title"' not in html
    assert H2 in html and DIALOG_TITLE not in html

    dialog_html = render(
        CARD_IMPORT + '{% call card(heading="Confirm", level=3, flush=True) %}x{% endcall %}')
    dm = re.search(r"<h(\d)\b[^>]*>", dialog_html)
    assert dm and dm.group(1) == "3", f"level=3 did not render an h3: {dialog_html!r}"
    assert 'data-role="dialog-title"' in dialog_html
    assert DIALOG_TITLE in dialog_html and H2 not in dialog_html

    no_heading_html = render(CARD_IMPORT + "{% call card() %}bare{% endcall %}")
    assert not re.search(r"<h[1-4]\b", no_heading_html), (
        "card() with no heading= argument rendered a heading anyway")
    assert HEAD not in no_heading_html and HEAD_FLUSH not in no_heading_html, (
        "card() with no heading= still rendered a head row")


def _first_heading_violations_in(text: str) -> list[str]:
    """Static scanner: for each {% call card( block in `text`, the first
    h1-h4 inside must be h2, UNLESS the block sits within a
    [role="dialog"] ancestor (approximated: role="dialog" appears anywhere
    in the preceding 2000 characters of the file -- a heuristic, not a
    real ancestor walk, adequate for a check that is vacuous over the real
    tree today and proven only against synthetic examples), in which case
    it must carry data-role="dialog-title" instead. A head row (HEAD/
    HEAD_FLUSH-classed div) with no heading inside it at all also fails."""
    violations = []
    for _args, body, start, _end in _card_call_blocks(text):
        in_dialog = 'role="dialog"' in text[max(0, start - 2000):start]
        head_row = re.search(r'class="(?:' + re.escape(HEAD) + "|" + re.escape(HEAD_FLUSH) + ')"',
                              body)
        hm = re.search(r"<h([1-4])\b[^>]*>", body)
        if head_row and not hm:
            violations.append("head row with no heading element inside it")
            continue
        if not hm:
            continue  # no heading at all -- legal (skeleton/canvas shell)
        if hm.group(1) != "2":
            if in_dialog and 'data-role="dialog-title"' in hm.group(0):
                continue
            violations.append(f"first heading is h{hm.group(1)}, not h2 (no dialog carve-out)")
    return violations


def test_the_first_heading_scan_actually_catches_a_violation():
    """Positive control for the static half."""
    bad = '{% call card() %}<h3 class="x">Wrong level</h3>{% endcall %}'
    assert _first_heading_violations_in(bad), "a non-h2 first heading was not caught"

    good = '{% call card() %}<h2 class="x">Right</h2>{% endcall %}'
    assert not _first_heading_violations_in(good)

    dialog_good = ('<div role="dialog">{% call card(level=3) %}'
                   '<h3 data-role="dialog-title">OK</h3>{% endcall %}</div>')
    assert not _first_heading_violations_in(dialog_good)

    dialog_bad = ('<div role="dialog">{% call card(level=3) %}'
                  '<h3>missing the data-role</h3>{% endcall %}</div>')
    assert _first_heading_violations_in(dialog_bad), (
        "a dialog-scoped h3 with no data-role=dialog-title was not caught")

    headless_head_row = ('{% call card() %}<div class="' + HEAD
                          + '">no heading here</div>{% endcall %}')
    assert _first_heading_violations_in(headless_head_row), (
        "a head row with no heading element inside it was not caught")


def test_no_real_template_violates_the_first_heading_rule():
    """C-7's real-tree half. Vacuous today (zero {% call card( sites in
    templates/) -- kept as a real, running assertion (rather than deleted)
    so steps 15-20 are governed by it the moment the first call site
    lands."""
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        vs = _first_heading_violations_in(text)
        if vs:
            violations.append(f"{p.relative_to(TEMPLATES).as_posix()}: {vs}")
    assert not violations, "\n".join(violations)


# ══════════════════════════════════════════════════════════════════════════
# C-8 -- test_a_card_never_contains_another_card
# Static half: vacuous (zero {% call card( sites). Rendered-DOM half: the
# test client, widened to include one specific /partials/ route -- see
# this file's own module docstring "WHAT THIS FILE CANNOT SEE" and the
# comment on _card_nesting_routes below for why.
# ══════════════════════════════════════════════════════════════════════════

def _static_call_depth_nesting(text: str) -> list[str]:
    blocks = _card_call_blocks(text)
    spans = [(s, e) for _a, _b, s, e in blocks]
    violations = []
    for i, (s1, e1) in enumerate(spans):
        for j, (s2, _e2) in enumerate(spans):
            if i != j and s1 < s2 < e1:
                violations.append(f"a {{%% call card( block at {s1} contains another at {s2}")
    return violations


def test_the_static_call_depth_scan_actually_catches_a_violation():
    """Positive control."""
    nested = '{% call card() %}{% call card() %}inner{% endcall %}{% endcall %}'
    assert _static_call_depth_nesting(nested), "a nested {% call card( was not caught"
    siblings = '{% call card() %}a{% endcall %}{% call card() %}b{% endcall %}'
    assert not _static_call_depth_nesting(siblings), (
        "two SIBLING (not nested) {% call card( blocks were flagged as nested")


_BG_CARD_ONLY = re.compile(r"(?<!:)\bbg-card\b")


def _is_floating(cls: str) -> bool:
    """A popover/dropdown/menu (§4.5) is independently positioned -- that
    is what lets it float ABOVE its parent's content instead of
    participating in the card's own in-flow layout, which is the
    structural fact that makes it NOT the nested-card §4.4 forbids.
    partials/server_analytics.html:96's snooze-menu (bg-card, absolute) is
    the real, live instance that exposed the need for this exemption --
    without it, a floating dropdown that happens to share the bg-card
    token with its host anomaly card reads as "a card inside a card"."""
    return bool(_FIXED_OR_ABS.search(cls))


_SCRIPT_BLOCK_RAW = re.compile(r"<script\b.*?</script>", re.S | re.I)


def _strip_script_blocks(html: str) -> str:
    """Blank out <script>...</script> CONTENT (same length, so offsets in
    any violation message still point at the right place in the original
    response). A rendered-DOM check cares about the actual DOM tree a
    browser would build -- a <script> tag's text content is JS SOURCE, not
    markup, even when that source happens to contain the literal
    characters `<div class="bg-card ...">` as part of a
    `let html = '...'` string it will only build at runtime (see this
    file's own module docstring, "WHAT THIS FILE CANNOT SEE": server_
    detail.html's log-viewer builders are exactly this shape, and without
    this stripping step they render as false "nested card" hits the
    moment /server/<name> is actually requested through the test client --
    caught by running this scan for real rather than trusting the
    synthetic positive controls alone)."""
    return _SCRIPT_BLOCK_RAW.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), html)


def _nested_bg_card_violations(html: str) -> list[str]:
    html = _strip_script_blocks(html)
    hits = []
    card_opens = [(m.start(), m.end(), m.group(1)) for m in _DIV_OPEN_CLASS.finditer(html)
                  if _BG_CARD_ONLY.search(m.group(1))]
    for i, (s1, e1, _cls1) in enumerate(card_opens):
        end1 = _div_extent(html, e1)
        for s2, _e2, cls2 in card_opens[i + 1:]:
            if s2 <= s1 or s2 >= end1:
                continue
            if _is_floating(cls2):
                continue
            hits.append(f"offset {s1} contains offset {s2}")
            break
    return hits


def test_the_nested_card_scan_actually_catches_a_violation():
    """Positive control for the rendered-DOM half."""
    bad = '<div class="bg-card rounded-lg p-5"><div class="bg-card rounded p-3">inner</div></div>'
    assert _nested_bg_card_violations(bad), "an in-flow nested bg-card was not caught"


def test_a_floating_popover_inside_a_card_is_not_a_nested_card():
    """Positive control for the _is_floating exemption -- the real shape
    at partials/server_analytics.html:31 (outer) / :96 (inner popover)."""
    ok = ('<div class="bg-card rounded-lg p-4 border border-line">'
          '<div class="hidden absolute left-0 bottom-full mb-1 bg-card border '
          'border-line rounded shadow-lg z-10 py-1 min-w-[100px]">menu</div>'
          '</div>')
    assert not _nested_bg_card_violations(ok), (
        "a floating (absolute) popover inside a card is being misread as a nested card")


@pytest.fixture(scope="module")
def app_obj():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app


@pytest.fixture(scope="module")
def client(app_obj):
    return app_obj.test_client()


def _card_nesting_routes(app_obj) -> list[str]:
    routes = _page_routes() + _settings_section_routes()
    extra = _server_detail_route()
    if extra:
        routes = routes + [extra]
    # Deliberately widened beyond the standard page-route list (which
    # excludes every /partials/ path -- correct for the HEADING tests,
    # which care about the page outline a real visit produces).
    # partials/server_analytics.html:31's anomaly card containing :96's
    # snooze-menu popover is the ONE real, static-Jinja-rendered
    # bg-card-in-bg-card instance anywhere in this tree today, and it
    # renders ONLY behind this htmx-fragment route
    # (routes/views.py:636 partial_server_analytics) -- never as part of
    # /server/<name>'s own initial response (routes/views.py:358's own
    # comment: "`analytics` is deliberately NOT gathered here"). Leaving
    # this route out would make C-8's rendered-DOM half permanently
    # vacuous over the one case it most needs to see (§19's own theme: "a
    # guardrail that cannot see the code is not a guardrail").
    try:
        servers = app_obj.config.get_servers()  # type: ignore[attr-defined]
    except Exception:
        servers = []
        try:
            import config as _config_mod
            servers = _config_mod.get_servers()
        except Exception:
            servers = []
    if servers:
        routes = routes + [f"/partials/server-analytics/{servers[0].name}"]
    return routes


def test_a_card_never_contains_another_card(app_obj, client):
    """C-8. Static {% call card( depth (vacuous today) PLUS a rendered-DOM
    confirmation through the test client. Skips non-200 pages so CI stays
    green without config.json; asserts checked >= 1 so a skip-everything
    failure is visible rather than silently passing."""
    static_violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        vs = _static_call_depth_nesting(text)
        if vs:
            static_violations.append(f"{p.relative_to(TEMPLATES).as_posix()}: {vs}")
    assert not static_violations, "static: " + "; ".join(static_violations)

    routes = _card_nesting_routes(app_obj)
    checked = 0
    rendered_violations = []
    for path in routes:
        r = client.get(path)
        if r.status_code != 200:
            continue
        checked += 1
        vs = _nested_bg_card_violations(r.get_data(as_text=True))
        if vs:
            rendered_violations.append(f"{path}: {vs}")
    assert checked >= 1, "every route was skipped (non-200) -- this check passed vacuously"
    assert not rendered_violations, "rendered: " + "; ".join(rendered_violations)


# ══════════════════════════════════════════════════════════════════════════
# C-9 -- test_the_lowest_step_is_the_reason_an_inset_surface_is_forbidden
# Contrast half: flat assert. text-faint-in-tile half: RATCHET
# (TILE_FAINT_BASELINE) -- see module docstring.
# ══════════════════════════════════════════════════════════════════════════

def test_the_lowest_step_is_the_reason_an_inset_surface_is_forbidden():
    """C-9, contrast half. Flat assert -- pin contrast(faint_light,
    raised_light) in 4.03-4.05 AND < 4.5 (§2.3/§4.4: this is the exact
    number that makes a nested surface a contrast failure the moment
    text-faint is used on it). Mirrors test_design_headings.py's H-3
    exactly, one surface over (raised instead of the ladder's card/page)."""
    on_raised = contrast(dt.TOKENS["faint"][0], dt.TOKENS["raised"][0])
    assert 4.03 <= on_raised <= 4.05, f"light faint-on-raised moved to {on_raised:.4f}:1"
    assert on_raised < 4.5, f"light faint-on-raised is {on_raised:.2f}:1 -- no longer a failure"


_P3 = re.compile(r"(?<!:)\bp-3\b")
_TEXT_FAINT = re.compile(r"(?<!:)\btext-faint\b")


def _tile_text_faint_violations(text: str) -> list[str]:
    hits = []
    for m in _DIV_OPEN_CLASS.finditer(text):
        cls = m.group(1)
        if not (_is_panel_shaped(cls) and _P3.search(cls)):
            continue
        body = text[m.end():_div_extent(text, m.end())]
        if _TEXT_FAINT.search(body):
            hits.append(f"offset {m.start()}: {_norm(cls)}")
    return hits


def test_the_tile_text_faint_scan_actually_catches_a_violation():
    """Positive control."""
    bad = '<div class="bg-page rounded-lg p-3 border border-line"><span class="text-faint">x</span></div>'
    assert _tile_text_faint_violations(bad), "text-faint inside a tile-shaped box was not caught"
    ok = '<div class="bg-page rounded-lg p-3 border border-line"><span class="text-muted">x</span></div>'
    assert not _tile_text_faint_violations(ok)


def _tile_text_faint_counts() -> dict[str, list[str]]:
    per_file: dict[str, list[str]] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        hits = _tile_text_faint_violations(text)
        if hits:
            per_file[rel] = hits
    return per_file


# Seeded 2026-09-17 by RUNNING _tile_text_faint_counts() against this tree.
# TWO sites, both settings.html (line 453, the backup-admin-account status
# box, and line 535, the LDAP Bind Account box a few lines below it) --
# verified by hand: both are a real
# `bg-page rounded-lg p-3 border border-line` (the pre-tile "nested box"
# idiom, Part I §7.7) with a text-faint descendant. This is not a
# coincidence the ratchet happens to catch -- it is precisely the latent
# contrast failure §4.4 names as the REASON nested/inset surfaces are
# forbidden (faint-on-page 4.34:1, faint-on-raised 4.04:1, both < 4.5),
# waiting for whichever future step converts this box to the pinned TILE
# string.
TILE_FAINT_BASELINE: dict[str, int] = {
    "settings.html": 2,
}

TILE_FAINT_TOTAL = 2


def test_no_tile_shaped_box_carries_text_faint():
    """C-9, second half. Ratchet (see module docstring)."""
    counts = {f: len(hits) for f, hits in _tile_text_faint_counts().items()}
    grew = [f"{f}: {n} (baseline {TILE_FAINT_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > TILE_FAINT_BASELINE.get(f, 0)]
    assert not grew, "text-faint appeared in a new tile-shaped box:\n  " + "\n  ".join(grew)


def test_the_tile_faint_baseline_is_not_left_behind():
    counts = {f: len(hits) for f, hits in _tile_text_faint_counts().items()}
    slack = {f: (b, counts.get(f, 0))
             for f, b in TILE_FAINT_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER text-faint-in-tile sites than the "
        "baseline; lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_tile_faint_site_outside_the_templates_that_already_have_one():
    counts = {f: len(hits) for f, hits in _tile_text_faint_counts().items()}
    new = sorted(set(counts) - set(TILE_FAINT_BASELINE))
    assert not new, f"new template with text-faint inside a tile-shaped box: {new}"


def test_the_total_number_of_tile_faint_sites_never_rises():
    counts = {f: len(hits) for f, hits in _tile_text_faint_counts().items()}
    total = sum(counts.values())
    assert total <= TILE_FAINT_TOTAL, f"total rose to {total} (was {TILE_FAINT_TOTAL})"
    assert total == TILE_FAINT_TOTAL, (
        f"total fell to {total}; lower TILE_FAINT_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# C-10 -- test_every_settings_dialog_declares_itself
# role="dialog" exists NOWHERE in this tree yet (step 19's job) -- this
# test is therefore VACUOUS over the real tree today, exactly the honest
# situation test_design_headings.py's H-16 documents for the same
# attribute. Proven instead by positive/negative synthetic controls.
# ══════════════════════════════════════════════════════════════════════════

_ROLE_DIALOG_TAG = re.compile(r'<[a-zA-Z][\w:-]*\b[^>]*\brole="dialog"[^>]*>', re.S)
_ARIA_MODAL_TRUE = re.compile(r'\baria-modal="true"')
_ARIA_LABELLEDBY = re.compile(r'\baria-labelledby="([^"]+)"')


def _dialog_violations(html: str) -> list[str]:
    out = []
    for m in _ROLE_DIALOG_TAG.finditer(html):
        tag = m.group(0)
        if not _ARIA_MODAL_TRUE.search(tag):
            out.append(f'role="dialog" without aria-modal="true": {tag[:100]!r}')
            continue
        lm = _ARIA_LABELLEDBY.search(tag)
        if not lm:
            out.append(f'role="dialog" without aria-labelledby: {tag[:100]!r}')
            continue
        if f'id="{lm.group(1)}"' not in html:
            out.append(f"aria-labelledby={lm.group(1)!r} resolves to no id in the document")
    return out


def test_the_dialog_scan_actually_catches_a_violation():
    """Positive/negative controls -- proves the mechanism works before
    trusting that it measures zero for the right reason. Same convention
    test_design_headings.py's H-16 used for role="dialog", which also does
    not exist anywhere in this tree yet."""
    good = ('<div role="dialog" aria-modal="true" aria-labelledby="dlg-t">'
            '<h2 id="dlg-t">X</h2></div>')
    assert _dialog_violations(good) == []

    no_modal = '<div role="dialog" aria-labelledby="dlg-t"><h2 id="dlg-t">X</h2></div>'
    assert _dialog_violations(no_modal)

    no_label = '<div role="dialog" aria-modal="true"></div>'
    assert _dialog_violations(no_label)

    dangling_label = '<div role="dialog" aria-modal="true" aria-labelledby="ghost"></div>'
    assert _dialog_violations(dangling_label)


def test_every_settings_dialog_declares_itself(client):
    """C-10. VACUOUS today: role="dialog" does not exist anywhere in this
    tree yet (grepped directly, matching test_design_headings.py's own
    H-16 finding) -- it is step 19's job ("every dialog declares itself").
    Kept as a real, running assertion over every settings-section route so
    the day step 19 adds the attribute, this test governs it immediately
    rather than needing to be written from scratch."""
    violations = []
    for path in _settings_section_routes():
        r = client.get(path)
        if r.status_code != 200:
            continue
        violations += [f"{path}: {v}" for v in _dialog_violations(r.get_data(as_text=True))]
    assert not violations, "\n".join(violations)


# ══════════════════════════════════════════════════════════════════════════
# C-11 -- test_the_section_header_row_is_gone
# RATCHET (SECTION_HEADER_BASELINE) -- named in the spec's own §9 registry.
# ══════════════════════════════════════════════════════════════════════════

_HEADING_OPEN = re.compile(r"<h[1-4]\b")
_HEADING_CLOSE = re.compile(r"</h[1-4]>")
_DIV_OPEN_ANY = re.compile(r"<div\b")
_DIV_CLOSE = re.compile(r"</div>")
_SECTION_ROW_WINDOW = 600


def _heading_already_inside_a_panel(text: str, heading_start: int, prev_boundary: int) -> bool:
    """Best-effort: is `heading_start` still nested inside the nearest
    PRECEDING panel-shaped <div>? (D2-compliant headings, already inside
    their card -- server_detail.html's Failed Login Heatmap <h3>, followed
    by an unrelated week-selector pill, is the real example that exposed
    the need for this: without it, that pill's coincidentally card-shaped
    class is misread as "the card this heading titles".) Uses
    _is_panel_shaped (not _is_card_shaped) so a padding-less flush modal
    shell still counts as an enclosing panel -- see _is_panel_shaped's own
    comment for the settings.html LDAP-picker dialog case that required
    this."""
    region = text[prev_boundary:heading_start]
    last_panel_open = None
    for dm in _DIV_OPEN_CLASS.finditer(region):
        if _is_panel_shaped(dm.group(1)):
            last_panel_open = dm.end()
    if last_panel_open is None:
        return False
    between = region[last_panel_open:]
    depth = 1
    tokens = sorted([(m.start(), 1) for m in _DIV_OPEN_ANY.finditer(between)] +
                     [(m.start(), -1) for m in _DIV_CLOSE.finditer(between)])
    for _pos, delta in tokens:
        depth += delta
        if depth <= 0:
            return False
    return True


def _section_header_row_sites(text: str) -> list[str]:
    """"Heading above a card" -- the OLD (pre-D2) idiom §7.2/§7.3
    describe, which the macro's own head row (§4.3) replaces: a heading
    OUTSIDE a card, immediately followed by that card. Modal title bars
    are excluded "by requiring the row's next sibling to be a card" (the
    spec's own words for C-11) -- which is exactly what looking for a
    CARD-SHAPED div among the next few elements already implements: a
    modal's title bar is typically followed by body prose, not another
    card-shaped element, so it does not match on its own."""
    hits = []
    heading_opens = [m.start() for m in _HEADING_OPEN.finditer(text)]
    heading_closes = [m.start() for m in _HEADING_CLOSE.finditer(text)]
    for idx, hm in enumerate(_HEADING_CLOSE.finditer(text)):
        prev_boundary = heading_closes[idx - 1] if idx > 0 else 0
        if _heading_already_inside_a_panel(text, heading_opens[idx], prev_boundary):
            continue
        window_end = hm.end() + _SECTION_ROW_WINDOW
        if idx + 1 < len(heading_closes):
            # Never read past the NEXT heading's own close -- otherwise a
            # heading-dense file (server_detail.html: 17, settings.html: 13
            # per test_design_headings.py's HEADING_BASELINE) double- or
            # triple-counts one card against every nearby heading whose
            # window happens to reach it.
            window_end = min(window_end, heading_closes[idx + 1])
        window = text[hm.end():window_end]
        # (up to) the first THREE div-opens after the heading closes: §7.3's
        # own model puts an optional controls cluster <div> as a sibling of
        # the heading inside the header-row wrapper before the real card is
        # reached (heading close -> [controls div] -> header-row div closes
        # -> THE CARD) -- "first div only" misses every header-row that has
        # a controls cluster. Capped at 3 so a heading followed by ordinary
        # body content (not this shape at all) does not walk arbitrarily
        # deep looking for some unrelated later card.
        for dm in list(_DIV_OPEN_CLASS.finditer(window))[:3]:
            if _is_card_shaped(dm.group(1)):
                hits.append(f"line {text.count(chr(10), 0, hm.start()) + 1}: {_norm(dm.group(1))}")
                break
    return hits


def test_the_section_header_row_scan_actually_catches_a_violation():
    """Positive control -- §7.3's own model shape."""
    sample = ('<div class="flex items-center justify-between gap-3 mb-4">'
              '<h2 class="text-lg font-semibold">Section</h2>'
              '<button>Add</button>'
              '</div>'
              '<div class="bg-card rounded-lg border border-line p-5">body</div>')
    assert _section_header_row_sites(sample), "the classic heading-then-card shape was not caught"

    already_inside = ('<div class="bg-card rounded-lg p-4 border border-line">'
                       '<h3 class="text-sm font-medium">Title</h3>'
                       '<div class="inline-flex rounded border border-line p-0.5 bg-page">tabs</div>'
                       '</div>')
    assert not _section_header_row_sites(already_inside), (
        "a heading already inside its own card wrongly matched an unrelated "
        "sibling control as if it were a following section card")


# Seeded 2026-09-17 by RUNNING _section_header_row_sites() against every
# template, immediately after WP-6 step 13 -- per C28, never copied from
# the spec's own "19" expectation. Measured: 24, across 13 files.
#
# Investigated by hand rather than accepted blindly (24 vs 19 is a ~26%
# overshoot): an early pass measured 30, then 28, and both turned out to
# include real detector bugs, fixed before this baseline was taken --
#   (1) no bound on how far past a heading the scan searched, so in a
#       heading-DENSE file (server_detail.html: 17 headings,
#       settings.html: 13, per test_design_headings.py's HEADING_BASELINE)
#       one real card was counted against several nearby headings whose
#       search windows all happened to reach it -- fixed by never reading
#       past the NEXT heading's own close;
#   (2) a heading ALREADY inside its own card (the D2-compliant shape) had
#       no way to be excluded, so an unrelated sibling control a few
#       elements later (e.g. server_detail.html:311's Failed-Login-Heatmap
#       <h3>, followed by a week-selector pill) was misread as "the
#       heading's card" -- fixed by _heading_already_inside_a_panel above.
# Every one of the 24 remaining sites was checked by hand against the
# actual file (not merely trusted from the regex): each is a genuine
# heading -> [optional controls/prose] -> card sequence, including the
# ones with the largest raw character gaps (a wordy i18n-heavy controls
# cluster or an intervening <p> undertext, not a detector miss --
# services_table.html:156's 455-character gap is a heading, an explanatory
# <p> already separately governed by HEADING_UNDERTEXT_BASELINE, and then
# its card). The residual gap against the spec's "19" is consistent with
# C28's own documented pattern of different detectors landing on different
# numbers for nominally the same shape (124 vs 131 for CARD_EXCEPTIONS'
# own count), plus 13 further implementation steps since that number was
# taken.
# Lowered 2026-09-17 by WP-6 step 15 -- RE-RUN, not hand-computed. Every
# one of the eight settings-family entries below (settings.html and seven
# partials) reached exactly zero: converting a "heading outside, then its
# card" site to {% call card(...) %} removes this shape BY CONSTRUCTION
# (the macro's own head row puts the heading INSIDE the card), so every
# such site this step touched is gone, not merely reduced. All eight
# entries are deleted rather than kept at 0, matching this ratchet's own
# convention. _detection.html, _rbac.html and _server_config.html were
# never in this dict to begin with (0 before, 0 after -- see this file's
# own header comment on why the detector's 600-char window and its
# already-inside-a-panel guard made those three vacuous for this exact
# shape, not exempt by any rule).
# Lowered 2026-09-17 by WP-6 step 16 -- RE-RUN, not hand-computed.
# monitoring.html's Alert Fatigue h2 (outside its card) and operations.html's
# three h2s (Runbooks, System & Tools, Data Management -- all previously
# outside their own bg-card div) all converted to {% call card(...) %},
# which removes this "heading outside, then its card" shape BY
# CONSTRUCTION exactly as step 15's own comment above already documents.
# Both entries reached exactly zero and are deleted.
# Lowered 2026-09-17 by WP-6 step 17 -- RE-RUN, not hand-computed.
# server_detail.html falls from 5 to 2: Current Metrics (its p-8 placeholder
# is now empty_state(card=true), so no card-shaped div-open follows the
# heading in source text any more), 24h Trend Chart (converted to
# card(heading=...)) and Config Changes (converted to
# card(flush=true, heading=..., controls=...)) all resolve BY CONSTRUCTION.
# Security and Dependencies remain (their own p-4 cards were deliberately
# NOT converted -- this step's own report explains the C-7/signal-icon
# conflict) -- both real, measured, not detector artifacts: an earlier
# over-long in-file comment on the Security site pushed its own card past
# this detector's 600-char window and briefly measured as 0, caught by
# re-running the detector rather than trusting the first number, and fixed
# by shortening the comment rather than the baseline.
SECTION_HEADER_BASELINE: dict[str, int] = {
    "partials/services_table.html": 1,
    "server_detail.html": 2,
    "workflows.html": 1,
}

SECTION_HEADER_TOTAL = 4


def _section_header_counts() -> dict[str, int]:
    per_file: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        hits = _section_header_row_sites(text)
        if hits:
            per_file[rel] = len(hits)
    return per_file


def test_the_section_header_row_is_gone():
    """C-11. Ratchet, driven toward 0 by steps 15-20 (every conversion to
    {% call card(...) %} removes the "heading outside, then its card"
    shape by construction -- the macro's head row puts the heading INSIDE
    the card). Not lowered here: this step converts zero call sites."""
    counts = _section_header_counts()
    grew = [f"{f}: {n} (baseline {SECTION_HEADER_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > SECTION_HEADER_BASELINE.get(f, 0)]
    assert not grew, "a new heading-then-card (pre-macro) site appeared:\n  " + "\n  ".join(grew)


def test_the_section_header_baseline_is_not_left_behind():
    counts = _section_header_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in SECTION_HEADER_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER heading-then-card sites than the "
        "baseline; lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_section_header_row_outside_the_templates_that_already_have_one():
    new = sorted(set(_section_header_counts()) - set(SECTION_HEADER_BASELINE))
    assert not new, f"new template with a heading-then-card (pre-macro) site: {new}"


def test_the_total_number_of_section_header_rows_never_rises():
    counts = _section_header_counts()
    total = sum(counts.values())
    assert total <= SECTION_HEADER_TOTAL, f"total rose to {total} (was {SECTION_HEADER_TOTAL})"
    assert total == SECTION_HEADER_TOTAL, (
        f"total fell to {total}; lower SECTION_HEADER_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ══════════════════════════════════════════════════════════════════════════
# Beyond C-1..C-11: three small, targeted tests pinning decisions Part IV
# names explicitly and this step's own instructions call out by name.
# ══════════════════════════════════════════════════════════════════════════

def test_subhead_has_no_rule_parameter():
    """Part IV, verbatim: "subhead(rule=true) does NOT exist" -- the
    divider between sub-blocks is subblock(rule=true) (a wrapper), never
    something drawn ON a heading element, so that "no mt-*/top-margin on a
    heading, ever" stays an ABSOLUTE rule with zero exceptions. Source-level
    check on the macro's own declared signature, not just "nobody happens
    to call it that way yet"."""
    src = CARD_HTML.read_text(encoding="utf-8")
    m = re.search(r"\{%\s*macro\s+subhead\(([^)]*)\)\s*%\}", src, re.S)
    assert m, "subhead macro definition not found in _card.html"
    assert "rule" not in re.findall(r"(\w+)\s*=", m.group(1)), (
        f"subhead() declares a rule= parameter -- it must not: {m.group(1)!r}")

    m2 = re.search(r"\{%\s*macro\s+subblock\(([^)]*)\)\s*%\}", src, re.S)
    assert m2, "subblock macro definition not found in _card.html"
    assert "rule" in re.findall(r"(\w+)\s*=", m2.group(1)), (
        "subblock() must declare rule= -- it is the wrapper the divider "
        "belongs to (C8)")


def test_controls_and_footer_render_without_a_safe_filter():
    """§4.3: controls/footer are pre-rendered Markup (from a call-site
    {% set x %}...{% endset %}), so {{ controls }}/{{ footer }} render
    correctly WITHOUT |safe -- and _card.html must never add one (a |safe
    here would strip the escaping {% set %}...{% endset %} already
    applied to whatever the caller interpolated, a real XSS hole)."""
    src = _code_only(CARD_HTML.read_text(encoding="utf-8"))
    assert "|safe" not in src, "_card.html must never use the |safe filter (see §4.3)"

    html = render(
        CARD_IMPORT +
        '{% set _c %}<button data-x="&amp;">Click</button>{% endset %}'
        '{% set _f %}<a href="/x">Link</a>{% endset %}'
        '{% call card(heading="T", controls=_c, footer=_f) %}body{% endcall %}')
    assert '<button data-x="&amp;">Click</button>' in html, (
        "controls markup was escaped instead of rendered -- Markup from "
        "{% set %}...{% endset %} should render as real HTML")
    assert '<a href="/x">Link</a>' in html, "footer markup was escaped instead of rendered"


def test_tip_btn_is_declared_but_the_real_tip_button_comes_from_tip_html():
    """Documents the TIP_BTN judgement call (see this file's own module
    docstring): the constant exists verbatim per §4.2, but card()/subhead()
    render their tip buttons via partials/_tip.html's tip_button(), whose
    real, shipped, already-tested class string is different (no `rounded`,
    no `transition-colors` -- _tip.html's own header comment argues both
    by name, a cascade-order concern with Tailwind's sheet loading after
    it). This pins that the divergence is deliberate and documented, not
    an oversight that will surprise the next reader."""
    src = CARD_HTML.read_text(encoding="utf-8")
    assert 'TIP_BTN' in src and TIP_BTN in src, "TIP_BTN constant is missing or edited"

    html = render(
        CARD_IMPORT +
        '{% call card(heading="T", tip_desc_key="d", tip_desc_fallback="Explain") %}x{% endcall %}')
    btn = re.search(r'<button[^>]*class="([^"]*)"[^>]*data-tip-title', html)
    assert btn, f"card() with a tip did not render a data-tip-title button: {html!r}"
    assert btn.group(1) != TIP_BTN, (
        "the rendered tip button now uses TIP_BTN's class string -- if this "
        "was intentional, _tip.html's own cascade-order argument against "
        "`rounded`/`transition-colors` (its header comment) needs revisiting "
        "too, not just this assertion")
    assert "Explain" in html, "the tip's sr-only mirror text is missing"
