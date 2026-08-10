"""Assign every non-status surface to violet or turquoise.

    python tools/migrate_brand_roles.py --check
    python tools/migrate_brand_roles.py

THE RULE
--------
Two brand colours, and one question decides which: is this the interface
responding to YOU, or is it part of the furniture?

    VIOLET (`brand`)    interaction and selection — focus, carets, the
                        scrollbar thumb, the sidebar's active item, checkbox
                        ticks, filter chips that are not statuses, and the
                        icon at the top of a page.

    TURQUOISE (`accent`) the secondary layer — icons inside cards, and the
                        primary action buttons.

STATUS COLOUR IS NOT BRAND COLOUR and nothing here touches it. `healthy`,
`warning`, `critical` and `offline` mean something the user has to read; a
warning triangle that turned turquoise for consistency would be a lie. The
one `data-lucide="info"` icon is exempt for the same reason — it is an
information indicator, not decoration.

WHY BUTTONS ARE THE SECONDARY COLOUR
------------------------------------
It looks inverted written down. It is not: violet marks where you ARE and
turquoise marks what you can DO, and a page has one focus but many buttons.
Making both violet leaves nothing to distinguish the ring around the field
you are typing in from the twelve buttons around it.

A filled turquoise button flips its label between themes — measured, white
on #0F766E is 5.47:1 and white on #2DD4BF is 1.86:1, so dark mode takes the
near-black `page` instead at 10.84:1.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import design_tokens as dt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# The icon at the top of a page: the first <i> inside an <h1>.
_PAGE_TITLE_ICON = re.compile(
    r"(<h1[^>]*>\s*<i data-lucide=\"[a-z-]+\" class=\")([^\"]*)(\")", re.S)
# Any other decorative icon.
_ICON = re.compile(r"(<i data-lucide=\"(?P<name>[a-z-]+)\" class=\")(?P<cls>[^\"]*)(\")")

# Icons whose colour is information, not decoration.
_MEANINGFUL = {"info"}

_SECONDARY = re.compile(r"\btext-(?:info|brand)\b")


def _swap(classes: str, frm: str, to: str) -> str:
    return re.sub(rf"\b{frm}\b", to, classes)


def convert(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()

    # 1. Page-title icons -> violet. Applied first, so step 2 sees the result.
    #    `text-accent` is accepted as an input too, so a title icon that a
    #    previous run wrongly turned turquoise is corrected rather than
    #    frozen — which is exactly what happened.
    def title(m: re.Match) -> str:
        cls = re.sub(r"\btext-(?:info|accent)\b", "text-brand", m.group(2))
        if cls != m.group(2):
            counts["page-title icon -> violet"] += 1
        return m.group(1) + cls + m.group(3)

    text = _PAGE_TITLE_ICON.sub(title, text)

    # The span of the title icon's CLASS ATTRIBUTE, which is what step 2
    # matches on. Keyed on the `<h1` position instead, the exemption never
    # fired — the two patterns start in different places — so step 2 turned
    # every page title back to turquoise and the rule silently did nothing.
    titles = {m.start(2) for m in _PAGE_TITLE_ICON.finditer(text)}

    # 2. Every other decorative icon -> turquoise.
    def icon(m: re.Match) -> str:
        if m.start("cls") in titles or m.group("name") in _MEANINGFUL:
            return m.group(0)
        cls = _SECONDARY.sub("text-accent", m.group("cls"))
        if cls != m.group("cls"):
            counts["card icon -> turquoise"] += 1
        return m.group(1) + cls + m.group(4)

    text = _ICON.sub(icon, text)

    # 3 and 4 work on class LISTS, so they see markup and JS-built strings
    #    alike, and never touch prose.
    out = text
    for start, end in reversed(dt.class_scopes(text)):
        body = new = text[start:end]

        # A toggle's ON state is a selection, like a checked checkbox — the
        # blue was arbitrary. The 4 that use `healthy`/`critical` are left
        # alone: those are saying something about the setting, not about
        # whether it is on.
        toggled, n = re.subn(r"\bpeer-checked:bg-info\b", "peer-checked:bg-brand", new)
        if n:
            new = toggled
            counts["toggle -> violet"] += n

        is_filled = re.search(r"\bbg-info\b(?!/)", new) and "text-white" in new
        if "status-filter-btn" in new and re.search(r"\bbg-info\b(?!/)", new):
            # A filter chip is a selection, not an action. `All` is not a
            # status, so it takes the selection colour.
            new = re.sub(r"\bbg-info\b(?!/)", "bg-brand", new)
            counts["filter chip -> violet"] += 1
        elif is_filled:
            new = re.sub(r"\bbg-info\b(?!/)", "bg-accent", new)
            new = re.sub(r"\bhover:bg-info-strong\b", "hover:bg-accent-strong", new)
            # White is unreadable on the light turquoise dark mode uses.
            if not re.search(r"\bdark:text-\w", new):
                new = re.sub(r"\btext-white\b", "text-white dark:text-page", new)
            counts["primary button -> turquoise"] += 1

        if new != body:
            out = out[:start] + new + out[end:]

    return out, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report, write nothing")
    args = ap.parse_args()

    total: Counter = Counter()
    for path in sorted((REPO_ROOT / "templates").rglob("*.html")):
        before = path.read_text(encoding="utf-8")
        after, counts = convert(before)
        if before == after:
            continue
        total += counts
        print(f"  {path.relative_to(REPO_ROOT).as_posix()}: "
              + ", ".join(f"{v} {k}" for k, v in counts.items()))
        if not args.check:
            path.write_text(after, encoding="utf-8")

    print()
    for role, n in total.most_common():
        print(f"  {n:>4}  {role}")
    print(f"\n{'would change' if args.check else 'changed'} {sum(total.values())} site(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
