"""Prism backup tool.

Standalone CLI:  python tools/backup.py [output-dir]

Captures the three pieces of state worth restoring:

  * data/prism.db          - 7y audit log + metrics + events
  * data/config.json       - server inventory + LDAP + backup-admin hash
  * data/prism.key.dpapi   - Fernet key wrapping every stored credential
                              (or data/prism.key if DPAPI variant missing)

Uses sqlite3.Connection.backup() for the DB so a running Prism is not
disrupted. Writes a manifest.json (host name, host SID best-effort, git
rev, sha256 per file) and a sibling RESTORE.md beside the artefacts.

The DPAPI-wrapped key is bound to the Windows account that originally
ran Prism. Restoring on a different host or service account decrypts
to garbage SILENTLY. The manifest records the SID so tools/restore.py
can warn loudly.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _current_user_sid() -> Optional[str]:
    """Best-effort: return the SID of the user running this script.

    pywin32 is preferred (it's already in requirements.txt for prod). If
    not importable (Linux/macOS dev env, or a slim Python install), fall
    back to ``whoami /user`` parsing on Windows. Returns None if neither
    works -- the manifest will record an empty string and a comment.
    """
    try:
        import win32security  # type: ignore
        import win32api  # type: ignore
        user = win32api.GetUserNameEx(2)  # NameSamCompatible -> DOMAIN\user
        sid_obj, _domain, _type = win32security.LookupAccountName(None, user)
        return win32security.ConvertSidToStringSid(sid_obj)
    except Exception:
        pass

    if sys.platform != "win32":
        return None

    try:
        out = subprocess.run(
            ["whoami", "/user"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("S-1-"):
                return line.split()[0]
            parts = line.split()
            for tok in parts:
                if tok.startswith("S-1-"):
                    return tok
    except Exception:
        return None
    return None


def _git_rev(repo_root: Path) -> str:
    head = repo_root / ".git" / "HEAD"
    if not head.exists():
        return "unknown"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            ref_path = repo_root / ".git" / ref.split(" ", 1)[1].strip()
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()
            # packed refs fallback
            packed = repo_root / ".git" / "packed-refs"
            target = ref.split(" ", 1)[1].strip()
            if packed.exists():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + target):
                        return line.split(" ", 1)[0]
            return "unknown"
        return ref
    except Exception:
        return "unknown"


RESTORE_MD = """# Prism backup -- restore instructions

This directory was produced by `tools/backup.py` on {host} at {ts}.

## Contents

- `prism-YYYYMMDD-HHMMSS.db` -- SQLite database (full audit log, metrics,
  events, baselines).
- `config.json` -- server inventory, LDAP config, backup-admin hash.
- `prism.key.dpapi` *or* `prism.key` -- Fernet key that wraps every stored
  WinRM/SNMP/SMTP credential.
- `manifest.json` -- file hashes + source-host fingerprint.

## Restore

    python tools/restore.py <this-directory>

The restore script will refuse to overwrite an existing `data/prism.db`
unless `--force` is passed.

## CRITICAL: DPAPI host binding

If this backup contains `prism.key.dpapi`, the encryption key is bound to
the Windows user account that was running Prism on `{host}` (SID
`{sid}`). Restoring on a DIFFERENT host or service account silently
decrypts to garbage -- every stored WinRM password becomes unusable
without a single error message.

If the source host or account is gone:
  1. Run `tools/restore.py --accept-key-loss <dir>` to put DB + config in
     place.
  2. Open the Prism UI, walk the server inventory, and re-enter every
     credential from your password manager.
  3. The audit log and historical metrics survive intact -- only the
     wrapped credentials need re-keying.

## What is NOT in this backup

- `data/audit_mirror.jsonl` -- per-host append-only mirror, ship to your
  SIEM separately. Restoring it would re-import old events into the
  mirror that the SIEM already has.
- Flask session secret (`data/flask_secret.key`) -- a fresh secret will
  log everyone out, which is the desired behaviour after DR.
- TLS certs (`data/test-cert.pem` etc.) -- regenerate on the new host.
"""


def _build_manifest(
    out_dir: Path,
    db_target: Path,
    config_src: Path,
    key_src: Optional[Path],
    install_state_src: Optional[Path] = None,
) -> dict:
    files = [
        {"name": db_target.name, "sha256": _sha256(db_target),
         "size": db_target.stat().st_size, "role": "database"},
        {"name": "config.json", "sha256": _sha256(out_dir / "config.json"),
         "size": (out_dir / "config.json").stat().st_size, "role": "config"},
    ]
    if key_src is not None:
        kn = key_src.name
        files.append({
            "name": kn,
            "sha256": _sha256(out_dir / kn),
            "size": (out_dir / kn).stat().st_size,
            "role": "fernet_key_dpapi" if kn.endswith(".dpapi") else "fernet_key_raw",
        })
    # F-BR-1 (CSV-15 remediation): include install_state.json in the
    # manifest if it exists. The file holds cross-restart install /
    # reboot / stabilising lifecycle state; without it, a restore on a
    # different host loses any in-flight install context.
    if install_state_src is not None and (out_dir / "install_state.json").exists():
        files.append({
            "name": "install_state.json",
            "sha256": _sha256(out_dir / "install_state.json"),
            "size": (out_dir / "install_state.json").stat().st_size,
            "role": "install_state",
        })

    sid = _current_user_sid()
    return {
        "schema": "prism-backup/1",
        "created_utc": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source_host": socket.gethostname(),
        "source_user_sid": sid or "",
        "source_user_sid_note": (
            "" if sid else
            "SID lookup failed (pywin32 not available or non-Windows host); "
            "DPAPI-bound key cannot be safely restored on a different host."
        ),
        "python_version": sys.version.split()[0],
        "git_revision": _git_rev(PROJECT_ROOT),
        "files": files,
    }


def run(output_dir: str | Path, data_dir: str | Path | None = None,
        config_path: str | Path | None = None) -> Path:
    """Run a backup. Returns the output directory path.

    ``data_dir`` defaults to ``<repo>/data`` and locates prism.db and the
    encryption key. ``config_path`` defaults to ``<repo>/config.json``.

    These are SEPARATE arguments because the two files genuinely live in
    different places, and conflating them is the bug this signature fixes: the
    config path was derived as ``data_dir / "config.json"``, so every scheduled
    backup raised FileNotFoundError before writing anything. Silently, for 20
    days — config.json is at the repo ROOT (per the README and .gitignore),
    while prism.db and the keys are under data/.
    """
    out = Path(output_dir).resolve()
    src = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR

    db_src = src / "prism.db"
    if not db_src.exists():
        raise FileNotFoundError(f"prism.db not found at {db_src}")
    config_src = (Path(config_path).resolve() if config_path
                  else PROJECT_ROOT / "config.json")
    if not config_src.exists():
        raise FileNotFoundError(f"config.json not found at {config_src}")

    # Prefer DPAPI-wrapped key; fall back to raw key.
    key_src: Optional[Path] = None
    for cand in (src / "prism.key.dpapi", src / "prism.key"):
        if cand.exists():
            key_src = cand
            break

    out.mkdir(parents=True, exist_ok=True)

    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    db_target = out / f"prism-{ts}.db"

    # Online-safe DB backup via the SQLite C API.
    src_conn = sqlite3.connect(str(db_src))
    try:
        dst_conn = sqlite3.connect(str(db_target))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    shutil.copy2(config_src, out / "config.json")
    if key_src is not None:
        shutil.copy2(key_src, out / key_src.name)

    # F-BR-1: install_state.json is operationally-important cross-restart
    # state. Copy if present; absent is fine (fresh install with nothing
    # in flight).
    install_state_src: Optional[Path] = src / "install_state.json"
    if install_state_src.exists():
        shutil.copy2(install_state_src, out / "install_state.json")
    else:
        install_state_src = None

    manifest = _build_manifest(out, db_target, config_src, key_src,
                               install_state_src=install_state_src)
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    (out / "RESTORE.md").write_text(
        RESTORE_MD.format(
            host=manifest["source_host"],
            ts=manifest["created_utc"],
            sid=manifest["source_user_sid"] or "(unknown)",
        ),
        encoding="utf-8",
    )

    return out


def _summarise(out_dir: Path) -> str:
    files = [p for p in out_dir.iterdir() if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    mb = total / (1024 * 1024)
    return f"OK: backup written to {out_dir}, {len(files)} files, total size {mb:.2f} MB"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Back up Prism state.")
    p.add_argument("output_dir", nargs="?", default="backup",
                   help="Directory to write the backup into (created if missing).")
    p.add_argument("--data-dir", default=None,
                   help="Override the source data directory (default: <repo>/data).")
    args = p.parse_args(argv)

    try:
        out = run(args.output_dir, data_dir=args.data_dir)
    except Exception as exc:
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 1
    print(_summarise(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
