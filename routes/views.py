"""HTML page routes and HTMX partial routes for Prism."""

import logging
from flask import Blueprint, render_template, request
from database import Database
from config_manager import ConfigManager
from analytics import get_server_analytics

logger = logging.getLogger("prism.views")

views_bp = Blueprint("views", __name__)

_db: Database = None
_config: ConfigManager = None


def register_view_routes(app, db: Database, config: ConfigManager):
    global _db, _config
    _db = db
    _config = config
    app.register_blueprint(views_bp)


# ── Full page routes ──

@views_bp.route("/")
def dashboard():
    logger.debug("Serving %s", request.path)
    try:
        # Pass server count to the template so it can render an empty-state
        # onboarding card when no servers are configured yet (helps brand-new
        # admins find the "Add Server" page on /servers instead of staring at
        # an empty dashboard).
        server_count = len(_config.get_servers())
        # Verdict header data — the dashboard should answer "is anything wrong
        # right now?" server-side on first paint, not after a skeleton + fetch.
        try:
            summary = _db.get_status_summary()
        except Exception:
            summary = None
        return render_template("dashboard.html", server_count=server_count, summary=summary)
    except Exception:
        logger.exception("Error rendering dashboard")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/server/<name>")
def server_detail(name: str):
    logger.debug("Serving %s", request.path)
    try:
        cfg = _config.get_server_by_name(name)
        if not cfg:
            return render_template("dashboard.html"), 404
        metrics = _db.get_latest_by_server(name)
        events = _db.get_server_events(name, limit=50)
        logs = _db.get_server_logs(name, hours=24, limit=50)
        settings = _config.get_settings()
        analytics = get_server_analytics(_db, name, server_type=cfg.type,
                                          timezone_str=settings.get("timezone", "Europe/Berlin"),
                                          settings=settings, thresholds=cfg.thresholds)
        # Compute active maintenance window for badge display.
        # Imports the maintenance helper directly (post-R1b canonical home).
        try:
            from maintenance import _get_active_maintenance_window
            active_window = _get_active_maintenance_window(cfg.name, settings)
        except Exception:
            active_window = None
        return render_template("server_detail.html", server=cfg, metrics=metrics,
                               events=events, logs=logs, analytics=analytics,
                               settings=settings, active_maintenance=active_window)
    except Exception:
        logger.exception("Error rendering server_detail for %s", name)
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/reports")
def reports():
    logger.debug("Serving %s", request.path)
    try:
        # max_compare_servers now comes from the global inject_locale context
        # processor, because the comparison partial moved to /monitoring and any
        # view may include it.
        return render_template("reports.html")
    except Exception:
        logger.exception("Error rendering reports")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/settings")
def settings():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        settings_data = _config.get_settings()
        return render_template("settings.html", servers=servers, settings=settings_data)
    except Exception:
        logger.exception("Error rendering settings")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/servers")
def servers_page():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        settings_data = _config.get_settings()
        return render_template("servers.html", servers=servers, settings=settings_data)
    except Exception:
        logger.exception("Error rendering servers")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/monitoring")
def monitoring_page():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        settings_data = _config.get_settings()
        return render_template("monitoring.html", servers=servers, settings=settings_data)
    except Exception:
        logger.exception("Error rendering monitoring")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/operations")
def operations_page():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        settings_data = _config.get_settings()
        return render_template("operations.html", servers=servers, settings=settings_data)
    except Exception:
        logger.exception("Error rendering operations")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/workflows")
def workflows_page():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        settings_data = _config.get_settings()
        return render_template("workflows.html", servers=servers, settings=settings_data)
    except Exception:
        logger.exception("Error rendering workflows")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/topology")
def topology():
    logger.debug("Serving %s", request.path)
    try:
        servers = _config.get_servers()
        return render_template("topology.html", servers=servers)
    except Exception:
        logger.exception("Error rendering topology")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


# ── HTMX partial routes (return HTML fragments, not full pages) ──

@views_bp.route("/partials/critical-issues")
def partial_critical_issues():
    logger.debug("Serving %s", request.path)
    try:
        servers_config = {s.name: s for s in _config.get_servers()}
        # Read from the in-memory cache populated by the collector
        # (legacy: end-of-cycle; v2: per-Result aggregator tick) instead
        # of running db.get_latest_all() (self-join on metrics) every
        # time the dashboard loads. Falls back to DB on a cold cache.
        # Post-R2: state module owns the cache; collector.py re-exports.
        from state import latest_by_server, _state_lock
        with _state_lock:
            latest = list(latest_by_server.values()) if latest_by_server else None
        if latest is None:
            latest = _db.get_latest_all()
        issues = []
        for m in latest:
            if m["status"] in ("critical", "warning", "offline"):
                cfg = servers_config.get(m["server_name"])
                issues.append({
                    **m,
                    "host": cfg.host if cfg else "?",
                    "type": cfg.type if cfg else "?",
                    # Per-server thresholds so the template doesn't have to
                    # fall back on hardcoded 70/85 values (which contradicted
                    # whatever the user set in Servers settings).
                    "thresholds": cfg.thresholds if cfg else {},
                })
        # Preserve the original ordering (critical > offline > warning > name)
        _sev = {"critical": 0, "offline": 1, "warning": 2}
        issues.sort(key=lambda i: (_sev.get(i.get("status"), 9), i.get("server_name", "")))
        return render_template("partials/critical_issues.html", issues=issues)
    except Exception:
        logger.exception("Error rendering partial critical_issues")
        return "Internal Server Error", 500


@views_bp.route("/partials/status-overview")
def partial_status_overview():
    logger.debug("Serving %s", request.path)
    try:
        summary = _db.get_status_summary()
        return render_template("partials/status_overview.html", summary=summary)
    except Exception:
        logger.exception("Error rendering partial status_overview")
        return "Internal Server Error", 500


@views_bp.route("/partials/verdict-header")
def partial_verdict_header():
    """The 'is anything wrong right now?' banner, as a refreshable partial so
    it tracks live status instead of freezing at first-paint values."""
    logger.debug("Serving %s", request.path)
    try:
        summary = _db.get_status_summary()
        server_count = len(_config.get_servers())
        return render_template("partials/verdict_header.html",
                               summary=summary, server_count=server_count)
    except Exception:
        logger.exception("Error rendering partial verdict_header")
        return "Internal Server Error", 500


@views_bp.route("/partials/server-grid")
def partial_server_grid():
    logger.debug("Serving %s", request.path)
    try:
        servers_config = {s.name: s for s in _config.get_servers()}
        # Same cache read as partial_critical_issues — avoids a second self-join
        # on the same request burst. Post-R2: state module owns the cache.
        from state import latest_by_server, _state_lock
        with _state_lock:
            latest_metrics = list(latest_by_server.values()) if latest_by_server else None
        if latest_metrics is None:
            latest_metrics = _db.get_latest_all()
        tag_assignments = _db.get_all_tag_assignments()
        all_tags = _db.get_all_tags()
        active_tag = request.args.get("tag")

        # In-flight install / restart state per server. Decorating the tile
        # with this lets the dashboard show "Installing" / "Rebooting" /
        # "Stabilising" instead of a confusing "offline" during the
        # update lifecycle. The aggregator clears the entry once metrics
        # start flowing again post-reboot; a periodics janitor times out
        # at 20 min as a safety net.
        from routes.api import _update_install_state
        # Snapshot so concurrent writes from request threads don't blow
        # us up mid-iteration. Cheap dict copy.
        install_states = dict(_update_install_state or {})

        # Per-server staleness flag (S1-6 from AUDIT-2026-05). Without this the
        # tile shows whatever status was last successfully collected, which for
        # a server whose collector future timed out can be hours-old "healthy"
        # — the canonical Purple Hat finding. We compute "seconds since last
        # collection" once at render time so the partial can render a "stale"
        # badge when the row is too old to trust.
        #
        # Threshold = poll_interval + 2 × cycle_timeout_budget. Earlier this
        # was 2.5× poll_interval but that's wrong in production: a Prism cycle
        # is bounded by the slowest WinRM server, not by the poll interval.
        # On a 60 s poll with 90 s collector_timeout the cycle wall-clock is
        # 60-180 s, so data is 60-180 s old at the moment the *next* cycle
        # finishes. The 2.5× rule (150 s) flips every tile to "stale" between
        # back-to-back cycles. The new rule (60 + 2×90 = 240 s by default)
        # only fires when collection genuinely missed a cycle.
        from datetime import datetime as _dt, timezone as _tz
        _settings = _config.get_settings()
        _poll_s = _settings.get("poll_interval_seconds", 60)
        _cycle_timeout_s = _settings.get("collector_timeout_seconds", 90)
        _stale_threshold_s = _poll_s + 2 * _cycle_timeout_s
        _now_utc = _dt.now(_tz.utc)

        def _staleness_seconds(ts_str):
            if not ts_str:
                return None
            try:
                # Stored format: "YYYY-MM-DDTHH:MM:SSZ" or with microseconds
                ts_clean = ts_str.replace("Z", "+00:00")
                ts = _dt.fromisoformat(ts_clean)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                return (_now_utc - ts).total_seconds()
            except (ValueError, TypeError):
                return None

        # Set of install statuses that DOMINATE the badge. While any of
        # these are active for a server, the tile shows the install state
        # instead of the threshold-based status, because a "healthy" or
        # "offline" badge during an update install would be misleading.
        # ``stabilising`` is included so we keep showing it for ~60 s after
        # metrics return — the aggregator pops the entry when the window
        # ends (see _handle_post_reboot in collector_v2/aggregator.py).
        _INSTALL_STATUSES_DOMINANT = {
            "queued", "searching", "downloading", "installing",
            "restart_required", "rebooting", "stabilising",
        }

        def _attach_install_state(entry: dict, name: str) -> dict:
            """Annotate a server row with its install_state (if any)."""
            ist = install_states.get(name)
            if ist and ist.get("status") in _INSTALL_STATUSES_DOMINANT:
                entry["install_state"] = ist
            return entry

        servers = []
        seen = set()
        for m in latest_metrics:
            name = m["server_name"]
            seen.add(name)
            cfg = servers_config.get(name)
            stale_s = _staleness_seconds(m.get("timestamp"))
            servers.append(_attach_install_state({
                **m,
                "host": cfg.host if cfg else "?",
                "type": cfg.type if cfg else "?",
                "thresholds": cfg.thresholds if cfg else {},
                "tags": tag_assignments.get(name, []),
                "staleness_seconds": stale_s,
                "is_stale": (stale_s is not None and stale_s > _stale_threshold_s),
            }, name))

        for name, cfg in servers_config.items():
            if name not in seen:
                servers.append(_attach_install_state({
                    "server_name": name,
                    "host": cfg.host,
                    "type": cfg.type,
                    "status": "unknown",
                    "cpu_percent": None, "ram_percent": None,
                    "disk_c_percent": None, "disk_d_percent": None,
                    "timestamp": None,
                    "thresholds": cfg.thresholds,
                    "tags": tag_assignments.get(name, []),
                }, name))

        # Filter by tag if specified
        if active_tag:
            servers = [s for s in servers
                       if any(str(tag["id"]) == active_tag for tag in s["tags"])]

        # Group by type
        grouped = {}
        for s in servers:
            t = s.get("type", "other")
            grouped.setdefault(t, []).append(s)

        return render_template("partials/server_grid.html", grouped=grouped,
                               all_tags=all_tags, active_tag=active_tag)
    except Exception:
        logger.exception("Error rendering partial server_grid")
        return "Internal Server Error", 500


@views_bp.route("/partials/activity-feed")
def partial_activity_feed():
    logger.debug("Serving %s", request.path)
    try:
        events = _db.get_consolidated_activity(limit=20)
        # Enrich events with alert fatigue noise scores
        try:
            for ev in events:
                score_rec = _db.get_alert_score(
                    ev.get("server_name", ""),
                    ev.get("metric", "") or "",
                    ev.get("event_type", "")
                )
                ev["noise_score"] = score_rec["score"] if score_rec else None
        except Exception:
            pass  # Don't break the feed if scoring fails
        return render_template("partials/activity_feed.html", events=events)
    except Exception:
        logger.exception("Error rendering partial activity_feed")
        return "Internal Server Error", 500


@views_bp.route("/partials/incidents-panel")
def partial_incidents_panel():
    logger.debug("Serving %s", request.path)
    try:
        incidents = _db.get_incidents(status="open", limit=20)
        # Enrich with event count per incident
        for inc in incidents:
            detail = _db.get_incident_detail(inc["id"])
            inc["event_count"] = len(detail.get("events", [])) if detail else 0
        return render_template("partials/incidents_panel.html", incidents=incidents)
    except Exception:
        logger.exception("Error rendering partial incidents_panel")
        return "Internal Server Error", 500


@views_bp.route("/partials/tls-overview")
def partial_tls_overview():
    try:
        certs = _db.get_all_tls_certificates()
        # Only show certs that need attention — hide when everything is healthy
        alerts = [c for c in certs if c.get("status") in ("expiring", "expired", "error")]
        return render_template("partials/tls_overview.html", certificates=alerts)
    except Exception:
        logger.exception("Error rendering partial tls_overview")
        return "Internal Server Error", 500


@views_bp.route("/partials/active-actions")
def partial_active_actions():
    """Dashboard widget — "what's happening RIGHT NOW" across the fleet.

    Surfaces every server with an in-flight install or restart lifecycle
    state so the operator sees activity without having to drill into
    individual server tiles. Empty body (the section auto-hides) when
    nothing is happening.

    States shown — see ``routes/api/updates`` for the lifecycle owner:

      * ``queued`` / ``searching`` / ``downloading`` / ``installing``
        — an update install is in flight on this server.
      * ``restart_required``
        — install finished, waiting for the scheduled reboot to fire.
      * ``rebooting``
        — restart command sent, server is unreachable while it
        reboots. Set by ``routes/api/updates._set_rebooting_state``;
        cleared by the aggregator's ``_handle_post_reboot`` when
        metrics return.
      * ``stabilising``
        — server is back online and we're giving it a 60 s settling
        window before reverting to the normal metric-based badge.

    Sorted: restart phases first (most operator-relevant — these have
    a hard reboot underway), then install phases, then by server name
    within each group.
    """
    try:
        from routes.api import _update_install_state
        # Snapshot to avoid mutation while iterating — request threads
        # write to this dict.
        install_states = dict(_update_install_state or {})

        _ACTION_STATUSES = {
            "queued", "searching", "downloading", "installing",
            "restart_required", "rebooting", "stabilising",
        }
        # Sort priority — restart phases first (they're time-critical and
        # the server may be unreachable), then install activity, then
        # alphabetically within each tier. The tier number is what gets
        # passed to the Jinja sort.
        _TIER = {
            "rebooting": 0,
            "stabilising": 1,
            "restart_required": 2,
            "installing": 3,
            "downloading": 4,
            "searching": 5,
            "queued": 6,
        }

        from datetime import datetime as _dt, timezone as _tz
        _now_utc = _dt.now(_tz.utc)

        def _elapsed_seconds(iso_ts):
            """Seconds since the given ISO timestamp, or None."""
            if not iso_ts:
                return None
            try:
                ts = _dt.fromisoformat(iso_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                return int((_now_utc - ts).total_seconds())
            except (ValueError, TypeError):
                return None

        items = []
        for name, ist in install_states.items():
            if not ist:
                continue
            status = ist.get("status")
            if status not in _ACTION_STATUSES:
                continue
            # Pick the most relevant elapsed timestamp per status. For
            # rebooting we show "how long since the reboot started" so
            # the operator can spot servers that are taking unusually
            # long (the janitor times out at 20 min).
            if status == "rebooting":
                elapsed_s = _elapsed_seconds(ist.get("reboot_started_at"))
            elif status == "stabilising":
                elapsed_s = _elapsed_seconds(ist.get("came_back_at"))
            else:
                elapsed_s = _elapsed_seconds(ist.get("started_at"))
            items.append({
                "server_name": name,
                "status": status,
                "tier": _TIER.get(status, 99),
                "message": ist.get("message") or "",
                "installed_count": ist.get("installed_count") or 0,
                "pending_count": ist.get("pending_count") or 0,
                "elapsed_s": elapsed_s,
                "restart_after": bool(ist.get("restart_after")),
            })
        items.sort(key=lambda x: (x["tier"], x["server_name"]))
        return render_template("partials/active_actions.html", items=items)
    except Exception:
        logger.exception("Error rendering partial active_actions")
        return "Internal Server Error", 500


@views_bp.route("/partials/updates-overview")
def partial_updates_overview():
    """Dashboard widget — shows servers with pending Windows updates.

    Reads the in-memory `server_update_info` dict populated by the collector
    on its ~30min update-check cycle. Each entry flagged if it needs a
    reboot or if the count > 0. Empty list → section hides itself.
    """
    try:
        # Post-R2: state module owns the cache.
        from state import server_update_info
        from routes.api import _update_install_state
        items = []
        # Track which servers have an active install so we render progress
        # instead of "N updates pending".
        active_installs = {}
        for name, ist in (_update_install_state or {}).items():
            if ist and ist.get("status") in ("queued", "searching", "downloading", "installing"):
                active_installs[name] = ist

        for name, info in (server_update_info or {}).items():
            if not info:
                continue
            count = int(info.get("count") or 0)
            pending_reboot = bool(info.get("pending_reboot"))
            reboot_required = bool(info.get("reboot_required"))
            error = info.get("error")

            # If this server has an active install job, show that instead
            if name in active_installs:
                ist = active_installs.pop(name)
                items.append({
                    "server_name": name,
                    "count": count,
                    "critical": 0,
                    "important": 0,
                    "reboot_required": reboot_required,
                    "pending_reboot": pending_reboot,
                    "error": None,
                    "checked_at": info.get("checked_at"),
                    "sev": "installing",
                    "install_status": ist.get("status", ""),
                    "install_message": ist.get("message", ""),
                })
                continue

            # Only surface servers that actually need attention
            if count <= 0 and not pending_reboot and not error:
                continue
            updates = info.get("updates") or []
            critical = sum(1 for u in updates if (u.get("severity") or "").lower() == "critical")
            important = sum(1 for u in updates if (u.get("severity") or "").lower() == "important")
            if error:
                sev = "error"
            elif pending_reboot or critical > 0:
                sev = "critical"
            elif important > 0 or reboot_required:
                sev = "warning"
            else:
                sev = "info"
            items.append({
                "server_name": name,
                "count": count,
                "critical": critical,
                "important": important,
                "reboot_required": reboot_required,
                "pending_reboot": pending_reboot,
                "error": error,
                "checked_at": info.get("checked_at"),
                "sev": sev,
            })

        # Also include servers with active installs that aren't in
        # server_update_info yet (e.g. right after Prism restart)
        for name, ist in active_installs.items():
            items.append({
                "server_name": name,
                "count": 0, "critical": 0, "important": 0,
                "reboot_required": False, "pending_reboot": False,
                "error": None,
                "checked_at": None,
                "sev": "installing",
                "install_status": ist.get("status", ""),
                "install_message": ist.get("message", ""),
            })

        # Sort: installing first, then error/critical/warning/info
        _order = {"installing": -1, "error": 0, "critical": 1, "warning": 2, "info": 3}
        items.sort(key=lambda x: (_order.get(x["sev"], 9), -x["count"], x["server_name"]))
        checked_any = any(bool((info or {}).get("checked_at")) for info in (server_update_info or {}).values())
        _checked_times = [info.get("checked_at") for info in (server_update_info or {}).values() if info and info.get("checked_at")]
        last_checked = max(_checked_times) if _checked_times else None
        return render_template("partials/updates_overview.html",
                               items=items, checked_any=checked_any, last_checked=last_checked)
    except Exception:
        logger.exception("Error rendering partial updates_overview")
        return "Internal Server Error", 500


@views_bp.route("/admin/rbac")
def rbac_admin_page():
    """Admin UI for managing per-server ACLs and tier-0 approvals.

    Access control is server-side: the page renders for any authenticated
    user, but the API calls it makes (/api/rbac/*) reject anyone who isn't
    a backup admin or a wildcard-admin. We intentionally don't gate the
    page itself so a non-admin user gets a clean 403 from the API instead
    of a confusing redirect loop.
    """
    logger.debug("Serving %s", request.path)
    try:
        servers = sorted(s.name for s in _config.get_servers())
        return render_template("rbac.html", server_names=servers)
    except Exception:
        logger.exception("Error rendering /admin/rbac")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/compliance")
def compliance_page():
    """CSV / compliance dashboard.

    Gated on ``settings.compliance.enabled = true`` — when off, the
    route 404s and the nav item is hidden (see base.html). Default off
    so non-regulated deployments don't see the feature.
    """
    logger.debug("Serving %s", request.path)
    import csv_compliance as _cc
    try:
        settings_data = _config.get_settings()
        if not _cc.is_compliance_enabled(settings_data):
            return render_template("404.html"), 404
        return render_template(
            "compliance.html",
            sops=_cc.list_sops(),
            settings=settings_data,
        )
    except Exception:
        logger.exception("Error rendering /compliance")
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/compliance/sop/<sop_id>")
def compliance_sop_page(sop_id: str):
    """Single-SOP page: rendered markdown + record-execution form."""
    logger.debug("Serving %s", request.path)
    import csv_compliance as _cc
    try:
        settings_data = _config.get_settings()
        if not _cc.is_compliance_enabled(settings_data):
            return render_template("404.html"), 404
        sop = _cc.get_sop(sop_id)
        if not sop:
            return render_template("404.html"), 404
        return render_template(
            "compliance_sop.html",
            sop=sop,
            settings=settings_data,
        )
    except Exception:
        logger.exception("Error rendering /compliance/sop/%s", sop_id)
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


@views_bp.route("/compliance/doc/<doc_id>")
def compliance_doc_page(doc_id: str):
    """Single-CSV-doc page: rendered markdown, read-only (no execute form).

    Parallel to compliance_sop_page but for the V-model + spec docs
    under docs/csv/ (readiness report, findings register, IQ/OQ/PQ
    protocols, traceability matrix, etc.).
    """
    logger.debug("Serving %s", request.path)
    import csv_compliance as _cc
    try:
        settings_data = _config.get_settings()
        if not _cc.is_compliance_enabled(settings_data):
            return render_template("404.html"), 404
        doc = _cc.get_csv_doc(doc_id)
        if not doc:
            return render_template("404.html"), 404
        return render_template(
            "compliance_doc.html",
            doc=doc,
            settings=settings_data,
        )
    except Exception:
        logger.exception("Error rendering /compliance/doc/%s", doc_id)
        return render_template("500.html") if _template_exists("500.html") else ("Internal Server Error", 500)


def _template_exists(template_name: str) -> bool:
    """Check if a template file exists without raising an exception."""
    try:
        from flask import current_app
        current_app.jinja_env.get_template(template_name)
        return True
    except Exception:
        return False
