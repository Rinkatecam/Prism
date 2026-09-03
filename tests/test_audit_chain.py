"""Tests for the audit-log hash chain + JSONL mirror (S1-7 from AUDIT-2026-05)."""

import json
import sqlite3
import pytest


#: The trigger under test, extracted from database.SCHEMA_SQL so the test
#: re-creates the REAL one rather than a paraphrase that could drift from it.
def _extract_trigger(name: str) -> str:
    import re
    import database
    m = re.search(rf"CREATE TRIGGER IF NOT EXISTS {name}\b.*?\nEND;",
                  database.SCHEMA_SQL, re.S)
    assert m, f"{name} not found in SCHEMA_SQL"
    return m.group(0)


_OVERWRITE_TRIGGER_SQL = _extract_trigger("audit_log_no_overwrite")


def test_chain_starts_clean(tmp_db):
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 0


def test_single_row_chain(tmp_db):
    tmp_db.log_audit("alice", "test", "test", "first event")
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 1


def test_multi_row_chain_intact(tmp_db):
    tmp_db.log_audit("alice", "a", "test", "1")
    tmp_db.log_audit("bob", "b", "test", "2")
    tmp_db.log_audit("carol", "c", "test", "3")
    res = tmp_db.verify_audit_chain()
    assert res["ok"] is True
    assert res["checked"] == 3


def test_chain_detects_content_tampering(tmp_db):
    """Drop the triggers (simulating an attacker with file write), tamper with
    a row's details, restore triggers — chain verification must catch it."""
    tmp_db.log_audit("alice", "a", "test", "original")
    tmp_db.log_audit("bob", "b", "test", "second")

    conn = tmp_db._get_conn()
    try:
        # Simulate out-of-band tampering: drop triggers, mutate, recreate.
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
        conn.execute("UPDATE audit_log SET details = 'TAMPERED' WHERE username = 'alice'")
        conn.commit()
    finally:
        conn.close()

    res = tmp_db.verify_audit_chain()
    assert res["ok"] is False
    assert res["first_break_reason"].startswith("row_hash mismatch")


def test_chain_detects_row_deletion(tmp_db):
    """An attacker who deletes a middle row breaks the prev_hash chain
    even if every remaining row still matches its own row_hash."""
    tmp_db.log_audit("alice", "a", "test", "first")
    tmp_db.log_audit("bob", "b", "test", "MIDDLE — to delete")
    tmp_db.log_audit("carol", "c", "test", "third")

    conn = tmp_db._get_conn()
    try:
        conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
        conn.execute("DELETE FROM audit_log WHERE username = 'bob'")
        conn.commit()
    finally:
        conn.close()

    res = tmp_db.verify_audit_chain()
    assert res["ok"] is False
    assert "prev_hash" in res["first_break_reason"]


def test_jsonl_mirror_written(tmp_db, tmp_path, monkeypatch):
    """log_audit() must append a parallel JSONL line to AUDIT_MIRROR_PATH."""
    mirror = tmp_path / "audit_mirror.jsonl"
    monkeypatch.setattr(tmp_db, "AUDIT_MIRROR_PATH", mirror)

    tmp_db.log_audit("alice", "test_action", "test", "hello world")
    tmp_db.log_audit("bob", "another", "test", "second")

    assert mirror.exists()
    lines = mirror.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["username"] == "alice"
    assert rows[0]["action"] == "test_action"
    assert "row_hash" in rows[0]
    # Mirror's chain must match: row 2's prev_hash equals row 1's row_hash
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]


def test_log_audit_accepts_explicit_context(tmp_db):
    """Callers outside Flask context can pass ip/session/request_id explicitly."""
    tmp_db.log_audit("alice", "test", "test", "details",
                     source_ip="10.0.0.5", session_id="sess123", request_id="req456")
    conn = tmp_db._get_conn()
    try:
        row = conn.execute(
            "SELECT source_ip, session_id, request_id FROM audit_log WHERE username='alice'"
        ).fetchone()
    finally:
        conn.close()
    assert row["source_ip"] == "10.0.0.5"
    assert row["session_id"] == "sess123"
    assert row["request_id"] == "req456"


def test_insert_or_replace_is_refused(tmp_db):
    """The regression test for the hole the whole re-baseline design rests on.

    `audit_log` carries BEFORE UPDATE and BEFORE DELETE triggers whose error
    messages say the table is append-only. It was not: `INSERT OR REPLACE`
    rewrites a row in place, because `PRAGMA recursive_triggers` is 0 by
    default, so REPLACE's implicit delete never reaches `audit_log_no_delete`,
    and there was no INSERT trigger to catch the insert half.

    Without this test the next schema edit can silently reopen it.
    """
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    row = conn.execute(
        "SELECT id, username, action FROM audit_log ORDER BY id LIMIT 1").fetchone()
    assert row["username"] == "alice"

    with pytest.raises(sqlite3.IntegrityError, match="overwriting an existing row"):
        conn.execute(
            "INSERT OR REPLACE INTO audit_log (id, timestamp, username, action, category)"
            " VALUES (?, '2020-01-01T00:00:00Z', 'mallory', 'rewritten', 'system')",
            (row["id"],))
        conn.commit()

    after = conn.execute(
        "SELECT username, action FROM audit_log WHERE id = ?", (row["id"],)).fetchone()
    assert after["username"] == "alice", "the row was rewritten"
    assert after["action"] == "login"


def test_insert_or_ignore_overwrite_is_refused(tmp_db):
    """OR IGNORE is the quieter sibling of OR REPLACE. It does not rewrite the
    row, but it must not silently succeed either — a caller that believes it
    appended and did not is how a missing audit record goes unnoticed."""
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    rid = conn.execute("SELECT MIN(id) AS i FROM audit_log").fetchone()["i"]
    with pytest.raises(sqlite3.IntegrityError, match="overwriting an existing row"):
        conn.execute(
            "INSERT OR IGNORE INTO audit_log (id, timestamp, username, action, category)"
            " VALUES (?, '2020-01-01T00:00:00Z', 'mallory', 'x', 'system')", (rid,))
        conn.commit()


def test_explicit_id_out_of_sequence_is_refused(tmp_db):
    """AUTOINCREMENT id reservation. Claiming a high id now leaves a free range
    below it that a later writer can fill with rows that appear older than they
    are. MAX+1 is allowed because that is what an append IS."""
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    top = conn.execute("SELECT MAX(id) AS m FROM audit_log").fetchone()["m"]

    with pytest.raises(sqlite3.IntegrityError, match="must be the next id"):
        conn.execute(
            "INSERT INTO audit_log (id, timestamp, username, action, category)"
            " VALUES (?, '2020-01-01T00:00:00Z', 'mallory', 'reserved', 'system')",
            (top + 500,))
        conn.commit()

    conn.execute(
        "INSERT INTO audit_log (id, timestamp, username, action, category)"
        " VALUES (?, '2020-01-01T00:00:00Z', 'bob', 'next', 'system')", (top + 1,))
    conn.commit()
    assert conn.execute("SELECT MAX(id) AS m FROM audit_log").fetchone()["m"] == top + 1


def test_duplicate_prev_hash_is_refused(tmp_db):
    """The 2026-08-25 fork, as a refused write instead of a permanent break.

    Two rows claiming the same parent is exactly what happened when several
    watchdog threads each read the same chain head. NULL prev_hash must stay
    legal and repeatable — that is a fresh database's genesis row, and the
    pre-migration rows carry it too.
    """
    tmp_db.log_audit("alice", "one", "auth", "first")
    conn = tmp_db._get_conn()
    head = conn.execute(
        "SELECT row_hash FROM audit_log WHERE row_hash IS NOT NULL"
        " ORDER BY id DESC LIMIT 1").fetchone()["row_hash"]

    conn.execute(
        "INSERT INTO audit_log (timestamp, username, action, category, prev_hash, row_hash)"
        " VALUES ('2026-01-01T00:00:00Z','bob','two','auth',?,'aaa')", (head,))
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="chain fork refused"):
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, category, prev_hash, row_hash)"
            " VALUES ('2026-01-01T00:00:01Z','carol','three','auth',?,'bbb')", (head,))
        conn.commit()

    for i in range(3):
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, category, prev_hash)"
            " VALUES ('2026-01-01T00:00:00Z','sys','premigration','system',NULL)")
    conn.commit()


def test_an_existing_duplicate_prev_hash_does_not_block_new_appends(tmp_db):
    """The live database contains a fork: rows 1671 and 1672 share a parent.

    The new trigger fires on NEW rows only, so the pre-existing pair is
    untouched — but a fork already in the table means the NEXT append's own
    prev_hash points at one of two rows, and the trigger must not read that as
    a fresh violation. If this fails, adding the trigger bricks appends on the
    production database and the collector stops recording anything.
    """
    tmp_db.log_audit("alice", "one", "auth", "first")
    conn = tmp_db._get_conn()
    head = conn.execute(
        "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()["row_hash"]

    # Manufacture the fork the way it happened, bypassing the guard the same
    # way the concurrent writers did — two rows, one parent.
    conn.execute("DROP TRIGGER audit_log_no_overwrite")
    for h in ("fork_a", "fork_b"):
        conn.execute(
            "INSERT INTO audit_log (timestamp, username, action, category, prev_hash, row_hash)"
            " VALUES ('2026-01-01T00:00:00Z','sys','forked','system',?,?)", (head, h))
    conn.commit()
    conn.executescript(_OVERWRITE_TRIGGER_SQL)

    tmp_db.log_audit("bob", "after", "auth", "still works")
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM audit_log WHERE username='bob'").fetchone()["c"] == 1


def test_explicit_id_zero_is_refused(tmp_db):
    """id=0 used to read as `NEW.id > 0` false, wrongly classified as
    auto-assign and exempted from every check. It is NOT ambiguous with the
    auto-assign placeholder (that is exactly -1, verified empirically) — 0 is
    a real explicit value the old guard misclassified."""
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audit_log (id, timestamp, username, action, category)"
            " VALUES (0, '2020-01-01T00:00:00Z', 'mallory', 'x', 'system')")
        conn.commit()


def test_explicit_negative_id_other_than_minus_one_is_refused(tmp_db):
    """Any negative id except exactly -1 reads as its own literal value
    inside the trigger (verified: -2, -5, -999 all distinguishable from the
    auto-assign placeholder), so all of them must be caught."""
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    for bad_id in (-2, -5, -999):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO audit_log (id, timestamp, username, action, category)"
                " VALUES (?, '2020-01-01T00:00:00Z', 'mallory', 'x', 'system')", (bad_id,))
            conn.commit()
        conn.rollback()


def test_explicit_id_negative_one_is_a_known_accepted_gap(tmp_db):
    """DOCUMENTS a real, narrow, irreducible limitation rather than hiding it.

    NEW.id for a genuine auto-assigned row (id omitted, or explicit NULL) is
    ALWAYS exactly -1 inside a BEFORE INSERT trigger on SQLite — confirmed
    empirically, not an assumption. An attacker who explicitly supplies
    id=-1 is therefore indistinguishable from a legitimate auto-assign at
    the point this trigger runs, and the row is accepted.

    CORRECTED (3rd review round): `recursive_triggers = ON` does NOT catch
    this. It only refuses a FOLLOW-UP `INSERT OR REPLACE` against a row
    already sitting at id=-1 (routed through `audit_log_no_delete`'s
    unconditional refusal on REPLACE's implicit delete). A rational attacker
    never needs that second write — the one-shot plant below already delivers
    the forged content on the FIRST attempt, using `tmp_db._get_conn()`,
    which has the pragma already ON (every connection in this process does).
    It still succeeds. So this is not "exploitable only on a connection
    without the pragma" — it is exploitable on every connection, pragma or
    not; the pragma's only effect is on a second write this attack does not
    need. Do not cite `recursive_triggers` as mitigating this case.

    Closing this completely needs a `CHECK (id >= 1)` column constraint,
    which is evaluated against the committed value rather than the
    BEFORE-trigger placeholder — verified to work and to not interfere with
    real auto-assigns. SQLite has no ALTER TABLE ADD CONSTRAINT, so adding it
    to the live 1840+-row audit_log requires a full table rebuild (new table,
    copy, drop, rename). That is a separate, higher-risk phase, not this one.

    If this test ever starts FAILING, it means the gap has been closed —
    update this docstring and either delete the test or repurpose it to
    assert the new, tighter behaviour.
    """
    tmp_db.log_audit("alice", "login", "auth", "first")
    conn = tmp_db._get_conn()
    conn.execute(
        "INSERT INTO audit_log (id, timestamp, username, action, category)"
        " VALUES (-1, '2020-01-01T00:00:00Z', 'planted', 'x', 'system')")
    conn.commit()
    row = conn.execute("SELECT username FROM audit_log WHERE id=-1").fetchone()
    assert row["username"] == "planted", (
        "if this now raises IntegrityError, the -1 gap has been closed "
        "(likely by a CHECK constraint) — update this test's docstring")


def test_recursive_triggers_pragma_is_on(tmp_db):
    """Pins the actual pragma value on every connection Prism opens. Without
    this, a future _get_conn refactor could drop the PRAGMA believing it
    'redundant' per the (now-corrected) comment, silently reopening the id=-1
    residual with no test catching it."""
    conn = tmp_db._get_conn()
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
