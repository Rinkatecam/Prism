"""Prism - Office Server Monitoring System. Flask entry point."""

import os
import ssl  # noqa: F401 — used conditionally for HTTPS
import logging
import threading
from pathlib import Path
from database import Database
from config_manager import ConfigManager
# Post-R3: v1 collector_loop + set_collector_config_ref deleted. The LDAP
# probe config handle now lives on collector_v2; see start_collector_v2()
# call below.
from flask import Flask, render_template, request
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ── Logging ──
_LOG_FMT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_LOG_DATEFMT)

# Durable, rotating file log so post-incident forensics survive terminal
# scrollback (stdout alone lost every CRITICAL the moment the console scrolled).
# stdout stays for foreground runs; this adds a persistent sink at
# data/logs/prism.log (5 × 10 MB). Never let logging setup abort startup.
try:
    from logging.handlers import RotatingFileHandler as _RotatingFileHandler
    _log_dir = Path(__file__).parent / "data" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = _RotatingFileHandler(
        _log_dir / "prism.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(_file_handler)
except Exception:
    logging.getLogger("prism").warning("durable file logging unavailable", exc_info=True)

logger = logging.getLogger("prism")

# ── Initialize core services ──
db = Database()
config = ConfigManager()

# ── Flask app ──
app = Flask(__name__)

# ── CSRF protection ──
# Accept the CSRF token from the X-CSRFToken header (for JSON API calls).
# Time limit disabled — this is an internal monitoring app behind auth;
# expiring tokens just frustrate users who leave the dashboard open all day.
app.config["WTF_CSRF_HEADERS"] = ["X-CSRFToken"]
app.config["WTF_CSRF_TIME_LIMIT"] = None  # Never expire CSRF tokens
app.config["TEMPLATES_AUTO_RELOAD"] = True
csrf = CSRFProtect(app)

# ── Session hardening ──
# HTTPONLY prevents XSS from stealing the cookie, SAMESITE=Lax stops
# CSRF via cross-site form posts, SECURE means HTTPS-only (only effective
# when Prism is fronted by HTTPS — disabled when running on plain HTTP for
# internal-network deployments).
from datetime import timedelta as _timedelta
import os as _os
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = _os.environ.get("PRISM_HTTPS_ONLY", "0") == "1"
# Cookie lifetime ceiling — sized to the LONGEST possible session
# ("remember me" → 30 days). The actual session duration is enforced
# server-side in auth.py:
#   * 30 days absolute timeout when session['remember_me'] is True
#   * 8h absolute timeout otherwise (auth.session_timeout_minutes)
#   * 30 min idle timeout (skipped when remember_me is set, except for
#     backup_admin which always has a 15 min idle floor)
# Previously this was 8h, which silently undid the remember-me intent —
# the cookie itself expired in 8h regardless of the user's choice, so
# operators had to sign in every 8h even after ticking the box.
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(days=30)

# S3-11 (W6) — request-body size cap. Internal monitoring app; no legitimate
# request needs more than a few hundred KB. 4 MB ceiling keeps the framework
# layer from accepting an unbounded JSON body that exhausts memory before any
# per-endpoint validation runs.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB

# ── Rate limiting ──
limiter = Limiter(get_remote_address, app=app, default_limits=[])

# ── Secret key for session signing ──
# S2-1 (BL3): operator can rotate by setting PRISM_SECRET_KEY (hex string)
# and restarting; that invalidates ALL sessions, which is the goal during
# incident response. Falls back to the on-disk key file for normal startup.
# See docs/SECRET_KEY_ROTATION.md.
SECRET_KEY_PATH = Path(__file__).parent / "data" / "flask_secret.key"
SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
_env_secret = os.environ.get("PRISM_SECRET_KEY", "").strip()
if _env_secret:
    # Accept hex (preferred — what `python -c "import secrets; secrets.token_hex(32)"`
    # produces) OR plain bytes for tolerance.
    try:
        app.secret_key = bytes.fromhex(_env_secret)
    except ValueError:
        app.secret_key = _env_secret.encode("utf-8")
    logger.info("Flask secret key loaded from PRISM_SECRET_KEY env var")
elif SECRET_KEY_PATH.exists():
    app.secret_key = SECRET_KEY_PATH.read_bytes()
else:
    _secret = os.urandom(32)
    SECRET_KEY_PATH.write_bytes(_secret)
    app.secret_key = _secret
    logger.info("Generated Flask secret key at %s", SECRET_KEY_PATH)

# Restrict secret key file permissions
from crypto_utils import _restrict_file_permissions  # noqa: E402
_restrict_file_permissions(SECRET_KEY_PATH)

# Seed built-in runbooks
from runbook_engine import seed_builtin_runbooks  # noqa: E402
seed_builtin_runbooks(db)

# Register routes
from routes.api import register_api_routes  # noqa: E402
from routes.views import register_view_routes  # noqa: E402
register_api_routes(app, db, config, limiter)
register_view_routes(app, db, config)

# ── Authentication middleware (disabled by default) ──
from auth import register_auth, assert_ldap_startup_safe  # noqa: E402
# S2-15: refuse to start if auth is enabled but neither LDAP nor backup admin
# is reachable. Logged-CRITICAL-but-allow-start when LDAP just unreachable.
try:
    assert_ldap_startup_safe(config)
except SystemExit:
    raise
except Exception:
    logger.exception("LDAP startup self-check raised unexpectedly; continuing")
register_auth(app, config, limiter, db=db)

# ── i18n + timestamp formatting context processor ──
from i18n import get_translations  # noqa: E402
from datetime import datetime, timezone as tz  # noqa: E402
import zoneinfo  # noqa: E402

def _format_timestamp(iso_str, settings):
    """Convert ISO UTC timestamp to configured timezone + format."""
    if not iso_str:
        return ""
    try:
        # Parse the UTC timestamp
        ts = iso_str.replace("Z", "+00:00") if "+" not in iso_str and iso_str.endswith("Z") else iso_str
        if "+" not in ts and "Z" not in ts:
            ts += "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.utc)
        # Convert to target timezone
        target_tz = zoneinfo.ZoneInfo(settings.get("timezone", "Europe/Berlin"))
        dt = dt.astimezone(target_tz)
        # Format date part
        date_fmt = settings.get("date_format", "DD.MM.YYYY")
        if date_fmt == "DD.MM.YYYY":
            date_str = dt.strftime("%d.%m.%Y")
        elif date_fmt == "YYYY-MM-DD":
            date_str = dt.strftime("%Y-%m-%d")
        elif date_fmt == "MM/DD/YYYY":
            date_str = dt.strftime("%m/%d/%Y")
        else:
            date_str = dt.strftime("%d/%m/%Y")
        # Format time part
        time_fmt = settings.get("time_format", "24h")
        if time_fmt == "12h":
            time_str = dt.strftime("%I:%M %p")
        else:
            time_str = dt.strftime("%H:%M")
        return date_str + " " + time_str
    except Exception:
        return iso_str[:16] if len(iso_str) > 16 else iso_str

def _format_time_only(iso_str, settings):
    """Convert ISO UTC timestamp to just the time in configured timezone."""
    if not iso_str:
        return ""
    try:
        ts = iso_str.replace("Z", "+00:00") if "+" not in iso_str and iso_str.endswith("Z") else iso_str
        if "+" not in ts and "Z" not in ts:
            ts += "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz.utc)
        target_tz = zoneinfo.ZoneInfo(settings.get("timezone", "Europe/Berlin"))
        dt = dt.astimezone(target_tz)
        if settings.get("time_format", "24h") == "12h":
            return dt.strftime("%I:%M %p")
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str[11:16] if len(iso_str) > 16 else iso_str

def _max_compare_servers() -> int:
    """MAX_COMPARE_SERVERS, imported lazily so a circular import can never take
    the whole context processor (and therefore every page) down. Falls back to
    the same value the API's own default uses."""
    try:
        from routes.api.reports import MAX_COMPARE_SERVERS
        return MAX_COMPARE_SERVERS
    except Exception:
        return 50


@app.context_processor
def inject_locale():
    """Make translations and format helpers available in all templates."""
    s = config.get_settings()
    lang = s.get("language", "en")
    t = get_translations(lang)
    # S3-9 (W2) — expose per-request CSP nonce to templates so every inline
    # <script> can render nonce="{{ csp_nonce }}". g.csp_nonce is set in
    # before_request; if context_processor runs outside a request (rare, e.g.
    # offline rendering), fall back to empty string — Jinja just emits
    # nonce="", which the policy ignores gracefully.
    # Compliance feature flag — when off, the CSV nav item and routes
    # don't surface at all. See csv_compliance.is_compliance_enabled.
    try:
        import csv_compliance as _cc
        _compliance_enabled = _cc.is_compliance_enabled(s)
    except Exception:
        _compliance_enabled = False
    return {
        "t": t,
        # Exposed so <html lang> can be correct. It was computed here but never
        # returned, so base.html hardcoded lang="en" for all 5 locales — which
        # makes a screen reader pronounce German and Japanese with English
        # phonetics (WCAG 3.1.1), and stops :lang() CSS from ever matching.
        "lang": lang,
        # The server-comparison partial can be included by ANY view, so its cap
        # comes from the global context rather than per-view plumbing. Single
        # source of truth is still routes/api/reports.py — a frontend that
        # disagrees with the API is how "All" became a silent 400.
        "max_compare_servers": _max_compare_servers(),
        "app_settings": s,
        "fmt_ts": lambda iso: _format_timestamp(iso, s),
        "fmt_time": lambda iso: _format_time_only(iso, s),
        "csp_nonce": getattr(_g_mod, "csp_nonce", ""),
        "compliance_enabled": _compliance_enabled,
    }

# ── Startup summary ──
_settings = config.get_settings()
_servers = config.get_servers()
_auth_cfg = _settings.get("auth", {})
_https_cfg = _settings.get("https", {})
logger.info(
    "Startup config: %d servers, auth=%s, https=%s",
    len(_servers),
    "enabled" if _auth_cfg.get("enabled") else "disabled",
    "enabled" if _https_cfg.get("enabled") else "disabled",
)

# ── Per-request correlation ID (S1-8 from AUDIT-2026-05) ──
# Generates a UUID for every incoming request and stashes it on flask.g so that
# log_audit() can pull it without each call site needing to know about it. The
# ID is also returned to the client as X-Request-ID, which gives the operator
# a join key when reading the dashboard's network panel during an incident.
import uuid as _uuid
import secrets as _secrets
from flask import g as _g_mod
@app.before_request
def _assign_request_id():
    _g_mod.request_id = _uuid.uuid4().hex
    # S3-9 (W2) — per-request CSP nonce. Cryptographically-random, base64-url
    # encoded, fresh for every response. Templates render it on every inline
    # <script> tag via {{ csp_nonce }} so future tightening of script-src can
    # drop 'unsafe-inline' once the in-template onclick= handlers are
    # converted to addEventListener (see template sweep notes in app.py).
    _g_mod.csp_nonce = _secrets.token_urlsafe(16)


@app.after_request
def _emit_request_id(response):
    rid = getattr(_g_mod, "request_id", None)
    if rid:
        response.headers["X-Request-ID"] = rid
    return response


# ── Security headers ──
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # S3-8 (W1) — HSTS only when Prism is reached over HTTPS. Setting it on
    # plain-HTTP responses causes a one-shot pinning failure for operators
    # who later switch to HTTPS *and* anyone who reaches Prism on http://
    # is already in trouble — the header would be tampered with. Gate on
    # the same env var that flips SESSION_COOKIE_SECURE.
    if _os.environ.get("PRISM_HTTPS_ONLY", "0") == "1":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # S3-9 (W2) — nonce-based script-src (the inline-handler sweep has LANDED).
    #
    # script-src now advertises 'nonce-{nonce}' and NO LONGER carries
    # 'unsafe-inline'. Per CSP Level 2+, a nonce-source makes the browser
    # ignore 'unsafe-inline' anyway, so the two can't coexist meaningfully —
    # every inline script must be nonce-tagged and every inline event handler
    # must be gone. Both preconditions now hold:
    #   * g.csp_nonce is set per-request in before_request; inject_locale
    #     exposes it as {{ csp_nonce }}; every inline <script> renders it.
    #   * All 282 inline on*= handlers across the 10 templates were converted
    #     to a delegated data-action / data-change / data-input / data-mousedown
    #     dispatcher (templates/base.html) — 0 inline handlers remain.
    #   * The two HTMX-swapped partials (critical_issues, incidents_panel) had
    #     their inline <script> hoisted into base.html: a swapped fragment gets
    #     a FRESH per-request nonce that would not match the host page's nonce,
    #     so those partials are now script-free.
    #   * dashboard's hx-on::after-swap handlers became data-toggle-empty,
    #     dispatched from a single htmx:afterSwap listener in base.html.
    #
    # NO EXTERNAL ORIGIN APPEARS IN THIS POLICY, and that is load-bearing
    # rather than tidy. script-src used to allow cdn.tailwindcss.com,
    # unpkg.com and cdn.jsdelivr.net, with style-src and connect-src carrying
    # one or two of the same — left behind when the front end stopped using
    # them. Every asset the browser loads (Tailwind, htmx, idiomorph,
    # Chart.js, Lucide, both web fonts) is vendored under static/vendor/ and
    # served from this origin; a measured dashboard load makes zero off-origin
    # requests.
    #
    # Nothing was broken and nothing was being fetched, which is exactly why
    # the entries survived: an allowlist entry that nothing uses still GRANTS
    # THE CAPABILITY. Injected script could have reached three third-party
    # origins, and "does this thing phone home" is the first question a
    # customer's security reviewer asks. The claim in docs/DATA_FLOWS.md is
    # what this policy has to be able to back.
    #
    # The comment that stood here explained the CDN entries in terms of
    # "Tailwind's CDN runtime" — untrue since Tailwind was vendored, and the
    # kind of stale justification that keeps a stale rule alive.
    #
    # style-src 'unsafe-inline' STAYS, and its reason is unchanged and real:
    # the vendored Tailwind BROWSER build generates CSS into a <style> element
    # at runtime, status-badge gradients are computed, and inline style="..."
    # attributes are used throughout. Removing it breaks the UI.
    _nonce = getattr(_g_mod, "csp_nonce", "")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{_nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    logger.debug("Security headers applied for %s", request.path)
    return response

# ── Error handlers ──
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(e):
    logger.exception("Internal server error: %s", request.path)
    return render_template("500.html"), 500

# ── Start the v2 collector ──
# v1 was retired in the COLLECTOR_V1_RETIREMENT effort; there is only
# one collector now. The ``collector_engine`` setting (if present in an
# old settings.json) is ignored — kept here as a single-line guard so
# operators who notice the missing knob know we noticed too.
_legacy_engine_setting = config.get_settings().get("collector_engine")
if _legacy_engine_setting is not None and _legacy_engine_setting != "v2":
    logger.warning(
        "settings.json has collector_engine=%r — that setting is ignored "
        "now that v1 has been retired. Remove the key to silence this.",
        _legacy_engine_setting,
    )

import collector_v2  # noqa: E402

# Don't spawn 15 background threads during pytest collection. Tests
# that need v2 running call ``collector_v2.start_collector_v2`` from a
# fixture; tests that just need the Flask app shouldn't pay for a real
# collector + 15 workers + supervisor + aggregator + periodics.
import sys as _sys
_under_pytest = "pytest" in _sys.modules

if not _under_pytest:
    # Defensive conversion. routes/api/config.py clamps this on save, but a
    # hand-edited config.json bypasses the API entirely — and this call used to
    # sit OUTSIDE the try/except below, so a non-numeric value raised at import
    # time and killed the process before the graceful-degradation path could
    # run. Fall back to the documented default instead of failing to boot.
    _DEFAULT_V2_WORKERS = 15
    try:
        _v2_workers = int(config.get_settings().get("collector_v2_num_workers", _DEFAULT_V2_WORKERS))
        if _v2_workers < 2 or _v2_workers > 100:
            raise ValueError(f"out of range: {_v2_workers}")
    except (ValueError, TypeError) as _e:
        logger.warning(
            "Invalid collector_v2_num_workers in config.json (%s) — falling back to %d. "
            "Fix the value in config.json or via the Settings page.",
            _e, _DEFAULT_V2_WORKERS,
        )
        _v2_workers = _DEFAULT_V2_WORKERS
    try:
        collector_v2.start_collector_v2(
            config.get_servers, config.get_settings, db,
            num_workers=_v2_workers,
        )
        # LDAP health probe needs a config handle (previously held by
        # ``set_collector_config_ref``). Stash it on the collector_v2 module
        # so periodics.py's _ldap_probe can read it.
        collector_v2._COLLECTOR_CONFIG_REF = config
    except Exception:
        logger.exception(
            "CRITICAL: collector v2 failed to start — Prism will run without "
            "background collection. The dashboard will only show stored data. "
            "Investigate the traceback above and restart."
        )
else:
    logger.info("Pytest detected — skipping collector_v2 background startup")

# ── Start restart scheduler daemon thread ──
from restart_scheduler import restart_scheduler_loop  # noqa: E402
restart_thread = threading.Thread(
    target=restart_scheduler_loop,
    args=(config.get_settings, db, config.get_servers),
    daemon=True,
    name="prism-restart-scheduler",
)
restart_thread.start()

# Seed workflow templates
from workflow_engine import seed_workflow_templates, workflow_scheduler_loop  # noqa: E402
seed_workflow_templates(db)

# Start workflow scheduler thread
workflow_thread = threading.Thread(
    target=workflow_scheduler_loop,
    args=(config.get_settings, db, config.get_servers),
    daemon=True,
    name="prism-workflow-scheduler",
)
workflow_thread.start()


# ── Background-thread watchdog (S2-11 / P10 from AUDIT-2026-05) ──
# Until now there was no supervisor for the three daemon threads. If any of
# them died via BaseException (KeyboardInterrupt in production, MemoryError,
# segfault in a C extension), the bulletproof Exception catch in each loop
# would NOT catch it; the thread silently terminated; Flask kept serving the
# dashboard from stale in-memory state.
#
# The watchdog wakes every 60s and checks two things per managed thread:
#   1. is_alive() — is the thread object still running?
#   2. heartbeat freshness — has the loop ticked recently? Treat
#      >5×LOOP_INTERVAL since last tick as "stuck" even if the thread is
#      technically alive.
# On any failure, log CRITICAL and write an audit row so an operator with a
# SIEM/email pipeline notices. We do NOT auto-restart the thread; a stuck
# thread is rare enough that operator-paged investigation is the right
# response, and an automatic restart could mask a recurring problem.
import time as _time_mod
_WATCHDOG_INTERVAL = 60  # seconds
_WATCHDOG_STALE_FACTOR = 5  # heartbeat older than 5× tick interval = stuck
import restart_scheduler as _restart_sched_mod
import workflow_engine as _workflow_engine_mod


def _hb_ago_to_ts(s_ago, now):
    """Convert a "<n> seconds ago" heartbeat snapshot to an absolute timestamp.

    ``None`` means the thread has not reported a heartbeat yet (cold start) —
    return 0.0 so the caller's ``hb > 0`` guard SKIPS the stale check instead of
    fabricating a 9999s-stale timestamp and firing a phantom CRITICAL (which also
    wrote a bogus thread_stuck row into the tamper-evident audit_log). A real 0
    ("ticked this instant", which ``or 9999`` also mis-coerced) maps to ``now``.
    """
    return (now - s_ago) if s_ago is not None else 0.0


def _is_stuck(healthy, hb, interval, now, stale_factor=_WATCHDOG_STALE_FACTOR):
    """True only if a thread is alive, has a real heartbeat (hb > 0), and that
    heartbeat is older than ``stale_factor × interval``. hb <= 0 (unknown) is
    never "stuck"."""
    if not (healthy and hb > 0):
        return False
    return (now - hb) > (interval * stale_factor)

# Cache previous-tick alive/heartbeat state so we only audit on TRANSITION,
# not every 60s while a thread is dead. Operators get one alert per failure.
# v1 is retired — the only collector threads we monitor are v2's.
_watchdog_state = {
    "restart": True,
    "workflow": True,
    "v2_supervisor": True,
    "v2_aggregator": True,
    "v2_workers": True,
}


def _watchdog_loop():
    """Daemon supervisor for the workhorse threads. Monitors the
    restart-scheduler, workflow-scheduler, and the three v2 collector
    components (supervisor, aggregator, worker pool).
    """
    logger.info("Background-thread watchdog started (interval=%ds, engine=v2)",
                _WATCHDOG_INTERVAL)
    # Pre-fetch the LOOP_INTERVAL constants so we don't read them every tick.
    restart_interval = getattr(_restart_sched_mod, "LOOP_INTERVAL", 30)
    workflow_interval = getattr(_workflow_engine_mod, "SCHEDULER_INTERVAL", 30)
    # v2 thresholds — see docs/COLLECTOR_V2_MIGRATION.md § Heartbeats
    v2_supervisor_interval = 5    # supervisor ticks every 5 s
    v2_aggregator_interval = 6    # aggregator should process at least one Result per 30 s
    v2_workers_interval = 36      # 180 s / _WATCHDOG_STALE_FACTOR=5

    def _v2_health():
        try:
            import collector_v2
            return collector_v2.get_health_snapshot()
        except Exception:
            return {}

    while True:
        try:
            now = _time_mod.time()
            checks = [
                ("restart", restart_thread,
                 getattr(_restart_sched_mod, "_last_heartbeat", 0),
                 restart_interval),
                ("workflow", workflow_thread,
                 getattr(_workflow_engine_mod, "_last_heartbeat", 0),
                 workflow_interval),
            ]
            # v2 threads — synthesise heartbeat checks from the health snapshot.
            # The v2 threads aren't direct thread objects here; we rely on the
            # state module's heartbeat timestamps. is_alive() proxy: heartbeat
            # within stale-factor × interval = alive.
            if True:  # v2 is always on post-retirement
                v2h = _v2_health()
                if v2h.get("started"):
                    # Synthesize (name, thread_proxy, last_heartbeat, interval)
                    # where thread_proxy is a tiny stand-in with .is_alive()
                    class _Alive:
                        def is_alive(self):  # noqa: D401
                            return True
                    alive = _Alive()
                    # Convert each "_s_ago" snapshot to an absolute timestamp.
                    # None (not ticked yet) -> 0.0 so the stale check is skipped,
                    # not fabricated as 9999s-stale (see _hb_ago_to_ts).
                    checks.extend([
                        ("v2_supervisor", alive,
                         _hb_ago_to_ts(v2h.get("supervisor_last_tick_s_ago"), now),
                         v2_supervisor_interval),
                        ("v2_aggregator", alive,
                         _hb_ago_to_ts(v2h.get("aggregator_last_tick_s_ago"), now),
                         v2_aggregator_interval),
                        ("v2_workers", alive,
                         _hb_ago_to_ts(v2h.get("workers_last_activity_s_ago"), now),
                         v2_workers_interval),
                    ])
            for name, thr, hb, interval in checks:
                healthy = thr.is_alive()
                stuck = _is_stuck(healthy, hb, interval, now)
                state_now = healthy and not stuck
                state_was = _watchdog_state.get(name, True)
                if state_now != state_was:
                    if not state_now:
                        # Transition: was healthy, now isn't.
                        if not healthy:
                            logger.critical(
                                "Watchdog: %s thread is DEAD (is_alive=False). "
                                "Investigate logs for the last exception traceback.",
                                name,
                            )
                            try:
                                db.log_audit("system", f"thread_dead_{name}", "system",
                                             f"Daemon thread {name} terminated unexpectedly")
                            except Exception:
                                pass
                        else:
                            logger.critical(
                                "Watchdog: %s thread heartbeat is %.0fs stale "
                                "(threshold=%ds). Thread is alive but stuck.",
                                name, now - hb, interval * _WATCHDOG_STALE_FACTOR,
                            )
                            try:
                                db.log_audit("system", f"thread_stuck_{name}", "system",
                                             f"Daemon thread {name} heartbeat stale "
                                             f"({int(now - hb)}s)")
                            except Exception:
                                pass
                    else:
                        # Recovered (only possible if collector restarted itself —
                        # we don't auto-restart). Log INFO + audit, don't alert.
                        logger.info("Watchdog: %s thread recovered", name)
                        try:
                            db.log_audit("system", f"thread_recovered_{name}", "system",
                                         f"Daemon thread {name} is healthy again")
                        except Exception:
                            pass
                _watchdog_state[name] = state_now
        except Exception:
            logger.exception("Watchdog loop error (continuing)")
        _time_mod.sleep(_WATCHDOG_INTERVAL)


watchdog_thread = threading.Thread(
    target=_watchdog_loop,
    daemon=True,
    name="prism-watchdog",
)
watchdog_thread.start()

logger.info("Prism started. Collector + restart scheduler + workflow scheduler + watchdog running. Dashboard at http://localhost:5000")

if __name__ == "__main__":
    import sys

    # ── HTTPS configuration ──
    settings = config.get_settings()
    https_cfg = settings.get("https", {})
    ssl_context = None

    if https_cfg.get("enabled"):
        cert = https_cfg.get("cert_file", "")
        key = https_cfg.get("key_file", "")
        if cert and key and Path(cert).exists() and Path(key).exists():
            ssl_context = (cert, key)
            logger.info("HTTPS enabled with cert=%s key=%s", cert, key)
        else:
            logger.warning(
                "HTTPS enabled in config but cert/key files not found "
                "(cert_file=%s, key_file=%s). Falling back to HTTP.",
                cert, key,
            )

    if "--dev" in sys.argv:
        scheme = "https" if ssl_context else "http"
        logger.info("Starting dev server on %s://0.0.0.0:5000", scheme)
        app.run(host="0.0.0.0", port=5000, debug=True, ssl_context=ssl_context)
    else:
        # Waitress does not natively support SSL/TLS.
        # For production HTTPS, use a reverse proxy (Caddy, nginx, or IIS)
        # in front of Waitress for TLS termination. Example with Caddy:
        #   caddy reverse-proxy --from :443 --to :5000
        from waitress import serve
        if ssl_context:
            logger.info(
                "Production mode: waitress does not support SSL directly. "
                "Use a reverse proxy (Caddy/nginx) for TLS termination."
            )
        # Thread count was hardcoded to 4 — which is waitress's own default,
        # i.e. it was never actually chosen. Measured on a 29-server fleet, the
        # two report endpoints then took ~5 s and ~1.2 s, so two concurrent
        # report requests plus two dashboard polls saturated the server; the log
        # showed 3,816 queue-depth warnings over 17 hours, peaking at depth 9.
        # Both have since been replaced by /api/reports/fleet at ~0.77 s
        # (2026-08-06), which removes most of that pressure but not the reason
        # for the headroom.
        #
        # 8 is a better default: these requests are dominated by SQLite reads
        # and WinRM waits, both of which release the GIL. It is a mitigation,
        # not a cure — the real fix is removing the O(N-servers) round-trips in
        # the report endpoints (see docs/plans/SCALING_500.md §7) — so it is
        # deliberately modest rather than a large number that would just move
        # the contention onto the database write lock.
        _threads = _settings.get("web_server_threads", 8)
        try:
            _threads = max(2, min(int(_threads), 64))
        except (TypeError, ValueError):
            _threads = 8
        logger.info(
            "Starting production server (waitress) on http://0.0.0.0:5000 "
            "(%d threads)", _threads,
        )
        serve(app, host="0.0.0.0", port=5000, threads=_threads)
