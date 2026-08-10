"""A rounded element must not be its own scroll container.

Chromium paints `::-webkit-scrollbar` in the element's padding box and does
NOT clip it to `border-radius`. So a card that scrolls has a straight 8px bar
running past its own rounded corners — the card reads as square down its
right-hand edge, like a sticker laid over it rather than part of it.

Invisible at a 4px radius. Obvious at 16px, which is where Phase 2 put cards.
The fix is structural, not cosmetic: rounding and clipping on an outer shell,
scrolling on an inner element.

    <div class="rounded-lg border overflow-hidden">   <- clips
      <div class="max-h-96 overflow-y-auto">          <- scrolls
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt  # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"

_SCROLLS = re.compile(r"\boverflow(?:-[xy])?-(?:auto|scroll)\b")
# `rounded-none` is explicitly square, so it is not a rounded element.
_ROUNDED = re.compile(r"\brounded(?:-(?:t|r|b|l|tl|tr|bl|br))?"
                      r"(?:-(?:sm|md|lg|xl|2xl|3xl|full))?\b")


def _offenders() -> list[str]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            body = text[start:end]
            if not (_SCROLLS.search(body) and _ROUNDED.search(body)):
                continue
            # `overflow-y-auto` alongside `rounded-full` on a pill is not the
            # shape being guarded; only vertical/both scrolling matters, and a
            # pill never scrolls in practice. Keep the rule simple and let the
            # allowlist carry any genuine exception.
            line = text.count("\n", 0, start) + 1
            found.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}")
    return found


# Genuine exceptions, each justified. Empty today.
ALLOWED: set[str] = set()


def test_no_rounded_element_is_also_a_scroll_container():
    offenders = sorted(set(_offenders()) - ALLOWED)
    assert not offenders, (
        "these elements are rounded AND scroll, so their scrollbar is painted "
        "straight across the corner. Move the radius and overflow-hidden to a "
        "wrapper and leave the scrolling on the inner element:\n  "
        + "\n  ".join(offenders))


def test_the_detector_recognises_the_shape_it_guards_against():
    """A guardrail whose detector never matches passes for the wrong reason.
    This is the exact markup that was in activity_feed.html."""
    bad = '<div class="bg-card rounded-lg border overflow-hidden max-h-[400px] overflow-y-auto">'
    assert _SCROLLS.search(bad) and _ROUNDED.search(bad)

    good_outer = '<div class="bg-card rounded-lg border overflow-hidden">'
    good_inner = '<div class="max-h-[400px] overflow-y-auto">'
    assert not (_SCROLLS.search(good_outer) and _ROUNDED.search(good_outer))
    assert not (_SCROLLS.search(good_inner) and _ROUNDED.search(good_inner))
