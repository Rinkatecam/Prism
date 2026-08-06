"""Authentication module for Prism. Provides LDAP/AD auth middleware.

Auth is disabled by default (pass-through). When enabled via config.json,
it checks for valid Flask sessions and validates credentials against an
LDAP/AD server using the ldap3 library.
"""

import re
import logging
from datetime import datetime, timezone

# S2-13 (W4) — embedded breach corpus. Top common passwords from SecLists
# (rockyou.txt top + common Windows admin passwords). Kept inline so the
# backup-admin policy works offline. ~200 entries, lower-cased; we compare
# case-insensitively below.
_COMMON_PASSWORDS: frozenset[str] = frozenset({
    "123456", "123456789", "12345678", "1234567", "1234567890", "12345",
    "password", "password1", "password123", "passw0rd", "p@ssw0rd",
    "p@ssword", "passwd", "qwerty", "qwerty123", "qwertyuiop", "qwerty1",
    "abc123", "111111", "1q2w3e4r", "1q2w3e4r5t", "qazwsx", "zxcvbn",
    "zxcvbnm", "asdfgh", "asdfghjkl", "letmein", "letmein1", "welcome",
    "welcome1", "welcome123", "admin", "admin1", "admin123", "administrator",
    "root", "toor", "guest", "test", "test123", "default", "default1",
    "monkey", "dragon", "master", "shadow", "superman", "batman", "trustno1",
    "iloveyou", "princess", "sunshine", "starwars", "freedom", "whatever",
    "hello", "hello123", "football", "baseball", "basketball", "soccer",
    "ninja", "azerty", "michael", "mustang", "access", "flower", "loveme",
    "hottie", "lovely", "654321", "666666", "777777", "888888", "999999",
    "121212", "131313", "112233", "qwertyuiop123", "iloveu", "babygirl",
    "soccer1", "summer", "winter", "spring", "autumn", "secret", "letmein123",
    "changeme", "changeme1", "changeme123", "tempest", "passw0rd1", "passw0rd123",
    "windows", "windowsxp", "windows7", "windows10", "microsoft", "office",
    "office365", "exchange", "outlook", "active", "directory", "server",
    "server1", "server123", "linux", "unix", "computer", "internet",
    "google", "facebook", "yahoo", "hotmail", "gmail", "yahoo123",
    "abcd1234", "1234abcd", "qwerty12", "qwer1234", "asdf1234", "zxcv1234",
    "11111", "22222", "33333", "44444", "55555", "00000", "abcdef",
    "abc12345", "passwd1", "passwd123", "p455w0rd", "p455word", "pa55w0rd",
    "pa55word", "p@$$w0rd", "p@$$word", "p@ssw0rd1", "p@ssword1",
    "summer2020", "summer2021", "summer2022", "summer2023", "summer2024",
    "winter2020", "winter2021", "winter2022", "winter2023", "winter2024",
    "spring2020", "spring2021", "spring2022", "spring2023", "spring2024",
    "fall2020", "fall2021", "fall2022", "fall2023", "fall2024",
    "autumn2020", "autumn2021", "autumn2022", "autumn2023", "autumn2024",
    "company1", "company123", "corporate", "business", "manager", "managers",
    "support", "support1", "support123", "helpdesk", "helpdesk1", "service",
    "service1", "service123", "operator", "operator1", "owner", "user",
    "user1", "user123", "users", "admin1234", "administrator1", "rootroot",
    "secret1", "secret123", "confidential", "sysadmin", "sysadmin1",
    "prism", "prism1", "prism123", "monitor", "monitor1", "monitoring",
    "backup", "backup1", "backup123", "backupadmin", "breakglass",
    "tier0", "tier-0", "domain", "domain1", "domainadmin", "dcadmin",
    "ad-admin", "adadmin", "qaz123", "wsx123", "edc123", "rfv123",
    "tgb123", "yhn123", "ujm123", "test1", "test1234", "demo", "demo1",
    "demo123", "sample", "example", "trial", "newpass", "newpass1",
    "newpassword", "oldpass", "oldpassword", "tempo", "temp", "temp1",
    "temp123", "tempadmin", "passwordnew", "password!", "password!1",
    "password@123", "Password1", "Password123", "Welcome1", "Welcome123",
    "Admin1", "Admin123", "Admin@123", "P@ssw0rd", "P@ssword", "P@ssw0rd1",
})


def validate_backup_admin_password(
    new_password: str,
    *,
    previous_hash: str | None = None,
) -> tuple[bool, str | None]:
    """S2-13: enforce strong policy on the backup-admin (break-glass) account.

    Returns (ok, error_message). Caller renders error_message to the user verbatim.
    Rules:
      - len >= 12
      - at least one digit AND one non-alphanumeric character
      - not in the embedded common-password list (case-insensitive)
      - not the same as the previous password (if previous_hash supplied)
    """
    if not new_password or len(new_password) < 12:
        return False, "Password must be at least 12 characters."
    if not any(c.isdigit() for c in new_password):
        return False, "Password must contain at least one digit."
    if not any(not c.isalnum() for c in new_password):
        return False, "Password must contain at least one symbol (non-alphanumeric)."
    if new_password.lower() in _COMMON_PASSWORDS:
        return False, "Password is in a list of common/breached passwords."
    if previous_hash:
        try:
            from werkzeug.security import check_password_hash
            if check_password_hash(previous_hash, new_password):
                return False, "New password must be different from the previous password."
        except Exception:
            # Hash format unknown / corrupted — don't block rotation
            pass
    return True, None

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    jsonify,
    url_for,
)

logger = logging.getLogger("prism.auth")

auth_bp = Blueprint("auth", __name__)

# Set by register_auth()
_config = None
_limiter = None
_db = None  # injected so auth events can be persisted to audit_log (S1-8)


def _lockout_params(auth_cfg: dict) -> tuple[int, int, int]:
    """Return (threshold, window_minutes, duration_minutes) for S2-12 lockout."""
    try:
        threshold = int(auth_cfg.get("lockout_threshold", 10))
    except (ValueError, TypeError):
        threshold = 10
    try:
        window_min = int(auth_cfg.get("lockout_window_minutes", 30))
    except (ValueError, TypeError):
        window_min = 30
    try:
        duration_min = int(auth_cfg.get("lockout_duration_minutes", 15))
    except (ValueError, TypeError):
        duration_min = 15
    return threshold, window_min, duration_min


def _maybe_send_lockout_alert(username: str, failures: int, window_min: int):
    """Best-effort email alert on lockout. Skips silently if email isn't configured."""
    if _config is None:
        return
    try:
        settings = _config.get_settings()
        email_cfg = settings.get("email", {})
        if not email_cfg.get("enabled", False):
            return
        from email_alerts import send_alert_email
        # Synthetic event: alert_scoring + dispatch glue expects an event-shape dict.
        synthetic_event = {
            "server_name": "prism-auth",
            "event_type": "critical",
            "metric": "account_lockout",
            "value": float(failures),
            "threshold": 0.0,
            "message": (f"Account '{username}' locked after {failures} failed login "
                        f"attempts in the last {window_min} minutes."),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Correct signature is send_alert_email(event, server_name, settings).
        # The previous 2-arg call always raised TypeError (swallowed below), so
        # lockout alert emails silently never sent.
        send_alert_email(synthetic_event, synthetic_event["server_name"], settings)
    except Exception:
        logger.debug("lockout email alert failed", exc_info=True)


def _record_failure(username: str):
    """Record a failed login (S2-12)."""
    if _db is None or not username:
        return
    try:
        from flask import request as _req
        ip = _req.remote_addr if _req else None
    except Exception:
        ip = None
    try:
        _db.record_auth_failure(username, ip)
    except Exception:
        logger.debug("record_auth_failure failed", exc_info=True)


def _clear_failures(username: str):
    if _db is None or not username:
        return
    try:
        _db.clear_failures_for(username)
    except Exception:
        logger.debug("clear_failures_for failed", exc_info=True)


def _audit(username: str, action: str, details: str = ""):
    """Lightweight wrapper. Quietly skips if _db isn't wired (e.g. early startup,
    standalone CLI use). Forensic context (IP, request ID, session) is auto-pulled
    by Database.log_audit from the active Flask request."""
    if _db is None:
        return
    try:
        _db.log_audit(username or "anonymous", action, "auth", details or None)
    except Exception:
        logger.debug("audit log write failed for %s/%s", username, action)


# ── S2-15 (W7): LDAP fail-closed startup + continuous health probe ────────
# Module-level state so the collector loop can call ldap_health_probe() on a
# 5-min cadence without owning its own thread (per audit instructions).
_ldap_health: dict = {
    "ok": False,
    "last_check": None,    # ISO ts
    "last_error": "",
    "url": "",
}
_last_ldap_check: float = 0.0  # epoch — read by collector to gate the probe


def _parse_ldap_host_port(ldap_url: str) -> tuple[str | None, int]:
    """Extract (host, port) from an ldap:// or ldaps:// URL. Returns (None, 0) on parse failure."""
    if not ldap_url:
        return None, 0
    try:
        from urllib.parse import urlparse
        u = urlparse(ldap_url if "://" in ldap_url else f"ldap://{ldap_url}")
        host = u.hostname
        port = u.port or (636 if (u.scheme or "").lower() == "ldaps" else 389)
        return host, port
    except Exception:
        return None, 0


def ldap_health_probe(config) -> dict:
    """TCP-connect to the configured LDAP host:port without binding (no creds needed).

    Updates the module-level `_ldap_health` cache and returns a snapshot dict.
    Writes an audit row only on state TRANSITIONS (not every probe), per the
    audit's 'on state transition' requirement.

    Safe to call from any thread (collector loop calls it; tests can call it
    directly). Idempotent — failures don't raise, they just record `ok=False`.
    """
    import socket
    auth_cfg = (config.get_settings() if config else {}).get("auth", {})
    if not auth_cfg.get("enabled", False):
        snapshot = {"ok": True, "last_check": datetime.now(timezone.utc).isoformat(),
                    "last_error": "auth disabled", "url": ""}
        _ldap_health.update(snapshot)
        return dict(_ldap_health)

    ldap_url = auth_cfg.get("ldap_url", "") or ""
    host, port = _parse_ldap_host_port(ldap_url)
    prev_ok = _ldap_health.get("ok")

    now_iso = datetime.now(timezone.utc).isoformat()
    if not host:
        snapshot = {"ok": False, "last_check": now_iso,
                    "last_error": "no ldap_url configured", "url": ldap_url}
    else:
        try:
            with socket.create_connection((host, port), timeout=5):
                snapshot = {"ok": True, "last_check": now_iso,
                            "last_error": "", "url": ldap_url}
        except Exception as e:
            snapshot = {"ok": False, "last_check": now_iso,
                        "last_error": f"{type(e).__name__}: {str(e)[:120]}",
                        "url": ldap_url}

    _ldap_health.update(snapshot)
    # Audit row only on transitions (suppresses noise from steady-state probes).
    if prev_ok is not None and snapshot["ok"] != prev_ok:
        _audit("system", "ldap_health_changed",
               f"ok={snapshot['ok']} url={snapshot['url']} err={snapshot['last_error'][:80]}")
    return dict(_ldap_health)


def get_ldap_health() -> dict:
    """Read-only snapshot of the most recent ldap_health_probe result."""
    return dict(_ldap_health)


def assert_ldap_startup_safe(config) -> None:
    """S2-15 (W7) startup gate.

    Refuses to start if `auth.enabled=true` AND `ldap_url` is empty AND no
    backup admin is configured (no recovery path). Otherwise logs a CRITICAL
    if LDAP is unreachable but allows start (so backup-admin can recover).

    Raises SystemExit on the unrecoverable case.
    """
    auth_cfg = config.get_settings().get("auth", {})
    if not auth_cfg.get("enabled", False):
        return
    has_backup = config.has_backup_admin() if hasattr(config, "has_backup_admin") else False
    ldap_url = auth_cfg.get("ldap_url", "") or ""
    if not ldap_url and not has_backup:
        msg = ("Auth is enabled but no ldap_url is configured AND no backup admin "
               "exists. Refusing to start (no recovery path). Either configure LDAP "
               "(settings.auth.ldap_url) or run /setup to create a backup admin.")
        logger.critical(msg)
        raise SystemExit(msg)
    # Run an initial probe to populate state and surface unreachable LDAP.
    snap = ldap_health_probe(config)
    if not snap["ok"] and ldap_url:
        logger.critical(
            "LDAP server unreachable on startup (%s): %s — backup admin remains the recovery path",
            ldap_url, snap["last_error"],
        )


def register_auth(app, config, limiter=None, db=None):
    """Register the auth blueprint and before_request hook."""
    global _config, _limiter, _db
    _config = config
    _limiter = limiter
    _db = db
    app.register_blueprint(auth_bp)
    if limiter:
        limiter.limit("5 per minute")(login_post)

    @app.before_request
    def check_setup():
        """Redirect to /setup if no backup admin exists yet (first-run).
        Only blocks unauthenticated users — LDAP-authenticated sessions pass through."""
        if request.path.startswith("/static/"):
            return None
        if request.path in ("/setup", "/login", "/logout"):
            return None
        if request.path.startswith(("/login", "/logout")):
            return None
        # If user already has a valid session (e.g. LDAP login), let them through
        if session.get("username"):
            return None
        if not _config.has_backup_admin():
            # No admin account and not logged in — force first-run setup
            if request.path.startswith("/api/"):
                return jsonify({"error": "Initial setup required", "setup_required": True}), 403
            return redirect(url_for("auth.setup"))

    @app.before_request
    def check_auth():
        """Gate requests when auth is enabled. Skips login/logout/static."""
        auth_cfg = _config.get_settings().get("auth", {})
        if not auth_cfg.get("enabled", False):
            return None  # Auth disabled -- pass through

        # Always allow these paths without auth
        allowed_prefixes = ("/login", "/logout", "/static/", "/setup")
        if any(request.path.startswith(p) for p in allowed_prefixes):
            return None

        # Check for valid session
        if not session.get("username"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login"))

        # ── S2-1 (BL3): session containment ─────────────────────────────
        # Two server-side gates layered on top of the cookie:
        #   1. revoked_sessions  — exact (username, login_time) was killed by an admin
        #   2. disabled_users    — username is blocked entirely
        # Either causes session.clear() + 401/redirect, with an audit row.
        if _db is not None:
            current_user = session.get("username", "")
            current_login_time = session.get("login_time", "")
            try:
                if current_login_time and _db.is_session_revoked(current_user, current_login_time):
                    session.clear()
                    _audit(current_user, "session_revoked_active",
                           "session matched revoked_sessions row")
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "Session revoked"}), 401
                    return redirect(url_for("auth.login"))
                if current_user and _db.is_user_disabled(current_user):
                    session.clear()
                    _audit(current_user, "disabled_user_blocked",
                           "user is in disabled_users table")
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "Account disabled"}), 401
                    return redirect(url_for("auth.login"))
            except Exception:
                # DB unreachable mid-request — fail open on the containment check
                # rather than locking everyone out. The audit triggers won't fire.
                logger.debug("session containment check failed", exc_info=True)

        # Check session timeout (30 days if "remember me", otherwise configured timeout)
        if session.get("remember_me"):
            timeout_minutes = 30 * 24 * 60  # 30 days
        else:
            timeout_minutes = auth_cfg.get("session_timeout_minutes", 480)
        login_time_str = session.get("login_time")
        if login_time_str:
            try:
                login_time = datetime.fromisoformat(login_time_str)
                elapsed = (datetime.now(timezone.utc) - login_time).total_seconds() / 60
                if elapsed > timeout_minutes:
                    expired_user = session.get("username", "unknown")
                    session.clear()
                    _audit(expired_user, "session_expired", f"after {elapsed:.0f} min")
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "Session expired"}), 401
                    return redirect(url_for("auth.login"))
            except (ValueError, TypeError):
                pass  # Malformed timestamp -- let it pass, next login will fix it

        # S3-10 (W5) — idle timeout. Above is the absolute timeout (login_time);
        # this is the sliding/idle one. A logged-in admin who steps away
        # comes back to a still-valid session after hours, even if their
        # login_time was 7h ago. Track last_activity per request, expire on
        # idle > N minutes. Default 30 min for normal users, 15 min for the
        # backup admin (per CIS Workstation §9 for privileged accounts).
        # Tunable via auth.idle_timeout_minutes / auth.idle_timeout_minutes_backup_admin.
        #
        # Remember-me handling: when the user ticked "Remember me" at login,
        # the user's INTENT is "stay logged in across browser sessions, up
        # to the 30-day absolute cap above." A 30-min idle timeout would
        # silently undo that intent — the user comes back the next morning
        # to find themselves on the login page despite checking the box.
        # So if remember_me is set, the idle timeout is bypassed for normal
        # users. The 30-day absolute timeout still bounds total exposure.
        #
        # EXCEPTION: backup admin keeps its 15-min idle floor even with
        # remember_me. Privileged accounts shouldn't get a "stay logged in
        # for 30 days" loophole that bypasses idle containment — the audit
        # checklist (W5) specifically calls this out.
        is_backup = bool(session.get("is_backup_admin"))
        remember_me = bool(session.get("remember_me"))
        skip_idle_for_remember_me = remember_me and not is_backup

        idle_default = 15 if is_backup else 30
        idle_minutes = auth_cfg.get(
            "idle_timeout_minutes_backup_admin" if is_backup else "idle_timeout_minutes",
            idle_default,
        )
        # 0 disables idle timeout (only absolute applies).
        try:
            idle_minutes = int(idle_minutes)
        except (TypeError, ValueError):
            idle_minutes = idle_default
        if skip_idle_for_remember_me:
            # Still record last_activity so the audit trail keeps moving
            # and an operator can see when the user last touched the app,
            # but don't enforce expiry on it.
            session["last_activity"] = datetime.now(timezone.utc).isoformat()
            idle_minutes = 0
        if idle_minutes > 0:
            last_activity_str = session.get("last_activity")
            now_iso = datetime.now(timezone.utc).isoformat()
            if last_activity_str:
                try:
                    last_activity = datetime.fromisoformat(last_activity_str)
                    idle_elapsed = (datetime.now(timezone.utc) - last_activity).total_seconds() / 60
                    if idle_elapsed > idle_minutes:
                        expired_user = session.get("username", "unknown")
                        session.clear()
                        _audit(expired_user, "session_idle_timeout",
                               f"idle for {idle_elapsed:.0f} min (threshold {idle_minutes})")
                        if request.path.startswith("/api/"):
                            return jsonify({"error": "Session idle timeout"}), 401
                        return redirect(url_for("auth.login"))
                except (ValueError, TypeError):
                    pass
            # Touch on every non-static request so the idle clock resets.
            session["last_activity"] = now_iso

        return None


def _check_user_allowed(conn, bind_dn, username, allowed_lower, base_dn):
    """Check if the authenticated user is in the allowed users/groups list.

    Matches by: exact username, DOMAIN\\username, or AD group membership
    (including nested groups via LDAP_MATCHING_RULE_IN_CHAIN).
    """
    import ldap3
    from ldap3.utils.conv import escape_filter_chars

    # Extract bare username (strip DOMAIN\\ prefix)
    bare = username.split("\\")[-1].lower() if "\\" in username else username.split("@")[0].lower()
    full_lower = username.lower()

    # Every value interpolated into an LDAP filter below is escaped. RFC 4515
    # metacharacters — ( ) * \ NUL — would otherwise change the filter's
    # STRUCTURE rather than being matched literally.
    #
    # Reachability is narrow but real: this function only runs after a
    # successful bind, so an attacker needs valid directory credentials whose
    # sAMAccountName also carries a metacharacter (AD largely forbids those, so
    # exploitation is unlikely in practice). What it protects is the
    # authorization boundary, not authentication: this function decides whether
    # an already-authenticated user passes `allowed_users`, and a filter that
    # always matches would turn "authenticated" into "authorized" for anyone in
    # the directory. `allowed_bare` and `user_dn` are lower-risk (admin config
    # and directory data respectively) but are escaped for the same reason —
    # a CN or DN containing a paren should never reshape a query.
    bare_esc = escape_filter_chars(bare)

    logger.debug("Checking allowed list for user '%s' (bare='%s')", username, bare)

    # Direct username match
    if bare in allowed_lower or full_lower in allowed_lower:
        logger.debug("User '%s' matched directly in allowed list", username)
        return True

    # Check AD group membership if base_dn is available
    if not base_dn:
        logger.warning("No base_dn configured, cannot check group membership")
        return False

    try:
        # Step 1: Find the user's DN and memberOf
        user_filter = f"(sAMAccountName={bare_esc})"
        search_ok = conn.search(
            base_dn, user_filter,
            search_scope=ldap3.SUBTREE,
            attributes=["distinguishedName", "memberOf"],
        )
        logger.debug("LDAP search result: ok=%s, entries=%d", search_ok, len(conn.entries))

        if not conn.entries:
            logger.warning("User '%s' not found in LDAP search", bare)
            return False

        entry = conn.entries[0]
        user_dn = str(entry.distinguishedName) if hasattr(entry, "distinguishedName") else ""
        logger.debug("User DN: %s", user_dn)

        # Get direct group memberships
        try:
            member_of = entry.memberOf.values if hasattr(entry, "memberOf") and entry.memberOf else []
        except Exception:
            member_of = []

        logger.debug("Direct memberOf groups (%d): %s", len(member_of),
                      [str(g)[:60] for g in member_of[:10]])

        # Check direct group memberships
        for group_dn in member_of:
            cn_match = re.match(r"CN=([^,]+)", str(group_dn), re.IGNORECASE)
            if cn_match:
                group_name = cn_match.group(1).lower()
                for allowed in allowed_lower:
                    allowed_bare = allowed.split("\\")[-1] if "\\" in allowed else allowed
                    if group_name == allowed_bare:
                        logger.info("User '%s' allowed via direct group '%s'", username, cn_match.group(1))
                        return True

        # Step 2: Check nested groups using LDAP_MATCHING_RULE_IN_CHAIN (1.2.840.113556.1.4.1941)
        # For each allowed entry that looks like a group name, search if user is a recursive member
        if user_dn:
            for allowed in allowed_lower:
                allowed_bare = allowed.split("\\")[-1] if "\\" in allowed else allowed
                # Find the group DN by its CN
                group_filter = f"(&(objectClass=group)(cn={escape_filter_chars(allowed_bare)}))"
                conn.search(base_dn, group_filter, search_scope=ldap3.SUBTREE,
                            attributes=["distinguishedName"])
                if conn.entries:
                    group_dn_str = str(conn.entries[0].distinguishedName)
                    # Now check recursive membership
                    recursive_filter = (
                        f"(&(distinguishedName={escape_filter_chars(user_dn)})"
                        f"(memberOf:1.2.840.113556.1.4.1941:="
                        f"{escape_filter_chars(group_dn_str)}))"
                    )
                    conn.search(base_dn, recursive_filter, search_scope=ldap3.SUBTREE,
                                attributes=["distinguishedName"])
                    if conn.entries:
                        logger.info("User '%s' allowed via nested group '%s'", username, allowed_bare)
                        return True

        logger.info("User '%s' not found in any allowed group", username)
    except Exception as e:
        logger.warning("Could not check group membership for '%s': %s", username, str(e)[:200])

    return False


@auth_bp.route("/login", methods=["GET"])
def login():
    """Render the login page."""
    auth_cfg = _config.get_settings().get("auth", {})
    if not auth_cfg.get("enabled", False):
        return redirect("/")
    return render_template("login.html")


@auth_bp.route("/login", methods=["POST"])
def login_post():
    """Validate credentials against LDAP/AD."""
    auth_cfg = _config.get_settings().get("auth", {})
    if not auth_cfg.get("enabled", False):
        return redirect("/")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        return render_template("login.html", error="Username and password are required.")

    # Validate username to prevent LDAP injection
    if not re.match(r'^[a-zA-Z0-9._@\\-]+$', username):
        return render_template("login.html", error="Invalid username format.")

    logger.info("Login attempt for user: %s", username[:30])

    # ── S2-1: refuse login outright if the account has been admin-disabled ──
    if _db is not None:
        try:
            if _db.is_user_disabled(username):
                _audit(username, "login_denied_disabled", "user in disabled_users")
                return render_template("login.html", error="Account is disabled. Contact your administrator.")
        except Exception:
            pass

    # ── S2-12 (W3): per-username lockout BEFORE bcrypt/scrypt verification ──
    # Default thresholds (config-tunable): 10 failures in 30 min → 15 min lockout.
    # We compare count_recent_failures(window) against threshold; the "duration"
    # is implicit: the lockout naturally lifts as old failures fall outside the
    # rolling window. We additionally lock for `lockout_duration_minutes` from
    # the most recent failure: if the most recent failure was <duration ago AND
    # we're at threshold, deny.
    threshold, window_min, duration_min = _lockout_params(auth_cfg)
    if _db is not None and threshold > 0:
        try:
            n_recent = _db.count_recent_failures(username, since_minutes=window_min)
            if n_recent >= threshold:
                _audit(username, "account_locked",
                       f"{n_recent} failures in last {window_min} min")
                _maybe_send_lockout_alert(username, n_recent, window_min)
                return render_template(
                    "login.html",
                    error=f"Account temporarily locked. Try again in {duration_min} minutes.",
                ), 429
        except Exception:
            pass

    ldap_url = auth_cfg.get("ldap_url", "")
    if not ldap_url:
        return render_template("login.html", error="LDAP server not configured.")

    # --- Check backup admin first (local account, works without LDAP) ---
    backup = auth_cfg.get("backup_admin", {})
    backup_user = backup.get("username", "")
    backup_pass_hash = backup.get("password_hash", "")
    if backup_user and backup_pass_hash and username.lower() == backup_user.lower():
        from werkzeug.security import check_password_hash
        if check_password_hash(backup_pass_hash, password):
            remember = request.form.get("remember_me") == "on"
            session.clear()
            session["username"] = username
            session["login_time"] = datetime.now(timezone.utc).isoformat()
            session["is_backup_admin"] = True
            session["remember_me"] = remember
            session.permanent = True
            logger.info("Backup admin '%s' authenticated successfully (remember=%s)", username, remember)
            _audit(username, "login_success", "backup_admin login")
            _clear_failures(username)
            return redirect("/")
        else:
            logger.warning("Failed backup admin login attempt for user: %s", username)
            _audit(username, "login_failed", "backup_admin: bad password")
            _record_failure(username)
            return render_template("login.html", error="Invalid username or password.")

    # --- LDAP authentication ---
    # Attempt LDAP simple bind
    try:
        import ldap3
        from ldap3 import Server, Connection, SIMPLE, SYNC

        server = Server(ldap_url, get_info=ldap3.NONE, connect_timeout=10)

        # Build the bind DN. For AD simple bind, use DOMAIN\username format.
        # If the username already contains a backslash or @, use it as-is.
        if "\\" in username or "@" in username:
            bind_dn = username
        else:
            # Try to extract domain from the LDAP URL or base DN
            base_dn = auth_cfg.get("ldap_base_dn", "")
            # Extract domain components from base DN (e.g., DC=ad,DC=example,DC=com -> AD)
            domain = ""
            if base_dn:
                parts = [p.split("=")[1] for p in base_dn.split(",")
                         if p.strip().upper().startswith("DC=")]
                if parts:
                    domain = parts[0].upper()
            if domain:
                bind_dn = f"{domain}\\{username}"
            else:
                bind_dn = username

        conn = Connection(
            server,
            user=bind_dn,
            password=password,
            authentication=SIMPLE,
            client_strategy=SYNC,
            auto_bind=False,
            raise_exceptions=False,
            read_only=True,
            receive_timeout=10,
        )

        bind_ok = conn.bind()
        logger.debug("LDAP bind for '%s' -> ok=%s, result=%s", bind_dn, bind_ok, conn.result)

        if bind_ok:
            # Check allowed users/groups list (if configured)
            # `or []` — a null on disk makes .get(k, []) return None, not [].
            allowed_list = auth_cfg.get("allowed_users") or []
            if allowed_list:
                # Normalize for case-insensitive comparison
                allowed_lower = [a.strip().lower() for a in allowed_list if a.strip()]
                if allowed_lower:
                    user_allowed = _check_user_allowed(
                        conn, bind_dn, username, allowed_lower,
                        auth_cfg.get("ldap_base_dn", ""),
                    )
                    if not user_allowed:
                        conn.unbind()
                        logger.warning("User %s authenticated but not in allowed list", username)
                        _audit(username, "login_denied", "ldap_bind ok but not in allowed_users")
                        return render_template("login.html", error="Access denied. Your account is not authorized.")

            # Authentication successful — regenerate session to prevent fixation
            remember = request.form.get("remember_me") == "on"
            session.clear()
            session["username"] = username
            session["login_time"] = datetime.now(timezone.utc).isoformat()
            session["remember_me"] = remember
            session.permanent = True
            conn.unbind()
            logger.info("User %s authenticated successfully (remember=%s)", username, remember)
            _audit(username, "login_success", f"ldap (remember={remember})")
            _clear_failures(username)
            return redirect("/")
        else:
            logger.warning("Failed login attempt for user: %s", username)
            conn.unbind()
            _audit(username, "login_failed", "ldap_bind failed")
            _record_failure(username)
            return render_template("login.html", error="Invalid username or password.")

    except ImportError:
        logger.error("ldap3 library not installed. Run: pip install ldap3")
        return render_template("login.html", error="LDAP library not installed on server.")
    except Exception as e:
        # Log detail server-side but show generic message to user
        sanitized_error = str(e)[:200]
        if "password" in sanitized_error.lower():
            sanitized_error = f"{type(e).__name__}: [details redacted]"
        logger.error("LDAP connection failed: %s", sanitized_error)
        return render_template(
            "login.html",
            error="Could not connect to LDAP server. Contact your administrator.",
        )


@auth_bp.route("/setup", methods=["GET"])
def setup():
    """Render the first-run admin setup page."""
    if _config.has_backup_admin():
        return redirect("/")
    return render_template("setup.html")


@auth_bp.route("/setup", methods=["POST"])
def setup_post():
    """Create the initial backup admin account (first-run only)."""
    if _config.has_backup_admin():
        return render_template("setup.html", error="An admin account already exists.")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not username or not password:
        return render_template("setup.html", error="Username and password are required.")
    if password != confirm:
        return render_template("setup.html", error="Passwords do not match.")
    # Validate username format
    if not re.match(r'^[a-zA-Z0-9._@\\-]+$', username):
        return render_template("setup.html", error="Invalid username format.")
    # S2-13 (W4): strong policy on the break-glass account.
    ok, err = validate_backup_admin_password(password, previous_hash=None)
    if not ok:
        return render_template("setup.html", error=err)

    from werkzeug.security import generate_password_hash
    pw_hash = generate_password_hash(password, method="scrypt")
    _config.set_backup_admin(username, pw_hash)
    logger.info("First-run setup: backup admin '%s' created", username)

    # Auto-login the newly created admin
    session.clear()
    session["username"] = username
    session["login_time"] = datetime.now(timezone.utc).isoformat()
    session["is_backup_admin"] = True
    session["remember_me"] = False
    session.permanent = True

    return redirect("/")


@auth_bp.route("/admin/reset-password", methods=["POST"])
def reset_admin_password():
    """Reset the backup admin password. Backup-admin / wildcard-admin only."""
    # Authorization (Council H4): rotating the tier-0 break-glass password must be
    # restricted to a backup admin or a wildcard ("*") admin — NOT any authenticated
    # LDAP user, who could otherwise seize the break-glass account during an LDAP
    # outage. When auth is disabled the app trusts the LAN, matching every other
    # admin action. Reuses the canonical RBAC guard (lazy import avoids a cycle).
    from routes.api._shared import _require_rbac_admin
    auth_err = _require_rbac_admin()
    if auth_err:
        return auth_err

    data = request.get_json()
    new_password = data.get("new_password", "") if data else ""
    confirm = data.get("confirm_password", "") if data else ""

    if not new_password:
        return jsonify({"ok": False, "error": "New password is required"})
    if new_password != confirm:
        return jsonify({"ok": False, "error": "Passwords do not match"})

    # S2-13 (W4): full policy + reuse prevention on rotation.
    settings = _config.get_settings()
    ba = settings.get("auth", {}).get("backup_admin", {})
    prev_hash = ba.get("password_hash") or None
    ok, err = validate_backup_admin_password(new_password, previous_hash=prev_hash)
    if not ok:
        _audit(session.get("username", ""), "backup_admin_password_reject", err or "policy")
        return jsonify({"ok": False, "error": err})

    from werkzeug.security import generate_password_hash
    pw_hash = generate_password_hash(new_password, method="scrypt")

    admin_user = ba.get("username", session.get("username", "admin"))
    _config.set_backup_admin(admin_user, pw_hash)
    logger.info("Admin password reset by user '%s'", session.get("username"))
    _audit(session.get("username", ""), "backup_admin_password_reset",
           f"target user={admin_user}")
    return jsonify({"ok": True})


@auth_bp.route("/logout")
def logout():
    """Clear session and redirect to login (or home if auth disabled)."""
    username = session.get("username", "unknown")
    session.clear()
    _audit(username, "logout", "user-initiated")
    auth_cfg = _config.get_settings().get("auth", {})
    if auth_cfg.get("enabled", False):
        logger.info("User '%s' logged out.", username)
        return redirect(url_for("auth.login"))
    return redirect("/")


# ── CLI helper: generate a password hash for the backup admin ──
# Usage:  python auth.py set-admin <username> <password>
if __name__ == "__main__":
    import sys
    from werkzeug.security import generate_password_hash

    if len(sys.argv) >= 4 and sys.argv[1] == "set-admin":
        uname = sys.argv[2]
        pwd = sys.argv[3]
        pw_hash = generate_password_hash(pwd, method="scrypt")
        print()
        print("Add this to config.json under settings.auth:")
        print()
        print(f'  "backup_admin": {{')
        print(f'    "username": "{uname}",')
        print(f'    "password_hash": "{pw_hash}"')
        print(f'  }}')
        print()
    else:
        print("Usage: python auth.py set-admin <username> <password>")
        print("  Generates a hashed password snippet for config.json")
