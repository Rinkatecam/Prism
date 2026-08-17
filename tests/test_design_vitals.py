"""Guardrails for the dashboard's vitals quadrant and its centre circle.

The circle is the first perpetually-animating STATUS display in the app, and
it is drawn on a <canvas>. Both facts create guard-shaped holes that this
repository has fallen into before:

  * A canvas animation is INVISIBLE to every reduced-motion rule a stylesheet
    can express. `animation-duration: 0.01ms !important` has no bearing on a
    requestAnimationFrame loop, so the blanket rule in app.css — the one that
    exists precisely because per-rule compliance decays — cannot reach this.
    The compliance is a branch in JavaScript, which means nothing enforces it
    but a test.
  * The quadrant's numbers are swapped by htmx every ~5s while the canvas
    must NOT be, or the beat restarts several times a minute. That is a
    structural property of where two elements sit relative to each other,
    and it looks fine in a screenshot either way.

WHAT THESE ARE BLIND TO, stated up front because a check that cannot see the
bug still reports green:

  * They read source. That the trace actually beats, and beats faster when
    the estate degrades, was measured in the running app: 24 canvas rows
    inked at `elevated`, 28 at `urgent`, 2 at `flat`, and the flat trace
    still painted after the loop stopped. Those numbers are recorded in
    static/js/vitals-monitor.js.
  * They cannot emulate `prefers-reduced-motion`. The browser pane offers no
    way to set it, so the reduced path is asserted structurally here and has
    NOT been observed running. It is the largest untested surface in this
    feature and is named as such rather than implied to be covered.
  * Geometry. That the circle centres on the cards' junction and covers no
    text was measured at 1280 / 1920 / 2560 (offset 0,0 and zero overlaps at
    all three); nothing here measures a pixel. The one structural half of it
    — equal grid rows, which is what puts the junction and the centre in the
    same place — is asserted below.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from routes.views import _VITALS_BPM

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
VITALS_JS = PROJECT_ROOT / "static" / "js" / "vitals-monitor.js"
QUADRANT = TEMPLATES / "partials" / "vitals_quadrant.html"
DASHBOARD = TEMPLATES / "dashboard.html"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
# Whole-line `//` only. A mid-line rule would eat the `//` in every URL and
# hide the code after it — see docs/OPS-LEARNINGS.md §2.5 #31.
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    """Blank comments, keeping line numbers true.

    Load-bearing here: this file's subjects are documented at length in their
    own source, and several of the strings below (`getImageData`,
    `animation-duration`, `tabindex`) appear in those explanations. A check
    that cannot tell code from commentary fires on its own rationale, and the
    cheapest way to make it green is to delete the rationale."""
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _js() -> str:
    return _code_only(VITALS_JS.read_text(encoding="utf-8"))


def _css() -> str:
    return _code_only(APP_CSS.read_text(encoding="utf-8"))


# ── reduced motion ───────────────────────────────────────────────────────

def test_the_trace_asks_about_reduced_motion_at_all():
    """The stylesheet cannot reach a rAF loop, so this query is the ONLY
    place the preference is honoured. Without it the page passes every
    reduced-motion test in the suite and animates forever anyway."""
    js = _js()
    assert re.search(r"matchMedia\(\s*'\(prefers-reduced-motion:\s*reduce\)'\s*\)", js), (
        "vitals-monitor.js no longer reads the reduced-motion preference; no "
        "CSS rule can substitute for this one")


def test_a_reduced_motion_reader_never_starts_the_loop():
    """The check must gate the LOOP, not merely exist. `reduceMotion` read
    into a variable and then ignored is the exact shape of a compliance
    measure that reports itself as installed."""
    js = _js()
    m = re.search(r"function start\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "start() has been reshaped; re-derive what gates the loop"
    body = m.group(1)
    assert "reduceMotion" in body, (
        "start() no longer consults reduceMotion, so the preference is read "
        "and discarded")
    assert re.search(r"if\s*\([^)]*reduceMotion[^)]*\)[\s\S]{0,200}stopRaf\(\)", body), (
        "the reduced-motion branch does not stop the animation loop")
    # And nothing may start it again further down the same function.
    after = body[body.index("reduceMotion"):]
    assert after.index("return") < after.index("startRaf"), (
        "start() falls through to startRaf() after the reduced-motion "
        "branch, so the branch changes nothing")


def test_stopping_the_sweep_never_leaves_an_empty_canvas():
    """Every path that declines to animate still paints once. A blank canvas
    in the middle of the quadrant does not read as "a monitor at rest", it
    reads as a region that failed to load — which is the outcome the
    reduced-motion decision was specifically weighed against."""
    js = _js()
    m = re.search(r"function start\(\)\s*\{(.*?)\n  \}", js, re.S)
    body = m.group(1)
    guard = re.search(r"if\s*\([^)]*reduceMotion[\s\S]{0,300}?\breturn\b", body)
    assert guard, "the bail-out branch is gone"
    assert "paint(" in guard.group(0), (
        "the no-animation branch returns without painting; the canvas stays "
        "blank for reduced-motion readers, a flat estate and a hidden tab")


def test_no_reduced_motion_exemption_was_quietly_granted():
    """The exemption was CONSIDERED AND DECLINED, and the argument is written
    into the reduced-motion block in app.css. This fails if someone adds a
    rule there that re-animates the trace — not because it could never be
    right, but because it has to be argued rather than slipped in beside two
    carve-outs that were."""
    css = _css()
    block = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{",
                      css)
    assert block, "the global reduced-motion block is gone"
    # Walk to the matching brace so the scan covers the block and nothing else.
    i, depth = block.end(), 1
    while i < len(css) and depth:
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    body = css[block.end():i]
    offenders = [line.strip() for line in body.splitlines()
                 if "vitals" in line]
    assert not offenders, (
        "a vitals rule appeared inside the reduced-motion block. The trace is "
        "a status display, not a progress indicator, and the numbers beside "
        "it carry the same information — see the argument in that block:\n  "
        + "\n  ".join(offenders))


# ── energy and the canvas ────────────────────────────────────────────────

def test_the_loop_stops_when_the_tab_is_hidden():
    js = _js()
    assert re.search(r"addEventListener\(\s*'visibilitychange'", js), (
        "nothing suspends the loop when the tab goes away")
    handler = js[js.index("'visibilitychange'"):]
    assert re.search(r"document\.hidden[\s\S]{0,120}stopRaf\(\)", handler), (
        "the visibility handler does not stop the loop")
    m = re.search(r"function start\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert "document.hidden" in m.group(1), (
        "start() can re-arm the loop while the tab is hidden, so anything "
        "that calls it — a partial swap, a severity change — undoes the "
        "visibility pause")


def test_the_trace_is_never_scrolled_by_reading_pixels_back():
    """`pulse-monitor.js` scrolls its strip with getImageData/putImageData
    because it stamps unrepeatable real events. This waveform is a pure
    function of (time, bpm), so recomputing it is both cheaper and free of
    the GPU sync stall that made `willReadFrequently` necessary over there.
    A readback appearing here means someone has copied the wrong file."""
    js = _js()
    for banned in ("getImageData", "putImageData", "willReadFrequently"):
        assert banned not in js, (
            f"{banned} in the vitals trace — the waveform is recomputed per "
            "frame precisely so it never needs a pixel readback")


def test_the_waveform_is_supersampled():
    """One sample per column misses the R spike entirely at this canvas size
    — measured, 8 of 31 rows inked, i.e. a lumpy sine rather than an ECG.
    The peak has to be sampled between pixel boundaries for the polyline to
    have a vertex on it."""
    js = _js()
    assert re.search(r"SAMPLES_PER_PX\s*=\s*([2-9]|\d\d)", js), (
        "SAMPLES_PER_PX is gone or is 1; the QRS complex is narrower than a "
        "pixel at this size and will not be drawn")
    # The step must be DERIVED from it, not merely named alongside it. The
    # first version of this test asserted `SAMPLES_PER_PX = <n>` and
    # `x += step` separately, and both stayed true when `step` was pinned to
    # 1 — the constant was still there, still described in a comment, and
    # doing nothing. Caught by mutation, not by re-reading it.
    assert re.search(r"step\s*=\s*1\s*/\s*SAMPLES_PER_PX", js), (
        "the sample step is no longer derived from SAMPLES_PER_PX, so the "
        "constant documents a resolution the trace does not use")
    assert re.search(r"x\s*\+=\s*step", js), (
        "the trace loop no longer steps by fractions of a pixel, so "
        "SAMPLES_PER_PX is defined and unused")


# ── one mapping, not two ─────────────────────────────────────────────────

def test_the_severity_colour_mapping_is_not_duplicated_in_javascript():
    """The trace's ink is `currentColor`, resolved from the
    `.vitals-core--*` modifiers. A token name appearing in the JS means a
    second copy of that mapping, and the two drift silently — the CSS one is
    what a reviewer looks at and the JS one is what gets drawn."""
    js = _js()
    assert "--c-" not in js, (
        "vitals-monitor.js names a design token; the severity -> colour "
        "mapping belongs only in app.css's .vitals-core--* rules")
    assert re.search(r"getComputedStyle\(\s*canvas\s*\)\.color", js), (
        "the trace no longer reads its ink from the resolved `color` "
        "property, which is the mechanism that keeps the mapping single")


def test_every_severity_has_a_ring_colour_and_a_label():
    """Three layers have to agree on the set of severities: the Python model
    that emits one, the CSS that colours the ring, and the label map that
    names it. A severity missing from the CSS renders the default accent
    halo — a green-ish ring on a failing estate — and one missing from the
    label map renders nothing at all."""
    css = _css()
    dash = _code_only(DASHBOARD.read_text(encoding="utf-8"))
    for severity in _VITALS_BPM:
        assert re.search(rf"\.vitals-core--{severity}\b", css), (
            f"no .vitals-core--{severity} rule; that severity falls back to "
            "the default halo colour")
        assert re.search(rf"'{severity}':", dash), (
            f"'{severity}' has no entry in dashboard.html's vitals_labels, so "
            "the state word is blank whenever the estate is in it")


def test_the_state_words_are_defined_once_and_used_twice():
    """Jinja renders the word for first paint and the JS re-renders it after
    every refresh. Two literal copies of five translated strings is two
    places to update and one to forget, and the failure is a state word that
    lags the tempo it labels."""
    dash = _code_only(DASHBOARD.read_text(encoding="utf-8"))
    assert dash.count("vitals_labels") == 3, (
        "expected exactly one definition of vitals_labels and two uses (the "
        "JSON handed to the JS, and the word rendered server-side); found "
        f"{dash.count('vitals_labels')} mentions")
    assert "data-vitals-labels='{{ vitals_labels | tojson }}'" in dash
    assert "{{ vitals_labels[vitals.severity] }}" in dash


def test_every_state_word_exists_in_every_locale():
    """One key per severity, in every language. The en dict already runs 39
    keys ahead of the others and a new feature is not the place to widen
    that. Driven off `_VITALS_BPM` rather than a literal count, so a sixth
    severity fails this the moment it is added — which is how
    `unmeasured` got its five translations rather than an English word in
    five locales."""
    from i18n import TRANSLATIONS
    keys = [f"vitals_state_{s}" for s in _VITALS_BPM]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated state words: " + ", ".join(missing)


# ── the canvas must not be swapped ───────────────────────────────────────

class _Nesting(HTMLParser):
    """Records, for each element carrying an id or hx-get, the stack of
    hx-get regions it sits inside."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str | None] = []
        self.found: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        region = a.get("hx-get")
        if a.get("id"):
            self.found[a["id"]] = [r for r in self.stack if r]
        if tag not in ("br", "hr", "img", "input", "meta", "link", "source"):
            self.stack.append(region)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


def test_the_circle_is_not_inside_the_region_that_gets_swapped():
    """The one structural fact the whole feature rests on. `morph:innerHTML`
    REPLACES the elements it swaps, so a <canvas> inside /partials/vitals
    would be discarded and rebuilt on every prismRefresh — about every 5s —
    and the beat would never get past its first cycle. It would still look
    like a working trace in a screenshot."""
    p = _Nesting()
    p.feed(_code_only(DASHBOARD.read_text(encoding="utf-8")))
    assert "estate-vitals" in p.found, (
        "#estate-vitals is gone from dashboard.html; the JS no-ops silently "
        "when it cannot find it")
    inside = p.found["estate-vitals"]
    assert not inside, (
        "the circle is inside a swapped region "
        f"({', '.join(inside)}) — its canvas will be replaced on every "
        "refresh and the animation will restart from a blank buffer")


def test_the_quadrant_cards_ARE_swapped():
    """The negative control for the test above. Pinning the circle outside a
    swapped region is only half the design; if the cards stopped refreshing
    too, the numbers would freeze at page-load values and both tests would
    still pass."""
    dash = _code_only(DASHBOARD.read_text(encoding="utf-8"))
    m = re.search(r'hx-get="/partials/vitals"[^>]*hx-trigger="([^"]*)"', dash, re.S)
    assert m, "the quadrant no longer fetches /partials/vitals"
    assert "load" in m.group(1) and "prismRefresh" in m.group(1), (
        f"the quadrant's trigger is {m.group(1)!r}; it must paint on load and "
        "track prismRefresh")


def test_the_javascript_reads_the_state_after_the_swap_has_settled():
    """`afterSwap` fires while idiomorph is still reconciling the incoming
    tree, so reading attributes then races the swap. table-sort.js waits for
    settle for the same reason."""
    js = _js()
    assert "htmx:afterSettle" in js, "the JS no longer follows the swap at all"
    assert "htmx:afterSwap" not in js, (
        "reading the swapped state at afterSwap races idiomorph's "
        "reconciliation; wait for afterSettle")


# ── the two unbuilt cards ────────────────────────────────────────────────

_SOON = re.compile(r"<div[^>]*vitals-card--soon[^>]*>", re.S)


def test_a_coming_soon_card_says_why_it_is_unavailable():
    """`cursor: not-allowed` says THAT a control is unavailable; these carry
    the reason. Same rule as every other disabled control in the app, but
    invisible to the scan in test_design_disabled.py — that one matches
    <button>/<input> with a `disabled` attribute, and these are neither.

    `aria-disabled` is checked as the STYLING HOOK it is, not as an
    announcement. Read out of the accessibility tree, these cards are three
    `generic` text nodes with no disabled state: `aria-disabled` is not a
    global attribute and a <div> has no role that supports it, so assistive
    technology ignores it. What it does do is match app.css's
    `[aria-disabled="true"]` rule — measured at opacity 0.72 and
    `cursor: not-allowed` on the rendered card. The announcement is the
    visible text, which `test_the_reason_is_readable_without_a_pointer`
    below is what actually guards."""
    html = _code_only(QUADRANT.read_text(encoding="utf-8"))
    cards = _SOON.findall(html)
    assert len(cards) == 2, (
        f"expected the Network and Scan cards, found {len(cards)}; if the "
        "count changed this test is measuring the wrong thing")
    for card in cards:
        one = re.sub(r"\s+", " ", card)
        assert 'aria-disabled="true"' in one, (
            "no aria-disabled, so the card does not pick up the global "
            f"disabled styling and reads as available: {one[:110]}")
        for attr in ("data-tip-title", "data-tip-desc"):
            assert attr in one, f"no {attr}: {one[:110]}"


def test_a_coming_soon_card_is_not_focusable_but_inert():
    """A tab stop on something that does nothing is worse than no tab stop:
    it promises a control and then swallows the keypress."""
    html = _code_only(QUADRANT.read_text(encoding="utf-8"))
    for card in _SOON.findall(html):
        one = re.sub(r"\s+", " ", card)
        assert "tabindex" not in one, (
            f"a tab stop on an inert card: {one[:110]}")
        assert "data-action" not in one and "href" not in one, (
            f"the card claims to do something: {one[:110]}")


def test_the_reason_is_readable_without_a_pointer():
    """The tip mechanism is hover-only — a real gap, inherited from the
    tooltip component and recorded in test_design_disabled.py. For these two
    cards the reason is the card's entire content, so relying on hover would
    leave a keyboard or screen-reader user with a card that says nothing but
    its own title."""
    html = _code_only(QUADRANT.read_text(encoding="utf-8"))
    body = html[html.index("vitals-card--tr"):]
    assert body.count("vitals-soon-desc") == 2, (
        "a coming-soon card lost its visible description, so its only "
        "explanation is a hover tooltip")


def test_both_unbuilt_cards_describe_their_scope_in_every_locale():
    from i18n import TRANSLATIONS
    keys = ["vitals_network_tip_title", "vitals_network_tip_desc",
            "vitals_network_desc", "vitals_scan_tip_title",
            "vitals_scan_tip_desc", "vitals_scan_desc", "vitals_coming_soon"]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated: " + ", ".join(missing)


def test_the_scan_card_says_it_is_not_an_attacking_tool():
    """The owner was explicit about this. "Scan" invites the other reading,
    and the card is the only place the distinction is ever made."""
    from i18n import TRANSLATIONS
    desc = TRANSLATIONS["en"]["vitals_scan_tip_desc"].lower()
    assert "not an attack" in desc, (
        "the Scan card no longer states that it is a posture check rather "
        "than an attack tool")


# ── geometry that can be read from the source ────────────────────────────

def test_the_quadrant_scales_continuously_rather_than_in_steps():
    """1280 -> 2560 with no breakpoint ladder. A `clamp()` on `vw` grows the
    circle with the viewport; a media-query ladder would leave the design
    correct at three widths and arbitrary everywhere between them."""
    css = _css()
    m = re.search(r"--vitals-dia:\s*([^;]+);", css)
    assert m, "--vitals-dia is gone"
    assert "clamp(" in m.group(1) and "vw" in m.group(1), (
        f"--vitals-dia is {m.group(1).strip()!r}; it must track the viewport")


def test_the_two_rows_of_the_quadrant_are_equal_height():
    """This is what puts the cards' junction and the circle's centre in the
    same place. Measured with `auto` as the row max: 150px and 212px at 1280,
    a 62px difference that moved the junction 26px off the centre the circle
    is positioned on — and it shifted again whenever a card's content
    changed height."""
    css = _css()
    m = re.search(r"\.vitals-grid\s*\{([^}]*)\}", css, re.S)
    assert m, ".vitals-grid is gone"
    rows = re.search(r"grid-auto-rows:\s*([^;]+);", m.group(1))
    assert rows, ".vitals-grid no longer sizes its rows"
    assert "1fr" in rows.group(1), (
        f"grid-auto-rows is {rows.group(1).strip()!r}; with `auto` as the max "
        "each row sizes itself and the junction drifts off the circle's centre")


def test_the_circle_does_not_steal_hover_from_the_cards_it_covers():
    """It overlaps all four cards and carries no control. Left
    hit-testable, it would eat the hover on whichever corner it covers —
    including the two cards whose reason is shown on hover."""
    css = _css()
    m = re.search(r"\.vitals-core\s*\{([^}]*)\}", css, re.S)
    assert m and re.search(r"pointer-events:\s*none", m.group(1)), (
        "the circle is hit-testable; it will swallow hover and clicks meant "
        "for the cards underneath it")


def test_the_dashboard_is_not_a_fixed_width_island_on_a_wall_screen():
    """The design's top end is 2560 and that is the requirement that does not
    survive being ignored. Measured with the default column: 1056px of empty
    grey either side of the quadrant at 2560, against 208px after."""
    dash = _code_only(DASHBOARD.read_text(encoding="utf-8"))
    assert re.search(r"{%\s*block content_width\s*%}\s*page-dashboard", dash), (
        "the dashboard no longer widens its content column")
    assert re.search(r"\.page-dashboard\s*\{[^}]*max-width", _css()), (
        ".page-dashboard has no width; the block override resolves to a class "
        "that does nothing, which looks exactly like it working")
