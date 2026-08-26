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


def _static_js_functions() -> set[str]:
    """Globals defined in static/js/*.js.

    base.html loads several of these with a <script src>, so their functions
    are as reachable from a page as anything in its own inline block. A scan
    blind to them reports working calls as missing — and the fix a reader
    would reach for is to move the function, not to fix the scan."""
    names: set[str] = set()
    static_js = PROJECT_ROOT / "static" / "js"
    for f in sorted(static_js.glob("*.js")):
        names |= _defined_functions(f.read_text(encoding="utf-8"))
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
    available = (_registry_names() | _defined_functions(src)
                 | _defined_functions(base_src) | _static_js_functions())

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


# ── the other way a page reaches a handler ───────────────────────────────

# Keywords first: `if (…)` matches "a name followed by a paren" exactly as
# well as `loadThing()` does, and the first run of this scan duly reported
# `if` on four pages — the check describing JavaScript's grammar rather than
# the page's dependencies.
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "new",
    "function", "else", "do", "try", "await", "yield", "delete", "void",
    "in", "of", "instanceof", "case",
}

_BUILTIN_CALLS = {
    # Things every page can call that are not page-local functions.
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "requestIdleCallback",
    "queueMicrotask", "structuredClone", "parseInt", "parseFloat", "String",
    "Number", "Boolean", "Array", "Object", "JSON", "Date", "Math", "Promise",
    "Set", "Map", "WeakMap", "RegExp", "Error", "URL", "URLSearchParams",
    "FormData", "Intl", "alert", "confirm", "prompt", "console",
    "encodeURIComponent", "decodeURIComponent", "isNaN", "isFinite",
    "matchMedia", "getComputedStyle", "atob", "btoa",
} | _JS_KEYWORDS


def _bootstrap_calls(src: str) -> set[str]:
    """Bare `name()` calls made directly in a DOMContentLoaded body.

    Nested function bodies are included — they run later but they run, and a
    name that is not there is the same ReferenceError whenever it fires."""
    out: set[str] = set()
    for m in re.finditer(r"addEventListener\('DOMContentLoaded',\s*(?:function\s*\(\)|\(\)\s*=>)\s*\{",
                         src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        body = src[m.end():i]
        out |= set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", body))
    return out - _BUILTIN_CALLS


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_bootstrap_call_resolves_on_its_own_page(page: Path):
    """A loader that moved away leaves its call behind, and the resulting
    ReferenceError aborts every initialiser after it in the same handler.

    Twice in two slices: `recalculateBaselines` and `loadRestartSchedules`.
    The first was caught by the data-action guard above; the second was only
    visible in the browser console, which is why this exists."""
    src = _code_only(_expand_includes(page.read_text(encoding="utf-8")))
    base_src = _code_only(BASE.read_text(encoding="utf-8"))
    available = (_defined_functions(src) | _defined_functions(base_src)
                 | _registry_names() | _static_js_functions())

    missing = sorted(c for c in _bootstrap_calls(src)
                     if c not in available and not c.startswith("_"))
    assert not missing, (
        f"{page.name} calls these on load and they are not defined on that "
        f"page:\n  " + "\n  ".join(missing))


def test_the_bootstrap_scan_finds_calls_at_all():
    """The same guard-on-the-guard as above: a regex that matched no
    DOMContentLoaded bodies would make every assertion vacuous."""
    total = sum(len(_bootstrap_calls(_code_only(p.read_text(encoding="utf-8"))))
                for p in _pages())
    assert total >= 20, f"only {total} bootstrap calls found across the pages"


def test_the_external_scripts_are_visible_to_the_scan():
    """Positive control for `_static_js_functions`, in place of a mutation
    that would have edited this file to prove this file works.

    `loadChart` lives in static/js/charts.js and is called from
    server_detail.html's bootstrap. If the external scripts ever stop being
    scanned, the bootstrap check reports it — and every other cross-file
    global — as missing, and the obvious "fix" is to move working code."""
    names = _static_js_functions()
    assert "loadChart" in names, (
        "static/js is no longer being scanned; cross-file globals will be "
        "reported as missing")
    assert len(names) >= 10, f"only {len(names)} globals found in static/js"


# ── a shared partial's dependencies ──────────────────────────────────────


def _shared_partials() -> dict[Path, list[Path]]:
    """Partials that more than one page includes, and who includes them."""
    users: dict[Path, list[Path]] = {}
    for page in _pages():
        text = page.read_text(encoding="utf-8")
        for name in re.findall(r'\{%\s*include\s+"([^"]+)"\s*%\}', text):
            target = TEMPLATES / name
            if target.exists():
                users.setdefault(target, []).append(page)
    return {k: v for k, v in users.items() if len(v) > 1}


def _javascript_of(src: str) -> str:
    """The JS in a partial, or "" if it holds none.

    A partial included INSIDE a page's <script> block is bare JavaScript with
    no tag of its own; one included in the body may carry <script> and
    <style>. Both shapes exist here, and the CSS ones must not be scanned:
    the first version of this check reported `calc()`, `attr()` and a dozen
    English words as missing dependencies of a stylesheet partial, which is
    the pattern matching the file format rather than the dependency."""
    if "<style" in src:
        src = re.sub(r"<style\b.*?</style>", " ", src, flags=re.S | re.I)
    # Jinja expressions are server-side: `{{ t.edit | default('Edit') }}`
    # reads as a call to `default()` to a scanner that cannot tell the two
    # languages apart, and these partials are full of them.
    src = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", src, flags=re.S)
    tagged = re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S | re.I)
    if tagged:
        return "\n".join(tagged)
    # Bare-JS partial: only if it actually looks like code.
    return src if re.search(r"\bfunction\s+\w+\s*\(|=>", src) else ""


def _calls_in(src: str) -> set[str]:
    """Bare `name(` calls in a source's JavaScript, minus what it defines."""
    js = _javascript_of(src)
    if not js:
        return set()
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", js))
    return called - _BUILTIN_CALLS - _defined_functions(js)


def test_a_shared_partial_only_calls_what_every_including_page_has():
    """`_escHtml` had three definitions and no owner, so moving a block off a
    page took that page's only copy with it. The shared runbook renderer then
    failed on /operations and nowhere else — reported in a table cell, and by
    nothing else.

    Checked per (partial, including page) pair, because a dependency that
    exists on one includer and not the other is exactly the failure."""
    base_src = _code_only(BASE.read_text(encoding="utf-8"))
    external = _static_js_functions()
    broken: list[str] = []

    for partial, pages in sorted(_shared_partials().items()):
        needs = _calls_in(_code_only(partial.read_text(encoding="utf-8")))
        if not needs:
            continue
        for page in pages:
            page_src = _code_only(_expand_includes(page.read_text(encoding="utf-8")))
            available = (_defined_functions(page_src) | _defined_functions(base_src)
                         | external | _registry_names())
            for name in sorted(needs - available):
                if name.startswith("_") and name not in {"_escHtml"}:
                    # Locals a partial defines under another spelling are not
                    # this check's business; `_escHtml` is named explicitly
                    # because it is the one that actually broke.
                    continue
                broken.append(f"{partial.name} needs {name}(), missing on {page.name}")

    assert not broken, (
        "shared partials calling something an including page does not "
        "define:\n  " + "\n  ".join(broken))


def test_there_are_shared_partials_to_check():
    """Guard on the guard, again: with no shared partials the assertion above
    is vacuous, and shared partials are the mechanism the whole restructure
    leans on."""
    shared = _shared_partials()
    assert len(shared) >= 2, f"only {len(shared)} shared partials found"


# ── helpers base.html owns, and pages depend on ──────────────────────────
#
# The mirror of the shared-partial check above. That one catches a partial
# calling something an including page lacks; this catches a PAGE calling
# something only base.html defines. Both are the `_escHtml` failure and only
# one was guarded.
#
# WP-4 D4d created four of these on purpose: it splits `testConnection`'s
# callers across /servers and Settings → Servers, so the helpers they share
# had to stop belonging to either page. The day one is moved back, two pages
# break and neither says so.
_BASE_OWNED_HELPERS = {
    "_escHtml": "three copies existed until a move took one page's only one",
    "loadConfig": "D4d: /servers and Settings \u2192 Servers both read the config",
    "doTestConnection": "D4d: the row button and the modal button share it",
    "showTestResult": "D4d: same",
    "setTestLoading": "D4d: same",
    "prismEmptyState": "every page's empty states render through it",
}


def test_base_html_still_owns_the_helpers_pages_depend_on():
    base_src = _code_only(BASE.read_text(encoding="utf-8"))
    defined = _defined_functions(base_src) | set(
        re.findall(r"window\.(\w+)\s*=\s*function", base_src))
    missing = [f"{name} ({why})"
               for name, why in sorted(_BASE_OWNED_HELPERS.items())
               if name not in defined]
    assert not missing, (
        "base.html no longer defines helpers that page scripts call:\n  "
        + "\n  ".join(missing))


def test_no_page_redefines_a_helper_base_html_owns():
    """Two definitions is how the first one became wrong. A page-local copy
    shadows the shared one, so a fix to base.html silently does not reach that
    page — which is the state `_escHtml` was in for a year."""
    dupes = []
    for page in sorted(TEMPLATES.glob("*.html")):
        if page.name == "base.html":
            continue
        src = _code_only(page.read_text(encoding="utf-8"))
        for name in _BASE_OWNED_HELPERS:
            if re.search(r"function\s+" + re.escape(name) + r"\s*\(", src):
                dupes.append(f"{page.name} defines its own {name}()")
    assert not dupes, (
        "pages shadowing a helper base.html owns:\n  " + "\n  ".join(dupes))


def test_the_helper_list_still_names_the_ones_that_matter():
    """"Not empty" is not enough: dropping one name leaves the rest, so the
    guard keeps passing while that helper quietly becomes unguarded. The ones
    with a known history are named individually.

    `_escHtml` is here because it is the one that actually broke a page. The
    four from D4d are here because that slice deliberately split their callers
    across two pages, which is the condition that makes a shared helper
    load-bearing rather than convenient."""
    required = {"_escHtml", "loadConfig", "doTestConnection", "showTestResult",
                "setTestLoading", "prismEmptyState"}
    missing = sorted(required - set(_BASE_OWNED_HELPERS))
    assert not missing, (
        f"dropped from the guarded list, so nothing checks them any more: {missing}")
    assert all(why.strip() for why in _BASE_OWNED_HELPERS.values()), (
        "every entry has to say why it is shared, or the list becomes a set of "
        "names nobody can prune safely")
