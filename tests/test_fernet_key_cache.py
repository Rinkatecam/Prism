"""The Fernet key must be unwrapped once per process, not once per password.

Found 2026-08-06 while diagnosing collector/web-server load. `get_fernet()`
built a fresh Fernet on every call, and `_load_or_create_key()` reads
`prism.key.dpapi` from disk and calls `CryptUnprotectData`. So a DPAPI syscall
plus a file read happened once PER PASSWORD.

Measured before the fix: one `ConfigManager.get_servers()` call (29 servers) =
29 key unwraps, 21-30 ms. At the ~2 calls/second observed in production that is
~58 DPAPI syscalls and ~58 file reads every second, on a 4-thread waitress
process whose request queue was observed backing up to depth 9.

After: 10 calls = 1 unwrap, 1.3 ms each.
"""

from __future__ import annotations

import pytest

import crypto_utils


@pytest.fixture(autouse=True)
def _reset():
    crypto_utils.reset_key_cache()
    yield
    crypto_utils.reset_key_cache()


def test_get_fernet_returns_the_same_instance():
    assert crypto_utils.get_fernet() is crypto_utils.get_fernet()


def test_key_is_unwrapped_once_across_many_calls(monkeypatch):
    calls = {"n": 0}
    real = crypto_utils._load_or_create_key

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(crypto_utils, "_load_or_create_key", counting)
    crypto_utils.reset_key_cache()
    for _ in range(50):
        crypto_utils.get_fernet()
    assert calls["n"] == 1, f"unwrapped {calls['n']} times; caching is not working"


def test_decrypting_many_passwords_costs_one_unwrap(monkeypatch):
    """The actual production shape: get_servers() decrypts 29 passwords."""
    calls = {"n": 0}
    real = crypto_utils._load_or_create_key

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(crypto_utils, "_load_or_create_key", counting)
    crypto_utils.reset_key_cache()

    tokens = [crypto_utils.encrypt_password(f"pw-{i}") for i in range(29)]
    calls["n"] = 0
    out = [crypto_utils.decrypt_password(t) for t in tokens]

    assert out == [f"pw-{i}" for i in range(29)], "round-trip broken"
    assert calls["n"] == 0, "cached instance should need no further unwrap"


def test_round_trip_still_works_through_the_cache():
    secret = "s3cret-value-!@#-äöü"
    assert crypto_utils.decrypt_password(crypto_utils.encrypt_password(secret)) == secret


def test_reset_key_cache_forces_a_reload(monkeypatch):
    """tools/rekey.py rotates the key mid-process; without invalidation the
    cache would keep decrypting with the retired key."""
    calls = {"n": 0}
    real = crypto_utils._load_or_create_key

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(crypto_utils, "_load_or_create_key", counting)
    crypto_utils.reset_key_cache()

    crypto_utils.get_fernet()
    assert calls["n"] == 1
    crypto_utils.get_fernet()
    assert calls["n"] == 1, "second call should hit the cache"

    crypto_utils.reset_key_cache()
    crypto_utils.get_fernet()
    assert calls["n"] == 2, "reset must force a reload"


def test_rekey_invalidates_the_cache():
    """Pin the wiring, not just the helper's existence."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "tools" / "rekey.py"
    body = src.read_text(encoding="utf-8")
    assert "reset_key_cache()" in body, (
        "rekey rotates the key on disk; it must invalidate the in-process cache")


def test_cache_is_thread_safe():
    """get_fernet() is called from the collector threads and from waitress
    request threads simultaneously."""
    import threading
    crypto_utils.reset_key_cache()
    seen = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        seen.append(crypto_utils.get_fernet())

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(seen) == 8
    assert all(f is seen[0] for f in seen), "racing threads got different instances"
