"""Apply the colour token mapping to static/css/app.css. Idempotent.

    python tools/migrate_css_tokens.py --check
    python tools/migrate_css_tokens.py --check --residue     # why each one stayed
    python tools/migrate_css_tokens.py

Always run `tools/verify_css_tokens.py` afterwards. This script reports how
many literals it rewrote; only the verifier can tell you whether the result
renders the colour the token actually holds. The two questions are different —
`tools/migrate_tokens.py` had four bugs that "N literals rewritten" reported as
success.

WHY THIS EXISTS
---------------
`static/css/app.css` is where the tokens are DEFINED, and it was the one file
that did not use them. Measured 2026-08-07, outside the generated block:

    210  colour occurrences (186 hex literals + 24 rgb()/rgba() calls)
     68  distinct colours
      1  occurrence of `var(--c-*)`  — and it is inside a comment

The templates are 96% tokenised, so after the C-Ink flip those 210 sites are
the previous palette rendering next to the new one: sidebar links, badges,
the HTMX error banner, the pulse panel, both scrollbars.

THE THEME IS THE SELECTOR, NOT THE FILE
---------------------------------------
A declaration inside a rule whose selector contains `.dark` renders in DARK
mode; everything else renders in LIGHT. That distinction is the whole job,
because the same literal belongs to different tokens on each side:

    #9CA3AF   light -> faint    (LEGACY_LIGHT)
              dark  -> muted    (ALIASES)
    #CBD5E1   light -> line     (TOKENS)
              dark  -> muted    (LEGACY_DARK)
    #F1F5F9   light -> page OR raised   (ambiguous, left alone)
              dark  -> ink      (TOKENS)

Both of those pairs occur in this file, in both directions. Resolving a
`.dark` rule against the light table picks a real token that renders the wrong
colour — the failure is silent, and only visible in one theme.

`.dark` is looked for in the rule's own selector AND in every enclosing
at-rule, so `@media (max-width: 767px) { .dark .x { … } }` is still dark.

TWO DELIBERATE RESTRICTIONS
---------------------------
1. The generated `:root`/`.dark` block is never touched. It is emitted by
   `design_tokens.render_css()` and asserted by tests/test_design_tokens.py;
   rewriting `--c-page: 241 245 249` in terms of itself is a cycle.

2. A value is converted ALL-OR-NOTHING. A gradient is a ramp, not a bag of
   colours: converting some stops and leaving others rewrites its SHAPE, not
   just its palette. `--brand-grad` is
   `linear-gradient(95deg, #7C3AED 0%, #8B5CF6 55%, #2DD4BF 100%)`; #7C3AED
   and #2DD4BF have no light token, #8B5CF6 folds onto `brand` (#5B21B6), and
   converting only the middle stop makes the ramp go violet -> DARKER violet
   -> turquoise, i.e. it reverses direction halfway. Six gradients are
   affected (`--brand-grad`, `.prism-wordmark`, `#topbar::after`, each in both
   themes) and each loses one otherwise-valid conversion. A seventh is lost to
   the same rule on `* { scrollbar-color: #CBD5E1 #F1F5F9 }`, where the thumb
   resolves and the track is ambiguous — converting one of two values that
   sit in the same declaration is how a thumb ends up matching nothing.

AMBIGUITY IS RESOLVED FROM THE PAIRED RULE, OR NOT AT ALL
---------------------------------------------------------
17 occurrences are hexes that more than one token holds in that theme —
#6EE7B7 is both `healthy` and `healthy-strong` in dark, #FFFFFF is both `card`
and `field` in light. Where the rule has a counterpart in the other theme
(`.badge-warning` / `.dark .badge-warning`, same property), the counterpart's
own candidate set intersects the ambiguous one and usually leaves exactly one
token. That resolves 11 of the 17 — every badge label, which pairs a light
`-strong` value with a dark one that also happens to equal the plain status
colour. The remaining 6 are left as literals: guessing would be invisible
until the palette pulls the candidates apart, which is precisely the bug this
whole toolchain exists to prevent.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = REPO_ROOT / "static" / "css" / "app.css"

# The generated block, delimited by sentinels that design_tokens.py writes.
#
# This used to be a regex matching the SHAPE of the colour block —
# `--c-name: <digits and spaces>`. That guard silently stopped covering
# anything the moment the generator emitted a value of a different shape, and
# it now emits durations, easing curves and box-shadows. A shadow is full of
# `rgb(...)`, so the converter would have walked straight into generated
# output and rewritten it, and the next regeneration would have wiped that.
#
# Sentinels say what is protected instead of inferring it.
GENERATED_BLOCK = re.compile(
    re.escape(dt.GENERATED_OPEN) + r".*?" + re.escape(dt.GENERATED_CLOSE),
    re.S)


# ── parsing ──────────────────────────────────────────────────────────────

def _mask(css: str, strings: bool = True) -> str:
    """`css` with comments — and optionally string bodies — blanked.

    Same length and same newlines as the input, so every offset taken from a
    masked copy indexes the original unchanged. That equivalence is the only
    reason two different maskings can be mixed in one walk.

    One pass, not two regexes. A quote can appear inside a comment and `/*`
    can appear inside a string, so whichever is scanned first has to consume
    the other — the same trap `design_tokens._string_spans` documents for
    JavaScript, where it hid 218 literals before it was fixed.

    `strings=True` is for the STRUCTURAL walk: a `;` or `{` inside
    `content: "{"` would otherwise desynchronise the brace stack.
    `strings=False` keeps `input[type="number"]` intact, so two selectors that
    differ only inside a string stay distinguishable as pairing keys.
    """
    out = list(css)
    i, n = 0, len(css)

    def blank(start: int, stop: int) -> None:
        for k in range(start, stop):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif css[i] in "'\"":
            quote, j = css[i], i + 1
            while j < n and css[j] != quote:
                j += 2 if css[j] == "\\" else 1
            if strings:
                blank(i + 1, min(j, n))
            i = min(j, n) + 1
        else:
            i += 1
    return "".join(out)


def strip_comments(css: str) -> str:
    """`css` with comments blanked and everything else, strings included, intact.

    Read selectors, property names and values from THIS, never from the raw
    text. A comment sits between rules far more often than not, so a prelude
    sliced from the raw text arrives as
    `/* Install / restart lifecycle badges … */ .badge-queued`, which is not a
    selector, does not pair with `.dark .badge-queued`, and prints a wall of
    prose in the residue listing. Spans still index the original, so the
    rewritten file keeps every comment exactly where it was.
    """
    return _mask(css, strings=False)


@dataclass(frozen=True)
class Declaration:
    """One `prop: value` and the selector chain that decides its theme.

    `at_chain` holds the enclosing at-rules outermost-first; `selector` is the
    innermost prelude. Both are searched for `.dark`, because a rule nested in
    `@media` is still a dark rule.
    """
    at_chain: tuple[str, ...]
    selector: str
    prop: str
    start: int          # value span, offsets into the ORIGINAL css text
    end: int


_WS = re.compile(r"\s+")
_PROP = re.compile(r"\s*([-\w]+)\s*:\s*")


def declarations(css: str) -> list[Declaration]:
    """Every declaration in the sheet, with its selector chain.

    Exported so the verifier walks exactly the regions the converter rewrites.
    A verifier that inspects a narrower set than the converter touches reports
    success over changes it never examined — the failure this toolchain keeps
    rediscovering.
    """
    masked = _mask(css)
    source = strip_comments(css)
    stack: list[str] = []
    out: list[Declaration] = []
    seg = 0

    def flush(start: int, end: int) -> None:
        m = _PROP.match(source, start, end)
        if not m or not stack:
            return
        value_start, value_end = m.end(), end
        while value_end > value_start and source[value_end - 1] in " \t\r\n":
            value_end -= 1
        out.append(Declaration(tuple(stack[:-1]), stack[-1], m.group(1),
                               value_start, value_end))

    for i, ch in enumerate(masked):
        if ch == "{":
            stack.append(_WS.sub(" ", source[seg:i]).strip())
            seg = i + 1
        elif ch == "}":
            # The tail of a block may be an unterminated declaration
            # (`{ color: red }`) or just whitespace after a nested block.
            flush(seg, i)
            if stack:
                stack.pop()
            seg = i + 1
        elif ch == ";":
            flush(seg, i)
            seg = i + 1
    return out


@dataclass(frozen=True)
class Slot:
    """One colour inside a declaration value.

    Either a literal (`hex` set) or an existing token reference (`token` set).
    Recognising both is what makes the converter idempotent and lets the
    verifier walk a file that is already converted.

    `alpha` is the opacity TEXT exactly as written — `0.08`, `.55`, `40%`. It
    is copied through verbatim rather than reformatted, because a converter
    that renders 0.40 as 0.4 produces a diff that is impossible to read.
    """
    start: int
    end: int
    hex: str | None
    token: str | None
    alpha: str | None


_SLOT = re.compile(
    # An already-converted reference. Tried first; it cannot collide with the
    # rgb() branch below, which requires digits.
    r"(?P<ref>rgb\(\s*var\(--c-(?P<token>[a-z-]+)\)\s*(?:/\s*(?P<ralpha>[^)\s]+)\s*)?\))"
    # 8 and 4 digit forms carry alpha in the last byte / nibble.
    r"|(?P<hex>#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{3})"
    r"(?![0-9A-Fa-f]))"
    r"|(?P<fn>rgba?\(\s*(?P<r>\d{1,3})\s*(?:,\s*|\s+)(?P<g>\d{1,3})\s*(?:,\s*|\s+)"
    r"(?P<b>\d{1,3})\s*(?:[,/]\s*(?P<falpha>[0-9.]+%?)\s*)?\))")


def _expand(digits: str) -> tuple[str, str | None]:
    """`#RGB` / `#RGBA` / `#RRGGBB` / `#RRGGBBAA` -> ('#RRGGBB', alpha-text)."""
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits)
    body, tail = digits[:6].upper(), digits[6:]
    if not tail:
        return "#" + body, None
    # 0.4863 rather than 124/255: the target syntax takes a number, and a
    # rounded decimal is the closest exactly-representable spelling.
    alpha = f"{int(tail, 16) / 255:.4f}".rstrip("0").rstrip(".")
    return "#" + body, alpha or "0"


def colour_slots(value: str) -> list[Slot]:
    """Every colour in a declaration value, literals and token references."""
    out: list[Slot] = []
    for m in _SLOT.finditer(value):
        if m.group("ref"):
            out.append(Slot(m.start(), m.end(), None, m.group("token"),
                            m.group("ralpha")))
        elif m.group("hex"):
            hex_value, alpha = _expand(m.group("hex")[1:])
            out.append(Slot(m.start(), m.end(), hex_value, None, alpha))
        else:
            channels = (int(m.group("r")), int(m.group("g")), int(m.group("b")))
            out.append(Slot(m.start(), m.end(), "#%02X%02X%02X" % channels,
                            None, m.group("falpha")))
    return out


# ── theme ────────────────────────────────────────────────────────────────

# `\.dark\b` would also match `.dark-mode`, because a word boundary sits
# between `k` and `-`. There is no such class today; there is no reason to
# leave the trap armed either.
_DARK = re.compile(r"\.dark(?![-\w])")

# The `--brand-*` custom properties are a SCALE, not stray literals: a
# two-step violet ramp and a two-step turquoise one, feeding the brand
# gradient. DESIGN_TOKENS_SPEC.md §2 says that gradient is unchanged.
#
# Converting them piecemeal breaks the ramp rather than tokenising it. Only
# `--brand-violet-lite` (#8B5CF6) has a token; its base `--brand-violet`
# (#7C3AED) does not. Folding just the one made "lite" resolve DARKER than
# the colour it is a lighter step of in light mode, and collapsed both steps
# onto a single value in dark mode — where three components use the pair
# precisely because they are two different violets.
#
# Migrating the whole ramp onto tokens is a design decision, not a mechanical
# one, so the tool declines instead of doing half of it.
RESERVED_PROPERTY = re.compile(r"--brand-")


def is_dark_rule(decl: Declaration) -> bool:
    """Does this declaration render in dark mode?

    THE crux of the conversion. Get it wrong and every literal that means one
    token in light and another in dark — #9CA3AF, #CBD5E1, #F1F5F9, #475569,
    #64748B — resolves to a real, valid, wrong token.

    `tools/verify_css_tokens.py` deliberately derives the theme itself instead
    of calling this, so that a fault here shows up as a reported difference
    rather than being agreed with.
    """
    return any(_DARK.search(part) for part in (*decl.at_chain, decl.selector))


def _base_selector(selector: str) -> str:
    """The selector with its `.dark` scope removed — the pairing key.

    `.dark .badge-warning` and `.badge-warning` are the two halves of one
    element's colour, and reading the light half is how the dark one gets
    disambiguated. Bare `.dark` maps to `:root`, because that pair is the
    custom-property block, not an element.
    """
    parts = []
    for part in selector.split(","):
        stripped = _WS.sub(" ", _DARK.sub("", part)).strip()
        parts.append(stripped or ":root")
    return ", ".join(parts)


_SHORTHAND = re.compile(r"^(background|border(?:-(?:top|right|bottom|left))?)"
                        r"(?:-color)?$")


def _colour_property(prop: str) -> str:
    """The property two rules must share to be each other's counterpart.

    `.pulse-panel { border: 1px solid #E5E7EB }` is overridden in dark mode by
    `.dark .pulse-panel { border-color: #334155 }` — same colour, different
    spelling. Keyed on the raw property name those two never meet, and five
    rules in this sheet are written exactly that way, so the pair goes unseen:
    the light half gets reported as gaining a dark-mode value it has had all
    along. Nothing in the sheet needs it to DISAMBIGUATE today, but a pairing
    table that is wrong about which rules pair is wrong wherever it is used.
    """
    m = _SHORTHAND.match(prop)
    return m.group(1) if m else prop


def _key(decl: Declaration, index: int) -> tuple:
    return (tuple(_base_selector(a) for a in decl.at_chain),
            _base_selector(decl.selector), _colour_property(decl.prop), index)


# ── planning ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Change:
    decl: Declaration
    slot: Slot
    dark: bool
    token: str | None
    reason: str         # why it was NOT converted, when token is None


def _candidates(slot: Slot, dark: bool) -> frozenset[str]:
    if slot.token is not None:
        return frozenset((slot.token,))
    return frozenset(dt.tokens_for(slot.hex or "", dark))


def plan(css: str) -> list[Change]:
    """Decide, for every colour slot outside the generated block, what it is.

    Returns one `Change` per slot — converted or not — so the caller can
    report the residue by reason instead of just counting what is left.
    """
    generated = GENERATED_BLOCK.search(css)
    gen_span = generated.span() if generated else (-1, -1)
    source = strip_comments(css)

    decls = [d for d in declarations(css)
             if not (gen_span[0] <= d.start < gen_span[1])]

    # Candidate sets of the OTHER theme, keyed by (at-chain, base selector,
    # property, slot index). A key with two different answers — the sheet
    # declares `::-webkit-scrollbar-thumb:hover` twice, once at the top and
    # once in the brand section — disambiguates nothing, so it is dropped
    # rather than resolved to whichever came last.
    partners: dict[tuple, set[frozenset[str]]] = {}
    for decl in decls:
        dark = is_dark_rule(decl)
        for index, slot in enumerate(colour_slots(source[decl.start:decl.end])):
            partners.setdefault((_key(decl, index), dark), set()).add(
                _candidates(slot, dark))

    changes: list[Change] = []
    for decl in decls:
        dark = is_dark_rule(decl)
        slots = colour_slots(source[decl.start:decl.end])
        row: list[Change] = []
        if RESERVED_PROPERTY.match(decl.prop):
            changes.extend(Change(decl, s, dark, None, "reserved: brand ramp")
                           for s in slots)
            continue
        for index, slot in enumerate(slots):
            if slot.token is not None:
                row.append(Change(decl, slot, dark, None, "already a token"))
                continue
            cands = dt.tokens_for(slot.hex or "", dark)
            if len(cands) == 1:
                row.append(Change(decl, slot, dark, cands[0], ""))
            elif not cands:
                row.append(Change(decl, slot, dark, None, "no token holds it"))
            else:
                other = partners.get((_key(decl, index), not dark), set())
                token = _disambiguate(cands, other)
                row.append(Change(decl, slot, dark, token,
                                  "" if token else
                                  f"ambiguous: {'/'.join(cands)}"))

        # All-or-nothing per value: see the module docstring. A value whose
        # colours only partly resolve keeps every one of them.
        literals = [c for c in row if c.slot.hex is not None]
        if literals and any(c.token is None for c in literals):
            blocked = next(c.reason for c in literals if c.token is None)
            row = [c if c.token is None else
                   Change(c.decl, c.slot, c.dark, None,
                          f"value not fully resolvable ({blocked})")
                   for c in row]
        changes.extend(row)
    return changes


def _disambiguate(cands: list[str], other: set[frozenset[str]]) -> str | None:
    """Intersect an ambiguous candidate set with the paired rule's.

    One entry only. If the other theme declares two different colours for the
    same selector and property there is no single counterpart, and a
    "disambiguation" from an arbitrary one of them is a guess wearing a
    justification.
    """
    if len(other) != 1:
        return None
    shared = [t for t in cands if t in next(iter(other))]
    return shared[0] if len(shared) == 1 else None


def render(token: str, alpha: str | None) -> str:
    """The target form. `rgb(var(--c-x) / a)`, alpha preserved verbatim."""
    return (f"rgb(var(--c-{token}))" if alpha is None
            else f"rgb(var(--c-{token}) / {alpha})")


def convert(css: str) -> str:
    """Rewrite every determined colour literal as a token reference."""
    edits = [(c.decl.start + c.slot.start, c.decl.start + c.slot.end,
              render(c.token, c.slot.alpha))
             for c in plan(css) if c.token is not None]
    # Right-to-left. `#94A3B8` occurs as a substring of no other slot here,
    # but slot spans overlap the moment a value holds two colours, and an
    # edit applied left-to-right invalidates every offset after it.
    for start, end, text in sorted(edits, reverse=True):
        css = css[:start] + text + css[end:]
    return css


# ── cli ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=[str(CSS_PATH)],
                        help="stylesheets to convert (default: static/css/app.css)")
    parser.add_argument("--check", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--residue", action="store_true",
                        help="list every literal left behind, grouped by reason")
    args = parser.parse_args()

    changed = converted = left = 0

    for raw in args.paths or [str(CSS_PATH)]:
        path = Path(raw)
        if not path.is_file():
            print(f"  skipped (not a file): {path}", file=sys.stderr)
            continue
        before = path.read_text(encoding="utf-8")
        rows = plan(before)
        after = convert(before)

        done = sum(1 for c in rows if c.token is not None)
        residue = [c for c in rows
                   if c.token is None and c.slot.hex is not None]
        converted += done
        left += len(residue)

        print(f"  {path.as_posix()}: {done} tokenised, "
              f"{len(residue)} literal(s) remain")
        if args.residue:
            reasons = Counter(c.reason for c in residue)
            for reason, n in reasons.most_common():
                print(f"      {n:>4}  {reason}")
                for c in residue:
                    if c.reason != reason:
                        continue
                    theme = "dark " if c.dark else "light"
                    print(f"             {c.slot.hex} {theme} "
                          f"{c.decl.selector} {{ {c.decl.prop} }}")
        if before == after:
            continue
        changed += 1
        if not args.check:
            path.write_text(after, encoding="utf-8")

    verb = "would change" if args.check else "changed"
    print(f"\n{changed} file(s) {verb}; {converted} converted, {left} left")
    if not args.check and changed:
        print("Now run: python tools/verify_css_tokens.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
