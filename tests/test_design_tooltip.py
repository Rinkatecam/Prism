"""The tooltip mechanism — DESIGN_SYSTEM_SPEC.md §5 and §8.3 (T-1..T-15).

Steps 1-5 of WP-6 landed the pinned-scale panel CSS (step 3), the keyboard/
touch/Escape state machine (step 4), and the authoring macros in
`partials/_tip.html` (step 5) — `tip()`, `tip_button()`, `tip_mirror()`,
`tip_overflow()`. Step 7 retrofitted the (measured, not the stale planned
"38") carrier estate onto them — see TIP_CARRIER_BASELINE's own comment for
the corrected count and what it does/doesn't include. Step 8 deleted the two
long-standing rival panels that predated all of this — `#block-tooltip`
(workflows.html) and `#topo-tooltip` (topology.html) — and repointed their
call sites at the same mechanism, leaving `#ps-tooltip` as the only tooltip
panel anywhere in the tree (T-7). This file is what governs every tooltip
written from here on.

WHAT THIS FILE CANNOT SEE — read before trusting a green run:

  * PROSE CLASSIFICATION. No regex can tell "explanatory" (tooltip-worthy,
    §5.7 gate 5) from "status" (must stay inline, gates 1-4). That is a
    human judgement call, checked in review against the five gates in §5.7,
    not something T-9 or T-14 can verify. Both ratchets pin a COUNT; neither
    can tell a good migration from a bad one.
  * ESCAPE, LONG-PRESS AND OUTSIDE-TAP BEHAVIOUR. T-6 proves the LISTENERS
    are registered (`document.addEventListener('keydown', ...)` exists).
    It cannot press Escape and observe the panel actually close, hold a
    finger down for 600ms, or tap outside a panel on a real touchscreen.
    That needed a live browser — step 10, measured 2026-09-18 against
    settings.html's poll_interval tip (a) and data_retention tip (b), a
    server_analytics.html cpu/ram forecast tip inside base.html's shared
    `#prism-modal` (c, d), settings/general's tip under mobile touch
    emulation (e), a `tip_overflow()` carrier on /servers' card view (f),
    /settings/general's tip at all four viewport edges (g), and
    server_detail.html's htmx-loaded analytics partial (j):
      (a) A real (not scripted) Shift+Tab lands `:focus-visible` on the
          `.ps-tip` button and the panel opens with the correct content,
          zero delay — `.focus()` alone does NOT do this (Chromium does
          not grant `:focus-visible` to a scripted focus call the same way
          it does to real keyboard navigation; the app's own onFocusIn
          correctly gates on `:focus-visible` for exactly this reason, so
          testing it needs an actual Tab keypress, not a shortcut).
      (b) Escape closes the panel; `document.activeElement` is still the
          `.ps-tip` button afterward — focus provably did not move.
      (c) With `#prism-modal` open (z-index 9999) and a tip open on a
          relocated real trigger inside its rect, one Escape closes only
          the tip (`#ps-tooltip` hidden) — the modal stayed open
          (`!classList.contains('hidden')` true throughout).
      (d) With the same modal+tip both open and their rects overlapping,
          `document.elementFromPoint()` at the overlap returns the
          tooltip panel's own child, not the modal — the panel is the
          actually-painted, actually-hit-testable element, not just a
          higher z-index number on paper. T-8 already pins the numbers
          (10000 > every other z-index in the tree, including this
          modal's 9999); this is the live confirmation the numbers
          actually resolve the tie in paint order too.
      (e) Under `resize_window` mobile-touch emulation (`navigator.
          maxTouchPoints` confirmed 5), a tap on the info-icon carrier
          opens the panel with the right content; a tap elsewhere on the
          page closes it.
      (f) A real `PointerEvent('pointerdown', {pointerType: 'touch'})` on
          a `tip_overflow()` carrier, held past PRESS_MS (600ms) before
          `pointerup`, opens the panel PINNED (stays open through the
          release) — confirming `longPressed` gates the synthesized
          click that follows so it does not immediately toggle the panel
          shut. A `contextmenu` event dispatched on the same carrier
          during/after the hold comes back `defaultPrevented: true` — no
          OS text-selection callout. (One methodology note from getting
          this measurement right: a scripted `pointerdown` with no
          matching event loop tick between dispatch and check can read
          `visible: false` even though `show()` already ran and the panel
          holds the correct content — an artifact of checking across
          separate tool calls with an unrelated intervening action, not
          the app closing early. Measuring dispatch, the 650ms wait and
          the visibility check all inside ONE script, so nothing else
          can run in between, is what makes this measurement reliable.)
      (g) `anchor()`'s own placement algorithm, exercised directly by
          relocating a real trigger (inline `position: fixed`) to each
          edge in turn: moved to the bottom edge, the panel flips ABOVE
          the trigger (`panelBottom <= buttonTop`, with the GAP honoured)
          instead of running off the bottom of the viewport. Moved to the
          left edge, the panel's own left edge holds at the EDGE margin
          (12px) rather than going negative. Moved to the right edge, the
          panel's right edge holds at `viewport width - EDGE` rather than
          overflowing — clamped, never flipped horizontally, exactly as
          the code's own comment describes. The top edge is the ordinary,
          default below-placement case, exercised implicitly by every
          other measurement here.
      (j) Opening a tip inside server_detail.html's htmx-loaded
          `#server-analytics` partial, then issuing a real `htmx.ajax()`
          reload of that same partial (not `htmx.trigger(el, 'load')` —
          that call does NOT re-fire an already-consumed one-shot `load`
          trigger, confirmed by instrumenting `htmx:beforeSwap`/
          `afterSwap` and observing neither fired; it produced a false
          "still open" reading on the first attempt, which turned out to
          be a test-methodology gap, not an app bug): the OLD carrier
          element is confirmed detached (`isConnected: false`) after the
          swap, and the panel is confirmed closed (`visible: false`) —
          `htmx:afterSwap`'s own `if (!current.isConnected) closeTip()`
          backstop firing correctly, not stranding the panel on a removed
          node.
      (a) was measured on a standalone `.ps-tip` button specifically;
      whether Tab equally reaches an `aria-disabled`+`data-inert`
      REASONED carrier (tests/test_design_disabled.py's own remaining
      step-10 note, below) was not separately re-measured live — the
      carrier is served by this exact same `bindAll()`/`onFocusIn`
      listener pair, registered identically regardless of which HTML
      attribute made the element a tip carrier, so the finding is
      inferred from shared code rather than re-tested against a second
      live control. The one real candidate found for a live re-test
      (settings/servers' delete-confirm-btn) sits behind a delete
      confirmation for a real fleet server; re-testing it was judged not
      worth that risk for a claim already covered by code-sharing.
  * RENDERED CONTRAST. T-11 pins the panel to the token scale (border-radius,
    transition, no stray `!important`). It cannot read a pixel off a
    rendered page in either theme. Step 10 (h), measured 2026-09-18 on
    settings.html's poll_interval tip (title "Per-server cadence" + its
    desc), reading `getComputedStyle` off the actually-rendered
    `#ps-tooltip` in both themes and computing WCAG contrast against its
    own background, not against a token value:
      light: bg rgb(255,255,255); title rgb(2,6,23) → 20.17:1; desc
        rgb(71,85,105) → 7.58:1.
      dark: bg rgb(15,22,35); title rgb(241,245,249) → 16.53:1; desc
        rgb(163,178,199) → 8.41:1.
    All four clear AA's 4.5:1 with large margins (the tightest, light
    desc at 7.58:1, still clears AAA's 7:1).
  * PREFERS-REDUCED-MOTION, LIVE. Step 10 (i) was NOT measured via an
    actual OS/browser-level media-feature toggle — the browser automation
    available for this measurement had no such emulation control exposed
    (`resize_window` emulates viewport and colour scheme, not this
    feature; no other tool in reach did either). Verified instead,
    honestly short of a live toggle: app.css's global rule (`*, *::before,
    *::after { transition-duration: 0.01ms !important; ... }`) uses the
    universal selector at `!important`, which no per-element rule can
    escape unless it ALSO carries `!important` at higher specificity —
    `#ps-tooltip`'s own transition rule does not, confirmed by reading it
    directly — and `test_reduced_motion_is_honoured_globally_not_rule_by_
    rule` (this file, passing) independently pins that no rule anywhere in
    the tree carries such an override. The delay custom properties
    (`--tip-delay-open`/`close`, T-12) are separate, unrelated CSS values
    the reduced-motion media query never touches, so "the delays are
    unchanged" holds by construction, not by a specific measurement. If a
    tool with real media-feature emulation becomes available, this is the
    one step-10 item still worth a genuine live re-check. Step 27 (WP-6
    close-out) checked again, while taking the RENDERED CONTRAST readings
    below, for any new tool offering that emulation: none had appeared,
    so this stays the reasoned-not-measured finding above rather than
    being silently re-labelled "done" or quietly dropped at close-out.
  * CARRIERS BUILT BY CLIENT-SIDE JAVASCRIPT AFTER THE PAGE LOADS. T-2/T-3/
    T-4 render every route through Flask's test client, which executes
    Jinja but no JavaScript — so a carrier that only exists after a script
    runs is invisible to every test in this file, not just the rendered
    ones (T-9's ratchet is narrower still — see its own comment). Step 7
    converted what it could reach this way regardless — a matching sr-only
    mirror + aria-describedby replicated by hand in the JS itself, for the
    pagination prev/next buttons, the security-status tile() factory and
    the failed-login heatmap cells in server_detail.html — but T-2/T-3/T-4
    passing green proves only the SERVER-RENDERED half; nothing here can
    confirm those JS-built carriers actually work in a live DOM. Step 10.
  * A CAUTION FOR ANYONE EXTENDING THIS FILE'S OWN REGEX SCANS: JS source
    that builds HTML via `'attr="' + var + '"'` string concatenation reads
    back to `_carrier_elements`/T-3 as a literal `' + var + '`-shaped
    attribute VALUE — a real false positive step 7 hit twice while adding
    the JS-built carriers above (server_detail.html's failed-login heatmap),
    not a hypothetical one. Backtick template literals with `${var}`
    read cleanly instead, and even then T-3's "label as long as desc"
    length check compares the PLACEHOLDER NAME's own length when the
    scanner can't evaluate the expression — keep an aria-label placeholder's
    variable name shorter than its desc counterpart's for exactly this
    reason (see that file's heatmap cell for the worked example).

── T-1..T-4 — RETROFITTED BY STEP 7, MARKERS REMOVED ─────────────────────

T-1 through T-4 assert that every hand-written `data-tip-*` carrier is
focusable and properly named. Before step 7 they were bare `<i>`/`<span>`/
`<div>` elements — not focusable, most with no `aria-label`, none with
`aria-describedby` — and all four tests were `xfail(strict=True)` for
exactly that reason. Step 7 converted every real carrier (the macros where
the shape fit, a hand-written `tabindex`/`aria-describedby`/`tip_mirror()`
triple where an existing labelled element — a badge, a chip, a disabled
button, a card link — had to stay the carrier itself rather than gain a
redundant second trigger) and, having verified all four now pass for real
(not vacuously — `test_the_heading_mirror_scan_actually_catches_a_violation`
and this suite's own positive controls exist for exactly that worry),
removed their `xfail` markers. `strict=True` did its job: the run failed
with `XPASS(strict)` the moment the retrofit made them genuinely pass,
which was the signal to take the markers off rather than leave them as
quietly-inert decoration.

── T-7 — DELETED BY STEP 8, MARKER REMOVED ───────────────────────────────

T-7 (`test_exactly_one_tooltip_panel_exists`) asserts `id="ps-tooltip"`
appears exactly once in the tree and `id="block-tooltip"`/`id="topo-tooltip"`
appear zero times. Before step 8 the latter two both existed — a bespoke
dark popup in workflows.html (icon + category + monospace example, positioned
from the cursor) and a second one in topology.html (status badge, CPU/RAM/
disk bars, a dependency list, also cursor-positioned) — both with their own
hardcoded slate hexes, and the test was `xfail(strict=True)` for exactly
that reason. Step 8 deleted both panels' markup, CSS and bind/show/hide JS
outright and repointed every call site at the same data-tip-title/
data-tip-desc pair `#ps-tooltip` already reads everywhere else:
workflows.html's palette items and canvas nodes both read a shared
BLOCK_DEFS table via a new `applyBlockTip()`, and topology.html's SVG graph
nodes read the fetched node data via a new `applyNodeTip()` — JS
`setAttribute` in both cases, since neither carrier is a Jinja-authored
static element (see `_tip_carrier_counts()`'s own header comment on why a
`setAttribute`-built carrier is invisible to T-9's ratchet, not just this
test). Unlike T-5's heading-mirror scan, T-7's own assertion is a direct
count of real ids in real files — there is no vacuous-pass risk to guard
against separately. `strict=True` did its job again: the run failed with
`XPASS(strict)` the moment the deletion made it genuinely pass, which was
the signal to take the marker off.

Deleting both panels' CSS also removed each one's own `z-index: 9999;`
declaration — two of the eight 9999 sites `tests/test_design_tokens.py`'s
`Z_LITERAL_BASELINE` counted when it was seeded. That ratchet (and its
`Z_LITERAL_TOTAL`) is lowered in the same commit as this file's changes, for
the same "not left behind" reason as every other baseline in this codebase.
The OTHER ratchet in that file, `LITERAL_BASELINE`/`LITERAL_TOTAL` (raw hex
colours), does NOT move: both panels' colours were plain CSS `property:
#hex` declarations, not the Tailwind arbitrary-value bracket syntax
(`-[#hex]`) that ratchet's detector matches, so measuring it — per this
codebase's own "run the detector, don't copy a number" rule — found zero
change in either file. Reported rather than forced, exactly like every
other baseline in this suite that measured different from what a brief
assumed.

── T-15 — LANDED BY STEP 9, MARKER REMOVED ───────────────────────────────

T-15 (`test_a_control_with_a_reason_is_aria_disabled_not_disabled`) turned
up already failing when step 6 measured the tree for its xfail set — one
more than that step's own brief named (T-1..T-4 only), found by running the
checks rather than assumed. At that point `prismSetDisabled` still set the
native `disabled` attribute whenever a reason was supplied, and
`data-inert` existed nowhere in the tree (checked: zero hits, any file, any
form). DESIGN_SYSTEM_SPEC.md's own step 9 text is "change
`prismSetDisabled`... and add T-15", naming this test as step 9's to make
pass, not step 6's or step 8's — so step 6 marked it `xfail(strict=True)`
naming step 9, a DEVIATION from that step's brief flagged there (and in its
own report) rather than either forced green (it could not honestly be) or
left as an unmarked failure, which would have broken "green with N
xfails" for every run from step 6 to step 9.

Step 9 made the change T-15 was written for: a truthy `title` now takes the
`aria-disabled="true"` + `data-inert="1"` path instead of native `disabled`
— see `prismSetDisabled`'s own header comment in base.html for the full
"machine working" vs "waiting on you" split — and the `[data-action]`
dispatcher's `run()` refuses to act on a carrier caught by `data-inert`.
`strict=True` did its job a third time: the run failed with `XPASS(strict)`
the moment the change made it genuinely pass, which was the signal to take
the marker off, exactly like T-1..T-4 and T-7 before it.

── THE Z_LITERAL_BASELINE RATCHET IS NOT HERE ────────────────────────────

DESIGN_SYSTEM_SPEC.md §9's ratchet registry lists `Z_LITERAL_BASELINE` as
living in this file. It does not: step 2 already built it, seeded and
tested, in `tests/test_design_tokens.py` (`Z_LITERAL_BASELINE`,
`Z_LITERAL_TOTAL`, and their "not left behind" / "no new file" / "total
never rises" companions) — see that file. Duplicating it here would give
the same ratchet two independent, driftable copies. T-8 below
(`test_the_tooltip_outranks_every_other_layer`) is a different assertion —
ordering/dominance, not a literal count — and lives here on its own.

── TWO BASELINES THAT MEASURED DIFFERENT FROM THIS STEP'S BRIEF ──────────

Per DESIGN_SYSTEM_SPEC.md's "Seeding rule (C28)": every baseline here comes
from RUNNING its detector against this tree, never from copying a number
out of a planning document. Both ran different from what was expected:

  * TIP_CARRIER_BASELINE. Expected 38 (base.html 10, server_detail.html 13,
    settings.html 4, server_card.html 4, server_comparison.html 2,
    vitals_quadrant.html 2, _server_config.html 2, operations.html 1).
    Measured: 25 (base.html 0, server_detail.html 10, the other six
    unchanged). base.html carries ZERO hand-written `data-tip-title=`
    carriers today — checked three independent ways (raw grep, a
    comment-aware substring scan, and a tag-aware scan; all three agree).
    The gap is not a detector bug: base.html's sidebar nav (WP-4 D7, "the
    sidebar stopped lying about where you are") now explains itself with
    visible labels and `aria-label`, not hover tooltips, and that commit
    landed before this one. Two more real carriers exist in
    server_detail.html via `el.setAttribute('data-tip-title', ...)` (the
    detection-mode chip and the status-dot fusion reason) that this
    baseline's detector — matching §9's literal wording, "hand-written
    `data-tip-title=`" — does not count, because a regex broad enough to
    catch `setAttribute(...)` calls also matches `prismSetDisabled`'s own
    generic implementation (`el.setAttribute('data-tip-title', title)`,
    where `title` is a parameter, not a carrier) as a false positive, and
    there is no reliable regex-only way to tell "a specific literal reason"
    from "a parameter being forwarded". Named here so step 7 does not get
    silently let off the hook for those two.
  * DESC_LINE_BASELINE. Expected "225 broad / 50-63 narrow ... depending on
    which detector variant you build" (§9) — deliberately not a precise
    target. Measured: 114 across 33 templates (41 in the Settings family).
    This is bookkeeping for the later content-migration steps (25-26), not
    something this step fixes; the detector variant and its trade-offs are
    documented at `_desc_line_counts()` below.

Every number above was reported to the coordinator rather than adjusted to
match the brief — "do not force the number" per the spec's own instruction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools import design_tokens as dt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"
BASE = TEMPLATES / "base.html"
APP_CSS = PROJECT_ROOT / "static" / "css" / "app.css"
TIP_PARTIAL = "partials/_tip.html"


# ── the comment stripper, and its positive control ────────────────────────
#
# Identical to tests/test_design_tokens.py and tests/test_design_disabled.py
# — same regexes, same function body — because a third independent
# reimplementation is a third place for the same bug (or the same fix) to
# drift out of step. What it strips: Jinja `{# #}`, HTML `<!-- -->`, JS
# block comments `/* */`, and `//` line comments anchored at the start of a
# line. What it must NOT strip: real code that merely contains
# comment-LIKE substrings — a `https://` URL, or a trailing `// note` after
# real code on the same line.
#
# Not hypothetical here: base.html:1038 carries
#   // `<i data-lucide="info" data-tip-title=…>` carriers on the Settings
# — a comment that quotes the exact fake-tag shape this file's carrier scan
# looks for. Without stripping, that one line is a false positive in BOTH
# directions at once: it fabricates a non-focusable `<i>` carrier for T-1's
# static scan, and (because base.html's whole <script> block is rendered
# verbatim into every page) the same fake tag reappears in T-2/T-3/T-4's
# rendered HTML on every route.
_COMMENTS = re.compile(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//[^\n]*", re.M)


def _code_only(text: str) -> str:
    blanked = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), blanked)


def test_the_counter_reads_code_and_not_the_comments_about_it():
    """Positive control for `_code_only`, in both directions — a stripper
    that strips nothing reports fake carriers out of its own documentation
    (under-stripping in the OTHER sense: over-REPORTING); a stripper that
    strips too much hides real ones. Both are asserted, over the exact fake
    tag base.html's own comment quotes."""
    quoted = ('    // `<i data-lucide="info" data-tip-title=…>` carriers on '
              'the Settings pages')
    assert "data-tip-title" in quoted, "the sample no longer contains what it quotes"
    assert "data-tip-title" not in _code_only(quoted), (
        "a fake carrier quoted inside a `//` comment is surviving the strip "
        "— T-1/T-2 will fabricate a violation out of this exact line")

    real = ('       <i data-lucide="info" class="w-3.5 h-3.5 text-faint"\n'
            '          data-tip-title="Real title" data-tip-desc="Real desc"></i>')
    assert "data-tip-title" in _code_only(real), (
        "the stripper is eating real code, not just comments — a genuine "
        "carrier would go uncounted")

    url_line = "  const u = 'https://example.test/a'; // trailing note"
    assert "https://example.test/a" in _code_only(url_line), (
        "a `//` line-comment rule anchored to mid-line would eat the "
        "`https://` in this URL and everything after it on the line")


# ── shared carrier extraction — used by T-1 through T-5 ───────────────────
#
# One element definition, reused everywhere a "carrier" needs inspecting:
# its tag name, its attributes (by value, not just by presence), the raw
# body between its open and close tag, and a whitespace-collapsed
# tags-stripped "visible text" derived from that body. Quote-aware ("…"/'…'
# treated as atomic units that may contain `>`) so a Jinja comparison
# embedded in an attribute value does not end the tag match early — the
# same class of bug tests/test_design_disabled.py's `_defuse` exists to
# avoid for `${…}` interpolations.
#
# Known blind spot, accepted rather than chased: a carrier built by
# concatenating a JS template literal across a ternary — `${atFirst ? '...'
# : ''}` — nests a JS single-quoted string containing HTML double-quoted
# attributes inside a `${}` interpolation. The quote-aware matcher generally
# survives this (the nested string has no OTHER single quote inside it in
# every case measured), but it is not guaranteed for an arbitrary future
# one, and T-1 is explicitly the static, best-effort half of the pair —
# T-2 catches what rendering exposes; step 10 is the real backstop for
# anything JavaScript composes at runtime.
_TAG_OPEN = re.compile(r"<([a-zA-Z][\w:-]*)\b((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>", re.S)
_HAS_TIP_ATTR = re.compile(r"\bdata-tip-(?:title|desc)\s*=")
_ATTR_VALUE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _attrs_of(attr_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_VALUE.finditer(attr_text):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _carrier_elements(text: str) -> list[dict]:
    """Every element in `text` carrying data-tip-title or data-tip-desc, as
    {tag, attrs, body, visible, start}. Matches the FIRST same-named close
    tag after the open tag, which is wrong for a carrier that nests another
    element of the same tag name — a shape none of today's carriers use
    (checked by hand against every one of the 25 in TIP_CARRIER_BASELINE)."""
    out = []
    for m in _TAG_OPEN.finditer(text):
        tag, attr_text = m.group(1), m.group(2)
        if not _HAS_TIP_ATTR.search(attr_text):
            continue
        attrs = _attrs_of(attr_text)
        close = re.search(rf"</{re.escape(tag)}\s*>", text[m.end():], re.I)
        body = text[m.end():m.end() + close.start()] if close else ""
        visible = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", body)).strip()
        out.append({"tag": tag.lower(), "attrs": attrs, "body": body,
                    "visible": visible, "start": m.start()})
    return out


def _is_focusable(el: dict) -> bool:
    """T-1/T-2's shape check: <button>, <a href>, <summary>, or tabindex="0".
    A shape check, not a "reachable right now" check — a `<button disabled>`
    counts (it IS a button); whether `disabled` also makes it unreachable is
    tests/test_design_disabled.py's and T-15's concern, not this one's."""
    if el["tag"] in ("button", "summary"):
        return True
    if el["tag"] == "a" and el["attrs"].get("href"):
        return True
    return el["attrs"].get("tabindex") == "0"


def _static_carrier_violations() -> list[str]:
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        for el in _carrier_elements(text):
            if not _is_focusable(el):
                violations.append(
                    f"{rel}: <{el['tag']}> carries data-tip-* but is not a "
                    'button/a[href]/summary and has no tabindex="0"')
    return violations


def test_every_tip_carrier_is_focusable():
    violations = _static_carrier_violations()
    assert not violations, (
        "tip carrier(s) a keyboard cannot reach:\n  " + "\n  ".join(violations))


# ── rendered fixtures — used by T-2, T-3, T-4 ─────────────────────────────
#
# Same client/route-discovery idiom as tests/test_pages_render.py: derive
# every GET route from the app's own URL map rather than hand-listing pages,
# so a route added without exercising this file is not a route silently
# unseen. Rendered once per test session (module-scoped) and shared across
# T-2/T-3/T-4 rather than re-rendered per test.

@pytest.fixture(scope="module")
def client():
    import app as prism_app
    prism_app.app.config["TESTING"] = True
    return prism_app.app.test_client()


def _page_routes() -> list[str]:
    import app as prism_app
    out = []
    for rule in prism_app.app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        path = rule.rule
        if path.startswith(("/api/", "/static/", "/partials/")):
            continue
        if path in ("/logout", "/login", "/setup"):
            continue  # auth flows: redirect by design
        if "<" in path:
            continue  # parameterised — handled by _server_detail_route below
        out.append(path)
    return sorted(set(out))


def _settings_sections() -> list[str]:
    from routes.views import _SETTINGS_SECTIONS
    return [f"/settings/{name}" for name in _SETTINGS_SECTIONS]


def _server_detail_route() -> str | None:
    """One real server's detail page, opportunistically — server_detail.html
    and server_card.html carry 14 of the 25 measured carriers, and skipping
    every parameterised route (test_pages_render.py's own approach) would
    leave T-2/T-3/T-4 blind to more than half the estate. Not required: an
    install with zero configured servers (or CI without config.json) must
    still pass every OTHER route, so this returns None rather than failing
    when there is nothing to look at. Reads the name at test time rather
    than hardcoding one — config.json is gitignored and its server names are
    this installation's own inventory, not something to commit."""
    import app as prism_app
    try:
        servers = prism_app.config.get_servers()
    except Exception:
        return None
    return f"/server/{servers[0].name}" if servers else None


def _rendered_bodies(client) -> dict[str, str]:
    routes = _page_routes() + _settings_sections()
    extra = _server_detail_route()
    if extra:
        routes = routes + [extra]
    bodies = {}
    for path in routes:
        r = client.get(path)
        if r.status_code == 200:
            bodies[path] = r.get_data(as_text=True)
    return bodies


@pytest.fixture(scope="module")
def rendered_pages(client) -> dict[str, str]:
    return _rendered_bodies(client)


def test_every_rendered_tip_carrier_is_focusable(rendered_pages):
    violations = []
    for path, html in rendered_pages.items():
        for el in _carrier_elements(_code_only(html)):
            if not _is_focusable(el):
                violations.append(f"{path}: <{el['tag']}>")
    assert not violations, (
        "rendered tip carrier(s) a keyboard cannot reach:\n  "
        + "\n  ".join(violations))


def test_every_icon_only_carrier_has_an_accessible_name(rendered_pages):
    violations = []
    for path, html in rendered_pages.items():
        for el in _carrier_elements(_code_only(html)):
            if el["visible"]:
                continue  # has visible text of its own — not this check's concern
            label = el["attrs"].get("aria-label", "").strip()
            desc = el["attrs"].get("data-tip-desc", "")
            if not label:
                violations.append(f"{path}: icon-only <{el['tag']}> has no aria-label")
            elif desc and len(label) >= len(desc):
                violations.append(
                    f"{path}: <{el['tag']}> aria-label is as long as or longer "
                    "than its data-tip-desc — the explanation may have been "
                    "pasted into the label")
    assert not violations, "\n  ".join(violations)


# `[^>]*` (not `.*?`) inside the lookaheads: sr-only spans never span
# multiple lines in this codebase, and bounding the lookahead to one line
# keeps a stray earlier/later `<span>` on another line from being pulled in.
_SR_ONLY_BY_ID = re.compile(
    r'<span\b(?=[^>]*\bclass="[^"]*\bsr-only\b)(?=[^>]*\bid="(?P<id>[^"]*)")'
    r'[^>]*>(?P<text>.*?)</span>', re.S)


def _sr_only_spans(text: str) -> dict[str, str]:
    return {m.group("id"): m.group("text") for m in _SR_ONLY_BY_ID.finditer(text)}


def test_every_tip_desc_is_mirrored_in_an_sr_only_span(rendered_pages):
    violations = []
    for path, html in rendered_pages.items():
        text = _code_only(html)
        mirrors = _sr_only_spans(text)
        for el in _carrier_elements(text):
            if "ps-tip-overflow" in el["attrs"].get("class", ""):
                continue  # §5.6: text already visible — no mirror, no describedby
            desc = el["attrs"].get("data-tip-desc", "")
            described_by = el["attrs"].get("aria-describedby", "")
            if not described_by:
                violations.append(
                    f"{path}: <{el['tag']}> has data-tip-desc but no aria-describedby")
                continue
            if described_by not in mirrors:
                violations.append(
                    f"{path}: aria-describedby={described_by!r} resolves to nothing "
                    "in the same document")
                continue
            if mirrors[described_by] != desc:
                violations.append(
                    f"{path}: sr-only mirror for {described_by!r} does not equal "
                    "data-tip-desc character for character")
    assert not violations, "\n  ".join(violations)


# ── T-5 — the mirror is never a heading's child (C17) ─────────────────────

_HEADING_BLOCK = re.compile(r"<(h[1-4])\b[^>]*>.*?</\1>", re.S | re.I)
_SR_ONLY_ANY = re.compile(r'<span\b[^>]*\bclass="[^"]*\bsr-only\b', re.I)


def test_no_tip_mirror_sits_inside_a_heading():
    """Nothing calls tip_button()/tip_mirror() from a real page yet (step 7),
    so this passes vacuously today — see the positive control below for
    proof the scan would catch a violation, not just that none exists."""
    violations = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        text = _code_only(p.read_text(encoding="utf-8"))
        for hm in _HEADING_BLOCK.finditer(text):
            if _SR_ONLY_ANY.search(hm.group(0)):
                violations.append(
                    f"{p.relative_to(TEMPLATES)}: sr-only span nested inside <{hm.group(1)}>")
    assert not violations, (
        "a tip mirror sits inside a heading (C17) — search_index._headings "
        "strips tags and drops any heading over 80 characters, so a nested "
        "mirror either pollutes the index label or deletes the jump "
        "target:\n  " + "\n  ".join(violations))


def test_the_heading_mirror_scan_actually_catches_a_violation():
    """Positive control: a scan that passes because nothing calls the macro
    yet is indistinguishable from a scan that is broken. Prove it fires."""
    sample = ('<h2 class="flex items-center gap-2">Title '
              '<span class="sr-only" id="ps-tip-1">desc</span></h2>')
    match = _HEADING_BLOCK.search(sample)
    assert match and _SR_ONLY_ANY.search(match.group(0)), (
        "the heading/mirror scan no longer catches a mirror nested in a heading")


# ── T-6 — the panel script binds keyboard and touch, not the cursor ───────

def _tooltip_iife() -> str:
    src = _code_only(BASE.read_text(encoding="utf-8"))
    start = src.index("function initGlobalTooltip")
    end = src.index("\n    })();", start)
    return src[start:end]


def test_the_panel_script_binds_the_keyboard_and_touch_paths():
    body = _tooltip_iife()
    for kind in ("focusin", "focusout", "click", "pointerenter", "pointerdown", "keydown"):
        assert f"'{kind}'" in body, f"{kind!r} is no longer bound anywhere in initGlobalTooltip"
    assert re.search(r"addEventListener\('scroll',\s*\w+,\s*\{[^}]*capture:\s*true", body), (
        "the document-level scroll listener is no longer registered on the capture phase")
    assert "mousemove" not in body, (
        "mousemove is back in the tooltip script — a panel that follows the "
        "cursor cannot be produced by a keyboard or a finger, and carrying "
        "two positioning models is how the two input paths drift (§5.5)")


# ── T-7 — exactly one tooltip panel, zero rivals ──────────────────────────

def _panel_id_counts() -> tuple[int, int]:
    ps = rivals = 0
    for p in TEMPLATES.rglob("*.html"):
        text = _code_only(p.read_text(encoding="utf-8"))
        ps += len(re.findall(r'id="ps-tooltip"', text))
        rivals += len(re.findall(r'id="(?:block-tooltip|topo-tooltip)"', text))
    return ps, rivals


def test_exactly_one_tooltip_panel_exists():
    ps, rivals = _panel_id_counts()
    assert ps == 1, f'id="ps-tooltip" appears {ps} time(s), expected exactly 1'
    assert rivals == 0, f"{rivals} rival tooltip panel id(s) still present"


# ── T-8 — the tooltip outranks every other layer ──────────────────────────
#
# A DIFFERENT assertion from test_design_tokens.py's Z_LITERAL_BASELINE
# ratchet (which counts raw z-index literals per file and drives the count
# down). This one proves ORDERING: whatever the tooltip's value is, nothing
# else in the tree may sit at or above it. That is the actual property that
# stops the #ps-tooltip/#prism-modal 9999 tie (DESIGN_SYSTEM_SPEC.md §5.1)
# from being re-created by some other pair of literals landing on the same
# number in the future — a per-file literal COUNT never rising says nothing
# about whether two of them still collide.
_Z_VALUE = re.compile(
    r"z-index\s*:\s*(?P<v1>-?\d+)"
    r"|(?<![\w-])-?z-\[(?P<v2>-?\d+)\]"
    r"|(?<![\w-])-?z-(?P<v3>\d+)(?![\w-])"
)


def _all_z_values() -> list[tuple[str, int]]:
    paths = sorted(TEMPLATES.rglob("*.html"))
    paths.append(APP_CSS)
    out = []
    for p in paths:
        text = _code_only(p.read_text(encoding="utf-8"))
        for m in _Z_VALUE.finditer(text):
            out.append((p.name, int(m.group("v1") or m.group("v2") or m.group("v3"))))
    return out


def test_the_tooltip_outranks_every_other_layer():
    values = _all_z_values()
    tooltip_value = dt.Z_LAYERS["tooltip"]
    assert any(v == tooltip_value for _, v in values), (
        f"no z-index literal of {tooltip_value} (dt.Z_LAYERS['tooltip']) found "
        "in the tree — has #ps-tooltip's declaration moved, or the scale's "
        "value changed without this test noticing?")
    others = [v for _, v in values if v != tooltip_value]
    assert others, "the z-index scan found nothing else to compare against — it has stopped matching"
    worst = max(others)
    assert worst < tooltip_value, (
        f"something in the tree sits at z-index {worst}, which the tooltip's "
        f"{tooltip_value} does not beat — the exact kind of DOM-order tie "
        "this scale exists to end")


# ── T-9 — the macro is the only way a tip is authored ─────────────────────
#
# "hand-written `data-tip-title=` outside partials/_tip.html" (DESIGN_SYSTEM
# _SPEC.md §9), read literally: the attribute-equals textual form, wherever
# it occurs (a Jinja template attribute, or the same text assembled inside a
# JS template literal or string concatenation — server_detail.html does
# both). Deliberately NOT extended to `el.setAttribute('data-tip-title', …)`
# calls: a regex broad enough to catch those also matches
# `window.prismSetDisabled`'s own generic implementation in base.html
# (`el.setAttribute('data-tip-title', title)`, where `title` is a parameter
# supplied by every DIFFERENT caller, not a carrier in its own right) as a
# false positive, and there is no reliable way to tell "a specific literal
# reason" from "a parameter forwarded through" with a regex alone. The two
# real setAttribute-built carriers this misses (server_detail.html's
# detection-mode chip and status-dot fusion reason) are named in the module
# docstring so step 7 does not get a free pass on them.
#
# Measured against this tree before step 7: 25, not the expected 38 —
# base.html specifically measured 0 where 10 were expected. See the module
# docstring for the full account of the 38 -> 25 gap.
#
# Step 7 retrofitted the estate onto the macros (tip()/tip_button()+
# tip_mirror()/tip_overflow()). What moved the COUNT, file by file, and why
# two files below are NOT at 0 despite being fully converted in SHAPE:
#
#   * settings.html 4 -> 0, partials/server_card.html 4 -> 0. Every carrier
#     in each is now a real tip()/tip_overflow() call — settings.html's four
#     bare <i data-lucide="info"> icons became tip() triggers; server_card
#     .html's three severity dots and the "picked" reason span became
#     tip_overflow() of the estate-derived reason, rendered as VISIBLE text
#     per §5.3 (the dot shape is gone; a colour-only dot cannot also be
#     "text that is already visible"). The attribute text now lives inside
#     partials/_tip.html, which this scan excludes by name — not in these
#     files' own source, so the count genuinely reaches 0.
#   * server_detail.html 10 -> 11: a net INCREASE, from a file step 7 fully
#     converted. The detection-mode chip used to be built entirely by
#     `chip.setAttribute('data-tip-title', ...)` at runtime — one of the two
#     setAttribute-built carriers this detector's regex structurally cannot
#     see (still true; see the header comment above). Step 7 moved it
#     server-side into Jinja instead, since the settings.get(...) values it
#     needs were already being read there for an unrelated JS constant —
#     trading "invisible to this ratchet" for "a real, static, accessible
#     carrier" is the right trade, but it is mechanically why this ONE
#     file's count rises even though nothing regressed. It is hand-written
#     (not a macro call) for the same reason as the next bullet.
#   * operations.html, partials/server_comparison.html, partials/settings/
#     _server_config.html, partials/vitals_quadrant.html, and 9 of
#     server_detail.html's 10 (all but the detection-mode chip above):
#     UNCHANGED counts, despite every one being genuinely retrofitted. Each
#     of these carriers is a badge, a threshold chip, a disabled "waiting on
#     you" button, or a card <a> that ALREADY shows its own visible label —
#     none of the four macros can annotate an existing labelled element
#     without gluing on a redundant second bare-icon trigger beside it
#     (tip()/tip_button() always mint their OWN new <button>). So the fix
#     there is `tabindex`/`aria-describedby` added directly to the existing
#     element, paired with a tip_mirror() sibling for the sr-only half —
#     T-1/T-2/T-4 are satisfied (focusable, mirrored), but the element's own
#     `data-tip-title=` stays hand-written, and this ratchet counts THAT
#     attribute's text, not whether the element carrying it is accessible.
#     Deliberately not zero, and not forced to be.
#
# Net: 25 -> 18. Four files fully eliminated (0 each); one gained a carrier
# it never had a hand-written version of; the rest hold steady because their
# shape genuinely does not fit any of the four macros without changing what
# the page looks like more than this step's brief asked for.
# base.html, settings.html and partials/server_card.html reached 0 (see
# the comment above for base.html's own account) and their entries are
# deleted here as part of WP-6 step 27's close-out sweep -- .get(f, 0)
# already treats a missing key identically to an explicit 0 for every
# test below, so this changes no test's behaviour, only removes
# now-redundant bookkeeping for files with nothing left to track.
#
# 18 -> 106, WP-6 D9 close-out (2026-09-18, the step found missing during
# step 27's own review of the internal spec doc -- see the step 27/28
# commit for the full account). D9 said every native `title="..."`
# attribute would be converted to this tooltip system; step 26 was
# supposed to finish that job and didn't. 92 real sites across 18 files
# (a corrected count -- the spec's own recount command silently excluded
# any line that also happened to contain the substring "aria-label", which
# is not a valid exclusion criterion and was hiding 5 genuine base.html
# sites) got the same treatment as step 7's original carriers: the
# existing element becomes the carrier directly wherever a macro would
# have glued on a redundant second trigger (icon-only buttons, draggable
# palette blocks, table cells, badges), tip()/tip_overflow() macro calls
# where a fresh bare trigger or a plain overflow-echo genuinely fit.
# base.html and settings.html un-reach their step 27 zero for exactly this
# reason -- both gain real, reviewed hand-written carriers, not drift.
# partials/server_card.html's two sites both went through tip_overflow(),
# so it stays at 0. Re-measured by running _tip_carrier_counts() itself
# after all conversions landed, not computed by adding up per-worker
# estimates.
TIP_CARRIER_BASELINE: dict[str, int] = {
    "base.html": 5,
    "compliance_doc.html": 1,
    "dashboard.html": 1,
    "operations.html": 3,
    "partials/_runbooks_manage.html": 2,
    "partials/_runbooks_run.html": 1,
    "partials/active_actions.html": 2,
    "partials/activity_feed.html": 1,
    "partials/server_comparison.html": 6,
    "partials/settings/_dependencies.html": 4,
    "partials/settings/_health_checks.html": 3,
    "partials/settings/_server_config.html": 5,
    "partials/vitals_quadrant.html": 2,
    "server_detail.html": 22,
    "servers.html": 3,
    "settings.html": 9,
    # 36 -> 13. The first D9 pass gave all 23 palette blocks a hand-written
    # carrier alongside their native title=; testing live showed
    # applyBlockTip() (this file's own JS, bound on DOMContentLoaded)
    # unconditionally overwrites title/desc/aria-describedby/mirror on
    # every one of them from BLOCK_DEFS[type].tip, and removes the native
    # title= itself once it runs. The hand-written half was dead on
    # arrival -- reverted (see NATIVE_TITLE_BASELINE below for where those
    # 23 sites live now). The remaining 13 are the canvas controls,
    # category chip pair, insert-variable and browse buttons -- real,
    # live hand-written carriers with no equivalent JS already doing the
    # same job.
    "workflows.html": 13,
}

_TIP_TITLE_ATTR = re.compile(r"\bdata-tip-title\s*=")


def _tip_carrier_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        if rel == TIP_PARTIAL:
            continue  # the macro's own definition, not a carrier
        n = len(_TIP_TITLE_ATTR.findall(_code_only(p.read_text(encoding="utf-8"))))
        if n:
            out[rel] = n
    return out


def test_the_macro_is_the_only_way_a_tip_is_authored():
    """T-9. Baseline 38 -> 0 per the spec; measured baseline here went
    25 -> 18 after step 7's retrofit, not to 0 (see the comment on
    TIP_CARRIER_BASELINE for exactly which files reached 0, which didn't,
    and why one file's count rose instead of falling)."""
    counts = _tip_carrier_counts()
    grew = [f"{f}: {n} (baseline {TIP_CARRIER_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > TIP_CARRIER_BASELINE.get(f, 0)]
    assert not grew, (
        "hand-written data-tip-title= appeared outside partials/_tip.html — "
        "use tip()/tip_button()/tip_overflow() instead:\n  " + "\n  ".join(grew))


def test_the_tip_carrier_baseline_is_not_left_behind_when_carriers_are_removed():
    counts = _tip_carrier_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in TIP_CARRIER_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now carry FEWER hand-written tips than the baseline; "
        "lower it (step 7's own job for all of them):\n  "
        + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_tip_carrier_outside_the_templates_that_already_have_one():
    new = sorted(set(_tip_carrier_counts()) - set(TIP_CARRIER_BASELINE))
    assert not new, f"new template(s) with hand-written tip carriers: {new}"


TIP_CARRIER_TOTAL = 83  # WP-6 D9 close-out -- see TIP_CARRIER_BASELINE's own comment


def test_the_total_number_of_tip_carriers_never_rises():
    counts = _tip_carrier_counts()
    total = sum(counts.values())
    assert total <= TIP_CARRIER_TOTAL, (
        f"total hand-written tip carriers rose to {total} (was {TIP_CARRIER_TOTAL})")
    assert total == TIP_CARRIER_TOTAL, (
        f"total fell to {total}; lower TIP_CARRIER_TOTAL to match, or the "
        "headroom step 7 just won is silently available to spend again")


# ── NATIVE_TITLE — every native title="..." became a real tooltip ─────────
#
# D9 (an early WP-6 decision) said every native `title="..."` attribute in
# the templates would be converted to this file's tooltip system, starting
# in step 7. Step 7 only converted the pre-existing hand-written data-tip-*
# carriers; the native-title sweep itself was never actually done, and no
# ratchet ever existed to catch that it hadn't been. Found during step 27's
# own close-out review of the internal spec doc (its own correction note,
# embedded in step 26's section, said to finish this "in this same commit"
# and add exactly this ratchet -- neither happened). Fixed as the D9
# close-out: 92 real sites across 18 files (re-measured live -- the spec's
# own recount command excluded any line containing the substring
# "aria-label" as if that were a valid exclusion, which is not, and was
# silently hiding 5 genuine sites in base.html), converted the same way
# T-9's own hand-written carriers are: the existing element becomes the
# carrier wherever a macro would glue on a redundant second trigger,
# tip()/tip_overflow() where a fresh bare trigger or a plain overflow-echo
# fits. Seeded at 0 by running the detector below against the tree
# immediately after every site was converted -- not assumed from the
# conversion count, which is why this is a separate ratchet from T-9's
# rather than folded into it (a site can leave the native-title count at
# zero via tip_overflow(), contributing nothing to TIP_CARRIER_BASELINE,
# so the two totals are not required to move together).
#
# 0 -> 23, immediately after, once workflows.html's own JS proved the
# first pass wrong for its 23 `.drag-block` palette sites. Those are not
# leftover debt: applyBlockTip() (workflows.html's own function, bound on
# DOMContentLoaded for every `.drag-block`) reads a per-block-type
# BLOCK_DEFS[type].tip.desc/.example -- richer than anything a bare title
# ever held -- builds the real tabindex/aria-describedby/data-tip-title/
# data-tip-desc/mirror itself, and removes the native title= as its own
# last step. That function's own comment says what the attribute is for:
# "only ever a no-JS/loading-race fallback" -- content for the gap before
# that script runs, not a carrier this ratchet should ever drive to zero.
# Converting it by hand produced a second, static carrier that JS
# silently overwrote on every load, satisfying this ratchet's letter while
# doing nothing for anyone -- caught by testing the rendered result live,
# not by reading the source. The other 69 sites (92 total minus these 23)
# have no such script and stay at the general rule: real conversion,
# zero baseline.
NATIVE_TITLE_BASELINE: dict[str, int] = {
    "workflows.html": 23,
}
NATIVE_TITLE_TOTAL = 23

_NATIVE_TITLE_ATTR = re.compile(
    r"(?<!data-tip-)(?<!data-sheet-)(?<!page_)(?<!\.)\btitle=[\"']")


def _native_title_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        n = len(_NATIVE_TITLE_ATTR.findall(text))
        if n:
            out[rel] = n
    return out


def test_no_native_title_appears_outside_the_baseline():
    """D9. Baseline/total 23, all in workflows.html's own JS-driven block
    palette -- every OTHER native title="..." in the tree was converted
    during the WP-6 close-out (see the comment above NATIVE_TITLE_BASELINE
    for the full history, including why these 23 are a permanent
    exception rather than debt). A JS bare variable or `.title`
    DOM-property assignment does not count -- the detector requires
    `title=` with no space and no leading `.`, which a real HTML
    attribute in this codebase always satisfies and neither of those two
    JS shapes ever does (checked against both live in server_detail.html
    and settings.html before trusting the regex)."""
    counts = _native_title_counts()
    grew = [f"{f}: {n} (baseline {NATIVE_TITLE_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > NATIVE_TITLE_BASELINE.get(f, 0)]
    assert not grew, (
        "native title=\"...\" reappeared -- convert it to a data-tip-* "
        "carrier via tip()/tip_button()/tip_overflow(), or hand-write one "
        "directly onto the existing element per T-9's own precedent:\n  "
        + "\n  ".join(grew))


def test_the_native_title_baseline_is_not_left_behind():
    counts = _native_title_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in NATIVE_TITLE_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now carry FEWER native title= sites than the "
        "baseline; lower it:\n  "
        + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_the_native_title_total_matches_the_baseline_sum():
    total = sum(_native_title_counts().values())
    assert total == NATIVE_TITLE_TOTAL, (
        f"NATIVE_TITLE_TOTAL says {NATIVE_TITLE_TOTAL}, tree has {total} -- "
        "update the constant to match a real re-measurement")


# ── T-10 — every tip() key exists in English ──────────────────────────────

_TIP_CALL = re.compile(r"\b(?:tip|tip_button|tip_mirror)\(\s*(['\"])(?P<key>[\w.]+)\1")
_TIP_KWARG_KEY = re.compile(r"(?:title_key|label_key)\s*=\s*(['\"])(?P<key>[\w.]+)\1")


def _tip_macro_keys(text: str) -> set[str]:
    keys = {m.group("key") for m in _TIP_CALL.finditer(text)}
    keys |= {m.group("key") for m in _TIP_KWARG_KEY.finditer(text)}
    return keys


def test_every_tip_key_exists_in_english():
    """Nothing calls tip()/tip_button()/tip_mirror() from a real page yet
    (step 7), so this passes vacuously today on an empty key set — see the
    positive control below for proof the extractor itself works. With the
    existing test_all_real_locales_cover_every_english_key
    (tests/test_i18n_fallback.py), this is what proves every tooltip string
    step 7 introduces is translated in all five locales, not just English."""
    import i18n
    en = i18n.TRANSLATIONS["en"]
    missing = []
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        text = _code_only(p.read_text(encoding="utf-8"))
        for key in sorted(_tip_macro_keys(text)):
            if key not in en:
                missing.append(f"{rel}: {key!r}")
    assert not missing, (
        "tip()/tip_button()/tip_mirror() call(s) reference a key missing from "
        "i18n.TRANSLATIONS['en']:\n  " + "\n  ".join(missing))


def test_the_tip_key_scan_finds_a_call_when_one_exists():
    """Positive control: a vacuous pass (zero calls today) is indistinguishable
    from a broken extractor without this."""
    sample = "{{ tip('made_up_test_key_xyz', 'fallback text', title_key='another_key') }}"
    assert _tip_macro_keys(sample) == {"made_up_test_key_xyz", "another_key"}, (
        "the tip()-call key extractor no longer finds a real call")


# ── T-11 — the panel stays on the pinned scales ───────────────────────────

def _tooltip_css_block() -> str:
    src = BASE.read_text(encoding="utf-8")
    start = src.index("#ps-tooltip {")
    end = src.index("[data-tip-title] {", start)
    return src[start:end]


def test_the_panel_stays_on_the_pinned_scales():
    """T-11. `#ps-tooltip` stays on the pinned radius/motion scales and
    introduces no `!important` that could escape the global
    reduced-motion block. This test only reads CSS source -- it cannot
    see a rendered pixel in either theme.

    RENDERED CONTRAST (step 27, WP-6 close-out; first measured step 10
    (h), 2026-09-18, on settings.html's poll_interval tip): reading
    `getComputedStyle` off the actually-rendered `#ps-tooltip` in both
    themes and computing WCAG contrast against its own background, not a
    token value --
      light: bg rgb(255,255,255); title rgb(2,6,23) -> 20.17:1; desc
        rgb(71,85,105) -> 7.58:1.
      dark: bg rgb(15,22,35); title rgb(241,245,249) -> 16.53:1; desc
        rgb(163,178,199) -> 8.41:1.
    All four clear AA's 4.5:1 with large margins (the tightest, light
    desc at 7.58:1, still clears AAA's 7:1). desc's fg/ratio pair is
    identical to H-2's rendered H3 number (test_design_headings.py) --
    both are the `muted` token on a card-coloured surface, the tooltip
    panel's background and a card's background being the same rendered
    colour in both themes. See this module's own docstring (RENDERED
    CONTRAST bullet) for the full step-10 record this reuses verbatim."""
    block = _tooltip_css_block()
    assert "border-radius: 0.5rem" in block, (
        "#ps-tooltip's border-radius left the two-value scale "
        "(0.5rem sm/DEFAULT, 1rem md/lg — tests/test_design_radii.py)")
    assert re.search(r"transition:\s*[^;]*var\(--dur-", block), (
        "the panel's transition no longer reads a duration token")
    assert re.search(r"transition:\s*[^;]*var\(--ease-", block), (
        "the panel's transition no longer reads an easing token")
    assert "animation" not in block, (
        "the panel declares an animation; §5.5 pins it to a plain transition")
    assert "!important" not in block, (
        "an !important here could out-rank app.css's global reduced-motion "
        "block depending on cascade/source order, defeating "
        "prefers-reduced-motion for the one component deliberately left off "
        "the exemption list (§5.5: 'a tooltip that does not animate is "
        "complete, not degraded')")


# ── T-12 — the delays compose from the motion tokens ──────────────────────

def test_the_delays_compose_from_the_motion_tokens():
    css = APP_CSS.read_text(encoding="utf-8")
    assert re.search(r"--tip-delay-open:\s*var\(--dur-slow\)", css), (
        "--tip-delay-open is no longer var(--dur-slow) — a raw millisecond "
        "literal here would be a second, invisible timing scale")
    assert re.search(r"--tip-delay-close:\s*var\(--dur-fast\)", css), (
        "--tip-delay-close is no longer var(--dur-fast)")


# ── T-13 — the panel is reachable by a pointer when visible (C15) ─────────

def test_the_panel_is_reachable_by_a_pointer_when_visible():
    css_src = BASE.read_text(encoding="utf-8")
    assert re.search(r"#ps-tooltip\.visible\s*\{[^}]*pointer-events:\s*auto", css_src), (
        "#ps-tooltip.visible no longer sets pointer-events: auto — a "
        "magnifier user could not move onto the panel to read it "
        "(WCAG 1.4.13 hoverable)")
    js = _code_only(css_src)
    assert re.search(r"current\.contains\(t\)\s*\|\|\s*tip\.contains\(t\)", js), (
        "the outside-close listener no longer tests containment against "
        "both the trigger and #ps-tooltip")


# ── T-14 — the description-line ratchet (§9; not driven to 0 here) ───────
#
# "element whose class carries a small size (text-xs/text-sm/text-[10px]/
# text-[11px]) AND text-muted/text-faint, whose body is a single Jinja
# expression or >=5 words, not in an empty-state region" — the spec itself
# says several detector variants are legitimate ("225 broad / 50-63 narrow
# ... depending on which detector variant you build") and that the target
# is NOT zero at this step. This is the "narrow" end of that range:
#
#   * Tag whitelist: p, span, div, small, li, dd, td — the shapes actually
#     used for a description line in this tree. A `<label>`-wrapped one
#     would be missed; none were found by hand-checking a sample.
#   * Non-greedy body matching stops at the FIRST same-named close tag, so a
#     description `<div>` that nests another `<div>` undercounts — the same
#     trade-off _carrier_elements makes above, for the same reason (a
#     regex-based scan of arbitrarily-nested HTML always has this edge).
#   * "Not in an empty-state region": partials/_empty_state.html is
#     excluded by filename, and a `data-empty-state` attribute is excluded
#     wherever present. `data-empty-state` does not exist anywhere in this
#     tree today (checked), so that half of the exclusion is currently a
#     no-op — kept for forward compatibility with H-13's ratchet in
#     tests/test_design_headings.py, which uses the same marker.
#
# This is content-migration bookkeeping for wave B (steps 25-26), not a gate
# this step closes — the ratchet only has to stop the count getting WORSE.
#
# RAISED 2026-09-17 by WP-6 step 16 (Batch C -- Reports, Monitoring,
# Operations) -- by +4, RE-MEASURED, not hand-typed, and prominently
# flagged (a ratchet moving the wrong direction is unusual and deserves
# scrutiny, not a quiet edit). This is the DOCUMENTED trade-off two
# comments above names by name: "non-greedy body matching stops at the
# FIRST same-named close tag, so a description <div> that nests another
# <div> undercounts". Before this step, each of these four description
# lines sat as a `<p>`/`<span>` INSIDE a hand-written `<div class="bg-card
# ...">` (and, for three of the four, a second wrapping header-row
# `<div>`) — neither of which is itself description-shaped, but the outer
# div's own (non-greedy, same-tag) match swallowed everything up to its
# own first nested `</div>`, hiding the real description line from ever
# getting its own match. Converting the card SHELL to
# {% call card(...) %} (this step's whole point) removes that literal
# wrapping `<div>` text from the template's own source -- the exact same
# "moved behind the macro" mechanism this file's own HEADING_BASELINE-style
# comments document elsewhere in this house, except here it makes a
# PRE-EXISTING match newly VISIBLE instead of making one disappear. Not one
# of these four lines changed its own tag, class or text by a single
# character; two are the ones this step's own brief explicitly says must
# NOT move yet (monitoring.html's "Noise Digest" paragraph, gated behind
# steps 10/24) and the other two ("Live" / "Checked: <timestamp>" status
# spans) are content-migration's own STATUS half, not its explanatory
# half, so none of the four is a candidate to fix by touching content here
# even if this step's scope allowed it. Confirmed by diffing this
# detector's own output against `git show HEAD:<file>` for all five
# touched templates before writing this comment -- reports.html's own
# count (16) did not move at all.
#   monitoring.html   (new key, was absent = 0): "Noise Digest — Alert
#     Scoring" (line ~27) -- untouched, left exactly in place per this
#     step's own explicit instruction.
#   operations.html (5 -> 6): the Data Management section's own
#     `{{ t.data_management_desc }}` div (the three Danger Zone boxes'
#     own desc paragraphs were ALREADY counted before this step -- they
#     sat one nesting level shallower, past the first swallowed `</div>`).
#   partials/active_actions.html (1 -> 2): the "Live" status span, now
#     `controls=`.
#   partials/updates_overview.html (2 -> 3): the "Checked: <timestamp>"
#     status span, now `controls=`.
# Raised 2026-09-17 by WP-6 step 17 -- same "unshadowing" effect as step 16
# above, three more sites. Confirmed by diffing this detector's own output
# against `git show HEAD:<file>` before writing this comment, exactly as
# step 16's own comment did:
#   partials/server_analytics.html (7 -> 8): the no-anomalies box's own
#     icon+message+hint <div> (bg-card rounded-lg p-6...) sat one nesting
#     level shallower, past the first swallowed `</div>`, now that the
#     outer shell is card(extra='mb-4') and not a literal <div>.
#   partials/server_comparison.html (1 -> 2): the card's own subtitle
#     paragraph ({{ t.comparison_desc }}), previously swallowed inside the
#     outer bg-card div's own non-greedy match, now the first real tag
#     card(heading=...)'s caller body starts with.
#   server_detail.html (13 -> 14): Config Changes' "Loading..." <div>,
#     previously swallowed the same way inside #config-changes-container's
#     own bg-card shell, now visible the instant that shell became
#     card(flush=true, ...).
# None of these three lines changed its own tag, class or text by a single
# character -- all three are pre-existing content the outer shell's own
# removal revealed to this regex, not new description-line prose.
# Changed 2026-09-17 by WP-6 step 18 (Batch E) -- RE-RUN, not hand-computed.
# dashboard.html (1 -> 0, deleted): the onboarding empty state's own
# `{{ t.get('no_servers_yet', ...) }}` paragraph is GONE, not merely moved --
# empty_state(card=true) replaces the whole hand-rolled block, and the
# macro's own hint line uses `opacity-60`, not a text-muted/text-faint
# token, so it was never going to re-enter this count either. Confirmed by
# diffing this detector's own output against `git show HEAD:dashboard.html`,
# the same convention steps 16/17's comments above already establish.
# compliance.html (3 -> 2): the OPPOSITE of steps 16/17's "unshadowing"
# effect above -- this is a SHADOWING one. The findings-card doorway
# conversion wraps its pre-existing uppercase label div (`text-xs ...
# text-muted ... <span>{{ t.get('findings_register', ...) }}</span>`) one
# nesting level deeper, inside a new flex-child <div class="min-w-0
# flex-1"> (mirroring dashboard.html's own doorway tiles). The label's own
# tag, class and text are byte-for-byte unchanged; it is now swallowed past
# the first intervening `</div>` the identical way steps 16/17's own three
# "unshadowed" sites were previously swallowed the other direction. Also
# confirmed by diffing against `git show HEAD:compliance.html`.
# Lowered by WP-6 step 25 (D3 wave A -- the Settings family's own gate-5
# lines converted to tip()), four files, each re-measured by running
# _desc_line_counts() before and after, not hand-computed:
#   settings.html               19 -> 7   (12 converted/merged into tip())
#   partials/settings/_detection.html   11 -> 7   (4 converted)
#   partials/settings/_compliance.html   2 -> 1   (1 converted)
#   partials/settings/_restarts.html     2 -> 0   (2 converted)
# The other four Settings-family files this step also reviewed --
# _tls.html, _rbac.html, _server_config.html, _health_checks.html -- kept
# their exact counts: every candidate line in them resolved to gate 1-4,
# not gate 5, so nothing there was a tooltip candidate at all. Every line
# that stayed inline anywhere in the family, in every one of the eight
# files, is recorded with its gate number in DESC_LINE_INLINE_EXEMPTIONS
# immediately below -- per §5.7, "the exemption table records the gate
# number for every line that stayed, so a reviewer sees the reasoning
# rather than a bare number."
#
# Lowered by WP-6 step 26 (D3 wave B -- the rest of the tree), seven
# files, each re-measured by running _desc_line_counts() before and
# after, not hand-computed:
#   reports.html                       16 -> 14  (fleet_report_desc,
#                                                  csv_metrics_desc,
#                                                  csv_events_desc converted)
#   operations.html                     6 -> 1   (data_management_desc,
#                                                  clean_data_desc,
#                                                  delete_all_desc,
#                                                  factory_reset_desc,
#                                                  dry_run_help converted)
#   partials/server_analytics.html      8 -> 7   (cpu_stationary_hint /
#                                                  ram_stationary_hint
#                                                  merged into one tip())
#   network.html                        2 -> 1   (network_today_desc
#                                                  converted; network_lede
#                                                  stays, gate 4)
#   scan.html                           2 -> 1   (scan_today_desc
#                                                  converted; scan_lede
#                                                  stays, gate 4)
#   partials/server_comparison.html     2 -> 1   (comparison_desc
#                                                  converted via card()'s
#                                                  own tip_desc_key=)
#   setup.html                          1 -> 0, ENTRY DELETED
#                                       (first_run_setup_desc converted;
#                                        this also clears its one
#                                        HEADING_UNDERTEXT_BASELINE site,
#                                        see that dict's own comment)
# compliance.html, compliance_sop.html, server_detail.html,
# partials/services_table.html, topology.html, dashboard.html, 500.html,
# login.html, servers.html, services.html, workflows.html, and the
# remaining Settings-adjacent partials were all reviewed too and needed no
# edit at all -- every candidate in them resolved to gate 1-4, or (a small
# number of cases, recorded individually in DESC_LINE_INLINE_EXEMPTIONS
# below) had no reachable heading/label to attach a tip() to, or would
# have required a hand-written data-tip-* carrier the T-9 ratchet forbids
# because the content is built entirely in JS with no Jinja-rendered path.
# One line, monitoring.html's noise_digest/alert_scoring under "Alert
# Fatigue", was deliberately NOT reviewed for conversion during step 26
# itself: a step-16 comment in that file blocked it "until steps 10/24
# clear it," and step 10 (DESIGN_SYSTEM_SPEC.md's own browser-measurement
# gate, which its own text says GATES STEPS 25 AND 26) had no recorded
# measurement anywhere in this tree at the time -- flagged then rather
# than silently converted or silently ignored.
#
# Lowered by a follow-up once step 10 landed and cleared that precondition
# (monitoring.html: 1 -> 0, entry deleted). Resolving it surfaced a real
# disagreement the step-16 comment had only flagged, not settled: an
# OLDER spec section (Part I §7.4) named this exact text a muted <p> doing
# a heading's job ("becomes an <h3> ... or is deleted"), while step 16's
# own brief separately read it as a §5.7 gate-5 EXPLANATORY-prose
# candidate (tooltip-worthy) -- two different fixes for the same line.
# The text itself ("Noise Digest — Alert Scoring") is a label pair, not a
# sentence explaining a control or a default, so §7.4's original reading
# is the one applied: subhead() (templates/monitoring.html), a real H3,
# no tip -- the label explains itself, there is no separate sentence of
# explanation to attach one to. Word count for THIS specific line was
# checked against the raw Jinja source the detector actually scans, not
# the rendered text: the two `if ... is defined else '...'` conditionals'
# own keywords and variable names push it over the 5-word threshold even
# though the rendered label is four words, which is why it appears here
# and not as a same-count no-op.
DESC_LINE_BASELINE: dict[str, int] = {
    "500.html": 1,
    "compliance.html": 2,
    "compliance_sop.html": 1,
    "login.html": 2,
    "network.html": 1,
    "operations.html": 1,
    "partials/active_actions.html": 2,
    "partials/activity_feed.html": 1,
    "partials/critical_issues.html": 1,
    "partials/server_analytics.html": 7,
    "partials/server_card.html": 3,
    "partials/server_comparison.html": 1,
    "partials/services_table.html": 1,
    "partials/settings/_compliance.html": 1,
    "partials/settings/_detection.html": 7,
    "partials/settings/_health_checks.html": 1,
    "partials/settings/_maintenance.html": 1,
    "partials/settings/_rbac.html": 2,
    "partials/settings/_server_config.html": 2,
    "partials/settings/_tls.html": 1,
    "partials/tls_overview.html": 1,
    "partials/updates_overview.html": 3,
    "reports.html": 14,
    "scan.html": 1,
    "server_detail.html": 14,
    "servers.html": 4,
    "services.html": 1,
    "settings.html": 7,
    "topology.html": 1,
    "workflows.html": 2,
}


# §5.7's exemption table (step 25). Every explanation-shaped line in the
# Settings family that stayed INLINE rather than becoming a tooltip,
# because a gate ahead of gate 5 fired first. Keyed by "file: i18n key"
# (or a short description where there is no key, e.g. a hardcoded native
# `title=`). This table is reviewer-facing, not machine-checked against
# the gate text itself -- test_every_inline_exemption_names_a_gate below
# only proves every entry gives SOME reason, the same shallow guarantee
# _EXEMPT_CONTAINERS-style tables give elsewhere in this codebase.
DESC_LINE_INLINE_EXEMPTIONS: dict[str, str] = {
    "settings.html: ldap_bind_desc":
        "gate 3 -- UPN input format rule ('user@domain.com'), unreadable "
        "mid-focus if it closed on blur behind a tooltip",
    "settings.html: allowed_users_desc":
        "gate 3 -- one-per-line format rule for the textarea directly below it",
    "settings.html: ldap_picker_hint":
        "gate 2 -- caveat on data about to be read (results capped at 200)",
    "partials/settings/_detection.html: detection_mode_help":
        "gate 2 -- CRITICAL threshold alerts always fire regardless of the "
        "chosen mode, a safety-net caveat",
    "partials/settings/_detection.html: thresholds_help":
        "gate 2 -- always-on safety net at the critical level",
    "partials/settings/_detection.html: exhaustion_floor_help":
        "gate 2 -- 'the baseline can never downgrade it'",
    "partials/settings/_detection.html: anomaly_help":
        "gate 2 -- alert coverage that survives detector-priority suppression",
    "partials/settings/_detection.html: spike_gate_help":
        "gates 1+2+3 -- embeds the exhaustion floors' live default values, "
        "a safety-net caveat (disk is never gated), and a format rule "
        "('set to 1 to disable')",
    "partials/settings/_restarts.html: no_server_restarts":
        "gate 1 -- empty-state title reporting live state",
    "partials/settings/_restarts.html: no_server_restarts_hint":
        "gate 4 -- only next-step guidance in an otherwise empty region",
    "partials/settings/_tls.html: no_certificates":
        "gate 1/4 -- empty-state title",
    "partials/settings/_tls.html: no_certificates_hint":
        "gate 4 -- only next-step guidance in an otherwise empty region",
    "partials/settings/_rbac.html: rbac_grant_hint":
        "gate 2 -- consequence of the grant about to be submitted",
    "partials/settings/_compliance.html: compliance_off_means":
        "gate 2 -- caveat about what disabling the module actually does",
    "partials/settings/_compliance.html: compliance_evidence_kept":
        "gate 2 -- a guarantee about evidence, attached to a toggle about "
        "to be flipped",
    "partials/settings/_server_config.html: skip-cert-verify title=":
        "gate 2 -- caveat on the security implication of an action about "
        "to be taken. Hardcoded English in a native title= attribute, not "
        "a t.get() call at all -- a separate, pre-existing §5.3 issue this "
        "step did not create and did not fix, since its content is gate 2 "
        "either way and no markup change was needed",
    "partials/settings/_server_config.html: delete_server_warning":
        "gate 2 -- consequence-of-action warning in the delete-confirm modal",
    "partials/settings/_health_checks.html: hc_verify_tls_hint":
        "gate 2 -- caveat about what the check verdict actually proves, "
        "not what it feels like it proves",

    # -- WP-6 step 26 (D3 wave B) additions below --

    # The four caveats DESIGN_SYSTEM_SPEC.md §5.7 names by key, all four
    # confirmed (by grep, not assumed) to live in reports.html alone.
    "reports.html: csv_latest_500":
        "gate 2 -- always returns the most recent 500 events",
    "reports.html: posture_no_history":
        "gate 2 -- current state only, no date range applies",
    "reports.html: evidence_contains":
        "gate 2 -- caveat on what the evidence export does and does not contain",
    "reports.html: firewall_in_export":
        "gate 2 -- caveat on firewall data's presence in the export",
    "operations.html: no_audit_entries":
        "gate 1 -- reports the actual current state (the audit log is empty)",
    "partials/services_table.html: services_switched_off_note":
        "gate 2 -- excludes switched-off rows from the counts above; this "
        "is also the one HEADING_UNDERTEXT_BASELINE site that legitimately "
        "stays at 1 rather than 0, because gate 2 fires before gate 5",
    "partials/server_comparison.html: select_servers":
        "gate 4 -- only content in the empty comparison area before servers are picked",
    "topology.html: topology_hint":
        "gate 4 -- the only interaction instruction for the SVG canvas "
        "(drag to pan, scroll to zoom, click a node)",
    "network.html: vitals_network_tip_desc":
        "gate 4 -- the absence card's own continuation of the page lede; "
        "reused verbatim as this same card's dashboard-quadrant tooltip "
        "elsewhere, kept visible here on purpose",
    "scan.html: vitals_scan_tip_desc":
        "gate 4 -- same reasoning as network.html's identical site",
    "500.html: error_unexpected":
        "gate 4 -- the only content on a blocked error page with one link back",
    "login.html: sign_in_to_continue":
        "gate 4 -- what to do on a page with no heading and one form",
    "login.html: ad_credentials_hint":
        "gate 3 -- source/format rule for the username and password fields below it",
    "workflows.html: no_workflows":
        "gate 4 -- classic empty-state hint",
    "partials/critical_issues.html: click_details":
        "gate 4 -- a navigational affordance repeated per card, not an "
        "explanation of a control; tooltipping 'click for details' would "
        "be self-defeating",
    "server_detail.html: heatmap/recent-failed-logins scope tags":
        "gate 2 -- '(last 4 weeks)' / '(last 24h)' are limits on the data "
        "range being read, same shape as posture_no_history/csv_latest_500",

    # Page-level ledes with no reachable heading or label in the file that
    # actually owns them (the real <h1> lives in base.html's #page-title
    # chip, outside every step-26 file's own scope) or that read as
    # wayfinding chrome rather than explanation. Gate 5 by content, but
    # tip()/tip_button() both need an anchor point this file does not
    # have -- inventing one (a new heading, an orphan trigger) would
    # exceed "touch only description-line markup." A distinct category
    # from a normal gate-2/3/4 call: the gate isn't what kept these
    # inline, the missing anchor is.
    "reports.html: reports_by_question_desc":
        "gate 4-adjacent, not gate 5 -- 'Every section is a question. Pick "
        "the question, then pick the format' orients the reader to the "
        "PAGE, it does not explain a control's meaning or a default; same "
        "category as network_lede/scan_lede, which §5.7's own worked "
        "examples already class gate 4",
    "compliance.html: compliance_subtitle":
        "gate 4-adjacent, not gate 5 -- 'GAMP 5 CSV operational status -- "
        "SOPs, audit chain, findings' is the identical page-scope-statement "
        "shape as reports.html's lede, same reasoning",
    "compliance.html: csv_docs_at":
        "gate 4-adjacent -- wayfinding chrome ('CSV docs at docs/csv/'), "
        "not explanation of a control",
    "services.html: services_lede":
        "gate 4-adjacent, not gate 5 -- 'Every probe Prism runs... and how "
        "it last answered' orients the reader to what the whole page shows, "
        "the same page-scope-statement shape as reports.html's lede. "
        "(Re-examined after first being filed as 'gate 5, no anchor': "
        "partials/services_table.html, the file that would have to carry "
        "the tip under C21's 'or the relevant card's H2', has no general "
        "overview heading of its own -- only a narrower 'Switched off (N)' "
        "sub-heading, the wrong place to attach a whole-page lede. Once "
        "that turned out to be a dead end, gate-classifying the line "
        "itself properly -- rather than treating the missing anchor as the "
        "thing to work around -- is what settled it: it was never gate 5 "
        "to begin with.)",

    # Content built entirely in JS (string concatenation / innerHTML),
    # with no Jinja-rendered path tip()/tip_button() could reach. Hand-
    # writing a new data-tip-* carrier outside partials/_tip.html is
    # exactly what T-9's TIP_CARRIER_TOTAL ratchets toward zero -- adding
    # one here would be a real, mechanical regression, not a workaround.
    # Gate-5-shaped content stays inline for a MECHANICAL reason, not a
    # gate one; a structural fix (e.g. a Jinja-rendered hidden template
    # clone) is possible but is bigger than this step's own scope.
    "reports.html: events_window_note":
        "gate 2-shaped caveat, JS-built in renderFleetDetail() -- no "
        "compliant carrier mechanism reaches it",
    "reports.html: observed_note":
        "gate 5-shaped explanation, JS-built -- same mechanical limitation",
    "server_detail.html: update_check_retry_note":
        "gate 5-shaped explanation, JS-built in renderUpdates() -- same "
        "mechanical limitation",
    "server_detail.html: restart_pending_desc":
        "gate 5-shaped explanation, JS-built -- same mechanical limitation",

    # Opt-in help surfaces: content already hidden behind a deliberate
    # click (a "Setup Guide" button, a floating builder-help panel), not
    # part of the page's default view. D3 exists to reduce clutter ON THE
    # PAGE; fragmenting a linear how-to guide the operator already chose
    # to open into separate hover-triggered pieces would make it harder
    # to read, not easier, and works against the same goal it would
    # nominally serve. A structural carve-out, the same shape as
    # partials/_empty_state.html's existing filename-based exclusion, but
    # recorded here rather than in the detector since only part of each
    # file qualifies.
    "servers.html: Setup Guide modal (four WinRM how-to lines)":
        "gates 2/3/3/3 by content, but the modal is opt-in help the "
        "operator already chose to open -- not default-view clutter",
    "workflows.html: Workflow Builder Guide panel (event-trigger note)":
        "gate 5 by content, same opt-in-help-surface reasoning",
}


def test_every_inline_exemption_names_a_gate():
    missing = [k for k, v in DESC_LINE_INLINE_EXEMPTIONS.items()
               if "gate" not in v.lower()]
    assert not missing, (
        "exemption(s) with no gate number named -- §5.7 requires the "
        "reviewer see WHICH gate fired, not just that one did:\n  "
        + "\n  ".join(missing))

_DESC_SMALL_SIZE = r"text-xs|text-sm|text-\[1[01]px\]"
_DESC_MUTED_FAINT = r"text-muted|text-faint"
_DESC_TAG_WITH_CLASS = re.compile(
    r'<(?P<tag>p|span|div|small|li|dd|td)\b[^>]*class="(?P<cls>[^"]*)"[^>]*>'
    r'(?P<body>.*?)</(?P=tag)>', re.S)
_DESC_SINGLE_JINJA = re.compile(r"^\s*\{\{[^{}]*\}\}\s*$", re.S)
_DESC_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def _is_desc_line(cls: str, body: str) -> bool:
    if not (re.search(_DESC_SMALL_SIZE, cls) and re.search(_DESC_MUTED_FAINT, cls)):
        return False
    if _DESC_SINGLE_JINJA.match(body.strip()):
        return True
    words = _DESC_WORD.findall(re.sub(r"<[^>]*>", " ", body))
    return len(words) >= 5


def _desc_line_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(TEMPLATES.rglob("*.html")):
        rel = p.relative_to(TEMPLATES).as_posix()
        if rel == "partials/_empty_state.html":
            continue
        text = _code_only(p.read_text(encoding="utf-8"))
        n = 0
        for m in _DESC_TAG_WITH_CLASS.finditer(text):
            if "data-empty-state" in m.group(0):
                continue
            if _is_desc_line(m.group("cls"), m.group("body")):
                n += 1
        if n:
            out[rel] = n
    return out


def test_no_description_line_remains():
    """T-14. Per §9 the target is NOT 0 at this step — only that the count
    never exceeds the measured baseline."""
    counts = _desc_line_counts()
    grew = [f"{f}: {n} (baseline {DESC_LINE_BASELINE.get(f, 0)})"
            for f, n in counts.items() if n > DESC_LINE_BASELINE.get(f, 0)]
    assert not grew, (
        "new description-line(s) appeared — D3 says explanatory prose "
        "becomes a tooltip (§5.7), not a new small-muted line:\n  "
        + "\n  ".join(grew))


def test_the_desc_line_baseline_is_not_left_behind_when_lines_are_removed():
    counts = _desc_line_counts()
    slack = {f: (b, counts.get(f, 0))
             for f, b in DESC_LINE_BASELINE.items() if counts.get(f, 0) < b}
    assert not slack, (
        "these files now hold FEWER description lines than the baseline; "
        "lower it so the headroom cannot be silently respent:\n  "
        + "\n  ".join(f"{f}: baseline {b} -> {n}" for f, (b, n) in slack.items()))


def test_no_description_line_outside_the_templates_that_already_have_one():
    new = sorted(set(_desc_line_counts()) - set(DESC_LINE_BASELINE))
    assert not new, f"new template(s) with a description line: {new}"


# Raised from 114 to 118 by WP-6 step 16 -- see DESC_LINE_BASELINE's own
# comment above for the full account of why (a documented detector
# blind-spot losing its cover, not new content).
# Raised from 118 to 121 by WP-6 step 17 -- same reason, three more sites,
# same comment block.
# Lowered from 121 to 119 by WP-6 step 18 -- RE-RUN, not hand-computed:
# dashboard.html's one site is retired along with the content it described
# (-1); compliance.html loses one to a macro-nesting shadow effect, the
# mirror image of steps 16/17's own unshadowing (-1) -- see
# DESC_LINE_BASELINE's own step-18 comment for both.
# Lowered from 119 to 100 by WP-6 step 25 -- RE-RUN, not hand-computed:
# the four Settings-family reductions in DESC_LINE_BASELINE's own step-25
# comment above sum to -19 (12 + 4 + 1 + 2).
# Lowered from 100 to 88 by WP-6 step 26 -- RE-RUN, not hand-computed: the
# seven reductions in DESC_LINE_BASELINE's own step-26 comment above sum
# to -12 (2 + 5 + 1 + 1 + 1 + 1 + 1).
# Lowered from 88 to 87 by the step-10 follow-up -- RE-RUN, not
# hand-computed: monitoring.html's noise_digest/alert_scoring line,
# converted to subhead() once step 10 cleared the precondition blocking
# it during step 26 itself. See DESC_LINE_BASELINE's own comment above.
DESC_LINE_TOTAL = 87


def test_the_total_number_of_description_lines_never_rises():
    counts = _desc_line_counts()
    total = sum(counts.values())
    assert total <= DESC_LINE_TOTAL, (
        f"total description lines rose to {total} (was {DESC_LINE_TOTAL})")
    assert total == DESC_LINE_TOTAL, (
        f"total fell to {total}; lower DESC_LINE_TOTAL to match, or the "
        "headroom just won is silently available to spend again")


# ── T-15 — a control with a reason is aria-disabled, not disabled ────────

def test_a_control_with_a_reason_is_aria_disabled_not_disabled():
    """Step 9. Extracts prismSetDisabled's body by the same string-boundary
    convention window.__prismSyncTipMirror's own comment describes (up to
    step 8 that boundary was untested directly; this is the first test to
    rely on it), so a future refactor that folds another function inside
    those boundaries breaks this test loudly rather than corrupting a scan
    silently."""
    src = _code_only(BASE.read_text(encoding="utf-8"))
    start = src.index("window.prismSetDisabled = function")
    end = src.index("\n      };", start) + len("\n      };")
    body = src[start:end]

    assert "data-inert" in body, (
        "prismSetDisabled never sets data-inert — a control given a reason "
        "is unreachable by keyboard/touch while disabled")
    assert "aria-disabled" in body, "prismSetDisabled never sets aria-disabled"
    assert not re.search(r"\bel\.disabled\s*=\s*true\b", body), (
        "prismSetDisabled still sets the native disabled attribute when given "
        "a reason — that is what makes the control unfocusable, which T-15 "
        "exists to end")

    m = re.search(r"function run\(el, e, key\)\s*\{(.*?)\n      \}", src, re.S)
    assert m, "the [data-action] dispatcher's run() has been reshaped"
    assert "data-inert" in m.group(1), (
        "the [data-action] dispatcher never checks data-inert — an inert "
        "control's action would still run")
