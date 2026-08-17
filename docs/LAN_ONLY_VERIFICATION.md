# Verifying that Prism is LAN-only, on your own kit

A procedure you run yourself, on your own hardware, against your own fleet.
Evidence you generate beats evidence a vendor supplies, so nothing here asks
you to take our word for anything.

Two independent checks. Run both — they fail in different ways, which is the
point of having two.

| | what it proves | how it can be wrong |
|---|---|---|
| **A. Firewall** | Prism works with all non-LAN egress blocked | nothing subtle; a blocked connection fails loudly |
| **B. Connection census** | every destination Prism reached was on your network | sampling can miss a very short connection |

**If they disagree, believe the firewall.** A is the primary evidence; B
supports it and tells you *what* was contacted rather than only that nothing
escaped.

Companion documents: `docs/DATA_FLOWS.md` is the static reading of the source —
which call sites exist and where each destination comes from. This file is the
runtime reading.

---

## Expected result, stated before you run anything

Committing the expected output is what makes "it matches the doc" a pass
condition rather than a judgement call. It is stated as **address classes**,
never as literal addresses — your fleet's addresses are yours, they differ from
every other installation, and a document listing them would be both useless to
you and a small liability.

**Check B must report:**

- `VERDICT: PASS — no connection to a routable public address observed.`
  (**Not** `INCONCLUSIVE` — that means the run observed nothing and proves
  nothing. Fix the cause it prints and run it again.)
- Loopback entries — the browser talking to Prism, and Prism's own health
  endpoints.
- Private (RFC1918 / ULA) entries on **port 5985 or 5986**, one group per
  server you monitor. This is WinRM and it is the product working.
- Private entries on **389 / 636** only if you configured LDAP, **25 / 465 /
  587** only if you configured SMTP, and whatever ports your health checks and
  webhooks name.
- **Zero** entries under `PUBLIC — OFF YOUR NETWORK`.

**Anything under `PUBLIC` is a finding.** Check your own configuration first —
a webhook URL or an HTTP health check pointed at an internet host is *supposed*
to go there, because you told it to. If a public destination appears that you
did not configure, that contradicts `docs/DATA_FLOWS.md` and we want to hear
about it.

Entries under `NEEDS REVIEW` are not failures and not free passes: they are
addresses the tool will not classify for you. See "Why three buckets" below.

---

## Check A — block all non-LAN egress and confirm nothing breaks

> **Read before running.** These commands change the host firewall. Run them on
> a test host or during a maintenance window, and know how to remove them
> before you add them. Blocking outbound traffic can affect other software on
> the same machine, including remote-management agents and anything that
> resolves DNS against an external resolver. **This document does not run
> anything for you — you do, deliberately.**

Run in an elevated PowerShell. Replace the placeholder ranges with the ones
your own network uses.

**1. Note how to undo it first.**

```powershell
Get-NetFirewallRule -DisplayName "PRISM-LANONLY-*" | Remove-NetFirewallRule
```

**2. Allow your own network, then block everything else outbound.**

`LocalSubnet` covers the segment this host sits on. If the servers you monitor
live on other subnets — or your directory or mail server does — add those
ranges to `-RemoteAddress` as a comma-separated list. Only you know which
ranges those are, so the command deliberately does not guess:

```powershell
New-NetFirewallRule -DisplayName "PRISM-LANONLY-allow-lan" -Direction Outbound -Action Allow -RemoteAddress LocalSubnet -Profile Any
New-NetFirewallRule -DisplayName "PRISM-LANONLY-block-rest" -Direction Outbound -Action Block -RemoteAddress Internet -Profile Any
```

Windows resolves `Internet` as "not any local range", so the pair reads as
"my network, and nothing else". If you add ranges to the allow rule, re-run
step 4 afterwards — a wider allow rule is exactly what makes a passing result
meaningless.

**3. Restart Prism, then exercise core monitoring** — the parts that must work
without internet access:

- the dashboard loads and the estate figure is populated, not `—`
- server cards show CPU / RAM / disk that update on the refresh cycle
- open a server's detail page; charts draw
- if configured: a health check reports up/down, a TLS check reports an expiry
- the collector's pulse in the top bar is alive

**4. Confirm the block is real** rather than assuming the rules applied. From
the same host:

```powershell
Test-NetConnection -ComputerName 1.1.1.1 -Port 443 -InformationLevel Quiet
```

This must return `False`. **If it returns `True` the rules did not take effect
and Check A proved nothing** — a passing test under a firewall that is not
actually blocking is the classic false negative, and it is the single most
likely way this procedure misleads you.

**5. Remove the rules** using the command from step 1.

What this establishes: core monitoring needs your LAN and nothing beyond it.
Opt-in integrations that point outside your network — a webhook to an external
service, for instance — will correctly stop working, because you aimed them
there.

---

## Check B — the connection census

With Prism running normally (no firewall rules needed):

```bash
python tools/verify_lan_only.py --port 5000 --seconds 180
```

It finds the process listening on that port, samples the OS connection table
for the window, and classifies every remote address. `--json` emits the same
data for scripting; `--pid` skips discovery.

| exit | verdict | meaning |
|---|---|---|
| 0 | PASS | connections were observed, none to a public address |
| 2 | FAIL | a routable public address was contacted |
| **3** | **INCONCLUSIVE** | **nothing was observed at all, or every sample failed** |
| 1 | — | no process found (usage error) |

**Treat anything non-zero as "not proven".** Exit 3 exists because a run that
observes nothing looks identical to a run that observes nothing *bad*: a stale
`--pid`, a mistyped `--port`, a missing `netstat`, or a genuinely idle collector
all produce zero rows. Scoring those as a pass would hand you clean-looking
evidence from an instrument that measured nothing — the same failure this tool's
localised-status-column bug produced, and the reason the verdict has three
values instead of two.

**Set `--seconds` to cover at least one full collector poll cycle**, otherwise
you are measuring an idle process. Prism's collector spends most of its time
asleep — a short window can legitimately observe nothing at all, which proves
nothing at all.

### Why three buckets and not two

The tool sorts addresses into LOCAL, NEEDS REVIEW and PUBLIC, and only PUBLIC
fails the run. The middle bucket exists because two ranges genuinely cannot be
judged by a tool:

- **CGNAT (`100.64.0.0/10`)** — carrier-grade NAT space on an ISP network, and
  internal addressing inside some large organisations. Calling it local would
  hide real egress; calling it public would fail the run for everyone using it
  internally. You know which yours is; the tool does not.
- **Multicast** — not a unicast path off your network, but not private
  addressing either. Reported so you can see it.

The buckets are not cosmetic. Python's own `ipaddress` module answers this
question misleadingly in both directions, which is why the classifier does not
use either of its obvious properties:

| address | `is_private` | `is_global` |
|---|---|---|
| `255.255.255.255` (Wake-on-LAN) | True | False |
| `224.0.0.1` (multicast) | False | **True** |
| `100.64.0.1` (CGNAT) | False | **True** |

A check written as `not is_private` reports the Wake-on-LAN broadcast — the one
hardcoded destination in Prism, documented in `docs/DATA_FLOWS.md` — as traffic
leaving your network. A check written as `is_global` reports multicast and CGNAT
the same way. Either would make the tool contradict the document it exists to
support, on its first run. `tests/test_lan_only.py` pins all of it, including
the `172.16.0.0/12` boundary, which ends at `172.31` and is the range people
implement wrongly.

### What Check B cannot see

- **Connections shorter than the sampling interval.** It polls the connection
  table; a session that opens and closes between two samples leaves no trace.
  WinRM calls are short. Lowering `--interval` narrows the gap and cannot close
  it. This is the whole reason Check A exists and is listed first.
- **Which feature made a connection.** It reports that Prism talked to a given
  address and port. `docs/DATA_FLOWS.md` maps ports to features.
- **What a dependency does on its own initiative.** It watches Prism's process,
  so a library's connections *are* counted — but attributing them needs the
  dependency review, not this tool.

### If PowerShell is unavailable

The tool prefers `Get-NetTCPConnection` and falls back to `netstat` when
launching PowerShell is denied, which is normal on a hardened host. It prints a
line saying which sampler it used; the results are equivalent.

Two portability notes, both of which caused the tool to be rewritten:

- **`netstat`'s status column is localised.** A non-English Windows prints its
  own words for LISTENING and ESTABLISHED, so matching the English strings
  matches nothing — and "no rows" renders as a clean pass with zero endpoints.
  The tool therefore identifies a listening socket structurally, by it having
  no peer (remote port 0, unspecified address), which holds in every language.
- **PIDs are matched exactly, not by substring.** Searching a `netstat` line
  for the digits of pid `500` also matches a local port of `5000`, which would
  attribute another process's connections to Prism — the worst available
  failure for a tool whose entire output is who Prism talked to. The PID column
  is parsed as a field and compared as an integer.

---

## Recording the result

For an audit file, keep: the `--json` output of Check B, the `Test-NetConnection`
result from Check A step 4 proving the block was live, and a note of which
Prism version you ran. The JSON contains your own addresses, so treat it with
the same care as any other network inventory.
