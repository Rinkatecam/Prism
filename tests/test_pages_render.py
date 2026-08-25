"""Every page route renders. Not "responds" — renders.

This file exists because a full green suite sat on top of a page that was
returning an error document. `/settings/rbac` had a Jinja syntax error, its
view caught the exception and returned `render_template("500.html")`, and
Flask answers a bare string with **HTTP 200** — so the route reported success
while serving "Something went wrong", and the test that checked every settings
section returned 200 was satisfied.

Two things were wrong and both are fixed:

  * the views' error path returned 200; it returns 500 now, in all fifteen of
    them;
  * nothing rendered these templates through Jinja at all. The suite has tests
    that render the dashboard and /servers against a stub base, and route
    tests that check status codes, and between them a template could fail to
    compile without a single failure.

WHAT THIS IS BLIND TO:

  * Whether the page is CORRECT. It renders every route and checks the
    response is not an error document. What the page then says is the job of
    the tests that read it.
  * Routes needing parameters that only exist on a live estate. Those are
    listed with the value used, so a page is never skipped silently.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


def _page_routes() -> list[str]:
    """Every GET route that returns a page, from the app's own URL map.

    Derived rather than listed: a route added without a test here would
    otherwise be a page nobody renders, which is the state this file was
    written to end."""
    import app as prism_app
    out = []
    for rule in prism_app.app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        path = rule.rule
        if path.startswith(("/api/", "/static/", "/partials/")):
            continue
        if path in ("/logout", "/login", "/setup"):
            continue  # auth flows: they redirect by design
        if "<" in path:
            continue  # parameterised — see the explicit list below
        out.append(path)
    return sorted(set(out))


# Routes that take a parameter, with a value that must resolve on any install.
_PARAMETERISED = [
    "/settings/general", "/settings/collector", "/settings/detection",
    "/settings/alerts", "/settings/operations", "/settings/security",
    "/settings/rbac", "/settings/notifications", "/settings/display",
]


# Routes that legitimately 404 on an install where their module is switched
# off. A reason per entry, because "it 404s" is also what a broken route says.
_OPTIONAL_MODULES = {
    "/compliance": "the compliance module is per-customer; its routes 404 when "
                   "csv_compliance.is_compliance_enabled() is false, and the "
                   "nav entry is hidden to match",
}


@pytest.mark.parametrize("path", _page_routes())
def test_every_page_route_renders(client, path):
    r = client.get(path)
    allowed = (200, 301, 302) + ((404,) if path in _OPTIONAL_MODULES else ())
    assert r.status_code in allowed, f"{path} -> {r.status_code}"
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        assert "Something went wrong" not in body, (
            f"{path} rendered the error page with a 200")


def test_the_optional_module_exemptions_carry_a_reason():
    assert all(reason.strip() for reason in _OPTIONAL_MODULES.values())


@pytest.mark.parametrize("path", _PARAMETERISED)
def test_every_settings_section_renders(client, path):
    """The sections are listed rather than derived because they are the
    parameter's whole domain, and a section that stopped rendering is exactly
    what got past the suite once."""
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    body = r.get_data(as_text=True)
    assert "Something went wrong" not in body, (
        f"{path} rendered the error page with a 200")
    assert 'data-settings-section=' in body, f"{path} rendered no section"


def test_the_error_path_answers_with_an_error_status():
    """The half that made the above invisible. `render_template("500.html")`
    on its own is a 200 — Flask reads a bare string as a body and nothing
    else. Asserted against the source of every view rather than by forcing a
    failure, because forcing one means breaking a template on purpose."""
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parent.parent / "routes" / "views.py").read_text(encoding="utf-8")
    bare = re.findall(r"return render_template\(\"500\.html\"\)(?!\s*,)", src)
    assert not bare, (
        f"{len(bare)} view(s) return the error page with a 200 status")
    wrapped = len(re.findall(r'\(render_template\("500\.html"\), 500\)', src))
    assert wrapped >= 10, (
        f"only {wrapped} views wrap their error page with a status; the "
        "pattern has drifted and this test is measuring the wrong thing")
