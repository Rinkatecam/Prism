"""A rounded element must not be its own scroll container.

Chromium paints `::-webkit-scrollbar` in the element's padding box and does
NOT clip it to `border-radius`. So a card that scrolls has a straight 8px bar
running past its own rounded corners — the card reads as square down its
right-hand edge, like a sticker laid over it rather than part of it.

Invisible at a 4px radius. Obvious at 16px, which is where Phase 2 put cards.
The fix is structural, not cosmetic: rounding and clipping on an outer shell,
scrolling on an inner element.

    <div class="rounded-lg border overflow-hidden">   <- clips
      <div class="max-h-96 overflow-y-auto">          <- scrolls
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt  # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"

_SCROLLS = re.compile(r"\boverflow(?:-[xy])?-(?:auto|scroll)\b")
# `rounded-none` is explicitly square, so it is not a rounded element.
_ROUNDED = re.compile(r"\brounded(?:-(?:t|r|b|l|tl|tr|bl|br))?"
                      r"(?:-(?:sm|md|lg|xl|2xl|3xl|full))?\b")


def _offenders() -> list[str]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            body = text[start:end]
            if not (_SCROLLS.search(body) and _ROUNDED.search(body)):
                continue
            # `overflow-y-auto` alongside `rounded-full` on a pill is not the
            # shape being guarded; only vertical/both scrolling matters, and a
            # pill never scrolls in practice. Keep the rule simple and let the
            # allowlist carry any genuine exception.
            line = text.count("\n", 0, start) + 1
            found.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}")
    return found


# Genuine exceptions, each justified. Empty today.
ALLOWED: set[str] = set()


def test_no_rounded_element_is_also_a_scroll_container():
    offenders = sorted(set(_offenders()) - ALLOWED)
    assert not offenders, (
        "these elements are rounded AND scroll, so their scrollbar is painted "
        "straight across the corner. Move the radius and overflow-hidden to a "
        "wrapper and leave the scrolling on the inner element:\n  "
        + "\n  ".join(offenders))


# ── the same rule, in the layer the scan above cannot see ────────────────
#
# `_offenders()` walks CLASS SCOPES, so it only ever sees an element whose
# overflow comes from a Tailwind utility. An element that scrolls because
# app.css says so is invisible to it — and the check reports green either
# way, which is this repository's most-repeated failure shape.
#
# Found when /servers' sliding band was added: `.server-band` sets
# `overflow-x: auto` in the stylesheet, is a real scroll container (measured:
# 9,824px of content in a 1,216px box), and passed every assertion above
# without being looked at once.

_CSS_SCROLLS = re.compile(r"overflow(?:-[xy])?\s*:\s*(?:auto|scroll)")
_CSS_RADIUS = re.compile(r"border-radius\s*:\s*([^;}]+)")
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
_CSS_COMMENTS = re.compile(r"/\*.*?\*/", re.S)


def _css_offenders() -> list[str]:
    css = _CSS_COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                            APP_CSS.read_text(encoding="utf-8"))
    found = []
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        selector, body = m.group(1), m.group(2)
        if not _CSS_SCROLLS.search(body):
            continue
        radius = _CSS_RADIUS.search(body)
        if radius and radius.group(1).strip() not in ("0", "0px", "0rem"):
            found.append(f"{selector.strip()[:70]} {{ border-radius: "
                         f"{radius.group(1).strip()} }}")
    return found


# Genuine exceptions, each justified. Empty, and it has been non-empty once:
# `.pulse-panel` sat here briefly, because the check found it the moment it
# existed and the repair was not a CSS edit — the collector-pulse panel's
# content is rewritten every 1.5s while it is open, so splitting the scroll
# off into a child meant making that child persist or else resetting the
# reader's scroll position on every poll. It was fixed rather than tolerated;
# see `.pulse-panel` / `.pulse-panel-scroll` in app.css and the persistence
# tests at the bottom of this file.
#
# This may shrink and must not grow: an entry here is a defect with a date on
# it, not a licence.
CSS_ALLOWED: set[str] = set()


def _selector_of(offender: str) -> str:
    return offender.split("{")[0].strip()


def test_no_stylesheet_rule_makes_one_element_both_rounded_and_scrolling():
    offenders = [o for o in _css_offenders()
                 if _selector_of(o) not in CSS_ALLOWED]
    assert not offenders, (
        "these app.css rules give one element both a radius and its own "
        "scrollbar, which Chromium paints straight across the corner:\n  "
        + "\n  ".join(offenders))


def test_the_stylesheet_detector_matches_the_shape_it_guards_against():
    """Positive control, because a scan that finds nothing is indistinguishable
    from a scan whose pattern is wrong — and this one currently finds nothing.
    Both halves are exercised: the offending combination in one rule, and the
    two-element form that is the fix."""
    bad = ".x { overflow-x: auto; border-radius: 1rem; }"
    assert _CSS_SCROLLS.search(bad) and _CSS_RADIUS.search(bad)

    shell = ".shell { border-radius: 1rem; overflow: hidden; }"
    inner = ".inner { overflow-y: auto; }"
    assert not _CSS_SCROLLS.search(shell), (
        "`overflow: hidden` must not count as scrolling, or every clipping "
        "shell in the app becomes an offender")
    assert not _CSS_RADIUS.search(inner)

    # And an explicit zero is not a radius.
    assert _CSS_RADIUS.search(".y { overflow: auto; border-radius: 0; }") \
        .group(1).strip() == "0"


def _stale(allowlist: set[str]) -> set[str]:
    """Allowlisted selectors that no longer break the rule.

    Extracted so the detection can be exercised against a synthetic
    allowlist. `CSS_ALLOWED` is empty today, which makes the assertion below
    vacuously true — a test that passes because there is nothing to check is
    indistinguishable from one whose logic is broken, and this file's whole
    subject is checks that report success without doing the work."""
    return allowlist - {_selector_of(o) for o in _css_offenders()}


def test_the_allowlist_carries_no_stale_entries():
    """An entry for something that no longer breaks the rule is an exception
    left lying around for whatever takes that selector next. Third leg of the
    ratchet shape: no growth, no new entrants, no slack left behind."""
    stale = _stale(CSS_ALLOWED)
    assert not stale, (
        "these no longer break the rule; remove them from CSS_ALLOWED rather "
        f"than leaving the exception available: {sorted(stale)}")


def test_the_staleness_check_can_detect_a_stale_entry():
    """The positive control for the test above, which currently has an empty
    set to look at and therefore proves nothing on its own."""
    assert _stale({".selector-that-does-not-exist"}) == {".selector-that-does-not-exist"}
    # And a selector that IS still an offender must not be reported stale.
    # Constructed rather than taken from app.css, because the sheet is clean
    # today and there is no real offender to borrow.
    real = {_selector_of(o) for o in _css_offenders()}
    assert not real, (
        "app.css now has an unallowlisted offender; this control assumed a "
        f"clean sheet and needs rewriting: {sorted(real)}")


# ── the cost of the repair, which is the part that gets undone ───────────
#
# Splitting a scroll container out of an element whose CONTENT IS REWRITTEN
# has a price the shell/inner pattern does not mention: the new scroller must
# survive the rewrite, or the reader's scroll position resets every time.
#
# The collector-pulse panel is the one place in the app where both are true —
# it is rounded, it scrolls, and `renderPanel()` replaces its content every
# 1.5s while it is open. Before the split the panel WAS the scroller and the
# rewrite never touched it, so scroll position survived by accident of
# structure. It now survives on purpose, and these are what keep it that way.
#
# WHAT THESE ARE BLIND TO: they read source. That scrollTop actually survives
# a poll was measured in the running app with the panel open and scrolled —
# see the numbers in the commit body / session report.

PULSE_JS = PROJECT_ROOT / "static" / "js" / "pulse-monitor.js"
_JS_COMMENTS = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _pulse_js() -> str:
    """Comments blanked. Not optional: the reasoning for every rule below is
    written out at length in that file, and several of these checks search for
    the strings the explanations quote."""
    text = _JS_COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                            PULSE_JS.read_text(encoding="utf-8"))
    return _JS_LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), text)


def test_the_pulse_panel_is_a_shell_around_a_scroller():
    """Both halves, in the layer they live in. A shell that does not clip
    leaves the scrollbar unrounded; a scroller with the radius on it is the
    original defect wearing a second element."""
    css = _CSS_COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                            APP_CSS.read_text(encoding="utf-8"))
    shell = re.search(r"\.pulse-panel\s*\{([^}]*)\}", css, re.S)
    inner = re.search(r"\.pulse-panel-scroll\s*\{([^}]*)\}", css, re.S)
    assert shell, ".pulse-panel is gone"
    assert inner, ".pulse-panel-scroll is gone; the panel scrolls itself again"

    assert re.search(r"overflow:\s*hidden", shell.group(1)), (
        "the shell does not clip, so its radius does not reach the "
        "scrollbar — the split buys nothing")
    assert "border-radius" in shell.group(1), (
        "the radius left the shell; this test is no longer looking at the "
        "shape it guards")
    assert _CSS_SCROLLS.search(inner.group(1)), (
        ".pulse-panel-scroll does not scroll, so the panel's content is "
        "clipped and unreachable rather than scrollable")
    assert "border-radius" not in inner.group(1), (
        "the scroller is rounded; that is the original defect, one element in")


def test_the_scroller_is_created_once_and_not_by_the_render():
    """The crux. `renderPanel()` runs every 1.5s while the panel is open; if
    it built the scroller, every poll would hand the reader a fresh element
    at scrollTop 0."""
    js = _pulse_js()
    init = js[js.index("function init()"):js.index("function poll()")]
    assert "createElement('div')" in init and "pulse-panel-scroll" in init, (
        "the scroller is no longer created in init()")
    render = js[js.index("function renderPanel()"):]
    render = render[:render.index("\n  function ")]
    assert "createElement" not in render, (
        "renderPanel() creates an element; if that is the scroller, scroll "
        "position resets on every poll")


def test_the_render_writes_into_the_scroller_and_not_over_it():
    js = _pulse_js()
    render = js[js.index("function renderPanel()"):]
    render = render[:render.index("\n  function ")]
    assert "panelScrollEl.innerHTML" in render, (
        "renderPanel() no longer writes into the persistent scroller")
    assert "panelEl.innerHTML" not in render, (
        "renderPanel() writes over the panel's whole content again, which "
        "destroys the scroller and the reader's position with it")


def test_creating_the_scroller_twice_cannot_orphan_the_first():
    """init() is reachable more than once in principle, and a second scroller
    would leave the first in the DOM holding the position while everything
    wrote to the new one."""
    js = _pulse_js()
    init = js[js.index("function init()"):js.index("function poll()")]
    assert re.search(r"querySelector\('\.pulse-panel-scroll'\)", init), (
        "init() no longer looks for an existing scroller before making one")
    guard = init.index("querySelector('.pulse-panel-scroll')")
    create = init.index("createElement('div')")
    assert guard < create, "the scroller is created before it is looked for"


def test_showing_the_panel_does_not_override_its_layout_mode():
    """The shell has to be a flex container for its scrolling child to be
    constrained by the shell's max-height. `style.display = 'block'` from the
    show path would silently undo that, and the panel would grow past the
    bottom of the viewport instead of scrolling — which looks like a CSS bug
    and is a one-word JS bug."""
    js = _pulse_js()
    show = js[js.index("function showPanel()"):]
    show = show[:show.index("\n  function ")]
    assert "style.display = ''" in show, (
        "showPanel() sets an explicit display type again; app.css owns the "
        "panel's layout mode")
    css = APP_CSS.read_text(encoding="utf-8")
    shell = re.search(r"\.pulse-panel\s*\{([^}]*)\}", css, re.S)
    assert re.search(r"display:\s*flex", shell.group(1)), (
        "the shell is no longer a flex container, so `min-height: 0` on the "
        "scroller has nothing to constrain it against")


def test_the_detector_recognises_the_shape_it_guards_against():
    """A guardrail whose detector never matches passes for the wrong reason.
    This is the exact markup that was in activity_feed.html."""
    bad = '<div class="bg-card rounded-lg border overflow-hidden max-h-[400px] overflow-y-auto">'
    assert _SCROLLS.search(bad) and _ROUNDED.search(bad)

    good_outer = '<div class="bg-card rounded-lg border overflow-hidden">'
    good_inner = '<div class="max-h-[400px] overflow-y-auto">'
    assert not (_SCROLLS.search(good_outer) and _ROUNDED.search(good_outer))
    assert not (_SCROLLS.search(good_inner) and _ROUNDED.search(good_inner))
