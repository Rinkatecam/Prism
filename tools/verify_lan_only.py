"""Enumerate every destination the running Prism process actually connects to.

    python tools/verify_lan_only.py --pid 1234
    python tools/verify_lan_only.py --port 5000 --seconds 180
    python tools/verify_lan_only.py --port 5000 --seconds 180 --json

Exit codes:

    0   PASS          connections were observed, none to a public address
    2   FAIL          a routable public address was contacted
    3   INCONCLUSIVE  nothing was observed at all, or every sample failed
    1   usage error   no process found

**3 exists because 0 used to cover it, and that made the tool's worst failure
look like its best result.** A stale `--pid`, a mistyped `--port`, a missing
`netstat` and an idle collector all produce zero rows; scoring that as PASS
hands someone clean-looking evidence produced by an instrument that measured
nothing. Scripts should treat anything non-zero as "not proven".

Windows first — `Get-NetTCPConnection` is the primary sampler — with a
`netstat` fallback so it also runs on Linux CI.

This is the DYNAMIC half of the evidence in `docs/DATA_FLOWS.md`. That document
is a static reading of the source: it proves no destination is hardcoded. This
proves what the process does at runtime on your own kit, with your own fleet
and your own configuration, which is the only version of the claim that should
convince anybody.

WHY THE CLASSIFIER IS NOT `ipaddress.is_private`
------------------------------------------------
Both obvious one-liners are wrong, and each is wrong in the direction that
matters. Measured on CPython 3.13:

  * `255.255.255.255` — the Wake-on-LAN broadcast, and the ONE hardcoded
    destination in Prism — reports `is_private = True` but `is_global = False`.
    A check written as `is_global` would pass it; a check written as
    `not is_private` would flag it. Getting this wrong means the verification
    tool reports a false positive on exactly the address the dossier singles
    out and defends.
  * `224.0.0.1` — multicast — reports `is_private = False` **and
    `is_global = True`**. A `not is_private` check calls it external. Nothing
    in Prism multicasts, but a dependency doing mDNS or SSDP discovery would
    produce a scary-looking finding that is not one.
  * `100.64.0.1` — CGNAT space — reports `is_private = False`,
    `is_global = True`. On a carrier network that is external. Inside a large
    corporate network that uses 100.64/10 for internal addressing, it is not.
    A tool cannot know which, so it must not pretend to.

So addresses land in three buckets, not two, and the ambiguous one is reported
rather than guessed:

  LOCAL    loopback, RFC1918 / ULA, link-local, 0.0.0.0, broadcast
  REVIEW   CGNAT and multicast — reported, never silently passed or failed
  PUBLIC   routable global unicast. This is the one that fails the run.

WHAT THIS CANNOT SEE
--------------------
**Sampling misses connections shorter than the interval.** This polls the OS
connection table; a TCP session that opens and closes between two samples
leaves no trace. WinRM calls are short. Raising `--seconds` and lowering
`--interval` narrows the gap and cannot close it.

That is why the firewall procedure in `docs/LAN_ONLY_VERIFICATION.md` is the
primary evidence and this script is the supporting evidence. A blocked
outbound connection produces a visible failure; it does not depend on catching
the packet in the act. Run both. If they disagree, believe the firewall.

It also cannot attribute a connection to a REASON. It reports that the process
talked to 10.0.0.5:5985, not which feature did — `docs/DATA_FLOWS.md` maps
ports to features.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

LOCAL, REVIEW, PUBLIC = "local", "review", "public"

# Ports Prism is expected to use outbound, from docs/DATA_FLOWS.md. Purely
# descriptive — an unexpected port is annotated, never failed, because the
# operator chooses health-check and webhook ports freely.
KNOWN_PORTS = {
    5985: "WinRM (HTTP)", 5986: "WinRM (HTTPS)",
    389: "LDAP", 636: "LDAPS",
    25: "SMTP", 465: "SMTPS", 587: "SMTP submission",
    9: "Wake-on-LAN",
    53: "DNS (the OS resolver, not Prism)",
}


def classify(addr: str) -> tuple[str, str]:
    """Bucket an address. Returns (bucket, why).

    The `why` is carried through to the report so a reader can check the
    judgement instead of trusting it.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return REVIEW, "unparseable address"

    if ip.is_unspecified:
        return LOCAL, "unspecified (0.0.0.0 / ::) — a listening socket"
    if ip.is_loopback:
        return LOCAL, "loopback"
    if ip.is_link_local:
        return LOCAL, "link-local — not forwarded by routers"
    if ip.version == 4 and ip == ipaddress.IPv4Address("255.255.255.255"):
        return LOCAL, "limited broadcast — not forwarded by routers"
    if ip.is_multicast:
        return REVIEW, "multicast — not a unicast exfiltration path, but not RFC1918 either"
    if ip.version == 4 and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return REVIEW, "CGNAT space — carrier-grade NAT, or a large private network"
    if ip.is_private:
        return LOCAL, "private address space (RFC1918 / ULA)"
    if ip.is_reserved:
        return REVIEW, "reserved range"
    return PUBLIC, "routable public unicast"


@dataclass
class Observation:
    endpoints: Counter = field(default_factory=Counter)   # (addr, port) -> hits
    samples: int = 0
    errors: list[str] = field(default_factory=list)


def _powershell_connections(pid: int) -> list[tuple[str, int]]:
    ps = (
        f"Get-NetTCPConnection -OwningProcess {pid} -ErrorAction SilentlyContinue"
        " | Where-Object {$_.State -ne 'Listen'}"
        " | Select-Object RemoteAddress,RemotePort"
        " | ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", ps],
                         capture_output=True, text=True, timeout=30)
    text = (out.stdout or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return [(str(d["RemoteAddress"]), int(d["RemotePort"])) for d in data]


def _split_endpoint(text: str) -> tuple[str, int] | None:
    """`1.2.3.4:443` / `[::1]:443` / `1.2.3.4.443` -> (addr, port)."""
    text = text.strip()
    if not text or text in ("*", "*:*"):
        return None
    if text.startswith("["):                       # [::1]:443
        addr, _, port = text.rpartition("]:")
        addr = addr.lstrip("[")
    else:
        addr, sep, port = text.rpartition(":")
        if not sep:                                # Linux `-tunp` can use a dot
            addr, _, port = text.rpartition(".")
    if not port.isdigit() or not addr or addr == "*":
        return None
    return addr, int(port)


def _netstat_rows() -> list[list[str]]:
    flags = ["-ano"] if platform.system() == "Windows" else ["-tunp"]
    out = subprocess.run(["netstat", *flags], capture_output=True, text=True,
                         timeout=60)
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].lower().startswith(("tcp", "udp")):
            rows.append(parts)
    return rows


def _row_pid(parts: list[str]) -> int | None:
    """The PID from a netstat row.

    Compared EXACTLY, not with `str(pid) in line`. The loose version matches a
    pid of 500 against a local port of 5000, and against any address or port on
    the row that happens to contain those digits — it would attribute other
    processes' connections to Prism, which for a tool whose entire output is
    "who did Prism talk to" is the worst available failure.
    """
    last = parts[-1]
    if last.isdigit():                             # Windows: trailing PID column
        return int(last)
    if "/" in last:                                # Linux: 1234/python
        head = last.split("/")[0]
        return int(head) if head.isdigit() else None
    return None


def _is_listening(remote: tuple[str, int] | None) -> bool:
    """A listening socket, decided STRUCTURALLY rather than by state text.

    `netstat`'s status column is LOCALISED by Windows — a non-English install
    prints its own words for LISTENING and ESTABLISHED. Matching the English
    strings therefore finds nothing on such a host, and "no rows matched"
    renders as a clean PASS with zero endpoints: a verification tool that
    always says yes, on any system whose display language is not English. This
    was not hypothetical; it is why the check is structural.

    A listening socket has no peer: remote port 0 and an unspecified address.
    That is true in every language.
    """
    if remote is None:
        return True
    addr, port = remote
    if port == 0:
        return True
    try:
        return ipaddress.ip_address(addr).is_unspecified
    except ValueError:
        return False


def _netstat_connections(pid: int) -> list[tuple[str, int]]:
    """Fallback sampler. Parses `netstat -ano` (Windows) / `-tunp` (Linux)."""
    found = []
    for parts in _netstat_rows():
        if _row_pid(parts) != pid:
            continue
        remote = _split_endpoint(parts[2])
        if _is_listening(remote):
            continue
        found.append(remote)
    return found


def find_pid_by_port(port: int) -> int | None:
    """The PID listening on `port`, or None.

    Tries PowerShell first, then netstat. The fallback is not defensive
    boilerplate: on a hardened Windows host, spawning `powershell.exe` from
    another process can be denied by local policy, which surfaces as `OSError`
    (`WinError 1260`) rather than as a non-zero exit code — so it has to be
    caught, not checked for. A tool that exists to prove a security property
    has to survive running on a locked-down machine, because that is where
    someone will want to run it.
    """
    if platform.system() == "Windows":
        ps = (f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
              f"-ErrorAction SilentlyContinue | Select-Object -First 1)"
              f".OwningProcess")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                  "-Command", ps],
                                 capture_output=True, text=True, timeout=30)
            text = (out.stdout or "").strip()
            if text.isdigit():
                return int(text)
        except (OSError, subprocess.SubprocessError):
            pass                                   # fall through to netstat

    try:
        for parts in _netstat_rows():
            # Structural, not textual — see _is_listening. The status column
            # is localised and cannot be matched against English words.
            if not _is_listening(_split_endpoint(parts[2])):
                continue
            local = _split_endpoint(parts[1])
            if local and local[1] == port:
                pid = _row_pid(parts)
                if pid:
                    return pid
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _pick_sampler():
    """PowerShell where it works, netstat where it does not.

    Probed once up front rather than discovered mid-run, so the warning about
    degrading appears before the observation window instead of after it.
    """
    if platform.system() != "Windows":
        return _netstat_connections, None
    try:
        _powershell_connections(0)                 # pid 0 owns nothing; cheap probe
        return _powershell_connections, None
    except (OSError, subprocess.SubprocessError) as exc:
        return _netstat_connections, (
            f"PowerShell unavailable ({type(exc).__name__}); using netstat. "
            "Expected wherever local policy prevents powershell.exe from "
            "being launched by another process; the results are equivalent.")
    except Exception as exc:                       # noqa: BLE001 — json/parse
        return _netstat_connections, f"PowerShell probe failed ({exc}); using netstat."


def observe(pid: int, seconds: int, interval: float) -> Observation:
    obs = Observation()
    sampler, warning = _pick_sampler()
    if warning:
        obs.errors.append(warning)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            for addr, port in sampler(pid):
                obs.endpoints[(addr, port)] += 1
        except Exception as exc:                       # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if msg not in obs.errors:
                obs.errors.append(msg)
            if sampler is _powershell_connections:
                sampler = _netstat_connections         # degrade once, keep going
        obs.samples += 1
        time.sleep(interval)
    return obs


def report(obs: Observation, as_json: bool) -> int:
    rows = []
    for (addr, port), hits in sorted(obs.endpoints.items()):
        bucket, why = classify(addr)
        rows.append({"address": addr, "port": port, "hits": hits,
                     "bucket": bucket, "why": why,
                     "service": KNOWN_PORTS.get(port, "operator-defined")})

    public = [r for r in rows if r["bucket"] == PUBLIC]
    review = [r for r in rows if r["bucket"] == REVIEW]

    # A verdict of three values, not two. "I saw nothing" is not "nothing
    # happened": it is also what a stale --pid, a mistyped --port, a missing
    # netstat, or a sampler that raised on every iteration looks like. Folding
    # that into PASS makes this tool's worst failure indistinguishable from its
    # best result — a broken instrument reading as clean evidence, which is the
    # same shape as the localised-status-column bug this file already carries a
    # note about. Someone attaches the output to an audit file either way.
    #
    # Observing nothing IS legitimate on an idle collector with no servers
    # configured, which is exactly why it must be reported as inconclusive
    # rather than guessed in either direction.
    inconclusive = (not rows) or (obs.samples > 0 and len(obs.errors) >= obs.samples)
    verdict = "FAIL" if public else ("INCONCLUSIVE" if inconclusive else "PASS")
    # 2 = saw something it should not. 3 = saw nothing it can stand behind.
    # Distinct codes so a script can tell "failed" from "proved nothing".
    code = 2 if public else (3 if inconclusive else 0)

    if as_json:
        print(json.dumps({"samples": obs.samples, "endpoints": rows,
                          "public": len(public), "review": len(review),
                          "errors": obs.errors,
                          "verdict": verdict}, indent=2))
        return code

    print(f"\nsamples taken: {obs.samples}")
    print(f"distinct endpoints: {len(rows)}\n")
    if obs.errors:
        print("sampler warnings:")
        for e in obs.errors:
            print(f"  {e}")
        print()

    if not rows:
        print("No outbound connections observed.")
        print("This is NOT a pass. Check, in this order: that the pid is the")
        print("running Prism process; that servers are configured; that the")
        print("window covered a full poll cycle; and the sampler warnings")
        print("above. An idle collector with no servers legitimately reaches")
        print("nothing — which is why this cannot be scored either way.\n")

    for bucket, label in ((LOCAL, "LOCAL"), (REVIEW, "NEEDS REVIEW"),
                          (PUBLIC, "PUBLIC — OFF YOUR NETWORK")):
        group = [r for r in rows if r["bucket"] == bucket]
        if not group:
            continue
        print(f"{label}  ({len(group)})")
        for r in group:
            print(f"  {r['address']}:{r['port']:<6} x{r['hits']:<4} "
                  f"{r['service']:<28} {r['why']}")
        print()

    if public:
        print("VERDICT: FAIL — Prism connected to a routable public address.")
        print("Every destination should come from your configuration. Check")
        print("your webhook URLs and health-check endpoints first: those are")
        print("operator-chosen and CAN legitimately be external if you pointed")
        print("them there. Anything else is a finding — please report it.")
        return code

    if inconclusive:
        print("VERDICT: INCONCLUSIVE — this run proves nothing, either way.")
        print("It observed no connections at all, or every sample failed. Fix")
        print("the cause above and run it again; do not record this as a pass.")
        return code

    print("VERDICT: PASS — no connection to a routable public address observed.")
    if review:
        print(f"({len(review)} endpoint(s) in the review bucket above — read them.)")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pid", type=int, help="Prism's process id")
    ap.add_argument("--port", type=int, default=5000,
                    help="find the pid by the port Prism listens on (default 5000)")
    ap.add_argument("--seconds", type=int, default=180,
                    help="observation window; cover at least one full poll cycle")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="seconds between samples")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pid = args.pid or find_pid_by_port(args.port)
    if not pid:
        print(f"Could not find a process listening on port {args.port}. "
              f"Pass --pid explicitly.", file=sys.stderr)
        return 1

    if not args.json:
        print(f"Watching pid {pid} for {args.seconds}s "
              f"(sample every {args.interval}s) …")
    return report(observe(pid, args.seconds, args.interval), args.json)


if __name__ == "__main__":
    sys.exit(main())
