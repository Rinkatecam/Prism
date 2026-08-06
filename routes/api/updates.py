"""Updates endpoints — split out from the original routes/api.py."""

import re
import time
import io
import json
from pathlib import Path
from flask import jsonify, request, Response, make_response, current_app
from flask import session as flask_session
from crypto_utils import is_password_masked, decrypt_password, PASSWORD_MASK
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


@api_bp.route("/sync-now", methods=["POST"])
def trigger_sync():
    """Trigger an immediate metrics collection for every server."""
    try:
        _v2_sync_now()
        logger.info("On-demand sync triggered via API")
        return jsonify({"ok": True, "message": "Sync triggered"})
    except Exception:
        logger.exception("Error triggering sync")
        return jsonify({"error": "Internal server error"}), 500


# ── Helpers for the auto-restart-after-update flow (Q2 from the May review) ──
# The user ticks "Restart server after update" in the install confirmation.
# Previously that intent lived in a tab-local JavaScript variable
# (`window._installRestartAfter`), so refreshing the page or closing the tab
# silently lost the auto-restart. Now the intent is persisted server-side in
# `_update_install_state[name]['restart_after']` and a daemon thread watches
# the target server's status file until reboot_required, at which point the
# restart fires automatically — no user tab required.


def _read_remote_install_status(cfg) -> dict | None:
    """Read C:\\ProgramData\\Prism\\update-status.json from a target server over
    WinRM and return the parsed payload. Returns None on any failure (caller
    should treat as "unchanged from last read")."""
    from pypsrp.powershell import PowerShell, RunspacePool
    read_script = r"""
$path = 'C:\ProgramData\Prism\update-status.json'
if (Test-Path $path) {
    try { [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) }
    catch { '{}' }
} else { '{}' }
"""
    try:
        wsman = _wu_make_wsman(cfg, read_timeout=30)
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(read_script)
            out = ps.invoke()
            raw = (str(out[0]) if out and out[0] is not None else "").lstrip("﻿").strip()
            if not raw or raw == "{}":
                return None
            return json.loads(raw)
    except Exception as e:
        logger.debug("remote install-status read failed for %s: %s", cfg.name, e)
        return None


def _clear_stale_remote_status_file(cfg) -> None:
    """Best-effort: delete the remote install-status file on a server when
    Windows confirms there's no pending reboot but the file still says
    ``restart_required``. Prevents the /update-status ↔ /updates
    ping-pong loop (the 2026-05-21 SRV01 incident) from re-firing on
    every dashboard refresh of that server.

    Runs in a daemon thread (callers pass ``threading.Thread(..., daemon=True)``)
    so a slow WinRM connection never blocks the HTTP response. Failures are
    logged at DEBUG only — the loop-prevention is already done by the
    /update-status endpoint refusing to import the stale file; this cleanup
    is just removing the dead file so future operators don't trip on it.
    """
    try:
        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=15)
        cleanup_script = r"""
$path = 'C:\ProgramData\Prism\update-status.json'
if (Test-Path $path) {
    try {
        Remove-Item -Path $path -Force -ErrorAction Stop
        'removed'
    } catch {
        'failed: ' + $_.Exception.Message
    }
} else {
    'missing'
}
"""
        with RunspacePool(wsman) as pool:
            ps = PowerShell(pool)
            ps.add_script(cleanup_script)
            output = ps.invoke()
            result = str(output[0]) if output else "no output"
        logger.info(
            "[%s] stale remote update-status.json cleanup: %s",
            cfg.name, result.strip(),
        )
    except Exception as e:
        logger.debug(
            "[%s] stale remote status cleanup failed (non-fatal): %s",
            cfg.name, e,
        )


def _set_rebooting_state(name: str, actor: str = "system") -> None:
    """Transition ``_update_install_state[name]`` to a ``rebooting`` row.

    Called immediately after we successfully send a restart command (via
    auto-restart, the user clicking Restart, or any other path). The
    dashboard reads ``_update_install_state`` so this is what makes the
    tile show "Rebooting" instead of going blank during the 3-5 min
    reboot window. The aggregator clears the row when metrics start
    flowing again (the ``_handle_post_reboot`` hook in
    ``collector_v2/aggregator.py``); the periodics janitor times out
    after 20 min if the server never comes back so the dashboard
    eventually shows the truthful offline state.

    Preserves the pre-reboot ``restart_after`` intent (in case multiple
    restarts get triggered in sequence) and the original
    ``installed_count`` for the post-reboot summary.
    """
    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prev = _shared._update_install_state.get(name) or {}
    _shared._update_install_state[name] = {
        "status": "rebooting",
        "message": "Server is restarting after update install.",
        "started_at": prev.get("started_at", iso_now),
        "updated_at": iso_now,
        "reboot_started_at": iso_now,
        "actor": actor,
        # Preserved fields useful after reboot
        "installed_count": prev.get("installed_count", 0),
        "pending_count": prev.get("pending_count", 0),
        "restart_after": False,  # restart fired, intent satisfied
    }
    _shared._persist_install_state()


def _trigger_server_restart_internal(name: str, actor: str = "system:auto_restart") -> bool:
    """Send a restart command to the target server via WinRM without going
    through the HTTP endpoint. Used by the auto-restart daemon. Returns True
    if the command landed (or the WinRM session died as expected during the
    reboot — which is success). Mirrors the same shutdown.exe path the manual
    /power endpoint uses."""
    cfg = _shared._config.get_server_by_name(name)
    if not cfg:
        logger.warning("auto-restart: server %s not found in config", name)
        return False
    from pypsrp.powershell import PowerShell, RunspacePool
    from winrm_factory import make_wsman
    _EXPECTED_DEATHS = (
        "winrm", "session", "pipeline", "runspace",
        "connection was forcibly closed", "existing connection was forcibly closed",
        "transport connection", "closed unexpectedly",
    )
    script = r"shutdown.exe /r /t 5 /f /c 'Prism: auto-restart after update'"
    try:
        wsman = make_wsman(cfg, connection_timeout=15, read_timeout=15)
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(script)
                ps.invoke()
                if ps.had_errors:
                    err = "; ".join(str(e) for e in ps.streams.error).lower()
                    if any(p in err for p in _EXPECTED_DEATHS):
                        pass  # expected — session died because the box went down
                    else:
                        logger.warning("auto-restart %s failed: %s", name, err[:300])
                        return False
        except Exception as e:
            if not any(p in str(e).lower() for p in _EXPECTED_DEATHS):
                raise
        logger.info("auto-restart fired on %s (actor=%s)", name, actor)
        try:
            _shared._db.log_audit(actor, "auto_restart", "server", name)
        except Exception:
            pass
        # Don't pop the install_state — keep it alive as ``status="rebooting"``
        # so the dashboard tile, the updates partial, and the server-detail
        # overlay all keep showing what the server is actually doing during
        # the 3–5 minute reboot window. The aggregator clears the state when
        # metrics start flowing again (see _handle_post_reboot in
        # collector_v2/aggregator.py); a janitor in periodics gives up after
        # 20 min if the server never returns.
        _set_rebooting_state(name, actor=actor)
        info = server_update_info.get(name)
        if info:
            info.pop("pending_reboot", None)
            info.pop("reboot_required", None)
        accelerate_server(name, reason="auto_restart_fired", duration_s=20 * 60)
        return True
    except Exception as e:
        logger.exception("auto-restart on %s crashed: %s", name, e)
        return False


def _spawn_auto_restart_watcher(name: str, actor: str):
    """Background daemon that polls the target server's install-status.json
    every 30 s for up to 90 min. When the status transitions to
    'restart_required' AND _update_install_state[name]['restart_after'] is
    still True, fires the restart. Designed to survive the user closing the
    tab — the install intent lives in the in-memory state dict, not on the
    page.

    NOTE: this thread does NOT survive a Flask restart. The
    ``auto_restart_scanner`` periodic job in
    ``collector_v2/periodics.py`` is a second, periodic safety-net that
    scans the install_state dict every minute and fires any pending
    auto-restart it finds. Together the watcher and the scanner make
    auto-restart resilient to most failure modes: watcher fires fast
    (30 s after restart_required), scanner fires within 60 s of any
    state the watcher missed.

    Stops cleanly if:
      * restart_after is cleared (user changed their mind via /cancel or
        another install kickoff)
      * status reaches a terminal non-restart state (completed/failed/idle)
      * 90 min elapse without restart_required appearing
    """
    import threading

    def _watch():
        logger.info(
            "auto-restart watcher started for %s (actor=%s, deadline=90min)",
            name, actor,
        )
        deadline = time.time() + 90 * 60
        last_status = None
        while time.time() < deadline:
            try:
                time.sleep(30)
                cur = _shared._update_install_state.get(name) or {}
                if not cur.get("restart_after"):
                    logger.info("auto-restart watcher for %s: intent cleared, exiting", name)
                    return
                cfg = _shared._config.get_server_by_name(name)
                if not cfg:
                    logger.warning("auto-restart watcher for %s: server gone from config, exiting", name)
                    return
                payload = _read_remote_install_status(cfg)
                if payload:
                    # Keep restart_after sticky across the read so we don't
                    # lose intent when overwriting the cached state
                    payload["restart_after"] = True
                    _shared._update_install_state[name] = payload
                    _shared._persist_install_state()
                    new_status = payload.get("status")
                    if new_status != last_status:
                        logger.info(
                            "auto-restart watcher for %s: status %s → %s",
                            name, last_status, new_status,
                        )
                        accelerate_server(name, reason=f"watcher_state={new_status}")
                        last_status = new_status

                    if new_status == "restart_required":
                        logger.info("auto-restart watcher for %s: firing restart", name)
                        ok = _trigger_server_restart_internal(name, actor=actor)
                        if ok:
                            logger.info("auto-restart watcher for %s: restart fired successfully", name)
                            return  # _update_install_state is now ``rebooting``
                        # If the restart command failed, leave the intent set
                        # and try again on the next tick. The periodics scanner
                        # will also retry within 60 s. Worst case the user
                        # comes back, sees the page, and triggers it manually.
                        logger.warning(
                            "auto-restart watcher for %s: restart command failed; will retry on next tick",
                            name,
                        )

                    if new_status in ("completed", "failed", "idle"):
                        # Terminal non-restart state — drop the intent so a future
                        # manual install doesn't carry it forward unexpectedly.
                        logger.info(
                            "auto-restart watcher for %s: terminal state '%s' reached, "
                            "clearing restart_after intent",
                            name, new_status,
                        )
                        if name in _shared._update_install_state:
                            _shared._update_install_state[name]["restart_after"] = False
                        return
                else:
                    # No payload — install-status.json not present yet (install
                    # PowerShell hasn't started writing) or unreachable. Log at
                    # debug because this is common in the first few ticks.
                    logger.debug("auto-restart watcher for %s: no remote status yet", name)
            except Exception:
                logger.warning("auto-restart watcher for %s tick failed", name, exc_info=True)
        logger.warning(
            "auto-restart watcher for %s timed out after 90 min — last status was %r. "
            "If the server is in restart_required, the periodics scanner will still pick it up.",
            name, last_status,
        )

    t = threading.Thread(target=_watch, daemon=True, name=f"prism-auto-restart-{name}")
    t.start()
    logger.info("auto-restart watcher spawned for %s (actor=%s)", name, actor)


@api_bp.route("/sync-updates-now", methods=["POST"])
def trigger_sync_updates():
    """Force Windows Update check on the next collector cycle + wake the
    collector. Unlike sync-now (which runs a regular metrics cycle that may
    or may not include the update check depending on modulo gating), this
    guarantees the WU COM search runs so server_update_info is refreshed
    right after the call returns."""
    try:
        _v2_sync_updates_now()
        logger.info("On-demand update check triggered via API")
        return jsonify({"ok": True, "message": "Update check triggered"})
    except Exception:
        logger.exception("Error triggering update check")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/sync-logs-now", methods=["POST"])
def trigger_sync_logs():
    """Force Windows event log collection for every server now.

    The supervisor sets next_logs_at = now for all tracked servers,
    so the next 5-second tick picks them up. Independent of the
    regular ``log_collection_interval_minutes`` cadence — this is a
    one-off override, not a permanent change.
    """
    try:
        _v2_sync_logs_now()
        logger.info("On-demand log sync triggered via API")
        return jsonify({"ok": True, "message": "Log sync triggered"})
    except Exception:
        logger.exception("Error triggering log sync")
        return jsonify({"error": "Internal server error"}), 500


def _wu_ps_script_body() -> str:
    """PowerShell script that runs LOCALLY on the target via a scheduled task.

    It writes progress to C:\\ProgramData\\Prism\\update-status.json so the
    Prism server can poll it through WinRM. The script runs as SYSTEM (via
    the scheduled task), which gives the Windows Update Agent the elevated
    interactive token it requires (remote WinRM sessions do NOT satisfy the
    WU COM Install() call — it returns 0x80070005 E_ACCESSDENIED).
    """
    return r"""
# First thing we do: make the directory and write a heartbeat file so we can
# tell "task never ran" (no heartbeat) apart from "task ran but crashed".
# Everything here is defensive — Start-Transcript has a history of being
# blocked by group policy / AppLocker on hardened servers, so we avoid it
# entirely and write our own log with Add-Content instead.
$dir = 'C:\ProgramData\Prism'
if (-not (Test-Path $dir)) {
    try { New-Item -ItemType Directory -Path $dir -Force | Out-Null } catch {}
}
$statusPath = Join-Path $dir 'update-status.json'
$logPath    = Join-Path $dir 'update-log.txt'
$heartbeatPath = Join-Path $dir 'update-heartbeat.txt'

# Truncate the log on each new run so we don't accumulate forever
try { Set-Content -Path $logPath -Value '' -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
try {
    "Script entered at $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))" |
        Out-File -FilePath $heartbeatPath -Encoding ascii -Force
} catch {}

function Log($msg) {
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$ts] $msg"
    try { Add-Content -Path $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
    try { Write-Host $line } catch {}
}

Log "=== Prism update task started ==="
try { Log ("Running as: " + [Security.Principal.WindowsIdentity]::GetCurrent().Name) } catch {}
try { Log ("PSVersion: " + $PSVersionTable.PSVersion.ToString()) } catch {}

function Write-Status {
    param([hashtable]$data)
    $data['updated_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    try {
        $json = $data | ConvertTo-Json -Compress -Depth 3
        [System.IO.File]::WriteAllText($statusPath, $json, [System.Text.Encoding]::UTF8)
    } catch {
        Log "Write-Status failed: $($_.Exception.Message)"
    }
}

$started = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$state = @{
    status = 'searching'
    message = 'Searching for available updates'
    started_at = $started
    installed_count = 0
    pending_count = 0
    reboot_required = $false
    error = $null
}
Write-Status $state

try {
    # Refuse to proceed if a reboot is already pending — WU will silently
    # refuse to install anything until the previous install's reboot happens.
    try {
        $preSys = New-Object -ComObject Microsoft.Update.SystemInfo
        if ($preSys -and $preSys.RebootRequired) {
            Log "Pre-check: RebootRequired=TRUE, refusing to install"
            $state['status'] = 'failed'
            $state['message'] = 'Cannot install: a reboot is already pending from a previous install'
            $state['error']   = 'Server has a pending reboot. Restart the server first, then retry.'
            $state['reboot_required'] = $true
            $state['completed_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            Write-Status $state
            return
        }
    } catch {}

    $session = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $searcher.ServerSelection = 2  # Public Microsoft Update — matches collector
    Log "Searching for updates..."
    $results = $searcher.Search("IsInstalled=0 AND IsHidden=0")
    Log "Search returned $($results.Updates.Count) update(s)"

    if ($results.Updates.Count -eq 0) {
        $state['status'] = 'completed'
        $state['message'] = 'No updates available'
        $state['completed_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Write-Status $state
        return
    }

    $state['pending_count'] = $results.Updates.Count
    $state['status'] = 'downloading'
    $state['message'] = "Downloading $($results.Updates.Count) update(s)"
    Write-Status $state

    $toDownload = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $results.Updates) {
        try { if (-not $u.EulaAccepted) { $u.AcceptEula() } } catch {}
        $toDownload.Add($u) | Out-Null
    }

    Log "Starting download of $($toDownload.Count) update(s)..."
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $toDownload
    $dlResult = $downloader.Download()
    Log "Download finished, aggregate ResultCode=$($dlResult.ResultCode)"

    # Build the install set from updates that were actually downloaded.
    # Some updates in the original batch may have failed to download; we
    # don't want to pass them to Install() because the aggregate result
    # becomes ambiguous.
    $toInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    $downloadedCount = 0
    foreach ($u in $toDownload) {
        try {
            if ($u.IsDownloaded) {
                $toInstall.Add($u) | Out-Null
                $downloadedCount++
            } else {
                Log "Skipping not-downloaded update: $($u.Title)"
            }
        } catch {}
    }
    Log "Ready-to-install: $downloadedCount / $($toDownload.Count)"

    if ($toInstall.Count -eq 0) {
        # Nothing is ready to install — surface as failure.
        $state['status'] = 'failed'
        $state['message'] = "Download stage reported $($dlResult.ResultCode) but no updates ended up in a ready state"
        $state['error']   = "Aggregate download code $($dlResult.ResultCode); 0 updates downloaded"
        $state['completed_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Write-Status $state
        return
    }

    $state['status'] = 'installing'
    $state['message'] = "Installing $($toInstall.Count) update(s)"
    $state['pending_count'] = $toInstall.Count
    Write-Status $state

    # Install with automatic retry. WU error 0x8024200D ("needs another
    # download") is common with cumulative/delta updates — the first download
    # gets metadata but the install discovers it needs more content. A single
    # re-download + re-install almost always fixes it.
    $maxAttempts = 2
    $result = $null
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        Log "Install attempt $attempt/$maxAttempts on $($toInstall.Count) update(s)..."
        $installer = $session.CreateUpdateInstaller()
        $installer.Updates = $toInstall
        $result = $installer.Install()
        Log "  Install() returned aggregate ResultCode=$($result.ResultCode) HResult=$($result.HResult)"

        # Check if we need to re-download (0x8024200D = WU_E_UH_NEEDANOTHERDOWNLOAD)
        $needRetry = $false
        for ($i = 0; $i -lt $toInstall.Count; $i++) {
            try {
                $hr = [int]$result.GetUpdateResult($i).HResult
                if ($hr -eq -2145124318 -or $hr -eq 0x8024200D) { $needRetry = $true; break }
            } catch {}
        }
        if ($needRetry -and $attempt -lt $maxAttempts) {
            Log "  Got WU_E_UH_NEEDANOTHERDOWNLOAD — re-downloading before retry..."
            $state['status'] = 'downloading'
            $state['message'] = "Re-downloading (attempt $($attempt+1))..."
            Write-Status $state
            try {
                $downloader2 = $session.CreateUpdateDownloader()
                $downloader2.Updates = $toInstall
                $dl2 = $downloader2.Download()
                Log "  Re-download ResultCode=$($dl2.ResultCode)"
            } catch {
                Log "  Re-download failed: $($_.Exception.Message)"
            }
            $state['status'] = 'installing'
            $state['message'] = "Installing (attempt $($attempt+1))..."
            Write-Status $state
        } else {
            break
        }
    }

    # Per-update result inspection. Aggregate ResultCode by itself is not
    # enough — Install() can return success while individual updates failed.
    # ResultCode: 0=NotStarted 1=InProgress 2=Succeeded 3=SucceededWithErrors 4=Failed 5=Aborted
    $succeeded = 0
    $failed = 0
    $partial = 0
    $failedTitles = New-Object System.Collections.ArrayList
    for ($i = 0; $i -lt $toInstall.Count; $i++) {
        try {
            $rc = [int]$result.GetUpdateResult($i).ResultCode
            $hr = [int]$result.GetUpdateResult($i).HResult
            $title = [string]$toInstall.Item($i).Title
            $hrHex = ('0x{0:X8}' -f $hr)
            Log ("  [{0}/{1}] rc={2} hr={3}  {4}" -f ($i+1), $toInstall.Count, $rc, $hrHex, $title)
            if     ($rc -eq 2) { $succeeded++ }
            elseif ($rc -eq 3) { $partial++; [void]$failedTitles.Add("$title ($hrHex partial)") }
            elseif ($rc -eq 4) { $failed++;  [void]$failedTitles.Add("$title ($hrHex failed)") }
            elseif ($rc -eq 5) { $failed++;  [void]$failedTitles.Add("$title ($hrHex aborted)") }
            else               { $failed++;  [void]$failedTitles.Add("$title (rc=$rc hr=$hrHex)") }
        } catch {
            $failed++
            Log "  per-update inspection failed: $($_.Exception.Message)"
        }
    }
    $aggregateRc = $null
    try { $aggregateRc = [int]$result.ResultCode } catch {}
    Log "Summary: succeeded=$succeeded failed=$failed partial=$partial aggregate=$aggregateRc"

    $reboot = $false
    try {
        $sysInfo = New-Object -ComObject Microsoft.Update.SystemInfo
        $reboot = [bool]$sysInfo.RebootRequired
    } catch {
        try { $reboot = [bool]$result.RebootRequired } catch {}
    }

    $state['installed_count'] = $succeeded
    $state['failed_count'] = $failed + $partial
    $state['reboot_required'] = [bool]$reboot
    $state['completed_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $state['aggregate_result_code'] = $aggregateRc

    $total = $toInstall.Count
    if ($succeeded -eq 0 -and ($failed + $partial) -gt 0) {
        # Nothing succeeded → hard failure. Never stuck in a pseudo-installed state.
        $state['status'] = 'failed'
        $state['message'] = "All $total update(s) failed to install"
        $state['error'] = ($failedTitles | Select-Object -First 5) -join '; '
    } elseif (($failed + $partial) -gt 0) {
        # Partial — some succeeded, some didn't. Surface as failed so the
        # user knows to investigate, but count what did go in.
        $state['status'] = 'failed'
        $state['message'] = "$succeeded of $total update(s) installed, $($failed + $partial) failed"
        $state['error'] = ($failedTitles | Select-Object -First 5) -join '; '
    } elseif ($reboot) {
        $state['status'] = 'restart_required'
        $state['message'] = "Installed $succeeded update(s) — restart required"
    } else {
        $state['status'] = 'completed'
        $state['message'] = "Installed $succeeded update(s)"
    }
    Write-Status $state
} catch {
    Log "EXCEPTION: $($_.Exception.Message)"
    Log $_.ScriptStackTrace
    $state['status'] = 'failed'
    $state['message'] = $_.Exception.Message
    $state['error'] = $_.Exception.Message
    $state['completed_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Write-Status $state
} finally {
    try { Log "=== script exiting ===" } catch {}
}
"""


def _wu_make_wsman(cfg, read_timeout: int = 60):
    """WU helper — delegates to the central factory so HTTPS/cert flags apply."""
    from winrm_factory import make_wsman
    return make_wsman(cfg, connection_timeout=30, read_timeout=read_timeout)


@api_bp.route("/servers/<name>/install-updates", methods=["POST"])
def install_server_updates(name: str):
    """Install Windows Updates on a remote server.

    We must use a local scheduled task running as SYSTEM — `Microsoft.Update.
    Session.CreateUpdateInstaller().Install()` (and on some servers
    `Download()` too) returns 0x80070005 E_ACCESSDENIED when called from a
    remote WinRM session, regardless of the caller's privileges. The WU COM
    API explicitly checks for an interactive/SYSTEM token.

    AppLocker / Constrained Language Mode on hardened servers often blocks
    arbitrary .ps1 files in C:\\ProgramData, so we pass the install script
    via `-EncodedCommand` (UTF-16LE base64, embedded in the command line)
    instead of writing it to disk. Nothing ends up on the target except:

      - the scheduled task definition itself (no .ps1 file)
      - a status JSON file in C:\\ProgramData\\Prism\\update-status.json
      - a log file in C:\\ProgramData\\Prism\\update-log.txt
    """
    auth_err = _require_server_permission(name, "admin")
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        # Stale in-flight guard with auto-unstick after 10 min
        cur = _shared._update_install_state.get(name) or {}
        in_flight = cur.get("status") in ("searching", "downloading", "installing", "queued")
        force = bool(request.args.get("force"))
        if in_flight and not force:
            import datetime as _dt
            try:
                updated_at = cur.get("updated_at") or cur.get("started_at")
                if updated_at:
                    t = _dt.datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
                    age_s = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds()
                    if age_s <= 600:
                        return jsonify({
                            "ok": False,
                            "error": "Update installation already in progress. Cancel first or wait.",
                            "status": cur.get("status"),
                        }), 409
                    logger.info("Auto-unsticking stale '%s' on %s (%.0fs old)", cur.get("status"), name, age_s)
            except Exception:
                pass

        import base64 as _b64
        script_body = _wu_ps_script_body()
        try:
            encoded_cmd = _b64.b64encode(script_body.encode("utf-16-le")).decode("ascii")
        except Exception as enc_err:
            return jsonify({"ok": False, "error": f"Encode failed: {enc_err}"}), 500

        bootstrap = r"""
$ErrorActionPreference = 'Stop'
$result = @{ ok = $false; stage = 'init' }
try {
    $dir = 'C:\ProgramData\Prism'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $statusPath = Join-Path $dir 'update-status.json'
    $heartbeatPath = Join-Path $dir 'update-heartbeat.txt'

    # Wipe any leftover state so this run is fresh
    try { Remove-Item -Path (Join-Path $dir 'install-updates.ps1') -Force -ErrorAction SilentlyContinue } catch {}
    try { Remove-Item -Path $heartbeatPath -Force -ErrorAction SilentlyContinue } catch {}
    try { Remove-Item -Path (Join-Path $dir 'update-log.txt') -Force -ErrorAction SilentlyContinue } catch {}

    $initial = @{
        status = 'queued'
        message = 'Scheduled task registered, waiting to start'
        started_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        updated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        installed_count = 0
        pending_count = 0
        reboot_required = $false
    }
    [System.IO.File]::WriteAllText($statusPath, ($initial | ConvertTo-Json -Compress -Depth 3), [System.Text.Encoding]::UTF8)

    $taskName = 'PrismInstallUpdates'
    $result.stage = 'unregister_old'
    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

    $result.stage = 'register'
    $encoded = 'ENCODED_COMMAND'
    $taskArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encoded
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null

    $result.stage = 'start'
    Start-ScheduledTask -TaskName $taskName

    # Diagnostic: wait up to 5 s for the task to either produce the heartbeat
    # file or for LastTaskResult to become non-zero. If neither, we know
    # nothing even tried to run.
    $result.stage = 'verify'
    $heartbeatSeen = $false
    $lastResult = $null
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $heartbeatPath) { $heartbeatSeen = $true; break }
        try {
            $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
            if ($info -and $info.LastTaskResult -ne $null -and $info.LastTaskResult -ne 267009) {
                # 267009 = "task is currently running", so anything else is a real result
                $lastResult = [int]$info.LastTaskResult
                if ($lastResult -ne 0) { break }
            }
        } catch {}
    }

    $result.ok = $true
    $result.stage = 'done'
    $result.heartbeat_seen = $heartbeatSeen
    $result.last_task_result = $lastResult
    $result | ConvertTo-Json -Compress
} catch {
    $result.ok = $false
    $result.error = $_.Exception.Message
    $result | ConvertTo-Json -Compress
}
"""
        bootstrap = bootstrap.replace("ENCODED_COMMAND", encoded_cmd)

        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=60)

        import json as _json
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(bootstrap)
                output = ps.invoke()
                raw = str(output[0]) if output else "{}"
                err_stream = ""
                if ps.had_errors:
                    try:
                        err_stream = "; ".join(str(e) for e in ps.streams.error)[:300]
                    except Exception:
                        err_stream = "PowerShell reported errors"
        except Exception as e:
            logger.exception("Install bootstrap dispatch failed for %s", name)
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

        try:
            boot_result = _json.loads(raw)
        except Exception:
            return jsonify({"ok": False, "error": f"Invalid bootstrap response: {raw[:200]}", "ps_stream_error": err_stream}), 500

        # If the task didn't even produce a heartbeat and didn't crash with a
        # specific code, something is blocking PowerShell from running at all
        # (AppLocker, CLM, etc.). Surface that clearly.
        hb = boot_result.get("heartbeat_seen")
        ltr = boot_result.get("last_task_result")
        diag_note = None
        if boot_result.get("ok") and not hb and (ltr in (None, 0)):
            diag_note = (
                "Bootstrap registered and started the scheduled task, but the "
                "task produced no heartbeat in 5s. This usually means "
                "AppLocker / Constrained Language Mode / Exploit Guard is "
                "blocking powershell.exe -EncodedCommand on this server. "
                "Check HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\SrpV2 "
                "and local group policy for script execution rules."
            )
        elif boot_result.get("ok") and ltr not in (None, 0):
            diag_note = f"Scheduled task exited immediately with code {ltr}. Check the target's Windows Event Log 'Microsoft-Windows-TaskScheduler/Operational' for details."

        # Q2 from the May review: accept restart_after in the request body so
        # the auto-restart-after-update intent persists server-side, instead
        # of dying with the user's browser tab.
        body = request.get_json(silent=True) or {}
        restart_after = bool(body.get("restart_after", False))

        # Seed the cached state so polling can kick in
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _shared._update_install_state[name] = {
            "status": "queued",
            "message": diag_note or "Scheduled task registered, waiting to start",
            "started_at": now_iso,
            "updated_at": now_iso,
            "installed_count": 0,
            "pending_count": 0,
            "reboot_required": False,
            "error": diag_note if not hb and ltr not in (None, 0) else None,
            "heartbeat_seen": hb,
            "last_task_result": ltr,
            # Persisted intent — auto-restart watcher reads this and fires
            # when status reaches restart_required.
            "restart_after": restart_after,
        }

        if not boot_result.get("ok"):
            return jsonify({
                "ok": False,
                "error": boot_result.get("error", "Bootstrap failed"),
                "stage": boot_result.get("stage"),
                "ps_stream_error": err_stream,
            }), 500

        accelerate_server(name, reason="install_kickoff")
        _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
        if restart_after:
            _spawn_auto_restart_watcher(name, actor=f"user:{_audit_user}")
        logger.info("Install-updates dispatched to %s (heartbeat_seen=%s, last_task_result=%s, restart_after=%s)",
                    name, hb, ltr, restart_after)
        _shared._db.log_audit(_audit_user, "install_updates", "server",
                              f"{name} (restart_after={restart_after})")
        return jsonify({
            "ok": True,
            "status": "queued",
            "message": diag_note or "Install started in background",
            "heartbeat_seen": hb,
            "last_task_result": ltr,
            "note": diag_note,
            "restart_after": restart_after,
        })

    except Exception as e:
        logger.exception("Error installing updates on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/install-updates-direct", methods=["POST"])
def install_server_updates_direct(name: str):
    """Diagnostic-only: runs the full WU COM sequence inline over WinRM and
    returns the raw response. Kept around because it immediately tells us
    whether a specific server will even let Download/Install run from a
    remote session. Not the button users click — that's /install-updates."""
    auth_err = _require_server_permission(name, "admin")
    if auth_err:
        return auth_err
    # F-078 remediation (this audit's scope): even a diagnostic-only
    # path that runs WU operations on a target needs an audit row — it
    # can change update state on the server (Download/Install side
    # effects). Operator attribution is captured BEFORE the call so a
    # crash mid-operation still leaves the audit footprint.
    _shared._db.log_audit(
        username=flask_session.get("username", "system"),
        action="install_updates_direct",
        category="updates",
        details=f"server={name}",
    )
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": f"Server '{name}' not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool

        # Longer read timeout — Install() can legitimately take several minutes.
        wsman = _wu_make_wsman(cfg, read_timeout=900)

        ps_direct = r"""
$ErrorActionPreference = 'Continue'
$out = @{
    ok = $false
    stage = 'init'
    message = ''
    searched_count = 0
    download_rc = $null
    download_hresult_hex = $null
    installed_count = 0
    install_rc = $null
    install_hresult_hex = $null
    reboot_required = $false
    per_update = @()
    running_as = $null
    ps_version = $null
    error = $null
}

try { $out.running_as = [Security.Principal.WindowsIdentity]::GetCurrent().Name } catch {}
try { $out.ps_version = $PSVersionTable.PSVersion.ToString() } catch {}

try {
    $out.stage = 'session'
    $session = New-Object -ComObject Microsoft.Update.Session

    $out.stage = 'search'
    $searcher = $session.CreateUpdateSearcher()
    $searcher.ServerSelection = 2  # Public Microsoft Update — matches collector
    $results = $searcher.Search("IsInstalled=0 AND IsHidden=0")
    $out.searched_count = [int]$results.Updates.Count

    if ($results.Updates.Count -eq 0) {
        $out.ok = $true
        $out.stage = 'done'
        $out.message = 'No updates available'
        $out | ConvertTo-Json -Compress -Depth 4
        return
    }

    $out.stage = 'download'
    $toInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $results.Updates) {
        try { if (-not $u.EulaAccepted) { $u.AcceptEula() } } catch {}
        $toInstall.Add($u) | Out-Null
    }

    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $toInstall
    $dlResult = $downloader.Download()
    $out.download_rc = [int]$dlResult.ResultCode
    try { $out.download_hresult_hex = ('0x{0:X8}' -f [int]$dlResult.HResult) } catch {}
    if ($dlResult.ResultCode -ge 4) {
        $out.stage = 'download_failed'
        $out.message = "Download returned ResultCode $($dlResult.ResultCode)"
        $out.error = "Download HResult $($out.download_hresult_hex)"
        $out | ConvertTo-Json -Compress -Depth 4
        return
    }

    # Only include actually-downloaded updates
    $ready = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $toInstall) {
        try { if ($u.IsDownloaded) { $ready.Add($u) | Out-Null } } catch {}
    }
    if ($ready.Count -eq 0) {
        $out.stage = 'nothing_downloaded'
        $out.message = 'No updates were actually downloaded'
        $out | ConvertTo-Json -Compress -Depth 4
        return
    }

    $out.stage = 'install'
    $installer = $session.CreateUpdateInstaller()
    $installer.Updates = $ready
    $installResult = $installer.Install()
    $out.install_rc = [int]$installResult.ResultCode
    try { $out.install_hresult_hex = ('0x{0:X8}' -f [int]$installResult.HResult) } catch {}

    $succeeded = 0
    for ($i = 0; $i -lt $ready.Count; $i++) {
        $rc = -1; $hr = 0; $title = ''
        try { $rc = [int]$installResult.GetUpdateResult($i).ResultCode } catch {}
        try { $hr = [int]$installResult.GetUpdateResult($i).HResult } catch {}
        try { $title = [string]$ready.Item($i).Title } catch {}
        if ($rc -eq 2) { $succeeded++ }
        $out.per_update += @{
            title = $title
            rc = $rc
            hr_hex = ('0x{0:X8}' -f $hr)
        }
    }
    $out.installed_count = $succeeded

    try {
        $sysInfo = New-Object -ComObject Microsoft.Update.SystemInfo
        $out.reboot_required = [bool]$sysInfo.RebootRequired
    } catch {
        try { $out.reboot_required = [bool]$installResult.RebootRequired } catch {}
    }

    $out.ok = ($succeeded -gt 0)
    $out.stage = 'done'
    if ($succeeded -eq 0) {
        $out.message = "Install returned ResultCode $($installResult.ResultCode), nothing succeeded"
        $out.error = "Aggregate $($out.install_hresult_hex); no updates ended up in Succeeded state"
    } elseif ($succeeded -lt $ready.Count) {
        $out.message = "Installed $succeeded of $($ready.Count) update(s)"
    } else {
        $out.message = "Installed $succeeded update(s)"
    }
    $out | ConvertTo-Json -Compress -Depth 4

} catch {
    $out.stage = 'exception'
    $out.error = $_.Exception.Message
    try { $out.error_hresult_hex = ('0x{0:X8}' -f [int]$_.Exception.HResult) } catch {}
    $out.message = "PowerShell exception at stage '$($out.stage)': $($_.Exception.Message)"
    $out | ConvertTo-Json -Compress -Depth 4
}
"""

        import json as _json
        raw = ""
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(ps_direct)
                output = ps.invoke()
                raw = str(output[0]) if output else ""
                err_stream = ""
                if ps.had_errors:
                    try:
                        err_stream = "; ".join(str(e) for e in ps.streams.error)[:400]
                    except Exception:
                        err_stream = "PowerShell reported errors"
        except Exception as e:
            logger.exception("Direct install failed for %s", name)
            return jsonify({
                "ok": False,
                "stage": "winrm_dispatch",
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            }), 500

        try:
            result = _json.loads(raw) if raw else {}
        except Exception as je:
            return jsonify({
                "ok": False,
                "stage": "parse",
                "error": f"Invalid JSON from target: {je}",
                "raw": raw[:500],
                "ps_stream_error": err_stream,
            }), 500

        # Update our in-memory cached update info if the install actually did
        # something, so the banner count on the dashboard reflects reality.
        if result.get("ok") and name in server_update_info:
            info = server_update_info[name]
            installed = int(result.get("installed_count") or 0)
            info["count"] = max(0, int(info.get("count") or 0) - installed)
            info["last_install"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if result.get("reboot_required"):
                info["pending_reboot"] = True

        # Always log the full result dict so we can see exactly what came back
        logger.info("Direct install on %s → %s", name, result)
        # Surface the PS error stream alongside the JSON result so the client
        # can see both.
        if err_stream:
            result["ps_stream_error"] = err_stream
        return jsonify(result)

    except Exception as e:
        logger.exception("Error installing updates on %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/cancel-updates", methods=["POST"])
def cancel_server_updates(name: str):
    """Force-cancel an in-flight update install on the target server.

    Kills the `PrismInstallUpdates` scheduled task if it's running, unregisters
    it, and wipes the status file so the UI guard stops blocking new runs.
    Use when the previous run got wedged in 'queued' / 'installing' and never
    advanced (e.g. old-script leftovers, a crashed task process, etc.).
    """
    auth_err = _require_server_permission(name, "admin")
    if auth_err:
        return auth_err
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": "Server not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=30)

        cleanup_script = r"""
$ErrorActionPreference = 'Continue'
$dir = 'C:\ProgramData\Prism'
$statusPath = Join-Path $dir 'update-status.json'
$logPath    = Join-Path $dir 'update-log.txt'
$taskName = 'PrismInstallUpdates'

$killed = $false
$procsKilled = 0

# Step 1: stop + unregister the scheduled task
try {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t) {
        try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue; $killed = $true } catch {}
        try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    }
} catch {}

# Step 2: kill any orphaned powershell.exe processes running our script.
# This also releases any file handles they're holding on update-status.json.
try {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*install-updates.ps1*' } |
        ForEach-Object {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $procsKilled++; $killed = $true } catch {}
        }
} catch {}

# Give Windows a beat to release the file handle after we killed the process
Start-Sleep -Milliseconds 500

# Step 3: DELETE the status file (don't try to rewrite — a locked file would
# silently fail and leave stale content). Retry a couple times in case of
# lingering handles.
$deleted = $false
for ($i = 0; $i -lt 3; $i++) {
    try {
        if (Test-Path $statusPath) {
            Remove-Item -LiteralPath $statusPath -Force -ErrorAction Stop
        }
        $deleted = $true
        break
    } catch {
        Start-Sleep -Milliseconds 400
    }
}

@{
    ok = $true
    task_killed = $killed
    procs_killed = $procsKilled
    status_file_deleted = $deleted
} | ConvertTo-Json -Compress
"""
        import json as _json
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(cleanup_script)
                output = ps.invoke()
                stdout = str(output[0]) if output else "{}"
                try:
                    result = _json.loads(stdout)
                except Exception:
                    result = {"ok": True, "task_killed": False, "raw": stdout[:200]}
        except Exception as e:
            logger.exception("cancel-updates WinRM failed for %s", name)
            # Still clear local cache so the user can retry
            _shared._update_install_state.pop(name, None)
            _shared._persist_install_state()
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

        # Clear our in-memory cache so the next install attempt is unblocked
        _shared._update_install_state.pop(name, None)
        _shared._persist_install_state()
        accelerate_server(name)
        logger.info("Update install cancelled on %s (task_killed=%s)", name, result.get("task_killed"))
        _audit_user = flask_session.get("username", request.remote_addr or "anonymous")
        _shared._db.log_audit(_audit_user, "cancel_updates", "server", f"{name}")
        return jsonify({"ok": True, **result, "message": "Install cancelled and state reset"})

    except Exception as e:
        logger.exception("Error cancelling updates for %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/update-task-info")
def get_update_task_info(name: str):
    """Query the target's scheduled task state.

    When update-log is empty but update-status says 'queued', it means the
    bootstrap wrote the initial state but the task itself never produced
    output. This endpoint lets us see whether the task started, completed,
    what LastTaskResult was (0 = OK, non-zero = problem), and whether the
    install-updates.ps1 file is even on disk.
    """
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": "Server not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=30)

        diag = r"""
$ErrorActionPreference = 'Continue'
$result = @{
    task_exists = $false
    task_state = $null
    last_run_time = $null
    last_task_result = $null
    last_task_result_hex = $null
    number_of_missed_runs = $null
    next_run_time = $null
    script_file_exists = $false
    script_file_size = 0
    script_file_mtime = $null
    status_file_exists = $false
    status_file_size = 0
    log_file_exists = $false
    log_file_size = 0
    heartbeat_exists = $false
    heartbeat_content = $null
    wu_service_state = $null
    running_ps_processes = 0
}

try {
    $t = Get-ScheduledTask -TaskName 'PrismInstallUpdates' -ErrorAction SilentlyContinue
    if ($t) {
        $result.task_exists = $true
        $result.task_state = [string]$t.State
        try {
            $i = Get-ScheduledTaskInfo -TaskName 'PrismInstallUpdates' -ErrorAction SilentlyContinue
            if ($i) {
                $result.last_run_time = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:ssZ') } else { $null }
                $result.last_task_result = [int]$i.LastTaskResult
                $result.last_task_result_hex = ('0x{0:X8}' -f ([int]$i.LastTaskResult))
                $result.number_of_missed_runs = [int]$i.NumberOfMissedRuns
                $result.next_run_time = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:ssZ') } else { $null }
            }
        } catch {}
    }
} catch {}

$scriptPath = 'C:\ProgramData\Prism\install-updates.ps1'
if (Test-Path $scriptPath) {
    $f = Get-Item $scriptPath
    $result.script_file_exists = $true
    $result.script_file_size = [int64]$f.Length
    $result.script_file_mtime = $f.LastWriteTimeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

$statusPath = 'C:\ProgramData\Prism\update-status.json'
if (Test-Path $statusPath) {
    $f = Get-Item $statusPath
    $result.status_file_exists = $true
    $result.status_file_size = [int64]$f.Length
}

$logPath = 'C:\ProgramData\Prism\update-log.txt'
if (Test-Path $logPath) {
    $f = Get-Item $logPath
    $result.log_file_exists = $true
    $result.log_file_size = [int64]$f.Length
}

$heartbeatPath = 'C:\ProgramData\Prism\update-heartbeat.txt'
if (Test-Path $heartbeatPath) {
    $result.heartbeat_exists = $true
    try { $result.heartbeat_content = (Get-Content -Path $heartbeatPath -Raw -ErrorAction SilentlyContinue) } catch {}
}

try {
    $wu = Get-Service -Name wuauserv -ErrorAction SilentlyContinue
    if ($wu) { $result.wu_service_state = [string]$wu.Status }
} catch {}

try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -like '*install-updates.ps1*' }
    if ($procs) {
        if ($procs -is [array]) { $result.running_ps_processes = $procs.Count }
        else { $result.running_ps_processes = 1 }
    }
} catch {}

$result | ConvertTo-Json -Compress -Depth 3
"""
        import json as _json
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(diag)
                output = ps.invoke()
                raw = str(output[0]) if output else "{}"
                try:
                    data = _json.loads(raw)
                except Exception as je:
                    return jsonify({"ok": False, "error": f"parse failure: {je}", "raw": raw[:400]}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": f"WinRM failed: {str(e)[:200]}"}), 502

        return jsonify({"ok": True, **data})
    except Exception as e:
        logger.exception("Error fetching task info for %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/update-log")
def get_update_install_log(name: str):
    """Fetch the PowerShell transcript from the target's last install run.
    Handy when the status file says the job finished but the actual WU
    outcome is mysterious (phantom 'installing', silent failures, etc.)."""
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": "Server not found"}), 404

        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=30)

        read_script = r"""
$path = 'C:\ProgramData\Prism\update-log.txt'
if (Test-Path $path) {
    try {
        # Only grab the last 16 KB so we don't blow up the WinRM payload on
        # long-running servers where the transcript has been appended over
        # multiple runs.
        $fi = Get-Item $path
        $reader = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $start = [math]::Max(0, $fi.Length - 16384)
        [void]$reader.Seek($start, [System.IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] ($fi.Length - $start)
        [void]$reader.Read($buf, 0, $buf.Length)
        $reader.Close()
        [System.Text.Encoding]::UTF8.GetString($buf)
    } catch {
        "ERROR reading transcript: $($_.Exception.Message)"
    }
} else {
    '(no transcript on target — either no install has been triggered, or the task never produced output)'
}
"""
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(read_script)
                output = ps.invoke()
                raw = str(output[0]) if output else ""
        except Exception as e:
            return jsonify({"ok": False, "error": f"WinRM read failed: {str(e)[:200]}"}), 502

        return jsonify({"ok": True, "log": raw})
    except Exception as e:
        logger.exception("Error fetching update log for %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/update-status")
def get_update_install_status(name: str):
    """Poll the update install status. Reads the remote status file via WinRM
    and also caches the result in _shared._update_install_state so consecutive fast
    polls don't hammer WinRM harder than needed."""
    try:
        cfg = _shared._config.get_server_by_name(name)
        if not cfg:
            return jsonify({"ok": False, "error": "Server not found"}), 404

        cached = _shared._update_install_state.get(name) or {}

        # If the install is in a terminal state AND it's been >30s since we last
        # updated, skip the network hit — client can just use the cached value.
        terminal = cached.get("status") in ("completed", "restart_required", "failed")
        # Always refresh at least once so we pick up the PS script's writes.

        from pypsrp.powershell import PowerShell, RunspacePool
        wsman = _wu_make_wsman(cfg, read_timeout=30)

        # Read via [System.IO.File]::ReadAllText to avoid the BOM / line-ending
        # quirks Get-Content has. Write the raw bytes to stdout so pypsrp gives
        # us exactly one string back.
        read_script = r"""
$path = 'C:\ProgramData\Prism\update-status.json'
if (Test-Path $path) {
    try {
        [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    } catch {
        '{}'
    }
} else {
    '{}'
}
"""
        import json as _json
        payload = {}
        try:
            with RunspacePool(wsman) as pool:
                ps = PowerShell(pool)
                ps.add_script(read_script)
                output = ps.invoke()
                if output:
                    raw = str(output[0]) if output[0] is not None else ""
                    # Strip UTF-8 BOM / whitespace / trailing newlines
                    raw = raw.lstrip("\ufeff").strip()
                    if raw and raw != "{}":
                        try:
                            payload = _json.loads(raw)
                        except Exception as je:
                            logger.warning(
                                "update-status: JSON parse failed for %s: %s — raw=%r",
                                name, je, raw[:400]
                            )
                            payload = {
                                "status": "unknown",
                                "message": f"Status file unreadable: {str(je)[:80]}",
                                "raw_preview": raw[:200],
                            }
        except Exception as e:
            logger.debug("update-status read failed for %s: %s", name, e)
            # Fall back to whatever we have cached
            if cached:
                return jsonify({"ok": True, **cached, "stale": True})
            return jsonify({"ok": False, "error": str(e)[:200]}), 502

        if payload:
            # Detect state transitions BEFORE we overwrite the cached payload —
            # each transition (queued → searching → downloading → installing →
            # restart_required → completed) is a "something is happening" signal
            # the dashboard should react to. Re-arming acceleration on each
            # transition keeps the per-cycle polling alive across long installs
            # (cumulative updates often take >5 min) without forcing operators
            # to manually re-trigger.
            prev_cached = _shared._update_install_state.get(name) or {}
            prev_state = prev_cached.get("status")
            new_state = payload.get("status")

            # ── DO NOT overwrite Prism-owned lifecycle states ────────────
            # ``rebooting`` and ``stabilising`` are written by Prism itself
            # (``_set_rebooting_state`` after a restart command lands;
            # ``_handle_post_reboot`` in the aggregator when metrics return)
            # — the remote ``update-status.json`` doesn't know about them
            # and still contains the last value the install script wrote,
            # usually ``restart_required``.
            #
            # If we let the remote-file payload overwrite our state, the
            # restart overlay disappears the moment WinRM successfully
            # reads the file again (e.g. during a short reboot window or
            # the stabilising phase). Operators who navigate away and
            # come back see ``restart_required`` again, the overlay never
            # re-appears, and the dashboard tile flips back to the
            # purple "restart needed" badge — confusing because the
            # server is actually mid-reboot or just settled.
            #
            # Fix: when the cached state is Prism-owned, return it as-is
            # and skip the overwrite. The aggregator's
            # ``_handle_post_reboot`` will transition us out of these
            # states when metrics start flowing again.
            _PRISM_OWNED = {"rebooting", "stabilising"}
            if prev_state in _PRISM_OWNED:
                logger.debug(
                    "update-status: keeping Prism-owned state %s for %s "
                    "(remote file said %s, ignoring)",
                    prev_state, name, new_state,
                )
                return jsonify({"ok": True, **prev_cached, "remote_state": new_state})

            # Carry forward the restart_after intent — it lives in Prism's
            # cached state, not in the target's status file, so we must NOT
            # let the file-read overwrite it. The watcher daemon clears it
            # when the auto-restart fires (or when the install reaches a
            # terminal non-restart state).
            if prev_cached.get("restart_after"):
                payload["restart_after"] = True

            # ── Trust live Windows over a stale remote update-status.json ──
            # A common path into this loop: an install completed on the
            # target weeks/months ago, leaving its on-disk status file at
            # ``restart_required``. The operator then rebooted the server
            # out-of-band (RDP / console / different orchestration tool),
            # which cleared Windows's RebootRequired flag — but the install
            # script's status file was NEVER cleaned up. Now:
            #
            #   * The collector's UPDATES check reports
            #     ``pending_reboot=False`` (Microsoft.Update.SystemInfo)
            #   * The remote file says ``restart_required``
            #
            # If we naively import the file, ``/api/servers/<n>/updates``
            # immediately pops the install_state (correctly — Windows is
            # authoritative). The next ``/update-status`` poll re-imports
            # the stale file → write fires a transition → acceleration.
            # The two endpoints ping-pong forever (the 2026-05-21 SRV01
            # incident). Break the loop here: if we have fresh, real
            # collector data that says "no reboot needed", ignore the
            # stale file's restart_required.
            #
            # Best-effort: nudge the remote file to ``completed`` so the
            # next install on that server starts from a clean slate. Done
            # silently — failures don't surface because the file is
            # advisory, not authoritative.
            if new_state == "restart_required":
                live_info = server_update_info.get(name) or {}
                live_fresh = bool(
                    live_info.get("checked_at")
                    and not live_info.get("error")
                    and not live_info.get("transient_error")
                )
                if live_fresh and not live_info.get("pending_reboot"):
                    logger.info(
                        "[%s] /update-status ignoring stale remote "
                        "restart_required (Windows reports no pending "
                        "reboot via live WU query)", name,
                    )
                    # Schedule a best-effort remote file cleanup so this
                    # doesn't repeat on every page load. Runs in a daemon
                    # so we don't slow down the response.
                    try:
                        import threading as _th
                        _th.Thread(
                            target=_clear_stale_remote_status_file,
                            args=(cfg,), daemon=True,
                        ).start()
                    except Exception:
                        logger.debug(
                            "[%s] could not schedule remote status cleanup",
                            name, exc_info=True,
                        )
                    return jsonify({
                        "ok": True,
                        "status": "idle",
                        "message": "No install activity (remote status file was stale; Windows confirms no pending reboot)",
                    })

            _shared._update_install_state[name] = payload
            _shared._persist_install_state()

            # Fire the auto-restart here too — covers the case where the
            # watcher daemon missed a transition (rare) and the user's poll
            # observes restart_required first. Idempotent: trigger_restart
            # clears restart_after via pop'ing the install state.
            if new_state == "restart_required" and payload.get("restart_after"):
                _trigger_server_restart_internal(name, actor="system:auto_restart_poll")

            # Acceleration policy
            # ────────────────────
            # We accelerate ONLY when an active install is in flight (or just
            # transitioned out of one). The 2026-05-21 SRV01/SRV03
            # incidents both came from accelerating transitions INTO a stale
            # terminal state — a server stuck in ``restart_required`` (or
            # ``failed``) for days would get a 10-min acceleration burst on
            # every Prism restart and every fresh dashboard view, because the
            # in-memory install_state starts as ``None`` and the remote file's
            # leftover value would register as a "transition."
            #
            # Rules:
            #   * Active progress states (queued/searching/downloading/
            #     installing) re-arm acceleration every poll — these states
            #     are short and the user wants live UI updates.
            #   * Transition OUT of an active state (e.g. installing →
            #     restart_required at install completion) accelerates ONCE
            #     so the badge appears quickly within a supervisor tick.
            #   * Transition from ``None`` → terminal/waiting state (the
            #     stale-file import case) does NOT accelerate. There's no
            #     ongoing activity; nothing changes if we wait one cycle.
            #   * Repeat polls in the same state never accelerate.
            ACTIVE_PROGRESS_STATES = {
                "queued", "searching", "downloading", "installing",
            }
            transitioned_out_of_active = (
                prev_state in ACTIVE_PROGRESS_STATES
                and new_state is not None
                and prev_state != new_state
            )
            if new_state in ACTIVE_PROGRESS_STATES or transitioned_out_of_active:
                accelerate_server(name, reason=f"install_state={new_state}")

            # If WU just finished, reflect it in the cached updates info — but
            # ONLY if the collector hasn't produced fresher data yet. Otherwise
            # we'd keep overwriting the collector's authoritative live WU
            # query on every page load. Specifically: skip the overlay when
            # server_update_info[name].checked_at is NEWER than the install's
            # completed_at (meaning the collector has re-scanned post-reboot).
            if payload.get("status") in ("completed", "restart_required") and name in server_update_info:
                info = server_update_info[name]
                info_checked = info.get("checked_at") or ""
                install_done = payload.get("completed_at") or ""
                # Only overlay when no fresher data exists
                if not info_checked or (install_done and info_checked <= install_done):
                    installed = int(payload.get("installed_count") or 0)
                    for u in info.get("updates", []):
                        u["status"] = "installed"
                    info["count"] = max(0, int(info.get("count") or 0) - installed)
                    info["last_install"] = install_done or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    if payload.get("reboot_required"):
                        info["pending_reboot"] = True

            return jsonify({"ok": True, **payload})

        # No payload (file missing) — return cached if any, otherwise "idle"
        if cached:
            return jsonify({"ok": True, **cached})
        return jsonify({"ok": True, "status": "idle", "message": "No install history"})

    except Exception as e:
        logger.exception("Error fetching update status for %s", name)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@api_bp.route("/servers/<name>/updates")
def get_server_updates(name: str):
    """Get pending Windows updates for a server.

    Priority (most authoritative first):
      1. collector.server_update_info[name] with a checked_at timestamp —
         this is a LIVE query of Microsoft.Update.SystemInfo.RebootRequired
         on the target, so it always reflects reality.
      2. _shared._update_install_state[name] — only consulted when the collector
         hasn't yet produced fresh data (e.g. right after a Prism restart),
         as a stop-gap so we don't show stale "no updates pending" either.

    Critically, if the collector reports a fresh check with pending_reboot=
    false, we DROP the stale install-state overlay and clear it — otherwise
    a successful reboot would still show "restart pending" until the process
    restarted. This was the bug after the SRV04 update+reboot cycle.
    """
    try:
        info = server_update_info.get(name)
        install_state = _shared._update_install_state.get(name) or {}

        # Base payload from the collector cache (or empty default)
        if info:
            payload = {"name": name, **info}
        else:
            payload = {"name": name, "count": 0, "updates": [], "checked_at": None}

        # Does the collector cache have authoritative fresh data? We count it
        # as "authoritative" when a checked_at is present AND no error flag.
        # If so, trust the collector's pending_reboot value exactly (it came
        # from a live WU COM query) and discard any stale install-state.
        #
        # Critical: ``info.transient_error`` means the aggregator preserved
        # the previous good payload through a WinRM blip / timeout (see
        # ``_handle_updates_result``). The ``pending_reboot`` field in that
        # case reflects the LAST SUCCESSFUL check, which may pre-date the
        # install entirely. We must NOT pop install_state based on that — it
        # caused the SRV01 bombardment loop (2026-05-21) where a server
        # offline since an install kept getting its install_state cleared
        # by stale data, which then made the /update-status endpoint see
        # every poll as a fresh transition into restart_required, firing
        # acceleration on every dashboard poll.
        collector_fresh = bool(
            info
            and info.get("checked_at")
            and not info.get("error")
            and not info.get("transient_error")
        )
        if collector_fresh:
            # If the target itself now says "no reboot pending", the install
            # must have finished + rebooted already. Clear the install state
            # so future polls don't keep trying to overlay it.
            if not info.get("pending_reboot") and install_state:
                _shared._update_install_state.pop(name, None)
                _shared._persist_install_state()
            return jsonify(payload)

        # Collector cache is stale or empty — fall back to the install-state
        # overlay as a short-term stand-in. Only applies to states where we
        # KNOW a reboot is needed because we just installed something.
        terminal = install_state.get("status") in ("completed", "restart_required")
        if terminal and install_state.get("reboot_required"):
            payload["pending_reboot"] = True
            payload["reboot_required"] = True
            if install_state.get("completed_at") and "last_install" not in payload:
                payload["last_install"] = install_state["completed_at"]

        return jsonify(payload)
    except Exception:
        logger.exception("Error in GET /api/servers/%s/updates", name)
        return jsonify({"error": "Internal server error"}), 500
