"""The /servers table row is the way through to that server's overview.

Owner request, verbatim: "for the servers page make the table also clickable
so for example I can click on one server and it sends me to the overview".

Three things had to be true at once, and each of them is a way this feature
is normally shipped broken:

  1. THE ROW GOES TO THE RIGHT SERVER. A row-sized target built from
     `loop.index0` instead of the server's identity looks perfect until the
     table is sorted — table-sort.js reorders the `<tbody>` elements
     themselves, so row 3 stops being server 3 the first time a column header
     is clicked, and the click then opens somebody else's host. The link is
     therefore built from `s.name`, and the test below renders TWO servers
     and pins each row's href to its own row's name rather than asserting
     that "a /server/ link exists".

  2. THE CONTROLS INSIDE THE ROW STILL WORK. This is the usual casualty. A
     whole-row handler that swallows its own buttons turns "manage tags" and
     "test connection" into "navigate away", and the failure is silent
     because the navigation looks like a feature.

     What makes it safe here is NOT a guard in servers.html — it is the shape
     of base.html's dispatcher. One listener on `document` resolves a click
     with `e.target.closest('[data-action]')` and runs THAT element's action
     only. `closest` returns the NEAREST carrier, so a click on the test
     button resolves to the test button and the row's `goto` is never looked
     up. The property that has to hold, then, is that every interactive thing
     inside the row carries a `data-action` of its own — and that is what is
     asserted, against the rendered row, plus the dispatcher line it depends
     on. A bare `<button>` dropped into the row later is the regression, and
     the negative control below proves the detector sees one.

  3. IT IS OPERABLE WITHOUT A MOUSE. A clickable `<tr>` is not a link: no tab
     stop, no Enter. base.html's keyboard bridge keys on
     `[data-action][tabindex]`, so the row is given `tabindex="0"` and an
     `aria-label` — the same shape as the three clickable rows this app
     already has (workflows.html, server_detail.html x2).

     No `role` is declared, and that is a decision rather than an omission.
     A `<tr>` is a `row`; overriding it to `link` takes the row out of the
     table's accessibility tree and costs all six cells their column-header
     association. The test below refuses `role="link"`/`role="button"` on the
     row for that reason, and says so, so the next reader does not "fix" it.

WHAT THESE ARE BLIND TO:

  * Behaviour. There is no JS runtime in this suite (see the header of
    tests/test_table_sort.py), so these read the shipped markup and the
    shipped dispatcher rather than clicking anything. The click resolution
    itself — that `closest` stops at the nested button — is a documented
    property of the DOM API, not of this repository, and the test pins the
    call that relies on it.
  * The focus ring. app.css's global `[tabindex]:focus-visible` rule is what
    draws it; that it covers this row follows from the row having a tabindex,
    which is asserted, but the ring was not re-measured here.
  * Pointer feel — that `cursor-pointer` actually renders as a hand, and that
    the hover tint reads at a glance. Needs a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
BASE = TEMPLATES / "base.html"
SERVERS = TEMPLATES / "servers.html"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    """Blank comments out, keeping line numbers true.

    Required here for the same reason test_design_keyboard.py needs it: the
    row's markup is preceded by a long comment that quotes `data-action`,
    `role="link"` and `stop-prop` while explaining why they are or are not
    used. A scan that cannot tell code from commentary would find the
    explanation and report the feature as present — or, worse, report the
    thing the comment warns AGAINST as present.
    """
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


# ── the rendered page ─────────────────────────────────────────────────────
#
# Rendered rather than grepped, because the property in (1) is about the
# relationship between two things in the same row — the name shown and the
# href followed — and a source scan can only see the Jinja expression that
# produces both. Two servers, so "the href is built from the loop variable"
# and "the href is a constant" cannot both pass.

class _T:
    """Stands in for the i18n dict, which resolves anything asked of it."""

    def get(self, key, default=None):
        return default if default is not None else key

    def __getattr__(self, name):
        return name


class _Server:
    def __init__(self, name, host, port, type_):
        self.name = name
        self.host = host
        self.port = port
        self.type = type_

    def __getattr__(self, item):
        return None


# The fictional fleet from docs/ANONYMISATION.md, with RFC 5737 addresses.
# TWO servers, not one: the property under test in (1) is that each row
# targets ITS OWN server, and a one-server fixture cannot tell a per-row href
# apart from a constant one.
FLEET = [
    _Server("DC01", "192.0.2.11", 5985, "domain_controller"),
    _Server("FILE01", "192.0.2.12", 5986, "file_server"),
]


@pytest.fixture(scope="module")
def servers_html() -> str:
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader([
            # A minimal stand-in for base.html — everything under test lives
            # in the content block, and the real base needs a request context.
            # Same stub as tests/test_design_loading.py uses.
            jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            jinja2.FileSystemLoader(str(TEMPLATES)),
        ]),
        autoescape=True,
    )
    # next_tip_id stand-in, same reason and shape as tests/test_design_loading.py's
    # servers_html fixture: servers.html's manage-tags tip (WP-6 D9 close-out)
    # needs it in this stub context too.
    _tip_ids = (f"tip-test-{i}" for i in range(1000))
    return env.get_template("servers.html").render(
        t=_T(), next_tip_id=lambda: next(_tip_ids),
        csp_nonce="test", servers=FLEET, settings={},
        app_settings={}, max_compare_servers=4,
    )


_GOTO_ROW = re.compile(r"<tr\b[^>]*\bdata-action=\"goto\"[^>]*>", re.S)


def _rows(html: str) -> list[tuple[str, str]]:
    """Every `goto` row as (opening tag, row body). Rows do not nest, so the
    first `</tr>` after the opening tag closes it."""
    out = []
    for m in _GOTO_ROW.finditer(html):
        end = html.index("</tr>", m.end())
        out.append((re.sub(r"\s+", " ", m.group(0)), html[m.end():end]))
    return out


# ── (1) a row navigates to THAT server ────────────────────────────────────

def test_a_row_navigates_to_its_own_servers_overview(servers_html):
    """One row per server, each pointing at its own name.

    The failure this is written against is a row whose target is derived from
    its POSITION. table-sort.js reorders the tbodies in place, so a
    position-derived href is correct exactly until the first sort and then
    opens the wrong host — with no error and no visible difference."""
    rows = _rows(servers_html)
    assert len(rows) == len(FLEET), (
        f"expected one clickable row per server, found {len(rows)}")

    for server, (tag, body) in zip(FLEET, rows):
        href = re.search(r'data-href="([^"]*)"', tag)
        assert href, f"the row for {server.name} carries no data-href to follow"
        assert href.group(1) == f"/server/{server.name}", (
            f"the row showing {server.name} points at {href.group(1)!r}")
        # ...and it is the row that actually shows that server, not merely the
        # nth row. Without this the whole set could be shifted by one and the
        # assertion above would still pass for every row but the last.
        assert f">{server.name}</span>" in body, (
            f"the row targeting {server.name} does not display {server.name}")


def test_the_target_is_the_route_flask_actually_serves(servers_html):
    """`/server/<name>`, singular — the page is `/servers` and the detail
    route is `/server/`, which is one character apart and produces a 404 that
    renders the dashboard rather than an error."""
    views = (PROJECT_ROOT / "routes" / "views.py").read_text(encoding="utf-8")
    assert '@views_bp.route("/server/<name>")' in views, (
        "the server detail route has moved; the rows now point at nothing")
    for tag, _ in _rows(servers_html):
        assert re.search(r'data-href="/server/[^/"]+"', tag), (
            f"row target is not a /server/<name> URL: {tag}")


# ── (2) a click on a nested control does not navigate ─────────────────────

# Natively interactive, i.e. things a reader clicks ON PURPOSE for a reason
# other than "open this server". `<i>`/`<span>` are not here: they are inert,
# and a click on one is a click on the row, which is the whole feature.
_INTERACTIVE = re.compile(
    r"<(button|a|input|select|textarea|summary)\b[^>]*>", re.I | re.S)


def _unguarded_controls(body: str) -> list[str]:
    """Interactive elements inside a `goto` row that do NOT answer their own
    click, and so would be swallowed by the row.

    `e.target.closest('[data-action]')` walks UP from what was clicked and
    stops at the first carrier. A control with its own `data-action` is that
    carrier and the row is never reached; a control without one is not, and
    the row's `goto` runs instead of it."""
    return [re.sub(r"\s+", " ", m.group(0)) for m in _INTERACTIVE.finditer(body)
            if "data-action=" not in m.group(0)]


def test_no_control_inside_a_row_is_swallowed_by_the_row(servers_html):
    """The crux. Manage-tags and test-connection sit inside the clickable
    row; if the row ate their clicks they would silently become "go to the
    overview", which looks like the feature working."""
    offenders = []
    for tag, body in _rows(servers_html):
        for control in _unguarded_controls(body):
            offenders.append(f"{tag[:60]}…  ->  {control[:110]}")
    assert not offenders, (
        "interactive elements inside a clickable row with no `data-action` of "
        "their own — `closest` will resolve their clicks to the row and "
        "navigate instead of running them. Give the control its own action, "
        "or `data-action=\"stop-prop\"`:\n  " + "\n  ".join(offenders))


def test_the_swallowing_detector_sees_a_bare_button():
    """The guard on the guard (OPS-LEARNINGS #15): a detector that finds
    nothing is indistinguishable from a detector whose pattern is wrong. This
    is the exact shape the row's own buttons would have had if they had been
    written without the dispatch."""
    bad = '<td><button class="p-1" title="Test connection"><i></i></button></td>'
    assert _unguarded_controls(bad), (
        "the detector no longer sees an unguarded button inside a row")
    good = '<td><button data-action="testConnection" class="p-1"></button></td>'
    assert not _unguarded_controls(good), (
        "the detector now flags a control that answers its own click")


def test_the_dispatcher_still_resolves_a_click_to_the_nearest_carrier():
    """Everything above rests on this one line in base.html. If the dispatch
    ever walked ancestors as well — or bound the handler to each carrier and
    let the event bubble — every nested button in every clickable row in the
    app would fire its own action AND the row's."""
    base = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"const _evtAttr = \{[^}]*\};(.*?)\n      \}\);", base, re.S)
    assert m, "base.html's delegated dispatch has been reshaped"
    dispatch = m.group(1)
    assert "e.target.closest(" in dispatch, (
        "the dispatcher no longer resolves a click with `closest`, so a nested "
        "control's click can also reach the row that contains it")
    # One element, one action: the matched carrier is run and nothing walks
    # further up from it.
    assert dispatch.count("run(el, e,") == 1, (
        "the dispatch runs more than one element per event")


def test_the_tag_pills_rendered_into_a_row_answer_their_own_click():
    """The pills are built in JS and injected into the first cell, so the
    rendered-page scan above cannot see them — the container is empty until
    `loadServerTags` resolves. They sit INSIDE the clickable row, so the same
    rule applies to them and has to be checked where they are written."""
    js = _code_only(SERVERS.read_text(encoding="utf-8"))
    m = re.search(r"function renderServerTagPills\([^)]*\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "renderServerTagPills has been reshaped"
    assert 'data-action="_srvRemoveTag"' in m.group(1), (
        "a tag pill inside the clickable row no longer carries its own action; "
        "clicking one now opens the server instead of removing the tag")


# ── (3) keyboard and screen readers ───────────────────────────────────────

def test_the_row_is_reachable_by_tab_and_activatable(servers_html):
    """A clickable `<tr>` is mouse-only unless something gives it a tab stop —
    WCAG 2.1.1. base.html's bridge keys on `[data-action][tabindex]`, so the
    tabindex is both the tab stop and the thing that wires up Enter/Space."""
    rows = _rows(servers_html)
    assert rows, "no clickable rows rendered"
    for tag, _ in rows:
        assert 'tabindex="0"' in tag, (
            f"the row is clickable but not reachable by keyboard: {tag}")

    base = _code_only(BASE.read_text(encoding="utf-8"))
    bridge = base[base.index("addEventListener('keydown'", base.index("const NATIVE")):]
    assert "'[data-action][tabindex]'" in bridge[:900], (
        "the keyboard bridge no longer picks up `[data-action][tabindex]`, so "
        "the row is focusable and does nothing when activated")
    assert "'Enter'" in bridge[:600], "the bridge no longer handles Enter"


def test_the_row_announces_which_server_it_opens(servers_html):
    """WCAG 4.1.2. Focus lands on the row; without a name it announces the
    six cells and nothing about what Enter would do."""
    for server, (tag, _) in zip(FLEET, _rows(servers_html)):
        label = re.search(r'aria-label="([^"]*)"', tag)
        assert label, f"the clickable row for {server.name} has no accessible name"
        assert server.name in label.group(1), (
            f"the row's name, {label.group(1)!r}, does not identify {server.name}")
        # The name comes FIRST because the label is translated into five
        # locales and Japanese puts the verb last; an "Open overview for …"
        # prefix reads correctly in four of them.
        assert label.group(1).startswith(server.name), (
            "the server name is not the first thing announced")


def test_the_row_does_not_impersonate_a_link_or_a_button(servers_html):
    """Deliberate, and recorded here so it is not "fixed" later.

    A `<tr>` has the implicit role `row`, and ARIA requires a `row` inside a
    `rowgroup`/`table`. Overriding it to `link` or `button` removes the row
    from the table's accessibility tree and takes all six cells' column-header
    association with it — a bigger loss for a screen-reader user than the role
    name is a gain. The accessible name above carries the purpose instead, and
    the three clickable rows this app already had take the same shape."""
    for tag, _ in _rows(servers_html):
        role = re.search(r'\brole="([^"]*)"', tag)
        assert role is None or role.group(1) == "row", (
            f"the clickable row declares role={role.group(1)!r}, which removes "
            "it from the table's accessibility tree")


# ── the affordance, and the sort it must not disturb ──────────────────────

def test_the_row_looks_clickable_without_a_second_hover_treatment(servers_html):
    """`cursor-default` was on this row before — it said "not a control", and
    it is exactly the class a reader trusts.

    The hover tint is NOT re-stated: the row already had
    `hover:bg-page dark:hover:bg-page/50`, and those two exact classes are
    load-bearing beyond looks — the tag-pill ink solver in servers.html
    computes contrast against that composite (see tests/test_tag_ink.py), so a
    second or different hover here would make its measured ratios wrong."""
    for tag, _ in _rows(servers_html):
        assert "cursor-pointer" in tag, f"the row does not look clickable: {tag}"
        assert "cursor-default" not in tag
        assert "hover:bg-page" in tag and "dark:hover:bg-page/50" in tag, (
            "the pre-existing row hover has been replaced rather than kept; "
            "the tag-ink solver is measured against exactly these two classes")
        assert not re.search(r"#[0-9A-Fa-f]{3,6}\b", tag), (
            f"a raw hex on the row instead of a design token: {tag}")


def test_the_sortable_header_still_has_its_six_columns(servers_html):
    """The row gained attributes, not cells. tests/test_table_sort.py pins the
    `servers-list` sort vector to six columns; a change in cell count here
    would break the header/body alignment without touching that file."""
    head = servers_html[servers_html.index('data-sort-table="servers-list"'):]
    head = head[:head.index("</thead>")]
    assert head.count("<th") == 6
    for tag, body in _rows(servers_html):
        assert body.count("<td") == 6, (
            f"the clickable row has {body.count('<td')} cells, not 6")


def test_goto_declines_a_drag_select_and_a_modifier_click():
    """A row-sized target is also a text-selection target. The Host column is
    a monospace address an operator drags across to copy, and the mouseup that
    ends that drag IS a click — navigating away mid-copy is worse than the row
    not being clickable at all. Modifier clicks are left to the browser for
    the same reason: ctrl/cmd+click means "open it somewhere else"."""
    base = _code_only(BASE.read_text(encoding="utf-8"))
    m = re.search(r"'goto': function \([^)]*\) \{(.*?)\n        \},", base, re.S)
    assert m, "the `goto` action has been reshaped"
    body = m.group(1)
    assert "getSelection" in body, (
        "`goto` no longer checks for a live text selection, so selecting the "
        "host address in a clickable row navigates away instead of copying")
    assert "ctrlKey" in body and "metaKey" in body, (
        "`goto` no longer declines modifier clicks")
    guard = body.index("getSelection")
    assert guard < body.index("location.href"), (
        "the guards run after the navigation, which is no guard at all")
