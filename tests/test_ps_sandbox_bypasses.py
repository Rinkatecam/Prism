"""The sandbox bypasses its own docstring admitted (collector audit finding 3).

THE FINDING, in the auditor's words: the workflow free-form PowerShell sandbox
is bypassable, and `ps_sandbox.py`'s own documentation listed working bypasses.
A limited-RBAC operator who could author a workflow therefore got remote code
execution as the WinRM service account.

TWO FIXES, and the order matters because only one of them is structural.

**The structural one** is not in this file: authoring a workflow that reaches
WinRM now requires the same per-server admin permission that executing it
already required (tests/test_workflow_authoring_rbac.py). That closes the
ESCALATION, which is the part a regex can never close — and it had to be closed
at authoring time, because a SCHEDULED workflow executes with no user present
and therefore no permission to check. Someone who already holds admin on a box
gains nothing by defeating a regex about that box.

**This file covers the second**: the sandbox should still bite. It remains a
tokeniser and not a PowerShell parser, so it is defence in depth and is
described as such. What changed:

  * the script is NORMALISED before matching, so PowerShell's backtick escape
    cannot split a denied identifier in half;
  * DYNAMIC INVOCATION is denied outright — the call operator and dot-sourcing
    applied to anything that is not a bare identifier. That is what kills the
    whole family of "compute the name of the thing you want to run" bypasses at
    once, rather than chasing each spelling of `Invoke-Expression`;
  * the REFLECTION SURFACE is denied — `.GetType(`, `.Assembly`, `.Invoke(`,
    `[scriptblock]::Create`, `[char]` — which is how a chain starting from an
    allowlisted cmdlet reached arbitrary process spawn.

WHAT IS STILL NOT CLOSED, deliberately and on the record: allowlisted cmdlets
(`Get-Content`, `Get-ChildItem`) accept arbitrary paths, so a free-form script
can read any file the service account can. A path allowlist would break the
diagnostic workflows those cmdlets exist for, and after the authoring gate the
only people who can write such a script are people with admin on that server,
who can read those files anyway. The honest full fix is still Constrained
Language Mode plus JEA on the targets (docs/WORKFLOW_SANDBOX.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ps_sandbox import validate_script                    # noqa: E402


def _rejected(script: str) -> str:
    ok, reason = validate_script(script)
    assert ok is False, f"the sandbox accepted: {script!r}"
    return reason


def _accepted(script: str) -> None:
    ok, reason = validate_script(script)
    assert ok is True, f"the sandbox rejected a legitimate script: {reason}"


# ── bypass 1: the call operator with a computed name ──────────────────────

def test_the_call_operator_on_a_concatenated_string_is_rejected():
    """`& ('I'+'nvoke-Expression')` — the literal never appears, the verb is
    not a verb, and there was no rule for a bare `&`."""
    _rejected("& ('I'+'nvoke-Expression') 'whoami'")


def test_the_call_operator_on_a_variable_is_rejected():
    _rejected("$a = 'something'\n& $a 'whoami'")


def test_the_call_operator_on_a_quoted_string_is_rejected():
    _rejected('& "Get-Service"')


def test_dot_sourcing_a_computed_value_is_rejected():
    _rejected("$a = 'x'\n. $a")


def test_dot_sourcing_after_a_semicolon_is_rejected():
    """The line-start anchor alone would miss it; a statement separator is just
    as good a place to start a statement."""
    _rejected("Get-Service ; . ('who'+'ami')")


# ── bypass 2: character-code reconstruction ───────────────────────────────

def test_char_code_reconstruction_is_rejected():
    """`& ([char]105+[char]101+[char]120)` spells `iex` without writing it."""
    _rejected("& ([char]105+[char]101+[char]120) 'whoami'")


def test_the_char_cast_alone_is_rejected():
    """Denied on its own, not only in an invocation position: the cast has no
    legitimate use in a diagnostic workflow and is the raw material for every
    spelling-based bypass."""
    _rejected("$x = [char]65")


def test_a_base64_decode_is_rejected():
    _rejected("[System.Convert]::FromBase64String('d2hvYW1p')")


# ── bypass 3: backtick escapes ────────────────────────────────────────────

def test_a_backtick_split_identifier_is_rejected():
    """PowerShell strips the backtick before execution; the tokeniser used to
    see two harmless halves. Normalising first is the whole fix."""
    _rejected("In`voke-Expression 'whoami'")


def test_a_backtick_split_alias_is_rejected():
    _rejected("i`ex 'whoami'")


def test_a_backtick_split_denied_cmdlet_is_rejected():
    _rejected("Start`-Process notepad")


def test_a_backtick_split_off_allowlist_cmdlet_is_rejected():
    """The same normalisation, reaching the ALLOWLIST rather than a hard-deny
    pattern. `Set-ADUser` is not hard-denied — it is simply not allowlisted — so
    only the tokeniser can catch it, and only if the tokeniser reads the
    normalised text. Without this case, a mutation that points the tokeniser
    back at the raw script lands cleanly and every test still passes, because
    every other backtick case happens to be caught by a hard-deny rule first.
    """
    _rejected("Set`-ADUser -Identity bob -Enabled $false")


# ── bypass 5: method and property chains ──────────────────────────────────

def test_a_reflection_chain_from_an_allowlisted_cmdlet_is_rejected():
    """The audit's example: start at `Get-Date`, end at arbitrary process
    spawn, never mentioning `Start-Process`."""
    _rejected("(Get-Date).GetType().Assembly.GetType('System.Diagnostics.Process')"
              ".GetMethod('Start').Invoke($null, @())")


def test_a_scriptblock_create_is_rejected():
    _rejected("[scriptblock]::Create('whoami')")


def test_an_invoke_method_call_is_rejected():
    _rejected("$sb.Invoke()")


def test_a_gettype_call_is_rejected():
    _rejected("(Get-Service).GetType()")


# ── the sandbox must still let real work through ──────────────────────────

def test_a_plain_diagnostic_script_is_still_accepted():
    _accepted("Get-Service -Name Spooler | Select-Object Status, Name")


def test_a_pipeline_with_a_property_access_is_still_accepted():
    """Property access is not method invocation. Denying every `.` would make
    the sandbox useless for its actual purpose."""
    _accepted("(Get-Service -Name Spooler).Status")


def test_a_multi_line_script_with_a_variable_is_still_accepted():
    _accepted("$svc = Get-Service -Name Spooler\n"
              "if ($svc.Status -ne 'Running') { Restart-Service -Name Spooler }")


def test_a_where_object_filter_is_still_accepted():
    _accepted("Get-ChildItem C:\\Temp | Where-Object { $_.Length -gt 1000 } "
              "| Measure-Object -Sum Length")


def test_a_hyphenated_non_verb_word_is_still_accepted():
    """A hyphenated identifier whose first part is not a PowerShell verb is not
    a cmdlet candidate, and must not be treated as one."""
    _accepted("# double-check the spooler\nGet-Service -Name Spooler")


def test_a_verb_hyphenated_word_in_a_COMMENT_is_rejected_and_that_is_deliberate():
    """A known and accepted false positive, recorded rather than fixed.

    `# read-only check` is rejected because `read` is a PowerShell verb and the
    tokeniser does not know it is inside a comment. The obvious fix — strip
    everything from `#` to end of line before matching — WEAKENS the gate,
    because `#` also occurs inside string literals:

        $x = "#"; & ($x)

    Stripping from that `#` would delete the actual invocation from the text
    being inspected, turning a comment-handling nicety into a bypass. For a
    default-deny gate the correct trade is to keep the false positive: the cost
    is rewording a comment, and the alternative cost is remote code execution.
    """
    ok, reason = validate_script("# read-only check\nGet-Service -Name Spooler")
    assert ok is False
    assert "read-only" in reason, "the message does not name what to reword"


def test_the_kill_switch_still_bypasses_everything():
    """`workflows.sandbox_enabled = false` is a documented escape hatch for
    pre-existing scripts. Hardening must not quietly take it away — an operator
    who turned the safety net off is entitled to have it stay off."""
    ok, _ = validate_script("& ('I'+'nvoke-Expression') 'whoami'", enabled=False)
    assert ok is True


# ── the reason has to be usable ───────────────────────────────────────────

@pytest.mark.parametrize("script", [
    "& ('I'+'nvoke-Expression') 'whoami'",
    "In`voke-Expression 'whoami'",
    "(Get-Date).GetType()",
    "[scriptblock]::Create('whoami')",
])
def test_a_rejection_says_what_it_objected_to(script):
    """A sandbox that says "no" without saying to what is a sandbox operators
    switch off."""
    reason = _rejected(script)
    assert reason.strip()
    assert reason.lower() != "empty script"
