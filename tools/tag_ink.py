"""Shared tag-pill contrast solver.

WHY THIS EXISTS
---------------
A tag's colour is chosen by an admin with a colour picker (`tools/design_
tokens.py` and the app's own token system have no say over it — it is
genuinely arbitrary, per-tag, per-installation data). An arbitrary colour has
no readability guarantee, so the label ink is solved rather than fixed: the
hue is kept, and the LIGHTNESS is nudged toward black (light theme) or white
(dark theme), in fixed 6% steps, until the composited chip clears 4.5:1
against every backdrop it actually renders on — capped at 24 steps so a
pathological input still terminates. See docs/OPS-LEARNINGS.md and the
commit that introduced this (`666c24e`) for the full history, including the
regression this module fixes: the first version baked ONE theme's ink into
an inline style at render time, so flipping the theme without a reload left
stale, unreadable ink on screen (measured as low as 1.74:1 — worse than the
3.26:1 defect it replaced).

THREE PLACES HOLD THIS ALGORITHM, AND THAT IS A DELIBERATE, DOCUMENTED
COMPROMISE, NOT AN OVERSIGHT
-----------------------------------------------------------------------
1. This module — the canonical, tested Python implementation.
2. `templates/servers.html`'s `_tagReadableInk` (JavaScript) — the tag
   table on the Servers page re-renders its pills client-side on every tag
   assign/remove, and the browser cannot call into this module.
3. A Jinja macro duplicated verbatim in `templates/partials/server_card.html`
   and `templates/partials/server_grid.html` — the dashboard card and the
   tag-filter bar render server-side and never touch JavaScript at all, so
   solving there means solving in Jinja.

The natural fix for (3) is a one-line Jinja filter registered on the Flask
app (`app.jinja_env.filters[...] = ...`), which would let both Jinja call
sites import this exact module and delete the macro. That line does not
live in this change: the task this module was written under scoped file
ownership to `templates/servers.html`, `templates/partials/server_card.
html`, `templates/partials/server_grid.html`, `tools/` and `tests/`, with
`app.py` explicitly out of reach because another agent was concurrently
editing this repository. Given that constraint, the Jinja macro is a
faithful, verified port of the functions below rather than a second
invention — `tests/test_tag_ink.py` renders the REAL macro from both
templates via a standalone `jinja2.Environment` and asserts its output
matches this module's output across a shared corpus of colours, including
every malformed one. A future change that can touch `app.py` should
register a filter backed by `tag_pill_css_props()` and delete both macro
copies; until then, this module is the spec the other two are checked
against, not merely a third opinion.

WHAT IS SHARED VS. DUPLICATED, AND WHY (see docs/OPS-LEARNINGS.md #19)
------------------------------------------------------------------------
The *arithmetic* (linearisation, luminance weights, contrast ratio, the
0.06 nudge, the 24-step cap) is identical in all three places by
construction — it is copied character-for-character from this module's
formulas into the JS and Jinja versions, and the equivalence test pins that.
What is NOT shared, because each call site's markup actually differs, is
which backdrop(s) the ink must clear:

- `servers.html`'s tag pills sit inside a `<tr>` carrying
  `hover:bg-page dark:hover:bg-page/50`. You cannot hover the pill without
  hovering the row, so its ink must clear contrast against BOTH the card
  (rest) and the row's hover composite (`ROW_HOVER_LIGHT` /
  `ROW_HOVER_DARK` below) — solving only against the card, as the first
  version did, left roughly three-quarters of colours failing AA the
  instant the row was hovered.
- `server_card.html` and `server_grid.html`'s pills have no hover
  background change at all (`.server-card:hover` only touches box-shadow/
  transform), so they solve against the card alone.
"""

from __future__ import annotations

import math
import re

# ── colour parsing ──────────────────────────────────────────────────────

_HEX6 = re.compile(r"^([0-9a-fA-F]{6})$")
_HEX3 = re.compile(r"^([0-9a-fA-F]{3})$")

# The admin-facing colour picker in the tag manager defaults to this grey
# (templates/servers.html, `#new-tag-color`); it is also what every
# malformed / missing colour falls back to.
DEFAULT_HEX = "6B7280"
DEFAULT_RGB = (0x6B, 0x72, 0x80)


def parse_hex(hex_color: str | None) -> tuple[int, int, int]:
    """Return the (r, g, b) a tag colour resolves to, expanding 3-digit CSS
    shorthand (`#abc` -> `#aabbcc`) and falling back to `DEFAULT_RGB` for
    anything else — missing, `None`, wrong length, non-hex characters.

    The original implementation's regex (`^#?([\\da-f]{6})$`) only accepted
    6-digit hex, so a valid 3-digit admin colour silently lost its identity
    and every tag using one rendered as the same indistinguishable grey —
    no crash, but the one thing the feature exists to provide (telling tags
    apart at a glance) was gone. This still declines anything else, on
    purpose: guessing at `rgb(...)` or a named colour is exactly the kind of
    guess docs/OPS-LEARNINGS.md #32 warns against.
    """
    s = (hex_color or "").strip()
    if s.startswith("#"):
        s = s[1:]
    m6 = _HEX6.match(s)
    if m6:
        h = m6.group(1)
    else:
        m3 = _HEX3.match(s)
        if not m3:
            return DEFAULT_RGB
        h = "".join(ch * 2 for ch in m3.group(1))
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ── WCAG contrast (pinned by tests/test_tag_ink.py against black/white = 21:1
#    and x/x = 1:1 — see docs/OPS-LEARNINGS.md #14: a contrast test built on
#    an unverified contrast helper is decorative) ──────────────────────────

def _linearize(channel_0_255: float) -> float:
    c = channel_0_255 / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = (_linearize(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb_a, rgb_b) -> float:
    la, lb = relative_luminance(rgb_a), relative_luminance(rgb_b)
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def _blend(fg, bg, alpha: float):
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(fg, bg))


def _js_round(x: float) -> int:
    """`Math.round` rounds half away from zero; Python's `round()` rounds
    half to even. Every value this function ever sees is non-negative (RGB
    channels nudging toward 0 or 255), so `floor(x + 0.5)` reproduces the
    JavaScript implementation's rounding exactly rather than merely
    approximately — the JS/Python/Jinja agreement test in
    tests/test_tag_ink.py depends on the two not silently diverging at a
    .5 boundary."""
    return math.floor(x + 0.5)


# ── the surfaces a pill can actually be composited against ────────────────

# static/css/app.css `--c-card` / `--c-page` (see :root and .dark blocks).
CARD_LIGHT = (255, 255, 255)
CARD_DARK = (15, 22, 35)
PAGE_LIGHT = (241, 245, 249)
PAGE_DARK = (5, 7, 13)

# templates/servers.html's tag-table row: `hover:bg-page` is opaque, so the
# light hover backdrop IS the page colour. `dark:hover:bg-page/50` is
# page-at-50%-alpha painted over the card beneath the row.
ROW_HOVER_LIGHT = PAGE_LIGHT
ROW_HOVER_DARK = tuple(_js_round(0.5 * p + 0.5 * c) for p, c in zip(PAGE_DARK, CARD_DARK))

AA_MIN_CONTRAST = 4.5
MAX_STEPS = 24
STEP = 0.06


def solve_ink(rgb, target, constraints, max_steps: int = MAX_STEPS, step: float = STEP):
    """Nudge `rgb` toward `target` (in fixed `step` fractions) until it
    clears `AA_MIN_CONTRAST` against every `(backdrop_rgb, alpha)` pair in
    `constraints` simultaneously, or `max_steps` is exhausted.

    All constraints for one call share a target (black for the light theme,
    white for dark), so nudging toward it can only ever help every
    constraint at once — there is no case where satisfying one backdrop
    moves the ink away from clearing another.
    """
    composites = [_blend(rgb, backdrop, alpha) for backdrop, alpha in constraints]
    ink = list(rgb)
    for _ in range(max_steps):
        if all(contrast_ratio(ink, c) >= AA_MIN_CONTRAST for c in composites):
            break
        ink = [_js_round(c + (t - c) * step) for c, t in zip(ink, target)]
    return tuple(ink)


def to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def to_rgba(rgb, alpha: float) -> str:
    r, g, b = (round(c) for c in rgb)
    return f"rgba({r},{g},{b},{alpha})"


# ── the public entry point ────────────────────────────────────────────────

def tag_pill_tokens(
    hex_color: str | None,
    tint_alpha: float = 0.22,
    hover_alpha: float | None = None,
    include_row_hover: bool = False,
    solve_against_hover: bool = True,
) -> dict:
    """Compute everything a tag pill needs to render legibly in both themes.

    `tint_alpha` is the alpha the raw admin colour is painted at for the
    chip's rest background; `hover_alpha` is the alpha for its hover
    background (defaults to `tint_alpha + 0.18`, matching the original
    JS solver — the hover tint must be denser than rest or the highlight
    reads as a dimming). `include_row_hover=True` models the servers.html
    tag-table backdrop (see module docstring); leave it `False` for a pill
    whose surroundings never change background on hover.

    `solve_against_hover=False` skips constraining the ink against any
    hover composite at all — for `server_card.html` / `server_grid.html`,
    whose pills have no hover-driven background change to clear in the
    first place (see `tag_ink_css_props`). `bg_hover` is still returned
    (some callers render it even when nothing solves against it), just not
    used to pick the ink.

    Returns ink_light / ink_dark as `#rrggbb`, plus bg / bg_hover / border
    as `rgba(...)` strings the raw colour composites correctly in either
    theme without needing a per-theme value (alpha blending against
    whatever surface is actually behind the element already IS the
    per-theme answer for a translucent colour).
    """
    rgb = parse_hex(hex_color)
    if hover_alpha is None:
        hover_alpha = min(1.0, tint_alpha + 0.18)

    light_constraints = [(CARD_LIGHT, tint_alpha)]
    dark_constraints = [(CARD_DARK, tint_alpha)]
    if solve_against_hover:
        if include_row_hover:
            light_constraints.append((ROW_HOVER_LIGHT, hover_alpha))
            dark_constraints.append((ROW_HOVER_DARK, hover_alpha))
        else:
            light_constraints.append((CARD_LIGHT, hover_alpha))
            dark_constraints.append((CARD_DARK, hover_alpha))

    ink_light = solve_ink(rgb, (0, 0, 0), light_constraints)
    ink_dark = solve_ink(rgb, (255, 255, 255), dark_constraints)

    return {
        "ink_light": to_hex(ink_light),
        "ink_dark": to_hex(ink_dark),
        "bg": to_rgba(rgb, tint_alpha),
        "bg_hover": to_rgba(rgb, hover_alpha),
        "border": to_rgba(rgb, 0.55),
        # Diagnostics only — not used by any render path, useful for tests
        # and for anyone re-measuring a specific colour by hand.
        "rest_ratio_light": round(contrast_ratio(ink_light, _blend(rgb, CARD_LIGHT, tint_alpha)), 2),
        "rest_ratio_dark": round(contrast_ratio(ink_dark, _blend(rgb, CARD_DARK, tint_alpha)), 2),
    }


def tag_ink_css_props(hex_color: str | None, tint_alpha: float = 0.0824) -> str:
    """The subset of `tag_pill_tokens` that `server_card.html` and
    `server_grid.html` actually need: just the two ink custom properties,
    formatted ready to drop into a `style="..."` attribute. Their
    background/border are already authored as raw-colour-plus-alpha
    literals in the markup (`{{ tag.color }}15` etc.) and are unaffected by
    theme, so only the ink needs solving. `0.0824` is `0x15 / 255`, the
    alpha those two templates already paint the background at.
    """
    tokens = tag_pill_tokens(hex_color, tint_alpha=tint_alpha, solve_against_hover=False)
    return f"--tag-ink-l:{tokens['ink_light']};--tag-ink-d:{tokens['ink_dark']};"
