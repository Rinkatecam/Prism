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
SERVER_CARD = PROJECT_ROOT / "templates" / "partials" / "server_card.html"


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

# The easing equivalent of the exemption above, kept separate and much
# smaller on purpose: a decelerating curve (var(--ease-standard/out)) is
# right for a tween with a start and an end, and wrong for a loop with
# neither. Applied to a full-turn rotation that repeats forever, ANY
# deceleration is a visible speed-up/slow-down at the same point every lap —
# not a subtle mistake, the loop visibly stutters — which is why Tailwind's
# own `.animate-spin` ships `linear infinite` and sits outside this file's
# scope for the same reason (it is in the vendored runtime build, not here).
#
# Keyed by animation NAME, not blanket-exempted, so a plain `ease`/`ease-in`
# landing on some other, non-looping `animation:`/`transition:` declaration
# is still caught — only the declaration that names one of these loops gets
# to use the keyword on the right of it.
ALLOWED_LOOP_EASINGS: dict[str, str] = {
    "badge-border-orbit": "linear",  # circulating border on restart_required
                                      # / stabilising — see app.css for the
                                      # pseudo-element this animates.
}


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
        exempt = next((v for k, v in ALLOWED_LOOP_EASINGS.items() if k in decl),
                      None)
        for bad in re.findall(r"(?<![-\w])(ease|ease-in|linear)(?![-\w(])", decl):
            if bad == exempt:
                continue
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


# `animation` as well as `transition`. The app.css check beside this one
# has covered both since it was written; the template version checked only
# `transition` and missed `animation: wf-ctx-pop 0.15s` — a context-menu
# entrance, which is exactly the "how long it takes to GET somewhere" case
# the loop exemption is NOT meant to cover.
#
# Anchored on a word boundary and required to be followed by a colon, so it
# cannot start matching inside the English word "transition" in help text.
# One template says "they fire on the transition, not every poll" in a <p>,
# and the unanchored version swallowed 172 characters of prose — a finding
# a contributor could only clear by rewording documentation.
_RAW_TRANSITION = re.compile(r"\b(?:transition|animation)\s*:[^;`\"'{}]*")
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



def test_the_orbiting_dot_is_gated_on_motion_path_support():
    """`offset-path: border-box` is Chromium 116+ / Firefox 122+.

    An engine without it drops that one declaration and keeps the rest of
    the rule, so the dot renders STATIC 2px outside the badge's top-left
    corner — measured. A stray dot pinned to a corner reads as a rendering
    fault, not as "this browser cannot animate". Inside `@supports`, such an
    engine draws no dot at all, which is what degrading gracefully means.
    """
    css = authored()
    i = css.find(".badge-restart_required::before")
    assert i != -1, "the orbiting-dot rule is gone"
    head = css[:i]
    guard = head.rfind("@supports (offset-path: border-box)")
    assert guard != -1, (
        "no @supports (offset-path: border-box) precedes the orbiting dot — "
        "without it an engine lacking motion path renders a STATIC dot 2px "
        "outside the badge's top-left corner, which reads as a rendering "
        "fault rather than as a missing animation"
    )
    assert head.count("{", guard) - head.count("}", guard) >= 1, (
        "the @supports block closes before the orbiting-dot rule, so the "
        "rule is not actually gated"
    )

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


# ── the lifecycle badges: colour reuses the palette, motion carries the
#    stage ("the icon animates when the machine is working, the border
#    animates when it is waiting on you") ───────────────────────────────────

# ── simulating the reduce cascade ─────────────────────────────────────────
#
# The test this section replaces asserted that three keyframe NAMES (plus a
# short list of selectors) did not appear inside the reduced-motion block.
# That can only ever see an exemption spelled by NAME. The real bug — see
# docs/OPS-LEARNINGS.md §2.2, "a test written from one mental model tests one
# shape" — was spelled by CLASS: the spinner exemption (`.animate-spin`)
# matches the downloading/installing icon too, because that icon carries
# `.animate-spin` for an unrelated reason (winning specificity against
# Tailwind's own rule, see app.css). A name check has no way to see an
# exemption inherited through a shared class it never mentions by name.
#
# The only test that CAN see it resolves the actual cascade: importance,
# then specificity, then source order, exactly what a browser does. What
# follows is a small, deliberately scoped simulator — not a general CSS
# engine — that knows exactly the selector grammar in and around the
# reduced-motion block: `*`, `*::before`, `*::after`, a bare class, two
# classes joined by a descendant space, a class plus a bare type
# (`svg`/`i`), and a class plus `::before`. That closed set is all this
# needs to cover.

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_ANIMATION_DURATION_TOKEN = re.compile(r"^\d*\.?\d+m?s$")
_ANIMATION_ITERATION_TOKEN = re.compile(r"^(infinite|\d+)$")
# `[class~="x"]` carries the same specificity weight as `.x` (both count in
# the "classes" column) and matches the same elements — app.css's reduced-
# motion block uses this form for the orbit-dot hide rule specifically to
# avoid an earlier, textually-identical occurrence of `.badge-restart_
# required::before` confusing two already-verified tests that search for
# that literal substring. This parser has to recognise both spellings.
_ATTR_CLASS = re.compile(r'\[class~="([\w-]+)"\]')


def _strip_css_comments(css: str) -> str:
    return _CSS_COMMENT.sub(" ", css)


def _specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes, types-and-pseudo-elements) for exactly the selectors
    this file uses. Not a general specificity calculator."""
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector)) + len(_ATTR_CLASS.findall(selector))
    pseudo_elems = len(re.findall(r"::(?:before|after)", selector))
    bare = re.sub(r"\.[\w-]+", "", selector)
    bare = _ATTR_CLASS.sub("", bare)
    bare = re.sub(r"::(?:before|after)", "", bare)
    types = len(re.findall(r"(?<![.\w#-])(?:svg|i)\b", bare))
    return (ids, classes, types + pseudo_elems)


def _parse_compound(part: str) -> dict:
    part = part.strip()
    pseudo = None
    m = re.search(r"::(before|after)$", part)
    if m:
        pseudo = m.group(1)
        part = part[: m.start()]
    classes = set(re.findall(r"\.([\w-]+)", part))
    classes |= set(_ATTR_CLASS.findall(part))
    rest = re.sub(r"\.[\w-]+", "", part)
    rest = _ATTR_CLASS.sub("", rest).strip()
    typ = rest if rest and rest != "*" else None
    return {"type": typ, "classes": classes, "pseudo": pseudo}


def _compound_matches(compound: dict, node: dict) -> bool:
    if compound["type"] and compound["type"] != node.get("type"):
        return False
    if not compound["classes"] <= node.get("classes", set()):
        return False
    if compound["pseudo"] != node.get("pseudo"):
        return False
    return True


def _selector_matches(selector: str, chain: list[dict]) -> bool:
    """`chain` runs outer ancestor first, target (or its pseudo-element)
    last. Descendant combinator only — nothing this checks uses `>`, `+` or
    `~`. A `::before` dot is modelled as its host's own node carrying
    `pseudo="before"`; CSS has no separate DOM node for it, and neither does
    this."""
    compounds = [_parse_compound(p) for p in selector.split()]
    if not compounds or not chain:
        return False
    if not _compound_matches(compounds[-1], chain[-1]):
        return False
    ancestors = chain[:-1]
    idx = 0
    for comp in compounds[:-1]:
        found = False
        while idx < len(ancestors):
            if _compound_matches(comp, ancestors[idx]):
                found = True
                idx += 1
                break
            idx += 1
        if not found:
            return False
    return True


def _expand_animation_shorthand(value: str) -> dict:
    """`animation: name duration timing-function iteration-count` — the only
    shape used in this file. A longhand declared in a MORE specific rule
    must still be able to override just that one component of a shorthand
    set elsewhere, which is why the cascade below resolves per-longhand
    rather than per-shorthand."""
    tokens: list[str] = []
    depth = 0
    cur = ""
    for ch in value:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch.isspace() and depth == 0:
            if cur:
                tokens.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    out: dict[str, str] = {}
    for tok in tokens:
        if _ANIMATION_DURATION_TOKEN.match(tok):
            out.setdefault("animation-duration", tok)
        elif _ANIMATION_ITERATION_TOKEN.match(tok):
            out.setdefault("animation-iteration-count", tok)
        elif tok == "linear" or tok.startswith(("cubic-bezier", "var(--ease")):
            out.setdefault("animation-timing-function", tok)
        else:
            out.setdefault("animation-name", tok)
    return out


def _parse_stylesheet(css: str) -> list[dict]:
    """Every flat rule in `css` (comments already stripped) as
    `{selectors, decls: {prop: (value, important)}, in_reduce, in_supports,
    order}`. `@keyframes` percentage blocks parse as harmless inert rules —
    their "selectors" (`0%`, `50%`, ...) match no real element."""
    rules: list[dict] = []
    stack: list[str] = []
    i, n, order = 0, len(css), 0
    while i < n:
        if css[i].isspace():
            i += 1
            continue
        if css[i] == "@":
            j = css.index("{", i)
            prelude = css[i:j]
            if "prefers-reduced-motion" in prelude:
                stack.append("reduce")
            elif prelude.lstrip().startswith("@supports"):
                stack.append("supports")
            else:
                stack.append("other")
            i = j + 1
            continue
        if css[i] == "}":
            if stack:
                stack.pop()
            i += 1
            continue
        j = css.index("{", i)
        k = css.index("}", j)
        selectors = [s.strip() for s in css[i:j].split(",") if s.strip()]
        decls: dict[str, tuple[str, bool]] = {}
        for part in css[j + 1 : k].split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            prop, val = part.split(":", 1)
            prop, val = prop.strip(), val.strip()
            important = val.endswith("!important")
            if important:
                val = val[: -len("!important")].strip()
            decls[prop] = (val, important)
        if "animation" in decls:
            value, important = decls["animation"]
            for lh, lv in _expand_animation_shorthand(value).items():
                decls.setdefault(lh, (lv, important))
        rules.append({
            "selectors": selectors,
            "decls": decls,
            "in_reduce": "reduce" in stack,
            "in_supports": "supports" in stack,
            "order": order,
        })
        order += 1
        i = k + 1
    return rules


def _cascade_value(rules: list[dict], chain: list[dict], prop: str):
    """The winning value for `prop` on `chain`, simulating
    `prefers-reduced-motion: reduce` TRUE and `@supports (offset-path:
    border-box)` TRUE — the modern-engine case; the pre-116 fallback (no dot
    at all) is covered separately by
    test_the_orbiting_dot_is_gated_on_motion_path_support and is not this
    test's job. Real CSS cascade order: importance beats specificity beats
    source order — all three ascending, so the last candidate after sorting
    is the winner."""
    candidates = []
    for rule in rules:
        if prop not in rule["decls"]:
            continue
        value, important = rule["decls"][prop]
        best = None
        for sel in rule["selectors"]:
            if _selector_matches(sel, chain):
                spec = _specificity(sel)
                if best is None or spec > best:
                    best = spec
        if best is not None:
            candidates.append((important, best, rule["order"], value))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return candidates[-1][3]


# Representative elements, outer ancestor first. Classes match the real
# markup in server_card.html exactly (the `w-3 h-3` size utilities are
# included deliberately — matching only on `animate-spin` and asserting the
# selector logic ignores the rest would be testing a cleaner element than
# the one the app actually renders).
_BADGE_ELEMENTS: dict[str, list[dict]] = {
    "searching":    [{"classes": {"badge", "badge-searching"}},
                      {"type": "i", "classes": {"w-3", "h-3"}}],
    "downloading":  [{"classes": {"badge", "badge-downloading"}},
                      {"type": "i", "classes": {"w-3", "h-3", "animate-spin"}}],
    "installing":   [{"classes": {"badge", "badge-installing"}},
                      {"type": "i", "classes": {"w-3", "h-3", "animate-spin"}}],
    "rebooting":    [{"classes": {"badge", "badge-rebooting"}},
                      {"type": "i", "classes": {"w-3", "h-3", "animate-spin"}}],
    "restart_required::before": [
        {"classes": {"badge", "badge-restart_required"}, "pseudo": "before"}],
    "stabilising::before": [
        {"classes": {"badge", "badge-stabilising"}, "pseudo": "before"}],
}


def test_the_simulator_can_report_a_positive_before_trusting_a_negative():
    """docs/OPS-LEARNINGS.md #21/#28: before believing a measurement that
    says "frozen", prove the same instrument can say "running" when running
    is the right answer — otherwise a simulator that always returns frozen
    would pass every assertion below for the wrong reason.

    `rebooting`'s icon is a genuine spinner (Tailwind's own `.animate-spin`
    keyframe, never overridden to badge-travel) and the reduced-motion block
    explicitly keeps spinners moving, slowed rather than stopped. This is
    the positive control: if it fails, nothing else in this file that
    reports "frozen" can be trusted either."""
    rules = _parse_stylesheet(_strip_css_comments(authored()))
    chain = _BADGE_ELEMENTS["rebooting"]
    assert _cascade_value(rules, chain, "animation-duration") == "2.25s", (
        "the control's own spinner should be exempted (slowed, not frozen) "
        "— if it isn't, the simulator cannot distinguish exempted from "
        "frozen and every 'frozen' result below is meaningless")
    assert _cascade_value(rules, chain, "animation-iteration-count") == "infinite"


def test_the_lifecycle_animations_get_no_reduced_motion_exemption():
    """Rewritten after adversarial review found the original version could
    not see the actual bug (see the module comment above this section): it
    checked that three keyframe NAMES were absent from the reduced-motion
    block, and `badge-travel` always was — the leak came through the shared
    `.animate-spin` CLASS, which a name check never looks at.

    This instead resolves the real cascade — importance, specificity,
    source order — for each lifecycle badge's actual carrier and asserts
    every one lands on the blanket freeze (0.01ms / 1 iteration), the same
    outcome as everything else that gets no named exemption. Cross-checked
    against the positive control above, which proves this simulator reports
    "running" when running is correct, not "frozen" unconditionally."""
    rules = _parse_stylesheet(_strip_css_comments(authored()))
    frozen = ("searching", "downloading", "installing",
              "restart_required::before", "stabilising::before")
    for name in frozen:
        chain = _BADGE_ELEMENTS[name]
        duration = _cascade_value(rules, chain, "animation-duration")
        iterations = _cascade_value(rules, chain, "animation-iteration-count")
        assert duration == "0.01ms", (
            f"{name}: animation-duration resolves to {duration!r} under "
            "simulated reduced motion, not the frozen 0.01ms — it is "
            "inheriting a reduced-motion exemption through a shared class")
        assert iterations == "1", (
            f"{name}: animation-iteration-count resolves to {iterations!r} "
            "under simulated reduced motion, not the frozen 1")


def test_the_parked_orbit_dot_is_hidden_under_reduced_motion():
    """Freezing `badge-border-orbit`'s duration (previous test) stops the
    dot MOVING but not EXISTING: the pseudo-element still renders, parked at
    `offset-distance: 0%` — a static glowing dot on every
    restart_required/stabilising badge, permanently. That is exactly the
    artefact `@supports (offset-path: border-box)` exists to spare a
    pre-116 engine from (see the comment on that block in app.css); a
    reduced-motion user on a current engine is owed the same "no dot"
    outcome, not a parked one. Asserts the dot is hidden outright."""
    rules = _parse_stylesheet(_strip_css_comments(authored()))
    for name in ("restart_required::before", "stabilising::before"):
        chain = _BADGE_ELEMENTS[name]
        display = _cascade_value(rules, chain, "display")
        assert display == "none", (
            f"{name}: display resolves to {display!r} under simulated "
            "reduced motion — the dot parks instead of disappearing")


def test_downloading_and_installing_travel_instead_of_spinning():
    """Both states render a lucide "loader" icon with Tailwind's own
    `.animate-spin` (see partials/server_card.html) — a ROTATING icon there
    reads as busy exactly like `rebooting`, which is the ambiguity the
    owner's spec removes. `.badge-x .animate-spin` is two classes, (0,2,0),
    which beats Tailwind's own single-class `.animate-spin` (0,1,0) — no
    `!important`, and it wins regardless of which stylesheet is injected
    last (see the trap in docs/OPS-LEARNINGS.md about Tailwind's runtime
    sheet loading after this one)."""
    css = authored()
    assert re.search(
        r"\.badge-downloading \.animate-spin,\s*\n"
        r"\.badge-installing \.animate-spin \{\s*\n"
        r"\s*animation: badge-travel", css), \
        "downloading/installing must override .animate-spin with badge-travel"
    keyframe = re.search(r"@keyframes badge-travel \{(.*?)\n\}", css, re.S)
    assert keyframe, "badge-travel keyframes missing"
    body = keyframe.group(1)
    assert "transform" in body and "rotate" not in body, \
        "badge-travel must move, not rotate"


def test_searching_is_ready_to_sway_not_spin():
    """`searching` now has its carrier: the `{% if %}` chain in
    partials/server_card.html grows a `search` magnifier for this state, so
    the rule below animates something visible. It sways rather than spins on
    purpose — a rotating glyph is the universal "busy" and would make
    searching read as rebooting, the one other state with a turning icon.
    The rule long predated the icon; nothing about it had to change when the
    template caught up, which is what the assertions here pin down."""
    css = authored()
    assert re.search(
        r"\.badge-searching svg,\s*\n\.badge-searching i \{\s*\n"
        r"\s*animation: badge-sway", css), \
        "searching must be wired to badge-sway, not left to inherit a spin"
    keyframe = re.search(r"@keyframes badge-sway \{(.*?)\n\}", css, re.S)
    assert keyframe, "badge-sway keyframes missing"
    body = keyframe.group(1)
    assert "translateX" in body and "rotate" not in body, \
        "badge-sway must move side to side, not rotate"


def test_the_searching_icon_carrier_exists_in_server_card():
    """`badge-searching`'s sway rule (app.css) has no visible carrier unless
    server_card.html actually renders an icon element for the `searching`
    install-lifecycle state — app.css said as much for a while after the
    carrier was added, because nothing pinned it. Nothing else in the suite
    reads this branch: deleting `{% elif istatus == 'searching' %}` from the
    icon chain leaves every other test green, since the sway rule simply
    matches an icon that no longer exists rather than failing to match at
    all. This is the pin the adversarial review asked for; the reviewer
    could not run the mutation (the write was blocked), so it is verified
    here: deleting the branch and rerunning this test fails it."""
    html = SERVER_CARD.read_text(encoding="utf-8")
    m = re.search(r"\{%-?\s*elif\s+istatus\s*==\s*'searching'\s*-?%\}", html)
    assert m, (
        "the searching branch of the install-lifecycle icon chain is "
        "missing from server_card.html — badge-searching's sway animation "
        "(app.css) has nothing left to animate")
    next_branch = re.search(r"\{%-?\s*(?:elif|else|endif)\b", html[m.end():])
    assert next_branch, "could not find the end of the searching branch"
    body = html[m.end():m.end() + next_branch.start()]
    assert "data-lucide=" in body, (
        "the searching branch renders no icon element — badge-sway "
        "(app.css) targets `.badge-searching svg, .badge-searching i` and "
        "needs one to exist")


def test_restart_required_and_stabilising_circulate_the_border_only():
    """The row that proves the owner's rule: restart_required is blocked on
    a human, nothing is running on the machine, so its icon (alert-circle,
    static already) must stay untouched by any animation rule — only the
    border, via the ::before pseudo-element, may move. Same for stabilising,
    whose check-circle icon never had a spin class to begin with."""
    css = authored()
    block = re.search(
        # Whitespace-tolerant: the rule is nested inside
        # `@supports (offset-path: border-box)`, so both selectors and the
        # closing brace are indented. Keyed on column-0 formatting, this
        # matched nothing and reported "the rule is missing" — which reads
        # as a deleted feature rather than a moved one.
        r"\.badge-restart_required::before,\s*"
        r"\.badge-stabilising::before\s*\{(.*?)\n\s*\}", css, re.S)
    assert block, "the circulating-border pseudo-element rule is missing"
    body = block.group(1)
    assert "animation: badge-border-orbit" in body
    assert "position: absolute" in body
    for base in ("badge-restart_required", "badge-stabilising"):
        assert not re.search(rf"\.{base}\s+(svg|i|\.animate-spin)\s*\{{", css), \
            f".{base}'s icon must carry no animation rule of its own"
    keyframe = re.search(r"@keyframes badge-border-orbit \{(.*?)\n\s*\}", css, re.S)
    assert keyframe, "badge-border-orbit keyframes missing"
    assert "offset-distance" in keyframe.group(1), (
        "the dot must travel via offset-distance — see the comment in "
        "app.css on why a rotating conic-gradient was rejected")


def test_the_circulating_border_does_not_distort_on_a_pill_shape():
    """The first implementation was a rotating conic-gradient masked to a
    ring — correct on a square debug element, and a long diagonal streak
    bleeding past the badge on the real ~7:1 pill, because a conic-gradient's
    angles live in the box's own coordinate space and do not account for
    aspect ratio. Screenshotted both before switching to `offset-path:
    border-box`, which traces the actual outline regardless of proportions.

    This test pins the property that made the first attempt wrong: nothing
    here may be sized or positioned relative to the FULL badge box in a way
    that reintroduces the distortion — the moving part must be a small,
    fixed-size dot on a path, not a gradient spanning the badge's own
    (highly non-square) dimensions."""
    css = authored()
    before = re.search(
        # Whitespace-tolerant: the rule is nested inside
        # `@supports (offset-path: border-box)`, so both selectors and the
        # closing brace are indented. Keyed on column-0 formatting, this
        # matched nothing and reported "the rule is missing" — which reads
        # as a deleted feature rather than a moved one.
        r"\.badge-restart_required::before,\s*"
        r"\.badge-stabilising::before\s*\{(.*?)\n\s*\}", css, re.S).group(1)
    assert "conic-gradient" not in before, (
        "a gradient spanning the badge's own box reintroduces the aspect-"
        "ratio distortion measured in the browser — see the comment above "
        "this rule in app.css")
    assert "offset-path: border-box" in before
    assert re.search(r"width:\s*\d+px", before) and re.search(r"height:\s*\d+px", before), \
        "the moving part must be a small, fixed-size dot, not something " \
        "sized from the badge's own box"


def test_linear_is_exempted_only_for_the_named_circulating_loop():
    """`ALLOWED_LOOP_EASINGS` must name the animation, not wave `linear`
    through everywhere — mutation target: widen the key to match anything
    and this goes red because `.animate-spin`'s own `linear` (in Tailwind's
    vendored build, never this file) is not what the exemption is for."""
    assert ALLOWED_LOOP_EASINGS == {"badge-border-orbit": "linear"}


def test_queued_carries_neither_icon_nor_border_motion():
    """queued is "accepted, nothing started" — the one lifecycle state with
    no animation at all, machine or human. If a future edit gives it a
    keyframe, this is the line that should make someone ask why."""
    css = authored()
    assert not re.search(r"\.badge-queued[^{]*\{[^}]*animation", css), \
        "badge-queued must stay motionless"
    assert "badge-queued::before" not in css


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
