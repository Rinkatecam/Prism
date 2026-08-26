"""Is this an attack, or one fault hitting every server? — WP-5.

The owner's question, verbatim: *"any indications that this is an atack or this
is a corelated problem on all servers"*. Those are two different objects that
produce the same surface symptom — several servers unhappy at once — and
telling them apart is a question of SHAPE, not volume:

    attack       one SOURCE  -> many targets, no common cause
    fleet fault  one CAUSE   -> many targets, no common source

Everything here is an operationalisation of that distinction. Each finding
carries a verdict, the numbers behind it, and — when Prism's own audit log can
explain it — what explained it.

WHAT THIS MODULE WILL NOT DO
----------------------------
It does not assert `attack` without naming the source and the per-target
counts. A verdict without its evidence rows is an opinion, and an opinion in a
security report is worse than silence.

MEASURED AGAINST THE LIVE FLEET BEFORE THE THRESHOLDS WERE CHOSEN
-----------------------------------------------------------------
The estate's busiest failed-login source is 733 attempts against **2** servers
over eleven days. Volume alone would flag it loudly; it is almost certainly a
service account with a stale password, not an attack. The breadth threshold is
what makes the answer correct, and it is the reason `MIN_ATTACK_SERVERS` is 3
rather than 2.

In the other direction, `System/3` at Error level appears on **29 of 29
servers, every hour, permanently**. Breadth alone would report a fleet-wide
fault every time the report ran. The spike test against each signature's own
history is what turns "everyone has this" into "everyone has this *right now,
more than usual*".

WHY `log_signatures` AND NOT `logs`
-----------------------------------
`log_signatures` is pre-aggregated per (server, source, level, event_id,
msg_hash, hour) so a fleet sweep reads ~82k rows instead of ~252k, and it
carries a **`msg_hash`**. That matters more than the speed: `event_id` is
ambiguous across providers — this estate has `2005` meaning both "a firewall
rule was modified" and "Federation certificate not found", from different
products. Correlating on `event_id` alone manufactures false clusters out of
unrelated software. `msg_hash` does not.

The cost is granularity: `hour_utc` buckets are coarser than the collector's
5-minute poll. That is the safe direction — a bucket too wide cannot split a
genuinely simultaneous event in two, which is the failure that would matter.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── thresholds, each with the reason it is that number ───────────────────

#: A fault on at least this share of the fleet is by construction not a
#: per-host problem. Below it the finding is a cluster.
FLEET_FAULT_SHARE = 0.5

#: The floor for reporting anything at all. Below three servers a shared
#: signature is a coincidence between two machines; at three it starts to be
#: a pattern.
#:
#: The design called for clusters to additionally prove a shared attribute —
#: a tag or role that a minority of the fleet carries — so that "four servers
#: had an error" becomes "the four domain controllers had an error". That
#: test cannot run in this estate: `server_tags` is empty. Clusters are
#: therefore reported with their server list and spike multiple, and the
#: operator judges. Withholding them until tags exist would report nothing.
MIN_CLUSTER_SERVERS = 3


def _fleet_fault_floor(fleet_size: int) -> int:
    """The single boundary between a cluster and a fleet-wide fault.

    It was two numbers: the SQL filtered on `int(fleet * 0.5)` and the
    classifier compared against `fleet * 0.5`. At 29 servers that is 14 and
    14.5, so a burst on exactly 14 servers passed the filter and was then
    labelled a cluster."""
    return -(-fleet_size * 1 // 2) if fleet_size else 0

#: An hour must carry this multiple of the signature's own historical mean to
#: count as a spike. `System/3` sits on 29/29 servers permanently; without
#: this every report would lead with it.
SPIKE_MULTIPLE = 3.0

#: A signature needs at least this much history before "unusual" means
#: anything. Two observations cannot establish a normal.
MIN_BASELINE_HOURS = 5

#: One source against fewer than three servers is routinely a mis-scoped
#: service account or a scheduled task with a stale password. Three is where
#: "the same credential failing on machines with nothing in common" starts.
MIN_ATTACK_SERVERS = 3

#: Fewer than ten attempts across three hosts is indistinguishable from one
#: laptop with a cached credential.
MIN_ATTACK_ATTEMPTS = 10

#: A spray is deliberately paced under the lockout policy, commonly five
#: attempts per thirty minutes. A fifteen-minute window misses that by
#: construction.
ATTACK_WINDOW_MINUTES = 60

#: One source trying many DIFFERENT accounts is a spray regardless of how
#: many servers it touches.
MIN_SPRAY_ACCOUNTS = 5

#: "Wrong password" has a dozen innocent explanations. "That user does not
#: exist", repeatedly, from one host, has approximately none.
STATUS_NO_SUCH_USER = "0xc0000064"
MIN_ENUMERATION_ATTEMPTS = 5

#: Prism's own actions are checked in this window either side of a cluster.
#: It demotes more false positives than every threshold above combined.
EXPLAIN_WINDOW_MINUTES = 15

#: Sources that carry no attacker information.
_IGNORED_SOURCES = ("-", "127.0.0.1", "::1", "")

#: Categories of Prism's own activity that can explain a burst — meaning
#: they ACT on a Windows host. This list was wider and it was wrong.
#:
#: `compliance` recorded SOP executions, which happen often; an SOP record is
#: a note that a human did something, not an action against 29 servers.
#: Including it meant nearly every hour contained one, so nearly every finding
#: was "explained". The measured cost: `Application/1511` at 10.6x normal
#: across 15 of 29 servers was dismissed because someone filed an SOP record in
#: the same hour.
#:
#: `security` (RBAC grants) and `settings` (Prism's own configuration) are
#: excluded for the same reason: neither reaches a monitored host, so neither
#: can produce a log burst on one.
_EXPLAINING_CATEGORIES = (
    "restart_schedule",   # reboots a server: service-state logs follow
    "workflow",           # runs a script on servers
    "maintenance",        # suppresses/changes collection for a set of servers
    "server",             # a server's own configuration changed
)


def _utc_floor(hours: int) -> str:
    """Window start, in the canonical stored shape.

    Timestamps are compared as TEXT throughout this database, so the shape has
    to match exactly — a space-separated stamp sorts below one with a 'T' and
    becomes invisible to every `>=` filter."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── one cause, many targets ──────────────────────────────────────────────

def fleet_faults(conn, hours: int = 24, fleet_size: int | None = None) -> list[dict]:
    """Signatures that spiked across many servers in the same hour.

    Two things must both be true: the signature reached an unusual NUMBER of
    servers, and its count that hour was unusual for that signature. Either
    alone produces noise — breadth alone reports the permanent fleet-wide
    chatter, and volume alone reports one busy host.
    """
    if fleet_size is None:
        row = conn.execute(
            "SELECT COUNT(DISTINCT server_name) FROM log_signatures").fetchone()
        fleet_size = row[0] if row else 0
    if not fleet_size:
        return []

    since = _utc_floor(hours)[:13]          # 'YYYY-MM-DDTHH'
    # The floor is the CLUSTER floor, not the fleet-fault one: filtering at
    # the fleet threshold made the cluster branch unreachable.
    min_servers = MIN_CLUSTER_SERVERS
    fleet_floor = _fleet_fault_floor(fleet_size)

    sql = """
    WITH per_hour AS (
        SELECT msg_hash, log_source, level, event_id, hour_utc,
               COUNT(DISTINCT server_name) AS servers,
               SUM(count)                  AS n
        FROM log_signatures
        GROUP BY msg_hash, hour_utc
    ),
    baseline AS (
        SELECT msg_hash,
               AVG(servers) AS avg_servers,
               AVG(n)       AS avg_n,
               COUNT(*)     AS hours_seen
        FROM per_hour
        GROUP BY msg_hash
    )
    SELECT p.hour_utc, p.log_source, p.level, p.event_id, p.msg_hash,
           p.servers, p.n, b.avg_servers, b.avg_n, b.hours_seen
    FROM per_hour p
    JOIN baseline b USING (msg_hash)
    WHERE p.hour_utc >= ?
      AND p.servers >= ?
      AND b.hours_seen >= ?
      AND p.n > b.avg_n * ?
    ORDER BY
        -- Severity first. `System/7036` (a service changed state) is
        -- Information, sits on the allowlist because service tracking wants
        -- it, and outnumbers everything: ranked by breadth alone it buries
        -- the Errors underneath it. An operator reading this wants the
        -- profile-service failure on 15 servers before the service-control
        -- chatter on 18.
        CASE p.level WHEN 'Critical' THEN 0 WHEN 'Error' THEN 1
                     WHEN 'Warning'  THEN 2 ELSE 3 END,
        p.servers DESC, (p.n / b.avg_n) DESC
    LIMIT 25
    """
    rows = conn.execute(
        sql, (since, min_servers, MIN_BASELINE_HOURS, SPIKE_MULTIPLE)).fetchall()

    out = []
    for r in rows:
        (hour, source, level, event_id, msg_hash,
         servers, n, avg_servers, avg_n, hours_seen) = r
        sample = conn.execute(
            "SELECT sample FROM log_signatures WHERE msg_hash = ? LIMIT 1",
            (msg_hash,)).fetchone()
        affected = [x[0] for x in conn.execute(
            "SELECT DISTINCT server_name FROM log_signatures "
            "WHERE msg_hash = ? AND hour_utc = ? ORDER BY server_name",
            (msg_hash, hour))]
        out.append({
            "kind": "fleet-fault" if servers >= fleet_floor else "cluster-fault",
            "hour_utc": hour,
            "log_source": source,
            "level": level,
            "event_id": event_id,
            "signature": msg_hash,
            "servers": servers,
            "fleet_size": fleet_size,
            "events": n,
            "normal_servers": round(avg_servers or 0, 1),
            "normal_events": round(avg_n or 0, 1),
            "spike_multiple": round((n / avg_n), 1) if avg_n else None,
            "baseline_hours": hours_seen,
            "sample": (sample[0] if sample else "") or "",
            "affected_servers": affected,
        })
    return out


# ── one source, many targets ─────────────────────────────────────────────

def auth_attacks(conn, hours: int = 24) -> list[dict]:
    """Failed logons grouped by source, characterised by SHAPE.

    The shape is the finding. "One source, many accounts" is a spray; "one
    account, many attempts" is far more likely a stale credential retrying;
    "one source, many servers" is lateral movement. Reporting all three as
    "failed logins detected" would waste the distinction the data already
    carries.
    """
    since = _utc_floor(min(hours, 24 * 30))
    sql = """
    SELECT source_ip,
           COUNT(DISTINCT server_name)  AS servers,
           COUNT(DISTINCT account_name) AS accounts,
           COUNT(*)                     AS attempts,
           MIN(timestamp)               AS first_seen,
           MAX(timestamp)               AS last_seen,
           SUM(CASE WHEN sub_status = ? THEN 1 ELSE 0 END) AS no_such_user
    FROM failed_logins
    WHERE timestamp >= ?
      AND source_ip IS NOT NULL
      AND source_ip NOT IN (?, ?, ?, ?)
    GROUP BY source_ip
    ORDER BY servers DESC, attempts DESC
    LIMIT 50
    """
    rows = conn.execute(
        sql, (STATUS_NO_SUCH_USER, since, *_IGNORED_SOURCES)).fetchall()

    out = []
    for (ip, servers, accounts, attempts, first, last, no_such_user) in rows:
        shapes = []
        if servers >= MIN_ATTACK_SERVERS and attempts >= MIN_ATTACK_ATTEMPTS:
            shapes.append("lateral")
        if accounts >= MIN_SPRAY_ACCOUNTS and attempts >= MIN_ATTACK_ATTEMPTS:
            shapes.append("spray")
        if no_such_user >= MIN_ENUMERATION_ATTEMPTS:
            shapes.append("enumeration")

        if shapes:
            verdict, why = "targeted-attack", _attack_sentence(
                shapes, ip, servers, accounts, attempts, no_such_user)
        elif accounts == 1 and attempts >= MIN_ATTACK_ATTEMPTS:
            # The estate's loudest source is exactly this: 733 attempts, one
            # account, two servers, over eleven days. Naming it plainly is
            # more useful than either alarming or staying silent.
            verdict, why = "stale-credential", (
                f"{attempts} failures for a single account from {ip} across "
                f"{servers} server(s) — the shape of a service account or "
                f"scheduled task holding an old password, not of an attack")
        else:
            verdict, why = "noise", (
                f"{attempts} failure(s) from {ip}, below every attack threshold")

        out.append({
            "kind": verdict,
            "source_ip": ip,
            "servers": servers,
            "accounts": accounts,
            "attempts": attempts,
            "no_such_user": no_such_user,
            "first_seen": first,
            "last_seen": last,
            "shapes": shapes,
            "why": why,
        })
    return out


def _attack_sentence(shapes, ip, servers, accounts, attempts, no_such_user) -> str:
    parts = []
    if "spray" in shapes:
        parts.append(f"{accounts} different accounts tried from one source")
    if "lateral" in shapes:
        parts.append(f"the same source against {servers} servers")
    if "enumeration" in shapes:
        parts.append(f"{no_such_user} attempts on accounts that do not exist")
    return f"{ip}: " + "; ".join(parts) + f" ({attempts} attempts)"


def attack_targets(conn, source_ip: str, hours: int = 24) -> list[dict]:
    """The per-target rows behind an attack verdict.

    A verdict is not evidence. This is what an operator opens next, and what a
    report has to print underneath the sentence."""
    since = _utc_floor(min(hours, 24 * 30))
    rows = conn.execute("""
        SELECT server_name, account_name, logon_type, workstation,
               COUNT(*) AS n, MIN(timestamp), MAX(timestamp)
        FROM failed_logins
        WHERE source_ip = ? AND timestamp >= ?
        GROUP BY server_name, account_name, logon_type, workstation
        ORDER BY n DESC
        LIMIT 100
    """, (source_ip, since)).fetchall()
    return [{"server": r[0], "account": r[1], "logon_type": r[2],
             "workstation": r[3], "attempts": r[4],
             "first_seen": r[5], "last_seen": r[6]} for r in rows]


# ── was it us? ───────────────────────────────────────────────────────────

def explain(conn, when_utc: str, servers: list[str] | None = None) -> list[dict]:
    """Prism's own activity around a cluster.

    The single highest-value check in this module. A fleet-wide burst at 03:04
    that coincides with a scheduled restart is not an incident, and saying so
    is the difference between a report someone reads and a report someone
    learns to ignore.
    """
    try:
        centre = datetime.strptime(when_utc[:13], "%Y-%m-%dT%H").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return []
    lo = (centre - timedelta(minutes=EXPLAIN_WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (centre + timedelta(hours=1, minutes=EXPLAIN_WINDOW_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    placeholders = ",".join("?" * len(_EXPLAINING_CATEGORIES))
    rows = conn.execute(f"""
        SELECT timestamp, username, action, category, details
        FROM audit_log
        WHERE timestamp >= ? AND timestamp <= ?
          AND category IN ({placeholders})
        ORDER BY timestamp
        LIMIT 20
    """, (lo, hi, *_EXPLAINING_CATEGORIES)).fetchall()
    return [{"timestamp": r[0], "username": r[1], "action": r[2],
             "category": r[3], "details": r[4]} for r in rows]


# ── the whole picture ────────────────────────────────────────────────────

def analyse(conn, hours: int = 24) -> dict:
    """Everything, with each finding explained where Prism can explain it."""
    faults = fleet_faults(conn, hours=hours)
    for f in faults:
        # The explanation is carried ALONGSIDE the verdict, never instead of
        # it. A fleet-wide burst that a scheduled restart accounts for is
        # still a fleet-wide burst — the operator wants it ranked lower, not
        # erased. Overwriting `kind` hid what happened behind why.
        f["explained_by"] = explain(conn, f["hour_utc"], f.get("affected_servers"))

    attacks = auth_attacks(conn, hours=hours)
    real = [a for a in attacks if a["kind"] == "targeted-attack"]
    for a in real:
        a["targets"] = attack_targets(conn, a["source_ip"], hours=hours)

    return {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fleet_faults": faults,
        "auth": attacks,
        "attacks": real,
        "summary": _summary(faults, attacks),
    }


def _summary(faults: list[dict], auth: list[dict]) -> str:
    """One sentence, and it must be allowed to say that nothing happened."""
    attacks = [a for a in auth if a["kind"] == "targeted-attack"]
    unexplained = [f for f in faults if not f.get("explained_by")]
    stale = [a for a in auth if a["kind"] == "stale-credential"]

    if attacks:
        return (f"{len(attacks)} source(s) show attack shape against this "
                f"estate; see the per-target rows below.")
    if unexplained:
        _sev = {"Critical": 0, "Error": 1, "Warning": 2}
        worst = min(unexplained,
                    key=lambda f: (_sev.get(f["level"], 3), -f["servers"]))
        return (f"No attack shape. {len(unexplained)} unexplained fleet event(s); "
                f"the widest reached {worst['servers']} of {worst['fleet_size']} "
                f"servers ({worst['log_source']}/{worst['event_id']}).")
    if stale:
        return (f"No attack shape and no unexplained fleet event. "
                f"{len(stale)} source(s) look like a stale credential retrying.")
    return "Nothing in this window reached a reporting threshold."
