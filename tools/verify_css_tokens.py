"""Prove the app.css token conversion moved no colour it was not meant to move.

Resolves every colour in every declaration back to a concrete RGB value in the
theme that declaration actually renders in, before and after conversion, and
compares. Exact, offline, no browser.

    python tools/verify_css_tokens.py
    python tools/verify_css_tokens.py --by-site    # every difference, listed

Exit 0 when every difference is an intended palette move; 1 otherwise.

WHAT "INTENDED" MEANS HERE, AND WHY IT IS NOT "SOMETHING CHANGED"
-----------------------------------------------------------------
This conversion is NOT colour-preserving, and a verifier that asserted it was
would fail on the first line. `tools/migrate_tokens.py` ran BEFORE the C-Ink
flip, so its output rendered identically; app.css is being converted AFTER it,
so `.sidebar-link { color: #6B7280 }` becoming `rgb(var(--c-muted))` is a real
move from #6B7280 to #475569. That move is the entire point.

So the property proved is narrower and more useful: for every site, the
literal that WAS there must be a recorded value of the token that is there
now, in that site's own theme — current, alias, or pre-flip — and the value it
resolves to must be what `design_tokens.TOKENS` holds for that token today.
Both halves are re-derived from the Python table. A substitution that merely
"happened" proves nothing; #9CA3AF -> `faint` and #9CA3AF -> `muted` are both
substitutions, and exactly one of them is right in a `.dark` rule.

WHAT IS DELIBERATELY NOT SHARED WITH THE CONVERTER
--------------------------------------------------
The PARSER is shared — `declarations()` and `colour_slots()` are imported.
A verifier that parses differently from the thing it verifies reports phantom
differences and misses real ones; the first version of
`verify_token_equivalence.py` was variant-blind while the converter was not
and produced 128 false positives.

The two DECISIONS are not shared, and are re-implemented below:

  * which theme a rule renders in (`_is_dark`), and
  * which token a literal is allowed to become (`_justified`).

Those are the two things the converter can get wrong. Calling
`migrate_css_tokens.is_dark_rule` here would mean a converter that reads every
`.dark` rule as light gets agreed with instead of caught — checked by mutation,
and it does get caught: `.dark .badge-offline { color: #9CA3AF }` resolved
against the light table yields `faint`, which is a real token holding a real
colour and the wrong one.

SPECIFICITY IS ASSUMED, NOT COMPUTED
------------------------------------
`.dark X` is taken to override `X`. True for every pair in this sheet (a class
outranks a pseudo-element, and `.dark *` outranks `*`), but it is an
assumption, and a `#id` light rule paired with a `.dark .class` dark rule would
break it. There are none today.
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt                 # noqa: E402
from tools import migrate_css_tokens as mct           # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS_REL = "static/css/app.css"
CSS_PATH = REPO_ROOT / CSS_REL


# ── the two decisions, re-derived ────────────────────────────────────────

def _is_dark(decl) -> bool:
    """Independent theme derivation. Deliberately not the converter's.

    Written out rather than imported so that the crux of the conversion has a
    second opinion. `.dark` may sit on the rule's own selector or on any
    enclosing at-rule.
    """
    parts = (*decl.at_chain, decl.selector)
    return any(_scopes_dark(part) for part in parts)


def _scopes_dark(selector: str) -> bool:
    for i in range(len(selector) - 4):
        if selector[i:i + 5] == ".dark":
            tail = selector[i + 5:i + 6]
            if tail == "" or not (tail.isalnum() or tail in "-_"):
                return True
    return False


_SIDES = ("", "-top", "-right", "-bottom", "-left")
# `border`, `border-bottom` and `border-bottom-color` all set a border colour;
# `background` and `background-color` both set a background. A light rule
# using the shorthand and its `.dark` override using the longhand are one
# pair, and this sheet writes five of them that way.
_SAME_COLOUR = {f"{fam}{side}{suffix}": f"{fam}{side}"
                for fam, sides in (("background", ("",)), ("border", _SIDES))
                for side in sides for suffix in ("", "-color")}


def _light_key(decl, index: int) -> tuple:
    """(rule, property, slot) with the `.dark` scope stripped — the pair key."""
    def base(selector: str) -> str:
        out = []
        for part in selector.split(","):
            words = [w for w in part.split() if not _scopes_dark(w)]
            out.append(" ".join(words) or ":root")
        return ", ".join(out)
    return (tuple(base(a) for a in decl.at_chain), base(decl.selector),
            _SAME_COLOUR.get(decl.prop, decl.prop), index)


def _index(css: str) -> dict:
    """(pair key, is_dark) -> the set of candidate token sets declared there.

    The verifier's own copy of the converter's pairing table, used only to
    decide whether an AMBIGUOUS literal had a legitimate partner to be
    resolved from. Without it, `#FFFFFF -> card` and `#FFFFFF -> field` both
    look like valid substitutions and the guess goes unnoticed until the
    palette pulls card and field apart.
    """
    source = mct.strip_comments(css)
    table: dict = {}
    for decl in mct.declarations(css):
        dark = _is_dark(decl)
        for i, slot in enumerate(mct.colour_slots(source[decl.start:decl.end])):
            names = (frozenset((slot.token,)) if slot.token
                     else frozenset(dt.tokens_for(slot.hex or "", dark)))
            table.setdefault((_light_key(decl, i), dark), set()).add(names)
    return table


def _justified(hex_value: str, token: str, dark: bool,
               partners: set[frozenset[str]]) -> str:
    """'' when `token` may hold `hex_value` here, else why not."""
    holders = dt.tokens_for(hex_value, dark)
    theme = "dark" if dark else "light"
    if token not in holders:
        return (f"{token} does not hold {hex_value} in {theme} "
                f"(holders: {'/'.join(holders) or 'none'})")
    if len(holders) == 1:
        return ""
    if len(partners) != 1:
        return (f"{hex_value} is ambiguous in {theme} ({'/'.join(holders)}) "
                f"and has no single counterpart to resolve it")
    shared = [t for t in holders if t in next(iter(partners))]
    if shared != [token]:
        return (f"{hex_value} is ambiguous in {theme} ({'/'.join(holders)}); "
                f"the counterpart narrows it to {'/'.join(shared) or 'nothing'}, "
                f"not {token}")
    return ""


# ── resolution ───────────────────────────────────────────────────────────

def resolve(slot, dark: bool) -> tuple[str, str | None]:
    """A slot as the concrete (#RRGGBB, alpha) it paints in this theme.

    A token reference is resolved THROUGH `design_tokens.TOKENS`, not asserted
    to exist. That is what makes this a check on the rendered colour rather
    than on the shape of the text.
    """
    if slot.token is not None:
        return dt.TOKENS[slot.token][1 if dark else 0].upper(), slot.alpha
    return (slot.hex or "").upper(), slot.alpha


def _audit(before: str, after: str, label: str) -> tuple[Counter, list[str], list[str]]:
    intended: Counter = Counter()
    sites: list[str] = []
    unexpected: list[str] = []

    # Compared, not required: `_audit` also runs over hand-written fragments
    # in the tests. `main()` is where app.css is required to HAVE the block.
    gen_b, gen_a = (mct.GENERATED_BLOCK.search(before),
                    mct.GENERATED_BLOCK.search(after))
    if (gen_b.group(0) if gen_b else None) != (gen_a.group(0) if gen_a else None):
        unexpected.append(f"{label}: the generated :root/.dark block changed")

    b_src, a_src = mct.strip_comments(before), mct.strip_comments(after)
    b_decls, a_decls = mct.declarations(before), mct.declarations(after)
    if len(b_decls) != len(a_decls):
        # Rule 6: a redundant `.dark` override is harmless and stays. Deleting
        # rules is where this goes wrong, so the declaration list is compared
        # as a whole rather than only where a colour changed.
        unexpected.append(
            f"{label}: declaration COUNT {len(b_decls)} -> {len(a_decls)}")
        return intended, sites, unexpected

    partners_before = _index(before)
    partners_after = _index(after)

    for b_decl, a_decl in zip(b_decls, a_decls):
        where = f"{b_decl.selector} {{ {b_decl.prop} }}"
        if (b_decl.at_chain, b_decl.selector, b_decl.prop) != \
           (a_decl.at_chain, a_decl.selector, a_decl.prop):
            unexpected.append(f"{label}: rule moved — {where} -> "
                              f"{a_decl.selector} {{ {a_decl.prop} }}")
            continue

        dark = _is_dark(b_decl)
        theme = "dark" if dark else "light"
        b_slots = mct.colour_slots(b_src[b_decl.start:b_decl.end])
        a_slots = mct.colour_slots(a_src[a_decl.start:a_decl.end])
        if len(b_slots) != len(a_slots):
            unexpected.append(f"{label}: {where} colour COUNT "
                              f"{len(b_slots)} -> {len(a_slots)}")
            continue

        for i, (b_slot, a_slot) in enumerate(zip(b_slots, a_slots)):
            # Before resolving, not after: `rgb(var(--c-suface))` is a typo
            # that CSS resolves to nothing at all — the element loses the
            # property entirely — and it would take this verifier down with a
            # KeyError instead of being reported.
            unknown = [s.token for s in (b_slot, a_slot)
                       if s.token is not None and s.token not in dt.TOKENS]
            if unknown:
                unexpected.append(f"{label}: {where} references "
                                  f"--c-{unknown[0]}, which is not a token")
                continue

            b_hex, b_alpha = resolve(b_slot, dark)
            a_hex, a_alpha = resolve(a_slot, dark)

            if b_alpha != a_alpha:
                unexpected.append(
                    f"{label}: {where} {theme} alpha {b_alpha} -> {a_alpha}")
                continue

            if a_slot.token is None:
                if a_hex != b_hex:
                    unexpected.append(
                        f"{label}: {where} {theme} literal {b_hex} -> {a_hex} "
                        "(rewritten without a token)")
                continue

            if b_slot.token is not None:
                if b_slot.token != a_slot.token:
                    unexpected.append(f"{label}: {where} {theme} token "
                                      f"{b_slot.token} -> {a_slot.token}")
                continue

            why = _justified(b_hex, a_slot.token, dark,
                             partners_before.get((_light_key(b_decl, i), not dark),
                                                 set()))
            if why:
                unexpected.append(f"{label}: {where} {theme} -> "
                                  f"{a_slot.token}: {why}")
                continue

            if a_hex == b_hex:
                intended[f"{theme}  {b_hex} already equals {a_slot.token}"] += 1
            else:
                intended[f"{theme}  {b_hex} -> {a_slot.token} {a_hex}"] += 1
                sites.append(f"{theme}  {where}: {b_hex} -> {a_hex}")

            # A LIGHT rule with no `.dark` counterpart used to paint its
            # literal in BOTH themes. Tokenising it gives dark mode a value it
            # never had — the same honest exception `verify_token_equivalence`
            # reports for the 870 unpaired template utilities. Real change,
            # listed rather than absorbed.
            if not dark and not partners_after.get((_light_key(a_decl, i), True)):
                dark_now = dt.TOKENS[a_slot.token][1].upper()
                intended[f"dark mode now follows {a_slot.token} "
                         f"({b_hex} -> {dark_now})"] += 1
                sites.append(f"dark   {where}: {b_hex} -> {dark_now} "
                             "(no .dark rule existed)")

    return intended, sites, unexpected


def _head_copy() -> str | None:
    """app.css as committed, so the working tree can be checked against it.

    Without this the verifier can only grade the converter's PROPOSAL. With
    it, it grades the diff that actually exists — including whether that diff
    is the one the converter produces, which is what catches a hand-edit made
    after the tool ran.
    """
    try:
        done = subprocess.run(["git", "show", f"HEAD:{CSS_REL}"],
                              cwd=REPO_ROOT, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.decode("utf-8")


def main() -> int:
    by_site = "--by-site" in sys.argv[1:]
    disk = CSS_PATH.read_text(encoding="utf-8")

    intended: Counter = Counter()
    sites: list[str] = []
    unexpected: list[str] = []
    passes: list[str] = []

    if not mct.GENERATED_BLOCK.search(disk):
        unexpected.append(
            f"{CSS_REL}: the generated :root/.dark block is gone — "
            "regenerate it from design_tokens.render_css()")

    head = _head_copy()
    if head is not None and head != disk:
        # The change sitting in the working tree, graded against HEAD — but
        # ONLY when the working tree is the conversion itself. Once someone
        # writes a new rule, adds a selector or deletes a dead override, the
        # two files no longer line up declaration-for-declaration and every
        # comparison after the first insertion is off by one. That reads as
        # hundreds of colour changes and none of them are real.
        #
        # The property worth asserting across an arbitrary edit is not "the
        # file equals convert(HEAD)" — that forbids authoring CSS at all —
        # but idempotence, which the proposal audit below already checks: a
        # hand-added literal that a token could express makes convert(disk)
        # differ from disk, and that is exactly the mistake worth catching.
        if mct.convert(head) == disk:
            i, s, u = _audit(head, disk, "worktree")
            intended += i
            sites += s
            unexpected += u
            passes.append("worktree vs HEAD")
        else:
            passes.append("worktree hand-edited since the tool ran — graded "
                          "on idempotence only")

    # The converter's proposal for whatever is on disk now. After the
    # conversion has landed this is a no-op, and saying so is the idempotency
    # assertion: a second run must find nothing left to do.
    i, s, u = _audit(disk, mct.convert(disk), "proposal")
    intended += i
    sites += s
    unexpected += u
    passes.append("converter proposal for the working copy")

    print("checked: " + "; ".join(passes) + "\n")

    if by_site:
        for line in sorted(sites):
            print(f"  {line}")
        print()
    print("INTENDED palette moves:")
    for key, n in intended.most_common():
        print(f"  {n:>5}  {key}")
    if not intended:
        print("  (none)")

    print(f"\nUNEXPECTED differences: {len(unexpected)}")
    for line in unexpected[:25]:
        print(f"  {line}")
    if len(unexpected) > 25:
        print(f"  ... and {len(unexpected) - 25} more")

    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
