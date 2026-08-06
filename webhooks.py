"""Microsoft Teams webhook integration for Prism monitoring.

Two safety helpers live here so they can be reused by API endpoints that
let users edit webhook URLs or alert messages from the UI:

* `validate_webhook_url(url)` — refuses non-HTTPS, non-Teams hosts, IP
  addresses, and embedded credentials. Failure mode: a misconfiguration
  that quietly POSTs alerts to attacker-controlled infra.
* `sanitize_alert_text(text)` — strips control characters, caps length at
  2000 chars, and removes embedded HTTP header characters (``\\r\\n``)
  so a maliciously crafted message can't smuggle additional headers into
  whatever transport layer carries it.
"""

import json
import logging
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import zoneinfo
from datetime import datetime, timezone

logger = logging.getLogger("prism.webhooks")

# Hosts we recognise as legitimate Teams / Slack / generic enterprise webhook
# endpoints. Operators can add to this list via settings.webhooks.allowed_hosts;
# the default keeps things tight.
DEFAULT_ALLOWED_WEBHOOK_HOSTS = {
    "outlook.office.com",
    "webhook.office.com",
    "hooks.slack.com",
    "discord.com",
    "discordapp.com",
}


def validate_webhook_url(url: str, allowed_hosts: set[str] | None = None) -> tuple[bool, str]:
    """Return (ok, reason). ok=True ↔ url is safe to POST to."""
    if not url or not isinstance(url, str):
        return False, "Webhook URL is empty"
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False, "Webhook URL is malformed"
    if parsed.scheme != "https":
        return False, "Webhook URL must use https://"
    if parsed.username or parsed.password:
        return False, "Webhook URL must not embed credentials"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "Webhook URL has no host"
    # Reject IP literals — webhooks should be DNS hostnames so a stolen
    # internal IP doesn't quietly point traffic at a sensitive service.
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or ":" in host:
        return False, "Webhook URL must use a DNS hostname, not an IP address"
    al = allowed_hosts or DEFAULT_ALLOWED_WEBHOOK_HOSTS
    if not any(host == h or host.endswith("." + h) for h in al):
        return False, f"Webhook host {host!r} is not on the allowed list"
    return True, ""


def sanitize_alert_text(text: str, max_len: int = 2000) -> str:
    """Strip control characters and cap length so a message can't smuggle
    HTTP headers, terminal escape sequences, or a 10 MB payload."""
    if not text:
        return ""
    # Drop CR/LF and control codes other than tab/newline-as-space.
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    return cleaned[:max_len]


def send_teams_webhook(webhook_url, server_name, event_type, metric, value, threshold, message, settings=None):
    """Send an alert notification to Microsoft Teams via Incoming Webhook.

    Uses Adaptive Card format for rich display.
    Returns dict with {ok: bool, error?: str}.
    """
    if not webhook_url:
        return {"ok": False, "error": "No webhook URL configured"}

    allowed = None
    if settings:
        wh_cfg = settings.get("webhooks", {}) or {}
        extra = wh_cfg.get("allowed_hosts")
        if isinstance(extra, list) and extra:
            allowed = DEFAULT_ALLOWED_WEBHOOK_HOSTS | {str(h).lower() for h in extra}
    ok, reason = validate_webhook_url(webhook_url, allowed)
    if not ok:
        logger.warning("Refusing to POST to webhook URL: %s", reason)
        return {"ok": False, "error": f"Webhook URL rejected: {reason}"}
    message = sanitize_alert_text(message)

    # Determine color based on event type
    if "critical" in event_type.lower():
        color = "attention"  # red
        status_emoji = "\U0001f534"
    elif "warning" in event_type.lower():
        color = "warning"  # yellow
        status_emoji = "\U0001f7e1"
    elif "recovery" in event_type.lower() or "healthy" in event_type.lower():
        color = "good"  # green
        status_emoji = "\U0001f7e2"
    else:
        color = "default"
        status_emoji = "\u26aa"

    # Build Adaptive Card payload
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "medium",
                            "weight": "bolder",
                            "text": f"{status_emoji} Prism Alert: {server_name}",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Server", "value": server_name},
                                {"title": "Event", "value": event_type},
                                {"title": "Metric", "value": metric or "N/A"},
                                {"title": "Value", "value": f"{value:.1f}%" if value is not None else "N/A"},
                                {"title": "Threshold", "value": f"{threshold:.1f}%" if threshold is not None else "N/A"},
                            ],
                        },
                        {
                            "type": "TextBlock",
                            "text": message,
                            "wrap": True,
                            "spacing": "medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": f"_Sent by Prism Monitoring_",
                            "isSubtle": True,
                            "size": "small",
                            "spacing": "medium",
                        },
                    ],
                },
            }
        ],
    }

    # Add dashboard link if available
    if settings and settings.get("email", {}).get("dashboard_url"):
        dashboard_url = settings["email"]["dashboard_url"]
        card["attachments"][0]["content"]["body"].append({
            "type": "ActionSet",
            "actions": [
                {
                    "type": "Action.OpenUrl",
                    "title": "Open Dashboard",
                    "url": dashboard_url,
                }
            ],
        })

    return _post_webhook(webhook_url, card)


def send_test_webhook(webhook_url, settings=None):
    """Send a test message to verify the webhook URL works.
    Returns dict with {ok: bool, message?: str, error?: str}.
    """
    if not webhook_url:
        return {"ok": False, "error": "No webhook URL provided"}

    tz_name = settings.get("timezone", "Europe/Berlin") if settings else "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    local_time = datetime.now(timezone.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "medium",
                            "weight": "bolder",
                            "text": "\u2705 Prism Webhook Test",
                        },
                        {
                            "type": "TextBlock",
                            "text": "This is a test message from Prism Server Monitoring. If you see this, your webhook is configured correctly!",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": f"_Sent at {local_time}_",
                            "isSubtle": True,
                            "size": "small",
                        },
                    ],
                },
            }
        ],
    }

    result = _post_webhook(webhook_url, card)
    if result["ok"]:
        result["message"] = "Test message sent successfully to Teams"
    return result


def _post_webhook(url, payload, retries=1):
    """POST JSON payload to webhook URL with retry support."""
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(1 + retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                if status in (200, 202):
                    return {"ok": True}
                else:
                    body = resp.read().decode("utf-8", errors="replace")
                    return {"ok": False, "error": f"HTTP {status}: {body[:200]}"}
        except urllib.error.HTTPError as e:
            error_msg = f"HTTP {e.code}: {e.reason}"
            logger.warning("Webhook attempt %d failed: %s", attempt + 1, error_msg)
            if attempt < retries:
                time.sleep(2)
            else:
                return {"ok": False, "error": error_msg}
        except urllib.error.URLError as e:
            error_msg = f"Connection error: {e.reason}"
            logger.warning("Webhook attempt %d failed: %s", attempt + 1, error_msg)
            if attempt < retries:
                time.sleep(2)
            else:
                return {"ok": False, "error": error_msg}
        except Exception as e:
            error_msg = str(e)
            logger.warning("Webhook attempt %d failed: %s", attempt + 1, error_msg)
            if attempt < retries:
                time.sleep(2)
            else:
                return {"ok": False, "error": error_msg}

    return {"ok": False, "error": "All retry attempts failed"}
