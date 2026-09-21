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

A class list holding a Jinja `{%` block is left alone entirely — see
`_is_static`. It is not statically known, and its branches are where status
colour lives.

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

# The icon at the top of a HEADING: the first <i> inside an <h1>, <h2> or
# <h3>. Named _HEADING_ICON — was _PAGE_TITLE_ICON, h1-only — because WP-6's
# heading ladder (DESIGN_SYSTEM_SPEC.md Part II §2.1/§2.4) extends the same
# rule downward: "VIOLET marks position at every scale. The bar's H1 says
# which page you are on; a card's H2 says which section you are in. Both are
# 'where you are'." So an H2 icon is violet with its text, same as the page
# title always was; an H3 icon is muted with ITS text instead, one rung down
# the same ladder. `_HEADING_ICON_TARGET` is the level -> token map.
#
# Left h1-only, this breaks the moment WP-6 steps 14-20 give a card's H2 its
# violet icon: step 2 below (every OTHER decorative icon -> turquoise) would
# see that icon, find no exemption for it, and revert it on the tool's very
# next run — exactly what already happened once to the page-title icon
# before its own exemption was keyed correctly (see
# test_the_assignment_is_idempotent's docstring). Widening the match now,
# ahead of any h2/h3 actually reaching the ladder, is what keeps that from
# happening a second time.
#
# `[a-z0-9-]+`, not `[a-z-]+`: 11 of the 139 distinct lucide names this tree
# uses end in a digit — settings-2, volume-2, trash-2, undo-2, table-2,
# loader-2, file-code-2, bar-chart-2, bar-chart-3, grid-3x3, edit-3.
# Measured 2026-08-28: 34 of the 472 icon sites in templates/**/*.html carry
# one, and the narrow class silently skipped every one of them. Three were
# sitting on `text-info` while `--check` reported the tree clean — a scanner
# that cannot see a rule's subjects reports success, it does not report that
# it looked at less than it was asked to.
#
# Three more digit-bearing icons live in templates/partials/settings/*.js,
# which neither this tool nor tests/test_design_roles.py scans (both glob
# `*.html`). They carry no colour class today, so nothing is wrong there yet.
_HEADING_ICON = re.compile(
    r"(<h([123])[^>]*>\s*<i data-lucide=\"[a-z0-9-]+\" class=\")([^\"]*)(\")", re.S)
_HEADING_ICON_TARGET = {"1": "text-brand", "2": "text-brand", "3": "text-muted"}
# Any other decorative icon.
_ICON = re.compile(r"(<i data-lucide=\"(?P<name>[a-z0-9-]+)\" class=\")(?P<cls>[^\"]*)(\")")

# Icons whose colour is information, not decoration.
_MEANINGFUL = {"info"}

_SECONDARY = re.compile(r"\btext-(?:info|brand)\b")


def _is_static(classes: str) -> bool:
    """Whether this class list is known at authoring time.

    A Jinja control block inside a class attribute means it is not:
    `{% if … %}text-critical{% else %}text-info{% endif %}` is a set of
    alternatives of which exactly one renders, and this tool cannot evaluate
    the condition that picks. Rewriting one branch recolours a state the tool
    never saw — and the branches are precisely where status colour lives.

    Measured on the tree 2026-08-28: 35 class lists across 10 templates carry
    `{%`. The two that sit on an `<i>` are tls_overview.html and
    updates_overview.html, and both switch between `text-critical` /
    `text-warning` and a neutral. Sweeping either into `text-accent` for
    consistency would paint an expired certificate turquoise — the exact lie
    the rule at the top of this file exists to prevent.

    This is a property of the class list, not a carve-out for two files: any
    class list assembled at render time is out of this tool's reach, wherever
    it appears. Such a site has to be converted by hand, branch by branch.
    """
    return "{%" not in classes


def _swap(classes: str, frm: str, to: str) -> str:
    return re.sub(rf"\b{frm}\b", to, classes)


def convert(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()

    # 1. Heading icons -> their level's token. Applied first, so step 2 sees
    #    the result.
    #
    #    h1 also accepts `text-accent` as an input, same as it always did:
    #    a page-title icon a pre-fix run of this tool wrongly turned
    #    turquoise is corrected rather than frozen — which is exactly what
    #    happened (test_the_assignment_is_idempotent).
    #
    #    h2/h3 do NOT accept `text-accent` as an input yet. `text-accent` is
    #    today's INTENDED colour for a card heading's icon until the heading
    #    ladder migration (DESIGN_SYSTEM_SPEC.md Part III steps 14-20)
    #    converts that heading's whole class string — text, weight, size and
    #    icon together — in one pass. "Card conversion IS the H2 migration;
    #    there is no separate H2 sweep" (spec C25): accepting accent here
    #    would make this tool exactly the separate sweep that rule forbids,
    #    repainting every card heading's icon violet or muted ahead of the
    #    rest of its own heading. Only `text-info` — a plain bug, an icon
    #    sitting on the informational-blue token that no heading should ever
    #    carry — is corrected at every level.
    def heading_icon(m: re.Match) -> str:
        if not _is_static(m.group(3)):
            return m.group(0)
        level = m.group(2)
        target = _HEADING_ICON_TARGET[level]
        frm = r"\btext-(?:info|accent)\b" if level == "1" else r"\btext-info\b"
        cls = re.sub(frm, target, m.group(3))
        if cls != m.group(3):
            counts[f"h{level} heading icon -> {target.split('-', 1)[1]}"] += 1
        return m.group(1) + cls + m.group(4)

    text = _HEADING_ICON.sub(heading_icon, text)

    # The span of each heading icon's CLASS ATTRIBUTE — h1, h2 AND h3 now,
    # not just h1 — which is what step 2 matches on. Keyed on this position
    # rather than the `<h1`/`<h2`/`<h3` start, because the two patterns start
    # in different places and a position keyed on the wrong one never fires:
    # that is exactly how step 2 once turned every page-title icon back to
    # turquoise while reporting nothing to do.
    headings = {m.start(3) for m in _HEADING_ICON.finditer(text)}

    # 2. Every other decorative icon -> turquoise.
    def icon(m: re.Match) -> str:
        if m.start("cls") in headings or m.group("name") in _MEANINGFUL:
            return m.group(0)
        if not _is_static(m.group("cls")):
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
        if not _is_static(body):
            continue

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
