"""HTTPS health checks verify certificates unless told not to, per check.

THE FINDING THIS CLOSES. `http_check(use_ssl=True)` built its SSL context with
`check_hostname = False` and `verify_mode = CERT_NONE`, unconditionally and
with no way to change it. An HTTPS health check therefore proved that
*something* answered on that port — never that it was the service you meant.
A machine-in-the-middle, a mis-issued certificate, or an expired one all read
as a clean "up". Reported in `docs/DATA_FLOWS.md`.

WHY IT IS A PER-CHECK SETTING AND NOT A GLOBAL ONE. Internal endpoints with
self-signed certificates are ordinary and legitimate, and a monitoring tool
that turns a wave of them red is a tool people switch off. The setting lives
on the individual check because that is the granularity at which the operator
actually knows the answer.

WHY THE DEFAULT IS VERIFY. A default of "don't verify" is the state this
finding is about: silent, invisible, and indistinguishable from a considered
decision. When verification is off it is now off because a row says so, and
the probe reports which mode it ran in — so the weaker setting leaves a trace
in the result instead of living in a constant nobody reads.

WHAT THESE TESTS CANNOT DO. They cannot complete a real TLS handshake against
a bad certificate — that needs a network peer and a CA that will misbehave on
request. What they pin is the security-relevant seam: the context the probe
hands to `urlopen`. `verify_mode`/`check_hostname` ARE the verification; a
context asserting them is asserting the property, not a proxy for it.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from health_checker import _ssl_context            # noqa: E402


def test_an_https_check_verifies_the_certificate_by_default():
    """The whole finding, as one assertion."""
    ctx = _ssl_context(verify_tls=True)
    assert ctx.verify_mode == ssl.CERT_REQUIRED, (
        "an HTTPS health check must validate the certificate chain, or it "
        "proves reachability and calls it health")
    assert ctx.check_hostname is True, (
        "chain validation without hostname checking accepts a valid "
        "certificate issued for somebody else — the half-measure that looks "
        "like verification")


def test_verification_can_be_switched_off_for_one_check():
    """Self-signed internal endpoints are real. The escape hatch is explicit,
    per check, and recorded in a row — not a constant in the source."""
    ctx = _ssl_context(verify_tls=False)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False, (
        "check_hostname must be cleared BEFORE verify_mode is set to NONE; "
        "Python raises if a context still requires hostname checking when "
        "verification is disabled")


def test_the_two_contexts_are_not_the_same_object():
    """A cached or shared context would let one check's opt-out silently
    weaken every other check in the fleet."""
    a, b = _ssl_context(verify_tls=True), _ssl_context(verify_tls=False)
    assert a is not b
    assert _ssl_context(verify_tls=True) is not a


# ── the setting has to survive the whole round trip, or it is decoration ──

def test_the_schema_carries_the_setting_and_defaults_to_verifying(tmp_path):
    """`health_check_config.enabled` is a live example in this repo of a
    column with no writer and an unreachable branch. A setting that the API
    cannot set is the same defect wearing a security label."""
    from database import Database
    db = Database(tmp_path / "t.db")
    cols = {r[1] for r in db._get_conn().execute(
        "PRAGMA table_info(health_check_config)").fetchall()}
    assert "verify_tls" in cols, "no verify_tls column on health_check_config"

    row_id = db.save_health_check_config(
        server_name="SERVER01", check_type="https",
        target_host="server01.example.com", target_port=443)
    cfg = [c for c in db.get_health_check_config("SERVER01") if c["id"] == row_id][0]
    assert cfg["verify_tls"] == 1, "a newly created HTTPS check must verify"


@pytest.mark.parametrize("supplied,expected", [(True, 1), (False, 0),
                                               (1, 1), (0, 0)])
def test_the_setting_round_trips_through_the_writer(tmp_path, supplied, expected):
    from database import Database
    db = Database(tmp_path / "t.db")
    row_id = db.save_health_check_config(
        server_name="SERVER01", check_type="https",
        target_host="server01.example.com", target_port=443,
        verify_tls=supplied)
    cfg = [c for c in db.get_health_check_config("SERVER01") if c["id"] == row_id][0]
    assert cfg["verify_tls"] == expected


def test_an_update_can_turn_verification_off_and_back_on(tmp_path):
    """The ON CONFLICT branch is a separate code path from the INSERT, and it
    is the one an operator actually uses — they create a check, watch it fail
    on a self-signed certificate, and edit it. A setting that only applies on
    first insert would strand them."""
    from database import Database
    db = Database(tmp_path / "t.db")
    args = dict(server_name="SERVER01", check_type="https",
                target_host="server01.example.com", target_port=443)

    db.save_health_check_config(**args, verify_tls=True)
    db.save_health_check_config(**args, verify_tls=False)
    cfg = db.get_health_check_config("SERVER01")[0]
    assert cfg["verify_tls"] == 0, "the update path ignored verify_tls"

    db.save_health_check_config(**args, verify_tls=True)
    cfg = db.get_health_check_config("SERVER01")[0]
    assert cfg["verify_tls"] == 1, "verification could be turned off but not on"


# ── the two links a mutation proved were untested ────────────────────────
#
# Both of these were found by aiming a mutation at them and watching NOTHING
# fail. The DB-layer tests above cover the store; they cannot see the API
# parsing the payload or the runner passing the stored value to the probe, and
# a setting is only as strong as the weakest link that carries it.

@pytest.mark.parametrize("payload,expected", [
    ({}, True),                          # absent — the case that matters
    ({"verify_tls": None}, True),        # explicitly null
    ({"verify_tls": True}, True),
    ({"verify_tls": False}, False),
    ({"verify_tls": 0}, False),
    ({"verify_tls": 1}, True),
])
def test_an_omitted_field_cannot_weaken_the_check(payload, expected):
    """`bool(data.get("verify_tls"))` reads the same and does the opposite.

    A missing key becomes None becomes False, so every check created by a
    client that does not send the field would silently stop validating
    certificates — the original defect, reintroduced at the API layer.
    """
    from routes.api.health import _verify_tls_from_payload
    assert _verify_tls_from_payload(payload) is expected


def test_the_runner_passes_the_stored_setting_to_the_probe(tmp_path, monkeypatch):
    """The link between the row and the socket.

    `_run_health_checks` imports the probes inside the function body, so the
    patch target is the health_checker module rather than the runner's
    namespace. Without this test the runner could drop the argument entirely
    and every suite above would still pass.
    """
    import health_checker
    from database import Database
    from healthchecks import _run_health_checks

    seen = []

    def fake_http_check(host, port, **kw):
        seen.append(kw.get("verify_tls"))
        return {"status": "up", "response_time_ms": 1.0,
                "http_status": 200, "error": None}

    monkeypatch.setattr(health_checker, "http_check", fake_http_check)

    db = Database(tmp_path / "t.db")
    db.save_health_check_config(server_name="SERVER01", check_type="https",
                                target_host="a.example.com", target_port=443,
                                verify_tls=False)
    db.save_health_check_config(server_name="SERVER02", check_type="https",
                                target_host="b.example.com", target_port=443,
                                verify_tls=True)
    _run_health_checks(db, {})

    assert sorted(seen, key=bool) == [False, True], (
        f"the runner did not carry each check's own verify_tls through; "
        f"probe saw {seen}")


def test_editing_one_field_does_not_blank_the_others(tmp_path):
    """The upsert must COALESCE, not assign.

    Editing a single field is the normal shape of an edit, and the route sends
    `None` for anything the caller omitted. A bare `= excluded.http_path` wrote
    that None over a configured path and status — so the very action this
    setting requires (turn verification off on an existing check) destroyed the
    rest of the check's configuration.
    """
    from database import Database
    db = Database(tmp_path / "t.db")
    key = dict(server_name="SERVER01", check_type="https",
               target_host="server01.example.com", target_port=443)

    db.save_health_check_config(**key, http_path="/healthz",
                                expected_status=204, name="API liveness",
                                verify_tls=True)

    # The partial edit an operator would actually make.
    db.save_health_check_config(**key, http_path=None, expected_status=None,
                                name=None, verify_tls=False)

    cfg = db.get_health_check_config("SERVER01")[0]
    assert cfg["verify_tls"] == 0, "the edit did not take effect"
    assert cfg["http_path"] == "/healthz", "http_path was blanked by a partial edit"
    assert cfg["expected_status"] == 204, "expected_status was blanked"
    assert cfg["name"] == "API liveness", "name was blanked"


def test_a_null_verify_tls_still_verifies(tmp_path, monkeypatch):
    """`bool(cfg.get("verify_tls", 1))` would read False here.

    A dict default only applies when the KEY IS ABSENT, so a present-but-None
    value silently selects the weaker behaviour. Pinned at the runner because
    that is where the pattern was used, and the failure leaves no trace.
    """
    import health_checker
    from healthchecks import _run_health_checks

    seen = []
    monkeypatch.setattr(health_checker, "http_check",
                        lambda host, port, **kw: (seen.append(kw.get("verify_tls")),
                                                  {"status": "up", "response_time_ms": 1.0,
                                                   "http_status": 200, "error": None})[1])

    # A real Database so every call the runner makes is the real one; only the
    # config READ is replaced, because the schema's NOT NULL constraint makes a
    # genuine NULL unstorable — which is exactly why this is the defensive case
    # rather than a live bug, and why it still has to be pinned.
    from database import Database
    db = Database(tmp_path / "t.db")
    monkeypatch.setattr(db, "get_health_check_config", lambda *a, **k: [
        {"id": 1, "name": "", "server_name": "SERVER01", "check_type": "https",
         "target_host": "a.example.com", "target_port": 443, "http_path": "/",
         "expected_status": 200, "enabled": 1, "verify_tls": None}])

    _run_health_checks(db, {})
    assert seen == [True], (
        f"a NULL verify_tls selected {seen} — absent or null must mean verify")


def test_the_probe_reports_which_mode_it_ran_in():
    """An unverified result must be distinguishable from a verified one by
    something other than reading the configuration. This is what stops the
    weaker setting from being invisible again."""
    import inspect
    src = inspect.getsource(__import__("health_checker").http_check)
    assert "tls_verified" in src, (
        "http_check does not report tls_verified in its result, so a caller "
        "cannot tell a verified 'up' from an unverified one")


# ── the UI carrier ────────────────────────────────────────────────────────
#
# The setting existed at four layers and was reachable from none of them,
# because the form that creates health checks had no field for it. A security
# default that an operator cannot opt out of through the product is not a
# setting, it is a breakage with a workaround — and the workaround was a
# hand-crafted API call. These pin the carrier.

def _servers_html() -> str:
    return (PROJECT_ROOT / "templates" / "servers.html").read_text(encoding="utf-8")


def test_the_form_offers_the_setting():
    html = _servers_html()
    assert 'id="hc-new-verify-tls"' in html, (
        "no verify_tls control in the health-check form; the setting is then "
        "reachable only by hand-crafting an API call")
    assert 'id="hc-new-verify-wrap"' in html
    assert "checked" in html.split('id="hc-new-verify-tls"')[1][:120], (
        "the checkbox must default to checked — the secure default has to be "
        "the one an operator gets without thinking about it")


def test_the_form_sends_the_setting_when_saving():
    html = _servers_html()
    assert "verify_tls: verifyTls" in html, (
        "saveNewHealthCheck does not send verify_tls, so the control is "
        "decorative and every check is created verifying regardless")


def test_editing_a_check_repopulates_the_setting():
    """Editing DELETES the row and re-creates it from this form.

    A checkbox left at its default would silently turn verification back on
    for an endpoint the operator deliberately exempted — on any unrelated
    edit, such as fixing a typo in the name.
    """
    html = _servers_html()
    assert "document.getElementById('hc-new-verify-tls').checked = hc.verify_tls !== 0;" in html, (
        "the edit path does not repopulate verify_tls")


def test_saving_resets_the_form_to_verifying():
    html = _servers_html()
    assert "document.getElementById('hc-new-verify-tls').checked = true;" in html, (
        "the form keeps the last opt-out, so the NEXT check silently inherits "
        "an exemption meant for a different endpoint")


def test_the_test_button_probes_the_same_way_the_saved_check_will():
    """Otherwise an operator tunes the endpoint against one behaviour and
    deploys the other, and 'it worked when I tested it' is the least
    debuggable complaint a monitoring tool can produce."""
    html = _servers_html()
    probe = html.split("'/api/health-checks/probe'")[1][:600]
    assert "verify_tls" in probe, "the ad-hoc probe ignores the checkbox"

    import inspect
    import routes.api.health as h
    src = inspect.getsource(h.probe_health_check)
    assert "_verify_tls_from_payload" in src, (
        "the probe endpoint does not apply the same absent-means-verify guard "
        "as the save endpoint")


def test_an_unverified_check_is_visible_in_the_list():
    """The reason this is a per-check row rather than a constant is that the
    weaker setting leaves a trace. If you must open the edit form to discover
    it, it is as invisible as the hardcoded CERT_NONE it replaced."""
    html = _servers_html()
    assert "tlsUnverified" in html
    assert "hc_tls_unverified" in html, "no badge for a non-verifying check"


def test_the_new_strings_exist_in_every_locale():
    from i18n import TRANSLATIONS
    keys = ["hc_verify_tls", "hc_verify_tls_hint",
            "hc_tls_unverified", "hc_tls_unverified_hint"]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated: " + ", ".join(missing)


def test_an_absent_name_is_not_the_same_as_an_empty_one():
    """The coercion that defeated COALESCE, pinned at the layer that does it.

    Coercing a MISSING name to an empty string produces a value that is not
    NULL, so the upsert's COALESCE cannot protect it and a partial edit still
    blanks the stored name. The DB-level test could not see this: it called
    save_health_check_config directly and never went through the route. Found
    by a live round trip against the running app, after the DB test passed.

    PARSED, NOT GREPPED. The first version read the raw source and failed on
    the comment beside the fix, which quotes the very expression it forbids --
    the fifth time a text-scanning check in this repository has fired on its
    own documentation, and the cheapest way to make it pass would have been to
    delete the explanation. `ast.unparse` round-trips the code and drops every
    comment; the docstring is stripped separately because it is a real node.
    """
    import ast
    import inspect
    import textwrap
    import routes.api.health as h

    tree = ast.parse(textwrap.dedent(inspect.getsource(h.save_health_check_config)))
    fn = tree.body[0]
    fn.body = [n for n in fn.body
               if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                       and isinstance(n.value.value, str))]
    code = ast.unparse(fn)

    assert "(data.get('name') or '').strip()" not in code, (
        "an absent name is coerced to empty string again, which overwrites "
        "the stored name on any partial update")
    assert "isinstance(_raw_name, str)" in code, (
        "the route no longer distinguishes an absent name from an empty one")
