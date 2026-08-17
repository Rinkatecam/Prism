"""Guardrails for the empty-state rule — Wave 3, task G1.

DESIGN_PHASE_3_SCOPE.md §4 states the rule in three parts: a faded ICON of
the thing that is missing, WHAT IS ABSENT in the user's words, and WHAT TO
DO ABOUT IT. It also names the part that gets dropped — the third — and it
was right: `activity_feed.html` said "no events" and stopped, and eleven
other places hand-rolled a `<div>` with a sentence in it.

The rule now has exactly two renderers, and both require the hint:

  * `partials/_empty_state.html` -> `empty_state(icon, message, hint)`
  * `base.html` -> `window.prismEmptyState(icon, message, hint, opts)`

`server_detail.html` previously had a third, private copy whose hint was
optional. All 14 of its callers happened to pass one, which is precisely the
condition under which a hole goes unnoticed — the next caller is where
coverage rots. It now delegates to the shared helper.

WHAT THESE ARE BLIND TO:

  * Whether a hint is USEFUL. "No data" / "There is no data" would pass
    every check here. The one mechanical proxy — a hint that merely repeats
    the message — is asserted; judgement is not automatable.
  * Zero states. "0 critical" is a RESULT and must stay in the normal
    layout; nothing here can tell a result from an absence, so a zero state
    wrongly converted would pass. Reviewed by hand: two candidates were
    rejected on those grounds — a "No backup yet" status dot in
    operations.html and a `<option>No category</option>` in workflows.html.
  * Error states. A failed request is not an empty collection. One was
    found masquerading as an empty state and is asserted against below.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _split_args(s: str) -> list[str]:
    """Split a call's arguments on top-level commas, respecting quotes."""
    out, depth, cur, quote = [], 0, "", None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote, cur = ch, cur + ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                out.append(cur)
                return out
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return out


# All three spellings. `_renderEmptyState` is server_detail.html's thin
# delegate and its 14 call sites are real empty states — the first version of
# this regex matched only the other two names, so the largest single
# population of empty states in the app was outside every check here.
_CALL = re.compile(r"(?:window\.)?prismEmptyState\(|_renderEmptyState\(|empty_state\(")
_EMPTYISH = {"", "''", '""', "``", "null", "undefined", "none", "None"}


def _hint_of(args: list[str]) -> str | None:
    """The hint argument, or None if it was never supplied.

    Positional counting alone is not enough. Dropping the hint from
    `empty_state('clock', msg, hint, card=true)` leaves `card=true` sitting
    in the hint's slot, which reads as a perfectly good non-empty argument —
    the check went green over an empty state with two parts. A keyword
    argument in that position means the positional hint was skipped."""
    for a in args[1:]:
        if re.match(r"^hint\s*=(?!=)", a):
            return a.split("=", 1)[1].strip()
    if len(args) < 3:
        return None
    third = args[2]
    if re.match(r"^\w+\s*=(?!=)", third):
        return None
    return third


def _calls() -> list[tuple[str, int, list[str]]]:
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(path.read_text(encoding="utf-8"))
        for m in _CALL.finditer(text):
            # Skip the macro DEFINITION only. The first version also skipped
            # anything whose preceding character was `=`, meaning to catch
            # `window.prismEmptyState = function` — but that form never
            # matches this regex anyway (there is ` = function ` between the
            # name and the paren), and the rule silently swallowed every
            # `el.innerHTML = prismEmptyState(…)` instead. Nine of eleven
            # call sites vanished and all three coverage tests went green
            # over an almost-empty list. Found by mutation.
            head = text[max(0, m.start() - 40):m.start()]
            if "macro" in head:
                continue
            args = [a.strip() for a in _split_args(text[m.end():m.end() + 1500])]
            found.append((path.relative_to(TEMPLATES).as_posix(),
                          text[:m.start()].count("\n") + 1, args))
    return found


def test_every_empty_state_supplies_all_three_parts():
    """The icon and the message are structural — the renderers always emit
    them. The hint is the argument a caller can omit, so it is the one worth
    checking at every call site."""
    calls = _calls()
    assert calls, "no empty-state calls found; this scan has stopped matching"
    missing = [f"{f}:{ln}  {args[1][:60] if len(args) > 1 else '?'}"
               for f, ln, args in calls
               if (_hint_of(args) or "") in _EMPTYISH]
    assert not missing, (
        "empty state with no hint — it says what is absent and not what to "
        "do about it, which is a dead end:\n  " + "\n  ".join(missing))


def test_a_hint_does_not_merely_restate_the_message():
    """The mechanical half of "is the hint useful". A hint identical to the
    message satisfies the letter of the rule and none of its purpose."""
    same = []
    for f, ln, args in _calls():
        raw_hint = _hint_of(args)
        if len(args) < 2 or raw_hint is None:
            continue
        msg, hint = args[1].strip("'\"` "), raw_hint.strip("'\"` ")
        if msg and hint and msg.lower() == hint.lower():
            same.append(f"{f}:{ln}  {msg[:60]}")
    assert not same, (
        "the hint repeats the message instead of saying what to do:\n  "
        + "\n  ".join(same))


def test_both_renderers_require_a_hint():
    """Two renderers, one rule. If either lets the hint go, the rule has a
    hole and the tests above only cover the callers that exist today."""
    base = _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))
    body = base[base.index("window.prismEmptyState"):]
    assert re.search(r"if\s*\(\s*!\s*hint\s*\)[\s\S]{0,200}throw", body[:900]), (
        "prismEmptyState no longer refuses a missing hint")
    # The Jinja macro cannot enforce it at render time, so the enforcement is
    # the call-site test above — assert the macro at least still takes it.
    macro = (TEMPLATES / "partials" / "_empty_state.html").read_text(encoding="utf-8")
    assert re.search(r"{%\s*macro\s+empty_state\(\s*icon\s*,\s*message\s*,\s*hint",
                     macro), "the macro signature dropped the hint"
    assert "text-[10px] mt-1 opacity-60" in macro, "the macro stopped rendering the hint"


def test_there_is_only_one_implementation_per_layer():
    """`server_detail.html` carried a private copy with an optional hint.
    Two implementations of one rule is how the two drift, and the one with
    the hole is invisible from the other."""
    sd = _code_only((TEMPLATES / "server_detail.html").read_text(encoding="utf-8"))
    m = re.search(r"function _renderEmptyState\([^)]*\)\s*{([\s\S]{0,400}?)\n  }", sd)
    assert m, "_renderEmptyState is gone or reshaped; check its 14 call sites"
    assert "prismEmptyState" in m.group(1), (
        "_renderEmptyState has its own implementation again rather than "
        "delegating to the shared helper")
    assert "safeHint ?" not in sd, "the optional-hint branch is back"


# ── the coverage hole: sites that never opted in ─────────────────────────
#
# Every check above inspects CALLS to the three renderers, so the rule is
# enforced only where somebody already chose to use one. Anything that
# hand-rolls the markup is invisible to all of them — and Wave 3's claim was
# "one renderer per layer", which is the claim this section falsifies.
#
# Re-derived rather than counted: nine sites reproduce the macro's own
# markup (the faded 8x8 icon at opacity-30 over a message and a hint) without
# going through either renderer. All nine DO supply a hint, so the three-part
# rule is honoured in content — this is a duplication defect, not a UX one.
# Seven of the nine hardcode an English hint, which in a five-locale app with
# a 39-key gap is the part that actually costs a user something.
#
# NOT converted here, and the reason is scope rather than judgement:
# converting them is Wave 3's G1 sweep re-run, and it needs ~9 translated
# hints across 5 locales. Ratcheted so the hole is visible and cannot grow.

_HANDROLLED_ICON = re.compile(
    r'data-lucide=[\'"`]?\$?\{?[\w-]+[\'"`]?\s+class=[\'"]w-8 h-8 mx-auto mb-2 opacity-30')

# Per-file counts, measured. May shrink, never grow.
HANDROLLED_BASELINE: dict[str, int] = {
    "monitoring.html": 4,
    "servers.html": 5,
}


def _handrolled() -> dict[str, int]:
    """Empty-state-shaped markup that does not go through a renderer.

    Excluded BY CAUSE, not by hand: a window around each match is checked for
    a renderer call, which drops both the renderers' own bodies and every
    genuine caller. Excluding by file or by line number would go stale the
    first time something moved."""
    counts: dict[str, int] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(path.read_text(encoding="utf-8"))
        n = 0
        for m in _HANDROLLED_ICON.finditer(text):
            window = text[max(0, m.start() - 600):m.start() + 700]
            if any(r in window for r in ("prismEmptyState", "empty_state(",
                                         "_renderEmptyState")):
                continue
            n += 1
        if n:
            counts[path.relative_to(TEMPLATES).as_posix()] = n
    return counts


def test_no_new_empty_state_hand_rolls_the_markup():
    counts = _handrolled()
    grew = [f"{f}: {n} (baseline {HANDROLLED_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > HANDROLLED_BASELINE.get(f, 0)]
    assert not grew, (
        "an empty state reproduces the renderer's markup instead of calling "
        "it. Use `empty_state(icon, message, hint)` or "
        "`window.prismEmptyState(...)`:\n  " + "\n  ".join(grew))


def test_the_hand_rolled_baseline_comes_down_when_a_site_is_converted():
    """Third leg of the ratchet. Without it, converting a site silently buys
    back headroom for the next one to be hand-rolled."""
    counts = _handrolled()
    slack = {f: (b, counts.get(f, 0))
             for f, b in HANDROLLED_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "fewer hand-rolled empty states than the baseline; lower it:\n  "
        + "\n  ".join(f"{f}: {b} -> {n}" for f, (b, n) in slack.items()))


def test_the_hand_rolled_detector_can_tell_a_caller_from_a_copy():
    """Positive and negative control in one. The detector's whole job is
    distinguishing markup that reproduces the pattern from markup a renderer
    produced — and the renderer's OWN body matches the pattern, so a detector
    without the cause-based exclusion reports it and every legitimate caller
    as offenders."""
    copy = ('<div class="text-sm text-faint text-center py-8">'
            '<i data-lucide="tags" class="w-8 h-8 mx-auto mb-2 opacity-30"></i>'
            '<p>No tags</p></div>')
    assert _HANDROLLED_ICON.search(copy), (
        "the detector no longer matches hand-rolled empty-state markup")
    caller = "{{ empty_state('tags', t.no_tags, t.no_tags_hint) }}"
    assert not _HANDROLLED_ICON.search(caller), (
        "a macro call is being counted as hand-rolled markup")


def test_a_failed_request_is_not_dressed_up_as_an_empty_state():
    """The failed-logins panel rendered "No data available" from its
    `.catch()` — the same sentence the app uses when a server genuinely has
    no failed logins. A broken endpoint read as a clean bill of health, on
    the one panel where that distinction matters most."""
    sd = (TEMPLATES / "server_detail.html").read_text(encoding="utf-8")
    catch = re.search(r"\.catch\(\(\)\s*=>\s*{([\s\S]{0,700}?)}\);", sd[sd.index("recent-failed-logins") - 2000:]
                      ) if "recent-failed-logins" in sd else None
    block = re.search(r"recent-failed-logins'\)\.innerHTML\s*=([\s\S]{0,600}?);", sd)
    assert block, "the failed-logins error branch has moved"
    text = block.group(1)
    assert "no_data" not in text, (
        "a failed request is reporting itself as an absence of data")
    assert "error" in text.lower() or "critical" in text, (
        "the error branch does not read as an error")
