"""Tests for ServerConfig defaults and the HTTPS port auto-flip."""

from models import ServerConfig


def test_defaults_are_https_on_5986():
    """The default flipped at WP-1.5 (collector audit finding 6). This test used
    to pin the opposite and was renamed rather than deleted, because the pair it
    forms with the one below is the whole contract: a NEW server gets HTTPS, and
    an EXPLICIT False still gets legacy HTTP. See
    tests/test_winrm_transport_defaults.py for the reasoning and the cost."""
    s = ServerConfig.from_dict({"name": "X", "host": "1.2.3.4",
                                "username": "u", "password": "p"})
    assert s.port == 5986
    assert s.use_https is True
    assert s.tier == 1


def test_an_explicit_false_is_still_legacy_http():
    s = ServerConfig.from_dict({"name": "X", "host": "1.2.3.4",
                                "username": "u", "password": "p",
                                "use_https": False})
    assert s.port == 5985
    assert s.use_https is False


def test_https_auto_flips_default_port():
    s = ServerConfig.from_dict({"name": "X", "host": "1.2.3.4",
                                "username": "u", "password": "p",
                                "use_https": True})
    assert s.port == 5986
    assert s.use_https is True


def test_explicit_custom_port_preserved():
    s = ServerConfig.from_dict({"name": "X", "host": "1.2.3.4",
                                "username": "u", "password": "p",
                                "use_https": True, "port": 9999})
    assert s.port == 9999


def test_password_is_masked_in_to_dict():
    s = ServerConfig.from_dict({"name": "X", "host": "1.2.3.4",
                                "username": "u", "password": "secret"})
    assert s.to_dict()["password"] != "secret"


def test_tier_0_serializes_through():
    s = ServerConfig.from_dict({"name": "DC01", "host": "1.2.3.4",
                                "username": "u", "password": "p", "tier": 0})
    assert s.tier == 0
    assert s.to_dict()["tier"] == 0
