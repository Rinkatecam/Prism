"""The corner radius scale — see docs/plans/DESIGN_TOKENS_SPEC.md §4.

Five radii were in use with no rule. The scale is now two values and a pill,
and it is declared in exactly one place: `theme.extend.borderRadius` in
templates/base.html.

Phase 1a overrode only `sm` and `md`. That set the scale by VALUE but left
738 of the 750 uses reading Tailwind's own DEFAULT and `lg`, so the rule
governed 12 elements and the promised "one line to soften the corners" would
have moved almost nothing. These tests exist so that cannot recur.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "templates" / "base.html"

# The scale. `full` is Tailwind's own 9999px and is not overridden.
SCALE = {"DEFAULT": "0.5rem", "sm": "0.5rem", "md": "1rem", "lg": "1rem"}

_RADIUS_CLASS = re.compile(
    r"\brounded(?:-(?:t|r|b|l|tl|tr|bl|br))?"
    r"(?:-(none|sm|md|lg|xl|2xl|3xl|full))?\b")


def _config_radii() -> dict[str, str]:
    block = re.search(r"borderRadius: \{(.*?)\}", BASE.read_text(encoding="utf-8"), re.S)
    assert block, "borderRadius is missing from tailwind.config in base.html"
    return dict(re.findall(r"(\w+): '([^']+)'", block.group(1)))


def test_the_scale_is_declared_exactly_as_intended():
    assert _config_radii() == SCALE


def test_every_radius_name_the_templates_use_is_one_the_config_pins():
    """A class reading Tailwind's default instead of the scale is invisible
    until someone changes the scale and it does not move. That is precisely
    what happened to 738 elements in Phase 1a."""
    pinned = set(SCALE) | {"full"}
    used: set[str] = set()
    for path in (PROJECT_ROOT / "templates").rglob("*.html"):
        for m in _RADIUS_CLASS.finditer(path.read_text(encoding="utf-8")):
            used.add(m.group(1) or "DEFAULT")
    unpinned = sorted(used - pinned)
    assert not unpinned, (
        f"radius name(s) not pinned by the config: {unpinned}. Either add "
        f"them to theme.extend.borderRadius or fold them into the scale.")


def test_the_scale_has_only_two_distinct_values():
    """Three radii was the goal and five was the problem. Two plus a pill is
    the scale; a third value creeping back in is a regression.

    Reads the CONFIG, not the SCALE constant above. Asserting that a literal
    written in this file has two distinct values is a tautology — it passed
    unchanged while a mutation put a third value into base.html.
    """
    values = set(_config_radii().values())
    assert len(values) == 2, f"the scale drifted to {len(values)} values: {sorted(values)}"


def test_the_small_radius_is_smaller_than_the_large_one():
    def rem(v: str) -> float:
        return float(v.removesuffix("rem"))
    radii = _config_radii()
    assert rem(radii["sm"]) < rem(radii["md"]), "sm must be tighter than md"
    assert radii["sm"] == radii["DEFAULT"], "bare `rounded` must equal sm"
    assert radii["md"] == radii["lg"], "`rounded-lg` must equal md"
