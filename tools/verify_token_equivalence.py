"""Prove the token conversion changed no colour it was not meant to change.

Resolves every colour utility in every class attribute back to the effective
(light, dark) hex pair it produces, before and after conversion, and compares.
Exact, offline, and it does not need a browser.

    python tools/verify_token_equivalence.py

Exit 0 when every difference is an intended alias collapse; 1 otherwise.

IF YOU SPOT-CHECK IN A BROWSER, DISABLE TRANSITIONS FIRST
---------------------------------------------------------
In a tab that is not compositing frames — the normal state for an automated
browser pane — a CSS transition never advances, and `getComputedStyle` returns
its START value indefinitely. `<body class="transition-colors duration-200">`
reported the light background half a second after switching to dark, while the
rule and the custom property were both provably correct. Inject

    *,*::before,*::after { transition: none !important; animation: none !important }

before reading, or every transitioned property will lie to you.

WHY NOT JUST TRUST THE TESTS
----------------------------
An earlier converter deleted `dark:bg-[#1E293B]` wherever the light half was
Tailwind's built-in `bg-white` rather than an arbitrary value, which would have
turned 275 cards white in dark mode. Every unit test passed, because they all
used arbitrary values on both sides. This walks the real markup instead.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

_CLASS_ATTR = re.compile(r'class="([^"]*)"')

# Reuse the converter's OWN pattern and variant splitter rather than writing a
# second copy. A verifier that parses differently from the thing it verifies
# reports phantom differences and misses real ones — the first version of this
# file was variant-blind while the converter was not, and produced 128 false
# positives that looked exactly like bugs.
_ARB = dt._UTIL
_TOK = re.compile(
    r"(?P<variants>(?:" + dt._VARIANT + r")*)"
    r"(?P<util>text|bg|border|ring|from|to|via|divide|placeholder|fill|stroke|accent|outline)"
    r"(?P<side>-(?:t|r|b|l|x|y|top|right|bottom|left))?"
    r"-(?P<token>" + dt.token_alternation() + r")\b"
    r"(?P<alpha>/\d{1,3})?")
_BUILTIN = re.compile(
    r"(?P<variants>(?:" + dt._VARIANT + r")*)"
    r"(?P<util>text|bg|border|ring)-(?P<name>white|black)\b"
    r"(?P<alpha>/\d{1,3})?")

_BUILTIN_HEX = {"white": "#FFFFFF", "black": "#000000"}

# Aliased values: these deliberately collapse onto a canonical one. Each is a
# small, intended change, recorded rather than hidden. Light and dark are
# separate tables because the same hex can be a light neighbour of one token
# and the dark half of another — #F1F5F9 is ink's dark value and folds onto
# raised in light mode.
_INTENDED = {a.upper(): dt.TOKENS[t][1].upper()
             for a, t in {**dt.ALIASES, **dt.DARK_ALIASES}.items()}
_INTENDED_LIGHT = {a.upper(): dt.TOKENS[t][0].upper()
                   for a, t in dt.LIGHT_ALIASES.items()}


def effective(body: str) -> set[tuple[str, str, str]]:
    """Every (utility-key, 'light'|'dark', hex@alpha) this class list can produce.

    The alpha modifier is part of the rendered colour, not decoration. Without
    it here, `bg-[#2563EB]/10 dark:bg-[#3B82F6]/20` collapsing to `bg-info/10`
    compared EQUAL — same hexes, same slots — while dark mode quietly went from
    20% to 10% opacity. 43 sites had mismatched halves.

    A SET, not a last-wins mapping. Class attributes routinely carry several
    mutually-exclusive values for the same utility:

        {% if expired %}text-[#DC2626]{% elif soon %}text-[#F59E0B]
        {% else %}text-[#6B7280]{% endif %}

    Only one branch ever renders, so "the effective colour" is not a single
    value — modelling it as one reported 11 correct conversions as breakages.
    Comparing the full set of reachable values is exact for both shapes.
    """
    out: set[tuple[str, str, str]] = set()

    def key(match: re.Match) -> tuple[bool, str]:
        is_dark, chain = dt._split_variants(match.group("variants"))
        side = match.groupdict().get("side") or ""
        return is_dark, chain + match.group("util") + side

    # `bg-page dark:bg-page/50` is what the converter emits when the two halves
    # had different opacities. Both classes set the same property, and in dark
    # mode the `dark:` one wins on specificity (`.dark .x` beats `.x`). Model
    # that, or the shadowed value reads as a spurious extra.
    #
    # Scoped to the same TOKEN deliberately. A class attribute can hold several
    # mutually-exclusive Jinja branches, and a broader rule would let one
    # branch's `dark:` utility suppress another branch's base value.
    shadowed: set[tuple[str, str]] = set()
    for m in _TOK.finditer(body):
        is_dark, k = key(m)
        if is_dark:
            shadowed.add((k, m.group("token")))

    for m in _TOK.finditer(body):
        light, dark = dt.TOKENS[m.group("token")]
        is_dark, k = key(m)
        a = m.group("alpha") or ""
        # A `dark:`-prefixed token utility states ONLY the dark slot; an
        # unprefixed one states both, because the token carries both values.
        if not is_dark:
            out.add((k, "light", light.upper() + a))
            if (k, m.group("token")) in shadowed:
                continue
        out.add((k, "dark", dark.upper() + a))
    for m in _BUILTIN.finditer(body):
        is_dark, k = key(m)
        out.add((k, "dark" if is_dark else "light",
                 _BUILTIN_HEX[m.group("name")] + (m.group("alpha") or "")))
    for m in _ARB.finditer(body):
        is_dark, k = key(m)
        out.add((k, "dark" if is_dark else "light",
                 m.group("hex").upper() + (m.group("alpha") or "")))

    return out


def _leaked_light(before: set[tuple[str, str, str]], k: str, gained: str) -> str:
    """Which light value was leaking into dark mode, for this gained dark one.

    Not simply "a light value on this key". One class attribute routinely
    carries several mutually-exclusive Jinja branches — `bg-[#10B981]` in one,
    `bg-[#DC2626]` in another — so picking the first reported
    `bg: dark was #10B981 -> #EF4444`, a green light half gaining a red dark,
    which is nonsense and would have been read as a conversion bug. Match on
    the token that actually owns the gained value.
    """
    base, _, alpha = gained.partition("/")
    for _kk, slot, hexv in sorted(before):
        if _kk != k or slot != "light":
            continue
        lbase, _, lalpha = hexv.partition("/")
        token = dt.token_for(lbase, is_dark=False)
        if token and dt.TOKENS[token][1].upper() == base and lalpha == alpha:
            return hexv
    return "(ambiguous)"


def main() -> int:
    paired_only = "--paired-only" in sys.argv[1:]
    by_file = "--by-file" in sys.argv[1:]
    intended = Counter()
    # Spec §5: the utilities with no dark half change dark mode when they are
    # tokenised, and that change is supposed to be INSPECTED rather than
    # absorbed. `--by-file` is what makes that inspection possible — per page,
    # which utility, what leaked, what it becomes.
    per_file: dict[str, Counter] = {}
    unexpected: list[str] = []
    files = 0

    for path in sorted((REPO_ROOT / "templates").rglob("*.html")):
        before = path.read_text(encoding="utf-8")
        after = dt.convert(before, paired_only=paired_only)
        if before == after:
            continue
        files += 1
        rel = path.relative_to(REPO_ROOT).as_posix()

        # The SAME scope function the converter rewrites with — markup
        # attributes and the JavaScript strings applied as classes at
        # runtime. A verifier that inspects a narrower region than the
        # converter touches reports success over changes it never saw.
        b_bodies = [before[a:b] for a, b in dt.class_scopes(before)]
        a_bodies = [after[a:b] for a, b in dt.class_scopes(after)]
        if len(b_bodies) != len(a_bodies):
            unexpected.append(
                f"{rel}  class-list COUNT {len(b_bodies)} -> {len(a_bodies)}")
        for b_attr, a_attr in zip(b_bodies, a_bodies):
            eb, ea = effective(b_attr), effective(a_attr)
            lost, gained = eb - ea, ea - eb

            for k, slot_name, hexv in sorted(lost):
                # An alias collapse changes the hex and MUST leave the opacity
                # alone; compare the two halves separately so a coincidental
                # alias cannot license an alpha change.
                base, _, alpha = hexv.partition("/")
                table = _INTENDED_LIGHT if slot_name == "light" else _INTENDED
                replacement = {h for kk, s, h in gained
                               if kk == k and s == slot_name}
                canonical = table.get(base)
                if canonical and f"{canonical}/{alpha}".rstrip("/") in replacement:
                    intended[f"alias {slot_name[0]}  {base} -> {canonical}"] += 1
                else:
                    unexpected.append(
                        f"{rel}  {k} {slot_name.upper()} lost {hexv}")

            for k, slot_name, hexv in sorted(gained):
                if slot_name != "dark":
                    if not any(kk == k and s == "light" for kk, s, _h in lost):
                        unexpected.append(f"{rel}  {k} LIGHT gained {hexv}")
                    continue
                had_dark = any(kk == k and s == "dark" for kk, s, _h in eb)
                if not had_dark:
                    # No dark value existed: the light colour was leaking into
                    # dark mode. This is the 870-utility fix the spec predicts.
                    intended[f"dark-mode value added  ({hexv})"] += 1
                    per_file.setdefault(rel, Counter())[
                        f"{k}: {_leaked_light(eb, k, hexv)} -> {hexv}"] += 1

    mode = "paired-only" if paired_only else "full"
    print(f"scanned {files} templates that the converter changes  [{mode}]\n")

    if by_file:
        for rel in sorted(per_file):
            rows = per_file[rel]
            print(f"{rel}  ({sum(rows.values())} elements gain a dark value)")
            for line, n in sorted(rows.items()):
                print(f"  {n:>4}x  {line}")
            print()
    print("INTENDED alias collapses (dark-mode consolidation):")
    for k, n in intended.most_common():
        print(f"  {n:>5}  {k}")
    if not intended:
        print("  (none)")

    if paired_only:
        # The whole point of the paired pass: a utility that had no dark half
        # is left alone, so nothing GAINS a dark value here. If one did, the
        # pass is not the inert half it claims to be.
        added = sum(n for k, n in intended.items() if k.startswith("dark-mode value added"))
        if added:
            unexpected.append(
                f"paired-only pass added {added} dark-mode value(s) — it must add none")

    print(f"\nUNEXPECTED differences: {len(unexpected)}")
    for line in unexpected[:25]:
        print(f"  {line}")
    if len(unexpected) > 25:
        print(f"  ... and {len(unexpected) - 25} more")

    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
