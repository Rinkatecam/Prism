"""Motion and elevation — see tools/design_tokens.py MOTION / EASING / ELEVATION.

Measured before this existed:

  * SIX durations across the two layers — 0.1, 0.15, 0.2, 0.3 and 0.6s in
    app.css, and 150/200/300/500ms from Tailwind classes in the templates.
  * TWO easing systems — a bare `ease` on all 16 CSS transitions, Tailwind's
    cubic-bezier(.4,0,.2,1) on the 123 in the markup.
  * TWELVE distinct box-shadow values with no scale.
  * `prefers-reduced-motion` honoured by FIVE rules, out of 123 transitions
    and 33 animations.

None of those numbers were chosen. They are what got typed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt  # noqa: E402

APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
BASE = PROJECT_ROOT / "templates" / "base.html"


def sheet() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def authored() -> str:
    """app.css with the generated block removed — the hand-written part."""
    css = sheet()
    start = css.index(dt.GENERATED_OPEN)
    end = css.index(dt.GENERATED_CLOSE) + len(dt.GENERATED_CLOSE)
    return css[:start] + css[end:]


# ── the scale exists in all three places ─────────────────────────────────

def test_the_generated_block_carries_motion_and_elevation():
    css = sheet()
    for name, value in dt.MOTION.items():
        assert f"--dur-{name}: {value};" in css
    for name, value in dt.EASING.items():
        assert f"--ease-{name}: {value};" in css
    for name, (light, dark) in dt.ELEVATION.items():
        assert f"--shadow-{name}: {light};" in css
        assert f"--shadow-{name}: {dark};" in css


def test_the_tailwind_config_points_at_the_same_variables():
    """Both layers must read ONE scale. A template's `duration-base` and a
    stylesheet's `var(--dur-base)` resolving to different numbers is the exact
    drift this replaces — and it looks fine until one of them moves."""
    base = BASE.read_text(encoding="utf-8")
    for name in dt.MOTION:
        assert f"'{name}': 'var(--dur-{name})'" in base
    for name in dt.EASING:
        assert f"'{name}': 'var(--ease-{name})'" in base
    for name in dt.ELEVATION:
        assert f"'{name}': 'var(--shadow-{name})'" in base


def test_the_sentinels_wrap_the_generated_block():
    """The CSS converter finds generated output by these markers. It used to
    match the SHAPE of the colour block — `--c-name: <digits>` — which stopped
    protecting anything the moment the generator emitted a duration or a
    shadow, and a shadow is full of `rgb(...)` for the converter to rewrite."""
    css = sheet()
    assert css.count(dt.GENERATED_OPEN) == 1
    assert css.count(dt.GENERATED_CLOSE) == 1
    assert css.index(dt.GENERATED_OPEN) < css.index(dt.GENERATED_CLOSE)

    from tools import migrate_css_tokens as mct
    block = mct.GENERATED_BLOCK.search(css)
    assert block, "the converter cannot find the generated block"
    assert "--shadow-lg" in block.group(0), \
        "the shadows must be inside the protected region, not beside it"


# ── nothing off the scale survives ───────────────────────────────────────

_TIMED = re.compile(r"(?<![-\w])(?:transition|animation):[^;}]*")
_DURATION = re.compile(r"(?<![\w.-])(\d*\.?\d+)(m?s)(?![\w.-])")

# Continuous loops, not UI transitions. A dot breathing at 1.2s and a
# skeleton sweeping at 1.4s are signals with their own tempo; folding either
# onto a 320ms UI duration turns it into a flicker. The scale governs how
# long it takes to GET somewhere, which is a different question from how
# fast something idles.
ALLOWED_DURATIONS = {"1.2s", "1.4s", "1.8s", "2.25s"}


def test_no_transition_uses_a_duration_off_the_scale():
    offenders = []
    for decl in _TIMED.findall(authored()):
        for value, unit in _DURATION.findall(decl):
            literal = f"{value}{unit}"
            if literal not in ALLOWED_DURATIONS:
                offenders.append(f"{literal} in `{decl.strip()[:70]}`")
    assert not offenders, (
        "use var(--dur-fast|base|slow):\n  " + "\n  ".join(offenders))


def test_no_transition_uses_an_easing_off_the_scale():
    """`ease` — the keyword all 16 declarations used — accelerates INTO the
    end of the movement, so things arrive with a small bump. Both scale
    curves decelerate, which is what makes a panel look like it settled."""
    offenders = []
    for decl in _TIMED.findall(authored()):
        for bad in re.findall(r"(?<![-\w])(ease|ease-in|linear)(?![-\w(])", decl):
            offenders.append(f"{bad} in `{decl.strip()[:70]}`")
    assert not offenders, (
        "use var(--ease-standard) or var(--ease-out):\n  " + "\n  ".join(offenders))


def test_elevation_shadows_come_from_the_scale():
    """A GLOW is not elevation. `0 0 6px rgb(var(--c-critical) / .8)` is a
    colour signal that happens to use box-shadow — zero offset, zero spread —
    and it is exempt on that basis rather than by name."""
    offenders = []
    for decl in re.findall(r"box-shadow:[^;}]*", authored()):
        value = decl.split(":", 1)[1].strip()
        if value in ("none",) or "var(--shadow-" in value:
            continue
        if re.match(r"^0 0 \d", value):          # a glow: no offset
            continue
        if "124, 58, 237" in value or "var(--c-brand)" in value:
            continue                              # brand-tinted button lift
        offenders.append(value[:80])
    assert not offenders, (
        "use var(--shadow-sm|md|lg):\n  " + "\n  ".join(offenders))


# ── reduced motion ───────────────────────────────────────────────────────

def _templates_without_comments() -> list[tuple[str, str]]:
    """(name, text) for every template, with comments blanked.

    Blanked rather than removed so reported line numbers stay true. The
    scale is discussed in comments in several files; matching those would
    make the guardrail cry wolf, and a guardrail that cries wolf is turned
    off.
    """
    out = []
    for path in sorted((PROJECT_ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
            text = re.sub(pattern, lambda m: re.sub(r"\S", " ", m.group(0)),
                          text, flags=re.S)
        text = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)
        out.append((path.relative_to(PROJECT_ROOT).as_posix(), text))
    return out


# Tailwind's own duration/easing utilities. `duration-base` and friends are
# ours and resolve through the custom properties; these resolve to numbers
# Tailwind picked.
_OFF_SCALE = re.compile(
    r"\b(duration-\d+|ease-in-out|ease-in|ease-linear)\b")


def test_no_template_uses_a_motion_utility_off_the_scale():
    """The scale has to bind BOTH layers or it binds neither.

    `tools/design_tokens.py` renders the durations into `tailwind.config`
    as `duration-fast|base|slow`, so a template CAN read the same values
    the stylesheet does. Nothing stopped it writing `duration-300` instead
    — and 14 utilities did, across 6 templates, silently running on
    Tailwind's numbers while app.css ran on the tokens.

    This scans raw text rather than parsed class attributes on purpose. One
    of the 14 was in a template literal whose colour is interpolated
    (`${color}`), so the class-scope detector did not recognise it as a
    class list at all — a guardrail that only looks where the converter
    looks inherits the converter's blind spots.
    """
    offenders = []
    for name, text in _templates_without_comments():
        for m in _OFF_SCALE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{name}:{line} {m.group(1)}")
    assert not offenders, (
        "use duration-fast|base|slow and ease-standard|out, which resolve "
        "through the same custom properties as app.css:\n  "
        + "\n  ".join(offenders))


_RAW_TRANSITION = re.compile(r"transition[^;`\"'{}]*")
_RAW_DURATION = re.compile(r"(?<![\w.-])(\d*\.?\d+)(ms|s)(?![\w.-])")


def test_no_raw_css_transition_in_a_template_is_off_the_scale():
    """Blind spot found by adversarial review, after the scale had already
    "landed" twice.

    `test_no_transition_uses_a_duration_off_the_scale` reads app.css.
    `test_no_template_uses_a_motion_utility_off_the_scale` reads templates,
    but only for Tailwind UTILITIES. Neither looked at raw CSS inside a
    template — `<style>` blocks and inline `style=""` attributes — and 16
    transitions lived in that gap across 5 templates, including four
    `transition:background 300ms` on server_detail.

    Two tests both reporting green over a third of the app's motion is this
    repository's whole failure mode: the scope of a check has to be the
    scope of the thing it checks.
    """
    offenders = []
    for name, text in _templates_without_comments():
        for decl in _RAW_TRANSITION.finditer(text):
            for value, unit in _RAW_DURATION.findall(decl.group(0)):
                literal = f"{value}{unit}"
                if literal in ALLOWED_DURATIONS:
                    continue
                line = text.count("\n", 0, decl.start()) + 1
                offenders.append(f"{name}:{line} {literal} in `{decl.group(0)[:60]}`")
    assert not offenders, (
        "raw CSS in a template must use var(--dur-fast|base|slow):\n  "
        + "\n  ".join(offenders))


def test_the_javascript_modules_are_scanned_too():
    """The other half of the same blind spot. Class strings applied to the
    DOM from `static/js/` are indistinguishable from markup at runtime, and
    the guardrail globbed `templates/**` only.

    Vendored libraries are excluded — they are not ours to hold to the
    scale, and scanning them produces noise that gets the test disabled.
    """
    offenders = []
    for path in sorted((PROJECT_ROOT / "static" / "js").glob("*.js")):
        text = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)),
                      path.read_text(encoding="utf-8"))
        text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"\S", " ", m.group(0)),
                      text, flags=re.S)
        for m in _OFF_SCALE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"static/js/{path.name}:{line} {m.group(1)}")
    assert not offenders, (
        "use duration-fast|base|slow and ease-standard|out:\n  "
        + "\n  ".join(offenders))


def test_the_comment_blanking_does_not_hide_real_offenders():
    """The blanking above is a liability: overreach and the guardrail stops
    guarding. Prove it still sees a live offender on the same line as a
    comment."""
    import re as _re
    sample = 'x = "p-2 duration-300"  // duration-500 mentioned in prose\n'
    blanked = _re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), sample)
    found = {m.group(1) for m in _OFF_SCALE.finditer(blanked)}
    assert found == {"duration-300"}, found


def test_reduced_motion_is_honoured_globally_not_rule_by_rule():
    """It was three blocks covering five rules. Every other transition kept
    running for someone who had asked it not to, and each new one opted out
    again by default, because honouring the preference meant remembering."""
    css = authored()
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert block, "no reduced-motion block"
    body = block.group(1)
    assert "*, *::before, *::after" in body, \
        "the rule must apply to everything, not to a list of selectors"
    for prop in ("animation-duration", "transition-duration",
                 "animation-iteration-count"):
        assert f"{prop}: 0.01ms !important" in body or \
               f"{prop}: 1 !important" in body, f"{prop} is not neutralised"


def test_the_loading_spinner_keeps_spinning_under_reduced_motion():
    """A spinner that stops is not reduced motion, it is a page that looks
    broken — the only thing saying the request is still in flight has frozen.
    WCAG 2.3.3 covers motion triggered by interaction; a progress indicator
    is essential motion and is exempt."""
    css = authored()
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S).group(1)
    assert ".animate-spin" in block
    spin = re.search(r"\.animate-spin \{(.*?)\}", block, re.S).group(1)
    assert "animation-iteration-count: infinite !important" in spin


def test_there_is_exactly_one_reduced_motion_block():
    """Three of them is how five rules ended up covered and 151 did not."""
    assert authored().count("@media (prefers-reduced-motion: reduce)") == 1


# ── the values themselves ────────────────────────────────────────────────

def test_the_durations_are_ordered_and_distinct():
    ms = [int(v.removesuffix("ms")) for v in dt.MOTION.values()]
    assert ms == sorted(ms) and len(set(ms)) == len(ms), dt.MOTION
    assert ms[0] >= 80, "below ~80ms a transition reads as a jump, not a move"
    assert ms[-1] <= 400, "above ~400ms the interface feels like it is lagging"


def test_dark_elevation_is_not_just_the_light_shadow_turned_up():
    """A shadow works by darkening what is behind it, and on a #05070D page
    there is nothing left to darken. The dark values have to separate the
    plane some other way — a lighter hairline at the edge."""
    for name, (light, dark) in dt.ELEVATION.items():
        assert light != dark, f"{name} is identical in both themes"
    assert "255 255 255" in dt.ELEVATION["md"][1], \
        "dark elevation needs a light edge, not only a darker shadow"
