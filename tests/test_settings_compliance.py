"""The Compliance settings section — WP-4 D6.

The slice is called "Compliance enable/config moves under Settings", and the
first thing reading it turned up is that there was nothing to move. The
module has exactly one setting, `compliance.enabled`, and it had no surface
anywhere: turning the module on meant hand-editing config.json on the server.
A feature flag with no control is not a decision, it is a gap — and this flag
gates a regulated module, so the operators most likely to need it are the
least likely to be editing JSON.

THE NAV QUESTION, ANSWERED DELIBERATELY. The WP-4 plan says "Compliance and
RBAC leave the main nav". RBAC left because it is configuration — who may do
what to which server. `/compliance` is not: it is SOP status, audit-chain
integrity, a findings register and a document browser. Those are things an
operator LOOKS AT, and WP-4's whole premise is that state belongs in the nav
and configuration belongs in Settings. So the enable switch moved to Settings
and the dashboard stayed in the nav, gated as it always was. Applying the
plan's letter here would have contradicted its principle; this is recorded
rather than assumed, and is the one place this slice departs from the plan.

WHAT THIS FILE IS BLIND TO:

  * Whether the compliance dashboard itself is any good. It is untouched by
    this slice and carries known debt (raw colour literals, bare loading
    texts) already booked against WP-8.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "templates" / "partials" / "settings" / "_compliance.html"

_KEYS = ["compliance_enable", "compliance_enable_desc", "compliance_off_means",
         "compliance_evidence_kept", "compliance_open_dashboard"]


@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


# ── the flag now has a control ────────────────────────────────────────────

def test_compliance_is_a_settings_section(client):
    from routes.views import _SETTINGS_SECTIONS
    assert "compliance" in _SETTINGS_SECTIONS
    r = client.get("/settings/compliance")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'data-settings-section="compliance"' in body
    assert 'id="compliance-enabled"' in body


def test_the_section_is_reachable_whether_or_not_the_module_is_on(client, monkeypatch):
    """The chicken-and-egg this slice exists to break. If the section only
    appeared once the module was enabled, the only way to enable it would
    still be the config file — which is the state before this change."""
    import app as prism_app
    real = prism_app.config.get_settings()
    off = {**real, "compliance": {"enabled": False}}
    monkeypatch.setattr(prism_app.config, "get_settings", lambda: off)
    r = client.get("/settings/compliance")
    assert r.status_code == 200, "the enable switch vanished when the module was off"
    assert 'id="compliance-enabled"' in r.get_data(as_text=True)


def test_the_toggle_is_tracked_by_the_save_bar():
    """`general-input`, not `security-input`: enabling the module changes
    routing and navigation, which a reload picks up, and does not touch the
    collector — so it must not raise the restart badge. Either class makes
    the Save bar notice; only one of them is honest about the consequence."""
    src = _SRC.read_text(encoding="utf-8")
    toggle = re.search(r'<input[^>]*id="compliance-enabled"[^>]*>', src, re.S)
    assert toggle, "the toggle is gone"
    assert "general-input" in toggle.group(0), (
        "the toggle is in no tracked bucket, so the Save bar cannot see it")
    assert "security-input" not in toggle.group(0), (
        "the toggle raises the restart badge for a change that needs no restart")


def test_the_section_contributes_only_its_own_subtree():
    """The clobber guard. Every section builder returns only its own keys, and
    a section that is not on the page contributes nothing — that is what stops
    /settings/display saving a payload that wipes the collector config."""
    src = (_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    m = re.search(r"compliance: function \(\) \{(.*?)\n  \},", src, re.S)
    assert m, "the compliance payload builder is gone"
    body = m.group(1)
    assert "compliance:" in body and "enabled:" in body
    ids = re.findall(r"getElementById\('([^']+)'\)", body)
    assert ids == ["compliance-enabled"], (
        f"the builder reads controls outside its own section: {ids}")


# ── URS-204's contract, which this change must not have loosened ──────────

def test_when_off_the_view_routes_are_absent(client, monkeypatch):
    """URS-204: with the flag off the nav item is hidden, the view routes 404
    and the API endpoints 404. Adding a control for the flag must not have
    added an exception to it — pinned here because a regulated requirement
    that only lives in a markdown file is a requirement nobody runs."""
    import app as prism_app
    real = prism_app.config.get_settings()
    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "compliance": {"enabled": False}})
    for path in ("/compliance", "/compliance/sop/SOP-001", "/compliance/doc/02_URS"):
        assert client.get(path).status_code == 404, f"{path} answered while off"


def test_when_off_the_nav_entry_is_hidden(client, monkeypatch):
    import app as prism_app
    real = prism_app.config.get_settings()
    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "compliance": {"enabled": False}})
    body = client.get("/settings/compliance").get_data(as_text=True)
    nav = re.search(r'<nav[^>]*sidebar[^>]*>.*?</nav>', body, re.S)
    haystack = nav.group(0) if nav else body
    assert 'href="/compliance"' not in haystack, (
        "the compliance nav entry is showing while the module is off")


def test_when_on_the_dashboard_and_its_nav_entry_appear(client, monkeypatch):
    """The half that proves the toggle DOES something.

    The two tests above run against a config where the module is already off,
    so on their own they assert a state that was true before this slice
    existed. This is the mirror: with the flag on, the view route renders and
    the nav entry is back. A gate that only ever says no is indistinguishable
    from a route that does not exist."""
    import app as prism_app
    real = prism_app.config.get_settings()
    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "compliance": {"enabled": True}})
    r = client.get("/compliance")
    assert r.status_code == 200, "the dashboard 404s while the module is on"
    body = r.get_data(as_text=True)
    assert "Something went wrong" not in body
    assert 'href="/compliance"' in body, "the nav entry is missing while on"


def _compliance_section(body: str) -> str:
    """Just the settings section, not the whole page.

    The main nav also links /compliance when the module is on, so a page-wide
    search cannot tell the section's own link from the sidebar's."""
    start = body.index('data-settings-section="compliance"')
    return body[start:body.index("</section>", start)]


def test_the_dashboard_link_only_appears_once_the_module_is_on(client, monkeypatch):
    """The bridge for someone who has just switched it on and is looking for
    where it went. Offering it while the module is off would be a link to a
    404 — which is what the routes correctly answer.

    Written first as source-position arithmetic: find the link, find the
    nearest `{% if %}` before it, assert the link sits between it and the
    `{% endif %}`. The mutation harness replaced the gate with `{% if True %}`
    and the test still passed, because `rindex` simply found the PREVIOUS
    guard — the one on the checkbox — and the arithmetic still held. Rendering
    the page twice cannot be fooled that way."""
    import app as prism_app
    real = prism_app.config.get_settings()

    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "compliance": {"enabled": False}})
    off = _compliance_section(
        client.get("/settings/compliance").get_data(as_text=True))
    assert 'href="/compliance"' not in off, (
        "the section offers a link to the dashboard while the module is off, "
        "and the dashboard answers 404 in that state")

    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "compliance": {"enabled": True}})
    on = _compliance_section(
        client.get("/settings/compliance").get_data(as_text=True))
    assert 'href="/compliance"' in on, (
        "the section offers no way through to the dashboard once it is on")


def test_the_dashboard_stays_in_the_main_navigation(client):
    """The deliberate departure from the plan's letter, pinned so it is a
    decision rather than a drift. The dashboard is state, not configuration;
    WP-4 moves configuration."""
    src = (_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'href="/compliance"' in src, (
        "the compliance dashboard left the main nav; it is a view surface, and "
        "WP-4 moves configuration into Settings, not the things operators read")
    assert "{% if compliance_enabled %}" in src, (
        "the nav entry lost its gate, which URS-204 requires")


# ── the strings ───────────────────────────────────────────────────────────

def test_every_string_exists_in_every_locale():
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in _KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated compliance strings: " + ", ".join(missing)


def test_no_locale_silently_reuses_the_english_text():
    from i18n import TRANSLATIONS
    for key in _KEYS:
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, (
                f"{lang}:{key} is the English string")


def test_each_fallback_is_exactly_its_english_entry():
    """`t.get(key, 'English')` renders the fallback whenever the key is
    missing, so a fallback that has drifted from the English entry is a page
    that says two different things depending on a lookup nobody sees. The
    first version of this section wrapped its fallbacks across lines, and
    Jinja put the newlines and the indentation IN the string."""
    from i18n import TRANSLATIONS
    src = _SRC.read_text(encoding="utf-8")
    for key in _KEYS:
        m = re.search(r"t\.get\(\s*'" + re.escape(key) + r"'\s*,\s*(.*?)\)\s*\}\}",
                      src, re.S)
        assert m, f"no t.get call for {key}"
        literal = m.group(1).strip()
        quote = literal[0]
        assert quote in "'\"", f"{key}'s fallback is not a string literal"
        fallback = literal[1:literal.rindex(quote)]
        assert fallback == TRANSLATIONS["en"][key], (
            f"{key}'s fallback has drifted from its English entry:\n"
            f"  fallback: {fallback!r}\n  english:  {TRANSLATIONS['en'][key]!r}")
