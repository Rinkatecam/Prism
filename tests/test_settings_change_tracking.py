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


# ── what the page sends ──────────────────────────────────────────────────
#
# The tracker above decides whether Save appears. This decides what Save
# posts, and it had the same shape of defect one layer up: the payload was
# built by fetching the WHOLE configuration and overwriting the parts this
# page renders, so every save echoed back ~35 settings sub-trees and the
# server list — including everything owned by /monitoring and /operations.
#
# That is a last-write-wins clobber over data the operator never saw. Two
# tabs are enough: change a threshold on /monitoring, press Save here, and
# the threshold reverts to whatever it was when this page loaded.
#
# Measured on the rendered page: 35 settings keys plus `servers` before,
# 14 keys and no server list after.

_OWNED_SETTINGS_KEYS = {
    # General
    "poll_interval_seconds", "log_collection_interval_minutes",
    "update_check_interval_minutes", "retention_days", "language",
    "timezone", "date_format", "time_format",
    # Collector
    "collector_v2_num_workers",
    # Security & access
    "https", "auth",
    # Notifications
    "email", "webhooks", "scheduled_reports",
}


def _save_builder() -> str:
    src = _script()
    i = src.index("function doSaveAllSettings(")
    # Sliced on CODE, not on the comment that follows the builder: _script()
    # blanks whole-line // comments, so anchoring here on prose raises rather
    # than asserting — the same mistake as anchoring a mutation on a comment,
    # one step earlier (OPS-LEARNINGS #38).
    j = src.index("const ldapPayload = {", i)
    return src[i:j]


def test_the_page_posts_only_the_settings_it_owns():
    """Every key assigned into the payload must be one this page renders a
    control for. A key here that no control drives is a value being echoed
    back from a fetch, which is the clobber."""
    assigned = set(re.findall(r"data\.settings\.(\w+)\s*=", _save_builder()))
    assert assigned == _OWNED_SETTINGS_KEYS, (
        f"unexpected: {sorted(assigned - _OWNED_SETTINGS_KEYS)}, "
        f"missing: {sorted(_OWNED_SETTINGS_KEYS - assigned)}")


def test_the_payload_starts_empty_rather_than_from_the_fetched_config():
    """The regression that matters, and it is one line: reinstating
    `data.settings = Object.assign(data.settings || {}, {...})` puts all ~35
    keys back and nothing else in this file would notice."""
    builder = _save_builder()
    assert "const data = { settings: {} };" in builder, (
        "the payload is no longer built from scratch")
    assert "Object.assign(data.settings || {}" not in builder, (
        "the payload is being seeded from the fetched configuration again")


def test_the_server_list_is_not_posted_back():
    """Omitting `servers` is what puts save_config on its `settings_only`
    path, where the list is preserved wholesale instead of being rewritten
    from a copy this page fetched some time earlier."""
    builder = _save_builder()
    assert not re.search(r"data\.servers\s*=", builder)
    assert '"servers"' not in builder and "'servers'" not in builder


def test_the_auth_subtree_is_still_sent_whole():
    """The one deliberate exception. save_config's auth validator normalises
    that sub-tree by writing every field back, so a partial auth object
    blanks `backup_admin` and the three `lockout_*` keys — none of which are
    rendered on this page. It is merged over the fetched value for exactly
    that reason, and dropping the merge would be a silent credential wipe."""
    builder = _save_builder()
    assert re.search(r"data\.settings\.auth\s*=\s*Object\.assign\(\{\},\s*current\.settings\?\.auth",
                     builder), (
        "auth is no longer merged over the fetched sub-tree; a partial auth "
        "object blanks backup_admin and the lockout settings")


def test_a_narrowed_save_preserves_everything_it_did_not_send(config_client):
    """The behaviour the narrowing depends on, exercised rather than assumed.

    Seeds sub-trees owned by other pages, posts only this page's keys, and
    checks that the others came through untouched — and that the owned ones
    actually changed, so a save that quietly did nothing cannot pass."""
    client, cfg = config_client

    elsewhere = {
        # Values chosen INSIDE the route's clamps — exhaustion_disk 80..100,
        # exhaustion_ram 90..100, which are different ranges. An out-of-range
        # value comes back as the clamp and reads exactly like a clobber; the
        # first draft of this test accused the save path of losing data the
        # validator had simply corrected, twice.
        "thresholds": {"enabled": True, "exhaustion_disk": 83, "exhaustion_ram": 93,
                       "slow_collection_ms": 4321},
        "tls_monitoring": {"enabled": True, "warning_days": 21, "critical_days": 3,
                           "check_interval_cycles": 11, "certificates": []},
        "scheduled_server_restart_schedule": {"enabled": True, "schedule": "weekly",
                                              "time": "02:30", "day": "3", "month_day": 1},
    }
    r = client.post("/api/config", json={"settings": dict(elsewhere)})
    assert r.status_code == 200, r.get_data(as_text=True)

    # What the Settings page posts: only its own keys.
    r = client.post("/api/config", json={"settings": {
        "language": "fr",
        "retention_days": 45,
        "poll_interval_seconds": 300,
        "scheduled_reports": dict(_SCHEDULED_REPORTS_PAYLOAD),
    }})
    assert r.status_code == 200, r.get_data(as_text=True)

    after = ConfigManager(cfg.config_path).get_settings()
    for key, expected in elsewhere.items():
        for field, value in expected.items():
            assert after.get(key, {}).get(field) == value, (
                f"{key}.{field} was clobbered by a save that never sent it: "
                f"{after.get(key, {}).get(field)!r} != {value!r}")
    assert after["language"] == "fr"
    assert after["retention_days"] == 45
    assert after["scheduled_reports"]["daily_time"] == "06:15"
