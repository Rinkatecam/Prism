"""Settings → Servers — WP-4 D4a.

The slice the plan specified, the board skipped, and D8's index crawl found:
`/servers` was still carrying Tag Management, Health Checks, Dependencies and
the add/edit/delete controls — all configuration, on the page WP-4 exists to
make view-only.

D4a is the first piece: TAG MANAGEMENT. The interesting part is that the line
runs THROUGH the tag feature rather than around it, which is the shape D3b hit
with runbooks:

  * creating, renaming, recolouring and deleting a tag is configuration and
    moves to Settings;
  * the pills on the server cards and the assign menu are how an operator
    READS and WORKS the fleet, and stay on /servers.

They never shared state — the assign menu reads `_allTags`, filled by
`loadServerTags`, not by the management loader. That was checked rather than
assumed, because "a name that resolves on the page it was written for and not
on the page it ends up serving" is a defect this package has now produced
five times. The sixth was caught here by the bootstrap guard: `loadTags()`
stayed in /servers' initialiser after its function left, which is a
ReferenceError that would have aborted `loadHealthChecks`, `loadDepsTable` and
`loadServerTags` behind it.

WHAT IS STILL OUTSTANDING (D4b, D4c): health checks, dependencies, and the
add/edit/delete server modals. `/servers` is not view-only yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PARTIAL = _ROOT / "templates" / "partials" / "settings" / "_servers.html"
_SERVERS = _ROOT / "templates" / "servers.html"

_MOVED = ["loadTags", "createTag", "editTag", "saveEditTag", "deleteTag"]
_STAYED = ["loadServerTags", "renderServerTagPills", "_tagReadableInk",
           "showTagAssign", "assignTagToServer"]


@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


def _code(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    return re.sub(r"^[ \t]*//[^\n]*", " ", src, flags=re.M)


# ── the move ──────────────────────────────────────────────────────────────

def test_the_servers_section_exists_and_renders(client):
    from routes.views import _SETTINGS_SECTIONS
    assert "servers" in _SETTINGS_SECTIONS
    r = client.get("/settings/servers")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-settings-section="servers"' in body
    assert "Something went wrong" not in body
    assert 'id="new-tag-name"' in body


def test_tag_management_left_the_servers_page():
    code = _code(_SERVERS)
    still = [f for f in _MOVED if re.search(r"\bfunction " + f + r"\b", code)
             or re.search(r"window\." + f + r"\s*=", code)]
    assert not still, f"tag management functions still defined on /servers: {still}"
    assert "SECTION 1b" not in code, "the tag management markup is still there"


def test_the_display_side_did_not_go_with_it():
    """The half of the feature that belongs on /servers. A move that took
    these would have stripped the pills off every server card, and the page
    would still have rendered."""
    code = _code(_SERVERS)
    # The DEFINITION, not the name. `renderServerTagPills in code` is true
    # when the function has been renamed, because its call sites still say it
    # — and a call site whose definition has gone is the exact bug this test
    # is here to catch, so matching one is worse than useless.
    missing = [f for f in _STAYED
               if not re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)
               and not re.search(r"window\." + re.escape(f) + r"\s*=", code)]
    assert not missing, f"display-side tag code left /servers: {missing}"


def test_the_partial_defines_exactly_the_moved_functions():
    code = _code(_PARTIAL)
    missing = [f for f in _MOVED if f"window.{f} =" not in code]
    assert not missing, f"not defined in the settings partial: {missing}"
    strays = [f for f in _STAYED if f"function {f}" in code]
    assert not strays, f"display-side code followed the move: {strays}"


def test_no_bootstrap_call_is_left_behind_on_servers():
    """The exact defect this move produced, and the guard that caught it
    before the browser did. `loadTags()` sat in /servers' initialiser after
    its function left — a ReferenceError that aborts every initialiser after
    it, so health checks, dependencies and the tag pills would all have
    silently stopped loading."""
    code = _code(_SERVERS)
    assert not re.search(r"^\s*loadTags\(\)", code, re.M), (
        "loadTags() is still called on /servers, where it no longer exists")


# ── the defects found in the code the move touched ───────────────────────

def test_the_assign_menu_no_longer_points_at_a_section_that_moved():
    """It said "Create tags first in the Tag Management section below" — true
    until this slice, then quietly wrong. The third stale in-page pointer this
    package has found, so it is checked rather than hoped for."""
    code = _code(_SERVERS)
    assert "Tag Management section below" not in code
    assert "tags_none_yet" in code, (
        "the empty-tags message is not read from the locale table")


def test_tag_names_and_colours_are_escaped_in_the_assign_menu():
    """Tag names and colours are operator input, and the assign menu built
    them straight into innerHTML while the management view three hundred lines
    away escaped both. A tag named with markup executed it; a colour is
    interpolated into a `style` attribute."""
    code = _code(_SERVERS)
    m = re.search(r"function showTagAssign.*?\n\}", code, re.S)
    assert m, "showTagAssign is gone"
    body = m.group(0)
    raw = re.findall(r"\$\{(t\.(?:name|color))\}", body)
    assert not raw, f"unescaped operator input in the assign menu: {raw}"


def test_the_popups_carry_no_raw_colour_literals():
    """Both script-built popups on this page hardcoded #1E293B / #334155 /
    #CBD5E1 — invisible to the colour ratchet, which reads templates, and a
    string assembled at runtime is not one. They also did not follow the
    theme: the values are dark-mode colours, painted in both."""
    code = _code(_SERVERS)
    for fn in ("showTagAssign", "showDepBrowsePopup"):
        m = re.search(r"function " + fn + r".*?\n\}", code, re.S)
        if not m:
            continue
        hits = [h for h in re.findall(r"#[0-9a-fA-F]{6}\b", m.group(0))
                if h.upper() != "#6B7280"]
        # #6B7280 is the fallback for a tag that has NO colour - operator data
        # with a default, not a UI colour, and the same value the colour input
        # starts on. Exempted by value with a reason rather than by loosening
        # the pattern until it excuses the next real one too.
        assert not hits, f"{fn} still carries raw colour literals: {hits}"


# ── the strings ───────────────────────────────────────────────────────────

_KEYS = ["tag_name_placeholder", "tag_colour", "no_tags_hint",
         "tag_name_required", "tag_create_failed", "tag_save_failed",
         "tag_delete_failed", "tag_delete_confirm", "tags_none_yet"]


def test_every_string_exists_in_every_locale():
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in _KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated tag strings: " + ", ".join(missing)


def test_no_locale_silently_reuses_the_english_text():
    from i18n import TRANSLATIONS
    for key in ("no_tags_hint", "tag_delete_confirm", "tags_none_yet"):
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, f"{lang}:{key} is English"


def test_the_delete_confirmation_names_the_tag_in_every_language():
    """Word order is the first thing that stops surviving translation, so the
    one sentence with a variable is a template with a named placeholder."""
    from i18n import TRANSLATIONS
    for lang, table in TRANSLATIONS.items():
        assert "{name}" in table["tag_delete_confirm"], (
            f"{lang} dropped the placeholder")


def test_the_section_carries_no_hardcoded_english():
    src = _PARTIAL.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    markup = src[:src.index("<script")]
    for attr in ("placeholder", "aria-label", "title"):
        bare = re.findall(attr + r'="([A-Za-z][^"{]*)"', markup)
        assert not bare, f"hardcoded English {attr}: {bare}"


def test_the_empty_state_is_the_shared_one():
    """The version that moved here was hand-rolled, and its hint ("Use the
    form above to create your first tag") had never been offered for
    translation."""
    code = _code(_PARTIAL)
    assert "prismEmptyState(" in code, "the shared empty state is not used"
    # `_code`, not `read_text`: this file's own comment quotes the string it
    # is banning, so the raw version reported the documentation as the defect
    # (OPS-LEARNINGS #36).
    assert "Use the form above" not in code


# ══════════════════════════════════════════════════════════════════════════
# D4b — health checks, and the anchor that could not be redirected
# ══════════════════════════════════════════════════════════════════════════

_HEALTH = _ROOT / "templates" / "partials" / "settings" / "_health_checks.html"

_HC_MOVED = ["loadHealthChecks", "editHealthCheck", "saveNewHealthCheck",
             "testNewHealthCheck", "addHealthCheckRow", "saveHealthCheck",
             "deleteHealthCheck", "testHealthCheck"]


def test_health_checks_left_the_servers_page():
    code = _code(_SERVERS)
    still = [f for f in _HC_MOVED
             if re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not still, f"health-check functions still on /servers: {still}"
    assert 'id="health-checks"' not in code, "the section markup is still there"


def test_the_health_check_form_is_whole_at_its_destination():
    """A move loses a function quietly: the page still renders, and only the
    button nobody pressed during review is dead."""
    code = _code(_HEALTH)
    missing = [f for f in _HC_MOVED
               if not re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not missing, f"lost in the move: {missing}"
    for shim in ("_srvEditHc", "_srvDeleteHc"):
        assert f"function {shim}(" in code, (
            f"{shim} stayed behind, so its data-action resolves to nothing")


def test_no_health_check_bootstrap_call_is_left_behind():
    code = _code(_SERVERS)
    assert not re.search(r"^\s*loadHealthChecks\(\)", code, re.M), (
        "loadHealthChecks() is still called on /servers, where it no longer exists")


def test_the_destination_loads_the_list_itself():
    code = _code(_HEALTH)
    # A BOOTSTRAP call, not any call. The loader calls itself after every
    # save, so `"loadHealthChecks()" in code` is true even with nothing
    # kicking it off — which is exactly the state this test exists to catch.
    # Top level, which in this file means column zero: the loader calls
    # itself after every save, and those calls are inside functions and
    # therefore indented. Excluding the definition line, which also starts at
    # column zero and also contains the name.
    bootstrap = re.search(
        r"^(?!function)\S.*\bloadHealthChecks\(\)", code, re.M)
    assert bootstrap, (
        "nothing calls the loader at load time on the page that now owns it, "
        "so the list renders empty until something else happens to save")


# ── the anchor trap the plan named ────────────────────────────────────────

def test_the_anchor_exists_where_the_link_points():
    """The trap, stated in the plan: "A 301 does not save an in-page anchor."
    The fragment never reaches the server, so a link to a moved section is a
    link to the top of whatever page answers — silently, with no 404 and
    nothing in a log."""
    src = _HEALTH.read_text(encoding="utf-8")
    assert 'id="health-checks"' in src, "the anchor did not travel with the section"

    services = (_ROOT / "templates" / "services.html").read_text(encoding="utf-8")
    assert 'href="/settings/servers#health-checks"' in services, (
        "/services still points at the old location")
    assert 'href="/servers#health-checks"' not in services


def test_the_anchor_resolves_on_the_rendered_page(client):
    """Asserted against what the app SERVES, not what the template says — the
    id could exist in a partial nobody includes."""
    body = client.get("/settings/servers").get_data(as_text=True)
    assert 'id="health-checks"' in body, (
        "the anchor is not on the rendered page /services links to")


def test_no_locale_still_names_the_old_location():
    """The two ends the plan did not list. Both vitals hints named
    "Servers → Health Checks" as the place to go, in five languages, and a
    hint that sends an operator to a page which no longer has the thing is
    worse than no hint."""
    from i18n import TRANSLATIONS
    stale = {
        "en": "Servers \u2192 Health Checks",
        "de": "Server \u2192 Gesundheitspr\u00fcfungen",
        "fr": "Serveurs \u2192 Contr\u00f4les de sant\u00e9",
        "es": "Servidores \u2192 Verificaciones de salud",
        "ja": "\u30b5\u30fc\u30d0\u30fc \u2192 \u30d8\u30eb\u30b9\u30c1\u30a7\u30c3\u30af",
    }
    bad = []
    for lang, phrase in stale.items():
        for key, value in TRANSLATIONS[lang].items():
            if isinstance(value, str) and phrase in value:
                bad.append(f"{lang}:{key}")
    assert not bad, "strings still naming the old location: " + ", ".join(bad)


def test_the_health_check_empty_state_is_the_shared_one():
    """It was hand-rolled, and its hint ('Click "Add Health Check" to monitor
    ports and URLs') had never been offered for translation."""
    code = _code(_HEALTH)
    # The CALL, not the import. `"empty_state(" in code` is satisfied by the
    # `{% from ... import empty_state %}` line, which survives the renderer
    # being taken back out of the markup.
    assert re.search(r"\{\{\s*empty_state\(", code), (
        "the shared renderer is imported but never called")
    assert "Click " not in code, "the hand-rolled hint is back"


def test_the_health_check_block_declares_no_section_of_its_own():
    """It is a sub-block of Settings → Servers, not a section. Every
    `<section>` on a settings page must say which section it is, and a second
    one here would be claiming to be a sub-page that has no route."""
    src = _HEALTH.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    assert "<section" not in src, (
        "the health-check block is a <section>; it nests inside the Servers "
        "section, which the declaration guard reads as an undeclared section")


def test_the_health_check_hint_exists_in_every_locale():
    from i18n import TRANSLATIONS
    missing = [lang for lang in TRANSLATIONS
               if "no_health_checks_hint" not in TRANSLATIONS[lang]]
    assert not missing, f"untranslated: {missing}"
    english = TRANSLATIONS["en"]["no_health_checks_hint"]
    for lang in TRANSLATIONS:
        if lang != "en":
            assert TRANSLATIONS[lang]["no_health_checks_hint"] != english
