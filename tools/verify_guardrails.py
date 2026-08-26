"""Mutation-check the design guardrails: break the code, expect the test to fail.

A passing test proves nothing on its own. This repository's most-repeated
failure is a check that reports success without doing the work
(docs/OPS-LEARNINGS.md §2.2), and the only cheap defence is to introduce the
defect on purpose and confirm the guard notices.

Run it:

    python tools/verify_guardrails.py            # all suites
    python tools/verify_guardrails.py keyboard   # one suite, by substring

Every mutation does five things, in this order:

  1. finds its anchor — a mutation whose anchor has drifted is reported as
     NOT APPLIED rather than passing silently;
  2. writes the change and RE-READS THE FILE to prove it landed. Three no-op
     mutations passed for the wrong reason in one session on this repo,
     thirty minutes after the warning about it was written down;
  3. confirms the named test EXISTS in the suite file. pytest exits 5 when a
     `-k` expression selects nothing, and "not zero" used to be read as "the
     test failed" — so a mutation naming a test in a different file reported
     itself as correctly caught while no test ran at all. Two did exactly
     that, in this file, within an hour of it being extended;
  4. runs that test and fails the run if it still passes — that test is blind;
  5. restores from the in-memory original and verifies the restore.

Restores never use git: the working tree is normally dirty during this work
and `git checkout --` would take the real changes with it.

WHAT THIS STILL CANNOT SEE, and it is the important gap: step 2 proves the
FILE changed, never that BEHAVIOUR did. A mutation that rewrites a line into
an equivalent one — `ON r.x = c.x` into `ON 1=1 AND r.x = c.x` — lands
perfectly and breaks nothing, and is then reported as a blind test rather
than as a useless mutation. One such went in during the dashboard round.
Read what a new mutation actually does; the tool will not tell you.

Baselines, all green:
  wave 3           50 mutations, 41 tests, 6 suites
  dashboard round 105 mutations, 92 tests, 11 suites

Between them, seven tests were blind when first written and were fixed
because of a mutation here, not because anyone re-read them.
"""

from __future__ import annotations

import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Mutation:
    label: str          # what defect this introduces, in plain words
    path: str           # repo-relative file to break
    find: str           # anchor; must be present exactly once-ish
    replace: str        # what to put there ('' to delete)
    test: str           # the -k expression for the test that must now fail


SUITES: dict[str, list[Mutation]] = {}


def suite(name: str, *mutations: Mutation) -> None:
    SUITES[name] = list(mutations)


# ── loading: skeletons and the settle animation ──────────────────────────
suite(
    "loading",
    Mutation("region loses its ghost", "templates/dashboard.html",
             "{{ vitals_cards() }}", "",
             "test_every_load_triggered_region_is_accounted_for"),
    # WP-3 slice C: the feed moved to /servers, compact.
    Mutation("the feed moves pages and leaves its ghost behind",
             "templates/servers.html",
             "{{ feed_rows(4, compact=true) }}", "",
             "test_the_feed_is_accounted_for_wherever_it_lives"),
    Mutation("the servers-page feed quietly asks for all twenty rows",
             "templates/servers.html",
             "/partials/activity-feed?compact=1", "/partials/activity-feed",
             "test_the_feed_is_accounted_for_wherever_it_lives"),
    Mutation("the ghost's cap drifts from the scroller's",
             "templates/partials/_skeletons.html",
             "{% if compact %}max-h-[240px]{% else %}max-h-[400px]{% endif %}",
             "{% if compact %}max-h-[200px]{% else %}max-h-[400px]{% endif %}",
             "test_the_two_feed_caps_and_their_ghosts_agree"),
    Mutation("hidden alert section grows a ghost", "templates/dashboard.html",
             '       data-toggle-empty="tls-alerts-section">\n  </div>',
             '       data-toggle-empty="tls-alerts-section">\n'
             '    <div class="skeleton h-4"></div>\n  </div>',
             "test_the_hidden_alert_sections_deliberately_have_no_ghost"),
    Mutation("ghost exposed to screen readers", "templates/partials/_skeletons.html",
             '{% macro vitals_cards() %}\n<div class="vitals-grid" aria-hidden="true">',
             '{% macro vitals_cards() %}\n<div class="vitals-grid">',
             "test_a_ghost_is_hidden_from_assistive_technology"),
    Mutation("pulse placeholder comes back", "templates/dashboard.html",
             '<section id="vitals-section"',
             '<div class="animate-pulse bg-gray-200"></div>\n<section id="vitals-section"',
             "test_the_old_pulse_placeholders_are_gone"),
    Mutation("skeleton-text left without skeleton", "templates/partials/_skeletons.html",
             '<div class="skeleton skeleton-text"></div>',
             '<div class="skeleton-text"></div>',
             "test_skeleton_text_is_never_used_without_skeleton"),
    Mutation("data-settled moved onto swapped content",
             "templates/partials/activity_feed.html",
             "<!-- Activity Feed - consolidated events timeline -->",
             '<!-- Activity Feed - consolidated events timeline -->\n<div class="data-settled">',
             "test_data_settled_is_never_placed_on_swapped_content"),
    Mutation("settle fires on every swap, not just the first", "templates/base.html",
             "if (el.getAttribute('data-was-ghost') !== '1') return;", "",
             "test_the_settle_is_keyed_on_a_ghost_having_been_there"),
    Mutation("dangling custom property reintroduced", "static/css/app.css",
             "  border-radius: 0.5rem;\n  background: rgb(var(--c-raised));",
             "  border-radius: var(--radius-sm, 0.5rem);\n  background: rgb(var(--c-raised));",
             "test_no_custom_property_is_read_without_being_defined"),
)

# ── states: hover/focus on clickable cards ───────────────────────────────
suite(
    "states",
    Mutation("focus ring rule deleted", "static/css/app.css",
             ".card-clickable:focus-visible,\na.server-card:focus-visible,\n"
             "button.server-card:focus-visible {",
             ".card-clickable-DISABLED:focus-visible {",
             "test_the_focus_ring_rule_exists_and_uses_the_brand_token"),
    Mutation("server grid dropped from the focus rule", "static/css/app.css",
             "a.server-card:focus-visible,\nbutton.server-card:focus-visible {",
             "a.server-card-OTHER:focus-visible {",
             "test_the_focus_ring_rule_exists_and_uses_the_brand_token"),
    Mutation("ring hardcoded instead of tokenised", "static/css/app.css",
             "box-shadow: 0 0 0 3px rgb(var(--c-brand) / 0.25), var(--shadow-md);\n"
             "  transform: translateY(-2px);",
             "box-shadow: 0 0 0 3px rgba(91, 33, 182, 0.25), var(--shadow-md);\n"
             "  transform: translateY(-2px);",
             "test_the_focus_ring_rule_exists_and_uses_the_brand_token"),
    Mutation("focus says less than hover", "static/css/app.css",
             "box-shadow: 0 0 0 3px rgb(var(--c-brand) / 0.25), var(--shadow-md);\n"
             "  transform: translateY(-2px);",
             "box-shadow: 0 0 0 3px rgb(var(--c-brand) / 0.25), var(--shadow-md);",
             "test_focus_is_not_weaker_than_hover"),
    Mutation("a new clickable card skips the class", "templates/compliance.html",
             'class="card-clickable block bg-card rounded-lg border border-line p-4"',
             'class="block bg-card rounded-lg border border-line p-4"',
             "test_every_card_shaped_control_opts_in"),
    Mutation("narrow tailwind transition left alongside the class",
             "templates/partials/critical_issues.html",
             'class="card-clickable block bg-card rounded-lg p-4 border border-line',
             'class="card-clickable transition-shadow block bg-card rounded-lg p-4 border border-line',
             "test_card_clickable_is_not_shadowed_by_a_narrow_tailwind_transition"),
)

# ── keyboard: non-native controls ────────────────────────────────────────
suite(
    "keyboard",
    Mutation("the reported card loses its tab stop", "templates/server_detail.html",
             'data-action="_sdToggleLoginCard" tabindex="0" role="button"',
             'data-action="_sdToggleLoginCard" role="button"',
             "test_every_clickable_non_native_element_is_keyboard_operable"),
    Mutation("an expandable table row loses its tab stop", "templates/server_detail.html",
             'data-action="toggleLogGroup" data-args="[${idx}]" tabindex="0"',
             'data-action="toggleLogGroup" data-args="[${idx}]"',
             "test_every_clickable_non_native_element_is_keyboard_operable"),
    Mutation("the context menu row loses its tab stop", "templates/workflows.html",
             'data-action="${item.action}" tabindex="0" role="menuitem"',
             'data-action="${item.action}" role="menuitem"',
             "test_every_clickable_non_native_element_is_keyboard_operable"),
    Mutation("aria-expanded left with nothing to read",
             "templates/partials/incidents_panel.html",
             'aria-expanded="false" aria-controls="incident-detail-{{ loop.index0 }}"',
             'aria-expanded="false"',
             "test_every_expanded_carrier_declares_what_it_controls"),
    Mutation("the keyboard bridge is removed", "templates/base.html",
             "document.addEventListener('keydown', function (e) {",
             "document.addEventListener('keydown-DISABLED', function (e) {",
             "test_the_keyboard_bridge_exists_and_skips_native_controls"),
    Mutation("the bridge stops excluding native controls (double-fire)",
             "templates/base.html",
             "const NATIVE = 'button, a[href], input, select, textarea, summary",
             "const NOTNATIVE = 'x, y, input, select, textarea, summary",
             "test_the_keyboard_bridge_exists_and_skips_native_controls"),
    Mutation("Space no longer suppressed, so the page scrolls", "templates/base.html",
             "        e.preventDefault();\n        run(el, e, el.getAttribute('data-action'));",
             "        run(el, e, el.getAttribute('data-action'));",
             "test_the_keyboard_bridge_exists_and_skips_native_controls"),
    Mutation("the [onclick] lookup comes back", "templates/server_detail.html",
             "      const owner = d.closest('[data-action]');",
             "      const owner = d.closest('[onclick]');",
             "test_nothing_looks_for_an_onclick_attribute_any_more"),
    Mutation("the exemption is widened until it excuses everything",
             "tests/test_design_keyboard.py",
             'if action in ("stop-prop", "modal-backdrop", "close-mobile-sidebar"):',
             "if True:",
             "test_the_detector_matches_a_real_historical_offender"),
    Mutation("the exemption is narrowed until backdrops need tab stops",
             "tests/test_design_keyboard.py",
             'backdrop = ("inset-0" in tag) and (action.startswith("close") or "backdrop" in action)',
             "backdrop = False",
             "test_the_exemption_rule_still_excuses_a_modal_backdrop"),
)

# ── empty states: the three-part rule ────────────────────────────────────
suite(
    "empty-states",
    Mutation("part 3 dropped from a JS empty state",
             "templates/partials/settings/_rbac.html",
             "window.prismEmptyState('check-circle', RBAC_T.noApprovals, RBAC_T.noApprovalsHint)",
             "window.prismEmptyState('check-circle', RBAC_T.noApprovals)",
             "test_every_empty_state_supplies_all_three_parts"),
    Mutation("part 3 dropped from the Jinja macro call",
             "templates/partials/activity_feed.html",
             "     t.get('no_events_hint', 'Nothing has needed attention recently — "
             "alerts and status changes will appear here as they happen'),\n     card=true) }}",
             "     card=true) }}",
             "test_every_empty_state_supplies_all_three_parts"),
    Mutation("hint reduced to a restatement of the message", "templates/workflows.html",
             "'No executions yet',\n      'Run a workflow and its history — who ran it, "
             "when, and the result — appears here'",
             "'No executions yet',\n      'No executions yet'",
             "test_a_hint_does_not_merely_restate_the_message"),
    Mutation("the JS renderer stops requiring a hint", "templates/base.html",
             "        if (!hint) {\n          throw new Error('prismEmptyState: a hint is required",
             "        if (false) {\n          throw new Error('prismEmptyState: a hint is required",
             "test_both_renderers_require_a_hint"),
    Mutation("the Jinja macro stops rendering the hint",
             "templates/partials/_empty_state.html",
             '  <p class="text-[10px] mt-1 opacity-60">{{ hint }}</p>', "",
             "test_both_renderers_require_a_hint"),
    Mutation("server_detail grows its own copy again", "templates/server_detail.html",
             "  function _renderEmptyState(icon, message, hint, opts) {\n"
             "    return window.prismEmptyState(icon, message, hint, opts);\n  }",
             "  function _renderEmptyState(icon, message, hint, opts) {\n"
             "    const safeHint = hint || '';\n"
             "    return '<div>' + (safeHint ? safeHint : '') + '</div>';\n  }",
             "test_there_is_only_one_implementation_per_layer"),
    Mutation("a failed request goes back to claiming there is no data",
             "templates/server_detail.html",
             '\'<p>{{ t.get("failed_logins_load_error", "Could not load failed logins") }}</p>\'',
             '\'<p>{{ t.get("no_data", "No data available") }}</p>\'',
             "test_a_failed_request_is_not_dressed_up_as_an_empty_state"),
    # ── the coverage hole Wave 4 pass 2 found: the rule was only ever
    #    enforced on sites that had already opted in.
    Mutation("a new empty state hand-rolls the renderer's markup",
             "templates/partials/vitals_quadrant.html",
             "    {{ empty_state('activity', t.get('no_health_checks', 'No health checks configured'),\n"
             "                   t.get('vitals_no_services_hint',\n"
             "                         'Add a probe under Operations → Health Checks to watch a port, a URL or a host')) }}",
             '    <div class="text-sm text-faint text-center py-8">\n'
             '      <i data-lucide="activity" class="w-8 h-8 mx-auto mb-2 opacity-30"></i>\n'
             '      <p>No health checks configured</p>\n'
             '      <p class="text-[10px] mt-1 opacity-60">Add a probe under Operations</p>\n'
             "    </div>",
             "test_no_new_empty_state_hand_rolls_the_markup"),
    Mutation("a converted site leaves its baseline behind",
             "tests/test_design_empty_states.py",
             '    "servers.html": 5,', '    "servers.html": 6,',
             "test_the_hand_rolled_baseline_comes_down_when_a_site_is_converted"),
    Mutation("the detector stops telling a caller from a copy",
             "tests/test_design_empty_states.py",
             "_HANDROLLED_ICON = re.compile(\n"
             "    r'data-lucide=[\\'\"`]?\\$?\\{?[\\w-]+[\\'\"`]?\\s+class=[\\'\"]w-8 h-8 mx-auto mb-2 opacity-30')",
             "_HANDROLLED_ICON = re.compile(r'empty_state|opacity-30')",
             "test_the_hand_rolled_detector_can_tell_a_caller_from_a_copy"),
)

# ── disabled controls: a reason, kept in step with the state ─────────────
suite(
    "disabled",
    Mutation("a markup-disabled control loses its reason", "templates/servers.html",
             "data-tip-title=\"{{ t.get('disabled_type_name_title', 'Type the server name first') }}\"",
             "", "test_every_markup_disabled_control_says_why"),
    Mutation("the reason hides behind an interpolation again", "templates/servers.html",
             '${monitored ? \'data-tip-title="Already monitored" data-tip-desc="This host is '
             'already in Prism — remove it from the Servers list first if you want to re-add it."\' : \'\'}',
             "${tipAttrs}",
             "test_every_markup_disabled_control_says_why"),
    Mutation("pointer-events removed from disabled controls", "static/css/app.css",
             '[disabled],\n[aria-disabled="true"],\n.is-disabled {\n  opacity: 0.72;\n}',
             '[disabled],\n[aria-disabled="true"],\n.is-disabled {\n  opacity: 0.72;\n'
             '  pointer-events: none;\n}',
             "test_pointer_events_are_never_removed_from_disabled_controls"),
    Mutation("the helper stops clearing the reason on enable", "templates/base.html",
             "          el.removeAttribute('data-tip-title');\n"
             "          el.removeAttribute('data-tip-desc');", "",
             "test_the_helper_clears_the_reason_when_it_enables"),
    Mutation("the helper stops enabling at all", "templates/base.html",
             "          el.disabled = false;", "",
             "test_the_helper_clears_the_reason_when_it_enables"),
    Mutation("a confirm control goes back to a bare assignment",
             "templates/operations.html",
             "  prismSetDisabled(document.getElementById('data-action-confirm-btn'),\n"
             "    '{{ t.get(\"disabled_type_keyword_title\", \"Type the confirmation word first\") }}',\n"
             "    '{{ t.get(\"disabled_type_keyword_desc\", \"This action cannot be undone. "
             "Type the word shown above to confirm.\") }}');",
             "  document.getElementById('data-action-confirm-btn').disabled = true;",
             "test_a_confirm_control_is_not_also_toggled_by_a_bare_assignment"),
    Mutation("the tooltip body is assembled as markup again", "templates/base.html",
             "        tip.textContent = '';", "        tip.innerHTML = '';",
             "test_the_tooltip_body_is_not_assembled_as_markup"),
    Mutation("interpolation defusing removed", "tests/test_design_disabled.py",
             'return _INTERP.sub(lambda m: m.group(0).replace(">", " ").replace("<", " "), text)',
             "return text",
             "test_the_scan_sees_past_a_template_interpolation"),
    Mutation("the Tailwind disabled: variant starts counting as the attribute",
             "tests/test_design_disabled.py",
             '_DISABLED_ATTR = re.compile(r"(?<![\\w-])disabled(?![\\w:-])")',
             '_DISABLED_ATTR = re.compile(r"(?<![\\w-])disabled")',
             "test_a_tailwind_disabled_variant_is_not_mistaken_for_the_attribute"),
)

# ── layered render: the deferred analytics region ────────────────────────
suite(
    "layered",
    Mutation("analytics recomputed in the page view", "routes/views.py",
             "        settings = _config.get_settings()\n"
             "        # `analytics` is deliberately NOT gathered here",
             "        settings = _config.get_settings()\n"
             "        analytics = get_server_analytics(_db, name, server_type=cfg.type,\n"
             "                                          timezone_str=settings.get('timezone', 'Europe/Berlin'),\n"
             "                                          settings=settings, thresholds=cfg.thresholds)\n"
             "        # `analytics` is deliberately NOT gathered here",
             "test_the_expensive_call_is_not_in_the_page_view"),
    Mutation("a cheap read deferred too, adding a request for nothing",
             "routes/views.py",
             "        logs = _db.get_server_logs(name, hours=24, limit=50)\n", "",
             "test_the_cheap_reads_stayed_inline"),
    Mutation("the partial route path changes and the template's hx-get dangles",
             "routes/views.py",
             '@views_bp.route("/partials/server-analytics/<name>")',
             '@views_bp.route("/partials/analytics/<name>")',
             "test_the_partial_view_exists_and_does_the_work"),
    Mutation("an unknown host starts returning a rendered page", "routes/views.py",
             '        if not cfg:\n            return "", 200',
             '        if not cfg:\n            return render_template("404.html"), 404',
             "test_an_unknown_host_does_not_inject_an_error_page_into_the_document"),
    Mutation("the region stops fetching itself", "templates/server_detail.html",
             '       hx-trigger="load"', '       hx-trigger="revealed"',
             "test_the_page_region_fetches_itself_and_ghosts_while_it_waits"),
    Mutation("the region waits with nothing on screen", "templates/server_detail.html",
             "    {{ forecast_cards((1 if _sk_disk_c else 0) + (1 if _sk_disk_d else 0) + 2) }}",
             "", "test_the_page_region_fetches_itself_and_ghosts_while_it_waits"),
    Mutation("the ghost's column logic drifts from the real grid",
             "templates/partials/_skeletons.html",
             "'lg:grid-cols-4' if forecast_count >= 4 else ('lg:grid-cols-3' if "
             "forecast_count == 3 else ('lg:grid-cols-2' if forecast_count == 2 else ''))",
             "'lg:grid-cols-4'",
             "test_the_ghost_and_the_real_grid_agree_on_columns"),
    Mutation("an inline script appears in the swapped fragment",
             "templates/partials/server_analytics.html",
             "{% if analytics and analytics.anomalies | length > 0 %}",
             "<script>console.log('hi')</script>\n"
             "{% if analytics and analytics.anomalies | length > 0 %}",
             "test_the_partial_carries_no_inline_script"),
    Mutation("the partial reads a variable its view never passes",
             "templates/partials/server_analytics.html",
             "{% if analytics and analytics.anomalies | length > 0 %}",
             "{{ settings.timezone }}\n"
             "{% if analytics and analytics.anomalies | length > 0 %}",
             "test_the_partial_renders_with_exactly_what_its_view_passes"),
    Mutation("the heading leaves with the data", "templates/server_detail.html",
             "    {{ t.anomalies }} &amp; {{ t.forecasts }}", "",
             "test_the_page_region_fetches_itself_and_ghosts_while_it_waits"),
)

# ── vitals: the quadrant, the circle and the two unbuilt cards ───────────
suite(
    "vitals",
    Mutation("the trace stops asking about reduced motion",
             "static/js/vitals-monitor.js",
             "const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;",
             "const reduceMotion = false;",
             "test_the_trace_asks_about_reduced_motion_at_all"),
    Mutation("reduced motion is read and then ignored",
             "static/js/vitals-monitor.js",
             "  function sweeping() {\n    return !reduceMotion && beating() && !document.hidden;",
             "  function sweeping() {\n    return beating() && !document.hidden;",
             "test_a_reduced_motion_reader_never_starts_the_loop"),
    Mutation("the paused trace leaves an empty canvas",
             "static/js/vitals-monitor.js",
             "      stopRaf();\n      paint(now());\n      return;",
             "      stopRaf();\n      return;",
             "test_stopping_the_sweep_never_leaves_an_empty_canvas"),
    Mutation("a reduced-motion exemption is slipped into the CSS block",
             "static/css/app.css",
             "  /* Decorative, and carries no information the numbers do not. */\n"
             "  .pulse-canvas { display: none; }",
             "  /* Decorative, and carries no information the numbers do not. */\n"
             "  .pulse-canvas { display: none; }\n"
             "  .vitals-canvas { animation-iteration-count: infinite !important; }",
             "test_no_reduced_motion_exemption_was_quietly_granted"),
    Mutation("the loop can re-arm while the tab is hidden",
             "static/js/vitals-monitor.js",
             "  function sweeping() {\n    return !reduceMotion && beating() && !document.hidden;",
             "  function sweeping() {\n    return !reduceMotion && beating();",
             "test_the_loop_stops_when_the_tab_is_hidden"),
    Mutation("a per-frame pixel readback is introduced",
             "static/js/vitals-monitor.js",
             "    ctx.clearRect(0, 0, w, h);",
             "    ctx.getImageData(0, 0, w, h);\n    ctx.clearRect(0, 0, w, h);",
             "test_the_trace_is_never_scrolled_by_reading_pixels_back"),
    Mutation("the waveform goes back to one sample per column",
             "static/js/vitals-monitor.js",
             "    const step = 1 / SAMPLES_PER_PX;",
             "    const step = 1;",
             "test_the_waveform_is_supersampled"),
    Mutation("the severity->token mapping is copied into the JS",
             "static/js/vitals-monitor.js",
             "    const ink = window.getComputedStyle(canvas).color;",
             "    const ink = severity === 'urgent' ? 'rgb(var(--c-critical))' : "
             "window.getComputedStyle(canvas).color;",
             "test_the_severity_colour_mapping_is_not_duplicated_in_javascript"),
    Mutation("a severity loses its ring colour", "static/css/app.css",
             ".vitals-core--urgent   { --vitals-halo: var(--c-critical); }",
             ".vitals-core--URGENT   { --vitals-halo: var(--c-critical); }",
             "test_every_severity_has_a_ring_colour_and_a_label"),
    Mutation("the state words are duplicated instead of shared",
             "templates/dashboard.html",
             "{{ vitals_labels[vitals.severity] }}",
             "{{ t.get('vitals_state_calm', 'Stable') }}",
             "test_the_state_words_are_defined_once_and_used_twice"),
    Mutation("a state word is dropped from one locale", "i18n.py",
             '        "vitals_state_flat": "Kein Signal",\n', "",
             "test_every_state_word_exists_in_every_locale"),
    Mutation("the canvas moves inside the swapped region",
             "templates/dashboard.html",
             '       hx-swap="morph:innerHTML">\n    {{ vitals_cards() }}\n  </div>',
             '       hx-swap="morph:innerHTML">\n    {{ vitals_cards() }}',
             "test_the_circle_is_not_inside_the_region_that_gets_swapped"),
    Mutation("the quadrant stops refreshing", "templates/dashboard.html",
             '<div hx-get="/partials/vitals" hx-trigger="load, prismRefresh from:body"',
             '<div hx-get="/partials/vitals" hx-trigger="load"',
             "test_the_quadrant_cards_ARE_swapped"),
    Mutation("the state is read while the morph is still reconciling",
             "static/js/vitals-monitor.js",
             "document.body.addEventListener('htmx:afterSettle', readState);",
             "document.body.addEventListener('htmx:afterSwap', readState);",
             "test_the_javascript_reads_the_state_after_the_swap_has_settled"),
    # These two used to name `test_a_coming_soon_card_*`. WP-3 gave both cards
    # a destination, so "unavailable control" stopped being what they are and
    # those tests were replaced rather than relaxed. The DEFECTS they guard
    # against are unchanged and are re-pointed at the tests that now hold
    # them; the second one's old anchor described markup that no longer
    # exists, which is the harness reporting a drifted anchor rather than a
    # blind test.
    Mutation("a card whose subject is unbuilt loses its reason",
             "templates/partials/vitals_quadrant.html",
             """data-tip-title="{{ t.get('vitals_scan_tip_title', 'Posture scanning is not built yet') }}\"""",
             "", "test_a_card_whose_subject_is_unbuilt_still_says_why"),
    Mutation("the disabled styling hook comes back on a working link",
             "templates/partials/vitals_quadrant.html",
             '<a href="/scan" class="vitals-card vitals-card--br vitals-card--soon',
             '<a href="/scan" aria-disabled="true" class="vitals-card vitals-card--br vitals-card--soon',
             "test_no_quadrant_card_claims_to_be_unavailable"),
    Mutation("the reason becomes hover-only",
             "templates/partials/vitals_quadrant.html",
             """    <p class="vitals-soon-desc">{{ t.get('vitals_scan_desc', 'Rogue devices, stale drivers, configuration drift') }}</p>\n""",
             "", "test_the_reason_is_readable_without_a_pointer"),
    Mutation("the quadrant grows a breakpoint ladder instead of scaling",
             "static/css/app.css",
             "  --vitals-dia: clamp(14rem, 22vw, 25rem);",
             "  --vitals-dia: 18rem;",
             "test_the_quadrant_scales_continuously_rather_than_in_steps"),
    Mutation("the grid rows go back to sizing themselves",
             "static/css/app.css",
             "  grid-auto-rows: minmax(var(--vitals-row), 1fr);",
             "  grid-auto-rows: minmax(var(--vitals-row), auto);",
             "test_the_two_rows_of_the_quadrant_are_equal_height"),
    Mutation("the circle starts stealing hover from the cards",
             "static/css/app.css",
             "  z-index: 2;\n  pointer-events: none;",
             "  z-index: 2;",
             "test_the_circle_does_not_steal_hover_from_the_cards_it_covers"),
    Mutation("the dashboard goes back to a fixed-width island",
             "templates/dashboard.html",
             "{% block content_width %}page-dashboard{% endblock %}", "",
             "test_the_dashboard_is_not_a_fixed_width_island_on_a_wall_screen"),
    Mutation("the width class resolves to nothing", "static/css/app.css",
             ".page-dashboard { max-width: 120rem; }",
             ".page-dashboard-UNUSED { max-width: 120rem; }",
             "test_the_dashboard_is_not_a_fixed_width_island_on_a_wall_screen"),

    # WP-3 slice B: the four cards navigate.
    Mutation("a card goes back to being a div that does nothing",
             "templates/partials/vitals_quadrant.html",
             '<a href="/network" class="vitals-card vitals-card--tr',
             '<div class="vitals-card vitals-card--tr',
             "test_every_quadrant_card_navigates_to_its_own_topic"),
    Mutation("two cards point at the same page",
             "templates/partials/vitals_quadrant.html",
             'href="/scan"', 'href="/network"',
             "test_every_quadrant_card_navigates_to_its_own_topic"),
    Mutation("a card points at a route the app does not serve",
             "templates/partials/vitals_quadrant.html",
             'href="/services"', 'href="/service"',
             "test_every_destination_is_a_route_the_app_serves"),
    Mutation("aria-disabled returns to a card that navigates",
             "templates/partials/vitals_quadrant.html",
             '<a href="/network" class="vitals-card',
             '<a aria-disabled="true" href="/network" class="vitals-card',
             "test_no_quadrant_card_claims_to_be_unavailable"),
    Mutation("the sub-AA dim is re-added under a different name",
             "static/css/app.css",
             ".vitals-card--soon {\n  border-style: dashed;\n}",
             ".vitals-card--soon {\n  border-style: dashed;\n  opacity: 0.72;\n}",
             "test_the_unbuilt_cards_are_marked_without_dimming_their_text"),
    Mutation("the two unbuilt cards stop being marked at all",
             "static/css/app.css",
             ".vitals-card--soon {\n  border-style: dashed;\n}",
             ".vitals-card--soon {\n  border-style: solid;\n}",
             "test_the_unbuilt_cards_are_marked_without_dimming_their_text"),
    Mutation("the scope line goes back to the faintest token",
             "static/css/app.css",
             "  color: rgb(var(--c-muted));\n  margin-top: 0.25rem;\n  max-width: 22ch;",
             "  color: rgb(var(--c-faint));\n  margin-top: 0.25rem;\n  max-width: 22ch;",
             "test_the_scope_line_is_not_the_faintest_token_available"),
    Mutation("a card loses its click affordance and looks like a panel",
             "templates/partials/vitals_quadrant.html",
             'class="vitals-card vitals-card--tl card-clickable"',
             'class="vitals-card vitals-card--tl"',
             "test_the_cards_opt_into_the_shared_clickable_treatment"),
    Mutation("a bespoke focus rule outranks the global ring",
             "static/css/app.css",
             ".vitals-card--soon {\n  border-style: dashed;\n}",
             ".vitals-card:focus-visible { outline: none; }\n"
             ".vitals-card--soon {\n  border-style: dashed;\n}",
             "test_the_cards_opt_into_the_shared_clickable_treatment"),
    Mutation("the card's state line loses a locale",
             "i18n.py",
             '"vitals_not_monitored_yet": "Noch nicht',
             '"_vitals_not_monitored_yet_removed": "Noch nicht',
             "test_both_unbuilt_cards_describe_their_scope_in_every_locale"),
)

# ── /servers: view modes, the band, and sort-before-pagination ───────────
suite(
    "servers-view",
    Mutation("the reorder is announced before the rows move",
             "static/js/table-sort.js",
             "    decorated.forEach(function (d) { d.u.place(); });\n",
             "",
             "test_the_announcement_comes_after_the_rows_have_actually_moved"),
    Mutation("the sort stops announcing itself", "static/js/table-sort.js",
             "    table.dispatchEvent(new CustomEvent('prism:tablesorted', {",
             "    table.dispatchEvent(new CustomEvent('prism:NOTHING', {",
             "test_the_sort_announces_that_the_order_moved"),
    Mutation("a no-op reorder resets the reader to page 1 every 15s",
             "static/js/table-sort.js",
             "    var alreadyInOrder = decorated.every(function (d, pos) { return d.i === pos; });\n"
             "    if (alreadyInOrder) return;\n",
             "",
             "test_nothing_is_announced_when_the_order_did_not_change"),
    Mutation("pagination stops following the sort", "templates/servers.html",
             "  document.body.addEventListener('prism:tablesorted', function (e) {",
             "  document.body.addEventListener('prism:NOTHING', function (e) {",
             "test_the_pagination_recomputes_when_the_order_changes"),
    Mutation("the reader is left on a page number that no longer means anything",
             "templates/servers.html",
             "      page = 1;   // a new order means page 1 holds different rows; staying on",
             "      // a new order means page 1 holds different rows; staying on",
             "test_the_pagination_recomputes_when_the_order_changes"),
    Mutation("the page slice is taken from a stale snapshot",
             "templates/servers.html",
             "    return Array.prototype.filter.call(table.tBodies, function (tb) {",
             "    return Array.prototype.filter.call([], function (tb) {",
             "test_the_page_slice_is_read_from_the_dom_and_not_from_a_snapshot"),
    Mutation("off-page rows are removed from the document",
             "templates/servers.html",
             "      tb.style.display = (i >= first && i < last) ? '' : 'none';",
             "      if (!(i >= first && i < last)) tb.remove();",
             "test_rows_off_the_page_are_hidden_and_never_removed"),
    Mutation("the page size drifts to something arbitrary",
             "templates/servers.html", "  const PAGE_SIZE = 25;",
             "  const PAGE_SIZE = 100;",
             "test_the_page_size_is_the_agreed_twenty_five"),
    Mutation("a boundary button is disabled with no reason",
             "templates/servers.html",
             "    prismSetDisabled(el('servers-page-prev'),",
             "    el('servers-page-prev').disabled = !!(",
             "test_the_boundary_buttons_say_why_they_are_unavailable"),
    Mutation("the band is flattened into a single A-Z run",
             "templates/partials/server_grid.html",
             '<div class="server-band-segment">', "<div>",
             "test_the_band_keeps_its_type_segments"),
    Mutation("segment order goes back to cache-iteration order",
             "routes/views.py",
             "        grouped = {k: grouped[k] for k in sorted(grouped)}\n", "",
             "test_both_levels_of_the_band_ordering_are_decided_server_side"),
    Mutation("cards within a segment are no longer alphabetical",
             "routes/views.py",
             '        for s in sorted(servers, key=lambda r: (r.get("server_name") or "").lower()):',
             "        for s in servers:",
             "test_both_levels_of_the_band_ordering_are_decided_server_side"),
    Mutation("the band becomes mouse-only past the first screenful",
             "templates/partials/server_grid.html",
             '<div class="server-band" tabindex="0" role="group"',
             '<div class="server-band" role="group"',
             "test_the_band_is_reachable_from_a_keyboard"),
    Mutation("the hidden band polls the whole fleet every 5s",
             "templates/servers.html",
             '''hx-get="/partials/server-grid" hx-trigger="prismBandLoad"''',
             '''hx-get="/partials/server-grid" hx-trigger="load, prismRefresh from:body"''',
             "test_the_hidden_card_view_does_not_fetch_the_whole_fleet_every_refresh"),
    Mutation("the band scroller becomes rounded", "static/css/app.css",
             "  overflow-x: auto;\n  overflow-y: hidden;",
             "  overflow-x: auto;\n  overflow-y: hidden;\n  border-radius: 1rem;",
             "test_the_band_scroller_is_not_rounded"),
    # ── the toggle's hover: the (0,2,0)-vs-(0,2,0) trap, third instance
    Mutation("the toggle rules lose their element qualifier",
             "static/css/app.css",
             "button.servers-view-btn[aria-pressed=\"true\"] {\n"
             "  background: rgb(var(--c-brand));",
             ".servers-view-btn[aria-pressed=\"true\"] {\n"
             "  background: rgb(var(--c-brand));",
             "test_the_toggle_cannot_be_overridden_by_a_tailwind_utility"),
    Mutation("the hover utility comes back onto the toggle",
             "templates/servers.html",
             'class="servers-view-btn px-3 py-1.5 text-sm font-medium flex items-center gap-1 border-l border-line"',
             'class="servers-view-btn px-3 py-1.5 text-sm font-medium flex items-center gap-1 border-l border-line hover:bg-page"',
             "test_the_toggle_cannot_be_overridden_by_a_tailwind_utility"),
    Mutation("the dark-mode ink override goes, inverting the pressed label",
             "static/css/app.css",
             ".dark button.servers-view-btn[aria-pressed=\"true\"] {",
             ".dark button.servers-view-btn-NONE[aria-pressed=\"true\"] {",
             "test_the_pressed_label_stays_readable_in_both_themes"),
    Mutation("the hover snaps instead of transitioning", "static/css/app.css",
             "  transition: background-color var(--dur-fast) var(--ease-standard),\n"
             "              color var(--dur-fast) var(--ease-standard);",
             "  color: inherit;",
             "test_the_hover_uses_the_motion_scale"),
    Mutation("the transition sweeps in layout properties too",
             "static/css/app.css",
             "  transition: background-color var(--dur-fast) var(--ease-standard),\n"
             "              color var(--dur-fast) var(--ease-standard);",
             "  transition: all var(--dur-fast) var(--ease-standard);",
             "test_the_hover_uses_the_motion_scale"),
    Mutation("a second carrier for the pressed state comes back",
             "templates/servers.html",
             "    if (cb) cb.setAttribute('aria-pressed', String(cards));",
             "    if (cb) cb.classList.add('active');",
             "test_the_pressed_state_has_exactly_one_carrier"),
    Mutation("the view mode becomes a permanent setting",
             "templates/servers.html",
             "      return sessionStorage.getItem(VIEW_KEY) === 'cards' ? 'cards' : 'table';",
             "      return localStorage.getItem(VIEW_KEY) === 'cards' ? 'cards' : 'table';",
             "test_the_remembered_view_lasts_for_the_tab_and_not_forever"),
    Mutation("blocked storage takes the page down with it",
             "templates/servers.html",
             "      return 'table';   // storage disabled — the default, not a crash",
             "      throw e;",
             "test_storage_being_unavailable_costs_the_preference_and_nothing_else"),
    Mutation("the filter hides the card and leaves its slot",
             "templates/servers.html",
             "      const slot = card.closest('.server-band-item') || card;",
             "      const slot = card;",
             "test_the_filter_hides_the_slot_and_not_just_the_card"),
)

# ── scroll: the rule now covers stylesheet-authored containers too ────────
#
# These two used to sit in the `servers-view` suite above and BOTH reported
# themselves as correctly caught while no test ran — their `-k` expressions
# name tests in test_design_scroll.py, which that suite never opens. See the
# TEST NOT FOUND branch in run_suite() for the mechanism.
suite(
    "scroll",
    Mutation("the band scroller becomes rounded, unseen by the utility scan",
             "static/css/app.css",
             "  overflow-x: auto;\n  overflow-y: hidden;",
             "  overflow-x: auto;\n  overflow-y: hidden;\n  border-radius: 1rem;",
             "test_no_stylesheet_rule_makes_one_element_both_rounded_and_scrolling"),
    Mutation("the CSS scroll detector stops matching anything",
             "tests/test_design_scroll.py",
             '_CSS_SCROLLS = re.compile(r"overflow(?:-[xy])?\\s*:\\s*(?:auto|scroll)")',
             '_CSS_SCROLLS = re.compile(r"overflow-NOTHING")',
             "test_the_stylesheet_detector_matches_the_shape_it_guards_against"),
    Mutation("the staleness check stops detecting anything",
             "tests/test_design_scroll.py",
             "    return allowlist - {_selector_of(o) for o in _css_offenders()}",
             "    return set()",
             "test_the_staleness_check_can_detect_a_stale_entry"),
    Mutation("the utility-scoped detector stops matching its own offender",
             "tests/test_design_scroll.py",
             '_SCROLLS = re.compile(r"\\boverflow(?:-[xy])?-(?:auto|scroll)\\b")',
             '_SCROLLS = re.compile(r"overflow-NOTHING")',
             "test_the_detector_recognises_the_shape_it_guards_against"),
    # ── the pulse panel: shell, scroller, and the position that must survive
    Mutation("the pulse panel scrolls itself again", "static/css/app.css",
             ".pulse-panel-scroll {\n  overflow-y: auto;",
             ".pulse-panel-SCROLL-GONE {\n  overflow-y: auto;",
             "test_the_pulse_panel_is_a_shell_around_a_scroller"),
    Mutation("the shell stops clipping, so the radius never reaches the bar",
             "static/css/app.css",
             "  flex-direction: column;\n  overflow: hidden;\n  background: rgb(var(--c-card));",
             "  flex-direction: column;\n  background: rgb(var(--c-card));",
             "test_the_pulse_panel_is_a_shell_around_a_scroller"),
    Mutation("the radius moves onto the scroller — the defect, one element in",
             "static/css/app.css",
             ".pulse-panel-scroll {\n  overflow-y: auto;",
             ".pulse-panel-scroll {\n  border-radius: 8px;\n  overflow-y: auto;",
             "test_the_pulse_panel_is_a_shell_around_a_scroller"),
    Mutation("the render rebuilds the scroller, resetting scroll every poll",
             "static/js/pulse-monitor.js",
             "    panelScrollEl.innerHTML = `",
             "    panelScrollEl = document.createElement('div');\n"
             "    panelEl.replaceChildren(panelScrollEl);\n"
             "    panelScrollEl.innerHTML = `",
             "test_the_scroller_is_created_once_and_not_by_the_render"),
    Mutation("the render writes over the panel instead of into the scroller",
             "static/js/pulse-monitor.js",
             "    panelScrollEl.innerHTML = `",
             "    panelEl.innerHTML = `",
             "test_the_render_writes_into_the_scroller_and_not_over_it"),
    Mutation("a second scroller orphans the first and its position",
             "static/js/pulse-monitor.js",
             "      panelScrollEl = panelEl.querySelector('.pulse-panel-scroll');\n"
             "      if (!panelScrollEl) {\n",
             "      if (true) {\n",
             "test_creating_the_scroller_twice_cannot_orphan_the_first"),
    Mutation("showing the panel overrides its layout mode",
             "static/js/pulse-monitor.js",
             "    panelEl.style.display = '';",
             "    panelEl.style.display = 'block';",
             "test_showing_the_panel_does_not_override_its_layout_mode"),
    Mutation("the shell stops being a flex container", "static/css/app.css",
             "  display: flex;\n  flex-direction: column;\n  overflow: hidden;",
             "  overflow: hidden;",
             "test_showing_the_panel_does_not_override_its_layout_mode"),
)

# ── the health-check summary behind the Services card ────────────────────
suite(
    "health-summary",
    # Every anchor below moved when Wave 4 rewrote the query from a
    # GROUP-BY-the-history form to a config-driven correlated one, for
    # performance (82.64 ms -> 0.033 ms at the retention default). The eight
    # behavioural tests passed unchanged through that rewrite, which is what
    # made it safe; these are what prove they would have caught it going wrong.
    Mutation("the count is driven from the history table, not the config",
             "database.py",
             "                FROM health_check_config c\n                WHERE c.enabled = 1",
             "                FROM health_check_results c\n                WHERE 1 = 1",
             "test_each_configured_probe_counts_once_however_often_it_ran"),
    Mutation("the oldest result wins instead of the newest",
             "database.py", "                    ORDER BY r.id DESC",
             "                    ORDER BY r.id ASC",
             "test_the_latest_result_wins_not_the_first"),
    Mutation("two probes on one host collapse into one", "database.py",
             "                    WHERE r.server_name = c.server_name\n"
             "                      AND r.check_type  = c.check_type\n"
             "                      AND r.target_host = c.target_host\n"
             "                      AND r.target_port = c.target_port",
             "                    WHERE r.server_name = c.server_name",
             "test_two_probes_on_one_host_stay_two"),
    Mutation("a switched-off probe is reported as down", "database.py",
             "                WHERE c.enabled = 1\n", "",
             "test_a_disabled_probe_is_not_counted_at_all"),
    Mutation("a never-probed service is reported as up", "database.py",
             '                summary["up" if status == "up" else\n'
             '                        "down" if status == "down" else "unknown"] += 1',
             '                summary["down" if status == "down" else "up"] += 1',
             "test_a_probe_with_no_result_yet_is_unknown"),
    Mutation("an unrecognised status is silently dropped", "database.py",
             '                        "down" if status == "down" else "unknown"] += 1\n'
             '                summary["total"] += 1',
             '                        "down" if status == "down" else "total"] += 1\n'
             '                summary["total"] += 1',
             "test_the_buckets_always_sum_to_the_total"),
    # ── the cost characteristic, which no behavioural test can see
    Mutation("the query goes back to scanning the whole history table",
             "database.py",
             "                SELECT (\n"
             "                    SELECT r.status\n"
             "                    FROM health_check_results r\n"
             "                    WHERE r.server_name = c.server_name\n"
             "                      AND r.check_type  = c.check_type\n"
             "                      AND r.target_host = c.target_host\n"
             "                      AND r.target_port = c.target_port\n"
             "                    ORDER BY r.id DESC\n"
             "                    LIMIT 1\n"
             "                ) AS status\n"
             "                FROM health_check_config c\n"
             "                WHERE c.enabled = 1",
             "                SELECT r.status AS status\n"
             "                FROM health_check_config c\n"
             "                LEFT JOIN (\n"
             "                    SELECT hr.* FROM health_check_results hr\n"
             "                    INNER JOIN (\n"
             "                        SELECT server_name, check_type, target_host,\n"
             "                               target_port, MAX(id) AS max_id\n"
             "                        FROM health_check_results\n"
             "                        GROUP BY server_name, check_type, target_host, target_port\n"
             "                    ) latest ON hr.id = latest.max_id\n"
             "                ) r\n"
             "                  ON r.server_name = c.server_name\n"
             "                 AND r.check_type  = c.check_type\n"
             "                 AND r.target_host = c.target_host\n"
             "                 AND r.target_port = c.target_port\n"
             "                WHERE c.enabled = 1",
             "test_the_summary_never_scans_the_whole_history_table"),
    Mutation("the covering index is dropped", "database.py",
             "CREATE INDEX IF NOT EXISTS idx_hc_results_probe\n"
             "    ON health_check_results(server_name, check_type, target_host, target_port, id);",
             "",
             "test_the_summary_never_scans_the_whole_history_table"),
    Mutation("the plan check reads a copy instead of the real query",
             "tests/test_health_check_summary.py",
             '    m = re.search(r\'conn\\.execute\\("""(.*?)"""\\)\', src, re.S)',
             '    m = re.search(r\'(ORDER BY r\\.id DESC)\', "ORDER BY r.id DESC c.enabled = 1 "\n'
             '                  "health_check_config health_check_results", re.S)',
             "test_the_plan_check_is_reading_the_query_the_method_actually_runs"),
)

# ── the literal ratchet's comment stripping ──────────────────────────────
suite(
    "literal-ratchet",
    # Two separate defects, and the first cut aimed both at the same test.
    # Removing the `_code_only` call from `_literal_counts` does not touch
    # `test_the_counter_reads_code…`, which exercises `_code_only` directly —
    # so it reported the test as blind when the test was simply not the one
    # that covers that line. The RATCHET is what regresses: servers.html holds
    # two literals inside a comment explaining their own removal, so an
    # unstripped count reads 11 against a baseline of 9.
    Mutation("the counter goes back to reading its own documentation",
             "tests/test_design_tokens.py",
             "            if (n := len(_LITERAL.findall(\n"
             "                _code_only(p.read_text(encoding=\"utf-8\")))))}",
             "            if (n := len(_LITERAL.findall(\n"
             "                p.read_text(encoding=\"utf-8\"))))}",
             "test_hardcoded_colour_literals_never_increase"),
    Mutation("the comment stripper stops stripping",
             "tests/test_design_tokens.py",
             "def _code_only(text: str) -> str:\n"
             "    blanked = _COMMENTS.sub(lambda m: re.sub(r\"[^\\n]\", \" \", m.group(0)), text)\n"
             "    return _LINE_COMMENT.sub(lambda m: \" \" * len(m.group(0)), blanked)",
             "def _code_only(text: str) -> str:\n    return text",
             "test_the_counter_reads_code_and_not_the_comments_about_it"),
    Mutation("the line-comment rule stops being anchored to the line start",
             "tests/test_design_tokens.py",
             '_LINE_COMMENT = re.compile(r"^[ \\t]*//[^\\n]*", re.M)\n\n\ndef _code_only',
             '_LINE_COMMENT = re.compile(r"//[^\\n]*")\n\n\ndef _code_only',
             "test_the_counter_reads_code_and_not_the_comments_about_it"),
)

# ── the status summary read from the cache instead of SQLite ──────────────
suite(
    "status-cache",
    Mutation("the completeness guard goes, so a partial cache understates",
             "routes/views.py",
             "    missing = configured - cache.keys()\n    if missing:",
             "    missing = set()\n    if missing:",
             "test_a_partial_cache_declines_rather_than_understating"),
    Mutation("a cold cache reports an empty fleet instead of declining",
             "routes/views.py",
             "    if not cache:\n        return None",
             "    if not cache:\n        return _fold_status_summary([])",
             "test_a_cold_cache_declines"),
    Mutation("the whole cache is folded, orphans included", "routes/views.py",
             "    return _fold_status_summary(cache[name] for name in configured)",
             "    return _fold_status_summary(cache.values())",
             "test_an_orphaned_cache_entry_is_not_counted"),
    Mutation("a bucket is dropped, so the cache disagrees with the query",
             "routes/views.py",
             '_STATUS_BUCKETS = ("healthy", "warning", "critical", "offline")',
             '_STATUS_BUCKETS = ("healthy", "warning", "critical")',
             "test_the_cache_and_the_query_produce_the_same_summary"),
    Mutation("an unknown status stops counting toward the total",
             "routes/views.py",
             '        if status in _STATUS_BUCKETS:\n'
             '            summary[status] += 1\n'
             '        summary["total"] += 1',
             '        if status in _STATUS_BUCKETS:\n'
             '            summary[status] += 1\n'
             '            summary["total"] += 1',
             "test_every_row_counts_toward_the_total_but_only_known_statuses_bucket"),
    Mutation("bucketing goes back to matching the summary's own keys",
             "routes/views.py",
             "        if status in _STATUS_BUCKETS:",
             "        if status in summary:",
             "test_a_status_named_like_a_bucket_key_cannot_clobber_the_total"),
    Mutation("the vitals context reads the query unconditionally again",
             "routes/views.py",
             "    summary = _status_summary_from_cache(names)\n    if summary is None:",
             "    summary = None\n    if summary is None:",
             "test_a_complete_cache_means_the_query_never_runs"),
    Mutation("the cache path loses its fallback, rendering summary=None",
             "routes/views.py",
             "        try:\n            summary = _db.get_status_summary()\n"
             "        except Exception:\n"
             '            logger.exception("vitals: could not read the status summary")\n'
             "            summary = None",
             "        summary = None",
             "test_a_complete_cache_means_the_query_never_runs"),
    Mutation("the hero banner goes back to its own query", "routes/views.py",
             "        ctx = _vitals_context()\n"
             "        return render_template(\"partials/verdict_header.html\",\n"
             "                               summary=ctx[\"summary\"],\n"
             "                               server_count=ctx[\"server_count\"])",
             "        summary = _db.get_status_summary()\n"
             "        server_count = len(_config.get_servers())\n"
             "        return render_template(\"partials/verdict_header.html\",\n"
             "                               summary=summary, server_count=server_count)",
             "test_only_one_place_reads_the_status_summary_for_the_dashboard"),
    # Structural, because no single-threaded test can see a missing lock and a
    # race-provoking one would be flaky. The mutation is what proves the
    # structural assertion is not decorative.
    Mutation("the cache is read without the lock that stops an intermittent 500",
             "routes/views.py",
             "        with _state._state_lock:\n"
             "            cache = dict(_state.latest_by_server or {})",
             "        cache = _state.latest_by_server or {}",
             "test_the_cache_is_snapshotted_under_the_lock"),
    Mutation("the cache is aliased inside the lock instead of copied",
             "routes/views.py",
             "            cache = dict(_state.latest_by_server or {})",
             "            cache = _state.latest_by_server or {}",
             "test_the_cache_is_snapshotted_under_the_lock"),
)

# ── outbound: Prism may not grow a new way to phone out ──────────────────
#
# These back the claim in docs/DATA_FLOWS.md. The fourth is the one with a
# lesson attached: the first version of it removed ONE of tls_checker.py's
# three `create_connection` calls and reported the test blind. The mutation
# landed perfectly and changed nothing the test measures, because the file
# still had two more sites of the same kind — the ratchet works on (file,
# kind) pairs, not on call counts. Re-aimed at winrm_factory.py, which has
# exactly one WinRM site, and it is caught. `landed` proves the file changed,
# never that behaviour did.
suite(
    "outbound",
    Mutation("a beacon URL is added as a module constant",
             "webhooks.py",
             "import urllib.parse",
             "import urllib.parse\n"
             "_TELEMETRY = 'https://metrics.vendor.example/ingest'",
             "test_no_external_host_literal_in_shipped_python"),
    Mutation("an outbound call gains a hardcoded destination",
             "health_checker.py",
             "        conn = socket.create_connection((host, port), timeout=timeout)",
             "        conn = socket.create_connection('updates.vendor.example', timeout=timeout)",
             "test_no_outbound_call_site_has_a_literal_destination"),
    Mutation("a new file learns to open a socket",
             "analytics.py",
             "import logging",
             "import logging\nimport smtplib\n\n"
             "def _phone_home(h):\n    return smtplib.SMTP(h)",
             "test_the_outbound_call_sites_match_the_audited_set"),
    Mutation("the last site of an audited path goes, baseline left stale",
             "winrm_factory.py",
             "    return WSMan(server_config.host, **kwargs)",
             "    return _stub_transport(server_config.host, **kwargs)",
             "test_the_outbound_baseline_is_not_left_stale"),
)

# ── health-tls: an HTTPS check validates the certificate ─────────────────
#
# Five mutations for four layers, because the setting is only as strong as the
# weakest link carrying it. Two of these were written FIRST against the
# database tests and reported blind — correctly: a DB test cannot see the API
# parsing a payload or the runner passing an argument. The gap was real, the
# tests for those two links did not exist, and they do now.
suite(
    "health-tls",
    Mutation("https checks stop verifying certificates",
             "health_checker.py",
             "    ctx = ssl.create_default_context()\n    if not verify_tls:",
             "    ctx = ssl.create_default_context()\n    if True:",
             "test_an_https_check_verifies_the_certificate_by_default"),
    Mutation("the chain is checked but the hostname is not",
             "health_checker.py",
             "    ctx = ssl.create_default_context()\n    if not verify_tls:",
             "    ctx = ssl.create_default_context()\n"
             "    ctx.check_hostname = False\n    if not verify_tls:",
             "test_an_https_check_verifies_the_certificate_by_default"),
    Mutation("an omitted API field silently weakens the check",
             "routes/api/health.py",
             "    return True if value is None else bool(value)",
             "    return bool(value)",
             "test_an_omitted_field_cannot_weaken_the_check"),
    Mutation("the ON CONFLICT update drops verify_tls",
             "database.py",
             "                        verify_tls = excluded.verify_tls\"\"\"",
             "                        name = excluded.name\"\"\"",
             "test_an_update_can_turn_verification_off_and_back_on"),
    Mutation("the runner hardcodes verification off for every check",
             "healthchecks.py",
             "                                verify_tls=True if _vt is None else bool(_vt))",
             "                                verify_tls=False)",
             "test_the_runner_passes_the_stored_setting_to_the_probe"),
    # Both of these were review findings, not hypotheticals.
    Mutation("a NULL verify_tls falls through to not verifying",
             "healthchecks.py",
             "                                verify_tls=True if _vt is None else bool(_vt))",
             "                                verify_tls=bool(cfg.get(\"verify_tls\", 1)))",
             "test_a_null_verify_tls_still_verifies"),
    Mutation("the upsert assigns instead of coalescing, blanking a partial edit",
             "database.py",
             "                        http_path = COALESCE(excluded.http_path, health_check_config.http_path),",
             "                        http_path = excluded.http_path,",
             "test_editing_one_field_does_not_blank_the_others"),
    # The UI carrier. A setting reachable from no screen is not a setting.
    Mutation("the form stops sending the checkbox",
             "templates/servers.html",
             "name: name, verify_tls: verifyTls }),",
             "name: name }),",
             "test_the_form_sends_the_setting_when_saving"),
    Mutation("editing forgets the opt-out and re-enables verification",
             "templates/servers.html",
             "checked = hc.verify_tls !== 0;",
             "checked = true;",
             "test_editing_a_check_repopulates_the_setting"),
    Mutation("an absent name is coerced to empty and blanks the stored one",
             "routes/api/health.py",
             "name = _raw_name.strip() if isinstance(_raw_name, str) else None",
             "name = (data.get('name') or '').strip()",
             "test_an_absent_name_is_not_the_same_as_an_empty_one"),
)

# ── severity-vocab: the six words and the one order (WP-1 phase 1) ───────
suite(
    "severity-vocab",
    Mutation("two adjacent states swap rank (down outranks unsteady)",
             "severity_vocab.py",
             '    "unsteady", "down", "impacted", "degraded", "chronic", "unknown", "healthy",',
             '    "down", "unsteady", "impacted", "degraded", "chronic", "unknown", "healthy",',
             "test_each_ratified_adjacency_holds"),
    Mutation("worst() of nothing claims healthy instead of unknown",
             "severity_vocab.py",
             '        return "unknown"\n    return min(states, key=lambda s: PRECEDENCE[s])',
             '        return "healthy"\n    return min(states, key=lambda s: PRECEDENCE[s])',
             "test_worst_of_nothing_is_unknown"),
    Mutation("critical maps to its own word instead of degraded",
             "severity_vocab.py",
             '    "critical": "degraded",',
             '    "critical": "critical",',
             "test_every_stored_status_maps_to_a_vocabulary_word"),
    Mutation("an unrecognised stored status KeyErrors a page render",
             "severity_vocab.py",
             '    return _STORED_TO_WORD.get(stored or "", "unknown")',
             '    return _STORED_TO_WORD[stored or ""]',
             "test_an_unrecognised_stored_status_maps_to_unknown"),
    Mutation("the grammar accepts a multiline cause",
             "severity_vocab.py",
             '    if "\\n" in cause:',
             '    if False:',
             "test_the_grammar_refuses_a_multiline_cause"),
)

# ── severity-roles: precedence, seeds, and the writer path ────────────────
suite(
    "severity-roles",
    Mutation("the override loses to the type seed",
             "severity_roles.py",
             '    if override in ROLES:\n        return override, "override"',
             '    if False:\n        return override, "override"',
             "test_an_explicit_override_wins_over_the_type_seed"),
    Mutation("a garbage override is honoured instead of ignored",
             "severity_roles.py",
             '    if override in ROLES:',
             '    if override:',
             "test_a_garbage_override_is_ignored_not_honoured"),
    Mutation("the DC seed is demoted to important",
             "severity_roles.py",
             '    "domain_controller": "critical_infrastructure",',
             '    "domain_controller": "important",',
             "test_the_required_outcomes_fall_out_of_the_seeds"),
    Mutation("tier sneaks into role resolution",
             "severity_roles.py",
             '    override = (_field(server, "criticality") or "").strip()',
             '    if int(_field(server, "tier", 1)) == 0:\n'
             '        return "critical_infrastructure", "override"\n'
             '    override = (_field(server, "criticality") or "").strip()',
             "test_tier_is_never_consulted"),
    Mutation("settings type_roles stop overlaying the code seeds",
             "severity_roles.py",
             '    type_map = {**TYPE_ROLES, **(model.get("type_roles") or {})}',
             '    type_map = dict(TYPE_ROLES)',
             "test_settings_can_reseed_a_type_fleet_wide"),
    Mutation("severity_model vanishes from the settings merge",
             "config_manager.py",
             '        "severity_model": {',
             '        "severity_model_DISABLED": {',
             "test_severity_model_survives_the_settings_merge"),
    Mutation("the config API stops validating criticality",
             "routes/api/config.py",
             '            if crit_raw not in ROLES:',
             '            if False:',
             "test_the_config_api_rejects_an_unknown_criticality"),
)

# ── maintenance-expiry: the forever-mute is dead (WP-1 phase 1) ───────────
suite(
    "maintenance-expiry",
    Mutation("the matcher stops refusing expired windows",
             "maintenance.py",
             '        if _window_expired(window):\n            continue',
             '        if False:\n            continue',
             "test_an_expired_adhoc_window_never_matches_even_before_the_sweep"),
    Mutation("a malformed expiry fails OPEN (keeps muting)",
             "maintenance.py",
             '        return True\n    return exp <= datetime.datetime.now(datetime.timezone.utc)',
             '        return False\n    return exp <= datetime.datetime.now(datetime.timezone.utc)',
             "test_a_malformed_expiry_disables_the_window_not_the_feature"),
    Mutation("ad-hoc windows demand a schedule again",
             "maintenance.py",
             '        if _is_adhoc(window):\n            return window',
             '        if False:\n            return window',
             "test_an_adhoc_window_needs_no_schedule_fields"),
    Mutation("the sweep deletes malformed windows (destroys evidence)",
             "maintenance.py",
             '            return False        # malformed: keep visible, matcher fails closed',
             '            return True',
             "test_the_sweep_leaves_malformed_expiries_in_place_but_they_do_not_match"),
    Mutation("the sweep rewrites settings even when nothing expired",
             "maintenance.py",
             '    if removed:\n        config_manager.save_maintenance_windows(kept)',
             '    if True:\n        config_manager.save_maintenance_windows(kept)',
             "test_the_sweep_is_a_noop_when_nothing_expired"),
)

# ── estate-fold: the weighted fold, rails, dwell, freeze, latch ───────────
#
# The core of WP-1 phase 2. The rails and the dwell guard are the promises
# the round table made to the owner; each gets a mutation that breaks the
# promise specifically.
suite(
    "estate-fold",
    Mutation("the critical-infrastructure rail is removed",
             "estate_fold.py",
             '    if rail_rank == 2 and _BAND_RANK[band] < 2:\n        band = "urgent"',
             '    if False:\n        band = "urgent"',
             "test_a_critical_infrastructure_host_down_rails_to_urgent"),
    Mutation("the any-host-down rail is removed",
             "estate_fold.py",
             '    elif rail_rank == 1 and _BAND_RANK[band] < 1:\n        band = "elevated"',
             '    elif False:\n        band = "elevated"',
             "test_any_host_down_rails_to_at_least_elevated"),
    Mutation("band boundaries become exclusive",
             "estate_fold.py",
             '        if score >= threshold:',
             '        if score > threshold:',
             "test_band_boundaries_are_inclusive"),
    Mutation("a server in maintenance keeps firing rails",
             "estate_fold.py",
             '        if s.get("in_maintenance"):\n            excluded += 1\n            continue',
             '        if s.get("in_maintenance"):\n            excluded += 1',
             "test_maintenance_excludes_a_server_from_score_AND_rails"),
    Mutation("the latch substitution is ignored by the fold",
             "estate_fold.py",
             '    status = server.get("latched_worst") or server.get("status") or ""\n    return status in ("critical", "offline", "down")',
             '    status = server.get("status") or ""\n    return status in ("critical", "offline", "down")',
             "test_a_latched_server_contributes_its_worst_recent_state"),
    Mutation("upward moves wait for the dwell too",
             "estate_fold.py",
             '    if new > cur:\n        # Upward: instant, freeze pierced by construction.\n        return _record_change(state, raw_band, now)',
             '    if new > cur:\n        if state.dwell_opened_at is None:\n            return replace(state, dwell_opened_at=now, dwell_target=raw_band)\n        return _record_change(state, raw_band, now)',
             "test_an_upward_move_displays_the_same_tick"),
    Mutation("the dwell shrinks to one poll interval",
             "estate_fold.py",
             '    dwell_needed = max(2 * poll_interval, 60)',
             '    dwell_needed = max(poll_interval, 30)',
             "test_a_downward_move_waits_for_the_dwell"),
    Mutation("the freshness guard accepts one answer",
             "estate_fold.py",
             '    answers_ok = bool(fresh_counts) and all(v >= 2 for v in fresh_counts.values())',
             '    answers_ok = bool(fresh_counts) and all(v >= 1 for v in fresh_counts.values())',
             "test_the_dwell_freshness_guard_blocks_on_one_answer"),
    Mutation("a relapse no longer cancels the dwell",
             "estate_fold.py",
             '    return replace(state, displayed_band=band, change_times=times,\n                   frozen=frozen, dwell_opened_at=None, dwell_target=None)',
             '    return replace(state, displayed_band=band, change_times=times,\n                   frozen=frozen)',
             "test_a_relapse_during_the_dwell_cancels_it"),
    Mutation("the freeze needs four changes instead of three",
             "estate_fold.py",
             '    if not frozen and len(times) >= _FREEZE_TRIP_CHANGES:',
             '    if not frozen and len(times) > _FREEZE_TRIP_CHANGES:',
             "test_the_third_band_change_in_30min_freezes_at_the_highest"),
    Mutation("the freeze blocks upward moves too",
             "estate_fold.py",
             '    if new > cur:\n        # Upward: instant, freeze pierced by construction.\n        return _record_change(state, raw_band, now)',
             '    if new > cur and not state.frozen:\n        return _record_change(state, raw_band, now)',
             "test_rails_pierce_the_freeze_upward"),
    Mutation("the freeze never releases",
             "estate_fold.py",
             '    if (state.raw_stable_since is not None\n            and now - state.raw_stable_since >= _FREEZE_RELEASE_STABLE_S):',
             '    if False:',
             "test_the_freeze_releases_after_ten_minutes_of_raw_stability"),
    Mutation("five transitions latch instead of six",
             "estate_fold.py",
             '_LATCH_K = 6',
             '_LATCH_K = 5',
             "test_five_transitions_do_not_latch"),
    Mutation("old transitions never age out of the latch window",
             "estate_fold.py",
             '    recent = [t for t in transition_times if now - t <= _LATCH_WINDOW_S]\n    return len(recent) >= _LATCH_K',
             '    return len(transition_times) >= _LATCH_K',
             "test_old_transitions_age_out_of_the_window"),
    Mutation("the latch releases while the window is still noisy",
             "estate_fold.py",
             '    return len(recent) <= _LATCH_RELEASE_MAX',
             '    return len(recent) <= 3',
             "test_the_latch_releases_only_when_the_window_is_quiet"),
    Mutation("why_not_higher goes silent",
             "estate_fold.py",
             '    rail_part = "a rail is armed" if rail else "no rail is armed"',
             '    return ""\n    rail_part = "a rail is armed" if rail else "no rail is armed"',
             "test_why_not_higher_names_the_distance_or_the_unarmed_rail"),
)

# ── estate-service: the wiring seam (WP-1 phase 2) ───────────────────────
suite(
    "estate-service",
    Mutation("roles stop being resolved — every server weighs the same",
             "estate_service.py",
             '        role, _source = resolve_role(s, settings)',
             '        role = "important"',
             "test_snapshot_resolves_role_and_weight_from_config"),
    Mutation("maintenance is never marked on the snapshot",
             "estate_service.py",
             '            "in_maintenance": _safe_in_maintenance(is_in_maintenance, s.name, settings),',
             '            "in_maintenance": False,',
             "test_snapshot_marks_maintenance_from_settings"),
    Mutation("an uncached server silently reads healthy",
             "estate_service.py",
             '                              or row.get("status") or "unknown")',
             '                              or row.get("status") or "healthy")',
             "test_snapshot_status_defaults_to_unknown_when_not_yet_cached"),
    Mutation("the latch substitution never reaches the fold",
             "estate_service.py",
             '            "latched_worst": latched_worst.get(s.name),',
             '            "latched_worst": None,',
             "test_snapshot_carries_the_latched_worst_when_present"),
    Mutation("the unmeasured gate is removed",
             "estate_service.py",
             '    if measured <= 0:\n        return "unmeasured"',
             '    if False:\n        return "unmeasured"',
             "test_configured_but_nothing_measured_is_unmeasured"),
    Mutation("the flat gate swallows an estate with survivors",
             "estate_service.py",
             '    if down >= measured and down > 0:\n        return "flat"',
             '    if down > 0:\n        return "flat"',
             "test_one_survivor_is_not_flat"),
    Mutation("fleet freshness never advances — the dwell can never clear",
             "estate_service.py",
             '        if newest is not None and newest != _fleet_token:',
             '        if False:',
             "test_fleet_freshness_gates_the_dwell_via_cache_timestamps"),
    Mutation("freshness is not reset when a new dwell opens",
             "estate_service.py",
             '        if _fold_state.dwell_opened_at != _dwell_marker:',
             '        if False:',
             "test_fleet_freshness_gates_the_dwell_via_cache_timestamps"),
    Mutation("a stale verdict is served forever",
             "estate_service.py",
             '        if age > _VERDICT_TTL_S:\n            return None',
             '        if False:\n            return None',
             "test_a_stale_verdict_expires_rather_than_being_trusted"),
)

# ── cascade: closure, cycles, the reducer, root election (WP-1 phase 3) ───
suite(
    "cascade",
    Mutation("BFS stops deduplicating — depth becomes longest-path",
             "cascade.py",
             '            if node == start or node in seen:\n                continue\n'
             '            seen[node] = depth\n'
             '            for nxt in adjacency.get(node, ()):\n'
             '                if nxt not in seen and nxt != start:',
             '            if node == start:\n                continue\n'
             '            seen[node] = depth\n'
             '            for nxt in adjacency.get(node, ()):\n'
             '                if nxt != start:',
             "test_unequal_paths_record_the_SHORTEST_depth"),
    Mutation("self-edges are followed instead of ignored",
             "cascade.py",
             '        if not dep or not up or dep == up:',
             '        if not dep or not up:',
             "test_closure_ignores_a_self_edge_rather_than_looping"),
    Mutation("indirect cycles are no longer detected",
             "cascade.py",
             '    closure = build_closure(edges)\n    return dependent in closure["upstream"].get(upstream, {})',
             '    return any(e.get("server_name") == upstream and e.get("depends_on") == dependent for e in edges)',
             "test_an_indirect_cycle_is_rejected"),
    Mutation("a healthy server under a dead upstream is called impacted",
             "cascade.py",
             '    if own_status not in _FAILED:',
             '    if False:',
             "test_a_healthy_server_is_never_impacted"),
    Mutation("a failing server with a HEALTHY upstream is muted anyway",
             "cascade.py",
             '    failed_ups = [(name, depth) for name, depth in ups.items()\n                  if _is_failed(name, states, groups)]',
             '    failed_ups = [(name, depth) for name, depth in ups.items()]',
             "test_a_failing_server_with_a_HEALTHY_upstream_owns_its_failure"),
    Mutation("the NEAREST failed upstream is blamed, not the root",
             "cascade.py",
             '    root = max(failed_ups, key=lambda pair: pair[1])[0]',
             '    root = min(failed_ups, key=lambda pair: pair[1])[0]',
             "test_the_deepest_failed_upstream_is_named_not_the_nearest"),
    Mutation("a warning downstream of a dead upstream gets muted",
             "cascade.py",
             '_FAILED = frozenset({"offline", "down", "critical"})',
             '_FAILED = frozenset({"offline", "down", "critical", "warning"})',
             "test_a_warning_downstream_of_a_dead_upstream_stays_degraded"),
    Mutation("a group fails when ANY member is down (mutes on one DC reboot)",
             "cascade.py",
             '        return all(states.get(m) in _FAILED for m in members)',
             '        return any(states.get(m) in _FAILED for m in members)',
             "test_a_group_upstream_fails_only_when_every_member_is_down"),
    Mutation("an empty group fails vacuously and mutes the fleet",
             "cascade.py",
             '        if not members:\n            return False',
             '        if not members:\n            pass',
             "test_an_empty_group_never_fails"),
    Mutation("independent failures are collapsed into one root",
             "cascade.py",
             '        if not failed_ups:\n            roots.append(s)\n            continue',
             '        if not failed_ups and not roots:\n            roots.append(s)\n            continue',
             "test_two_independent_failures_elect_two_roots"),
    Mutation("election tie-breaks on latest onset instead of earliest",
             "cascade.py",
             '                              onsets.get(pair[0], float("inf")),',
             '                              -onsets.get(pair[0], 0.0),',
             "test_the_earliest_onset_wins_a_tie_between_two_candidate_roots"),
)

# ── cascade-persistence: the writer refuses cycles; promotion is one-way ──
suite(
    "cascade-persistence",
    Mutation("the writer stops rejecting cycles",
             "database.py",
             '        if would_create_cycle(existing, server_name, depends_on):',
             '        if False:',
             "test_a_cycle_creating_edge_is_refused"),
    Mutation("a rejected edge is written anyway before the raise",
             "database.py",
             '        from cascade import would_create_cycle\n        existing = self.get_all_dependencies()',
             '        from cascade import would_create_cycle\n        existing = []',
             "test_an_indirect_cycle_is_refused"),
    Mutation("removing an edge leaves the closure stale",
             "database.py",
             '                conn.execute("DELETE FROM server_dependencies WHERE id = ?", (dep_id,))\n                conn.commit()\n            finally:\n                conn.close()\n        self.rebuild_dependency_closure()',
             '                conn.execute("DELETE FROM server_dependencies WHERE id = ?", (dep_id,))\n                conn.commit()\n            finally:\n                conn.close()',
             "test_removing_an_edge_rebuilds_the_closure"),
    # The two promote_incident mutations that used to stand here are gone with
    # the method they broke: children are created AT promotion now, so there is
    # no UPDATE to make repeatable and no root row to mis-stamp. The invariants
    # they protected moved into the `promotion` suite below, aimed at the
    # mechanism that actually runs.
    Mutation("the row identity is editable after the fact",
             "database.py",
             '        allowed = {"status", "severity", "resolved_at", "resolved_by", "resolution_notes",\n                   "description", "root_cause_server", "title"}',
             '        allowed = {"status", "severity", "resolved_at", "resolved_by", "resolution_notes",\n                   "description", "root_cause_server", "title", "subject_server"}',
             "test_the_identity_of_a_row_is_not_updatable"),
)

# ── cascade-integration: seeding, the reducer in the fold, election ───────
suite(
    "cascade-integration",
    Mutation("a DC gets seeded a dependency on its own group",
             "cascade.py",
             '        (dcs if stype in _DC_TYPES else others).append(name)',
             '        others.append(name)\n'
             '        if stype in _DC_TYPES:\n            dcs.append(name)',
             "test_a_dc_never_depends_on_the_group_it_belongs_to"),
    Mutation("severed edges are seeded anyway (escape hatch removed)",
             "cascade.py",
             '             for n in others if n not in severed]',
             '             for n in others]',
             "test_a_severed_server_gets_no_assumed_edge"),
    Mutation("seeding cannot be switched off",
             "cascade.py",
             '    if model.get("seed_domain_group") is False:\n        return [], {}',
             '    if False:\n        return [], {}',
             "test_seeding_can_be_switched_off_entirely"),
    Mutation("a fleet with no DC seeds an empty group anyway",
             "cascade.py",
             '    if not dcs:\n        return [], {}',
             '    if False:\n        return [], {}',
             "test_a_fleet_with_no_domain_controller_seeds_nothing"),
    Mutation("assumed edges stop being labelled as assumed",
             "cascade.py",
             '"assumed": True,\n              "reason": "assumed — domain membership"}',
             '"assumed": False,\n              "reason": ""}',
             "test_assumed_edges_are_labelled_as_assumed"),
    Mutation("the reducer never reaches the snapshot",
             "estate_service.py",
             '        if closure:\n            state, reason_code, root = effective_severity(',
             '        if False:\n            state, reason_code, root = effective_severity(',
             "test_an_impacted_server_contributes_less_than_a_failed_one"),
    Mutation("the impacted root is not carried on the unit",
             "estate_service.py",
             '                impacted_by = root',
             '                impacted_by = None',
             "test_the_impacted_reason_travels_with_the_unit"),
    Mutation("an ongoing cascade respawns its incident every cycle",
             "analytics.py",
             '            if db.get_open_incident_by_subject(root):\n                continue',
             '            if False:\n                continue',
             "test_an_ongoing_cascade_reuses_its_incident"),
    Mutation("a lone failure with no dependents is dropped",
             "analytics.py",
             '    for root, children in sorted(roots.items()):',
             '    for root, children in sorted((r, c) for r, c in roots.items() if c):',
             "test_a_lone_failure_with_no_dependents_still_gets_its_incident"),
    Mutation("dedup keys on the title again instead of the subject",
             "database.py",
             '                "WHERE COALESCE(NULLIF(subject_server, \'\'), root_cause_server) = ? "',
             '                "WHERE title = ? "',
             "test_an_ongoing_cascade_reuses_its_incident"),
)

# ── promotion: the mute has an exit, and it fires exactly once ────────
suite(
    "promotion",
    Mutation("promotion fires on stale readings",
             "cascade.py",
             "    fresh = set(fresh or ())",
             "    fresh = set(states)",
             "test_promotion_is_fail_closed_when_the_child_is_not_fresh"),
    Mutation("a child is promoted while its root is still down",
             "cascade.py",
             "        if any(_is_failed(u, states, groups) for u in ups):\n            continue",
             "        if False:\n            continue",
             "test_a_child_is_not_promoted_while_its_root_is_still_down"),
    Mutation("a child that already owns an incident is promoted again",
             "cascade.py",
             "        if status not in _FAILED or name not in fresh or name in owners:",
             "        if status not in _FAILED or name not in fresh:",
             "test_a_child_that_already_owns_an_incident_is_not_promoted_again"),
    Mutation("the nearest hop is named as the origin, not the root",
             "cascade.py",
             "        out[name] = max(origins, key=lambda pair: pair[1])[0]",
             "        out[name] = min(origins, key=lambda pair: pair[1])[0]",
             "test_the_deepest_incident_owning_upstream_is_named_as_the_origin"),
    Mutation("an upstream that never owned an incident becomes an origin",
             "cascade.py",
             "        origins = [(u, d) for u, d in ups.items() if u in owners]",
             "        origins = [(u, d) for u, d in ups.items()]",
             "test_a_group_upstream_that_recovered_can_be_an_origin_without_an_incident"),
    Mutation("the open-incident snapshot is never populated",
             "analytics.py",
             "            if subject:\n                open_subjects.add(subject)",
             "            if False:\n                open_subjects.add(subject)",
             "test_a_promoted_incident_names_the_child_and_keeps_the_origin"),
    Mutation("a promoted child forgets the outage it came from",
             "analytics.py",
             "                root_cause_server=origin or root, description=desc,",
             "                root_cause_server=root, description=desc,",
             "test_a_promoted_incident_names_the_child_and_keeps_the_origin"),
    Mutation("the subject is not recorded, collapsing the identity rule",
             "analytics.py",
             "                title=title, severity=\"critical\", subject_server=root,",
             "                title=title, severity=\"critical\", subject_server=None,",
             "test_a_root_incident_carries_itself_as_its_subject"),
    # This one lives here rather than beside the schema mutations in
    # cascade-persistence: the defect is in the WRITER, so only a test that
    # runs the writer can see it. Aimed at test_an_ordinary_incident_is_not_
    # born_promoted (which only exercises create_incident) it landed cleanly
    # and reported itself as a blind test.
    Mutation("every incident is born promoted, so the marker means nothing",
             "analytics.py",
             "                promoted_at=stamp if origin else None)",
             "                promoted_at=stamp)",
             "test_a_root_incident_carries_itself_as_its_subject"),
    Mutation("the promotion marker is never appended",
             "analytics.py",
             "                _append_promotion_marker(db, root, origin)",
             "                pass",
             "test_a_promoted_incident_appends_a_marker_event_carrying_a_correlation_id"),
    Mutation("a NULL subject stops matching its root, hiding legacy rows",
             "database.py",
             '                "WHERE COALESCE(NULLIF(subject_server, \'\'), root_cause_server) = ? "',
             '                "WHERE subject_server = ? "',
             "test_a_legacy_incident_without_a_subject_is_found_by_its_root"),
    Mutation("dedup keys on the origin, so one orphan blocks its siblings",
             "database.py",
             '                "WHERE COALESCE(NULLIF(subject_server, \'\'), root_cause_server) = ? "',
             '                "WHERE root_cause_server = ? "',
             "test_a_promoted_incident_is_not_found_under_its_origin"),
    Mutation("auto-resolution closes a promoted child when its ORIGIN recovers",
             "analytics.py",
             '                subject = (detail.get("subject_server")\n                           or detail.get("root_cause_server"))',
             '                subject = detail.get("root_cause_server")',
             "test_auto_resolution_follows_the_subject_not_the_origin"),
    Mutation("a quiet fleet skips the correlation pass entirely",
             "collector_v2/aggregator.py",
             "        correlate = _correlate_events_fn()\n        if correlate is None:",
             "        if not _recent_events:\n            return\n        correlate = _correlate_events_fn()\n        if correlate is None:",
             "test_the_aggregator_runs_the_correlation_pass_on_a_quiet_fleet"),
    Mutation("the quiet pass returns without electing or promoting",
             "analytics.py",
             "        try:\n            _cascade_election(db, servers, settings)\n        except Exception:\n            logger.exception(\"Closure-driven cascade election failed\")\n        _auto_resolve_incidents(db)\n        return []",
             "        _auto_resolve_incidents(db)\n        return []",
             "test_the_quiet_pass_elects_too"),
)

# ── business-hours: the weight profile that must not become silence ────
suite(
    "business-hours",
    Mutation("the window ignores the configured zone and reads UTC",
             "severity_roles.py",
             '        tz = ZoneInfo(settings.get("timezone") or "UTC")',
             '        tz = ZoneInfo("UTC")',
             "test_the_configured_timezone_decides_and_not_utc"),
    Mutation("end_hour becomes inclusive, damping an hour late every day",
             "severity_roles.py",
             "        return start <= local.hour < end",
             "        return start <= local.hour <= end",
             "test_start_is_inclusive_and_end_is_exclusive"),
    Mutation("the weekend is treated as a working week",
             "severity_roles.py",
             "    if local.weekday() not in days:\n        return False",
             "    if False:\n        return False",
             "test_the_weekend_is_outside_the_window"),
    Mutation("a midnight-wrapping window reads as never inside",
             "severity_roles.py",
             "    return local.hour >= start or local.hour < end",
             "    return start <= local.hour < end",
             "test_a_window_that_wraps_midnight_is_honoured"),
    Mutation("an unconfigured window damps the weights anyway",
             "severity_roles.py",
             '    if not window.get("enabled"):\n        return None',
             "    if False:\n        return None",
             "test_the_window_is_off_unless_it_is_switched_on"),
    Mutation("no opinion is read as outside hours",
             "severity_roles.py",
             "    if now is None or is_business_hours(now, settings) is not False:",
             "    if now is None:",
             "test_an_unreadable_window_leaves_the_weights_alone"),
    Mutation("critical infrastructure softens overnight too",
             "severity_roles.py",
             '    "critical_infrastructure": 10,\n    "important": 2,',
             '    "critical_infrastructure": 4,\n    "important": 2,',
             "test_critical_infrastructure_never_weighs_less_outside_the_window"),
    Mutation("a weight of zero is honoured, making the machine invisible",
             "severity_roles.py",
             "        return max(1, int(outside[role]))",
             "        return int(outside[role])",
             "test_a_zero_outside_weight_is_floored_not_honoured"),
    Mutation("the site's own outside numbers are ignored",
             "severity_roles.py",
             "    outside = {**OUTSIDE_WEIGHTS, **((model.get(\"business_hours\") or {})\n                                     .get(\"outside_weights\") or {})}",
             "    outside = dict(OUTSIDE_WEIGHTS)",
             "test_the_outside_profile_is_configurable"),
    Mutation("the site-wide weight override is dropped inside the window",
             "severity_roles.py",
             "    base = int(overrides.get(role, WEIGHTS[role]))",
             "    base = int(WEIGHTS[role])",
             "test_the_site_wide_weight_override_still_wins_inside_the_window"),
    Mutation("the estate snapshot never passes its clock down",
             "estate_service.py",
             '            "weight": weight_for(role, settings, now=now),',
             '            "weight": weight_for(role, settings),',
             "test_the_estate_snapshot_applies_the_profile"),
)

# ── ingest-caps: bounds on what a semi-trusted host may send ────────
suite(
    "ingest-caps",
    Mutation("a cap of zero is honoured, so ingest discards everything",
             "ingest_caps.py",
             "            out[key] = max(1, int(value))",
             "            out[key] = int(value)",
             "test_a_cap_of_zero_is_floored_rather_than_honoured"),
    Mutation("an unreadable cap raises instead of degrading",
             "ingest_caps.py",
             "        except (TypeError, ValueError):",
             "        except NotImplementedError:",
             "test_an_unreadable_cap_falls_back_to_its_default"),
    Mutation("an unknown config key is trusted into the caps",
             "ingest_caps.py",
             "        if key not in DEFAULTS:\n            continue",
             "        if False:\n            continue",
             "test_an_unknown_key_is_ignored"),
    Mutation("an oversize payload is accepted",
             "ingest_caps.py",
             "    if raw is None or len(raw) <= limit:\n        return True",
             "    if True:\n        return True",
             "test_an_oversize_payload_is_rejected_and_counted"),
    Mutation("a payload exactly at the limit is refused (off-by-one)",
             "ingest_caps.py",
             "    if raw is None or len(raw) <= limit:",
             "    if raw is None or len(raw) < limit:",
             "test_a_payload_at_exactly_the_limit_is_accepted"),
    Mutation("the row cap does not bound anything",
             "ingest_caps.py",
             "    kept = rows[:row_limit]",
             "    kept = rows[:]",
             "test_rows_beyond_the_cap_are_dropped_and_counted"),
    Mutation("an overlong field is stored whole",
             "ingest_caps.py",
             "    if not isinstance(value, str) or len(value) <= limit:\n        return value, False",
             "    if True:\n        return value, False",
             "test_an_overlong_message_is_truncated_and_counted"),
    Mutation("a compliant payload is truncated anyway (off-by-one)",
             "ingest_caps.py",
             "    if not isinstance(value, str) or len(value) <= limit:",
             "    if not isinstance(value, str) or len(value) < limit:",
             "test_a_compliant_payload_passes_through_unchanged"),
    Mutation("rows are edited in place, so the log stops matching the store",
             "ingest_caps.py",
             "        new = dict(row)",
             "        new = row",
             "test_capping_does_not_mutate_the_caller_s_rows"),
    Mutation("a malformed row raises and stalls the worker",
             "ingest_caps.py",
             "        if not isinstance(row, dict):",
             "        if False:",
             "test_a_malformed_row_does_not_raise"),
    Mutation("an int field is stringified to satisfy a length rule",
             "ingest_caps.py",
             "    if not isinstance(value, str) or len(value) <= limit:\n        return value, False\n    return value[:limit], True",
             "    if len(str(value)) <= limit:\n        return value, False\n    return str(value)[:limit], True",
             "test_every_host_controlled_field_of_a_failed_login_is_capped"),
    Mutation("failed logins fall back to the generic row cap",
             "ingest_caps.py",
             '    return _cap_rows(rows, c["max_failed_logins"], c["max_message_chars"],',
             '    return _cap_rows(rows, c["max_rows_per_check"], c["max_message_chars"],',
             "test_failed_logins_use_their_own_row_cap"),
    Mutation("what the caps discarded is never counted",
             "ingest_caps.py",
             '        _bump("rows_dropped", dropped)',
             "        pass",
             "test_rows_beyond_the_cap_are_dropped_and_counted"),
    Mutation("the check layer returns the host's payload uncapped",
             "collector_v2/checks.py",
             "    return True, ingest_caps.cap_log_rows(parsed, server=server.name), None, None",
             "    return True, parsed, None, None",
             "test_the_check_layer_caps_what_the_target_returned"),
    Mutation("the giant payload reaches json.loads",
             "collector_v2/checks.py",
             "            if raw and not ingest_caps.payload_ok(raw, server=server_name):",
             "            if False:",
             "test_the_winrm_reader_refuses_a_giant_payload_before_parsing"),
    Mutation("the log writer trusts its caller",
             "database.py",
             "        logs_list = ingest_caps.cap_log_rows(logs_list, caps, server=server_name)",
             "        logs_list = list(logs_list)",
             "test_the_writer_caps_too_even_when_the_collector_did_not"),
    Mutation("the failed-login writer trusts its caller",
             "database.py",
             "        logins = ingest_caps.cap_failed_logins(logins, caps, server=server_name)",
             "        logins = list(logins)",
             "test_the_failed_login_writer_caps_too"),
    Mutation("the failed-login query goes back to unbounded",
             "collector_v2/scripts.py",
             "Id=4625,4740; StartTime=$cutoff} -MaxEvents 200 -ErrorAction",
             "Id=4625,4740; StartTime=$cutoff} -ErrorAction",
             "test_the_failed_login_query_asks_for_a_bounded_number_of_events"),
)

# ── winrm-transport: the default, and the sentence that survives it ────
suite(
    "winrm-transport",
    Mutation("a new server is monitored over plain HTTP again",
             "models.py",
             '            use_https=bool(data.get("use_https", True)),',
             '            use_https=bool(data.get("use_https", False)),',
             "test_a_server_added_without_an_opinion_gets_https"),
    Mutation("the dataclass default disagrees with from_dict",
             "models.py",
             "    use_https: bool = True",
             "    use_https: bool = False",
             "test_a_bare_server_config_defaults_to_https"),
    Mutation("an explicit False is overridden by the new default",
             "models.py",
             '            use_https=bool(data.get("use_https", True)),',
             "            use_https=True,",
             "test_an_explicit_false_is_still_honoured"),
    Mutation("turning HTTPS on also turns certificate checking off",
             "models.py",
             '            https_skip_verify=bool(data.get("https_skip_verify", False)),',
             "            https_skip_verify=True,",
             "test_certificate_validation_is_not_skipped_by_default"),
    Mutation("the connection failure says nothing about the transport",
             "winrm_factory.py",
             '    if kind not in ("offline", "winrm"):\n        return error',
             "    return error\n    if False:\n        return error",
             "test_an_https_connection_failure_names_both_ways_out"),
    Mutation("transport advice is attached to a parse error too",
             "winrm_factory.py",
             '    if kind not in ("offline", "winrm"):',
             "    if False:",
             "test_the_hint_is_not_added_to_an_unrelated_failure"),
    Mutation("an HTTP server's error gets HTTPS advice",
             "winrm_factory.py",
             '    if not bool(getattr(server_config, "use_https", False)):\n        return error',
             "    if False:\n        return error",
             "test_the_hint_is_not_added_when_the_server_is_on_http"),
    Mutation("the check layer drops the hint",
             "collector_v2/checks.py",
             "        return False, \"\", explain_transport_failure(server, err, kind), kind",
             "        return False, \"\", err, kind",
             "test_the_check_layer_attaches_the_hint"),
)

# ── ps-sandbox-bypasses: the shapes the audit demonstrated ───────────
suite(
    "ps-sandbox-bypasses",
    Mutation("the script is matched before backticks are normalised",
             "ps_sandbox.py",
             "    probe = normalize_for_matching(script)",
             "    probe = script",
             "test_a_backtick_split_identifier_is_rejected"),
    Mutation("normalisation keeps the backtick, so it splits identifiers",
             "ps_sandbox.py",
             '    return script.replace("`", "")',
             "    return script",
             "test_a_backtick_split_alias_is_rejected"),
    Mutation("the tokeniser reads the raw script, not the normalised one",
             "ps_sandbox.py",
             "    for tok in _TOKEN_RE.findall(probe):",
             "    for tok in _TOKEN_RE.findall(script):",
             "test_a_backtick_split_off_allowlist_cmdlet_is_rejected"),
    Mutation("the call operator on a computed name is allowed again",
             "ps_sandbox.py",
             r'    r"&\s*[\(\$\[\"\']",',
             r'    r"&&&\s*[\(\$\[\"\']",',
             "test_the_call_operator_on_a_concatenated_string_is_rejected"),
    Mutation("dot-sourcing a computed value is allowed again",
             "ps_sandbox.py",
             r'    r"(?:^|[;{}|\n])\s*\.\s*[\(\$\[]",',
             r'    r"(?:^)ZZZ\s*\.\s*[\(\$\[]",',
             "test_dot_sourcing_a_computed_value_is_rejected"),
    Mutation("the char cast comes back",
             "ps_sandbox.py",
             r'    r"\[\s*(System\.)?char\s*\]",',
             r'    r"\[\s*(System\.)?charZZZ\s*\]",',
             "test_the_char_cast_alone_is_rejected"),
    Mutation("the reflection chain is allowed again",
             "ps_sandbox.py",
             r'    r"\.\s*GetType\s*\(",',
             r'    r"\.\s*GetTypeZZZ\s*\(",',
             "test_a_gettype_call_is_rejected"),
    Mutation("scriptblock creation is allowed again",
             "ps_sandbox.py",
             r'    r"\[\s*scriptblock\s*\]\s*::",',
             r'    r"\[\s*scriptblockZZZ\s*\]\s*::",',
             "test_a_scriptblock_create_is_rejected"),
    Mutation("property access is denied along with method invocation",
             "ps_sandbox.py",
             r'    r"\.\s*Invoke\s*\(",',
             r'    r"\.\s*[A-Za-z]+",',
             "test_a_pipeline_with_a_property_access_is_still_accepted"),
    Mutation("hardening quietly overrides the operator's kill switch",
             "ps_sandbox.py",
             "    if not enabled:\n        return True, \"\"",
             "    if False:\n        return True, \"\"",
             "test_the_kill_switch_still_bypasses_everything"),
)

# ── workflow-authoring-rbac: you may author what you may run ─────────
suite(
    "workflow-authoring-rbac",
    Mutation("creating a WinRM workflow needs no permission again",
             "routes/api/workflows.py",
             '        "rbac_denied_workflow_create", f"workflow-create name={name!r}")\n    if gate:\n        return gate',
             '        "rbac_denied_workflow_create", f"workflow-create name={name!r}")\n    if False:\n        return gate',
             "test_creating_a_powershell_workflow_without_admin_is_denied"),
    Mutation("update is ungated, so create-empty-then-fill is the bypass",
             "routes/api/workflows.py",
             '            "rbac_denied_workflow_update", f"workflow-update id={wf_id}")\n        if gate:\n            return gate',
             '            "rbac_denied_workflow_update", f"workflow-update id={wf_id}")\n        if False:\n            return gate',
             "test_updating_a_workflow_to_add_a_powershell_block_is_denied"),
    Mutation("cloning copies somebody else's blocks ungated",
             "routes/api/workflows.py",
             '            "rbac_denied_workflow_clone", f"workflow-clone src={wf_id}")\n        if gate:\n            return gate',
             '            "rbac_denied_workflow_clone", f"workflow-clone src={wf_id}")\n        if False:\n            return gate',
             "test_cloning_a_powershell_workflow_without_admin_is_denied"),
    Mutation("the gate asks for control rather than admin",
             "routes/api/workflows.py",
             '        perm_err = _require_server_permission(server_name, "admin")\n        if not perm_err:',
             '        perm_err = _require_server_permission(server_name, "control")\n        if not perm_err:',
             "test_control_permission_is_not_enough_to_author"),
    Mutation("ambient blocks are gated too, making the rule about workflows",
             "routes/api/workflows.py",
             "        if node_type not in _WINRM_BLOCK_TYPES:\n            continue",
             "        if False:\n            continue",
             "test_an_ambient_block_needs_no_server_permission"),
    Mutation("a half-configured node is rejected, breaking the editor",
             "routes/api/workflows.py",
             "        if not server_name or server_name in checked:\n            continue",
             "        if server_name in checked:\n            continue",
             "test_a_node_with_no_server_yet_is_allowed"),
    Mutation("a denied authoring attempt leaves no audit trail",
             "routes/api/workflows.py",
             '        _shared._db.log_audit(actor, event, "rbac",',
             '        _noop = (actor, event, "rbac",',
             "test_a_denied_authoring_attempt_is_audited"),
)

# ── fleet-walk: the first scale ceiling was a for loop ──────────────
suite(
    "fleet-walk",
    Mutation("the walk is serial again",
             "collector_v2/fleet_walk.py",
             "    if workers == 1:",
             "    if True:",
             "test_the_walk_overlaps_its_items"),
    Mutation("concurrency is unbounded",
             "collector_v2/fleet_walk.py",
             "    workers = max(1, min(int(workers), len(items)))",
             "    workers = len(items)",
             "test_concurrency_is_bounded_by_the_worker_count"),
    # There is deliberately NO mutation for the `workers == 1` fast path. A
    # ThreadPoolExecutor with max_workers=1 is behaviourally identical to the
    # serial loop — same order, same peak concurrency of one — so any mutation
    # swapping one for the other lands cleanly and changes nothing, and the tool
    # would report a blind TEST when the truth is a useless MUTATION (see this
    # file's header on exactly that gap). The behaviour that matters is covered
    # by test_one_worker_is_a_plain_serial_walk; the branch itself is a
    # thread-creation optimisation, and fleet_walk.py says so.
    Mutation("one broken host ends the pass",
             "collector_v2/fleet_walk.py",
             '        except Exception:\n            with lock:\n                state["failed"] += 1',
             '        except NotImplementedError:\n            with lock:\n                state["failed"] += 1',
             "test_one_failing_item_does_not_stop_the_pass"),
    Mutation("peak concurrency is reported rather than observed",
             "collector_v2/fleet_walk.py",
             '            if state["in_flight"] > state["max_in_flight"]:\n                state["max_in_flight"] = state["in_flight"]',
             "            pass",
             "test_the_walk_overlaps_its_items"),
    Mutation("overrunning the cadence is logged at info, not warned",
             "collector_v2/fleet_walk.py",
             "    if budget_s and elapsed > budget_s:",
             "    if False:",
             "test_exceeding_the_cadence_is_logged_as_a_warning"),
    Mutation("a healthy pass warns anyway",
             "collector_v2/fleet_walk.py",
             "    if budget_s and elapsed > budget_s:",
             "    if budget_s and elapsed >= 0:",
             "test_staying_inside_the_budget_logs_no_warning"),
    Mutation("zero workers means the job never runs",
             "collector_v2/fleet_walk.py",
             '        return max(1, int((settings or {}).get("collector_v2_periodic_workers",',
             '        return int((settings or {}).get("collector_v2_periodic_workers",',
             "test_a_zero_worker_count_degrades_to_serial_not_to_nothing"),
    Mutation("an unreadable worker count raises",
             "collector_v2/fleet_walk.py",
             "    except (TypeError, ValueError):",
             "    except NotImplementedError:",
             "test_an_unreadable_worker_count_falls_back_to_the_default"),
)

# ── retention-lock: the chunking was cancelled one level up ──────────
suite(
    "retention-lock",
    Mutation("the lock is held across every chunk again",
             "database.py",
             "            with self._write_lock:\n                cur = conn.execute(\n                    f\"DELETE FROM {table} WHERE rowid IN \"",
             "            if True:\n                cur = conn.execute(\n                    f\"DELETE FROM {table} WHERE rowid IN \"",
             "test_the_chunked_delete_owns_its_own_lock"),
    Mutation("the caller re-wraps the whole loop in the write lock",
             "database.py",
             "            logs_deleted = self._chunked_delete(conn, \"logs\", \"timestamp\", d_logs)",
             "            with self._write_lock:\n                logs_deleted = self._chunked_delete(conn, \"logs\", \"timestamp\", d_logs)",
             "test_no_caller_holds_the_write_lock_across_the_chunked_delete"),
    Mutation("the chunk size stops bounding the statement",
             "database.py",
             "                    f\" LIMIT {int(chunk)})\",",
             "                    f\")\",",
             "test_the_lock_is_taken_once_per_chunk"),
)

# ── heart-monitor: colour is the truth, motion is the news (WP-2) ────
suite(
    "heart-monitor",
    Mutation("the percentage comes back to the face of the circle",
             "templates/dashboard.html",
             "    <span class=\"vitals-core-label\">{{ t.get('vitals_estate', 'Estate') }}</span>",
             "    <span class=\"vitals-core-label\">{{ t.get('vitals_estate', 'Estate') }}</span>\n"
             "    <span class=\"vitals-core-percent\" data-vitals-percent-out=\"\"></span>",
             "test_the_percentage_left_the_face_of_the_circle"),
    Mutation("the heart becomes a div with a click handler",
             "templates/dashboard.html",
             "    <button type=\"button\" class=\"vitals-heart\" data-vitals-detail-toggle",
             "    <div class=\"vitals-heart\" data-vitals-detail-toggle",
             "test_the_heart_is_a_real_button"),
    Mutation("the detail is open on first paint",
             "templates/dashboard.html",
             "    <div id=\"estate-vitals-detail\" class=\"vitals-detail\" hidden>",
             "    <div id=\"estate-vitals-detail\" class=\"vitals-detail\">",
             "test_the_detail_starts_closed"),
    Mutation("the severity word leaves the page entirely",
             "templates/dashboard.html",
             "    <span class=\"sr-only\" data-vitals-state-out=\"\" aria-live=\"polite\"",
             "    <span class=\"sr-only\" aria-live=\"polite\"",
             "test_the_severity_word_survives_as_live_text"),
    Mutation("the heart stops being clickable, so the numbers are unreachable",
             "static/css/app.css",
             ".vitals-heart {\n  pointer-events: auto;",
             ".vitals-heart {\n  pointer-events: none;",
             "test_only_the_heart_becomes_hit_testable"),
    Mutation("the heart gets a second severity mapping of its own",
             "static/css/app.css",
             "  fill: rgb(var(--vitals-halo) / 0.16);\n  stroke: rgb(var(--vitals-halo));",
             "  fill: rgb(var(--c-accent) / 0.16);\n  stroke: rgb(var(--c-accent));",
             "test_the_heart_reuses_the_existing_severity_mapping"),
    Mutation("the detail refuses the pointer it sits inside",
             "static/css/app.css",
             ".vitals-detail {\n  pointer-events: auto;",
             ".vitals-detail {\n  pointer-events: none;",
             "test_the_detail_is_hit_testable_too"),
    Mutation("the squeeze runs off a second clock",
             "static/js/vitals-monitor.js",
             "      heartEl.style.setProperty(\n        '--beat', squeezing() ? squeeze(beatU).toFixed(3) : '0');",
             "      heartEl.dataset.phase = String(beatU);",
             "test_the_squeeze_is_driven_from_the_trace_s_own_frame"),
    Mutation("a heart with no news contracts anyway",
             "static/js/vitals-monitor.js",
             "'--beat', squeezing() ? squeeze(beatU).toFixed(3) : '0');",
             "'--beat', squeeze(beatU).toFixed(3));",
             "test_the_still_heart_is_not_a_shrunken_heart"),
    Mutation("the squeeze peaks a fifth of a beat off the R spike",
             "static/js/vitals-monitor.js",
             "  const R_CENTRE = 0.250;",
             "  const R_CENTRE = 0.450;",
             "test_the_squeeze_peaks_on_the_r_spike"),
    Mutation("an acknowledged change keeps squeezing",
             "static/js/vitals-monitor.js",
             "    return sweeping() && unacknowledged;",
             "    return sweeping();",
             "test_an_acknowledged_state_does_not_squeeze"),
    Mutation("reduced motion stops being honoured by the loop",
             "static/js/vitals-monitor.js",
             "    return !reduceMotion && beating() && !document.hidden;",
             "    return beating() && !document.hidden;",
             "test_reduced_motion_keeps_the_colour_and_drops_the_squeeze"),
    # The defect the owner reported: one predicate for both motions froze the
    # ECG the moment the heart was clicked.
    Mutation("the trace freezes once the change is acknowledged",
             "static/js/vitals-monitor.js",
             "  function sweeping() {\n    return !reduceMotion && beating() && !document.hidden;",
             "  function sweeping() {\n    return !reduceMotion && beating() && unacknowledged && !document.hidden;",
             "test_the_trace_is_not_gated_on_acknowledgement"),
    Mutation("the two motions collapse back into one decision",
             "static/js/vitals-monitor.js",
             "  function squeezing() {\n    return sweeping() && unacknowledged;\n  }",
             "  function squeezing() {\n    return sweeping();\n  }",
             "test_an_acknowledged_state_does_not_squeeze"),
    Mutation("the resting state is treated as news, so calm beats too",
             "static/js/vitals-monitor.js",
             "    if (!newsworthy()) return true;",
             "    if (false) return true;",
             "test_a_calm_estate_with_nothing_wrong_is_not_news"),
    Mutation("the acknowledgement key forgets which problem it is",
             "static/js/vitals-monitor.js",
             "    return severity + '|' + rail;",
             "    return severity;",
             "test_acknowledgement_is_keyed_on_what_is_wrong_not_just_how_bad"),
    Mutation("the acknowledgement outlives the state it was given for",
             "static/js/vitals-monitor.js",
             "      _ackFor = null;\n      unacknowledged = !acknowledged();",
             "      unacknowledged = !acknowledged();",
             "test_the_acknowledgement_is_forgotten_when_the_state_changes"),
    Mutation("the acknowledgement is persisted again",
             "static/js/vitals-monitor.js",
             "  function remember() {\n    _ackFor = ackKey();\n  }",
             "  function remember() {\n    _ackFor = ackKey();\n    window.localStorage.setItem('prism.vitals.ack', '1');\n  }",
             "test_the_acknowledgement_is_not_persisted"),
    Mutation("opening the detail no longer acknowledges",
             "static/js/vitals-monitor.js",
             "    remember();\n    unacknowledged = false;",
             "    unacknowledged = false;",
             "test_opening_the_detail_is_what_acknowledges"),
    Mutation("the beat decision stops being published to the DOM",
             "static/js/vitals-monitor.js",
             "    core.setAttribute('data-beating', squeezing() ? 'true' : 'false');",
             "    void squeezing;",
             "test_the_beat_state_is_observable_from_the_dom"),
    Mutation("start() decides for itself instead of asking the predicates",
             "static/js/vitals-monitor.js",
             "    const run = sweeping();",
             "    const run = !reduceMotion && beating() && !document.hidden;",
             "test_the_two_motions_are_separate_decisions"),
    Mutation("the detail builds markup out of a server name",
             "static/js/vitals-monitor.js",
             "    if (railOut) railOut.textContent = rail;",
             "    if (railOut) railOut.innerHTML = rail;",
             "test_the_detail_never_builds_markup_out_of_a_server_name"),
    Mutation("the squeeze depth zeroing leaves the reduced-motion block",
             "static/css/app.css",
             "  .vitals-heart { --beat-depth: 0; will-change: auto; }",
             "  .vitals-heart { will-change: auto; }",
             "test_the_reduced_motion_override_is_not_in_a_width_query"),
)

# ── restart-overlay: tokens, text, and the strings that did not exist ──
suite(
    "restart-overlay",
    Mutation("a phase colour goes back to a raw hex",
             "static/css/app.css",
             ".restart-overlay--stabilising { --restart-accent: var(--c-warning); }",
             ".restart-overlay--stabilising { --restart-accent: 245 158 11; }",
             "test_every_phase_has_a_rule_and_an_accent"),
    Mutation("the icon stops reading the phase accent",
             "static/css/app.css",
             "  color: rgb(var(--restart-accent));\n}\n\n.restart-overlay-icon svg {",
             "  color: rgb(var(--c-ink));\n}\n\n.restart-overlay-icon svg {",
             "test_the_accent_is_read_rather_than_repeated"),
    Mutation("a dot state stops resolving to a token",
             "static/css/app.css",
             ".restart-dot--waiting { background: rgb(var(--c-warning)); }",
             ".restart-dot--waiting { background: orange; }",
             "test_every_dot_state_maps_to_a_token"),
    Mutation("the scrim goes back to a fixed dark colour",
             "static/css/app.css",
             "  background: rgb(var(--c-page) / 0.92);",
             "  background: rgba(15, 23, 42, 0.92);",
             "test_the_scrim_and_the_ink_are_both_theme_aware"),
    Mutation("the primary button loses its dark-mode correction",
             "static/css/app.css",
             ".dark .restart-overlay-btn--primary {\n  color: rgb(var(--c-page));\n}",
             ".dark .restart-overlay-btn--primary {\n  border-color: rgb(var(--c-brand));\n}",
             "test_the_primary_button_does_not_go_white_on_pale_violet_in_dark_mode"),
    Mutation("a failed measurement leaves a strip of the page uncovered",
             "static/css/app.css",
             "  left: 0;\n  right: 0;\n  bottom: 0;\n  z-index: 50;",
             "  left: 14rem;\n  right: 0;\n  bottom: 0;\n  z-index: 50;",
             "test_the_default_left_edge_covers_more_rather_than_less"),
    Mutation("the server name goes back into markup",
             "templates/server_detail.html",
             "    msg.textContent = RESTART_LABELS.waiting + ' ' + SERVER_NAME + ' '\n      + RESTART_LABELS.toRespond + '\u2026';",
             "    msg.innerHTML = `${RESTART_LABELS.waiting} ${SERVER_NAME} ${RESTART_LABELS.toRespond}\u2026`;",
             "test_the_server_name_goes_in_as_text"),
    Mutation("the stabilising reason is written as markup again",
             "templates/server_detail.html",
             "        if (msgEl) msgEl.textContent = why;",
             "        if (msgEl) msgEl.innerHTML = why;",
             "test_the_stabilising_message_is_not_markup"),
    Mutation("the overlay is pinned to a hardcoded sidebar width again",
             "templates/server_detail.html",
             "      const box = main.getBoundingClientRect();\n      if (box.width > 0) overlay.style.left = Math.round(box.left) + 'px';",
             "      overlay.style.left = '14rem';",
             "test_the_overlay_is_not_pinned_to_a_hardcoded_sidebar_width"),
    Mutation("the overlay stops announcing its phase",
             "templates/server_detail.html",
             "    overlay.setAttribute('role', 'status');",
             "    overlay.dataset.role = 'status';",
             "test_the_overlay_announces_its_phase"),
    Mutation("the dismiss control becomes a div",
             "templates/server_detail.html",
             "    const dismissBtn = document.createElement('button');",
             "    const dismissBtn = document.createElement('div');",
             "test_the_actions_are_real_buttons"),
    Mutation("a colour is assigned from script again",
             "templates/server_detail.html",
             "      el.className = 'restart-dot' + (state ? ' restart-dot--' + state : '');",
             "      el.style.background = state ? 'green' : 'grey';",
             "test_the_overlay_sets_no_colour_from_javascript"),
    Mutation("the rebooting icon loses the class its carve-out is spelled by",
             "templates/server_detail.html",
             'stroke-linejoin="round" class="animate-spin" aria-hidden="true"><path d="M21 12a9 9 0 1 1-9-9"/>',
             'stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-9-9"/>',
             "test_the_spinner_keeps_the_sanctioned_carve_out"),
    Mutation("a finished state starts spinning, claiming activity",
             "templates/server_detail.html",
             'ready: \'<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
             'ready: \'<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="animate-spin" aria-hidden="true">',
             "test_only_the_rebooting_icon_spins"),
    Mutation("a locale loses one of the overlay's strings", "i18n.py",
             '        "restart_overlay_stabilising": "Stabilisiert sich",\n', "",
             "test_every_overlay_string_exists_in_every_locale"),
    Mutation("a locale silently carries the English sentence", "i18n.py",
             '        "restart_overlay_rebooting": "Der Server startet neu. Diese Seite wird automatisch aktualisiert, sobald er vollst\u00e4ndig zur\u00fcck ist.",',
             '        "restart_overlay_rebooting": "The server is rebooting. This page will refresh automatically when it is fully back.",',
             "test_no_locale_silently_reuses_the_english_text"),
    Mutation("the timeout message states a number the poller does not run",
             "i18n.py",
             '        "restart_overlay_investigate": "The server did not come back within the check window. It may still be installing updates, or something went wrong.",',
             '        "restart_overlay_investigate": "The server did not come back after 4 checks.",',
             "test_the_timeout_message_does_not_state_a_wrong_number"),
)

# ── acceleration-release: stop hammering a host that is already back ──
suite(
    "acceleration-release",
    Mutation("the release arms acceleration on a server that had none",
             "collector_v2/supervisor.py",
             "        if h is None or h.accelerated_until is None:\n            return False",
             "        if h is None:\n            return False",
             "test_a_server_that_was_never_accelerated_is_not_accelerated_now"),
    Mutation("an untracked server gets a health row invented for it",
             "collector_v2/supervisor.py",
             "        if h is None or h.accelerated_until is None:",
             "        h = h or state.server_health.setdefault(name, ServerHealth(name=name))\n        if False:",
             "test_an_untracked_server_is_not_created"),
    # No mutation for "an expired window is revived": ONE comparison in
    # settle_acceleration covers both that and "already shorter", because
    # `until` can never be in the past. A separate check was written there
    # first, this harness reported its test as blind, and it was removed rather
    # than documented — so there is nothing left to break.
    Mutation("a shorter window is lengthened to the settle window",
             "collector_v2/supervisor.py",
             "        if h.accelerated_until <= until:\n            return False",
             "        if False:\n            return False",
             "test_a_shorter_window_is_never_lengthened"),
    Mutation("a negative duration stamps a time in the past",
             "collector_v2/supervisor.py",
             "    until = now + timedelta(seconds=max(0, int(duration_s)))",
             "    until = now + timedelta(seconds=int(duration_s))",
             "test_a_negative_duration_is_treated_as_zero_not_as_the_past"),
    Mutation("the long window is left to run to completion",
             "collector_v2/supervisor.py",
             "        h.accelerated_until = until\n    logger.info(\n        \"Accelerated polling for %s shortened",
             "        pass\n    logger.info(\n        \"Accelerated polling for %s shortened",
             "test_a_long_window_is_cut_to_the_settle_window"),
    Mutation("the comeback no longer releases the window",
             "collector_v2/aggregator.py",
             "                _settle(server.name, duration_s=self._STABILISING_WINDOW_S)",
             "                pass",
             "test_the_aggregator_releases_on_the_transition_into_healthy"),
    Mutation("the maintenance gate moves in front of the release",
             "collector_v2/aggregator.py",
             "        # RELEASE ACCELERATED POLLING once the machine is demonstrably back.",
             "        if maint_suppressed:\n            return\n        # RELEASE ACCELERATED POLLING once the machine is demonstrably back.",
             "test_the_release_is_not_behind_the_maintenance_gate"),
    Mutation("a manual restart goes back to arming the safety ceiling",
             "routes/api/power.py",
             'accelerate_server(name, duration_s=5 * 60, reason="manual_restart")',
             'accelerate_server(name, duration_s=20 * 60, reason="manual_restart")',
             "test_a_manual_restart_does_not_arm_the_ceiling"),
)

# ── lan-only: the address classifier behind the LAN-only verdict ─────────
suite(
    "lan-only",
    Mutation("the broadcast is classified before the private check",
             "tools/verify_lan_only.py",
             "    if ip.version == 4 and ip == ipaddress.IPv4Address(\"255.255.255.255\"):",
             "    if False:",
             "test_the_wake_on_lan_broadcast_is_local"),
    Mutation("multicast is judged instead of reported",
             "tools/verify_lan_only.py",
             "    if ip.is_multicast:\n        return REVIEW,",
             "    if ip.is_multicast:\n        return PUBLIC,",
             "test_multicast_is_not_reported_as_public"),
    Mutation("CGNAT is guessed as local rather than surfaced",
             "tools/verify_lan_only.py",
             "        return REVIEW, \"CGNAT space",
             "        return LOCAL, \"CGNAT space",
             "test_cgnat_space_is_flagged_for_a_human_rather_than_guessed"),
    Mutation("an unparseable address is waved through as local",
             "tools/verify_lan_only.py",
             "        return REVIEW, \"unparseable address\"",
             "        return LOCAL, \"unparseable address\"",
             "test_an_unparseable_address_is_reviewed_not_ignored"),
)

# ── csp: the front-end half of the no-vendor-endpoint claim ──────────────
#
# Its own suite because these name tests in tests/test_csp.py. Filed under
# `outbound` they would have reported themselves as caught while pytest
# selected nothing — the exact accident this harness detects at step 3.
suite(
    "csp",
    Mutation("the CSP re-admits a CDN origin on script-src",
             "app.py",
             "        f\"script-src 'self' 'nonce-{_nonce}'; \"",
             "        f\"script-src 'self' https://cdn.jsdelivr.net 'nonce-{_nonce}'; \"",
             "test_no_csp_directive_permits_an_external_origin"),
    # The per-directive check that used to live here read only script-src, and
    # passed while style-src and connect-src both named CDNs. This mutation
    # exists to prove the replacement reads the WHOLE header.
    Mutation("a CDN returns to connect-src only, leaving script-src clean",
             "app.py",
             "        \"connect-src 'self'; \"",
             "        \"connect-src 'self' https://unpkg.com; \"",
             "test_no_csp_directive_permits_an_external_origin"),
)

# ── palette: the retired --brand-* ramp must not regrow ──────────────────
#
# The third mutation is the one worth keeping: it regresses only the SECOND
# of the two focus-ring rules and leaves the first reading the token. A rule
# that applies PARTIALLY is far harder to see than one that does not apply at
# all, because the half that works tells you it is working — three separate
# defects in this sheet have had that shape.
suite(
    "palette",
    Mutation("a flat ramp token is re-added beside the gradient",
             "static/css/app.css",
             "  --brand-grad: linear-gradient(95deg, #7C3AED 0%",
             "  --brand-violet: #7C3AED;\n"
             "  --brand-grad: linear-gradient(95deg, #7C3AED 0%",
             "test_the_brand_ramp_is_gone_apart_from_the_gradient"),
    Mutation("the global focus ring goes back to a raw literal",
             "static/css/app.css",
             ":focus-visible {\n  outline: 2px solid rgb(var(--c-brand));",
             ":focus-visible {\n  outline: 2px solid #7C3AED;",
             "test_the_global_focus_ring_reads_the_brand_token"),
    Mutation("only the interactive-elements ring regresses, the bare one holds",
             "static/css/app.css",
             "[tabindex]:focus-visible {\n  outline: 2px solid rgb(var(--c-brand));",
             "[tabindex]:focus-visible {\n  outline: 2px solid #A78BFA;",
             "test_the_global_focus_ring_reads_the_brand_token"),
    # These two replay a defect that actually shipped into the working tree
    # during the ramp's retirement: the `.dark` colour was deleted as
    # redundant (true of the VALUE, false of the SPECIFICITY) and the active
    # nav link rendered muted. Found by measuring the running page, not by a
    # test — which is why there is now a test.
    Mutation("the active nav link's dark colour is dropped as redundant",
             "static/css/app.css",
             ".dark .sidebar-link-active {\n"
             "  background: rgb(var(--c-brand) / 0.14);\n"
             "  color: rgb(var(--c-brand));\n}",
             ".dark .sidebar-link-active {\n"
             "  background: rgb(var(--c-brand) / 0.14);\n}",
             "test_the_active_nav_link_restates_its_colour_for_dark"),
    Mutation("only the active link's HOVER loses its dark colour",
             "static/css/app.css",
             ".dark .sidebar-link-active:hover {\n"
             "  background: rgb(var(--c-brand) / 0.22);\n"
             "  color: rgb(var(--c-brand));\n}",
             ".dark .sidebar-link-active:hover {\n"
             "  background: rgb(var(--c-brand) / 0.22);\n}",
             "test_the_active_nav_link_restates_its_colour_for_dark"),
)

# ── estate-vitals: the severity model itself ─────────────────────────────
#
# Separate from the `vitals` suite above because these break routes/views.py
# and must name tests in tests/test_estate_vitals.py — the `vitals` suite
# never opens that file, and a mutation filed in the wrong suite reports
# itself as caught while no test runs.
suite(
    "estate-vitals",
    Mutation("the unmeasured trigger widens to 'mostly unknown'",
             "routes/views.py",
             "elif unknown == monitored:",
             "elif unknown * 2 > monitored:",
             "test_one_host_that_HAS_answered_takes_the_estate_out_of_unmeasured"),
    Mutation("the unmeasured branch is dropped and calm claims it again",
             "routes/views.py",
             "elif unknown == monitored:",
             "elif False:",
             "test_an_estate_that_has_never_been_measured_is_neither_flat_nor_calm"),
    Mutation("an estate nothing has reported on is given a tempo",
             "routes/views.py",
             '"unmeasured": 0}',
             '"unmeasured": 60}',
             "test_the_tempo_rises_with_the_severity"),
    Mutation("unmeasured goes back to scoring 0%",
             "routes/views.py",
             'scored = monitored > 0 and severity != "unmeasured"',
             'scored = monitored > 0',
             "test_an_estate_that_has_never_been_measured_is_neither_flat_nor_calm"),
    Mutation("the dash is keyed off the tempo, so flat loses its real 0%",
             "routes/views.py",
             'scored = monitored > 0 and severity != "unmeasured"',
             'scored = monitored > 0 and severity not in ("unmeasured", "flat")',
             "test_only_the_two_never_measured_states_withhold_a_percentage"),
)

# ── health-overview: one row per probe, and it must agree with the card ──
suite(
    "health-overview",
    Mutation("the probe key loses its port, so two probes on one host share a result",
             "database.py",
             "AND r2.target_port = c.target_port",
             "AND 1 = 1",
             "test_never_probed_is_distinguishable_from_probed_with_no_verdict"),
    Mutation("the FIRST result wins instead of the latest",
             "database.py",
             "ORDER BY r2.id DESC",
             "ORDER BY r2.id ASC",
             "test_the_latest_result_wins_per_probe_not_across_the_table"),
    Mutation("the enabled flag stops being returned, so the page cannot mark it",
             "database.py",
             "c.expected_status, c.enabled, c.verify_tls,",
             "c.expected_status, c.verify_tls,",
             "test_a_disabled_probe_is_listed_and_flagged"),
    Mutation("a probe that has never answered is reported as up",
             "database.py",
             'probe["status"] = ("up" if status == "up" else',
             'probe["status"] = ("up" if status != "down" else',
             "test_a_configured_probe_appears_before_it_has_ever_run"),
    Mutation("the summary's enabled filter is copied across and hides switched-off probes",
             "database.py",
             "ORDER BY c.server_name, c.id",
             "WHERE c.enabled = 1 ORDER BY c.server_name, c.id",
             "test_the_overview_does_not_filter_on_enabled"),
    Mutation("a failing probe folds into unknown, so the page under-reports failures",
             "database.py",
             '"down" if status == "down" else "unknown")',
             '"unknown")',
             "test_the_enabled_rows_reproduce_the_summary_exactly"),
)


# ── overview-pages: /services, /network, /scan ───────────────────────────
suite(
    "overview-pages",
    # The failure mode this package is most likely to repeat.
    Mutation("one locale loses a string and t.get quietly serves English",
             "i18n.py",
             '"services_lede": "Prism ',
             '"_services_lede_removed": "Prism ',
             "test_every_string_on_these_pages_exists_in_every_locale"),
    Mutation("a locale carries the English sentence instead of a translation",
             "i18n.py",
             '"network_lede": "Prism a',
             '"network_lede": "Prism does not watch network equipment yet. This is what '
             'that would mean, and what it watches instead today.", "_was": "Prism a',
             "test_no_locale_silently_carries_the_english_sentence"),
    Mutation("the hint goes back to sending operators to Operations for a probe",
             "i18n.py",
             '"vitals_no_services_hint": "Add a probe under Servers',
             '"vitals_no_services_hint": "Add a probe under Operations',
             "test_no_surface_still_sends_the_operator_to_operations_for_a_probe"),

    # A page about an absence must not manufacture a measurement.
    Mutation("a zero appears on a page where nothing is measured",
             "templates/network.html",
             "{{ t.get('overview_today', 'What Prism watches today') }}",
             "0 {{ t.get('overview_today', 'What Prism watches today') }}",
             "test_a_page_about_an_absence_renders_no_number"),
    Mutation("an empty table promises rows that are not coming",
             "templates/scan.html",
             '<ul class="flex flex-wrap gap-2">',
             '<table><thead><tr><th>Device</th></tr></thead><tbody></tbody></table>'
             '<ul class="flex flex-wrap gap-2">',
             "test_a_page_about_an_absence_has_no_table"),
    Mutation("the page grows a live region for data nobody collects",
             "templates/network.html",
             '<div class="max-w-3xl">',
             '<div class="max-w-3xl" hx-get="/partials/vitals" hx-trigger="load">',
             "test_a_page_about_an_absence_fetches_nothing"),
    Mutation("the page stops saying that nothing is collected",
             "templates/network.html",
             "{{ t.get('overview_not_collected', 'Nothing here is being collected yet.') }}",
             "",
             "test_a_page_about_an_absence_says_so_in_words"),
    Mutation("a script arrives, and with it every defect a script can carry",
             "templates/scan.html",
             '<ul class="flex flex-wrap gap-2">',
             '<script></script><ul class="flex flex-wrap gap-2">',
             "test_the_new_pages_run_no_script"),
    Mutation("an inline style slips past the colour ratchet's blind spot",
             "templates/services.html",
             'class="text-sm text-muted mb-6 max-w-2xl"',
             'class="text-sm text-muted mb-6 max-w-2xl" style="color:#334155"',
             "test_the_new_pages_carry_no_inline_style"),

    # /services must not contradict the card that links to it.
    Mutation("the region gains a load trigger and flashes empty before it fills",
             "templates/services.html",
             'hx-trigger="prismRefresh from:body"',
             'hx-trigger="load, prismRefresh from:body"',
             "test_the_services_region_is_painted_before_it_is_refreshed"),
    Mutation("first paint stops being server-rendered",
             "templates/services.html",
             '{% include "partials/services_table.html" %}',
             "",
             "test_the_services_region_is_painted_before_it_is_refreshed"),
    Mutation("the figures count the page's own rows instead of the summary",
             "templates/partials/services_table.html",
             "{{ summary.total }}",
             "{{ probes | length }}",
             "test_the_services_counts_come_from_the_summary_not_from_the_rows"),
    Mutation("switched-off probes join the counted table",
             "templates/partials/services_table.html",
             "rejectattr('enabled')",
             "selectattr('enabled')",
             "test_the_disabled_probes_are_outside_the_counted_table"),
    Mutation("the anchor /services links to disappears from /servers",
             "templates/servers.html",
             'id="health-checks"',
             'id="health-checks-moved"',
             "test_the_route_to_configuration_is_named_and_exists"),

    # A failed read reported as an empty estate.
    Mutation("the empty state is checked first and swallows the failed read",
             "templates/partials/services_table.html",
             "{% if not readable %}",
             "{% if probes | length == 0 and not readable %}",
             "test_a_failed_read_is_not_reported_as_an_empty_estate"),
    Mutation("the failure panel grows a hint, telling the operator to fix Prism's bug",
             "templates/partials/services_table.html",
             "{{ t.get('services_unreadable',",
             "{{ t.get('vitals_no_services_hint', '') }}{{ t.get('services_unreadable',",
             "test_the_failure_panel_offers_no_hint"),
    Mutation("a failed summary read stops clearing the flag",
             "routes/views.py",
             'summary = {"total": 0, "up": 0, "down": 0, "unknown": 0}\n        readable = False',
             'summary = {"total": 0, "up": 0, "down": 0, "unknown": 0}',
             "test_the_context_reports_whether_the_read_succeeded"),
    Mutation("the OTHER stale hint survives the correction",
             "i18n.py",
             '"vitals_nothing_monitored_hint": "Add a server on the Servers page',
             '"vitals_nothing_monitored_hint": "Add a server on the Servers page, '
             'or a health check under Operations',
             "test_no_surface_still_sends_the_operator_to_operations_for_a_probe"),

    # WP-3 slice C: the four doorways where the activity feed used to be.
    Mutation("a doorway points at the wrong topic",
             "templates/dashboard.html",
             '<a href="/network" class="card-clickable',
             '<a href="/servers" class="card-clickable',
             "test_the_dashboard_offers_a_doorway_to_each_of_the_four_topics"),
    Mutation("a doorway starts reporting the estate's state",
             "templates/dashboard.html",
             '<span class="tabular-nums font-semibold text-ink">{{ server_count }}</span>',
             '<span class="tabular-nums font-semibold text-ink">{{ vitals.servers.healthy }}</span>',
             "test_a_doorway_reports_scope_and_leaves_state_to_the_circle"),
    Mutation("the doorways grow a live region and a wait to cover",
             "templates/dashboard.html",
             '<section id="overview-sections" class="mt-8">',
             '<section id="overview-sections" class="mt-8" hx-get="/partials/vitals" hx-trigger="load">',
             "test_a_doorway_fetches_nothing_and_therefore_ghosts_nothing"),
    Mutation("a failed probe read is printed as zero probes",
             "templates/dashboard.html",
             "{% if services %}", "{% if True %}",
             "test_a_failed_probe_read_is_not_reported_as_zero_probes"),
    Mutation("the dead preference checkbox comes back to Settings",
             "templates/settings.html",
             '<div id="pref-saved-msg"',
             '<input type="checkbox" id="pref-show-activity">\n    <div id="pref-saved-msg"',
             "test_the_preference_for_a_section_the_dashboard_no_longer_owns_is_gone"),
    Mutation("the dashboard starts branching on the removed preference again",
             "templates/dashboard.html",
             "    if (prefs.showStatus === false) hide('vitals-section');",
             "    if (prefs.showStatus === false) hide('vitals-section');\n"
             "    if (prefs.showActivity === false) hide('activity-section');",
             "test_the_preference_for_a_section_the_dashboard_no_longer_owns_is_gone"),
)


# ── settings: the "have you changed anything?" mechanism ────────────────
suite(
    "settings-tracking",
    Mutation("a control loses its bucket and its edits go nowhere",
             "templates/settings.html",
             'class="security-input w-full px-2 py-1 text-sm rounded border border-line bg-field font-mono text-xs">',
             'class="w-full px-2 py-1 text-sm rounded border border-line bg-field font-mono text-xs">',
             "test_every_settings_control_is_tracked_or_deliberately_exempt"),
    Mutation("the tracker starts naming a control by id again",
             "templates/settings.html",
             "  const snap = _settingsSnapshots[bucket] || {};\n  const controls = _settingsControls(bucket);",
             "  const snap = _settingsSnapshots[bucket] || {};\n"
             "  if (document.getElementById('poll-interval')) { /* list is back */ }\n"
             "  const controls = _settingsControls(bucket);",
             "test_the_tracker_reads_the_dom_rather_than_a_list_of_ids"),
    Mutation("revert stops walking the same control set as capture",
             "templates/settings.html",
             "function revertSettingsBucket(bucket) {\n  const snap = _settingsSnapshots[bucket] || {};\n  _settingsControls(bucket).forEach(function (el) {",
             "function revertSettingsBucket(bucket) {\n  const snap = _settingsSnapshots[bucket] || {};\n  document.querySelectorAll('.' + bucket + '-input').forEach(function (el) {",
             "test_all_three_operations_walk_the_same_control_set"),
    Mutation("a checkbox is compared by value, so toggling it reads as no change",
             "templates/settings.html",
             "  return (el.type === 'checkbox' || el.type === 'radio') ? String(el.checked) : el.value;",
             "  return el.value;",
             "test_a_checkbox_is_compared_by_checked_and_not_by_value"),
    Mutation("a control the snapshot never saw is absorbed in silence",
             "templates/settings.html",
             "    if (!(el.id in snap)) return true;",
             "    if (!(el.id in snap)) continue;",
             "test_a_control_the_snapshot_has_never_seen_counts_as_a_change"),
    Mutation("scheduled reports go back to claiming they need a restart",
             "templates/settings.html",
             'class="general-input text-xs rounded border border-line bg-field px-2 py-1">',
             'class="security-input text-xs rounded border border-line bg-field px-2 py-1">',
             "test_the_bucket_states_whether_a_restart_is_needed"),
    Mutation("the one field that DOES need a restart leaves the restart bucket",
             "templates/settings.html",
             'class="security-input w-20 px-2 py-1 text-sm rounded border border-line bg-field text-right">',
             'class="general-input w-20 px-2 py-1 text-sm rounded border border-line bg-field text-right">',
             "test_the_one_field_that_does_need_a_restart_is_still_in_that_bucket"),
    Mutation("the page stops sending one of the eight fields it collects",
             "templates/settings.html",
             "      include_pdf: document.getElementById('sched-include-pdf').checked,",
             "",
             "test_the_payload_below_is_the_one_the_page_actually_sends"),
    # WP-4 D1b/D1c: the save posts only what the page owns, section by section.
    Mutation("the payload is hand-built inside the save again",
             "templates/settings.html",
             "    const data = buildSettingsPayload(current);",
             "    const data = current; data.settings = data.settings || {};",
             "test_the_payload_is_assembled_rather_than_hand_built"),
    Mutation("a sub-tree this page does not render creeps into a builder",
             "templates/settings.html",
             "      retention_days: parseInt(document.getElementById('retention-days').value) || 30,",
             "      workflows: {},\n"
             "      retention_days: parseInt(document.getElementById('retention-days').value) || 30,",
             "test_the_section_builders_cover_exactly_the_keys_the_page_owns"),
    Mutation("an owned key is emitted by the wrong section",
             "templates/settings.html",
             "      retention_days: parseInt(document.getElementById('retention-days').value) || 30,",
             "      thresholds: {},\n"
             "      retention_days: parseInt(document.getElementById('retention-days').value) || 30,",
             "test_each_builder_emits_its_own_section_and_nothing_else"),
    Mutation("a key is emitted by the wrong section, which only shows once the page splits",
             "templates/settings.html",
             "      email: getEmailSettingsFromForm(),",
             "",
             "test_each_builder_emits_its_own_section_and_nothing_else"),
    Mutation("the server list is echoed back and stops being preserved wholesale",
             "templates/settings.html",
             "  return { settings: settings };",
             "  return { settings: settings, servers: (window.configData || {}).servers };",
             "test_the_server_list_is_not_posted_back"),
    Mutation("auth stops being merged, blanking backup_admin and the lockout keys",
             "templates/settings.html",
             "      auth: Object.assign({}, current.settings?.auth || {}, {",
             "      auth: Object.assign({}, {",
             "test_the_auth_subtree_is_still_sent_whole"),
    Mutation("a builder runs even when its section is absent",
             "templates/settings.html",
             "    if (!settingsSectionPresent(name)) return;",
             "    if (false) return;",
             "test_an_absent_section_contributes_nothing"),
    Mutation("the LDAP write stops asking whether its section is there",
             "templates/settings.html",
             "    const ldapPayload = !settingsSectionPresent('security') ? null : {",
             "    const ldapPayload = {",
             "test_the_ldap_side_write_is_guarded_by_its_own_section"),
    Mutation("a section stops declaring itself and silently saves nothing",
             "templates/settings.html",
             '<section class="mb-8" data-settings-section="notifications">',
             '<section class="mb-8">',
             "test_every_settings_section_declares_itself"),
    Mutation("a control moves out of the section whose builder reads it",
             "templates/settings.html",
             '<section class="mb-8" data-settings-section="collector">',
             '<section class="mb-8" data-settings-section="collector">\n'
             '  <input type="text" id="retention-days-stray">',
             "test_every_control_lives_in_the_section_that_saves_it"),
    Mutation("the server-side merge stops preserving what the caller omitted",
             "config_manager.py",
             'config["settings"] = self._deep_merge_settings(on_disk, settings)',
             'config["settings"] = settings',
             "test_a_narrowed_save_preserves_everything_it_did_not_send"),
    # WP-4 D1d: one section per page.
    # Anchored on the tuple's TAIL rather than the whole line: naming every
    # element meant this mutation drifted each time a section was added,
    # and a drifted anchor is reported as NOT APPLIED rather than caught.
    Mutation("the router loses a section the page still renders",
             "routes/views.py",
             ', "display")',
             ')',
             "test_the_router_and_the_markup_agree_about_the_sections"),
    Mutation("an unknown section falls back to the first instead of 404ing",
             "routes/views.py",
             '    if section not in _SETTINGS_SECTIONS:\n'
             '        return render_template("404.html") if _template_exists("404.html") else ("Not Found", 404), 404',
             '    if section not in _SETTINGS_SECTIONS:\n'
             '        section = _SETTINGS_SECTIONS[0]',
             "test_an_unknown_section_is_a_404_rather_than_a_silent_fallback"),
    Mutation("a section stops being gated and renders on every sub-page",
             "templates/settings.html",
             "{% if section == 'notifications' %}",
             "{% if True %}",
             "test_every_section_renders_only_when_it_is_the_active_one"),
    Mutation("the nav hardcodes its own list instead of the router's",
             "templates/settings.html",
             "{% for name in settings_sections %}",
             "{% for name in ['general', 'collector'] %}",
             "test_the_nav_is_generated_from_the_router_list"),
    Mutation("the active tab is marked by colour alone",
             "templates/settings.html",
             '{% if name == section %}aria-current="page"{% endif %}',
             "",
             "test_the_nav_is_generated_from_the_router_list"),
    Mutation("a section-scoped initialiser stops checking its controls are there",
             "templates/settings.html",
             "  if (!httpsToggle || !httpsWarning) return;",
             "  if (false) return;",
             "test_a_section_scoped_initialiser_checks_its_controls_are_there"),
    Mutation("the save validator dereferences a field from another section",
             "templates/settings.html",
             "  const pollField = el('poll-interval');\n  if (pollField) {",
             "  const pollField = el('poll-interval');\n  if (true) {",
             "test_the_save_validator_skips_checks_whose_section_is_absent"),
    # WP-4 D2: the detection block moved out of /monitoring.
    Mutation("the detection builder drops one of its four sub-trees",
             "templates/settings.html",
             "      security_alerts: {\n        failed_login_tracking:",
             "      _security_alerts_removed: {\n        failed_login_tracking:",
             "test_the_section_builders_cover_exactly_the_keys_the_page_owns"),
    Mutation("a control lands in the section without the builder reading it",
             "templates/partials/settings/_detection.html",
             '  <div id="baseline-security-card"',
             '  <input type="text" id="detection-stray-control">\n  <div id="baseline-security-card"',
             "test_every_control_lives_in_the_section_that_saves_it"),
    Mutation("the demoted detector cards are faded again",
             "templates/settings.html",
             "        label.textContent = PRISM_DETECTION_OFF;",
             "        label.textContent = PRISM_DETECTION_OFF;\n"
             "        toggle.closest('.rounded-lg').style.opacity = '0.65';",
             "test_no_detector_card_is_dimmed_to_say_it_is_demoted"),
    Mutation("a detector card loses the label that states its rank",
             "templates/partials/settings/_detection.html",
             'data-detection-priority="anomaly"',
             'data-anomaly-priority-removed="anomaly"',
             "test_every_detector_card_states_its_rank"),
    Mutation("the ranks stop being recomputed when a detector is toggled",
             "templates/settings.html",
             "    if (toggle) toggle.addEventListener('change', updateDetectionPriority);",
             "    if (false) toggle.addEventListener('change', updateDetectionPriority);",
             "test_the_ranks_are_recomputed_when_a_detector_is_toggled"),
    Mutation("the faintest token comes back to text sitting on the page surface",
             "templates/partials/settings/_detection.html",
             'class="text-xs text-muted mt-1">\n            {{ t.get(\'thresholds_help\'',
             'class="text-xs text-faint mt-1">\n            {{ t.get(\'thresholds_help\'',
             "test_the_detection_help_text_is_not_the_faintest_token"),
    # WP-4 D4: Permissions, and the standards the move brought it up to.
    Mutation("a colour literal comes back to the permissions page",
             "templates/partials/settings/_rbac.html",
             "      info:  'bg-info-tint border border-info/30 text-info-strong',",
             "      info:  'bg-[#2563EB]/10 border border-[#2563EB]/30 text-[#1E40AF]',",
             "test_the_permissions_section_carries_no_colour_literal"),
    Mutation("a permission colour goes back to an inline style",
             "templates/partials/settings/_rbac.html",
             'class="font-bold ${permClass}"',
             'style="color:${permClass}"',
             "test_no_permission_colour_is_applied_as_an_inline_style"),
    Mutation("a loading state goes back to saying the word",
             "templates/partials/settings/_rbac.html",
             '<div id="rbac-acl-list" class="text-xs text-muted"><div class="rbac-ghost" aria-hidden="true">',
             '<div id="rbac-acl-list" class="text-xs text-muted">Loading<div class="rbac-ghost" aria-hidden="true">',
             "test_every_loading_region_shows_a_ghost_rather_than_the_word"),
    Mutation("a ghost is exposed to assistive technology",
             "templates/partials/settings/_rbac.html",
             '<div class="rbac-ghost" aria-hidden="true">\n      <div class="skeleton h-4 w-40 mb-2">',
             '<div class="rbac-ghost">\n      <div class="skeleton h-4 w-40 mb-2">',
             "test_the_ghosts_are_hidden_from_assistive_technology"),
    Mutation("a timestamp is rendered outside the configured timezone",
             "templates/partials/settings/_rbac.html",
             "${_escHtml(formatTs(r.granted_at))}",
             "${_escHtml(r.granted_at)}",
             "test_every_timestamp_goes_through_the_configured_timezone"),
    Mutation("the old RBAC url stops resolving",
             "routes/views.py",
             'return redirect("/settings/rbac", code=301)',
             'return ("Not Found", 404)',
             "test_the_old_rbac_url_still_resolves"),
    Mutation("the periodics job stops re-reading settings, so the bucket's premise fails",
             "collector_v2/periodics.py",
             "_check_scheduled_reports(db, get_servers(), get_settings())",
             "_check_scheduled_reports(db, get_servers(), {})",
             "test_scheduled_reports_really_do_take_effect_without_a_restart"),
)


# ── every data-action resolves to something that runs ───────────────────
suite(
    "action-dispatch",
    Mutation("a moved handler is left behind and its button goes dead",
             "templates/settings.html",
             "function recalculateBaselines() {",
             "function recalculateBaselines_moved_away() {",
             "test_every_dispatched_action_resolves_to_a_handler"),
    Mutation("the dispatcher's global-function fallback is removed",
             "templates/base.html",
             "        const g = window[key];",
             "        const g = undefined;",
             "test_the_fallback_to_a_global_function_still_exists"),
    Mutation("a loader is left behind when its section moves",
             "templates/settings.html",
             "function loadRestartSchedules() {",
             "function loadRestartSchedules_moved_away() {",
             "test_every_bootstrap_call_resolves_on_its_own_page"),
    Mutation("a shared helper is defined on only some of its pages",
             "templates/base.html",
             "      window._escHtml = function (s) {",
             "      window._escHtml_moved_away = function (s) {",
             "test_a_shared_partial_only_calls_what_every_including_page_has"),
    Mutation("base.html dispatches an attribute this scan does not know about",
             "templates/base.html",
             "mousedown: 'data-mousedown', mouseup: 'data-mouseup' };",
             "mousedown: 'data-mousedown', mouseup: 'data-mouseup', dblclick: 'data-dblclick' };",
             "test_the_dispatched_attributes_are_the_ones_base_html_lists"),
)


# ── every page route renders, and a failure answers 500 ─────────────────
suite(
    "pages-render",
    Mutation("a settings section stops rendering",
             "templates/settings.html",
             "{% if section == 'rbac' %}",
             "{% if section == 'rbac' %}{{ undefined_filter | nosuchfilter }}",
             "test_every_settings_section_renders"),
    Mutation("the error path goes back to answering 200",
             "routes/views.py",
             '(render_template("500.html"), 500) if _template_exists("500.html")',
             'render_template("500.html") if _template_exists("500.html")',
             "test_the_error_path_answers_with_an_error_status"),
)


# ── the permissions page's translations ─────────────────────────
suite(
    "permissions",
    Mutation("a string goes back to being built into the script",
             "templates/partials/settings/_rbac.html",
             "showToast(RBAC_T.usernameRequired, 'warn')",
             "showToast('Username required', 'warn')",
             "test_the_script_renders_no_bare_english_sentence"),
    Mutation("an aria-label goes back to English",
             "templates/partials/settings/_rbac.html",
             "aria-label=\"{{ t.get('rbac_server_aria', 'Server name') }}\"",
             "aria-label=\"Server name\"",
             "test_every_aria_label_on_the_page_is_translated"),
    Mutation("a locale loses a key and silently falls back to English",
             "i18n.py",
             '"rbac_no_acls_hint": "Jeder Benutzer',
             '"rbac_no_acls_hint_disabled": "Jeder Benutzer',
             "test_every_permissions_string_exists_in_every_locale"),
    Mutation("a sentence is assembled from fragments again",
             "templates/partials/settings/_rbac.html",
             "function rbacFill",
             "function rbacFillDisabled",
             "test_the_sentences_substitute_rather_than_concatenate"),
    Mutation("the page carries its own escaper again",
             "templates/partials/settings/_rbac.html",
             "  const RBAC_T = {",
             "  function _escHtml(s) { return s; }\n  const RBAC_T = {",
             "test_the_page_uses_the_shared_escaper"),
    Mutation("a timestamp stops going through the formatter",
             "templates/partials/settings/_rbac.html",
             "_escHtml(formatTs(a.requested_at))",
             "_escHtml(a.requested_at)",
             "test_every_timestamp_goes_through_the_formatter"),
    Mutation("the old /admin/rbac address stops redirecting",
             "routes/views.py",
             'return redirect("/settings/rbac", code=301)',
             'return redirect("/settings/rbac", code=302)',
             "test_the_old_rbac_address_still_resolves"),
    Mutation("a label is resolved at its use site rather than from RBAC_T",
             "templates/partials/settings/_rbac.html",
             "showToast(d.error || RBAC_T.grantFailed, 'error')",
             "showToast(d.error || {{ t.get('rbac_grant_failed', 'x') | tojson }}, 'error')",
             "test_the_labels_are_defined_once_for_the_script"),
)


# ── the compliance module's enable switch ───────────────────────
suite(
    "settings-compliance",
    Mutation("the toggle falls out of every tracked bucket",
             "templates/partials/settings/_compliance.html",
             'class="sr-only peer general-input"',
             'class="sr-only peer"',
             "test_the_toggle_is_tracked_by_the_save_bar"),
    Mutation("the toggle starts demanding a restart it does not need",
             "templates/partials/settings/_compliance.html",
             'class="sr-only peer general-input"',
             'class="sr-only peer security-input"',
             "test_the_toggle_is_tracked_by_the_save_bar"),
    Mutation("the section hides itself when the module is off",
             "templates/settings.html",
             "{% if section == 'compliance' %}",
             "{% if section == 'compliance' and settings.compliance.enabled %}",
             "test_the_section_is_reachable_whether_or_not_the_module_is_on"),
    Mutation("the gate stops answering the flag, so the module cannot turn on",
             "csv_compliance.py",
             'return bool(cfg.get("enabled", False))',
             'return False',
             "test_when_on_the_dashboard_and_its_nav_entry_appear"),
    Mutation("the gate opens regardless of the flag (URS-204 lost)",
             "csv_compliance.py",
             'return bool(cfg.get("enabled", False))',
             'return True',
             "test_when_off_the_view_routes_are_absent"),
    Mutation("the nav entry loses its gate",
             "templates/base.html",
             "{% if compliance_enabled %}",
             "{% if True %}",
             "test_when_off_the_nav_entry_is_hidden"),
    Mutation("the dashboard link is offered while the module is off",
             "templates/partials/settings/_compliance.html",
             "{% if settings.compliance.enabled %}\n    <div class=\"border-t border-line pt-4\">",
             "{% if True %}\n    <div class=\"border-t border-line pt-4\">",
             "test_the_dashboard_link_only_appears_once_the_module_is_on"),
    Mutation("the payload builder reaches outside its own section",
             "templates/settings.html",
             "        enabled: document.getElementById('compliance-enabled').checked,",
             "        enabled: document.getElementById('compliance-enabled').checked,\n"
             "        stray: document.getElementById('retention-days').value,",
             "test_the_section_contributes_only_its_own_subtree"),
    Mutation("a fallback drifts away from its English entry",
             "templates/partials/settings/_compliance.html",
             "'Enable the compliance module'",
             "'Enable compliance'",
             "test_each_fallback_is_exactly_its_english_entry"),
    Mutation("a locale loses a key and falls back to English invisibly",
             "i18n.py",
             "'compliance_off_means': 'Solange es deaktiviert",
             "'compliance_off_means_disabled': 'Solange es deaktiviert",
             "test_every_string_exists_in_every_locale"),
)


# ── the main navigation ─────────────────────────────────────
suite(
    "navigation",
    Mutation("the order drifts back to page age",
             "templates/base.html",
             '<a href="/servers" class="sidebar-link',
             '<a href="/zservers" class="sidebar-link',
             "test_the_nav_is_in_the_order_the_plan_specifies"),
    Mutation("Settings stops highlighting on its sub-pages",
             "templates/base.html",
             "{% if request.path.startswith('/settings') %}sidebar-link-active",
             "{% if request.path == '/settings' %}sidebar-link-active",
             "test_settings_stays_active_across_its_sub_pages"),
    Mutation("server detail pages stop highlighting Servers",
             "templates/base.html",
             "request.path == '/servers' or request.path.startswith('/server/')",
             "request.path == '/servers'",
             "test_a_server_detail_page_keeps_servers_active"),
    Mutation("an aria-label goes back to hardcoded English",
             "templates/base.html",
             'aria-label="{{ nav_settings }}"',
             'aria-label="Settings"',
             "test_no_nav_entry_says_one_thing_and_announces_another"),
    Mutation("an entry announces a different string than it shows",
             "templates/base.html",
             'aria-label="{{ nav_reports }}"',
             'aria-label="{{ nav_topology }}"',
             "test_each_entry_announces_exactly_its_visible_label"),
    Mutation("a template links a route the app does not serve",
             "templates/base.html",
             '<a href="/topology" class="sidebar-link',
             '<a href="/topology-map" class="sidebar-link',
             "test_every_internal_link_in_every_template_resolves"),
    Mutation("the RBAC redirect stops being permanent",
             "routes/views.py",
             'return redirect("/settings/rbac", code=301)',
             'return redirect("/settings/rbac", code=302)',
             "test_the_url_that_did_move_still_redirects"),
)


# ── jump-to search ──────────────────────────────────────
suite(
    "jump-search",
    Mutation("settings sub-pages stop being crawled, so a setting is unfindable",
             "search_index.py",
             'for path in list(_CRAWLABLE) + [f"/settings/{n}" for n in _SETTINGS_SECTIONS]:',
             'for path in list(_CRAWLABLE):',
             "test_a_heading_inside_a_settings_section_is_findable"),
    Mutation("live counts go back into the labels",
             "search_index.py",
             'text = re.sub(r"\\s*\\(\\d+\\)\\s*$", "", text)',
             'pass',
             "test_live_counts_are_not_baked_into_the_labels"),
    Mutation("the recursion guard is removed",
             "search_index.py",
             'if getattr(_building, "active", False):',
             'if False:',
             "test_the_crawl_cannot_recurse"),
    Mutation("an empty query dumps the whole index",
             "search_index.py",
             "    if not q:\n        return []",
             "    if not q:\n        return entries[:limit]",
             "test_an_empty_query_returns_nothing"),
    Mutation("prefix and substring rank the same",
             "search_index.py",
             "        if label.startswith(q):\n            rank = 0",
             "        if label.startswith(q):\n            rank = 2",
             "test_a_prefix_match_outranks_a_substring"),
    Mutation("servers stop outranking pages",
             "search_index.py",
             'kind_rank = {"server": 0, "settings": 1, "page": 2, "heading": 3}',
             'kind_rank = {"server": 3, "settings": 1, "page": 0, "heading": 2}',
             "test_a_server_outranks_a_heading_at_the_same_strength"),
    Mutation("the endpoint answers an empty query with the index",
             "routes/api/search.py",
             'if not query:\n        return jsonify({"ok": True, "results": []})',
             'if not query:\n        query = "e"',
             "test_the_endpoint_does_not_dump_the_index_when_asked_nothing"),
    Mutation("the highlight stops being announced",
             "templates/base.html",
             "input.setAttribute('aria-activedescendant', rows[i].id);",
             "rows[i].dataset.active = '1';",
             "test_the_highlight_is_named_and_not_only_coloured"),
    Mutation("a result label is drawn without escaping",
             "templates/base.html",
             "'<span class=\"truncate\">' + _escHtml(r.label) + '</span>'",
             "'<span class=\"truncate\">' + r.label + '</span>'",
             "test_the_results_are_escaped_before_they_are_drawn"),
    Mutation("a stale response may overwrite a newer one",
             "templates/base.html",
             "        .then(d => { if (mine === seq) draw(d.results || []); })",
             "        .then(d => { draw(d.results || []); })",
             "test_a_stale_response_cannot_overwrite_a_newer_one"),
    Mutation("the slash shortcut steals a typed slash",
             "templates/base.html",
             "const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) ||",
             "const typing = false && (",
             "test_the_slash_shortcut_does_not_steal_a_typed_slash"),
    Mutation("a top-bar control goes back to hardcoded English",
             "templates/base.html",
             "aria-label=\"{{ t.get('toggle_theme', 'Toggle dark mode') }}\"",
             'aria-label="Toggle dark mode"',
             "test_the_top_bar_carries_no_hardcoded_english"),
    Mutation("code is wedged inside a <script src> tag, where it never runs",
             "templates/base.html",
             "js/table-sort.js') }}\"></script>",
             "js/table-sort.js') }}\">\n  var deadCode = 1;\n  </script>",
             "test_no_script_tag_carries_both_a_src_and_inline_code"),
    Mutation("a locale loses a jump-to key",
             "i18n.py",
             "'jump_no_results': 'Keine Treffer'",
             "'jump_no_results_disabled': 'Keine Treffer'",
             "test_every_string_exists_in_every_locale"),
)


# ── tag management leaves /servers ───────────────────────────
suite(
    "settings-servers",
    Mutation("a management function is left behind on /servers",
             "templates/servers.html",
             "// Tag management (create / rename / recolour / delete) moved to",
             "function loadTags() {}\n// Tag management (create / rename / recolour / delete) moved to",
             "test_tag_management_left_the_servers_page"),
    Mutation("the display half is dragged along with the move",
             "templates/servers.html",
             "function renderServerTagPills",
             "function renderServerTagPillsMoved",
             "test_the_display_side_did_not_go_with_it"),
    Mutation("a moved function never lands in the settings partial",
             "templates/partials/settings/_servers.html",
             "window.saveEditTag = function",
             "window.saveEditTagX = function",
             "test_the_partial_defines_exactly_the_moved_functions"),
    Mutation("the dangling bootstrap call comes back",
             "templates/servers.html",
             "  loadDepsTable();",
             "  loadTags();\n  loadDepsTable();",
             "test_no_bootstrap_call_is_left_behind_on_servers"),
    Mutation("the assign menu points at the section that moved again",
             "templates/servers.html",
             "pAlert({{ t.get('tags_none_yet',",
             "pAlert('Create tags first in the Tag Management section below.'); pAlert({{ t.get('tags_none_yet',",
             "test_the_assign_menu_no_longer_points_at_a_section_that_moved"),
    Mutation("a tag name goes back into innerHTML unescaped",
             "templates/servers.html",
             "        ${_escHtml(t.name)}",
             "        ${t.name}",
             "test_tag_names_and_colours_are_escaped_in_the_assign_menu"),
    Mutation("a popup goes back to a raw colour literal",
             "templates/servers.html",
             "hover:bg-raised text-xs text-ink\"'",
             "hover:bg-[#334155] text-xs text-[#CBD5E1]\"'",
             "test_the_popups_carry_no_raw_colour_literals"),
    Mutation("the empty state is hand-rolled again",
             "templates/partials/settings/_servers.html",
             "none.innerHTML = window.prismEmptyState('tags', TAG_T.empty, TAG_T.emptyHint);",
             "none.innerHTML = '<p>No tags created yet</p>';",
             "test_the_empty_state_is_the_shared_one"),
    Mutation("the delete confirmation loses its placeholder in one locale",
             "i18n.py",
             "'tag_delete_confirm': \"Tag '{name}' l\u00f6schen?",
             "'tag_delete_confirm': \"Tag l\u00f6schen?",
             "test_the_delete_confirmation_names_the_tag_in_every_language"),
    # ── D4b: health checks, and the anchor ──────────────────────
    Mutation("a health-check function is left behind on /servers",
             "templates/servers.html",
             "// Health checks moved to Settings",
             "function loadHealthChecks() {}\n// Health checks moved to Settings",
             "test_health_checks_left_the_servers_page"),
    Mutation("a function is lost in the move",
             "templates/partials/settings/_health_checks.html",
             "function testNewHealthCheck(",
             "function testNewHealthCheckX(",
             "test_the_health_check_form_is_whole_at_its_destination"),
    Mutation("a dispatch shim stays behind, so its button goes dead",
             "templates/partials/settings/_health_checks.html",
             "function _srvDeleteHc()",
             "function _srvDeleteHcX()",
             "test_the_health_check_form_is_whole_at_its_destination"),
    Mutation("nothing loads the list on the page that owns it",
             "templates/partials/settings/_health_checks.html",
             "if (document.getElementById('health-check-list')) loadHealthChecks();",
             "// list loads itself, surely",
             "test_the_destination_loads_the_list_itself"),
    Mutation("the anchor is left behind when the section moves",
             "templates/partials/settings/_health_checks.html",
             '<div class="mb-8" id="health-checks">',
             '<div class="mb-8" id="health-checks-config">',
             "test_the_anchor_exists_where_the_link_points"),
    Mutation("the link keeps pointing at the old page",
             "templates/services.html",
             'href="/settings/servers#health-checks"',
             'href="/servers#health-checks"',
             "test_the_anchor_exists_where_the_link_points"),
    Mutation("a locale hint still sends the operator to the old place",
             "i18n.py",
             "Add a probe under Settings \u2192 Servers to watch",
             "Add a probe under Servers \u2192 Health Checks to watch",
             "test_no_locale_still_names_the_old_location"),
    Mutation("the health-check empty state is hand-rolled again",
             "templates/partials/settings/_health_checks.html",
             "{{ empty_state('activity',",
             "{{ '<p>No health checks configured</p>' }}{# empty_state('activity',",
             "test_the_health_check_empty_state_is_the_shared_one"),
    Mutation("the block claims to be a settings section of its own",
             "templates/partials/settings/_health_checks.html",
             '<div class="mb-8" id="health-checks">',
             '<section class="mb-8" id="health-checks">',
             "test_the_health_check_block_declares_no_section_of_its_own"),
    Mutation("the health-check bootstrap call comes back to /servers",
             "templates/servers.html",
             "  loadDepsTable();",
             "  loadHealthChecks();\n  loadDepsTable();",
             "test_no_health_check_bootstrap_call_is_left_behind"),
    Mutation("a locale loses a tag string",
             "i18n.py",
             "'tag_create_failed': 'Tag konnte nicht erstellt werden'",
             "'tag_create_failed_x': 'Tag konnte nicht erstellt werden'",
             "test_every_string_exists_in_every_locale"),
)


# ── the locale table's own integrity ─────────────────────────
suite(
    "i18n-integrity",
    Mutation("a key is defined twice and the second silently wins",
             "i18n.py",
             "'tags_none_yet': 'No tags exist yet.",
             "'sign_out': 'Something else', 'tags_none_yet': 'No tags exist yet.",
             "test_no_locale_defines_the_same_key_twice"),
    Mutation("the duplicate baseline is widened to excuse a new one",
             "tests/test_i18n_fallback.py",
             '"search", "select_all", "status", "thresholds",\n}',
             '"search", "select_all", "status", "thresholds", "sign_out",\n}',
             "test_no_locale_defines_the_same_key_twice"),
)


SUITE_FILES = {
    "loading": "tests/test_design_loading.py",
    "status-cache": "tests/test_status_summary_cache.py",
    "vitals": "tests/test_design_vitals.py",
    "estate-vitals": "tests/test_estate_vitals.py",
    "palette": "tests/test_css_tokens.py",
    "outbound": "tests/test_outbound_ratchet.py",
    "csp": "tests/test_csp.py",
    "health-tls": "tests/test_health_check_tls.py",
    "lan-only": "tests/test_lan_only.py",
    "severity-vocab": "tests/test_severity_vocab.py",
    "severity-roles": "tests/test_severity_roles.py",
    "maintenance-expiry": "tests/test_maintenance_expiry.py",
    "estate-fold": "tests/test_estate_fold.py",
    "estate-service": "tests/test_estate_service.py",
    "cascade": "tests/test_cascade.py",
    "cascade-persistence": "tests/test_cascade_persistence.py",
    "cascade-integration": "tests/test_cascade_integration.py",
    "promotion": "tests/test_cascade_promotion.py",
    "heart-monitor": "tests/test_design_heart_monitor.py",
    "restart-overlay": "tests/test_design_restart_overlay.py",
    "acceleration-release": "tests/test_acceleration_release.py",
    "ingest-caps": "tests/test_ingest_caps.py",
    "winrm-transport": "tests/test_winrm_transport_defaults.py",
    "ps-sandbox-bypasses": "tests/test_ps_sandbox_bypasses.py",
    "workflow-authoring-rbac": "tests/test_workflow_authoring_rbac.py",
    "fleet-walk": "tests/test_fleet_walk.py",
    "retention-lock": "tests/test_retention_lock.py",
    "business-hours": "tests/test_severity_business_hours.py",
    "servers-view": "tests/test_design_servers_view.py",
    "health-summary": "tests/test_health_check_summary.py",
    "literal-ratchet": "tests/test_design_tokens.py",
    "scroll": "tests/test_design_scroll.py",
    "states": "tests/test_design_states.py",
    "keyboard": "tests/test_design_keyboard.py",
    "empty-states": "tests/test_design_empty_states.py",
    "disabled": "tests/test_design_disabled.py",
    "layered": "tests/test_design_layered.py",
    "health-overview": "tests/test_health_check_overview.py",
    "overview-pages": "tests/test_design_overview_pages.py",
    "settings-tracking": "tests/test_settings_change_tracking.py",
    "action-dispatch": "tests/test_action_dispatch.py",
    "pages-render": "tests/test_pages_render.py",
    "permissions": "tests/test_design_permissions.py",
    "settings-compliance": "tests/test_settings_compliance.py",
    "navigation": "tests/test_navigation.py",
    "jump-search": "tests/test_jump_search.py",
    "settings-servers": "tests/test_settings_servers.py",
    "i18n-integrity": "tests/test_i18n_fallback.py",
}


# pytest's exit code for "no tests were collected", which is what a `-k`
# expression naming a test that is not in the file produces.
_EXIT_NOTESTSCOLLECTED = 5


def _pytest(suite_file: str, k: str = "") -> tuple[bool, str, int]:
    cmd = [sys.executable, "-m", "pytest", suite_file, "-q", "--no-header",
           "-p", "no:randomly"]
    if k:
        cmd += ["-k", k]
    p = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (p.stdout or "").strip().splitlines()
    return p.returncode == 0, tail[-1] if tail else "(no output)", p.returncode


# Line endings, per file. `Path.read_text` normalises CRLF to LF and
# `write_text` translates back on Windows, so every file the harness touched
# came back with CRLF even when it went in with LF — `git status` said
# modified while `git diff` said nothing, because Git normalises the content.
#
# Reading with `newline=""` fixes the restore and breaks the matching: every
# Mutation's `find` is written with `\n`, so a multi-line anchor stops
# matching a CRLF file. Both halves are needed — read normalised, write in
# whatever the file actually uses.
_FILE_NEWLINES = {}


def _read_exact(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
        seen = fh.newlines
    # A tuple means mixed endings; `\n` is what the repo uses.
    _FILE_NEWLINES[str(path)] = seen if isinstance(seen, str) else "\n"
    return text


def _write_exact(path, text):
    with open(path, "w", encoding="utf-8",
              newline=_FILE_NEWLINES.get(str(path), "\n")) as fh:
        fh.write(text)


def run_suite(name: str) -> tuple[int, int]:
    suite_file = SUITE_FILES[name]
    print(f"\n=== {name}  ({suite_file}) ===")
    green, tail, _ = _pytest(suite_file)
    if not green:
        print(f"  BASELINE NOT GREEN, skipping: {tail}")
        return 0, len(SUITES[name])
    print(f"  baseline: {tail}")

    caught = 0
    for m in SUITES[name]:
        path = PROJECT_ROOT / m.path
        if not path.exists():
            # A missing FILE used to raise out of run_suite, which stopped the
            # whole run — every suite after it silently never ran. Reported
            # like a missing anchor now: loudly, and the run continues.
            print(f"  !! {m.label}\n       FILE NOT FOUND: {m.path} — "
                  "the file moved or was deleted; update this mutation")
            continue
        original = _read_exact(path)
        if m.find not in original:
            print(f"  !! {m.label}\n       ANCHOR NOT FOUND in {m.path} — "
                  "the code moved; update this mutation")
            continue
        _write_exact(path, original.replace(m.find, m.replace, 1))
        try:
            landed = _read_exact(path) != original
            passed, _, code = _pytest(suite_file, m.test)
            # A `-k` that selects NOTHING exits 5, and "not zero" was being
            # read as "the test failed" — so a mutation whose named test lives
            # in a different file reported itself as correctly caught while no
            # test ran at all. Two of them did exactly that, in this file,
            # within an hour of it being extended. It is the same shape as
            # every entry in docs/OPS-LEARNINGS.md §2.2: the checker's own
            # correctness was the thing nobody checked.
            missing = code == _EXIT_NOTESTSCOLLECTED
            ok = landed and not passed and not missing
            caught += bool(ok)
            if ok:
                print(f"  OK  {m.label}")
            else:
                if missing:
                    why = (f"TEST NOT FOUND in {suite_file} — the -k "
                           "expression selected nothing, so this mutation "
                           "proves nothing; move it to the suite whose file "
                           "holds that test")
                elif not landed:
                    why = "mutation DID NOT LAND"
                else:
                    why = "test still PASSED — it is blind"
                print(f"  !!  {m.label}\n       {why}  ({m.test})")
        finally:
            _write_exact(path, original)
            restored = _read_exact(path)
            if restored != original:
                raise SystemExit(f"RESTORE FAILED for {m.path} — fix by hand before continuing")
    return caught, len(SUITES[name])


def main(argv: list[str]) -> int:
    wanted = [n for n in SUITES if not argv or any(a in n for a in argv)]
    if not wanted:
        print(f"no suite matches {argv}; known: {', '.join(SUITES)}")
        return 2

    total_caught = total = 0
    for name in wanted:
        c, t = run_suite(name)
        total_caught += c
        total += t

    print(f"\n{total_caught}/{total} mutations correctly caught")
    if total_caught != total:
        print("\nA mutation that is not caught means the test cannot see the defect "
              "it was written for. Fix the TEST, not this file.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
