"""The address classifier behind the LAN-only verification.

`tools/verify_lan_only.py` is run by an operator on their own Windows host to
show that Prism connects nowhere off their network. Its verdict is only as good
as one function — `classify()` — and that function is the reason this file
exists: **both obvious one-line implementations are wrong, and each is wrong in
the direction that matters.**

Measured on CPython 3.13 rather than remembered:

    address           is_private   is_global
    255.255.255.255   True         False
    224.0.0.1         False        True
    100.64.0.1        False        True

So `not is_private` reports the Wake-on-LAN broadcast — the ONE hardcoded
destination in Prism, and the one `docs/DATA_FLOWS.md` singles out to defend —
as a connection off the network. And `is_global` reports multicast and CGNAT as
external. A verification tool that cries wolf on its own documented exception
is worse than no tool: the first person to run it concludes the document lies.

The live sampler is NOT tested here and cannot be: it needs a running Prism, a
configured fleet, and Windows. It is exercised by the procedure in
`docs/LAN_ONLY_VERIFICATION.md`. What is pinned here is every judgement the
tool makes about what it sees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.verify_lan_only import LOCAL, PUBLIC, REVIEW, classify   # noqa: E402


# ── the two traps, pinned first because they are the whole point ──────────

def test_the_wake_on_lan_broadcast_is_local():
    """255.255.255.255 is the one hardcoded destination in the application.

    A limited broadcast is not forwarded by routers, so it physically cannot
    leave the segment. If this tool called it external, its very first run on a
    customer's kit would contradict `docs/DATA_FLOWS.md`, which names this
    address and explains why it is safe.
    """
    bucket, why = classify("255.255.255.255")
    assert bucket == LOCAL
    assert "broadcast" in why


def test_multicast_is_not_reported_as_public():
    """`is_global` is True for 224.0.0.1, which is why it is not the test.

    Nothing in Prism multicasts. A dependency doing mDNS or SSDP discovery
    would, and it is not an exfiltration path — but it is not RFC1918 either,
    so it goes to review rather than being waved through.
    """
    bucket, why = classify("224.0.0.1")
    assert bucket == REVIEW
    assert "multicast" in why


def test_cgnat_space_is_flagged_for_a_human_rather_than_guessed():
    """100.64.0.0/10 is carrier NAT — or a large corporate network.

    The tool cannot know which, so it must not pretend to. Reporting it as
    LOCAL would hide a genuine egress; reporting it as PUBLIC would fail the
    run for every organisation that uses that range internally.
    """
    bucket, why = classify("100.64.0.1")
    assert bucket == REVIEW
    assert "CGNAT" in why


# ── the ordinary cases ────────────────────────────────────────────────────

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "::1",                 # loopback
    "10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.10",   # RFC1918
    "fd00::1",                          # IPv6 ULA
    "169.254.1.1", "fe80::1",           # link-local
    "0.0.0.0", "::",                    # unspecified — a listening socket
])
def test_addresses_that_cannot_carry_data_off_the_network_are_local(addr):
    assert classify(addr)[0] == LOCAL, f"{addr} should be LOCAL"


@pytest.mark.parametrize("addr", [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888",
])
def test_routable_public_addresses_fail_the_run(addr):
    """The assertion the whole tool exists to make."""
    bucket, why = classify(addr)
    assert bucket == PUBLIC, f"{addr} must be PUBLIC"
    assert "public" in why


def test_the_whole_rfc1918_boundary_is_respected_not_just_the_middle():
    """Off-by-one at a range edge is how a private-range check quietly leaks.

    172.16.0.0/12 is the range people get wrong — it ends at 172.31, not
    172.16, and 172.32.x.x is public.
    """
    assert classify("172.15.255.255")[0] == PUBLIC
    assert classify("172.16.0.0")[0] == LOCAL
    assert classify("172.31.255.255")[0] == LOCAL
    assert classify("172.32.0.0")[0] == PUBLIC


def test_an_unparseable_address_is_reviewed_not_ignored():
    """A sampler that returns junk must not produce a silent PASS. Reporting
    nothing is how a broken instrument reads as a clean result."""
    for junk in ("", "not-an-ip", "999.999.999.999", "*"):
        assert classify(junk)[0] == REVIEW


def test_every_bucket_carries_a_reason():
    """The report prints `why` beside each address so a reader can check the
    judgement rather than trust it. A bucket with an empty reason is a verdict
    with no argument."""
    addrs = ("127.0.0.1", "10.0.0.5", "255.255.255.255", "224.0.0.1",
             "100.64.0.1", "8.8.8.8", "garbage")
    reasons = []
    for addr in addrs:
        bucket, why = classify(addr)
        assert bucket in (LOCAL, REVIEW, PUBLIC)
        assert why and why.strip(), f"{addr} classified with no reason at all"
        reasons.append(why)

    # Distinctness is the real assertion. A constant placeholder would satisfy
    # "non-empty" for every address and explain nothing — which is the shape
    # of a check that passes while asserting nothing.
    assert len(set(reasons)) == len(addrs), (
        "two addresses in different situations share a reason string: "
        f"{reasons}")


def test_only_public_fails_the_run():
    """The three buckets are not equivalent and the tool's exit code depends
    on the distinction: REVIEW prints loudly and still exits 0, because a
    false FAIL trains operators to ignore the tool."""
    assert LOCAL != REVIEW != PUBLIC
    assert classify("10.0.0.5")[0] != PUBLIC
    assert classify("100.64.0.1")[0] != PUBLIC
    assert classify("8.8.8.8")[0] == PUBLIC
