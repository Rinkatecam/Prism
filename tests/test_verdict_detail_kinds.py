"""The ``kind`` classification in FusedVerdict.detail().

Part B of docs/plans/ALERT_NOISE_AND_VERDICT_UX_PLAN.md. The dashboard used to
render a bare severity badge with no reason, so a real threshold breach looked
identical to a statistical blip well inside safe limits — the owner had to ask a
human why 10 hosts were amber. ``detail()`` now classifies each entry so the card
can render a breach loudly, a deviation quietly, and a suppressed observation
almost silently.

These pin the contract the templates rely on (partials/server_card.html,
partials/critical_issues.html). Both templates degrade to drawing nothing when
``kind`` is absent, so a cold cache written before this change is safe — but the
producer must keep emitting it.
"""

from __future__ import annotations

import pytest

from detection import evaluate_server
from tests.test_detection_fusion import FakeDB, Srv, _metrics, _settings

DEV_OFF = {
    "deviation_direction": "both",
    "deviation_min_pct_of_warning": 0,
    "deviation_requires_authority": False,
}


def _baseline(**over):
    """Baseline config with the raise gates wide open unless overridden."""
    base = dict(DEV_OFF)
    base.update(over)
    return base


def _sustained(db, srv, metrics, settings, times=5):
    """Evaluate repeatedly so the deviation sustain ring (N-of-M, default 3)
    actually fills — a single sample never reaches Layer 3, which would make
    these tests silently vacuous. Each caller uses a distinct server name so the
    module-level ring in detection.py can't leak between tests."""
    verdict = None
    for _ in range(times):
        verdict = evaluate_server(db, srv, metrics, settings)
    return verdict


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------

def test_boring_healthy_still_omitted():
    v = evaluate_server(FakeDB(), Srv(), _metrics(), _settings())
    assert v.detail() == {}


def test_legacy_keys_are_preserved():
    """`elevated` and `reason` must survive — older cached rows and the
    /api/servers/<name> consumers both read them."""
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)})
    v = evaluate_server(db, Srv("sql01"), _metrics(cpu=10, ram=93), _settings())
    entry = v.detail().get("ram")
    assert entry is not None
    assert "elevated" in entry and "reason" in entry
    assert entry["reason"]


# ---------------------------------------------------------------------------
# kind classification
# ---------------------------------------------------------------------------

def test_static_breach_is_kind_breach():
    # RAM 88 over a 75/85 band, no baseline slot -> pure static warning.
    v = evaluate_server(FakeDB(), Srv(), _metrics(cpu=10, ram=88), _settings())
    entry = v.detail().get("ram")
    assert entry["kind"] == "breach"
    assert entry["value"] == pytest.approx(88.0)


def test_exhaustion_floor_is_kind_floor():
    """The floor is a hard truth and must outrank a plain breach."""
    v = evaluate_server(FakeDB(), Srv(), _metrics(cpu=10, ram=99), _settings())
    entry = v.detail().get("ram")
    assert entry["kind"] == "floor"


def test_elevated_normal_is_kind_elevated():
    db = FakeDB(slots={"ram": (90.0, 2.0, 60)})
    v = evaluate_server(db, Srv("sql01"), _metrics(cpu=10, ram=93), _settings())
    entry = v.detail().get("ram")
    assert entry["kind"] == "elevated"
    assert entry["elevated"] is True


def test_layer3_raise_is_kind_deviation():
    """Static zone healthy, raised only because it differs from its own
    baseline — must be distinguishable from a real breach."""
    db = FakeDB(slots={"ram": (20.0, 1.0, 60)})
    v = _sustained(db, Srv("kind-dev"), _metrics(cpu=10, ram=40),
                   _settings(baseline_detection=_baseline()))
    entry = v.detail().get("ram")
    assert entry is not None, "Layer 3 did not raise — sustain ring never filled"
    assert entry["kind"] == "deviation"
    assert entry["elevated"] is False
    assert v.status == "warning"


# ---------------------------------------------------------------------------
# Suppressed observations
# ---------------------------------------------------------------------------

def test_suppressed_deviation_is_reported_but_not_alarming():
    """The whole point of §A5: filtered out of alerting, still visible.

    A falling deviation well inside limits is suppressed by the direction gate
    with the shipped defaults, and must surface as kind=suppressed carrying the
    gate name — never as a warning.
    """
    db = FakeDB(slots={"ram": (60.0, 1.0, 60)})
    v = _sustained(db, Srv("kind-sup"), _metrics(cpu=10, ram=20), _settings())

    assert v.status == "healthy", "a suppressed deviation must not change status"
    entry = v.detail().get("ram")
    assert entry is not None, "suppressed deviation was not surfaced at all"
    assert entry["kind"] == "suppressed"
    assert entry["gate"] in ("direction", "authority", "proximity")
    assert entry["elevated"] is False
    assert "not alerting" in entry["reason"]


def test_suppressed_wording_differs_from_alarm_wording():
    """The observation text must not read like a warning — 'differs from', not
    'is above/below', so a tooltip can't be mistaken for an alert."""
    db = FakeDB(slots={"ram": (60.0, 1.0, 60)})
    v = _sustained(db, Srv("kind-word"), _metrics(cpu=10, ram=20), _settings())
    mv = v.metrics.get("ram")
    assert mv and mv.deviation_suppressed, "deviation was not suppressed"
    assert "differs from" in mv.reason
    assert "is above" not in mv.reason and "is below" not in mv.reason


def test_suppressed_metric_keeps_its_deviation_payload():
    """mv.deviation must survive suppression — the baseline event pipeline and
    any future Observations view both read it."""
    db = FakeDB(slots={"ram": (60.0, 1.0, 60)})
    v = _sustained(db, Srv("kind-payload"), _metrics(cpu=10, ram=20), _settings())
    mv = v.metrics.get("ram")
    assert mv and mv.deviation_suppressed, "deviation was not suppressed"
    assert mv.deviation is not None
    assert mv.deviation["metric"] == "ram"


def test_suppressed_never_appears_as_a_warning_kind():
    """Guard against a future refactor letting a suppressed entry render amber."""
    db = FakeDB(slots={"ram": (60.0, 1.0, 60)})
    v = _sustained(db, Srv("kind-noamber"), _metrics(cpu=10, ram=20), _settings())
    kinds = {e.get("kind") for e in v.detail().values()}
    assert "suppressed" in kinds, "nothing was suppressed — test is vacuous"
    assert "breach" not in kinds and "deviation" not in kinds
    assert v.status == "healthy"


# ---------------------------------------------------------------------------
# Template contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["floor", "breach", "deviation"])
def test_templates_handle_every_flagging_kind(kind):
    """server_card.html and critical_issues.html both select on these exact
    kind strings. A rename in detection.py without a template change would
    silently stop rendering the reason."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("templates/partials/server_card.html",
                "templates/partials/critical_issues.html"):
        text = (root / rel).read_text(encoding="utf-8")
        assert f"'{kind}'" in text, f"{rel} does not handle kind={kind}"


def test_card_template_handles_suppressed_kind():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "templates/partials/server_card.html").read_text(encoding="utf-8")
    assert "'suppressed'" in text
