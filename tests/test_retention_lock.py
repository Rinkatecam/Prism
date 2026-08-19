"""Retention's chunked delete must release the write lock between batches.

Collector audit, scale findings. `_chunked_delete` exists because a single
`DELETE FROM logs WHERE timestamp < ?` over tens of millions of rows holds the
process-global write lock for minutes, blocking every collector write in the
process. Its docstring said the loop "keeps each statement small so the lock is
released between batches".

It did not. The caller wrapped the entire loop in one `with self._write_lock`,
so the chunking bounded each STATEMENT while the lock was held for the whole
multi-minute operation — the exact stall the chunking was written to prevent. The
comment beside it even said the delete happened "OUTSIDE the big lock block".
This is the repeating shape in this codebase: not a missing mechanism, a
mechanism whose effect was cancelled one level up, described correctly and
behaving otherwise.

There is a second reason this has to stay right, and it is worse than slow.
`_write_lock` is a plain `Lock`, not an `RLock`. Now that the acquire lives
inside the loop, a caller that takes the lock first does not merely serialise —
it DEADLOCKS the retention thread. That cannot be tested by calling it (the test
would hang), so it is asserted structurally instead, over every caller.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _CountingLock:
    """Wraps a real lock and counts how many times it is entered."""

    def __init__(self, inner):
        self._inner = inner
        self.acquisitions = 0
        self.held = False
        self.max_concurrent_holds = 0

    def __enter__(self):
        self._inner.acquire()
        self.acquisitions += 1
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._inner.release()
        return False

    def acquire(self, *a, **k):
        return self._inner.acquire(*a, **k)

    def release(self):
        return self._inner.release()


def _seed_old_logs(db, n: int) -> None:
    """`n` log rows well outside any retention window."""
    with db._write_lock:
        conn = db._get_conn()
        try:
            conn.executemany(
                "INSERT INTO logs (server_name, timestamp, log_source, level, "
                "event_id, message) VALUES (?, ?, ?, ?, ?, ?)",
                [("SRV", "2001-01-01T00:00:00Z", "System", "Error", 1, f"m{i}")
                 for i in range(n)])
            conn.commit()
        finally:
            conn.close()


def test_the_lock_is_taken_once_per_chunk(tmp_db):
    """The behavioural claim. Five rows at two per chunk is three statements, so
    three acquisitions — not one acquisition spanning all three."""
    _seed_old_logs(tmp_db, 5)
    counter = _CountingLock(tmp_db._write_lock)
    tmp_db._write_lock = counter

    conn = tmp_db._get_conn()
    try:
        deleted = tmp_db._chunked_delete(conn, "logs", "timestamp", 0, chunk=2)
    finally:
        conn.close()

    assert deleted == 5
    assert counter.acquisitions == 3, (
        f"expected one acquisition per chunk (3), got {counter.acquisitions} — "
        f"the lock is being held across batches again")
    assert counter.held is False, "the lock was not released"


def test_the_lock_is_free_between_chunks(tmp_db):
    """What "released between batches" means to the thread that was blocked: a
    collector write can get in while retention is still working."""
    _seed_old_logs(tmp_db, 6)
    observed = []

    real = tmp_db._write_lock

    class _Watcher(_CountingLock):
        def __exit__(self, *exc):
            super().__exit__(*exc)
            # Between chunks the lock must be genuinely acquirable by someone
            # else. Non-blocking, so a failure is a failed assertion rather
            # than a hung test.
            got = self._inner.acquire(blocking=False)
            observed.append(got)
            if got:
                self._inner.release()

    tmp_db._write_lock = _Watcher(real)
    conn = tmp_db._get_conn()
    try:
        tmp_db._chunked_delete(conn, "logs", "timestamp", 0, chunk=2)
    finally:
        conn.close()

    assert observed, "no chunk boundary was observed"
    assert all(observed), "the lock stayed held across a chunk boundary"


def test_a_short_delete_still_takes_the_lock_once(tmp_db):
    """One partial chunk is one statement. The loop must not acquire twice for
    a single batch, because retention runs hourly and the lock is the fleet's."""
    _seed_old_logs(tmp_db, 3)
    counter = _CountingLock(tmp_db._write_lock)
    tmp_db._write_lock = counter
    conn = tmp_db._get_conn()
    try:
        tmp_db._chunked_delete(conn, "logs", "timestamp", 0, chunk=10)
    finally:
        conn.close()
    assert counter.acquisitions == 1


def test_nothing_recent_is_deleted(tmp_db):
    """The lock change must not have changed what retention deletes."""
    _seed_old_logs(tmp_db, 2)
    with tmp_db._write_lock:
        conn = tmp_db._get_conn()
        try:
            conn.execute(
                "INSERT INTO logs (server_name, timestamp, log_source, level, "
                "event_id, message) VALUES ('SRV', "
                "strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'System', 'Error', 1, 'new')")
            conn.commit()
        finally:
            conn.close()
    conn = tmp_db._get_conn()
    try:
        tmp_db._chunked_delete(conn, "logs", "timestamp", 30, chunk=2)
        remaining = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    finally:
        conn.close()
    assert remaining == 1


# ── the deadlock guard, asserted structurally ─────────────────────────────

def test_no_caller_holds_the_write_lock_across_the_chunked_delete():
    """`_write_lock` is a plain Lock, not an RLock. A caller that holds it while
    calling `_chunked_delete` no longer merely serialises — it deadlocks the
    retention thread outright. That cannot be tested by calling it, so it is
    asserted over the AST of every caller instead.

    This is also the exact defect that existed: `cleanup_old_data` wrapped the
    call in `with self._write_lock`, and back then the symptom was a stall
    rather than a hang, which is why it survived so long.
    """
    tree = ast.parse((PROJECT_ROOT / "database.py").read_text(encoding="utf-8"))

    def _calls_chunked_delete(node) -> bool:
        return any(isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "_chunked_delete"
                   for n in ast.walk(node))

    offenders = []
    for with_node in (n for n in ast.walk(tree) if isinstance(n, ast.With)):
        holds_lock = any(
            isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "_write_lock"
            for item in with_node.items)
        if holds_lock and _calls_chunked_delete(with_node):
            offenders.append(with_node.lineno)

    assert not offenders, (
        f"database.py holds _write_lock across _chunked_delete at line(s) "
        f"{offenders} — a plain Lock, so this deadlocks rather than stalls")


def test_the_chunked_delete_owns_its_own_lock():
    """The other half of the pair: having removed the outer lock, the acquire
    has to actually be inside the loop. Absent both, retention would write
    without the lock at all — faster, and corrupting."""
    tree = ast.parse((PROJECT_ROOT / "database.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_chunked_delete")
    loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While))
    takes_lock = any(
        isinstance(item.context_expr, ast.Attribute)
        and item.context_expr.attr == "_write_lock"
        for w in ast.walk(loop) if isinstance(w, ast.With)
        for item in w.items)
    assert takes_lock, "the chunk loop does not take the write lock at all"
