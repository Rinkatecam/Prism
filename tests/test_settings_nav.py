"""The Settings theme registry — WP-6 step 21 (DESIGN_SYSTEM_SPEC.md §7.1/D4).

This step is data-layer only: ONE registry (`SettingsTheme`/`SETTINGS_THEMES`
in routes/views.py) now feeds three things that used to drift independently:

  * the router's own `_SETTINGS_SECTIONS` (derived below it, same slugs, same
    order — search_index.py and every test file that already imported
    `_SETTINGS_SECTIONS` directly need no change at all);
  * the top-bar theme menu steps 22-23 add (nothing renders it yet — this
    file cannot test markup that does not exist);
  * search_index.py's jump-to labels, which used to compute a settings
    section's display name with `slug.title()` — the exact mechanism that
    rendered "Rbac" instead of "Permissions" in the jump box.

No template changes land in this step, so there is nothing here about
markup, ARIA or keyboard behaviour — that is steps 22 and 23's own test
file additions (S-1..S-16), appended to this same file when they land.

D10 (DESIGN_SYSTEM_SPEC.md §0.0.2, already ratified — resolves Part V owner
question 2) renamed the `servers` theme's LABEL to "Server Configuration",
behind a new key, `settings_theme_servers_configuration` — not its slug, not
its icon, and not the sidebar's own `servers_page` key/label, which a
handful of templates legitimately keep using for the unrelated top-level
Servers page. `test_the_servers_theme_carries_the_d10_correction` below
pins this specifically, since silently reverting to the spec's own stale
§7.1 table text (`servers_page`/"Servers") would undo a ratified decision
without any test noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_obj():
    # The one sanctioned way to exercise real Flask/app behaviour: `import
    # app` INSIDE a fixture body, which only ever executes while pytest is
    # the runner. app.py's own `_under_pytest = "pytest" in sys.modules`
    # gate is what keeps this from starting a real collector, restart
    # scheduler, workflow scheduler or watchdog against the live 29-server
    # fleet. Never do this at module import time, and never as a bare
    # `python -c`/script outside pytest — see this repo's own house rules.
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app


@pytest.fixture(scope="module")
def client(app_obj):
    c = app_obj.test_client()
    # A fresh CI checkout has no config.json, so no backup admin exists and
    # auth.check_setup's before_request hook redirects every page to /setup
    # (see tests/test_reset_password_authz.py's _client(), the established
    # fix for this same gap). A logged-in session is realistic -- these
    # tests exercise rendered markup, not the first-run gate itself.
    with c.session_transaction() as sess:
        sess["username"] = "tester"
    return c


# ── the registry's own structural invariants ────────────────────────────

def test_the_registry_derives_settings_sections_and_has_eleven_icons():
    """The step's own Verify line, as a real test rather than a bare
    `python -c` (so it keeps asserting this on every future run, and so it
    never needs `import app` at all — `routes.views` does not import
    `app.py`, so this is safe even outside pytest, but it lives here for
    consistency with the rest of the suite)."""
    from routes.views import SETTINGS_THEMES, _SETTINGS_SECTIONS
    assert tuple(t.slug for t in SETTINGS_THEMES) == _SETTINGS_SECTIONS
    assert len({t.icon for t in SETTINGS_THEMES}) == 11


def test_the_registry_has_exactly_eleven_themes_with_no_duplicate_slugs():
    from routes.views import SETTINGS_THEMES
    assert len(SETTINGS_THEMES) == 11
    slugs = [t.slug for t in SETTINGS_THEMES]
    assert len(set(slugs)) == 11, f"duplicate slug(s) in SETTINGS_THEMES: {slugs}"


def test_every_theme_key_resolves_in_every_locale_without_a_blank():
    """Catches a typo'd `key` before it ever reaches a template: every
    theme's key must resolve to a non-empty string in all five locales,
    whether via that locale's own translation or the English fallback."""
    from routes.views import SETTINGS_THEMES
    from i18n import TRANSLATIONS, get_translations
    for lang in TRANSLATIONS:
        merged = get_translations(lang)
        for theme in SETTINGS_THEMES:
            label = merged.get(theme.key, theme.fallback)
            assert label, f"{lang}:{theme.slug} ({theme.key}) resolved to a blank label"


def test_the_registry_order_matches_the_rendered_settings_nav(client):
    """Judgement call, pinned: the CURRENT live tab order — read off a REAL
    RENDERED /settings page, not assumed from DESIGN_SYSTEM_SPEC.md §7.1's
    own table, and not from settings.html's label dict (that dict is looked
    up by key, `{...}[name]`, once per loop iteration — its own textual key
    order is incidental and today is NOT the render order: the dict lists
    ...operations, rbac, compliance, security..., while the actual tab
    order below, like `_SETTINGS_SECTIONS`, has security before rbac and
    compliance. An earlier draft of this test compared against that dict's
    key order directly and failed for a reason that was never a real
    defect) — happens to already match §7.1's table order exactly: general,
    collector, servers, detection, alerts, operations, security, rbac,
    compliance, notifications, display. SETTINGS_THEMES copies that verified
    order. This test pins the finding: if a future step reorders the menu
    without updating SETTINGS_THEMES to match, the rendered menu and the
    registry would silently disagree, and this catches it.

    WP-6 step 23: the label dict and `<nav id="settings-section-nav">` this
    docstring's history refers to are gone (§7.4 — replaced outright by the
    top-bar theme menu). Repointed at `#settings-theme-panel` — the control
    that replaced it — via the same `_settings_theme_panel_html` helper
    S-6/S-7 further down this file already use. The underlying claim this
    test makes (rendered order == registry order) has not changed, only
    where that order is now rendered; S-6 below additionally proves this on
    ALL eleven /settings/<slug> pages, not just /settings/general, so the
    two tests are complementary rather than duplicates."""
    from routes.views import _SETTINGS_SECTIONS

    _, panel = _settings_theme_panel_html(client, "general")
    rendered_order = re.findall(r'href="/settings/([a-z-]+)"', panel)
    assert rendered_order == list(_SETTINGS_SECTIONS), (
        f"rendered order {rendered_order} != registry order "
        f"{list(_SETTINGS_SECTIONS)}")


def test_the_servers_theme_carries_the_d10_correction():
    """Pins D10 specifically: the spec's own §7.1 table text is stale
    (`SettingsTheme("servers", "server", "servers_page", "Servers")`) and
    copying it verbatim would silently undo a ratified decision. Getting
    this one wrong is the single most consequential mistake this step could
    make without any generic test catching it, because `servers_page` is a
    perfectly real, valid, already-translated key — reusing it would not
    look like an error anywhere except in the final rendered word."""
    from routes.views import SETTINGS_THEMES
    servers_theme = next(t for t in SETTINGS_THEMES if t.slug == "servers")
    assert servers_theme.icon == "server"
    assert servers_theme.key == "settings_theme_servers_configuration", (
        "the servers theme still points at a stale/reused key")
    assert servers_theme.fallback == "Server Configuration", (
        "the servers theme fallback reverted to the stale 'Servers' label")
    assert servers_theme.key != "servers_page", (
        "D10 exists specifically so this theme stops reusing the sidebar's "
        "own servers_page key")


# ── the three new i18n keys ──────────────────────────────────────────────

_NEW_KEYS = ["settings_theme_servers_configuration", "settings_theme_menu_aria",
             "settings_theme_menu_title"]


def test_the_new_keys_are_genuinely_translated_not_english_copies():
    from i18n import TRANSLATIONS
    for key in _NEW_KEYS:
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            assert key in TRANSLATIONS[lang], f"{lang} is missing {key}"
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, (
                f"{lang}:{key} is an English copy, not a real translation")


# ── the context processor ────────────────────────────────────────────────

def test_settings_themes_is_exposed_to_every_template(app_obj):
    """Steps 22-23 build the top-bar menu from `settings_themes` without
    their own import, exactly the way every other context-processor value
    (t, lang, app_settings, ...) already works. Proven by actually rendering
    a template string through the app's real Jinja environment inside a
    request context -- not by reading inject_locale()'s source -- because a
    context processor that raises or a key that never reaches the dict both
    look identical from the source alone."""
    from flask import render_template_string
    with app_obj.test_request_context("/"):
        rendered = render_template_string(
            "{{ settings_themes|length }}|{{ settings_themes[0].slug }}|"
            "{{ settings_themes[-1].slug }}")
    length, first_slug, last_slug = rendered.split("|")
    assert length == "11"
    assert first_slug == "general"
    assert last_slug == "display"


# ── the jump-to index stops saying 'Rbac' ────────────────────────────────

def test_the_jump_index_labels_rbac_as_permissions(app_obj):
    import search_index
    index = search_index.build(app_obj)
    settings_labels = {e["url"]: e["label"] for e in index if e["kind"] == "settings"}
    assert settings_labels.get("/settings/rbac") == "Permissions", settings_labels
    assert "Rbac" not in settings_labels.values(), settings_labels


def test_no_settings_index_entry_or_sublabel_says_rbac_anywhere(app_obj):
    """Broader than the single settings-kind entry above: a heading inside
    /settings/rbac is indexed with `sublabel: page_name`, which used the
    identical `slug.title()` computation and would still say "Rbac" there
    even if only the settings-kind entry above were fixed."""
    import search_index
    index = search_index.build(app_obj)
    offenders = [e for e in index if e.get("label") == "Rbac" or e.get("sublabel") == "Rbac"]
    assert not offenders, f"'Rbac' still appears in the jump index: {offenders}"


def test_the_search_endpoint_returns_permissions_not_rbac(client):
    """The step's own Verify line, against the real `/api/search` route."""
    r = client.get("/api/search?q=permissions")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    labels = [e["label"] for e in body["results"]]
    assert "Permissions" in labels, f"'Permissions' missing from results: {labels}"
    assert "Rbac" not in labels, f"stale 'Rbac' label still returned: {labels}"


# ══════════════════════════════════════════════════════════════════════════
# WP-6 step 22 — the theme chip: markup and ARIA, no behaviour yet.
# DESIGN_SYSTEM_SPEC.md Part II §8.4's own table, S-1..S-8 and S-14..S-16
# ONLY (S-9..S-13 are the interactive-behaviour tests and are step 23's job,
# not this one's -- there is no JS to test yet).
#
# Which S-numbers are genuinely new here, and which step 21 already covers
# under a different test name (checked BEFORE writing any of the below, to
# avoid duplicate coverage of the same fact):
#
#   S-1  PARTIALLY covered -- step 21's own
#        test_the_registry_derives_settings_sections_and_has_eleven_icons
#        already proves slugs == _SETTINGS_SECTIONS. It does NOT prove every
#        theme's icon/key are individually non-empty (11 distinct values
#        does not rule out one of them being ""). This file's own S-1 below
#        restates the first fact (so §8.4's table has one self-contained
#        test under its own name) and adds the second.
#   S-2  NOT covered under this name -- the same fact is implied by that
#        same step-21 test's `len({icons}) == 11`, but nothing reports WHICH
#        icon collided if it ever fails again. New, diagnostic version below.
#   S-3  NOT covered -- step 21 shipped no template, so nothing checked the
#        lucide bundle against SETTINGS_THEMES' icons yet.
#   S-4  NOT covered -- same reason; also see this test's own docstring for
#        a real methodology finding (a naive grep undercounts).
#   S-5  NOT covered -- there was no button to check.
#   S-6  NOT covered -- there were no menu items to check.
#   S-7  NOT covered -- there was no current-theme marker to check.
#   S-8  NOT covered -- there was no chip to check for.
#   S-14 PARTIALLY covered -- step 21's
#        test_every_theme_key_resolves_in_every_locale_without_a_blank
#        already proves the eleven theme-label keys resolve non-blank in
#        every locale. It does NOT check the two menu keys this step's own
#        control renders (settings_theme_menu_aria/_title). Restated below
#        as one self-contained S-14 covering both halves under §8.4's name.
#   S-15 NOT covered under this name or this scope -- step 21's
#        test_the_new_keys_are_genuinely_translated_not_english_copies
#        applies the same "not an English copy" rule to THREE keys lumped
#        together, one of which (settings_theme_servers_configuration) is a
#        THEME LABEL, not a menu string. S-15 is deliberately narrower (the
#        two menu keys ONLY) -- see that test's own docstring for why the
#        distinction matters (a loanword theme label like German
#        'Compliance' must NOT be held to this rule).
#   S-16 PARTIALLY covered -- step 21's own
#        test_the_jump_index_labels_rbac_as_permissions and
#        test_no_settings_index_entry_or_sublabel_says_rbac_anywhere already
#        prove this for the 'rbac' slug specifically (the one slug.title()
#        gets most visibly wrong: "Rbac"). This generalises the same proof
#        to the other two slugs where the registry's fallback differs from
#        slug.title() -- 'servers' and 'display' -- without repeating the
#        rbac case.
# ══════════════════════════════════════════════════════════════════════════


def test_the_theme_registry_matches_the_router():
    """S-1. See the module-level note above for what step 21 already proves
    and what this adds: every theme also carries a genuinely non-empty icon
    and key, not merely eleven distinct-but-possibly-blank values."""
    from routes.views import SETTINGS_THEMES, _SETTINGS_SECTIONS
    assert tuple(t.slug for t in SETTINGS_THEMES) == _SETTINGS_SECTIONS
    for theme in SETTINGS_THEMES:
        assert theme.icon, f"{theme.slug}: blank icon"
        assert theme.key, f"{theme.slug}: blank key"


def test_every_theme_icon_is_distinct():
    """S-2. "Would have caught both existing collisions" (§8.4) means the
    failure message has to NAME them, not just count them -- a bare
    `len(set) == 11` (step 21's own registry test) goes red but does not say
    which two themes are fighting over one icon. This reports the mapping."""
    from collections import Counter
    from routes.views import SETTINGS_THEMES
    counts = Counter(t.icon for t in SETTINGS_THEMES)
    dupes = {icon: [t.slug for t in SETTINGS_THEMES if t.icon == icon]
              for icon, n in counts.items() if n > 1}
    assert not dupes, f"icon(s) shared by more than one theme: {dupes}"


def test_every_theme_icon_exists_in_the_vendored_lucide():
    """S-3. Reuses test_design_headings.py's own bundle parser
    (_kebab_to_pascal/_bundle_icon_names/LUCIDE) rather than re-implementing
    it -- see that file's H-12 docstring for why the real `iconAndAliases`
    table, not a bare `exports.` grep, is the correct thing to check
    against (a first version of THAT test failed on a real, working icon
    for exactly this reason)."""
    from tests.test_design_headings import _kebab_to_pascal, _bundle_icon_names, LUCIDE
    from routes.views import SETTINGS_THEMES
    available = _bundle_icon_names(LUCIDE.read_text(encoding="utf-8"))
    assert len(available) >= 1000, (
        f"only {len(available)} name(s) resolved -- the bundle parser has drifted")
    missing = [f"{t.slug}:{t.icon}" for t in SETTINGS_THEMES
               if _kebab_to_pascal(t.icon) not in available]
    assert not missing, f"theme icon(s) with no bundle entry: {missing}"


def test_no_theme_icon_is_new_to_the_codebase():
    """S-4. "The vocabulary does not grow by a single icon" (§7.1) -- every
    one of the eleven must already be used SOMEWHERE else in the tree.

    Checked two ways, not one, after a real methodology miss found while
    writing this test: a literal `data-lucide="name"` attribute (most
    templates), AND an `icon='name'`/`icon="name"` keyword argument to the
    card()/subhead() macros. partials/_card.html renders ITS OWN icon as
    `data-lucide="{{ icon }}"` -- a Jinja expression, never a literal string
    in the CALLING template's own source -- so a data-lucide-only grep
    reports 'user-check' and 'sliders' as appearing nowhere else, which is
    false: partials/settings/_rbac.html:53 passes `icon='user-check'` to
    card(), and settings.html:887 passes `icon='sliders'` the same way.
    Both patterns are scanned so this test sees what the app actually
    renders, not just what happens to be a literal in a template's text."""
    from pathlib import Path
    from routes.views import SETTINGS_THEMES

    root = Path(__file__).resolve().parent.parent / "templates"
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in root.rglob("*.html")
        if p.name != "_settings_nav.html")

    elsewhere = (set(re.findall(r'data-lucide="([a-z0-9-]+)"', text))
                 | set(re.findall(r"icon=['\"]([a-z0-9-]+)['\"]", text)))

    missing = [f"{t.slug}:{t.icon}" for t in SETTINGS_THEMES if t.icon not in elsewhere]
    assert not missing, (
        f"theme icon(s) that appear nowhere but _settings_nav.html: {missing} "
        "-- §7.1 says the icon vocabulary does not grow by even one")


def _settings_theme_panel_html(client, slug: str = "general") -> tuple[str, str]:
    """(full page html, panel-only html) for /settings/<slug>. The panel has
    no nested <div> of its own (its children are <a>/<i>/<span> only), so
    the first `</div>` after the panel's opening tag is genuinely its own
    closing tag -- verified by construction against partials/_settings_nav.html
    rather than assumed."""
    html = client.get(f"/settings/{slug}").get_data(as_text=True)
    start = html.index('id="settings-theme-panel"')
    end = html.index("</div>", start)
    return html, html[start:end]


def test_the_trigger_is_a_menu_button_and_says_so(client):
    """S-5. haspopup/expanded/controls/role/labelledby -- §7.2's own
    contract, read off a real rendered page (test client, JS never runs)."""
    html, panel = _settings_theme_panel_html(client)

    assert 'id="settings-theme-trigger"' in html
    assert 'aria-haspopup="menu"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="settings-theme-panel"' in html

    m = re.search(r'<button[^>]*id="settings-theme-trigger"[^>]*>', html, re.S)
    assert m, "no <button id=\"settings-theme-trigger\"> found"
    trigger_tag = m.group(0)
    assert 'aria-haspopup="menu"' in trigger_tag
    assert 'aria-expanded="false"' in trigger_tag
    assert 'aria-controls="settings-theme-panel"' in trigger_tag
    label = re.search(r'aria-label="([^"]*)"', trigger_tag)
    assert label and label.group(1).strip(), "the trigger's aria-label is blank"

    assert 'role="menu"' in panel or re.search(
        r'id="settings-theme-panel"[^>]*role="menu"', html, re.S), (
        "the panel does not declare role=\"menu\"")
    assert 'aria-labelledby="settings-theme-trigger"' in html, (
        "the panel's aria-labelledby does not point back at the trigger")


def test_every_theme_is_a_menuitem_link_to_its_own_url(client):
    """S-6. All eleven, server-rendered, in registry order, with JS
    disabled by construction (the test client never executes JS) --
    checked on EVERY ONE of the eleven /settings/<slug> pages, not just
    one, which is the step's own Verify-line proof, taken literally: "all
    eleven /settings/<slug> routes render every menu option in the raw
    HTML with JS disabled"."""
    from routes.views import SETTINGS_THEMES
    expected = [f"/settings/{t.slug}" for t in SETTINGS_THEMES]

    for theme in SETTINGS_THEMES:
        _, panel = _settings_theme_panel_html(client, theme.slug)

        assert panel.count('role="menuitem"') == 11, (
            f"/settings/{theme.slug}: expected 11 role=\"menuitem\" anchors, "
            f"found {panel.count('role=\"menuitem\"')}")
        hrefs = re.findall(r'href="(/settings/[a-z-]+)"', panel)
        assert hrefs == expected, (
            f"/settings/{theme.slug}: menu hrefs {hrefs} != registry order {expected}")

        tabindexes = re.findall(r'tabindex="(-?\d)"', panel)
        assert len(tabindexes) == 11
        assert tabindexes.count("0") == 1, (
            f"/settings/{theme.slug}: expected exactly one tabindex=\"0\" "
            f"(roving tabindex), got {tabindexes}")
        assert all(t in ("0", "-1") for t in tabindexes)


def test_the_current_theme_is_marked_by_more_than_colour(client):
    """S-7. One aria-current="page" in the whole panel, and that SAME item
    (not merely some item) carries the check icon -- colour is never the
    only signal (§7.2)."""
    _, panel = _settings_theme_panel_html(client, "detection")
    assert panel.count('aria-current="page"') == 1, (
        "expected exactly one current theme in the panel")

    start = panel.index('href="/settings/detection"')
    end = panel.index("</a>", start)
    current_item = panel[start:end]
    assert 'aria-current="page"' in current_item, (
        "/settings/detection's own menu item is not the one marked current")
    assert 'data-lucide="check"' in current_item, (
        "the current theme has aria-current but no check icon")
    assert 'tabindex="0"' in current_item, (
        "the current theme is not the roving-tabindex stop")

    # negative control: a NON-current item carries neither marker.
    other_start = panel.index('href="/settings/general"')
    other_end = panel.index("</a>", other_start)
    other_item = panel[other_start:other_end]
    assert 'aria-current' not in other_item
    assert 'data-lucide="check"' not in other_item


def test_the_operator_can_still_see_they_are_in_settings(client):
    """S-8. On all eleven /settings/<slug> paths: the sidebar's own
    /settings link stays sidebar-link-active, AND the new chip renders."""
    from routes.views import _SETTINGS_SECTIONS
    for slug in _SETTINGS_SECTIONS:
        html = client.get(f"/settings/{slug}").get_data(as_text=True)
        assert 'id="settings-theme-trigger"' in html, (
            f"/settings/{slug}: the theme chip did not render")

        m = re.search(r'<a href="/settings"[^>]*class="([^"]*)"', html)
        assert m, f"/settings/{slug}: sidebar Settings link not found"
        assert "sidebar-link-active" in m.group(1), (
            f"/settings/{slug}: sidebar Settings link lost sidebar-link-active: "
            f"{m.group(1)!r}")


def test_every_theme_label_exists_in_every_locale():
    """S-14. Eleven theme-label keys (step 21's own
    test_every_theme_key_resolves_in_every_locale_without_a_blank already
    proves this half) PLUS the two menu keys this step's control actually
    renders (settings_theme_menu_aria on the trigger; settings_theme_menu_title,
    unused by this step's markup but already translated for step 23's mobile
    sheet title) -- one self-contained S-14 covering both halves."""
    from routes.views import SETTINGS_THEMES
    from i18n import TRANSLATIONS, get_translations
    menu_keys = ["settings_theme_menu_aria", "settings_theme_menu_title"]
    for lang in TRANSLATIONS:
        merged = get_translations(lang)
        for theme in SETTINGS_THEMES:
            label = merged.get(theme.key, theme.fallback)
            assert label, f"{lang}:{theme.slug} ({theme.key}) resolved to a blank label"
        for key in menu_keys:
            assert merged.get(key), f"{lang}:{key} resolved to a blank string"


def test_no_locale_reuses_the_english_menu_string():
    """S-15. The NEW MENU KEYS ONLY -- settings_theme_menu_aria/_title.

    Deliberately narrower than step 21's own
    test_the_new_keys_are_genuinely_translated_not_english_copies, which
    lumps these two together with settings_theme_servers_configuration (also
    new in step 21, but a THEME LABEL, not a menu string). Kept separate
    under this exact name because §8.4 names the failure mode this guards
    against: applying an identical-string rule to the ELEVEN theme labels
    would fail on a CORRECT loanword translation (German
    `TRANSLATIONS['de']['compliance'] == 'Compliance'` is right, not a
    missed translation). This test's scope proves, by construction, that it
    never touches those eleven label keys -- only the two menu-specific ones."""
    from i18n import TRANSLATIONS
    menu_keys = ["settings_theme_menu_aria", "settings_theme_menu_title"]
    for key in menu_keys:
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            assert key in TRANSLATIONS[lang], f"{lang} is missing {key}"
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, (
                f"{lang}:{key} is an English copy, not a real translation")


def test_the_index_labels_a_theme_from_the_registry(app_obj):
    """S-16. Step 21 already pins this for 'rbac' specifically
    (test_the_jump_index_labels_rbac_as_permissions /
    test_no_settings_index_entry_or_sublabel_says_rbac_anywhere) -- rbac is
    the slug where slug.title() ('Rbac') is most visibly wrong. Generalises
    the same proof to the other two slugs where SETTINGS_THEMES' fallback
    differs from slug.title(): 'servers' (D10 -- 'Server Configuration', not
    'Servers') and 'display' (-> 'Display Preferences', not 'Display').

    The other eight slugs' correct label happens to EQUAL slug.title() (e.g.
    'general'.title() == 'General'), so asserting label != slug.title() for
    all eleven would fail on those eight for giving the right answer --
    this checks only the slugs where the two computations diverge, which is
    exactly where a silent slug.title() regression would be visible."""
    import search_index
    index = search_index.build(app_obj)
    settings_labels = {e["url"]: e["label"] for e in index if e["kind"] == "settings"}
    assert settings_labels.get("/settings/servers") == "Server Configuration", settings_labels
    assert settings_labels.get("/settings/display") == "Display Preferences", settings_labels
    assert "Servers" not in settings_labels.values(), (
        "the servers theme's index label reverted to the sidebar's own word")
    assert "Display" not in settings_labels.values(), (
        "the display theme's index label reverted to a bare slug.title()")


# ══════════════════════════════════════════════════════════════════════════
# WP-6 step 23 — the theme menu behaves, and the tab strip goes.
# DESIGN_SYSTEM_SPEC.md Part II §8.4's own table, S-9..S-13: the five tests
# step 22 explicitly left for this step ("there is no JS to test yet").
#
# All five are STATIC: they read base.html's/app.css's own source text (the
# same slice-by-marker-comment convention test_jump_search.py already uses
# on `Jump-to (WP-4 D8)`) or the rendered HTML through the test client. None
# of them execute JavaScript -- this suite has no browser, and the two
# genuinely interactive checks (a real keyboard sequence; the 375px sheet)
# are this step's own explicitly deferred, real-browser-only verification.
# ══════════════════════════════════════════════════════════════════════════

def _settings_theme_menu_script() -> str:
    """The initSettingsThemeMenu() IIFE's own source, sliced from its marker
    comment to the closing `</script>` of the tag it lives in -- mirrors
    test_jump_search.py's `src[src.index("Jump-to (WP-4 D8)"):]` exactly,
    down to anchoring on the marker's plain text rather than its decorative
    box-drawing dashes, which is what keeps this working regardless of
    exactly how many trailing `─` characters follow the text on that line.

    This IIFE is appended INSIDE the same nonce'd `<script>` tag that
    already hosts Jump-to (D4's own spec text: never a new `<script>` tag,
    and never inside a `<script src>` one -- see base.html's own comment on
    the historical mistake three IIFEs above this one). Slicing to the next
    `</script>` after the marker is therefore exactly this IIFE's own
    extent: nothing else is appended after it in the same tag."""
    base = Path(__file__).resolve().parent.parent / "templates" / "base.html"
    src = base.read_text(encoding="utf-8")
    start = src.index("Settings theme menu (WP-6 D4)")
    end = src.index("</script>", start)
    return src[start:end]


def test_the_menu_is_keyboard_operable():
    """S-9. The sliced IIFE names every key §7.2's keyboard contract
    requires, a type-ahead buffer with its own reset timer, at least one
    preventDefault, and roving-tabindex management. Static-source, like
    every test in this section -- this cannot prove the keys actually WORK
    (that needs a real browser, and is this step's own deferred check), only
    that the code implementing them is genuinely present rather than merely
    described in a comment."""
    block = _settings_theme_menu_script()
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Escape"):
        assert f"'{key}'" in block, f"{key} is not handled inside the panel's keydown"
    assert "setTimeout" in block and "500" in block, (
        "no 500ms type-ahead buffer timer")
    assert "clearTimeout" in block, (
        "the buffer is never reset -- a keypress arriving after the window "
        "would append to a stale search instead of starting a new one")
    assert "preventDefault" in block
    assert "setRoving" in block and "tabindex" in block, (
        "no roving-tabindex management in the script")


def test_escape_returns_focus_to_the_trigger():
    """S-10. Not merely that SOME `.focus()` call exists in base.html (it
    has several, e.g. Jump-to's own `/` shortcut) -- it has to be reachable
    from the Escape branch specifically. `closeAndFocusTrigger` is the one
    function the Escape case calls that is not also called from anywhere
    else that should NOT move focus (outside pointerdown / Tab-out both
    close via plain `close()`, deliberately -- see those tests below and
    the function's own comment in base.html)."""
    block = _settings_theme_menu_script()
    escape_at = block.index("'Escape'")
    nearby = block[escape_at:escape_at + 200]
    assert "closeAndFocusTrigger" in nearby, (
        "Escape does not call the function that focuses the trigger")

    fn = re.search(r"function closeAndFocusTrigger\(\)\s*\{([^}]*)\}", block)
    assert fn, "closeAndFocusTrigger is not defined"
    assert "trigger.focus()" in fn.group(1), (
        "closeAndFocusTrigger no longer focuses the trigger")


def test_the_menu_closes_on_a_pointerdown_outside():
    """S-11. A document-level pointerdown listener, scoped by
    `.closest('#settings-theme-wrap')` -- the WRAP, not the panel alone,
    which would treat a pointerdown on the trigger itself as "outside" and
    close the menu out from under the very interaction opening it."""
    block = _settings_theme_menu_script()
    assert "addEventListener('pointerdown'" in block, (
        "no pointerdown listener in the settings-theme-menu script")
    assert ".closest('#settings-theme-wrap')" in block, (
        "the outside-pointerdown check is not scoped to #settings-theme-wrap")


def test_the_selector_is_gone_from_the_page_body(client):
    """S-12. The eleven-tab strip is gone in full: the `<nav>`, its id, the
    `settings_sections` loop variable that fed it, and the inline label
    dict that translated each slug -- checked against settings.html's own
    source AND a real rendered page, not just one or the other."""
    settings_html_path = (Path(__file__).resolve().parent.parent
                           / "templates" / "settings.html")
    src = settings_html_path.read_text(encoding="utf-8")
    assert "settings-section-nav" not in src
    assert "settings_sections" not in src, (
        "the router-fed loop variable that built the old nav is still here")
    assert "'collector': t.get('collector_engine_section'" not in src, (
        "the old nav's inline label dict is still here")

    rendered = client.get("/settings/general").get_data(as_text=True)
    assert "settings-section-nav" not in rendered


def test_the_sheet_variant_exists_below_sm():
    """S-13. §7.3's own literal block: fixed, pinned to the bottom, top
    cleared, capped at 60vh, radius on the top corners only -- read from the
    real stylesheet the browser loads, not a paraphrase of it. The rule
    targets `.settings-theme-menu`, the class _settings_nav.html's panel
    carries (not the `#settings-theme-panel` id JS uses), per this
    codebase's own hand-authored-CSS-hook convention (.pulse-cta,
    .stepper-btn)."""
    css_path = (Path(__file__).resolve().parent.parent
                / "static" / "css" / "app.css")
    css = css_path.read_text(encoding="utf-8")
    m = re.search(r"@media \(max-width: 639px\) \{(.*?)\n\}", css, re.S)
    assert m, "no `@media (max-width: 639px)` rule in app.css"
    block = m.group(1)
    assert ".settings-theme-menu" in block
    assert "position: fixed" in block
    assert "left: 0; right: 0; bottom: 0; top: auto" in block, (
        "the sheet is not pinned to the bottom with top cleared")
    assert "max-height: 60vh" in block
    assert "border-radius: 1rem 1rem 0 0" in block, (
        "the sheet does not round only its top corners")
