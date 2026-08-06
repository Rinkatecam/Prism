"""Prism restore tool.

Standalone CLI:  python tools/restore.py <backup-dir> [--force] [--accept-key-loss]

Companion to ``tools/backup.py``. Verifies the manifest hashes, warns
loudly if the DPAPI-wrapped key was bound to a different Windows account
on the source host, then drops the files into ``<repo>/data/`` (or a
custom destination via ``--data-dir``). Does not touch the running
Prism process -- prints restart instructions instead.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    from . import backup as _backup  # package import (tests, "python -m tools.restore")
except ImportError:  # direct script: "python tools/restore.py"
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup as _backup  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def load_manifest(backup_dir: Path) -> dict:
    mp = backup_dir / "manifest.json"
    if not mp.exists():
        raise FileNotFoundError(f"manifest.json not found in {backup_dir}")
    return json.loads(mp.read_text(encoding="utf-8"))


def verify_manifest(backup_dir: str | Path) -> bool:
    """Return True iff every file listed in the manifest matches its sha256."""
    backup_dir = Path(backup_dir)
    try:
        manifest = load_manifest(backup_dir)
    except Exception:
        return False
    for entry in manifest.get("files", []):
        f = backup_dir / entry["name"]
        if not f.exists():
            return False
        if _backup._sha256(f) != entry["sha256"]:
            return False
    return True


def _sid_mismatch(manifest: dict) -> Tuple[bool, str, str]:
    """Return (mismatch, source_sid, current_sid).

    A mismatch is only flagged when both SIDs are known and differ. If
    either side is empty we cannot prove a mismatch -- but the operator
    still gets a softer warning at the call-site.
    """
    src_sid = (manifest.get("source_user_sid") or "").strip()
    cur_sid = _backup._current_user_sid() or ""
    if src_sid and cur_sid and src_sid != cur_sid:
        return True, src_sid, cur_sid
    return False, src_sid, cur_sid


SID_WARNING = """
============================================================================
  WARNING: DPAPI key was wrapped under a DIFFERENT Windows account.

    backup source SID : {src}
    current host  SID : {cur}

  The DPAPI-wrapped encryption key (prism.key.dpapi) was bound to the
  Windows user that originally ran Prism. Decryption WILL FAIL SILENTLY
  for every stored credential -- WinRM passwords, SNMP communities, SMTP
  passwords -- because Fernet treats the bad key as "valid but wrong"
  and emits ciphertext-shaped garbage.

  To recover, you need EITHER:
    (a) the original user's profile + password (so DPAPI can unwrap), or
    (b) accept the loss and re-add every server's credentials manually
        from your password manager after restore completes.

  Re-run with --accept-key-loss to proceed with option (b).
============================================================================
""".strip()


def run(
    backup_dir: str | Path,
    data_dir: str | Path | None = None,
    *,
    force: bool = False,
    accept_key_loss: bool = False,
) -> Path:
    """Restore a backup. Returns the destination data directory."""
    backup_dir = Path(backup_dir).resolve()
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"backup directory not found: {backup_dir}")

    manifest = load_manifest(backup_dir)

    if not verify_manifest(backup_dir):
        raise RuntimeError(
            "manifest verification FAILED: a file is missing or its SHA-256 "
            "does not match. Refusing to restore (possible tampering or "
            "incomplete copy)."
        )

    dest = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
    dest.mkdir(parents=True, exist_ok=True)

    if (dest / "prism.db").exists() and not force:
        raise RuntimeError(
            f"{dest / 'prism.db'} already exists. Refusing to overwrite "
            "without --force. This tool is for disaster recovery, not "
            "casual rollback."
        )

    mismatch, src_sid, cur_sid = _sid_mismatch(manifest)
    has_dpapi = any(
        e["name"].endswith(".dpapi") for e in manifest.get("files", [])
    )
    if has_dpapi and mismatch and not accept_key_loss:
        raise RuntimeError(SID_WARNING.format(src=src_sid, cur=cur_sid))

    # Identify the DB file (its name is timestamped).
    db_entry = next(
        (e for e in manifest["files"] if e.get("role") == "database"),
        None,
    )
    if db_entry is None:
        raise RuntimeError("manifest has no database entry")
    db_src = backup_dir / db_entry["name"]
    shutil.copy2(db_src, dest / "prism.db")

    shutil.copy2(backup_dir / "config.json", dest / "config.json")

    for entry in manifest.get("files", []):
        n = entry["name"]
        if n.endswith(".dpapi") or n == "prism.key":
            shutil.copy2(backup_dir / n, dest / n)

    return dest


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Restore a Prism backup.")
    p.add_argument("backup_dir", help="Directory produced by tools/backup.py")
    p.add_argument("--data-dir", default=None,
                   help="Destination data directory (default: <repo>/data).")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing data/prism.db.")
    p.add_argument("--accept-key-loss", action="store_true",
                   help="Proceed even if the DPAPI key was bound to a "
                        "different Windows account on the source host.")
    args = p.parse_args(argv)

    try:
        dest = run(
            args.backup_dir,
            data_dir=args.data_dir,
            force=args.force,
            accept_key_loss=args.accept_key_loss,
        )
    except Exception as exc:
        print(f"ERROR: restore failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: restored from {Path(args.backup_dir).resolve()}, "
          f"restart Prism: python app.py")
    print(f"    (destination: {dest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
