"""The tooltip authoring surface — DESIGN_SYSTEM_SPEC.md §5.6, WP-6 step 5.

partials/_tip.html's four macros are not called from any shipped template
yet (step 7 does that retrofit); this file is the only thing exercising
them until then. It renders the macros directly through a bare
jinja2.Environment, the same pattern as tests/test_design_layered.py and
tests/test_incidents_panel.py, rather than through Flask — `with context`
on the import is what makes `t` and `next_tip_id` visible inside them at
all (see _tip.html's own header comment), so the harness below imports
them the same way a real template must, or it would not be testing what
production actually does.

WHAT THIS IS BLIND TO:

  * Anything that only exists once the macros are wired into a real page:
    rendered contrast, the panel actually opening on hover/focus/touch,
    z-index, the Escape/long-press/outside-tap behaviour. That is all
    steps 6 (ratchets) and 10 (browser measurement), against real carriers
    step 7 creates.
  * Whether a future call site passes a real i18n key or a hardcoded
    sentence in the *_key slots — see
    test_passing_prose_instead_of_a_key_fails_silently_not_loudly below for
    exactly why that is a code-review rule, not something a test here can
    enforce.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import jinja2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PROJECT_ROOT / "templates"

_IMPORT = (
    '{% from "partials/_tip.html" import tip, tip_button, tip_mirror, '
    'tip_overflow with context %}'
)


def _counter():
    """A stand-in next_tip_id(): production's real one (app.py's
    _next_tip_id) is exercised separately below, through Flask, because it
    depends on flask.g. Macro-shape tests only need SOME source of
    distinct ids, not that specific counter."""
    n = itertools.count(1)
    return lambda: f"ps-tip-{next(n)}"


def render(source: str, t=None, next_tip_id=None, **extra) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined, autoescape=True)
    tmpl = env.from_string(_IMPORT + source)
    ctx = dict(t=t if t is not None else {}, next_tip_id=next_tip_id or _counter())
    ctx.update(extra)
    return tmpl.render(**ctx)


# ── tip(): the standalone trigger ────────────────────────────────────────

def test_tip_emits_exactly_one_button_and_one_mirror_with_matching_ids():
    html = render("{{ tip('d_key', 'fallback desc') }}")

    buttons = re.findall(r"<button\b[^>]*>", html)
    mirrors = re.findall(r'<span class="sr-only"[^>]*>', html)
    assert len(buttons) == 1, html
    assert len(mirrors) == 1, html

    mirror_id = re.search(r'id="([^"]+)"', mirrors[0]).group(1)
    described_by = re.search(r'aria-describedby="([^"]+)"', buttons[0]).group(1)
    assert mirror_id, "the mirror has no id at all"
    assert mirror_id == described_by, (
        f"button points aria-describedby at {described_by!r} but the "
        f"mirror's id is {mirror_id!r}")


def test_tip_mirror_text_equals_the_desc_character_for_character():
    html = render("{{ tip('d_key', 'The exact description text.') }}")
    mirror_text = re.search(r'<span class="sr-only"[^>]*>([^<]*)</span>', html).group(1)
    assert mirror_text == "The exact description text."
    # And the same string is what the JS panel reads from the carrier —
    # bindAll/show() never look at the mirror at all (see _tip.html's
    # header comment); duplicated on purpose, so it must be byte-identical.
    assert 'data-tip-desc="The exact description text."' in html


def test_tip_reads_desc_from_t_when_present_else_the_fallback():
    """Proves t.get(desc_key, desc_fallback) is really being called — not
    that the macro just always prints its second argument."""
    only_fallback = render("{{ tip('missing_key', 'THE FALLBACK TEXT') }}", t={})
    assert "THE FALLBACK TEXT" in only_fallback

    real_key = render(
        "{{ tip('real_key', 'THE FALLBACK TEXT') }}",
        t={"real_key": "THE REAL TRANSLATION"})
    assert "THE REAL TRANSLATION" in real_key
    assert "THE FALLBACK TEXT" not in real_key


def test_tip_bare_icon_gets_a_short_aria_label_not_the_explanation():
    """§5.3: a trigger with no visible text needs a NAME (aria-label),
    never the explanation itself — the label must not just be the
    (typically much longer) description repeated."""
    desc = ("A considerably longer sentence describing exactly what this "
            "control does and why, the kind of text that belongs in the "
            "panel, not in a name.")
    html = render("{{ tip('d', desc) }}", desc=desc)
    label = re.search(r'aria-label="([^"]+)"', html).group(1)
    assert label != desc
    assert len(label) < len(desc)
    assert label == "Explain"  # the generic fallback, no title/label given


def test_tip_label_prefers_explicit_label_then_title_then_generic():
    generic = render("{{ tip('d', 'desc') }}")
    assert 'aria-label="Explain"' in generic

    with_title = render("{{ tip('d', 'desc', title_fallback='Poll interval') }}")
    assert 'aria-label="About Poll interval"' in with_title

    with_label = render(
        "{{ tip('d', 'desc', title_fallback='Poll interval', "
        "label_fallback='Custom label wins') }}")
    assert 'aria-label="Custom label wins"' in with_label
    assert "About Poll interval" not in with_label


def test_tip_uses_i18n_for_the_about_composition_not_hardcoded_english():
    """The 'About <title>' wording itself has to come from `t`, or a
    template using tip() with only a title (no explicit label) would be
    hardcoding English regardless of what desc_key/title_key resolve to.
    A deliberately non-English, ASCII-only marker string stands in for a
    real locale here so the test cannot pass by accident if the macro
    happened to hardcode the literal word "About" instead of reading
    tip_about_named from `t`."""
    html = render(
        "{{ tip('d', 'desc', title_fallback='Poll interval') }}",
        t={"tip_about_named": "XYZZY {title} PLUGH"})
    assert 'aria-label="XYZZY Poll interval PLUGH"' in html


def test_tip_carries_the_data_tip_attributes_bindall_requires():
    """base.html's bindAll() selects `[data-tip-title]` — presence, not
    truthiness (see _tip.html's header comment) — so the attribute must be
    on the element even when there is no title text to show."""
    no_title = render("{{ tip('d', 'the description') }}")
    assert 'data-tip-title=""' in no_title, (
        "no title was given, but data-tip-title must still be present or "
        "bindAll() never attaches listeners to this carrier at all")
    assert 'data-tip-desc="the description"' in no_title

    with_title = render("{{ tip('d', 'the description', title_fallback='A Title') }}")
    assert 'data-tip-title="A Title"' in with_title


def test_tip_icon_is_info_at_the_specified_size_inside_the_button():
    html = render("{{ tip('d', 'desc') }}")
    assert re.search(
        r'<button\b[^>]*>\s*<i data-lucide="info" class="w-3\.5 h-3\.5"',
        html, re.S), html


def test_tip_button_carries_the_ps_tip_class_and_hit_target():
    html = render("{{ tip('d', 'desc') }}")
    button = re.search(r"<button\b[^>]*>", html).group(0)
    assert "ps-tip" in button.split('class="')[1].split('"')[0].split()
    assert "w-8" in button and "h-8" in button and "-m-2" in button


def test_passing_prose_instead_of_a_key_fails_silently_not_loudly():
    """Documents the constraint the brief calls out explicitly: 'the macro
    takes a key, never prose' (§5.6) is enforced at CODE REVIEW, not at
    runtime. `t.get(desc_key, desc_fallback)` cannot distinguish 'a typo'd
    key that is not in `t` yet' from 'a full English sentence that was
    never meant to be a key at all' — both simply fall through to
    desc_fallback, with no exception, no warning, and (if desc_fallback
    happens to read fine on its own, which it usually does, being English
    prose someone wrote) no visible symptom either.

    The real guard is a human checking the first argument to tip(...) is
    an actual entry in i18n.TRANSLATIONS['en'] — the same thing step 6's
    T-10 (test_every_tip_key_exists_in_english) checks mechanically, but
    only for keys that made it into a real template; it cannot stop one
    from being written wrong in the first place, only catch it once
    written.
    """
    prose_used_as_a_key = ("Please configure the poll interval to a value "
                            "between 10 and 3600 seconds")
    html = render("{{ tip(desc_key, 'fallback text') }}",
                   t={}, desc_key=prose_used_as_a_key)
    # Renders cleanly, no exception raised — which is precisely the
    # failure mode: nothing here or in the wider suite would flag it.
    assert "fallback text" in html
    assert prose_used_as_a_key not in html


# ── tip_button() + tip_mirror(): the heading-adjacent split ──────────────

def test_tip_button_and_tip_mirror_share_the_callers_id():
    html = render(
        "{% set hid = next_tip_id() %}"
        "<h2>Heading text{{ tip_button('d', 'the desc', hid) }}</h2>"
        "{{ tip_mirror('d', 'the desc', hid) }}"
    )
    button_tag = re.search(r"<button\b[^>]*>", html).group(0)
    mirror = re.search(r'<span class="sr-only" id="([^"]+)">([^<]*)</span>', html)
    assert mirror, html
    described_by = re.search(r'aria-describedby="([^"]+)"', button_tag).group(1)
    assert described_by == mirror.group(1) == "ps-tip-1"
    assert mirror.group(2) == "the desc"


def test_tip_button_alone_emits_no_mirror():
    """tip_button() must not ALSO render a sr-only span — only tip()
    (the fused macro) does that. Otherwise the heading-adjacent path would
    duplicate the mirror once from tip_button() and once from the
    caller's own tip_mirror() call."""
    html = render("{{ tip_button('d', 'desc', 'some-id') }}")
    assert "sr-only" not in html
    assert '<button' in html


def test_tip_mirror_never_sits_inside_the_heading():
    """C17, exercised the way a real caller must use the split: tip_button
    inside the <h2>, tip_mirror as a sibling AFTER it."""
    html = render(
        "{% set hid = next_tip_id() %}"
        "<h2>Heading{{ tip_button('d', 'desc', hid) }}</h2>"
        "{{ tip_mirror('d', 'desc', hid) }}"
    )
    heading_close = html.index("</h2>")
    mirror_open = html.index('<span class="sr-only"')
    assert heading_close < mirror_open, (
        "the mirror rendered before </h2> closed — a caller could only "
        "produce that by putting tip_mirror() INSIDE the heading, which "
        "is exactly what C17 forbids")


# ── tip_overflow(): already-visible, already-clipped text ────────────────

def test_tip_overflow_has_no_mirror_and_no_describedby():
    html = render("{{ tip_overflow('Some long clipped status text') }}")
    assert "sr-only" not in html
    assert "aria-describedby" not in html
    assert "aria-label" not in html


def test_tip_overflow_still_wires_up_data_tip_title_present_but_empty():
    """The same bindAll()-presence requirement as tip()'s title-less case,
    proven separately here because tip_overflow's caller can never supply
    a title at all — if this attribute were ever dropped, EVERY
    tip_overflow carrier in the app would silently stop responding to
    hover/focus/touch, all at once."""
    html = render("{{ tip_overflow('Clipped text') }}")
    assert 'data-tip-title=""' in html
    assert 'data-tip-desc="Clipped text"' in html


def test_tip_overflow_shows_the_text_itself_as_visible_content():
    html = render("{{ tip_overflow('Clipped text') }}")
    assert re.search(r">\s*Clipped text\s*<", html)


def test_tip_overflow_is_keyboard_reachable_via_tabindex():
    """A bare <span> is not one of the natively-focusable tag types step
    6's T-1 accepts (button, a[href], summary) — without tabindex="0" a
    keyboard user could never reach text a mouse user can hover."""
    html = render("{{ tip_overflow('Clipped text') }}")
    assert 'tabindex="0"' in html


def test_tip_overflow_carries_its_own_marker_class_not_ps_tip():
    """.ps-tip (exact token) marks a REAL <button> to base.html's JS, which
    then skips long-press setup because a tap on a button already fires
    click natively. tip_overflow's carrier is not a button and DOES need
    the long-press path, so it must carry a distinct token —
    .ps-tip-overflow — not .ps-tip itself. A shared prefix is not the same
    token: classList.contains('ps-tip') on class="ps-tip-overflow" is
    false, and this test pins that rather than assuming it."""
    html = render("{{ tip_overflow('x') }}")
    class_attr = re.search(r'class="([^"]*)"', html).group(1)
    assert "ps-tip-overflow" in class_attr.split()
    assert "ps-tip" not in class_attr.split()


def test_tip_overflow_appends_the_callers_extra_class():
    html = render("{{ tip_overflow('x', extra_class='truncate max-w-xs') }}")
    class_attr = re.search(r'class="([^"]*)"', html).group(1)
    assert class_attr.split() == ["ps-tip-overflow", "truncate", "max-w-xs"]


def test_tip_overflow_with_no_extra_class_has_no_trailing_whitespace():
    html = render("{{ tip_overflow('x') }}")
    assert 'class="ps-tip-overflow"' in html


# ── _next_tip_id() / next_tip_id: the flask.g-backed counter ─────────────
#
# These go through the real Flask app object, not the fake counter above,
# because the property being tested — survives more than one
# render_template()-equivalent call within a single request — is specific
# to flask.g and cannot be observed through a bare jinja2.Environment.
# Per the house rule this session was given: app.py's module-level code no
# longer starts the real collector/scheduler threads under pytest (see
# commit 70864bb), so importing `app` here and using test_request_context()
# is safe and does not touch the 29 production servers.

def test_next_tip_id_is_unique_across_calls_in_one_request():
    import app as prism_app
    with prism_app.app.test_request_context("/"):
        a = prism_app._next_tip_id()
        b = prism_app._next_tip_id()
        c = prism_app._next_tip_id()
    assert len({a, b, c}) == 3, (a, b, c)


def test_next_tip_id_survives_two_separate_inject_locale_calls_in_one_request():
    """The exact scenario named in §5.6: inject_locale is a context
    PROCESSOR, so Flask calls it once per render_template(), not once per
    request. Calling it twice here stands in for one view rendering a
    partial separately from its main template in the same request — if the
    counter were a local closed over by inject_locale instead of living on
    flask.g, both calls would restart at the same first id."""
    import app as prism_app
    with prism_app.app.test_request_context("/"):
        first_render_ctx = prism_app.inject_locale()
        second_render_ctx = prism_app.inject_locale()
        first_id = first_render_ctx["next_tip_id"]()
        second_id = second_render_ctx["next_tip_id"]()
    assert first_id != second_id, (
        "two render_template()-equivalent calls in the same request "
        "produced the same tooltip id")


def test_next_tip_id_is_exposed_to_templates_under_that_exact_name():
    import app as prism_app
    with prism_app.app.test_request_context("/"):
        ctx = prism_app.inject_locale()
    assert "next_tip_id" in ctx
    assert callable(ctx["next_tip_id"])


def test_next_tip_id_starts_clean_on_a_new_request():
    """The other half of flask.g's lifetime: per-request, so it must NOT
    remember the previous request's count."""
    import app as prism_app
    with prism_app.app.test_request_context("/"):
        first_of_request_one = prism_app._next_tip_id()
    with prism_app.app.test_request_context("/"):
        first_of_request_two = prism_app._next_tip_id()
    assert first_of_request_one == first_of_request_two == "ps-tip-1"
