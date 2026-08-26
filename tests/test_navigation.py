"""The main navigation — WP-4 D7.

The nav was ordered by the age of its pages: Dashboard, Reports, Topology,
then the fleet. An operator's day starts at the fleet and reaches Reports
occasionally, so the order was backwards for everyone who uses it daily. D7
puts the daily path first and the reference surfaces after it.

Reordering meant reading those lines, and reading them turned up two defects
older than this slice:

  * **Settings was never highlighted on its own sub-pages.** The check was
    `request.path == '/settings'`, written when Settings was a single page.
    D1 gave it nine sub-pages, and every one of them has rendered with
    nothing active in the sidebar since — the nav telling the operator they
    are not on the page they are on. Servers already handled its detail
    pages; Settings was the one left behind.

  * **Nine hardcoded English aria-labels, in the app's primary navigation.**
    An aria-label REPLACES the visible text for assistive technology, so a
    German operator saw "Einstellungen" and heard "Settings". With the
    sidebar collapsed the visible label is hidden entirely and the aria-label
    is the only name there is.

WHAT THIS FILE IS BLIND TO:

  * Whether the ORDER is the right one. It pins the order the plan specifies
    so it cannot drift silently; it cannot tell you the plan was right.
  * Anything below the nav — the user block, collapse toggle and logout are
    a separate region and keep their own tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent / "templates" / "base.html"


# The order WP4_INFORMATION_ARCHITECTURE.md §4 specifies. Compliance is not in
# that list because the plan expected it to leave the nav; D6 kept it (it is a
# view, not configuration) and placed it with the other reference surfaces.
_ORDER = ["/", "/servers", "/monitoring", "/operations", "/workflows",
          "/reports", "/topology", "/settings"]


@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


def _nav(body: str) -> str:
    m = re.search(r'<nav class="flex-1.*?</nav>', body, re.S)
    assert m, "the sidebar nav is gone"
    return m.group(0)


def _links(nav: str) -> list[str]:
    return re.findall(r'href="(/[^"]*)" class="sidebar-link', nav)


def test_the_nav_is_in_the_order_the_plan_specifies(client):
    """Dashboard, then the fleet and what is happening to it, then the things
    you act with, then the reference surfaces, then Settings."""
    assert _links(_nav(client.get("/").get_data(as_text=True))) == _ORDER


def test_the_order_does_not_depend_on_which_page_you_are_on(client):
    """A nav that reorders itself is a nav nobody can build muscle memory
    for."""
    for path in ("/", "/servers", "/monitoring", "/settings/general", "/reports"):
        assert _links(_nav(client.get(path).get_data(as_text=True))) == _ORDER, (
            f"the nav order changed on {path}")


# ── the active state ──────────────────────────────────────────────────────

def test_every_nav_entry_marks_itself_active_on_its_own_page(client):
    for path in _ORDER:
        nav = _nav(client.get(path).get_data(as_text=True))
        active = re.findall(
            r'href="(/[^"]*)" class="sidebar-link sidebar-link-active', nav)
        assert active == [path], (
            f"{path} highlighted {active or 'nothing'} in the sidebar")


def test_settings_stays_active_across_its_sub_pages(client):
    """The defect this slice found. `request.path == '/settings'` is false on
    every one of the nine sections, so the sidebar showed nothing active —
    for the whole of D1 through D6."""
    from routes.views import _SETTINGS_SECTIONS
    for section in _SETTINGS_SECTIONS:
        path = f"/settings/{section}"
        nav = _nav(client.get(path).get_data(as_text=True))
        active = re.findall(
            r'href="(/[^"]*)" class="sidebar-link sidebar-link-active', nav)
        assert active == ["/settings"], (
            f"{path} highlighted {active or 'nothing'}")


def test_a_server_detail_page_keeps_servers_active(client):
    """The precedent Settings should have followed. Asserted on the source
    rather than a live server name, which not every install has."""
    src = _BASE.read_text(encoding="utf-8")
    assert "request.path.startswith('/server/')" in src, (
        "server detail pages no longer highlight Servers")


# ── the labels ────────────────────────────────────────────────────────────

def test_no_nav_entry_says_one_thing_and_announces_another():
    """An aria-label replaces the visible text for assistive technology. A
    hardcoded English one on a translated link makes the app show one word
    and say a different one — and with the sidebar collapsed, the spoken one
    is the only one."""
    src = _BASE.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    nav = _nav(src)
    bare = re.findall(r'aria-label="([A-Za-z][^"{]*)"', nav)
    assert not bare, f"hardcoded English aria-labels in the main nav: {bare}"


def test_each_entry_announces_exactly_its_visible_label():
    """Not merely 'translated' — the SAME string. Two independent lookups
    drift the moment one of them is renamed."""
    src = _BASE.read_text(encoding="utf-8")
    nav = _nav(src)
    entries = re.findall(
        r'aria-label="\{\{ (\w+) \}\}".*?<span class="sidebar-label">\{\{ (\w+) \}\}</span>',
        nav, re.S)
    assert len(entries) == len(_links(nav)), (
        f"{len(entries)} entries pair an aria-label with its label, "
        f"{len(_links(nav))} links exist")
    for aria, label in entries:
        assert aria == label, (
            f"the entry announces {aria} but shows {label}")


# ── D7's other half: what moved, and whether anything still points at it ──

def test_every_internal_link_in_every_template_resolves(client):
    """D7's brief is "301s for every route that moved". The honest way to find
    out what moved is to ask what the app still links to that it can no longer
    serve, rather than to reason from memory about which slice moved what.

    The answer for WP-4 turned out to be: nothing. The package moved CONTROLS
    between sections, not PAGES between URLs — the one URL that changed was
    RBAC's, and D4 left a 301 behind it. This test is what keeps that true."""
    import app as prism_app
    root = Path(__file__).resolve().parent.parent

    rules = []
    for rule in prism_app.app.url_map.iter_rules():
        rules.append(re.compile("^" + re.sub(r"<[^>]+>", "[^/]+", rule.rule) + "$"))

    dead = {}
    for tpl in sorted(root.glob("templates/**/*.html")):
        src = tpl.read_text(encoding="utf-8")
        src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
        for m in re.finditer(r'href="(/[^"#?]*)', src):
            href = m.group(1)
            if "{{" in href or "{%" in href:
                continue  # built at render time; not checkable statically
            if not any(rx.match(href) for rx in rules):
                dead.setdefault(href, set()).add(tpl.relative_to(root).as_posix())

    assert not dead, "links to routes the app does not serve:\n  " + "\n  ".join(
        f"{h} <- {', '.join(sorted(f))}" for h, f in sorted(dead.items()))


def test_the_url_that_did_move_still_redirects(client):
    """RBAC is the only page in WP-4 whose address changed."""
    r = client.get("/admin/rbac")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/settings/rbac")
