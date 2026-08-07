"""Move focus styling from `focus:` to `focus-visible:`.

    python tools/migrate_focus_visible.py --check
    python tools/migrate_focus_visible.py

WHY
---
Measured across the templates: **87** `focus:` utilities and **0**
`focus-visible:` ones. `:focus` matches a mouse click as well as keyboard
navigation, so clicking any button left a 2px ring stuck on it until focus
moved elsewhere. That is what the four `focus:outline-none` utilities are
compensating for, and it is why focus rings get quietly deleted from
codebases — the fix people reach for removes the keyboard affordance too.

`:focus-visible` is the browser's own answer: it matches when the user is
navigating by keyboard and not when they are pointing. Text inputs still
match on click, because typing follows a click into a field, so nothing is
lost there.

SCOPE
-----
Rewrites only inside a class list — `tools/design_tokens.class_scopes()`,
the same regions the colour converter walks, covering markup attributes,
`<script>` class strings and Jinja expressions. `:focus` in a stylesheet and
the word "focus:" in prose are both left alone.

`peer-focus:` and `group-focus:` are NOT touched. They style a DIFFERENT
element from the one receiving focus — a floating label reacting to its
input — and `peer-focus-visible` would silently stop matching in the case
those exist for. Measured: 20 uses, all of them that shape.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt  # noqa: E402

# `(?<![\w-])` so `peer-focus:` and `group-focus:` are not matched — the
# lookbehind rejects the hyphen that precedes them.
_FOCUS = re.compile(r"(?<![\w-])focus:")


def convert(text: str) -> str:
    """Rewrite `focus:` to `focus-visible:` inside class lists only."""
    out = text
    for start, end in reversed(dt.class_scopes(text)):
        out = out[:start] + _FOCUS.sub("focus-visible:", text[start:end]) + out[end:]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="defaults to every template")
    parser.add_argument("--check", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    paths = ([Path(p) for p in args.paths]
             or sorted((root / "templates").rglob("*.html")))

    changed = total = 0
    for path in paths:
        before = path.read_text(encoding="utf-8")
        after = convert(before)
        if before == after:
            continue
        n = len(_FOCUS.findall(before)) - len(_FOCUS.findall(after))
        changed += 1
        total += n
        print(f"  {path.relative_to(root).as_posix()}: {n} utility(ies)")
        if not args.check:
            path.write_text(after, encoding="utf-8")

    verb = "would move" if args.check else "moved"
    print(f"\n{verb} {total} utility(ies) in {changed} file(s) to focus-visible:")
    return 0


if __name__ == "__main__":
    sys.exit(main())
