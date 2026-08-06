"""Lightweight contract tests for the PS_COLLECT_LOGS PowerShell payload.

We don't run PowerShell here (no Windows runtime in CI). Instead we
verify the string contains the channels we expect to query and the
events we expect to filter out. That's enough to prevent silent
regressions like "someone removed Firewall from the channel list."

If we ever do invest in real PowerShell-against-a-mock-WinRM tests,
this file should grow. For now: text checks only.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def ps_logs() -> str:
    from collector_v2.scripts import PS_COLLECT_LOGS
    return PS_COLLECT_LOGS


# ─────────────────────────────────────────────────────────────────────
# Channel coverage — the four sources the UI expects
# ─────────────────────────────────────────────────────────────────────

def test_ps_collect_logs_queries_system_channel(ps_logs):
    assert "'System'" in ps_logs, (
        "System channel removed from PS_COLLECT_LOGS — Windows Logs "
        "panel will lose its core event stream."
    )


def test_ps_collect_logs_queries_application_channel(ps_logs):
    assert "'Application'" in ps_logs


def test_ps_collect_logs_queries_security_channel(ps_logs):
    assert "'Security'" in ps_logs


def test_ps_collect_logs_queries_firewall_channel(ps_logs):
    """The Microsoft-Windows-Windows Firewall channel must be queried so
    the new Firewall Logs section in server_detail.html has data."""
    assert "Microsoft-Windows-Windows Firewall With Advanced Security/Firewall" in ps_logs


def test_ps_collect_logs_emits_firewall_as_display_source(ps_logs):
    """Rows from the firewall channel must surface with source='Firewall'
    (not the long channel name) so the frontend's ``?source=Firewall``
    filter works."""
    assert "display = 'Firewall'" in ps_logs


# ─────────────────────────────────────────────────────────────────────
# Packet-noise suppression — keep firewall logs operationally useful
# ─────────────────────────────────────────────────────────────────────

def test_ps_collect_logs_excludes_firewall_packet_noise(ps_logs):
    """Event IDs 5152 (dropped packet) and 5153 (allowed packet) fire
    hundreds of times per minute on busy servers. If they were ingested,
    they'd drown out rule changes / service stops / blocked apps. Verify
    the filter list explicitly contains them."""
    assert "5152" in ps_logs, "5152 missing from FIREWALL_NOISE_IDS"
    assert "5153" in ps_logs, "5153 missing from FIREWALL_NOISE_IDS"
    assert "FIREWALL_NOISE_IDS" in ps_logs


# ─────────────────────────────────────────────────────────────────────
# Schema contract — DB writer expects these exact field names
# ─────────────────────────────────────────────────────────────────────

def test_ps_collect_logs_emits_required_fields(ps_logs):
    """The aggregator's _handle_logs_result reads source / time / level /
    event_id / message from each row. Renaming any of these silently
    breaks ingestion."""
    for required in ("source ", "time ", "level ", "event_id ", "message "):
        assert required in ps_logs, f"Required field {required!r} missing from PS_COLLECT_LOGS"


def test_ps_collect_logs_truncates_message_to_200_chars(ps_logs):
    """Long messages would bloat the DB. The PS payload truncates to
    200 chars before sending. Pin that limit."""
    assert "Substring(0,200)" in ps_logs
