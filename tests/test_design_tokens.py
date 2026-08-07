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


# ── classes assigned from JavaScript are still classes ───────────────────
#
# 629 literals live outside any `class="…"` attribute — in `className = '…'`
# assignments and ternary branches that build a class string at runtime. The
# browser applies them exactly like the ones in the markup, so leaving them
# behind would mean those elements sitting out the palette change.

def test_a_classname_assignment_is_converted():
    src = "el.className = 'text-[#6B7280] dark:text-[#CBD5E1] font-medium';"
    assert dt.convert(src) == "el.className = 'text-muted font-medium';"


def test_each_branch_of_a_ternary_is_its_own_class_list():
    """`ok ? 'text-[#10B981]' : 'text-[#DC2626]'` — two strings, only one of
    which is ever applied. They cannot pair with each other."""
    src = "fs.className = 'text-2xl ' + (open === 0 ? 'text-[#10B981]' : 'text-[#DC2626]');"
    out = dt.convert(src)
    assert "'text-healthy'" in out and "'text-critical'" in out


def test_prose_containing_a_utility_is_left_alone():
    """The guard on widening the scope. A class list is almost entirely
    compound words; prose is bare ones. Measured across every candidate
    string in the templates, the only bare words are `rounded`, `border` and
    `flex`, so requiring bare words to be utilities costs nothing and stops
    a sentence from being rewritten as markup."""
    src = "const msg = 'Check the text-[#DC2626] value before saving';"
    assert dt.convert(src) == src


def test_a_template_literal_holding_markup_is_handled_by_its_inner_attribute():
    """A JS string containing `<div class="…">` is not itself a class list —
    the attribute inside it is. Converting the outer string as one would
    treat `<div` as a class."""
    src = 'html += \'<div class="text-[#6B7280] dark:text-[#CBD5E1]">x</div>\';'
    assert dt.convert(src) == 'html += \'<div class="text-muted">x</div>\';'


# ── the families the first twelve tokens had no name for ─────────────────

def test_the_input_surface_resolves_past_three_colliding_darks():
    """`bg-white dark:bg-[#0F172A]` is 157 form inputs — the single largest
    surviving pattern, and unconvertible with twelve tokens: no token was
    #FFFFFF light and #0F172A dark.

    #0F172A is now the dark half of THREE tokens (page, raised, field), so a
    hex->token lookup cannot answer this on its own; the light half has to
    pick. A dict keyed on the dark hex would silently return whichever was
    declared last, which is how a token test passed on a collision before.
    """
    before = '<input class="bg-white dark:bg-[#0F172A] border border-line">'
    assert dt.convert(before) == '<input class="bg-field border border-line">'


def test_a_status_tint_pair_collapses():
    """Badges are a tinted surface with a strong label on it — a different
    role from the status colour itself, and 60 elements spell it by hand."""
    before = '<span class="bg-[#FEE2E2] dark:bg-[#7F1D1D] text-[#991B1B] dark:text-[#FCA5A5]">'
    assert dt.convert(before) == '<span class="bg-critical-tint text-critical-strong">'


def test_a_hyphenated_token_is_not_matched_as_its_own_prefix():
    """`critical-strong` starts with `critical`. If the alternation is not
    ordered longest-first, the converter and the verifier both read
    `text-critical-strong` as `text-critical` followed by stray text, and the
    verifier then reports a difference that is not there."""
    assert dt.convert('<b class="text-critical-strong">') == \
        '<b class="text-critical-strong">'
    import tools.verify_token_equivalence as v
    found = {m.group("token") for m in v._TOK.finditer("text-critical-strong bg-warning-tint")}
    assert found == {"critical-strong", "warning-tint"}, found


def test_page_and_raised_still_resolve_by_their_light_half():
    """The two tokens that already collided on #0F172A must keep resolving
    from the light side, not from declaration order."""
    assert dt.convert('<div class="bg-[#F9FAFB] dark:bg-[#0F172A]">') == '<div class="bg-page">'
    assert dt.convert('<div class="bg-[#F3F4F6] dark:bg-[#0F172A]">') == '<div class="bg-raised">'


# ── the alpha modifier is part of the pair, not decoration ───────────────
#
# 43 paired utilities in the templates carry a DIFFERENT opacity on each half.
# Collapsing those to one class keeps the light alpha and silently restates
# dark mode. The verifier could not see it: `effective()` compared
# (utility, theme, hex) and dropped the modifier.

def test_a_differing_dark_alpha_is_kept_not_absorbed():
    """`bg-[#2563EB]/10 dark:bg-[#3B82F6]/20` — same colours, different
    opacities. One class cannot express both, so the dark half becomes a
    token utility carrying its own alpha instead of being deleted."""
    before = '<span class="bg-[#2563EB]/10 dark:bg-[#3B82F6]/20">'
    assert dt.convert(before) == '<span class="bg-info/10 dark:bg-info/20">'


def test_a_dark_only_alpha_survives_a_solid_light_half():
    """The commonest shape of it — 30 of the 43 are `bg-[#F9FAFB]` against
    `dark:bg-[#0F172A]/50`, a solid light surface and a translucent dark one."""
    before = '<div class="bg-[#F9FAFB] dark:bg-[#0F172A]/50">'
    assert dt.convert(before) == '<div class="bg-page dark:bg-page/50">'


def test_matching_alphas_still_collapse_to_one_class():
    """The fix must not cost the collapse where the opacities do agree."""
    before = '<div class="bg-[#DC2626]/10 dark:bg-[#EF4444]/10">'
    assert dt.convert(before) == '<div class="bg-critical/10">'


def test_a_builtin_light_half_keeps_a_translucent_dark_half():
    before = '<div class="bg-white dark:bg-[#1E293B]/70">'
    assert dt.convert(before) == '<div class="bg-card dark:bg-card/70">'


def test_an_edit_lands_on_the_utility_it_matched_not_the_first_lookalike():
    """`text-[#9CA3AF]` is a substring of `dark:text-[#9CA3AF]`.

    #9CA3AF is the one hex that is both a light token value (`faint`) and an
    aliased dark (`muted`), so both spellings can appear in one class list.
    Substituting by string content rewrote whichever came first — turning
    `dark:text-[#9CA3AF] text-[#9CA3AF]` into `dark:text-faint
    text-[#9CA3AF]`, which tokenises the wrong theme and leaves the other
    half a literal. Edits are applied by span for this reason.
    """
    before = '<p class="dark:text-[#9CA3AF] text-[#9CA3AF]">'
    assert dt.convert(before) == '<p class="dark:text-[#9CA3AF] text-faint">'


# ── splitting the conversion into two reviewable halves ──────────────────
#
# Spec §5: Phase 1a must be visually inert, and the ONE honest exception —
# the utilities that set a light colour with no `dark:` counterpart, which
# leaks straight into dark mode — gets its own commit so those changes are
# inspected rather than absorbed into a 5,000-line diff. `paired_only=True`
# is that split.

def test_paired_only_leaves_an_unpaired_light_utility_alone():
    """Converting this ADDS a dark value where none existed. Real change, so
    it belongs in the commit that lists the changes, not the bulk one."""
    src = '<p class="text-[#6B7280]">x</p>'
    assert dt.convert(src, paired_only=True) == src
    assert dt.convert(src) == '<p class="text-muted">x</p>'


def test_paired_only_still_collapses_a_real_pair():
    """Both halves already exist, so the token renders the same two colours."""
    before = '<span class="text-[#6B7280] dark:text-[#CBD5E1]">x</span>'
    assert dt.convert(before, paired_only=True) == '<span class="text-muted">x</span>'


def test_paired_only_still_collapses_a_builtin_pair():
    """`bg-white dark:bg-[#1E293B]` is paired — the light half just isn't an
    arbitrary value. 275 elements; excluding them would strand the dark half."""
    before = '<div class="bg-white dark:bg-[#1E293B] p-6">'
    assert dt.convert(before, paired_only=True) == '<div class="bg-card p-6">'


def test_paired_only_is_scoped_per_variant_chain():
    """A `dark:hover:` counterpart pairs with `hover:`, not with the base."""
    src = '<a class="text-[#6B7280] dark:hover:text-[#CBD5E1]">'
    assert dt.convert(src, paired_only=True) == src, \
        "the base light utility has no base dark counterpart"


def test_the_two_passes_reach_the_same_place_as_one():
    """The split is a review device, not a semantic change.

    Task 6 (paired) followed by Task 7 (the rest) must land on exactly the
    output a single full pass produces — otherwise the two-commit sequence
    quietly ships something the verifier never checked. Asserted on the real
    templates, because the interesting shapes are the ones nobody would think
    to write by hand.
    """
    for path in sorted((PROJECT_ROOT / "templates").rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        staged = dt.convert(dt.convert(source, paired_only=True))
        assert staged == dt.convert(source), \
            f"{path.name}: two-pass conversion diverges from one-pass"


def test_css_uses_channel_triplets_not_hex():
    """A bare hex inside var() breaks Tailwind's opacity modifier, and 329
    sites depend on it. The format is a requirement, not a preference."""
    css = dt.render_css()
    assert "--c-critical: 220 38 38;" in css
    assert "#" not in css, "no hex may survive into the custom properties"
    assert ":root {" in css and ".dark {" in css


def test_hyphenated_token_keys_are_quoted_in_the_tailwind_map():
    """`critical-tint: '…'` is a JavaScript syntax error.

    The config is an inline <script> in base.html. A syntax error there does
    not degrade gracefully — `tailwind.config` never gets assigned, every
    semantic colour silently falls back to nothing, and the whole application
    loses its styling. Quote the keys.
    """
    js = dt.render_tailwind_colors()
    for name in dt.TOKENS:
        if "-" in name:
            assert f"'{name}':" in js, f"{name} must be quoted"


def test_the_tailwind_map_parses_as_a_javascript_object_literal():
    """Asserting on substrings would not have caught an unquoted key. Parse
    it. JSON is strict enough to reject exactly what JS rejects here."""
    import json
    body = dt.render_tailwind_colors()
    quoted = re.sub(r"^(\s*)'([^']+)':", r'\1"\2":', body, flags=re.M)
    quoted = quoted.replace("'rgb(", '"rgb(').replace(")'", ')"')
    json.loads("{" + quoted + "}")


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
        assert f"'{name}': 'rgb(var(--c-{name})" in js


def test_app_css_matches_the_python_table():
    """app.css says "generated from tools/design_tokens.py" and "tests assert
    these match the Python table". Until this test existed, neither the CSS
    nor the Tailwind map was checked against anything — the header described
    a guarantee that was not being enforced, which is the same shape as the
    three unused colour abstractions this whole exercise replaced.

    Drift here is invisible: a token defined in Python and missing from the
    CSS renders as nothing at all.
    """
    css = (PROJECT_ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    for name, (light, dark) in dt.TOKENS.items():
        assert f"--c-{name}: {dt._channels(light)};" in css, \
            f"--c-{name} missing or stale in the :root block"
        assert f"--c-{name}: {dt._channels(dark)};" in css, \
            f"--c-{name} missing or stale in the .dark block"


def test_base_html_tailwind_map_matches_the_python_table():
    """Same guarantee for the other half. A token present in the CSS but
    absent from `theme.extend.colors` produces a class Tailwind never emits,
    so the element simply has no colour."""
    base = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    for name in dt.TOKENS:
        assert f"'{name}': 'rgb(var(--c-{name}) / <alpha-value>)'" in base, \
            f"{name} missing from tailwind.config in base.html"


def test_every_token_has_distinct_light_and_dark():
    for name, (light, dark) in dt.TOKENS.items():
        assert light.upper() != dark.upper(), f"{name} is identical in both themes"
