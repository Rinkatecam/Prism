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
_DEPS = _ROOT / "templates" / "partials" / "settings" / "_dependencies.html"

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
    # Both files: showDepBrowsePopup moved to the dependencies partial in
    # D4c, and the `if not m: continue` below quietly stopped covering it.
    # A test that silently checks half of what it names is worse than one
    # that fails.
    sources = {"showTagAssign": _code(_SERVERS),
               "showDepBrowsePopup": _code(_DEPS)}
    for fn, code in sources.items():
        m = re.search(r"function " + fn + r".*?\n\}", code, re.S)
        assert m, f"{fn} is gone from the file that should hold it"
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
    # Comments stripped first: this file's own step-15 comment quotes
    # `id="health-checks"` in prose (explaining why the id is load-bearing),
    # and a bare substring check cannot tell that quote from the real
    # attribute -- WP-6 step 24 found this blind: a guardrails mutation
    # that renamed the real element's id still left this assertion passing
    # off the comment alone.
    src = re.sub(r"\{#.*?#\}", " ", _HEALTH.read_text(encoding="utf-8"), flags=re.S)
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


# ══════════════════════════════════════════════════════════════════════════
# D4c — dependencies, and a third dispatch shim
# ══════════════════════════════════════════════════════════════════════════


_DEP_MOVED = ["toggleDepForm", "onDepTypeChange", "onDepTargetModeChange",
              "browseDepService", "browseDepProcess", "showDepBrowsePopup",
              "loadDepsTable", "addDependency", "editDependency",
              "removeDependency"]

# Every `data-action` and `data-input` the dependency markup names. Three of
# these are shims whose bodies live beside the functions they call, and a shim
# left behind is a control that resolves to nothing — the dispatcher answers
# an unknown name by doing nothing at all, silently.
_DEP_SHIMS = ["_srvBrowseDepService", "_srvBrowseDepProcess", "_srvFilterDepBrowse"]


def test_dependencies_left_the_servers_page():
    code = _code(_SERVERS)
    still = [f for f in _DEP_MOVED
             if re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not still, f"dependency functions still on /servers: {still}"
    assert 'id="dep-add-form"' not in code, "the dependency markup is still there"


def test_the_dependency_editor_is_whole_at_its_destination():
    code = _code(_DEPS)
    missing = [f for f in _DEP_MOVED
               if not re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not missing, f"lost in the move: {missing}"


def test_every_dependency_shim_travelled_with_its_function():
    """The third shim, `_srvFilterDepBrowse`, is the one I did not think to
    look for. It filters the service/process browser popup, and left behind it
    would have made that search box do nothing — no error, because the
    dispatcher resolves an unknown action to a no-op. The dispatch guard found
    it; this pins it."""
    dest = _code(_DEPS)
    src = _code(_SERVERS)
    for shim in _DEP_SHIMS:
        assert f"function {shim}(" in dest, f"{shim} did not travel"
        assert f"function {shim}(" not in src, f"{shim} is defined twice"


def test_no_dependency_bootstrap_call_is_left_behind():
    code = _code(_SERVERS)
    assert not re.search(r"^\s*loadDepsTable\(\)", code, re.M), (
        "loadDepsTable() is still called on /servers, where it no longer exists")


def test_the_destination_loads_its_own_table():
    code = _code(_DEPS)
    bootstrap = re.search(r"^(?!function)\S.*\bloadDepsTable\(\)", code, re.M)
    assert bootstrap, (
        "nothing calls the loader at load time on the page that owns it")


def test_both_dependency_empty_states_use_the_shared_renderer():
    """There were two — one in the markup, one built by `loadDepsTable` — and
    both reproduced the renderer's markup by hand."""
    code = _code(_DEPS)
    assert re.search(r"\{\{\s*empty_state\(", code), "the markup one is hand-rolled"
    assert "prismEmptyState(" in code, "the script one is hand-rolled"
    assert "Click " not in code, "a hand-rolled hint is back"


def test_the_dependency_hint_was_not_defined_a_second_time():
    """`no_dependencies_hint` already existed in all five locales, saying
    "Define dependencies in Servers settings" — written before this move and
    already naming the place it creates. Adding a second definition would have
    silently replaced it everywhere; the duplicate-key guard caught that, and
    this pins the fallback to the entry so the two cannot drift."""
    from i18n import TRANSLATIONS
    src = _DEPS.read_text(encoding="utf-8")
    english = TRANSLATIONS["en"]["no_dependencies_hint"]
    assert src.count(english) == 2, (
        "the two fallbacks do not both match the existing English entry")


# ══════════════════════════════════════════════════════════════════════════
# D4d — add / edit / delete, and the reference that hoisting hid
# ══════════════════════════════════════════════════════════════════════════

_CONFIG = _ROOT / "templates" / "partials" / "settings" / "_server_config.html"

_CFG_MOVED = ["showAddForm", "editServer", "closeModal", "deleteServer",
              "confirmDeleteServer", "closeDeleteModal", "saveServer",
              "saveServerConfig", "testConnectionFromModal", "captureModalState",
              "isModalDirty", "exportServersCSV", "importServersCSV",
              "discoverServers", "addDiscoveredServers", "closeDiscoveryModal",
              "populateCustomServerTypes", "applyDefaults"]

# Stays: an action on an existing server, not a change to one.
_CFG_STAYS = ["testConnection", "showGuide", "closeGuide", "showServerInfo",
              "populateStatusColumn", "loadServerTags"]


def test_the_configuration_functions_left_the_fleet_page():
    code = _code(_SERVERS)
    still = [f for f in _CFG_MOVED
             if re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not still, f"still defined on /servers: {still}"


def test_the_actions_and_the_view_stayed():
    """`testConnection` is the interesting one. It is an action on an existing
    server — "do these credentials still work" — and D3's rule is that acting
    is not configuring. It shares three helpers with the modal's test button,
    which is why D4d-i moved those to base.html first."""
    code = _code(_SERVERS)
    missing = [f for f in _CFG_STAYS
               if not re.search(r"function\s+" + re.escape(f) + r"\s*\(", code)]
    assert not missing, f"taken from /servers by mistake: {missing}"


def test_no_configuration_control_is_left_on_the_fleet_page(client):
    """Asserted against the RENDERED page: a control removed from the table
    but left on the card view is still a control."""
    body = client.get("/servers").get_data(as_text=True)
    for action in ("showAddForm", "editServer", "deleteServer",
                   "discoverServers", "exportServersCSV"):
        assert f'data-action="{action}"' not in body, (
            f"/servers still offers {action}")
    assert 'data-action="testConnection"' in body, "the action button went too"


def test_the_fleet_page_says_where_its_configuration_went(client):
    """A page that has just lost its controls and does not say where they are
    is worse than one that never had them."""
    body = client.get("/servers").get_data(as_text=True)
    assert 'href="/settings/servers"' in body


def test_the_settings_page_has_a_table_to_act_on(client):
    """The one part of D4 that is a build rather than a move: /servers keeps
    its table for READING the fleet, so Settings needs its own narrow one for
    changing it."""
    body = client.get("/settings/servers").get_data(as_text=True)
    assert 'id="settings-server-table"' in body
    for action in ("showAddForm", "editServer", "deleteServer", "discoverServers"):
        assert f'data-action="{action}"' in body, f"Settings cannot {action}"
    assert 'id="server-modal"' in body and 'id="delete-modal"' in body


def test_the_setup_guide_did_not_travel():
    """It came across in the markup cut — the functions were cut by name, the
    markup by range — and settings.html ended up with three
    `data-action="closeGuide"` and no handler. The guide is help about adding a
    server; /servers still offers it."""
    assert 'id="guide-modal"' in _SERVERS.read_text(encoding="utf-8")
    assert "closeGuide" not in _code(_CONFIG), (
        "the guide modal is in the settings partial, whose page has no handler")


# ── the defect hoisting hid ──────────────────────────────────────────────

def test_no_moved_name_is_still_evaluated_on_the_fleet_page():
    """The one that got through every file-level check.

        document.addEventListener('DOMContentLoaded', populateCustomServerTypes);

    is a REFERENCE, not a call, so the bootstrap guard — which scans for
    `name()` — did not see it. It throws at module scope, so the rest of the
    script never runs: the symptom was the setup guide's Escape key, three
    hundred lines below.

    Function declarations HOIST, which is why the checks I ran first said
    nothing: `showGuide` was callable and the script tag had run. Both are
    true of a script that threw on its second statement."""
    code = _code(_SERVERS)
    js = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                              code, re.S | re.I))
    moved = set(re.findall(r"function\s+(\w+)\s*\(", _code(_CONFIG)))
    here = set(re.findall(r"function\s+(\w+)\s*\(", js))
    dangling = sorted(
        n for n in moved - here
        if re.search(r"addEventListener\([^)]*,\s*" + re.escape(n) + r"\s*\)", js)
        or re.search(r"=\s*" + re.escape(n) + r"\s*;", js))
    assert not dangling, (
        f"/servers evaluates names that moved away: {dangling}")


def test_escape_still_closes_something_on_each_page():
    """The handler that broke. On /servers it closes the guide; in Settings it
    closes whichever modal is open. Reaching for an element that is not on the
    page throws on the null, so neither may reference the other's."""
    servers = _code(_SERVERS)
    config = _code(_CONFIG)
    assert "closeGuide()" in servers
    assert "delete-modal" not in servers and "server-modal" not in servers, (
        "/servers reaches for modal elements it no longer has")
    for modal in ("delete-modal", "server-modal", "discovery-modal"):
        assert modal in config, f"the settings handler forgot {modal}"


def test_the_fleet_empty_state_names_the_new_place():
    """It said 'Click "Add Server" or "Discover Servers" to get started', and
    both buttons left in this slice. Fifth stale in-page pointer in WP-4."""
    src = _SERVERS.read_text(encoding="utf-8")
    assert "Add Server" not in src or "no_servers_hint_fleet" in src
    assert 'Click "Add Server"' not in src
    from i18n import TRANSLATIONS
    assert "Settings" in TRANSLATIONS["en"]["no_servers_hint_fleet"]
