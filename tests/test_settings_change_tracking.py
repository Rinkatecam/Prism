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
    """settings.html with comments AND scripts removed, and INCLUDES resolved.

    Scripts are stripped because the tracker's own source mentions the ids it
    no longer lists, and because a control built from a JS string is not part
    of the server-rendered form this scan is about.

    Includes are resolved because a section's markup may live in a partial —
    the detection block does, having moved there from /monitoring. Without
    this, every control-level assertion sees an empty section and reports the
    builder's ids as phantoms: the test would be measuring which FILE the
    markup sits in rather than what the page contains."""
    src = SETTINGS.read_text(encoding="utf-8")

    def _inline(match):
        target = PROJECT_ROOT / "templates" / match.group(1)
        return target.read_text(encoding="utf-8") if target.exists() else match.group(0)

    for _ in range(3):  # partials may include partials; bounded, not recursive
        expanded = re.sub(r'\{%\s*include\s+"([^"]+)"\s*%\}', _inline, src)
        if expanded == src:
            break
        src = expanded

    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    return re.sub(r"<script\b.*?</script>", " ", src, flags=re.S | re.I)


def _script() -> str:
    src = SETTINGS.read_text(encoding="utf-8")
    src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
    body = "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S | re.I))
    # Whole-line // only: a mid-line rule eats the `//` in a URL.
    return re.sub(r"^[ \t]*//[^\n]*", " ", body, flags=re.M)


def _controls(markup: str | None = None) -> list[tuple[str, set[str]]]:
    out = []
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>",
                          _markup() if markup is None else markup, re.I):
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


# Whole regions that save through their own endpoint rather than the settings
# save bar. A container rule rather than a list of ids: the maintenance modal
# holds twelve controls, two of them without an id, and a list of twelve would
# go stale the first time one was renamed.
# Regions that save through their own endpoint rather than the settings save
# bar. Keyed by the ATTRIBUTE that identifies them, because the two members
# are different shapes — one is a modal, one is a whole settings section —
# and a rule that only understood ids would have needed a second mechanism
# the moment the second one arrived.
_EXEMPT_CONTAINERS = {
    'id="maint-modal"': ("maintenance windows save one at a time from this "
                         "modal, through POST /api/maintenance-windows"),
    'data-settings-section="operations"': ("scheduled restarts save through "
                                           "POST /api/scheduled-restarts and "
                                           "their own button"),
}


def _markup_outside_exempt_containers() -> str:
    """The page's markup with the self-saving regions removed."""
    markup = _markup()
    for attr in _EXEMPT_CONTAINERS:
        i = markup.find(attr)
        assert i != -1, f"the exempt region {attr!r} is not on the page any more"
        # Back up to the element's own opening tag, then drop to the end of
        # the file's block: both members are the last thing in their region.
        start = markup.rfind("<", 0, i)
        rest = markup[start:]
        end = rest.find("\n</section>")
        if end == -1:
            end = rest.find("\n</div>\n")
        markup = markup[:start] + (rest[end:] if end != -1 else "")
    return markup


def test_the_container_exemptions_all_carry_a_reason():
    assert all(reason.strip() for reason in _EXEMPT_CONTAINERS.values())


def test_every_settings_control_is_tracked_or_deliberately_exempt():
    """The assertion the old design made impossible to write.

    With three hand-maintained lists there was nothing to compare the page
    against; with the marker class as the source of truth, a control that
    carries neither class and is not exempt is a field whose edit will be
    silently discarded."""
    orphans = []
    for ident, classes in _controls(_markup_outside_exempt_containers()):
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
    m = re.search(r"scheduled_reports:\s*\{(.*?)\n      \},",
                  _section_builder_bodies()["notifications"], re.S)
    assert m, "the notifications builder no longer builds a scheduled_reports object"
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
    # Detection (moved from /monitoring, WP-4 D2)
    "thresholds", "anomaly_detection", "baseline_detection", "security_alerts",
    # Alerts (moved from /monitoring, WP-4 D2b)
    "tls_monitoring",
    # Security & access
    "https", "auth",
    # Notifications
    "email", "webhooks", "scheduled_reports",
}


_SECTION_KEYS = {
    "general": {"retention_days", "language", "timezone", "date_format", "time_format"},
    "collector": {"poll_interval_seconds", "log_collection_interval_minutes",
                  "update_check_interval_minutes", "collector_v2_num_workers"},
    "detection": {"thresholds", "anomaly_detection", "baseline_detection",
                  "security_alerts"},
    "alerts": {"tls_monitoring"},
    "security": {"https", "auth"},
    "notifications": {"email", "scheduled_reports", "webhooks"},
}


def _builder_map() -> str:
    src = _script()
    i = src.index("const _SETTINGS_SECTION_BUILDERS = {")
    j = src.index("function settingsSectionPresent(", i)
    return src[i:j]


def _section_builder_bodies() -> dict[str, str]:
    """Each section builder's source, split on the `name: function (` heads."""
    body = _builder_map()
    heads = [(m.start(), m.group(1)) for m in
             re.finditer(r"^  (\w+): function \(", body, re.M)]
    assert heads, "the builder map has changed shape"
    out = {}
    for k, (pos, name) in enumerate(heads):
        stop = heads[k + 1][0] if k + 1 < len(heads) else len(body)
        out[name] = body[pos:stop]
    return out


def _save_body() -> str:
    src = _script()
    i = src.index("function doSaveAllSettings(")
    return src[i:src.index("const ldapPayload", i)]


def _controls_read_by(section: str) -> set[str]:
    """Every control a section's builder reads, FOLLOWING DELEGATION.

    The notifications builder hands `email` to `getEmailSettingsFromForm()`
    rather than inlining eleven getElementById calls, so a scan of the builder
    alone sees none of them and reports eleven orphans. Expanding the helpers
    the builder calls is the difference between checking the code and checking
    the shape somebody happened to write it in."""
    script = _script()
    body = _section_builder_bodies()[section]
    seen = set(re.findall(r"getElementById\('([^']+)'\)", body))
    for helper_name in set(re.findall(r"\b(\w+)\(\)", body)):
        m = re.search(rf"function {helper_name}\(\)\s*\{{(.*?)\n\}}", script, re.S)
        if m:
            seen |= set(re.findall(r"getElementById\('([^']+)'\)", m.group(1)))
    return seen


def test_every_settings_section_declares_itself():
    """The section attribute is what the builder keys off. A section without
    one contributes nothing to a save and shows no symptom until an operator
    edits a field in it and the value never lands."""
    sections = re.findall(r"<section\b[^>]*>", _markup(), re.I)
    undeclared = [s for s in sections if "data-settings-section=" not in s]
    assert not undeclared, (
        "settings sections that do not declare themselves:\n  "
        + "\n  ".join(undeclared))
    names = re.findall(r'data-settings-section="([^"]+)"', _markup())
    assert len(names) == len(set(names)), f"duplicate section names: {names}"
    # `_SECTION_KEYS` lists the sections that contribute to the settings
    # payload. Two do not and still exist: `display` keeps its preferences in
    # localStorage, `operations` posts its restart schedules to their own
    # endpoint. Both are real sections with real URLs, so the declaration
    # check has to know them even though no builder does.
    assert set(names) == set(_SECTION_KEYS) | {"display", "operations"}, sorted(names)


def test_the_section_builders_cover_exactly_the_keys_the_page_owns():
    """Union of what the builders emit, against the declared owned set."""
    emitted = set()
    for name, body in _section_builder_bodies().items():
        emitted |= set(re.findall(r"^      (\w+):", body, re.M))
    assert emitted == _OWNED_SETTINGS_KEYS, (
        f"unexpected: {sorted(emitted - _OWNED_SETTINGS_KEYS)}, "
        f"missing: {sorted(_OWNED_SETTINGS_KEYS - emitted)}")


def test_each_builder_emits_its_own_section_and_nothing_else():
    """The strong one. A key emitted by the wrong section is invisible while
    the whole page renders — every section is present, so the payload is
    identical — and wrong the moment the page is split, which is the entire
    point of this change."""
    bodies = _section_builder_bodies()
    for name, expected in _SECTION_KEYS.items():
        assert name in bodies, f"no builder for the {name} section"
        emitted = set(re.findall(r"^      (\w+):", bodies[name], re.M))
        assert emitted == expected, (
            f"{name} emits {sorted(emitted)}, expected {sorted(expected)}")


def test_every_control_lives_in_the_section_that_saves_it():
    """A control moved between sections without its key moving is a field the
    save cannot see once the page is split. Checked against the markup, so
    moving the markup is what fails."""
    # Exempt containers dropped first: their controls are inside a section but
    # deliberately outside its builder, and the two exemption checks must read
    # the same rule or they disagree about what a settings control is.
    markup = _markup_outside_exempt_containers()
    for name in _SECTION_KEYS:
        m = re.search(rf'<section\b[^>]*data-settings-section="{name}"[^>]*>(.*?)</section>',
                      markup, re.S)
        assert m, f"the {name} section is gone from the markup"
        in_markup = set(re.findall(r'<(?:input|select|textarea)\b[^>]*id="([^"]+)"',
                                   m.group(1), re.I))
        read_by_builder = _controls_read_by(name)
        orphans = in_markup - read_by_builder
        assert not orphans, (
            f"controls in the {name} section that its builder never reads: "
            f"{sorted(orphans)}")
        phantom = read_by_builder - in_markup
        assert not phantom, (
            f"the {name} builder reads controls that are not in its section: "
            f"{sorted(phantom)}")


def test_an_absent_section_contributes_nothing():
    """The prerequisite for sub-pages: a builder runs only when its section is
    on the page. Without the guard the first missing control throws inside a
    promise chain, and the operator gets a save that never resolves."""
    body = _builder_map() + _script()
    m = re.search(r"function buildSettingsPayload\(current\)\s*\{(.*?)\n\}",
                  _script(), re.S)
    assert m, "buildSettingsPayload is gone"
    assert "settingsSectionPresent(name)" in m.group(1), (
        "the payload no longer checks whether a section is present")


def test_the_ldap_side_write_is_guarded_by_its_own_section():
    """It reads five controls from Security and fires FIRST, aborting the
    chain on failure — so on a page without that section an unguarded write
    would kill the save before it started."""
    assert "!settingsSectionPresent('security') ? null" in _script(), (
        "the LDAP write is no longer guarded by the security section")
    assert "ldapPayload === null" in _script(), (
        "nothing skips the LDAP request when there is no payload for it")


def test_the_display_section_has_no_builder():
    """Its four controls are dashboard preferences in localStorage. A builder
    returning {} would imply it has server-side settings and it does not."""
    assert "display" not in _section_builder_bodies()
    assert 'data-settings-section="display"' in _markup()


def test_the_payload_is_assembled_rather_than_hand_built():
    """The regression: reinstating an inline `data.settings.X = ...` block
    inside doSaveAllSettings puts the page back to posting whatever that block
    happens to list, whether or not the section is there."""
    save = _save_body()
    assert "const data = buildSettingsPayload(current);" in save
    assert not re.search(r"data\.settings\.\w+\s*=", save), (
        "the save function assigns settings keys directly again")
    assert not re.search(r"data\.servers\s*=", save)


def test_the_server_list_is_not_posted_back():
    """Omitting `servers` is what puts save_config on its `settings_only`
    path, where the fleet list is preserved wholesale instead of being
    rewritten from a copy this page fetched some time earlier.

    Asserted on the ASSEMBLY, not on doSaveAllSettings. The first version of
    this test read only the save function; D1c moved the construction into
    buildSettingsPayload, and a mutation adding `servers` back there sailed
    through. The harness caught it."""
    m = re.search(r"function buildSettingsPayload\(current\)\s*\{(.*?)\n\}",
                  _script(), re.S)
    assert m, "buildSettingsPayload is gone"
    assert "servers" not in m.group(1), (
        "the payload carries a server list again, which takes the save off "
        "the settings_only path")
    assert not re.search(r"data\.servers\s*=", _save_body())


def test_the_auth_subtree_is_still_sent_whole():
    """The one deliberate read of the fetched config. save_config's auth
    validator normalises that sub-tree by writing every field back, so a
    partial auth object blanks `backup_admin` and the three `lockout_*` keys —
    none of which are rendered anywhere. Dropping the merge is a silent
    credential wipe."""
    security = _section_builder_bodies()["security"]
    assert re.search(r"auth:\s*Object\.assign\(\{\},\s*current\.settings\?\.auth",
                     security), (
        "auth is no longer merged over the fetched sub-tree")


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


# ── the shell: one section per page ──────────────────────────────────────
#
# Three things now agree about what a settings section is: the ROUTER's list,
# the `data-settings-section` attributes in the markup, and the save payload's
# builder map. Any two of them agreeing while the third differs produces a
# page that is unreachable, empty, or saves nothing — none of which raises.


def _router_sections() -> tuple:
    from routes.views import _SETTINGS_SECTIONS
    return _SETTINGS_SECTIONS


def test_the_router_and_the_markup_agree_about_the_sections():
    """A name in the router that the template does not render is a blank
    page; a section in the template the router does not know is unreachable.
    Neither raises, and both look like the page forgot something."""
    in_markup = re.findall(r'data-settings-section="([^"]+)"', _markup())
    assert list(_router_sections()) == in_markup, (
        f"router: {list(_router_sections())}\nmarkup: {in_markup}")


def test_the_builder_map_covers_every_section_that_has_settings():
    """The third party to the agreement. `display` is the one section with no
    builder, because its controls are localStorage preferences."""
    builders = set(_section_builder_bodies())
    # `operations` joins `display` as a section with no builder: its
    # scheduled restarts post to their own endpoint, so nothing of theirs
    # belongs in the settings payload.
    assert builders | {"display", "operations"} == set(_router_sections())


def test_every_section_renders_only_when_it_is_the_active_one():
    """The gate itself. A section without one renders on every sub-page,
    which is the old single-page behaviour returning quietly for that one
    section — and it would take its controls into every other page's save."""
    markup = _markup()
    for name in _router_sections():
        pattern = (r"\{%\s*if section == '" + name + r"'\s*%\}\s*"
                   r'<section class="mb-8" data-settings-section="' + name + '">')
        assert re.search(pattern, markup), (
            f"the {name} section is not gated on being the active section")


def test_the_nav_is_generated_from_the_router_list():
    """A nav with its own hardcoded list drifts from the routes, and the
    failure is a tab that 404s or a section nobody can reach."""
    markup = _markup()
    m = re.search(r'<nav id="settings-section-nav".*?</nav>', markup, re.S)
    assert m, "the section nav is gone"
    nav = m.group(0)
    assert "{% for name in settings_sections %}" in nav, (
        "the nav no longer iterates the router's list")
    assert 'href="/settings/{{ name }}"' in nav
    assert 'aria-current="page"' in nav, (
        "nothing but colour says which section is active")


def test_an_unknown_section_is_a_404_rather_than_a_silent_fallback():
    """A stale or typo'd link that quietly shows General looks like the page
    forgot the operator's setting."""
    import app as prism_app
    client = prism_app.app.test_client()
    assert client.get("/settings").status_code == 200
    for name in _router_sections():
        assert client.get(f"/settings/{name}").status_code == 200, name
    assert client.get("/settings/nonsense").status_code == 404


def test_settings_renders_the_first_section_without_redirecting():
    """`/settings` is the URL every operator has bookmarked. Keeping it a
    real page means the split costs nobody a redirect."""
    import app as prism_app
    r = prism_app.app.test_client().get("/settings")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Scripts stripped first: `settingsSectionPresent` builds the very same
    # attribute selector from a string, so a scan of the whole response finds
    # `data-settings-section="' + name + '"` and reports a second section that
    # is not there. The check is about MARKUP.
    body = re.sub(r"<script\b.*?</script>", " ", body, flags=re.S | re.I)
    rendered = re.findall(r'data-settings-section="([^"]+)"', body)
    assert rendered == [_router_sections()[0]], rendered


def test_a_section_scoped_initialiser_checks_its_controls_are_there():
    """Two DOMContentLoaded blocks bind listeners to Security controls. On
    any other sub-page those are null, and an uncaught TypeError in a
    DOMContentLoaded handler stops every initialiser registered after it —
    not just the one that threw. Both were observed doing exactly that
    before the guards went in."""
    script = _script()
    for handle in ("httpsToggle", "authToggle"):
        m = re.search(rf"const {handle} = document\.getElementById\('[^']+'\);(.*?)\n\s*function ",
                      script, re.S)
        assert m, f"the {handle} initialiser has been reshaped"
        # The guard must NAME the handle. `"return;" in block` was the first
        # version and it accepted `if (false) return;` — a hedge satisfied by
        # the absence of the thing under test (OPS-LEARNINGS #37), caught by
        # the mutation harness rather than by re-reading it.
        assert re.search(rf"if \([^)]*!{handle}[^)]*\)\s*return;", m.group(1)), (
            f"the {handle} initialiser does not bail out on {handle} being "
            "absent from this page")


def test_the_save_validator_skips_checks_whose_section_is_absent():
    """It validates fields from three different sections. A validator that
    throws is worse than one that skips: the exception aborts the save with
    no message and the operator sees a button that did nothing."""
    m = re.search(r"function _validateBeforeSave\(\)\s*\{(.*?)\n\}", _script(), re.S)
    assert m, "_validateBeforeSave is gone"
    body = m.group(1)
    for guarded in ("httpsEnabled &&", "authEnabled &&"):
        assert guarded in body, f"the validator no longer guards on {guarded!r}"
    for field in ("pollField", "retentionField", "timeoutField"):
        assert re.search(rf"if \({field}\)", body), (
            f"the validator dereferences {field} without checking it is there")


# ── the detection cards' priority labels ─────────────────────────────────
#
# The demoted detectors used to be shown by fading their card to
# `opacity: 0.65`. Measured on the rendered page after the block moved here:
# 2.39 light / 3.07 dark on the help text — the third dim-over-muted-text
# failure in this round and the worst of them. The fade was also the only
# thing saying a detector was demoted, and an opacity says nothing to a
# screen reader.


def test_no_detector_card_is_dimmed_to_say_it_is_demoted():
    """The regression. `style.opacity` on these cards is the exact defect
    that was removed, and it would look like a tidy re-implementation."""
    script = _script()
    m = re.search(r"const updateDetectionPriority = \(\) => \{(.*?)\n  \};", script, re.S)
    assert m, "the detection-priority block is gone"
    assert "opacity" not in m.group(1), (
        "the demoted detector cards are being faded again")


def test_every_detector_card_states_its_rank():
    """Three cards, three labels. A card without one says nothing about its
    priority, which is what two of the three did before."""
    markup = _markup()
    keys = set(re.findall(r'data-detection-priority="([^"]+)"', markup))
    assert keys == {"baseline", "anomaly", "thresholds"}, sorted(keys)


def test_the_ranks_are_recomputed_when_a_detector_is_toggled():
    """The labels rendered once and never moved when the listeners were lost
    in a rewrite — which looks exactly like a working feature until you
    toggle something. Found by toggling it in the running page."""
    script = _script()
    m = re.search(r"\['baseline-enabled', 'anomaly-enabled', 'thresholds-enabled'\]\.forEach\((.*?)\}\);",
                  script, re.S)
    assert m, "nothing re-ranks the detectors when one is toggled"
    # Guarded on the TOGGLE, not merely present. `if (false) toggle.add...`
    # satisfies a substring check while binding nothing — the same hedge that
    # made the initialiser-guard test blind, caught the same way.
    assert re.search(r"if \(toggle\)\s*toggle\.addEventListener\('change', updateDetectionPriority\)",
                     m.group(1)), (
        "the change binding is present but not reached")


def test_the_rank_words_come_from_the_locale_table():
    """`Priority 1` was a hardcoded English string in the markup — visible to
    every operator in every language, and invisible to the locale-coverage
    tests because it never went through `t.get`."""
    from i18n import TRANSLATIONS
    for key in ("detection_priority", "detection_off"):
        missing = [lang for lang in TRANSLATIONS if key not in TRANSLATIONS[lang]]
        assert not missing, f"{key} missing from {missing}"
    assert "Priority 1" not in _markup(), (
        "a rank is hardcoded in English in the markup again")


def test_the_detection_help_text_is_not_the_faintest_token():
    """These cards sit on the PAGE surface, where `faint` measures 4.34 —
    under AA. It read 2.39 until the dim came off, so removing the dim did
    not fix this text; it uncovered it."""
    partial = (PROJECT_ROOT / "templates" / "partials" / "settings"
               / "_detection.html").read_text(encoding="utf-8")
    partial = re.sub(r"\{#.*?#\}", " ", partial, flags=re.S)
    assert "text-faint" not in partial, (
        "faint text is back on the detection cards, which sit on bg-page")
