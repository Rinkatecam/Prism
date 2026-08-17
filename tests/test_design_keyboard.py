"""Guardrails for keyboard operability of non-native controls.

Across the templates there were 29 elements that were clickable and nothing
else — `<div data-action="…">`, `<tr class="cursor-pointer">`, a `<span>`
tag pill — and NOT ONE carried a tabindex or a role. Every disclosure, list
picker and clickable row in the application was mouse-only: WCAG 2.1.1
(Keyboard) and 4.1.2 (Name, Role, Value).

The fix is one keyboard bridge in `base.html` keyed on `[data-action]
[tabindex]`, so an element becomes operable by the same dispatch that
already handles its click. That shape was chosen because the failure this
repository keeps producing is a rule that reaches only some of its carriers:
here you cannot make an element focusable and forget to make it work.

WHAT THESE ARE BLIND TO:

  * Behaviour. They read markup. That a native `<button data-action>` does
    NOT fire its action twice on Enter was measured in the browser (0 extra
    invocations via the bridge, exactly 1 on click) — a double-fire would be
    harmless on a filter and destructive on a delete confirm.
  * Whether an `aria-controls` id resolves at runtime. Most are built inside
    JS template literals, so no static scan can follow them. Measured
    instead: 37 carriers on the server-detail page, zero unresolved.
  * Whether the focus ring is visible. The global ring at the end of
    app.css matches `[tabindex]:focus-visible`, so these were ringed the
    moment they became focusable; confirmed on real carriers at
    `outline: solid 2px rgb(124 58 237)`.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
STATIC_JS = PROJECT_ROOT / "static" / "js"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
# Whole-line `//` comments only — deliberately NOT `//` anywhere on a line.
# A mid-line rule would eat the `//` in every `https://` URL and inside
# regex literals, and OPS-LEARNINGS §2.5 records a scan that desynchronised
# on exactly this kind of shortcut and silently hid 158 class strings.
# Anchored to the line start, the worst case is that a trailing comment
# survives, which over-reports rather than under-reports.
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    """Blank comments out, keeping line numbers true.

    Both `//` and `/* */` matter here: the first version stripped only block
    and markup comments, so the `[onclick]` test failed on the prose in
    `toggleLoginCard` explaining the `[onclick]` bug it exists to prevent.
    A check that cannot tell code from commentary fires on its own
    explanation, and the cheapest way to silence it is to delete the
    explanation."""
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


# Elements that are clickable but not natively interactive. Matches markup and
# JS template literals alike — most of these are built from strings.
_ELEMENT = re.compile(r"<(div|span|li|tr|td|section|article)\b[^>]*?>", re.S)
_ACTION = re.compile(r"data-action=[\"'`]?\$?\{?([A-Za-z_][\w-]*)")


def _clickable_non_native() -> list[tuple[str, int, str, str]]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(path.read_text(encoding="utf-8"))
        for m in _ELEMENT.finditer(text):
            tag = m.group(0)
            action = _ACTION.search(tag)
            if not (action or "cursor-pointer" in tag or "onclick=" in tag):
                continue
            line = text[:m.start()].count("\n") + 1
            found.append((path.relative_to(TEMPLATES).as_posix(), line,
                          action.group(1) if action else "", re.sub(r"\s+", " ", tag)))
    return found


def _needs_keyboard(action: str, tag: str) -> bool:
    """A clickable non-native element needs a tab stop UNLESS it is one of the
    two shapes where a tab stop would be wrong.

    A modal backdrop is a click-outside-to-close affordance layered over the
    page; its keyboard equivalent is Escape, and putting it in the tab order
    would insert a full-screen control between the user and the dialog. An
    element carrying `stop-prop` is an event guard around content, not a
    control at all."""
    if 'aria-hidden="true"' in tag:
        return False
    if action in ("stop-prop", "modal-backdrop", "close-mobile-sidebar"):
        return False
    backdrop = ("inset-0" in tag) and (action.startswith("close") or "backdrop" in action)
    return not backdrop


def test_every_clickable_non_native_element_is_keyboard_operable():
    """The coverage guard. Without it this regresses the moment somebody adds
    the next `<div data-action="…">`, which is how all 29 got there."""
    missing = [f"{f}:{ln}  {action or '(cursor-pointer only)'}  {tag[:100]}"
               for f, ln, action, tag in _clickable_non_native()
               if _needs_keyboard(action, tag) and "tabindex" not in tag]
    assert not missing, (
        "clickable, not natively interactive, and not reachable by keyboard "
        "— a mouse-only control:\n  " + "\n  ".join(missing))


def test_the_detector_matches_a_real_historical_offender():
    """A guardrail that finds nothing is indistinguishable from a guardrail
    whose pattern is wrong (OPS-LEARNINGS §2.2 #15). This is the exact markup
    that was in `server_detail.html` before the fix — a login card that could
    not be reached or operated from a keyboard."""
    offender = ('<div class="rounded-lg border-l-4 border border-line bg-card '
                'hover:shadow-md transition-all cursor-pointer break-inside-avoid" '
                'style="border-left-color: red" data-action="_sdToggleLoginCard">')
    m = _ELEMENT.search(offender)
    assert m, "the element pattern no longer matches the markup it was written for"
    action = _ACTION.search(offender)
    assert action and action.group(1) == "_sdToggleLoginCard"
    assert _needs_keyboard(action.group(1), offender), (
        "the exemption rule now excuses the original offender")
    assert "tabindex" not in offender


def test_the_exemption_rule_still_excuses_a_modal_backdrop():
    """The negative control for the same rule. A backdrop must stay OUT of the
    tab order; if this starts failing, the exemption has been narrowed into
    uselessness and the test above will demand tab stops on full-screen
    overlays."""
    backdrop = '<div class="fixed inset-0 bg-black/50" data-action="closeModal">'
    assert not _needs_keyboard("closeModal", backdrop)


def test_the_keyboard_bridge_exists_and_skips_native_controls():
    """A `<button data-action>` already fires a click on Enter and Space.
    Handling the keydown as well runs the action twice — cosmetic on a
    filter, not on a delete confirm."""
    base = _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))
    assert re.search(r"addEventListener\(\s*'keydown'", base), "no keyboard bridge"
    bridge = base[base.index("addEventListener('keydown'"):]
    assert "'Enter'" in bridge[:600] and ("' '" in bridge[:600] or "'Spacebar'" in bridge[:600]), (
        "the bridge does not handle Enter and Space")
    assert "NATIVE" in bridge[:900], (
        "nothing excludes natively-interactive targets, so a <button "
        "data-action> will run its action twice on Enter")
    assert re.search(r"NATIVE\s*=\s*'[^']*\bbutton\b[^']*a\[href\]", base), (
        "the native-element list must cover at least button and a[href]")
    assert "preventDefault" in bridge[:900], (
        "Space will scroll the page and Enter will submit the enclosing form")


def test_every_expanded_carrier_declares_what_it_controls():
    """`aria-expanded` is maintained centrally by re-reading the element named
    in `aria-controls`. Without that attribute nothing updates it, and the
    control permanently announces itself as collapsed — worse than silence,
    because it is confidently wrong."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(path.read_text(encoding="utf-8"))
        for m in _ELEMENT.finditer(text):
            tag = m.group(0)
            if "aria-expanded" in tag and "aria-controls" not in tag:
                offenders.append(f"{path.relative_to(TEMPLATES).as_posix()}:"
                                 f"{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "`aria-expanded` with no `aria-controls` — nothing can keep it "
        "honest:\n  " + "\n  ".join(offenders))


def test_nothing_looks_for_an_onclick_attribute_any_more():
    """The CSP hardening replaced every inline `onclick` with `data-action`.
    Code left behind looking for `[onclick]` finds null and throws.

    That is not hypothetical: `toggleLoginCard` ran
    `d.closest('[onclick]').classList.remove(…)` and threw
    "Cannot read properties of null" on the first iteration, so the failed-
    login cards did not expand for ANYONE — measured against the running
    app, not inferred. It looked like an accessibility gap and was a dead
    feature."""
    offenders = []
    sources = list(TEMPLATES.rglob("*.html")) + list(STATIC_JS.glob("*.js"))
    for path in sources:
        text = _code_only(path.read_text(encoding="utf-8"))
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"""(closest|querySelector(All)?|matches)\(\s*['"`][^'"`]*\[onclick\]""", line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{i}")
    assert not offenders, (
        "selecting on `[onclick]`, which the CSP migration removed — this "
        "resolves to null and throws:\n  " + "\n  ".join(offenders))
