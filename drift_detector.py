"""Config drift detection for Prism. Collects system configuration snapshots
and detects changes between collection cycles."""

import json
import re
import logging

logger = logging.getLogger("prism.drift")

# ── PowerShell snapshot scripts ──────────────────────────────────────

PS_SNAPSHOT_SERVICES = (
    "Get-Service | Select-Object Name, Status, StartType | ConvertTo-Json -Compress"
)

PS_SNAPSHOT_HOTFIXES = (
    "Get-HotFix | Select-Object HotFixID, InstalledOn, Description | ConvertTo-Json -Compress"
)

PS_SNAPSHOT_FIREWALL = (
    "Get-NetFirewallRule -Enabled True -ErrorAction SilentlyContinue "
    "| Select-Object Name, Direction, Action, Profile | ConvertTo-Json -Compress"
)

PS_SNAPSHOT_LOCAL_ADMINS = r"""
$members = ([ADSI]'WinNT://./Administrators,group').Members() | ForEach-Object {
    $_.GetType().InvokeMember('Name','GetProperty',$null,$_,$null)
}
@($members | ForEach-Object { @{ Name = $_ } }) | ConvertTo-Json -Compress
"""

PS_SNAPSHOT_SCHEDULED_TASKS = (
    "Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } "
    "| Select-Object TaskName, State | ConvertTo-Json -Compress"
)

# Map snapshot type name -> (PS script, key field)
SNAPSHOT_TYPES = {
    "services":        (PS_SNAPSHOT_SERVICES, "Name"),
    "hotfixes":        (PS_SNAPSHOT_HOTFIXES, "HotFixID"),
    "firewall":        (PS_SNAPSHOT_FIREWALL, "Name"),
    "local_admins":    (PS_SNAPSHOT_LOCAL_ADMINS, "Name"),
    "scheduled_tasks": (PS_SNAPSHOT_SCHEDULED_TASKS, "TaskName"),
}


def collect_snapshot(pool, snapshot_type: str) -> list[dict]:
    """Run the appropriate PS script on a WinRM RunspacePool and return parsed JSON."""
    from pypsrp.powershell import PowerShell

    if snapshot_type not in SNAPSHOT_TYPES:
        logger.warning("Unknown snapshot type: %s", snapshot_type)
        return []

    script, _key_field = SNAPSHOT_TYPES[snapshot_type]

    ps = PowerShell(pool)
    ps.add_script(script)
    output = ps.invoke()

    if ps.had_errors:
        err_msgs = [str(e) for e in ps.streams.error]
        logger.warning("Snapshot %s had errors: %s", snapshot_type, "; ".join(err_msgs)[:200])
        if not output:
            return []

    stdout = str(output[0]) if output else "[]"
    if not stdout.strip():
        return []

    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Cannot parse snapshot %s output: %s", snapshot_type, stdout[:200])
        return []

    # PowerShell may return a single dict for one result
    if isinstance(data, dict):
        data = [data]

    return data


def diff_snapshots(old_data: list, new_data: list, key_field: str) -> list[dict]:
    """Compare two lists of dicts by key_field.

    Returns list of changes:
      {change_type: 'added'|'removed'|'modified', key, field?, old_value?, new_value?}
    """
    old_map = {str(item.get(key_field, "")): item for item in (old_data or [])}
    new_map = {str(item.get(key_field, "")): item for item in (new_data or [])}

    changes = []

    # Added items
    for key in new_map:
        if key not in old_map:
            changes.append({
                "change_type": "added",
                "key": key,
                "field": key_field,
                "old_value": None,
                "new_value": key,
            })

    # Removed items
    for key in old_map:
        if key not in new_map:
            changes.append({
                "change_type": "removed",
                "key": key,
                "field": key_field,
                "old_value": key,
                "new_value": None,
            })

    # Modified items
    for key in new_map:
        if key in old_map:
            old_item = old_map[key]
            new_item = new_map[key]
            all_fields = set(old_item.keys()) | set(new_item.keys())
            for field in all_fields:
                if field == key_field:
                    continue
                old_val = str(old_item.get(field, ""))
                new_val = str(new_item.get(field, ""))
                if old_val != new_val:
                    changes.append({
                        "change_type": "modified",
                        "key": key,
                        "field": field,
                        "old_value": old_val,
                        "new_value": new_val,
                    })

    return changes


def collect_all_snapshots(pool, enabled_types: list[str] | None = None) -> dict[str, list]:
    """Run all enabled snapshot types and return {type: data}."""
    if enabled_types is None:
        enabled_types = list(SNAPSHOT_TYPES.keys())

    results = {}
    for snap_type in enabled_types:
        if snap_type not in SNAPSHOT_TYPES:
            continue
        try:
            data = collect_snapshot(pool, snap_type)
            results[snap_type] = data
        except Exception:
            logger.exception("Failed to collect snapshot: %s", snap_type)
            results[snap_type] = []

    return results


def redact_sensitive(text: str, patterns: list[str]) -> str:
    """Apply regex patterns to replace matches with [REDACTED]."""
    if not patterns or not text:
        return text
    for pattern in patterns:
        try:
            text = re.sub(pattern, "[REDACTED]", text)
        except re.error:
            logger.warning("Invalid redaction pattern: %s", pattern)
    return text
