"""WP-2 — the heart monitor at the centre of the dashboard.

THE OWNER'S COMPLAINT, which is the whole brief: "97% answers nothing." The
circle's readout was a percentage, a count and a state word — three ways of
saying how much rather than one way of saying what. It is replaced by the word
ESTATE, a heart coloured by severity, and the ECG trace running through it; the
numbers move behind one gesture, together with the things that actually answer
the question — the rail sentence naming a machine, the biggest contributors, and
why the reading is not worse.

FOUR RULES THIS FILE PINS, each of which is easy to lose:

  * COLOUR CARRIES THE FULL TRUTH, and motion means NEW. The heart beats only
    while a change is unacknowledged (ratified), and "acknowledged" means
    somebody opened the detail — the one gesture that shows what is wrong. A
    reduced-motion reader therefore loses nothing, because the state was never
    in the motion.
  * COLOUR IS NEVER THE ONLY CARRIER. A heart that is red and says nothing is
    unreadable to a screen reader and to anyone who cannot tell red from amber.
    The severity word survives as live text even though it left the face of the
    circle.
  * THE CIRCLE STAYS UN-HIT-TESTABLE except for the heart itself. `.vitals-core`
    covers all four quadrant cards and two of them show their reason on hover;
    making the whole disc clickable would eat that. Only a small target dead
    centre becomes interactive.
  * ONE CLOCK. The squeeze and the trace are driven from the same animation
    frame, so the heart cannot beat out of step with the R spike it is supposed
    to be beating on. Two clocks is how they drift.

WHY THE BEAT IS OBSERVABLE FROM THE DOM. `data-beating` and
`data-unacknowledged` exist so this behaviour can be checked deterministically
rather than by sampling an animation. In an automated browser pane CSS
transitions never advance and requestAnimationFrame is throttled to under one
frame per second, so "did it animate" is not a question that can be answered by
looking — the instrument is the throttle. An attribute that says what the loop
decided is answerable. (docs/OPS-LEARNINGS.md, browser traps.)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DASHBOARD = PROJECT_ROOT / "templates" / "dashboard.html"
CSS = PROJECT_ROOT / "static" / "css" / "app.css"
JS = PROJECT_ROOT / "static" / "js" / "vitals-monitor.js"

_SEVERITIES = ("calm", "elevated", "urgent", "flat", "idle", "unmeasured")


_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
# Whole-line `//` only. A mid-line rule would eat the `//` in every URL and
# hide the code after it — see docs/OPS-LEARNINGS.md §2.5 #31.
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    """Blank comments, keeping line numbers true.

    Load-bearing, and learned the hard way in this very file: the check that
    forbids `innerHTML` fired on two comments — htmx's `morph:innerHTML` swap
    strategy, and the comment explaining why innerHTML is forbidden. A check
    that cannot tell code from commentary fires on its own rationale, and the
    cheapest way to make it green is to delete the rationale. That is the sixth
    occurrence of this shape in the repo (OPS-LEARNINGS §2.2), so the answer is
    the same helper the neighbouring suite already uses.
    """
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _dash() -> str:
    return _code_only(DASHBOARD.read_text(encoding="utf-8"))


def _css() -> str:
    return _code_only(CSS.read_text(encoding="utf-8"))


def _js() -> str:
    return _code_only(JS.read_text(encoding="utf-8"))


def _strip_at_blocks(css: str) -> str:
    """CSS with every `@media`/`@supports` block removed, braces balanced.

    Needed because a selector appears in more than one rule: `.vitals-heart`
    has a base rule AND an override inside the reduced-motion block. A helper
    that returned "the first rule with this selector" returned whichever came
    first in the FILE, which is the reduced-motion one — so an assertion about
    the base rule was quietly reading a different rule and failing for the wrong
    reason. It cost a debugging round before this existed.
    """
    out = []
    i = 0
    while i < len(css):
        at = css.find("@", i)
        if at == -1:
            out.append(css[i:])
            break
        head = css[at:at + 10]
        if not (head.startswith("@media") or head.startswith("@supports")):
            out.append(css[i:at + 1])
            i = at + 1
            continue
        out.append(css[i:at])
        brace = css.find("{", at)
        if brace == -1:
            break
        depth, j = 1, brace + 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        i = j
    return "".join(out)


def _rule(css: str, selector: str) -> str:
    """The body of one BASE CSS rule (outside any at-block), by exact selector."""
    base = _strip_at_blocks(css)
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", base, re.S)
    assert m, f"no base rule for {selector}"
    return m.group(1)


def _reduced_motion_block(css: str) -> str:
    at = css.index("@media (prefers-reduced-motion: reduce)")
    brace = css.index("{", at)
    depth, j = 1, brace + 1
    while j < len(css) and depth:
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
        j += 1
    return css[brace + 1:j - 1]


# ── the face of the circle ────────────────────────────────────────────────

def test_the_circle_shows_the_word_estate():
    assert "vitals-core-label" in _dash()
    assert "vitals_estate" in _dash()


def test_the_circle_has_a_heart():
    dash = _dash()
    assert "vitals-heart" in dash, "the heart is missing from the circle"
    assert "vitals-heart-shape" in dash, (
        "the heart has no shape element to colour and squeeze")


def test_the_percentage_left_the_face_of_the_circle():
    """The owner's actual complaint. A number that answers "how much" where the
    question is "what is wrong" is worse than no number, because it looks like
    an answer."""
    dash = _dash()
    body = dash[dash.index('id="estate-vitals"'):]
    face = body[:body.index("vitals-detail")]
    assert "vitals-core-percent" not in face, (
        "the percentage is still on the face of the circle")


def test_the_numbers_moved_into_the_detail_rather_than_being_deleted():
    """"Not deleted" is part of the spec: an operator who wants the count must
    still be able to get it, in one gesture."""
    dash = _dash()
    detail = dash[dash.index('id="estate-vitals-detail"'):]
    assert "data-vitals-percent-out" in detail
    assert "data-vitals-count-out" in detail


# ── the one gesture ───────────────────────────────────────────────────────

def test_the_heart_is_a_real_button():
    """Not a div with a click handler. A button is focusable, is reachable by
    keyboard, is announced as actionable, and fires on Enter and Space without
    any of that being reimplemented."""
    dash = _dash()
    m = re.search(r"<button[^>]*vitals-heart[^>]*>", dash)
    assert m, "the heart is not a <button>"
    tag = m.group(0)
    assert 'type="button"' in tag, (
        "a button inside no form still defaults to submit in some browsers")
    assert "aria-expanded" in tag, "the toggle does not say whether it is open"
    assert 'aria-controls="estate-vitals-detail"' in tag, (
        "the button does not name the region it controls")


def test_the_detail_starts_closed():
    dash = _dash()
    m = re.search(r'<[^>]*id="estate-vitals-detail"[^>]*>', dash)
    assert m, "no detail region"
    assert "hidden" in m.group(0), "the detail is open on first paint"


def test_only_the_heart_becomes_hit_testable():
    """The pair that has to hold together. `.vitals-core` covers all four
    quadrant cards, two of which show their reason on hover — so the disc stays
    transparent to the pointer and only the heart takes it back."""
    css = _css()
    assert re.search(r"pointer-events:\s*none", _rule(css, ".vitals-core")), (
        "the whole circle is hit-testable again; it will swallow hover meant "
        "for the cards underneath")
    assert re.search(r"pointer-events:\s*auto", _rule(css, ".vitals-heart")), (
        "the heart cannot be clicked, so the numbers are unreachable")


def test_the_detail_is_hit_testable_too():
    """It contains text an operator will want to select, and it sits inside a
    container that refuses the pointer."""
    assert re.search(r"pointer-events:\s*auto", _rule(_css(), ".vitals-detail"))


# ── colour is never the only carrier ──────────────────────────────────────

def test_the_severity_word_survives_as_live_text():
    """The heart's colour is the glanceable carrier, and it cannot be the only
    one: a screen reader gets nothing from a fill, and neither does a reader who
    cannot separate red from amber. The word stays, announced when it changes."""
    dash = _dash()
    m = re.search(r'<[^>]*data-vitals-state-out[^>]*>', dash)
    assert m, "the severity word is gone entirely"
    region = dash[dash.index('id="estate-vitals"'):]
    assert 'aria-live="polite"' in region, (
        "a severity change is never announced to a screen reader")


def test_the_heart_button_names_the_state_it_is_showing():
    """An icon button labelled only by its icon is unlabelled."""
    dash = _dash()
    m = re.search(r"<button[^>]*vitals-heart[^>]*>", dash)
    assert re.search(r"aria-label|aria-labelledby", m.group(0)), (
        "the heart button has no accessible name")


def test_every_severity_colours_the_heart():
    """Driven off the severity list rather than a literal, so a sixth severity
    fails this the moment it exists instead of rendering a default-accent heart
    on a failing estate."""
    css = _css()
    for severity in _SEVERITIES:
        assert re.search(rf"\.vitals-core--{severity}\b", css), (
            f".vitals-core--{severity} has no rule, so the heart falls back to "
            "the default halo")


def test_the_heart_reuses_the_existing_severity_mapping():
    """One mapping, not two. The halo variable already resolves severity to a
    token; the heart reads the same variable rather than repeating the
    three-way choice with its own literals."""
    body = _rule(_css(), ".vitals-heart-shape")
    assert "--vitals-halo" in body, (
        "the heart names its own colours instead of reading the halo variable, "
        "so there are now two severity mappings to keep in step")


def test_the_heart_carries_no_colour_literal():
    """The global ratchet covers the file; this says it about the heart, so a
    later hex added here fails with a message about the heart."""
    css = _css()
    start = css.index(".vitals-heart")
    block = css[start:start + 2000]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), (
        "a raw colour literal appeared in the heart's rules")


# ── one clock ─────────────────────────────────────────────────────────────

def test_the_squeeze_is_driven_from_the_trace_s_own_frame():
    """Two clocks drift. A CSS keyframe animation whose duration is recomputed
    from bpm looks right and is not synchronised with the R spike the heart is
    supposed to be beating on — and nothing would ever notice, because both
    look like a beating heart."""
    js = _js()
    assert re.search(r"function paint\b", js)
    m = re.search(r"function paint\([^)]*\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate paint()"
    body = m.group(1)
    # `squeeze(` specifically — the function that computes the contraction —
    # and NOT an alternation over vaguer words. This assertion originally read
    # `squeeze|beatPhase`, and a mutation replacing the real call with
    # `heartEl.dataset.beatPhase = ...` satisfied it while removing the
    # behaviour entirely. A hedge in a pattern is a hole in the test.
    assert "squeeze(" in body, (
        "the contraction is not computed inside paint(), so the heart is "
        "running off a second clock and can drift from its own R spike")
    assert "--beat" in body, (
        "paint() computes a squeeze it never applies to the heart")


def test_the_squeeze_is_a_pure_function_of_phase():
    """So it can be reasoned about, and so a static frame is reproducible
    rather than depending on when the page happened to load."""
    js = _js()
    assert re.search(r"function\s+squeeze\s*\(\s*u\s*\)", js), (
        "squeeze() is not a function of the beat phase alone")


def test_the_squeeze_peaks_on_the_r_spike():
    """The heart must contract WITH the tall spike. Off by a fraction of a beat
    and it reads as two unrelated animations sharing a box."""
    js = _js()
    m = re.search(r"const\s+R_CENTRE\s*=\s*([0-9.]+)", js)
    assert m, "the R spike's phase is not named, so the squeeze cannot cite it"
    r_centre = float(m.group(1))
    wave = re.search(r"\[(0\.250),\s*[0-9.]+,\s*1\.00\]", js)
    assert wave, "the R deflection is no longer at the phase the squeeze assumes"
    assert abs(float(wave.group(1)) - r_centre) < 1e-9, (
        "the squeeze and the R spike disagree about where the beat is")


# ── motion means NEW ─────────────────────────────────────────────────────

def test_the_beat_state_is_observable_from_the_dom():
    """Not decoration: in an automated pane rAF is throttled below one frame a
    second and CSS transitions never advance, so "did it animate" cannot be
    answered by looking. An attribute saying what the loop decided can be."""
    js = _js()
    assert "data-beating" in js
    assert "data-unacknowledged" in js


def _animating_body(js: str) -> str:
    """The single function that decides whether anything moves.

    Asserted through this rather than through `start()` on purpose: the four
    reasons not to move (reduced motion, no beat, acknowledged, hidden tab) live
    in ONE predicate whose answer is also published to the DOM. Two copies of
    that condition would be two answers, and the attribute would eventually
    describe a decision the loop did not make.
    """
    m = re.search(r"function animating\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "there is no single animating() decision"
    return m.group(1)


def test_the_decision_to_move_is_made_in_one_place():
    js = _js()
    start = re.search(r"function start\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert start, "could not isolate start()"
    assert "animating()" in start.group(1), (
        "start() decides for itself instead of asking animating(), so the "
        "published data-beating can disagree with what the loop does")


def test_an_acknowledged_state_does_not_beat():
    js = _js()
    assert re.search(r"function\s+acknowledged\s*\(", js), (
        "there is no acknowledgement check, so the heart beats forever")
    assert "unacknowledged" in _animating_body(js), (
        "the movement decision ignores acknowledgement, so a change that has "
        "been looked at keeps beating")


def test_acknowledgement_is_keyed_on_what_is_wrong_not_just_how_bad():
    """Two different critical problems are two pieces of news. Keyed on
    severity alone, the second one would arrive already acknowledged — silent,
    at exactly the moment silence is wrong."""
    js = _js()
    m = re.search(r"function\s+ackKey\s*\([^)]*\)\s*\{(.*?)\n  \}", _js(), re.S)
    assert m, "no ackKey() function"
    body = m.group(1)
    assert "severity" in body and "rail" in body, (
        "the acknowledgement key ignores WHICH problem it is")


def test_opening_the_detail_is_what_acknowledges():
    """Asserted inside openDetail(), not across the whole file: `remember`
    merely EXISTING somewhere is what a mutation removing the call slipped past
    the first time this was written."""
    js = _js()
    m = re.search(r"function openDetail\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "there is no openDetail()"
    body = m.group(1)
    assert "remember()" in body, (
        "opening the detail does not record the acknowledgement, so the heart "
        "keeps beating at somebody who has already looked")
    assert "unacknowledged = false" in body, (
        "the in-memory flag is not cleared, so the beat only stops on reload")


def test_the_detail_never_builds_markup_out_of_a_server_name():
    """Every string in that panel contains a SERVER NAME — operator-supplied
    text that arrives through a database and an HTML attribute. Building markup
    out of it would make the most prominent element on the dashboard an
    injection sink for anyone who can add a server."""
    js = _js()
    assert "innerHTML" not in js, (
        "vitals-monitor.js assigns innerHTML; the detail's content is "
        "server-controlled text and must go in through textContent")
    m = re.search(r"function writeDetail\([^)]*\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate writeDetail()"
    assert "textContent" in m.group(1)


def test_a_calm_estate_with_nothing_wrong_is_not_news():
    """Found by looking at the running dashboard, not by reading the code. A
    calm estate beat on every fresh page load, because nothing had been
    acknowledged yet — technically consistent with the rule and wrong in
    substance. A signal that fires in the resting state is ambient, and ambient
    motion is exactly what the redesign exists to remove."""
    js = _js()
    m = re.search(r"function\s+newsworthy\s*\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "nothing distinguishes a state worth reporting from a resting one"
    body = m.group(1)
    assert "'calm'" in body, "the resting severity is not excluded"
    assert "rail" in body, (
        "calm WITH a rail is news — something is down and being explained away "
        "rather than nothing being wrong — and this ignores it")
    ack = re.search(r"function\s+acknowledged\s*\([^)]*\)\s*\{(.*?)\n  \}", js, re.S)
    assert "newsworthy()" in ack.group(1), (
        "acknowledged() does not consult it, so the resting state still beats")


def test_an_unavailable_store_fails_towards_beating():
    """Private browsing throws on localStorage. The safe direction for a signal
    that means NEW is to over-signal: a heart that beats when it did not need to
    is noise, and one that stays still when something changed is a missed
    outage."""
    js = _js()
    m = re.search(r"function\s+acknowledged\s*\([^)]*\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate acknowledged()"
    body = m.group(1)
    assert "catch" in body, "a throwing store is not handled at all"
    assert re.search(r"catch[^{]*\{[^}]*return false", body, re.S), (
        "a throwing store is treated as acknowledged, which silences the beat "
        "exactly when it cannot be verified")


def test_reduced_motion_keeps_the_colour_and_drops_the_squeeze():
    """The global rule, applied to the new carrier. Colour is the full truth, so
    a still heart is complete rather than degraded.

    Both halves are asserted, because either alone is insufficient: the JS
    branch is what actually stops the loop (no stylesheet rule can reach a
    canvas painted from rAF, or a transform written by script), and the
    stylesheet's zeroed depth is what makes a loop bug harmless rather than
    visible."""
    assert "reduceMotion" in _animating_body(_js()), (
        "the movement decision no longer checks reduced motion")
    rm = _reduced_motion_block(_css())
    assert re.search(r"\.vitals-heart\s*\{[^}]*--beat-depth:\s*0", rm), (
        "the reduced-motion block does not neutralise the squeeze depth")


def test_the_reduced_motion_override_is_not_in_a_width_query():
    """It was, for one round: the anchor used to insert it matched a
    `max-width` block first, which would have killed the beat on every narrow
    screen and left it running for the readers who asked for stillness. Both
    failures silent."""
    css = _css()
    rm = _reduced_motion_block(css)
    assert "--beat-depth: 0;" in rm
    # And nowhere else: a second copy in a width query is the defect above.
    # The semicolon is load-bearing — without it the pattern also matches the
    # base rule's `--beat-depth: 0.09`, which is a prefix of it, and the test
    # fails claiming a duplicate that does not exist.
    assert css.count("--beat-depth: 0;") == 1, (
        "the squeeze depth is zeroed in more than one at-block; check whether "
        "one of them is a width query rather than a motion query")


def test_the_still_heart_is_not_a_shrunken_heart():
    """A static frame must render the heart at rest, not frozen mid-squeeze —
    a permanently contracted heart reads as a rendering bug."""
    js = _js()
    # The mechanism, not a word: the still branch writes a literal 0 squeeze.
    # This assertion used to match on "resting", which appeared ONLY in a
    # comment — so it passed while asserting nothing, and only showed itself
    # once comments were being stripped. Exactly the failure this suite exists
    # to catch, found in the suite itself.
    assert re.search(r"setProperty\(\s*'--beat',\s*still\s*\?\s*'0'", js), (
        "nothing pins the resting frame to zero contraction, so a paused heart "
        "keeps whatever phase it stopped on")


# ── the detail answers the question the percentage did not ────────────────

def test_the_detail_names_the_machine():
    """WP-1's rail is a sentence naming a server. It is the single most useful
    thing on this dashboard and it had nowhere to appear until now."""
    dash = _dash()
    detail = dash[dash.index('id="estate-vitals-detail"'):]
    assert "vitals.rail" in detail, "the rail sentence is not rendered"


def test_the_detail_explains_why_it_is_not_worse():
    detail = _dash()[_dash().index('id="estate-vitals-detail"'):]
    assert "why_not_higher" in detail, (
        "the anti-mistrust clause is computed and never shown")


def test_the_detail_lists_the_contributors():
    detail = _dash()[_dash().index('id="estate-vitals-detail"'):]
    assert "contributors" in detail


def test_the_detail_says_when_the_reading_is_being_held():
    """`settling` means the fold is deliberately holding a cautious answer. An
    operator watching a number refuse to move deserves to be told why."""
    detail = _dash()[_dash().index('id="estate-vitals-detail"'):]
    assert "settling" in detail


# ── translations ──────────────────────────────────────────────────────────

def test_every_new_string_exists_in_every_locale():
    """The en dictionary already runs ahead of the others and a new feature is
    not the place to widen that."""
    from i18n import TRANSLATIONS
    keys = [
        "vitals_detail_toggle", "vitals_detail_cause",
        "vitals_detail_not_higher", "vitals_detail_contributors",
        "vitals_detail_settling", "vitals_detail_all_clear",
    ]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated heart-monitor strings: " + ", ".join(missing)
