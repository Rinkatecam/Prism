"""Tests for webhook URL validation + message sanitisation."""

from webhooks import validate_webhook_url, sanitize_alert_text


def test_https_teams_url_accepted():
    ok, _ = validate_webhook_url("https://outlook.office.com/webhook/abc")
    assert ok


def test_subdomain_of_allowed_host_accepted():
    ok, _ = validate_webhook_url("https://hooks.slack.com/services/T1/B2/x")
    assert ok


def test_http_rejected():
    ok, reason = validate_webhook_url("http://outlook.office.com/x")
    assert not ok
    assert "https" in reason


def test_ip_address_rejected():
    ok, _ = validate_webhook_url("https://192.168.1.10/webhook")
    assert not ok


def test_random_host_rejected():
    ok, _ = validate_webhook_url("https://attacker.example.com/x")
    assert not ok


def test_credentials_in_url_rejected():
    ok, _ = validate_webhook_url("https://user:pass@outlook.office.com/x")
    assert not ok


def test_extra_allowed_host_via_settings():
    ok, _ = validate_webhook_url(
        "https://chat.internal.corp/x",
        allowed_hosts={"chat.internal.corp"},
    )
    assert ok


def test_sanitize_strips_control_chars():
    out = sanitize_alert_text("line1\r\nline2\x00bad\x07")
    assert "\r" not in out
    assert "\n" not in out
    assert "\x00" not in out
    assert "\x07" not in out
    assert "line1" in out and "line2" in out


def test_sanitize_caps_length():
    s = "x" * 5000
    out = sanitize_alert_text(s, max_len=200)
    assert len(out) == 200
