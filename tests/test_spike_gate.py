"""Static-breach spike gate for CPU and RAM.

Owner request 2026-08-05: "servers shouldn't trigger an issue or a warning if the
server had a spike in cpu or ram as that creates noise — it should flag this only
if it has been like that for at least 5 rounds of the collector."

Before this, only CPU had smoothing (3-of-5) and only for WARNING; RAM alarmed on
the first sample. Now a cpu/ram breach must hold for `spike_sustain_cycles`
CONSECUTIVE rounds (default 5), for warning AND critical.

Deliberately NOT gated, and pinned below so it stays that way:
  * exhaustion floors (RAM 98% / disk 95%) - a hard truth, must page instantly
  * disk - climbs monotonically, so gating only delays the metric that matters
"""

from __future__ import annotations

import pytest

import detection
from detection import evaluate_server
from tests.test_detection_fusion import FakeDB, Srv, _metrics, _settings


@pytest.fixture(autouse=True)
def _clear_rings():
    """The sustain rings are module-level and keyed by server name."""
    detection._static_spike_history.clear()
    detection._cpu_warn_history.clear()
    yield
    detection._static_spike_history.clear()
    detection._cpu_warn_history.clear()


def _gated(cycles=5, **over):
    """Settings with the spike gate explicitly ON.

    tests/test_detection_fusion.py's shared _settings() deliberately sets
    spike_sustain_cycles=1 so its single-sample fusion tests still exercise
    fusion. This file is about the gate itself, so it states the value it means
    rather than inheriting that override.
    """
    anomaly = {"enabled": True, "spike_sustain_cycles": cycles}
    anomaly.update(over.pop("anomaly_detection", {}))
    return _settings(anomaly_detection=anomaly, **over)


def _run(db, srv, metrics, settings, rounds):
    """Feed the same sample N times; return the verdict from each round."""
    return [evaluate_server(db, srv, metrics, settings) for _ in range(rounds)]


# ---------------------------------------------------------------------------
# The headline behaviour: 5 rounds required
# ---------------------------------------------------------------------------

# Harness thresholds (tests/test_detection_fusion.py): cpu 75/90, ram 70/85,
# disk 80/90. Values below are chosen to land in the WARNING band, not critical.
@pytest.mark.parametrize("metric,value", [("ram", 80.0), ("cpu", 88.0)])
def test_breach_is_gated_until_the_fifth_round(metric, value):
    """Rounds 1-4 stay healthy; round 5 flips the badge."""
    verdicts = _run(FakeDB(), Srv("g-%s" % metric),
                    _metrics(**{metric: value}), _gated(), 5)

    assert [v.status for v in verdicts[:4]] == ["healthy"] * 4, \
        "a breach alarmed before it was sustained"
    assert verdicts[4].status == "warning"


def test_single_spike_never_alarms():
    """One bad sample surrounded by good ones must stay silent."""
    db, srv, s = FakeDB(), Srv("g-single"), _gated()
    evaluate_server(db, srv, _metrics(ram=20), s)
    spike = evaluate_server(db, srv, _metrics(ram=88), s)
    after = evaluate_server(db, srv, _metrics(ram=20), s)

    assert spike.status == "healthy"
    assert after.status == "healthy"


def test_one_good_sample_resets_the_count():
    """window == need, so the streak must be unbroken."""
    db, srv, s = FakeDB(), Srv("g-reset"), _gated()
    for _ in range(4):
        evaluate_server(db, srv, _metrics(ram=88), s)
    evaluate_server(db, srv, _metrics(ram=20), s)          # breaks the streak
    verdicts = _run(db, srv, _metrics(ram=88), s, 4)       # only 4 again

    assert [v.status for v in verdicts] == ["healthy"] * 4


# ---------------------------------------------------------------------------
# Critical is gated too — a jump to critical for one sample is still a spike
# ---------------------------------------------------------------------------

def test_critical_is_also_gated():
    verdicts = _run(FakeDB(), Srv("g-crit"), _metrics(cpu=10, ram=93), _gated(), 5)

    assert [v.status for v in verdicts[:4]] == ["healthy"] * 4
    assert verdicts[4].status == "critical"


# ---------------------------------------------------------------------------
# The floor is NEVER gated — genuine exhaustion must page on sample one
# ---------------------------------------------------------------------------

def test_exhaustion_floor_bypasses_the_gate_entirely():
    """RAM 99% is past the 98% floor: instant critical, no waiting."""
    v = evaluate_server(FakeDB(), Srv("g-floor"), _metrics(cpu=10, ram=99), _gated())

    assert v.status == "critical"
    assert v.metrics["ram"].is_floor is True
    assert v.metrics["ram"].spike_gated is False


def test_disk_is_not_gated():
    """Disk climbs monotonically — delaying it would be actively harmful."""
    v = evaluate_server(FakeDB(), Srv("g-disk"), _metrics(cpu=10, ram=20, disk_c=85),
                        _gated())

    assert v.status == "warning"
    assert v.metrics["disk_c"].spike_gated is False


# ---------------------------------------------------------------------------
# Gated breaches stay visible as observations, not silent
# ---------------------------------------------------------------------------

def test_gated_breach_is_surfaced_as_a_quiet_observation():
    v = evaluate_server(FakeDB(), Srv("g-obs"), _metrics(cpu=10, ram=88), _gated())
    mv = v.metrics["ram"]

    assert v.status == "healthy"
    assert mv.spike_gated is True
    assert "not sustained" in mv.reason
    entry = v.detail().get("ram")
    assert entry is not None, "a gated breach must not vanish from the UI"
    assert entry["kind"] == "spike"
    assert entry["elevated"] is False


def test_card_template_handles_the_spike_kind():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "templates/partials/server_card.html").read_text(encoding="utf-8")
    assert "'spike'" in text, "server_card.html must render kind=spike quietly"


# ---------------------------------------------------------------------------
# Configurability + rollback
# ---------------------------------------------------------------------------

def test_cycles_of_one_disables_the_gate():
    """Instant-alarm rollback path."""
    s = _settings(anomaly_detection={"enabled": True, "spike_sustain_cycles": 1})
    v = evaluate_server(FakeDB(), Srv("g-off"), _metrics(cpu=10, ram=80), s)

    assert v.status == "warning"
    assert v.metrics["ram"].spike_gated is False


def test_custom_cycle_count_is_honoured():
    s = _settings(anomaly_detection={"enabled": True, "spike_sustain_cycles": 2})
    verdicts = _run(FakeDB(), Srv("g-two"), _metrics(cpu=10, ram=80), s, 2)

    assert verdicts[0].status == "healthy"
    assert verdicts[1].status == "warning"


@pytest.mark.parametrize("raw,expected", [
    (0, 1), (-5, 1), (1, 1), (5, 5), (20, 20), (999, 20),
    ("nonsense", 5), (None, 5), ([], 5),
])
def test_cycle_count_is_clamped_and_fails_safe(raw, expected):
    from detection import _spike_sustain_cycles
    assert _spike_sustain_cycles({"anomaly_detection": {"spike_sustain_cycles": raw}}) == expected


def test_default_is_five():
    from detection import _spike_sustain_cycles
    from config_manager import ConfigManager
    assert _spike_sustain_cycles({}) == 5
    assert ConfigManager._DEFAULT_SETTINGS["anomaly_detection"]["spike_sustain_cycles"] == 5


# ---------------------------------------------------------------------------
# The legacy CPU ring must keep being fed (external consumers depend on it)
# ---------------------------------------------------------------------------

def test_legacy_cpu_ring_is_still_written_every_sample():
    """analytics.py::_cpu_gate_passes and the per-server ack reset both read
    _cpu_warn_history. evaluate_server remains its single writer."""
    db, srv, s = FakeDB(), Srv("g-legacy"), _gated()
    for _ in range(3):
        evaluate_server(db, srv, _metrics(cpu=88), s)

    ring = detection._cpu_warn_history.get("g-legacy")
    assert ring is not None, "legacy CPU ring stopped being written"
    assert len(ring) == 3 and all(ring)
