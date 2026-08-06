"""Config endpoints — split out from the original routes/api.py."""

import re
import time
import io
import json
from pathlib import Path
from flask import jsonify, request, Response, make_response, current_app
from flask import session as flask_session
from crypto_utils import (
    is_password_masked, decrypt_password, encrypt_password, PASSWORD_MASK,
)
import collector_v2 as _collector_v2
from collector_v2 import (
    accelerate_server,
    sync_now as _v2_sync_now,
    sync_logs_now as _v2_sync_logs_now,
    sync_updates_now as _v2_sync_updates_now,
)
from state import (
    server_auth_info,
    server_update_info,
    server_hardware_info,
)
from email_alerts import send_test_email
from analytics import get_server_analytics, forecast_disk, forecast_metric
from reports import generate_csv_metrics, generate_csv_events, generate_pdf_report
from i18n import get_translations

from . import _shared
from ._shared import (
    api_bp,
    logger,
    _require_auth,
    _current_actor,
    _is_backup_admin,
    _server_tier,
    _require_server_permission,
    _require_rbac_admin,
)


@api_bp.route("/config", methods=["GET"])
def get_config():
    """Get current config (servers + settings). Passwords are masked.

    Masking contract:
      - server.password → masked by ServerConfig.to_dict()
      - settings.email.password → PASSWORD_MASK if set, "" if empty
      - settings.auth.ldap_bind_password → same
    POST /api/config then checks is_password_masked() to preserve the existing
    encrypted value when the field comes back unchanged. See save_config below.
    """
    try:
        servers = _shared._config.get_servers()
        settings = _shared._config.get_settings()
        # Deep-copy the sensitive nested dicts so we don't mutate the cached config
        import copy
        settings = copy.deepcopy(settings)
        # Mask email password
        email_cfg = settings.get("email") or {}
        if email_cfg.get("password"):
            email_cfg["password"] = PASSWORD_MASK
        # Mask LDAP bind password (if any)
        auth_cfg = settings.get("auth") or {}
        if auth_cfg.get("ldap_bind_password"):
            auth_cfg["ldap_bind_password"] = PASSWORD_MASK
        return jsonify({
            "servers": [s.to_dict() for s in servers],
            "settings": settings,
        })
    except Exception:
        logger.exception("Error in GET /api/config")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/config", methods=["POST"])
def save_config():
    """Save updated server list and settings.

    Closes audit finding R1 from docs/AUDIT-2026-05.md: this endpoint used to
    accept arbitrary JSON from any authenticated user and would happily
    overwrite settings.auth.backup_admin (the break-glass password hash)
    or settings.auth.allowed_users (the LDAP allowlist). One LDAP user could
    rewrite the backup-admin hash and re-login as backup admin — bypassing
    every per-server and tier-0 control in one HTTP request.

    Two layers of protection now:
      1. _require_rbac_admin gate — only backup admins or wildcard-admin
         users can mutate config at all.
      2. Defence-in-depth strip filter — even with admin rights, requests
         to this endpoint cannot mutate auth.backup_admin / auth.allowed_users
         / auth.ldap_*. Each has a dedicated endpoint that does the right
         thing:
           - auth.backup_admin  -> auth.py:/admin/reset-password
           - auth.ldap_*        -> POST /api/config/ldap (save_ldap_config below)
           - auth.allowed_users -> the RBAC admin UI (/admin/rbac)
         If a future RBAC role expands the admin set, the strip still holds.
    """
    auth_err = _shared._require_rbac_admin()
    if auth_err:
        return auth_err
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "Missing request body"}), 400

    # Defence-in-depth strip filter (S1-2 / R1). Even with admin rights, this
    # endpoint is a generic config writer and is the wrong place to mutate
    # sensitive auth fields. Replace any user-supplied auth.backup_admin /
    # auth.allowed_users / auth.ldap_* values with what's currently on disk;
    # operators rotate those through dedicated endpoints (auth.py:/admin/
    # reset-password for the break-glass password, POST /api/config/ldap for
    # directory settings). Stripping is silent — the request still succeeds for
    # legitimate non-auth fields, so any UI that needs an auth field to actually
    # persist MUST call that field's dedicated endpoint, not this one.
    _SENSITIVE_AUTH_KEYS = (
        "backup_admin", "allowed_users",
        "ldap_url", "ldap_base_dn", "ldap_user_filter",
        "ldap_bind_user", "ldap_bind_password",
    )
    if isinstance(data.get("settings"), dict) and isinstance(data["settings"].get("auth"), dict):
        existing_auth = _shared._config.get_settings().get("auth", {}) or {}
        _auth_defaults = type(_shared._config)._DEFAULT_SETTINGS.get("auth", {})
        for k in _SENSITIVE_AUTH_KEYS:
            if k in data["settings"]["auth"]:
                # Preserve current value; ignore whatever the caller sent.
                #
                # Fall back to the DECLARED DEFAULT rather than None when the key
                # has never been configured. A bare `existing_auth.get(k)` wrote
                # a null for any unconfigured sensitive key, and that is how
                # auth.allowed_users became `null` on disk — which then 500'd the
                # entire Settings page, because Jinja's `| default([])` does not
                # substitute for None. Coercing here also self-heals an existing
                # null on the next save.
                preserved = existing_auth.get(k)
                if preserved is None:
                    preserved = _auth_defaults.get(k, "")
                data["settings"]["auth"][k] = preserved

    # Allow settings-only updates (e.g. from monitoring page).
    # If 'servers' is omitted, preserve the existing server list unchanged.
    settings_only = "servers" not in data
    if settings_only:
        servers = [s for s in _shared._config.get_raw_servers()]
    else:
        servers = data["servers"]
    # Build a lookup of existing encrypted passwords by server name
    existing_servers = {s.get("name"): s for s in _shared._config.get_raw_servers()}

    # Validate and process each server (skip in settings-only mode — existing servers are already valid)
    for i, s in enumerate([] if settings_only else servers):
        name = s.get("name", "").strip()
        host = s.get("host", "").strip()

        if not name or not host:
            return jsonify({"ok": False, "error": f"Server at index {i} missing 'name' or 'host'"}), 400

        # Validate server name: alphanumeric, hyphens, underscores, dots
        if not re.match(r'^[A-Za-z0-9._-]+$', name):
            return jsonify({"ok": False, "error": f"Server name '{name}' contains invalid characters"}), 400

        # Validate hostname: alphanumeric, hyphens, dots (FQDN or IP)
        if not re.match(r'^[A-Za-z0-9._-]+$', host):
            return jsonify({"ok": False, "error": f"Host '{host}' contains invalid characters"}), 400

        # Validate port
        port = s.get("port", 5985)
        try:
            port = int(port)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"Invalid port for server '{name}'"}), 400
        if port < 1 or port > 65535:
            return jsonify({"ok": False, "error": f"Port {port} out of range for server '{name}'"}), 400
        s["port"] = port

        # Tier (RBAC): 0=critical, 1=standard, 2=dev. Default 1.
        tier_raw = s.get("tier", 1)
        try:
            tier = int(tier_raw)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": f"Invalid tier for server '{name}'"}), 400
        if tier not in (0, 1, 2):
            return jsonify({"ok": False, "error": f"Tier for '{name}' must be 0, 1 or 2"}), 400
        s["tier"] = tier

        # HTTPS — closes audit findings R7 (S3-1) and W10 (S3-12).
        existing_entry = existing_servers.get(name) or {}
        existing_https = bool(existing_entry.get("use_https", False))
        new_https = bool(s.get("use_https", existing_https))
        new_skip_verify = bool(s.get("https_skip_verify", False))

        # S3-12: NEW servers (no existing entry) default to HTTPS. Operators
        # who explicitly set use_https=false on a new add still bypass — but
        # the form omitting the flag means HTTPS-by-default.
        if not existing_entry and "use_https" not in s:
            new_https = True

        # S3-1: refuse to downgrade an already-HTTPS server back to HTTP unless
        # an admin token is present. Old behaviour let any user with config-
        # write rewrite use_https=False, exposing credentials to the LAN at the
        # next collector cycle and the only signal was a debug log.
        if existing_https and not new_https:
            from . import _shared as _sh_mod
            if _sh_mod._require_rbac_admin() is not None:
                return jsonify({
                    "ok": False,
                    "error": f"Server '{name}' was previously HTTPS — downgrade requires admin role.",
                }), 403

        # S3-12: refuse https_skip_verify on tier-0 servers. The cert validation
        # cannot legitimately be skipped on DCs / mail / primary DB.
        if new_https and new_skip_verify and tier == 0:
            return jsonify({
                "ok": False,
                "error": f"Tier-0 server '{name}' cannot have https_skip_verify=true. "
                         "Roll out a real cert.",
            }), 400

        s["use_https"] = new_https
        s["https_skip_verify"] = new_skip_verify

        # Handle masked passwords: if password is the mask, preserve the existing encrypted password
        pw = s.get("password", "")
        if is_password_masked(pw):
            existing = existing_servers.get(name)
            if existing:
                s["password"] = existing.get("password", "")
            else:
                s["password"] = ""
        # Otherwise, the new plain-text password will be encrypted by save_config

        # Validate per-server thresholds: must be 0..100 ints; warning < critical
        thresholds = s.get("thresholds")
        if thresholds and isinstance(thresholds, dict):
            for metric in ("cpu", "ram", "disk"):
                wkey = f"{metric}_warning"
                ckey = f"{metric}_critical"
                if wkey not in thresholds and ckey not in thresholds:
                    continue
                try:
                    w = int(thresholds.get(wkey, 0))
                    c = int(thresholds.get(ckey, 0))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": f"{metric} thresholds for '{name}' must be numbers"}), 400
                # Clamp to 0..100
                w = max(0, min(100, w))
                c = max(0, min(100, c))
                if w >= c:
                    return jsonify({"ok": False, "error": f"{metric}_warning ({w}) must be less than {metric}_critical ({c}) for '{name}'"}), 400
                thresholds[wkey] = w
                thresholds[ckey] = c
            s["thresholds"] = thresholds

    # Validate settings
    #
    # IMPORTANT (Bug 5, docs/plans/CRITICAL_BUGS_REMEDIATION.md §5): every scalar
    # below is validated ONLY IF THE CALLER ACTUALLY POSTED IT. These blocks used
    # to read ``settings.get(key, <default>)`` and then unconditionally assign the
    # result back — which meant a partial settings POST (templates/monitoring.html
    # builds ``{settings: {}}`` from scratch) INJECTED the default for every key it
    # omitted. Combined with the merge in ConfigManager.save_config that would
    # still silently reset retention/poll/language on an unrelated page save.
    # Presence-guard first, then validate. Absent key -> untouched on disk ->
    # get_settings() supplies the default, which is the correct behaviour and keeps
    # first-run / no-config.json CI green.
    settings = data.get("settings", None)
    if settings:
        def _clamped_int(key, default, lo, hi, label):
            """Validate settings[key] in place if present. Returns an error
            response to bubble up, or None on success/absence."""
            if key not in settings:
                return None
            try:
                val = int(settings.get(key, default))
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": f"{label} must be a number"}), 400
            if val < lo or val > hi:
                return jsonify({"ok": False, "error": f"{label} must be {lo}-{hi}"}), 400
            settings[key] = val
            return None

        # Poll interval is stored in SECONDS but described to operators in minutes,
        # hence the bespoke message rather than the generic one.
        if "poll_interval_seconds" in settings:
            try:
                poll = int(settings["poll_interval_seconds"])
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "Invalid settings values"}), 400
            if poll < 60 or poll > 3600:
                return jsonify({"ok": False, "error": "Poll interval must be 1-60 minutes"}), 400
            settings["poll_interval_seconds"] = poll

        if "retention_days" in settings:
            try:
                retention = int(settings["retention_days"])
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "Invalid settings values"}), 400
            if retention < 1 or retention > 365:
                return jsonify({"ok": False, "error": "Retention must be 1-365 days"}), 400
            settings["retention_days"] = retention

        err = _clamped_int("log_collection_interval_minutes", 5, 1, 60,
                           "Log collection interval")
        if err:
            return err

        # Update check interval — matches the HTML bounds at templates/settings.html.
        # collector_v2/supervisor.py already degrades gracefully on garbage, but
        # validating here stops a direct API call persisting a nonsense cadence.
        err = _clamped_int("update_check_interval_minutes", 30, 10, 120,
                           "Update check interval")
        if err:
            return err

        # Worker pool size is load-bearing for startup: app.py converts it with
        # int() and a bad value used to kill the process on the next boot. The HTML
        # min/max is trivially bypassed by a direct POST, hence the server-side
        # gate. app.py is defensive independently too, because a hand-edited
        # config.json never passes through this endpoint at all.
        err = _clamped_int("collector_v2_num_workers", 15, 2, 100, "Worker pool size")
        if err:
            return err

        # Detection fusion — exhaustion floors + baseline downgrade authority
        # (docs/plans/DETECTION_FUSION_PLAN.md §2, §7). Clamped server-side so
        # a malformed/malicious POST can't push a floor out of a sane range —
        # e.g. an exhaustion_ram floor below the critical threshold would make
        # the "hard truth" floor fire before the static critical band does.
        thresholds_cfg = settings.get("thresholds")
        if thresholds_cfg and isinstance(thresholds_cfg, dict):
            if "exhaustion_ram" in thresholds_cfg:
                try:
                    exhaustion_ram = int(thresholds_cfg.get("exhaustion_ram", 98))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": "exhaustion_ram must be a number"}), 400
                thresholds_cfg["exhaustion_ram"] = max(90, min(100, exhaustion_ram))
            if "exhaustion_disk" in thresholds_cfg:
                try:
                    exhaustion_disk = int(thresholds_cfg.get("exhaustion_disk", 95))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": "exhaustion_disk must be a number"}), 400
                thresholds_cfg["exhaustion_disk"] = max(80, min(100, exhaustion_disk))

        baseline_cfg = settings.get("baseline_detection")
        if baseline_cfg and isinstance(baseline_cfg, dict):
            if "allow_downgrade" in baseline_cfg:
                baseline_cfg["allow_downgrade"] = bool(baseline_cfg.get("allow_downgrade"))
            if "min_span_weeks" in baseline_cfg:
                try:
                    span_weeks = int(baseline_cfg.get("min_span_weeks", 2))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": "min_span_weeks must be a number"}), 400
                baseline_cfg["min_span_weeks"] = max(1, min(8, span_weeks))
            if "min_coverage_pct" in baseline_cfg:
                try:
                    coverage_pct = int(baseline_cfg.get("min_coverage_pct", 50))
                except (ValueError, TypeError):
                    return jsonify({"ok": False, "error": "min_coverage_pct must be a number"}), 400
                baseline_cfg["min_coverage_pct"] = max(10, min(100, coverage_pct))

            # Deviation-from-self raise gates (ALERT_NOISE_AND_VERDICT_UX_PLAN §3).
            # Presence-guarded like everything else in this block so a partial POST
            # can't inject defaults over saved values (Bug 5).
            if "deviation_direction" in baseline_cfg:
                direction = str(baseline_cfg.get("deviation_direction", "high")).strip().lower()
                if direction not in ("high", "both"):
                    return jsonify({
                        "ok": False,
                        "error": "deviation_direction must be 'high' or 'both'",
                    }), 400
                baseline_cfg["deviation_direction"] = direction
            if "deviation_min_pct_of_warning" in baseline_cfg:
                try:
                    dev_pct = int(baseline_cfg.get("deviation_min_pct_of_warning", 80))
                except (ValueError, TypeError):
                    return jsonify({
                        "ok": False,
                        "error": "deviation_min_pct_of_warning must be a number",
                    }), 400
                baseline_cfg["deviation_min_pct_of_warning"] = max(0, min(100, dev_pct))
            if "deviation_requires_authority" in baseline_cfg:
                baseline_cfg["deviation_requires_authority"] = bool(
                    baseline_cfg.get("deviation_requires_authority"))

        # Static-breach spike gate — how many consecutive rounds a cpu/ram
        # threshold breach must hold before it alarms. Clamped 1..20: 1 disables
        # the gate, and an absurdly high value would make cpu/ram effectively
        # unalarmable, which is worse than noise. Presence-guarded (Bug 5).
        anomaly_cfg = settings.get("anomaly_detection")
        if anomaly_cfg and isinstance(anomaly_cfg, dict):
            if "spike_sustain_cycles" in anomaly_cfg:
                try:
                    spike = int(anomaly_cfg.get("spike_sustain_cycles", 5))
                except (ValueError, TypeError):
                    return jsonify({
                        "ok": False,
                        "error": "spike_sustain_cycles must be a number",
                    }), 400
                anomaly_cfg["spike_sustain_cycles"] = max(1, min(20, spike))

        # Validate locale settings
        VALID_LANGUAGES = ("en", "de", "fr", "es", "ja")
        VALID_TIMEZONES = ("Europe/Berlin", "Europe/London", "Europe/Paris", "Europe/Zurich",
                           "Europe/Vienna", "US/Eastern", "US/Central", "US/Pacific", "Asia/Tokyo", "UTC")
        VALID_DATE_FORMATS = ("DD.MM.YYYY", "YYYY-MM-DD", "MM/DD/YYYY", "DD/MM/YYYY")
        VALID_TIME_FORMATS = ("24h", "12h")

        # Presence-guarded for the same reason as the numeric block above: an
        # unconditional assign here reset an operator's language back to "en" and
        # their timezone to Europe/Berlin on every Monitoring-page save.
        if "language" in settings:
            lang = str(settings.get("language", "en")).strip().lower()
            if lang not in VALID_LANGUAGES:
                lang = "en"
            settings["language"] = lang

        if "timezone" in settings:
            tz = str(settings.get("timezone", "Europe/Berlin")).strip()
            if tz not in VALID_TIMEZONES:
                tz = "Europe/Berlin"
            settings["timezone"] = tz

        if "date_format" in settings:
            df = str(settings.get("date_format", "DD.MM.YYYY")).strip()
            if df not in VALID_DATE_FORMATS:
                df = "DD.MM.YYYY"
            settings["date_format"] = df

        if "time_format" in settings:
            tf = str(settings.get("time_format", "24h")).strip()
            if tf not in VALID_TIME_FORMATS:
                tf = "24h"
            settings["time_format"] = tf

        # ─────────────────────────────────────────────────────────────────────
        # SUB-TREE CONTRACT for the five validators below (https / auth / email /
        # webhooks / scheduled_reports).
        #
        # Each normalises its sub-tree by writing EVERY field back, so a caller
        # that posts a fragment (e.g. {"email": {"recipients": [...]}}) blanks the
        # omitted siblings before ConfigManager.save_config's merge ever sees the
        # value. The merge protects omitted TOP-LEVEL keys; it cannot protect
        # omitted keys inside these sub-trees.
        #
        #   Rule: omit a top-level settings key freely. NEVER post a partial
        #   sub-tree for https / auth / email / webhooks / scheduled_reports —
        #   build the whole object, as templates/settings.html does.
        #
        # Pinned by tests/test_config_partial_save.py
        # ::test_subtree_contract_partial_subtree_resets_its_siblings.
        # ─────────────────────────────────────────────────────────────────────

        # Validate HTTPS settings — reject enable with missing/invalid cert or key
        # so the next restart doesn't crash-loop on missing files.
        https_cfg = settings.get("https")
        if https_cfg and isinstance(https_cfg, dict):
            https_cfg["enabled"] = bool(https_cfg.get("enabled", False))
            https_cfg["cert_file"] = str(https_cfg.get("cert_file", "")).strip()
            https_cfg["key_file"] = str(https_cfg.get("key_file", "")).strip()
            if https_cfg["enabled"]:
                cert_p = https_cfg["cert_file"]
                key_p = https_cfg["key_file"]
                if not cert_p or not key_p:
                    return jsonify({"ok": False, "error": "HTTPS enabled but cert_file or key_file path is empty"}), 400
                try:
                    if not Path(cert_p).is_file():
                        return jsonify({"ok": False, "error": f"HTTPS cert file not found: {cert_p}"}), 400
                    if not Path(key_p).is_file():
                        return jsonify({"ok": False, "error": f"HTTPS key file not found: {key_p}"}), 400
                except Exception as e:
                    return jsonify({"ok": False, "error": f"HTTPS path check failed: {e}"}), 400

        # Validate auth settings
        auth_cfg = settings.get("auth")
        if auth_cfg and isinstance(auth_cfg, dict):
            auth_cfg["enabled"] = bool(auth_cfg.get("enabled", False))
            auth_type = str(auth_cfg.get("type", "ldap")).strip().lower()
            if auth_type not in ("ldap",):
                return jsonify({"ok": False, "error": "Auth type must be 'ldap'"}), 400
            auth_cfg["type"] = auth_type
            auth_cfg["ldap_url"] = str(auth_cfg.get("ldap_url", "")).strip()
            auth_cfg["ldap_base_dn"] = str(auth_cfg.get("ldap_base_dn", "")).strip()
            auth_cfg["ldap_user_filter"] = str(auth_cfg.get("ldap_user_filter", "(sAMAccountName={username})")).strip()
            try:
                timeout = int(auth_cfg.get("session_timeout_minutes", 480))
            except (ValueError, TypeError):
                timeout = 480
            if timeout < 5 or timeout > 1440:
                return jsonify({"ok": False, "error": "Session timeout must be 5-1440 minutes"}), 400
            auth_cfg["session_timeout_minutes"] = timeout

            # Preserve existing LDAP bind password if field is blank or the mask
            # (GET /api/config sends PASSWORD_MASK for set passwords).
            new_bind_pw = str(auth_cfg.get("ldap_bind_password", "") or "").strip()
            existing_auth = _shared._config.get_settings().get("auth", {}) or {}
            if not new_bind_pw or is_password_masked(new_bind_pw):
                auth_cfg["ldap_bind_password"] = existing_auth.get("ldap_bind_password", "")

        # Validate email settings
        email_cfg = settings.get("email")
        if email_cfg and isinstance(email_cfg, dict):
            email_cfg["enabled"] = bool(email_cfg.get("enabled", False))
            email_cfg["use_tls"] = bool(email_cfg.get("use_tls", True))
            email_cfg["send_on_critical"] = bool(email_cfg.get("send_on_critical", True))
            email_cfg["send_on_warning"] = bool(email_cfg.get("send_on_warning", False))

            smtp_server = str(email_cfg.get("smtp_server", "")).strip()
            if smtp_server and not re.match(r'^[A-Za-z0-9._-]+$', smtp_server):
                return jsonify({"ok": False, "error": "Invalid SMTP server format"}), 400
            email_cfg["smtp_server"] = smtp_server

            try:
                smtp_port = int(email_cfg.get("smtp_port", 587))
            except (ValueError, TypeError):
                return jsonify({"ok": False, "error": "SMTP port must be a number"}), 400
            if smtp_port < 1 or smtp_port > 65535:
                return jsonify({"ok": False, "error": "SMTP port must be 1-65535"}), 400
            email_cfg["smtp_port"] = smtp_port

            email_cfg["username"] = str(email_cfg.get("username", "")).strip()
            # Preserve existing email password if field is blank OR the mask
            # (GET /api/config sends PASSWORD_MASK for set passwords — see get_config above).
            new_email_pw = str(email_cfg.get("password", "")).strip()
            if not new_email_pw or is_password_masked(new_email_pw):
                existing = _shared._config.get_settings().get("email", {})
                email_cfg["password"] = existing.get("password", "")
            else:
                email_cfg["password"] = new_email_pw
            email_cfg["from_address"] = str(email_cfg.get("from_address", "")).strip()
            email_cfg["dashboard_url"] = str(email_cfg.get("dashboard_url", "http://localhost:5000")).strip()

            recipients = email_cfg.get("recipients", [])
            if not isinstance(recipients, list):
                return jsonify({"ok": False, "error": "Email recipients must be a list"}), 400
            email_cfg["recipients"] = [str(r).strip() for r in recipients if str(r).strip()]

    try:
        _shared._config.save_config(servers, settings)
    except Exception:
        logger.exception("Error saving config")
        return jsonify({"ok": False, "error": "Failed to save configuration"}), 500
    logger.info("Config saved via API: %d servers", len(servers))
    try:
        username = flask_session.get("username", "anonymous")
        _shared._db.log_audit(username, "Settings updated", "settings", "Configuration saved via API")
    except Exception:
        pass
    return jsonify({"ok": True})


# Sub-keys of settings.auth this endpoint is allowed to write. Deliberately
# excludes backup_admin and allowed_users — those remain write-protected
# everywhere except their own dedicated flows (auth.py:/admin/reset-password
# and the RBAC admin UI). Keep in sync with _SENSITIVE_AUTH_KEYS in save_config.
_LDAP_WRITABLE_KEYS = (
    "ldap_url", "ldap_base_dn", "ldap_user_filter",
    "ldap_bind_user", "ldap_bind_password",
)


@api_bp.route("/config/ldap", methods=["POST"])
def save_ldap_config():
    """Dedicated writer for the settings.auth.ldap_* directory settings.

    Why this exists: POST /api/config is a generic config writer, so it carries
    a defence-in-depth strip filter (_SENSITIVE_AUTH_KEYS) that replaces any
    posted auth.ldap_* value with what's already on disk. Combined with
    settings.html rebuilding data.settings.auth from the DOM on every save, that
    made LDAP configuration **unsaveable from the UI** — the edit was discarded
    and a success toast was shown anyway.

    The strip filter is correct and stays. ldap_url in particular belongs in the
    same risk class as backup_admin: repointing it at an attacker-controlled
    directory server hands over authentication wholesale. So rather than
    widening the generic writer, directory settings get their own endpoint that
    is admin-gated, validated, and audited — which is what the comment on
    save_config always claimed existed.

    Writes straight into the raw on-disk settings dict (the same pattern as
    POST /api/scheduled-restarts) rather than going through
    ConfigManager.save_config, which replaces the entire settings block and
    re-encrypts server passwords. This touches nothing but the five LDAP keys.
    """
    auth_err = _require_rbac_admin()
    if auth_err:
        return auth_err

    data = request.get_json()
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Missing request body"}), 400

    ldap_url = str(data.get("ldap_url", "") or "").strip()
    base_dn = str(data.get("ldap_base_dn", "") or "").strip()
    user_filter = str(data.get("ldap_user_filter", "") or "").strip()
    bind_user = str(data.get("ldap_bind_user", "") or "").strip()
    bind_pw_in = str(data.get("ldap_bind_password", "") or "").strip()

    # Reject anything that isn't an LDAP URL outright. Without this an operator
    # could paste an http:// or file:// value that silently never authenticates.
    if ldap_url and not re.match(r"^ldaps?://[^\s/]+", ldap_url, re.IGNORECASE):
        return jsonify({
            "ok": False,
            "error": "LDAP URL must start with ldap:// or ldaps:// followed by a host",
        }), 400

    if not user_filter:
        user_filter = "(sAMAccountName={username})"
    if "{username}" not in user_filter:
        return jsonify({
            "ok": False,
            "error": "LDAP user filter must contain the {username} placeholder",
        }), 400

    existing_auth = _shared._config.get_settings().get("auth", {}) or {}

    # Blank or masked bind password means "unchanged" — GET /api/config sends
    # PASSWORD_MASK for a set password, so a straight round-trip must not wipe it.
    #
    # ENCRYPTED AT REST, same Fernet envelope as servers[*].password. This used to
    # be stored plain text, justified as matching "every existing reader" — but
    # only ONE of the two readers actually needed that, and the cost was real:
    # tools/rekey.py lists this field as a canonical credential path yet skips
    # non-'enc:' values, so key rotation silently never re-protected it.
    # Both readers now decrypt (routes/api/misc.py LDAP probe, and the AD
    # discovery flow further down this file). decrypt_password() passes a legacy
    # plain-text value through unchanged, so configs written before this change
    # keep working, and ConfigManager._migrate_plaintext_passwords upgrades them
    # on next start.
    existing_pw_stored = existing_auth.get("ldap_bind_password", "") or ""
    if not bind_pw_in or is_password_masked(bind_pw_in):
        # Unchanged. Still run it through encrypt_password so a legacy plain-text
        # value on disk gets upgraded by this save instead of being rewritten
        # as-is; encrypt_password is a no-op on an already-'enc:' value.
        bind_pw = encrypt_password(existing_pw_stored) if existing_pw_stored else ""
        pw_rotated = False
    else:
        bind_pw = encrypt_password(bind_pw_in)
        # Compare PLAIN TEXT, not ciphertext. Fernet embeds a random IV and a
        # timestamp, so re-encrypting an identical password yields a different
        # token every time — comparing tokens would report a rotation on every
        # single save and make the audit trail useless.
        pw_rotated = bind_pw_in != decrypt_password(existing_pw_stored)

    new_values = {
        "ldap_url": ldap_url,
        "ldap_base_dn": base_dn,
        "ldap_user_filter": user_filter,
        "ldap_bind_user": bind_user,
        "ldap_bind_password": bind_pw,
    }

    try:
        _shared._config.create_backup()
    except Exception:
        logger.warning("Failed to back up config before LDAP save", exc_info=True)

    try:
        raw = _shared._config._get_raw_config()
        raw_settings = raw.setdefault("settings", {})
        raw_auth = raw_settings.setdefault("auth", {})
        for key in _LDAP_WRITABLE_KEYS:
            raw_auth[key] = new_values[key]
        with _shared._config._lock:
            with open(_shared._config.config_path, "w") as f:
                json.dump(raw, f, indent=2)
            _shared._config._cache = None
            _shared._config._cache_mtime = 0.0
    except Exception:
        logger.exception("Error saving LDAP config")
        return jsonify({"ok": False, "error": "Failed to save LDAP configuration"}), 500

    # Audit what changed, never the credential itself — just whether it rotated.
    try:
        changed = [
            k for k in ("ldap_url", "ldap_base_dn", "ldap_user_filter", "ldap_bind_user")
            if (existing_auth.get(k, "") or "") != new_values[k]
        ]
        if pw_rotated:
            changed.append("ldap_bind_password")
        _shared._db.log_audit(
            _current_actor(), "update_ldap_config", "settings",
            "Updated LDAP settings: " + (", ".join(changed) if changed else "no effective change"),
        )
    except Exception:
        pass

    logger.info("LDAP config saved via API")
    return jsonify({"ok": True})


@api_bp.route("/test-email", methods=["POST"])
def test_email():
    """Send a test email to verify SMTP configuration."""
    try:
        settings = _shared._config.get_settings()
        # Allow overriding with posted settings (from unsaved form)
        data = request.get_json()
        if data and "email" in data:
            settings["email"] = data["email"]

        email_cfg = settings.get("email", {})
        if not email_cfg.get("smtp_server"):
            return jsonify({"ok": False, "error": "SMTP server is not configured"})
        if not email_cfg.get("recipients"):
            return jsonify({"ok": False, "error": "No recipients configured"})

        success, message = send_test_email(settings)
        key = "message" if success else "error"
        return jsonify({"ok": success, key: message})
    except Exception:
        logger.exception("Error in POST /api/test-email")
        return jsonify({"ok": False, "error": "Internal server error"}), 500


@api_bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Test WinRM connectivity to a server. Returns success/failure with details.

    Closes audit finding RF2 (R5 + B2) from docs/AUDIT-2026-05.md. This endpoint
    used to be unauthenticated and accept arbitrary host + credentials, which
    turned Prism into:
      * a port-scanner from inside its own trusted network segment
      * a credential-spray oracle (distinct error categories told the attacker
        which guesses were close)
      * a stored-password disclosure primitive (the masked-password branch
        substituted the saved cred for an unauthenticated caller)

    Three changes:
      1. _require_auth — at minimum, the caller must be a logged-in user.
      2. host must be in inventory unless caller is rbac admin (admins can
         test new servers during onboarding; everyone else can only retest
         already-configured servers).
      3. Masked-password substitution requires rbac admin too. Regular users
         must supply the password they're testing.
    """
    auth_err = _shared._require_auth()
    if auth_err:
        return auth_err

    data = request.get_json()
    if not data or not data.get("host"):
        return jsonify({"ok": False, "error": "Missing 'host' in request body"}), 400

    host = data["host"]
    username = data.get("username", "administrator")
    password = data.get("password", "")
    use_https = bool(data.get("use_https", False))
    skip_verify = bool(data.get("https_skip_verify", False))
    port = data.get("port") or (5986 if use_https else 5985)

    # Validate host format
    if not re.match(r'^[A-Za-z0-9._-]+$', host):
        return jsonify({"ok": False, "error": "Invalid hostname format"}), 400

    # Validate port is an integer in valid range
    try:
        port = int(port)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid port value"}), 400
    if port < 1 or port > 65535:
        return jsonify({"ok": False, "error": "Port must be between 1 and 65535"}), 400

    # Inventory check: regular users can only test connections to servers
    # already in config. RBAC admins can test arbitrary hosts (needed for
    # onboarding new servers).
    inventory_hosts = {srv.host for srv in _shared._config.get_servers()}
    inventory_names = {srv.name for srv in _shared._config.get_servers()}
    is_in_inventory = host in inventory_hosts or data.get("name", "") in inventory_names
    if not is_in_inventory:
        if _shared._require_rbac_admin() is not None:
            return jsonify({
                "ok": False,
                "error": "Host not in inventory; arbitrary-host testing requires admin role.",
            }), 403

    # Masked-password substitution: rbac admin only. Regular users must supply
    # an explicit password — they can test with their own credentials but cannot
    # use Prism as an oracle for the stored credentials of other servers.
    if is_password_masked(password):
        if _shared._require_rbac_admin() is not None:
            return jsonify({
                "ok": False,
                "error": "Masked-password substitution requires admin role. Supply the password explicitly.",
            }), 403
        server_name = data.get("name", "")
        cfg = _shared._config.get_server_by_name(server_name) if server_name else None
        if cfg:
            password = cfg.password
        else:
            # Try to find by host match
            for srv in _shared._config.get_servers():
                if srv.host == host:
                    password = srv.password
                    break
            else:
                return jsonify({"ok": False, "error": "Cannot test with masked password for unknown server"}), 400

    try:
        from pypsrp.powershell import PowerShell, RunspacePool
        from pypsrp.wsman import WSMan
    except ImportError:
        return jsonify({
            "ok": False,
            "error": "pypsrp not installed",
            "detail": "Run: pip install pypsrp",
        })

    try:
        _wsman_kwargs = dict(
            port=port,
            username=username,
            password=password,
            ssl=use_https,
            auth="negotiate",
            connection_timeout=15,
            read_timeout=10,
        )
        if use_https:
            _wsman_kwargs["cert_validation"] = not skip_verify
        wsman = WSMan(host, **_wsman_kwargs)
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script("$env:COMPUTERNAME")
            output = ps.invoke()

            if ps.had_errors:
                err_msgs = [str(e) for e in ps.streams.error]
                return jsonify({
                    "ok": False,
                    "error": "PowerShell command failed",
                    "detail": "; ".join(err_msgs)[:500],
                })

            hostname = str(output[0]).strip() if output else "unknown"
            return jsonify({
                "ok": True,
                "hostname": hostname,
                "message": f"Connected successfully to {hostname}",
            })

    except Exception as e:
        # Collapsed error response: the diagnostic value of distinguishing
        # "auth failed" vs "connection refused" vs "host not found" is small
        # for an admin testing a known-good config; the *oracle* value of
        # those distinctions to an attacker (port scan, credential spray,
        # username validation) is large. So everything funnels into one
        # generic "connection test failed" with details only in the server
        # log. Closes RF2 from AUDIT-2026-05.
        error_str = str(e).lower()
        # Categorise server-side for the operator log only — never echo to client.
        if "401" in str(e) or "unauthorized" in error_str:
            log_category = "auth_failed"
        elif "connection" in error_str and ("refused" in error_str or "reset" in error_str):
            log_category = "conn_refused"
        elif "timeout" in error_str or "timed out" in error_str:
            log_category = "timeout"
        elif "name or service not known" in error_str or "getaddrinfo" in error_str:
            log_category = "dns_fail"
        else:
            log_category = "other"
        logger.warning("Test connection to %s failed (%s): %s", host, log_category, str(e)[:300])
        error = "Connection test failed"
        detail = "See server logs for details."
        return jsonify({"ok": False, "error": error, "detail": detail})


@api_bp.route("/cert-info")
def cert_info():
    """Read a PEM certificate file and return expiration info.

    Security model:
      - Auth required (this endpoint returns file contents).
      - Only .pem / .crt / .cer extensions accepted — prevents drive-by
        reads of arbitrary server files via this endpoint.
      - Paths do NOT have to be in current config so users can verify a
        new cert path BEFORE saving (was a chicken-and-egg bug previously).
    """
    auth = _require_auth()
    if auth:
        return auth
    cert_path = request.args.get("path", "").strip()
    if not cert_path:
        return jsonify({"valid": False, "error": "No certificate path provided"}), 400

    # Extension allowlist — keeps this endpoint from being a generic file reader
    if not re.search(r"\.(pem|crt|cer)$", cert_path, re.IGNORECASE):
        return jsonify({"valid": False, "error": "Only .pem, .crt, .cer files can be verified"}), 400

    p = Path(cert_path).resolve()

    if not p.exists():
        return jsonify({"valid": False, "error": "File not found"}), 404
    if not p.is_file():
        return jsonify({"valid": False, "error": "Path is not a file"}), 400

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from datetime import datetime, timezone

        pem_data = p.read_bytes()
        cert = x509.load_pem_x509_certificate(pem_data)

        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
        now = datetime.now(timezone.utc)
        days_remaining = (not_after - now).days

        # Extract subject CN
        try:
            subject_cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        except (IndexError, Exception):
            subject_cn = str(cert.subject)

        # Extract issuer CN
        try:
            issuer_cn = cert.issuer.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        except (IndexError, Exception):
            issuer_cn = str(cert.issuer)

        return jsonify({
            "valid": True,
            "subject": subject_cn,
            "issuer": issuer_cn,
            "not_before": not_before.strftime("%Y-%m-%d"),
            "not_after": not_after.strftime("%Y-%m-%d"),
            "days_remaining": days_remaining,
            "expired": days_remaining < 0,
        })

    except Exception as e:
        logger.warning("Failed to read certificate %s: %s", cert_path, e)
        return jsonify({"valid": False, "error": f"Cannot parse certificate: {type(e).__name__}"}), 400


@api_bp.route("/csrf-token")
def get_csrf_token():
    """Return a fresh CSRF token. Called periodically by base.html JS to
    keep the meta tag current on long-lived pages (dashboards left open
    all day). The token is tied to the session, so as long as the session
    cookie is valid, the token works for POST requests."""
    from flask_wtf.csrf import generate_csrf
    return jsonify({"token": generate_csrf()})


@api_bp.route("/config/backups")
def list_config_backups():
    auth = _require_auth()
    if auth: return auth
    backups = _shared._config.list_backups()
    return jsonify({"ok": True, "backups": backups})


@api_bp.route("/config/backups/restore", methods=["POST"])
def restore_config_backup():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json(force=True)
    filename = data.get("filename", "")
    if not filename:
        return jsonify({"ok": False, "error": "No filename provided"}), 400
    user = flask_session.get("username", "system")
    try:
        # Validate filename pattern to prevent path traversal
        import re
        if not re.match(r'^config_\d{8}_\d{6}\.json$', filename):
            return jsonify({"ok": False, "error": "Invalid backup filename"}), 400

        # Validate the backup file is parseable JSON with required keys BEFORE applying
        import json as _json
        backup_dir = _shared._config.config_path.parent / "data" / "config_backups"
        backup_path = backup_dir / filename
        if not backup_path.exists():
            return jsonify({"ok": False, "error": "Backup file not found"}), 404
        try:
            parsed = _json.loads(backup_path.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"ok": False, "error": "Backup file is not valid JSON"}), 400
        if not isinstance(parsed, dict) or "servers" not in parsed:
            return jsonify({"ok": False, "error": "Backup is missing 'servers' key"}), 400

        _shared._config.restore_backup(filename)
        try:
            _shared._db.log_audit(user, "restore_config_backup", "system", f"Restored config from backup: {filename}")
        except Exception:
            pass
        logger.info("Config restored from backup %s by %s", filename, user)
        return jsonify({"ok": True, "message": "Config restored from backup"})
    except Exception as e:
        logger.exception("Failed to restore config backup %s", filename)
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/config/backups/download/<filename>")
def download_config_backup(filename):
    auth = _require_auth()
    if auth: return auth
    import re
    if not re.match(r'^config_\d{8}_\d{6}\.json$', filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    backup_dir = _shared._config.config_path.parent / "data" / "config_backups"
    filepath = backup_dir / filename
    if not filepath.exists():
        return jsonify({"ok": False, "error": "Backup not found"}), 404
    return Response(
        filepath.read_text(encoding="utf-8"),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@api_bp.route("/config/backups/upload", methods=["POST"])
def upload_config_backup():
    auth = _require_auth()
    if auth: return auth
    user = flask_session.get("username", "system")
    try:
        data = request.get_json(force=True)
        config_data = data.get("config")
        if not config_data or not isinstance(config_data, dict):
            return jsonify({"ok": False, "error": "Invalid config data"}), 400
        # Validate it has expected structure
        if "servers" not in config_data:
            return jsonify({"ok": False, "error": "Config must contain 'servers' key"}), 400
        # Create backup of current config first
        _shared._config.create_backup()
        # Write the uploaded config
        import json as _json
        with open(_shared._config.config_path, "w") as f:
            _json.dump(config_data, f, indent=2)
        _shared._config._cache = None
        _shared._config._cache_mtime = 0.0
        try:
            _shared._db.log_audit(user, "upload_config", "system",
                         f"Uploaded config replacement ({len(config_data.get('servers', []))} servers)")
        except Exception:
            pass
        logger.info("Config uploaded and applied by %s", user)
        return jsonify({"ok": True, "message": "Config uploaded and applied"})
    except Exception as e:
        logger.exception("Failed to upload config backup")
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/test-webhook", methods=["POST"])
def test_webhook():
    auth = _require_auth()
    if auth: return auth
    data = request.get_json(force=True)
    webhook_url = data.get("webhook_url", "").strip()
    if not webhook_url:
        return jsonify({"ok": False, "error": "No webhook URL provided"}), 400
    from webhooks import send_test_webhook
    result = send_test_webhook(webhook_url, settings=_shared._config.get_settings())
    return jsonify(result)


@api_bp.route("/discover-servers", methods=["POST"])
def discover_servers():
    """Query Active Directory for computer objects via LDAP.

    Closes audit finding R8 (S3-2 from AUDIT-2026-05). Was _require_auth
    only — any LDAP user borrowed the configured service account's
    directory-read rights for full computer enumeration. Now requires
    rbac admin (operator with intent to onboard servers).
    """
    auth = _shared._require_rbac_admin()
    if auth: return auth

    settings = _shared._config.get_settings()
    auth_settings = settings.get("auth", {})
    ldap_url = auth_settings.get("ldap_url", "").strip()
    base_dn = auth_settings.get("ldap_base_dn", "").strip()

    if not ldap_url or not base_dn:
        return jsonify({"ok": False, "error": "LDAP URL and Base DN must be configured in Security settings"}), 400

    # Get optional search filter from request
    data = request.get_json(force=True) if request.is_json else {}
    search_filter = data.get("filter", "(&(objectClass=computer)(operatingSystem=*Server*))")

    try:
        from ldap3 import Server, Connection, ALL, SUBTREE

        server = Server(ldap_url, get_info=ALL, connect_timeout=10)

        # Use dedicated LDAP bind credentials from auth settings
        bind_user = auth_settings.get("ldap_bind_user", "").strip()
        # Decrypt: the bind password is stored Fernet-encrypted (see
        # save_ldap_config). This read used the stored value RAW, which is why
        # the field was kept plain text on disk — that is now fixed at both ends.
        # decrypt_password() returns a legacy plain-text value unchanged, so this
        # works before and after the migration.
        bind_pass = decrypt_password(
            auth_settings.get("ldap_bind_password", "") or ""
        ).strip()

        if bind_user and bind_pass:
            # Use SIMPLE bind with UPN format (user@domain.com)
            # This avoids the MD4/NTLM hash issue on modern Python/OpenSSL
            conn = Connection(server, user=bind_user, password=bind_pass,
                              auto_bind=True, read_only=True, receive_timeout=15)
        else:
            return jsonify({"ok": False, "error": "LDAP Bind credentials not configured. Set them in Security → Authentication → LDAP Bind Account."}), 400

        # Search for computer objects
        conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["cn", "dNSHostName", "operatingSystem", "operatingSystemVersion", "whenCreated"],
            size_limit=500,
        )

        # Get existing server names/hosts for deduplication
        existing_servers = _shared._config.get_servers()
        existing_hosts = {s.host.lower() for s in existing_servers}
        existing_names = {s.name.lower() for s in existing_servers}

        discovered = []
        for entry in conn.entries:
            name = str(entry.cn) if hasattr(entry, "cn") else ""
            host = str(entry.dNSHostName) if hasattr(entry, "dNSHostName") and entry.dNSHostName else ""
            os_name = str(entry.operatingSystem) if hasattr(entry, "operatingSystem") and entry.operatingSystem else ""
            os_version = str(entry.operatingSystemVersion) if hasattr(entry, "operatingSystemVersion") and entry.operatingSystemVersion else ""

            if not name and not host:
                continue

            # Check if already monitored
            is_existing = (
                (host and host.lower() in existing_hosts) or
                (name and name.lower() in existing_names)
            )

            discovered.append({
                "name": name,
                "host": host or name,
                "os": os_name,
                "os_version": os_version,
                "already_monitored": is_existing,
            })

        conn.unbind()

        # Sort: non-monitored first, then by name
        discovered.sort(key=lambda x: (x["already_monitored"], x["name"].lower()))

        try:
            username = flask_session.get("username", "anonymous")
            _shared._db.log_audit(username, f"AD discovery: found {len(discovered)} computers", "discovery")
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "servers": discovered,
            "total": len(discovered),
            "new": sum(1 for d in discovered if not d["already_monitored"]),
        })

    except ImportError:
        return jsonify({"ok": False, "error": "ldap3 library not installed. Run: pip install ldap3"}), 500
    except Exception as e:
        logger.exception("AD discovery failed")
        return jsonify({"ok": False, "error": f"LDAP query failed: {str(e)}"}), 500


@api_bp.route("/config-changes")
def get_config_changes():
    """Cross-server config change log with optional filters."""
    server = request.args.get("server")
    hours = request.args.get("hours", 168, type=int)
    snap_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int)
    changes = _shared._db.get_config_changes(server_name=server, hours=hours,
                                     limit=limit, snapshot_type=snap_type)
    return jsonify({"ok": True, "changes": changes})
