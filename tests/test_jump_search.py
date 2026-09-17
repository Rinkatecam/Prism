"""Jump-to search — WP-4 D8.

The last slice of the package, and the only one that indexes the information
architecture rather than moving it. It was put last for that reason: an index
built before the IA settled would have indexed a structure about to change.

THE INDEX IS CRAWLED, NOT DECLARED, and that is the design decision worth
defending. Roughly a tenth of this app's headings are concatenations,
`| default(...)` filters or `is defined` guards with a live badge appended.
A Jinja parser would resolve most of them and quietly mis-resolve the rest,
and "quietly wrong" is the worst failure mode a navigation aid has. Rendering
the pages also makes the index arrive in the operator's own language, because
the page already was.

It also means the index cannot go stale against the IA: the very first crawl
found that `/servers` still carries Tag Management, Health Checks and
Dependencies — configuration the plan's D4 says should have moved to Settings,
in a slice that was never done. An index built from a declared list would have
listed what the plan intended. This one listed what the app actually is.

WHAT THIS IS BLIND TO:

  * Page CONTENT. An IP address, an error message, a table cell: none of it
    is indexed, deliberately.
  * Anything below h2. `/operations`' audit trail is an h3, so "audit" finds
    nothing — an honest limit of the brief rather than a bug, and the empty
    state says what the search does cover.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent / "templates" / "base.html"


@pytest.fixture(scope="module")
def app_obj():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app


@pytest.fixture(scope="module")
def index(app_obj):
    import search_index
    return search_index.build(app_obj)


@pytest.fixture(scope="module")
def client(app_obj):
    return app_obj.test_client()


# ── the index ─────────────────────────────────────────────────────────────

def test_the_index_covers_all_four_kinds(index):
    kinds = {e["kind"] for e in index}
    assert kinds == {"server", "settings", "page", "heading"}, sorted(kinds)


def test_every_settings_section_is_reachable_by_name(index):
    """The question the search exists to answer is "where did that setting
    go", so every section has to be findable by its own name."""
    from routes.views import _SETTINGS_SECTIONS
    urls = {e["url"] for e in index}
    missing = [n for n in _SETTINGS_SECTIONS if f"/settings/{n}" not in urls]
    assert not missing, f"settings sections absent from the index: {missing}"


def test_every_configured_server_is_in_the_index(index, app_obj):
    import app as prism_app
    names = {getattr(s, "name", None) for s in prism_app.config.get_servers()}
    indexed = {e["label"] for e in index if e["kind"] == "server"}
    assert names <= indexed, f"servers absent from the index: {names - indexed}"


def test_every_entry_has_a_label_and_a_url_that_resolves(index, app_obj):
    rules = [re.compile("^" + re.sub(r"<[^>]+>", "[^/]+", r.rule) + "$")
             for r in app_obj.url_map.iter_rules()]
    bad = []
    for e in index:
        if not e.get("label") or not e.get("url"):
            bad.append(e)
            continue
        path = e["url"].split("#")[0]
        if not any(rx.match(path) for rx in rules):
            bad.append(e)
    assert not bad, f"index entries that go nowhere: {bad[:5]}"


def test_live_counts_are_not_baked_into_the_labels(index):
    """"Servers (29)" and "1 Critical" are a heading with a number stapled on.
    An index that keeps the number is wrong the moment a server is added, and
    wrong in the way that makes an operator distrust the whole box."""
    offenders = [e["label"] for e in index
                 if re.search(r"\(\d+\)\s*$", e["label"])
                 or re.match(r"^\d+\s+[A-Za-z]", e["label"])]
    assert not offenders, f"live counts in index labels: {offenders}"


def test_a_heading_inside_a_settings_section_is_findable(index):
    """The specific case that justifies crawling rather than listing sections:
    TLS is an h2 inside Alerts, so a section-name-only index cannot find it,
    and "where did the TLS setting go" is exactly the question asked."""
    import search_index
    hits = search_index.search(index, "tls")
    assert hits, "nothing matches 'tls'"
    assert any(h["url"].startswith("/settings/") for h in hits), (
        f"'tls' found only {[h['url'] for h in hits]}")


def test_a_page_entry_never_falls_back_to_its_path(index):
    """WP-6 DESIGN_SYSTEM_SPEC.md §6.1. `build()`'s page name is
    `next((t for lvl, t in found if lvl == 1), path)` -- the H1 stays in
    the DOM on every page (CSS-clipped on '/', per #page-title[data-home],
    never deleted) precisely so this fallback is never actually taken. Had
    the dashboard's `<h1 class="sr-only">` been deleted instead of
    consolidated into base.html's always-present topbar H1 (C22), '/'
    would index as {"label": "/", "kind": "page"} -- an entry that still
    satisfies test_every_entry_has_a_label_and_a_url_that_resolves above
    while making "dashboard" unfindable by its own name."""
    offenders = [e for e in index if e["kind"] == "page" and e["label"] == e["url"]]
    assert not offenders, (
        f"page entry/ies fell back to their own path instead of reading "
        f"an H1: {offenders}")

    dashboard = [e for e in index if e["kind"] == "page" and e["url"] == "/"]
    assert dashboard, "no page entry indexed for '/' at all"
    assert dashboard[0]["label"] != "/", (
        "the dashboard's index label is its own path -- the H1 fallback "
        "was taken")


def test_an_anchor_never_points_at_the_top_bar(index):
    """WP-6 DESIGN_SYSTEM_SPEC.md §6.2/C11. `_anchor_for` is narrowed to
    `<h2\\b` (never h1, never h3) specifically so it can never resolve to
    `#page-title` -- the id the new topbar H1 carries. Before that
    narrowing, `<h[12]` would have matched the H1 itself whenever a page's
    title happened to prefix-match one of its own headings (e.g.
    '/servers' normalises "Servers (29)" to "Servers", the same word the
    page title already is), producing a jump-to entry that scrolls the
    operator to the top bar instead of the section they searched for."""
    offenders = [e["url"] for e in index if e["url"].endswith("#page-title")]
    assert not offenders, f"an index entry anchors to the top bar: {offenders}"

    # Direct proof at the function level -- not just "the built index
    # happens not to contain one" -- that _anchor_for prefers a real h2
    # over the top bar even when the h1 shares the exact same text.
    import search_index
    both = '<h1 id="page-title">Servers</h1><main><h2 id="real">Servers</h2></main>'
    assert search_index._anchor_for(both, "Servers") == "real", (
        "_anchor_for no longer prefers a real h2 over the top bar when "
        "both share the same text")
    h1_only = '<h1 id="page-title">Servers</h1>'
    assert search_index._anchor_for(h1_only, "Servers") is None, (
        "_anchor_for resolved to the top bar's own id -- the <h2\\b "
        "narrowing (C11) has regressed back to matching <h1")


def test_the_crawl_cannot_recurse(app_obj):
    """The index is built by rendering pages. A rendered page asking for the
    index would recurse until the stack ran out — and inside a request, so the
    symptom is a hung page rather than an error anyone can read."""
    import search_index
    search_index._building.active = True
    try:
        assert search_index.build(app_obj) == []
    finally:
        search_index._building.active = False


# ── the ranking ───────────────────────────────────────────────────────────

def test_an_empty_query_returns_nothing(index):
    import search_index
    assert search_index.search(index, "") == []
    assert search_index.search(index, "   ") == []


def test_a_prefix_match_outranks_a_substring(index):
    import search_index
    entries = [
        {"label": "Maintenance Windows", "sublabel": "", "url": "/a", "kind": "heading"},
        {"label": "Windows Updates", "sublabel": "", "url": "/b", "kind": "heading"},
    ]
    assert search_index.search(entries, "windows")[0]["url"] == "/b", (
        "the entry that STARTS with the query should come first")


def test_a_server_outranks_a_heading_at_the_same_strength(index):
    """On this kind of dashboard a typed name is almost always a host."""
    import search_index
    entries = [
        {"label": "Reports", "sublabel": "", "url": "/reports", "kind": "page"},
        {"label": "REPORTSRV01", "sublabel": "", "url": "/server/REPORTSRV01", "kind": "server"},
    ]
    assert search_index.search(entries, "report")[0]["kind"] == "server"


def test_the_result_list_is_capped(index):
    import search_index
    assert len(search_index.search(index, "e", limit=5)) <= 5


# ── the endpoint ──────────────────────────────────────────────────────────

def test_the_endpoint_answers_a_query(client):
    r = client.get("/api/search?q=set")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert isinstance(body["results"], list)


def test_the_endpoint_does_not_dump_the_index_when_asked_nothing(client):
    """A box that lists eighty destinations the moment it is focused has
    answered a question nobody asked."""
    assert client.get("/api/search?q=").get_json()["results"] == []
    assert client.get("/api/search").get_json()["results"] == []


# ── the control ───────────────────────────────────────────────────────────

def _topbar() -> str:
    src = _BASE.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    return src[src.index('<header id="topbar"'):src.index("</header>")]


def test_the_box_is_a_combobox_and_says_so():
    bar = _topbar()
    assert 'role="combobox"' in bar
    assert 'aria-expanded="false"' in bar, "the initial state is not announced"
    assert 'aria-controls="jump-results"' in bar
    assert 'role="listbox"' in bar


def test_the_highlight_is_named_and_not_only_coloured():
    """Arrow keys moving a highlight that assistive technology cannot follow
    is the standard way this control is built wrong: the sighted user watches
    the selection move and nobody else knows it did."""
    src = _BASE.read_text(encoding="utf-8")
    block = src[src.index("Jump-to (WP-4 D8)"):]
    assert re.search(r"setAttribute\('aria-activedescendant',\s*rows\[i\]\.id\)", block), (
        "the highlighted row is not named to assistive technology")
    assert "aria-selected" in block


def test_the_results_are_escaped_before_they_are_drawn():
    """Server names come from configuration and headings come from rendered
    pages, so neither is beyond an operator's reach."""
    src = _BASE.read_text(encoding="utf-8")
    block = src[src.index("Jump-to (WP-4 D8)"):]
    drawn = re.findall(r"'\s*\+\s*(r\.\w+|T\.\w+|ICON\[[^\]]+\][^+]*)", block)
    unescaped = [d for d in drawn if "_escHtml" not in d]
    assert not unescaped, f"values interpolated without escaping: {unescaped}"


def test_a_stale_response_cannot_overwrite_a_newer_one():
    """Type "des", then "desfil": two requests are in flight and the slower
    one may land last. Without a sequence guard the panel shows the results
    for a query the operator has already moved past."""
    src = _BASE.read_text(encoding="utf-8")
    block = src[src.index("Jump-to (WP-4 D8)"):]
    assert "const mine = ++seq" in block, "the request is not sequenced at all"

    # Every line that draws inside the fetch chain has to check first. The
    # first version of this test asserted the guard string appeared SOMEWHERE
    # in the block, and the harness removed it from the success path — where
    # it matters — while the copy in the error path kept the test green.
    ask = block[block.index("function ask()"):]
    ask = ask[:ask.index("\n    }")]
    draws = [line.strip() for line in ask.splitlines() if "draw(" in line]
    assert draws, "ask() no longer draws anything"
    unguarded = [line for line in draws if "mine === seq" not in line]
    assert not unguarded, (
        "responses drawn without checking they are still the current query: "
        + "; ".join(unguarded))


def test_the_slash_shortcut_does_not_steal_a_typed_slash():
    src = _BASE.read_text(encoding="utf-8")
    block = src[src.index("Jump-to (WP-4 D8)"):]
    assert "isContentEditable" in block and "INPUT|TEXTAREA|SELECT" in block, (
        "'/' would be captured while the operator is typing in a field")


def test_the_empty_state_says_what_the_search_does_not_cover():
    """The one honest thing to say when a search finds nothing is what it
    would have found. Without it, "Nothing matches" reads as "this server
    does not exist"."""
    from i18n import TRANSLATIONS
    hint = TRANSLATIONS["en"]["jump_no_results_hint"]
    assert "content" in hint.lower()


def test_no_script_tag_carries_both_a_src_and_inline_code():
    """The defect that got past twenty-two passing tests.

    This block was first appended before the last `</script>` in base.html,
    which belonged to a `<script src="...">`. The browser IGNORES inline
    content inside a src tag, so the code parsed, carried a valid CSP nonce,
    appeared in `document.scripts`, and never executed once. The search box
    rendered perfectly and did nothing at all.

    Every other test in this file reads the template as TEXT, and the text was
    exactly right. Only opening the page found it, which is the argument for
    live verification in one sentence: a file-level test cannot tell you
    whether the browser will RUN what it is reading.

    Checked across every template, because appending to a file rather than to
    a tag is a mistake anyone makes once."""
    root = Path(__file__).resolve().parent.parent / "templates"
    pattern = re.compile(r"<script\b([^>]*\bsrc=[^>]*)>(.*?)</script>",
                         re.S | re.I)
    offenders = []
    for tpl in sorted(root.rglob("*.html")):
        # Comments first. This guard's own explanation quotes the markup it
        # warns about, so the unblanked version reported the documentation as
        # the defect - OPS-LEARNINGS #36, again.
        src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ",
                     tpl.read_text(encoding="utf-8"), flags=re.S)
        for m in pattern.finditer(src):
            body = m.group(2).strip()
            if body:
                offenders.append(f"{tpl.name}: {body[:60]}")
    assert not offenders, (
        "script tags with BOTH src and inline code - the inline half never "
        "runs:\n  " + "\n  ".join(offenders))


# ── the strings ───────────────────────────────────────────────────────────

_KEYS = ["jump_placeholder", "jump_aria", "jump_aria_results",
         "jump_no_results", "jump_no_results_hint"]


def test_every_string_exists_in_every_locale():
    from i18n import TRANSLATIONS
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in _KEYS
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated jump-to strings: " + ", ".join(missing)


def test_no_locale_silently_reuses_the_english_text():
    from i18n import TRANSLATIONS
    for key in _KEYS:
        english = TRANSLATIONS["en"][key]
        for lang in TRANSLATIONS:
            if lang == "en":
                continue
            assert TRANSLATIONS[lang][key] != english, f"{lang}:{key} is English"


def test_the_top_bar_carries_no_hardcoded_english():
    """Scoped to the whole top bar rather than the new box.

    It was scoped to the box first and failed on `aria-label="Toggle dark
    mode"` next door — a hardcoded label that predated this slice by a long
    way, alongside two more and the connection-lost banner, which every
    operator sees on every blip in English regardless of locale. Five more
    strings that had never been offered for translation, found because a test
    reached two elements further than intended."""
    bar = _topbar()
    for attr in ("placeholder", "aria-label", "title"):
        bare = re.findall(attr + r'="([A-Za-z][^"{]*)"', bar)
        assert not bare, f"hardcoded English {attr} in the top bar: {bare}"
