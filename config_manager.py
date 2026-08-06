"""Configuration manager for Prism. Loads config.json with caching."""

import json
import logging
import shutil
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from models import ServerConfig
from crypto_utils import (
    encrypt_password,
    decrypt_password,
    restrict_config_permissions,
)

logger = logging.getLogger("prism.config")

CONFIG_PATH = Path(__file__).parent / "config.json"


class ConfigManager:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self._cache = None
        self._cache_mtime = 0.0
        self._cache_checked_at = 0.0
        self._lock = threading.Lock()
        # Encrypt any legacy plain-text passwords on first load
        self._migrate_plaintext_passwords()

    def get_servers(self) -> list[ServerConfig]:
        """Get the list of server configurations, re-reading file if changed.
        Passwords are decrypted transparently so the collector can use them."""
        config = self._get_raw_config()
        servers = []
        for s in config.get("servers", []):
            # Decrypt the password before creating the ServerConfig
            s_copy = dict(s)
            s_copy["password"] = decrypt_password(s_copy.get("password", ""))
            servers.append(ServerConfig.from_dict(s_copy))
        # DEBUG, not INFO, and the wording is now honest. `_get_raw_config()`
        # above is cached, so this line fired on every CACHE HIT too — it said
        # "Config loaded" ~2x/second and accounted for 10,763 lines of
        # prism.log, drowning the entries that matter.
        logger.debug("get_servers(): %d servers", len(servers))
        return servers

    # Default settings structure, used to fill in missing keys
    _DEFAULT_SETTINGS = {
        # ── Windows event-log ingest controls ─────────────────────────────
        # Measured on the live fleet: `logs` held 1,771,744 rows — 96% of every
        # row in the database — of which 73.1% were Information level, and the
        # whole table collapsed to 163,986 distinct signatures (a 10.8x
        # duplicate ratio). Projected to 500 servers that is ~58M rows.
        #
        # Filtering and coalescing together cut it roughly 40x. Both are
        # switchable, and what gets dropped is COUNTED and reported — silent
        # filtering is how a monitoring tool loses trust.
        "log_ingest": {
            "drop_information": True,
            # Information-level events worth keeping. Everything else at that
            # level is discarded at ingest. Format "Source/EventID".
            "information_allowlist": [
                "System/7036",   # a service entered the running/stopped state
                "System/1074",   # shutdown/restart initiated by a user or process
                "System/6006",   # event log service stopped — clean shutdown
                "System/6008",   # the previous shutdown was UNEXPECTED
                "System/7045",   # a new service was installed
            ],
            # Roll identical rows up into per-signature-per-hour counts in
            # log_signatures. The raw row is still written to `logs` for
            # drill-down; signatures are what survives raw retention.
            "coalesce_signatures": True,
        },
        # ── Per-table retention ───────────────────────────────────────────
        # retention_days below is the fallback for anything not named here.
        # These exist because one uniform value is why `logs` dominates: the
        # rows that matter (metrics, events) are tiny and worth keeping longer,
        # while raw log lines are the volume and are only needed for recent
        # drill-down. audit_log is deliberately on its own knob — an audit
        # trail truncated by a debug-log setting is a finding, not a feature.
        "retention": {
            "logs_days": 7,
            "log_signatures_days": 90,
            "metrics_days": 30,
            "events_days": 90,
            "audit_log_days": 365,
        },
        "poll_interval_seconds": 60,
        # Waitress worker threads (production mode only; the Flask dev server
        # ignores it). Was hardcoded to 4 — waitress's own default, so never
        # actually chosen — while the capacity report then measured ~5s, so two
        # report requests plus two dashboard polls saturated the server. Its
        # replacement, /api/reports/fleet, measures ~0.77s (2026-08-06).
        # Clamped 2..64 at use. See docs/plans/SCALING_500.md §7.
        "web_server_threads": 8,
        # How often to pull Windows event logs + failed logins from each server.
        # Read by collector_v2/supervisor.py, which schedules the LOGS check on
        # this cadence (floored at 30s). UI: settings.html "Collector" section.
        "log_collection_interval_minutes": 5,
        "update_check_interval_minutes": 30,
        # Collector worker pool size. There is only one collector
        # (v2 — supervisor + worker pool + aggregator). The historical
        # ``collector_engine`` setting was removed when v1 was retired;
        # see ``docs/COLLECTOR_V1_RETIREMENT.md``. ``app.py`` logs a
        # warning if an old settings.json still carries the key.
        "collector_v2_num_workers": 15,
        "retention_days": 30,
        "language": "en",
        "timezone": "Europe/Berlin",
        "date_format": "DD.MM.YYYY",
        "time_format": "24h",
        "https": {
            "enabled": False,
            "cert_file": "",
            "key_file": "",
        },
        "auth": {
            "enabled": False,
            "type": "ldap",
            # Declared so get_settings() always yields a LIST for the LDAP
            # allowlist. It was undeclared, so when a null landed on disk there
            # was no default to fall back to and the Settings page 500'd on
            # `allowed_users | join`. Empty list = allow all AD users.
            "allowed_users": [],
            "ldap_url": "",
            "ldap_base_dn": "",
            "ldap_user_filter": "(sAMAccountName={username})",
            "ldap_bind_user": "",
            "ldap_bind_password": "",
            "session_timeout_minutes": 480,
            # S2-12 (W3) account lockout knobs. 10 failures in 30 min
            # locks the account for 15 min (rolling window). Set
            # lockout_threshold=0 to disable lockout entirely.
            "lockout_threshold": 10,
            "lockout_window_minutes": 30,
            "lockout_duration_minutes": 15,
        },
        "email": {
            "enabled": False,
            "smtp_server": "",
            "smtp_port": 587,
            "use_tls": True,
            "username": "",
            "password": "",
            "from_address": "",
            "recipients": [],
            "send_on_critical": True,
            "send_on_warning": False,
            "dashboard_url": "http://localhost:5000",
        },
        "scheduled_reports": {
            "enabled": False,
            "daily_enabled": True,
            "daily_time": "07:00",
            "weekly_enabled": False,
            "weekly_day": "monday",
            "weekly_time": "07:00",
            "email_report": True,
            "include_pdf": True,
        },
        "webhooks": {
            "enabled": False,
            "teams_webhook_url": "",
            "send_on_critical": True,
            "send_on_warning": False,
        },
        "maintenance_windows": [],
        # ─────────────────────────────────────────────────────────────────
        # Scheduled restarts (Operations page). These MUST be declared here
        # even though POST /api/scheduled-restarts writes them straight into
        # the raw on-disk dict: ``get_settings()`` below builds its result by
        # iterating _DEFAULT_SETTINGS only, so any top-level key missing from
        # this dict is invisible to every reader. Omitting them silently broke
        # the whole feature — the GET endpoint, the manual /api/restart-now
        # trigger, and the restart_scheduler thread all read through
        # get_settings() and saw nothing, so no restart ever ran while saves
        # kept reporting success. Defaults below must stay in sync with the
        # fallbacks in routes/api/misc.py's GET handler.
        # Regression guard: tests/test_scheduled_restarts_roundtrip.py
        # ─────────────────────────────────────────────────────────────────
        "scheduled_flask_restart": {
            "enabled": False,
            "schedule": "daily",
            "time": "03:00",
            "day": "sunday",
        },
        "scheduled_server_restart_schedule": {
            "enabled": False,
            "schedule": "weekly",
            "time": "03:00",
            "day": "6",
            "month_day": 1,
        },
        "scheduled_server_restarts": [],
        "restart_delay_between_seconds": 60,
        "tls_monitoring": {
            "enabled": False,
            "check_interval_cycles": 30,
            "warning_days": 30,
            "critical_days": 7,
            "certificates": [],
        },
        # In-app compliance dashboard (CSV / GAMP 5 surfacing).
        # When enabled=true, the ``/compliance`` nav item + view routes
        # appear and the read-only API endpoints under ``/api/sop/*``
        # and ``/api/csv-doc/*`` respond. RBAC-admin gating still
        # applies to the ``/api/sop/<id>/execute`` write path. When
        # enabled=false (default) the entire surface is hidden — the
        # nav item disappears, the view routes 404, the API endpoints
        # 404. See URS-200..206 in docs/csv/02_URS.md.
        "compliance": {
            "enabled": False,
        },
        # ─────────────────────────────────────────────────────────────────
        # Detection sections — see the module docstring in detection.py
        # (search _active_level_detector) and docs/STATUS_FLOW.md. Three sections work
        # together: thresholds (simple) < anomaly_detection (statistical)
        # < baseline_detection (smart). Highest enabled wins for cpu/ram/disk
        # WARNING events; CRITICAL thresholds always fire as a safety net.
        # UI: templates/monitoring.html "Detection Mode" sub-section + the
        # detection-mode chip in templates/server_detail.html header.
        # ─────────────────────────────────────────────────────────────────
        # Hardcoded thresholds (the simple detector). Acts as a fallback
        # when smarter detectors (baseline / anomaly) cannot decide.
        # Per-server thresholds (cpu_warning, ram_critical, disk_warning, ...)
        # live on each server config entry — see models.py DEFAULT_THRESHOLDS.
        # This section only holds GLOBAL knobs (enable + slow-collection warning).
        "thresholds": {
            "enabled": True,
            "slow_collection_ms": 10000,
            # Exhaustion floors (docs/plans/DETECTION_FUSION_PLAN.md §2, §7).
            # At or above these percentages the metric is ALWAYS critical —
            # no baseline, however mature, can downgrade it. Finite-resource
            # exhaustion is a hard truth, not a matter of "normal for this
            # server". No CPU floor: CPU has no hard exhaustion point, so it
            # relies on static thresholds + N-of-M gating instead.
            "exhaustion_ram": 98,
            "exhaustion_disk": 95,
        },
        "baseline_detection": {
            "enabled": True,
            "sigma_warning": 2.0,
            "sigma_critical": 3.0,
            "min_samples": 10,
            "history_weeks": 4,
            "recalc_hour": "02:00",
            # Anti-noise gates — prevent the same deviation from flooding
            # the event log and email inbox every 60 seconds.
            "suppression_hours": 4,       # Don't re-alert same server+metric within N hours
            "min_cycles_warning": 3,      # Require 3-of-5 sustained cycles for warning event
            "min_cycles_critical": 2,     # Require 2-of-3 sustained cycles for critical event
            "re_alert_delta": 2.0,        # Only re-alert if value changed by >N% since last alert
            # Baseline authority (docs/plans/DETECTION_FUSION_PLAN.md §2, §7).
            # A mature baseline may DOWNGRADE a static warning/critical verdict
            # to healthy ("normal for this server") only when ALL of these
            # hold: allow_downgrade is on, the data span covers min_span_weeks,
            # and at least min_coverage_pct of the 168 hour-of-week slots have
            # min_samples observations. No authority → static rules verbatim.
            "allow_downgrade": True,      # master switch — instant rollback if off
            "min_span_weeks": 2,          # data history required before downgrade authority
            "min_coverage_pct": 50,       # % of 168 hour-of-week slots that must be populated
            # Deviation-from-self RAISE gates (docs/plans/
            # ALERT_NOISE_AND_VERDICT_UX_PLAN.md §3). Measured on a 30-server
            # fleet, the ungated Layer 3 produced 9 warnings where only 4 had a
            # real threshold breach. Read by detection.py::_deviation_may_raise.
            # Legacy behaviour = {"both", 0, False}.
            "deviation_direction": "high",          # high|both — only rising deviations alarm
            "deviation_min_pct_of_warning": 80,     # must reach N% of the metric's warning bar
            "deviation_requires_authority": True,   # raising needs the same maturity as downgrading
        },
        # Statistical anomaly detection (analytics.py). Runs alongside
        # baseline detection but only the *winning* detector for a given
        # metric/cycle is allowed to fire — see priority rule in detection.py.
        "anomaly_detection": {
            "enabled": True,
            # ``check_every_cycles`` was removed when v1 was retired —
            # under v2 detection runs on every sample (cheap thanks to
            # the analytics baseline cache; see ``analytics.py`` module
            # docstring). Suppression / ack / snooze layers prevent
            # alert storms.
            "suppression_hours": 4,
            # Rate-of-change (acceleration) detector — defaults to OFF because
            # it overlaps heavily with the level detectors and was a noise
            # source. Turn on only if you specifically want acceleration alerts.
            "rate_detection_enabled": False,
            "rate_suppression_hours": 2,
            "low_side_only_when_baseline_on": True,
            # ─── CPU N-of-M gating ───
            # CPU is the noisiest metric — brief spikes during backups, scans,
            # and builds are normal. To prevent a single high reading from
            # firing a warning, require the metric to be in warning state for
            # N out of the last M cycles. CRITICAL still fires immediately
            # (1-of-1) — sustained or massive spikes are never delayed.
            # Set warning_consecutive=1 to disable smoothing entirely.
            # Static-breach spike gate (detection.py::_spike_sustain_cycles).
            # A cpu/ram value over its warning OR critical threshold must hold
            # for this many CONSECUTIVE collector rounds before the badge flips.
            # Stops a one-sample jump from creating an alert. Exhaustion floors
            # are never gated; disk is excluded (it climbs monotonically).
            # 1 disables the gate. Legacy behaviour for RAM was effectively 1.
            "spike_sustain_cycles": 5,
            "cpu_warning_consecutive_cycles": 3,
            "cpu_warning_window_cycles": 5,
        },
        # ─────────────────────────────────────────────────────────────────
        # Security alerts — gates failed-login tracking + the WinRM-based
        # security status checks (Defender / Firewall / BitLocker / ports /
        # local users). UI: templates/monitoring.html "Security Alerts"
        # sub-section. Backend: security_checker.py (collect_security_status,
        # _build_ps_script) + failed_logins.py (_collect_all_failed_logins,
        # scheduled by collector_v2/periodics.py).
        # Display: templates/server_detail.html "Security Status" panel
        # (loadSecurityStatus JS) + failed-login heatmap.
        # Notifications: dispatched via email_alerts.send_alert_email +
        # webhooks.send_teams_webhook from the same call sites that insert
        # the events.
        # ─────────────────────────────────────────────────────────────────
        "security_alerts": {
            "failed_login_tracking": True,
            "login_failure_threshold": 10,
            "lockout_alert": True,         # detects EventID 4740 in failed_logins.py
            # New security monitoring features
            "security_status_check": True,
            "defender_check": True,        # gates _PS_DEFENDER block in security_checker
            "defender_max_sig_age_days": 7,
            "firewall_check": True,        # gates _PS_FIREWALL block
            "bitlocker_check": True,       # gates _PS_BITLOCKER block
            "track_local_users": True,     # gates _PS_LOCAL_USERS block
            "track_open_ports": True,      # gates _PS_OPEN_PORTS block
        },
        # ─────────────────────────────────────────────────────────────────
        # Alert Fatigue — see alert_scoring.py.
        # When enabled, alerts whose (server, metric, severity) score is
        # above `email_throttle_score` will be inserted into the events
        # table but NOT dispatched via email/webhook. This prevents repeated
        # noisy alerts from spamming notification channels.
        # The score itself is a fire/ack ratio over a half-life decay window
        # and is shown in the Noise Digest (monitoring.html).
        # UI: templates/monitoring.html "Alert Fatigue" sub-section.
        # ─────────────────────────────────────────────────────────────────
        "alert_fatigue": {
            "enabled": True,
            "email_throttle_score": 70,    # 0..100 — alerts above this skip email
            "webhook_throttle_score": 80,  # 0..100 — alerts above this skip webhook
            "noise_digest_threshold": 40,  # display-only: scores above flagged amber
            "noise_digest_critical": 70,   # display-only: scores above flagged red
        },
        "drift_detection": {
            "enabled": False,
            "snapshot_types": ["services", "hotfixes", "local_admins"],
            "check_interval_cycles": 60,
            "alert_on_change": True,
            "redaction_patterns": [],
        },
        "workflows": {
            "enabled": True,
            "max_concurrent_executions": 3,
            "execution_timeout_minutes": 30,
            # PowerShell sandbox for run_powershell and condition blocks. See
            # ps_sandbox.py — disabling this is equivalent to "remote code
            # execution as a feature", so leave enabled unless you have a
            # better mitigation upstream (e.g. JEA endpoints, signed scripts).
            "sandbox": {
                "enabled": True,
                "allowed_cmdlets": [],     # extra cmdlets layered on top of DEFAULT_ALLOWED_CMDLETS
                "max_script_chars": 4000,
            },
        },
        # Feature 1.8: scheduled online DB backup. Co-located under data/backups
        # for v1 (no off-box surface). stale_after_hours slightly exceeds
        # interval_hours so one missed run doesn't immediately alarm.
        "database_backup": {
            "enabled": True,
            "interval_hours": 24,
            "keep": 14,
            "stale_after_hours": 26,
            "alert_severity": "warning",
        },
    }

    def get_settings(self) -> dict:
        """Get the settings section of the config, with defaults for missing keys."""
        config = self._get_raw_config()
        settings = config.get("settings", {})
        # Merge defaults for any missing top-level or nested keys
        merged = dict(self._DEFAULT_SETTINGS)
        for key, default_val in self._DEFAULT_SETTINGS.items():
            if key in settings:
                if isinstance(default_val, dict):
                    # Merge nested dict defaults
                    merged[key] = dict(default_val)
                    merged[key].update(settings[key])
                else:
                    merged[key] = settings[key]
        return merged

    def get_server_by_name(self, name: str) -> ServerConfig | None:
        """Look up a single server by name."""
        for server in self.get_servers():
            if server.name == name:
                return server
        return None

    def create_backup(self) -> str:
        """Copy config.json to data/config_backups/config_<timestamp>.json.
        Creates the backup directory if needed. Prunes to keep only latest 20."""
        backup_dir = self.config_path.parent / "data" / "config_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"config_{timestamp}.json"
        backup_path = backup_dir / backup_name
        shutil.copy2(self.config_path, backup_path)
        # Prune old backups, keep latest 20
        backups = sorted(backup_dir.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[20:]:
            old.unlink()
        logger.info("Config backup created: %s", backup_name)
        return backup_name

    def list_backups(self) -> list[dict]:
        """Return list of backups sorted newest first."""
        backup_dir = self.config_path.parent / "data" / "config_backups"
        if not backup_dir.exists():
            return []
        backups = []
        for p in sorted(backup_dir.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = p.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            backups.append({
                "filename": p.name,
                "timestamp": mtime.isoformat(),
                "size_bytes": stat.st_size,
            })
        return backups

    def restore_backup(self, filename: str) -> bool:
        """Restore config from a backup file. Validates filename against traversal."""
        import re
        if not re.match(r'^config_\d{8}_\d{6}\.json$', filename):
            raise ValueError(f"Invalid backup filename: {filename}")
        backup_dir = self.config_path.parent / "data" / "config_backups"
        backup_path = backup_dir / filename
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found: {filename}")
        shutil.copy2(backup_path, self.config_path)
        with self._lock:
            self._cache = None
            self._cache_mtime = 0.0
        logger.info("Config restored from backup: %s", filename)
        return True

    @staticmethod
    def _deep_merge_settings(base: dict, incoming: dict) -> dict:
        """Recursively merge ``incoming`` over ``base`` and return a new dict.

        Dicts merge key-by-key; lists and scalars REPLACE wholesale. That split
        is deliberate:

          * merging dicts is what makes a partial settings POST safe — an omitted
            sub-tree keeps its stored value instead of vanishing.
          * replacing lists is what keeps deletion working — removing a TLS
            certificate, an email recipient or a maintenance window is expressed
            by posting the shorter list, and an element-wise merge would make
            those items unremovable.
        """
        merged = dict(base)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ConfigManager._deep_merge_settings(merged[key], value)
            else:
                merged[key] = value
        return merged

    def save_config(self, servers: list[dict], settings: dict | None = None):
        """Write updated config to disk and clear cache. Encrypts passwords.

        ``settings`` is MERGED over what is already on disk, not substituted for
        it (Bug 5, docs/plans/CRITICAL_BUGS_REMEDIATION.md §5). This used to be a
        straight replacement, which meant any caller posting a partial settings
        block silently deleted every key it omitted: one save from the Monitoring
        page wiped the operator's SMTP credentials, Teams webhook URL, LDAP
        directory config and restart schedule, and reset retention, poll interval
        and UI language to defaults — behind a success toast, because
        get_settings() then backfilled the defaults indistinguishably.

        ``servers`` keeps replace semantics: the server list is always managed
        wholesale, and merging it would make deletions impossible.
        """
        # Create backup before saving
        try:
            self.create_backup()
        except Exception:
            logger.warning("Failed to create config backup before save", exc_info=True)

        # Encrypt all passwords before saving
        for s in servers:
            pw = s.get("password", "")
            if pw:
                s["password"] = encrypt_password(pw)

        # Merge against the RAW on-disk settings, never get_settings(): the latter
        # is filtered through _DEFAULT_SETTINGS, so round-tripping it would drop
        # any undeclared top-level key (that is exactly how the Scheduled Restarts
        # keys were being lost — see Bug 1) and would also bake every default into
        # config.json on each write.
        on_disk = (self._get_raw_config().get("settings") or {})
        config = {"servers": servers}
        if settings:
            config["settings"] = self._deep_merge_settings(on_disk, settings)
        else:
            config["settings"] = dict(on_disk)

        with self._lock:
            with open(self.config_path, "w") as f:
                json.dump(config, f, indent=2)
            restrict_config_permissions(self.config_path)
            self._cache = None
            self._cache_mtime = 0.0
        logger.info("Config saved: %d servers", len(servers))

    def _get_raw_config(self) -> dict:
        """Load config with mtime-based caching (re-check file at most every 5s)."""
        now = time.time()
        with self._lock:
            if self._cache is not None and (now - self._cache_checked_at) < 5.0:
                return self._cache

            try:
                mtime = self.config_path.stat().st_mtime
            except FileNotFoundError:
                logger.info("Config file not found at %s (first run?)", self.config_path)
                return {"servers": [], "settings": {}}

            if self._cache is not None and mtime == self._cache_mtime:
                self._cache_checked_at = now
                return self._cache

            try:
                with open(self.config_path, "r") as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.exception("Failed to parse config file %s", self.config_path)
                return {"servers": [], "settings": {}}
            self._cache_mtime = mtime
            self._cache_checked_at = now
            return self._cache


    def _migrate_plaintext_passwords(self):
        """One-time migration: encrypt any plain-text passwords found in config.

        Covers the same three canonical credential paths that tools/rekey.py
        rotates — servers[*].password, settings.email.password and
        settings.auth.ldap_bind_password. Keep the two lists in sync: rekey
        SKIPS any value without the 'enc:' prefix, so a credential this
        migration misses is a credential key rotation silently never
        re-protects. That is exactly how ldap_bind_password stayed plain text.
        """
        if not self.config_path.exists():
            logger.info("Config file not found at %s (first run?)", self.config_path)
            return
        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read config for password migration")
            return

        count = 0
        for s in config.get("servers", []):
            pw = s.get("password", "")
            if pw and not pw.startswith("enc:"):
                s["password"] = encrypt_password(pw)
                count += 1

        # settings.* credentials. Guarded on dict type so a hand-mangled config
        # can't raise here — this runs in __init__ and must never block startup.
        settings = config.get("settings")
        if isinstance(settings, dict):
            migrated_keys = []
            email = settings.get("email")
            if isinstance(email, dict):
                pw = email.get("password", "")
                if pw and not str(pw).startswith("enc:"):
                    email["password"] = encrypt_password(pw)
                    count += 1
                    migrated_keys.append("settings.email.password")
            auth = settings.get("auth")
            if isinstance(auth, dict):
                pw = auth.get("ldap_bind_password", "")
                if pw and not str(pw).startswith("enc:"):
                    auth["ldap_bind_password"] = encrypt_password(pw)
                    count += 1
                    migrated_keys.append("settings.auth.ldap_bind_password")
            if migrated_keys:
                # Names only, never values.
                logger.info("Encrypting plain-text credential(s): %s",
                            ", ".join(migrated_keys))

        if count > 0:
            with open(self.config_path, "w") as f:
                json.dump(config, f, indent=2)
            restrict_config_permissions(self.config_path)
            logger.info("Migrated %d plaintext passwords to encrypted", count)

    def get_maintenance_windows(self):
        """Get the maintenance windows list from settings."""
        settings = self.get_settings()
        return settings.get("maintenance_windows", [])

    def save_maintenance_windows(self, windows):
        """Save updated maintenance windows list."""
        config = self._get_raw_config()
        settings = config.get("settings", {})
        settings["maintenance_windows"] = windows
        # Re-save full config
        servers = config.get("servers", [])
        with self._lock:
            import json as _json
            with open(self.config_path, "w") as f:
                _json.dump({"servers": servers, "settings": settings}, f, indent=2)
            from crypto_utils import restrict_config_permissions
            restrict_config_permissions(self.config_path)
            self._cache = None
            self._cache_mtime = 0.0
        logger.info("Maintenance windows saved: %d windows", len(windows))

    def has_backup_admin(self) -> bool:
        """Return True if settings.auth.backup_admin has both username and password_hash set."""
        config = self._get_raw_config()
        backup_admin = (
            config.get("settings", {})
            .get("auth", {})
            .get("backup_admin", {})
        )
        return bool(backup_admin.get("username")) and bool(backup_admin.get("password_hash"))

    def set_backup_admin(self, username: str, password_hash: str):
        """Save backup_admin credentials to config.

        Also stamps `password_set_at` (UTC ISO) for the S2-13 (W4) policy /
        future Sprint-3 nag-banner that alerts on passwords older than 90 days.
        """
        # Create backup before saving
        try:
            self.create_backup()
        except Exception:
            logger.warning("Failed to create config backup before save", exc_info=True)

        config = self._get_raw_config()
        settings = config.get("settings", {})
        auth = settings.setdefault("auth", {})
        auth["backup_admin"] = {
            "username": username,
            "password_hash": password_hash,
            "password_set_at": datetime.now(timezone.utc).isoformat(),
        }
        # Re-save full config
        servers = config.get("servers", [])
        with self._lock:
            import json as _json
            with open(self.config_path, "w") as f:
                _json.dump({"servers": servers, "settings": settings}, f, indent=2)
            from crypto_utils import restrict_config_permissions
            restrict_config_permissions(self.config_path)
            self._cache = None
            self._cache_mtime = 0.0
        logger.info("Backup admin credentials saved for user: %s", username)

    def get_raw_servers(self) -> list[dict]:
        """Get raw server dicts from config (passwords still encrypted).
        Used by the API to merge with incoming changes."""
        config = self._get_raw_config()
        return list(config.get("servers", []))


if __name__ == "__main__":
    mgr = ConfigManager()
    servers = mgr.get_servers()
    print(f"Loaded {len(servers)} servers:")
    for s in servers:
        print(f"  {s.name} ({s.type}) @ {s.host}:{s.port}")
        print(f"    Thresholds: {s.thresholds}")
    settings = mgr.get_settings()
    print(f"Settings: {settings}")
    print("Config manager test passed!")
