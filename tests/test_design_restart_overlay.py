"""The restart overlay (WP-3, owner-flagged verification item).

WHAT IT IS. A full-cover panel over the content area while a server reboots, and
the sanctioned CIRCLE pattern rather than the skeleton one: an ACTION is in
flight and there is no content shape to ghost.

THREE DEFECTS, one of them not on the flagged list:

  * **Nineteen raw hex colours** in inline styles and JS assignments, every one
    picked from the dark palette — so the overlay was a dark panel with light
    text in BOTH themes. The project's colour-literal ratchet could not see a
    single one: that scanner matches Tailwind arbitrary-value utilities
    (`text-[#3B82F6]`), and these were `style="color:#F8FAFC"` and
    `el.style.background = '#10B981'`. A ratchet that cannot see a whole
    category of literal is not protecting that category.
  * **The panel was built with innerHTML** and a template literal splicing the
    server's name into markup. A server name is operator-supplied text arriving
    through a database; it belongs in textContent. The SVGs still go in as
    markup, and that is the distinction rather than an inconsistency: they are
    developer-authored constants with nothing interpolated.
  * **All eighteen of its strings were missing from every locale.** Each was
    written `t.get(key, 'English text')`, so a German, French, Spanish or
    Japanese operator read English and always had. `t.get` never fails, so no
    test could see it — which makes a fallback the most effective way to hide an
    untranslated string that exists.

REDUCED MOTION, and why the spinner is allowed to keep spinning. The doctrine
this project settled on: a SKELETON claims content is coming; a CIRCLE claims an
ACTION is in flight. A frozen spinner asserts something false about a request
still running, so `.animate-spin` has a sanctioned carve-out (slowed, not
stopped) — and this overlay's spinner is exactly the case that carve-out exists
for. The dots are different: they carry information in their COLOUR, the
transition merely eases it, and the global rule freezing that transition is
correct because the colour still changes, instantly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEMPLATE = PROJECT_ROOT / "templates" / "server_detail.html"
CSS = PROJECT_ROOT / "static" / "css" / "app.css"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)

_PHASES = ("rebooting", "stabilising", "ready", "timeout")
_DOT_STATES = ("met", "waiting", "pending", "failed")

_KEYS = (
    "server_restarting_title", "restart_overlay_waiting",
    "restart_overlay_to_respond", "restart_overlay_check",
    "restart_overlay_title", "restart_overlay_rebooting",
    "restart_overlay_stabilising", "restart_overlay_stabilising_generic",
    "restart_overlay_stage2", "restart_overlay_pending_reboot",
    "restart_overlay_warming_up", "restart_overlay_settling",
    "server_back_online", "restart_overlay_reloading",
    "server_not_responding", "restart_overlay_investigate",
    "dismiss", "retry",
)


def _code_only(text: str) -> str:
    """Blank comments, keeping line numbers true.

    Load-bearing here for the same reason it is everywhere else in this suite:
    the overlay's own source explains which hex colours were REMOVED, and quotes
    one of them. A check that cannot tell code from commentary fires on its own
    rationale, and the cheapest way to make it green is to delete the rationale.
    """
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _tpl() -> str:
    return _code_only(TEMPLATE.read_text(encoding="utf-8"))


def _css() -> str:
    return _code_only(CSS.read_text(encoding="utf-8"))


def _overlay_source() -> str:
    """Just the overlay's own code, so a failure names the overlay.

    Both markers are CODE. The end marker was first written as the text of the
    section comment that follows the overlay — which `_code_only` blanks, so the
    slice raised instead of asserting. Anchoring a slice on prose in a file
    whose comments you are deliberately erasing is a contradiction; that is the
    same mistake as anchoring an assertion on a comment, arriving one step
    earlier.
    """
    tpl = _tpl()
    start = tpl.index("const RESTART_LABELS = {")
    end = tpl.index("function _sdLoadChart()")
    return tpl[start:end]


# ── the colours left the template ─────────────────────────────────────────

def test_the_overlay_carries_no_raw_colour_literal():
    """The flagged defect. Nineteen of them, all from the dark palette, in a
    panel that renders in both themes."""
    found = re.findall(r"#[0-9A-Fa-f]{3,8}\b", _overlay_source())
    assert not found, f"raw colour literals back in the overlay: {found}"


def test_the_overlay_sets_no_colour_from_javascript():
    """`el.style.background = '#10B981'` is the same defect wearing a different
    syntax, and it is the form the ratchet is least able to see."""
    src = _overlay_source()
    offenders = re.findall(r"style\.(?:background|backgroundColor|color)\s*=", src)
    assert not offenders, (
        "the overlay assigns a colour from script; phase and dot state are "
        "carried by classes so the token mapping stays in one place")


def test_every_phase_has_a_rule_and_an_accent():
    """Driven off the phase list rather than a literal, so a fifth phase fails
    this the moment it exists instead of inheriting whatever the last one set."""
    css = _css()
    for phase in _PHASES:
        m = re.search(rf"\.restart-overlay--{phase}\s*\{{([^}}]*)\}}", css)
        assert m, f"no .restart-overlay--{phase} rule"
        body = m.group(1)
        assert "--restart-accent" in body, (
            f"the {phase} phase does not set the accent, so its title, icon and "
            "buttons inherit the previous phase's colour")
        # And the accent has to BE a token. Asserting only that the variable is
        # set let a mutation swap `var(--c-warning)` for a raw `245 158 11` and
        # pass — the whole defect this file exists for, reintroduced through the
        # one property that was supposed to prevent it.
        assert re.search(r"--restart-accent:\s*var\(--c-[a-z-]+\)", body), (
            f"the {phase} phase's accent is not a token reference")


def test_the_accent_is_read_rather_than_repeated():
    """One variable, several consumers. The version this replaced assigned a hex
    to the title, the icon stroke, the dots and the buttons separately — four
    chances for a phase to disagree with itself."""
    css = _css()
    for selector in (".restart-overlay-icon",):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert m and "--restart-accent" in m.group(1), (
            f"{selector} does not read the phase accent")


def test_every_dot_state_maps_to_a_token():
    css = _css()
    for state in _DOT_STATES:
        m = re.search(rf"\.restart-dot--{state}\s*\{{([^}}]*)\}}", css)
        assert m, f"no .restart-dot--{state} rule"
        assert "var(--c-" in m.group(1), (
            f"the {state} dot is not painted from a token")


def test_the_scrim_and_the_ink_are_both_theme_aware():
    """The whole point of the fix. The old overlay was a dark scrim with light
    text in light mode too — self-consistent, and wrong in one of the two
    themes it shipped in."""
    body = re.search(r"\.restart-overlay\s*\{([^}]*)\}", _css()).group(1)
    assert "var(--c-page)" in body, (
        "the scrim is not painted from a theme-aware token")
    title = re.search(r"\.restart-overlay-title\s*\{([^}]*)\}", _css()).group(1)
    assert "var(--c-ink)" in title


def test_the_primary_button_does_not_go_white_on_pale_violet_in_dark_mode():
    """The brand token lifts to a pale violet in dark mode, so white-on-brand
    inverts to unreadable. Two other controls in this stylesheet already hit
    that and carry the same correction; a third that forgot it would be a
    contrast bug reachable only from a timed-out restart."""
    css = _css()
    m = re.search(r"\.dark\s+\.restart-overlay-btn--primary\s*\{([^}]*)\}", css)
    assert m, "no dark-mode rule on the primary button"
    # `(?<![-\w])` because `border-color:` and `background-color:` both CONTAIN
    # the substring `color:`. Without it, a mutation that replaced the text
    # colour with a border colour satisfied this test while leaving white ink on
    # pale violet — the exact contrast bug it was written to prevent.
    assert re.search(r"(?<![-\w])color:\s*rgb\(var\(--c-", m.group(1)), (
        "the dark-mode rule does not set a TEXT colour from a token; white ink "
        "on the pale-violet brand fill is unreadable")


# ── the server's name is text, not markup ─────────────────────────────────

def test_the_server_name_goes_in_as_text():
    src = _overlay_source()
    assert "textContent" in src
    assert not re.search(r"innerHTML\s*=\s*[`'\"][^`'\"]*\$\{", src), (
        "a template literal is interpolated into innerHTML; the only thing "
        "interpolated here is a server name, which is operator-supplied")


def test_the_only_markup_assignments_are_the_constant_icons():
    """innerHTML is not banned outright — the SVGs are developer-authored
    constants and building them with createElementNS would be noise. What is
    banned is markup that carries a value from outside."""
    src = _overlay_source()
    for m in re.finditer(r"innerHTML\s*=\s*([^;]+);", src):
        rhs = m.group(1).strip()
        assert rhs.startswith("RESTART_ICONS"), (
            f"innerHTML assigned something other than a constant icon: {rhs[:60]}")


def test_the_stabilising_message_is_not_markup():
    """It interpolates a number today and is one edit from interpolating
    something a host controls."""
    src = _overlay_source()
    assert not re.search(r"msgEl\.innerHTML", src), (
        "the message is written as markup; it is assembled from payload values")


# ── it is anchored to the layout, not to a magic number ───────────────────

def test_the_overlay_is_not_pinned_to_a_hardcoded_sidebar_width():
    src = _overlay_source()
    assert "14rem" not in src, (
        "the overlay's left edge is a hardcoded sidebar width again; it "
        "misaligns the moment the sidebar collapses")
    assert "getBoundingClientRect" in src, (
        "the overlay does not measure the content box it is supposed to cover")


def test_the_default_left_edge_covers_more_rather_than_less():
    """If the measurement fails, the overlay should cover the whole viewport
    rather than leave a strip of live UI beside a panel claiming the page is
    busy."""
    body = re.search(r"\.restart-overlay\s*\{([^}]*)\}", _css()).group(1)
    m = re.search(r"left:\s*([^;]+);", body)
    assert m and m.group(1).strip() == "0", (
        "the CSS default for `left` is not 0, so a failed measurement leaves "
        "part of the page uncovered")


# ── it announces itself ───────────────────────────────────────────────────

def test_the_overlay_announces_its_phase():
    """The phases ARE the progress. A panel that changes silently every twenty
    seconds tells a screen-reader user nothing at all."""
    src = _overlay_source()
    assert "'role', 'status'" in src or '"role", "status"' in src
    assert "aria-live" in src


def test_the_actions_are_real_buttons():
    src = _overlay_source()
    assert src.count("createElement('button')") >= 2, (
        "the dismiss and retry controls are not buttons")
    assert "type = 'button'" in src, (
        "a button with no type defaults to submit in some browsers")


# ── the translations that did not exist ───────────────────────────────────

def test_every_overlay_string_exists_in_every_locale():
    """All eighteen were missing from all five locales. `t.get(key, 'English')`
    rendered English everywhere and no test could tell, because the fallback
    made an untranslated string look exactly like a translated one."""
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in _KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, ("untranslated restart-overlay strings: "
                         + ", ".join(missing))


def test_no_locale_silently_reuses_the_english_text():
    """A key that exists with the English string in it is the same defect with
    an extra step. Checked on the two longest sentences, where a coincidental
    match cannot happen."""
    from i18n import TRANSLATIONS
    for key in ("restart_overlay_rebooting", "restart_overlay_investigate"):
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, (
                f"{lang}:{key} is the English string")


def test_the_labels_are_defined_once_for_the_script():
    """One object handed to the script, rather than a Jinja call inlined at each
    use. The version this replaced had `{{ t.get(...) }}` inside JS string
    literals in eleven places, which is eleven chances to quote it wrong."""
    src = _overlay_source()
    assert "RESTART_LABELS" in src
    inline = re.findall(r"\{\{\s*t\.get\(", src)
    assert len(inline) <= len(_KEYS), (
        "more Jinja translation calls than there are strings; some are inlined "
        "at their use site rather than coming from RESTART_LABELS")


def test_the_timeout_message_does_not_state_a_wrong_number():
    """It said "did not come back after 4 checks" while the poller runs 30. A
    number in a message is a claim, and this one had been wrong for as long as
    it existed."""
    from i18n import TRANSLATIONS
    text = TRANSLATIONS["en"]["restart_overlay_investigate"]
    assert not re.search(r"\b4 checks\b", text)
    src = _overlay_source()
    m = re.search(r"const maxAttempts = (\d+);", src)
    assert m, "the attempt cap is no longer named in the overlay"
    # If the message ever states a number again, it must be THAT number.
    for n in re.findall(r"\b(\d+)\b", text):
        assert n == m.group(1), (
            f"the timeout message states {n} while the poller runs "
            f"{m.group(1)} checks")


# ── reduced motion ────────────────────────────────────────────────────────

def test_the_spinner_keeps_the_sanctioned_carve_out():
    """A frozen spinner asserts something false about a request still in
    flight, which is why `.animate-spin` has an exemption. This overlay is the
    case that exemption exists for: an action IS in flight, for minutes."""
    src = _overlay_source()
    assert "animate-spin" in src, (
        "the rebooting icon no longer carries the class the reduced-motion "
        "carve-out is spelled by, so it will freeze mid-rotation")
    css = _css()
    block = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{", css)
    i, depth = block.end(), 1
    while i < len(css) and depth:
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    body = css[block.end():i]
    assert re.search(r"\.animate-spin\s*\{[^}]*animation-duration:\s*2\.25s", body), (
        "the spinner carve-out is gone, so the overlay's spinner freezes")


def test_only_the_rebooting_icon_spins():
    """Stabilising, ready and timeout are states, not activity. A spinner on a
    finished state is the same false claim in the other direction."""
    src = _overlay_source()
    icons = re.search(r"const RESTART_ICONS = \{(.*?)\n  \};", src, re.S)
    assert icons, "could not isolate RESTART_ICONS"
    for phase in ("stabilising", "ready", "timeout"):
        entry = re.search(rf"{phase}:\s*'([^']*)'", icons.group(1))
        assert entry, f"no icon for {phase}"
        assert "animate-spin" not in entry.group(1), (
            f"the {phase} icon spins, claiming activity where there is a state")


def test_the_dots_carry_colour_rather_than_motion():
    """Their transition is frozen by the global rule and that is correct: the
    information is the colour, and it still changes — instantly."""
    body = re.search(r"\.restart-dot\s*\{([^}]*)\}", _css()).group(1)
    assert "background" in body
    assert "animation" not in body, (
        "the dots animate rather than transition; an animation here would need "
        "its own reduced-motion argument and does not have one")
