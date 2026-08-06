"""Feature: per-server 24h sparkline data (UX high-impact — 'creeping vs pegged').

One grouped, downsampled query for the whole fleet (NOT N per-server queries),
returning {server_name: [worst-resource % per time-bucket, oldest->newest]} so
the servers page can inject a tiny SVG polyline into each card.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from database import Database


@pytest.fixture()
def db():
    return Database(Path(tempfile.mkdtemp()) / "test.db")


def test_empty_fleet_has_no_sparklines(db):
    assert db.get_fleet_sparklines() == {}


def test_sparkline_series_shape_and_worst_resource(db):
    # worst-of(cpu,ram,disk_c,disk_d) per sample, MAX over the bucket.
    db.insert_metric("srv1", 30.0, 90.0, 40.0, 10.0, "warning")   # worst = 90
    db.insert_metric("srv1", 20.0, 20.0, 20.0, 20.0, "healthy")   # worst = 20
    s = db.get_fleet_sparklines(hours=24, buckets=24)
    assert "srv1" in s
    assert len(s["srv1"]) == 24
    # both samples are "now" → newest bucket (last slot); bucket MAX = 90
    assert s["srv1"][-1] == 90.0
    # untouched servers are absent (card just shows no sparkline)
    assert "srv2" not in s


def test_sparkline_handles_null_metrics(db):
    db.insert_metric("srv1", None, None, None, None, "unknown")
    s = db.get_fleet_sparklines()
    # a row with all-NULL metrics coalesces to 0 — series still present, no crash
    assert "srv1" in s and len(s["srv1"]) == 24
