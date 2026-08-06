"""Feature 1.8 — T6: run_scheduled_backup().

Per-run subdir under data/backups/<ts>/, rotation to keep=N whole subdirs,
persisted outcome, and a SINGLE deduped stale-backup event per staleness
episode (re-armed on the next success). Reuses tools.backup.run() unchanged.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from collector_v2 import periodics
from database import Database


@pytest.fixture()
def db():
    return Database(Path(tempfile.mkdtemp()) / "test.db")


_SETTINGS = {"database_backup": {"enabled": True, "keep": 2,
                                 "stale_after_hours": 26, "alert_severity": "warning"}}


def _fake_backup(output_dir, data_dir=None, config_path=None, **kwargs):
    """Emulate tools.backup.run — write a manifest into the passed dir.

    ``config_path`` is accepted because the real signature grew it: the config
    path used to be derived as ``data_dir/config.json``, which is not where
    config.json lives, so every scheduled backup aborted. ``**kwargs`` keeps
    this double from being the thing that breaks on the next signature change.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text("{}")
    return out / "prism-x.db"


def _iso_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_rotation_keeps_only_N(db, tmp_path):
    root = tmp_path / "backups"
    with patch("tools.backup.run", side_effect=_fake_backup):
        for i in range(3):
            periodics.run_scheduled_backup(
                db, _SETTINGS, backups_root=root,
                now=datetime(2026, 7, 1, 10, i, 0, tzinfo=timezone.utc),
            )
    subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    assert len(subdirs) == 2, "keep=2 must prune the oldest subdir"
    for d in subdirs:
        assert (d / "manifest.json").exists()


def test_backup_run_persists_success_ts(db, tmp_path):
    with patch("tools.backup.run", side_effect=_fake_backup):
        periodics.run_scheduled_backup(db, _SETTINGS, backups_root=tmp_path / "b")
    st = db.get_backup_state()
    assert st["last_ok"] == 1
    assert st["last_success_ts"]
    assert st["last_path"]


def test_backup_failure_records_error_and_reraises(db, tmp_path):
    with patch("tools.backup.run", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError):
            periodics.run_scheduled_backup(db, _SETTINGS, backups_root=tmp_path / "b")
    st = db.get_backup_state()
    assert st["last_ok"] == 0
    assert "disk full" in (st["last_error"] or "")


def test_stale_backup_fires_single_event(db, tmp_path):
    db.set_backup_state(last_success_ts=_iso_ago(48), last_ok=1)
    events = []
    real_insert = db.insert_event

    def _spy(server, etype, metric, value, threshold, message):
        if metric == "backup_age":
            events.append((server, etype, message))
        return real_insert(server, etype, metric, value, threshold, message)

    with patch("tools.backup.run", side_effect=RuntimeError("fail")), \
            patch.object(db, "insert_event", side_effect=_spy):
        for _ in range(3):
            with pytest.raises(RuntimeError):
                periodics.run_scheduled_backup(db, _SETTINGS, backups_root=tmp_path / "b")
    assert len(events) == 1, f"expected exactly one backup_age event, got {len(events)}"


def test_failed_backup_removes_its_orphan_subdir(db, tmp_path):
    # A failed run must not leave an empty/partial <ts> subdir: rotation sorts
    # newest-first, so an orphan would survive and could evict a GOOD backup.
    root = tmp_path / "b"
    with patch("tools.backup.run", side_effect=RuntimeError("fail")):
        with pytest.raises(RuntimeError):
            periodics.run_scheduled_backup(
                db, _SETTINGS, backups_root=root,
                now=datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc),
            )
    subdirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    assert subdirs == [], "a failed backup must not leave an orphan subdir"


def test_stale_alert_rearms_on_success(db, tmp_path):
    db.set_backup_state(last_success_ts=_iso_ago(48), last_ok=1)
    with patch("tools.backup.run", side_effect=RuntimeError("fail")):
        with pytest.raises(RuntimeError):
            periodics.run_scheduled_backup(db, _SETTINGS, backups_root=tmp_path / "b")
    assert db.get_backup_state()["last_alerted_ts"], "stale alert must set last_alerted_ts"
    with patch("tools.backup.run", side_effect=_fake_backup):
        periodics.run_scheduled_backup(db, _SETTINGS, backups_root=tmp_path / "b")
    assert not db.get_backup_state().get("last_alerted_ts"), "success must clear last_alerted_ts"
