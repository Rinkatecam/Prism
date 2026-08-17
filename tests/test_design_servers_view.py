"""Guardrails for /servers' two view modes, the sliding band and pagination.

The server cards moved off the dashboard and became one of two views here,
and the table gained pagination. One property matters more than the rest:

    SORT MUST PRECEDE PAGINATION.

If the sort is applied to the visible page instead of to the whole set, "sort
by status" puts the worst row on page 1 rather than the worst row in the
fleet — and on any fleet that fits one page it is indistinguishable from
working. That was flagged during intake as the trap in this task, so it is
asserted from both ends here: table-sort.js announces that the order moved,
and the pagination recomputes from the DOM order rather than from a snapshot.

WHAT THESE ARE BLIND TO:

  * Behaviour. These read source. The property was measured in the running
    app against the real 29-server fleet: sorting name-descending put the
    alphabetically last 25 on page 1, the DOM was fully descending, and all
    29 rows were still in the document. That is the check that actually
    proves it; these keep it true.
  * Whether the band's segment order is the one a reader expects. It is
    ordered by type KEY, not by translated label, so the sequence does not
    change with the UI language — deliberate, and invisible to anyone who
    only ever opens one locale.
  * The filters. `applyFilters` hides cards by `display`, and that it hides
    the sizing WRAPPER rather than the card (which would leave a run of gaps
    where the filtered servers were) was measured: 29 slots hidden, 0 cards
    left visible inside a hidden slot.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
TABLE_SORT = PROJECT_ROOT / "static" / "js" / "table-sort.js"
SERVERS = TEMPLATES / "servers.html"
GRID = TEMPLATES / "partials" / "server_grid.html"
VIEWS = PROJECT_ROOT / "routes" / "views.py"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    """Blank comments, keeping line numbers true.

    Not optional in this file. The ordering rule is explained at length in
    both servers.html and table-sort.js, and those explanations quote the
    wrong version — `remove()`, "sorting the visible page" — which is exactly
    what several checks below search for."""
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _servers_js() -> str:
    """The view-mode / pagination script block, not the whole 2,900-line file.

    Scoped so a match cannot come from one of the page's other twelve script
    blocks — the dependency browser and the tag editor both build tables and
    both talk about rows."""
    text = _code_only(SERVERS.read_text(encoding="utf-8"))
    start = text.index("const VIEW_KEY")
    return text[start:]


def _paginate_body() -> str:
    js = _servers_js()
    m = re.search(r"function paginate\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "paginate() has been reshaped; re-derive what these tests read"
    return m.group(1)


# ── sort, then paginate ──────────────────────────────────────────────────

def test_the_sort_announces_that_the_order_moved():
    """Pagination has to be recomputed from the new order, and the only
    alternative to an explicit signal is hoping the two handlers happen to
    run in a convenient sequence."""
    js = _code_only(TABLE_SORT.read_text(encoding="utf-8"))
    assert "prism:tablesorted" in js, (
        "applySort no longer announces a reorder; anything derived from the "
        "sorted order is now running on a stale order")


def test_the_announcement_comes_after_the_rows_have_actually_moved():
    """Dispatching before the reorder would hand every listener the OLD
    order — and the listener would look correct, run at the right time, and
    produce a page slice of the previous sort."""
    js = _code_only(TABLE_SORT.read_text(encoding="utf-8"))
    m = re.search(r"function applySort\([^)]*\)\s*\{", js)
    assert m, "applySort is gone"
    body = js[m.end():]
    body = body[:body.index("\n  }")]
    place = body.index("d.u.place()")
    dispatch = body.index("prism:tablesorted")
    assert place < dispatch, (
        "the reorder event is dispatched before the rows are placed, so every "
        "listener recomputes from the order that is being replaced")


def test_nothing_is_announced_when_the_order_did_not_change():
    """servers.html rewrites 29 status cells every 15s, which re-triggers the
    sort with an unchanged key. The `alreadyInOrder` guard exists to skip a
    no-op reorder; dispatching anyway would reset the reader to page 1 every
    fifteen seconds while they were reading page 2."""
    js = _code_only(TABLE_SORT.read_text(encoding="utf-8"))
    m = re.search(r"function applySort\([^)]*\)\s*\{", js)
    body = js[m.end():]
    body = body[:body.index("\n  }")]
    guard = body.index("if (alreadyInOrder) return;")
    assert guard < body.index("prism:tablesorted"), (
        "the no-op guard no longer precedes the dispatch")
    short = body.index("units.length < 2")
    assert short < body.index("prism:tablesorted")


def test_the_pagination_recomputes_when_the_order_changes():
    js = _servers_js()
    assert re.search(r"addEventListener\(\s*'prism:tablesorted'", js), (
        "nothing listens for the reorder, so the page slice keeps the "
        "previous sort's rows")
    handler = js[js.index("'prism:tablesorted'"):]
    handler = handler[:handler.index("});")]
    assert "paginate()" in handler, "the listener does not re-page"
    assert re.search(r"page\s*=\s*1", handler), (
        "a new order means page 3 holds different rows than the page 3 the "
        "reader was looking at; the position has no meaning across a re-sort")


def test_the_page_slice_is_read_from_the_dom_and_not_from_a_snapshot():
    """table-sort.js reorders the <tbody> elements themselves, so DOM order
    IS sorted order and reading it fresh is what makes "sort first" true by
    construction. A snapshot taken at load would be the pre-sort order
    forever, and would look right until someone clicked a column."""
    js = _servers_js()
    m = re.search(r"function groups\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "groups() is gone"
    assert "tBodies" in m.group(1), (
        "groups() no longer reads the table's live tbody list")
    assert "groups()" in _paginate_body(), (
        "paginate() no longer calls groups(), so it is not reading the "
        "current order")


def test_rows_off_the_page_are_hidden_and_never_removed():
    """A detached row is invisible to the SORT as well as to the reader,
    which silently turns "sort the fleet" into "sort this page" — the exact
    defect this whole file exists for, arrived at from the other direction."""
    body = _paginate_body()
    assert re.search(r"\.style\.display\s*=", body), (
        "paginate() no longer hides by display")
    for banned in ("remove()", "removeChild", "innerHTML", "detach"):
        assert banned not in body, (
            f"paginate() uses {banned}; a row out of the document cannot be "
            "sorted, so the next sort covers only the visible page")


def test_the_page_size_is_the_agreed_twenty_five():
    js = _servers_js()
    m = re.search(r"PAGE_SIZE\s*=\s*(\d+)", js)
    assert m, "PAGE_SIZE is gone"
    assert 20 <= int(m.group(1)) <= 30, (
        f"PAGE_SIZE is {m.group(1)}; ~25 rows per page was the decision")


def test_the_boundary_buttons_say_why_they_are_unavailable():
    """Same rule as every other disabled control in the app, and reached the
    same way: `prismSetDisabled` sets the state and the reason together, so a
    re-enabled button cannot keep a stale explanation."""
    body = _paginate_body()
    assert body.count("prismSetDisabled") == 2, (
        "the prev/next buttons are no longer disabled through the helper that "
        "keeps the reason in step with the state")
    html = _code_only(SERVERS.read_text(encoding="utf-8"))
    for btn_id in ("servers-page-prev", "servers-page-next"):
        m = re.search(rf'<button[^>]*id="{btn_id}"[^>]*>', html, re.S)
        assert m, f"#{btn_id} is gone"
        assert not re.search(r"(?<![\w-])disabled(?![\w:-])", m.group(0)), (
            f"#{btn_id} is disabled in MARKUP as well as from the helper; the "
            "two disagree the moment the helper enables it")


# ── the band ─────────────────────────────────────────────────────────────

def test_the_band_keeps_its_type_segments():
    """Owner's decision, and the one most likely to be "simplified" away: the
    headings are laid ALONG the band rather than flattened into a single A-Z
    run."""
    html = _code_only(GRID.read_text(encoding="utf-8"))
    assert "server-band-segment" in html, (
        "the band no longer has per-type segments; the grouping the grid had "
        "was kept deliberately, not inherited")
    band = html[html.index('class="server-band"'):]
    assert "<h3" in band[:900], "the segments lost their headings"


def test_both_levels_of_the_band_ordering_are_decided_server_side():
    """The template only ever sees what the view hands it, so it cannot fix a
    grouping that arrived in cache-iteration order. Sorting the display in
    one layer and the grouping in another is how the two disagree."""
    src = VIEWS.read_text(encoding="utf-8")
    m = re.search(r"grouped = \{\}\n(.*?)return render_template\(\"partials/server_grid",
                  src, re.S)
    assert m, "the grouping block in partial_server_grid has moved"
    block = m.group(1)
    assert "sorted(servers" in block and "server_name" in block, (
        "cards are no longer ordered by name within a segment")
    assert re.search(r"sorted\(grouped\)", block), (
        "segment order is left to dict insertion order, which is whatever "
        "order the metrics cache iterated in and is not stable between "
        "refreshes")
    grid = _code_only(GRID.read_text(encoding="utf-8"))
    assert "| sort" not in grid, (
        "the template sorts as well; two orderings for one list")


def test_the_band_is_reachable_from_a_keyboard():
    """A scroll container is not keyboard-scrollable unless it is focusable.
    Without this the card view is mouse-and-trackpad only past the first
    screenful — and it is invisible to the `[data-action]` scan in
    test_design_keyboard.py, because nothing here is clickable."""
    html = _code_only(GRID.read_text(encoding="utf-8"))
    m = re.search(r'<div class="server-band"[^>]*>', html, re.S)
    assert m, "the band scroller is gone"
    tag = re.sub(r"\s+", " ", m.group(0))
    assert 'tabindex="0"' in tag, f"the band cannot be focused: {tag[:120]}"
    assert "aria-label" in tag, (
        "a focusable region with no name announces itself as an unlabelled "
        f"group: {tag[:120]}")


def test_the_hidden_card_view_does_not_fetch_the_whole_fleet_every_refresh():
    """`hx-trigger="load, prismRefresh from:body"` on a hidden region would
    pull every server's card every ~5s for a reader who is looking at the
    table. The fetch is triggered when the view opens, and the refresh bridge
    forwards the event only while it is visible."""
    html = _code_only(SERVERS.read_text(encoding="utf-8"))
    m = re.search(r'id="server-grid-container"[^>]*hx-trigger="([^"]*)"', html, re.S)
    assert m, "the band region no longer declares a trigger"
    trigger = m.group(1)
    assert "load" not in trigger and "from:body" not in trigger, (
        f'the band fetches on its own ({trigger!r}); it is hidden by default '
        "and would poll the fleet unread")
    js = _servers_js()
    bridge = js[js.index("addEventListener('prismRefresh'"):]
    bridge = bridge[:bridge.index("});")]
    assert "classList.contains('hidden')" in bridge and "return" in bridge, (
        "the refresh bridge does not check that the card view is visible")


def test_the_band_scroller_is_not_rounded():
    """Chromium paints `::-webkit-scrollbar` in the padding box and does NOT
    clip it to `border-radius`, so a rounded scroll container gets a straight
    bar running past its own corners.

    test_design_scroll.py enforces this — but only for elements whose
    overflow comes from a TAILWIND UTILITY, because it scans class
    attributes. The band's overflow is authored in app.css, so that check
    cannot see it at all. This is the same property, asserted in the layer it
    actually lives in."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    m = re.search(r"\.server-band\s*\{([^}]*)\}", css, re.S)
    assert m, ".server-band is gone"
    body = m.group(1)
    assert re.search(r"overflow-x:\s*(auto|scroll)", body), (
        "this test is no longer looking at a scroll container, so it proves "
        "nothing")
    radius = re.search(r"border-radius:\s*([^;]+);", body)
    assert radius is None or radius.group(1).strip() in ("0", "0px"), (
        f"the band scroller is rounded ({radius.group(1).strip()}); move the "
        "radius to a clipping shell around it")


# ── the toggle ───────────────────────────────────────────────────────────

def test_the_pressed_state_has_exactly_one_carrier():
    """Two carriers for one state is two things to keep in step, and the half
    a sighted reviewer checks is not the half a screen reader reads. app.css
    styles off `aria-pressed`, so the announced state and the visible state
    cannot come apart."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    assert '.servers-view-btn[aria-pressed="true"]' in css, (
        "the toggle is no longer styled from aria-pressed")
    js = _servers_js()
    m = re.search(r"window\._srvSetView = function[^{]*\{(.*?)\n  \};", js, re.S)
    assert m, "_srvSetView has been reshaped"
    body = m.group(1)
    assert body.count("setAttribute('aria-pressed'") == 2, (
        "both buttons must be updated together, or two can read as pressed")
    assert "classList.add('active'" not in body, (
        "a second state carrier is back on the view buttons")


def test_the_toggle_cannot_be_overridden_by_a_tailwind_utility():
    """The trap that made the pressed segment render as an empty box.

    `.servers-view-btn[aria-pressed="true"]` is (0,2,0) and so is Tailwind's
    `.hover\\:bg-page:hover`. At equal specificity the later sheet wins, and
    Tailwind's is injected after app.css — so `hover:bg-page` in the markup
    replaced the pressed button's violet fill with the near-black page colour
    while this block's near-black `color` stayed. Near-black on near-black:
    measured at roughly 1:1, the label gone, the segment reading as empty.

    Two things keep it fixed and both are asserted, because either alone
    would leave the door open: the rules are element-qualified so they win
    (0,3,1) against any utility, AND the utility is gone from the markup so
    there is nothing to win against.

    Third instance of this shape in app.css — `padding-right` on `select` lost
    to `px-2`, `[disabled]`'s cursor lost to preflight. Note what they share: a
    rule that applies PARTIALLY, where the half that works tells you it is
    working."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    rules = re.findall(r"(^|\})\s*([^{}]*\.servers-view-btn[^{}]*)\{", css, re.M)
    selectors = [r[1].strip() for r in rules]
    assert selectors, "the toggle's rules are gone from app.css"
    unqualified = [s for s in selectors if not re.search(r"\bbutton\.servers-view-btn", s)]
    assert not unqualified, (
        "these toggle rules are not element-qualified, so a Tailwind utility "
        "beats them at equal specificity:\n  " + "\n  ".join(unqualified))

    html = _code_only(SERVERS.read_text(encoding="utf-8"))
    for m in re.finditer(r"<button[^>]*servers-view-btn[^>]*>", html, re.S):
        tag = re.sub(r"\s+", " ", m.group(0))
        assert "hover:" not in tag, (
            "a hover utility is back on the toggle, competing with the rule "
            f"that owns its appearance: {tag[:130]}")


def test_the_pressed_label_stays_readable_in_both_themes():
    """The pressed segment is a filled brand button, and a filled brand fill
    inverts between themes — the same trap the filled turquoise button hit,
    where white-on-fill went from 5.47:1 to 1.86:1 in dark mode.

    THE INK TOKENS ARE READ OUT OF app.css, not assumed. The first version of
    this test hardcoded `card` for light and `page` for dark and computed the
    contrast from the token table — so it passed with the `.dark` override
    RENAMED OUT of existence, which is precisely the defect it was written to
    catch. A test that survives the production artefact being deleted is
    testing the test (docs/OPS-LEARNINGS.md §2.2 #12). Caught by mutation."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from tools import design_tokens as dt

    css = _code_only(APP_CSS.read_text(encoding="utf-8"))

    def ink_of(selector_pattern: str) -> str:
        m = re.search(selector_pattern + r"\s*\{([^}]*)\}", css, re.S)
        assert m, f"no rule matching {selector_pattern!r}; the pressed state " \
                  "has no ink of its own and inherits whatever is around it"
        c = re.search(r"color:\s*rgb\(var\(--c-([\w-]+)\)\)", m.group(1))
        assert c, f"{selector_pattern!r} sets no tokenised colour"
        return c.group(1)

    light_ink = ink_of(r"button\.servers-view-btn\[aria-pressed=\"true\"\]")
    dark_ink = ink_of(r"\.dark button\.servers-view-btn\[aria-pressed=\"true\"\]")
    assert dark_ink != light_ink, (
        f"dark mode reuses the light ink ({light_ink}); the brand fill lifts to "
        "a pale violet in dark mode, so the same ink cannot serve both")

    def luminance(hex_value: str) -> float:
        def channel(c: float) -> float:
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        h = hex_value.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    def contrast(a: str, b: str) -> float:
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    # index 0 = light, 1 = dark, with the ink token each theme's rule actually
    # names. The swap between them is the whole point of the `.dark` override.
    for theme, index, ink_token in (("light", 0, light_ink), ("dark", 1, dark_ink)):
        fill = dt.TOKENS["brand"][index]
        ink = dt.TOKENS[ink_token][index]
        ratio = contrast(ink, fill)
        assert ratio >= 4.5, (
            f"the pressed toggle label is {ratio:.2f}:1 in {theme} "
            f"({ink} on {fill}); AA needs 4.5")


def test_the_hover_uses_the_motion_scale():
    """A hover that snaps is not the polish this control asked for, and a
    hover on an off-scale duration is the thing test_design_motion.py exists
    to prevent. It also must not transition `all`: this button's width changes
    with its label, and sweeping layout properties into the transition is how
    a colour change becomes a reflow."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    m = re.search(r"button\.servers-view-btn\s*\{([^}]*)\}", css, re.S)
    assert m, "the toggle's base rule is gone"
    body = m.group(1)
    assert "transition" in body, "the toggle hover snaps"
    assert "var(--dur-" in body and "var(--ease-" in body, (
        "the transition uses a literal duration or easing instead of the scale")
    assert not re.search(r"transition:\s*all", body), (
        "`transition: all` sweeps in layout properties on a control whose "
        "width depends on its label")


def test_the_remembered_view_lasts_for_the_tab_and_not_forever():
    """A working preference, not a setting — the same decision and the same
    storage as the column sort in table-sort.js."""
    js = _servers_js()
    assert "sessionStorage" in js, "the view is no longer remembered"
    assert "localStorage" not in js, (
        "the view mode moved to localStorage; it is a per-tab working state, "
        "and Settings is where durable preferences live")
    m = re.search(r"VIEW_KEY\s*=\s*'([^']+)'", js)
    assert m and m.group(1).startswith("prism-"), (
        "the storage key is unprefixed and can collide")


def test_storage_being_unavailable_costs_the_preference_and_nothing_else():
    """sessionStorage throws in a hardened browser profile and in private
    modes. An uncaught throw here runs before `paginate()` and would leave
    the table showing all 29 rows with no controls."""
    js = _servers_js()
    m = re.search(r"function currentView\(\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "currentView() is gone"
    body = m.group(1)
    assert "try" in body and "catch" in body, (
        "currentView() does not survive storage being unavailable")
    assert re.search(r"catch[^{]*\{[^}]*return\s+'table'", body, re.S), (
        "the failure path does not fall back to the default view")


def test_the_filter_removal_list_is_derived_rather_than_written_out():
    """The version this replaced listed every class to remove by hand, and
    the list still named colours from before the token migration — classes
    nothing had added for months. Deriving the removals from the same table
    that adds them is what stops the two drifting."""
    js = _servers_js()
    m = re.search(r"window\.setStatusFilter = function[^{]*\{(.*?)\n  \};", js, re.S)
    assert m, "setStatusFilter has been reshaped"
    body = m.group(1)
    assert "FILTER_STYLES" in body and "classList.remove" in body
    assert not re.search(r"classList\.remove\([^)]*'(hover:bg-|text-|border-)[^']*'[^)]*,[^)]*,[^)]*,",
                         body), (
        "a hand-written removal list is back; derive it from FILTER_STYLES")


def test_the_filter_hides_the_slot_and_not_just_the_card():
    """The band wraps each card in a sizing element. Hiding the card alone
    leaves its slot behind — a run of gaps where the filtered servers were,
    which reads as a rendering bug rather than as a filter."""
    js = _servers_js()
    m = re.search(r"window\.applyFilters = function[^{]*\{(.*?)\n  \};", js, re.S)
    assert m, "applyFilters has been reshaped"
    assert "closest('.server-band-item')" in m.group(1), (
        "applyFilters hides the card rather than its slot")
