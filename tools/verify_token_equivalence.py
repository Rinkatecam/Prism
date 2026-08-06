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
    r"-(?P<token>" + "|".join(dt.TOKENS) + r")\b")
_BUILTIN = re.compile(
    r"(?P<variants>(?:" + dt._VARIANT + r")*)"
    r"(?P<util>text|bg|border|ring)-(?P<name>white|black)\b")

_BUILTIN_HEX = {"white": "#FFFFFF", "black": "#000000"}

# Aliased darks: these deliberately collapse onto a canonical value. Each is a
# small, intended dark-mode change, recorded rather than hidden.
_INTENDED = {a.upper(): dt.TOKENS[t][1].upper() for a, t in dt.ALIASES.items()}


def effective(body: str) -> set[tuple[str, str, str]]:
    """Every (utility-key, 'light'|'dark', hex) this class list can produce.

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

    for m in _TOK.finditer(body):
        light, dark = dt.TOKENS[m.group("token")]
        _is_dark, k = key(m)
        out.add((k, "light", light.upper()))
        out.add((k, "dark", dark.upper()))
    for m in _BUILTIN.finditer(body):
        is_dark, k = key(m)
        out.add((k, "dark" if is_dark else "light", _BUILTIN_HEX[m.group("name")]))
    for m in _ARB.finditer(body):
        is_dark, k = key(m)
        out.add((k, "dark" if is_dark else "light", m.group("hex").upper()))

    return out


def main() -> int:
    intended = Counter()
    unexpected: list[str] = []
    files = 0

    for path in sorted((REPO_ROOT / "templates").rglob("*.html")):
        before = path.read_text(encoding="utf-8")
        after = dt.convert(before)
        if before == after:
            continue
        files += 1
        rel = path.relative_to(REPO_ROOT).as_posix()

        for b_attr, a_attr in zip(_CLASS_ATTR.findall(before),
                                  _CLASS_ATTR.findall(after)):
            eb, ea = effective(b_attr), effective(a_attr)
            lost, gained = eb - ea, ea - eb

            for k, slot_name, hexv in sorted(lost):
                if slot_name == "light":
                    unexpected.append(f"{rel}  {k} LIGHT lost {hexv}")
                    continue
                replacement = {h for kk, s, h in gained if kk == k and s == "dark"}
                if _INTENDED.get(hexv) in replacement:
                    intended[f"alias  {hexv} -> {_INTENDED[hexv]}"] += 1
                else:
                    unexpected.append(f"{rel}  {k} DARK lost {hexv}")

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

    print(f"scanned {files} templates that the converter changes\n")
    print("INTENDED alias collapses (dark-mode consolidation):")
    for k, n in intended.most_common():
        print(f"  {n:>5}  {k}")
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
