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
    return app_obj.test_client()


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
    order. This test pins the finding: if a future step reorders the tab
    strip without updating SETTINGS_THEMES to match, the rendered tabs and
    the registry (and therefore the steps 22-23 menu) would silently
    disagree, and this catches it."""
    import re
    from routes.views import _SETTINGS_SECTIONS

    html = client.get("/settings/general").get_data(as_text=True)
    start = html.index('id="settings-section-nav"')
    nav = html[start:html.index("</nav>", start)]
    rendered_order = re.findall(r'href="/settings/(\w+)"', nav)
    assert rendered_order == list(_SETTINGS_SECTIONS), (
        f"rendered tab order {rendered_order} != registry order "
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
