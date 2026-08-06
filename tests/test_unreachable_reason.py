"""B-2: 'offline' conflated a down host with an unreachable one.

Two incidents on this fleet, both mislabelled because every failure rendered
identically as "Server is unreachable":

  * A healthy domain controller sat labelled offline for DAYS. RPC 135, SMB 445,
    RDP 3389, DNS 53 and LDAP 389 were all reachable; only WinRM 5985 was being
    dropped, because the NIC was classified Public so the stock LocalSubnet-
    scoped firewall rule never matched off-subnet traffic. Nothing was wrong
    with the server.
  * STANDALONE01 read offline for its ENTIRE recorded history (189 samples, never
    once online). The host was up the whole time — WinRM, SMB, RDP and RPC all
    open — but it is not domain-joined, so its name never resolved.

Those need different responses: an outage, a firewall rule, and a DNS/domain
problem. The collector already had the information and threw it away, and the
one log line that carried it was truncated at [:120] — landing on "(Ca", the
first two characters of "Caused by NameResolutionError".

Security note: /api/test-connection deliberately COLLAPSES these categories
(RF2, AUDIT-2026-05) because distinguishing "auth failed" from "host not found"
is an oracle for port scanning and credential spray. That reasoning does not
apply here — the dashboard is authenticated, RBAC-gated, and reports on the
operator's own fleet, where the distinction is the entire diagnostic value.
"""

from __future__ import annotations

import pytest

from collector_v2.checks import (
    REASON_TEXT,
    classify_unreachable,
    unreachable_reason_text,
)


# ── the two real incidents ────────────────────────────────────────────────

def test_the_standalone01_error_classifies_as_dns():
    """Verbatim from the live log. This is the string that spent a month
    truncated at '(Ca'."""
    err = ("ConnectionError: HTTPConnectionPool(host='standalone01.ad.example.com', "
           "port=5985): Max retries exceeded with url: /wsman (Caused by "
           "NameResolutionError(\"<urllib3.connection.HTTPConnection object at "
           "0x0>: Failed to resolve 'standalone01.ad.example.com' "
           "([Errno 11001] getaddrinfo failed)\"))")
    assert classify_unreachable(err) == "dns"
    assert "resolve" in unreachable_reason_text(err)


def test_the_filtered_domain_controller_classifies_as_timeout():
    """Packets dropped by a firewall present as a read timeout — the host is
    fine, the management path is filtered."""
    assert classify_unreachable("ReadTimeout during WSMan request - attempt 0") == "timeout"
    assert "filtered" in unreachable_reason_text("ReadTimeout during WSMan request")


# ── the rest of the taxonomy ──────────────────────────────────────────────

@pytest.mark.parametrize("err,expected", [
    ("Failed to resolve 'x' ([Errno 11001] getaddrinfo failed)", "dns"),
    ("Name or service not known", "dns"),
    ("Der DNS-Name ist nicht vorhanden", "dns"),      # German-locale hosts
    ("No such host is known", "dns"),
    ("ConnectionRefusedError: [WinError 10061] connection refused", "refused"),
    ("An existing connection was forcibly closed by the remote host", "refused"),
    ("AuthenticationError: Failed to authenticate the user X with negotiate", "auth"),
    ("401 Unauthorized", "auth"),
    ("ConnectTimeout during initial authentication request", "timeout"),
    ("No route to host", "network"),
    ("Network is unreachable", "network"),
    ("The WSMan shell was not found on the server", "rebooting"),
    ("Die Shell wurde nicht auf dem Server gefunden", "unknown"),
    ("something nobody has seen before", "unknown"),
])
def test_classification(err, expected):
    assert classify_unreachable(err) == expected


def test_dns_wins_over_the_timeout_it_is_wrapped_in():
    """A DNS failure surfaces as a ConnectionError whose text ALSO mentions
    retries and timeouts. The most specific cause must win, or every failure
    collapses back into 'timeout'."""
    err = ("Max retries exceeded ... Read timed out ... "
           "Caused by NameResolutionError getaddrinfo failed")
    assert classify_unreachable(err) == "dns"


def test_refused_wins_over_timeout():
    assert classify_unreachable("connection refused after timeout") == "refused"


def test_every_reason_has_operator_facing_text():
    from collector_v2.checks import UNREACHABLE_REASONS
    for reason in UNREACHABLE_REASONS:
        assert reason in REASON_TEXT, f"{reason} has no operator-facing phrase"
        assert REASON_TEXT[reason].strip()
    assert "unknown" in REASON_TEXT


def test_unknown_degrades_to_the_old_wording():
    """An unclassifiable error must not produce a blank or a misleading
    reason — it falls back to what the message always said."""
    assert unreachable_reason_text("???") == "unreachable"


def test_classifier_accepts_an_exception_not_just_a_string():
    assert classify_unreachable(RuntimeError("getaddrinfo failed")) == "dns"


def test_classification_is_case_insensitive():
    assert classify_unreachable("GETADDRINFO FAILED") == "dns"


def test_empty_input_is_unknown_not_a_crash():
    assert classify_unreachable("") == "unknown"
    assert classify_unreachable(None) == "unknown"


# ── it reaches the operator ───────────────────────────────────────────────

def test_offline_event_message_carries_the_reason():
    """The event text is what the dashboard and the issues panel display."""
    from collector_v2 import aggregator
    aggregator._last_unreachable_reason["srv-dns"] = "dns"

    class _DB:
        def __init__(self): self.events = []
        def insert_event(self, *a): self.events.append(a)
    class _Srv:
        name = "srv-dns"

    agg = aggregator.Aggregator.__new__(aggregator.Aggregator)
    agg.db = _DB()
    agg._append_recent_event = lambda e: None
    out = agg._fire_offline(_Srv())

    msg = out[-1]
    assert "unreachable" in msg
    assert "resolve" in msg, f"reason missing from the operator-facing text: {msg}"


def test_offline_event_falls_back_cleanly_for_an_unclassified_server():
    from collector_v2 import aggregator
    aggregator._last_unreachable_reason.pop("srv-none", None)

    class _DB:
        def insert_event(self, *a): pass
    class _Srv:
        name = "srv-none"

    agg = aggregator.Aggregator.__new__(aggregator.Aggregator)
    agg.db = _DB()
    agg._append_recent_event = lambda e: None
    msg = agg._fire_offline(_Srv())[-1]
    assert "unreachable" in msg


def test_the_offline_log_line_is_no_longer_truncated_before_the_cause():
    """The [:120] truncation landed on '(Ca' — the first two characters of
    'Caused by NameResolutionError', i.e. exactly the substring that identified
    the fault."""
    import inspect
    from collector_v2 import aggregator
    src = inspect.getsource(aggregator.Aggregator)
    assert 'synthesising offline row' in src
    idx = src.index('synthesising offline row')
    window = src[idx - 400:idx + 400]
    assert 'classify_unreachable' in window, "reason must be logged"
    assert '[:120]' not in window, "truncation that hid the cause is back"
