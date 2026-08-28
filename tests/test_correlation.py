"""Attack shape vs fleet-wide fault — WP-5.

The owner asked to be able to see *"any indications that this is an atack or
this is a corelated problem on all servers"*. Those are two objects with one
symptom, and the discriminator is SHAPE:

    attack       one SOURCE -> many targets
    fleet fault  one CAUSE  -> many targets, no common source

Both detectors were calibrated against the live estate before their thresholds
were fixed, and the calibration is the interesting part:

  * The busiest failed-login source is **733 attempts against 2 servers over
    eleven days**. Volume alone flags it; it is almost certainly a service
    account holding an old password. `MIN_ATTACK_SERVERS = 3` is what makes
    the answer right, and this file pins that it stays right.

  * `System/3` at Error level sits on **29 of 29 servers permanently**.
    Breadth alone reports a fleet-wide fault on every run for ever. The spike
    test against each signature's own history is what turns "everyone has
    this" into "everyone has this right now, more than usual".

TWO DEFECTS FOUND BY RUNNING IT, NOT BY READING IT — both pinned below:

  1. **False explanations.** `compliance` was in the explaining categories.
     Recording that an SOP was executed is a note that a human did something,
     not an action on 29 servers — and SOP records are frequent, so nearly
     every finding got "explained". The measured cost: `Application/1511` at
     10.6x normal across 15 of 29 servers, the most significant event in the
     estate's history, was dismissed because someone filed an SOP record in
     the same hour.

  2. **Explanations erased verdicts.** Overwriting `kind` with
     `explained-change` hid what happened behind why. A burst a scheduled
     restart accounts for is still a burst; it should rank lower, not vanish.

WHAT THIS IS BLIND TO: whether a finding MATTERS. It reports shape and
magnitude against history. Whether a profile-service failure on fifteen
servers is urgent is a judgement no threshold can make.
"""

from __future__ import annotations

import sqlite3

import pytest

import correlation


@pytest.fixture()
def db():
    """A throwaway database with the two tables the detectors read."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE log_signatures (
            server_name TEXT, log_source TEXT, level TEXT, event_id INTEGER,
            msg_hash TEXT, hour_utc TEXT, count INTEGER,
            first_seen TEXT, last_seen TEXT, sample TEXT);
        CREATE TABLE failed_logins (
            id INTEGER PRIMARY KEY, server_name TEXT, timestamp TEXT,
            source_ip TEXT, account_name TEXT, event_id INTEGER,
            source_port TEXT, domain TEXT, logon_type TEXT, workstation TEXT,
            status_code TEXT, sub_status TEXT, process_name TEXT);
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, timestamp TEXT, username TEXT, action TEXT,
            category TEXT, details TEXT);
    """)
    return conn


def _ago(minutes=0, hours=0):
    """A timestamp relative to now, in the stored shape.

    Absolute dates in seeded test data are time-bombs: they pass on the day
    they are written and expire silently once the window moves past them."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hour_ago(hours=0):
    """An `hour_utc` bucket relative to now."""
    return _ago(hours=hours)[:13]


def _sig(conn, *, servers, hour, count, msg_hash="h1", source="Application",
         level="Error", event_id=1511, sample="profile not found"):
    for i in range(servers):
        conn.execute(
            "INSERT INTO log_signatures VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"SRV{i:02d}", source, level, event_id, msg_hash, hour,
             count, "", "", sample))


def _baseline(conn, *, hours, servers, count, msg_hash="h1", **kw):
    """Quiet history, so a spike has something to be unusual against."""
    for h in range(hours):
        _sig(conn, servers=servers, hour=_hour_ago(24 * (h + 2)),
             count=count, msg_hash=msg_hash, **kw)


# ── one cause, many targets ──────────────────────────────────────────────

def test_a_signature_on_most_of_the_fleet_that_spiked_is_a_fleet_fault(db):
    _baseline(db, hours=10, servers=2, count=5)
    _sig(db, servers=20, hour=_hour_ago(0), count=500)
    found = correlation.fleet_faults(db, hours=24 * 365, fleet_size=29)
    assert found, "a 20-of-29 spike was not reported"
    assert found[0]["kind"] == "fleet-fault"
    assert found[0]["servers"] == 20


def test_permanent_fleet_wide_chatter_is_not_a_fault(db):
    """`System/3` is on 29 of 29 servers every hour. Breadth alone would
    report it for ever; only a spike against its own history is news."""
    for h in range(40):
        _sig(db, servers=29, hour=_hour_ago(h + 1),
             count=100, msg_hash="chatter", source="System", event_id=3)
    found = correlation.fleet_faults(db, hours=24 * 365, fleet_size=29)
    assert not [f for f in found if f["signature"] == "chatter"], (
        "constant fleet-wide chatter was reported as a fault")


def test_a_signature_with_no_history_is_not_called_unusual(db):
    """A few observations cannot establish a normal.

    The first version of this test used ONE observation, which the SPIKE test
    rejects on its own (with a single sample `avg == n`, so `n > avg * 3` is
    false). It therefore passed with the baseline gate removed — it named one
    gate and exercised another.

    Four hours, three quiet and one enormous, clears the spike test on
    arithmetic and leaves only the baseline gate standing: `hours_seen = 4` is
    under `MIN_BASELINE_HOURS = 5`."""
    for h in range(3):
        _sig(db, servers=2, hour=_hour_ago(24 * (h + 1)), count=1)
    _sig(db, servers=20, hour=_hour_ago(0), count=100)

    assert correlation.MIN_BASELINE_HOURS > 4, (
        "this test is calibrated against a baseline requirement of 5 hours")
    assert not correlation.fleet_faults(db, hours=24 * 365, fleet_size=29), (
        "a signature with four hours of history was judged against a normal "
        "it does not have")


def test_a_narrow_burst_is_a_cluster_not_a_fleet_fault(db):
    _baseline(db, hours=10, servers=1, count=5)
    _sig(db, servers=4, hour=_hour_ago(0), count=400)
    found = correlation.fleet_faults(db, hours=24 * 365, fleet_size=29)
    assert found and found[0]["kind"] == "cluster-fault"


def test_a_two_server_coincidence_is_not_reported_at_all(db):
    """What the cluster floor is for, and what the test above did not check:
    at four servers the floor is irrelevant, so dropping it to 1 changed
    nothing that test looked at.

    Two machines sharing a signature is a coincidence. Three is where it
    starts to be a pattern."""
    _baseline(db, hours=10, servers=1, count=5, msg_hash="pair")
    _sig(db, servers=2, hour=_hour_ago(0), count=400, msg_hash="pair")
    found = correlation.fleet_faults(db, hours=24 * 365, fleet_size=29)
    assert not [f for f in found if f["signature"] == "pair"], (
        "two servers sharing a signature was reported as a pattern")


def test_errors_are_ranked_above_information(db):
    """`System/7036` (a service changed state) is Information, is on the
    allowlist because service tracking wants it, and outnumbers everything.
    Ranked by breadth alone it buries the Errors underneath it."""
    _baseline(db, hours=10, servers=2, count=5, msg_hash="err")
    _baseline(db, hours=10, servers=2, count=5, msg_hash="info",
              source="System", level="Information", event_id=7036)
    _sig(db, servers=15, hour=_hour_ago(0), count=500, msg_hash="err")
    _sig(db, servers=25, hour=_hour_ago(0), count=500, msg_hash="info",
         source="System", level="Information", event_id=7036)
    found = correlation.fleet_faults(db, hours=24 * 365, fleet_size=29)
    assert found[0]["level"] == "Error", (
        f"Information ranked above Error: {[(f['level'], f['servers']) for f in found]}")


# ── one source, many targets ─────────────────────────────────────────────

def _login(conn, ip, server, account, when, sub_status="0xc000006a"):
    conn.execute(
        "INSERT INTO failed_logins (server_name, timestamp, source_ip, "
        "account_name, event_id, logon_type, workstation, sub_status) "
        "VALUES (?,?,?,?,4625,'3','WS',?)",
        (server, when, ip, account, sub_status))


def test_one_source_against_many_servers_is_an_attack(db):
    for i in range(4):
        for n in range(4):
            _login(db, "10.0.0.9", f"SRV{i}", "admin", _ago(minutes=n))
    found = [a for a in correlation.auth_attacks(db, hours=24)
             if a["source_ip"] == "10.0.0.9"]
    assert found and found[0]["kind"] == "targeted-attack"
    assert "lateral" in found[0]["shapes"]


def test_many_attempts_on_one_account_from_one_source_is_not_an_attack(db):
    """The estate's loudest source is exactly this shape: hundreds of
    failures, ONE account, two servers, over days. That is a service account
    with a stale password. Calling it an attack would train the reader to
    ignore the report."""
    for n in range(40):
        _login(db, "10.0.0.5", "SRV1" if n % 2 else "SRV2", "svc_backup",
               _ago(minutes=n))
    found = [a for a in correlation.auth_attacks(db, hours=24)
             if a["source_ip"] == "10.0.0.5"]
    assert found and found[0]["kind"] == "stale-credential", (
        f"got {found[0]['kind'] if found else 'nothing'}")


def test_one_source_trying_many_accounts_is_a_spray(db):
    for i in range(6):
        for n in range(2):
            _login(db, "10.0.0.7", "SRV1", f"user{i}", _ago(minutes=i * 10 + n))
    found = [a for a in correlation.auth_attacks(db, hours=24)
             if a["source_ip"] == "10.0.0.7"]
    assert found and "spray" in found[0]["shapes"]


def test_repeated_unknown_usernames_are_enumeration(db):
    """"Wrong password" has innocent explanations. "That user does not
    exist", repeatedly, from one host, has approximately none."""
    for i in range(6):
        _login(db, "10.0.0.8", "SRV1", f"ghost{i}", _ago(minutes=i),
               sub_status=correlation.STATUS_NO_SUCH_USER)
    found = [a for a in correlation.auth_attacks(db, hours=24)
             if a["source_ip"] == "10.0.0.8"]
    assert found and "enumeration" in found[0]["shapes"]


def test_local_and_unknown_sources_are_ignored(db):
    for src in ("-", "127.0.0.1", "::1"):
        for n in range(20):
            _login(db, src, "SRV1", "admin", _ago(minutes=n))
    assert not correlation.auth_attacks(db, hours=24), (
        "a source carrying no attacker information was reported")


def test_an_attack_verdict_names_its_targets(db):
    """A verdict without its evidence rows is an opinion, and an opinion in a
    security report is worse than silence."""
    for i in range(4):
        for n in range(4):
            _login(db, "10.0.0.9", f"SRV{i}", "admin", _ago(minutes=n))
    result = correlation.analyse(db, hours=24)
    assert result["attacks"], "no attack found to check"
    targets = result["attacks"][0]["targets"]
    assert targets and all(t["server"] and t["attempts"] for t in targets)


# ── was it us? ───────────────────────────────────────────────────────────

def test_a_burst_prism_caused_is_labelled_with_what_caused_it(db):
    _baseline(db, hours=10, servers=2, count=5)
    _sig(db, servers=20, hour=_hour_ago(0), count=500)
    db.execute("INSERT INTO audit_log (timestamp, username, action, category, details) "
               "VALUES (?,'svc_patch','restart_executed',"
               "'restart_schedule','monthly patch window')", (_ago(minutes=1),))
    found = correlation.analyse(db, hours=24 * 365)["fleet_faults"]
    assert found[0]["explained_by"], "a scheduled restart did not explain the burst"


def test_an_explanation_does_not_erase_the_finding(db):
    """Overwriting the verdict with `explained-change` hid what happened
    behind why it happened."""
    _baseline(db, hours=10, servers=2, count=5)
    _sig(db, servers=20, hour=_hour_ago(0), count=500)
    db.execute("INSERT INTO audit_log (timestamp, username, action, category, details) "
               "VALUES (?,'svc','restart_executed','restart_schedule','x')", (_ago(minutes=1),))
    found = correlation.analyse(db, hours=24 * 365)["fleet_faults"]
    assert found[0]["kind"] == "fleet-fault", (
        f"the verdict was replaced by its explanation: {found[0]['kind']}")
    assert found[0]["explained_by"]


def test_an_sop_record_does_not_explain_anything_on_a_server(db):
    """The defect that suppressed the estate's most significant event.
    Recording an SOP execution is a note that a human did something; it does
    not touch 29 Windows hosts."""
    _baseline(db, hours=10, servers=2, count=5)
    _sig(db, servers=20, hour=_hour_ago(0), count=500)
    db.execute("INSERT INTO audit_log (timestamp, username, action, category, details) "
               "VALUES (?,'alice','sop_execution_recorded',"
               "'compliance','SOP-004')", (_ago(minutes=1),))
    found = correlation.analyse(db, hours=24 * 365)["fleet_faults"]
    assert not found[0]["explained_by"], (
        "an SOP record was accepted as the cause of a fleet-wide log burst")


def test_only_categories_that_touch_a_host_can_explain(db):
    forbidden = {"compliance", "security", "settings"}
    assert not forbidden & set(correlation._EXPLAINING_CATEGORIES), (
        "a category that cannot reach a Windows host is allowed to explain "
        "activity on one")


# ── the sentence a human reads ───────────────────────────────────────────

def test_the_summary_can_say_that_nothing_happened(db):
    """A report that always finds something is a report nobody believes."""
    assert "threshold" in correlation.analyse(db, hours=24)["summary"].lower()


def test_the_summary_leads_with_an_attack_when_there_is_one(db):
    for i in range(4):
        for n in range(4):
            _login(db, "10.0.0.9", f"SRV{i}", "admin", _ago(minutes=n))
    assert "attack" in correlation.analyse(db, hours=24)["summary"].lower()


def test_no_seeded_timestamp_is_a_fixed_date():
    """Seeded data must move with the clock.

    Both this file and the evidence tests originally wrote rows at
    `2026-08-26T10:00:00Z`, because that was the day they were written. The
    code under test computes its window as `now - hours`, so two days later
    six tests failed for a reason that had nothing to do with the behaviour
    they guard.

    That failure mode is worse than a plain bug: the suite went red on its
    own, on a day nobody had touched it, which is exactly how a team learns
    to ignore red.

    Docstrings are stripped before the scan — this one names the offending
    date, and a guard that reads its own explanation as the defect is a
    mistake this project has made repeatedly."""
    import re
    from pathlib import Path

    here = Path(__file__).resolve().parent
    offenders = []
    for name in ("test_correlation.py", "test_evidence_report.py"):
        text = (here / name).read_text(encoding="utf-8")
        code = re.sub(r'"""(?:.|\n)*?"""', '""', text)
        for lineno, line in enumerate(code.splitlines(), 1):
            if re.search(r"\d{4}-\d{2}-\d{2}T\d{2}", line):
                offenders.append(f"{name}:{lineno}: {line.strip()[:70]}")

    assert not offenders, (
        "test data seeded at a fixed timestamp; use a clock-relative helper "
        "so the window keeps reaching it:\n  " + "\n  ".join(offenders[:8]))
