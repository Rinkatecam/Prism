"""Two measured performance defects, both observed live in the running app.

Neither is testable by executing JS (no JS runtime in this test suite, same
constraint as tests/test_design_*.py), so — matching that suite's existing
pattern — these are static-analysis tests over the shipped source, each
paired with a mutation described in the fixing commit/report proving the
assertion actually fails when the fix is reverted.

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
gesture. The fix gates that path behind a real, passive `scroll` listener
(`isScrolling`, checked again at timer-fire time) and caches the measured
panel height per row. Measured with a realistic scroll-with-dwell replay
(16 synthetic mouseenter/mouseleave pairs, continuous scroll events,
matching the exact pattern used to measure the "before" number): forced
`scrollHeight` reads during the storm went from 5 to 0.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PULSE_JS = PROJECT_ROOT / "static" / "js" / "pulse-monitor.js"
SERVERS_HTML = PROJECT_ROOT / "templates" / "servers.html"


def pulse_js() -> str:
    return PULSE_JS.read_text(encoding="utf-8")


def servers_html() -> str:
    return SERVERS_HTML.read_text(encoding="utf-8")


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
    fire time before doing the expensive open (fetch + forced-layout
    scrollHeight read + max-height transition), otherwise a scroll gesture
    that merely dwells on a row for >150ms re-triggers the full cost.

    Mutation: delete the `if (isScrolling) return;` guard -> fails.
    """
    html = servers_html()
    fn_start = html.index("function showServerInfo(")
    fn_body = html[fn_start:fn_start + 600]
    assert re.search(r"if\s*\(\s*isScrolling\s*\)\s*return", fn_body), (
        "showServerInfo() no longer bails out while a scroll is in progress "
        "-- every row a scroll gesture merely dwells on for >150ms will "
        "force a synchronous layout read and kick off a fetch + transition "
        "again (the measured scroll hang)."
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
    # a passive:true elsewhere (e.g. the resize listener) can't satisfy this
    # by accident.
    next_listener = html.index("addEventListener(", start + 10)
    block = html[start:next_listener]
    assert re.search(r"passive\s*:\s*true", block), (
        "the window 'scroll' listener block does not contain "
        "{ passive: true } -- " + block[:200]
    )


def test_servers_panel_height_is_cached_not_reread_every_open():
    """`panel.scrollHeight` forces a synchronous layout (measured ~1.9ms per
    call live). A row that is opened more than once must not pay that cost
    again -- the panel's content height is stable for a given row/viewport.

    Mutation: replace the cached lookup with a bare
    `panel.style.maxHeight = panel.scrollHeight + 'px'` (today's read every
    time) -> fails.
    """
    html = servers_html()
    fn_start = html.index("function showServerInfo(")
    fn_end = html.index("\nfunction hideServerInfo(", fn_start)
    fn_body = html[fn_start:fn_end]
    assert "panelHeightCache" in fn_body, (
        "showServerInfo() measures panel.scrollHeight on every open again; "
        "it should cache it per row index (invalidated on resize) so a "
        "repeat hover on the same row doesn't force another layout."
    )
    assert re.search(r"panelHeightCache\s*\[\s*index\s*\]\s*==\s*null", fn_body), (
        "the cache must be checked before reading scrollHeight, not after"
    )


def test_servers_resize_invalidates_the_panel_height_cache():
    """A row's rendered height can change at a different viewport width
    (the info line is flex-wrap and can gain/lose a line) -- the cache must
    not survive a resize.

    Mutation: remove the `resize` listener that resets panelHeightCache ->
    fails.
    """
    html = servers_html()
    assert re.search(
        r"window\.addEventListener\(\s*'resize'\s*,\s*function[^{]*\{\s*panelHeightCache\s*=\s*\{\s*\}",
        html,
    ), "no resize listener clears panelHeightCache"
