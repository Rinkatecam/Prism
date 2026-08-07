"""Focus styling — keyboard affordance without the mouse-click artefact.

Measured before this change: 87 `focus:` utilities, 0 `focus-visible:`.
`:focus` matches a mouse click too, so clicking a button left a ring stuck
on it. The usual response is to delete the ring, which takes the keyboard
affordance with it — four `focus:outline-none` utilities were already doing
a partial version of that.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt          # noqa: E402
from tools import migrate_focus_visible as mfv  # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"
_BARE_FOCUS = re.compile(r"(?<![\w-])focus:")


def test_the_converter_moves_a_plain_focus_utility():
    assert mfv.convert('<input class="focus:ring-2 focus:ring-brand">') == \
        '<input class="focus-visible:ring-2 focus-visible:ring-brand">'


def test_peer_focus_is_not_touched():
    """`peer-focus:` styles a DIFFERENT element from the one being focused —
    a floating label reacting to its input. `peer-focus-visible` would stop
    matching in exactly the case it exists for, and a label that no longer
    lifts is not obviously a focus bug when you go looking."""
    src = '<label class="peer-focus:text-brand group-focus:underline">'
    assert mfv.convert(src) == src


def test_a_stylesheet_pseudo_class_is_not_touched():
    """`:focus` in CSS is not the Tailwind variant, and the converter only
    walks class lists."""
    src = "<style>.btn:focus { outline: none }</style>"
    assert mfv.convert(src) == src


def test_prose_is_not_touched():
    src = "<p>Set the focus: sharp, then retry.</p>"
    assert mfv.convert(src) == src


def test_the_conversion_is_idempotent():
    once = mfv.convert('<a class="focus:ring-2">')
    assert mfv.convert(once) == once


def test_no_template_still_uses_a_bare_focus_variant():
    """The guardrail. Without it the next hand-written control reintroduces
    the artefact, and nothing fails."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            for m in _BARE_FOCUS.finditer(text[start:end]):
                line = text.count("\n", 0, start + m.start()) + 1
                offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}")
    assert not offenders, (
        "use focus-visible: so the ring does not appear on a mouse click "
        "(run tools/migrate_focus_visible.py):\n  " + "\n  ".join(offenders[:20]))


def test_every_focusable_control_that_kills_its_outline_supplies_a_ring():
    """`outline-none` with nothing in its place is an invisible focus state.

    Four elements suppress the native outline. Each must draw its own, or
    keyboard users lose the control entirely — the failure mode is silent
    for anyone using a mouse, which is everyone who reviews it.
    """
    naked = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            body = text[start:end]
            if "outline-none" not in body:
                continue
            if not re.search(r"focus(-visible)?:(ring|outline|border)", body):
                line = text.count("\n", 0, start) + 1
                naked.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}")
    assert not naked, (
        "outline suppressed with no focus ring to replace it:\n  "
        + "\n  ".join(naked))
