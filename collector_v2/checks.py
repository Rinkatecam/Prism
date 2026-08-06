"""Per-check functions for collector v2.

Each function:
  * Takes a ServerConfig (and possibly a WSMan/pool to reuse)
  * Runs ONE PowerShell script against the target
  * Parses the result
  * Returns a 4-tuple: (ok, data, error_message, error_kind)

The worker pool wraps these in a WorkItem→Result envelope with timing
and deadline enforcement. These check functions themselves should NOT
implement their own retry — that's the supervisor's job (backoff).

Each function is also defensive against:
  * pypsrp not installed → returns ("pypsrp_missing", error_kind="exception")
  * WSMan transport errors → categorized via _is_offline_error
  * PS stream errors → categorized as error_kind="ps"
  * JSON parse failures → error_kind="parse"
  * Shutdown-in-progress / connection-reset family → error_kind="offline"
    (so the aggregator can suppress the alarming "Update check failed"
    banner and surface a transient-reboot hint instead — see the recent
    server_detail.html auto-overlay flow)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .scripts import (
    PS_COLLECT_SCRIPT,
    PS_COLLECT_LOGS,
    PS_CHECK_UPDATES,
    PS_HARDWARE_SCRIPT,
)

logger = logging.getLogger("prism.collector_v2.checks")


# ── Offline-marker detection (extracted from v1 collect_server) ──────────
# Lowercase substring patterns that mean "this isn't a real failure, the
# target is just rebooting / unreachable / mid-shutdown." When matched,
# error_kind is set to "offline" so the aggregator knows to preserve the
# previous good payload instead of overwriting with a scary banner.

_OFFLINE_MARKERS: tuple[str, ...] = (
    "0x8033805b",   # Shell not found on server (target rebooted mid-call)
    "0x80338029",   # Access denied / host not trusted
    "readtimeout",  # pypsrp ReadTimeout — target not answering
    "connection was forcibly closed",
    "existing connection was forcibly closed",
    "shell was not found",
    "shell nicht auf dem server gefunden",
    "wird neu gestartet",
    "connection refused",
    "no route to host",
    "network is unreachable",
    "timed out",
    # Reboot-in-progress messages (WU / WSMan / RPC)
    "shutdown is in progress",
    "ein system-herunterfahren",
    "wird heruntergefahren",
    "0x8000401a",
    "the rpc server is unavailable",
    "der rpc-server ist nicht verfügbar",
    # TCP-socket reset family — fires when target tears down during reboot
    "connection aborted",
    "connectionreseterror",
    "connection reset by peer",
    "10054",
    "10053",
    "10060",
    "10061",
    "eine vorhandene verbindung wurde",
    "vom remotehost geschlossen",
    "verbindungsversuch ist fehlgeschlagen",
    "max retries exceeded",
    "newconnectionerror",
    # WSMan ERROR_OPERATION_ABORTED (Windows error 995). Fires when the
    # WinRM session is killed mid-call — most often because the target is
    # rebooting (cumulative-update install + auto-reboot) or because the
    # target's WinRM service was restarted while a long-running call (WU
    # COM query, log scrape) was in flight. The marker covers both
    # locales — the German phrase is what we actually see on this fleet,
    # the English equivalent is what an English-locale Windows would emit.
    "code: 995",
    "wegen eines threadendes",
    "anwendungsanforderung abgebrochen",
    "i/o operation has been aborted",
    "thread exit or an application request",
    # Windows Update API errors that mean "WU service / system was being
    # shut down" — by definition transient (next cycle reaches a stable
    # box). Worth pre-emptively listing the common HRESULTs so they don't
    # surface as scary banners on the dashboard.
    "0x8024001e",       # WU_E_SERVICE_STOP — service was stopping
    "0x80240020",       # WU_E_NO_INTERACTIVE_USER — sometimes during reboot
    "wu_e_service_stop",
)


def _is_offline_error(exc_or_str: Any) -> bool:
    """True if the error string matches any of the known reboot/unreachable
    patterns. Caller should classify the Result as error_kind='offline'."""
    s = str(exc_or_str).lower()
    return any(m in s for m in _OFFLINE_MARKERS)


# Human-facing reason for an unreachable host, keyed by what the failure
# actually was. "offline" is one word for at least four different situations
# that need different responses, and presenting them identically cost real
# diagnostic time twice on this fleet:
#
#   * a healthy domain controller sat labelled offline for days because its NIC
#     was classified Public, so the LocalSubnet-scoped WinRM rule never matched
#     — RPC, SMB, RDP, DNS and LDAP were all fine.
#   * STANDALONE01 read offline for its entire history because its DNS name did not
#     resolve; the host was up the whole time and simply not domain-joined.
#
# Note on why this is safe to show here but NOT on /api/test-connection: that
# endpoint deliberately collapses these categories, because distinguishing
# "auth failed" from "host not found" is an oracle for port scanning and
# credential spray (RF2, AUDIT-2026-05). The dashboard is different — it is
# authenticated, RBAC-gated, and reports on the operator's OWN fleet, where the
# distinction is the entire diagnostic value.
UNREACHABLE_REASONS: dict[str, tuple[str, ...]] = {
    # Name resolution failed — the host may be perfectly healthy.
    "dns": ("getaddrinfo", "name or service not known", "nameresolutionerror",
            "temporary failure in name resolution", "nodename nor servname",
            "der dns-name ist nicht vorhanden", "no such host is known"),
    # Something answered and said no — the host is up, the listener is not.
    "refused": ("connection refused", "actively refused", "connectionreseterror",
                "connection was forcibly closed", "reset by peer"),
    # Credentials/Kerberos — the host is up and reachable.
    "auth": ("401", "unauthorized", "access is denied", "0x80338029",
             "authenticationerror", "failed to authenticate"),
    # Packets dropped. Classic firewall filtering: nothing answers at all.
    "timeout": ("readtimeout", "connecttimeout", "timed out", "timeout"),
    # Routing.
    "network": ("no route to host", "network is unreachable",
                "network is down", "unreachable network"),
    # The target is mid-reboot — expected, transient, not a fault.
    "rebooting": ("shell was not found", "0x8033805b", "wird neu gestartet",
                  "shell nicht auf dem server gefunden", "restarting"),
}

# Checked in order: a message can match several (a DNS failure surfaces as a
# ConnectionError whose text also contains "timeout"), and the FIRST match is
# the most specific cause. DNS before refused before auth before timeout,
# because timeout is the vaguest and would otherwise swallow the rest.
_REASON_ORDER = ("dns", "refused", "auth", "network", "rebooting", "timeout")

REASON_TEXT: dict[str, str] = {
    "dns": "name does not resolve",
    "refused": "connection refused — host up, WinRM not listening",
    "auth": "authentication failed",
    "timeout": "no response — packets dropped, likely filtered",
    "network": "no route to host",
    "rebooting": "restarting",
    "unknown": "unreachable",
}


def classify_unreachable(exc_or_str: Any) -> str:
    """Return WHY a host is unreachable: one of UNREACHABLE_REASONS' keys, or
    'unknown'.

    This exists so the dashboard can stop saying "offline" for a host that is
    demonstrably up. It is a hint for the operator, never a control-flow
    decision — everything downstream still treats the host as unreachable.
    """
    s = str(exc_or_str).lower()
    for reason in _REASON_ORDER:
        if any(m in s for m in UNREACHABLE_REASONS[reason]):
            return reason
    return "unknown"


def unreachable_reason_text(exc_or_str: Any) -> str:
    """Short operator-facing phrase for why a host is unreachable."""
    return REASON_TEXT.get(classify_unreachable(exc_or_str), REASON_TEXT["unknown"])


def _unwrap_ps_json_value_array(parsed: Any) -> Any:
    """PowerShell's ConvertTo-Json sometimes wraps arrays of hashtables as
    `{"value": [...], "Count": N}` (PSObject serialization). Unwrap so the
    caller always sees a flat list/dict.

    Without this, the v1 collector silently stored every log entry as
    `source='Unknown', level=default, message=''` because the wrapper dict
    didn't have the per-row keys."""
    if isinstance(parsed, dict) and set(parsed.keys()) == {"value", "Count"} \
            and isinstance(parsed["value"], list):
        return parsed["value"]
    return parsed


def _run_ps(pool, script: str, server_name: str) -> tuple[bool, str, str | None, str | None]:
    """Run one PS script in an existing RunspacePool and return raw stdout.

    Returns (ok, stdout, error_message, error_kind).

    The check-specific function is responsible for JSON-parsing stdout
    when ok is True.
    """
    try:
        from pypsrp.powershell import PowerShell
    except ImportError:
        return False, "", "pypsrp not installed", "exception"

    try:
        ps = PowerShell(pool)
        ps.add_script(script)
        output = ps.invoke()

        ps_err_msg = ""
        if ps.had_errors:
            try:
                ps_err_msg = "; ".join(str(e) for e in ps.streams.error)[:400]
            except Exception:
                ps_err_msg = "PowerShell stream had errors"

        # PS scripts often return BOTH had_errors=True AND useful stdout
        # (e.g. WU script catches its own exceptions and emits JSON anyway).
        # We treat that case as "ok" if output exists; the check function
        # can decide how to interpret the parsed payload.
        if output:
            raw = str(output[0]) if output[0] is not None else ""
            if raw:
                return True, raw, None, None

        # No output — that's a real failure
        if ps_err_msg:
            kind = "offline" if _is_offline_error(ps_err_msg) else "ps"
            return False, "", ps_err_msg, kind
        return False, "", "No output from PowerShell", "ps"
    except Exception as e:
        kind = "offline" if _is_offline_error(e) else "winrm"
        return False, "", f"{type(e).__name__}: {str(e)[:300]}", kind


# ── Public check functions ───────────────────────────────────────────────
# Each takes (server, pool) and returns (ok, data, error, error_kind).
# `pool` is an already-opened pypsrp RunspacePool — the worker manages
# the connection lifecycle.

def check_metrics(server, pool) -> tuple[bool, dict | None, str | None, str | None]:
    """Pull CPU/RAM/disk metrics from the target server.

    Returns:
        ok=True with data={"cpu": float, "ram": float, "disk_c": float,
                            "disk_d": float, "collection_time_ms": int}
        ok=False with error + error_kind on any failure.
    """
    t0 = time.time()
    ok, raw, err, kind = _run_ps(pool, PS_COLLECT_SCRIPT, server.name)
    if not ok:
        return False, None, err, kind
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as je:
        return False, None, f"Bad metrics JSON: {str(je)[:200]}; raw={raw[:200]}", "parse"
    data["collection_time_ms"] = int((time.time() - t0) * 1000)
    return True, data, None, None


def check_logs(server, pool) -> tuple[bool, list | None, str | None, str | None]:
    """Pull recent event log entries from the target server.

    Returns:
        ok=True with data = list[{"source", "time", "level", "event_id",
                                   "message"}]  (top-30 prioritized by severity
                                                 per System/Application/Security)
        ok=False with error + error_kind on any failure.
    """
    ok, raw, err, kind = _run_ps(pool, PS_COLLECT_LOGS, server.name)
    if not ok:
        return False, None, err, kind
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as je:
        return False, None, f"Bad logs JSON: {str(je)[:200]}", "parse"

    parsed = _unwrap_ps_json_value_array(parsed)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return False, None, f"Logs payload not a list: {type(parsed).__name__}", "parse"
    return True, parsed, None, None


def check_updates(server, pool) -> tuple[bool, dict | None, str | None, str | None]:
    """Pull pending Windows Update list + reboot-required state.

    Returns:
        ok=True with data = {"count": int, "updates": list, "reboot_required":
                              bool, "pending_reboot": bool, "error"?: str}
        ok=False with error + error_kind on any failure.

    Note: the PS script catches its own exceptions and returns JSON with
    an "error" field even on internal failure. We treat that as a SUCCESSFUL
    check that yielded an error-classified payload — the aggregator will
    then apply the _is_offline_error gate to decide whether to surface or
    suppress the error.
    """
    ok, raw, err, kind = _run_ps(pool, PS_CHECK_UPDATES, server.name)
    if not ok:
        return False, None, err, kind
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as je:
        return False, None, f"Bad updates JSON: {str(je)[:200]}", "parse"
    if not isinstance(data, dict):
        return False, None, f"Updates payload not a dict: {type(data).__name__}", "parse"
    # If the script returned its own error field AND it looks like a reboot
    # message, classify the WHOLE result as offline so aggregator suppresses.
    inner_err = data.get("error")
    if inner_err and _is_offline_error(inner_err):
        return False, None, str(inner_err)[:300], "offline"
    return True, data, None, None


def check_hardware(server, pool) -> tuple[bool, dict | None, str | None, str | None]:
    """Pull hardware inventory (CPU model, cores, RAM, disk sizes).

    Returns:
        ok=True with data = {"cpu_name", "cores", "threads", "total_ram_gb",
                              "os", "disk_c_size_gb", "disk_c_free_gb",
                              "disk_d_size_gb", "disk_d_free_gb"}
        ok=False with error + error_kind on any failure.
    """
    ok, raw, err, kind = _run_ps(pool, PS_HARDWARE_SCRIPT, server.name)
    if not ok:
        return False, None, err, kind
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as je:
        return False, None, f"Bad hardware JSON: {str(je)[:200]}", "parse"
    return True, data, None, None


# NOTE: there is deliberately no failed-login check here. A `check_failed_logins`
# used to sit at this spot, fully implemented but never dispatched — it was absent
# from both the CheckType enum (types.py) and _CHECK_DISPATCH (workers.py), and its
# comment claimed it was "piggy-backed on the LOGS check", which was never true:
# _handle_logs_result only calls db.insert_logs().
#
# The live failed-login path is entirely separate and works: periodics.py runs a
# `_failed_logins` job on a 300s cadence which calls
# failed_logins.py::_collect_all_failed_logins(), opening its own WinRM session.
# If failed logins ever need the worker-pool shape instead, add a CheckType member
# and a dispatch entry — don't just re-add an undispatched function.
