"""Guardrails for "a disabled control must say WHY" — Wave 3, task G3.

`app.css` gives disabled controls `cursor: not-allowed` to say THAT they are
unavailable and reserves `data-tip-title`/`data-tip-desc` for the reason.
Nothing supplied the reasons: 8 markup-disabled controls and 23 places
setting `.disabled` from JS, none with one.

Not every disable needs prose, and the distinction is the same one the owner
drew for the lifecycle badges — the machine working versus the system
waiting on you:

  * MACHINE WORKING. A button disabled for the duration of a fetch, next to
    a "Testing connection…" / "Sending test email…" / "Recalculating…"
    message. Transient and already explained; a tooltip about a sub-second
    state is noise. Left alone deliberately.
  * WAITING ON YOU. Confirm-word boxes, "pick at least two servers", the
    comparison cap. These are dead ends without a reason, and they are the
    ones converted to `prismSetDisabled`.

WHAT THESE ARE BLIND TO:

  * Whether a reason is TRUE. Nothing here can tell that "Pick at least two
    servers" is the actual enabling condition.
  * Whether the tooltip is reachable without a mouse. It is not — the
    mechanism is hover-only, so these reasons are invisible to keyboard and
    screen-reader users. That is a real gap, inherited from the tooltip
    component, and it belongs to the tooltip-coverage session scoped in
    DESIGN_PHASE_3_SCOPE.md §1, not to this one.
  * That a real pointer reaches a disabled <button> at all. Measured in the
    browser instead: mouseover and mouseenter both fire and the tip renders.
    The `pointer-events` test below is what keeps that true.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


# A `>` inside a `${…}` interpolation ends the tag match early. That hid the
# pagination NEXT button (`${currentPage >= totalPages ? 'disabled'`) while
# finding PREV (`<= 1`) — the two differ only in which comparison operator
# they use. Blank the angle brackets inside interpolations, preserving length
# so line numbers and the `disabled` inside them survive.
_INTERP = re.compile(r"\$\{[^{}]*\}")


def _defuse(text: str) -> str:
    return _INTERP.sub(lambda m: m.group(0).replace(">", " ").replace("<", " "), text)


_TAG = re.compile(r"<(button|input|select|textarea|fieldset)\b[^>]*?>", re.S)
# The attribute, not Tailwind's `disabled:` variant and not `data-disabled`.
_DISABLED_ATTR = re.compile(r"(?<![\w-])disabled(?![\w:-])")


def _markup_disabled() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _defuse(_code_only(path.read_text(encoding="utf-8")))
        for m in _TAG.finditer(text):
            tag = m.group(0)
            if not _DISABLED_ATTR.search(tag):
                continue
            found.append((path.relative_to(TEMPLATES).as_posix(),
                          text[:m.start()].count("\n") + 1,
                          re.sub(r"\s+", " ", tag)))
    return found


def test_every_markup_disabled_control_says_why():
    controls = _markup_disabled()
    assert controls, "no markup-disabled controls found; this scan has stopped matching"
    missing = [f"{f}:{ln}  {tag[:100]}" for f, ln, tag in controls
               if "data-tip-title" not in tag]
    assert not missing, (
        "disabled with no reason — `cursor: not-allowed` says THAT it is "
        "unavailable and nothing says why:\n  " + "\n  ".join(missing))


def test_the_scan_sees_past_a_template_interpolation():
    """Positive control for the blind spot that actually bit. The PREV and
    NEXT pagination buttons are the same markup differing only in `<=` vs
    `>=`; without `_defuse` the scan found one and silently skipped the
    other, so "every control has a reason" was true of the half it could
    see."""
    # The ORIGINAL form, before the fix. `>=` inside the interpolation is the
    # whole bug — writing this sample with the repaired `${atLast ? …}` made
    # the positive control pass for the wrong reason, since there is then no
    # `>` for the naive regex to trip on.
    sample = ('html += `<button data-action="${onPageChange}" '
              'data-args="[${currentPage + 1}]" '
              "${currentPage >= totalPages ? 'disabled' : ''}\n  class=\"x\">`")
    raw_hit = bool(_DISABLED_ATTR.search(_TAG.search(sample).group(0))) \
        if _TAG.search(sample) else False
    defused = _TAG.search(_defuse(sample))
    assert defused is not None
    assert _DISABLED_ATTR.search(defused.group(0)), (
        "the interpolation defusing no longer works; disabled controls "
        "inside template literals will go unseen")
    assert not raw_hit, (
        "this positive control no longer demonstrates the bug it was written "
        "for — the naive scan now finds it, so the guard proves nothing")


def test_a_tailwind_disabled_variant_is_not_mistaken_for_the_attribute():
    """Negative control. `disabled:opacity-50` is a STYLE for the disabled
    state, not a disabled control; counting it demanded a reason on buttons
    that are never disabled (rbac.html's grant button was the false positive
    that showed this)."""
    styled_only = '<button class="rounded bg-accent disabled:opacity-50">Grant</button>'
    m = _TAG.search(styled_only)
    assert m and not _DISABLED_ATTR.search(m.group(0))


def test_pointer_events_are_never_removed_from_disabled_controls():
    """The whole mechanism rests on a disabled control still receiving hover.
    Measured: it does, and `pointer-events` computes to `auto`. A single
    `pointer-events: none` on `[disabled]` — a common reflex, and one that
    looks like a tidy-up — would silently remove every reason in the app
    while leaving all the markup in place and every other test green."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    for m in re.finditer(r"([^{}]*)\{([^}]*)\}", css):
        selector, body = m.group(1), m.group(2)
        if "disabled" not in selector:
            continue
        pe = re.search(r"pointer-events\s*:\s*([\w-]+)", body)
        if pe and pe.group(1) == "none":
            raise AssertionError(
                "`pointer-events: none` on a disabled selector — the reason "
                f"tooltip can never fire again:\n  {selector.strip()[:120]}")


def test_the_helper_clears_the_reason_when_it_enables():
    """A stale reason is worse than none: it explains a condition that no
    longer holds, on a control the user can now press. Setting `.disabled`
    and the reason in one call is what stops the two drifting."""
    base = _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))
    assert "window.prismSetDisabled" in base, "the helper is gone"
    body = base[base.index("window.prismSetDisabled"):]
    body = body[:body.index("\n      };") + 10]
    assert re.search(r"if\s*\(\s*!\s*title\s*\)", body), (
        "no enable branch — a falsy reason must enable the control")
    for attr in ("data-tip-title", "data-tip-desc", "aria-disabled"):
        assert f"removeAttribute('{attr}')" in body, (
            f"the enable branch does not clear {attr}, so it goes stale")
    assert "el.disabled = false" in body, "the enable branch never enables"


def test_a_confirm_control_is_not_also_toggled_by_a_bare_assignment():
    """A control whose reason lives in markup and whose state is flipped by a
    plain `.disabled =` keeps the reason after it is enabled. Catches the
    exact drift `prismSetDisabled` exists to prevent."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _defuse(_code_only(path.read_text(encoding="utf-8")))
        for m in _TAG.finditer(text):
            tag = m.group(0)
            if "data-tip-title" not in tag or not _DISABLED_ATTR.search(tag):
                continue
            el_id = re.search(r'id="([^"]+)"', tag)
            if not el_id:
                continue
            bare = re.search(rf"""getElementById\(\s*['"]{re.escape(el_id.group(1))}['"]\s*\)\s*\.disabled\s*=""", text)
            if bare:
                offenders.append(f"{path.relative_to(TEMPLATES).as_posix()}  #{el_id.group(1)}")
    assert not offenders, (
        "a control carrying a reason in markup is toggled by a bare "
        "`.disabled =`; use prismSetDisabled so the reason clears too:\n  "
        + "\n  ".join(offenders))


def test_the_tooltip_body_is_not_assembled_as_markup():
    """Reasons carry interpolated data — `data-tip-desc="{{ _reason }}"` on
    the server card comes from the fusion layer — and Jinja's escaping is
    undone by `getAttribute`. Building the panel with innerHTML would make
    every reason an injection point."""
    base = _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))
    show = base[base.index("function show(e)"):]
    show = show[:show.index("function hide()")]
    assert "innerHTML" not in show, (
        "the tooltip body is assembled with innerHTML again; a reason "
        "containing markup would execute")
    assert show.count("textContent") >= 2, "the tooltip no longer sets text safely"
