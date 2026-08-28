"""The evidence report — WP-5.

Prism holds 38 tables and `/reports` exported two. `audit_log`,
`failed_logins`, `sop_log` and `server_security_status` — who did what, and
was the estate defended — had no export path at all. This is that path, in
three formats.

THE TWO THINGS THIS FILE EXISTS TO PROTECT

**1. "Unknown" must never be printed as "no."**

`security_checker.py` has no way to express "could not read". Its PowerShell
failure path sets `enabled = $false` (line 44) and the Python that stores it
does `1 if defender.get("enabled") else 0` (line 155) — so a failed WinRM
call, a missing key and a genuinely disabled Defender all become `0`.

The live estate shows what that produces: all 29 servers carry the identical
tuple `defender_enabled=0, sig_age_days=999, engine_version='0.0.0.0',
bitlocker_encrypted_pct=-1, bitlocker_status='Unknown'` while
`firewall_service_running=1`. Twenty-nine machines do not independently reach
a 999-day-old signature.

Read naively, this report would have stated **"Defender disabled on all 29
servers"**. That is not a milder version of the truth, it is a different
conclusion — one sends someone to an incident bridge, the other sends someone
to fix a collector. Every test below that touches posture is protecting that
distinction.

**2. The report must disclose what it could not see.**

The audit chain in this estate is forked at row 1672 and, because `audit_log`
is append-only by database trigger, permanently so. A report printing "chain
verified" would be false for ever. §0 prints the real result, whatever it is.

WHAT THIS FILE IS BLIND TO: whether the findings MATTER. It checks that the
document says true things in a readable shape. Whether a profile failure on
fifteen servers is urgent is a judgement no test makes.
"""

from __future__ import annotations

import csv
import io
import sqlite3

import pytest

import evidence
import reports_evidence


@pytest.fixture()
def conn():
    """A database with the evidence tables, and nothing in them by default."""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp TEXT,
            username TEXT, action TEXT, category TEXT, details TEXT,
            source_ip TEXT, prev_hash TEXT, row_hash TEXT);
        CREATE TABLE failed_logins (id INTEGER PRIMARY KEY, server_name TEXT,
            timestamp TEXT, source_ip TEXT, account_name TEXT, event_id INTEGER,
            source_port TEXT, domain TEXT, logon_type TEXT, workstation TEXT,
            status_code TEXT, sub_status TEXT, process_name TEXT);
        CREATE TABLE sop_log (id INTEGER PRIMARY KEY, sop_id TEXT,
            executed_at TEXT, executed_by TEXT, result TEXT, notes TEXT,
            evidence_ref TEXT);
        CREATE TABLE server_security_status (server_name TEXT PRIMARY KEY,
            last_checked TEXT, defender_enabled INTEGER,
            defender_rt_protection INTEGER, defender_sig_age_days INTEGER,
            defender_engine_version TEXT, firewall_service_running INTEGER,
            firewall_domain_enabled INTEGER, firewall_private_enabled INTEGER,
            firewall_public_enabled INTEGER, bitlocker_encrypted_pct INTEGER,
            bitlocker_status TEXT, open_ports_json TEXT, local_users_json TEXT,
            raw_data TEXT);
        CREATE TABLE logs (id INTEGER PRIMARY KEY, server_name TEXT,
            timestamp TEXT, log_source TEXT, level TEXT, event_id INTEGER,
            message TEXT);
        CREATE TABLE log_signatures (server_name TEXT, log_source TEXT,
            level TEXT, event_id INTEGER, msg_hash TEXT, hour_utc TEXT,
            count INTEGER, first_seen TEXT, last_seen TEXT, sample TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, server_name TEXT,
            timestamp TEXT, event_type TEXT, message TEXT);
        CREATE TABLE incidents (id INTEGER PRIMARY KEY, created_at TEXT,
            status TEXT, severity TEXT, title TEXT);
    """)
    return c


def _ago(minutes=0, hours=0):
    """A timestamp relative to now, in the stored shape.

    Absolute dates in seeded test data are time-bombs: they pass on the day
    they are written and expire silently once the window moves past them."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")



def _posture(c, name, **kw):
    row = dict(defender_enabled=1, defender_rt_protection=1,
               defender_sig_age_days=2, defender_engine_version="1.1.24010.1",
               firewall_service_running=1, firewall_domain_enabled=1,
               firewall_private_enabled=1, firewall_public_enabled=1,
               bitlocker_encrypted_pct=100, bitlocker_status="FullyEncrypted")
    row.update(kw)
    c.execute("""INSERT INTO server_security_status
        (server_name, last_checked, defender_enabled, defender_rt_protection,
         defender_sig_age_days, defender_engine_version, firewall_service_running,
         firewall_domain_enabled, firewall_private_enabled, firewall_public_enabled,
         bitlocker_encrypted_pct, bitlocker_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, _ago(minutes=1), row["defender_enabled"],
         row["defender_rt_protection"], row["defender_sig_age_days"],
         row["defender_engine_version"], row["firewall_service_running"],
         row["firewall_domain_enabled"], row["firewall_private_enabled"],
         row["firewall_public_enabled"], row["bitlocker_encrypted_pct"],
         row["bitlocker_status"]))


# ── unknown is not no ────────────────────────────────────────────────────

def test_an_unread_defender_is_reported_as_unknown_not_disabled(conn):
    """The sentinel tuple every server in the live estate carries."""
    _posture(conn, "SRV1", defender_enabled=0, defender_rt_protection=0,
             defender_sig_age_days=999, defender_engine_version="0.0.0.0")
    s = evidence.security_posture(conn)["servers"][0]
    assert s["defender_enabled"] is None, (
        "a failed Defender read was reported as a value")
    assert "Defender" in s["unmeasured"]
    assert not any("disabled" in c for c in s["concerns"]), (
        f"an unread Defender produced a security concern: {s['concerns']}")


def test_a_genuinely_disabled_defender_is_still_reported(conn):
    """The distinction has to cut both ways, or it is just suppression."""
    _posture(conn, "SRV1", defender_enabled=0, defender_sig_age_days=3,
             defender_engine_version="1.1.24010.1")
    s = evidence.security_posture(conn)["servers"][0]
    assert s["defender_enabled"] == 0
    assert "Defender" not in s["unmeasured"]
    assert any("disabled" in c for c in s["concerns"]), (
        "a real disabled Defender was not reported")


def test_an_unread_bitlocker_is_reported_as_unknown(conn):
    _posture(conn, "SRV1", bitlocker_encrypted_pct=-1, bitlocker_status="Unknown")
    s = evidence.security_posture(conn)["servers"][0]
    assert s["bitlocker_encrypted_pct"] is None
    assert "BitLocker" in s["unmeasured"]


def test_the_report_warns_when_a_field_was_unreadable_fleet_wide(conn):
    for i in range(5):
        _posture(conn, f"SRV{i}", defender_enabled=0, defender_sig_age_days=999,
                 defender_engine_version="0.0.0.0")
    sp = evidence.security_posture(conn)
    assert sp["with_unmeasured"] == 5
    assert sp["collection_warning"], "no warning for a fleet-wide unreadable field"
    assert "not as disabled" in sp["collection_warning"].lower()


def test_a_real_firewall_problem_is_still_a_concern(conn):
    """Firewall state IS read reliably in this estate — the sentinel problem
    is Defender and BitLocker only — so a firewall finding must survive."""
    _posture(conn, "SRV1", firewall_public_enabled=0)
    s = evidence.security_posture(conn)["servers"][0]
    assert any("public" in c for c in s["concerns"])


def test_the_spreadsheet_says_not_read_rather_than_zero(conn):
    """A `0` in a spreadsheet cell reads as "off". The distinction the query
    layer preserves must survive into the file someone opens."""
    _posture(conn, "SRV1", defender_enabled=0, defender_sig_age_days=999,
             defender_engine_version="0.0.0.0")
    doc = _doc(conn)
    rows = list(csv.reader(io.StringIO(reports_evidence.generate_evidence_csv(doc))))
    i = next(i for i, r in enumerate(rows)
             if r and r[0].lstrip("'").startswith("== Security posture"))
    header, data = rows[i + 1], rows[i + 2]
    assert data[header.index("DefenderEnabled")] == "not read"
    assert data[header.index("BitLockerPct")] != "0"


# ── the report discloses its own limits ──────────────────────────────────

def test_the_report_states_what_it_could_not_see(conn):
    prov = evidence.provenance(conn, None, hours=24)
    assert "disclosure" in prov
    assert "audit_chain" in prov["disclosure"]


def test_a_broken_audit_chain_is_reported_as_broken(conn):
    class _DB:
        def verify_audit_chain(self, limit=None):
            return {"ok": False, "checked": 1700, "first_break_id": 1672,
                    "first_break_reason": "prev_hash mismatch"}
    d = evidence.provenance(conn, _DB(), hours=24)["disclosure"]["audit_chain"]
    assert d["state"] == "broken"
    assert d["first_break_id"] == 1672
    assert "cannot be repaired" in d["note"]


def test_an_intact_chain_is_not_described_as_broken(conn):
    class _DB:
        def verify_audit_chain(self, limit=None):
            return {"ok": True, "checked": 1700, "first_break_id": None,
                    "first_break_reason": None}
    d = evidence.provenance(conn, _DB(), hours=24)["disclosure"]["audit_chain"]
    assert d["state"] == "intact"


def test_posture_says_it_has_no_history(conn):
    """`upsert_security_status` is INSERT OR REPLACE: one row per server, no
    history anywhere. The section can say "as of", never "since"."""
    _posture(conn, "SRV1")
    limitation = evidence.security_posture(conn)["limitation"].lower()
    # Both halves of the claim. Checking only for "history" passed when the
    # opening sentence was replaced, because the word survived further down —
    # a substring that outlives the sentence it belonged to.
    assert "current state only" in limitation, (
        f"the section does not say it is a snapshot: {limitation[:90]}")
    assert "no history" in limitation, (
        f"the section does not say history is unavailable: {limitation[:90]}")


def test_an_empty_firewall_section_says_which_kind_of_empty(conn):
    """Zero rows means "not collected yet" here, not "nothing happened", and
    the difference is the whole value of the section."""
    f = evidence.firewall_changes(conn, hours=24)
    assert f["total_ever"] == 0
    assert f["note"] and "not yet collected" in f["note"]


def test_a_firewall_section_with_events_carries_no_excuse(conn):
    conn.execute("INSERT INTO logs (server_name, timestamp, log_source, level, "
                 "event_id, message) VALUES ('SRV1',?,"
                 "'Firewall','Information',2004,'rule added')", (_ago(minutes=2),))
    f = evidence.firewall_changes(conn, hours=24)
    assert f["total_ever"] == 1
    assert f["note"] is None
    assert f["events"][0]["meaning"] == "a rule was added"


# ── the failure reasons a reader needs named ─────────────────────────────

def test_the_common_failure_reasons_are_named_not_left_as_hex(conn):
    """`0xc000006e` was the single most common reason in this estate and the
    report's largest row read "unrecognised"."""
    for code in ("0xc000006e", "0xc0000064", "0xc000006a", "0xc0000022"):
        assert evidence.SUB_STATUS.get(code), f"{code} has no meaning"
    for i, code in enumerate(("0xc000006e", "0xc0000064")):
        conn.execute("INSERT INTO failed_logins (server_name, timestamp, source_ip,"
                     " account_name, event_id, sub_status) VALUES "
                     "('SRV1',?,'10.0.0.1','u',4625,?)", (_ago(minutes=2), code))
    reasons = evidence.authentication(conn, hours=24)["by_reason"]
    assert all(r["meaning"] != "unrecognised" for r in reasons), reasons


# ── the document, as a document ──────────────────────────────────────────

def _doc(conn):
    return evidence.build(conn, db=None, hours=24)


def test_every_section_is_present(conn):
    doc = _doc(conn)
    assert set(doc) == {"provenance", "verdict", "security_posture",
                        "authentication", "firewall", "audit"}


def test_the_pdf_is_a4_and_fits(conn):
    pytest.importorskip("pypdf")
    from pypdf import PdfReader
    _posture(conn, "SRV1")
    pdf = reports_evidence.generate_evidence_pdf(_doc(conn))
    reader = PdfReader(io.BytesIO(pdf))
    assert reader.pages
    for page in reader.pages:
        assert abs(float(page.mediabox.width) - 595.276) < 2, "not A4"


def test_no_evidence_table_is_wider_than_the_frame():
    """`_table` asserts this at build time; the constant is pinned here so it
    cannot be quietly raised to make a wide table fit."""
    assert reports_evidence.FRAME_WIDTH <= 482, (
        "the frame constant exceeds A4 minus its margins")


def test_stored_text_is_escaped_before_it_reaches_a_paragraph(conn):
    """A Paragraph parses its input as markup, so a Windows message with `<`
    or `&` would produce malformed XML and take the document down."""
    st = reports_evidence._styles()
    para = reports_evidence._p('a <script> & "quotes"', st["cell"])
    assert "&lt;" in para.text and "&amp;" in para.text


def test_the_pdf_survives_markup_in_stored_text(conn):
    """Exercised end to end, not asserted about."""
    pytest.importorskip("pypdf")
    conn.execute("INSERT INTO audit_log (timestamp, username, action, category,"
                 " details) VALUES (?,'a<b>','x & y',"
                 "'system','<injected>&amp;')", (_ago(minutes=1),))
    pdf = reports_evidence.generate_evidence_pdf(_doc(conn))
    assert pdf[:5] == b"%PDF-"


def _is_banner(cell: str) -> bool:
    """A section banner, with or without the formula guard's prefix.

    The banner is literally `== Name ==`, so it STARTS WITH `=` — which means
    a spreadsheet treats it as a formula and shows an error where the heading
    should be. It did exactly that for as long as this file has existed. The
    CSV guard neutralises it like any other formula-leading cell, which fixes
    the heading as a side effect of a change made for a different reason.

    So the prefix is expected here, and stripping it is what lets these tests
    assert about the heading rather than about the escaping."""
    return cell.lstrip("'").startswith("== ")


def test_the_csv_carries_every_section(conn):
    _posture(conn, "SRV1")
    rows = list(csv.reader(io.StringIO(
        reports_evidence.generate_evidence_csv(_doc(conn)))))
    banners = {r[0].lstrip("'") for r in rows if r and _is_banner(r[0])}
    for expected in ("Verdict", "Security posture", "Failed logons by source",
                     "Firewall policy events", "Audit trail"):
        assert any(expected in b for b in banners), f"{expected} missing: {banners}"
