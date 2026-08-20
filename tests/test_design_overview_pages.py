"""WP-3's three overview pages: /services, /network, /scan.

Two of the three describe an ABSENCE, and that is the whole difficulty. The
easy way to build a page for something nobody monitors is to build the page
you would build if they did and let it render zeroes — four stat tiles at 0,
an empty table with headers, a skeleton that never settles. Every one of
those is a lie in a different tense: a zero is a measurement, a table header
is a promise of rows, a skeleton is a claim that content is coming.

So most of what follows is negative. It asserts what these pages must NOT
contain, because nothing about them failing would look like a failure — a
network page showing "0 devices" reads as a healthy network.

The third page, /services, has the opposite risk: it is the first surface in
the application to list health-check probes, and its counts come from a
DIFFERENT accessor than its rows (see tests/test_health_check_overview.py for
why that is deliberate). An operator arrives here by clicking the dashboard
card that carries the same four numbers. If the two ever disagree, the page
that was supposed to explain the card contradicts it.

WHAT THESE ARE BLIND TO:

  * Rendering. These parse template SOURCE. Whether the page paints, and what
    contrast it paints at, is measured against the running app with the
    browser tools — a source assertion cannot see a cascade defect.
  * The English fallbacks. `t.get(key, 'English')` is unreachable once the key
    exists in `en`, which the first test below requires; the fallback text
    itself is therefore never rendered and is not compared.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"

SERVICES = TEMPLATES / "services.html"
NETWORK = TEMPLATES / "network.html"
SCAN = TEMPLATES / "scan.html"
SERVICES_TABLE = TEMPLATES / "partials" / "services_table.html"

NEW_PAGES = (SERVICES, NETWORK, SCAN, SERVICES_TABLE)
ABSENCE_PAGES = (NETWORK, SCAN)


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Jinja comments blanked, so an assertion cannot be satisfied — or
    defeated — by prose that merely mentions the thing under test.

    This is OPS-LEARNINGS #36 in its template form: a check silenced by its
    own documentation. These files argue their decisions in `{# #}` blocks at
    some length, and several of those blocks contain the exact words the
    tests below search for.
    """
    return re.sub(r"\{#.*?#\}", " ", text, flags=re.S)


def _keys_used(text: str) -> set[str]:
    """Every translation key the template asks for, from the source."""
    body = _code_only(text)
    return set(re.findall(r"t\.get\(\s*'([a-z0-9_]+)'", body))


ALL_KEYS = sorted(set().union(*(_keys_used(_src(p)) for p in NEW_PAGES)))


# ── the translations, which is where this shape of work fails ────────────

def test_the_pages_actually_ask_for_translations():
    """Guard on the guard. Every test below is driven by `ALL_KEYS`, which is
    scraped from the templates — so a scrape that silently returned nothing
    would make the locale coverage tests vacuously true, which is exactly the
    'saw nothing bad' vs 'saw nothing' distinction the tooling rules name."""
    assert len(ALL_KEYS) >= 15, f"only found {len(ALL_KEYS)} keys: {ALL_KEYS}"


def test_every_string_on_these_pages_exists_in_every_locale():
    """OPS-LEARNINGS #39. `t.get` cannot fail, so a key present in no locale
    renders English to every non-English operator and every test still
    passes. Three new pages is three new chances to repeat it."""
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in ALL_KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated overview-page strings: " + ", ".join(missing)


def test_no_locale_silently_carries_the_english_sentence():
    """The same defect with an extra step: the key exists, and its value is
    the English text. Checked on the long sentences, where a coincidental
    match between two languages cannot happen — a one-word label like
    'Target' legitimately collides."""
    from i18n import TRANSLATIONS
    sentences = [k for k in ALL_KEYS
                 if len(TRANSLATIONS["en"].get(k, "")) > 40]
    assert len(sentences) >= 5, f"too few long strings to check: {sentences}"
    offenders = []
    for key in sentences:
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            if TRANSLATIONS[lang].get(key) == english:
                offenders.append(f"{lang}:{key}")
    assert not offenders, "English text sitting in a locale: " + ", ".join(offenders)


# ── the two pages about an absence ───────────────────────────────────────

@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_renders_no_number(path):
    """A zero is a MEASUREMENT. `0 devices` on a page where nothing is
    measured is the most believable wrong thing this package could ship:
    it reads as a healthy network rather than as an unbuilt feature.

    Matches standalone digits in the rendered body — headings, figures,
    counts — while tolerating them inside class names, where `gap-3` and
    `w-5` are geometry."""
    body = _code_only(_src(path))
    # Strip attributes; what is left is what a reader sees.
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\{\{.*?\}\}", " ", text, flags=re.S)
    digits = re.findall(r"(?<![\w-])\d+(?![\w-])", text)
    assert not digits, f"{path.name} renders bare numbers: {digits}"


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_has_no_table(path):
    """An empty table with headers promises rows. There are none coming."""
    assert "<table" not in _code_only(_src(path)), (
        f"{path.name} has a table for data that is not collected")


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_carries_no_skeleton(path):
    """The standing rule, applied in the direction that is easy to get
    wrong: a skeleton is a claim that content is coming. On these two,
    nothing is coming — not slowly, not at all."""
    body = _code_only(_src(path))
    assert "skeleton" not in body.lower(), f"{path.name} ghosts content that never arrives"
    assert "_skeletons.html" not in body


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_fetches_nothing(path):
    """No htmx, no fetch, no refresh trigger. A page with a live region for
    data nobody collects eventually renders something, and whatever it
    renders will be wrong."""
    body = _code_only(_src(path))
    for marker in ("hx-get", "hx-trigger", "fetch(", "prismRefresh"):
        assert marker not in body, f"{path.name} carries {marker}"


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_says_so_in_words(path):
    """Stated once, plainly, rather than implied by emptiness. The shared
    key is the same on both pages so the product says one thing."""
    assert "overview_not_collected" in _code_only(_src(path))


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_a_page_about_an_absence_offers_somewhere_to_go(path):
    """The empty-state rule generalised: what is absent, and what to do
    instead. A page that only says 'not yet' is a dead end, and an operator
    reached it by clicking something."""
    body = _code_only(_src(path))
    hrefs = set(re.findall(r'href="(/[a-z/]*)"', body))
    assert hrefs, f"{path.name} names no route onward"
    assert "overview_today" in body, (
        f"{path.name} lists links without saying what they are for")


@pytest.mark.parametrize("path", ABSENCE_PAGES, ids=lambda p: p.name)
def test_coming_soon_did_not_follow_the_card_onto_the_page(path):
    """On the quadrant card that phrase justified an inert control. On a
    page it reads as a delivery date, and none has been given."""
    body = _code_only(_src(path)).lower()
    assert "coming soon" not in body
    assert "vitals_coming_soon" not in body


# ── /services: the page must not contradict the card that links to it ────

def test_the_services_counts_come_from_the_summary_not_from_the_rows():
    """The rows include switched-off probes; the summary excludes them. If
    the page counted its own rows, its figures would exceed the dashboard
    card's by exactly the number of disabled probes — a discrepancy with no
    visible cause, on the page whose job is to explain the card."""
    body = _code_only(_src(SERVICES_TABLE))
    for field in ("summary.total", "summary.up", "summary.down", "summary.unknown"):
        assert field in body, f"the figure block does not read {field}"
    assert "live | length" not in body.split("<div class=\"grid")[1].split("</div>")[0] \
        if "<div class=\"grid" in body else True


def test_the_disabled_probes_are_outside_the_counted_table():
    """Shown — this is the only surface where they are visible at all — but
    in their own section, because they are outside the four figures above.
    Same tbody would make the row count disagree with the figure."""
    body = _code_only(_src(SERVICES_TABLE))
    assert "rejectattr('enabled')" in body, "the page does not separate switched-off probes"
    assert "services_switched_off_note" in body, (
        "the switched-off group does not say it is uncounted")


def test_a_failed_read_is_not_reported_as_an_empty_estate():
    """The two states produce the same empty list, and only one of them is
    about the operator. Rendering "No health checks configured" over a failed
    read has Prism assert something it does not know — this repository's
    most-repeated defect, moved into the UI layer.

    Asserted on the ORDER of the branches, not merely on the key existing: an
    `{% if probes | length == 0 %}` that comes first swallows the failure
    case whatever else the template goes on to say."""
    body = _code_only(_src(SERVICES_TABLE))
    fail_at = body.find("{% if not readable %}")
    empty_at = body.find("{% elif probes | length == 0 %}")
    assert fail_at != -1, "the template does not branch on the read having failed"
    assert empty_at != -1, "the empty state is no longer the second branch"
    assert fail_at < empty_at
    assert "services_unreadable" in body


def test_the_failure_panel_offers_no_hint():
    """An empty state's third part is what to do about it. Here there is
    nothing for the operator to do — the fault is Prism's — and a hint would
    send them to configure a probe in response to a database error."""
    body = _code_only(_src(SERVICES_TABLE))
    panel = body[body.find("{% if not readable %}"):body.find("{% elif probes")]
    assert "empty_state(" not in panel
    assert "vitals_no_services_hint" not in panel


def test_the_context_reports_whether_the_read_succeeded():
    """The flag has to reach the template from the view, and the view must
    not infer it from the list being empty — which is the thing it exists to
    distinguish."""
    import inspect
    from routes import views
    src = inspect.getsource(views._services_context)
    assert '"readable": readable' in src
    assert src.count("readable = False") == 2, (
        "both reads must be able to clear the flag")


def test_the_services_region_is_painted_before_it_is_refreshed():
    """Server-rendered on first paint via include, then swapped on
    prismRefresh — the verdict header's pattern. A `load` trigger would blank
    a region that is already correct, and this page deliberately has no
    skeleton to cover the gap that would open."""
    body = _code_only(_src(SERVICES))
    assert '{% include "partials/services_table.html" %}' in body
    assert 'hx-trigger="prismRefresh from:body"' in body
    assert "load," not in body and 'hx-trigger="load' not in body, (
        "a load trigger with no skeleton flashes an empty region")


def test_the_services_page_does_not_configure_anything():
    """WP-4's rule, binding on new pages from the start: a page either shows
    state or configures. The only interactive things here are links."""
    body = _code_only(_src(SERVICES)) + _code_only(_src(SERVICES_TABLE))
    for tag in ("<form", "<input", "<select", "<textarea", "<button"):
        assert tag not in body, f"/services carries {tag} — it is a state page"


def test_the_route_to_configuration_is_named_and_exists():
    """A state page that does not say where its state comes from is the dead
    end an empty state without a hint is.

    And the destination is checked rather than assumed: the hint this
    replaces pointed at Operations, where health checks have never lived."""
    body = _code_only(_src(SERVICES))
    assert 'href="/servers#health-checks"' in body, (
        "/services does not name where probes are configured")
    servers = _src(TEMPLATES / "servers.html")
    assert 'id="health-checks"' in servers, (
        "the anchor /services links to does not exist on /servers")


def test_no_surface_still_sends_the_operator_to_operations_for_a_probe():
    """The stale hint, in the one place it can still hide: the locale table.
    It survived in all five languages because `t.get` returned it happily and
    nothing rendered a 404 — the operator just arrived at a page with no such
    section and concluded the feature was missing.

    BOTH hints, because there were two. The quadrant's whole-region empty
    state carried the same wrong destination as the Services card's, and
    fixing one and not the other is how a correction half-lands: the surviving
    copy reads like the deliberate one."""
    from i18n import TRANSLATIONS
    keys = ("vitals_no_services_hint", "vitals_nothing_monitored_hint")
    wrong = ("Operations", "Opérations", "Operaciones", "Betrieb", "運用")
    for lang, table in TRANSLATIONS.items():
        for key in keys:
            hint = table.get(key, "")
            assert hint, f"{lang}:{key} is missing entirely"
            found = [w for w in wrong if w in hint]
            assert not found, (
                f"{lang}:{key} still points at {found[0]} for a health check")


# ── shared: nothing here animates, and nothing here is disabled ──────────

@pytest.mark.parametrize("path", NEW_PAGES, ids=lambda p: p.name)
def test_the_new_pages_add_no_motion(path):
    """Asserted rather than assumed. Nothing on these pages needs to move,
    so nothing does — and the reduced-motion argument is therefore not
    'it is honoured' but 'there is nothing to honour'. A future animation
    lands with this test in front of it."""
    body = _code_only(_src(path))
    for marker in ("animate-", "@keyframes", "transition-transform"):
        assert marker not in body, f"{path.name} animates ({marker})"


@pytest.mark.parametrize("path", NEW_PAGES, ids=lambda p: p.name)
def test_the_new_pages_carry_no_inline_style(path):
    """The restart overlay's lesson: the colour-literal ratchet matches
    Tailwind arbitrary-value utilities and is blind to `style="color:#..."`
    and to `el.style.background = '#...'`. Nineteen literals hid in that gap
    on one component. The cheapest defence on a NEW file is to have no inline
    style at all."""
    body = _code_only(_src(path))
    assert "style=" not in body, f"{path.name} carries an inline style"
    assert ".style." not in body, f"{path.name} sets style from script"


@pytest.mark.parametrize("path", NEW_PAGES, ids=lambda p: p.name)
def test_the_new_pages_run_no_script(path):
    """No script means no innerHTML, no colour assignment, no locale-less
    string built in JS — three of WP-3's earlier defects in one class. If a
    page here ever needs script, this test is the place that decision gets
    made deliberately."""
    assert "<script" not in _code_only(_src(path)), f"{path.name} runs script"
