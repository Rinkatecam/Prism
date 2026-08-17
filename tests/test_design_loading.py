"""Guardrails for the loading layer — skeletons and the settle animation.

Wave 3, task F. These exist because `.skeleton`, `.skeleton-text` and
`.data-settled` shipped in `app.css` last round with ZERO carriers in any
template: the rule was written and nothing referenced it. That is the
repository's most-repeated failure shape (docs/OPS-LEARNINGS.md §2), and a
stylesheet cannot notice it.

WHAT THESE TESTS ARE BLIND TO, stated up front because the other half of
that lesson is that a check which cannot see the bug still reports green:

  * They cannot see whether a ghost is the RIGHT SHAPE. A skeleton exists to
    be the same size as the thing it stands in for, and nothing here
    measures a pixel. That was measured in the browser against the running
    app — stat card 86px vs 86px, feed 402px vs 402px, server card 250px vs
    249px — and the numbers are recorded in `partials/_skeletons.html` so
    the next person can re-derive them rather than re-discover them.
  * They render the dashboard against a stub base template. A regression
    that lives in `base.html`'s own markup is outside their reach.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"


# ── rendering ────────────────────────────────────────────────────────────
#
# The dashboard is rendered, not grepped. Its ghosts come from macro calls
# (`{{ stat_cards() }}`), so a source scan for `class="skeleton"` would find
# nothing and pass for the wrong reason — and would go on passing if the
# macro were emptied out.

class _T:
    """Stands in for the i18n dict, which resolves anything asked of it."""

    def get(self, key, default=None):
        return default if default is not None else key

    def __getattr__(self, name):
        return name


@pytest.fixture(scope="module")
def dashboard_html() -> str:
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader([
            # A minimal stand-in for base.html: everything this test cares
            # about lives in the content block, and the real base needs a
            # request context we do not want to build here.
            jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            jinja2.FileSystemLoader(str(TEMPLATES)),
        ]),
        autoescape=True,
    )
    return env.get_template("dashboard.html").render(
        t=_T(), server_count=29, summary=None, csp_nonce="test",
        # The shape routes.views._estate_vitals returns. Spelled out rather
        # than imported so this stays a template test: importing the view
        # would drag in Flask, the config manager and a database handle to
        # render four cards' worth of markup.
        vitals={
            "ok": 27, "warn": 1, "bad": 1, "dead": 0, "unknown": 0,
            "monitored": 29, "percent": 93, "severity": "urgent", "bpm": 132,
            "servers": {"total": 29, "healthy": 27, "warning": 1,
                        "critical": 1, "offline": 0, "unknown": 0},
            "services": {"total": 0, "up": 0, "down": 0, "unknown": 0},
        },
    )


class _Regions(HTMLParser):
    """Collect every hx-get element with the depth of the nearest enclosing
    element whose class list contains `hidden`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.regions: list[dict] = []
        self._open: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        hidden = "hidden" in classes
        inside_hidden = hidden or any(h for _, h in self.stack)
        if a.get("hx-get"):
            region = {
                "hx_get": a["hx-get"],
                "trigger": a.get("hx-trigger") or "",
                "inside_hidden": inside_hidden,
                "depth": len(self.stack),
                "skeletons": 0,
                "aria_hidden_root": None,
            }
            self.regions.append(region)
            self._open.append(region)
        elif self._open and "skeleton" in classes:
            self._open[-1]["skeletons"] += 1
        if self._open and len(self.stack) == self._open[-1]["depth"] + 1:
            if self._open[-1]["aria_hidden_root"] is None:
                self._open[-1]["aria_hidden_root"] = a.get("aria-hidden") == "true"
        # Void elements never push a level.
        if tag not in ("br", "hr", "img", "input", "meta", "link", "source"):
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        while self.stack:
            popped, _ = self.stack.pop()
            if popped == tag:
                break
        while self._open and len(self.stack) <= self._open[-1]["depth"]:
            self._open.pop()


def _regions(html: str) -> list[dict]:
    p = _Regions()
    p.feed(html)
    return p.regions


def _load_triggered(regions):
    return [r for r in regions if re.search(r"\bload\b", r["trigger"])]


# Jinja comments, HTML comments, CSS comments. Every text-scanning check
# below strips these first, and the reason is worth recording: the first cut
# of two of these tests failed on `partials/_skeletons.html`, whose header
# comment explains the very rules they enforce and quotes the markup they
# forbid. A guardrail that cannot tell documentation from code will fire on
# its own explanation, and the cheapest way to make it green is to delete
# the explanation.
_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)


def _code_only(text: str) -> str:
    """Blank comments out rather than deleting them, so line numbers hold."""
    return _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


# ── the ghosts ───────────────────────────────────────────────────────────

def test_every_load_triggered_region_is_accounted_for(dashboard_html):
    """A region that fetches its own data on load either shows a ghost while
    it waits, or is one of the conditionally-hidden alert sections that
    deliberately shows nothing. There is no third case, and "somebody forgot"
    must not be able to masquerade as one of the first two."""
    regions = _load_triggered(_regions(dashboard_html))
    # 8 -> 7 with the dashboard redesign, and the floor is here so that a
    # region silently disappearing cannot make "every region is accounted
    # for" true by emptying the page. Two regions left and one arrived:
    # `/partials/status-overview` was deleted (the quadrant carries its
    # numbers) and `/partials/server-grid` moved to /servers, against
    # `/partials/vitals` arriving. Verdict-header is not counted at all —
    # it refreshes on prismRefresh but paints server-side via {% include %},
    # so it never has a loading state to cover.
    assert len(regions) >= 7, (
        f"expected the dashboard's load-triggered regions, found {len(regions)}; "
        "if the page was restructured this test is now measuring the wrong thing")

    unaccounted = [r["hx_get"] for r in regions
                   if r["skeletons"] == 0 and not r["inside_hidden"]]
    assert not unaccounted, (
        "these regions fetch on load, are visible from first paint, and show "
        "nothing while they wait:\n  " + "\n  ".join(unaccounted))


def test_the_hidden_alert_sections_deliberately_have_no_ghost(dashboard_html):
    """The inverse, asserted rather than assumed. A ghost inside a section
    that starts `hidden` is invisible, so it buys nothing; unhiding the
    section to show one would promise a panel that usually resolves to
    nothing and disappears again. If somebody adds a skeleton there, they
    should have to argue with this test first."""
    over_eager = [r["hx_get"] for r in _load_triggered(_regions(dashboard_html))
                  if r["inside_hidden"] and r["skeletons"] > 0]
    assert not over_eager, (
        "a hidden alert section grew a skeleton; it cannot be seen, and "
        "revealing the section to show it would flash an empty panel:\n  "
        + "\n  ".join(over_eager))


def test_a_ghost_is_hidden_from_assistive_technology(dashboard_html):
    """A skeleton has nothing to say to a screen reader. Every ghost root is
    aria-hidden, so the region reads as empty until real content lands
    rather than announcing a fistful of blank boxes."""
    exposed = [r["hx_get"] for r in _load_triggered(_regions(dashboard_html))
               if r["skeletons"] > 0 and not r["aria_hidden_root"]]
    assert not exposed, (
        "ghost markup is exposed to assistive technology in:\n  "
        + "\n  ".join(exposed))


def test_the_old_pulse_placeholders_are_gone(dashboard_html):
    """The placeholders these replaced were `animate-pulse` over raw
    `bg-gray-200 dark:bg-gray-700` — outside the token system entirely, and
    an opacity pulse on up to eight cards where a background sweep
    composites. Neither may come back."""
    assert "animate-pulse" not in dashboard_html
    stray = sorted(set(re.findall(r"bg-gray-\d{2,3}", dashboard_html)))
    assert not stray, f"raw Tailwind greys back in the dashboard: {stray}"


def test_skeleton_text_is_never_used_without_skeleton():
    """`.skeleton-text` sets height and margin ONLY — the background and the
    sweep live on `.skeleton`. A lone `class="skeleton-text"` is an invisible
    0.75em box that holds space and shows nothing, which looks exactly like
    a region that failed to load. Measured in the browser: transparent
    background, no animation."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        code = _code_only(path.read_text(encoding="utf-8"))
        for i, line in enumerate(code.splitlines(), 1):
            for m in re.finditer(r'class="([^"]*\bskeleton-text\b[^"]*)"', line):
                if "skeleton" not in m.group(1).split():
                    rel = path.relative_to(TEMPLATES).as_posix()
                    offenders.append(f"{rel}:{i}: {m.group(1)}")
    assert not offenders, (
        "`skeleton-text` without `skeleton` renders an invisible box:\n  "
        + "\n  ".join(offenders))


# ── the settle ───────────────────────────────────────────────────────────

def test_data_settled_is_never_placed_on_swapped_content():
    """`.data-settled` fades content in once. These regions re-swap on every
    prismRefresh — roughly every 5s under collector v2 — so the class has to
    sit on the CONTAINER, which `morph:innerHTML` never replaces. Putting it
    on the content makes the fade re-run forever and turns a settle into a
    flicker.

    That is not hypothetical: base.html carries a comment about the healthy
    banner re-arming its dismiss timer every few seconds for exactly this
    reason. State that lives in swapped DOM is state that resets."""
    offenders = []
    for path in sorted((TEMPLATES / "partials").rglob("*.html")):
        if "data-settled" in _code_only(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(TEMPLATES).as_posix())
    assert not offenders, (
        "`data-settled` found in a swapped-in partial; it belongs on the "
        "container, which the swap does not replace:\n  " + "\n  ".join(offenders))


def test_the_settle_is_keyed_on_a_ghost_having_been_there():
    """The fade should mark the moment a ghost became real, not every swap.
    The wiring detects the outgoing ghost in `htmx:beforeSwap` and only then
    adds the class in `htmx:afterSwap`.

    Blind spot, stated: this checks that the guard is READ before the class
    is added. It cannot see an inverted guard, and it cannot see behaviour.
    The behaviour was measured in the browser — one `data-in` animation on
    the ghost->data swap, none on two subsequent data->data swaps.

    The first version of this test asserted only that the strings
    `data-was-ghost` and `classList.add('data-settled')` both appeared
    somewhere in the file. Deleting the guard line outright left both true
    and the test green. It was caught by mutation, not by reading it."""
    base = _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))

    adds = [m.start() for m in re.finditer(r"classList\.add\(\s*'data-settled'\s*\)", base)]
    assert len(adds) == 1, (
        f"expected exactly one place to add `data-settled`, found {len(adds)}")

    assert re.search(r"htmx:beforeSwap[\s\S]{0,400}querySelector\(\s*'\.skeleton'\s*\)", base), (
        "nothing detects the outgoing ghost, so the guard can never be set")

    # The guard must be read, and read BEFORE the add, and close enough to it
    # to be the same handler rather than a coincidence elsewhere in the file.
    guard = None
    for m in re.finditer(r"getAttribute\(\s*'data-was-ghost'\s*\)[^\n]*return", base):
        if m.end() < adds[0]:
            guard = m
    assert guard is not None, (
        "`data-settled` is added without first checking `data-was-ghost`, so "
        "the fade will re-run on every prismRefresh — a flicker, not a settle")
    gap = adds[0] - guard.end()
    assert gap < 400, (
        f"the ghost guard is {gap} chars from the class it guards; that is not "
        "the same handler, so it is not actually guarding anything")


# ── the stylesheet ───────────────────────────────────────────────────────

_DEF = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
_USE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")


def test_no_custom_property_is_read_without_being_defined():
    """`.skeleton` was written as `border-radius: var(--radius-sm, 0.5rem)`
    and `--radius-sm` is defined nowhere in the project. It rendered at the
    right 8px the whole time, because the fallback was doing all the work —
    so nothing looked wrong, and the declaration pointed a reader at a token
    that does not exist.

    That is the same trap Phase 2 removed from motion, where a template's
    `duration-200` and a stylesheet's `0.2s` were independent numbers that
    happened to agree. A value that is correct by coincidence stops being
    correct the moment somebody defines the thing it names.

    Comments are stripped first: the token header discusses `var(--c-x)` as
    prose, and matching that would make this test permanently red for a
    reason that has nothing to do with the stylesheet."""
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    defined = set(_DEF.findall(css))
    # A handful are defined in base.html's Tailwind config rather than here.
    defined |= set(_DEF.findall(
        _code_only((TEMPLATES / "base.html").read_text(encoding="utf-8"))))
    dangling = sorted(set(_USE.findall(css)) - defined)
    assert not dangling, (
        "read but never defined — the fallback (or nothing) is doing the "
        f"work: {dangling}")
