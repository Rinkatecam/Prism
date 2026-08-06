"""Regression test for Council-audit H4: /admin/reset-password authorization.

Before the fix, reset_admin_password() gated only on `session['username']`, so
ANY authenticated LDAP user could rotate the tier-0 break-glass admin password
and then seize the backup-admin account. It now fronts the endpoint with the
canonical _require_rbac_admin() guard (backup-admin / wildcard-admin only, and a
no-op when auth is disabled). This test pins that the guard is wired and its
rejection is propagated — the guard's own logic is exercised elsewhere.
"""

from __future__ import annotations

import flask


def _client():
    from app import app
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # exercise authz, not CSRF (covered elsewhere)
    c = app.test_client()
    # Give the request a logged-in session so the first-run check_setup hook
    # (redirects to /setup when no backup admin exists — the state on a fresh CI
    # checkout with no config.json) lets it through to the endpoint. The scenario
    # under test IS an authenticated non-admin user, so this is realistic.
    with c.session_transaction() as sess:
        sess["username"] = "tester"
    return c


def test_reset_password_is_blocked_when_guard_rejects(monkeypatch):
    """A non-admin (guard returns 403) must NOT reach the password rotation."""
    import routes.api._shared as shared
    monkeypatch.setattr(
        shared, "_require_rbac_admin",
        lambda: (flask.jsonify({"ok": False, "error": "forbidden"}), 403),
    )
    r = _client().post("/admin/reset-password",
                       json={"new_password": "Sup3r-Secret!23", "confirm_password": "Sup3r-Secret!23"})
    assert r.status_code == 403, "reset-password must reject non-admins before rotating the password"


def test_reset_password_proceeds_when_guard_allows(monkeypatch):
    """When the guard allows (e.g. auth disabled / backup admin), the endpoint
    proceeds into its normal validation rather than 401/403 on authorization."""
    import routes.api._shared as shared
    monkeypatch.setattr(shared, "_require_rbac_admin", lambda: None)
    r = _client().post("/admin/reset-password",
                       json={"new_password": "", "confirm_password": ""})
    assert r.status_code == 200
    body = r.get_json()
    # Rejected on the empty-password rule, proving it got PAST the auth guard.
    assert body["ok"] is False and "password" in body["error"].lower()
