"""The incidents accordion, and why an open detail used to close by itself.

The owner reported it: open an incident on the dashboard, wait for a server to
update, and the detail collapses again. Two defects sat behind that, and only
the first is visible from the screen.

  1. The panel is re-fetched and morph-swapped on every ``prismRefresh``
     (``dashboard.html``: ``hx-trigger="load, prismRefresh from:body"``), and
     the server-rendered markup hardcodes ``class="incident-detail hidden"``
     and ``aria-expanded="false"``. The server cannot know what the operator
     opened, so every refresh reasserted "closed" over their choice. Fixed by
     remembering the open set client-side and re-applying it after the swap.

  2. The detail panel's DOM id was ``incident-detail-{{ loop.index0 }}`` — a
     POSITION, not an identity. That is the more dangerous one, because it
     only appears once you fix the first: restoring "index 0 was open" after a
     refresh re-opens whichever incident now sits at index 0, which is a
     different incident as soon as one is raised or resolved. An accordion
     that silently shows you the wrong incident's detail is worse than one
     that closes.

This file tests (2), because it is the half that can be checked without a
browser: identity must survive reordering.
"""

from __future__ import annotations

import pathlib
import re

import jinja2
import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
PARTIAL = "partials/incidents_panel.html"


class _T:
    def get(self, key, default=None):
        return default if default is not None else key

    def __getattr__(self, name):
        return name


class _D(dict):
    __getattr__ = dict.get


def _render(incidents):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined, autoescape=True)
    return env.get_template(PARTIAL).render(
        t=_T(), fmt_ts=lambda v: "now", incidents=incidents)


def _inc(id_, title, status="open"):
    return _D(id=id_, title=title, severity="critical", status=status,
              created_at="2026-08-28T10:09:00Z", description="d",
              root_cause_server="SRV", event_count=0,
              resolved_by=None, resolution_notes=None)


#: Two incidents that will be rendered in both orders.
A, B = _inc(41, "first"), _inc(77, "second")


def _detail_ids(html):
    return re.findall(r'id="(incident-detail-[^"]+)"', html)


def test_the_detail_id_is_the_incident_not_its_position():
    """The regression test for the bug that only shows up after the visible
    one is fixed.

    Rendered in one order then the other, each incident must keep the SAME
    detail id. With ``loop.index0`` both renders produce
    ``incident-detail-0`` and ``incident-detail-1``, so the ids are identical
    between renders while pointing at different incidents — and restoring
    "0 was open" opens the wrong one."""
    forward = _detail_ids(_render([A, B]))
    reversed_ = _detail_ids(_render([B, A]))

    assert forward == ["incident-detail-41", "incident-detail-77"]
    assert reversed_ == ["incident-detail-77", "incident-detail-41"]
    # The set is stable; only the order moved. That is the whole property.
    assert set(forward) == set(reversed_)


def test_every_trigger_controls_the_panel_it_opens():
    """`aria-controls` has to name a real id on the same row, or the keyboard
    bridge in base.html re-reads the wrong element's display and reports an
    `aria-expanded` that is the opposite of the truth."""
    html = _render([A, B])
    controls = re.findall(r'aria-controls="([^"]+)"', html)
    ids = _detail_ids(html)
    assert controls == ids, f"aria-controls {controls} does not match ids {ids}"


def test_the_trigger_carries_the_key_the_restore_looks_up():
    """base.html keys the open-set off `data-incident-key`. If the attribute
    is absent the toggle still works and the restore silently never fires —
    which is exactly the bug, back again and invisible."""
    html = _render([A, B])
    keys = re.findall(r'data-incident-key="([^"]+)"', html)
    assert keys == ["41", "77"]
    for k in keys:
        assert f'aria-controls="incident-detail-{k}"' in html


def test_a_missing_id_still_produces_unique_ids():
    """The fallback the original comment was worried about. Two rows without
    an id must not collide, because a duplicate DOM id points two triggers at
    one panel and opening either opens the first."""
    html = _render([_inc(None, "x"), _inc(None, "y")])
    ids = _detail_ids(html)
    assert len(ids) == len(set(ids)) == 2, ids


def test_the_restore_hook_exists_in_base():
    """A source check, and a deliberately weak one — it cannot prove the
    handler runs, only that the wiring is present. The property it guards is
    the half of this bug a Jinja render cannot see: that something re-applies
    the open set after the panel is swapped back in."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "_openIncidents" in base
    assert re.search(r"htmx:afterSwap[\s\S]{0,400}data-incident-key", base), (
        "base.html no longer re-applies open incident details after an "
        "htmx swap; an opened detail will close on the next prismRefresh")
