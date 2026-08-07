"""Tokenising static/css/app.css — see tools/migrate_css_tokens.py.

`app.css` is where the tokens are DEFINED, and it was the one file that did
not use them: 210 colour occurrences outside the generated block, 68 distinct
colours, and the only `var(--c-*)` in the file was inside a comment. After the
C-Ink flip those 210 sites were the previous palette rendering beside the new
one.

The conversion here is NOT colour-preserving — that is the point of it — so
"the output looks different" proves nothing on its own. Every test below fixes
a specific way of getting it wrong instead: the wrong THEME, the wrong token
out of several holding the same value, a dropped alpha, a deleted rule, or a
verifier that agrees with the converter instead of checking it.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt              # noqa: E402
from tools import migrate_css_tokens as mct        # noqa: E402
from tools import verify_css_tokens as vct         # noqa: E402

APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"


def sheet() -> str:
    return APP_CSS.read_text(encoding="utf-8")


# ── the theme is the selector ────────────────────────────────────────────
#
# The crux. A declaration inside a rule whose selector contains `.dark`
# renders in dark mode, and several literals in this sheet belong to a
# DIFFERENT token in each theme. Resolving against the wrong table yields a
# real token holding a real colour — the wrong one — and it is only visible in
# one theme, which is the one nobody screenshots.

def test_the_same_literal_takes_a_different_token_in_each_theme():
    """#9CA3AF is `faint` in light (LEGACY_LIGHT) and `muted` in dark
    (ALIASES). Both spellings occur in app.css — `.breadcrumb-sep` and
    `.dark .badge-offline` — so a theme-blind converter does not merely miss
    one, it converts one of them to a token that renders the wrong grey."""
    assert dt.tokens_for("#9CA3AF", is_dark=False) == ["faint"]
    assert dt.tokens_for("#9CA3AF", is_dark=True) == ["muted"]
    assert mct.convert(".x { color: #9CA3AF; }") == \
        ".x { color: rgb(var(--c-faint)); }"
    assert mct.convert(".dark .x { color: #9CA3AF; }") == \
        ".dark .x { color: rgb(var(--c-muted)); }"


def test_a_dark_rule_nested_in_a_media_block_is_still_dark():
    """`.dark` may sit on an enclosing at-rule's descendant, not only on the
    innermost selector. Checking the selector alone reads every rule inside
    `@media` as light."""
    src = "@media (max-width: 767px) { .dark .x { color: #CBD5E1; } }"
    assert "rgb(var(--c-muted))" in mct.convert(src)


def test_the_two_theme_derivations_agree_on_the_real_sheet():
    """The verifier deliberately derives the theme itself rather than calling
    the converter, so that a fault in the converter is caught instead of
    agreed with. Deliberate duplication drifts unless something watches it —
    which is the failure mode that produced 128 phantom differences in
    verify_token_equivalence.py's first version."""
    css = sheet()
    for decl in mct.declarations(css):
        assert mct.is_dark_rule(decl) == vct._is_dark(decl), decl


def test_the_two_pairing_keys_agree_on_the_real_sheet():
    """Same reason, for the other duplicated decision. The converter and the
    verifier must consider the same two rules to be each other's counterpart,
    or the verifier grades a disambiguation it did not reconstruct."""
    css = sheet()
    for decl in mct.declarations(css):
        for index in range(3):
            assert mct._key(decl, index) == vct._light_key(decl, index), decl


# ── ambiguity is resolved from context, or not at all ────────────────────

def test_an_ambiguous_hex_with_no_counterpart_is_left_alone():
    """#FFFFFF is BOTH `card` and `field` in light mode. `.btn-brand { color:
    #fff }` has no `.dark .btn-brand` to resolve it, and picking the first
    candidate would look correct today — card and field are the same colour —
    and become wrong the moment the palette pulls them apart. That is exactly
    the class of bug this toolchain exists to prevent, so it must stay a
    literal."""
    assert dt.tokens_for("#FFFFFF", is_dark=False) == ["card", "field"]
    src = ".btn-brand { color: #fff; }"
    assert mct.convert(src) == src


def test_an_ambiguous_hex_is_resolved_by_its_paired_rule():
    """`.pulse-panel` is white in light and #1E293B in dark. #1E293B resolves
    to `card` alone, and intersecting that with {card, field} leaves one
    answer. 11 of the 17 ambiguous occurrences resolve this way — every badge
    label, whose dark value is simultaneously the status colour and the
    -strong colour."""
    src = (".pulse-panel { background: #FFFFFF; }\n"
           ".dark .pulse-panel { background: #1E293B; }\n")
    out = mct.convert(src)
    assert "background: rgb(var(--c-card));" in out
    assert out.count("rgb(var(--c-card))") == 2


def test_a_badge_label_takes_the_strong_token_not_the_status_one():
    """#FCD34D is `warning` AND `warning-strong` in dark. The light half is
    #92400E, which only `warning-strong` holds, so the pair decides it. Guess
    `warning` instead and the badge label goes on rendering the same yellow
    until someone changes the status colour alone."""
    assert dt.tokens_for("#FCD34D", is_dark=True) == ["warning", "warning-strong"]
    src = (".badge-warning { color: #92400E; }\n"
           ".dark .badge-warning { color: #FCD34D; }\n")
    out = mct.convert(src)
    assert out.count("rgb(var(--c-warning-strong))") == 2
    assert "--c-warning)" not in out


def test_two_conflicting_counterparts_disambiguate_nothing():
    """The sheet declares `::-webkit-scrollbar-thumb:hover` twice — once in
    the original scrollbar block and once in the brand section that overrides
    it. A pairing table that resolved to whichever came last would be a guess
    wearing a justification."""
    src = (".x { background: #FFFFFF; }\n"
           ".dark .x { background: #1E293B; }\n"      # -> card
           ".dark .x { background: #080E1A; }\n")     # -> field
    assert "#FFFFFF" in mct.convert(src), \
        "two counterparts naming different tokens single out neither"


# ── alpha is part of the colour ──────────────────────────────────────────

def test_rgba_becomes_a_token_with_the_same_alpha():
    """A translucent wash must stay translucent — dropping the alpha turns it
    into a solid fill, which is the whole card rather than a tint on it.

    Deliberately NOT a `--brand-*` property: those are reserved (see
    RESERVED_PROPERTY), so using one here would assert the reservation rather
    than the alpha handling and would pass for the wrong reason.
    """
    src = ".dark .panel { background: rgba(167, 139, 250, 0.14); }"
    assert mct.convert(src) == \
        ".dark .panel { background: rgb(var(--c-brand) / 0.14); }"


def test_the_alpha_text_is_copied_through_verbatim():
    """0.40 must not be reformatted to 0.4. It renders identically and it
    makes the diff unreadable, which is how a real change hides in one."""
    assert mct.convert(".x { color: rgba(107, 114, 128, 0.40); }") == \
        ".x { color: rgb(var(--c-muted) / 0.40); }"


def test_an_eight_digit_hex_keeps_its_alpha():
    """None are present today. The converter accepts the form, so it has to
    carry the alpha rather than silently discard the last byte — a colour that
    quietly becomes opaque is worse than one that stays a literal."""
    assert mct.convert(".x { color: #6B7280CC; }") == \
        ".x { color: rgb(var(--c-muted) / 0.8); }"
    assert mct.convert(".x { color: #6B7280FF; }") == \
        ".x { color: rgb(var(--c-muted) / 1); }"


# ── a value is converted whole, or not at all ────────────────────────────

def test_a_partly_resolvable_gradient_keeps_every_stop():
    """A gradient is a RAMP. `--brand-grad` is
    `linear-gradient(95deg, #7C3AED 0%, #8B5CF6 55%, #2DD4BF 100%)`: the outer
    stops have no light token, the middle one folds onto `brand` (#5B21B6),
    and converting only the middle makes the ramp run violet -> DARKER violet
    -> turquoise. It reverses direction halfway, which is not a palette change
    but a shape change."""
    src = (":root { --brand-grad: linear-gradient(95deg, #7C3AED 0%, "
           "#8B5CF6 55%, #2DD4BF 100%); }")
    assert mct.convert(src) == src


def test_a_fully_resolvable_multi_colour_value_is_converted():
    """The restriction is about mixed resolution, not about arity. Firefox's
    `scrollbar-color` takes thumb and track in one declaration and both halves
    of the dark one resolve."""
    assert mct.convert(".dark * { scrollbar-color: #475569 #1E293B; }") == \
        ".dark * { scrollbar-color: rgb(var(--c-faint)) rgb(var(--c-card)); }"


# ── the things that must never happen ────────────────────────────────────

def test_the_generated_block_is_untouched():
    """`:root`/`.dark` at the top of app.css is emitted by
    `design_tokens.render_css()` and asserted by tests/test_design_tokens.py.
    Rewriting `--c-page: 241 245 249` in terms of `var(--c-page)` is a cycle,
    and CSS resolves a cyclic custom property to nothing at all — the whole
    palette would silently evaporate."""
    css = sheet()
    before = mct.GENERATED_BLOCK.search(css)
    after = mct.GENERATED_BLOCK.search(mct.convert(css))
    assert before and after
    assert before.group(0) == after.group(0)


def test_no_rule_and_no_declaration_is_ever_removed():
    """A redundant `.dark` override — `.metric-bar` and `.dark .metric-bar`
    both resolve to `line` — is harmless and stays. Deleting is where this
    goes wrong: the equivalent converter for the templates deleted an
    apparently-redundant `dark:bg-[#1E293B]` and would have turned 275 cards
    white in dark mode, with every unit test still passing."""
    css = sheet()
    before = [(d.at_chain, d.selector, d.prop) for d in mct.declarations(css)]
    after = [(d.at_chain, d.selector, d.prop)
             for d in mct.declarations(mct.convert(css))]
    assert before == after


def test_a_hex_inside_a_comment_is_not_converted():
    """Comments carry hexes as documentation. Rewriting one produces a comment
    that describes a colour nobody can look up any more."""
    src = "/* was #6B7280 */\n.x { color: #6B7280; }"
    out = mct.convert(src)
    assert out.startswith("/* was #6B7280 */")
    assert "rgb(var(--c-muted))" in out


def test_a_comment_between_rules_does_not_become_part_of_the_selector():
    """Selectors are read from a comment-blanked copy. Read from the raw text
    the prelude arrives as `/* Install / restart lifecycle badges … */
    .badge-queued`, which pairs with nothing — so every `.dark` counterpart in
    that section silently loses its partner and the ambiguous ones stop
    resolving."""
    src = ("/* a note */\n.badge-warning { color: #92400E; }\n"
           "/* another */\n.dark .badge-warning { color: #FCD34D; }\n")
    assert mct.convert(src).count("rgb(var(--c-warning-strong))") == 2


def test_conversion_is_idempotent():
    """The tool must be safe to re-run over an already-converted sheet — and
    the second pass must not see `rgb(var(--c-x))` as an unresolvable colour
    and block the rest of its value."""
    once = mct.convert(sheet())
    assert mct.convert(once) == once


def test_every_colour_in_the_sheet_is_inside_a_parsed_declaration():
    """A converter that cannot see part of the file reports success over the
    part it never read. Measured: 210 colour slots outside the generated
    block, and the walker must account for all of them — including the
    unterminated last declaration of a block and rules nested inside
    `@media` and `@keyframes`."""
    css = sheet()
    source = mct.strip_comments(css)
    generated = mct.GENERATED_BLOCK.search(css).span()

    everywhere = {(s.start, s.end) for s in mct.colour_slots(source)
                  if not (generated[0] <= s.start < generated[1])}
    parsed = {(d.start + s.start, d.start + s.end)
              for d in mct.declarations(css)
              for s in mct.colour_slots(source[d.start:d.end])}
    assert everywhere - parsed == set()
    # A floor, not a snapshot. The exact count moves whenever anyone writes a
    # rule, and pinning it turns ordinary CSS authoring into a test failure
    # that teaches nothing. What must not happen is `colour_slots` quietly
    # finding almost nothing and the set-difference above passing vacuously.
    assert len(everywhere) > 150, (
        f"only {len(everywhere)} colour slots found — the walker has probably "
        "stopped recognising a syntax the sheet uses")


# ── the verifier has to be able to fail ──────────────────────────────────
#
# Each of these mutates the converter and asserts the verifier notices. A
# verifier that shares the decision it is checking cannot do that, which is
# why `_is_dark` and `_justified` are written out in verify_css_tokens.py
# instead of imported from the converter.

def test_the_verifier_catches_a_theme_blind_converter(monkeypatch):
    """THE mutation. Make every rule look light and the converter still
    produces valid CSS referencing real tokens — `.dark .badge-offline {
    color: #9CA3AF }` becomes `faint`, which renders #7E8DA3 where `muted`
    would render #A3B2C7. Nothing about the output looks wrong."""
    css = sheet()
    monkeypatch.setattr(mct, "is_dark_rule", lambda decl: False)
    broken = mct.convert(css)
    assert broken != css, "the mutation must still convert something"
    _intended, _sites, unexpected = vct._audit(css, broken, "mutant")
    assert unexpected, "a theme-blind conversion must be reported"
    assert any("does not hold" in line for line in unexpected)


def test_the_verifier_catches_an_ambiguous_hex_resolved_by_guessing(monkeypatch):
    """The other mutation: take the first candidate instead of declining.
    `#fff` in `.btn-brand` becomes `card`, which is #FFFFFF today, so the
    rendered colour is unchanged and a diff-the-pixels check would pass. Only
    re-deriving the ambiguity catches it."""
    css = sheet()
    honest = mct.convert(css)
    monkeypatch.setattr(mct, "_disambiguate", lambda cands, other: cands[0])
    guessed = mct.convert(css)
    assert guessed != honest, "the mutation must resolve something extra"
    _intended, _sites, unexpected = vct._audit(css, guessed, "mutant")
    assert any("ambiguous" in line for line in unexpected), unexpected


def test_the_verifier_catches_a_dropped_alpha():
    """`rgb(var(--c-brand))` where `rgb(var(--c-brand) / 0.14)` was intended
    is a wash becoming a solid fill. Same token, same hex, wrong colour."""
    before = ".dark { --tint: rgba(167, 139, 250, 0.14); }"
    after = ".dark { --tint: rgb(var(--c-brand)); }"
    _i, _s, unexpected = vct._audit(before, after, "t")
    assert any("alpha" in line for line in unexpected), unexpected


def test_the_verifier_catches_a_deleted_declaration():
    """Rule 6: redundant overrides stay. The declaration list is compared as a
    whole so a deletion cannot hide behind "no colour changed"."""
    before = ".x { color: #6B7280; }\n.dark .x { color: #CBD5E1; }\n"
    after = ".x { color: rgb(var(--c-muted)); }\n"
    _i, _s, unexpected = vct._audit(before, after, "t")
    assert any("COUNT" in line for line in unexpected), unexpected


def test_the_verifier_catches_a_token_the_literal_never_held():
    """#DC2626 is `critical` in LIGHT. It is not a dark value of anything, so
    a `.dark` rule may not claim it — even though `critical` is obviously the
    "right" token by name. Naming is not evidence."""
    before = ".dark .x { color: #DC2626; }"
    after = ".dark .x { color: rgb(var(--c-critical)); }"
    _i, _s, unexpected = vct._audit(before, after, "t")
    assert any("does not hold" in line for line in unexpected), unexpected


def test_the_verifier_reports_a_token_that_does_not_exist():
    """`rgb(var(--c-suface))` is a typo CSS resolves to nothing at all: the
    declaration is dropped and the element loses the property. Left to crash
    on the KeyError, the verifier would report a traceback instead of the one
    line that says which selector is broken."""
    before = ".x { color: #6B7280; }"
    after = ".x { color: rgb(var(--c-suface)); }"
    _i, _s, unexpected = vct._audit(before, after, "t")
    assert any("not a token" in line for line in unexpected), unexpected


def test_the_verifier_resolves_token_references_through_the_python_table():
    """Not "a substitution happened" — what the substitution RENDERS. Derived
    from TOKENS, so the assertion cannot go stale when the palette moves."""
    slot = mct.colour_slots("rgb(var(--c-muted))")[0]
    assert vct.resolve(slot, dark=False) == (dt.TOKENS["muted"][0].upper(), None)
    assert vct.resolve(slot, dark=True) == (dt.TOKENS["muted"][1].upper(), None)


def test_the_verifier_reports_no_unexpected_difference_for_the_real_sheet():
    """The end-to-end. Whatever state app.css is in, the conversion the tool
    would apply to it must be clean."""
    css = sheet()
    _i, _s, unexpected = vct._audit(css, mct.convert(css), "app.css")
    assert not unexpected, "\n  ".join(unexpected)


# ── the strongest property: the sheet round-trips through the palette ────

def _render_to_literals(css: str) -> str:
    """Every `rgb(var(--c-T))` replaced by the hex it paints in ITS theme."""
    source = mct.strip_comments(css)
    edits = []
    for decl in mct.declarations(css):
        dark = vct._is_dark(decl)
        for slot in mct.colour_slots(source[decl.start:decl.end]):
            if slot.token is None:
                continue
            hex_value = dt.TOKENS[slot.token][1 if dark else 0]
            text = (hex_value if slot.alpha is None else
                    "rgba(%d, %d, %d, %s)" % (
                        *(int(hex_value[i:i + 2], 16) for i in (1, 3, 5)),
                        slot.alpha))
            edits.append((decl.start + slot.start, decl.start + slot.end, text))
    for start, end, text in sorted(edits, reverse=True):
        css = css[:start] + text + css[end:]
    return css


def test_no_token_reference_in_app_css_round_trips_to_a_different_token():
    """Render the whole sheet back to literals and convert it again.

    This is the check that does not depend on remembering what the file used
    to say: if `rgb(var(--c-line))` really is the token that owns the colour
    at that site, then painting that colour and re-resolving it must land on
    `line` again. A site allowed to come back as a plain literal — the value
    is ambiguous with no counterpart — is fine. A site that comes back as a
    DIFFERENT token means the sheet is asserting something the mapping does
    not support.
    """
    css = sheet()
    rendered = _render_to_literals(css)
    again = mct.convert(rendered)

    source, back = mct.strip_comments(css), mct.strip_comments(again)
    original = mct.declarations(css)
    restored = mct.declarations(again)
    assert len(original) == len(restored)

    wrong, unresolved = [], 0
    for o_decl, r_decl in zip(original, restored):
        o_slots = mct.colour_slots(source[o_decl.start:o_decl.end])
        r_slots = mct.colour_slots(back[r_decl.start:r_decl.end])
        assert len(o_slots) == len(r_slots)
        for o_slot, r_slot in zip(o_slots, r_slots):
            if o_slot.token is None:
                continue
            if r_slot.token is None:
                unresolved += 1
            elif r_slot.token != o_slot.token:
                wrong.append(f"{o_decl.selector} {{ {o_decl.prop} }}: "
                             f"{o_slot.token} -> {r_slot.token}")
    assert not wrong, "\n  ".join(wrong)
    # Measured 2026-08-07: 5. `page` renders #F1F5F9 in light, which
    # LIGHT_ALIASES also folds onto `raised`, so those sites cannot be
    # re-derived from their own colour alone. Recorded rather than waved
    # through — if it grows, a token pair has collided.
    #
    # Measured 2026-08-07 again, after the lifecycle-badge tokenisation: 6.
    # `brand-strong`'s dark half is deliberately IDENTICAL to `brand`'s
    # (196 181 253 — the same convention every other -strong token already
    # uses: `healthy-strong`/`healthy`, `warning-strong`/`warning` and
    # `critical-strong`/`critical` all share their dark half too, which is
    # why 4 of the original 5 were already this exact shape). Adding a
    # fifth pair that collides the same way was the deliberate choice, not
    # an accident — `.dark .pulse-panel-title--warming { color:
    # rgb(var(--c-brand)) }` pairs with a LIGHT rule using the reserved
    # `--brand-violet` chrome literal (#7C3AED), which matches neither
    # candidate's light half, so the round-trip correctly declines rather
    # than guessing between `brand` and `brand-strong`.
    assert unresolved == 6, unresolved


# ── the ratchet ──────────────────────────────────────────────────────────
#
# Measured 2026-08-07 after conversion: 133 of 210 colour slots reference a
# token, 77 remain literals. Grouped by reason:
#
#   45  no token holds that value in that theme — the indigo/purple/orange
#       lifecycle badges (queued, restart_required, rate_anomaly, correlated)
#       have no token at all, plus pure black in five shadows and a handful
#       of cross-theme strays.
#   18  reserved: the `--brand-*` ramp. Two violet steps and two turquoise
#       ones feeding the brand gradient, which the spec fixes as unchanged.
#       Only `--brand-violet-lite` has a token and its own base does not, so
#       converting what resolves made "lite" darker than the colour it is a
#       lighter step of, and collapsed both steps onto one value in dark
#       mode. Migrating the ramp is a design decision, not a mechanical one.
#    4  the value holds more than one colour and they do not all resolve —
#       the remaining gradients.
#    4  genuinely ambiguous with no counterpart: 2x #F1F5F9 (page/raised),
#       2x #fff (card/field).
#
# An allowlist of thirty-odd colours would assert nothing. A ratchet does:
# the number may fall, never rise.
#
# Lowered 2026-08-07: 71 -> 57, tokenising the update/restart lifecycle
# badges (`accent-tint`, `brand-tint`, `brand-strong` added to TOKENS).
# -14 the 12 lifecycle-badge literals themselves (queued/searching/
#     downloading -> accent-tint+accent-strong; installing/rebooting/
#     restart_required -> brand-tint+brand-strong) plus 2 on `.badge-
#     correlated`, which happened to already spell the exact violet pair
#     restart_required/rebooting used by hand and so became resolvable by
#     the same tokens without its rule being touched.
# The circulating-border pseudo-element (badge-restart_required::before /
# badge-stabilising::before) introduces no literal at all: a first version
# masked a conic-gradient with two `#fff` mask layers (+2, since a mask
# value is not a themed colour and stays ambiguous the same way card/field
# already are), but that shape distorted badly on the badge's actual ~7:1
# pill proportions (measured and screenshotted) and was replaced with a
# small dot on `offset-path: border-box` coloured by `currentColor` —
# no hex anywhere.
# 71 - 14 = 57.

CSS_LITERAL_BASELINE = 57


def _residue() -> list:
    return [c for c in mct.plan(sheet())
            if c.token is None and c.slot.hex is not None]


def test_hardcoded_colour_literals_in_app_css_never_increase():
    """The stylesheet that defines the tokens spent this whole project not
    using them, because typing a hex was easier and nothing ever failed. This
    is the thing that fails."""
    left = _residue()
    assert len(left) <= CSS_LITERAL_BASELINE, (
        f"app.css literals rose to {len(left)} from {CSS_LITERAL_BASELINE}; "
        "use a token from tools/design_tokens.TOKENS, or run "
        "tools/migrate_css_tokens.py:\n  "
        + "\n  ".join(f"{c.slot.hex} {c.decl.selector} {{ {c.decl.prop} }}"
                      for c in left[:20]))


def test_the_app_css_baseline_is_not_left_behind():
    """A ratchet that is never tightened is a ratchet in name only."""
    left = len(_residue())
    assert left == CSS_LITERAL_BASELINE, (
        f"app.css is down to {left} literals; lower CSS_LITERAL_BASELINE "
        f"from {CSS_LITERAL_BASELINE}")


def test_app_css_is_fully_converted():
    """Everything the mapping CAN resolve, it has. Without this the file
    passes the ratchet by never being converted at all."""
    css = sheet()
    assert mct.convert(css) == css, (
        "run: python tools/migrate_css_tokens.py")
