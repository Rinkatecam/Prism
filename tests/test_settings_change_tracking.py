"""The Settings page's "have you changed anything?" mechanism.

It has failed twice, in the same way, six weeks apart. Both times a field was
on the page and missing from a hand-maintained list, so the sticky Save bar
never opened for it, the save path that would have written it correctly never
fired, and the operator's edit vanished on navigation. Nothing could notice:
every test passed, the field rendered, the change listener was even bound.

  * The first was `update_check_interval_minutes`, recorded in
    docs/plans/SETTINGS_CONSOLIDATION_PLAN.md row 3 and fixed by adding the
    field to three lists.
  * The second was the ENTIRE scheduled-reports block — eight controls, all
    carrying the marker class, none of them in the compare list. Measured on
    the rendered page: 43 controls marked, 35 compared.

The second one is why this file exists. Adding a field to three lists fixes
one instance and leaves the mechanism that produced it, and "remember to
update three lists" is not a mechanism — it is a hope. The lists are gone:
capture, compare and revert all walk the controls that carry the bucket's
marker class, which settings.html's own section comment already described as
"the JS change-detection bucket".

WHAT THESE ARE BLIND TO:

  * They read the template. That the Save bar actually opens was measured in
    the running app, per control, before and after the rewrite — including
    the discovery that the bar's `translate-y-full` is removed inside a
    requestAnimationFrame, which never fires in the browser pane, so the
    first measurement reported the CONTROL case as broken too. `hidden` is
    removed synchronously and is the signal that works there.
  * They cannot tell whether a bucket assignment is semantically right. The
    one case that is asserted below — scheduled reports needing no restart —
    is asserted against the periodics job that re-reads settings, not
    against the marker class alone.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = PROJECT_ROOT / "templates" / "settings.html"


def _markup() -> str:
    """settings.html with comments AND scripts removed.

    Scripts are stripped because the tracker's own source mentions the ids it
    no longer lists, and because a control built from a JS string is not part
    of the server-rendered form this scan is about."""
    src = SETTINGS.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    return re.sub(r"<script\b.*?</script>", " ", src, flags=re.S | re.I)


def _script() -> str:
    src = SETTINGS.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    body = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S | re.I))
    # Whole-line // only: a mid-line rule eats the `//` in a URL.
    return re.sub(r"^[ \t]*//[^\n]*", " ", body, flags=re.M)


def _controls() -> list[tuple[str, set[str]]]:
    out = []
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", _markup(), re.I):
        ident = re.search(r'id="([^"]+)"', tag)
        cls = re.search(r'class="([^"]*)"', tag)
        out.append((ident.group(1) if ident else "", set((cls.group(1) if cls else "").split())))
    return out


# Controls that deliberately do NOT go through the config save path. Each
# needs a reason, and the reason is the point: an unexplained exemption is
# how a real field gets quietly parked here to make this test green.
_EXEMPT = {
    "pref-time-range": "dashboard display preference — localStorage, saved on change",
    "pref-refresh-interval": "dashboard display preference — localStorage, saved on change",
    "pref-show-critical": "dashboard display preference — localStorage, saved on change",
    "pref-show-status": "dashboard display preference — localStorage, saved on change",
    "ldap-picker-search": "a search box inside the user picker; filters a list, configures nothing",
    "reset-pw-new": "backup-admin password reset — its own admin-gated endpoint",
    "reset-pw-confirm": "backup-admin password reset — its own admin-gated endpoint",
}


def test_every_settings_control_is_tracked_or_deliberately_exempt():
    """The assertion the old design made impossible to write.

    With three hand-maintained lists there was nothing to compare the page
    against; with the marker class as the source of truth, a control that
    carries neither class and is not exempt is a field whose edit will be
    silently discarded."""
    orphans = []
    for ident, classes in _controls():
        if "general-input" in classes or "security-input" in classes:
            continue
        if ident in _EXEMPT:
            continue
        orphans.append(ident or "(no id)")
    assert not orphans, (
        "settings controls that neither carry a change-tracking bucket nor "
        "appear in the exemption list — an edit to any of them opens no Save "
        "bar and is lost on navigation:\n  " + "\n  ".join(orphans))


def test_a_tracked_control_can_actually_be_snapshotted():
    """The tracker keys its snapshot by id, so a marked control without one
    is skipped in silence — the same failure wearing a different hat."""
    idless = [classes for ident, classes in _controls()
              if not ident and ({"general-input", "security-input"} & classes)]
    assert not idless, f"{len(idless)} marked control(s) have no id to key on"


def test_the_exemptions_all_carry_a_reason():
    """A bare list would be a place to park inconvenient fields."""
    assert all(reason.strip() for reason in _EXEMPT.values())
    present = {ident for ident, _ in _controls()}
    stale = sorted(k for k in _EXEMPT if k not in present)
    assert not stale, (
        f"exemptions for controls that no longer exist: {stale} — an "
        "exemption list nobody prunes stops describing the page")


# ── the mechanism itself ─────────────────────────────────────────────────

_BUCKET_FNS = ("captureSettingsBucket", "settingsBucketChanged", "revertSettingsBucket")


def _fn_body(name: str) -> str:
    src = _script()
    m = re.search(rf"function {name}\(([^)]*)\)\s*\{{(.*?)\n\}}", src, re.S)
    assert m, f"{name} is gone; the tracker has been rewritten"
    return m.group(2)


def test_the_tracker_reads_the_dom_rather_than_a_list_of_ids():
    """The regression that matters. Any of the three operations reaching for
    a specific control by id means the list is back, and the list is what
    fails."""
    for fn in _BUCKET_FNS:
        body = _fn_body(fn)
        assert "getElementById" not in body, (
            f"{fn} names controls individually again; that is the mechanism "
            "this rewrite removed")


def test_all_three_operations_walk_the_same_control_set():
    """Capture, compare and revert disagreeing is how a field ends up
    tracked for Save and not for Cancel — two of the three lists, which the
    old code's own comment warned about."""
    for fn in _BUCKET_FNS:
        assert "_settingsControls(" in _fn_body(fn), (
            f"{fn} does not go through the shared control lookup")


def test_a_checkbox_is_compared_by_checked_and_not_by_value():
    """`value` on an unchecked checkbox is "on" — comparing it finds no
    change however many times it is toggled."""
    body = _fn_body("captureSettingsBucket") + _script()
    m = re.search(r"function _settingsValue\(el\)\s*\{(.*?)\n\}", _script(), re.S)
    assert m, "_settingsValue is gone"
    assert "checkbox" in m.group(1) and ".checked" in m.group(1)


def test_a_control_the_snapshot_has_never_seen_counts_as_a_change():
    """Fail loud, not quiet. A control that appeared after the snapshot was
    taken is either a real edit or a template regression; both deserve the
    Save bar. Absorbing it silently is the failure this file is about."""
    body = _fn_body("settingsBucketChanged")
    assert re.search(r"!\(el\.id in snap\)\s*\)?\s*return true", body), (
        "an unseen control no longer counts as a change")


# ── bucket semantics: the badge must not lie ─────────────────────────────

_SCHEDULED_REPORT_IDS = (
    "sched-enabled", "sched-daily-enabled", "sched-daily-time",
    "sched-weekly-enabled", "sched-weekly-day", "sched-weekly-time",
    "sched-email-report", "sched-include-pdf",
)


def test_the_bucket_states_whether_a_restart_is_needed():
    """`security-input` raises the restart badge and the restart
    confirmation; `general-input` does not. So the bucket is a claim about
    the field, not about which section it renders in.

    The scheduled-report controls were in the security bucket AND invisible
    to the compare function, so the wrong claim never showed — no Save bar
    meant no badge. Fixing the tracker made it visible at once: the badge
    began demanding a restart for a setting that takes effect within a
    minute."""
    marked = {ident: classes for ident, classes in _controls() if ident}
    for ident in _SCHEDULED_REPORT_IDS:
        assert ident in marked, f"{ident} is gone from the settings page"
        assert "general-input" in marked[ident], (
            f"{ident} is in the restart bucket; scheduled reports need no restart")


def test_scheduled_reports_really_do_take_effect_without_a_restart():
    """The evidence behind the bucket above, asserted against the code that
    reads the setting rather than against the class name — otherwise this
    pair of tests would just agree with itself."""
    periodics = (PROJECT_ROOT / "collector_v2" / "periodics.py").read_text(encoding="utf-8")
    m = re.search(r"def _scheduled_reports\(\):(.*?)\n    def ", periodics, re.S)
    assert m, "the scheduled-reports job has moved; re-derive whether it re-reads settings"
    assert "get_settings()" in m.group(1), (
        "the job no longer re-reads settings, so a change may now need a "
        "restart and the bucket above is wrong")


def test_the_one_field_that_does_need_a_restart_is_still_in_that_bucket():
    """The negative half. If everything drifted to `general-input` the test
    above would pass and the restart confirmation would never fire."""
    marked = {ident: classes for ident, classes in _controls() if ident}
    assert "security-input" in marked.get("collector-v2-workers", set()), (
        "the worker-pool size left the restart bucket; changing it needs a "
        "restart to take effect")


# ── the other end of the chain ───────────────────────────────────────────
#
# Everything above is about whether the Save bar OPENS. That is only half the
# question: the eight fields could be tracked perfectly and still be dropped
# on the way to disk. `doSaveAllSettings()` builds `data.settings.
# scheduled_reports` from all eight (read from the template source), and this
# pins the rest of the journey — POST, validate, write, read back — so the
# chain is closed end to end rather than at both ends with a gap in between.

import pytest
from flask import Flask

from config_manager import ConfigManager

_SCHEDULED_REPORTS_PAYLOAD = {
    "enabled": True,
    "daily_enabled": True,
    "daily_time": "06:15",
    "weekly_enabled": True,
    "weekly_day": "friday",
    "weekly_time": "18:45",
    "email_report": True,
    "include_pdf": True,
}


@pytest.fixture()
def config_client(tmp_path):
    """The real /api/config route over a throwaway config.

    Mirrors tests/test_config_partial_save.py's fixture, including the
    save/restore of the `_shared` globals — without it a tmp config leaks
    into every later test in the session."""
    from database import Database
    from routes.api import register_api_routes
    from routes.api import _shared as shared

    db = Database(tmp_path / "tracking.db")
    cfg = ConfigManager(tmp_path / "config.json")

    app = Flask(__name__)
    app.secret_key = "test-key"
    app.config["TESTING"] = True
    register_api_routes(app, db, cfg, limiter=None)

    prev_db, prev_cfg = getattr(shared, "_db", None), getattr(shared, "_config", None)
    shared._db, shared._config = db, cfg
    try:
        yield app.test_client(), cfg
    finally:
        shared._db, shared._config = prev_db, prev_cfg


def test_the_payload_below_is_the_one_the_page_actually_sends():
    """The round-trip test is only worth something if it posts what the form
    posts. The field names are read out of `doSaveAllSettings()`'s own
    `data.settings.scheduled_reports = {...}` literal rather than restated
    here — a payload agreed with a constant beside it proves nothing about
    the page (OPS-LEARNINGS #12)."""
    m = re.search(r"data\.settings\.scheduled_reports\s*=\s*\{(.*?)\n\s*\};",
                  _script(), re.S)
    assert m, "the save path no longer builds a scheduled_reports object"
    sent = set(re.findall(r"^\s*(\w+):", m.group(1), re.M))
    assert sent == set(_SCHEDULED_REPORTS_PAYLOAD), (
        f"the page sends {sorted(sent)}; this test posts "
        f"{sorted(_SCHEDULED_REPORTS_PAYLOAD)}")
    assert len(sent) == len(_SCHEDULED_REPORT_IDS), (
        f"{len(sent)} fields sent for {len(_SCHEDULED_REPORT_IDS)} controls "
        "on the page — one of them is not being saved")


def test_a_scheduled_report_change_reaches_disk_and_reads_back(config_client):
    client, cfg = config_client
    r = client.post("/api/config", json={"settings": {
        "scheduled_reports": dict(_SCHEDULED_REPORTS_PAYLOAD)}})
    assert r.status_code == 200, r.get_data(as_text=True)

    fresh = ConfigManager(cfg.config_path).get_settings().get("scheduled_reports", {})
    for key, expected in _SCHEDULED_REPORTS_PAYLOAD.items():
        assert fresh.get(key) == expected, (
            f"scheduled_reports.{key} did not survive the round trip: "
            f"{fresh.get(key)!r} != {expected!r}")
