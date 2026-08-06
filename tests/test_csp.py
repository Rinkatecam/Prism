"""Tests for the Sprint-3 CSP hardening (S3-9 / W2 from AUDIT-2026-05).

The inline-handler sweep has LANDED, so script-src is now nonce-based:
'unsafe-inline' is GONE and 'nonce-{nonce}' is advertised.

Preconditions that make this safe (all verified below):
  * g.csp_nonce is set per-request in app.py before_request; inject_locale
    exposes it as csp_nonce; every inline <script> renders nonce="{{ csp_nonce }}".
  * All inline on*= event handlers were converted to a delegated
    data-action / data-change / data-input / data-mousedown dispatcher
    (templates/base.html) — 0 inline handlers remain in rendered pages.
  * The two HTMX-swapped partials (critical_issues, incidents_panel) had their
    inline <script> hoisted into base.html; a swapped fragment carries a fresh
    per-request nonce that would not match the host page's nonce, so those
    partials are script-free.

  * style-src deliberately keeps 'unsafe-inline' — Tailwind's CDN runtime,
    dynamic status-badge gradients, and inline style="..." attributes from
    template-rendered widgets all rely on it. Documented in app.py.

These tests load the real app module (with its CSP after_request hook)
rather than constructing a fresh Flask instance — the header lives on
the production app object.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# /login is the canonical unauthenticated probe surface. NOTE: when auth is
# disabled (the default — and the CI state, where config.json is absent) a GET
# /login 302-redirects to "/", which in turn lands on the dashboard or first-run
# /setup page. The CSP *header* is emitted by the after_request hook on the
# redirect response too, so the header-only tests below probe _PROBE_PATH
# directly. Tests that need the rendered HTML body (the nonce on an inline
# <script>) pass follow_redirects=True to reach the page that actually renders it.
_PROBE_PATH = "/login"


@pytest.fixture()
def app_client():
    """The real Flask app's test_client.

    We import lazily inside the fixture so module-level side-effects (DB
    init, daemon thread launch) only happen for tests that need them.
    """
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _csp(response) -> str:
    """Return the CSP header string from a response or fail clearly."""
    csp = response.headers.get("Content-Security-Policy")
    assert csp is not None, "no Content-Security-Policy header on response"
    return csp


def _directive(csp: str, name: str) -> str:
    """Extract a single directive's value list (everything after `name `)."""
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " ") or part == name:
            return part
    raise AssertionError(f"directive {name!r} not in CSP: {csp!r}")


# ── 1. Header is present ──────────────────────────────────────────────────
def test_csp_header_present(app_client):
    r = app_client.get(_PROBE_PATH)
    assert "Content-Security-Policy" in r.headers


# ── 2. nonce wiring is alive in rendered templates ────────────────────────
_NONCE_ATTR_RE = re.compile(r'<script[^>]*\bnonce="([A-Za-z0-9_\-]+)"', re.IGNORECASE)
_SCRIPT_NONCE_IN_SRC_RE = re.compile(r"'nonce-([A-Za-z0-9_\-]+)'")


def test_csp_nonce_present_in_rendered_html(app_client):
    r = app_client.get(_PROBE_PATH, follow_redirects=True)
    body = r.get_data(as_text=True)
    m = _NONCE_ATTR_RE.search(body)
    assert m, "no <script nonce='...'> in rendered /login"
    assert len(m.group(1)) >= 16, (
        f"nonce too short to be from secrets.token_urlsafe(16): {m.group(1)!r}"
    )


# ── 3. nonce rotates per request ──────────────────────────────────────────
def test_csp_nonce_per_request(app_client):
    r1 = app_client.get(_PROBE_PATH, follow_redirects=True)
    r2 = app_client.get(_PROBE_PATH, follow_redirects=True)
    n1 = _NONCE_ATTR_RE.search(r1.get_data(as_text=True))
    n2 = _NONCE_ATTR_RE.search(r2.get_data(as_text=True))
    assert n1 and n2, "both responses must contain a rendered nonce"
    assert n1.group(1) != n2.group(1), (
        f"nonce did not rotate between requests: {n1.group(1)!r}"
    )


# ── 4. style-src keeps 'unsafe-inline' (intentional concession) ───────────
def test_csp_style_src_keeps_unsafe_inline(app_client):
    r = app_client.get(_PROBE_PATH)
    style_src = _directive(_csp(r), "style-src")
    assert "'unsafe-inline'" in style_src, (
        f"style-src must keep 'unsafe-inline' for Tailwind/badges: {style_src!r}"
    )


# ── 5. script-src post-sweep shape: nonce YES, 'unsafe-inline' NO ─────────
def test_csp_script_src_is_nonce_based(app_client):
    r = app_client.get(_PROBE_PATH)
    script_src = _directive(_csp(r), "script-src")
    # 'unsafe-inline' is GONE — the inline-handler sweep landed.
    assert "'unsafe-inline'" not in script_src, (
        f"'unsafe-inline' must be removed now that inline handlers are gone: "
        f"{script_src!r}"
    )
    # nonce-source is advertised.
    assert _SCRIPT_NONCE_IN_SRC_RE.search(script_src), (
        f"script-src must advertise 'nonce-...': {script_src!r}"
    )
    # 'strict-dynamic' stays OUT — it would disable the CDN host allowlist.
    assert "'strict-dynamic'" not in script_src, (
        f"'strict-dynamic' would disable the CDN host allowlist: {script_src!r}"
    )
    # CDN host-sources survive (a nonce does NOT disable host allowlists).
    for host in (
        "https://cdn.tailwindcss.com",
        "https://unpkg.com",
        "https://cdn.jsdelivr.net",
    ):
        assert host in script_src, f"CDN host {host} dropped from script-src: {script_src!r}"


# ── 6. The advertised nonce matches the nonce on the rendered inline script ─
# This is the load-bearing correctness check: if the header nonce and the
# template nonce ever diverge, every inline <script> silently stops executing.
def test_csp_header_nonce_matches_body_nonce(app_client):
    r = app_client.get(_PROBE_PATH, follow_redirects=True)
    header_nonce = _SCRIPT_NONCE_IN_SRC_RE.search(_csp(r))
    body_nonce = _NONCE_ATTR_RE.search(r.get_data(as_text=True))
    assert header_nonce and body_nonce, "need a nonce in both header and body"
    assert header_nonce.group(1) == body_nonce.group(1), (
        "CSP header nonce %r != rendered <script> nonce %r — inline scripts "
        "would be blocked" % (header_nonce.group(1), body_nonce.group(1))
    )


# ── 7. No inline event handlers survive in rendered pages ─────────────────
_INLINE_HANDLER_RE = re.compile(
    r'\son(click|change|submit|input|mousedown|mouseup|mouseenter|mouseleave|'
    r'mouseover|focus|blur|load|error|keydown|keyup|keypress)="',
    re.IGNORECASE,
)
_RENDERED_PAGES = ["/", "/servers", "/workflows", "/operations", "/settings", "/reports", "/monitoring"]


def _real_script_open_tags(html: str):
    """Yield each real <script ...> opening tag, skipping any '<script' that
    appears inside another script's text content (a JS string or comment)."""
    low = html.lower()
    i = 0
    while True:
        j = low.find("<script", i)
        if j == -1:
            return
        k = html.find(">", j)
        if k == -1:
            return
        yield html[j : k + 1]
        end = low.find("</script>", k)
        i = end + len("</script>") if end != -1 else k + 1


@pytest.mark.parametrize("path", _RENDERED_PAGES)
def test_rendered_pages_have_no_inline_handlers(app_client, path):
    r = app_client.get(path, follow_redirects=True)
    if r.status_code != 200:
        pytest.skip(f"{path} returned {r.status_code} (auth/route unavailable in this env)")
    body = r.get_data(as_text=True)
    hits = _INLINE_HANDLER_RE.findall(body)
    assert not hits, f"{path} still has inline on*= handlers: {hits[:5]}"
    # And every inline <script> must carry a nonce (no bare inline scripts).
    for tag in _real_script_open_tags(body):
        if "src=" in tag:
            continue
        assert "nonce=" in tag, f"{path} has a nonce-less inline <script>: {tag!r}"


# ── 8. HTMX-swapped partials are script-free ──────────────────────────────
# A swapped fragment gets a fresh per-request nonce that will not match the host
# page's nonce, so any inline <script> in the fragment would be blocked. This is
# checked against the template SOURCE (Jinja comments stripped, since the prose
# mentions "<script>") so it holds regardless of runtime config. (Hitting the
# route with follow_redirects on an unconfigured instance — e.g. CI with no
# config.json — lands on a full setup page that legitimately carries scripts,
# which is not the invariant under test.)
_SWAPPED_PARTIALS = [
    "templates/partials/critical_issues.html",
    "templates/partials/incidents_panel.html",
]


@pytest.mark.parametrize("rel", _SWAPPED_PARTIALS)
def test_swapped_partials_are_script_free(rel):
    src = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
    src = re.sub(r"{#.*?#}", "", src, flags=re.DOTALL)  # drop Jinja comments
    assert "<script" not in src.lower(), (
        f"{rel}: an HTMX-swapped partial must ship no <script> tag — a fragment's "
        f"fresh per-request nonce won't match the host page's CSP nonce. Host the "
        f"JS in base.html instead."
    )


# ── 9. Other security directives we depend on are intact ──────────────────
def test_csp_other_directives_intact(app_client):
    r = app_client.get(_PROBE_PATH)
    csp = _csp(r)
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "default-src 'self'" in csp
