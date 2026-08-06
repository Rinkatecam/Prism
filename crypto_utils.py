"""Credential encryption utilities for Prism.

Uses Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256).

Key storage strategy
--------------------
The Fernet key itself is sensitive — anyone who reads it can decrypt every
stored credential. We support two on-disk formats for the key:

1. **DPAPI-protected** (`data/prism.key.dpapi`, preferred on Windows). The
   raw Fernet key is wrapped via Windows DPAPI (`CryptProtectData`,
   CRYPTPROTECT_LOCAL_MACHINE=False), which ties the wrapping to the user
   account that runs Prism. A stolen DB backup or VM snapshot is useless
   without that user's profile + credentials. First-run on Windows creates
   this file; legacy `data/prism.key` is auto-migrated and deleted.

2. **Plain-text** (`data/prism.key`, fallback). Used when DPAPI isn't
   available (non-Windows dev box, missing pywin32) or when the user has
   set `PRISM_DPAPI=0`. The file is icacls-restricted to the running
   user, but that's it — anyone with local privilege escalation can read
   it. Acceptable for single-host dev, NOT for production.

The migration is idempotent. Existing deployments that already wrote
prism.key will, on next start, see DPAPI available, encrypt the key
in-place, and remove the plain-text file. Operators don't need to do
anything except restart Prism.
"""

import os
import subprocess
import logging
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("prism.crypto")

KEY_PATH = Path(__file__).parent / "data" / "prism.key"
KEY_PATH_DPAPI = Path(__file__).parent / "data" / "prism.key.dpapi"

# DPAPI is only meaningful on Windows. The pywin32 import is lazy so that
# Linux/macOS dev environments still function (with the plain-text fallback).
_DPAPI_DISABLED_ENV = "PRISM_DPAPI"


def _dpapi_available() -> bool:
    if os.environ.get(_DPAPI_DISABLED_ENV, "1") == "0":
        return False
    if os.name != "nt":
        return False
    try:
        import win32crypt  # noqa: F401  -- pywin32
        return True
    except ImportError:
        return False


def _dpapi_encrypt(blob: bytes) -> bytes:
    """Wrap `blob` with current-user DPAPI. Raises RuntimeError on failure."""
    import win32crypt
    # CryptProtectData(data, description, optional_entropy, reserved, prompt_struct, flags)
    # flags=0 → CRYPTPROTECT_UI_FORBIDDEN is implied by our calling convention,
    # but we want CURRENT_USER scope (not LOCAL_MACHINE), so we pass 0.
    return win32crypt.CryptProtectData(blob, "prism-fernet-key", None, None, None, 0)


def _dpapi_decrypt(blob: bytes) -> bytes:
    import win32crypt
    _, plain = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return plain

# Prefix added to encrypted passwords so we can distinguish them from plain text
ENCRYPTED_PREFIX = "enc:"

# Mask returned by the API instead of real passwords
PASSWORD_MASK = "********"


def _restrict_file_permissions(filepath: Path):
    """Restrict file to owner-only read/write on Windows (via icacls)."""
    try:
        filepath_str = str(filepath)
        # Remove inherited permissions and grant only the current user full control
        username = os.environ.get("USERNAME", "")
        if username:
            # (R,W,D) — the D is load-bearing. This used to grant only (R,W),
            # and because /inheritance:r strips everything else, the owner was
            # left unable to DELETE the very file it had just written. That is
            # what stranded a plain-text prism.key next to its DPAPI replacement
            # for 34 days: the migration's unlink() failed with Access Denied,
            # the failure was logged at warning level, and nothing retried.
            # Write access without delete access is not a meaningful extra
            # restriction — the owner can already truncate the file to nothing.
            result = subprocess.run(
                ["icacls", filepath_str, "/inheritance:r", "/grant:r", f"{username}:(R,W,D)"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "icacls failed (rc=%d) on %s: %s",
                    result.returncode, filepath_str, result.stderr.strip(),
                )
            else:
                logger.debug("Restricted permissions on %s", filepath_str)
    except Exception as e:
        logger.warning("Could not restrict file permissions on %s: %s", filepath, e)


def _purge_plaintext_key_if_redundant() -> bool:
    """Delete ``data/prism.key`` when a working DPAPI-wrapped key supersedes it.

    Only ever called after ``KEY_PATH_DPAPI`` has been read AND successfully
    unwrapped, so by construction the plain-text file is redundant at that
    point. Returns True if a file was removed.

    Deliberately loud on failure: a plain-text key that cannot be removed is a
    standing exposure, not a cosmetic problem, and the previous code's
    ``logger.warning`` was quiet enough to be missed for a month.
    """
    if not KEY_PATH.exists():
        return False

    def _removed() -> bool:
        logger.warning(
            "Removed redundant PLAIN-TEXT key %s — a working DPAPI-wrapped key "
            "(%s) supersedes it. Anything holding a copy of that file (backups, "
            "VM snapshots) should be treated as holding your credentials.",
            KEY_PATH, KEY_PATH_DPAPI,
        )
        return True

    try:
        KEY_PATH.unlink()
        return _removed()
    except PermissionError:
        # Expected on any deployment that ran the old _restrict_file_permissions:
        # it granted (R,W) with inheritance stripped, leaving the owner without
        # the DELETE right on its own file. Restore delete access, then retry.
        logger.info("Plain-text key not deletable; restoring delete rights on %s",
                    KEY_PATH)
        try:
            username = os.environ.get("USERNAME", "")
            if username:
                subprocess.run(
                    ["icacls", str(KEY_PATH), "/grant", f"{username}:(R,W,D)"],
                    capture_output=True, text=True,
                )
            KEY_PATH.unlink()
            return _removed()
        except Exception as e2:
            logger.error(
                "SECURITY: could not delete redundant plain-text key %s even after "
                "restoring delete rights (%s). Every stored credential is readable "
                "by anyone who can read that file. Delete it manually.",
                KEY_PATH, e2,
            )
            return False
    except Exception as e:
        logger.error(
            "SECURITY: could not delete redundant plain-text key %s (%s). Every "
            "stored credential is readable by anyone who can read that file. "
            "Delete it manually.",
            KEY_PATH, e,
        )
        return False


def _load_or_create_key() -> bytes:
    """Load the Fernet key from disk, or generate a new one on first run.

    Priority:
        1. DPAPI-wrapped file (prism.key.dpapi) if present and DPAPI available.
        2. Legacy plain-text file (prism.key). Auto-migrated to DPAPI when
           possible.
        3. Generate a brand new key, store it via DPAPI when possible,
           otherwise plain-text.
    """
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    use_dpapi = _dpapi_available()

    # 1) DPAPI-wrapped file
    if use_dpapi and KEY_PATH_DPAPI.exists():
        try:
            wrapped = KEY_PATH_DPAPI.read_bytes()
            key = _dpapi_decrypt(wrapped)
            if key:
                # Enforce the invariant the migration below intends but cannot
                # guarantee: once a working DPAPI-wrapped key exists, NO
                # plain-text copy may remain. The migration deletes it, but its
                # unlink() sits inside a try/except — if that unlink ever failed
                # (file locked, transient AV handle) the warning scrolled past
                # and this branch returned early on every subsequent start, so
                # the plain-text key stayed on disk forever. Observed in the
                # wild: prism.key survived 34 days next to prism.key.dpapi,
                # byte-identical, still decrypting live credentials — which
                # silently voids the whole point of DPAPI, because a copied
                # data/ directory carries both the key and the database and
                # file ACLs do not survive the copy.
                _purge_plaintext_key_if_redundant()
                return key
            logger.warning("DPAPI key file empty after decrypt; falling through")
        except Exception as e:
            # If DPAPI decrypt fails, the wrapping account changed (e.g.
            # the service was reinstalled under a different user). The
            # plain-text file (if any) becomes the recovery path.
            logger.error("DPAPI decrypt failed: %s — falling back to plain-text key", e)

    # 2) Plain-text file. If DPAPI is now available, migrate -- but ONLY
    #    when the operator has explicitly opted in via the env var. The
    #    silent migration path is a key-injection escape hatch: an
    #    attacker who can write data/prism.key (without a corresponding
    #    .dpapi file) gets Prism to adopt the attacker's Fernet key on
    #    next start. See docs/KEY_ROTATION.md.
    if KEY_PATH.exists():
        # Only gate when migration would actually fire (DPAPI available
        # and no DPAPI file yet). On non-Windows / pywin32-missing dev
        # boxes the plain-text file is the supported steady state.
        if use_dpapi and not KEY_PATH_DPAPI.exists():
            allow_migrate = os.environ.get("PRISM_ALLOW_PLAINTEXT_MIGRATION") == "1"
            if not allow_migrate:
                raise RuntimeError(
                    "Refusing to migrate plain-text key without "
                    "PRISM_ALLOW_PLAINTEXT_MIGRATION=1 "
                    "(see docs/KEY_ROTATION.md)"
                )
        key = KEY_PATH.read_bytes().strip()
        if key:
            if use_dpapi:
                wrapped_ok = False
                try:
                    wrapped = _dpapi_encrypt(key)
                    KEY_PATH_DPAPI.write_bytes(wrapped)
                    _restrict_file_permissions(KEY_PATH_DPAPI)
                    wrapped_ok = True
                except Exception as e:
                    logger.warning(
                        "Could not migrate key to DPAPI (%s); leaving plain-text in place.",
                        e,
                    )
                # Removal is a SEPARATE step from wrapping, on purpose. Both used
                # to share one try/except, so a failed unlink was reported as
                # "could not migrate" even though the .dpapi file had already
                # been written — and nothing ever retried, because the next
                # start returns from the DPAPI branch above. Splitting them means
                # a wrap that succeeded is reported as such, and the removal gets
                # its own loud, specific error.
                if wrapped_ok:
                    logger.info("Migrated encryption key to DPAPI (%s)", KEY_PATH_DPAPI)
                    _purge_plaintext_key_if_redundant()
            return key

    # 3) Generate a new key
    key = Fernet.generate_key()
    if use_dpapi:
        try:
            wrapped = _dpapi_encrypt(key)
            KEY_PATH_DPAPI.write_bytes(wrapped)
            _restrict_file_permissions(KEY_PATH_DPAPI)
            logger.info("Generated new DPAPI-wrapped encryption key at %s", KEY_PATH_DPAPI)
            return key
        except Exception as e:
            logger.warning("DPAPI encrypt failed (%s); falling back to plain-text key", e)
    KEY_PATH.write_bytes(key)
    _restrict_file_permissions(KEY_PATH)
    logger.warning(
        "Generated PLAIN-TEXT encryption key at %s. Install pywin32 and "
        "restart to migrate to DPAPI.",
        KEY_PATH,
    )
    return key


_fernet_cache: Fernet | None = None
_fernet_lock = __import__("threading").Lock()


def reset_key_cache() -> None:
    """Drop the memoised Fernet. Call after rotating the key on disk.

    ``tools/rekey.py`` writes a new key file mid-process; without this the
    in-process cache would keep decrypting with the retired key.
    """
    global _fernet_cache
    with _fernet_lock:
        _fernet_cache = None


def get_fernet() -> Fernet:
    """Return a Fernet instance using the app's encryption key.

    MEMOISED, and that is not a micro-optimisation. This used to build a fresh
    Fernet on every call, and ``_load_or_create_key`` reads ``prism.key.dpapi``
    from disk and calls ``CryptUnprotectData`` — so a DPAPI syscall plus a file
    read happened once PER PASSWORD. ``ConfigManager.get_servers()`` decrypts
    all 29 server passwords, so a single call cost 29 unwraps and 21-30 ms;
    measured at roughly 2 calls/second in production, that is ~58 DPAPI
    syscalls and ~58 file reads every second, on a 4-thread waitress process
    whose request queue was observed backing up to depth 9.

    The key cannot change under a running process except via ``tools/rekey.py``,
    which calls ``reset_key_cache()``.
    """
    global _fernet_cache
    if _fernet_cache is None:
        with _fernet_lock:
            if _fernet_cache is None:      # re-check under the lock
                _fernet_cache = Fernet(_load_or_create_key())
    return _fernet_cache


def encrypt_password(plain_password: str) -> str:
    """Encrypt a plain-text password. Returns a string prefixed with 'enc:'."""
    if not plain_password:
        return ""
    # Already encrypted? Return as-is.
    if plain_password.startswith(ENCRYPTED_PREFIX):
        return plain_password
    f = get_fernet()
    token = f.encrypt(plain_password.encode("utf-8"))
    return ENCRYPTED_PREFIX + token.decode("ascii")


def decrypt_password(stored_password: str) -> str:
    """Decrypt a stored password. Handles both encrypted and legacy plain-text."""
    if not stored_password:
        return ""
    if not stored_password.startswith(ENCRYPTED_PREFIX):
        # Legacy plain-text password -- return as-is (will be encrypted on next save)
        return stored_password
    token = stored_password[len(ENCRYPTED_PREFIX):]
    try:
        f = get_fernet()
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt password -- key may have changed")
        return ""


def is_password_masked(password: str) -> bool:
    """Check if a password value is the API mask placeholder."""
    return password == PASSWORD_MASK


def restrict_config_permissions(config_path: Path):
    """Restrict config.json to owner-only access."""
    _restrict_file_permissions(config_path)
