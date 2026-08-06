"""Regression test for the never-worked lockout alert email (council audit P0).

auth._maybe_send_lockout_alert called send_alert_email(settings, event) — but the
real signature is send_alert_email(event, server_name, settings). Every call
raised TypeError, was swallowed at debug level, and the admin's brute-force
alert email silently never sent. This pins the correct 3-arg invocation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import auth


def test_lockout_alert_calls_send_with_correct_signature():
    fake_cfg = MagicMock()
    fake_cfg.get_settings.return_value = {
        "email": {"enabled": True, "recipients": ["ops@example.com"]}
    }
    with patch.object(auth, "_config", fake_cfg), \
            patch("email_alerts.send_alert_email") as mock_send:
        auth._maybe_send_lockout_alert("bob", 5, 30)

    assert mock_send.call_count == 1, "lockout alert must send exactly one email"
    args, kwargs = mock_send.call_args
    # send_alert_email(event: dict, server_name: str, settings: dict)
    assert len(args) == 3, f"expected 3 positional args, got {len(args)}: {args!r}"
    event, server_name, settings = args
    assert isinstance(event, dict) and event.get("metric") == "account_lockout"
    assert isinstance(server_name, str) and server_name
    assert isinstance(settings, dict) and "email" in settings


def test_lockout_alert_skips_when_email_disabled():
    fake_cfg = MagicMock()
    fake_cfg.get_settings.return_value = {"email": {"enabled": False}}
    with patch.object(auth, "_config", fake_cfg), \
            patch("email_alerts.send_alert_email") as mock_send:
        auth._maybe_send_lockout_alert("bob", 5, 30)
    mock_send.assert_not_called()
