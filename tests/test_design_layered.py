"""Guardrails for layered rendering — Wave 3, task F3.

`/server/<name>` gathered everything before sending a byte. Measured per call
on the busiest host, median of seven:

    get_latest_by_server        0.01 ms
    get_server_events(50)       0.10 ms
    get_server_logs(24h, 50)    0.10 ms
    get_server_analytics       31.77 ms   <- 99.3% of the total

So ONE region was the page. Moving it behind `/partials/server-analytics/<name>`
took the document from 37ms/45ms to 8ms/9ms, level with every other page
(`/servers` 9ms, unchanged as a control). The three cheap reads stayed inline
deliberately — deferring them would have been motion without effect.

WHAT THESE ARE BLIND TO:

  * The timing itself. Nothing here re-measures 31.77ms; a test that did
    would be a flaky benchmark. What is guarded is the STRUCTURE that the
    measurement justified — above all that the expensive call does not creep
    back into the page view, which would silently restore the old cost with
    every other test still green.
  * Whether the region looks right. Verified in the browser: ghost and real
    grid classes byte-identical at 1440px, 3 == 3 columns, region within 58px.
  * The anomaly branch, which no host in the fleet currently produces. It was
    rendered with synthetic anomalies instead: 2 cards, both `data-action`
    controls present, no unrendered Jinja.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
VIEWS = PROJECT_ROOT / "routes" / "views.py"

_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*(?://|#)[^\n]*", re.M)


def _code_only(text: str) -> str:
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def _view_body(name: str) -> str:
    """The source of one view function, comments stripped."""
    src = _code_only(VIEWS.read_text(encoding="utf-8"))
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^@|\Z)", src, re.S | re.M)
    assert m, f"view {name} not found in routes/views.py"
    return m.group(0)


def test_the_expensive_call_is_not_in_the_page_view():
    """The regression that undoes all of this in one line. Re-adding
    `get_server_analytics` to the page view restores 31.77ms to every
    /server/<name> render, and nothing else in the suite would notice — the
    page would still be correct, just four times slower again."""
    body = _view_body("server_detail")
    assert "get_server_analytics" not in body, (
        "get_server_analytics is back in the page view; the region is meant "
        "to be fetched after paint by partial_server_analytics")
    assert "analytics=" not in body, (
        "the page view passes `analytics` to the template again")


def test_the_cheap_reads_stayed_inline():
    """The other half of the decision, asserted so it is not 'tidied up' into
    three more round trips. Each costs 0.01-0.10ms; a partial fetch costs a
    request. Deferring them would make the page slower, not faster."""
    body = _view_body("server_detail")
    for call in ("get_latest_by_server", "get_server_events", "get_server_logs"):
        assert call in body, (
            f"{call} was moved out of the page view. It costs under 0.1ms — "
            "deferring it adds a request and removes nothing")


def test_the_partial_view_exists_and_does_the_work():
    body = _view_body("partial_server_analytics")
    assert "get_server_analytics" in body, "the partial view does not compute analytics"
    src = _code_only(VIEWS.read_text(encoding="utf-8"))
    assert re.search(r'@views_bp\.route\(\s*"/partials/server-analytics/<name>"\s*\)', src), (
        "the route is not registered at the path the template requests")


def test_an_unknown_host_does_not_inject_an_error_page_into_the_document():
    """The response is swapped straight into the page. Returning a rendered
    404 or 500 page here would put a whole HTML document inside a <section>."""
    body = _view_body("partial_server_analytics")
    assert re.search(r"if not cfg:\s*\n\s*return \"\", 200", body), (
        "an unknown server must return an empty body, not a page")


def test_the_page_region_fetches_itself_and_ghosts_while_it_waits():
    page = _code_only((TEMPLATES / "server_detail.html").read_text(encoding="utf-8"))
    m = re.search(r'<div id="server-analytics"[^>]*>', page, re.S)
    assert m, "the analytics region is gone from server_detail.html"
    tag = m.group(0)
    assert "hx-get=" in tag and "/partials/server-analytics/" in tag
    assert re.search(r'hx-trigger="[^"]*\bload\b', tag), "the region never fetches"
    assert "forecast_cards(" in page, "the region shows nothing while it waits"
    # The heading is shell and must not have gone with the data.
    assert "t.anomalies" in page, (
        "the section heading left with the data; the shell should keep its "
        "labels so the page does not visibly grow a new section")


def test_the_ghost_and_the_real_grid_agree_on_columns():
    """Two halves of one layout. The column count comes from which disks the
    host reports, and both sides compute it from `metrics` — if they drift,
    the ghost reserves the wrong grid and the content jumps sideways on
    arrival. Verified live at 1440px: the two class strings were identical."""
    partial = _code_only((TEMPLATES / "partials" / "server_analytics.html").read_text(encoding="utf-8"))
    skel = _code_only((TEMPLATES / "partials" / "_skeletons.html").read_text(encoding="utf-8"))

    def cols_expr(text: str) -> str | None:
        m = re.search(r"grid grid-cols-1 sm:grid-cols-2 \{\{(.*?)\}\} gap-3", text, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else None

    a, b = cols_expr(partial), cols_expr(skel)
    assert a, "the partial's responsive grid expression changed shape"
    assert b, "the skeleton's responsive grid expression changed shape"
    assert a == b, ("the ghost and the real grid compute their columns "
                    f"differently:\n  partial:  {a}\n  skeleton: {b}")


def test_the_partial_carries_no_inline_script():
    """This fragment is fetched with its own request, so a per-request CSP
    nonce inside it would not match the host page's and the script would be
    blocked. The two `data-action` handlers it uses are delegated from
    base.html and need no rebinding."""
    # _code_only, because the partial's own header comment explains this rule
    # and names the tag. Third time in this wave that a text-scanning check
    # fired on its own documentation; the cheapest way to silence one is to
    # delete the explanation, which is the wrong repair.
    partial = _code_only((TEMPLATES / "partials" / "server_analytics.html")
                         .read_text(encoding="utf-8"))
    assert "<script" not in partial


def test_the_partial_renders_with_exactly_what_its_view_passes():
    """A partial that reads a variable its view does not provide renders a
    blank region in production and nothing anywhere says why. StrictUndefined
    turns that into a failure here instead.

    The view passes server, metrics and analytics; `t` and the time filters
    come from context processors."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined, autoescape=True)

    class _T:
        def get(self, key, default=None):
            return default if default is not None else key

        def __getattr__(self, name):
            return name

    class _D(dict):
        __getattr__ = dict.get

    ctx = dict(
        t=_T(), fmt_ts=lambda v: "now", fmt_time=lambda v: "now",
        server=_D(name="HOST01", type="domain_controller"),
        metrics=_D(disk_c_percent=40.0, disk_d_percent=10.0),
        analytics=_D(
            anomalies=[_D(metric="ram", severity="critical", current=97.0,
                          baseline=42.0, deviation=55.0, direction="above_baseline",
                          acknowledged=False, detected_at="2026-08-11T09:00:00")],
            forecasts={
                "disk_c": _D(enough_data=True, kind="linear", days_until_full=41,
                             trend_per_day=0.4, confidence="medium"),
                "disk_d": _D(enough_data=False),
                "ram": _D(enough_data=True, kind="stationary", range_min=29.6,
                          range_max=35.2, avg=31.6, now=29.9,
                          elevated_but_stable=False),
                "cpu": _D(enough_data=True, kind="stationary", range_min=0.0,
                          range_max=100.0, avg=10.3, now=13.1,
                          elevated_but_stable=True)}),
    )
    out = env.get_template("partials/server_analytics.html").render(**ctx)
    assert "{{" not in out and "{%" not in out, "unrendered Jinja in the output"
    assert "_sdAckAnomaly" in out, (
        "the acknowledge control vanished; no live host currently produces an "
        "anomaly, so this synthetic render is the only thing exercising it")
    assert out.count("bg-card rounded-lg p-4 border border-line") == 4, (
        "expected four forecast cards for a host reporting both disks")


def test_the_empty_analytics_case_does_not_blow_up():
    """A cold cache returns no anomalies and no forecasts. The region must
    render nothing rather than raise — it is swapped into a live page."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined, autoescape=True)

    class _T:
        def get(self, key, default=None):
            return default if default is not None else key

        def __getattr__(self, name):
            return name

    class _D(dict):
        __getattr__ = dict.get

    out = env.get_template("partials/server_analytics.html").render(
        t=_T(), fmt_ts=lambda v: "now", fmt_time=lambda v: "now",
        server=_D(name="HOST01", type="other"), metrics=None,
        analytics=_D(anomalies=[], forecasts={}))
    assert "{%" not in out
