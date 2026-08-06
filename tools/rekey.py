"""Prism Fernet-key rotation tool.

Standalone CLI:  python tools/rekey.py [--in-place|--dry-run]

Why this exists
---------------
Prism wraps every stored credential (servers[*].password, the SMTP password,
the LDAP bind password) with a single process-wide Fernet key. That key
itself is wrapped via Windows DPAPI under ``data/prism.key.dpapi`` with
*current-user* scope. Two operational consequences:

  * Rotating the Prism service account is destructive — every stored
    credential becomes unreadable, recovery is "re-enter every password".
    In practice, operators never rotate. A leaked service account
    credential remains valid forever.
  * There is no in-app rekey path; even if compromise is suspected, the
    only remediation is "delete prism.key.dpapi, re-add every server".

This tool gives operators an actual rotation procedure:

    1. Read current Fernet key (via crypto_utils).
    2. Decrypt every encrypted password in config.json.
    3. Generate a new Fernet key.
    4. Re-encrypt every password with the new key.
    5. Atomically swap key file + config.json.
    6. Audit-log the rotation (if a Database is reachable).

The swap order matters: we write the new key file FIRST, then the new
config.json, then archive the old key. If we crash mid-rekey the operator
can roll back by restoring the archived ``.bak`` key.

Run with ``--dry-run`` to count and verify decryption without writing.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

import crypto_utils  # noqa: E402

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
LOG_PATH = DEFAULT_DATA_DIR / "rekey.log"

logger = logging.getLogger("prism.rekey")


def _setup_file_logger(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Encrypted-field discovery / mutation
# ---------------------------------------------------------------------------

def _is_encrypted(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(crypto_utils.ENCRYPTED_PREFIX)
        and len(value) > len(crypto_utils.ENCRYPTED_PREFIX)
    )


def _iter_credential_paths(cfg: dict) -> list[tuple[list, str]]:
    """Return (parent, key) pairs for every field we know carries a credential.

    We only descend into the canonical credential locations:
      * servers[*].password
      * settings.email.password
      * settings.auth.ldap_bind_password
    """
    out: list[tuple[list, str]] = []

    servers = cfg.get("servers")
    if isinstance(servers, list):
        for srv in servers:
            if isinstance(srv, dict) and "password" in srv:
                out.append((srv, "password"))

    settings = cfg.get("settings", {})
    if isinstance(settings, dict):
        email = settings.get("email")
        if isinstance(email, dict) and "password" in email:
            out.append((email, "password"))
        auth = settings.get("auth")
        if isinstance(auth, dict) and "ldap_bind_password" in auth:
            out.append((auth, "ldap_bind_password"))

    return out


# ---------------------------------------------------------------------------
# Core rekey routine
# ---------------------------------------------------------------------------

class RekeyError(RuntimeError):
    pass


def run(
    config_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    *,
    dry_run: bool = False,
    db: Any | None = None,
) -> dict:
    """Perform (or simulate) a Fernet-key rotation. Returns a summary dict.

    Keys in the summary:
        rekeyed:    int  - number of credentials successfully re-encrypted
        skipped:    int  - empty / non-encrypted fields left as-is
        failed:     int  - decryption failures (dry-run continues; in-place aborts)
        backup:     str  - path to archived old key (in-place only) or ""
        dry_run:    bool
    """
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    ddir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR

    if not cfg_path.exists():
        raise RekeyError(f"config.json not found at {cfg_path}")

    logger.info("rekey starting (dry_run=%s) config=%s data_dir=%s",
                dry_run, cfg_path, ddir)

    # Load OLD fernet via the running app's resolution path. This honours
    # PRISM_DPAPI=0 and the existing DPAPI/plain-text precedence.
    old_key_path_dpapi = ddir / "prism.key.dpapi"
    old_key_path_plain = ddir / "prism.key"

    # Use crypto_utils.get_fernet, but we have to make sure crypto_utils
    # is looking at the SAME data dir we are. In the default case it is
    # (KEY_PATH is repo-root-relative). For tests we pass data_dir and
    # monkeypatch in the tests.
    # Drop any memoised Fernet BEFORE reading the old key. get_fernet() caches
    # per process, so a long-running caller (or a test that repoints KEY_PATH)
    # could otherwise hand us a key that is no longer the one on disk — and
    # rekey must decrypt with whatever is actually there right now, or it
    # re-encrypts every credential with a key nothing can read.
    crypto_utils.reset_key_cache()
    old_fernet = crypto_utils.get_fernet()

    # Read config.
    raw = cfg_path.read_text(encoding="utf-8")
    cfg = json.loads(raw)

    targets = _iter_credential_paths(cfg)
    logger.info("scanned config: %d credential field(s) found", len(targets))

    # Pass 1: decrypt with old key.
    decrypted: list[tuple[dict, str, str]] = []
    skipped = 0
    failed = 0
    for parent, key in targets:
        val = parent.get(key, "")
        if not val:
            skipped += 1
            continue
        if not _is_encrypted(val):
            # Plain-text or already-blank — leave as-is, no rewrite needed.
            skipped += 1
            continue
        token = val[len(crypto_utils.ENCRYPTED_PREFIX):]
        try:
            plain = old_fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            failed += 1
            logger.error("decrypt FAILED for field %r (token len=%d) — "
                         "old key cannot read this credential",
                         key, len(token))
            if not dry_run:
                raise RekeyError(
                    f"decryption failed for {key}; refusing to rewrite. "
                    "The old key cannot decrypt every stored credential — "
                    "fix that first, otherwise you'd lose data."
                )
            continue
        decrypted.append((parent, key, plain))

    logger.info("decrypted %d credential(s) with old key (skipped=%d failed=%d)",
                len(decrypted), skipped, failed)

    if dry_run:
        logger.info("dry-run: not writing anything")
        return {
            "rekeyed": len(decrypted),
            "skipped": skipped,
            "failed": failed,
            "backup": "",
            "dry_run": True,
        }

    # Generate fresh key.
    new_key = Fernet.generate_key()
    new_fernet = Fernet(new_key)

    # Pass 2: re-encrypt under new key (in memory only — config not written
    # until the new key file is on disk).
    rewrites: list[tuple[dict, str, str]] = []
    for parent, key, plain in decrypted:
        token = new_fernet.encrypt(plain.encode("utf-8"))
        rewrites.append((
            parent, key,
            crypto_utils.ENCRYPTED_PREFIX + token.decode("ascii"),
        ))

    # ---- Atomic swap ------------------------------------------------------
    # 1. Archive the old key file (.bak) BEFORE we overwrite anything.
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = ""
    if old_key_path_dpapi.exists():
        backup_path = ddir / f"prism.key.dpapi.{ts}.bak"
        shutil.copy2(old_key_path_dpapi, backup_path)
        archived = str(backup_path)
        logger.info("archived old DPAPI key to %s", backup_path)
    elif old_key_path_plain.exists():
        backup_path = ddir / f"prism.key.{ts}.bak"
        shutil.copy2(old_key_path_plain, backup_path)
        archived = str(backup_path)
        logger.info("archived old plain key to %s", backup_path)

    # 2. Write new key file. Prefer DPAPI when available, mirror the
    #    on-disk format used by crypto_utils._load_or_create_key.
    use_dpapi = crypto_utils._dpapi_available()
    if use_dpapi:
        try:
            wrapped = crypto_utils._dpapi_encrypt(new_key)
            old_key_path_dpapi.write_bytes(wrapped)
            crypto_utils._restrict_file_permissions(old_key_path_dpapi)
            logger.info("wrote new DPAPI-wrapped key to %s", old_key_path_dpapi)
        except Exception as e:
            logger.error("DPAPI encrypt failed (%s); falling back to plain-text",
                         e)
            use_dpapi = False
    if not use_dpapi:
        old_key_path_plain.write_bytes(new_key)
        crypto_utils._restrict_file_permissions(old_key_path_plain)
        logger.info("wrote new plain-text key to %s", old_key_path_plain)

    # get_fernet() memoises the key, so a rotation performed inside a running
    # process would otherwise keep decrypting with the retired one.
    crypto_utils.reset_key_cache()

    # 3. Apply re-encrypted values to the in-memory cfg, then atomically
    #    write config.json (write-then-rename so a crash leaves the old
    #    file intact).
    for parent, key, new_val in rewrites:
        parent[key] = new_val

    fd, tmpname = tempfile.mkstemp(
        prefix="config.", suffix=".tmp", dir=str(cfg_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmpname, cfg_path)
    except Exception:
        # Best-effort cleanup of the tmp file. The old config is still on
        # disk, and the new key is on disk too — that's a recoverable
        # state: operator restores the .bak key and the old config keeps
        # working.
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
    crypto_utils.restrict_config_permissions(cfg_path)
    logger.info("wrote re-encrypted config to %s", cfg_path)

    # 4. If we migrated from plain-text, the .dpapi is now the source of
    #    truth — remove the old plain-text file. (We already archived it.)
    if old_key_path_dpapi.exists() and old_key_path_plain.exists() and use_dpapi:
        try:
            old_key_path_plain.unlink()
            logger.info("removed legacy plain-text key file")
        except OSError as e:
            logger.warning("could not remove %s: %s", old_key_path_plain, e)

    # 5. Audit-log if we got a Database.
    summary = {
        "rekeyed": len(rewrites),
        "skipped": skipped,
        "failed": failed,
        "backup": archived,
        "dry_run": False,
    }
    if db is not None:
        try:
            db.log_audit(
                "system",
                "fernet_key_rotated",
                "system",
                details=json.dumps({
                    "rekeyed": summary["rekeyed"],
                    "skipped": summary["skipped"],
                    "archived": archived,
                    "format": "dpapi" if use_dpapi else "plaintext",
                }),
            )
            logger.info("audit row written")
        except Exception as e:
            logger.warning("could not write audit row: %s", e)

    logger.info("rekey complete: %d credential(s) rotated", len(rewrites))
    return summary


def _format_summary(s: dict) -> str:
    if s["dry_run"]:
        return (
            f"DRY-RUN OK: would rekey {s['rekeyed']} credential(s) "
            f"(skipped={s['skipped']} failed={s['failed']})"
        )
    return (
        f"OK: rekeyed {s['rekeyed']} credentials, "
        f"old key archived to {s['backup'] or '(no prior key)'}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Rotate the Prism Fernet key and re-encrypt every stored credential.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--in-place", action="store_true",
                      help="Perform the rotation (default mode).")
    mode.add_argument("--dry-run", action="store_true",
                      help="Decrypt + count only; do not write anything.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                   help="Path to config.json (default: <repo>/config.json)")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                   help="Path to data dir holding the key file (default: <repo>/data)")
    args = p.parse_args(argv)

    _setup_file_logger(LOG_PATH)

    # The Database connection is optional — if Prism is running we'd have
    # contention on prism.db, and this tool is meant to be run with Prism
    # stopped. We attempt a non-disruptive open but tolerate failure.
    db = None
    try:
        from database import Database
        db_path = Path(args.data_dir) / "prism.db"
        if db_path.exists():
            db = Database(db_path)
    except Exception as e:
        logger.warning("could not open prism.db for audit log: %s", e)

    try:
        summary = run(
            config_path=args.config,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
            db=db,
        )
    except RekeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        logger.exception("rekey crashed: %s", e)
        print(f"ERROR: rekey crashed: {e}", file=sys.stderr)
        return 1

    print(_format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
