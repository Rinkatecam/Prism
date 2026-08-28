"""Which of the two brand colours a surface gets — see tools/migrate_brand_roles.py.

One question decides it: is this the interface responding to YOU, or is it
part of the furniture?

    VIOLET (`brand`)     interaction and selection — focus, caret, the
                         scrollbar thumb, the sidebar's active item, checkbox
                         ticks, toggles, non-status filter chips, and the
                         icon at the top of a page.

    TURQUOISE (`accent`) the secondary layer — icons inside cards, and the
                         primary action buttons.

It reads inverted written down. It is not: violet marks where you ARE and
turquoise marks what you can DO. A page has one focus and many buttons, so
making both violet leaves nothing to separate the ring around the field you
are typing in from the twelve buttons around it.

STATUS COLOUR IS NOT BRAND COLOUR. Nothing here touches healthy, warning,
critical or offline — those mean something the reader has to act on, and a
warning triangle turned turquoise for consistency would be a lie.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import design_tokens as dt          # noqa: E402
from tools import migrate_brand_roles as mbr   # noqa: E402

TEMPLATES = PROJECT_ROOT / "templates"

# The two scans every rule below is built on, hoisted out of the individual
# tests so there is ONE definition of "an icon" to narrow — and so the
# positive control at the bottom of this file is guarding the same regex the
# assertions use rather than a copy of it.
#
# `[a-z0-9-]+`, not `[a-z-]+`: 11 of the 139 distinct lucide names in this
# tree end in a digit (settings-2, trash-2, bar-chart-3, grid-3x3, edit-3,
# undo-2, volume-2, loader-2, table-2, file-code-2, bar-chart-2), covering 34
# of 472 icon sites. See test_the_icon_scan_can_see_a_digit_in_the_name.
_TITLE_ICON = re.compile(
    r"<h1[^>]*>\s*<i data-lucide=\"[a-z0-9-]+\" class=\"([^\"]*)\"", re.S)
_ICON = re.compile(r"<i data-lucide=\"([a-z0-9-]+)\" class=\"([^\"]*)\"")


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_the_assignment_is_idempotent():
    """The tool is the definition of the rule, so re-running it over a
    converted tree must be a no-op. It was not: the page-title exemption
    compared an `<h1>` match position against an `<i>` match position, never
    fired, and step 2 turned every page title back to turquoise — the rule
    ran, reported nine changes, and achieved nothing."""
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        once, _ = mbr.convert(text)
        twice, counts = mbr.convert(once)
        assert once == twice, f"{path.name}: not idempotent"
        assert not counts, f"{path.name}: second pass still wants {dict(counts)}"


def test_every_page_title_icon_is_violet():
    """The one icon per page that says which page this is."""
    seen, wrong = 0, []
    for path in _templates():
        for m in _TITLE_ICON.finditer(path.read_text(encoding="utf-8")):
            seen += 1
            if "text-brand" not in m.group(1):
                colour = re.search(r"text-[a-z-]+", m.group(1))
                wrong.append(f"{path.name}: {colour.group(0) if colour else '(none)'}")
    assert seen >= 9, f"only {seen} page titles matched — the pattern has drifted"
    assert not wrong, "page-title icons must be text-brand:\n  " + "\n  ".join(wrong)


def test_no_decorative_icon_is_left_on_the_informational_blue():
    """`text-info` on an icon meant "blue", not "information". The single
    `data-lucide="info"` is exempt because there it means exactly that."""
    offenders = []
    for path in _templates():
        for m in _ICON.finditer(path.read_text(encoding="utf-8")):
            if m.group(1) == "info":
                continue
            if re.search(r"\btext-info\b", m.group(2)):
                offenders.append(f"{path.name}: {m.group(1)}")
    assert not offenders, (
        "decorative icons take text-accent, page titles text-brand:\n  "
        + "\n  ".join(offenders))


def test_the_icon_scan_can_see_a_digit_in_the_name():
    """The guard on the guard above: `[a-z-]+` cannot match `settings-2`.

    Measured on this tree 2026-08-28, before the character class was widened:
    the scan saw 438 icon sites and there are 472. The 34 it was blind to
    included settings.html:63's `<i data-lucide="settings-2" class="w-5 h-5
    text-info">`, monitoring.html's `volume-2` and server_comparison.html's
    `bar-chart-3` — three text-info icons sitting in plain sight while
    test_no_decorative_icon_is_left_on_the_informational_blue passed and
    `migrate_brand_roles.py --check` reported nothing to do. A narrow
    character class does not fail; it shrinks what the rules govern and
    reports green over the part it dropped.

    So this asserts a MATCH rather than an absence: narrow the class back and
    this test goes red, instead of every other test in the file going quiet.

    The migrator's two patterns are asserted alongside the tests' own,
    because a rule the converter cannot see is only enforced until someone
    runs the converter — and the converter is what rewrites the tree.
    """
    sample = ('<h1 class="x"><i data-lucide="settings-2" '
              'class="w-5 h-5 text-brand"></i>Settings</h1>')
    assert _ICON.search(sample), "the icon scan cannot see a digit in a name"
    assert _TITLE_ICON.search(sample), "the page-title scan cannot see a digit"
    assert mbr._ICON.search(sample), "the migrator cannot see a digit in a name"
    assert mbr._PAGE_TITLE_ICON.search(sample), (
        "the migrator's page-title scan cannot see a digit")

    names, sites = set(), 0
    for path in _templates():
        for m in _ICON.finditer(path.read_text(encoding="utf-8")):
            names.add(m.group(1))
            sites += 1
    assert "settings-2" in names, (
        "settings-2 is in templates/settings.html but the scan does not "
        "report it — the character class has been narrowed")
    digits = sorted(n for n in names if any(c.isdigit() for c in n))
    assert len(digits) >= 11, (
        f"only {len(digits)} digit-bearing icon names visible: {digits} — "
        "measured 11 across 34 sites")
    assert sites >= 470, f"only {sites} icon sites scanned — measured 472"


def test_no_primary_button_is_left_on_the_informational_blue():
    """A filled `bg-info text-white` button was the app's primary action."""
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            body = text[start:end]
            if re.search(r"\bbg-info\b(?!/)", body) and "text-white" in body:
                offenders.append(f"{path.name}:{text.count(chr(10), 0, start) + 1}")
    assert not offenders, (
        "primary buttons take bg-accent:\n  " + "\n  ".join(offenders))


def test_a_filled_turquoise_button_inverts_its_label_in_dark_mode():
    """Measured: white on the light-mode fill #0F766E is 5.47:1, and white on
    the dark-mode fill #2DD4BF is 1.86:1 — unreadable. Dark mode has to take
    a dark label. Every filled accent button therefore needs the override,
    and one without it is invisible in exactly one theme."""
    naked = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for start, end in dt.class_scopes(text):
            body = text[start:end]
            if not (re.search(r"\bbg-accent\b(?!/)", body) and "text-white" in body):
                continue
            if not re.search(r"\bdark:text-\w", body):
                naked.append(f"{path.name}:{text.count(chr(10), 0, start) + 1}")
    assert not naked, (
        "a filled bg-accent button needs a dark-mode label override:\n  "
        + "\n  ".join(naked))


def test_status_colours_were_not_swept_up():
    """The guard on the whole exercise. If a sweep for consistency ever
    recolours a warning or a critical, the interface starts lying."""
    counts = {}
    for path in _templates():
        for name in ("healthy", "warning", "critical"):
            counts[name] = counts.get(name, 0) + len(re.findall(
                rf"\btext-{name}\b", path.read_text(encoding="utf-8")))
    # Measured on the tree at the time the roles were assigned.
    assert counts["critical"] >= 90, counts
    assert counts["warning"] >= 60, counts
    assert counts["healthy"] >= 40, counts


def test_the_two_brand_colours_are_separated_by_hue():
    """Violet and turquoise are told apart by HUE, not by lightness.

    Measured: in dark mode `brand` #C4B5FD and `accent` #2DD4BF sit 0.005
    apart in relative luminance — effectively identical. Anyone who cannot
    use the hue cue sees one colour.

    That is acceptable here, and the reason is worth writing down rather than
    discovering later: neither colour ever encodes information on its own.
    Violet marks focus and selection, which are also marked by a ring, a
    filled checkbox or an accent bar; turquoise marks buttons and icons,
    which are also marked by being buttons and icons. Nothing in the
    interface requires the reader to distinguish the two to understand it,
    so WCAG 1.4.1 is satisfied by structure rather than by contrast.

    What must not happen is the two drifting into the same hue, at which
    point the distinction stops existing for everybody.
    """
    def hue(h: str) -> float:
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        hi, lo = max(r, g, b), min(r, g, b)
        if hi == lo:
            return 0.0
        d = hi - lo
        if hi == r:
            deg = ((g - b) / d) % 6
        elif hi == g:
            deg = (b - r) / d + 2
        else:
            deg = (r - g) / d + 4
        return deg * 60

    for index, theme in ((0, "light"), (1, "dark")):
        a, b = dt.TOKENS["brand"][index], dt.TOKENS["accent"][index]
        apart = abs(hue(a) - hue(b))
        apart = min(apart, 360 - apart)
        assert apart > 60, (
            f"{theme}: brand {a} and accent {b} are only {apart:.0f}° apart "
            "in hue, and they carry no lightness difference to fall back on")
