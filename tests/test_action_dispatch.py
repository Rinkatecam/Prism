"""Every `data-action` must resolve to something that runs.

base.html's dispatcher looks the name up in a registry, then falls back to a
global function of the same name — and if neither exists **it silently does
nothing**. No console error, no exception, no visual difference: a button that
looks exactly like every other button and is inert.

This file exists because that happened. WP-4 D2a moved the detection block's
markup from /monitoring to a Settings partial and left
`recalculateBaselines()` behind in /monitoring's script. The Recalculate
Baselines button on /settings/detection had no handler at all, and nothing —
not the test suite, not the browser console, not the page — said so. It was
found by listing the page's actions by hand a slice later.

Moving markup between templates is exactly what WP-4 does for the rest of its
packages, so the same defect is available on every remaining move.

WHAT THIS IS BLIND TO:

  * Whether the handler DOES the right thing. This asserts only that the name
    resolves.
  * Handlers attached by other means (a direct addEventListener on an id).
    Those are not `data-action` and are not this file's subject.
  * `data-change` / `data-input` / `data-submit`, which the same dispatcher
    serves. They are checked too — the dispatcher treats them identically, so
    excluding them would leave three quarters of the surface unguarded.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
BASE = TEMPLATES / "base.html"

# The attributes base.html's `_evtAttr` map dispatches. Kept in step with it
# by test_the_dispatched_attributes_are_the_ones_base_html_lists below, so a
# new event type cannot be added there and go unguarded here.
_DISPATCH_ATTRS = ("data-action", "data-change", "data-submit", "data-input",
                   "data-mousedown", "data-mouseup")

_COMMENTS = re.compile(r"\{#.*?#\}|<!--.*?-->", re.S)


def _code_only(text: str) -> str:
    """Comments blanked. Several templates document their action names in
    prose, and a scan that cannot tell code from commentary reports handlers
    for buttons that do not exist — and, worse, accepts a name that appears
    only in a comment as proof that one does."""
    text = _COMMENTS.sub(" ", text)
    return re.sub(r"^[ \t]*//[^\n]*", " ", text, flags=re.M)


def _pages() -> list[Path]:
    """Page templates: everything that extends base.html."""
    return sorted(p for p in TEMPLATES.glob("*.html")
                  if "{% extends" in p.read_text(encoding="utf-8"))


def _expand_includes(text: str, depth: int = 3) -> str:
    for _ in range(depth):
        def _inline(match):
            target = TEMPLATES / match.group(1)
            return target.read_text(encoding="utf-8") if target.exists() else match.group(0)
        expanded = re.sub(r'\{%\s*include\s+"([^"]+)"\s*%\}', _inline, text)
        if expanded == text:
            break
        text = expanded
    return text


def _registry_names() -> set[str]:
    """The names base.html registers centrally."""
    src = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"window\.registerActions\(\{(.*?)\n\s*\}\);", src, re.S)
    assert m, "base.html no longer registers a central action map"
    return set(re.findall(r"'([\w-]+)':", m.group(1)))


def _defined_functions(src: str) -> set[str]:
    """Global functions the dispatcher's fallback can find.

    Five forms, and the list was arrived at by being wrong: the first version
    knew only `function name(`, and reported five of /servers' handlers as
    missing. All five are `window.name = function`, which is not merely valid
    but the CORRECT form for a handler defined inside an IIFE — the only way
    to reach `window[key]` from a closure. A guard that demands one spelling
    is a guard that would have had those five rewritten to satisfy it.
    """
    names = set(re.findall(r"\bfunction\s+(\w+)\s*\(", src))
    names |= set(re.findall(r"\basync\s+function\s+(\w+)\s*\(", src))
    names |= set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?function\b", src))
    names |= set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", src))
    # `window.name = …` — the form a handler defined inside an IIFE must use.
    names |= set(re.findall(r"\bwindow\.(\w+)\s*=\s*(?:async\s*)?function\b", src))
    names |= set(re.findall(r"\bwindow\.(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", src))
    # Assigned over later (`saveAllSettings = function () {…}`) still resolves.
    names |= set(re.findall(r"^\s*(\w+)\s*=\s*function\b", src, re.M))
    return names


def _requested_actions(src: str) -> set[str]:
    out: set[str] = set()
    for attr in _DISPATCH_ATTRS:
        out |= set(re.findall(rf'{attr}="([^"{{]+)"', src))
    return out


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_dispatched_action_resolves_to_a_handler(page: Path):
    """The whole point. A name that resolves to neither the registry nor a
    global function is a control that does nothing, quietly."""
    src = _code_only(_expand_includes(page.read_text(encoding="utf-8")))
    base_src = _code_only(BASE.read_text(encoding="utf-8"))
    available = _registry_names() | _defined_functions(src) | _defined_functions(base_src)

    missing = sorted(a for a in _requested_actions(src) if a not in available)
    assert not missing, (
        f"{page.name} asks for handlers that do not exist on that page — the "
        f"dispatcher will silently do nothing:\n  " + "\n  ".join(missing))


def test_the_scan_finds_actions_at_all():
    """Guard on the guard: a regex that matched nothing would make every
    assertion above vacuously true, which is the 'saw nothing' versus 'saw
    nothing bad' distinction this repository keeps relearning."""
    total = sum(len(_requested_actions(_code_only(p.read_text(encoding="utf-8"))))
                for p in _pages())
    assert total >= 50, f"only {total} dispatched actions found across the pages"


def test_the_dispatched_attributes_are_the_ones_base_html_lists():
    """If base.html gains a seventh event attribute, this file must know: an
    attribute the dispatcher serves and this scan ignores is a quarter of the
    surface going unguarded without anything failing."""
    src = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"_evtAttr\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "the dispatcher's event map has moved"
    listed = set(re.findall(r"'(data-[\w-]+)'", m.group(1)))
    assert listed == set(_DISPATCH_ATTRS), (
        f"base.html dispatches {sorted(listed)}; this file scans "
        f"{sorted(_DISPATCH_ATTRS)}")


def test_the_fallback_to_a_global_function_still_exists():
    """The registry alone would not resolve most of these names. If the
    fallback is ever removed, every page-local handler stops working at once
    and the tests above would still pass on the registry names."""
    src = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"function run\(el, e, key\)\s*\{(.*?)\n      \}", src, re.S)
    assert m, "the dispatcher's run() has been reshaped"
    assert "window[key]" in m.group(1), (
        "the global-function fallback is gone; every page-local data-action "
        "handler now resolves to nothing")
