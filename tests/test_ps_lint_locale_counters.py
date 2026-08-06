"""B-3 — hardcoded Get-Counter paths cannot work on a localized fleet.

Performance-counter object and counter names follow the target's OS **MUI**
language, not its thread culture, so a host can report `Get-Culture` as en-US
while its counters are German. Measured on a domain controller (BACKLOG.md B-3):

    Get-Counter '\\Memory\\Available MBytes'       -> object not found
    Get-Counter '\\Arbeitsspeicher\\Verfügbare MB'  -> 194 MB

Prism's own collection is unaffected — it uses `Get-CimInstance`, whose property
names are English everywhere. But `Get-Counter` is on the sandbox allowlist, so
operator-authored workflow steps can and will hit this.

The lint is ADVISORY. It must never change whether a script is allowed to run:
an operator targeting an English-locale host is writing correct code.
"""

from __future__ import annotations

import pytest

from ps_sandbox import lint_script, validate_script


def _codes(script):
    return {f["code"] for f in lint_script(script)}


# ── the rule fires ───────────────────────────────────────────────────────

def test_english_counter_path_is_flagged():
    script = "Get-Counter '\\Memory\\Available MBytes' -MaxSamples 1"
    assert "LOCALE_COUNTER_PATH" in _codes(script)


def test_the_flagged_path_is_quoted_back_to_the_operator():
    """A warning that does not say WHICH path is hard to act on."""
    script = "Get-Counter '\\Processor(_Total)\\% Processor Time'"
    message = lint_script(script)[0]["message"]
    assert "Processor" in message
    assert "Get-CimInstance" in message, "must name the fix, not just the fault"


@pytest.mark.parametrize("script", [
    "Get-Counter '\\Memory\\Available MBytes'",
    'Get-Counter "\\PhysicalDisk(_Total)\\Avg. Disk Queue Length"',
    "get-counter '\\System\\Processor Queue Length'",          # case-insensitive
    "$p = '\\Paging File(_Total)\\% Usage'; Get-Counter $p -MaxSamples 2",
])
def test_counter_path_shapes(script):
    assert "LOCALE_COUNTER_PATH" in _codes(script)


def test_a_localized_path_is_flagged_too():
    """The advice is 'do not hardcode', not 'do not write English'.

    A German path is just as broken the moment the workflow runs against an
    English host — which is the normal case for a mixed estate.
    """
    script = "Get-Counter '\\Arbeitsspeicher\\Verfügbare MB'"
    assert "LOCALE_COUNTER_PATH" in _codes(script)


def test_suppressed_errors_turn_a_loud_failure_into_a_silent_one():
    """Get-Counter fails loudly by itself. Paired with silenced errors it
    returns $null, and an empty aggregate reads as a successful empty run —
    which is exactly how B-3 presented before being re-run with -ErrorAction Stop."""
    script = ("$ErrorActionPreference = 'SilentlyContinue'\n"
              "Get-Counter '\\Memory\\Available MBytes'")
    assert _codes(script) == {"LOCALE_COUNTER_PATH", "SILENCED_COUNTER_ERRORS"}


def test_inline_erroraction_silentlycontinue_also_counts():
    script = "Get-Counter '\\Memory\\Available MBytes' -ErrorAction SilentlyContinue"
    assert "SILENCED_COUNTER_ERRORS" in _codes(script)


def test_erroraction_stop_is_not_flagged_as_silent():
    script = "Get-Counter '\\Memory\\Available MBytes' -ErrorAction Stop"
    assert "SILENCED_COUNTER_ERRORS" not in _codes(script)


# ── the rule stays quiet ─────────────────────────────────────────────────

@pytest.mark.parametrize("script", [
    "Get-CimInstance Win32_OperatingSystem",
    "Get-CimInstance Win32_PerfFormattedData_Memory_Memory | Select AvailableMBytes",
    "Get-Service -Name Spooler | Select Status",
    "Get-ChildItem 'C:\\Windows\\Temp' | Measure-Object",   # backslashes, no Get-Counter
    "$ErrorActionPreference = 'SilentlyContinue'; Get-Service Spooler",
    "",
])
def test_no_false_positives(script):
    assert lint_script(script) == [], script


def test_get_counter_without_a_hardcoded_path_is_not_flagged():
    """Resolving the name at runtime is the recommended pattern; do not nag it."""
    script = ("$n = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\"
              "CurrentVersion\\Perflib\\CurrentLanguage').Counter\n"
              "Get-Counter -Counter $resolved -ErrorAction Stop")
    assert "LOCALE_COUNTER_PATH" not in _codes(script)


def test_non_string_input_is_tolerated():
    assert lint_script(None) == []
    assert lint_script(12345) == []


# ── advisory, never a gate ───────────────────────────────────────────────

def test_lint_does_not_change_validity():
    """The whole point: this must not become a second, quieter allowlist."""
    script = "Get-Counter '\\Memory\\Available MBytes' -MaxSamples 1"

    ok, reason = validate_script(script)

    assert ok is True, reason
    assert lint_script(script), "and yet it still warns"


def test_validate_script_still_rejects_what_it_always_rejected():
    ok, _reason = validate_script("Remove-Item C:\\Windows -Recurse")
    assert ok is False
