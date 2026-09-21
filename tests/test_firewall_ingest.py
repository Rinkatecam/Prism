"""Firewall events survive the journey from the wire to the table — WP-5.

`tests/test_firewall_logs_endpoint.py` has passed since the panel shipped. It
inserts rows tagged `log_source='Firewall'` and asserts the endpoint returns
them, which it does. What it cannot notice is that **no such row can ever
arrive**: the live database held zero of them while the UI promised "Policy
changes, blocked apps, and service state changes will appear here."

A test that seeds the state it verifies can only tell you the reader works. It
says nothing about whether the writer can produce that state. This file tests
the write path.

THREE MECHANISMS DESTROYED THEM, all verified against the running system:

  A. **The cap.** `max_rows_per_check` was 100, sized when the collection
     script emitted 30 rows from three channels. There are four now, so it
     emits up to 120 and the last channel is truncated. Firewall is last.
     Measured: 10 of 30 surviving.

  B. **The allowlist.** Every operational firewall event is Level 4, which the
     script maps to `Information`, and `insert_logs` drops Information unless
     `"Source/EventID"` is allowlisted. The default allowlist was five
     entries, all `System/*`. Nothing firewall-shaped could pass it — which is
     why the count was zero rather than merely low.

  C. **The noise filter was on the wrong channel.** 5152/5153 are Filtering
     Platform events; Windows writes them to the SECURITY log. The filter that
     drops them was gated on the Firewall channel, where they never appear.

WHAT THIS IS BLIND TO: whether a real Windows host emits these events at all.
That depends on the host's audit policy, not on Prism, and it is asserted
nowhere here.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections import Counter

import pytest


def _channel_rows(source: str, n: int, level: str, event_id: int) -> list[dict]:
    return [{"source": source, "time": "2026-08-26T10:00:00Z", "level": level,
             "event_id": event_id, "message": f"{source} row"} for _ in range(n)]


def _full_batch() -> list[dict]:
    """What the collection script emits on a busy host: 30 per channel, in
    the order `$channels` declares them, with Firewall last."""
    return (_channel_rows("System", 30, "Error", 7001)
            + _channel_rows("Application", 30, "Error", 1000)
            + _channel_rows("Security", 30, "Error", 4625)
            + _channel_rows("Firewall", 30, "Information", 2004))


# ── A: the cap ────────────────────────────────────────────────────────────

def test_the_row_cap_is_large_enough_for_every_channel():
    """The script emits 30 per channel across four channels. A cap below 120
    silently truncates whichever channel is declared last."""
    import ingest_caps
    assert ingest_caps.DEFAULTS["max_rows_per_check"] >= 120, (
        "the cap is below what one collection emits, so the last channel is "
        "clipped — and the last channel is Firewall")


def test_the_cap_does_not_starve_the_last_channel():
    import ingest_caps
    kept = Counter(r["source"] for r in ingest_caps.cap_log_rows(_full_batch(), server="T"))
    assert kept["Firewall"] == 30, (
        f"Firewall kept {kept['Firewall']} of 30 rows; the cap is clipping the "
        f"last channel: {dict(kept)}")


# ── B: the allowlist ──────────────────────────────────────────────────────

def test_firewall_policy_events_are_allowed_through_ingest():
    """Level 4 -> "Information" -> dropped unless allowlisted. This is the one
    that made the count zero."""
    from config_manager import ConfigManager
    allow = set(ConfigManager().get_settings()["log_ingest"]["information_allowlist"])
    missing = [f"Firewall/{i}" for i in (2004, 2005, 2006)
               if f"Firewall/{i}" not in allow]
    assert not missing, (
        f"firewall policy events are not allowlisted, so ingest drops them: {missing}")


def test_a_firewall_event_actually_lands_in_the_table():
    """The whole path, on a throwaway database: cap, then ingest, then read
    the row back. This is what every existing firewall test skipped by
    inserting its own rows."""
    import ingest_caps
    from config_manager import ConfigManager
    from database import Database

    path = os.path.join(tempfile.mkdtemp(), "fw_ingest.db")
    db = Database(db_path=path)
    cfg = ConfigManager().get_settings()["log_ingest"]
    db.insert_logs("TESTSRV", ingest_caps.cap_log_rows(_full_batch(), server="TESTSRV"),
                   ingest_cfg=cfg, caps=ingest_caps.resolve({}))

    con = sqlite3.connect(path)
    try:
        stored = con.execute(
            "SELECT COUNT(*) FROM logs WHERE log_source='Firewall'").fetchone()[0]
    finally:
        con.close()
    assert stored > 0, (
        "no firewall row survived ingest, while the UI promises policy changes "
        "will appear — which was the shipped state")


def test_the_per_packet_events_are_still_kept_out():
    """The volume ones must NOT be allowlisted. Letting 5152/5153 through
    would trade an empty panel for an unusable one."""
    from config_manager import ConfigManager
    allow = set(ConfigManager().get_settings()["log_ingest"]["information_allowlist"])
    noisy = [e for e in ("Firewall/5152", "Firewall/5153",
                         "Security/5152", "Security/5153") if e in allow]
    assert not noisy, f"high-volume per-packet events allowlisted: {noisy}"


# ── C: the filter is on the channel the events actually use ──────────────

def test_the_packet_noise_filter_covers_the_security_channel():
    """5152/5153 are Filtering Platform events and Windows writes them to the
    SECURITY log. The filter was gated on the Firewall channel, where they
    never appear — protecting nothing and leaving the real one exposed."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "collector_v2" / "scripts.py").read_text(encoding="utf-8")
    assert "$FIREWALL_NOISE_IDS" in src, "the noise filter is gone"
    gate = [ln for ln in src.splitlines() if "-notcontains [int]$_.Id" in ln]
    assert gate, "the filter body is gone"
    # The gate that guards it must include Security.
    guard = [ln for ln in src.splitlines()
             if "$displayName -eq" in ln and "Firewall" in ln and "{" in ln]
    assert guard, "the filter's channel gate is gone"
    assert any("Security" in ln for ln in guard), (
        "the packet-noise filter still runs only on the Firewall channel, "
        "where 5152/5153 never appear")


# ── D: the dead bucket ────────────────────────────────────────────────────

def test_no_selection_bucket_is_unreachable():
    """`$buckets[2]` was allocated and selected from, and the classifier never
    wrote to it. Harmless, but it made the selection read as 15+10+5 when it
    was really 15+10 and a top-up."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "collector_v2" / "scripts.py").read_text(encoding="utf-8")
    import re
    alloc = re.search(r"\$buckets = @\{([^}]*)\}", src)
    assert alloc, "the bucket allocation is gone"
    declared = set(re.findall(r"(\d+)=@\(\)", alloc.group(1)))
    written = set(re.findall(r"\$buckets\[(\d+)\] \+=", src))
    unreachable = declared - written
    assert not unreachable, (
        f"buckets allocated and never filled: {sorted(unreachable)}")
