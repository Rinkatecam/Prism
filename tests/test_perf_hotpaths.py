"""Performance defects in the hover-accordion / save-bar machinery, all
observed live in the running app.

None of this is testable by executing JS (no JS runtime in this test suite,
same constraint as tests/test_design_*.py), so — matching that suite's
existing pattern — these are static-analysis tests over the shipped source,
each paired with a mutation described below or in the fixing commit/report
proving the assertion actually fails when the fix is reverted. Live-browser
before/after reproductions (which this static suite cannot do) were run
separately for each defect below; see the accompanying report.

── Defect 1: canvas readback (static/js/pulse-monitor.js) ──────────────────
`draw()` calls ctx.getImageData()/putImageData() every ~100ms, continuously,
for as long as pulse events keep flowing — measured live: 106-114 calls in
4 seconds of normal polling, not a rare burst. Without `willReadFrequently`
the browser keeps the canvas GPU-backed and every getImageData() forces a
GPU->CPU sync stall (Chrome's own console warning). Measured before picking
a fix: the "obvious" alternative — a canvas drawing itself via
`ctx.drawImage(canvas, -px, 0)`, which never reads pixels back — diverged
from the current output by ~4.8% of pixels once compared byte-for-byte at
this canvas's real size with its actual anti-aliased beat-stroke pattern
(drawImage alpha-composites; putImageData does a raw non-premultiplied
replace). `willReadFrequently: true` changes zero drawing calls, so it is
the only candidate provably byte-identical to today's output.

── Defect 2: scroll-induced hover storm (templates/servers.html) ───────────
mouseenter/mouseleave fire on a `.server-group` row whenever the element
under a STATIONARY pointer changes — which includes scrolling the row out
from under the cursor, not just moving the mouse. Measured live: a single
scripted 10-tick wheel scroll over a fixed cursor position fired 21
mouseenter + 8 mouseleave events. Any dwell that survived the pre-existing
150ms hover-intent delay used to run the full open path unconditionally: an
/api/servers fetch, a forced-layout `panel.scrollHeight` read (measured
~1.9ms/call), and a 300ms transition on `max-height` (layout-triggering,
not transform/opacity) — for potentially several rows in one scroll
gesture. Gated behind a real, passive `scroll` listener (`isScrolling`,
re-checked again at timer-fire time, not just schedule time).

── Defect 3: the scroll-storm fix's own fix (templates/servers.html) ───────
Two demonstrable regressions the scroll-storm fix itself introduced, found
by an adversarial review of that commit (ba1edba):

  (a) STUCK-OPEN PANEL. The scroll-settle path added by that commit is a
      SECOND independent scheduler for showServerInfo() (the mouseenter
      listener in base.html is the first). mouseenter alone can never alias
      hoverTimers[index] — the browser never fires two mouseenter for an
      element without an intervening mouseleave — but the second caller
      can, running while a mouseenter timer for the same row is still
      pending. showServerInfo() used to overwrite hoverTimers[index] with
      no clearTimeout first, orphaning the first timer; hideServerInfo()
      only ever clears the survivor. Reproduced live: mouseenter at t=0,
      scroll-settle calling showServerInfo() again at t=50, mouseleave at
      t=80 (clearing only the second timer) — the panel opened at t~900ms,
      on a row nothing was pointing at, with mouseleave already fired and
      never firing again to close it. Fixed by clearing any pending timer
      for the row before scheduling a new one, unconditionally, so no
      caller can alias the slot.

      Related: lastPointerX/Y (the scroll-settle hit-test's cursor
      position) used to survive the pointer leaving the document entirely,
      so a wheel/keyboard/scrollbar scroll fired while the mouse was
      off-window would hit-test against a stale position. Fixed with a
      `mouseleave` listener on `document` that resets both to -1.

  (b) STALE HEIGHT CACHE. The commit's own height cache was invalidated
      only on `resize`, on the stated belief that resize is the only thing
      that changes a panel's height. It is not: the panel's content comes
      from /api/servers and changes independently of viewport width (a
      server that starts reporting metrics after its first hover, a
      conditional `Disk D:` clause, a status word of different length).
      Measured: 56px true content height vs. 36px cached/applied, 20px
      permanently clipped. Also, toggleSidebar() (base.html) changes
      content width by 10rem via `sidebar.style.width` directly and fires
      zero resize events, so that path could never have invalidated the
      cache either, even with a fix. Fixed by deleting the measurement
      instead of patching the invalidation: `.server-info-panel` is now
      `display:grid` and opens by animating `grid-template-rows` 0fr -> 1fr,
      which needs no JS-side height read, ever, first open or repeat.

── Defect 4: duration-300 vs duration-slow (servers/settings/monitoring) ───
`duration-300` became `duration-slow` (--dur-slow, 320ms) on the elements
these three files' cleanup timeouts hide right after — but the timeouts
still hardcoded 300. Invisible today (300/320 of a hard-decelerating curve
is ~99% done); not invisible the moment --dur-slow is retuned centrally,
which is the entire point of having the token. Each now reads
`getComputedStyle(document.documentElement).getPropertyValue('--dur-slow')`
at call time, with a fallback constant kept numerically equal to app.css's
token (checked below) for a browser that can't resolve the property.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PULSE_JS = PROJECT_ROOT / "static" / "js" / "pulse-monitor.js"
SERVERS_HTML = PROJECT_ROOT / "templates" / "servers.html"
SETTINGS_HTML = PROJECT_ROOT / "templates" / "settings.html"
MONITORING_HTML = PROJECT_ROOT / "templates" / "monitoring.html"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"


def pulse_js() -> str:
    return PULSE_JS.read_text(encoding="utf-8")


def servers_html() -> str:
    return SERVERS_HTML.read_text(encoding="utf-8")


def settings_html() -> str:
    return SETTINGS_HTML.read_text(encoding="utf-8")


def monitoring_html() -> str:
    return MONITORING_HTML.read_text(encoding="utf-8")


def app_css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def strip_js_line_comments(text: str) -> str:
    """Drop `// ...` line comments before asserting on *code*, so a
    docstring-style comment that mentions the very thing being forbidden
    (e.g. explaining why `panel.scrollHeight` was removed) can't make a
    "must not appear" assertion pass or fail for the wrong reason. Not a
    general JS parser -- fine here because none of the checked function
    bodies contain a `//` inside a string literal or a URL.
    """
    return re.sub(r"//.*", "", text)


# ── Defect 1: canvas readback ────────────────────────────────────────────

def test_pulse_canvas_context_opts_into_will_read_frequently():
    """The context that later calls getImageData()/putImageData() every
    frame must be created with willReadFrequently, so the browser keeps its
    backing store CPU-resident instead of forcing a GPU sync on every read.

    Mutation: drop `, { willReadFrequently: true }` so the call reverts to
    the bare `canvas.getContext('2d')` this replaced -> fails.
    """
    js = pulse_js()
    m = re.search(r"ctx\s*=\s*canvas\.getContext\(\s*'2d'\s*(,\s*\{[^}]*\})?\s*\)", js)
    assert m, "could not find the canvas context creation call at all"
    opts = m.group(1) or ""
    assert re.search(r"willReadFrequently\s*:\s*true", opts), (
        "pulse-monitor.js creates its 2D context without willReadFrequently: "
        "true, so every getImageData() in draw() forces a GPU->CPU readback "
        "stall on every frame (measured: 106-114 calls per 4s of normal "
        "polling) — this is the exact pattern Chrome's own console warns "
        "about. See the comment above the getContext call for the "
        "measurement that ruled out the drawImage-self-copy alternative."
    )


def test_pulse_still_uses_the_verified_getimagedata_scroll_not_a_self_blit():
    """Guard against silently swapping in the rejected alternative later.
    `ctx.drawImage(canvas, ...)` (the canvas drawing itself) was measured to
    diverge ~4.8% of pixels from the current output once beat-stroke
    anti-aliasing accumulates over sustained scrolling — see the getContext
    comment. If someone re-introduces a self-referential drawImage scroll
    here without re-doing that measurement, this should fail loudly rather
    than silently ship a rendering regression.

    Mutation: replace the getImageData/putImageData block with
    `ctx.drawImage(canvas, -pxScroll, 0)` -> fails (no getImageData call
    left in draw()).
    """
    js = pulse_js()
    draw_start = js.index("function draw(dtMs)")
    draw_body = js[draw_start:draw_start + 1200]
    assert "getImageData(" in draw_body and "putImageData(" in draw_body


# ── Defect 2: scroll-induced hover storm ─────────────────────────────────

def test_servers_hover_open_is_gated_on_a_scroll_flag():
    """showServerInfo's hover-intent timer must re-check scroll state at
    fire time before doing the expensive open (fetch + panel open), otherwise
    a scroll gesture that merely dwells on a row for >150ms re-triggers the
    full cost.

    Mutation: delete the `if (isScrolling) return;` guard -> fails.
    """
    html = servers_html()
    fn_start = html.index("function showServerInfo(")
    fn_end = html.index("\nfunction hideServerInfo(", fn_start)
    fn_body = html[fn_start:fn_end]
    assert re.search(r"if\s*\(\s*isScrolling\s*\)\s*return", fn_body), (
        "showServerInfo() no longer bails out while a scroll is in progress "
        "-- every row a scroll gesture merely dwells on for >150ms will "
        "kick off a fetch + panel open again (the measured scroll hang)."
    )


def test_servers_scroll_listener_is_passive():
    """The scroll listener that flips `isScrolling` must be passive: a
    blocking scroll listener can itself delay the browser's ability to
    start scrolling, which was one of the explicitly-suspected mechanisms
    for this defect.

    Mutation: drop `{ passive: true }` from the scroll listener -> fails.
    """
    html = servers_html()
    start = html.index("window.addEventListener('scroll'")
    # Slice up to (not including) the NEXT addEventListener registration, so
    # a passive:true elsewhere can't satisfy this by accident.
    next_listener = html.index("addEventListener(", start + 10)
    block = html[start:next_listener]
    assert re.search(r"passive\s*:\s*true", block), (
        "the window 'scroll' listener block does not contain "
        "{ passive: true } -- " + block[:200]
    )


# ── Defect 3a: stuck-open panel (timer aliasing) ─────────────────────────

def test_showserverinfo_clears_pending_timer_before_rescheduling():
    """showServerInfo() has two independent callers: the per-row mouseenter
    listener (base.html) and the scroll-settle elementFromPoint hit-test
    below. mouseenter alone can never alias hoverTimers[index] -- the
    browser never fires two mouseenter for an element without an
    intervening mouseleave -- but the second caller can, running while a
    mouseenter timer for the same row is still pending. Without clearing it
    first, the second call orphans the first timer; hideServerInfo() only
    ever clears the survivor, so the orphaned timer later opens a panel
    nothing is pointing at, with mouseleave already fired and never firing
    again to close it. Reproduced live: mouseenter at t=0, a second
    showServerInfo() call at t=50, hideServerInfo() at t=80 -- the panel
    opened anyway at t~900ms and stayed open.

    Mutation: delete the `clearTimeout(hoverTimers[index]);` line that
    precedes the `hoverTimers[index] = setTimeout(...)` assignment -> fails.
    """
    html = servers_html()
    fn_start = html.index("function showServerInfo(")
    fn_end = html.index("\nfunction hideServerInfo(", fn_start)
    fn_body = html[fn_start:fn_end]
    assert re.search(
        r"clearTimeout\(\s*hoverTimers\[index\]\s*\)\s*;\s*hoverTimers\[index\]\s*=\s*setTimeout",
        fn_body,
    ), (
        "showServerInfo() no longer clears any pending timer for this row "
        "before scheduling a new one -- the scroll-settle hit-test below "
        "can call showServerInfo() while a mouseenter timer for the same "
        "row is still pending, and without this the second call orphans "
        "the first timer (the stuck-open panel defect)."
    )


def test_pointer_position_is_invalidated_when_pointer_leaves_document():
    """lastPointerX/Y feed the scroll-settle elementFromPoint hit-test and
    used to survive the pointer leaving the document entirely, so a wheel,
    keyboard or scrollbar scroll fired while the mouse was off-window (or
    never on the page at all) would hit-test against a stale, unrelated
    position.

    Mutation: delete the `document.addEventListener('mouseleave', ...)`
    block that resets lastPointerX/Y to -1 -> fails.
    """
    html = servers_html()
    assert re.search(
        r"document\.addEventListener\(\s*'mouseleave'\s*,\s*function\s*\(\s*\)\s*\{\s*"
        r"lastPointerX\s*=\s*-1\s*;\s*lastPointerY\s*=\s*-1\s*;",
        html,
    ), "no document 'mouseleave' listener invalidates lastPointerX/lastPointerY"


# ── Defect 3b: stale panel height cache ──────────────────────────────────

def test_servers_panel_open_forces_no_layout_read():
    """The panel used to measure panel.scrollHeight once per row and cache
    it, invalidated only on resize -- but the panel's content comes from
    /api/servers and changes independently of viewport width (a server that
    starts reporting after its first hover, a conditional `Disk D:` clause,
    a status word of different length), so the cached max-height went stale
    and clipped the new content for the tab's lifetime (measured: 56px true
    height, 36px applied, 20px permanently clipped). Deleted the
    measurement rather than patching the invalidation: the panel is
    `display:grid` and opens via `grid-template-rows` 0fr -> 1fr, which
    needs no JS-side height read at all, on the first open or any later one.

    Mutation: reintroduce a `panel.scrollHeight` read anywhere in
    showServerInfo() -> fails.
    """
    html = servers_html()
    fn_start = html.index("function showServerInfo(")
    fn_end = html.index("\nfunction hideServerInfo(", fn_start)
    fn_body = html[fn_start:fn_end]
    code_only = strip_js_line_comments(fn_body)
    assert "scrollHeight" not in code_only, (
        "showServerInfo() reads panel.scrollHeight again -- that forces a "
        "synchronous layout (measured ~1.9ms/call) on every open, which is "
        "exactly what the grid-template-rows rewrite was meant to remove "
        "entirely rather than just cache."
    )
    assert re.search(r"panel\.style\.gridTemplateRows\s*=\s*'1fr'", fn_body), (
        "showServerInfo() no longer opens the panel by setting "
        "panel.style.gridTemplateRows = '1fr'"
    )


def test_servers_panel_height_cache_is_gone_not_just_invalidated():
    """The old cache (`panelHeightCache`, invalidated only on resize) is
    deleted outright rather than patched with more invalidation points --
    there is no cache left to go stale. This also sidesteps a second,
    unfixable gap in the resize-based approach: toggleSidebar() (base.html)
    changes content width by 10rem via `sidebar.style.width` directly and
    fires zero resize events, so that path could never have invalidated a
    height cache either. grid-template-rows has no height cache for a
    sidebar toggle (or anything else) to invalidate.

    Mutation: reintroduce `panelHeightCache` anywhere in servers.html, or
    revert the panel markup to `overflow-hidden` + `max-height:0` -> fails.
    """
    html = servers_html()
    assert "panelHeightCache" not in html, (
        "panelHeightCache still exists somewhere in servers.html -- the "
        "point of the grid-template-rows rewrite was to delete the height "
        "cache, not just add another invalidation point to it."
    )
    assert re.search(
        r'class="server-info-panel grid[^"]*"\s+style="grid-template-rows:0fr;"',
        html,
    ), (
        "the .server-info-panel markup is no longer a grid with an initial "
        "grid-template-rows:0fr -- showServerInfo()/hideServerInfo() toggle "
        "that property directly and need it to start closed."
    )


def test_servers_hide_toggles_grid_template_rows_not_max_height():
    """hideServerInfo's close path and its own "did it actually close"
    check must agree with showServerInfo's open mechanism -- both read/write
    `gridTemplateRows`, not the old `maxHeight`.

    Mutation: change either the close assignment or the check back to
    `panel.style.maxHeight` -> fails.
    """
    html = servers_html()
    fn_start = html.index("function hideServerInfo(")
    fn_end = html.index("\n// ── Setup Guide", fn_start)
    fn_body = html[fn_start:fn_end]
    assert "maxHeight" not in fn_body, "hideServerInfo() still touches maxHeight"
    assert re.search(r"panel\.style\.gridTemplateRows\s*=\s*'0fr'", fn_body)
    assert re.search(r"panel\.style\.gridTemplateRows\s*===\s*'0fr'", fn_body), (
        "hideServerInfo()'s cleanup must re-check gridTemplateRows is still "
        "'0fr' before hiding the row (a re-open during the close transition "
        "must not have its display toggled off from under it)"
    )


# ── Defect 4: duration-300 vs duration-slow ───────────────────────────────

def test_cleanup_timeouts_read_dur_slow_at_runtime_not_hardcoded():
    """Four cleanup timeouts across three templates hide a `#save-bar` or
    `.server-info-panel` right after its `duration-slow` (--dur-slow)
    transition -- but duration-300 became duration-slow (320ms) without
    updating these, so they still hardcoded 300. Each must now read the
    live custom property instead, so a future retune of --dur-slow doesn't
    leave the cleanup racing ahead of the transition it waits out.

    Mutation: hardcode `300` back into any of the four call sites -> fails.
    """
    checks = [
        (SERVERS_HTML, servers_html(), "function hideServerInfo(", "\n// ── Setup Guide"),
        (SETTINGS_HTML, settings_html(), "function checkForChanges(", "\nfunction cancelAllChanges("),
        (SETTINGS_HTML, settings_html(), "function doSaveAllSettings(", "\nfunction getEmailSettingsFromForm("),
        (MONITORING_HTML, monitoring_html(), "function saveMonitoringSettings(", "\n// ── Change detection"),
    ]
    for path, html, start_marker, end_marker in checks:
        start = html.index(start_marker)
        end = html.index(end_marker, start)
        body = html[start:end]
        assert "getDurSlowMs()" in body, (
            f"{path.name}::{start_marker.strip()} no longer calls "
            "getDurSlowMs() before hiding its save-bar/panel"
        )
        assert not re.search(r"classList\.add\(\s*'hidden'\s*\)\s*,\s*300\s*\)", body), (
            f"{path.name}::{start_marker.strip()} still hardcodes 300 in "
            "the cleanup setTimeout"
        )



def _dur_slow_helper_files() -> list[tuple[str, str]]:
    """(name, text) for EVERY template defining a --dur-slow reader.

    Discovered, not listed. The first version of these tests hardcoded three
    files; three more copies existed under a second name (`_durSlowMs` rather
    than `getDurSlowMs`), added by a different author, and a wrong fallback
    or a dropped unit branch in any of those three passed green.

    This is the failure the commit that introduced them named in its own
    message — "the scope of a check has to be the scope of the thing it
    checks" — committed in the same breath as the sentence.
    """
    root = Path(__file__).resolve().parent.parent
    out = []
    for path in sorted((root / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"function\s+_?[gG]etDurSlowMs\s*\(|function\s+_durSlowMs\s*\(", text):
            out.append((path.name, text))
    assert len(out) >= 6, (
        f"expected at least 6 files defining a --dur-slow reader, found "
        f"{len(out)}: {[n for n, _ in out]} — if a copy was removed, lower "
        f"this floor deliberately rather than letting coverage shrink silently"
    )
    return out

def test_dur_slow_fallback_matches_css_token():
    """Each of the three templates' DUR_SLOW_FALLBACK_MS (used only when
    getComputedStyle can't resolve the custom property) must stay
    numerically equal to app.css's --dur-slow, or the fallback silently
    drifts from the token it stands in for.

    Mutation: change DUR_SLOW_FALLBACK_MS in any of the three files without
    updating app.css's --dur-slow (or vice versa) -> fails.
    """
    css = app_css()
    m = re.search(r"--dur-slow:\s*([\d.]+)ms", css)
    assert m, "app.css no longer defines --dur-slow in ms"
    token_ms = float(m.group(1))

    for name, html in _dur_slow_helper_files():
        fm = re.search(r"DUR_SLOW_FALLBACK_MS\s*=\s*([\d.]+)", html)
        assert fm, f"{name} has no DUR_SLOW_FALLBACK_MS fallback constant"
        assert float(fm.group(1)) == token_ms, (
            f"{name}'s DUR_SLOW_FALLBACK_MS ({fm.group(1)}) does not "
            f"match app.css's --dur-slow ({token_ms}ms)"
        )


def test_get_dur_slow_ms_parses_ms_and_bare_seconds():
    """getDurSlowMs() must handle both unit spellings a duration custom
    property can carry (`320ms` today; a retune could plausibly be written
    as `0.5s`), not just the one --dur-slow happens to use right now.

    Mutation: drop the `raw.endsWith('ms') ? num : num * 1000` branch (i.e.
    always treat the parsed number as milliseconds) -> fails, since this
    test's exec of the function body against a fake '0.5s' input would
    silently be wrong. Since this suite can't execute JS, this test instead
    asserts the branching logic is present in the source.
    """
    for name, html in _dur_slow_helper_files():
        m = re.search(r"function\s+_?(?:get)?[dD]urSlowMs\s*\(", html)
        assert m, f"{name} lost its --dur-slow reader"
        fn_start = m.start()
        # Indentation-tolerant: three of the six copies are nested inside a
        # callback and close with "\n  }", not "\n}". Keyed on the literal
        # the first version used, this raised ValueError rather than
        # reporting a real result.
        close = re.compile(r"\n\s*\}").search(html, fn_start)
        assert close, f"{name}'s --dur-slow reader has no closing brace"
        body = html[fn_start:close.start()]
        assert re.search(r"endsWith\(\s*'ms'\s*\)", body), (
            f"{name}'s --dur-slow reader no longer branches on a 'ms' "
            "suffix -- a token authored in bare seconds would be "
            "misinterpreted as milliseconds (500x too long)"
        )
