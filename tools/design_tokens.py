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
    "field":    ("#FFFFFF", "#0F172A"),   # input / select / textarea surface
    "line":     ("#E5E7EB", "#334155"),   # borders and dividers
    "ink":      ("#111827", "#F1F5F9"),   # primary text
    "muted":    ("#6B7280", "#CBD5E1"),   # secondary text
    "faint":    ("#9CA3AF", "#475569"),   # tertiary text, placeholders
    "brand":    ("#8B5CF6", "#A78BFA"),   # violet end of the brand gradient
    "accent":   ("#14B8A6", "#2DD4BF"),   # turquoise end of the same gradient
    "info":     ("#2563EB", "#3B82F6"),
    "healthy":  ("#10B981", "#34D399"),
    "warning":  ("#F59E0B", "#FBBF24"),
    "critical": ("#DC2626", "#EF4444"),
    # A badge is a tinted SURFACE with a strong label on it. That is a
    # different role from the status colour, and 60 elements spelled it by
    # hand: a pale wash in light mode, a deep one in dark, with the text
    # inverting between them. Measured pairs, not invented.
    "critical-tint":   ("#FEE2E2", "#7F1D1D"),
    "critical-strong": ("#991B1B", "#FCA5A5"),
    "warning-tint":    ("#FEF3C7", "#78350F"),
    "warning-strong":  ("#92400E", "#FCD34D"),
    "healthy-tint":    ("#D1FAE5", "#064E3B"),
    "healthy-strong":  ("#065F46", "#6EE7B7"),
    "info-tint":       ("#E0E7FF", "#312E81"),
    "info-strong":     ("#1D4ED8", "#60A5FA"),
}

# Values that were paired inconsistently and collapse onto a canonical token.
# Recorded here rather than folded in silently, because each is a small
# deliberate change to what that element renders as.
#
# `_LIGHT` and `_DARK` are separate tables: the same hex can be a light value
# for one token and a dark value for another (#F1F5F9 is ink's dark half and
# a light near-neighbour of raised), so a single alias map would be wrong in
# one of the two directions.
ALIASES: dict[str, str] = {
    "#94A3B8": "muted",   # 58 uses as the dark half of muted
    "#9CA3AF": "muted",   # 39 uses as the dark half of muted (light: faint)
    "#64748B": "faint",   # 14 uses as the dark half of faint
    "#4B5563": "faint",
    "#D1D5DB": "line",
}

# Light-side neighbours, folded onto the nearest token. Contrast on the light
# card measured before -> after, because "nearest in RGB" is not the same
# question as "still readable":
LIGHT_ALIASES: dict[str, str] = {
    "#1F2937": "ink",     # 24x body text.  14.68:1 -> 17.74:1
    "#374151": "ink",     # 16x body text.  10.31:1 -> 17.74:1, the largest
                          #     single fold: a secondary text level that the
                          #     ink/muted/faint scale already covers.
    "#4B5563": "muted",   #  4x text.        7.56:1 ->  4.83:1
    "#D1D5DB": "line",    # 26x border/fill.  1.47:1 ->  1.24:1
    "#F1F5F9": "raised",  #  5x surface.    RGB distance 3.7 — imperceptible
    "#94A3B8": "faint",   #  8x text.        2.56:1 ->  2.54:1
}
# Dark-side neighbours of `ink`, both paired with a light half that folds onto
# ink above, so the pair stays a pair.
DARK_ALIASES: dict[str, str] = {
    "#E2E8F0": "ink",     # 15x, pairs with #374151.  11.87:1 -> 13.35:1
    "#F8FAFC": "ink",     # 10x, pairs with #1F2937.  13.98:1 -> 13.35:1
}

_ALIAS_UPPER: dict[str, str] = {h.upper(): t for h, t in ALIASES.items()}
for _hex, _token in {**DARK_ALIASES}.items():
    _ALIAS_UPPER.setdefault(_hex.upper(), _token)


def _by_hex(index: int) -> dict[str, list[str]]:
    """hex -> EVERY token holding it in that slot, in declaration order.

    A plain dict here is a trap the repository has already fallen into: `page`
    and `raised` both hold #0F172A, `card` and `field` both hold #FFFFFF, and
    a last-wins mapping resolves to whichever happened to be declared later.
    A token test once passed for exactly that reason. Callers that can
    disambiguate — the light half of a pair, a built-in stand-in — do so
    explicitly against this list.
    """
    out: dict[str, list[str]] = {}
    for name, pair in TOKENS.items():
        out.setdefault(pair[index].upper(), []).append(name)
    return out


_LIGHT_ALL = _by_hex(0)
_DARK_ALL = _by_hex(1)
for _hex, _token in LIGHT_ALIASES.items():
    _LIGHT_ALL.setdefault(_hex.upper(), []).append(_token)
for _hex, _token in {**ALIASES, **DARK_ALIASES}.items():
    _DARK_ALL.setdefault(_hex.upper(), []).append(_token)

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


def token_alternation() -> str:
    """The token names as a regex alternation, LONGEST FIRST.

    Python's `|` is first-match, not longest-match, so a bare
    `"|".join(TOKENS)` lets `critical` win against `critical-strong` and the
    pattern then reads `text-critical-strong` as `text-critical` with three
    stray characters after it. Silent, and it makes the verifier report a
    difference that does not exist.
    """
    return "|".join(sorted(TOKENS, key=len, reverse=True))


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


def tokens_for(hex_value: str, is_dark: bool) -> list[str]:
    """Every token holding this literal in this theme. Empty if unmapped."""
    return (_DARK_ALL if is_dark else _LIGHT_ALL).get(hex_value.upper(), [])


def token_for(hex_value: str, is_dark: bool, other_half: str | None = None
              ) -> str | None:
    """Which token owns this literal? None if unmapped or still ambiguous.

    `other_half` is the colour the SAME utility sets in the opposite theme,
    when there is one. It is the only thing that can separate tokens sharing a
    value — #0F172A is the dark half of page, raised AND field, and #FFFFFF is
    the light half of both card and field. Guessing here is how 157 form
    inputs would have become cards.
    """
    candidates = tokens_for(hex_value, is_dark)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    if other_half is not None:
        slot = 0 if is_dark else 1
        exact = [t for t in candidates
                 if TOKENS[t][slot].upper() == other_half.upper()]
        if len(exact) == 1:
            return exact[0]
    return None


def convert(text: str, paired_only: bool = False) -> str:
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

    `paired_only=True` additionally skips any light utility that has no
    `dark:` counterpart. Those 870 utilities leak their light colour straight
    into dark mode, so tokenising one is a real change — the spec puts them in
    their own commit, listed, rather than buried in the mechanical bulk. The
    two passes provably land where a single pass would; there is a test over
    the real templates asserting exactly that.
    """
    def rewrite(match: re.Match) -> str:
        body = match.group(1)
        # Edits are (start, end, text) SPANS, applied right-to-left, never
        # `str.replace`. `bg-[#F9FAFB]` is a prefix of both `hover:bg-[#F9FAFB]`
        # and `bg-[#F9FAFB]/50`, so a substring edit can land inside a
        # neighbouring utility it does not own — and which utility gets
        # corrupted depends on the order the matches happen to be in.
        edits: list[tuple[int, int, str]] = []
        light_utilities: set[str] = set()

        # Which utilities have a dark half at all? Needed BEFORE pass 1 under
        # `paired_only`, and keyed by the full variant chain so `hover:` and
        # the base utility are never mistaken for each other.
        has_dark: set[str] = set()
        dark_hex: dict[str, str] = {}
        for m in _UTIL.finditer(body):
            is_dark, chain = _split_variants(m.group("variants"))
            if is_dark:
                k = chain + m.group("util") + (m.group("side") or "")
                has_dark.add(k)
                dark_hex[k] = m.group("hex")

        # Pass 1 — light arbitrary values become tokens. The KEY includes the
        # variant chain, so `hover:bg` and `bg` never pair with each other.
        light_token: dict[str, str] = {}
        light_alpha: dict[str, str] = {}
        for m in _UTIL.finditer(body):
            is_dark, chain = _split_variants(m.group("variants"))
            if is_dark:
                continue
            utility = m.group("util") + (m.group("side") or "")
            key = chain + utility
            token = token_for(m.group("hex"), is_dark=False,
                              other_half=dark_hex.get(key))
            if token is None:
                continue
            if paired_only and key not in has_dark:
                continue
            alpha = m.group("alpha") or ""
            light_utilities.add(key)
            light_token[key] = token
            light_alpha[key] = alpha
            edits.append((*m.span(), f"{chain}{utility}-{token}{alpha}"))

        # Pass 2 — a built-in light class (bg-white) standing in for the light
        # half, where its colour genuinely equals the token's light value.
        builtins: dict[str, tuple[int, int, str]] = {}
        for m in _BUILTIN_UTIL.finditer(body):
            builtins[m.group("util")] = (*m.span(), _BUILTIN_LIGHT[m.group("name")])

        # Pass 3 — the dark half. It may ONLY be dropped when something has
        # actually taken its place. Deleting an orphan is what turned cards
        # white in dark mode.
        for m in _UTIL.finditer(body):
            is_dark, chain = _split_variants(m.group("variants"))
            if not is_dark:
                continue
            utility = m.group("util") + (m.group("side") or "")
            key = chain + utility
            hex_value = m.group("hex").upper()
            alpha = m.group("alpha") or ""

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
                    if alpha == light_alpha.get(key, ""):
                        edits.append((*m.span(), ""))
                    else:
                        # Same colour, different opacity — 43 sites do this.
                        # One class cannot carry two alphas, so the dark half
                        # stays as its own utility. Dropping it would restate
                        # dark mode at the light half's opacity.
                        edits.append(
                            (*m.span(), f"dark:{chain}{utility}-{chosen}{alpha}"))
                continue

            stand_in = builtins.get(m.group("util")) if not chain else None
            # The stand-in's colour is the light half, so it disambiguates:
            # #0F172A belongs to page, raised AND field, and only `field` is
            # also #FFFFFF in light mode.
            token = token_for(hex_value, is_dark=True,
                              other_half=stand_in[2] if stand_in else None)
            if token is None:
                continue
            if stand_in and stand_in[2].upper() == TOKENS[token][0].upper():
                edits.append((stand_in[0], stand_in[1], f"{utility}-{token}"))
                edits.append((*m.span(),
                              "" if not alpha else f"dark:{utility}-{token}{alpha}"))
                light_utilities.add(key)
                light_token[key] = token
                light_alpha[key] = ""
            # else: leave BOTH halves exactly as they are.

        for start, end, text in sorted(edits, reverse=True):
            body = body[:start] + text + body[end:]

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
    """The `theme.extend.colors` entries for templates/base.html.

    Every key is quoted, not just the hyphenated ones. `critical-tint:` is a
    JavaScript syntax error, and the config is an inline <script> — a syntax
    error there means `tailwind.config` is never assigned at all and the
    application loses its entire theme, not one colour.
    """
    return ",\n".join(
        f"            '{n}': 'rgb(var(--c-{n}) / <alpha-value>)'" for n in TOKENS)
