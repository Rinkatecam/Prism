"""The Permissions page — WP-4 D4b.

RBAC moved out of the main nav into Settings, because who may do what to which
server is *configuration*: it was the last top-level entry that configured
rather than showed. Moving it meant reading it, and reading it turned up a
page that had never been through the app's own standards.

The largest finding was invisible: thirty-three strings the page renders had
never gone through `t.get` at all. Not missing translations — strings that
were never *offered* for translation, so no locale-coverage test could see
them. Five of the thirty-three are aria-labels, where nothing on screen
changes when they stay English; what changes is what a screen reader says,
which is the one audience that hears only these.

WHAT THIS FILE IS BLIND TO:

  * Whether the translations are GOOD. It checks a locale has its own string
    rather than the English one; it cannot check that string is right.
  * The layout. It is deliberately unchanged — rearranging four cards without
    the owner is redesign by assumption.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "templates" / "partials" / "settings" / "_rbac.html"


def _source() -> str:
    return _SRC.read_text(encoding="utf-8")


def _code(src: str | None = None) -> str:
    """Source with every comment syntax blanked.

    All three, because five separate tests in this suite have now fired on
    their own documentation — a guard reading a sentence that *describes* the
    defect as the defect (OPS-LEARNINGS #36). This file's own header says the
    words "Loading…" while explaining that they were removed."""
    src = _source() if src is None else src
    src = re.sub(r"\{#.*?#\}|<!--.*?-->|/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"^[ \t]*//[^\n]*", " ", src, flags=re.M)


def _script() -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", _code(), re.S | re.I))


def _labels_block() -> tuple[str, str]:
    """(the RBAC_T object, everything else in the script).

    The split matters: the English defaults inside `t.get(key, 'English')` are
    the one place English prose belongs, and a scan that cannot tell the
    difference flags the fix as the defect."""
    body = _script()
    start = body.index("const RBAC_T")
    end = body.index("};", start) + 2
    return body[start:end], body[:start] + body[end:]


# Every key the page resolves. Listed rather than scraped: a key that stopped
# being used should fail here, not quietly drop out of a derived set.
_SCRIPT_KEYS = [
    "rbac_badge_backup_admin", "rbac_badge_permissive", "rbac_badge_auth_off",
    "rbac_no_auth", "rbac_failed_to_load", "rbac_no_servers",
    "rbac_permissive_banner", "rbac_acl_forbidden", "rbac_no_acls",
    "rbac_no_acls_hint", "rbac_col_user", "rbac_col_perm", "rbac_col_granted",
    "rbac_revoke", "rbac_revoke_aria", "rbac_revoke_confirm",
    "rbac_revoke_failed", "rbac_no_approvals", "rbac_no_approvals_hint",
    "rbac_approval_line", "rbac_approval_meta", "rbac_approve", "rbac_reject",
    "rbac_approve_aria", "rbac_reject_aria", "rbac_decision_failed",
    "rbac_username_required", "rbac_grant_failed",
]

_ARIA_KEYS = [
    "rbac_refresh_aria", "rbac_username_aria", "rbac_server_aria",
    "rbac_permission_aria", "rbac_grant_aria",
]

_KEYS = _SCRIPT_KEYS + _ARIA_KEYS


# ── the strings that were never offered for translation ───────────────────

def test_every_permissions_string_exists_in_every_locale():
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in _KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated permissions strings: " + ", ".join(missing)


def test_no_locale_silently_reuses_the_english_text():
    """A key present with the English string in it is the same defect wearing
    a coat. Checked on the sentences, where a coincidental match cannot
    happen — a two-word column header legitimately can."""
    from i18n import TRANSLATIONS
    for key in ("rbac_permissive_banner", "rbac_acl_forbidden",
                "rbac_no_acls_hint", "rbac_refresh_aria"):
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, (
                f"{lang}:{key} is the English string")


def test_the_labels_are_defined_once_for_the_script():
    """One object handed to the script rather than a Jinja call inlined at
    each use — the restart overlay's lesson. Eighteen `{{ t.get(…) }}` inside
    JS string literals are eighteen chances to quote a translation wrong in a
    way that breaks exactly one language."""
    assert "const RBAC_T" in _source(), "the label object is gone"
    labels, rest = _labels_block()
    stray = re.findall(r"\{\{\s*t\.get\('([a-z_0-9]+)'", rest)
    assert not stray, (
        "translation calls inlined at their use site rather than read from "
        f"RBAC_T: {stray}")
    assert len(re.findall(r"\{\{\s*t\.get\(", labels)) >= len(_SCRIPT_KEYS), (
        "RBAC_T resolves fewer strings than the page renders")


def _strip_interpolations(text: str) -> str:
    """Remove `${...}` spans, counting braces.

    A non-greedy `\$\{[^}]*\}` stops at the first `}`, and this page has an
    interpolation containing an object literal — so the naive version left the
    object's own keys behind and read them as English."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            out.append(" ")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def test_the_script_renders_no_bare_english_sentence():
    """The scan that found the original thirty-three.

    It looks at what a READER would see, not at string literals: markup tags,
    `${...}` interpolations and Tailwind class tokens are stripped first, and
    what survives has to be two or more ordinary words before it counts as a
    sentence. Scanning literals instead was the first attempt and it flagged
    every class string on the page, which is a guard nobody would keep."""
    _, body = _labels_block()
    suspects = []
    for m in re.finditer(r"'([^'\
]{4,})'|\"([^\"\
]{4,})\"|`([^`]{4,})`", body):
        raw = (m.group(1) or m.group(2) or m.group(3)).strip()
        if raw in ("use strict",):
            continue  # a JS directive, not a string anyone reads
        text = re.sub(r"<[^>]*>", " ", _strip_interpolations(raw))
        words = [w for w in re.split(r"[\s,.:;?!()]+", text)
                 if re.fullmatch(r"[A-Za-z']{3,}", w)]
        if len(words) >= 2:
            suspects.append(raw)
    assert not suspects, (
        "English prose still built into the script rather than read from "
        "RBAC_T: " + "; ".join(repr(x) for x in suspects))


def test_the_sentences_substitute_rather_than_concatenate():
    """Word order is the first thing that stops surviving translation. A
    sentence assembled from fragments can only ever come out in English
    order, so the three with variables in them are templates with named
    placeholders and one filler."""
    from i18n import TRANSLATIONS
    body = _script()
    # `"function rbacFill" in body` was the first version, and the mutation
    # harness renamed the function to rbacFillDisabled and the test still
    # passed — a substring assertion is satisfied by a longer name. Both the
    # definition and the call sites are named exactly now.
    assert re.search(r"function rbacFill\s*\(", body), (
        "the placeholder filler is gone or renamed")
    # Two of the three wrap the template in _escHtml first, so the key
    # is not always the first token after the paren.
    calls = re.findall(r"\brbacFill\([^)]*RBAC_T\.(\w+)", body)
    assert len(calls) >= 3, (
        f"only {len(calls)} sentence(s) go through the filler; the three with "
        "variables in them all should")
    for key, holders in (("rbac_revoke_confirm", ("{user}", "{server}")),
                         ("rbac_approval_line", ("{user}", "{action}", "{server}")),
                         ("rbac_approval_meta", ("{requested}", "{expires}"))):
        for lang, table in TRANSLATIONS.items():
            for h in holders:
                assert h in table[key], f"{lang}:{key} dropped {h}"


def test_every_aria_label_on_the_page_is_translated():
    """These are the ones a sighted reader can never catch, because nothing
    on screen is wrong."""
    src = _code()
    bare = re.findall(r'aria-label="([A-Za-z][^"{]*)"', src)
    assert not bare, "hardcoded English aria-labels: " + ", ".join(bare)


# ── what the move was actually for ────────────────────────────────────────

def test_the_page_uses_the_shared_escaper():
    """A fourth copy of an HTML escaper lived here. Three copies of the same
    six-line function is how `_escHtml is not defined` reached /operations:
    the partial moved, its private copy stayed behind."""
    body = _script()
    assert "function _escHtml" not in body and "_escHtml = function" not in body, (
        "the page carries its own escaper again; base.html defines one")
    assert "_escHtml(" in body, "the page stopped escaping"


def test_every_timestamp_goes_through_the_formatter():
    """Three raw timestamps rendered here in whatever the API returned, while
    every other timestamp in the app converts to the configured timezone."""
    body = _script()
    raw = re.findall(r"_escHtml\((a\.(?:requested_at|expires_at)|[a-z]\.granted_at)\)", body)
    assert not raw, f"timestamps bypassing formatTs: {raw}"
    assert body.count("formatTs(") >= 3, (
        "fewer than three timestamps go through formatTs")


def test_the_loading_states_are_ghosts_rather_than_the_word_loading():
    """Three bare "Loading…" texts. The hand-rolled empty-state ratchet exists
    because each one is a place the app looks unfinished."""
    body = _code()
    assert "Loading…" not in body and "Loading..." not in body, (
        "a bare loading text is back")
    assert _source().count("rbac-ghost") >= 3, "fewer than three skeletons"


def test_the_page_sets_no_colour_from_javascript():
    """Twelve of the fourteen raw colour literals were in script-built class
    strings and one was an inline style, which is the colour ratchet's blind
    spot — it reads templates, and a string assembled at runtime is not one."""
    body = _script()
    hits = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    assert not hits, f"raw colour literals in script: {hits}"
    assert "style.color" not in body and 'style="color:' not in body, (
        "colour set from JS rather than a token class")


# ── it is reachable, and the old address still works ──────────────────────

@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


def test_the_old_rbac_address_still_resolves(client):
    """/admin/rbac was linked from the sidebar for a year; a bookmark is not
    a reason to keep a nav entry, but it is a reason to keep the URL."""
    r = client.get("/admin/rbac")
    assert r.status_code == 301, f"/admin/rbac -> {r.status_code}"
    assert r.headers["Location"].endswith("/settings/rbac")


def test_the_permissions_section_renders(client):
    """The direct reason this file exists: the page 500'd on a Jinja syntax
    error in one of my own comments, and every test in the suite stayed
    green."""
    r = client.get("/settings/rbac")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Something went wrong" not in body
    assert "const RBAC_T" in body, "the labels did not reach the page"


def test_the_labels_survive_a_non_english_locale(client, monkeypatch):
    """`t.get(key, 'English')` never raises, so a page whose labels all fell
    back to English looks identical to a translated one — which is exactly how
    thirty-three untranslated strings survived here for a year.

    The language comes from the config setting, not from Accept-Language, so
    the setting is what gets swapped. Read-only: `get_settings` is patched to
    return a copy with one key changed, and nothing is written."""
    import app as prism_app
    from i18n import TRANSLATIONS
    real = prism_app.config.get_settings()
    monkeypatch.setattr(prism_app.config, "get_settings",
                        lambda: {**real, "language": "de"})
    r = client.get("/settings/rbac")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'lang="de"' in body, "the language setting did not reach the page"
    for key in ("rbac_no_acls_hint", "rbac_permissive_banner", "rbac_refresh_aria"):
        # The longest plain-ASCII run, because the labels reach the page
        # through `| tojson` (which escapes an em-dash to \u2014) and through
        # HTML attributes (which escape quotes) — comparing the whole string
        # would be testing the escaping, not the translation.
        runs = re.findall(r"[A-Za-z ,-]{12,}", TRANSLATIONS["de"][key])
        assert runs, f"no ASCII-stable fragment in de:{key} to look for"
        needle = max(runs, key=len).strip()
        assert needle in body, (
            f"{key} rendered as English on a German page (looked for "
            f"{needle!r})")
