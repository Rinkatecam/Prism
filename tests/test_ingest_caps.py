"""Collector-side caps on untrusted ingest (collector audit finding 1, HIGH).

THE DEFECT. Prism's limits on what a monitored host may send lived only in the
PowerShell it asks that host to run — 30 rows, 200 characters. A compromised
host is free to ignore the script and answer with whatever it likes, and every
layer above took the answer as given: the check returned it, the writer stored
it, and there was no `MaxEvents` on the failed-login query at all. One hostile
box could bloat a database that every other server writes through a
process-global lock, so the blast radius of one owned machine was the whole
fleet's monitoring.

THE SHAPE OF THE FIX, and the two decisions inside it:

  * an OVERSIZE PAYLOAD IS REJECTED, not truncated. Truncated JSON does not
    parse, so truncating would surface a host streaming megabytes as "Bad JSON"
    — the symptom furthest from the cause. A rejection names what happened.
  * TOO MANY ROWS ARE TRUNCATED, not rejected. Thirty good rows arriving with
    ten thousand junk ones should still yield the thirty. Truncation keeps the
    first N, so a hostile host can decide WHICH rows survive; that is accepted
    deliberately. The cap bounds storage, and no cap can make a compromised
    host tell the truth.

Everything capped is COUNTED and reported on /api/system/health, because
silent discarding is how a monitoring tool loses trust — the same rule the
Information-level ingest filter already follows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ingest_caps                                        # noqa: E402


@pytest.fixture(autouse=True)
def _reset_counters():
    ingest_caps.reset_counters()
    yield
    ingest_caps.reset_counters()


# ── resolving the caps ────────────────────────────────────────────────────

def test_the_defaults_apply_when_nothing_is_configured():
    caps = ingest_caps.resolve({})
    assert caps == ingest_caps.DEFAULTS
    assert ingest_caps.resolve(None) == ingest_caps.DEFAULTS


def test_a_site_can_raise_a_cap():
    """A busy domain controller legitimately produces more failed logins in
    fifteen minutes than a print server does in a week."""
    caps = ingest_caps.resolve({"ingest_caps": {"max_failed_logins": 5000}})
    assert caps["max_failed_logins"] == 5000
    assert caps["max_rows_per_check"] == ingest_caps.DEFAULTS["max_rows_per_check"]


def test_a_cap_of_zero_is_floored_rather_than_honoured():
    """Zero would mean "discard everything", which is a monitoring outage
    wearing a config key's name. There is deliberately no way to configure the
    caps into silence."""
    caps = ingest_caps.resolve({"ingest_caps": {"max_rows_per_check": 0,
                                                "max_message_chars": -50}})
    assert caps["max_rows_per_check"] == 1
    assert caps["max_message_chars"] == 1


def test_an_unreadable_cap_falls_back_to_its_default():
    """config.json is hand-edited. A typo must degrade to the default, not
    raise — a bad character in one key cannot be allowed to stop ingest."""
    caps = ingest_caps.resolve({"ingest_caps": {"max_rows_per_check": "lots"}})
    assert caps["max_rows_per_check"] == ingest_caps.DEFAULTS["max_rows_per_check"]


def test_an_unknown_key_is_ignored():
    caps = ingest_caps.resolve({"ingest_caps": {"max_bananas": 3}})
    assert "max_bananas" not in caps


# ── the payload ceiling ───────────────────────────────────────────────────

def test_a_normal_payload_is_accepted():
    assert ingest_caps.payload_ok('{"cpu": 12}', server="SRV") is True


def test_a_payload_at_exactly_the_limit_is_accepted():
    caps = {**ingest_caps.DEFAULTS, "max_payload_chars": 10}
    assert ingest_caps.payload_ok("0123456789", caps=caps, server="SRV") is True


def test_an_oversize_payload_is_rejected_and_counted():
    caps = {**ingest_caps.DEFAULTS, "max_payload_chars": 10}
    assert ingest_caps.payload_ok("0123456789X", caps=caps, server="SRV") is False
    assert ingest_caps.snapshot()["payloads_rejected"] == 1


def test_an_empty_payload_is_not_treated_as_oversize():
    assert ingest_caps.payload_ok("", server="SRV") is True
    assert ingest_caps.snapshot()["payloads_rejected"] == 0


# ── row and field caps ────────────────────────────────────────────────────

def _log(msg="ok", n=1):
    return [{"source": "System", "level": "Error", "event_id": 7,
             "time": "2026-08-19T10:00:00Z", "message": msg} for _ in range(n)]


def test_a_compliant_payload_passes_through_unchanged():
    """The PowerShell caps at 30 rows and 200 characters. A host that honours
    them must never be truncated, or the cap becomes a data-loss bug for every
    well-behaved machine on the fleet.

    Sized at EXACTLY the caps, not comfortably inside them. A message of 200
    against a limit of 400 cannot see an off-by-one at the boundary, and a
    boundary a test never visits is a boundary nobody has checked — a `<` where
    `<=` belongs would silently clip one character off every full-length row.
    """
    caps = ingest_caps.DEFAULTS
    rows = _log("x" * caps["max_message_chars"], caps["max_rows_per_check"])
    out = ingest_caps.cap_log_rows(rows, server="SRV")
    assert out == rows
    assert ingest_caps.snapshot()["rows_dropped"] == 0
    assert ingest_caps.snapshot()["fields_truncated"] == 0


def test_rows_beyond_the_cap_are_dropped_and_counted():
    caps = {**ingest_caps.DEFAULTS, "max_rows_per_check": 5}
    out = ingest_caps.cap_log_rows(_log("ok", 12), caps=caps, server="SRV")
    assert len(out) == 5
    assert ingest_caps.snapshot()["rows_dropped"] == 7


def test_an_overlong_message_is_truncated_and_counted():
    caps = {**ingest_caps.DEFAULTS, "max_message_chars": 20}
    out = ingest_caps.cap_log_rows(_log("y" * 500), caps=caps, server="SRV")
    assert out[0]["message"] == "y" * 20
    assert ingest_caps.snapshot()["fields_truncated"] == 1


def test_capping_does_not_mutate_the_caller_s_rows():
    """The collector logs and re-reads its own payloads. Editing them in place
    would make the log say something different from what was stored."""
    rows = _log("z" * 500)
    caps = {**ingest_caps.DEFAULTS, "max_message_chars": 10}
    ingest_caps.cap_log_rows(rows, caps=caps, server="SRV")
    assert rows[0]["message"] == "z" * 500


def test_a_malformed_row_does_not_raise():
    """A hostile host sends whatever it likes, including a list of strings and
    a null. Ingest must survive it — a crash here stalls a worker thread."""
    out = ingest_caps.cap_log_rows(["not a dict", None, {"message": "fine"}],
                                   server="SRV")
    assert {"message": "fine"} in out


def test_every_host_controlled_field_of_a_failed_login_is_capped():
    """The account name, the workstation and the process label all come from
    the target's own event XML. Capping only the obvious one leaves the others
    as the flood path."""
    caps = {**ingest_caps.DEFAULTS, "max_message_chars": 3}
    # `source_port` is a CAPPED field that a target may send as an int, so it is
    # the case that exercises the type guard. The limit is deliberately shorter
    # than the number's string form: with a longer limit, "left alone" and
    # "stringified, then found short enough" are indistinguishable. `event_id`
    # is not on the capped list at all, so it proves nothing here — which is
    # what a blind mutation had to point out before this test could see it.
    row = {"account_name": "a" * 300, "workstation": "w" * 300,
           "process_name": "p" * 300, "source_ip": "1.2",
           "source_port": 54321, "event_id": 4625}
    out = ingest_caps.cap_failed_logins([row], caps=caps, server="SRV")[0]
    assert out["account_name"] == "a" * 3
    assert out["workstation"] == "w" * 3
    assert out["process_name"] == "p" * 3
    assert out["source_ip"] == "1.2", "a short field was truncated anyway"
    assert out["source_port"] == 54321, "an int field was stringified and clipped"
    assert out["event_id"] == 4625


def test_failed_logins_use_their_own_row_cap():
    caps = {**ingest_caps.DEFAULTS, "max_failed_logins": 3,
            "max_rows_per_check": 99}
    out = ingest_caps.cap_failed_logins(
        [{"account_name": "u", "event_id": 4625}] * 10, caps=caps, server="SRV")
    assert len(out) == 3


def test_the_counters_are_cumulative():
    caps = {**ingest_caps.DEFAULTS, "max_rows_per_check": 1}
    ingest_caps.cap_log_rows(_log("a", 3), caps=caps, server="A")
    ingest_caps.cap_log_rows(_log("a", 3), caps=caps, server="B")
    assert ingest_caps.snapshot()["rows_dropped"] == 4


# ── the seams: the caps have to be ON the untrusted paths ─────────────────

def test_the_check_layer_caps_what_the_target_returned(monkeypatch):
    """The audit's actual finding: `check_logs` returned whatever came back.
    The PowerShell limit is advisory — this is the one a compromised host
    cannot ignore."""
    from collector_v2 import checks
    flood = [{"source": "S", "level": "Error", "event_id": 1,
              "time": "2026-08-19T10:00:00Z", "message": "m" * 5000}
             for _ in range(4000)]
    monkeypatch.setattr(checks, "_run_ps",
                        lambda *a, **k: (True, __import__("json").dumps(flood),
                                         None, None))
    ok, data, err, kind = checks.check_logs(_Server(), pool=None)
    assert ok
    assert len(data) <= ingest_caps.DEFAULTS["max_rows_per_check"]
    assert all(len(r["message"]) <= ingest_caps.DEFAULTS["max_message_chars"]
               for r in data)


def test_the_winrm_reader_refuses_a_giant_payload_before_parsing(monkeypatch):
    """Finding 4. One host streaming gigabytes inside its deadline can OOM a
    worker, and `json.loads` is where the memory goes."""
    from collector_v2 import checks

    class _FakePS:
        had_errors = False
        streams = type("S", (), {"error": []})()

        def __init__(self, pool):
            pass

        def add_script(self, script):
            pass

        def invoke(self):
            return ["x" * (ingest_caps.DEFAULTS["max_payload_chars"] + 1)]

    monkeypatch.setitem(sys.modules, "pypsrp.powershell",
                        type("M", (), {"PowerShell": _FakePS})())
    ok, raw, err, kind = checks._run_ps(None, "script", _Server())
    assert ok is False
    assert kind == "oversize"
    assert raw == ""
    assert ingest_caps.snapshot()["payloads_rejected"] == 1


def test_the_writer_caps_too_even_when_the_collector_did_not(tmp_db):
    """Defence in depth. `insert_logs` is a public method and the collector is
    not its only possible caller — a CSV import or a future integration must
    not be able to write an unbounded row either."""
    tmp_db.insert_logs("SRV", _log("q" * 9000, 400),
                       caps={**ingest_caps.DEFAULTS, "max_rows_per_check": 10,
                             "max_message_chars": 50})
    rows = [dict(r) for r in tmp_db._get_conn().execute(
        "SELECT * FROM logs WHERE server_name = 'SRV'").fetchall()]
    assert len(rows) == 10
    assert all(len(r["message"]) <= 50 for r in rows)


def test_the_failed_login_writer_caps_too(tmp_db):
    tmp_db.insert_failed_logins(
        "SRV",
        [{"timestamp": f"2026-08-19T10:00:{i:02d}Z", "account_name": "u" * 900,
          "event_id": 4625, "source_ip": "1.2.3.4"} for i in range(30)],
        caps={**ingest_caps.DEFAULTS, "max_failed_logins": 4,
              "max_message_chars": 12})
    rows = [dict(r) for r in tmp_db._get_conn().execute(
        "SELECT * FROM failed_logins WHERE server_name = 'SRV'").fetchall()]
    assert len(rows) == 4
    assert all(len(r["account_name"]) <= 12 for r in rows)


def test_the_failed_login_query_asks_for_a_bounded_number_of_events():
    """The PowerShell side of the same finding. `Get-WinEvent` with no
    `-MaxEvents` will happily return every 4625 in the window, and a
    brute-force flood makes that number unbounded before the collector ever
    sees a row.

    Asserted against the INVOCATION LINES, not the whole constant. A plain
    substring check passed while the flag was absent, because the comment
    explaining the flag also contains its name — the same self-documentation
    trap that has bitten text-scanning checks in this repo repeatedly, in the
    mirror-image direction: here the prose masked a removal instead of
    triggering a false positive. Comment lines are excluded and every real
    Get-WinEvent call must carry the bound.
    """
    from collector_v2.scripts import PS_COLLECT_FAILED_LOGINS
    calls = [ln for ln in PS_COLLECT_FAILED_LOGINS.splitlines()
             if "Get-WinEvent" in ln and not ln.lstrip().startswith("#")]
    assert calls, "no Get-WinEvent invocation found at all"
    for line in calls:
        assert "-MaxEvents" in line, f"unbounded event query: {line.strip()}"


def test_the_shipped_config_defaults_match_the_module_s():
    """Two sources of truth for the same numbers is how a "configured" cap ends
    up differing from the enforced one. The config block exists so an operator
    can SEE the caps; this pins it to what actually runs."""
    from config_manager import ConfigManager
    shipped = ConfigManager._DEFAULT_SETTINGS["ingest_caps"]
    assert shipped == ingest_caps.DEFAULTS


def test_the_health_endpoint_reports_what_the_caps_discarded():
    """Silent capping is indistinguishable from a collector that is quietly
    broken. A non-zero `payloads_rejected` is worth an operator's attention
    whether the host is hostile or merely misconfigured — so it has to be
    somewhere an operator looks."""
    from unittest.mock import patch
    from datetime import datetime, timezone
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    now_iso = datetime.now(timezone.utc).isoformat()
    with client.session_transaction() as sess:
        sess["username"] = "caps_test_user"
        sess["login_time"] = now_iso
        sess["last_activity"] = now_iso
        sess["remember_me"] = False
    with patch("routes.api.health._require_auth", return_value=None):
        body = client.get("/api/system/health").get_json()
    reported = body["db"]["ingest_caps"]
    assert set(reported) == set(ingest_caps.snapshot())


class _Server:
    name = "SRV"
    host = "srv.example.invalid"
