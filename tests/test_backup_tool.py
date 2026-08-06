"""Tests for tools/backup.py and tools/restore.py.

We exercise the importable ``run`` / ``verify_manifest`` helpers rather
than shelling out, so the suite stays fast and works on Linux/macOS dev
machines (the only Windows-specific bit -- pywin32 SID lookup -- is
guarded with a try/except in the tool itself).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tools import backup as backup_mod
from tools import restore as restore_mod


def _seed_data_dir(tmp_path):
    """Stand up a minimal data/ that backup.run() can chew on.

    Uses the real Database class to produce a syntactically-correct
    prism.db with a known row in the audit_log table.
    """
    from database import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    db = Database(data_dir / "prism.db")
    db.log_audit("alice", "TEST_ACTION", category="general", details="seed")

    # config.json goes NEXT TO data/, mirroring the real layout where it lives
    # at the repo root — deliberately NOT inside data_dir. It used to be written
    # into data_dir, which SATISFIED backup.run()'s wrong assumption instead of
    # challenging it. That is how 883 passing tests coexisted with a scheduled
    # backup that had not succeeded once in 20 days.
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"servers": [], "settings": {"x": 1}}),
        encoding="utf-8",
    )
    (data_dir / "prism.key.dpapi").write_bytes(b"\x00\x01\x02fakekey")
    return data_dir, config_path


def test_backup_round_trip(tmp_path):
    data_dir, config_path = _seed_data_dir(tmp_path)
    out_dir = tmp_path / "out"

    backup_mod.run(out_dir, data_dir=data_dir, config_path=config_path)

    manifest_path = out_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema"] == "prism-backup/1"
    assert manifest["source_host"]
    names = {f["name"] for f in manifest["files"]}
    assert "config.json" in names
    assert "prism.key.dpapi" in names
    assert any(n.startswith("prism-") and n.endswith(".db") for n in names)

    # Hashes verify clean.
    assert restore_mod.verify_manifest(out_dir) is True

    # RESTORE.md sibling exists and mentions DPAPI.
    restore_md = (out_dir / "RESTORE.md").read_text(encoding="utf-8")
    assert "DPAPI" in restore_md

    # Backed-up DB is readable and carries our seeded row.
    db_file = next(out_dir.glob("prism-*.db"))
    src_count = sqlite3.connect(str(data_dir / "prism.db")).execute(
        "SELECT COUNT(*) FROM audit_log"
    ).fetchone()[0]
    bak_count = sqlite3.connect(str(db_file)).execute(
        "SELECT COUNT(*) FROM audit_log"
    ).fetchone()[0]
    assert src_count == bak_count
    assert src_count >= 1


def test_tampering_detected(tmp_path):
    data_dir, config_path = _seed_data_dir(tmp_path)
    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir, config_path=config_path)

    db_file = next(out_dir.glob("prism-*.db"))
    raw = bytearray(db_file.read_bytes())
    # Flip one byte deep enough to dodge SQLite header magic.
    raw[len(raw) // 2] ^= 0xFF
    db_file.write_bytes(bytes(raw))

    assert restore_mod.verify_manifest(out_dir) is False


def test_restore_refuses_on_sid_mismatch(tmp_path, monkeypatch):
    data_dir, config_path = _seed_data_dir(tmp_path)
    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir, config_path=config_path)

    # Rewrite the manifest with a fabricated source SID, then re-hash...
    # no -- the manifest's own bytes aren't hashed (only files-on-disk
    # are), so rewriting it leaves verify_manifest happy.
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_user_sid"] = "S-1-5-21-1111111111-2222222222-3333333333-1001"
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Pin the "current" SID to something different so the mismatch is real.
    monkeypatch.setattr(
        backup_mod, "_current_user_sid",
        lambda: "S-1-5-21-9999999999-8888888888-7777777777-1001",
    )

    dest = tmp_path / "restore_target"

    with pytest.raises(RuntimeError, match="DIFFERENT Windows account"):
        restore_mod.run(out_dir, data_dir=dest)

    # With --accept-key-loss it proceeds.
    restore_mod.run(out_dir, data_dir=dest, accept_key_loss=True)
    assert (dest / "prism.db").exists()
    assert (dest / "config.json").exists()
    assert (dest / "prism.key.dpapi").exists()


def test_restore_refuses_overwrite_without_force(tmp_path, monkeypatch):
    data_dir, config_path = _seed_data_dir(tmp_path)
    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir, config_path=config_path)

    dest = tmp_path / "restore_target"
    dest.mkdir()
    (dest / "prism.db").write_bytes(b"existing")

    # Suppress SID warning regardless of platform.
    monkeypatch.setattr(backup_mod, "_current_user_sid", lambda: "")

    with pytest.raises(RuntimeError, match="already exists"):
        restore_mod.run(out_dir, data_dir=dest)

    restore_mod.run(out_dir, data_dir=dest, force=True)
    assert (dest / "prism.db").read_bytes() != b"existing"


# ── B-1 regression: config.json does NOT live in data/ ───────────────────
#
# tools/backup.py derived the config path as data_dir/config.json. prism.db and
# the keys ARE in data/, so those two checks passed and the third raised
# FileNotFoundError — aborting every scheduled backup before it wrote anything.
# It failed every 30 seconds for 20 days while the suite stayed green, because
# the fixture wrote config.json into data_dir and so agreed with the code
# instead of with reality.

def test_backup_succeeds_when_config_is_outside_data_dir(tmp_path):
    """The exact production layout: config.json at the repo root, prism.db and
    the key under data/."""
    data_dir, config_path = _seed_data_dir(tmp_path)
    assert not (data_dir / "config.json").exists(), \
        "fixture must not put config.json in data/ — that is what hid the bug"

    out_dir = tmp_path / "out"
    backup_mod.run(out_dir, data_dir=data_dir, config_path=config_path)

    assert (out_dir / "config.json").exists(), "config.json must be in the bundle"
    assert restore_mod.verify_manifest(out_dir) is True


def test_backup_defaults_the_config_to_the_repo_root_not_the_data_dir(tmp_path):
    """With config_path omitted, the fallback must be the REPO ROOT. If it ever
    reverts to data_dir/config.json this fails instead of silently aborting in
    production."""
    from pathlib import Path
    import inspect
    src = inspect.getsource(backup_mod.run)
    assert 'PROJECT_ROOT / "config.json"' in src
    assert 'src / "config.json"' not in src, "config path derived from data_dir again"


def test_missing_config_still_raises_a_clear_error(tmp_path):
    """The guard itself must survive — a genuinely absent config is an error,
    it just must not be manufactured by looking in the wrong place."""
    data_dir, _cfg = _seed_data_dir(tmp_path)
    with pytest.raises(FileNotFoundError, match="config.json"):
        backup_mod.run(tmp_path / "out2", data_dir=data_dir,
                       config_path=tmp_path / "nope" / "config.json")


def test_scheduled_backup_passes_an_explicit_config_path():
    """The periodic must not rely on backup.run()'s fallback — it resolves the
    path from ConfigManager, the single source of truth."""
    import inspect
    from collector_v2 import periodics
    src = inspect.getsource(periodics.run_scheduled_backup)
    assert "config_path=" in src, "scheduled backup must pass config_path explicitly"
    assert "ConfigManager" in src
