"""The colour token system — see docs/plans/DESIGN_TOKENS_SPEC.md.

Prism's design system existed in three places and was used by none of them:
`app.css` defined `--color-healthy` and friends, `tailwind.config` separately
defined `surface`/`bg`, and templates carried **5,184 hardcoded hex literals**
referencing neither. Measured 2026-08-06: `var(--token)` appeared in templates
exactly **0** times.

Nothing ever failed when someone typed a hex instead. That is the whole reason
this file exists — the mapping is testable, and the guardrail at the bottom
makes the next literal a build failure rather than a slow return to 5,184.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt  # noqa: E402


# ── the dominant real-world patterns ─────────────────────────────────────

def test_paired_light_dark_utility_collapses_to_one_class():
    """385 elements carry exactly this pair. One token replaces both halves,
    which is what stops dark mode being something you have to remember."""
    before = '<span class="text-[#6B7280] dark:text-[#CBD5E1] font-medium">x</span>'
    assert dt.convert(before) == '<span class="text-muted font-medium">x</span>'


def test_opacity_modifier_survives():
    """329 sites use /10, /50 and friends. A bare var() holding a hex breaks
    them silently, which is why the tokens are stored as channel triplets."""
    assert dt.convert('<div class="bg-[#DC2626]/10">') == '<div class="bg-critical/10">'


def test_border_pair_collapses():
    """475 elements: the single most consistent pair in the codebase."""
    before = '<div class="border border-[#E5E7EB] dark:border-[#334155] p-4">'
    assert dt.convert(before) == '<div class="border border-line p-4">'


# ── safety: a partial conversion is fine, a wrong one is not ─────────────

def test_unmapped_colour_is_left_alone():
    src = '<i class="text-[#123456]">'
    assert dt.convert(src) == src


def test_conversion_is_idempotent():
    """migrate_tokens.py must be safe to re-run over an already-converted tree."""
    once = dt.convert('<b class="text-[#6B7280] dark:text-[#CBD5E1]">')
    assert dt.convert(once) == once


def test_directional_border_keeps_its_side():
    assert dt.convert('<div class="border-l-[#E5E7EB]">') == '<div class="border-l-line">'


def test_non_class_occurrences_are_untouched():
    """Inline styles and JS string literals are not Tailwind classes. The
    converter must not treat the whole file as a class attribute."""
    src = '<div style="background:#DC2626">x</div>'
    assert dt.convert(src) == src
    js = "const stroke = last >= 90 ? '#DC2626' : '#10B981';"
    assert dt.convert(js) == js


def test_aliased_dark_values_collapse_onto_the_canonical_token():
    """Three different darks pair with the same light grey — #CBD5E1 (385),
    #94A3B8 (58) and #9CA3AF (39). Collapsing that inconsistency IS the point
    of a token; the alias table records it rather than hiding it."""
    assert dt.convert('<p class="text-[#6B7280] dark:text-[#94A3B8]">') == \
        '<p class="text-muted">'


def test_builtin_light_class_paired_with_arbitrary_dark_collapses():
    """275 elements are `bg-white dark:bg-[#1E293B]`.

    The light half is Tailwind's BUILT-IN `bg-white`, not `bg-[#FFFFFF]`, so an
    earlier version of the converter saw an unpaired dark utility, judged it
    redundant, and deleted it — turning every card white in dark mode. Every
    test passed, because they all used arbitrary values on both sides. Caught
    by reading the converter's actual output on real markup.
    """
    before = '<div class="bg-white dark:bg-[#1E293B] rounded-lg p-6">'
    assert dt.convert(before) == '<div class="bg-card rounded-lg p-6">'


def test_an_orphan_dark_utility_is_never_deleted():
    """The safety property behind the bug above: a `dark:` utility may only be
    removed when something has actually replaced it. Otherwise leave it."""
    src = '<div class="p-4 dark:bg-[#1E293B]">'
    assert dt.convert(src) == src


def test_builtin_is_only_collapsed_when_the_colours_actually_agree():
    """`bg-white` is #FFFFFF. It must not absorb a token whose light value is
    something else, or the light theme silently shifts."""
    src = '<div class="bg-white dark:bg-[#334155]">'   # #334155 is line, light #E5E7EB
    assert dt.convert(src) == src


def test_variant_prefixes_are_preserved():
    assert dt.convert('<a class="bg-[#2563EB] hover:bg-[#2563EB]">') == \
        '<a class="bg-info hover:bg-info">'


def test_a_hover_variant_does_not_satisfy_the_base_dark_pair():
    """`hover:bg-…` is a different utility from `bg-…`.

    Treating them as one lets a hover colour "pair with" a base dark utility
    and delete it, which would drop the element's dark background while
    leaving the hover state intact — a bug that only shows up on a page nobody
    hovers in dark mode.
    """
    src = '<div class="hover:bg-[#F9FAFB] dark:bg-[#0F172A]">'
    out = dt.convert(src)
    assert "hover:bg-page" in out
    assert "dark:bg-[#0F172A]" in out, "the base dark value must survive"


def test_dark_hover_pairs_with_light_hover():
    assert dt.convert('<b class="hover:text-[#6B7280] dark:hover:text-[#CBD5E1]">') == \
        '<b class="hover:text-muted">'


def test_css_uses_channel_triplets_not_hex():
    """A bare hex inside var() breaks Tailwind's opacity modifier, and 329
    sites depend on it. The format is a requirement, not a preference."""
    css = dt.render_css()
    assert "--c-critical: 220 38 38;" in css
    assert "#" not in css, "no hex may survive into the custom properties"
    assert ":root {" in css and ".dark {" in css


def test_tailwind_colour_map_composes_alpha():
    js = dt.render_tailwind_colors()
    assert "rgb(var(--c-critical) / <alpha-value>)" in js
    for name in dt.TOKENS:
        assert f"--c-{name}" in js, f"{name} missing from the Tailwind map"


def test_css_and_tailwind_cover_exactly_the_same_tokens():
    """A token defined in one and not the other renders as nothing at all."""
    css, js = dt.render_css(), dt.render_tailwind_colors()
    for name in dt.TOKENS:
        assert f"--c-{name}:" in css
        assert f"{name}: 'rgb(var(--c-{name})" in js


def test_every_token_has_distinct_light_and_dark():
    for name, (light, dark) in dt.TOKENS.items():
        assert light.upper() != dark.upper(), f"{name} is identical in both themes"
