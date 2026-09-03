"""The jump-to index — WP-4 D8.

What an operator wants from a search box in an ops dashboard is not
full-text: it is "get me to FILE01", "where did the TLS setting go", "take
me to the audit trail". So this builds a list of DESTINATIONS — things with a
name and a URL — rather than an index of content.

FOUR SOURCES, and the difference between them matters:

  * **Pages** and **settings sections** come from the router's own tuples.
    Declared lists drift; the router cannot, because it is what serves them.

  * **Servers** come from the configuration, which is what the rest of the app
    treats as the fleet's definition.

  * **Headings** are read from RENDERED pages, not parsed out of the
    templates. Roughly a tenth of this app's headings are concatenations,
    `| default(...)` filters or `is defined` guards with a badge appended —
    a Jinja parser would resolve most of them and quietly mis-resolve the
    rest, and "quietly wrong" is the worst failure mode for a navigation
    aid. Rendering also means the index is in the operator's own language
    for free, because the page already was.

WHAT THIS IS BLIND TO, deliberately:

  * Page CONTENT. Searching for a server's IP or an error message finds
    nothing. This is a jump-to, and pretending otherwise would be worse than
    the honest limit.
  * Pages behind a parameter (`/server/<name>`) beyond the server list
    itself, and pages a crawl cannot reach without side effects.
"""

from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

# The crawl renders pages, and a rendered page must never trigger a crawl.
# Without this a single template referencing the index would recurse until the
# stack ran out — and it would do it inside a request, so the symptom would be
# a hung page rather than an obvious error.
_building = threading.local()

_cache: dict[str, list[dict]] = {}
_cache_lock = threading.Lock()

# Routes worth crawling for headings. Not derived from the URL map: a crawl
# GETs the page, and "every GET route" includes ones with side effects or
# costs that a search box has no business paying. Listed, with the rule that
# a new page joins this list deliberately.
_CRAWLABLE = (
    "/", "/servers", "/monitoring", "/operations", "/workflows",
    "/reports", "/topology", "/services", "/network", "/scan",
)

# Headings that are furniture rather than destinations. A jump-to entry for
# "Loading" or a bare count helps nobody.
_SKIP_HEADINGS = {"prism", "loading", "loading…", "…", "-", "--"}


def _headings(html: str) -> list[tuple[int, str]]:
    """(level, text) for every h1/h2 in a rendered page.

    Tags inside the heading are dropped rather than the whole heading being
    skipped: most carry an icon, and several carry a live count badge whose
    number would otherwise end up in the index."""
    out = []
    for m in re.finditer(r"<h([12])\b[^>]*>(.*?)</h\1>", html, re.S | re.I):
        # WP-6 C17/§6.3 — a tooltip's sr-only mirror is a sibling of its
        # trigger, never inside a heading, precisely so it cannot reach here.
        # Stripped by CONTENT (not just tags) as belt-and-braces: were one
        # ever nested inside a heading anyway, its explanatory text must not
        # pollute the jump-to label or push the heading past the 80-char drop.
        text = re.sub(r'<span[^>]*\bclass="[^"]*\bsr-only\b[^"]*"[^>]*>.*?</span>',
                      " ", m.group(2), flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        # Live counts are state, not part of the name. "Servers (29)" and
        # "1 Critical" are both a heading with a number stapled on, and an
        # index that keeps the number is stale the moment it is built.
        text = re.sub(r"\s*\(\d+\)\s*$", "", text)
        text = re.sub(r"^\d+\s+(?=[A-Za-z])", "", text)
        text = text.strip()
        if not text or text.lower() in _SKIP_HEADINGS:
            continue
        if len(text) > 80:  # a paragraph in a heading tag is not a destination
            continue
        out.append((int(m.group(1)), text))
    return out


def _anchor_for(html: str, text: str) -> str | None:
    """An id on the heading, so the jump lands on the section not the page.

    WP-6 C11 — narrowed from `<h[12]` to `<h2\\b`. The base H1 now carries
    `id="page-title"` and is first in document order (`<header>` precedes
    `<main>`), so the old pattern would resolve a page's own title heading
    — "Servers (29)" normalised to "Servers" against a page title of
    "Servers" — to `#page-title`, a link that scrolls the operator to the
    top bar instead of the section they asked for. h1 is never indexed as
    a heading by `_headings` (`build()` reads it separately, as the page
    name), so it never needs an anchor of its own."""
    m = re.search(
        r'<h2\b[^>]*\bid="([^"]+)"[^>]*>(?:(?!</h2>).)*?'
        + re.escape(text[:20]), html, re.S | re.I)
    return m.group(1) if m else None


def build(app, settings: dict | None = None) -> list[dict]:
    """Every jump target, as {label, sublabel, url, kind}.

    `app` is passed in rather than imported: importing the Flask app from a
    module the app itself imports is a cycle, and the one place this is called
    from already has it."""
    if getattr(_building, "active", False):
        # A crawled page asked for the index. Answer empty rather than recurse.
        return []

    _building.active = True
    entries: list[dict] = []
    try:
        from routes.views import _SETTINGS_SECTIONS

        # ── settings sections ──────────────────────────────────────────
        # Labels come from the page itself for the same reason headings do:
        # the nav's label map is Jinja, and re-implementing it here would be
        # a second source of truth for the same nine words.
        client = app.test_client()
        for name in _SETTINGS_SECTIONS:
            entries.append({
                "label": name.replace("_", " ").title(),
                "sublabel": "Settings",
                "url": f"/settings/{name}",
                "kind": "settings",
            })

        # ── servers ────────────────────────────────────────────────────
        try:
            import app as prism_app
            for server in prism_app.config.get_servers():
                name = getattr(server, "name", None) or str(server)
                entries.append({
                    "label": name,
                    "sublabel": getattr(server, "type", "") or "Server",
                    "url": f"/server/{name}",
                    "kind": "server",
                })
        except Exception:
            logger.exception("jump index: could not read the server list")

        # ── pages and their headings ───────────────────────────────────
        # Settings sub-pages are crawled too, and they are the reason this
        # crawls at all: "where did the TLS setting go" is the question a
        # jump-to has to answer, and TLS is an h2 inside a section, not a
        # section name.
        for path in list(_CRAWLABLE) + [f"/settings/{n}" for n in _SETTINGS_SECTIONS]:
            try:
                resp = client.get(path)
                if resp.status_code != 200:
                    continue
                html = resp.get_data(as_text=True)
            except Exception:
                logger.exception("jump index: %s did not render", path)
                continue

            found = _headings(html)
            page_name = next((t for lvl, t in found if lvl == 1), path)
            if not path.startswith("/settings/"):
                entries.append({
                    "label": page_name,
                    "sublabel": "Page",
                    "url": path,
                    "kind": "page",
                })
            else:
                # Its h2s belong to the section, named as the nav names it.
                page_name = path.rsplit("/", 1)[1].replace("_", " ").title()
            for lvl, text in found:
                if lvl == 1:
                    continue
                anchor = _anchor_for(html, text)
                entries.append({
                    "label": text,
                    "sublabel": page_name,
                    "url": f"{path}#{anchor}" if anchor else path,
                    "kind": "heading",
                })
    finally:
        _building.active = False

    # Two pages can carry the same heading ("Overview"); the first wins, and
    # the sublabel is what tells them apart when both survive.
    seen = set()
    deduped = []
    for e in entries:
        key = (e["label"].lower(), e["url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def get(app, lang: str) -> list[dict]:
    """The index for a language, built once.

    Keyed by language because the headings are rendered, so the index IS the
    translation. Cleared by `invalidate` when the fleet or the language
    changes — a server added an hour ago that the search cannot find reads as
    the search being broken."""
    with _cache_lock:
        if lang in _cache:
            return _cache[lang]
    built = build(app)
    with _cache_lock:
        _cache[lang] = built
    return built


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def search(entries: list[dict], query: str, limit: int = 12) -> list[dict]:
    """Rank by how the operator typed it, not by string distance.

    A prefix match beats a word-start match beats a substring. Fuzzy matching
    is deliberately absent: on a list of a few hundred short names it turns
    a wrong keystroke into a confident wrong answer, and the operator can see
    the whole list is short."""
    q = (query or "").strip().lower()
    if not q:
        return []

    scored = []
    for e in entries:
        label = e["label"].lower()
        if label.startswith(q):
            rank = 0
        elif re.search(r"\b" + re.escape(q), label):
            rank = 1
        elif q in label:
            rank = 2
        elif q in (e.get("sublabel") or "").lower():
            rank = 3
        else:
            continue
        # Servers first within a rank: on this kind of dashboard a name is
        # almost always a host.
        kind_rank = {"server": 0, "settings": 1, "page": 2, "heading": 3}
        scored.append((rank, kind_rank.get(e["kind"], 4), len(label), e))

    scored.sort(key=lambda t: t[:3])
    return [e for _, _, _, e in scored[:limit]]
