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
    """The safety property behind the bug above: a `dark:` utility may only
    be removed when something has actually replaced it.

    It may be RE-SPELLED, which is a different thing — `dark:bg-card` renders
    #1E293B in dark mode and nothing in light mode, exactly as before. What
    must never happen is the value disappearing.
    """
    assert dt.convert('<div class="p-4 dark:bg-[#1E293B]">') == \
        '<div class="p-4 dark:bg-card">'


def test_a_dark_only_utility_becomes_a_dark_token_utility():
    """`dark:border-[#334155]` with no light half at all — 200 of these.

    Inert: in dark mode the token's dark value IS #334155, and in light mode
    the variant does not apply. But it is the difference between the border
    following the palette and staying navy after everything around it moves.
    """
    assert dt.convert('<div class="dark:border-[#334155]">') == \
        '<div class="dark:border-line">'


def test_an_ambiguous_dark_only_utility_is_left_alone():
    """#0F172A is the dark half of page, raised AND field. With no light half
    to disambiguate there is nothing to resolve it, and picking one would be
    a guess that only shows up after the palette splits them apart."""
    src = '<div class="p-4 dark:bg-[#0F172A]">'
    assert dt.convert(src) == src


def test_builtin_is_only_collapsed_when_the_colours_actually_agree():
    """`bg-white` is #FFFFFF. It must not absorb a token whose light value is
    something else, or the light theme silently shifts.

    #334155 is `line`, whose light value is #E5E7EB — so `bg-white` stays
    exactly as it is. The dark half may still be re-spelled on its own,
    which leaves both themes rendering what they rendered before.
    """
    out = dt.convert('<div class="bg-white dark:bg-[#334155]">')
    assert "bg-white" in out, "the light half must not be absorbed"
    assert out == '<div class="bg-white dark:bg-line">'


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
    src = "<script>el.className = 'text-[#6B7280] dark:text-[#CBD5E1] font-medium';</script>"
    assert dt.convert(src) == "<script>el.className = 'text-muted font-medium';</script>"


def test_each_branch_of_a_ternary_is_its_own_class_list():
    """`ok ? 'text-[#10B981]' : 'text-[#DC2626]'` — two strings, only one of
    which is ever applied. They cannot pair with each other."""
    src = ("<script>fs.className = 'text-2xl ' + "
           "(open === 0 ? 'text-[#10B981]' : 'text-[#DC2626]');</script>")
    out = dt.convert(src)
    assert "'text-healthy'" in out and "'text-critical'" in out


def test_javascript_is_only_read_inside_a_script_block():
    """Quote pairing is done with a regex, and it desynchronises the moment
    it meets an HTML attribute or an apostrophe in prose — after which every
    string is mispaired. Confining the scan to <script> removes the HTML from
    the input entirely. A first attempt scanned whole files and found 286 of
    629, silently missing the rest.

    Nothing is lost by the restriction: CSP forbids inline handlers here
    (tests/test_csp.py), so script blocks are the only place JS runs.
    """
    loose = "el.className = 'text-[#6B7280] dark:text-[#CBD5E1]';"
    assert dt.convert(loose) == loose


def test_an_apostrophe_in_a_comment_does_not_swallow_the_next_string():
    """The bug that made one file's class strings invisible while its
    neighbours converted cleanly.

    A regex pairs the apostrophe in `don't` with the next quote further down
    and mispairs every string after it. Strings and comments have to be
    recognised in the same pass, because each can contain what looks like the
    start of the other. This left 218 literals unconverted with no error.
    """
    src = ("<script>\n"
           "// don't let this comment swallow the next string\n"
           "el.className = 'text-[#6B7280] dark:text-[#CBD5E1]';\n"
           "</script>")
    assert "'text-muted'" in dt.convert(src)


def test_a_url_inside_a_string_is_not_read_as_a_comment():
    """The mirror image: `//` inside a string starts no comment."""
    src = ("<script>\n"
           "const u = 'https://example.test/x';\n"
           "el.className = 'text-[#6B7280] dark:text-[#CBD5E1]';\n"
           "</script>")
    out = dt.convert(src)
    assert "'text-muted'" in out and "https://example.test/x" in out


def test_a_jinja_expression_building_a_class_string_is_converted():
    """`{% set cls = ('text-[#F59E0B]' if hot else 'text-[#6B7280]') %}` is a
    class list too, and it is not inside a script block."""
    src = "{% set c = ('text-[#F59E0B] font-semibold' if hot else 'text-[#6B7280]') %}"
    assert dt.convert(src) == \
        "{% set c = ('text-warning font-semibold' if hot else 'text-muted') %}"


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

def test_the_input_surface_resolves_past_a_colliding_light_value():
    """Form inputs — 157 of them, the single largest pattern the twelve-token
    set could not express, because no token was `card` in light mode and
    recessed in dark.

    `card` and `field` are BOTH #FFFFFF in light mode, so a hex->token lookup
    cannot answer this alone; the dark half has to pick. A dict keyed on the
    hex would return whichever was declared last, which is exactly how a
    token test passed on a collision once before.
    """
    light, dark = dt.TOKENS["field"]
    before = f'<input class="bg-[{light}] dark:bg-[{dark}] border border-line">'
    assert dt.convert(before) == '<input class="bg-field border border-line">'
    assert dt.tokens_for(light, is_dark=False) == ["card", "field"], \
        "the collision this resolves must actually exist"


def test_a_literal_from_the_previous_palette_still_resolves():
    """The pre-flip values are kept as folds rather than deleted. A hex
    copied from older markup lands on the right token instead of quietly
    rendering the old palette beside the new one."""
    assert dt.convert('<p class="text-[#6B7280] dark:text-[#CBD5E1]">') == \
        '<p class="text-muted">'
    assert dt.convert('<div class="border-[#E5E7EB]">') == '<div class="border-line">'


def test_the_one_legacy_value_that_cannot_be_folded_is_not_guessed():
    """#0F172A was the dark half of page, raised AND field before the flip,
    and those three are now three different colours. Folding it would mean
    picking one, and the wrong pick is invisible until someone looks."""
    src = '<div class="p-4 dark:bg-[#0F172A]">'
    assert dt.convert(src) == src


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


def test_surfaces_resolve_to_the_token_whose_BOTH_halves_match():
    """page, card, raised and field are four surfaces that overlap in one
    theme or the other. Each pair must land on the token matching both."""
    for name in ("page", "card", "raised", "field"):
        light, dark = dt.TOKENS[name]
        assert dt.convert(f'<div class="bg-[{light}] dark:bg-[{dark}]">') == \
            f'<div class="bg-{name}">', name


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
    """The commonest shape of it — 30 of the 43 were a solid light surface
    against a translucent dark one."""
    light, dark = dt.TOKENS["page"]
    before = f'<div class="bg-[{light}] dark:bg-[{dark}]/50">'
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
    assert "#" not in css, "no hex may survive into the custom properties"
    assert ":root {" in css and ".dark {" in css
    # Derived, not transcribed — a hardcoded triplet here just breaks every
    # time the palette moves and teaches nothing.
    for name, (light, dark) in dt.TOKENS.items():
        assert f"--c-{name}: {dt._channels(light)};" in css
        assert f"--c-{name}: {dt._channels(dark)};" in css


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


# ── the guardrail ────────────────────────────────────────────────────────
#
# Spec §7 asked for a test that fails on any new `[#hex]`, with a short
# allowlist of justified one-offs. That shape assumed the conversion would
# reach zero. It reached 188 of an original 5,184 — 96.4% — and every
# survivor has a real reason, measured:
#
#   100  a LIGHT utility whose colour is not a light token value. Mostly
#        cross-theme oddities (`text-[#3B82F6]` in light mode, where #3B82F6
#        is info's DARK value, x16) plus deliberate chrome: the workflow
#        guide panel (#404040 x10) and inline code chips (#1A1A1A x7), which
#        are meant to look the same in both themes.
#    60  a DARK utility whose colour is not a dark token value —
#        `dark:bg-[#F9FAFB]` x11, `dark:text-[#6B7280]` x9. Several are
#        probably contrast bugs; none are conversion failures.
#    28  outside any class scope the converter can identify. The scanner
#        reads <script> blocks and Jinja expressions; a class string built
#        by concatenation across statements is beyond a regex.
#
# An allowlist of forty-odd colours would assert nothing. A RATCHET does:
# the count per file may fall, never rise. That is the property §7 actually
# wanted — "without it this regrows" — and it is enforceable.

LITERAL_BASELINE: dict[str, int] = {
    "server_detail.html": 47,
    "workflows.html": 33,
    "dashboard.html": 16,
    "rbac.html": 14,
    "reports.html": 12,
    "servers.html": 9,
    "settings.html": 10,
    "partials/active_actions.html": 7,
    "monitoring.html": 6,
    "partials/server_card.html": 6,
    "compliance.html": 5,
    "partials/incidents_panel.html": 5,
    "partials/verdict_header.html": 5,
    "operations.html": 4,
    "partials/server_comparison.html": 4,
    "base.html": 1,
    "partials/critical_issues.html": 1,
    "setup.html": 1,
}

_LITERAL = re.compile(r"-\[#[0-9A-Fa-f]{6}\]")


def _literal_counts() -> dict[str, int]:
    templates = PROJECT_ROOT / "templates"
    return {p.relative_to(templates).as_posix(): n
            for p in sorted(templates.rglob("*.html"))
            if (n := len(_LITERAL.findall(p.read_text(encoding="utf-8"))))}


def test_hardcoded_colour_literals_never_increase():
    """Three colour abstractions already existed in this repository and all
    three were referenced by nothing, because typing a hex was easier and
    nothing ever failed. This is the thing that fails."""
    counts = _literal_counts()
    grew = [f"{f}: {n} literal(s), baseline {LITERAL_BASELINE.get(f, 0)}"
            for f, n in counts.items() if n > LITERAL_BASELINE.get(f, 0)]
    assert not grew, (
        "hardcoded colour literals increased. Use a token from "
        "tools/design_tokens.TOKENS, or run tools/migrate_tokens.py:\n  "
        + "\n  ".join(grew))


def test_the_baseline_is_not_left_behind_when_literals_are_removed():
    """A ratchet that is never tightened is a ratchet in name only. If the
    real count has dropped, the baseline is stale and must come down with
    it — otherwise it silently buys back the headroom that was just won."""
    counts = _literal_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in LITERAL_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER literals than the baseline; lower it:\n  "
        + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_colour_literal_outside_the_templates_that_already_have_one():
    """A brand-new template must start clean. The baseline records history;
    it is not a licence to add the next one somewhere else."""
    new = sorted(set(_literal_counts()) - set(LITERAL_BASELINE))
    assert not new, f"new template(s) with hardcoded colours: {new}"


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


# ── contrast is a build failure, not a matter of taste ───────────────────

def _relative_luminance(hex_value: str) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = hex_value.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_contrast_helper_agrees_with_known_values():
    """A contrast test built on a broken formula passes everything. Black on
    white is exactly 21:1 and any colour on itself is exactly 1:1."""
    assert round(contrast("#000000", "#FFFFFF"), 2) == 21.0
    assert round(contrast("#777777", "#777777"), 2) == 1.0
    assert 4.5 <= contrast("#767676", "#FFFFFF") <= 4.6   # the AA boundary grey


TEXT_TOKENS = ("ink", "muted", "faint", "brand", "accent", "info",
               "healthy", "warning", "critical")


def test_every_text_token_clears_wcag_aa_on_its_card():
    """A palette that fails contrast is a defect, not a style choice.

    Of the three catalog themes inspected while choosing this one, two had a
    dark `destructive` at 1.33:1 and an `accent-foreground` at 1.15:1 —
    invisible, and shipped. The palette Prism replaced failed six of eight,
    with `faint` at 1.93:1 in dark mode.
    """
    for index, theme in ((0, "light"), (1, "dark")):
        card = dt.TOKENS["card"][index]
        for name in TEXT_TOKENS:
            ratio = contrast(dt.TOKENS[name][index], card)
            assert ratio >= 4.5, (
                f"{name} on card is {ratio:.2f}:1 in {theme} mode")


def test_every_status_label_clears_aa_on_its_own_tint():
    """A badge's label sits on the tint, not on the card. Checking it against
    the card would pass a combination nothing ever renders.

    `accent` and `brand` were added tokenising the update/restart lifecycle
    badges — the label there is `-strong` on `-tint` exactly like the four
    status colours, so it is measured the same way rather than trusted on
    the strength of "it looks like the others"."""
    for index, theme in ((0, "light"), (1, "dark")):
        for status in ("critical", "warning", "healthy", "info",
                        "accent", "brand"):
            tint = dt.TOKENS[f"{status}-tint"][index]
            strong = dt.TOKENS[f"{status}-strong"][index]
            ratio = contrast(strong, tint)
            assert ratio >= 4.5, (
                f"{status}-strong on {status}-tint is {ratio:.2f}:1 in {theme}")


# ── the lifecycle badges collapse seven colours to three ─────────────────
#
# queued/searching/downloading fetch (turquoise); installing/rebooting/
# restart_required apply the change or wait on a human (violet);
# stabilising settles back in (green, unchanged). Motion — which of these
# is the machine working on versus waiting on you — is tested in
# tests/test_design_motion.py; this only pins the colour bucket, so a
# regression that quietly puts `installing` back on amber (which reads as a
# warning, the exact confusion the collapse removes) fails here.
LIFECYCLE_COLOUR_BUCKET = {
    "queued": "accent", "searching": "accent", "downloading": "accent",
    "installing": "brand", "rebooting": "brand", "restart_required": "brand",
    "stabilising": "healthy",
}


def test_lifecycle_badges_use_their_collapsed_colour_bucket():
    css = (PROJECT_ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    for status, bucket in LIFECYCLE_COLOUR_BUCKET.items():
        for prefix, theme in (("", "light"), (r"\.dark ", "dark")):
            # Anchored at the START of the line: an unanchored pattern with
            # prefix="" also matches inside ".dark .badge-x { ... }", so a
            # broken LIGHT rule was invisible to this check as long as the
            # (still-correct) DARK rule happened to sit later in the file —
            # caught by mutating `.badge-installing` back to warning and
            # watching this assertion stay green.
            pattern = (rf"(?m)^{prefix}\.badge-{status}\s*\{{\s*"
                       rf"background:\s*rgb\(var\(--c-{bucket}-tint\)\);\s*"
                       rf"color:\s*rgb\(var\(--c-{bucket}-strong\)\);")
            assert re.search(pattern, css), (
                f".badge-{status} ({theme}) must render {bucket}-tint / "
                f"{bucket}-strong")


def test_the_surfaces_stay_distinguishable_from_each_other():
    """page, card and raised have to read as different planes. `field` is
    deliberately identical to `card` in light mode — a white input on a white
    card, separated by its border, which is what it has always been."""
    for index, theme in ((0, "light"), (1, "dark")):
        for a, b in (("page", "card"), ("card", "raised")):
            ratio = contrast(dt.TOKENS[a][index], dt.TOKENS[b][index])
            assert ratio >= 1.05, (
                f"{a} and {b} are {ratio:.3f}:1 apart in {theme} — "
                "indistinguishable")


def test_every_token_has_distinct_light_and_dark():
    for name, (light, dark) in dt.TOKENS.items():
        assert light.upper() != dark.upper(), f"{name} is identical in both themes"
