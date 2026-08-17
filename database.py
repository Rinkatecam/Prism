"""SQLite database module for Prism monitoring system."""

import csv
import hashlib
import io
import json as _json
import sqlite3
import logging
import time
import threading
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("prism.db")


DB_PATH = Path(__file__).parent / "data" / "prism.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    cpu_percent REAL,
    ram_percent REAL,
    disk_c_percent REAL,
    disk_d_percent REAL,
    status TEXT NOT NULL DEFAULT 'unknown',
    collection_time_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_metrics_server_time
    ON metrics (server_name, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_metrics_timestamp
    ON metrics (timestamp DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    event_type TEXT NOT NULL,
    metric TEXT,
    value REAL,
    threshold REAL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_server_time
    ON events (server_name, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON events (timestamp DESC);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    log_source TEXT NOT NULL,
    level TEXT NOT NULL,
    event_id INTEGER,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_server_time
    ON logs (server_name, timestamp DESC);

-- Covering index for the fleet-wide log queries the Reports page needs.
-- Measured on a copy of the live 1.77M-row table: a 24-hour by-server-and-level
-- query went 450ms -> 210ms, and the top-signature query 1246ms -> 819ms
-- (chosen as a COVERING INDEX). A partial index on level <> 'Information' was
-- measured too and the planner would not use it, so this is the plain form.
CREATE INDEX IF NOT EXISTS idx_logs_time_level
    ON logs (timestamp, level, server_name, log_source, event_id);

-- Identical log lines rolled up to one row per signature per hour.
--
-- The live table had a 10.8x duplicate ratio: 1,771,744 rows collapsed to
-- 163,986 distinct (server, source, level, event_id, message) signatures. This
-- is what survives the raw table's short retention — `logs` keeps recent rows
-- for drill-down, signatures keep the history for "is this new?" and "how many
-- servers see this?", which are the questions a fleet report can actually
-- answer.
--
-- WITHOUT ROWID because the primary key IS the row; a second b-tree would just
-- double the write cost.
CREATE TABLE IF NOT EXISTS log_signatures (
    server_name TEXT    NOT NULL,
    log_source  TEXT    NOT NULL,
    level       TEXT    NOT NULL,
    event_id    INTEGER,
    msg_hash    TEXT    NOT NULL,
    hour_utc    TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    sample      TEXT    NOT NULL,
    PRIMARY KEY (server_name, log_source, level, event_id, msg_hash, hour_utc)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_log_sig_hour
    ON log_signatures (hour_utc);
CREATE INDEX IF NOT EXISTS idx_log_sig_first_seen
    ON log_signatures (first_seen);

CREATE TABLE IF NOT EXISTS anomaly_suppression (
    server_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'above_baseline',
    last_alert_time TEXT NOT NULL,
    last_severity TEXT NOT NULL,
    last_value REAL NOT NULL,
    UNIQUE(server_name, metric, direction)
);

CREATE TABLE IF NOT EXISTS anomaly_acknowledgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    ack_type TEXT NOT NULL DEFAULT 'acknowledged',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    snooze_until TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_ack_server_metric ON anomaly_acknowledgments(server_name, metric);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    username TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    details TEXT,
    -- Per-event forensic context (S1-8 from AUDIT-2026-05).
    -- Auto-populated by log_audit() from Flask request/session/g when available;
    -- collector/scheduler call-sites pass NULL.
    source_ip TEXT,
    session_id TEXT,        -- hash of session cookie's login_time, NOT the cookie itself
    request_id TEXT,        -- per-request UUID set in before_request hook
    -- Hash chain (S1-7): row_hash = sha256(prev_hash || row content). Any tampering
    -- with a past row breaks the chain detectably even by a process that bypassed
    -- the SQL-layer triggers (e.g. by editing the SQLite file out of band).
    prev_hash TEXT,
    row_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp
    ON audit_log (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_category
    ON audit_log (category);

-- Append-only protection: triggers block UPDATE and DELETE on audit_log so an
-- attacker (or a buggy code path) can't tamper with or erase the audit trail.
-- Only INSERT is allowed. To rotate old records, use a separate retention job
-- that copies to an archive table BEFORE this trigger blocks any cleanup.
-- (Currently we don't auto-rotate audit_log — it grows forever, which is the
-- right default for a security-sensitive table.)
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only — UPDATE not allowed');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only — DELETE not allowed');
END;

-- ---------------------------------------------------------------------------
-- S2-1 (BL3) — session containment primitives
--
-- revoked_sessions: marks a specific (username, login_time) tuple as forcibly
--   ended by an admin. Checked in auth.before_request — if a request's session
--   matches a row here, the session is cleared and the request is denied.
--
-- disabled_users: blocks login + active sessions for a given username. The
--   username column is stored lower-cased to match the case-insensitive login
--   path (auth.py compares username.lower() == backup_user.lower()).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS revoked_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    login_time TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    revoked_by TEXT NOT NULL DEFAULT 'system',
    UNIQUE(username, login_time)
);
CREATE INDEX IF NOT EXISTS idx_revoked_sessions_user ON revoked_sessions(username);

CREATE TABLE IF NOT EXISTS disabled_users (
    username TEXT PRIMARY KEY,
    disabled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    disabled_by TEXT NOT NULL DEFAULT 'system',
    reason TEXT
);

-- S2-12 (W3) — per-username auth failure tracking for account lockout.
-- Cleared on successful login. Old rows pruned by cleanup_old_data (24h).
CREATE TABLE IF NOT EXISTS auth_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    attempted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_failures_user_time ON auth_failures(username, attempted_at DESC);

-- ---------------------------------------------------------------------------
-- Per-server RBAC (P0 from AUDIT_2026-04-28)
--
-- Permission levels (cumulative):
--   view    — see metrics, events, logs for this server
--   control — view + restart services, run safe workflows
--   admin   — control + power actions, install updates, run any workflow
--
-- Default policy: when the user_server_acl table is EMPTY, RBAC is in
-- "permissive" mode and any authenticated user has admin-equivalent access
-- (preserves backwards compatibility with single-admin deployments).
-- The moment the first row is inserted, enforcement begins for ALL users.
--
-- Tier-0 servers (ServerConfig.tier == 0) require an explicit 'admin' ACL
-- row even in permissive mode. There is no implicit-tier-0 access.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_server_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,            -- lower-cased, bare (no DOMAIN\\)
    server_name TEXT NOT NULL,         -- '*' = wildcard, all servers
    permission TEXT NOT NULL,          -- 'view' | 'control' | 'admin'
    granted_by TEXT NOT NULL DEFAULT 'system',
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(username, server_name)
);
CREATE INDEX IF NOT EXISTS idx_acl_user ON user_server_acl(username);
CREATE INDEX IF NOT EXISTS idx_acl_server ON user_server_acl(server_name);

-- Tier-0 destructive actions can require dual-admin approval. When a user
-- requests one, we stage it here and a second admin must approve before
-- the action executes. Approvals expire after 1 hour.
CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    requested_by TEXT NOT NULL,
    server_name TEXT NOT NULL,
    action TEXT NOT NULL,              -- 'restart' | 'shutdown' | 'install_updates' | 'workflow'
    payload_json TEXT,                 -- JSON-encoded action parameters
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected' | 'expired' | 'consumed'
    approved_by TEXT,
    decided_at TEXT,
    expires_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+1 hour'))
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_approvals(status);

CREATE TABLE IF NOT EXISTS restart_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    run_id TEXT NOT NULL,
    server_name TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    updates_installed INTEGER DEFAULT 0,
    -- F-A-1 (CSV-11 remediation): operator attribution on the row
    -- itself. Was previously only available via the audit_log row
    -- written by the route handler, joined by timestamp + server
    -- which is fragile. Default 'system' for legacy rows and for
    -- truly system-driven restarts (the periodics scheduler).
    actor TEXT NOT NULL DEFAULT 'system'
);

-- ── Compliance / CSV: SOP execution log ─────────────────────────────
-- Each row is one execution of a documented Standard Operating
-- Procedure (e.g., quarterly ACL review per SOP-03). The compliance
-- dashboard reads ``MAX(executed_at) GROUP BY sop_id`` to compute
-- "current / due-soon / overdue" status.
--
-- **APPEND-ONLY** (F-PHD-2 remediation). SOP executions are regulated
-- evidence — they prove the operator performed the procedure at
-- time T. Mutating a row after the fact would defeat the integrity
-- guarantee, same as for ``audit_log``. The UPDATE/DELETE triggers
-- below enforce this at the DB level. If an operator records a
-- typo'd execution by mistake, the documented remediation is to
-- record a NEW execution with ``result='partial'`` and a note
-- saying "supersedes #N due to typo" — see SOP-05.
CREATE TABLE IF NOT EXISTS sop_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sop_id TEXT NOT NULL,                    -- "SOP-01" .. "SOP-09"
    executed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    executed_by TEXT NOT NULL,               -- username
    result TEXT NOT NULL DEFAULT 'pass',     -- pass | fail | partial
    notes TEXT,                              -- free-form, ≤ 2000 char convention
    evidence_ref TEXT                        -- file path / ticket id / URL
);

CREATE INDEX IF NOT EXISTS idx_sop_log_sop_time
    ON sop_log(sop_id, executed_at DESC);

CREATE TRIGGER IF NOT EXISTS sop_log_no_update
BEFORE UPDATE ON sop_log
BEGIN
    SELECT RAISE(ABORT, 'sop_log is append-only — UPDATE not allowed; record a new execution with result=partial that supersedes the original');
END;

CREATE TRIGGER IF NOT EXISTS sop_log_no_delete
BEFORE DELETE ON sop_log
BEGIN
    SELECT RAISE(ABORT, 'sop_log is append-only — DELETE not allowed');
END;

CREATE INDEX IF NOT EXISTS idx_restart_log_run
    ON restart_log(run_id);

CREATE INDEX IF NOT EXISTS idx_restart_log_ts
    ON restart_log(timestamp);

CREATE TABLE IF NOT EXISTS server_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#6B7280',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS server_tag_assignments (
    server_name TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES server_tags(id) ON DELETE CASCADE,
    UNIQUE(server_name, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_tag_assign_server ON server_tag_assignments(server_name);
CREATE INDEX IF NOT EXISTS idx_tag_assign_tag ON server_tag_assignments(tag_id);

CREATE TABLE IF NOT EXISTS tls_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 443,
    subject TEXT,
    issuer TEXT,
    not_before TEXT,
    not_after TEXT,
    days_remaining INTEGER,
    last_checked TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    error TEXT,
    UNIQUE(server_name, host, port)
);
CREATE INDEX IF NOT EXISTS idx_tls_server ON tls_certificates(server_name);
CREATE INDEX IF NOT EXISTS idx_tls_expiry ON tls_certificates(days_remaining ASC);

CREATE TABLE IF NOT EXISTS health_check_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    server_name TEXT NOT NULL,
    check_type TEXT NOT NULL DEFAULT 'tcp',
    target_host TEXT NOT NULL,
    target_port INTEGER NOT NULL,
    http_path TEXT DEFAULT '/',
    expected_status INTEGER DEFAULT 200,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- Validate the TLS chain and hostname on an `https` check. Default ON:
    -- without it a check proves that something answered on the port, not that
    -- it was the service you meant. Set to 0 per check for internal endpoints
    -- with self-signed certificates — an ordinary case, and the reason this is
    -- a row and not a constant.
    verify_tls INTEGER NOT NULL DEFAULT 1,
    UNIQUE(server_name, check_type, target_host, target_port)
);
CREATE INDEX IF NOT EXISTS idx_hc_config_server ON health_check_config(server_name);

CREATE TABLE IF NOT EXISTS health_check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    check_type TEXT NOT NULL,
    target_host TEXT NOT NULL,
    target_port INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    response_time_ms REAL,
    error TEXT,
    last_checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_hc_results_server ON health_check_results(server_name);
-- The probe's full identity, plus `id` so the index covers "newest row for
-- this probe" without touching the table. `get_health_check_summary` runs on
-- the dashboard's 5-second refresh path and this is what keeps it off a full
-- scan of an append-only history table: measured at one month of retention
-- with 12 probes (103,680 rows), 65.55 ms without it against 0.033 ms with it
-- and the config-driven query it enables. The existing server_name index
-- cannot serve that lookup — two probes on one host share a server_name.
CREATE INDEX IF NOT EXISTS idx_hc_results_probe
    ON health_check_results(server_name, check_type, target_host, target_port, id);

-- F4: Baseline Deviation Alerts
CREATE TABLE IF NOT EXISTS metric_baselines (
    server_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    hour_of_week INTEGER NOT NULL,
    avg_value REAL NOT NULL,
    stddev REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(server_name, metric, hour_of_week)
);
CREATE INDEX IF NOT EXISTS idx_baselines_server ON metric_baselines(server_name, metric);

-- F5: Failed Login Heatmap
CREATE TABLE IF NOT EXISTS failed_logins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    source_ip TEXT,
    source_port TEXT,
    account_name TEXT,
    domain TEXT,
    event_id INTEGER,
    logon_type TEXT,
    workstation TEXT,
    status_code TEXT,
    sub_status TEXT,
    process_name TEXT,
    UNIQUE(server_name, timestamp, account_name, source_ip)
);
CREATE INDEX IF NOT EXISTS idx_failed_logins_server ON failed_logins(server_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_failed_logins_time ON failed_logins(timestamp DESC);

-- F6: Config Drift Detection
CREATE TABLE IF NOT EXISTS config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    snapshot_type TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_server ON config_snapshots(server_name, snapshot_type, timestamp DESC);

CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    snapshot_type TEXT NOT NULL,
    change_type TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_server ON config_changes(server_name, timestamp DESC);

-- F7: Correlated Incident Grouping
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status TEXT NOT NULL DEFAULT 'open',
    severity TEXT NOT NULL DEFAULT 'warning',
    title TEXT NOT NULL,
    description TEXT,
    root_cause_server TEXT,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, created_at DESC);

CREATE TABLE IF NOT EXISTS incident_events (
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(incident_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_incident_events ON incident_events(incident_id);

CREATE TABLE IF NOT EXISTS alert_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    metric TEXT NOT NULL,
    event_type TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.0,
    fire_count INTEGER NOT NULL DEFAULT 0,
    ack_count INTEGER NOT NULL DEFAULT 0,
    suppress_count INTEGER NOT NULL DEFAULT 0,
    last_fired TEXT,
    last_acked TEXT,
    last_sent_email TEXT,
    last_sent_webhook TEXT,
    last_resolved TEXT,
    UNIQUE(server_name, metric, event_type)
);
CREATE INDEX IF NOT EXISTS idx_alert_scores ON alert_scores(score DESC);

-- Feature 1.8: single-row record of the scheduled DB-backup outcome. id is
-- pinned to 1 (CHECK) so there is exactly one row, upserted in place.
CREATE TABLE IF NOT EXISTS backup_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_success_ts TEXT,
    last_ok INTEGER,
    last_path TEXT,
    last_error TEXT,
    last_alerted_ts TEXT
);

CREATE TABLE IF NOT EXISTS server_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_name TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    dependency_type TEXT NOT NULL DEFAULT 'service',
    custom_type_name TEXT,
    target_mode TEXT NOT NULL DEFAULT 'port',
    port INTEGER,
    service_name TEXT,
    process_name TEXT,
    description TEXT,
    UNIQUE(server_name, depends_on, dependency_type)
);
CREATE INDEX IF NOT EXISTS idx_deps_server ON server_dependencies(server_name);
CREATE INDEX IF NOT EXISTS idx_deps_target ON server_dependencies(depends_on);

-- F10: Runbook Library with Quick-Actions
CREATE TABLE IF NOT EXISTS runbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    category TEXT DEFAULT 'general',
    steps_json TEXT NOT NULL,
    created_by TEXT DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    is_builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runbook_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runbook_id INTEGER NOT NULL REFERENCES runbooks(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status TEXT NOT NULL DEFAULT 'pending',
    output TEXT,
    executed_by TEXT DEFAULT 'system',
    dry_run INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rb_exec_server ON runbook_executions(server_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_rb_exec_runbook ON runbook_executions(runbook_id, timestamp DESC);

-- Workflow categories
CREATE TABLE IF NOT EXISTS workflow_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#8B5CF6',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Workflow definitions
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    category_id INTEGER REFERENCES workflow_categories(id) ON DELETE SET NULL,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    trigger_config TEXT DEFAULT '{}',
    canvas_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    is_template INTEGER NOT NULL DEFAULT 0,
    created_by TEXT DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_workflows_cat ON workflows(category_id);

-- Workflow execution records
CREATE TABLE IF NOT EXISTS workflow_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger_source TEXT DEFAULT 'manual',
    executed_by TEXT DEFAULT 'system',
    summary TEXT,
    duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wf_exec_workflow ON workflow_executions(workflow_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_exec_status ON workflow_executions(status);

-- Per-step execution results
CREATE TABLE IF NOT EXISTS workflow_execution_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES workflow_executions(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    node_label TEXT,
    server_name TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    output TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_wf_exec_steps ON workflow_execution_steps(execution_id);

-- Security status snapshot (latest state per server)
CREATE TABLE IF NOT EXISTS server_security_status (
    server_name TEXT PRIMARY KEY,
    last_checked TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    -- Defender
    defender_enabled INTEGER,
    defender_rt_protection INTEGER,
    defender_sig_age_days INTEGER,
    defender_engine_version TEXT,
    -- Firewall
    firewall_service_running INTEGER,
    firewall_domain_enabled INTEGER,
    firewall_private_enabled INTEGER,
    firewall_public_enabled INTEGER,
    -- BitLocker
    bitlocker_encrypted_pct INTEGER,
    bitlocker_status TEXT,
    -- Open ports (JSON array)
    open_ports_json TEXT,
    -- Local users (JSON array)
    local_users_json TEXT,
    raw_data TEXT
);
"""


class _PooledConnection(sqlite3.Connection):
    """A connection whose ``close()`` does nothing.

    Every call site in this module follows the shape::

        conn = self._get_conn()
        try:
            ...
        finally:
            conn.close()

    Pooling connections per thread therefore requires either rewriting ~200
    call sites or neutralising ``close()``. The latter keeps the change to one
    place and cannot be forgotten at a new call site. ``Database
    .close_thread_connection()`` performs the real close via
    ``sqlite3.Connection.close``.
    """

    def close(self) -> None:  # noqa: D102 - intentional no-op, see class docstring
        pass


class Database:
    """Thread-safe SQLite database wrapper. One writer (collector), many readers (API)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        # Per-thread connection pool; see _get_conn.
        self._thread_local = threading.local()
        # F-D-1 (CSV-11 remediation): tracking surface for log_audit
        # insert failures so the watchdog / health endpoint can detect
        # the "audit-blind" condition. Atomic int updates in CPython
        # are safe across threads without a lock.
        self._audit_insert_failures: int = 0
        self._audit_mirror_failures: int = 0
        self._init_schema()
        logger.info("Database initialized at %s", self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use.

        Connections are POOLED PER THREAD and deliberately outlive the caller.
        Every call site follows ``conn = self._get_conn() ... finally:
        conn.close()``, so rather than rewrite ~200 call sites the pooled
        connection's ``close()`` is a no-op (see _PooledConnection) and the real
        close happens in ``close_thread_connection()``.

        Why: this used to open a brand-new connection per operation — connect,
        ``PRAGMA journal_mode=WAL``, ``PRAGMA busy_timeout``, then close —
        measured at 2.02 ms of pure overhead on EVERY database call, of which
        there are several per collected metric across a 29-server fleet polling
        every 60s. ``journal_mode`` is persisted in the database file, so
        re-setting it on every connection was pure waste.

        Threads are bounded and long-lived (30 collector workers, the
        aggregator, the periodics thread, the schedulers and the web server's
        thread pool), so the pool tops out at a few dozen handles.
        """
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            # Clear any transaction a previous caller left open. With
            # per-operation connections an uncommitted transaction died with the
            # connection; pooled, it would leak into the next unrelated caller.
            try:
                conn.rollback()
                return conn
            except sqlite3.Error:
                # Connection is unusable (rare). Fall through and rebuild it.
                try:
                    sqlite3.Connection.close(conn)
                except Exception:
                    pass
                self._thread_local.conn = None

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                               factory=_PooledConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL is the standard durable-enough pairing: still crash-safe
        # for an application crash, and the relaxation is that a HOST power loss
        # can lose the last commits. Measured 8.4x faster than the SQLite
        # default of FULL (0.07 ms vs 0.61 ms per metric insert), because FULL
        # fsyncs on every commit and this process commits per row. Losing the
        # last few seconds of a 60-second monitoring sample on a power cut is an
        # acceptable trade; a global write lock held 10s of every minute is not.
        conn.execute("PRAGMA synchronous=NORMAL")
        # S3-13 (P11) — without busy_timeout, sqlite returns OperationalError
        # immediately on contention. WAL prevents most reader/writer races,
        # but checkpointing the WAL still acquires a write lock, and concurrent
        # inserts from collector + restart + workflow + Flask threads
        # occasionally race. 5s gives the contender time to retry transparently
        # before the caller sees a "database is locked" error and silently
        # drops an audit/metric row.
        conn.execute("PRAGMA busy_timeout = 5000")
        self._thread_local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        """Really close this thread's pooled connection.

        ``conn.close()`` is a no-op on pooled connections, so a thread that is
        shutting down calls this to release the handle. Safe to call when no
        connection exists.
        """
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            self._thread_local.conn = None
            try:
                sqlite3.Connection.close(conn)
            except Exception:
                logger.debug("Failed closing pooled connection", exc_info=True)

    def _init_schema(self):
        conn = self._get_conn()
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
            # Migration: add correlation_id to events table
            try:
                conn.execute("ALTER TABLE events ADD COLUMN correlation_id TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Migration: add extended fields to failed_logins table
            for col in ["source_port TEXT", "domain TEXT", "logon_type TEXT",
                        "workstation TEXT", "status_code TEXT", "sub_status TEXT",
                        "process_name TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE failed_logins ADD COLUMN {col}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass  # Column already exists
            # Migration: add name to health_check_config
            try:
                conn.execute("ALTER TABLE health_check_config ADD COLUMN name TEXT DEFAULT ''")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration: add verify_tls to health_check_config.
            #
            # DEFAULT 1 applies to existing rows too, so an upgrade turns
            # certificate validation ON for HTTPS checks that were created
            # while it was unconditionally off. That is the intended direction
            # — the previous behaviour was the defect — and it is a visible
            # change: a check against a self-signed endpoint will start
            # reporting down with a certificate error naming the problem, and
            # the operator turns verification off for that one check.
            #
            # Migrating existing rows to 0 was considered and rejected. It
            # would preserve every current reading, and bake the finding in
            # permanently for exactly the installations that already have it.
            try:
                conn.execute("ALTER TABLE health_check_config "
                             "ADD COLUMN verify_tls INTEGER NOT NULL DEFAULT 1")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            # Migration: server_security_status is new table, no migration needed

            # Migration: add custom_type_name, target_mode, service_name, process_name to server_dependencies
            for col in ["custom_type_name TEXT", "target_mode TEXT DEFAULT 'port'", "service_name TEXT", "process_name TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE server_dependencies ADD COLUMN {col}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass

            # Migration: add forensic context + hash chain columns to audit_log (S1-7 + S1-8 from
            # docs/AUDIT-2026-05.md). Existing rows will have NULL for all five — the chain is
            # validated only over rows that have row_hash populated. Triggers below (no_update,
            # no_delete) still apply, so this is purely additive on existing data.
            for col in ["source_ip TEXT", "session_id TEXT", "request_id TEXT",
                        "prev_hash TEXT", "row_hash TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass

            # F-A-1 (CSV-11 remediation): add ``actor`` to restart_log
            # on existing DBs. Default ``'system'`` so legacy rows
            # remain attributable to system-driven restarts. Idempotent
            # — caught by OperationalError if the column already exists
            # (fresh DBs get it from the CREATE TABLE above).
            try:
                conn.execute("ALTER TABLE restart_log ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            # Feature 1.1: per-channel last-sent + last-resolved timestamps on
            # alert_scores for the repeat-interval throttle (bounds how often a
            # recurring alert re-notifies). Additive; fresh DBs get them from the
            # CREATE TABLE above.
            for col in ["last_sent_email TEXT", "last_sent_webhook TEXT",
                        "last_resolved TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE alert_scores ADD COLUMN {col}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    # ── Write operations (collector thread only) ──

    def insert_metric(self, server_name: str, cpu: float | None, ram: float | None,
                      disk_c: float | None, disk_d: float | None,
                      status: str, collection_time_ms: int | None = None):
        start = time.time()
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO metrics
                       (server_name, cpu_percent, ram_percent, disk_c_percent, disk_d_percent,
                        status, collection_time_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (server_name, cpu, ram, disk_c, disk_d, status, collection_time_ms)
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to insert metric for server %s", server_name)
                return
            finally:
                conn.close()
        elapsed = time.time() - start
        if elapsed > 1.0:
            logger.warning("Slow insert_metric for %s: %.2fs", server_name, elapsed)

    def insert_event(self, server_name: str, event_type: str, metric: str | None,
                     value: float | None, threshold: float | None, message: str):
        start = time.time()
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO events
                       (server_name, event_type, metric, value, threshold, message)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (server_name, event_type, metric, value, threshold, message)
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to insert event for server %s", server_name)
                return
            finally:
                conn.close()
        elapsed = time.time() - start
        if elapsed > 1.0:
            logger.warning("Slow insert_event for %s: %.2fs", server_name, elapsed)

    def insert_event_correlated(self, server_name: str, event_type: str, metric: str | None,
                                value: float | None, threshold: float | None,
                                message: str, correlation_id: str):
        """Insert an event with a correlation ID linking related events."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO events (server_name, event_type, metric, value, threshold, message, correlation_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (server_name, event_type, metric, value, threshold, message, correlation_id),
                )
                conn.commit()
            finally:
                conn.close()

    def update_event_correlation(self, event_id: int, correlation_id: str):
        """Backfill correlation_id on an existing event."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE events SET correlation_id = ? WHERE id = ?",
                    (correlation_id, event_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_events_in_window(self, minutes: int = 15) -> list[dict]:
        """Get events from the last N minutes for correlation analysis."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, server_name, event_type, metric, value, threshold, message, timestamp "
                "FROM events WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                "ORDER BY timestamp DESC",
                (f"-{minutes} minutes",),
            ).fetchall()
            return [
                {"id": r[0], "server_name": r[1], "event_type": r[2], "metric": r[3],
                 "value": r[4], "threshold": r[5], "message": r[6], "timestamp": r[7]}
                for r in rows
            ]
        finally:
            conn.close()

    # Map localized Windows log level names to English
    _LEVEL_NORMALIZE = {
        "informationen": "Information", "information": "Information",
        "warnung": "Warning", "warning": "Warning",
        "fehler": "Error", "error": "Error",
        "kritisch": "Critical", "critical": "Critical",
    }

    def _normalize_level(self, level: str) -> str:
        return self._LEVEL_NORMALIZE.get(level.lower().strip(), level) if level else "Information"

    @staticmethod
    def _canonical_ts(value) -> str:
        """Normalise a timestamp to the canonical stored form ``%Y-%m-%dT%H:%M:%SZ``.

        Window filters compare timestamps as TEXT, so a row stored in a
        different shape than the cutoff is compared lexicographically against
        it and silently falls on the wrong side. ``'2026-08-05 11:00:00'`` is
        LESS than ``'2026-08-05T10:00:00Z'`` because a space (0x20) sorts below
        'T' (0x54) — so a space-formatted row is invisible to every ``>=``
        window query, whatever its actual instant.

        This was live: ``insert_logs`` stored ``log["time"]`` verbatim, so the
        format was whatever the caller happened to send. The collector sends
        ``…T…Z`` and the DB is uniformly canonical today, but nothing enforced
        it, and a caller using a space or an offset was one import away from
        writing rows that no query could see.

        Accepts the canonical form, a space separator, fractional seconds, and
        an explicit UTC offset (converted). Anything unparseable is returned
        unchanged — storing an odd value beats discarding the row.
        """
        if not value:
            return ""
        s = str(value).strip()
        try:
            iso = s.replace(" ", "T", 1) if ("T" not in s and " " in s) else s
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return s

    # Cumulative counters for what the ingest filter discarded, exposed on
    # /api/system/health. Silent filtering is how a monitoring tool loses trust;
    # a visible, counted filter is a feature.
    logs_dropped_information: int = 0
    logs_kept_by_allowlist: int = 0

    def insert_logs(self, server_name: str, logs_list: list[dict],
                    ingest_cfg: dict | None = None):
        """Bulk insert log dicts (keys: source, time, level, event_id, message).

        Two volume controls, both configurable via ``settings.log_ingest`` and
        both applied here rather than at query time, because the cost being
        managed is storage and write-lock time, not read time:

        1. **Information-level rows are dropped** unless their
           ``Source/EventID`` is on the allow-list. Measured on the live fleet,
           Information was 73.1% of 1,771,744 rows and nothing queried it.
        2. **Every surviving row is also coalesced** into ``log_signatures`` as
           a per-signature-per-hour count. The live table had a 10.8x duplicate
           ratio, and the signature table is what survives the raw table's much
           shorter retention.

        Timestamps are normalised first — see ``_canonical_ts`` for why a
        non-canonical row is invisible to every window query.
        """
        if not logs_list:
            return
        cfg = ingest_cfg or {}
        drop_info = cfg.get("drop_information", True)
        allowlist = set(cfg.get("information_allowlist") or ())
        coalesce = cfg.get("coalesce_signatures", True)

        start = time.time()
        rows = []
        dropped = 0
        kept_by_allow = 0
        for log in logs_list:
            level = self._normalize_level(log.get("level", "Information"))
            source = log.get("source", "Unknown")
            event_id = log.get("event_id")
            if drop_info and level == "Information":
                if f"{source}/{event_id}" in allowlist:
                    kept_by_allow += 1
                else:
                    dropped += 1
                    continue
            rows.append((
                server_name,
                self._canonical_ts(log.get("time", "")),
                source,
                level,
                event_id,
                log.get("message", ""),
            ))

        if dropped:
            Database.logs_dropped_information += dropped
        if kept_by_allow:
            Database.logs_kept_by_allowlist += kept_by_allow

        if not rows:
            logger.debug("insert_logs %s: all %d rows filtered at ingest",
                         server_name, dropped)
            return

        sig_rows = self._build_signature_rows(rows) if coalesce else []

        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO logs (server_name, timestamp, log_source, level, event_id, message)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                if sig_rows:
                    # Upsert: a signature already seen this hour just increments.
                    conn.executemany(
                        """INSERT INTO log_signatures
                             (server_name, log_source, level, event_id, msg_hash,
                              hour_utc, count, first_seen, last_seen, sample)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(server_name, log_source, level, event_id,
                                       msg_hash, hour_utc)
                           DO UPDATE SET
                             count      = count + excluded.count,
                             first_seen = MIN(first_seen, excluded.first_seen),
                             last_seen  = MAX(last_seen,  excluded.last_seen)""",
                        sig_rows,
                    )
                conn.commit()
                logger.debug("insert_logs %s: %d stored, %d dropped, %d signatures",
                             server_name, len(rows), dropped, len(sig_rows))
            except Exception:
                logger.exception("Failed to insert logs for server %s", server_name)
                return
            finally:
                conn.close()
        elapsed = time.time() - start
        if elapsed > 1.0:
            logger.warning("Slow insert_logs for %s: %.2fs", server_name, elapsed)

    @staticmethod
    def _build_signature_rows(rows: list[tuple]) -> list[tuple]:
        """Collapse raw log rows into per-(signature, hour) counts.

        Aggregated in Python BEFORE touching the database, so a batch of 500
        identical rows becomes one upsert rather than 500. That matters because
        the write lock is global — batching here is a write-lock optimisation as
        much as a storage one.

        The signature is hashed rather than stored verbatim so the primary key
        stays a fixed width; the first message seen is kept as ``sample`` so the
        row is still readable.
        """
        import hashlib
        agg: dict[tuple, list] = {}
        for server_name, ts, source, level, event_id, message in rows:
            msg = (message or "")[:200]
            h = hashlib.sha1(msg.encode("utf-8", "replace")).hexdigest()[:16]
            hour = ts[:13] if len(ts) >= 13 else ts     # 'YYYY-MM-DDTHH'
            key = (server_name, source, level, event_id, h, hour)
            slot = agg.get(key)
            if slot is None:
                agg[key] = [1, ts, ts, msg]
            else:
                slot[0] += 1
                if ts < slot[1]:
                    slot[1] = ts
                if ts > slot[2]:
                    slot[2] = ts
        return [(*key, cnt, first, last, sample)
                for key, (cnt, first, last, sample) in agg.items()]

    def get_anomaly_suppression(self, server_name: str, metric: str, direction: str = "above_baseline"):
        """Check if an anomaly is currently suppressed."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT last_alert_time, last_severity, last_value FROM anomaly_suppression "
                "WHERE server_name = ? AND metric = ? AND direction = ?",
                (server_name, metric, direction),
            ).fetchone()
            if row:
                return {"last_alert_time": row[0], "last_severity": row[1], "last_value": row[2]}
            return None
        finally:
            conn.close()

    def upsert_anomaly_suppression(self, server_name: str, metric: str, direction: str,
                                    severity: str, value: float):
        """Insert or update anomaly suppression record."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO anomaly_suppression (server_name, metric, direction, last_alert_time, last_severity, last_value) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_name, metric, direction) DO UPDATE SET "
                    "last_alert_time = excluded.last_alert_time, "
                    "last_severity = excluded.last_severity, "
                    "last_value = excluded.last_value",
                    (server_name, metric, direction, now, severity, value),
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup_anomaly_suppression(self, hours: int = 24):
        """Remove stale suppression records older than N hours."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM anomaly_suppression WHERE last_alert_time < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                    (f"-{hours} hours",),
                )
                conn.commit()
            finally:
                conn.close()

    def add_acknowledgment(self, server_name: str, metric: str, ack_type: str = "acknowledged",
                           snooze_until: str = None, notes: str = None) -> int:
        """Create an anomaly acknowledgment. Returns the new row ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "INSERT INTO anomaly_acknowledgments (server_name, metric, ack_type, snooze_until, notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (server_name, metric, ack_type, snooze_until, notes),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

    def get_active_acknowledgments(self, server_name: str = None, metric: str = None) -> list[dict]:
        """Get active acknowledgments. Filters by server and/or metric if provided."""
        conn = self._get_conn()
        try:
            query = ("SELECT id, server_name, metric, ack_type, created_at, snooze_until, notes "
                     "FROM anomaly_acknowledgments WHERE 1=1")
            params = []
            if server_name:
                query += " AND server_name = ?"
                params.append(server_name)
            if metric:
                query += " AND metric = ?"
                params.append(metric)
            # Exclude expired snoozes
            query += (" AND (ack_type = 'acknowledged' OR (ack_type = 'snoozed' "
                      "AND snooze_until > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))")
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [
                {"id": r[0], "server_name": r[1], "metric": r[2], "ack_type": r[3],
                 "created_at": r[4], "snooze_until": r[5], "notes": r[6]}
                for r in rows
            ]
        finally:
            conn.close()

    def remove_acknowledgment(self, ack_id: int):
        """Delete an acknowledgment by ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM anomaly_acknowledgments WHERE id = ?", (ack_id,))
                conn.commit()
            finally:
                conn.close()

    def analyze(self) -> bool:
        """Refresh SQLite's query-planner statistics (``sqlite_stat1``).

        Without these the planner has no idea of column selectivity and
        full-SCANs ``idx_metrics_server_time`` for per-server aggregates
        instead of using a skip-scan. Measured on the live database, which had
        no ``sqlite_stat1`` at all: a 24-hour fleet ledger went 12.4 ms -> 5.5 ms
        (2.2x) and the plan changed from
        ``SCAN metrics USING INDEX`` to
        ``SEARCH metrics USING INDEX (ANY(server_name) AND timestamp>?)``.
        The gap widens with row count, so it matters more as the fleet grows.

        Called from the retention periodic, right after the row counts have
        changed. Best-effort: stale statistics are a slow query, not an error.
        """
        try:
            with self._write_lock:
                conn = self._get_conn()
                try:
                    conn.execute("ANALYZE")
                    conn.commit()
                finally:
                    conn.close()
            return True
        except Exception:
            logger.warning("ANALYZE failed; query plans may be suboptimal",
                           exc_info=True)
            return False

    def cleanup_expired_snoozes(self):
        """Remove expired snooze acknowledgments."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM anomaly_acknowledgments WHERE ack_type = 'snoozed' "
                    "AND snooze_until <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                )
                conn.commit()
            finally:
                conn.close()

    def get_status_timeline(self, server_name: str, hours: int = 720) -> list[dict]:
        """Get chronological status entries for SLA/uptime computation.

        Returns list of {timestamp, status} ordered ASC for the given time window.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT timestamp, status FROM metrics "
                "WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                "ORDER BY timestamp ASC",
                (server_name, f"-{hours} hours"),
            ).fetchall()
            return [{"timestamp": r[0], "status": r[1]} for r in rows]
        finally:
            conn.close()

    def get_fleet_metrics_window(self, hours: int = 720) -> list[dict]:
        """Every server's readings in one window, in ONE scan.

        Returns {server_name, timestamp, status, cpu_percent, ram_percent,
        disk_c_percent, disk_d_percent} ordered by (server_name, timestamp ASC)
        so a caller can slice it per server without re-sorting.

        This exists because the fleet report needs BOTH the status timeline and
        the metric values for every server. Assembling that from the per-server
        readers costs 29 x get_status_timeline + 29 x get_metric_stats = 58
        queries over the same rows; measured, this one scan returns all 67,786
        rows of a 720-hour window in ~360 ms.

        The superset of columns means one row shape feeds both
        ``compute_uptime_stats(timeline=...)`` and
        ``forecast_metric(history=...)``.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT server_name, timestamp, status,
                          cpu_percent, ram_percent, disk_c_percent, disk_d_percent
                   FROM metrics
                   WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   ORDER BY server_name ASC, timestamp ASC""",
                (f"-{hours} hours",),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_fleet_event_counts(self, hours: int = 720) -> dict[str, dict[str, int]]:
        """Count raised events per server per metric over a window.

        Returns ``{server_name: {metric: count}}`` for warning/critical/anomaly
        events. ``metric`` is whatever the raiser stored — ``cpu``, ``ram``,
        ``baseline_deviation``, ``security_status``, ``failed_logins``, ... —
        and a NULL metric is bucketed under ``"other"``.

        Used to CHARACTERISE degraded time that no static threshold explains.
        It is deliberately not a per-reading join: `events` is state-change
        driven (3,663 rows against 67,786 readings in the same window), so the
        counts describe the window, not individual readings.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT server_name, metric, COUNT(*) AS n
                   FROM events
                   WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                     AND event_type IN ('warning', 'critical', 'anomaly')
                   GROUP BY server_name, metric""",
                (f"-{hours} hours",),
            ).fetchall()
            out: dict[str, dict[str, int]] = {}
            for r in rows:
                out.setdefault(r["server_name"], {})[r["metric"] or "other"] = r["n"]
            return out
        finally:
            conn.close()

    # JSONL mirror for the audit log. Every successful insert is also appended
    # here so an attacker who tampers with prism.db (out-of-band file rewrite)
    # leaves a divergent on-disk trail. The path is configurable so operators
    # can point Windows Event Forwarder / a SIEM agent at it.
    AUDIT_MIRROR_PATH = Path(__file__).parent / "data" / "audit_mirror.jsonl"

    def log_audit(self, username: str, action: str, category: str = "general",
                  details: str | None = None,
                  source_ip: str | None = None,
                  session_id: str | None = None,
                  request_id: str | None = None):
        """Insert a row into the audit_log table.

        Forensic context (`source_ip`, `session_id`, `request_id`) auto-populates
        from the active Flask request when callers don't pass them — so the
        ~50 existing call sites work unchanged but pick up the new fields when
        they fire from inside an HTTP handler. Threads outside Flask (collector,
        scheduler) call this with all three None.

        **F-D-2 (CSV-11 remediation): caller convention for ``details``.**
        The ``details`` field is stored as TEXT with no enforced length
        cap in the schema, but operationally callers should keep the
        payload under 500 characters. Put the SALIENT info first (what,
        which server, which target, the action's discriminator) — any
        longer trailing context (stack trace, dump) may be elided by
        the SIEM ingest layer or wrap awkwardly in the UI.

        Hash chain: each row stores `prev_hash` (the previous row's `row_hash`)
        and `row_hash = sha256(prev_hash || timestamp || username || action ||
        category || details || source_ip || session_id || request_id)`. Tampering
        with any past row's content breaks the chain on the next read by
        `verify_audit_chain()`. The triggers prevent in-process tampering; the
        chain detects out-of-process tampering.

        After a successful insert we also append the row to AUDIT_MIRROR_PATH
        as JSONL. Failure to mirror is logged but does not roll back the DB
        insert — losing a mirror line is much less bad than losing the audit row.

        **F-D-1 (CSV-11 remediation): insert-failure visibility.** A DB
        outage that prevents the audit row from landing is itself a
        regulatory finding (we are required to record the action and
        we couldn't). Failures increment ``_audit_insert_failures`` so
        the watchdog can surface them through ``/api/system/health``,
        and they are also written to the OS-level Python logger at
        WARNING so an external log shipper can detect them.
        """
        # Auto-fill from Flask context if available and the caller didn't supply.
        if source_ip is None or session_id is None or request_id is None:
            try:
                from flask import request as _req, session as _sess, g as _g, has_request_context
                if has_request_context():
                    if source_ip is None:
                        source_ip = _req.remote_addr
                    if session_id is None:
                        # Hash the login_time so a leaked DB doesn't yield session-hijack
                        # material. login_time + username uniquely identifies a session;
                        # the hash gives operators a stable join key without exposing it.
                        lt = _sess.get("login_time") if _sess else None
                        if lt:
                            session_id = hashlib.sha256(
                                f"{username}|{lt}".encode("utf-8")
                            ).hexdigest()[:16]
                    if request_id is None:
                        request_id = getattr(_g, "request_id", None)
            except (ImportError, RuntimeError):
                # No Flask in scope or no active request context — context fields stay None.
                pass

        with self._write_lock:
            conn = self._get_conn()
            try:
                # Read the current chain head BEFORE insert, inside the same lock,
                # so concurrent writes can't race the chain.
                row = conn.execute(
                    "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["row_hash"] if (row and row["row_hash"]) else None

                # Compute row_hash from the canonical content we're about to insert.
                # The DB picks `timestamp` via its DEFAULT clause, so we read it back
                # after insert to compute the final hash. To avoid a double-write,
                # we compute the timestamp here and pass it explicitly.
                from datetime import datetime as _dt, timezone as _tz
                ts = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                content = "|".join([
                    prev_hash or "",
                    ts, username or "", action or "", category or "",
                    details or "", source_ip or "", session_id or "", request_id or "",
                ])
                row_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

                conn.execute(
                    """INSERT INTO audit_log
                       (timestamp, username, action, category, details,
                        source_ip, session_id, request_id, prev_hash, row_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, username, action, category, details,
                     source_ip, session_id, request_id, prev_hash, row_hash),
                )
                conn.commit()
            except Exception:
                # F-D-1: bump the visible counter so /api/system/health
                # can show "audit blind" and an external monitor can
                # alert. We still don't re-raise — losing an audit row
                # is bad but worse is rolling back the operator's
                # action because of a transient DB blip.
                self._audit_insert_failures += 1
                logger.warning(
                    "Failed to insert audit log entry: %s / %s (failures=%d)",
                    action, category, self._audit_insert_failures,
                )
                return
            finally:
                conn.close()

        # Mirror to JSONL outside the DB lock — disk slowness shouldn't block writers.
        try:
            self.AUDIT_MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self.AUDIT_MIRROR_PATH, "a", encoding="utf-8") as f:
                f.write(_json.dumps({
                    "timestamp": ts, "username": username, "action": action,
                    "category": category, "details": details,
                    "source_ip": source_ip, "session_id": session_id,
                    "request_id": request_id, "prev_hash": prev_hash,
                    "row_hash": row_hash,
                }, ensure_ascii=False) + "\n")
        except Exception:
            self._audit_mirror_failures += 1
            logger.warning(
                "Failed to mirror audit log to %s (failures=%d)",
                self.AUDIT_MIRROR_PATH, self._audit_mirror_failures,
            )

    # ── S2-1 (BL3): session containment primitives ────────────────────────
    def is_session_revoked(self, username: str, login_time: str) -> bool:
        """Return True if (username, login_time) was explicitly revoked by an admin."""
        if not username or not login_time:
            return False
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM revoked_sessions WHERE username = ? AND login_time = ? LIMIT 1",
                (username, login_time),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def revoke_session(self, username: str, login_time: str, by: str = "system") -> int:
        """Mark a specific (username, login_time) as revoked. Idempotent."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO revoked_sessions (username, login_time, revoked_by) "
                    "VALUES (?, ?, ?)",
                    (username, login_time, by),
                )
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

    def list_revoked_sessions(self, limit: int = 200) -> list[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, username, login_time, revoked_at, revoked_by "
                "FROM revoked_sessions ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def is_user_disabled(self, username: str) -> bool:
        """Return True if `username` (case-insensitive) is in the disabled_users table."""
        if not username:
            return False
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM disabled_users WHERE username = ? LIMIT 1",
                (username.lower(),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def disable_user(self, username: str, by: str = "system", reason: str = "") -> None:
        if not username:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO disabled_users (username, disabled_by, reason) "
                    "VALUES (?, ?, ?)",
                    (username.lower(), by, reason or None),
                )
                conn.commit()
            finally:
                conn.close()

    def enable_user(self, username: str) -> int:
        if not username:
            return 0
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM disabled_users WHERE username = ?",
                    (username.lower(),),
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    def list_disabled_users(self) -> list[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT username, disabled_at, disabled_by, reason "
                "FROM disabled_users ORDER BY disabled_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── S2-12 (W3): account lockout helpers ───────────────────────────────
    def record_auth_failure(self, username: str, ip: str | None = None) -> None:
        if not username:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO auth_failures (username, source_ip) VALUES (?, ?)",
                    (username.lower(), ip),
                )
                conn.commit()
            finally:
                conn.close()

    def count_recent_failures(self, username: str, since_minutes: int = 30) -> int:
        if not username:
            return 0
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM auth_failures "
                "WHERE username = ? AND attempted_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                (username.lower(), f"-{int(since_minutes)} minutes"),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def clear_failures_for(self, username: str) -> int:
        if not username:
            return 0
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM auth_failures WHERE username = ?",
                    (username.lower(),),
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    def cleanup_auth_failures(self, hours: int = 24) -> int:
        """Prune auth_failure records older than `hours`. Called from cleanup_old_data."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM auth_failures WHERE attempted_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{int(hours)} hours",),
                )
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()

    def verify_audit_chain(self, limit: int | None = None) -> dict:
        """Walk audit_log and verify each row's hash matches sha256(prev_hash || content).

        Returns:
            {"ok": bool, "checked": int, "first_break_id": int | None,
             "first_break_reason": str | None}

        Rows pre-migration (NULL row_hash) are skipped — the chain begins at the
        first row that has row_hash populated. Subsequent NULLs in a chained
        sequence count as a break.
        """
        conn = self._get_conn()
        try:
            sql = ("SELECT id, timestamp, username, action, category, details, "
                   "source_ip, session_id, request_id, prev_hash, row_hash "
                   "FROM audit_log ORDER BY id ASC")
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()

        prev = None
        checked = 0
        chain_started = False
        for r in rows:
            if r["row_hash"] is None:
                if chain_started:
                    return {"ok": False, "checked": checked,
                            "first_break_id": r["id"],
                            "first_break_reason": "NULL row_hash after chain started"}
                continue  # pre-migration row; skip
            chain_started = True
            content = "|".join([
                r["prev_hash"] or "",
                r["timestamp"] or "", r["username"] or "", r["action"] or "",
                r["category"] or "", r["details"] or "", r["source_ip"] or "",
                r["session_id"] or "", r["request_id"] or "",
            ])
            expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if expected != r["row_hash"]:
                return {"ok": False, "checked": checked,
                        "first_break_id": r["id"],
                        "first_break_reason": "row_hash mismatch (content tampered)"}
            if prev is not None and r["prev_hash"] != prev:
                return {"ok": False, "checked": checked,
                        "first_break_id": r["id"],
                        "first_break_reason": "prev_hash mismatch (row inserted/deleted out of band)"}
            prev = r["row_hash"]
            checked += 1
        return {"ok": True, "checked": checked, "first_break_id": None, "first_break_reason": None}

    def get_audit_log(self, limit: int = 100, offset: int = 0, category: str | None = None) -> list[dict]:
        """Return audit log entries ordered by timestamp descending."""
        conn = self._get_conn()
        try:
            sql = "SELECT id, timestamp, username, action, category, details, source_ip, session_id, request_id FROM audit_log"
            params: list = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_audit_csv(self, category: str | None = None) -> str:
        """Export the audit log as a CSV string."""
        conn = self._get_conn()
        try:
            sql = "SELECT id, timestamp, username, action, category, details, source_ip, session_id, request_id FROM audit_log"
            params: list = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY timestamp DESC"
            rows = conn.execute(sql, params).fetchall()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "timestamp", "username", "action", "category", "details"])
            for r in rows:
                writer.writerow([r["id"], r["timestamp"], r["username"], r["action"], r["category"], r["details"]])
            return output.getvalue()
        finally:
            conn.close()

    def delete_server_data(self, server_name: str) -> dict:
        """Wipe ALL historical data for a server from every related table.
        Called when the user removes a server from the config — without this,
        the server's old metrics/events/logs/etc. linger in the DB and the
        dashboard keeps showing it as "offline".

        Returns a dict of {table: rows_deleted} so the caller can show what
        was cleaned up. Note: audit_log entries are NEVER deleted (table is
        append-only via triggers — historical record of who did what stays).
        """
        # Tables with a `server_name` column. Order doesn't matter much
        # since we don't rely on FK cascades — we wipe explicitly.
        tables_with_server_name = [
            "metrics", "events", "logs",
            "anomaly_suppression", "anomaly_acknowledgments",
            "restart_log", "server_tag_assignments", "tls_certificates",
            "health_check_config", "health_check_results", "metric_baselines",
            "failed_logins", "config_snapshots", "config_changes",
            "alert_scores", "runbook_executions", "workflow_execution_steps",
            "server_security_status",
        ]
        deleted = {}
        with self._write_lock:
            conn = self._get_conn()
            try:
                for tbl in tables_with_server_name:
                    try:
                        cur = conn.execute(f"DELETE FROM {tbl} WHERE server_name = ?", (server_name,))
                        if cur.rowcount > 0:
                            deleted[tbl] = cur.rowcount
                    except sqlite3.OperationalError:
                        # Table doesn't exist on older schemas — ignore
                        pass
                # server_dependencies has TWO server columns — wipe both directions
                try:
                    cur = conn.execute(
                        "DELETE FROM server_dependencies WHERE server_name = ? OR depends_on = ?",
                        (server_name, server_name),
                    )
                    if cur.rowcount > 0:
                        deleted["server_dependencies"] = cur.rowcount
                except sqlite3.OperationalError:
                    pass
                conn.commit()
                logger.info("Wiped data for server '%s': %s", server_name, deleted)
            except Exception:
                logger.exception("Failed to wipe data for server '%s'", server_name)
                return deleted
            finally:
                conn.close()
        return deleted

    def _chunked_delete(self, conn, table: str, ts_col: str, days: int,
                        chunk: int = 10000) -> int:
        """Delete rows older than ``days`` in bounded batches.

        A single ``DELETE FROM logs WHERE timestamp < ?`` over tens of millions
        of rows holds the GLOBAL write lock for minutes and builds a WAL the
        size of the deletion — during which every collector write blocks. The
        loop keeps each statement small so the lock is released between
        batches. ``logs`` alone was 1.77M rows at 29 servers and projects to
        tens of millions, so this is the table that needs it.

        Returns the number of rows deleted.
        """
        total = 0
        cutoff = (f"-{days} days",)
        while True:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} "
                f" WHERE {ts_col} < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) "
                f" LIMIT {int(chunk)})",
                cutoff,
            )
            n = cur.rowcount
            conn.commit()
            total += n
            if n < chunk:
                return total

    def cleanup_old_data(self, retention_days: int = 30,
                         per_table: dict | None = None):
        """Apply retention. ``per_table`` overrides ``retention_days`` per table.

        One uniform retention is why `logs` dominated the database: metrics and
        events are small and worth keeping longer, while raw log lines are the
        volume and are only needed for recent drill-down (their history lives on
        in ``log_signatures``). See settings.retention for the defaults.
        """
        pt = per_table or {}
        d_metrics = int(pt.get("metrics_days", retention_days))
        d_events = int(pt.get("events_days", retention_days))
        d_logs = int(pt.get("logs_days", retention_days))
        d_sigs = int(pt.get("log_signatures_days", retention_days))

        start = time.time()
        # logs is deleted OUTSIDE the big lock block, in bounded chunks, because
        # it is by far the largest table and a single statement would stall
        # every writer in the process.
        conn = self._get_conn()
        try:
            with self._write_lock:
                logs_deleted = self._chunked_delete(conn, "logs", "timestamp", d_logs)
                # log_signatures is WITHOUT ROWID (its primary key IS the row),
                # so it cannot be chunked by rowid — and does not need to be:
                # coalescing makes it ~40x smaller than `logs`, so a single
                # statement is short enough not to stall the write lock.
                cur = conn.execute(
                    "DELETE FROM log_signatures "
                    "WHERE last_seen < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{d_sigs} days",),
                )
                sigs_deleted = cur.rowcount
                conn.commit()
        finally:
            conn.close()
        if sigs_deleted:
            logger.info("Retention: pruned %d log signature rows (>%dd)",
                        sigs_deleted, d_sigs)

        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM metrics WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{d_metrics} days",)
                )
                metrics_deleted = cur.rowcount
                cur = conn.execute(
                    "DELETE FROM events WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{d_events} days",)
                )
                events_deleted = cur.rowcount
                # Clean up resolved incidents older than retention period
                conn.execute(
                    "DELETE FROM incident_events WHERE incident_id IN "
                    "(SELECT id FROM incidents WHERE status = 'resolved' AND resolved_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
                    (f"-{retention_days} days",)
                )
                conn.execute(
                    "DELETE FROM incidents WHERE status = 'resolved' AND resolved_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{retention_days} days",)
                )
                # Clean up old config snapshots and changes
                conn.execute(
                    "DELETE FROM config_snapshots WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{retention_days} days",)
                )
                conn.execute(
                    "DELETE FROM config_changes WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{retention_days} days",)
                )
                # Clean up stale alert scores with negligible noise
                conn.execute("DELETE FROM alert_scores WHERE score < 0.1")
                # Clean up old runbook executions
                conn.execute(
                    "DELETE FROM runbook_executions WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{retention_days} days",)
                )
                # Clean up old workflow executions (steps first for FK safety)
                conn.execute(
                    "DELETE FROM workflow_execution_steps WHERE execution_id IN "
                    "(SELECT id FROM workflow_executions WHERE started_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?))",
                    (f"-{retention_days} days",)
                )
                conn.execute(
                    "DELETE FROM workflow_executions WHERE started_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                    (f"-{retention_days} days",)
                )
                # S2-8 / P7 from AUDIT-2026-05: tables that previously had no
                # retention and grew unbounded. health_check_results in
                # particular hits millions of rows per year on a 30-server
                # fleet probing every cycle. Each entry below is the per-row
                # timestamp column for that table — if the column name doesn't
                # match, the OperationalError is caught so retention still
                # makes progress on the other tables.
                _untimely = [
                    ("health_check_results", "last_checked"),
                    ("failed_logins",        "timestamp"),
                    ("restart_log",          "started_at"),
                    ("tls_certificates",     "checked_at"),
                    ("server_security_status", "last_check"),
                ]
                for tbl, ts_col in _untimely:
                    try:
                        conn.execute(
                            f"DELETE FROM {tbl} WHERE {ts_col} < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
                            (f"-{retention_days} days",)
                        )
                    except sqlite3.OperationalError as e:
                        # Column name drift between schema versions — log and continue.
                        logger.debug("Retention skipped for %s.%s: %s", tbl, ts_col, e)
                # S2-12: prune stale auth_failures (24h window — much shorter than
                # general retention because lockout windows are minutes, not days).
                try:
                    conn.execute(
                        "DELETE FROM auth_failures WHERE attempted_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-24 hours')"
                    )
                except sqlite3.OperationalError as e:
                    logger.debug("Retention skipped for auth_failures: %s", e)
                conn.commit()
                logger.info("Cleaned up %d old metrics, %d old events, %d old logs",
                            metrics_deleted, events_deleted, logs_deleted)
            except Exception:
                logger.exception("Failed to cleanup old data")
                return
            finally:
                conn.close()
        elapsed = time.time() - start
        if elapsed > 1.0:
            logger.warning("Slow cleanup_old_data: %.2fs", elapsed)

    # ── Read operations (API threads) ──

    def get_latest_all(self) -> list[dict]:
        """Get the most recent metric reading for every server."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT m.*
                FROM metrics m
                INNER JOIN (
                    SELECT server_name, MAX(id) as max_id
                    FROM metrics
                    GROUP BY server_name
                ) latest ON m.id = latest.max_id
                ORDER BY
                    CASE m.status
                        WHEN 'critical' THEN 0
                        WHEN 'offline' THEN 1
                        WHEN 'warning' THEN 2
                        WHEN 'unknown' THEN 3
                        ELSE 4
                    END,
                    m.server_name
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_by_server(self, server_name: str) -> dict | None:
        """Get the most recent metric reading for a specific server."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM metrics WHERE server_name = ? ORDER BY timestamp DESC LIMIT 1",
                (server_name,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_server_history(self, server_name: str, hours: int = 24) -> list[dict]:
        """Get metric history for a server within a time window."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT timestamp, cpu_percent, ram_percent, disk_c_percent, disk_d_percent, status
                   FROM metrics
                   WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   ORDER BY timestamp ASC""",
                (server_name, f"-{hours} hours")
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_server_history_bucketed(self, server_name: str, hours: int = 24,
                                    buckets: int = 240) -> list[dict]:
        """Time-bucketed metric history — bounded output regardless of window.

        ``get_server_history`` returns every row, which is fine for one server
        but is what forced the 6-server cap on the comparison endpoints: at a
        720-hour window that is thousands of rows PER server, and the payload
        grows linearly with the number of servers compared.

        Bucketing makes the response size a function of ``buckets``, not of the
        window or the row count, so comparing the whole fleet costs about the
        same as comparing two servers. Each bucket reports the MEAN of its
        samples — the right choice for a trend line — while
        ``compare_server_stats`` deliberately keeps using the FULL series,
        because p95 and stddev computed over pre-averaged buckets would be
        wrong (averaging destroys exactly the spread those statistics measure).

        Buckets are indexed from now backwards, then reversed, so the result is
        oldest -> newest like ``get_server_history``. Empty buckets are omitted
        rather than emitted as nulls, so a gap stays a gap.
        """
        if buckets < 1:
            buckets = 1
        bucket_h = (hours * 1.0) / buckets
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT CAST(((julianday('now') - julianday(timestamp)) * 24.0) / ?
                            AS INTEGER) AS bucket,
                          MAX(timestamp) AS timestamp,
                          AVG(NULLIF(cpu_percent, -1))    AS cpu_percent,
                          AVG(NULLIF(ram_percent, -1))    AS ram_percent,
                          AVG(NULLIF(disk_c_percent, -1)) AS disk_c_percent,
                          AVG(NULLIF(disk_d_percent, -1)) AS disk_d_percent,
                          COUNT(*) AS samples
                   FROM metrics
                   WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   GROUP BY bucket
                   ORDER BY bucket DESC""",
                (bucket_h, server_name, f"-{hours} hours"),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d.pop("bucket", None)
                # NULLIF above turns the -1 "metric absent" sentinel into NULL so
                # it cannot drag an average down; re-emit it as -1 for the
                # frontend, which already understands that convention.
                for k in ("cpu_percent", "ram_percent", "disk_c_percent", "disk_d_percent"):
                    d[k] = -1.0 if d[k] is None else round(d[k], 1)
                out.append(d)
            return out
        finally:
            conn.close()

    def get_fleet_sparklines(self, hours: int = 24, buckets: int = 24) -> dict:
        """Downsampled per-server 'worst resource %' series for card sparklines.

        ONE grouped query for the WHOLE fleet (not N per-server queries).
        Returns ``{server_name: [v0..v_{buckets-1}]}`` oldest->newest, where each
        value is the MAX over that time-bucket of the worst of
        cpu/ram/disk_c/disk_d (0-100), or None for an empty bucket. Servers with
        no data in the window are simply absent.
        """
        if buckets < 1:
            buckets = 1
        bucket_h = (hours * 1.0) / buckets
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT server_name,
                          CAST(((julianday('now') - julianday(timestamp)) * 24.0) / ? AS INTEGER) AS bucket,
                          MAX(MAX(COALESCE(cpu_percent,0), COALESCE(ram_percent,0),
                                  COALESCE(disk_c_percent,0), COALESCE(disk_d_percent,0))) AS worst
                   FROM metrics
                   WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   GROUP BY server_name, bucket""",
                (bucket_h, f"-{hours} hours"),
            ).fetchall()
        finally:
            conn.close()
        series: dict = {}
        for r in rows:
            d = dict(r)
            b = d.get("bucket")
            if b is None or b < 0 or b >= buckets:
                continue
            arr = series.setdefault(d["server_name"], [None] * buckets)
            arr[buckets - 1 - b] = round(d["worst"], 1) if d["worst"] is not None else None
        return series

    def get_status_summary(self) -> dict:
        """Get counts of servers by status."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT m.status, COUNT(*) as count
                FROM metrics m
                INNER JOIN (
                    SELECT server_name, MAX(id) as max_id
                    FROM metrics
                    GROUP BY server_name
                ) latest ON m.id = latest.max_id
                GROUP BY m.status
            """).fetchall()
            summary = {"total": 0, "healthy": 0, "warning": 0, "critical": 0, "offline": 0}
            for row in rows:
                status = row["status"]
                count = row["count"]
                if status in summary:
                    summary[status] = count
                summary["total"] += count
            return summary
        finally:
            conn.close()

    def get_recent_events(self, limit: int = 20) -> list[dict]:
        """Get the most recent events across all servers."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_consolidated_activity(self, limit: int = 20) -> list[dict]:
        """Get consolidated activity feed: groups consecutive same-type events per server.

        Returns list of dicts with:
          server_name, event_type, metric, message, first_seen, last_seen, count, resolved, resolved_at
        """
        conn = self._get_conn()
        try:
            # Get recent events (more than limit to allow grouping)
            rows = conn.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT 200"
            ).fetchall()
            events = [dict(r) for r in rows]

            if not events:
                return []

            # Group consecutive same-type events per server
            consolidated = []
            seen = {}  # key -> index in consolidated

            for evt in events:
                server = evt["server_name"]
                etype = evt["event_type"]
                msg = evt["message"] or ""
                ts = evt["timestamp"]

                # Key: server + event_type + metric (group same kind of issue)
                key = f"{server}|{etype}|{evt.get('metric', '')}"

                if key in seen:
                    entry = consolidated[seen[key]]
                    entry["count"] += 1
                    # first_seen is the oldest (events are DESC, so later ones are older)
                    entry["first_seen"] = ts
                else:
                    seen[key] = len(consolidated)
                    consolidated.append({
                        "server_name": server,
                        "event_type": etype,
                        "metric": evt.get("metric"),
                        "value": evt.get("value"),
                        "threshold": evt.get("threshold"),
                        "message": msg,
                        "first_seen": ts,
                        "last_seen": ts,
                        "count": 1,
                        "resolved": False,
                        "resolved_at": None,
                    })

            # Check if issues are now resolved by looking at current server status
            latest = {r["server_name"]: r for r in self._get_latest_status(conn)}

            for entry in consolidated:
                server = entry["server_name"]
                current = latest.get(server)
                if entry["event_type"] in ("critical", "warning", "offline"):
                    if current and current["status"] == "healthy":
                        entry["resolved"] = True
                        # Find the resolved event timestamp
                        resolved_evt = self._find_resolved_event(
                            conn, server, entry["last_seen"]
                        )
                        entry["resolved_at"] = resolved_evt
                elif entry["event_type"] == "resolved":
                    entry["resolved"] = True
                    entry["resolved_at"] = entry["last_seen"]

            return consolidated[:limit]
        finally:
            conn.close()

    def _get_latest_status(self, conn) -> list:
        rows = conn.execute("""
            SELECT m.server_name, m.status
            FROM metrics m
            INNER JOIN (
                SELECT server_name, MAX(id) as max_id
                FROM metrics GROUP BY server_name
            ) latest ON m.id = latest.max_id
        """).fetchall()
        return [dict(r) for r in rows]

    def _find_resolved_event(self, conn, server_name: str, after_ts: str) -> str | None:
        row = conn.execute(
            """SELECT timestamp FROM events
               WHERE server_name = ? AND event_type = 'resolved' AND timestamp >= ?
               ORDER BY timestamp ASC LIMIT 1""",
            (server_name, after_ts)
        ).fetchone()
        return row["timestamp"] if row else None

    def get_metric_stats(self, server_name: str, hours: int = 168) -> list[dict]:
        """Get raw metric arrays for analytics (anomaly detection / forecasting).

        Returns list of dicts with timestamp, cpu_percent, ram_percent,
        disk_c_percent, disk_d_percent -- only the columns needed.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT timestamp, cpu_percent, ram_percent, disk_c_percent, disk_d_percent
                   FROM metrics
                   WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   ORDER BY timestamp ASC""",
                (server_name, f"-{hours} hours")
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_server_events(self, server_name: str, limit: int = 50) -> list[dict]:
        """Get recent events for a specific server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE server_name = ? ORDER BY timestamp DESC LIMIT ?",
                (server_name, limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_server_logs(self, server_name: str, hours: int = 24,
                        source: str | None = None, level: str | None = None,
                        limit: int = 100) -> list[dict]:
        """Get Windows event logs for a server with optional filters."""
        conn = self._get_conn()
        try:
            sql = "SELECT * FROM logs WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)"
            params: list = [server_name, f"-{hours} hours"]
            if source:
                sql += " AND log_source = ?"
                params.append(source)
            if level:
                sql += " AND level = ?"
                params.append(level)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_log_search(self, query: str, server_name: str | None = None,
                       hours: int = 24, limit: int = 50) -> list[dict]:
        """Search log messages across servers."""
        conn = self._get_conn()
        try:
            sql = "SELECT * FROM logs WHERE message LIKE ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)"
            params: list = [f"%{query}%", f"-{hours} hours"]
            if server_name:
                sql += " AND server_name = ?"
                params.append(server_name)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_db_stats(self) -> dict:
        """Return database statistics: file size, table counts, oldest/newest records."""
        try:
            size_bytes = self.db_path.stat().st_size
        except OSError:
            size_bytes = 0

        table_counts = {"metrics": 0, "events": 0, "audit_log": 0}
        oldest_record = None
        newest_record = None

        try:
            conn = self._get_conn()
            try:
                for table in table_counts:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    table_counts[table] = row[0] if row else 0

                row = conn.execute("SELECT MIN(timestamp) FROM metrics").fetchone()
                if row and row[0]:
                    oldest_record = row[0]

                row = conn.execute("SELECT MAX(timestamp) FROM metrics").fetchone()
                if row and row[0]:
                    newest_record = row[0]
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to get DB stats")

        return {
            "size_bytes": size_bytes,
            "table_counts": table_counts,
            "oldest_record": oldest_record,
            "newest_record": newest_record,
        }

    # ── Security status operations ──

    def upsert_security_status(self, server_name: str, **fields):
        """Insert or replace security status snapshot for a server."""
        allowed = {
            "defender_enabled", "defender_rt_protection", "defender_sig_age_days",
            "defender_engine_version", "firewall_service_running",
            "firewall_domain_enabled", "firewall_private_enabled",
            "firewall_public_enabled", "bitlocker_encrypted_pct", "bitlocker_status",
            "open_ports_json", "local_users_json", "raw_data",
        }
        cols = ["server_name", "last_checked"]
        vals = [server_name, datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')]
        for k, v in fields.items():
            if k in allowed:
                cols.append(k)
                vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO server_security_status ({col_list}) VALUES ({placeholders})",
                    vals
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to upsert security status for %s", server_name)
            finally:
                conn.close()

    def get_security_status(self, server_name: str) -> dict | None:
        """Return security status dict for a server, or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM server_security_status WHERE server_name = ?",
                (server_name,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_security_status(self) -> list[dict]:
        """Return all security status snapshots."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM server_security_status ORDER BY server_name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────────────
    # Cleanup table lists — single source of truth to prevent drift between
    # clean_all_data() and delete_all_data_and_servers(). When you add a new
    # table to the schema, add it to one of these lists.
    #
    # _CLEAN_TABLES   = telemetry / noise — wiped by "Clean Data" button.
    #                   Keeps user-authored content (runbooks, workflows,
    #                   health checks, dependencies, tags, baselines).
    # _NUKE_TABLES    = everything in _CLEAN_TABLES PLUS user content —
    #                   wiped by "Delete All Data" button.
    # ─────────────────────────────────────────────────────────────────────
    _CLEAN_TABLES = [
        "metrics", "events", "logs",
        "anomaly_suppression", "anomaly_acknowledgments",
        "tls_certificates",
        "health_check_results",
        "failed_logins",
        "config_snapshots", "config_changes",
        "incident_events", "incidents",
        "alert_scores",
        "runbook_executions",
        "restart_log",
        "server_security_status",
        "workflow_execution_steps", "workflow_executions",
    ]
    _NUKE_EXTRA_TABLES = [
        "server_tag_assignments", "server_tags",
        "health_check_config",
        "metric_baselines",
        "server_dependencies",
    ]

    def clean_all_data(self) -> dict:
        """Wipe telemetry & noise data. KEEPS user-authored content + config + audit.

        What is wiped: metrics, events, logs, anomaly state, TLS health,
        health check results (not config), failed logins, config snapshots,
        incidents, alert scores, runbook/workflow execution history,
        restart log, security status snapshots.

        What is KEPT: audit_log, runbooks, workflows, workflow_categories,
        health_check_config, metric_baselines, server_tags, server_dependencies,
        config.json (servers + settings).
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                counts = {}
                for table in self._CLEAN_TABLES:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                    conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()
        return counts

    def delete_all_data_and_servers(self) -> dict:
        """Wipe ALL data including user-authored content. Server removal
        handled externally via config_manager (api.py /data/delete endpoint).

        What is wiped: everything in _CLEAN_TABLES + _NUKE_EXTRA_TABLES +
        non-builtin runbooks + non-template workflows + workflow categories.

        What is KEPT: audit_log, builtin runbooks, template workflows.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                counts = {}
                for table in self._CLEAN_TABLES + self._NUKE_EXTRA_TABLES:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                    conn.execute(f"DELETE FROM {table}")
                # Runbooks: only delete non-builtin
                cur = conn.execute("SELECT COUNT(*) FROM runbooks WHERE is_builtin = 0")
                counts["runbooks"] = cur.fetchone()[0]
                conn.execute("DELETE FROM runbooks WHERE is_builtin = 0")
                # Workflows: only delete non-template
                cur = conn.execute("SELECT COUNT(*) FROM workflows WHERE is_template = 0")
                counts["workflows"] = cur.fetchone()[0]
                conn.execute("DELETE FROM workflows WHERE is_template = 0")
                cur = conn.execute("SELECT COUNT(*) FROM workflow_categories")
                counts["workflow_categories"] = cur.fetchone()[0]
                conn.execute("DELETE FROM workflow_categories")
                conn.commit()
            finally:
                conn.close()
        return counts

    def factory_reset(self) -> dict:
        """Delete ALL rows from ALL tables including audit_log."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                counts = {}
                for table in ["metrics", "events", "logs", "anomaly_suppression",
                              "anomaly_acknowledgments", "audit_log",
                              "server_tag_assignments", "server_tags",
                              "tls_certificates", "health_check_results",
                              "health_check_config", "metric_baselines", "failed_logins",
                              "config_snapshots", "config_changes",
                              "incident_events", "incidents", "alert_scores",
                              "server_dependencies", "runbook_executions",
                              "runbooks", "restart_log", "server_security_status",
                              "workflow_execution_steps", "workflow_executions",
                              "workflows", "workflow_categories"]:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                    conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()
        return counts

    # ── Dependency map operations ──

    def add_dependency(self, server_name: str, depends_on: str,
                       dependency_type: str = "service", port: int | None = None,
                       description: str | None = None, custom_type_name: str | None = None,
                       target_mode: str = "port", service_name: str | None = None,
                       process_name: str | None = None) -> int:
        """Add a dependency relationship. Returns the new row id."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "INSERT INTO server_dependencies "
                    "(server_name, depends_on, dependency_type, custom_type_name, target_mode, port, service_name, process_name, description) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (server_name, depends_on, dependency_type, custom_type_name, target_mode, port, service_name, process_name, description)
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def remove_dependency(self, dep_id: int):
        """Remove a dependency by id."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM server_dependencies WHERE id = ?", (dep_id,))
                conn.commit()
            finally:
                conn.close()

    def get_all_dependencies(self) -> list[dict]:
        """Return all dependency records."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, server_name, depends_on, dependency_type, custom_type_name, target_mode, port, service_name, process_name, description "
                "FROM server_dependencies ORDER BY server_name, depends_on"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_dependencies_for_server(self, server_name: str) -> list[dict]:
        """What this server depends ON."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, server_name, depends_on, dependency_type, custom_type_name, target_mode, port, service_name, process_name, description "
                "FROM server_dependencies WHERE server_name = ? ORDER BY depends_on",
                (server_name,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_dependents(self, server_name: str) -> list[dict]:
        """What depends on THIS server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, server_name, depends_on, dependency_type, custom_type_name, target_mode, port, service_name, process_name, description "
                "FROM server_dependencies WHERE depends_on = ? ORDER BY server_name",
                (server_name,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Restart log operations ──

    def insert_restart_log(self, run_id: str, server_name: str, action: str,
                           status: str, details: str = "", updates_installed: int = 0,
                           actor: str = "system"):
        """Insert a restart log entry.

        F-A-1 (CSV-11 remediation): ``actor`` is the operator-attributable
        identity (username for UI-driven, ``"system"`` for periodics-
        driven). Defaults to ``"system"`` so legacy callers that don't
        pass it keep working.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO restart_log (run_id, server_name, action, status, details, "
                    "updates_installed, actor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, server_name, action, status, details, updates_installed, actor),
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to insert restart log for %s", server_name)
            finally:
                conn.close()

    def get_restart_log(self, limit: int = 50, run_id: str | None = None) -> list[dict]:
        """Get recent restart log entries, optionally filtered by run_id."""
        conn = self._get_conn()
        try:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM restart_log WHERE run_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM restart_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_restart_run(self) -> list[dict]:
        """Get the most recent run_id and all its entries."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT run_id FROM restart_log ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not row:
                return []
            run_id = row["run_id"]
            rows = conn.execute(
                "SELECT * FROM restart_log WHERE run_id = ? ORDER BY timestamp ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Compliance / SOP execution log ──────────────────────────────────
    def insert_sop_execution(self, sop_id: str, executed_by: str,
                             result: str = "pass", notes: str | None = None,
                             evidence_ref: str | None = None) -> int:
        """Record one execution of a documented SOP.

        Writes one row to ``sop_log`` AND one row to ``audit_log`` so the
        compliance trail is captured in both the dedicated table and the
        regulatory append-only log. Returns the new sop_log row id.

        Args:
            sop_id: canonical SOP identifier ("SOP-01", "SOP-05", etc.)
            executed_by: username of the operator performing the SOP
            result: 'pass' | 'fail' | 'partial'
            notes: free-form description, ≤ 2000 chars by convention
            evidence_ref: optional pointer (filename, ticket id, URL)
        """
        if result not in ("pass", "fail", "partial"):
            raise ValueError(f"invalid result: {result!r}")
        if notes and len(notes) > 2000:
            notes = notes[:1997] + "..."
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO sop_log
                       (sop_id, executed_by, result, notes, evidence_ref)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sop_id, executed_by, result, notes, evidence_ref),
                )
                new_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
        # Audit row outside the write lock (log_audit takes its own).
        try:
            self.log_audit(
                username=executed_by,
                action="sop_execution_recorded",
                category="compliance",
                details=f"sop={sop_id}, result={result}, row_id={new_id}",
            )
        except Exception:
            logger.warning(
                "sop_log insert succeeded but audit_log failed for %s by %s",
                sop_id, executed_by,
            )
        return new_id

    def get_latest_sop_execution(self, sop_id: str) -> dict | None:
        """Return the most recent execution for one SOP, or None if never run.

        Ordering uses ``id DESC`` (auto-increment, monotonic) rather than
        ``executed_at DESC``. The timestamp column has only second
        resolution, so two inserts within the same second tie and sqlite
        returns either arbitrarily — that produced an intermittent test
        flake (post-PhD-audit observation). ``id`` is strictly monotonic
        per the INTEGER PRIMARY KEY AUTOINCREMENT so it always gives the
        last-inserted row.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sop_log WHERE sop_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (sop_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_sop_execution_history(self, sop_id: str, limit: int = 50) -> list[dict]:
        """Return execution history for one SOP, most recent first.

        Ordered by ``id DESC`` for the same stability reason as
        ``get_latest_sop_execution``."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM sop_log WHERE sop_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (sop_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_all_latest_sop_executions(self) -> dict[str, dict]:
        """Return a dict ``{sop_id: latest_row}`` covering every SOP that
        has ever been executed. SOPs that have never run are absent
        from the dict — the caller treats absence as "never executed"."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                # Window function would be cleaner but we keep this portable
                # to older SQLite by using a correlated subquery.
                """SELECT s.* FROM sop_log s
                   WHERE s.id = (SELECT MAX(id) FROM sop_log
                                 WHERE sop_id = s.sop_id)"""
            ).fetchall()
            return {r["sop_id"]: dict(r) for r in rows}
        finally:
            conn.close()

    # ── End compliance ──────────────────────────────────────────────────

    def get_server_restart_events(self, server_name: str, hours: int = 24) -> list[dict]:
        """Get restart log entries for a server within a time window.

        Returns list of dicts with timestamp, action, status, details,
        updates_installed for chart annotation overlays.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT timestamp, action, status, details, updates_installed
                   FROM restart_log
                   WHERE server_name = ? AND timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                   ORDER BY timestamp ASC""",
                (server_name, f"-{hours} hours")
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Server Tag operations (F1) ──

    def create_tag(self, name: str, color: str = '#6B7280') -> int:
        """Create a new tag. Returns the new tag ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "INSERT INTO server_tags (name, color) VALUES (?, ?)",
                    (name, color),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

    def get_all_tags(self) -> list[dict]:
        """Get all tags with server_count via LEFT JOIN to assignments."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT t.id, t.name, t.color, t.created_at,
                          COUNT(a.server_name) AS server_count
                   FROM server_tags t
                   LEFT JOIN server_tag_assignments a ON t.id = a.tag_id
                   GROUP BY t.id
                   ORDER BY t.name"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_tag(self, tag_id: int, name: str | None = None, color: str | None = None):
        """Update an existing tag's name and/or color."""
        fields = []
        params = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if color is not None:
            fields.append("color = ?")
            params.append(color)
        if not fields:
            return
        params.append(tag_id)
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE server_tags SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()

    def delete_tag(self, tag_id: int):
        """Delete a tag and its assignments (CASCADE)."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("DELETE FROM server_tags WHERE id = ?", (tag_id,))
                conn.commit()
            finally:
                conn.close()

    def assign_tag(self, server_name: str, tag_id: int):
        """Assign a tag to a server. Ignores if already assigned."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO server_tag_assignments (server_name, tag_id) VALUES (?, ?)",
                    (server_name, tag_id),
                )
                conn.commit()
            finally:
                conn.close()

    def remove_tag_assignment(self, server_name: str, tag_id: int):
        """Remove a tag assignment from a server."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM server_tag_assignments WHERE server_name = ? AND tag_id = ?",
                    (server_name, tag_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_tags_for_server(self, server_name: str) -> list[dict]:
        """Get all tags assigned to a specific server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT t.id, t.name, t.color
                   FROM server_tags t
                   INNER JOIN server_tag_assignments a ON t.id = a.tag_id
                   WHERE a.server_name = ?
                   ORDER BY t.name""",
                (server_name,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_servers_by_tag(self, tag_id: int) -> list[str]:
        """Get all server names that have a specific tag."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT server_name FROM server_tag_assignments WHERE tag_id = ? ORDER BY server_name",
                (tag_id,),
            ).fetchall()
            return [r["server_name"] for r in rows]
        finally:
            conn.close()

    def get_all_tag_assignments(self) -> dict[str, list[dict]]:
        """Bulk fetch all tag assignments: {server_name: [{id, name, color}]}."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT a.server_name, t.id, t.name, t.color
                   FROM server_tag_assignments a
                   INNER JOIN server_tags t ON t.id = a.tag_id
                   ORDER BY a.server_name, t.name"""
            ).fetchall()
            result: dict[str, list[dict]] = {}
            for r in rows:
                server = r["server_name"]
                if server not in result:
                    result[server] = []
                result[server].append({"id": r["id"], "name": r["name"], "color": r["color"]})
            return result
        finally:
            conn.close()

    # ── TLS Certificate operations (F2) ──

    def upsert_tls_certificate(self, server_name: str, host: str, port: int,
                                subject: str | None, issuer: str | None,
                                not_before: str | None, not_after: str | None,
                                days_remaining: int | None, status: str,
                                error: str | None = None):
        """Insert or update a TLS certificate record."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO tls_certificates
                       (server_name, host, port, subject, issuer, not_before, not_after,
                        days_remaining, last_checked, status, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(server_name, host, port) DO UPDATE SET
                        subject = excluded.subject,
                        issuer = excluded.issuer,
                        not_before = excluded.not_before,
                        not_after = excluded.not_after,
                        days_remaining = excluded.days_remaining,
                        last_checked = excluded.last_checked,
                        status = excluded.status,
                        error = excluded.error""",
                    (server_name, host, port, subject, issuer, not_before, not_after,
                     days_remaining, now, status, error),
                )
                conn.commit()
            finally:
                conn.close()

    def get_all_tls_certificates(self) -> list[dict]:
        """Get all TLS certificates ordered by days_remaining ASC."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM tls_certificates ORDER BY days_remaining ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_tls_certificates_for_server(self, server_name: str) -> list[dict]:
        """Get TLS certificates for a specific server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM tls_certificates WHERE server_name = ? ORDER BY days_remaining ASC",
                (server_name,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_expiring_certificates(self, threshold_days: int = 30) -> list[dict]:
        """Get certificates expiring within threshold_days."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM tls_certificates WHERE days_remaining <= ? ORDER BY days_remaining ASC",
                (threshold_days,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete_tls_certificate(self, cert_id: int):
        """Delete a TLS certificate by ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM tls_certificates WHERE id = ?", (cert_id,))
                conn.commit()
            finally:
                conn.close()

    # ── Health Check operations (F3) ──

    def save_health_check_config(self, server_name: str, check_type: str,
                                  target_host: str, target_port: int,
                                  http_path: str = '/', expected_status: int = 200,
                                  name: str = '', verify_tls: bool = True) -> int:
        """Save a health check configuration. Returns the config ID.

        `verify_tls` is carried on BOTH the insert and the ON CONFLICT update.
        The update is the path an operator actually exercises — create a
        check, watch it fail against a self-signed certificate, edit it — so a
        setting honoured only on first insert would strand them with no way to
        turn verification off, and no way to turn it back on afterwards.

        THE UPDATE COALESCES rather than assigning. A caller that supplies only
        the key fields plus the one thing it wants to change used to blank the
        rest: `http_path` and `expected_status` arrive as None from the route
        when absent, and a bare `= excluded.http_path` wrote that None over a
        configured `/healthz` and a configured 204. Editing one field is the
        normal shape of an edit, so the destructive version was waiting for the
        first person to do the obvious thing.

        `verify_tls` is deliberately NOT coalesced: it is a real boolean whose
        False is meaningful, the route resolves absent-to-True before we see
        it, and coalescing would make "turn verification off" unexpressible.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    """INSERT INTO health_check_config
                       (server_name, check_type, target_host, target_port, http_path, expected_status, name, verify_tls)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(server_name, check_type, target_host, target_port) DO UPDATE SET
                        http_path = COALESCE(excluded.http_path, health_check_config.http_path),
                        expected_status = COALESCE(excluded.expected_status, health_check_config.expected_status),
                        name = COALESCE(excluded.name, health_check_config.name),
                        verify_tls = excluded.verify_tls""",
                    (server_name, check_type, target_host, target_port, http_path,
                     expected_status, name, 1 if verify_tls else 0),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

    def delete_health_check_config(self, config_id: int):
        """Delete a health check configuration by ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM health_check_config WHERE id = ?", (config_id,))
                conn.commit()
            finally:
                conn.close()

    def get_health_check_config(self, server_name: str | None = None) -> list[dict]:
        """Get health check configurations, optionally filtered by server."""
        conn = self._get_conn()
        try:
            if server_name:
                rows = conn.execute(
                    "SELECT * FROM health_check_config WHERE server_name = ? ORDER BY id",
                    (server_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM health_check_config ORDER BY server_name, id"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def upsert_health_check_result(self, server_name: str, check_type: str,
                                    target_host: str, target_port: int,
                                    status: str, response_time_ms: float | None = None,
                                    error: str | None = None):
        """Insert a health check result."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO health_check_results
                       (server_name, check_type, target_host, target_port, status, response_time_ms, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (server_name, check_type, target_host, target_port, status, response_time_ms, error),
                )
                conn.commit()
            finally:
                conn.close()

    def get_health_check_summary(self) -> dict:
        """Counts of ENABLED health-check probes, by their most recent result.

        Shape deliberately mirrors :meth:`get_status_summary` so the
        dashboard's services card reads the same way as its servers card:
        ``{"total", "up", "down", "unknown"}``, ``total`` being the sum.

        Three things this does that the obvious one-liner gets wrong, each
        worth stating because each produces a plausible number:

          * It counts CONFIGURED PROBES, not result rows.
            ``health_check_results`` is append-only history — one probe on
            the 5-minute periodics cadence contributes ~288 rows a day — so
            grouping statuses there answers "how often has a probe run",
            which looks like a fleet size and is not one.
          * It excludes ``enabled = 0``. A probe the operator switched off
            is not down, and counting it as down makes the card demand
            attention for something nobody is watching.
          * A probe with no result yet is ``unknown``, not ``up``. Hence the
            LEFT JOIN: after a restart, or between adding a probe and the
            next periodics tick, there is genuinely no answer, and the
            honest report of that is its own bucket rather than a default
            that flatters.

        The probe's identity is the ``health_check_config`` UNIQUE tuple
        (server_name, check_type, target_host, target_port) — the same
        identity ``healthchecks.py`` uses to find a probe's previous status
        for transition detection. Anything narrower (server_name alone)
        would collapse two probes on one host into one row.

        WHY IT IS SHAPED LIKE THIS, because the obvious form is 2,000x
        slower and this one runs every five seconds.

        The readable version groups ``health_check_results`` by the probe key
        to find each ``MAX(id)`` and joins that back. It is correct, and it
        touches every row in an APPEND-ONLY HISTORY TABLE to answer a
        question about a dozen probes. Measured, 12 probes, one row per probe
        per five minutes:

            1 day      3,456 rows      1.42 ms
            1 week    27,648 rows     12.75 ms
            1 month  131,328 rows     82.64 ms     <- the retention default
            3 months 442,368 rows    382.96 ms

        Retention prunes the table at ``retention_days`` (default 30), so the
        third row is the realistic ceiling and the fourth is what an operator
        who raises retention gets. 82 ms on the dashboard's refresh path is
        worse than the 31.77 ms analytics call that Wave 3 moved off
        ``/server/<name>`` for being that page's entire server-side cost.

        Driving from the CONFIG side instead — a dozen rows, each doing one
        indexed seek to the newest result for that probe — is 0.033 ms at the
        same 30-day size, a 2,000x reduction with byte-identical output. The
        plan is ``SCAN c`` plus a ``SEARCH r USING INDEX
        idx_hc_results_probe``, against a ``SCAN health_check_results``
        before. That index exists for this query; see SCHEMA_SQL.

        The bucketing stays in Python rather than becoming a ``GROUP BY``:
        the result set is one row per configured probe, so there is nothing
        to aggregate in SQL that is cheaper to aggregate here, and the
        NULL-folding rule is easier to read as code than as a CASE.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT (
                    SELECT r.status
                    FROM health_check_results r
                    WHERE r.server_name = c.server_name
                      AND r.check_type  = c.check_type
                      AND r.target_host = c.target_host
                      AND r.target_port = c.target_port
                    ORDER BY r.id DESC
                    LIMIT 1
                ) AS status
                FROM health_check_config c
                WHERE c.enabled = 1
            """).fetchall()
            summary = {"total": 0, "up": 0, "down": 0, "unknown": 0}
            for row in rows:
                status = row["status"]
                # NULL (never probed) and any status the probes do not emit
                # both land in `unknown` rather than being dropped, so
                # `total` always equals up + down + unknown.
                summary["up" if status == "up" else
                        "down" if status == "down" else "unknown"] += 1
                summary["total"] += 1
            return summary
        finally:
            conn.close()

    def get_health_check_results(self, server_name: str | None = None) -> list[dict]:
        """Get health check results, optionally filtered by server."""
        conn = self._get_conn()
        try:
            if server_name:
                rows = conn.execute(
                    "SELECT * FROM health_check_results WHERE server_name = ? ORDER BY last_checked DESC",
                    (server_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM health_check_results ORDER BY last_checked DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── F4: Baseline methods ──────────────────────────────────────

    def get_metric_history_raw(self, server_name: str, hours: int = 672) -> list[dict]:
        """Get raw metric rows for baseline computation."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT timestamp, cpu_percent, ram_percent, disk_c_percent, disk_d_percent
                   FROM metrics WHERE server_name = ?
                   AND timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' hours')
                   ORDER BY timestamp ASC""",
                (server_name, f"-{hours}"),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def upsert_baseline(self, server_name: str, metric: str, hour_of_week: int,
                        avg_value: float, stddev: float, sample_count: int):
        """Insert or update a baseline slot."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO metric_baselines (server_name, metric, hour_of_week, avg_value, stddev, sample_count, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                       ON CONFLICT(server_name, metric, hour_of_week) DO UPDATE SET
                       avg_value=excluded.avg_value, stddev=excluded.stddev,
                       sample_count=excluded.sample_count, updated_at=excluded.updated_at""",
                    (server_name, metric, hour_of_week, avg_value, stddev, sample_count),
                )
                conn.commit()
            finally:
                conn.close()

    def get_baseline(self, server_name: str, metric: str, hour_of_week: int):
        """Get a single baseline slot."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM metric_baselines WHERE server_name=? AND metric=? AND hour_of_week=?",
                (server_name, metric, hour_of_week),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_baseline_coverage(self, server_name: str, min_samples: int = 10) -> dict:
        """P14 from AUDIT-2026-05: report what fraction of (metric, hour-of-week)
        baseline slots have ≥ min_samples observations. Used by the dashboard
        to surface 'baseline coverage' per server so operators can see which
        servers' smart detector is fully armed and which are still warming up.

        Returns:
            {
              "metrics": {metric_name: {"covered": int, "total": 168}, ...},
              "overall_pct": float,
              "min_samples": int,
            }
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT metric, hour_of_week, sample_count FROM metric_baselines "
                "WHERE server_name = ?",
                (server_name,),
            ).fetchall()
        finally:
            conn.close()
        TOTAL_SLOTS = 168  # 7 days * 24 hours
        per_metric: dict[str, dict] = {}
        for r in rows:
            m = r["metric"]
            if m not in per_metric:
                per_metric[m] = {"covered": 0, "total": TOTAL_SLOTS}
            if (r["sample_count"] or 0) >= min_samples:
                per_metric[m]["covered"] += 1
        total_covered = sum(v["covered"] for v in per_metric.values())
        total_possible = sum(v["total"] for v in per_metric.values()) or 1
        return {
            "metrics": per_metric,
            "overall_pct": round(100.0 * total_covered / total_possible, 1),
            "min_samples": min_samples,
        }

    def get_metric_history_span_days(self, server_name: str) -> float:
        """Days between this server's OLDEST metric row and now (0.0 if none).

        Feeds the fused verdict's baseline-authority check: a baseline may
        only downgrade static verdicts once ≥ min_span_weeks of history
        exists (DETECTION_FUSION_PLAN §2 — warm-up before power).
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT MIN(timestamp) AS first_ts FROM metrics WHERE server_name = ?",
                (server_name,),
            ).fetchone()
        finally:
            conn.close()
        first_ts = row["first_ts"] if row else None
        if not first_ts:
            return 0.0
        try:
            first = datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400.0)
        except (ValueError, TypeError):
            return 0.0

    def get_baseline_age_hours(self, server_name: str) -> float | None:
        """Hours since this server's baselines were last recomputed.

        None when the server has no baseline rows at all. Feeds the
        authority freshness gate (stale baselines lose downgrade power).
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT MAX(updated_at) AS last_upd FROM metric_baselines WHERE server_name = ?",
                (server_name,),
            ).fetchone()
        finally:
            conn.close()
        last_upd = row["last_upd"] if row else None
        if not last_upd:
            return None
        raw = str(last_upd)
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0)

    def get_all_baselines(self, server_name: str) -> list[dict]:
        """Get all baseline slots for a server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM metric_baselines WHERE server_name=? ORDER BY metric, hour_of_week",
                (server_name,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_baselines_last_updated(self) -> str | None:
        """Newest ``updated_at`` across all baseline slots, fleet-wide (ISO UTC
        string), or None if ``metric_baselines`` is empty. Used by the
        scheduled baseline-recalc periodic job (``collector_v2.periodics``)
        to decide whether a startup catch-up run is needed."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT MAX(updated_at) FROM metric_baselines").fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def has_metric_history(self) -> bool:
        """True if the ``metrics`` table has at least one row, fleet-wide.
        Used by the baseline-recalc startup catch-up to distinguish "nothing
        collected yet" (don't catch up) from "baselines are empty but there's
        metric history to build them from" (do catch up)."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT 1 FROM metrics LIMIT 1").fetchone()
            return row is not None
        finally:
            conn.close()

    # ── F5: Failed Login methods ────────────────────────────────

    def insert_failed_logins(self, server_name: str, logins: list[dict]):
        """Bulk insert failed login events (ignore duplicates)."""
        if not logins:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.executemany(
                    """INSERT OR IGNORE INTO failed_logins
                       (server_name, timestamp, source_ip, source_port, account_name,
                        domain, event_id, logon_type, workstation, status_code, sub_status, process_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (server_name, l.get("timestamp", ""), l.get("source_ip", ""),
                         l.get("source_port", ""), l.get("account_name", ""),
                         l.get("domain", ""), l.get("event_id", 0),
                         l.get("logon_type", ""), l.get("workstation", ""),
                         l.get("status_code", ""), l.get("sub_status", ""),
                         l.get("process_name", ""))
                        for l in logins
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def get_failed_login_heatmap(self, server_name: str, hours: int = 168) -> list[dict]:
        """Get aggregated heatmap data (day-of-week × hour)."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT CAST(strftime('%%w', timestamp) AS INTEGER) as dow,
                          CAST(strftime('%%H', timestamp) AS INTEGER) as hour,
                          COUNT(*) as count
                   FROM failed_logins
                   WHERE server_name = ? AND timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' hours')
                   GROUP BY dow, hour""",
                (server_name, f"-{hours}"),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_failed_logins_recent(self, server_name: str, hours: int = 24,
                                 limit: int = 100) -> list[dict]:
        """Get recent failed login entries."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM failed_logins
                   WHERE server_name = ? AND timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' hours')
                   ORDER BY timestamp DESC LIMIT ?""",
                (server_name, f"-{hours}", limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_failed_login_count(self, server_name: str, minutes: int = 15) -> int:
        """Count failed logins in last N minutes for spike detection."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM failed_logins
                   WHERE server_name = ? AND timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' minutes')""",
                (server_name, f"-{minutes}"),
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    # ── F6: Config Drift Detection ──

    def insert_config_snapshot(self, server_name: str, snapshot_type: str, data_json: str) -> int:
        """Insert a new config snapshot and return its id."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "INSERT INTO config_snapshots (server_name, snapshot_type, data_json) VALUES (?, ?, ?)",
                    (server_name, snapshot_type, data_json),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def get_latest_snapshot(self, server_name: str, snapshot_type: str) -> dict | None:
        """Get the most recent snapshot by timestamp for a server + type."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT id, server_name, timestamp, snapshot_type, data_json
                   FROM config_snapshots
                   WHERE server_name = ? AND snapshot_type = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (server_name, snapshot_type),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def insert_config_changes(self, server_name: str, snapshot_type: str, changes: list[dict]):
        """Bulk insert config change records."""
        if not changes:
            return
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO config_changes
                       (server_name, snapshot_type, change_type, field, old_value, new_value)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (server_name, snapshot_type, c.get("change_type", ""),
                         c.get("key", "") + ("." + c["field"] if c.get("field") and c.get("change_type") == "modified" else ""),
                         c.get("old_value"), c.get("new_value"))
                        for c in changes
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def get_config_changes(self, server_name: str | None = None, hours: int = 168,
                           limit: int = 100, snapshot_type: str | None = None) -> list[dict]:
        """Get config changes, optionally filtered by server and type."""
        conn = self._get_conn()
        try:
            sql = """SELECT id, server_name, timestamp, snapshot_type, change_type,
                            field, old_value, new_value
                     FROM config_changes
                     WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' hours')"""
            params: list = [f"-{hours}"]
            if server_name:
                sql += " AND server_name = ?"
                params.append(server_name)
            if snapshot_type:
                sql += " AND snapshot_type = ?"
                params.append(snapshot_type)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_snapshot_history(self, server_name: str, snapshot_type: str,
                            limit: int = 10) -> list[dict]:
        """Get recent snapshots for a server + type."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, server_name, timestamp, snapshot_type, data_json
                   FROM config_snapshots
                   WHERE server_name = ? AND snapshot_type = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (server_name, snapshot_type, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── F7: Incident operations ──

    def create_incident(self, title: str, severity: str, description: str | None = None,
                        root_cause_server: str | None = None) -> int:
        """Create a new incident and return its ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "INSERT INTO incidents (title, severity, description, root_cause_server) VALUES (?, ?, ?, ?)",
                    (title, severity, description, root_cause_server)
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_incident(self, incident_id: int, **kwargs):
        """Flexible update for an incident. Supported keys: status, severity, resolved_at,
        resolved_by, resolution_notes, description, root_cause_server, title.
        updated_at is set automatically."""
        allowed = {"status", "severity", "resolved_at", "resolved_by", "resolution_notes",
                   "description", "root_cause_server", "title"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [incident_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(f"UPDATE incidents SET {set_clause} WHERE id = ?", values)
                conn.commit()
            finally:
                conn.close()

    def link_event_to_incident(self, incident_id: int, event_id: int):
        """Link an event to an incident (ignores duplicates)."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO incident_events (incident_id, event_id) VALUES (?, ?)",
                    (incident_id, event_id)
                )
                conn.commit()
            finally:
                conn.close()

    def get_incidents(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Get incidents, optionally filtered by status."""
        conn = self._get_conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_incident_detail(self, incident_id: int) -> dict | None:
        """Get a single incident with its linked events."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if not row:
                return None
            incident = dict(row)
            events = conn.execute(
                """SELECT e.*, ie.added_at AS linked_at
                   FROM incident_events ie
                   JOIN events e ON e.id = ie.event_id
                   WHERE ie.incident_id = ?
                   ORDER BY e.timestamp DESC""",
                (incident_id,)
            ).fetchall()
            incident["events"] = [dict(e) for e in events]
            return incident
        finally:
            conn.close()

    def get_open_incident_count(self) -> int:
        """Count open incidents."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM incidents WHERE status = 'open'").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def get_open_incident_id_by_title_prefix(self, title_prefix: str) -> int | None:
        """Return the id of the most-recent OPEN incident whose title starts with
        ``title_prefix`` (or None). Used by the correlation engine to dedup: an
        ongoing situation (e.g. "Cascading failure from APPSRV06 …") reuses its
        existing open incident instead of spawning a fresh one every cycle.

        LIKE wildcards in the prefix (``%`` and ``_`` — real in tag names) are
        escaped so they match literally.
        """
        esc = (title_prefix.replace("\\", "\\\\")
               .replace("%", "\\%")
               .replace("_", "\\_"))
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM incidents WHERE status = 'open' AND title LIKE ? ESCAPE '\\' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (esc + "%",)
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def collapse_duplicate_open_incidents(self) -> int:
        """Resolve duplicate OPEN incidents that share an identical title, keeping
        only the most-recent one per title. Returns how many were collapsed.

        A self-healing safety net: duplicates carry no extra signal, and this
        clears any pre-dedup backlog (or races) without deleting history — the
        collapsed rows become status='resolved', resolved_by='auto'.
        """
        import re
        # Group by a title normalized to drop a trailing "(…count…)"
        # parenthetical, so the same ongoing situation whose count fluctuates
        # per cycle ("Multiple servers offline (2 servers)" vs "(29 servers)",
        # "Cascading failure from X (1 dependent)" vs "(3 dependents)") collapses
        # to a single incident — matching the creation-time prefix dedup.
        def _norm(t):
            return re.sub(r"\s*\([^()]*\)\s*$", "", t or "").strip()

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._write_lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, title FROM incidents WHERE status = 'open' "
                    "ORDER BY created_at DESC, id DESC"
                ).fetchall()
                seen: set[str] = set()
                dupes: list[int] = []
                for r in rows:
                    key = _norm(r["title"])
                    if key in seen:
                        dupes.append(r["id"])
                    else:
                        seen.add(key)
                for iid in dupes:
                    conn.execute(
                        "UPDATE incidents SET status = 'resolved', resolved_by = 'auto', "
                        "resolved_at = ?, "
                        "resolution_notes = 'Superseded — duplicate incident collapsed', "
                        "updated_at = ? WHERE id = ?",
                        (now, now, iid)
                    )
                conn.commit()
                return len(dupes)
            finally:
                conn.close()

    def vacuum_db(self) -> dict:
        """Run VACUUM on the database. Returns old and new file sizes."""
        with self._write_lock:
            old_size = self.db_path.stat().st_size
            conn = self._get_conn()
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
            new_size = self.db_path.stat().st_size
        return {"old_size": old_size, "new_size": new_size}


    # ── Alert Scoring operations (F9) ──

    def upsert_alert_score(self, server_name: str, metric: str, event_type: str,
                           fire_count: int, ack_count: int, suppress_count: int,
                           score: float, last_fired: str | None = None,
                           last_acked: str | None = None,
                           last_sent_email: str | None = None,
                           last_sent_webhook: str | None = None,
                           last_resolved: str | None = None):
        """Insert or update an alert score record.

        last_sent_email/last_sent_webhook/last_resolved back the feature-1.1
        repeat-interval throttle. Like last_fired/last_acked they are
        COALESCE-merged, so callers that omit them never clobber a prior value.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO alert_scores
                       (server_name, metric, event_type, fire_count, ack_count,
                        suppress_count, score, last_fired, last_acked,
                        last_sent_email, last_sent_webhook, last_resolved)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(server_name, metric, event_type)
                       DO UPDATE SET fire_count=excluded.fire_count,
                                     ack_count=excluded.ack_count,
                                     suppress_count=excluded.suppress_count,
                                     score=excluded.score,
                                     last_fired=COALESCE(excluded.last_fired, last_fired),
                                     last_acked=COALESCE(excluded.last_acked, last_acked),
                                     last_sent_email=COALESCE(excluded.last_sent_email, last_sent_email),
                                     last_sent_webhook=COALESCE(excluded.last_sent_webhook, last_sent_webhook),
                                     last_resolved=COALESCE(excluded.last_resolved, last_resolved)""",
                    (server_name, metric or "", event_type, fire_count, ack_count,
                     suppress_count, score, last_fired, last_acked,
                     last_sent_email, last_sent_webhook, last_resolved)
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to upsert alert score for %s/%s/%s",
                                 server_name, metric, event_type)
            finally:
                conn.close()

    def mark_alert_sent(self, server_name: str, metric: str, event_type: str,
                        channel: str, ts: str | None = None):
        """Stamp the last-sent timestamp for a notification channel (feature 1.1).

        ``channel`` must be 'email' or 'webhook' (anything else is a no-op). Only
        the single ``last_sent_<channel>`` column is written — fatigue counts and
        score are left untouched — so it never disturbs the noise engine. A fresh
        row with zeroed counts is created if none exists.
        """
        col = {"email": "last_sent_email", "webhook": "last_sent_webhook"}.get(channel)
        if col is None:
            return
        ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._write_lock:
            conn = self._get_conn()
            try:
                # col is whitelisted above, so the f-string is injection-safe.
                conn.execute(
                    f"""INSERT INTO alert_scores
                        (server_name, metric, event_type, score, fire_count,
                         ack_count, suppress_count, {col})
                        VALUES (?, ?, ?, 0, 0, 0, 0, ?)
                        ON CONFLICT(server_name, metric, event_type)
                        DO UPDATE SET {col}=excluded.{col}""",
                    (server_name, metric or "", event_type, ts),
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to mark alert sent for %s/%s/%s (%s)",
                                 server_name, metric, event_type, channel)
            finally:
                conn.close()

    def get_alert_score(self, server_name: str, metric: str,
                        event_type: str) -> dict | None:
        """Get a single alert score record."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM alert_scores
                   WHERE server_name = ? AND metric = ? AND event_type = ?""",
                (server_name, metric or "", event_type)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_alert_scores(self, limit: int = 50) -> list[dict]:
        """Get all alert scores ordered by noise score descending."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM alert_scores ORDER BY score DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_alert_scores_for_server(self, server_name: str) -> list[dict]:
        """Get alert scores for a specific server."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM alert_scores WHERE server_name = ? ORDER BY score DESC",
                (server_name,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def reset_alert_scores(self):
        """Delete all alert scores (e.g. after threshold adjustments)."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM alert_scores")
                conn.commit()
            finally:
                conn.close()

    def cleanup_alert_scores(self, min_score: float = 0.1):
        """Remove very low scoring records to keep table tidy."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM alert_scores WHERE score < ?", (min_score,))
                conn.commit()
            finally:
                conn.close()

    # ── Backup state (feature 1.8) ──

    def set_backup_state(self, last_success_ts: str | None = None,
                         last_ok: int | None = None, last_path: str | None = None,
                         last_error: str | None = None,
                         last_alerted_ts: str | None = None):
        """Upsert the single-row backup_state record.

        Only non-None fields are written (COALESCE-merged), so a failed run can
        record last_ok=0 + last_error without clobbering the prior successful
        timestamp. Pass last_ok explicitly as 0 or 1.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO backup_state
                       (id, last_success_ts, last_ok, last_path, last_error, last_alerted_ts)
                       VALUES (1, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           last_success_ts=COALESCE(excluded.last_success_ts, last_success_ts),
                           last_ok=COALESCE(excluded.last_ok, last_ok),
                           last_path=COALESCE(excluded.last_path, last_path),
                           last_error=COALESCE(excluded.last_error, last_error),
                           last_alerted_ts=COALESCE(excluded.last_alerted_ts, last_alerted_ts)""",
                    (last_success_ts, last_ok, last_path, last_error, last_alerted_ts),
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to set backup_state")
            finally:
                conn.close()

    def get_backup_state(self) -> dict | None:
        """Return the single-row backup_state, or None if never written."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM backup_state WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Runbook operations (F10) ──

    def create_runbook(self, name: str, description: str, category: str,
                       steps_json: str, created_by: str = "system",
                       is_builtin: bool = False) -> int:
        """Insert a new runbook. Uses INSERT OR IGNORE for seed idempotency."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO runbooks
                       (name, description, category, steps_json, created_by, is_builtin)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, description, category, steps_json, created_by,
                     1 if is_builtin else 0),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_runbook(self, runbook_id: int, **kwargs):
        """Update a runbook with provided fields."""
        allowed = {"name", "description", "category", "steps_json"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updates["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [runbook_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(f"UPDATE runbooks SET {set_clause} WHERE id = ?", values)
                conn.commit()
            finally:
                conn.close()

    def delete_runbook(self, runbook_id: int) -> bool:
        """Delete a custom (non-builtin) runbook. Returns True if deleted."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM runbooks WHERE id = ? AND is_builtin = 0",
                    (runbook_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_runbooks(self, category: str | None = None) -> list[dict]:
        """Get all runbooks, optionally filtered by category."""
        conn = self._get_conn()
        try:
            if category:
                rows = conn.execute(
                    "SELECT * FROM runbooks WHERE category = ? ORDER BY is_builtin DESC, name",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runbooks ORDER BY is_builtin DESC, name"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_runbook(self, runbook_id: int) -> dict | None:
        """Get a single runbook by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runbooks WHERE id = ?", (runbook_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def insert_runbook_execution(self, runbook_id: int, server_name: str,
                                  status: str, output: str,
                                  executed_by: str = "system",
                                  dry_run: bool = False,
                                  duration_ms: int = 0) -> int:
        """Create an execution record and return its ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO runbook_executions
                       (runbook_id, server_name, status, output, executed_by, dry_run, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (runbook_id, server_name, status, output, executed_by,
                     1 if dry_run else 0, duration_ms),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_runbook_execution(self, exec_id: int, **kwargs):
        """Update execution record with provided fields."""
        allowed = {"status", "output", "duration_ms"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [exec_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE runbook_executions SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
            finally:
                conn.close()

    def get_runbook_executions(self, server_name: str | None = None,
                               runbook_id: int | None = None,
                               limit: int = 50) -> list[dict]:
        """Get execution history with optional filters."""
        conn = self._get_conn()
        try:
            sql = """SELECT re.*, r.name AS runbook_name
                     FROM runbook_executions re
                     JOIN runbooks r ON r.id = re.runbook_id"""
            params: list = []
            wheres = []
            if server_name:
                wheres.append("re.server_name = ?")
                params.append(server_name)
            if runbook_id is not None:
                wheres.append("re.runbook_id = ?")
                params.append(runbook_id)
            if wheres:
                sql += " WHERE " + " AND ".join(wheres)
            sql += " ORDER BY re.timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_runbook_execution(self, exec_id: int) -> dict | None:
        """Get a single execution record."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT re.*, r.name AS runbook_name
                   FROM runbook_executions re
                   JOIN runbooks r ON r.id = re.runbook_id
                   WHERE re.id = ?""",
                (exec_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ── Workflow category operations ──

    def create_workflow_category(self, name: str, color: str = '#8B5CF6') -> int:
        """Create a new workflow category. Returns the new category ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "INSERT INTO workflow_categories (name, color) VALUES (?, ?)",
                    (name, color),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def get_workflow_categories(self) -> list[dict]:
        """Get all workflow categories with workflow count via LEFT JOIN."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT c.id, c.name, c.color, c.created_at,
                          COUNT(w.id) AS workflow_count
                   FROM workflow_categories c
                   LEFT JOIN workflows w ON c.id = w.category_id
                   GROUP BY c.id
                   ORDER BY c.name"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_workflow_category(self, cat_id: int, name: str | None = None,
                                 color: str | None = None):
        """Update a workflow category's name and/or color."""
        fields = []
        params = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if color is not None:
            fields.append("color = ?")
            params.append(color)
        if not fields:
            return
        params.append(cat_id)
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE workflow_categories SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()

    def delete_workflow_category(self, cat_id: int):
        """Delete a workflow category. Workflows referencing it get category_id set to NULL."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("DELETE FROM workflow_categories WHERE id = ?", (cat_id,))
                conn.commit()
            finally:
                conn.close()

    # ── Workflow CRUD operations ──

    def create_workflow(self, name: str, description: str | None,
                        category_id: int | None, trigger_type: str = "manual",
                        trigger_config: str = "{}", canvas_json: str = "{}",
                        created_by: str = "system",
                        is_template: bool = False) -> int:
        """Create a new workflow definition. Returns the new workflow ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO workflows
                       (name, description, category_id, trigger_type, trigger_config,
                        canvas_json, created_by, is_template)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, description, category_id, trigger_type, trigger_config,
                     canvas_json, created_by, 1 if is_template else 0),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_workflow(self, workflow_id: int, **kwargs):
        """Update a workflow with provided fields. Automatically sets updated_at."""
        allowed = {"name", "description", "category_id", "trigger_type",
                    "trigger_config", "canvas_json", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updates["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [workflow_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(f"UPDATE workflows SET {set_clause} WHERE id = ?", values)
                conn.commit()
            finally:
                conn.close()

    def delete_workflow(self, workflow_id: int) -> bool:
        """Delete a workflow. Blocks deletion if is_template=1. Returns True if deleted."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                cur = conn.execute(
                    "DELETE FROM workflows WHERE id = ? AND is_template = 0",
                    (workflow_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_workflows(self, category_id: int | None = None,
                      include_templates: bool = False) -> list[dict]:
        """Get all workflows with optional category filter and last execution status."""
        conn = self._get_conn()
        try:
            # Last execution status — the frontend reads
            # ``wf.last_execution_status`` (templates/workflows.html). The
            # SQL alias must match that name exactly; until 2026-05-22 it
            # was ``last_exec_status``, which silently coerced to ``undefined``
            # in JS and made the workflow card always show "Never run" even
            # for workflows that had executed many times.
            sql = """SELECT w.*,
                            c.name AS category_name,
                            (SELECT we.status FROM workflow_executions we
                             WHERE we.workflow_id = w.id
                             ORDER BY we.started_at DESC LIMIT 1) AS last_execution_status,
                            (SELECT we.completed_at FROM workflow_executions we
                             WHERE we.workflow_id = w.id
                             ORDER BY we.started_at DESC LIMIT 1) AS last_execution_at
                     FROM workflows w
                     LEFT JOIN workflow_categories c ON w.category_id = c.id"""
            params: list = []
            wheres = []
            if category_id is not None:
                wheres.append("w.category_id = ?")
                params.append(category_id)
            if not include_templates:
                wheres.append("w.is_template = 0")
            if wheres:
                sql += " WHERE " + " AND ".join(wheres)
            sql += " ORDER BY w.name"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_workflow(self, workflow_id: int) -> dict | None:
        """Get a single workflow by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def clone_workflow(self, workflow_id: int, new_name: str,
                       created_by: str = "system") -> int:
        """Clone a workflow with a new name. Returns the new workflow ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Workflow {workflow_id} not found")
            source = dict(row)
        finally:
            conn.close()
        return self.create_workflow(
            name=new_name,
            description=source.get("description"),
            category_id=source.get("category_id"),
            trigger_type=source.get("trigger_type", "manual"),
            trigger_config=source.get("trigger_config", "{}"),
            canvas_json=source.get("canvas_json", "{}"),
            created_by=created_by,
            is_template=False,
        )

    # ── Workflow execution operations ──

    def create_workflow_execution(self, workflow_id: int,
                                  trigger_source: str = "manual",
                                  executed_by: str = "system") -> int:
        """Create a new workflow execution record. Returns the execution ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO workflow_executions
                       (workflow_id, trigger_source, executed_by)
                       VALUES (?, ?, ?)""",
                    (workflow_id, trigger_source, executed_by),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_workflow_execution(self, exec_id: int, **kwargs):
        """Update a workflow execution with provided fields."""
        allowed = {"status", "completed_at", "summary", "duration_ms"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [exec_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE workflow_executions SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
            finally:
                conn.close()

    def get_workflow_executions(self, workflow_id: int | None = None,
                                limit: int = 50) -> list[dict]:
        """Get execution history with workflow name via JOIN."""
        conn = self._get_conn()
        try:
            sql = """SELECT we.*, w.name AS workflow_name
                     FROM workflow_executions we
                     JOIN workflows w ON w.id = we.workflow_id"""
            params: list = []
            if workflow_id is not None:
                sql += " WHERE we.workflow_id = ?"
                params.append(workflow_id)
            sql += " ORDER BY we.started_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_workflow_execution_detail(self, exec_id: int) -> dict | None:
        """Get a single execution with all its steps."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT we.*, w.name AS workflow_name
                   FROM workflow_executions we
                   JOIN workflows w ON w.id = we.workflow_id
                   WHERE we.id = ?""",
                (exec_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            steps = conn.execute(
                """SELECT * FROM workflow_execution_steps
                   WHERE execution_id = ?
                   ORDER BY id""",
                (exec_id,),
            ).fetchall()
            result["steps"] = [dict(s) for s in steps]
            return result
        finally:
            conn.close()

    def cancel_workflow_execution(self, exec_id: int):
        """Cancel a running workflow execution (sets status to 'cancelled' only if 'running')."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE workflow_executions SET status = 'cancelled' WHERE id = ? AND status = 'running'",
                    (exec_id,),
                )
                conn.commit()
            finally:
                conn.close()

    # ── Workflow step operations ──

    def insert_workflow_step(self, execution_id: int, node_id: str,
                             node_type: str, node_label: str | None = None,
                             server_name: str | None = None) -> int:
        """Insert a workflow execution step. Returns the step ID."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO workflow_execution_steps
                       (execution_id, node_id, node_type, node_label, server_name)
                       VALUES (?, ?, ?, ?, ?)""",
                    (execution_id, node_id, node_type, node_label, server_name),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_workflow_step(self, step_id: int, **kwargs):
        """Update a workflow step with provided fields."""
        allowed = {"status", "started_at", "completed_at", "output", "error"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [step_id]
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE workflow_execution_steps SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
            finally:
                conn.close()

    def get_workflow_steps(self, execution_id: int) -> list[dict]:
        """Get all steps for a workflow execution."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM workflow_execution_steps WHERE execution_id = ? ORDER BY id",
                (execution_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    # ── RBAC helpers ──────────────────────────────────────────────────────
    # Permission ranking: view < control < admin
    _PERM_RANK = {"view": 1, "control": 2, "admin": 3}

    @classmethod
    def _norm_user(cls, username: str) -> str:
        """Normalize a username to bare-lowercase (strip DOMAIN\\, @suffix)."""
        if not username:
            return ""
        u = username.strip()
        if "\\" in u:
            u = u.split("\\")[-1]
        if "@" in u:
            u = u.split("@")[0]
        return u.lower()

    def acl_is_empty(self) -> bool:
        """Return True when no ACL rows exist (permissive mode)."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM user_server_acl").fetchone()
            return (row["c"] if row else 0) == 0
        finally:
            conn.close()

    def get_user_permission(self, username: str, server_name: str) -> str | None:
        """Return the highest permission ('view'|'control'|'admin') the user has
        for `server_name`, considering wildcard ('*') ACL rows. None if no access."""
        u = self._norm_user(username)
        if not u:
            return None
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT permission FROM user_server_acl
                   WHERE username = ? AND server_name IN (?, '*')""",
                (u, server_name),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        best = None
        best_rank = 0
        for r in rows:
            rank = self._PERM_RANK.get(r["permission"], 0)
            if rank > best_rank:
                best_rank = rank
                best = r["permission"]
        return best

    def grant_acl(self, username: str, server_name: str, permission: str,
                  granted_by: str = "system") -> int:
        if permission not in self._PERM_RANK:
            raise ValueError(f"Invalid permission: {permission!r}")
        u = self._norm_user(username)
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO user_server_acl (username, server_name, permission, granted_by)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(username, server_name) DO UPDATE SET
                         permission = excluded.permission,
                         granted_by = excluded.granted_by,
                         granted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
                    (u, server_name, permission, granted_by),
                )
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

    def revoke_acl(self, username: str, server_name: str) -> int:
        u = self._norm_user(username)
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM user_server_acl WHERE username = ? AND server_name = ?",
                    (u, server_name),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def list_acl(self) -> list[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, username, server_name, permission, granted_by, granted_at "
                "FROM user_server_acl ORDER BY username, server_name"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ── Tier-0 dual-approval helpers ──
    def create_approval_request(self, requested_by: str, server_name: str, action: str,
                                payload_json: str = "{}") -> int:
        with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO pending_approvals (requested_by, server_name, action, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (requested_by, server_name, action, payload_json),
                )
                conn.commit()
                return cur.lastrowid or 0
            finally:
                conn.close()

    def decide_approval(self, approval_id: int, approver: str, approved: bool) -> bool:
        """Mark an approval request as approved/rejected. Returns True if state changed.
        The same user who requested cannot approve their own request."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT requested_by, status, expires_at FROM pending_approvals WHERE id = ?",
                    (approval_id,),
                ).fetchone()
                if not row or row["status"] != "pending":
                    return False
                if self._norm_user(row["requested_by"]) == self._norm_user(approver):
                    return False
                # Check expiry
                if row["expires_at"]:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        exp = _dt.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                        if _dt.now(_tz.utc) > exp:
                            conn.execute(
                                "UPDATE pending_approvals SET status='expired' WHERE id=?",
                                (approval_id,),
                            )
                            conn.commit()
                            return False
                    except (ValueError, TypeError):
                        pass
                conn.execute(
                    """UPDATE pending_approvals
                       SET status = ?, approved_by = ?,
                           decided_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                       WHERE id = ?""",
                    ("approved" if approved else "rejected", approver, approval_id),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def consume_approval(self, approval_id: int) -> dict | None:
        """Atomically consume an approved request (single-use). Returns the row or None."""
        with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM pending_approvals WHERE id = ? AND status = 'approved'",
                    (approval_id,),
                ).fetchone()
                if not row:
                    return None
                conn.execute(
                    "UPDATE pending_approvals SET status='consumed' WHERE id = ?",
                    (approval_id,),
                )
                conn.commit()
                return dict(row)
            finally:
                conn.close()

    def export_audit_log_jsonl(self, dest_path: Path | str,
                               older_than_days: int | None = None) -> int:
        """Write audit_log rows to a JSONL file for cold-storage / SIEM ingest.

        We *never* delete from audit_log (the BEFORE-DELETE trigger blocks it
        and that's by design — see SCHEMA_SQL). This export is purely additive:
        the file is the durable archive; the table remains the source of truth.

        Returns the number of rows written.
        """
        import json as _json
        from pathlib import Path as _Path

        dest = _Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        try:
            if older_than_days is not None and older_than_days > 0:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) ORDER BY id",
                    (f"-{int(older_than_days)} days",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY id"
                ).fetchall()
        finally:
            conn.close()

        with open(dest, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(_json.dumps(dict(r), ensure_ascii=False) + "\n")
        logger.info("Exported %d audit_log rows to %s", len(rows), dest)
        return len(rows)

    def list_pending_approvals(self, include_decided: bool = False) -> list[dict]:
        conn = self._get_conn()
        try:
            if include_decided:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals ORDER BY requested_at DESC LIMIT 200"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_approvals WHERE status='pending' ORDER BY requested_at DESC"
                ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    # Quick self-test
    import tempfile, os
    db = Database(Path(tempfile.mkdtemp()) / "test.db")
    db.insert_metric("TestServer-01", 25.0, 60.0, 45.0, 30.0, "healthy", 150)
    db.insert_metric("TestServer-02", 92.0, 85.0, 95.0, None, "critical", 200)
    db.insert_event("TestServer-02", "critical", "disk_c", 95.0, 90.0, "Disk C: exceeded 90% (95.0%)")

    latest = db.get_latest_all()
    assert len(latest) == 2, f"Expected 2 servers, got {len(latest)}"

    summary = db.get_status_summary()
    assert summary["total"] == 2
    assert summary["healthy"] == 1
    assert summary["critical"] == 1

    events = db.get_recent_events()
    assert len(events) == 1

    server = db.get_latest_by_server("TestServer-01")
    assert server["cpu_percent"] == 25.0

    history = db.get_server_history("TestServer-01", hours=1)
    assert len(history) == 1

    print("All database tests passed!")
