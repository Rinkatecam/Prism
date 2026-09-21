"""Collector-side caps on what a monitored host may send.

Collector audit finding 1 (HIGH), plus findings 4 and 9
(`docs/plans/COLLECTOR_AUDIT_2026-08.md`).

THE THREAT MODEL, stated plainly because it decides every default here: a
monitored Windows server is SEMI-TRUSTED. Prism asks it to run a PowerShell
script and believes the answer. The script caps its own output at 30 rows and
200 characters — and a host that has been compromised is under no obligation to
run the script Prism sent, or to answer within any limit at all. Before this
module the limits existed nowhere else: the check returned whatever came back,
the writer stored it, and the failed-login query had no bound on its result set.
Because every server writes through one process-global write lock, that made
one owned machine able to stall monitoring for the entire fleet.

TWO DECISIONS, both deliberate and both testable:

  * An OVERSIZE PAYLOAD IS REJECTED, never truncated. Truncated JSON does not
    parse, so truncating would report a host streaming megabytes as "Bad JSON"
    — the symptom furthest from the cause, and the one a reader would blame on
    the script. A rejection names what actually happened.

  * TOO MANY ROWS ARE TRUNCATED, never rejected. Thirty real rows arriving
    alongside ten thousand junk ones should still yield the thirty. Truncation
    keeps the FIRST N, which means a hostile host chooses which rows survive.
    That is accepted: the cap's job is to bound storage and write-lock time, and
    no cap can make a compromised host tell the truth about its own event log.

WHAT THIS IS NOT. It is not a filter and not a detector. It bounds volume. The
adjacent findings it does NOT close are named in the audit and stay open on
purpose: a host can still forge the CONTENT of its logs, and it can still make
every line unique so that no two rows coalesce (finding 9) — the row cap bounds
how much that costs per collection, and nothing here judges what a row says.

Everything capped is COUNTED and surfaced on /api/system/health. A monitoring
tool that quietly throws data away is one you stop trusting; the Information-
level ingest filter already follows that rule and so does this.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("prism.ingest")

#: The ceilings. Each is generously above what the shipped PowerShell produces,
#: so a compliant host is NEVER truncated — a cap that clips honest data is a
#: data-loss bug wearing a security feature's name.
#:
#: max_payload_chars   one check's whole response, read before `json.loads`.
#: max_rows_per_check  rows from one check. The logs script emits 30.
# Raised 100 -> 120 when this was found clipping the Firewall
    # channel: the script emits 30 per channel and there are FOUR
    # channels (System, Application, Security, Firewall), so 100 cut
    # the last one. Firewall is last, so Firewall is what was lost.
#: max_message_chars   any single host-controlled string field. The script
#:                     truncates messages at 200.
#: max_failed_logins   rows from one failed-login collection. Separate because a
#:                     busy domain controller legitimately produces far more
#:                     failed logons in fifteen minutes than a print server.
DEFAULTS: dict[str, int] = {
    "max_payload_chars": 512 * 1024,
    "max_rows_per_check": 120,
    "max_message_chars": 400,
    "max_failed_logins": 200,
}

#: Host-controlled string fields on a failed-login row. All of them come from
#: the target's own event XML, so capping only the obvious one would leave the
#: others as the flood path.
_FAILED_LOGIN_TEXT_FIELDS = (
    "account_name", "domain", "workstation", "process_name",
    "source_ip", "source_port", "logon_type", "status_code", "sub_status",
)

_counter_lock = threading.Lock()
_counters = {"payloads_rejected": 0, "rows_dropped": 0, "fields_truncated": 0}


def _bump(name: str, n: int = 1) -> None:
    if n <= 0:
        return
    with _counter_lock:
        _counters[name] += n


def snapshot() -> dict:
    """What the caps have discarded this process lifetime."""
    with _counter_lock:
        return dict(_counters)


def reset_counters() -> None:
    """Test hook. Never called in production — these are lifetime counters."""
    with _counter_lock:
        for key in _counters:
            _counters[key] = 0


def resolve(settings: dict | None) -> dict:
    """Merge `settings.ingest_caps` over the defaults.

    Every value is coerced to an int and FLOORED AT 1. Zero or a negative would
    mean "discard everything", which is a monitoring outage wearing a config
    key's name — so there is deliberately no way to configure the caps into
    silence. A value that cannot be read at all falls back to its default
    rather than raising: config.json is hand-edited, and one bad character must
    not stop ingest for the whole fleet.

    Raising a cap IS offered, because a busy fleet legitimately needs it. An
    operator who sets a cap absurdly high has chosen that, and the counters
    still say what is arriving.
    """
    out = dict(DEFAULTS)
    for key, value in ((settings or {}).get("ingest_caps") or {}).items():
        if key not in DEFAULTS:
            continue                      # unknown key: not ours to guess at
        try:
            out[key] = max(1, int(value))
        except (TypeError, ValueError):
            logger.debug("ingest_caps.%s is unreadable; using the default", key)
    return out


def payload_ok(raw: str, caps: dict | None = None, *,
               server: str | None = None) -> bool:
    """Is this response small enough to parse? False ⇒ reject the check.

    Called BEFORE `json.loads`, which is where the memory of a host streaming
    gigabytes inside its deadline would actually go (audit finding 4).
    """
    limit = (caps or DEFAULTS)["max_payload_chars"]
    if raw is None or len(raw) <= limit:
        return True
    _bump("payloads_rejected")
    logger.warning("[%s] response of %d chars exceeds the %d-char ingest "
                   "ceiling; the check is refused rather than truncated",
                   server or "?", len(raw), limit)
    return False


def _cap_text(value, limit: int) -> tuple[object, bool]:
    """Truncate a string field. Non-strings are returned untouched — an
    event_id is an int and stringifying it here would change the column's
    meaning to satisfy a length rule that never applied to it."""
    if not isinstance(value, str) or len(value) <= limit:
        return value, False
    return value[:limit], True


def _cap_rows(rows, row_limit: int, text_limit: int, fields,
              server: str | None) -> list:
    if not isinstance(rows, list):
        return []
    kept = rows[:row_limit]
    dropped = len(rows) - len(kept)
    if dropped > 0:
        _bump("rows_dropped", dropped)
        logger.warning("[%s] ingest returned %d rows; keeping %d and dropping "
                       "%d at the cap", server or "?", len(rows),
                       len(kept), dropped)

    out = []
    truncated = 0
    for row in kept:
        if not isinstance(row, dict):
            # A hostile host can answer with a list of strings. Keep the row
            # out of the batch rather than raising: a crash here stalls a
            # worker thread, which is the outcome the cap exists to prevent.
            continue
        # Copied, never edited in place: the collector logs and re-reads its own
        # payloads, and mutating them would make the log say something other
        # than what was stored.
        new = dict(row)
        for field in fields:
            if field in new:
                new[field], hit = _cap_text(new[field], text_limit)
                truncated += 1 if hit else 0
        out.append(new)
    if truncated:
        _bump("fields_truncated", truncated)
    return out


def cap_log_rows(rows, caps: dict | None = None, *,
                 server: str | None = None) -> list:
    """Bound one logs payload: row count, and the message field's length."""
    c = caps or DEFAULTS
    return _cap_rows(rows, c["max_rows_per_check"], c["max_message_chars"],
                     ("message", "source"), server)


def cap_failed_logins(rows, caps: dict | None = None, *,
                      server: str | None = None) -> list:
    """Bound one failed-login payload. Its own row cap, and every one of the
    host-controlled text fields."""
    c = caps or DEFAULTS
    return _cap_rows(rows, c["max_failed_logins"], c["max_message_chars"],
                     _FAILED_LOGIN_TEXT_FIELDS, server)
