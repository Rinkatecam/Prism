"""Regression guard: disk metrics must be restricted to FIXED local disks.

A DVD/optical drive (Win32_LogicalDisk DriveType=5) or removable media
(DriveType=2) mounted at C:/D: reports FreeSpace=0 on a read-only disc, which
the old unfiltered query turned into 100% "used" — a permanent false CRITICAL
for a drive that isn't real server storage (the case where a fleet host ships a
DVD drive at D:). Both the metrics and hardware scripts must carry `DriveType=3` so
optical/removable drives fall through to -1 (which detection skips).
"""

from __future__ import annotations

import re

from collector_v2.scripts import PS_COLLECT_SCRIPT, PS_HARDWARE_SCRIPT


def _drive_queries(script: str) -> list[str]:
    """Every Win32_LogicalDisk -Filter "..." clause in a script."""
    return re.findall(r'Win32_LogicalDisk\s+-Filter\s+"([^"]+)"', script)


def test_metrics_script_filters_fixed_disks_only():
    filters = _drive_queries(PS_COLLECT_SCRIPT)
    assert filters, "expected Win32_LogicalDisk drive queries in PS_COLLECT_SCRIPT"
    for f in filters:
        assert "DriveType=3" in f, (
            f"drive query {f!r} must restrict to DriveType=3 (fixed disks) so "
            f"optical/removable media can't produce a false critical"
        )


def test_hardware_script_filters_fixed_disks_only():
    filters = _drive_queries(PS_HARDWARE_SCRIPT)
    assert filters, "expected Win32_LogicalDisk drive queries in PS_HARDWARE_SCRIPT"
    for f in filters:
        assert "DriveType=3" in f


def test_both_c_and_d_are_filtered():
    # Both drive letters must carry the filter — a DVD can be mounted at either.
    for script in (PS_COLLECT_SCRIPT, PS_HARDWARE_SCRIPT):
        filters = _drive_queries(script)
        assert any("DeviceID='C:'" in f for f in filters)
        assert any("DeviceID='D:'" in f for f in filters)
        assert all("DriveType=3" in f for f in filters)
