"""PowerShell script sanitiser for the workflow engine.

Workflow `run_powershell` and `condition` blocks let users author arbitrary
scripts that execute as the WinRM service account on remote servers — which
in practice is "remote code execution as a feature". This module enforces a
DEFAULT-DENY allowlist for cmdlets/keywords that may appear inside those
user-authored scripts.

Design choices
--------------
* Allowlist > blocklist. Operators add what they need. Anything not on the
  list is rejected — there's no "we forgot to ban Invoke-Expression" failure
  mode.
* Pure regex tokenisation. We don't parse PowerShell (Roslyn-equivalent
  parsers are out of scope). The sandbox only inspects identifier-like
  tokens and rejects suspicious operators (`&`, `iex`, `Invoke-Expression`,
  `[Reflection.Assembly]::Load`, `Add-Type`, etc.).
* Per-deployment overrides. Settings → Workflows → "Allowed cmdlets" lets
  ops edit the allowlist without code changes.
* Honour a kill-switch. When `workflows.sandbox_enabled = false` (advanced
  setting, off by default), pre-existing scripts continue to run untouched.
  Emit a loud warning so operators know the safety net is disabled.

The module is intentionally single-purpose and dependency-free so it can be
imported by both the runtime executor and a /api/workflows/validate endpoint.

## Known limitations and the proper fix

This sandbox is a regex tokeniser, not a PowerShell parser. A determined user
who knows PowerShell's surface area can defeat it. The classes of bypass we
know about (audit 2026-05, finding B1):

1. **String concatenation through the call operator**. Example::

       & ('I'+'nvoke-Expression') 'whoami'

   The literal `nvoke-Expression` is not a HARD_DENY hit, the verb `nvoke`
   is not in PS_VERBS, and there is no HARD_DENY rule for the bare `&`.

2. **Char-code reconstruction**. Example::

       & ([char]105+[char]101+[char]120) 'whoami'

   The string `iex` never appears literally, so `\\biex\\b` does not match.

3. **Backtick escapes**. Example::

       In`voke-Expression 'whoami'

   PowerShell's parser strips the backtick before execution, but the regex
   sees `In` and `voke-Expression` as two separate tokens.

4. **Allowlisted aliases give arbitrary file read**. ``gc``, ``cat``,
   ``Get-Content``, ``gci``, ``ls``, ``dir`` are on the default allowlist
   (they are needed for legitimate diagnostic workflows) but accept
   arbitrary paths -- there is no path allowlist.

5. **Method/property chains are not inspected**. ``(Get-Date).GetType()
   .Assembly.GetType('System.Diagnostics.Process').GetMethod('Start',...)
   .Invoke(...)`` reaches arbitrary process spawn even though
   ``Start-Process`` is hard-denied.

### What we shipped instead (Sprint 1, 2026-05)

* For block executors that take **structured user input** (service name,
  process name, drive letter, etc.), ``workflow_engine`` now uses pypsrp
  parameter binding (``add_script(...).add_parameter(...)``) rather than
  f-string interpolation. PowerShell's parameter binder treats bound
  values as typed inputs, never re-parsed as code, so attacker-controlled
  service/process/drive strings cannot escape into the script body. This
  makes the regex sandbox irrelevant for those blocks.

* For the two blocks that genuinely take **free-form scripts** --
  ``run_powershell`` and ``condition`` -- this regex sandbox remains the
  only line of defence. Operators should treat any workflow that uses
  those blocks as requiring dual-control admin approval.

### The proper longer-term fix (Sprint 2/3)

The honest fix is **PowerShell Constrained Language Mode + JEA endpoints**
on every WinRM target. CLM disables the .NET reflection surface that makes
bypasses 1-3 and 5 work; JEA caps which cmdlets can run and with which
parameter shapes regardless of what the caller sends. That rollout is
per-server-config work outside this sprint. See
``docs/WORKFLOW_SANDBOX.md`` for the operator-facing rollout sketch.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Tuple

logger = logging.getLogger("prism.ps_sandbox")


# Cmdlets known to be safe for read-only / mild-control workflow steps.
# Keep the list small and conservative; operators can extend per-deployment.
DEFAULT_ALLOWED_CMDLETS: frozenset[str] = frozenset(
    name.lower() for name in [
        # Service inspection / control
        "Get-Service", "Restart-Service", "Start-Service", "Stop-Service",
        "Set-Service",
        # Process inspection (no Start-Process — rejected)
        "Get-Process",
        # Filesystem read
        "Get-ChildItem", "Get-Item", "Get-Content", "Test-Path",
        "Get-PSDrive", "Get-Volume",
        # System inspection
        "Get-ComputerInfo", "Get-WmiObject", "Get-CimInstance",
        "Get-EventLog", "Get-WinEvent", "Get-HotFix", "Get-Date",
        "Get-Counter", "Get-NetAdapter", "Get-NetIPAddress",
        # Azure AD Connect / AD-sync — operators commonly build workflows
        # around forcing a delta sync after a Group Policy / object change.
        # These are scoped to the AAD sync engine, not arbitrary AD writes:
        #   Start-ADSyncSyncCycle           — trigger Delta/Initial sync
        #   Get-ADSyncConnectorRunStatus    — is a sync currently running?
        #   Get-ADSyncScheduler             — current scheduler config
        # Adding read-only AD account/group lookups too because workflows
        # often need them as preflight checks ("does user X exist before
        # we (something)?"). Writes (New-ADUser / Set-ADUser / etc.) are
        # intentionally NOT here — keep them out unless explicitly needed.
        "Start-ADSyncSyncCycle", "Get-ADSyncConnectorRunStatus",
        "Get-ADSyncScheduler",
        "Get-ADUser", "Get-ADGroup", "Get-ADComputer",
        "Get-ADGroupMember", "Get-ADDomain", "Get-ADDomainController",
        "Get-ADOrganizationalUnit", "Get-ADReplicationFailure",
        "Get-ADReplicationPartnerMetadata",
        # Network diagnostics — common in "is this server reachable from
        # itself" / "can it resolve DNS" / "is firewall blocking X"
        # troubleshooting workflows. All read-only or probe-only.
        "Test-Connection", "Test-NetConnection", "Resolve-DnsName",
        "Get-NetTCPConnection", "Get-NetUDPEndpoint", "Get-NetRoute",
        "Get-NetIPInterface", "Get-DnsClientServerAddress",
        "Get-NetFirewallProfile", "Get-NetFirewallRule",
        # Restart-NetAdapter is the only network modify cmdlet here —
        # operators sometimes scripted "kick the NIC" routines and it's
        # less dangerous than Restart-Computer (already HARD_DENY).
        "Restart-NetAdapter",
        # Storage / disk read — for ad-hoc capacity audits, SMB share
        # inspection, snapshotting which disks a VM has.
        "Get-PhysicalDisk", "Get-Disk", "Get-Partition",
        "Get-StoragePool", "Get-SmbShare", "Get-SmbSession",
        "Get-FileShare",
        # Scheduled tasks — checking whether a job ran / has the right
        # last-result. Read-only.
        "Get-ScheduledTask", "Get-ScheduledTaskInfo",
        # Local user / group inspection — common for "is account X in
        # local Administrators?" preflight checks. Read-only.
        "Get-LocalUser", "Get-LocalGroup", "Get-LocalGroupMember",
        # Print server — checking queue state on print servers.
        "Get-Printer", "Get-PrintJob",
        # Group Policy — `Get-GPResultantSetOfPolicy` is the cmdlet form
        # of `gpresult /r`. Useful diagnostic on misconfigured servers.
        "Get-GPO", "Get-GPResultantSetOfPolicy",
        # System uptime / general health
        "Get-Uptime",
        # IIS / web hosting (only useful on servers with the IIS PS module
        # installed; harmless on others — sandbox check is static-string).
        "Get-Website", "Get-WebAppPool",
        # Failover cluster — checking cluster role state on Hyper-V or
        # SQL Always-On hosts. Read-only.
        "Get-Cluster", "Get-ClusterNode", "Get-ClusterGroup",
        "Get-ClusterResource",
        # Hyper-V — read-only VM inventory / state.
        "Get-VM", "Get-VMHost", "Get-VMSwitch",
        # Plumbing
        "Where-Object", "Select-Object", "ForEach-Object", "Measure-Object",
        "Sort-Object", "Group-Object", "ConvertTo-Json", "ConvertFrom-Json",
        "Out-String", "Format-List", "Format-Table", "Write-Output",
        "Write-Host",
        # Common aliases (PowerShell allows aliases in user input)
        "gci", "ls", "dir", "gc", "cat", "where", "select", "foreach",
    ]
)

# Tokens that are NEVER allowed regardless of allowlist. These bypass the
# allowlist entirely (e.g. `iex` is an alias for Invoke-Expression which can
# execute arbitrary user-supplied strings, defeating the whole sandbox).
HARD_DENY: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in [
    r"\bInvoke-Expression\b", r"\biex\b",
    r"\bAdd-Type\b",
    r"\bNew-Object\s+System\.Net",        # WebClient, Sockets, …
    r"\bInvoke-WebRequest\b", r"\biwr\b",
    r"\bInvoke-RestMethod\b", r"\birm\b",
    r"\bStart-Process\b", r"\bsaps\b",
    r"\bStop-Computer\b", r"\bRestart-Computer\b",
    r"\bRemove-Item\b", r"\bri\b\s",
    r"\bSet-ExecutionPolicy\b",
    r"\[(System\.)?Reflection\.Assembly\]",
    r"\[(System\.)?Diagnostics\.Process\]",
    r"::FromBase64String\b",
    r"-EncodedCommand\b",
    # Process tampering
    r"\bStop-Process\b\s+-Name\s+['\"]?(MsMpEng|wuauserv|prism|sysmon)",
    # Privilege escalation / token games
    r"\bImpersonate", r"\bSeTcbPrivilege\b",
    # Powershell self-invocation
    r"\bpowershell(\.exe)?\b\s+-(c|enc|ec|noprofile)",
])


_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9-]+)\b")


def normalize_allowlist(extra: Iterable[str] | None) -> frozenset[str]:
    """Combine DEFAULT_ALLOWED_CMDLETS with user-supplied extras (case-insensitive)."""
    if not extra:
        return DEFAULT_ALLOWED_CMDLETS
    extras_lower = {str(x).strip().lower() for x in extra if str(x).strip()}
    return DEFAULT_ALLOWED_CMDLETS | frozenset(extras_lower)


def validate_script(script: str, *, allowed_cmdlets: Iterable[str] | None = None,
                    enabled: bool = True) -> Tuple[bool, str]:
    """Return (ok, reason). When `enabled=False`, always returns (True, '').

    The function is intentionally cheap — every call goes through two regex
    passes and one tokeniser pass over the script body. Callers MUST also
    enforce a maximum script length upstream (we don't do that here so the
    rule lives in one place: the API layer).
    """
    if not enabled:
        return True, ""

    if not isinstance(script, str):
        return False, "Script must be a string"

    if not script.strip():
        return False, "Empty script"

    # Hard-deny patterns — checked first so they always trump the allowlist
    for pat in HARD_DENY:
        m = pat.search(script)
        if m:
            return False, f"Disallowed token: {m.group(0)!r}"

    allowlist = normalize_allowlist(allowed_cmdlets)

    # Identifiers that look like cmdlets (Verb-Noun) MUST be on the allowlist.
    # We treat any single token containing a dash and starting with a known
    # PowerShell verb as a cmdlet candidate. Variables ($foo), keywords
    # (if/else/foreach), and operators are ignored.
    PS_VERBS = {
        "get", "set", "new", "remove", "add", "test", "start", "stop",
        "restart", "invoke", "out", "import", "export", "select", "where",
        "foreach", "sort", "group", "measure", "convertto", "convertfrom",
        "format", "write", "read", "enable", "disable", "register",
        "unregister", "clear", "show", "hide", "find", "search", "resolve",
        "update", "install", "uninstall", "lock", "unlock", "checkpoint",
        "wait",
    }
    for tok in _TOKEN_RE.findall(script):
        if "-" not in tok:
            continue
        verb = tok.split("-", 1)[0].lower()
        if verb not in PS_VERBS:
            continue  # Probably a hyphenated identifier, not a cmdlet
        if tok.lower() not in allowlist:
            return False, f"Cmdlet {tok!r} is not on the allowlist"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────
# Lint — advisory only, never blocks. See docs/plans/BACKLOG.md B-3.
#
# validate_script answers "is this allowed to run". These answer "is this
# likely to do what you meant", which is a different question and must not
# share an exit code with it: an operator targeting an English-locale host is
# entitled to write English counter paths.
# ─────────────────────────────────────────────────────────────────────────

# A quoted counter path: '\Object\Counter' or "\Object(inst)\Counter".
_COUNTER_PATH_RE = re.compile(r"""['"]\s*\\\\?[A-Za-z][^'"\\]*\\[^'"]*['"]""")
_GET_COUNTER_RE = re.compile(r"\bGet-Counter\b", re.IGNORECASE)
_SILENCED_ERRORS_RE = re.compile(
    r"\$ErrorActionPreference\s*=\s*['\"]?SilentlyContinue|-ErrorAction\s+SilentlyContinue",
    re.IGNORECASE)


def lint_script(script: str) -> list[dict]:
    """Non-blocking advisories about a workflow script.

    Returns ``[{"code": ..., "message": ...}]`` — empty when nothing to say.

    **LOCALE_COUNTER_PATH.** Performance-counter object and counter names are
    localized, and they follow the OS *MUI* language rather than the session's
    thread culture — so a box can report `Get-Culture` as en-US while its
    counters are German. Measured on a domain controller (BACKLOG.md B-3):

        Get-Counter '\\Memory\\Available MBytes'      -> object not found
        Get-Counter '\\Arbeitsspeicher\\Verfügbare MB' -> 194 MB

    Prism's own collection is unaffected (it uses `Get-CimInstance`, whose
    PROPERTY names are English on every locale). This only bites
    operator-authored workflow steps, and `Get-Counter` is on the allowlist, so
    they can and will write them.

    **SILENCED_COUNTER_ERRORS.** `Get-Counter` fails loudly on its own. It only
    becomes silent when paired with suppressed errors — the script then gets
    `$null`, aggregates over nothing, and renders an empty table that reads as
    "collected fine, nothing to report". That is exactly how B-3 presented
    before it was re-run with `-ErrorAction Stop`.
    """
    if not isinstance(script, str) or not script.strip():
        return []

    findings: list[dict] = []
    if _GET_COUNTER_RE.search(script):
        paths = _COUNTER_PATH_RE.findall(script)
        if paths:
            findings.append({
                "code": "LOCALE_COUNTER_PATH",
                "message": (
                    "Hardcoded Get-Counter path(s) "
                    + ", ".join(sorted(set(p.strip() for p in paths))[:3])
                    + ". Counter names follow the target's OS MUI language, not "
                      "its thread culture, so this fails on a non-English host. "
                      "Prefer Get-CimInstance Win32_PerfFormattedData_*, or "
                      "resolve the name from the Perflib registry index."),
            })
        if _SILENCED_ERRORS_RE.search(script):
            findings.append({
                "code": "SILENCED_COUNTER_ERRORS",
                "message": (
                    "Get-Counter combined with suppressed errors. A locale "
                    "mismatch will yield $null instead of an error, and an "
                    "empty result reads like a successful empty collection. "
                    "Use -ErrorAction Stop so the failure is visible."),
            })
    return findings


def get_sandbox_settings(settings: dict) -> Tuple[bool, list[str], int]:
    """Read the workflow sandbox config block out of settings.

    Returns (enabled, allowed_extras, max_script_chars).
    """
    wf = (settings or {}).get("workflows", {}) or {}
    sandbox = wf.get("sandbox", {}) or {}
    enabled = bool(sandbox.get("enabled", True))
    extras = sandbox.get("allowed_cmdlets", []) or []
    if isinstance(extras, str):
        extras = [s.strip() for s in extras.split(",") if s.strip()]
    max_len = int(sandbox.get("max_script_chars", 4000))
    return enabled, list(extras), max_len
