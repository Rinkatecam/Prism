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
#
# WP-6 §2.1/§2.4 widens this from h1-only ("the page title") to h1/h2/h3
# ("the heading ladder"): an H2's icon is violet with its text, same as the
# page title always was; an H3's is muted with its text instead. Mirrors
# tools/migrate_brand_roles.py's own _HEADING_ICON, kept as an independent
# object rather than an import so a rule the migrator enforces is also
# checked by a scan that shares none of its code.
_HEADING_ICON = re.compile(
    r"<h([123])\b([^>]*)>\s*<i data-lucide=\"[a-z0-9-]+\" class=\"([^\"]*)\"", re.S)
_ICON = re.compile(r"<i data-lucide=\"([a-z0-9-]+)\" class=\"([^\"]*)\"")

# WP-6 D2/C22 — base.html's topbar <h1 id="page-title"> is the only h1 left
# anywhere (test_the_one_page_title_icon_is_violet below asserts that), and
# every page supplies its OWN icon by overriding `{% block page_icon %}`.
# That means base.html's own source never shows a resolved icon NAME — the
# attribute reads `data-lucide="{% block page_icon %}circle-dot{% endblock
# %}"` — so `_HEADING_ICON` above (which requires a literal name, same as
# the migrator it mirrors) structurally cannot match it. This is the one
# heading-icon site that needs a pattern of its own; h2/h3 keep their icon
# name as a plain literal exactly as before and _HEADING_ICON sees them
# fine, which is why this file has two patterns instead of a single one
# loosened to fit both shapes.
_TOPBAR_H1_ICON = re.compile(
    r'<h1 id="page-title"[^>]*>\s*<i data-lucide="[^"]*"\s+class="([^"]*)"', re.S)

# WP-6 §2.6 — a heading whose colour the ladder does not govern says so by
# attribute, not by naming convention: a dialog's accessible name (its
# colour is fixed regardless of level), the workflow guide panel's
# deliberately theme-invariant chrome, and a verdict banner where a status
# colour outranks the ladder because a critical verdict rendered violet
# would be a lie. None of the three carry a heading icon today — measured
# below as a positive control that stays honest if one ever does.
_HEADING_CARVE_OUTS = {"dialog-title", "chrome", "verdict"}


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


def test_the_one_page_title_icon_is_violet():
    """WP-6 D2/C22 — every page's title now lives in base.html's topbar H1;
    replaces test_every_page_title_icon_is_violet, whose `seen >= 9` counted
    an <h1><i> pair per page and necessarily collapsed to 1 the instant the
    H1s consolidated. It failed for the right reason (DESIGN_SYSTEM_SPEC.md
    Part II §8.5 names this exact replacement), not a regression to chase."""
    h1_sites = sorted(path.name for path in _templates()
                       if re.search(r"<h1\b", path.read_text(encoding="utf-8")))
    assert h1_sites == ["base.html"], (
        f"exactly one file may contain an <h1>, and it is base.html: {h1_sites}")

    m = _TOPBAR_H1_ICON.search((TEMPLATES / "base.html").read_text(encoding="utf-8"))
    assert m, "base.html's #page-title <h1> no longer carries a leading icon"
    assert "text-brand" in m.group(1), (
        f"the page-title icon must be text-brand, is: {m.group(1)!r}")


# WP-6 §8.1/§2.1 — an H2's icon is text-brand and an H3's is text-muted once
# that heading reaches the ladder, which happens one card family at a time
# in DESIGN_SYSTEM_SPEC.md Part III steps 14-20 ("card conversion IS the H2
# migration — there is no separate H2 sweep", C25). A flat "every one
# already is" assert is unsatisfiable the moment it is written: measured on
# this tree 2026-09-03, immediately after WP-6 step 12 consolidated every
# page's own <h1>, ALL 49 <h2><i> pairs and 19 of 22 <h3><i> pairs are still
# on their PRE-ladder colour (mostly text-accent, plus the handful of
# legitimate status carve-outs §3.4 permits — Danger Zone, Incidents,
# All Clear — which this ratchet cannot tell apart from "not yet migrated"
# and does not try to; both count against the same baseline, and a rung
# that is a genuine, permanent carve-out simply keeps its file's count above
# zero forever, the same way workflows.html's theme-invariant literals sit
# in LITERAL_BASELINE without ever reaching 0).
#
# So these are RATCHETS — the shape DESIGN_SYSTEM_SPEC.md §0 itself names
# for exactly this state ("the rule cannot be met yet") — seeded by running
# this file's own detector against the tree as it stands right now, per the
# spec's own C28 ("every ratchet is seeded by running its own detector,
# never by typing a number"). Steps 14-20 lower these two dicts (and this
# file's totals) as each card family converts; test_design_headings.py's
# eventual HEADING_BASELINE (spec step 13) will likely subsume both, at
# which point these may be deleted rather than merged.
# Lowered 2026-09-17 by WP-6 step 15 (Batch B, the Settings family) --
# RE-RUN, not hand-computed. This file's own comment above already named
# this exact consequence ("Steps 14-20 lower these two dicts... as each
# card family converts"). All ten settings-family entries (settings.html
# and nine partials) reached exactly zero and are deleted rather than kept
# at 0: every H2 icon step 15 converted now renders via card()'s own
# H2_ICON constant (`w-5 h-5 flex-shrink-0 text-brand`, already
# text-brand/violet by construction), and a macro-rendered `<h2><i>` pair
# is not literal `<h2` source text, so it is invisible to THIS file's
# source-level regex scan the same way it is to test_design_headings.py's
# HEADING_BASELINE -- proven compliant instead by test_design_cards.py's
# C-7 (renders the macro for real) and C-1 (the icon class is the pinned
# H2_ICON string, character for character). H3_ICON_BASELINE is
# UNCHANGED by this step: the six settings.html div-pseudo-headings
# converted to subhead() were `<div>`s, never `<h3>` tags, so they were
# never counted here either before (0) or after (0) -- this file's
# remaining settings.html/_server_config.html H3 entries are unrelated,
# pre-existing modal-title icons (step 19's job).
H2_ICON_BASELINE: dict[str, int] = {
    "dashboard.html": 1,
    "monitoring.html": 1,
    "operations.html": 3,
    "partials/active_actions.html": 1,
    "partials/critical_issues.html": 1,
    "partials/server_comparison.html": 1,
    "partials/services_table.html": 1,
    "partials/updates_overview.html": 1,
    "reports.html": 7,
    "server_detail.html": 9,
    "servers.html": 1,
    "workflows.html": 1,
}
H2_ICON_TOTAL = 28

H3_ICON_BASELINE: dict[str, int] = {
    "operations.html": 3,
    "partials/server_comparison.html": 3,
    "partials/settings/_server_config.html": 2,
    "server_detail.html": 4,
    "servers.html": 1,
    "settings.html": 4,
    "topology.html": 1,
    "workflows.html": 1,
}
H3_ICON_TOTAL = 19


def _non_compliant_heading_icons(level: str, target: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for m in _HEADING_ICON.finditer(text):
            if m.group(1) != level:
                continue
            role = re.search(r'data-role="([^"]+)"', m.group(2))
            if role and role.group(1) in _HEADING_CARVE_OUTS:
                continue
            if target not in m.group(3):
                rel = path.relative_to(TEMPLATES).as_posix()
                counts[rel] = counts.get(rel, 0) + 1
    return counts


def test_every_section_heading_icon_is_violet():
    """Ratchet (see the block comment above H2_ICON_BASELINE): a non-carve-
    out H2 icon not yet text-brand may not exceed its file's baseline, the
    total may not rise even via a brand-new file entry (H2_ICON_TOTAL — the
    companion LITERAL_TOTAL exists for in tests/test_design_tokens.py, for
    the same reason: a per-file ceiling alone still permits adding one new
    entry to the dict), and the baseline must come down when the real count
    does. The `seen >= 28` guard is a positive control — a scan finding
    fewer than that means the pattern itself has drifted, not that the tree
    improved. (Lowered from 40 by WP-6 step 15: every settings-family H2
    icon this step converted now renders violet via card()'s own H2_ICON
    constant, a macro-generated `<h2><i>` pair that is invisible to this
    file's source-text scan the same way it is to test_design_headings.py's
    HEADING_BASELINE -- see H2_ICON_BASELINE's own comment.)"""
    counts = _non_compliant_heading_icons("2", "text-brand")
    seen = sum(counts.values())
    assert seen >= 28, f"only {seen} <h2><i> pairs matched — the pattern has drifted"

    grew = {f: (H2_ICON_BASELINE.get(f, 0), n)
            for f, n in counts.items() if n > H2_ICON_BASELINE.get(f, 0)}
    assert not grew, (
        "non-violet H2 icons increased — convert via the heading ladder "
        "(§2.1), not by adding one more:\n  "
        + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in grew.items()))

    assert seen <= H2_ICON_TOTAL, (
        f"total non-violet H2 icons rose to {seen} (was {H2_ICON_TOTAL}) — a new "
        "H2_ICON_BASELINE entry redistributes existing debt, it does not add to it")
    assert seen == H2_ICON_TOTAL, (
        f"total fell to {seen}; lower H2_ICON_TOTAL to match, or the headroom "
        "just won is silently available to spend again")

    stale = {f: (b, counts.get(f, 0))
             for f, b in H2_ICON_BASELINE.items() if counts.get(f, 0) < b}
    assert not stale, (
        "these files hold FEWER non-violet H2 icons than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in stale.items()))


def test_every_h3_icon_is_muted():
    """Ratchet twin of the H2 test above, against text-muted."""
    counts = _non_compliant_heading_icons("3", "text-muted")
    seen = sum(counts.values())

    grew = {f: (H3_ICON_BASELINE.get(f, 0), n)
            for f, n in counts.items() if n > H3_ICON_BASELINE.get(f, 0)}
    assert not grew, (
        "non-muted H3 icons increased — convert via the heading ladder "
        "(§2.1), not by adding one more:\n  "
        + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in grew.items()))

    assert seen <= H3_ICON_TOTAL, (
        f"total non-muted H3 icons rose to {seen} (was {H3_ICON_TOTAL}) — a new "
        "H3_ICON_BASELINE entry redistributes existing debt, it does not add to it")
    assert seen == H3_ICON_TOTAL, (
        f"total fell to {seen}; lower H3_ICON_TOTAL to match, or the headroom "
        "just won is silently available to spend again")

    stale = {f: (b, counts.get(f, 0))
             for f, b in H3_ICON_BASELINE.items() if counts.get(f, 0) < b}
    assert not stale, (
        "these files hold FEWER non-muted H3 icons than the baseline; "
        "lower it:\n  " + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in stale.items()))


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
    assert _HEADING_ICON.search(sample), "the heading-icon scan cannot see a digit"
    assert mbr._ICON.search(sample), "the migrator cannot see a digit in a name"
    assert mbr._HEADING_ICON.search(sample), (
        "the migrator's heading-icon scan cannot see a digit")

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
    # 470 -> 459 with WP-6 step 12: 11 page-title icons (one per page,
    # `<h1><i data-lucide="LITERAL">`) disappeared from the templates this
    # scan reads, not because they were deleted, but because they moved
    # BEHIND base.html's single shared H1, whose icon name is
    # `{% block page_icon %}...{% endblock %}` — a Jinja expression, not a
    # literal — because every page supplies its own by overriding that
    # block. No single template's source shows the resolved name any more,
    # so this scan (deliberately unchanged: it is verifying the ICON-NAME
    # character class, not the heading ladder) can no longer count them.
    # test_the_one_page_title_icon_is_violet is what checks base.html's one
    # remaining heading icon now, via a pattern built for that specific
    # shape (_TOPBAR_H1_ICON).
    #
    # 459 -> 433 with WP-6 step 15 (Batch B, the Settings family): every
    # icon on a heading this step converted to card()/subhead() is now
    # passed as an `icon='name'` MACRO ARGUMENT (settings.html and nine
    # partials), not literal `<i data-lucide="name" class="...">` markup in
    # the template this scan reads -- the same "moved behind a macro"
    # effect the step-12 comment above already documents for the page-title
    # icon, one card family later. `settings-2` itself stays findable
    # (servers.html:54, services.html:31, both untouched by this step), so
    # `digits >= 11` above is unaffected; only the raw SITE count drops.
    assert sites >= 433, f"only {sites} icon sites scanned — measured 433 post-WP-6-step-15"


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
