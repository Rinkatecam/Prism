"""Health-check probe runner — synthetic monitoring beyond the metrics
the WinRM collector pulls.

Operators configure per-server health checks via the UI (operations
page → Health Checks). Each row is a (target_host, target_port,
check_type) tuple in the ``health_check_config`` table. The runner
polls all configured probes at the cadence set by
``collector_v2/periodics.py`` (default 5 min) and fires an event on
status transitions.

Why not just rely on WinRM metrics? Because a Windows service can be
down while the host itself is healthy — pinging the OS doesn't tell
you whether IIS is serving traffic on :443, whether the DNS resolver
on :53/udp is responsive, or whether an external HTTPS endpoint is
returning 200s. These probes complement the host-level signals.

Check types:
  * ``tcp``   — open a TCP socket
  * ``http``  — GET against a URL, follow no redirects, accept 2xx/3xx
  * ``https`` — same with TLS
  * ``udp``   — send a UDP packet and check for ICMP-unreachable
  * ``icmp``  — ping

The underlying probe primitives live in ``health_checker.py``; this
module is the orchestrator (schedule + state-change → event).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("prism.healthchecks")


def _run_health_checks(db, settings: dict) -> None:
    """Run all configured health check probes; fire events on status changes.

    Compares the new probe result against the most recent stored result
    for the same (server_name, target_host, target_port, check_type)
    tuple. Status transitions emit:
      * ``up → down``  : ``critical`` event "Health check FAILED"
      * ``down → up``  : ``info`` event "Health check RECOVERED"

    No event is emitted on first-ever check (no prior status to compare).
    No event is emitted when status is unchanged.
    """
    from health_checker import tcp_probe, http_check, udp_probe, icmp_ping

    configs = db.get_health_check_config()
    if not configs:
        return

    for cfg in configs:
        if not cfg.get("enabled", True):
            continue

        host = cfg["target_host"]
        port = cfg["target_port"]
        check_type = cfg["check_type"]
        server_name = cfg["server_name"]

        if check_type == "http":
            result = http_check(host, port, path=cfg.get("http_path", "/"),
                                use_ssl=False, timeout=10)
        elif check_type == "https":
            # ABSENT OR NULL MEANS VERIFY. `bool(cfg.get("verify_tls", 1))`
            # reads the same and is wrong: a dict default only applies when the
            # KEY IS MISSING, so a present-but-None value becomes False and the
            # check silently stops validating certificates. Same guard, and the
            # same reason, as routes/api/health.py:_verify_tls_from_payload —
            # the two must not drift, because the failure is invisible in both.
            _vt = cfg.get("verify_tls")
            result = http_check(host, port, path=cfg.get("http_path", "/"),
                                use_ssl=True, timeout=10,
                                verify_tls=True if _vt is None else bool(_vt))
        elif check_type == "udp":
            result = udp_probe(host, port, timeout=5)
        elif check_type == "icmp":
            result = icmp_ping(host, timeout=5)
        else:
            result = tcp_probe(host, port, timeout=5)

        status = result.get("status", "down")

        # Compare to previous result for state-change detection
        previous = db.get_health_check_results(server_name)
        prev_status = None
        for p in previous:
            if (p["target_host"] == host
                    and p["target_port"] == port
                    and p["check_type"] == check_type):
                prev_status = p["status"]
                break

        db.upsert_health_check_result(
            server_name=server_name,
            check_type=check_type,
            target_host=host,
            target_port=port,
            status=status,
            response_time_ms=result.get("response_time_ms"),
            error=result.get("error"),
        )

        # Fire alert on status change (no event on first-ever check)
        check_name = cfg.get("name") or f"{check_type} {host}:{port}"
        if prev_status and prev_status != status:
            if status == "down":
                db.insert_event(server_name, "critical", "health_check", None, None,
                                f"Health check FAILED: {check_name} ({check_type} {host}:{port})")
                logger.warning("[%s] Health check DOWN: %s %s:%d", server_name, check_type, host, port)
            elif status == "up" and prev_status == "down":
                db.insert_event(server_name, "info", "health_check", None, None,
                                f"Health check RECOVERED: {check_name} ({check_type} {host}:{port})")
                logger.info("[%s] Health check UP: %s %s:%d", server_name, check_type, host, port)

    logger.debug("Health checks completed for %d endpoints", len(configs))
