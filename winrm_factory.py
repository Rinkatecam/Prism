"""Centralised WinRM connection factory + correlation-ID helper.

All callers (collector, workflow_engine, runbook_engine, restart_scheduler,
security_checker, routes/api.py) build pypsrp WSMan connections with slightly
different timeouts and SSL flags. Consolidating into one factory means a
single place to change auth, port, or transport defaults — and a single
place to enforce HTTPS when the ServerConfig opts in.

Why this lives in its own module
--------------------------------
Putting the factory in `crypto_utils.py` would couple credential decryption
with transport policy. Putting it in `models.py` would force every importer
to pull pypsrp. Keeping it here lets WinRM-aware modules import the factory
and unaware modules (analytics, reports) stay clean.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import ServerConfig

logger = logging.getLogger("prism.winrm")


def make_wsman(
    server_config: "ServerConfig",
    *,
    connection_timeout: int = 15,
    read_timeout: int = 30,
    auth: str = "negotiate",
):
    """Build a pypsrp WSMan with the right transport based on ServerConfig flags.

    HTTPS rules:
      * `use_https=True`  → ssl=True, default port 5986, server cert validated
        (unless `https_skip_verify=True`, which is intentionally noisy in logs).
      * `use_https=False` → ssl=False on port 5985 (legacy default).

    The port stored on ServerConfig wins over our defaults, so an operator
    can explicitly run HTTPS on a non-standard port.
    """
    try:
        from pypsrp.wsman import WSMan
    except ImportError:
        raise RuntimeError("pypsrp not installed. Run: pip install pypsrp")
    from crypto_utils import decrypt_password

    use_https = bool(getattr(server_config, "use_https", False))
    skip_verify = bool(getattr(server_config, "https_skip_verify", False))

    # Port resolution. ServerConfig.__post_init__ already auto-flips 5985↔5986
    # but a custom port is preserved as-is.
    port = getattr(server_config, "port", None)
    if not port:
        port = 5986 if use_https else 5985

    if use_https and skip_verify:
        logger.warning(
            "WinRM HTTPS to %s with cert validation DISABLED. "
            "Roll out a real cert ASAP — credential MITM is back on the table.",
            server_config.name,
        )

    password = decrypt_password(server_config.password)

    kwargs = dict(
        port=port,
        username=server_config.username,
        password=password,
        ssl=use_https,
        auth=auth,
        connection_timeout=connection_timeout,
        read_timeout=read_timeout,
    )
    # pypsrp uses cert_validation=False to disable server cert checking; default
    # is True. We expose this knob explicitly so the warning above isn't
    # misleading.
    if use_https:
        kwargs["cert_validation"] = not skip_verify

    return WSMan(server_config.host, **kwargs)


def current_correlation_id() -> str:
    """S3-7 (BL7): return the active request's correlation ID, or generate a
    fresh one if we're outside an HTTP request (collector / scheduler context).

    Used by callers that wrap PowerShell scripts with a prelude so the same
    ID can be:
      * matched in the audit_log row (auto-filled by Database.log_audit)
      * matched in restart_log.run_id (now linked, see set_correlation_id_for_run)
      * grepped in Windows event-log records on the target server (the prelude
        echoes `[CorrId=<id>]` via Write-Information / Write-EventLog)

    A single grep across audit_log + restart_log + on-target Windows event
    message gives the operator the join key — fixing the canonical 'audit log
    says someone restarted DC01 at 23:47, can't tie to the same session that
    did X at 23:31' problem from the Blue Hat audit.
    """
    try:
        from flask import g, has_request_context
        if has_request_context():
            rid = getattr(g, "request_id", None)
            if rid:
                return rid
    except (ImportError, RuntimeError):
        pass
    # Outside Flask context (collector, scheduler, CLI) — synthesise a fresh ID
    # so the joinable key still exists across the three logs.
    import uuid as _uuid
    return _uuid.uuid4().hex


def correlation_id_prelude(corr_id: str | None = None) -> str:
    """Return a PowerShell prelude that exports the correlation ID into the
    target's environment and echoes it via Write-Information.

    Usage::

        from winrm_factory import correlation_id_prelude, current_correlation_id
        cid = current_correlation_id()
        script = correlation_id_prelude(cid) + "\\nGet-Service -Name Spooler"
        wsman = make_wsman(server_config)
        # Run script on wsman; the target now has $Global:PrismCorrId set,
        # and a Write-Information record carrying [CorrId=<cid>] is emitted.

    The prelude is intentionally cheap — one assignment + one Write-Information
    line — so it's safe to inject into every WinRM script.
    """
    if not corr_id:
        corr_id = current_correlation_id()
    # Single-quote the value (PowerShell treats single-quoted strings as
    # verbatim — no interpolation, no $-expansion, no backtick escapes
    # interpreted). The corr_id is hex-only by construction so quoting is
    # belt-and-suspenders, not a security gate.
    safe_id = "".join(c for c in corr_id if c.isalnum() or c == "-")[:64]
    return (
        f"$Global:PrismCorrId = '{safe_id}'\n"
        f"Write-Information \"[PrismCorrId={safe_id}]\" -InformationAction Continue\n"
    )
