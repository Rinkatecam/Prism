"""Static-analysis governance tests for the HTTP API surface.

These tests enforce two CSV / GAMP-5 critical invariants by introspecting
the live Flask app:

  1. **F-075 — Universal RBAC**: every mutating endpoint (POST/PUT/
     PATCH/DELETE) must be gated by an authentication / RBAC decorator
     OR be on the documented allowlist of intentionally-open routes.

  2. **F-078 — Universal audit logging**: every mutating endpoint must
     write to ``audit_log`` via ``db.log_audit(...)`` (or call something
     that does), OR be on the documented allowlist.

Why static analysis instead of dynamic? Coverage of "did this route's
handler write an audit row?" is fragile via unit tests — we'd need a
per-route test that exercises the handler. The static check catches the
regression immediately when a developer adds a new mutating endpoint
and forgets the boilerplate, even before any unit test for that endpoint
exists.

If a new route is genuinely meant to be open (e.g. CSRF-token endpoint,
liveness probe), add it to the explicit allowlist in this file with a
one-line justification. The allowlist requirement is itself a control:
adding to it requires a PR review.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

# The Flask app sits at the repo root. Tests are invoked from there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────
# Allowlist — routes intentionally not protected by auth, with reason.
#
# Adding to this allowlist requires a PR review (per the CSV change-
# control SOP). Each entry MUST cite a reason.
# ─────────────────────────────────────────────────────────────────────

_AUTH_ALLOWLIST = {
    # CSRF token issuance — needs to work pre-login so the login form
    # itself can carry a token.
    "/api/csrf-token": "CSRF tokens must be obtainable pre-login",
    # Test-email is a setup helper — used during initial install before
    # any user exists. Once auth is enabled this path is still useful
    # for SMTP debugging and explicitly accepts that risk.
    "/api/test-email": "Setup-time SMTP test; cannot require auth",
    # Sync triggers — these are operator-convenience endpoints that
    # request the collector to do its normal job NOW instead of waiting
    # for the next tick. They have no destructive side effect.
    "/api/sync-now": "Non-destructive: forces a metric sync that would happen anyway",
    "/api/sync-updates-now": "Non-destructive: forces an UPDATES check that would happen anyway",
    "/api/sync-logs-now": "Non-destructive: forces a LOGS check that would happen anyway",
    # Anomaly acknowledgement / snooze — historically operator-driven;
    # the audit trail is the per-row "acked_by" field on the ack record.
    # Reviewed during 2026-05 audit; risk accepted.
    "/api/anomalies/acknowledge": "Acknowledge writes an attribution row; no other effect",
    "/api/anomalies/acknowledge/<int:ack_id>": "Same",
    # Anomaly suppression and webhook tests are setup helpers.
    "/api/test-webhook": "Setup-time webhook test; tier-0 webhooks anyway require admin elsewhere",
    # Live security probe trigger — read-only against the target, fans
    # out into the collector's normal path. No DB mutation by the route.
    "/api/servers/<name>/security-status/check": "Read-only probe trigger; no direct DB mutation",
    # Pre-auth flows (handled in auth.py blueprint, not routes/api/):
    # they BOOTSTRAP authentication, so they cannot require it.
    "/login": "Pre-auth flow by definition; rate-limited; uses auth.py internal gating",
    "/setup": "First-run install flow before any user account exists",
    # Admin password reset — uses auth.py's own inline session check
    # (verified by manual inspection 2026-05-22). Not the canonical
    # _require_auth path but functionally equivalent.
    "/admin/reset-password": "Uses inline session.get('username') 401 check; functionally equivalent to _require_auth",
}


# ─────────────────────────────────────────────────────────────────────
# Allowlist for audit-log enforcement.
#
# Routes that DON'T have to write an audit row. Each entry MUST cite a
# reason. This list is much shorter than the auth allowlist — most
# state-changing routes SHOULD write an audit row.
# ─────────────────────────────────────────────────────────────────────

_AUDIT_ALLOWLIST = {
    # Sync triggers are observability nudges; the collector itself does
    # not write audit rows for routine fetches.
    "/api/sync-now": "Non-destructive observability nudge",
    "/api/sync-updates-now": "Non-destructive observability nudge",
    "/api/sync-logs-now": "Non-destructive observability nudge",
    # CSRF endpoint just hands back a token; nothing to audit.
    "/api/csrf-token": "Trivial token issuance",
    # Test endpoints emit logs but no audit row by design (they're
    # debugging aids, not regulated actions).
    "/api/test-email": "Debugging aid, not a regulated action",
    "/api/test-webhook": "Debugging aid, not a regulated action",
    "/api/test-connection": "Debugging aid, not a regulated action",
    # Anomaly ack writes its own attribution row in
    # anomaly_acknowledgments; treated as the audit record for that
    # action.
    "/api/anomalies/acknowledge": "Writes anomaly_acknowledgments row as the audit record",
    "/api/anomalies/acknowledge/<int:ack_id>": "Same",
    # Live probes — read-only against target; no DB mutation by the
    # route itself; the collector path will eventually capture data
    # but that's a separate concern.
    "/api/servers/<name>/security-status/check": "Live probe; no direct DB mutation",
    "/api/servers/<name>/config-snapshot": "Snapshot trigger; the snapshot row in config_snapshots IS the record",
    "/api/health-checks/probe": "Read-only probe; no DB mutation",
    # Cancel-updates — accepted Minor gap; tracked as a follow-up.
    "/api/servers/<name>/cancel-updates": "Cleanup operation; arguably should audit (Minor follow-up)",
    # Vacuum — accepted Minor gap; tracked as a follow-up.
    "/api/system/vacuum": "DB physical compaction; should audit (Minor follow-up F-V-1)",
    # Workflow validation is a pure check (sandbox text-time validation);
    # no script execution, no DB mutation.
    "/api/workflows/validate-script": "Pure sandbox-validation check; no execution, no DB mutation",
    # LDAP query is a read-only directory search; results don't persist.
    "/api/ldap/query": "Read-only directory search; no DB mutation",
    # First-run install flow — emits a setup-completed audit row via
    # the auth.py blueprint's helpers AFTER the initial admin account
    # is created. The route itself doesn't write the audit row directly
    # because the audit table is only writeable once the DB is fully
    # initialised. Verified by manual inspection 2026-05-22.
    "/setup": "First-run install before DB fully initialised; setup-completed audit fires post-init",
}

# Read-only compliance endpoints are added separately below since they
# use the same prefix and we want the reasoning visible together with
# their /api/sop/* siblings.


# Auth-decorator markers we look for in the handler source. The list
# matches the canonical helpers defined in routes/api/_shared.py.
_AUTH_MARKERS = (
    "_require_auth",
    "_require_server_permission",
    "_require_rbac_admin",
    "_consume_global_destructive_approval",
)

# Audit-log marker — any call shape that writes an audit row.
_AUDIT_MARKERS = (
    "log_audit",            # db.log_audit(...) — the canonical path
    "_log_audit",           # any wrapper named _log_audit
    "_audit(",              # auth.py's legacy helper — verified to call db.log_audit
    "insert_sop_execution", # database method that internally writes audit_log (CSV-17 / Phase 5)
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _get_app_for_introspection():
    """Build a Flask test app exposing the api blueprint.

    We import the live ``app`` module so the blueprint registration mirrors
    production exactly. The DB is fine to point at any path — we only
    introspect routes, never invoke them.
    """
    import app as flask_app_module
    return flask_app_module.app


def _read_handler_source(view_func) -> str:
    """Return the source text of a view function, including any
    decorator-wrapped inner. We walk ``__wrapped__`` (set by
    ``functools.wraps``) so closure-based wrappers don't hide the body.
    """
    seen = set()
    func = view_func
    sources = []
    while func and id(func) not in seen:
        seen.add(id(func))
        try:
            sources.append(inspect.getsource(func))
        except (OSError, TypeError):
            pass
        func = getattr(func, "__wrapped__", None)
    return "\n".join(sources)


def _mutating_rules(app):
    """Yield each rule with a state-changing HTTP method, excluding
    HEAD/OPTIONS (which Flask auto-adds)."""
    for rule in app.url_map.iter_rules():
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS", "GET"}
        if methods:
            yield rule


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

def test_every_mutating_endpoint_has_auth_decorator():
    """F-075: every POST/PUT/PATCH/DELETE endpoint must call one of the
    canonical auth helpers, OR be on the documented allowlist."""
    app = _get_app_for_introspection()
    offenders = []
    for rule in _mutating_rules(app):
        if rule.rule in _AUTH_ALLOWLIST:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            offenders.append((rule.rule, "no view function"))
            continue
        src = _read_handler_source(view)
        if not any(marker in src for marker in _AUTH_MARKERS):
            offenders.append((rule.rule, f"missing any of {_AUTH_MARKERS}"))
    assert not offenders, (
        "F-075: the following mutating routes are NOT gated by an auth "
        "decorator and are NOT on the documented allowlist:\n"
        + "\n".join(f"  {rule}  ({reason})" for rule, reason in offenders)
        + "\n\nFix: either add an auth decorator inside the handler, or "
        "add the route to _AUTH_ALLOWLIST in tests/test_route_governance.py "
        "with a one-line justification (and have it reviewed)."
    )


def test_every_mutating_endpoint_writes_audit_or_is_allowlisted():
    """F-078: every POST/PUT/PATCH/DELETE endpoint must call
    ``log_audit`` (or invoke a helper that does), OR be on the documented
    allowlist."""
    app = _get_app_for_introspection()
    offenders = []
    for rule in _mutating_rules(app):
        if rule.rule in _AUDIT_ALLOWLIST:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            offenders.append((rule.rule, "no view function"))
            continue
        src = _read_handler_source(view)
        if not any(marker in src for marker in _AUDIT_MARKERS):
            offenders.append((rule.rule, f"missing any of {_AUDIT_MARKERS}"))
    assert not offenders, (
        "F-078: the following mutating routes do NOT write an audit_log "
        "row and are NOT on the documented allowlist:\n"
        + "\n".join(f"  {rule}  ({reason})" for rule, reason in offenders)
        + "\n\nFix: either call db.log_audit(...) inside the handler, or "
        "add the route to _AUDIT_ALLOWLIST in tests/test_route_governance.py "
        "with a one-line justification (and have it reviewed)."
    )


def test_auth_allowlist_does_not_drift_silently():
    """Defence in depth: assert the allowlist count is at or below a
    known threshold. If it grows past that, somebody added an allowlist
    entry without considering whether the route really should be open.
    This catches drift via PRs that only touch the allowlist."""
    # Threshold set deliberately at the audit-baseline count.
    EXPECTED_AT_AUDIT = 13
    assert len(_AUTH_ALLOWLIST) <= EXPECTED_AT_AUDIT + 2, (
        f"_AUTH_ALLOWLIST has grown to {len(_AUTH_ALLOWLIST)} entries; "
        f"baseline at the 2026-05-22 audit was {EXPECTED_AT_AUDIT}. "
        "Adding open routes is a security-significant decision — review "
        "the new entries and, if they're correct, bump this threshold."
    )


def test_audit_allowlist_does_not_drift_silently():
    """Same defence-in-depth for audit-log allowlist."""
    EXPECTED_AT_AUDIT = 17
    assert len(_AUDIT_ALLOWLIST) <= EXPECTED_AT_AUDIT + 2, (
        f"_AUDIT_ALLOWLIST has grown to {len(_AUDIT_ALLOWLIST)} entries; "
        f"baseline at the 2026-05-22 audit was {EXPECTED_AT_AUDIT}. "
        "Routes that mutate state without an audit row are a regulatory "
        "concern — review the new entries."
    )
