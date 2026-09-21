"""WinRM transport defaults (collector audit finding 6).

THE FINDING. `use_https` defaulted to False, so a server added without an
opinion was monitored over WinRM on port 5985. Negotiate gives message-level
encryption there, so this was never plaintext credentials on the wire — but it
is still the wrong default to SHIP: no transport encryption and no server
certificate to check, chosen silently on the operator's behalf.

WHAT CHANGED, and the one thing that makes it safe. The default is now HTTPS.
An existing installation is unaffected: every server entry written by the app
carries an explicit `use_https`, and an explicit False is still honoured — the
tests below pin that, because a default flip that silently overrode a
deliberate choice would take a whole fleet off monitoring at once.

WHAT IT COSTS, and why the cost is paid in a message rather than in a fallback.
Windows enables only the HTTP listener by default, so a NEW server on a host
without an HTTPS listener now fails to connect. There is deliberately no
automatic fallback to HTTP: a security default that quietly downgrades itself
is not a default, it is a suggestion. Instead the failure has to say what
happened and name both ways out — otherwise the change turns into "Prism can't
reach my server", which is the least useful sentence in monitoring.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import ServerConfig                        # noqa: E402
import winrm_factory                                   # noqa: E402


def _entry(**over):
    base = {"name": "SRV", "host": "srv.example.invalid",
            "username": "svc", "password": "x", "type": "file_server"}
    base.update(over)
    return base


# ── the default ───────────────────────────────────────────────────────────

def test_a_server_added_without_an_opinion_gets_https():
    assert ServerConfig.from_dict(_entry()).use_https is True


def test_a_bare_server_config_defaults_to_https():
    """`from_dict` and the dataclass default have to agree. Two different
    defaults for one field is how "the form says HTTPS and the collector used
    HTTP" happens."""
    srv = ServerConfig(name="SRV", host="h", username="u", password="p")
    assert srv.use_https is True


def test_the_default_port_follows_the_default_transport():
    assert ServerConfig.from_dict(_entry()).port == 5986


# ── the existing fleet must not move ──────────────────────────────────────

def test_an_explicit_false_is_still_honoured():
    """The load-bearing test. Every server entry the app has ever written
    carries an explicit value; if the flip overrode those, an upgrade would
    take an entire fleet off monitoring in one restart."""
    srv = ServerConfig.from_dict(_entry(use_https=False))
    assert srv.use_https is False
    assert srv.port == 5985


def test_an_explicit_false_survives_a_round_trip():
    srv = ServerConfig.from_dict(_entry(use_https=False))
    again = ServerConfig.from_dict(srv.to_dict())
    assert again.use_https is False


def test_a_custom_port_survives_the_default():
    srv = ServerConfig.from_dict(_entry(port=15986))
    assert srv.use_https is True and srv.port == 15986


# ── turning HTTPS on must not turn verification off ───────────────────────

def test_certificate_validation_is_not_skipped_by_default():
    """The finding's other half: `https_skip_verify` reopens MITM. Defaulting
    HTTPS on while defaulting verification off would have been a downgrade
    dressed as an upgrade."""
    assert ServerConfig.from_dict(_entry()).https_skip_verify is False


def test_the_factory_validates_the_certificate_by_default(monkeypatch):
    captured = {}

    class _WSMan:
        def __init__(self, host, **kwargs):
            captured["host"] = host
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "pypsrp.wsman",
                        type("M", (), {"WSMan": _WSMan})())
    monkeypatch.setattr("crypto_utils.decrypt_password", lambda p: "pw")
    winrm_factory.make_wsman(ServerConfig.from_dict(_entry()))
    assert captured["ssl"] is True
    assert captured["cert_validation"] is True
    assert captured["port"] == 5986


# ── the failure has to be legible ─────────────────────────────────────────

def test_an_https_connection_failure_names_both_ways_out():
    """Without this sentence the default flip presents as an unreachable
    server, and the operator has no reason to suspect the transport."""
    srv = ServerConfig.from_dict(_entry())
    out = winrm_factory.explain_transport_failure(
        srv, "ConnectionError: timed out", "offline")
    assert "5986" in out
    assert "5985" in out, "the message does not say what Windows enables by default"
    assert "timed out" in out, "the original error was thrown away"


def test_the_hint_is_not_added_when_the_server_is_on_http():
    srv = ServerConfig.from_dict(_entry(use_https=False))
    err = "ConnectionError: timed out"
    assert winrm_factory.explain_transport_failure(srv, err, "offline") == err


def test_the_hint_is_not_added_to_an_unrelated_failure():
    """A parse error or a PowerShell error is not a transport problem, and
    attaching transport advice to it would send the reader the wrong way."""
    srv = ServerConfig.from_dict(_entry())
    err = "Bad metrics JSON"
    assert winrm_factory.explain_transport_failure(srv, err, "parse") == err


def test_the_check_layer_attaches_the_hint(monkeypatch):
    """The seam. The helper existing is not the same as an operator seeing it.

    Stubs the PowerShell layer, NOT `_run_ps` — the annotation happens inside
    `_run_ps` so that all four checks inherit it from one place, and a test that
    stubbed `_run_ps` would be blind to exactly the code it was written for.
    """
    from collector_v2 import checks

    class _FakePS:
        had_errors = False
        streams = type("S", (), {"error": []})()

        def __init__(self, pool):
            pass

        def add_script(self, script):
            pass

        def invoke(self):
            raise ConnectionError("timed out")

    monkeypatch.setitem(sys.modules, "pypsrp.powershell",
                        type("M", (), {"PowerShell": _FakePS})())
    ok, data, err, kind = checks.check_metrics(
        ServerConfig.from_dict(_entry()), pool=None)
    assert ok is False
    assert "timed out" in err
    assert "5985" in err, "the operator gets no reason to suspect the transport"


def test_the_check_layer_leaves_an_http_server_s_error_alone(monkeypatch):
    from collector_v2 import checks

    class _FakePS:
        had_errors = False
        streams = type("S", (), {"error": []})()

        def __init__(self, pool):
            pass

        def add_script(self, script):
            pass

        def invoke(self):
            raise ConnectionError("timed out")

    monkeypatch.setitem(sys.modules, "pypsrp.powershell",
                        type("M", (), {"PowerShell": _FakePS})())
    ok, data, err, kind = checks.check_metrics(
        ServerConfig.from_dict(_entry(use_https=False)), pool=None)
    assert ok is False
    assert "5985" not in err
