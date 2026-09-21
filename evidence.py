"""The evidence report's query layer — WP-5.

Prism's database has 38 tables and `/reports` exported two of them. The three
groups that answer *"who did what, and was the estate defended"* — `audit_log`,
`failed_logins`, `sop_log`, and `server_security_status` — had no export path
at all. This module is that path.

EVERY SECTION IS A QUESTION, NOT A TABLE. That is the whole design. The
Reports page's diagnosed disease (`docs/plans/WP5_REPORTS_RESEARCH.md` §2) is
that it is organised around what the code produces rather than what the
operator came for; an evidence report organised by table would repeat it.

TWO RULES THAT LOOK LIKE PEDANTRY AND ARE NOT
---------------------------------------------
**Explicit columns, never `SELECT *`.** Migrated columns append in *migration*
order on an existing database and in *CREATE TABLE* order on a fresh one.
`failed_logins` is already skewed this way in production. A positional read
works in development and silently mislabels every column in the field — in a
document whose entire purpose is to be believed.

**An evidence document must state what it could not see.** §0 carries the
ingest counters, the Information-drop count and the audit-chain result,
including when that result is bad. A report that only shows what it found is
an advertisement, not evidence.

WHAT THIS MODULE REFUSES TO USE, and why
----------------------------------------
* `auth_failures` — a lockout counter, not a record. `clear_failures_for`
  deletes every row for a user on successful login, so it cannot answer "did
  this account fail earlier today". Prism's own failed logins come from
  `audit_log` category `auth`.
* `get_failed_login_heatmap` — its SQL contains `strftime('%%w', ...)` in a
  non-formatted string, so SQLite receives a literal `%%w` and every row
  buckets to day 0, hour 0. It has no production caller; this module does not
  become its first.
* `restart_log` — zero writers outside tests, zero rows. Anything reading it
  for evidence finds nothing and says so silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: Tables this report draws on. Named explicitly so §0 can state the row count
#: and time span of each one it cites — a reader cannot judge a finding
#: without knowing how much was looked at.
CITED_TABLES = (
    "audit_log", "failed_logins", "sop_log", "server_security_status",
    "logs", "log_signatures", "events", "incidents",
)


def _window(hours: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), now.strftime(fmt)


# ── §0 provenance: what this document is, and what it could not see ──────

def provenance(conn, db, hours: int) -> dict:
    """Where the numbers came from, and every reason they might be incomplete.

    The disclosure half is not decoration. `ingest_caps` drops rows when a
    payload is oversized, `insert_logs` drops Information-level events that
    are not allowlisted, and the audit chain can be broken. A report that
    hides any of those is asserting completeness it does not have.
    """
    start, end = _window(hours)

    tables = {}
    for name in CITED_TABLES:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()
            count = row[0] if row else 0
            span = (None, None)
            cols = {c[1] for c in conn.execute(f"PRAGMA table_info([{name}])")}
            tcol = "timestamp" if "timestamp" in cols else (
                "last_checked" if "last_checked" in cols else (
                    "executed_at" if "executed_at" in cols else (
                        "created_at" if "created_at" in cols else (
                            "hour_utc" if "hour_utc" in cols else None))))
            if tcol:
                span = conn.execute(
                    f"SELECT MIN([{tcol}]), MAX([{tcol}]) FROM [{name}]").fetchone()
            tables[name] = {"rows": count, "first": span[0], "last": span[1],
                            "time_column": tcol}
        except Exception:
            logger.exception("provenance: could not read %s", name)
            tables[name] = {"rows": None, "first": None, "last": None,
                            "time_column": None}

    return {
        "window_start": start,
        "window_end": end,
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tables": tables,
        "disclosure": _disclosure(conn, db),
    }


def _disclosure(conn, db) -> dict:
    """What was dropped, truncated or cannot be trusted.

    The audit-chain result is reported as it is. This estate's chain is
    forked at row 1672 — two rows written in the same instant by the watchdog
    both claimed the same predecessor — and because `audit_log` is append-only
    by database trigger, a fork is permanent. Printing "verified" would be
    false for ever, which is the one thing an audit report must never be.
    """
    out = {}

    try:
        import ingest_caps
        out["ingest"] = ingest_caps.snapshot()
    except Exception:
        out["ingest"] = None

    for attr in ("logs_dropped_information", "logs_kept_by_allowlist",
                 "_audit_mirror_failures", "_audit_insert_failures"):
        out[attr.lstrip("_")] = getattr(db, attr, None) if db is not None else None

    try:
        chain = db.verify_audit_chain() if db is not None else None
    except Exception:
        logger.exception("disclosure: audit chain verification failed")
        chain = None

    if chain is None:
        out["audit_chain"] = {"state": "unknown",
                              "note": "the chain could not be verified"}
    elif chain.get("ok"):
        out["audit_chain"] = {
            "state": "intact",
            "rows_checked": chain.get("checked"),
            "note": f"{chain.get('checked')} rows verified",
        }
    else:
        out["audit_chain"] = {
            "state": "broken",
            "rows_checked": chain.get("checked"),
            "first_break_id": chain.get("first_break_id"),
            "reason": chain.get("first_break_reason"),
            "note": (
                f"The hash chain does not verify from row "
                f"{chain.get('first_break_id')}: {chain.get('first_break_reason')}. "
                "Rows before that point are unaffected. `audit_log` is "
                "append-only by database trigger, so a break cannot be "
                "repaired in place — it is recorded here rather than hidden."
            ),
        }
    return out


# ── §2 what defends the estate, as of now ────────────────────────────────

def security_posture(conn) -> dict:
    """Defender, firewall and BitLocker per server.

    "As of", never "since": `upsert_security_status` is INSERT OR REPLACE, so
    exactly one row per server exists and there is no history anywhere. The
    report cannot answer "was BitLocker on last quarter" and says so rather
    than implying it can.
    """
    rows = conn.execute("""
        SELECT server_name, last_checked,
               defender_enabled, defender_rt_protection, defender_sig_age_days,
               defender_engine_version,
               firewall_service_running, firewall_domain_enabled,
               firewall_private_enabled, firewall_public_enabled,
               bitlocker_encrypted_pct, bitlocker_status
        FROM server_security_status
        ORDER BY server_name
    """).fetchall()

    servers = []
    for r in rows:
        (name, checked, dfn, rt, sig_age, engine,
         fw_svc, fw_dom, fw_priv, fw_pub, bl_pct, bl_status) = r

        defender_read = _defender_was_read(dfn, sig_age, engine)
        bitlocker_read = _bitlocker_was_read(bl_pct, bl_status)

        concerns, unmeasured = [], []

        if not defender_read:
            unmeasured.append("Defender")
        else:
            if not dfn:
                concerns.append("Defender disabled")
            if rt is not None and not rt:
                concerns.append("real-time protection off")
            if sig_age is not None and 0 <= sig_age > 7:
                concerns.append(f"signatures {sig_age} days old")

        if fw_svc is not None and not fw_svc:
            concerns.append("firewall service not running")
        for label, val in (("domain", fw_dom), ("private", fw_priv), ("public", fw_pub)):
            if val is not None and not val:
                concerns.append(f"firewall off ({label})")

        if not bitlocker_read:
            unmeasured.append("BitLocker")

        servers.append({
            "server": name, "last_checked": checked,
            "defender_enabled": dfn if defender_read else None,
            "defender_rt_protection": rt if defender_read else None,
            "defender_sig_age_days": sig_age if defender_read else None,
            "defender_engine_version": engine if defender_read else None,
            "firewall_service_running": fw_svc,
            "firewall_domain_enabled": fw_dom,
            "firewall_private_enabled": fw_priv,
            "firewall_public_enabled": fw_pub,
            "bitlocker_encrypted_pct": bl_pct if bitlocker_read else None,
            "bitlocker_status": bl_status if bitlocker_read else None,
            "concerns": concerns,
            "unmeasured": unmeasured,
        })

    checked_times = [s["last_checked"] for s in servers if s["last_checked"]]
    unmeasured_all = sorted({u for s in servers for u in s["unmeasured"]})
    return {
        "servers": servers,
        "total": len(servers),
        "with_concerns": sum(1 for s in servers if s["concerns"]),
        "with_unmeasured": sum(1 for s in servers if s["unmeasured"]),
        "unmeasured_fields": unmeasured_all,
        "as_of": max(checked_times) if checked_times else None,
        "limitation": (
            "Current state only. Security status is stored one row per server "
            "and overwritten on each check, so no history exists and this "
            "section cannot answer what the posture was at any earlier date."
        ),
        "collection_warning": (
            None if not unmeasured_all else
            f"{', '.join(unmeasured_all)} could not be read on "
            f"{sum(1 for s in servers if s['unmeasured'])} of {len(servers)} "
            "servers. Those fields are reported as unknown, NOT as disabled: "
            "the collector stores a failed read and a genuine negative as the "
            "same value, so treating them alike would report a fleet-wide "
            "security emergency that is actually a collection fault."
        ),
    }


#: The values `security_checker.py` writes when a read fails. Its PowerShell
#: failure path sets `enabled = $false` and `sig_age_days = -1`, its signature
#: branch falls back to 999, and the Python coerces a missing key to 0 — so
#: "could not determine" and "switched off" are indistinguishable in the
#: column alone. These sentinels are the only way to tell them apart.
_SIG_AGE_UNREAD = 999
_ENGINE_UNREAD = "0.0.0.0"
_BITLOCKER_UNREAD_PCT = -1
_BITLOCKER_UNREAD_STATUS = "Unknown"


def _defender_was_read(enabled, sig_age, engine) -> bool:
    """False when the Defender block never arrived.

    A real Defender install reports an engine version and a signature age. A
    machine claiming version 0.0.0.0 with 999-day-old signatures did not
    answer — and every server in this estate reports exactly that."""
    if engine in (None, "", _ENGINE_UNREAD) and sig_age in (None, _SIG_AGE_UNREAD, -1):
        return False
    return True


def _bitlocker_was_read(pct, status) -> bool:
    if pct is None or pct == _BITLOCKER_UNREAD_PCT:
        return status not in (None, "", _BITLOCKER_UNREAD_STATUS)
    return True


# ── §3 who tried to get in ───────────────────────────────────────────────

#: Windows sub-status codes, which say WHY a logon failed. The distinction
#: matters: "wrong password" has a dozen innocent explanations, "no such user"
#: has almost none.
SUB_STATUS = {
    "0xc0000064": "no such user",
    "0xc000006a": "wrong password",
    "0xc0000234": "account locked out",
    "0xc0000072": "account disabled",
    "0xc0000070": "workstation restriction",
    "0xc0000193": "account expired",
    "0xc0000071": "password expired",
    "0xc000015b": "logon type not granted",
    # Present in this estate and previously unrecognised: 0xc000006e was the
    # MOST COMMON reason, so the report's largest row read "unrecognised".
    "0xc000006e": "account restriction (logon hours, workstation or expiry)",
    "0xc0000022": "access denied",
    "0x0": "no sub-status reported",
}


def authentication(conn, hours: int) -> dict:
    """Failed logons, grouped three ways because each answers a different
    question: which source, which account, and what kind of failure."""
    start, end = _window(hours)

    by_source = [dict(zip(
        ("source_ip", "servers", "accounts", "attempts", "first_seen", "last_seen"), r))
        for r in conn.execute("""
            SELECT source_ip, COUNT(DISTINCT server_name),
                   COUNT(DISTINCT account_name), COUNT(*),
                   MIN(timestamp), MAX(timestamp)
            FROM failed_logins
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY source_ip ORDER BY COUNT(*) DESC LIMIT 50
        """, (start, end))]

    by_account = [dict(zip(
        ("account", "servers", "sources", "attempts", "first_seen", "last_seen"), r))
        for r in conn.execute("""
            SELECT account_name, COUNT(DISTINCT server_name),
                   COUNT(DISTINCT source_ip), COUNT(*),
                   MIN(timestamp), MAX(timestamp)
            FROM failed_logins
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY account_name ORDER BY COUNT(*) DESC LIMIT 50
        """, (start, end))]

    by_reason = []
    for code, n in conn.execute("""
            SELECT sub_status, COUNT(*) FROM failed_logins
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY sub_status ORDER BY COUNT(*) DESC
        """, (start, end)):
        by_reason.append({"sub_status": code,
                          "meaning": SUB_STATUS.get((code or "").lower(), "unrecognised"),
                          "attempts": n})

    total = conn.execute(
        "SELECT COUNT(*) FROM failed_logins WHERE timestamp >= ? AND timestamp <= ?",
        (start, end)).fetchone()[0]

    return {"total": total, "by_source": by_source,
            "by_account": by_account, "by_reason": by_reason}


# ── §4 what changed on the firewalls ─────────────────────────────────────

FIREWALL_POLICY_EVENTS = {
    2004: "a rule was added",
    2005: "a rule was modified",
    2006: "a rule was deleted",
    2008: "firewall settings changed",
    2009: "profile settings changed",
    2010: "active network profile changed",
    2033: "ALL rules deleted",
}


def firewall_changes(conn, hours: int) -> dict:
    """Firewall policy events, and an honest note when there are none.

    Until 2026-08-26 this section could only ever have been empty: the
    collector queried the Firewall channel, but every one of these events is
    Information-level and `insert_logs` dropped Information that was not
    allowlisted — and the allowlist held only `System/*` entries. Zero rows
    reached the table in 21 days across 29 servers.

    That is fixed, but the fix only takes effect from the next collector
    restart, so an empty section here still means "not yet collected" rather
    than "nothing happened". Saying which is the difference between evidence
    and a blank page.
    """
    start, end = _window(hours)
    rows = conn.execute("""
        SELECT server_name, timestamp, level, event_id, message
        FROM logs
        WHERE timestamp >= ? AND timestamp <= ? AND log_source = 'Firewall'
        ORDER BY timestamp DESC
        LIMIT 200
    """, (start, end)).fetchall()

    events = [{"server": r[0], "timestamp": r[1], "level": r[2],
               "event_id": r[3],
               "meaning": FIREWALL_POLICY_EVENTS.get(r[3], ""),
               "message": r[4]} for r in rows]

    ever = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE log_source = 'Firewall'").fetchone()[0]

    return {
        "events": events,
        "total_in_window": len(events),
        "total_ever": ever,
        "note": (
            None if ever else
            "No firewall events have ever reached this database. The collector "
            "queries the Windows Firewall channel, but until 2026-08-26 every "
            "such event was discarded at ingest as unallowlisted "
            "Information-level. Collection begins at the next collector "
            "restart; an empty section before then means 'not yet collected', "
            "not 'nothing happened'."
        ),
    }


# ── §5 who did what inside Prism ─────────────────────────────────────────

def audit_trail(conn, hours: int, limit: int = 500) -> dict:
    """Prism's own actions, and the procedures recorded against them."""
    start, end = _window(hours)

    entries = [dict(zip(
        ("id", "timestamp", "username", "action", "category", "details", "source_ip"), r))
        for r in conn.execute("""
            SELECT id, timestamp, username, action, category, details, source_ip
            FROM audit_log
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY id DESC LIMIT ?
        """, (start, end, limit))]

    by_category = [{"category": c, "actions": n} for c, n in conn.execute("""
        SELECT category, COUNT(*) FROM audit_log
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY category ORDER BY COUNT(*) DESC
    """, (start, end))]

    by_user = [{"username": u, "actions": n} for u, n in conn.execute("""
        SELECT username, COUNT(*) FROM audit_log
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY username ORDER BY COUNT(*) DESC LIMIT 25
    """, (start, end))]

    sop = [dict(zip(("id", "sop_id", "executed_at", "executed_by", "result", "notes"), r))
           for r in conn.execute("""
               SELECT id, sop_id, executed_at, executed_by, result, notes
               FROM sop_log
               WHERE executed_at >= ? AND executed_at <= ?
               ORDER BY executed_at DESC LIMIT 200
           """, (start, end))]

    return {"entries": entries, "by_category": by_category,
            "by_user": by_user, "sop_executions": sop,
            "total": conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE timestamp >= ? AND timestamp <= ?",
                (start, end)).fetchone()[0]}


# ── the whole document ───────────────────────────────────────────────────

def build(conn, db=None, hours: int = 168) -> dict:
    """Every section, in the order a reader needs them.

    Verdict first: an operator opening an evidence report wants to know
    whether anything happened before they want the tables that prove it.
    """
    import correlation

    return {
        "provenance": provenance(conn, db, hours),
        "verdict": correlation.analyse(conn, hours=hours),
        "security_posture": security_posture(conn),
        "authentication": authentication(conn, hours),
        "firewall": firewall_changes(conn, hours),
        "audit": audit_trail(conn, hours),
    }
