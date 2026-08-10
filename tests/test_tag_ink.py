"""Tests for the shared tag-pill contrast solver — see tools/tag_ink.py.

WHAT THIS FILE DOES AND DOES NOT EXECUTE
-----------------------------------------
Per docs/OPS-LEARNINGS.md and tests/test_table_sort.py's own note: there is
no Node.js or any other JavaScript runtime in this environment (checked —
no selenium/playwright/js2py/PyMiniRacer), so the JS half of this feature
(`_tagReadableInk` / `renderServerTagPills` in templates/servers.html)
cannot be *run* from pytest. Re-implementing it in Python here and testing
THAT would be exactly the trap this repository's history warns about — the
test would pass forever even if the shipped JS were wrong, because it would
never look at the shipped JS.

So this file does two different things and does not blur them:

1. Structural regression guards that extract the REAL source text from
   templates/servers.html (regex, same technique as test_table_sort.py) and
   assert specific properties the earlier defects violated: no per-render
   `classList.contains('dark')` read, both ink custom properties present,
   the hover backdrop constants present, the 3-digit hex branch present,
   and the exact nudge-loop line that a real regression (an unindexed
   `target` used where `target[i]` was needed — found and fixed while
   writing this suite; see the mismatch it produces in
   `test_the_fixed_nudge_line_is_not_the_unindexed_regression`) is gone.

2. A genuine, executable agreement test between tools/tag_ink.py and the
   Jinja macros duplicated in templates/partials/server_card.html and
   templates/partials/server_grid.html — both run inside this same Python
   process, so there is no JS-runtime problem, and the two are rendered for
   real (via a standalone jinja2.Environment over the actual template
   files) rather than compared against a re-implementation.

The JS-vs-Python agreement (defect 2's "two runtimes... add a test that
runs BOTH implementations") was verified the same way test_table_sort.py
verifies DOM-dependent behaviour: against the real running app, with the
browser tooling, calling `_tagReadableInk` directly with the corpus below
plus hostile/malformed inputs. That evidence is in the task report, not
faked here as a pytest assertion — see docs/OPS-LEARNINGS.md #21/#28 on why
a measurement that cannot be executed should not be simulated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import tag_ink  # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"
SERVERS_JS = TEMPLATES / "servers.html"
SERVER_CARD = TEMPLATES / "partials" / "server_card.html"
SERVER_GRID = TEMPLATES / "partials" / "server_grid.html"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"

# A shared corpus: a grey ramp (where rest/hover backdrops matter most,
# since a near-neutral tag colour is closest to indistinguishable from its
# own backdrop), the 6 colours measured in the commit that introduced this
# solver, saturated primaries/secondaries, a spread of the app's own brand
# hues, 3-digit shorthand, and every malformed shape SMELL 4 / the original
# regex could see.
GREY_RAMP = [f"#{v:02x}{v:02x}{v:02x}" for v in range(0, 256, 17)]
NAMED = [
    "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF",
    "#800000", "#008000", "#000080", "#808000", "#800080", "#008080",
    "#C0C0C0", "#6B7280", "#2DD4BF", "#A61B1B", "#EF4444", "#F59E0B",
    "#10B981", "#3B82F6", "#8B5CF6", "#EC4899", "#14B8A6", "#F97316",
    "#64748B", "#E5E7EB", "#111827", "#F43F5E", "#0EA5E9", "#22C55E",
    "#EAB308", "#BD0912",
]
SHORTHAND = ["#abc", "#ABC", "#000", "#fff", "#f0a", "#0f0"]
MALFORMED = ["notacolor", "#12345", "#1234567", "", "rgb(1,2,3)", "   ", None, "######"]

CORPUS = GREY_RAMP + NAMED
CORPUS_WITH_EDGE_CASES = CORPUS + SHORTHAND + MALFORMED


# ── 1. the contrast helper itself (docs/OPS-LEARNINGS.md #14) ─────────────

def test_contrast_helper_is_pinned_against_known_values():
    """A contrast test built on an unverified contrast helper is decorative
    — pin it against values nobody can dispute."""
    assert tag_ink.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)
    for rgb in [(0, 0, 0), (255, 255, 255), (107, 114, 128), (45, 212, 191)]:
        assert tag_ink.contrast_ratio(rgb, rgb) == pytest.approx(1.0, abs=1e-9)


def test_contrast_helper_mutation_a_broken_ratio_formula_is_caught():
    """Mutation check: dropping the WCAG formula's `+0.05` offset (a
    difference instead of a ratio) must NOT reproduce 21:1 for black/white
    — if it did, this suite's pin would be worthless. Black and white are
    exact luminance 0 and 1 regardless of the gamma curve or channel
    weights (both are convex combinations that hit their bounds at the
    bounds), so THIS is the part of the formula a pin actually exercises."""
    def _broken_ratio(a, b):
        la, lb = tag_ink.relative_luminance(a), tag_ink.relative_luminance(b)
        return max(la, lb) - min(la, lb)

    assert _broken_ratio((0, 0, 0), (255, 255, 255)) != pytest.approx(21.0, abs=0.01)


# ── 2. hex parsing, including SMELL 4 (3-digit shorthand) ─────────────────

@pytest.mark.parametrize("shorthand,expected", [
    ("#abc", (0xAA, 0xBB, 0xCC)),
    ("#ABC", (0xAA, 0xBB, 0xCC)),
    ("abc", (0xAA, 0xBB, 0xCC)),
    ("#000", (0, 0, 0)),
    ("#fff", (255, 255, 255)),
    ("#f0a", (0xFF, 0x00, 0xAA)),
])
def test_parse_hex_expands_3_digit_shorthand(shorthand, expected):
    """The original regex (`^#?([\\da-f]{6})$`) rejected valid 3-digit CSS
    hex and fell through to the default grey — a tag using `#abc` became
    indistinguishable from one using no colour at all. This is the SMELL 4
    fix; see the identical assertion against the Jinja macros in
    test_the_jinja_macros_agree_with_python below."""
    assert tag_ink.parse_hex(shorthand) == expected
    assert tag_ink.parse_hex(shorthand) != tag_ink.DEFAULT_RGB


@pytest.mark.parametrize("bad", MALFORMED)
def test_parse_hex_declines_malformed_input_without_raising(bad):
    assert tag_ink.parse_hex(bad) == tag_ink.DEFAULT_RGB


@pytest.mark.parametrize("hx,expected", [
    ("#6B7280", (0x6B, 0x72, 0x80)),
    ("6B7280", (0x6B, 0x72, 0x80)),
    ("6b7280", (0x6B, 0x72, 0x80)),
    ("  #6b7280  ", (0x6B, 0x72, 0x80)),
])
def test_parse_hex_is_case_and_hash_and_whitespace_insensitive(hx, expected):
    assert tag_ink.parse_hex(hx) == expected


# ── 3. the solver: AA against every constraint at once ────────────────────

def test_solve_ink_clears_every_simultaneous_constraint():
    """Two backdrops with different luminance, one call: the result must
    clear 4.5:1 against BOTH, not just whichever was checked last."""
    rgb = tag_ink.parse_hex("#6B7280")
    constraints = [((255, 255, 255), 0.22), ((241, 245, 249), 0.40)]
    ink = tag_ink.solve_ink(rgb, (0, 0, 0), constraints)
    for backdrop, alpha in constraints:
        composite = tag_ink._blend(rgb, backdrop, alpha)
        assert tag_ink.contrast_ratio(ink, composite) >= tag_ink.AA_MIN_CONTRAST


def test_solve_ink_mutation_a_single_constraint_solver_fails_the_other():
    """Mutation check: solving against only the FIRST constraint (the bug
    FRAGILE 3 describes — modelling the card and ignoring the real hover
    backdrop) must fail to clear the second one for at least one colour in
    the corpus, or this suite could not tell the fixed solver from the
    broken one."""
    failures = 0
    for hx in CORPUS:
        rgb = tag_ink.parse_hex(hx)
        card = (255, 255, 255)
        row_hover = (241, 245, 249)
        tint_alpha, hover_alpha = 0.22, 0.40
        single_constraint_ink = tag_ink.solve_ink(rgb, (0, 0, 0), [(card, tint_alpha)])
        hover_composite = tag_ink._blend(rgb, row_hover, hover_alpha)
        if tag_ink.contrast_ratio(single_constraint_ink, hover_composite) < tag_ink.AA_MIN_CONTRAST:
            failures += 1
    assert failures > 0, "expected the single-constraint (pre-FRAGILE-3-fix) solve to fail at least one colour"


@pytest.mark.parametrize("hx", CORPUS_WITH_EDGE_CASES)
def test_servers_html_style_solve_clears_aa_rest_and_hover_both_themes(hx):
    """Models templates/servers.html's tag table: rest against the card,
    hover against the ROW's hover backdrop (FRAGILE 3), in both themes."""
    tokens = tag_ink.tag_pill_tokens(hx, tint_alpha=0.22, include_row_hover=True)
    rgb = tag_ink.parse_hex(hx)
    hover_alpha = min(1.0, 0.22 + 0.18)

    light_ink = tag_ink.parse_hex(tokens["ink_light"][1:])
    dark_ink = tag_ink.parse_hex(tokens["ink_dark"][1:])

    rest_light = tag_ink._blend(rgb, tag_ink.CARD_LIGHT, 0.22)
    rest_dark = tag_ink._blend(rgb, tag_ink.CARD_DARK, 0.22)
    hover_light = tag_ink._blend(rgb, tag_ink.ROW_HOVER_LIGHT, hover_alpha)
    hover_dark = tag_ink._blend(rgb, tag_ink.ROW_HOVER_DARK, hover_alpha)

    assert tag_ink.contrast_ratio(light_ink, rest_light) >= tag_ink.AA_MIN_CONTRAST
    assert tag_ink.contrast_ratio(light_ink, hover_light) >= tag_ink.AA_MIN_CONTRAST
    assert tag_ink.contrast_ratio(dark_ink, rest_dark) >= tag_ink.AA_MIN_CONTRAST
    assert tag_ink.contrast_ratio(dark_ink, hover_dark) >= tag_ink.AA_MIN_CONTRAST


@pytest.mark.parametrize("hx", CORPUS_WITH_EDGE_CASES)
def test_card_and_grid_style_solve_clears_aa_on_bg_card(hx):
    """Models server_card.html / server_grid.html: no hover backdrop change
    to solve against, just the card, in both themes."""
    tokens = tag_ink.tag_pill_tokens(hx, tint_alpha=0.0824, solve_against_hover=False)
    assert tokens["rest_ratio_light"] >= tag_ink.AA_MIN_CONTRAST
    assert tokens["rest_ratio_dark"] >= tag_ink.AA_MIN_CONTRAST


def test_the_24_step_cap_is_not_reached_by_the_shared_corpus():
    """docs/OPS-LEARNINGS.md's review found the cap never binds (max used
    13). Re-verify for THIS solver (constants/constraints changed since):
    raising the cap must not change a single result."""
    for hx in CORPUS_WITH_EDGE_CASES:
        for tint_alpha in (0.22, 0.0824):
            rgb = tag_ink.parse_hex(hx)
            for target in ((0, 0, 0), (255, 255, 255)):
                backdrop = tag_ink.CARD_LIGHT if target == (0, 0, 0) else tag_ink.CARD_DARK
                constraints = [(backdrop, tint_alpha)]
                at_cap = tag_ink.solve_ink(rgb, target, constraints, max_steps=24)
                beyond_cap = tag_ink.solve_ink(rgb, target, constraints, max_steps=48)
                assert at_cap == beyond_cap, f"{hx!r} still moving after 24 steps"


# ── 4. no NaN, ever, on malformed input (docs/OPS-LEARNINGS.md's swept 23) ─

@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_input_never_produces_nan_or_raises(bad):
    tokens = tag_ink.tag_pill_tokens(bad, tint_alpha=0.22, include_row_hover=True)
    assert re.fullmatch(r"#[0-9a-f]{6}", tokens["ink_light"])
    assert re.fullmatch(r"#[0-9a-f]{6}", tokens["ink_dark"])


# ── 5. app.css drift guard — the hardcoded backdrop constants must match
#      the tokens they model, in every one of the three implementations ──

def _extract_root_block(css_text: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css_text)
    assert m, f"could not find a `{selector} {{ ... }}` block in app.css"
    return m.group(1)


def _extract_channels(block: str, prop: str) -> tuple[int, int, int]:
    m = re.search(re.escape(prop) + r":\s*(\d+)\s+(\d+)\s+(\d+)\s*;", block)
    assert m, f"could not find {prop} in the block"
    return tuple(int(x) for x in m.groups())


def test_backdrop_constants_match_app_css_tokens():
    """CARD_LIGHT/CARD_DARK/PAGE_LIGHT/PAGE_DARK are hand-copied from
    static/css/app.css's --c-card / --c-page (this module cannot import
    CSS). If a future change to app.css moves those tokens and this module
    is not updated to match, every solve silently targets the wrong
    backdrop with no error anywhere — this is the drift guard for that."""
    css_text = APP_CSS.read_text(encoding="utf-8")
    root = _extract_root_block(css_text, ":root")
    dark = _extract_root_block(css_text, ".dark")

    assert tag_ink.CARD_LIGHT == _extract_channels(root, "--c-card")
    assert tag_ink.PAGE_LIGHT == _extract_channels(root, "--c-page")
    assert tag_ink.CARD_DARK == _extract_channels(dark, "--c-card")
    assert tag_ink.PAGE_DARK == _extract_channels(dark, "--c-page")


def test_row_hover_dark_is_the_documented_composite():
    """`dark:hover:bg-page/50` is page-at-50%-alpha over the card beneath
    the row (see tools/tag_ink.py's module docstring and FRAGILE 3)."""
    expected = tuple(
        tag_ink._js_round(0.5 * p + 0.5 * c)
        for p, c in zip(tag_ink.PAGE_DARK, tag_ink.CARD_DARK)
    )
    assert tag_ink.ROW_HOVER_DARK == expected
    assert tag_ink.ROW_HOVER_LIGHT == tag_ink.PAGE_LIGHT


# ── 6. structural regression guards on the real JS source ─────────────────
# Same technique as tests/test_table_sort.py: extract the literal text from
# the shipped file and assert properties of it, rather than re-implementing
# and testing the re-implementation.

@pytest.fixture(scope="module")
def servers_js_source() -> str:
    return SERVERS_JS.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\([^)]*\)\s*\{", source)
    assert m, f"function {name} not found in {SERVERS_JS}"
    start = m.end() - 1
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


def test_ink_solver_no_longer_reads_the_theme_class_at_render_time(servers_js_source):
    """BROKEN 1: the previous version read
    `document.documentElement.classList.contains('dark')` once per render
    and baked that theme's ink into an inline style, so toggling the theme
    without a reload left stale, unreadable ink on screen. The fix is
    architectural — solve both themes, always — so this string must not
    appear anywhere the ink is computed."""
    ink_fn = _extract_function(servers_js_source, "_tagReadableInk")
    solve_fn = _extract_function(servers_js_source, "_tagSolveInk")
    assert "classList.contains" not in ink_fn
    assert "classList.contains" not in solve_fn


def test_ink_solver_emits_both_theme_custom_properties(servers_js_source):
    render_fn = _extract_function(servers_js_source, "renderServerTagPills")
    assert "--tag-ink-l:" in render_fn
    assert "--tag-ink-d:" in render_fn
    assert "text-[var(--tag-ink-l)]" in render_fn
    assert "dark:text-[var(--tag-ink-d)]" in render_fn


def test_ink_function_returns_both_theme_inks(servers_js_source):
    ink_fn = _extract_function(servers_js_source, "_tagReadableInk")
    assert "inkLight:" in ink_fn
    assert "inkDark:" in ink_fn


def test_hex_parser_accepts_3_digit_shorthand(servers_js_source):
    """SMELL 4, JS side: extract the real parsing function and run its
    actual regexes (plain character-class regex, portable to Python `re`
    the same way test_table_sort.py's are) against the shared corpus."""
    parse_fn = _extract_function(servers_js_source, "_tagParseHex")
    six = re.search(r"/(\^#\?\(\[0-9a-f\]\{6\}\)\$)/i", parse_fn)
    three = re.search(r"/(\^#\?\(\[0-9a-f\]\{3\}\)\$)/i", parse_fn)
    assert six and three, f"expected a 6-digit and a 3-digit hex pattern in _tagParseHex:\n{parse_fn}"
    six_re = re.compile(six.group(1), re.IGNORECASE)
    three_re = re.compile(three.group(1), re.IGNORECASE)
    for shorthand in SHORTHAND:
        assert six_re.match(shorthand) or three_re.match(shorthand), shorthand


def test_hover_backdrop_constants_present(servers_js_source):
    """FRAGILE 3: the row-hover composite (not just the card) must be part
    of what the JS solves against."""
    assert "_TAG_ROW_HOVER_LIGHT" in servers_js_source
    assert "_TAG_ROW_HOVER_DARK" in servers_js_source
    ink_fn = _extract_function(servers_js_source, "_tagReadableInk")
    assert "_TAG_ROW_HOVER_LIGHT" in ink_fn
    assert "_TAG_ROW_HOVER_DARK" in ink_fn


def test_the_fixed_nudge_line_is_not_the_unindexed_regression(servers_js_source):
    """A real bug caught while building this suite: the nudge line read
    `target - c` where `target` is a 3-element array (`[0,0,0]` /
    `[255,255,255]`) and `c` a scalar channel — `[0,0,0] - 5` is `NaN` in
    JavaScript, and EVERY colour that ever needed a single nudge produced
    `#NaNNaNNaN` (confirmed live: only inputs that needed zero nudging,
    like pure black solved toward black, escaped it). The fix indexes
    `target[i]`. This guards against reintroducing the unindexed form."""
    solve_fn = _extract_function(servers_js_source, "_tagSolveInk")
    assert "target[i]" in solve_fn
    assert re.search(r"\(target\s*-\s*c\)", solve_fn) is None


# ── 7. cross-implementation agreement: tools/tag_ink.py vs the Jinja
#      macros duplicated in server_card.html and server_grid.html ─────────
# tools/tag_ink.py's module docstring explains why there are two Jinja
# copies rather than one shared filter: app.py (where a Jinja filter would
# be registered) was out of scope for this change, per file ownership, so
# the macro is a verified port instead. This is the test that verification
# depends on — it renders the REAL files, not a re-implementation.

class _Ctx(dict):
    """A dict that also answers attribute access and .get(k, default), so
    Jinja's `server.server_name` / `t.get('x', 'y')` both work against a
    plain fixture without needing the app's real request context."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            return None

    def get(self, key, default=None):
        return dict.get(self, key, default)


@pytest.fixture(scope="module")
def jinja_env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))


_TAG_INK_STYLE_RE = re.compile(r"--tag-ink-l:(#[0-9a-fA-F]{6});--tag-ink-d:(#[0-9a-fA-F]{6});")


def _render_server_card_tags(env: jinja2.Environment, tags: list[dict]) -> list[tuple[str, str]]:
    server = _Ctx(
        server_name="TESTSRV", status="healthy", cpu_percent=10, ram_percent=10,
        disk_c_percent=10, disk_d_percent=None, host="10.0.0.1", thresholds={},
        verdict_detail={}, install_state=None, tags=tags,
    )
    t = _Ctx(get=lambda k, d=None: d)
    out = env.get_template("partials/server_card.html").render(server=server, t=t, max_compare_servers=50)
    return _TAG_INK_STYLE_RE.findall(out)


def _render_server_grid_tags(env: jinja2.Environment, tags: list[dict]) -> list[tuple[str, str]]:
    t = _Ctx(get=lambda k, d=None: d)
    all_tags = [dict(tag, server_count=0) for tag in tags]
    out = env.get_template("partials/server_grid.html").render(
        t=t, all_tags=all_tags, active_tag=None, grouped={},
    )
    return _TAG_INK_STYLE_RE.findall(out)


@pytest.mark.parametrize("hx", CORPUS_WITH_EDGE_CASES)
def test_the_jinja_macros_agree_with_python(jinja_env, hx):
    expected = tag_ink.tag_ink_css_props(hx)
    m = _TAG_INK_STYLE_RE.search(expected)
    expected_light, expected_dark = m.group(1).lower(), m.group(2).lower()

    tags = [{"id": 1, "name": "probe", "color": hx if hx is not None else ""}]

    card_results = _render_server_card_tags(jinja_env, tags)
    assert card_results, "server_card.html did not render a tag pill with a --tag-ink-l style"
    card_light, card_dark = card_results[0]
    assert card_light.lower() == expected_light
    assert card_dark.lower() == expected_dark

    grid_results = _render_server_grid_tags(jinja_env, tags)
    assert grid_results, "server_grid.html did not render a tag pill with a --tag-ink-l style"
    grid_light, grid_dark = grid_results[0]
    assert grid_light.lower() == expected_light
    assert grid_dark.lower() == expected_dark


def test_the_two_jinja_render_paths_agree_with_each_other():
    """Transitively true if the above passes for every input, but asserted
    directly too: this is the literal property BROKEN 2 asked for — two
    server-side render paths, one input set, must agree with each other."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
    for hx in CORPUS_WITH_EDGE_CASES:
        tags = [{"id": 1, "name": "probe", "color": hx if hx is not None else ""}]
        card = _render_server_card_tags(env, tags)[0]
        grid = _render_server_grid_tags(env, tags)[0]
        assert card == grid, f"{hx!r}: server_card={card} server_grid={grid}"


def test_mutation_reverting_3_digit_expansion_breaks_the_agreement_test():
    """Mutation check for the agreement test itself: if the Jinja macro's
    3-digit branch were deleted (falling back to the default grey like the
    pre-SMELL-4 bug), `#abc` must stop matching tools/tag_ink.py's
    expansion — proving the agreement test would actually catch that
    regression rather than passing regardless."""
    without_expansion_default = tag_ink.tag_ink_css_props("6B7280")
    real_result = tag_ink.tag_ink_css_props("#abc")
    assert without_expansion_default != real_result


# ── 8. wiring — the fix actually landed on both Jinja render paths ────────

def test_server_card_calls_the_shared_macro_not_the_raw_colour():
    src = SERVER_CARD.read_text(encoding="utf-8")
    assert "tag_ink_props(tag.color)" in src
    assert re.search(r'style="\s*color:\s*\{\{\s*tag\.color\s*\}\}', src) is None, (
        "server_card.html still bakes the raw admin colour in as ink (BROKEN 2 regression)")


def test_server_grid_calls_the_shared_macro_not_the_raw_colour():
    src = SERVER_GRID.read_text(encoding="utf-8")
    assert "tag_ink_props(tag.color)" in src
    # Not `[-\w]color:` — that would also match the (unrelated, unchanged)
    # `border-color:` a few characters earlier in the same style attribute.
    assert re.search(r"(?<![-\w])color:\s*\{\{\s*tag\.color\s*\}\}\s*;", src) is None, (
        "server_grid.html still bakes the raw admin colour in as ink (BROKEN 2 regression)")


def test_no_new_hex_literal_utility_in_the_tag_pill_markup_itself():
    """docs/OPS-LEARNINGS.md's literal ratchet (tests/test_design_tokens.py,
    off-limits to edit for this change) counts `-[#hex]` Tailwind utilities
    per file. Diffing the whole file against HEAD is not this test's job —
    another agent is concurrently editing unrelated parts of these same
    files (e.g. server_card.html's install-state border colours), so a
    whole-file count is not attributable to this change alone. What IS this
    change's to guarantee: the macro definitions and the pill/button markup
    they feed use a CSS custom property and a `text-[var(...)]` class that
    names a *variable*, not a colour — neither is a `-[#hex]` literal."""
    literal = re.compile(r"-\[#[0-9A-Fa-f]{6}\]")
    for path in (SERVER_CARD, SERVER_GRID):
        text = path.read_text(encoding="utf-8")
        call_at = text.index("tag_ink_props(")
        macro_start = text.index("_tag_ink_solve")
        snippet = text[macro_start:call_at + 400]
        assert not literal.search(snippet), f"{path}: unexpected -[#hex] literal near the tag-ink wiring"
