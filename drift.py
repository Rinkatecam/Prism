"""Config drift detection — periodic snapshots of service / hotfix /
local-admin lists per server, diff against the previous snapshot, and
emit ``config_drift`` events when something changes.

The snapshot collection primitives (PowerShell scripts, JSON parsing,
diff logic) live in ``drift_detector.py`` — this module is the
orchestrator that runs the snapshots on the v2 periodics cadence.

Cadence: default 1h (hardcoded in ``collector_v2/periodics.py``) but
overridable via ``drift_detection.check_interval_cycles`` in
settings.json. The operator must also set ``drift_detection.enabled``
to True before this code does anything — drift is opt-in because the
snapshots can be sensitive (local admin members, registered services).

Snapshot types collected (configurable via
``drift_detection.snapshot_types``):
  * ``services``      — Win32_Service rows + startup type
  * ``hotfixes``      — installed Windows updates (Get-HotFix)
  * ``local_admins``  — members of the local Administrators group

Each type is stored in the ``config_snapshots`` table; diffs go to
``config_changes``. Redaction patterns from settings can strip
PII / passwords from changed values before storage.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("prism.drift")


def _collect_drift_snapshots(db, servers, settings: dict) -> None:
    """Collect config drift snapshots from all servers and detect changes.

    Per server:
      1. Open a WinRM RunspacePool.
      2. Run the configured snapshot scripts via ``drift_detector``.
      3. Diff against the latest stored snapshot for each type.
      4. Apply redaction patterns to changed values.
      5. Store the new snapshot + changes.
      6. Emit a ``config_drift`` event if any changes were detected and
         ``alert_on_change`` is True.

    Errors per-server are swallowed at debug level — a flaky server
    doesn't block drift detection for the rest of the fleet.
    """
    from pypsrp.wsman import WSMan
    from pypsrp.powershell import RunspacePool
    from crypto_utils import decrypt_password
    import drift_detector

    drift_cfg = settings.get("drift_detection", {})
    if not drift_cfg.get("enabled", False):
        return

    enabled_types = drift_cfg.get("snapshot_types", ["services", "hotfixes", "local_admins"])
    redaction_patterns = drift_cfg.get("redaction_patterns", [])
    alert_on_change = drift_cfg.get("alert_on_change", True)

    for server in servers:
        try:
            from winrm_factory import make_wsman
            wsman = make_wsman(server, connection_timeout=15, read_timeout=30)
            with RunspacePool(wsman) as pool:
                snapshots = drift_detector.collect_all_snapshots(pool, enabled_types)

                total_changes = 0
                for snap_type, new_data in snapshots.items():
                    if not new_data:
                        continue

                    # Resolve the key field for this snapshot type
                    _script, key_field = drift_detector.SNAPSHOT_TYPES.get(
                        snap_type, (None, "Name"),
                    )

                    # Load previous snapshot for diffing
                    prev = db.get_latest_snapshot(server.name, snap_type)
                    prev_data = []
                    if prev:
                        try:
                            prev_data = json.loads(prev.get("data_json", "[]"))
                        except (json.JSONDecodeError, ValueError):
                            prev_data = []

                    # Compute diff
                    changes = drift_detector.diff_snapshots(prev_data, new_data, key_field)

                    # Apply redaction to sensitive values BEFORE storage
                    if redaction_patterns:
                        for c in changes:
                            if c.get("old_value"):
                                c["old_value"] = drift_detector.redact_sensitive(
                                    c["old_value"], redaction_patterns,
                                )
                            if c.get("new_value"):
                                c["new_value"] = drift_detector.redact_sensitive(
                                    c["new_value"], redaction_patterns,
                                )

                    # Persist new snapshot
                    data_json = json.dumps(new_data, default=str)
                    db.insert_config_snapshot(server.name, snap_type, data_json)

                    # Persist changes + emit event
                    if changes:
                        db.insert_config_changes(server.name, snap_type, changes)
                        total_changes += len(changes)

                        if alert_on_change:
                            summary = f"{len(changes)} config change(s) detected in {snap_type}"
                            db.insert_event(
                                server.name, "info", "config_drift",
                                len(changes), None, summary,
                            )

                if total_changes:
                    logger.info("[%s] Drift detection: %d total changes",
                                server.name, total_changes)
                    db.log_audit("system", "drift_detection",
                                 "config", f"{server.name}: {total_changes} changes detected")
                else:
                    logger.debug("[%s] Drift detection: no changes", server.name)

        except Exception:
            logger.debug("[%s] Drift snapshot collection skipped", server.name)
