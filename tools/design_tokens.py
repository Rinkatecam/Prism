"""Single source of truth for Prism's colour tokens.

`tools/migrate_tokens.py` and `tests/test_design_tokens.py` both import this,
so the mapping that gets APPLIED and the mapping that gets ASSERTED are the
same object. A second copy would drift, and drift here is invisible until
someone opens the wrong page in the wrong theme.

WHY THIS EXISTS
---------------
Measured across templates on 2026-08-06: **0** uses of `var(--token)` and
**5,184** hardcoded hex literals, spread over 113 distinct colours. Three
separate colour abstractions already existed — `app.css` custom properties,
`tailwind.config.extend.colors`, and the status classes — and all three were
referenced by nothing, because typing a hex was always easier and nothing ever
failed.

The mapping below is derived from how colours are actually USED, not from
taste. The dominant light/dark pairs, by count:

    text    #6B7280 -> #CBD5E1   x385   (also -> #94A3B8 x58, -> #9CA3AF x39)
    border  #E5E7EB -> #334155   x475
    text    #9CA3AF -> #475569   x62
    text    #2563EB -> #3B82F6   x67
    bg      #F9FAFB -> #0F172A   x46

Note the first line: three different dark values pair with the same light
grey. Collapsing that inconsistency is precisely what a token is for.

CHANNEL TRIPLETS, NOT HEX
-------------------------
Values render as `R G B` so Tailwind can compose them as
`rgb(var(--c-x) / <alpha-value>)`. A bare `var()` holding a hex silently
breaks the opacity modifier, and **329** sites depend on it (`bg-critical/10`,
`bg-page/50`). This is not a stylistic preference; it is the reason the format
is what it is.
"""

from __future__ import annotations

import re

# token -> (light, dark).
#
# Phase 1a deliberately keeps TODAY's values so the mechanical conversion of
# 5,184 literals is visually inert and therefore reviewable. Phase 1b swaps
# these twelve pairs for the C-Ink palette in one small diff, so that if
# something looks wrong afterwards the commit responsible is unambiguous.
TOKENS: dict[str, tuple[str, str]] = {
    "page":     ("#F9FAFB", "#0F172A"),   # app background
    "card":     ("#FFFFFF", "#1E293B"),   # panel / card surface
    "raised":   ("#F3F4F6", "#0F172A"),   # hover + inset surface
    "line":     ("#E5E7EB", "#334155"),   # borders and dividers
    "ink":      ("#111827", "#F1F5F9"),   # primary text
    "muted":    ("#6B7280", "#CBD5E1"),   # secondary text
    "faint":    ("#9CA3AF", "#475569"),   # tertiary text, placeholders
    "brand":    ("#8B5CF6", "#A78BFA"),   # violet end of the brand gradient
    "info":     ("#2563EB", "#3B82F6"),
    "healthy":  ("#10B981", "#34D399"),
    "warning":  ("#F59E0B", "#FBBF24"),
    "critical": ("#DC2626", "#EF4444"),
}

# Light or dark values that were paired inconsistently and collapse onto a
# canonical token. Recorded here rather than silently folded in, because each
# one is a small deliberate change to what that element renders as.
ALIASES: dict[str, str] = {
    "#94A3B8": "muted",   # 58 uses as the dark half of muted
    "#9CA3AF": "muted",   # 39 uses as the dark half of muted (light: faint)
    "#64748B": "faint",   # 14 uses as the dark half of faint
    "#4B5563": "faint",
    "#D1D5DB": "line",
}

_ALIAS_UPPER: dict[str, str] = {h.upper(): t for h, t in ALIASES.items()}
_LIGHT: dict[str, str] = {light.upper(): name for name, (light, _d) in TOKENS.items()}
_DARK: dict[str, str] = {dark.upper(): name for name, (_l, dark) in TOKENS.items()}
for _hex, _token in ALIASES.items():
    _DARK.setdefault(_hex.upper(), _token)

# A Tailwind arbitrary-colour utility: `text-[#hex]`, `dark:bg-[#hex]/50`,
# `border-l-[#hex]`. The side group is an explicit alternation rather than a
# character class — `-[trblxy]` would also swallow the first letter of a
# colour-family name and mangle the utility.
#
# The variant chain is captured, not skipped. `hover:bg-…` is a DIFFERENT
# utility from `bg-…`; treating them as one would let a hover colour "pair
# with" a base `dark:` utility and delete it, dropping the element's dark
# background while leaving its hover state intact — a fault visible only to
# someone hovering that element in dark mode.
_VARIANT = (r"(?:dark|hover|focus|focus-visible|focus-within|active|visited|"
            r"disabled|checked|group-hover|group-focus|peer-hover|peer-focus|"
            r"first|last|odd|even|sm|md|lg|xl|2xl|print|rtl|ltr|"
            r"motion-safe|motion-reduce|aria-expanded):")
_UTIL = re.compile(
    rf"(?P<variants>(?:{_VARIANT})*)"
    r"(?P<util>text|bg|border|ring|from|to|via|divide|placeholder|fill|stroke|accent|outline)"
    r"(?P<side>-(?:t|r|b|l|x|y|top|right|bottom|left))?"
    r"-\[(?P<hex>#[0-9A-Fa-f]{6})\]"
    r"(?P<alpha>/\d{1,3})?"
)


def _split_variants(chain: str) -> tuple[bool, str]:
    """Return (is_dark, the chain with `dark:` removed)."""
    parts = [p for p in chain.split(":") if p]
    return ("dark" in parts,
            "".join(f"{p}:" for p in parts if p != "dark"))

_CLASS_ATTR = re.compile(r'class="([^"]*)"')

# Tailwind BUILT-IN colour classes that appear as the light half of a pair
# whose dark half is an arbitrary value. Measured: 275 elements are
# `bg-white dark:bg-[#1E293B]` — the light side never spells `bg-[#FFFFFF]`.
#
# This nearly shipped as a silent regression. An earlier converter saw the
# dark utility with no arbitrary light counterpart, classed it redundant and
# deleted it, which would have made every card white in dark mode. All nine
# tests passed, because every one of them used arbitrary values on both sides.
# The output only looked wrong when read against real markup.
_BUILTIN_LIGHT: dict[str, str] = {
    "white": "#FFFFFF",
    "black": "#000000",
}
_BUILTIN_UTIL = re.compile(
    r"(?<!dark:)\b(?P<util>text|bg|border|ring)-(?P<name>white|black)\b")


def token_for(hex_value: str, is_dark: bool) -> str | None:
    """Which token owns this literal, in this theme? None if unmapped."""
    return (_DARK if is_dark else _LIGHT).get(hex_value.upper())


def convert(text: str) -> str:
    """Rewrite arbitrary-colour utilities inside class="..." to token classes.

    A light utility and its `dark:` counterpart collapse to ONE class, because
    the token already carries both values.

    Two deliberate restrictions:

      * Only the inside of a `class="..."` attribute is touched. Inline
        `style="background:#DC2626"` and JS literals like
        `stroke = '#DC2626'` are not Tailwind classes and rewriting them would
        produce invalid CSS.
      * An unmapped colour is left exactly as it was. A partial conversion is
        fine — the guardrail test reports the residue. A wrong conversion is
        not, because nothing downstream would catch it.
    """
    def rewrite(match: re.Match) -> str:
        body = match.group(1)
        replacements: list[tuple[str, str]] = []
        light_utilities: set[str] = set()

        # Pass 1 — light arbitrary values become tokens. The KEY includes the
        # variant chain, so `hover:bg` and `bg` never pair with each other.
        light_token: dict[str, str] = {}
        for m in _UTIL.finditer(body):
            is_dark, chain = _split_variants(m.group("variants"))
            if is_dark:
                continue
            token = token_for(m.group("hex"), is_dark=False)
            if token is None:
                continue
            utility = m.group("util") + (m.group("side") or "")
            key = chain + utility
            light_utilities.add(key)
            light_token[key] = token
            replacements.append(
                (m.group(0), f"{chain}{utility}-{token}{m.group('alpha') or ''}"))

        # Pass 2 — a built-in light class (bg-white) standing in for the light
        # half, where its colour genuinely equals the token's light value.
        builtins: dict[str, tuple[str, str]] = {}
        for m in _BUILTIN_UTIL.finditer(body):
            builtins[m.group("util")] = (m.group(0), _BUILTIN_LIGHT[m.group("name")])

        # Pass 3 — the dark half. It may ONLY be dropped when something has
        # actually taken its place. Deleting an orphan is what turned cards
        # white in dark mode.
        removable: list[str] = []
        for m in _UTIL.finditer(body):
            is_dark, chain = _split_variants(m.group("variants"))
            if not is_dark:
                continue
            utility = m.group("util") + (m.group("side") or "")
            key = chain + utility
            hex_value = m.group("hex").upper()

            if key in light_utilities:
                # Redundant ONLY when this dark value IS the dark half of the
                # token the light side chose. Asking the reverse lookup instead
                # is ambiguous: `page` and `raised` share the dark value
                # #0F172A, so `_DARK` silently resolves to whichever was
                # declared last, and `bg-[#F9FAFB] dark:bg-[#0F172A]` would
                # either be left with an orphan or, worse, matched to the wrong
                # token. Comparing against the light side's own token is exact.
                chosen = light_token.get(key)
                if chosen and (TOKENS[chosen][1].upper() == hex_value
                               or _ALIAS_UPPER.get(hex_value) == chosen):
                    # Exact match, or a recorded alias — the three different
                    # darks that pair with the same light grey collapse onto
                    # the canonical one. That consolidation is the intent; it
                    # is listed in ALIASES rather than happening silently.
                    removable.append(m.group(0))
                continue

            token = token_for(hex_value, is_dark=True)
            if token is None:
                continue
            stand_in = builtins.get(m.group("util")) if not chain else None
            if stand_in and stand_in[1].upper() == TOKENS[token][0].upper():
                replacements.append(
                    (stand_in[0], f"{utility}-{token}{m.group('alpha') or ''}"))
                light_utilities.add(key)
                light_token[key] = token
                removable.append(m.group(0))
            # else: leave BOTH halves exactly as they are.

        for original, replacement in replacements:
            body = body.replace(original, replacement, 1)
        for original in removable:
            body = body.replace(original, "", 1)

        return 'class="' + re.sub(r"\s{2,}", " ", body).strip() + '"'

    return _CLASS_ATTR.sub(rewrite, text)


def _channels(hex_value: str) -> str:
    h = hex_value.lstrip("#")
    return " ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def render_css() -> str:
    """The `:root` / `.dark` blocks for static/css/app.css."""
    light = "\n".join(f"  --c-{n}: {_channels(l)};" for n, (l, _d) in TOKENS.items())
    dark = "\n".join(f"  --c-{n}: {_channels(d)};" for n, (_l, d) in TOKENS.items())
    return ":root {\n" + light + "\n}\n\n.dark {\n" + dark + "\n}\n"


def render_tailwind_colors() -> str:
    """The `theme.extend.colors` entries for templates/base.html."""
    return ",\n".join(
        f"            {n}: 'rgb(var(--c-{n}) / <alpha-value>)'" for n in TOKENS)
