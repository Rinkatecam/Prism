"""Guardrails for interaction states on cards — Wave 3, task G2.

`.card-clickable` shipped in Phase 2 with hover and active rules, no
focus-visible rule, and — measured — not a single element in any template
carrying the class. Both halves of that are the same failure: a rule with no
carrier, and a carrier with a missing rule.

WHAT THESE ARE BLIND TO:

  * They cannot see whether the ring is VISIBLE. `:focus-visible` needs a
    keyboard origin, so it was forced with `focus({focusVisible: true})` in
    the browser and read off computed style, with transitions disabled first
    because this pane never advances them. Both card types measured
    border -> rgb(91 33 182) (`--c-brand`), box-shadow none -> a 3px brand
    halo plus `--shadow-md`, transform none -> translateY(-2px).
  * They only see elements written as markup. A card built from a JS string
    is invisible to them. One such case exists — a `<div>` disclosure in
    `server_detail.html` around line 3055 with `data-action` and no
    `tabindex`, which cannot receive focus at all. It is deliberately NOT
    given `.card-clickable`: attaching a focus ring to something unfocusable
    is precisely the empty-rule pattern this file exists to prevent. It is
    tracked separately as a keyboard-operability defect.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)


def _code_only(text: str) -> str:
    return _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


CLICKABLE_SELECTORS = ("card-clickable", "server-card")

# An element that is both INTERACTIVE and CARD-SHAPED.
_TAG = re.compile(r"<(a|button)\b[^>]*?>", re.S)


def _card_shaped_controls() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(path.read_text(encoding="utf-8"))
        for m in _TAG.finditer(text):
            tag = m.group(0)
            if re.search(r"\bbg-card\b", tag) and re.search(r"\brounded-(lg|md|xl)\b", tag):
                line = text[:m.start()].count("\n") + 1
                found.append((path.relative_to(TEMPLATES).as_posix(), line,
                              re.sub(r"\s+", " ", tag)))
    return found


def test_the_focus_ring_rule_exists_and_uses_the_brand_token():
    """Clickable cards lift on hover. Hover is unreachable from a keyboard,
    so without this rule a keyboard user tabbing a grid of 29 cards sees
    nothing at all until focus reaches a control inside one — WCAG 2.4.7."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    m = re.search(
        r"([^{}]*\.card-clickable:focus-visible[^{}]*)\{([^}]*)\}", css)
    assert m, "no :focus-visible rule for .card-clickable"
    selector, body = m.group(1), m.group(2)

    for needed in ("a.server-card:focus-visible", "button.server-card:focus-visible"):
        assert needed in selector, (
            f"{needed} missing — the server grid is the case that motivated "
            "this rule and it uses its own class, not .card-clickable")

    # Check the box-shadow DECLARATION, not the rule body. Asserting
    # `"--c-brand" in body` passed while the ring itself was hardcoded to
    # rgba(91, 33, 182, 0.25), because the neighbouring `border-color` still
    # mentioned the token. Caught by mutation. A near-miss like this also
    # slips past the hex ratchet in test_design_tokens.py, which only looks
    # for `-[#rrggbb]` in templates.
    shadow = re.search(r"box-shadow\s*:([^;]*);", body)
    assert shadow, "no ring drawn"
    assert "var(--c-brand" in shadow.group(1), (
        "the ring colour must come from the brand token, not a literal:\n  "
        f"box-shadow:{shadow.group(1)}")


def test_focus_is_not_weaker_than_hover():
    """A focus state that says less than the hover state tells a keyboard
    user less than a mouse user. Both carry the lift and an elevation."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    focus = re.search(r"\.card-clickable:focus-visible[^{]*\{([^}]*)\}", css)
    assert focus, "no :focus-visible rule for .card-clickable"
    body = focus.group(1)
    assert "translateY(-2px)" in body, (
        "hover lifts the card and focus does not; the keyboard gets a weaker "
        "signal than the pointer for the same affordance")
    assert "--shadow-md" in body, "hover raises the card and focus does not"


def test_every_card_shaped_control_opts_in():
    """Coverage regresses the moment someone adds a card. `.card-clickable`
    is opt-in by design — a plain panel must stay a plain panel — so the
    thing worth enforcing is that an element which is BOTH interactive and
    card-shaped has made the choice explicitly."""
    missing = [f"{f}:{ln}  {tag[:110]}"
               for f, ln, tag in _card_shaped_controls()
               if not any(c in tag for c in CLICKABLE_SELECTORS)]
    assert not missing, (
        "interactive, card-shaped, and neither `card-clickable` nor "
        "`server-card` — so it lifts on hover for nobody and shows no focus "
        "ring:\n  " + "\n  ".join(missing))


def test_the_inventory_is_not_empty():
    """The check above passes trivially if the scan finds nothing — which is
    what would happen if the markup moved to a component system or the class
    attribute changed shape. Assert it is still looking at something."""
    found = _card_shaped_controls()
    assert len(found) >= 3, (
        f"only {len(found)} card-shaped controls found; this scan has "
        "probably stopped matching the markup rather than the markup having "
        "stopped existing")


def test_card_clickable_is_not_shadowed_by_a_narrow_tailwind_transition():
    """`transition-shadow` alongside `.card-clickable` narrows the transition
    to one property. Tailwind's sheet is injected after app.css and wins at
    equal specificity, so the lift the class adds would snap instead of
    moving — the class would look half-broken for a reason invisible in
    either file on its own."""
    offenders = []
    for f, ln, tag in _card_shaped_controls():
        if "card-clickable" not in tag:
            continue
        narrow = re.findall(r"\btransition-(shadow|colors|opacity|none)\b", tag)
        if narrow:
            offenders.append(f"{f}:{ln}  transition-{narrow[0]}")
    assert not offenders, (
        "a narrowed Tailwind transition overrides the one `.card-clickable` "
        "sets, so the lift will not animate:\n  " + "\n  ".join(offenders))
