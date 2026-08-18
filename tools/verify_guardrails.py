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
    Mutation("part 3 dropped from a JS empty state", "templates/rbac.html",
             "window.prismEmptyState('check-circle', 'No pending approvals',\n"
             "          'Nothing is waiting on you — requests needing sign-off will appear here')",
             "window.prismEmptyState('check-circle', 'No pending approvals')",
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
             "    if (reduceMotion || !beating() || document.hidden) {",
             "    if (!beating() || document.hidden) {",
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
             "    if (reduceMotion || !beating() || document.hidden) {",
             "    if (reduceMotion || !beating()) {",
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
    Mutation("a coming-soon card loses its reason",
             "templates/partials/vitals_quadrant.html",
             """data-tip-title="{{ t.get('vitals_scan_tip_title', 'Posture scanning is not built yet') }}\"""",
             "", "test_a_coming_soon_card_says_why_it_is_unavailable"),
    Mutation("a coming-soon card becomes focusable but inert",
             "templates/partials/vitals_quadrant.html",
             '<div class="vitals-card vitals-card--tr vitals-card--soon" aria-disabled="true"',
             '<div class="vitals-card vitals-card--tr vitals-card--soon" tabindex="0" aria-disabled="true"',
             "test_a_coming_soon_card_is_not_focusable_but_inert"),
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
    "servers-view": "tests/test_design_servers_view.py",
    "health-summary": "tests/test_health_check_summary.py",
    "literal-ratchet": "tests/test_design_tokens.py",
    "scroll": "tests/test_design_scroll.py",
    "states": "tests/test_design_states.py",
    "keyboard": "tests/test_design_keyboard.py",
    "empty-states": "tests/test_design_empty_states.py",
    "disabled": "tests/test_design_disabled.py",
    "layered": "tests/test_design_layered.py",
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
        original = path.read_text(encoding="utf-8")
        if m.find not in original:
            print(f"  !! {m.label}\n       ANCHOR NOT FOUND in {m.path} — "
                  "the code moved; update this mutation")
            continue
        path.write_text(original.replace(m.find, m.replace, 1), encoding="utf-8")
        try:
            landed = path.read_text(encoding="utf-8") != original
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
            path.write_text(original, encoding="utf-8")
            restored = path.read_text(encoding="utf-8")
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
