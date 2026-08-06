"""Window-scoped queries must compare timestamps in the stored format.

Found 2026-08-05. Every window filter used `timestamp >= datetime('now', ?)`.
SQLite's `datetime()` renders `2026-08-05 09:36:10` (space separator, no Z),
while every row is stored `2026-08-05T11:13:41Z`. The comparison is TEXT, and
'T' (0x54) sorts above ' ' (0x20), so the cutoff was effectively pushed back to
the start of its own calendar day and the query over-read.

Measured on the live 60k-row DB before the fix:
    2h  window: 9589 rows returned vs 2567 correct  (3.74x)
    24h window: 12639 rows returned vs 9589 correct (1.32x)

28 call sites were affected across database.py and routes/api/misc.py,
including the retention DELETEs (which under-deleted by the same margin, in the
safe direction).

The second half of the fix is Database._canonical_ts: insert_logs stored
`log["time"]` verbatim, so the stored format was whatever the caller sent. A
space-formatted row is invisible to EVERY `>=` window query regardless of its
actual instant — so normalising on write is what stops the bug reappearing from
the other side.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database import Database

CANON = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _utc(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


# ── the normaliser ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("2026-08-05T11:13:41Z", "2026-08-05T11:13:41Z"),      # already canonical
    ("2026-08-05 11:13:41",  "2026-08-05T11:13:41Z"),      # space separator
    ("2026-08-05T11:13:41",  "2026-08-05T11:13:41Z"),      # naive, no zone
    ("2026-08-05T11:13:41.123456Z", "2026-08-05T11:13:41Z"),   # fractional
    ("2026-08-05T11:13:41+00:00",   "2026-08-05T11:13:41Z"),   # explicit UTC
    ("2026-08-05T13:13:41+02:00",   "2026-08-05T11:13:41Z"),   # offset -> UTC
    ("", ""),
    (None, ""),
])
def test_canonical_ts_normalises_known_shapes(raw, expected):
    assert Database._canonical_ts(raw) == expected


def test_canonical_ts_passes_through_garbage_rather_than_dropping_it():
    """An odd value stored is recoverable; a discarded row is not."""
    assert Database._canonical_ts("not a date") == "not a date"


def test_canonical_ts_is_idempotent():
    once = Database._canonical_ts("2026-08-05 11:13:41")
    assert Database._canonical_ts(once) == once


# ── the write path ────────────────────────────────────────────────────────

def test_insert_logs_normalises_a_space_formatted_timestamp(db):
    """This is the shape that was invisible to every window query."""
    db.insert_logs("s1", [{
        "time": _utc(minutes=5).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "System", "level": "Error", "event_id": 1, "message": "x",
    }])
    conn = sqlite3.connect(db.db_path)
    stored = conn.execute("SELECT timestamp FROM logs").fetchone()[0]
    conn.close()
    assert "T" in stored and stored.endswith("Z"), stored


def test_a_space_formatted_log_is_now_visible_to_a_window_query(db):
    """The regression this guards: written in one shape, queried in another,
    silently returning nothing."""
    db.insert_logs("s1", [{
        "time": _utc(minutes=5).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Firewall", "level": "Error", "event_id": 5025, "message": "y",
    }])
    assert len(db.get_server_logs("s1", hours=1)) == 1


def test_offset_timestamps_are_converted_not_just_reformatted(db):
    """A +02:00 time must land at its UTC instant, or a Frankfurt-local feed
    would appear two hours in the future and escape every window."""
    local = (_utc(minutes=30) + timedelta(hours=2))
    db.insert_logs("s1", [{
        "time": local.strftime("%Y-%m-%dT%H:%M:%S") + "+02:00",
        "source": "System", "level": "Error", "event_id": 2, "message": "z",
    }])
    assert len(db.get_server_logs("s1", hours=1)) == 1


# ── the read path: window boundaries ──────────────────────────────────────

def _seed_metrics(db, offsets_hours):
    conn = sqlite3.connect(db.db_path)
    conn.executemany(
        """INSERT INTO metrics (server_name, timestamp, cpu_percent, ram_percent,
                                disk_c_percent, disk_d_percent, status)
           VALUES (?,?,?,?,?,?,?)""",
        [("s1", _utc(hours=h).strftime(CANON), 10.0, 50.0, 40.0, -1.0, "healthy")
         for h in offsets_hours],
    )
    conn.commit()
    conn.close()


def test_window_excludes_rows_older_than_the_cutoff(db):
    """The core bug: a 2h window must NOT return a 20h-old row. Before the fix
    it did, whenever the cutoff and the row fell in the same calendar day."""
    _seed_metrics(db, [0.5, 1.0, 20.0, 100.0])
    rows = db.get_server_history("s1", hours=2)
    assert len(rows) == 2, [r["timestamp"] for r in rows]


def test_window_is_monotonic_in_its_size(db):
    """A larger window can never return fewer rows. This property is what the
    old comparison violated non-obviously."""
    _seed_metrics(db, [0.5, 3, 10, 30, 100, 500])
    counts = [len(db.get_server_history("s1", hours=h))
              for h in (1, 2, 6, 24, 72, 720)]
    assert counts == sorted(counts), counts
    assert counts[0] == 1 and counts[-1] == 6


def test_bucketed_history_honours_the_same_boundary(db):
    """get_server_history_bucketed had the identical defect."""
    _seed_metrics(db, [0.5, 1.0, 20.0])
    buck = db.get_server_history_bucketed("s1", hours=2, buckets=240)
    assert sum(r["samples"] for r in buck) == 2


def test_raw_and_bucketed_agree_on_which_rows_are_in_window(db):
    _seed_metrics(db, [0.2, 0.4, 5, 50])
    for h in (1, 6, 72):
        raw = len(db.get_server_history("s1", hours=h))
        buck = sum(r["samples"] for r in db.get_server_history_bucketed("s1", hours=h, buckets=240))
        assert raw == buck, f"{h}h: raw={raw} bucketed={buck}"


def test_a_row_exactly_at_the_cutoff_day_boundary_is_not_swept_in(db):
    """The specific failure shape: same calendar day, hours outside the window."""
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=5, second=0, microsecond=0)
    if (now - start_of_day) < timedelta(hours=3):
        pytest.skip("run before 03:05 UTC — no same-day out-of-window slot exists")
    conn = sqlite3.connect(db.db_path)
    conn.execute(
        """INSERT INTO metrics (server_name, timestamp, cpu_percent, ram_percent,
                                disk_c_percent, disk_d_percent, status)
           VALUES (?,?,?,?,?,?,?)""",
        ("s1", start_of_day.strftime(CANON), 10.0, 50.0, 40.0, -1.0, "healthy"))
    conn.commit()
    conn.close()
    assert db.get_server_history("s1", hours=2) == []
