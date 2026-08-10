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
    pattern = re.compile(
        r"<h1[^>]*>\s*<i data-lucide=\"[a-z-]+\" class=\"([^\"]*)\"", re.S)
    seen, wrong = 0, []
    for path in _templates():
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
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
    pattern = re.compile(r"<i data-lucide=\"([a-z-]+)\" class=\"([^\"]*)\"")
    for path in _templates():
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            if m.group(1) == "info":
                continue
            if re.search(r"\btext-info\b", m.group(2)):
                offenders.append(f"{path.name}: {m.group(1)}")
    assert not offenders, (
        "decorative icons take text-accent, page titles text-brand:\n  "
        + "\n  ".join(offenders))


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
