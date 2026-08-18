"""The server-state vocabulary: six words, one order, one tooltip grammar.

WP-1 of the 2026-08 restructure (docs/plans/SEVERITY_MODEL_SPEC.md). The
round table ratified a CLOSED vocabulary for what a server can BE on
screen, and this module is its single home. The severity reducer (phase 3)
imports the order; templates render the words; every tooltip goes through
the one grammar. Nothing else may define a state word, and the stored
status enums are never rewritten — words map at render.

The order, most-urgent-to-display first, with each adjacency's argument:

    unsteady   the latch's whole job is a word that stops changing; a host
               that stays down sheds the latch within one window anyway
    down       your own confirmed outage beats an inherited suspicion
    impacted   losing your upstream threatens the whole function; a
               threshold breach only threatens headroom
    degraded   a fresh breach is news; chronic is accepted old news
    chronic    a known long-standing condition still beats silence
    unknown    never claim health that was not measured
    healthy    …but Unknown contributes zero pressure, so a fresh install
               boots calm, not alarmed

Warning-vs-critical is NOT a word: both render as "degraded" and the badge
COLOUR carries the grade. The six words answer "what state is this machine
in"; the colour answers "how hard". Collapsing those into more words was
explicitly rejected — the seventh word is the day admins stop reading.
"""

from __future__ import annotations

# The closed set, in precedence order. Adding a word is a round-table
# decision, not an append — see the module docstring's ceiling argument.
STATES: tuple[str, ...] = (
    "unsteady", "down", "impacted", "degraded", "chronic", "unknown", "healthy",
)

# rank 0 = shown first when states compete. Derived, so STATES is the only
# thing to edit and the two can never disagree.
PRECEDENCE: dict[str, int] = {state: rank for rank, state in enumerate(STATES)}


def worst(states) -> str:
    """The state that WINS display among `states`, by the ratified order.

    Empty input returns "unknown" — no measurements is an absence, and the
    vocabulary already has the honest word for it. An unknown WORD raises
    KeyError on purpose: silently ranking a typo last would let 'helathy'
    read as more urgent than down.
    """
    states = list(states)
    if not states:
        return "unknown"
    return min(states, key=lambda s: PRECEDENCE[s])


# ── the stored-status bridge ──────────────────────────────────────────────
#
# metrics.status keeps writing healthy/warning/critical/offline/unknown
# forever — history is never rewritten. This is the ONE mapping from stored
# enum to rendered word. warning and critical both land on "degraded"; the
# badge colour (which still reads the stored status) carries the grade.
_STORED_TO_WORD = {
    "healthy": "healthy",
    "warning": "degraded",
    "critical": "degraded",
    "offline": "down",
    "unknown": "unknown",
}


def word_for_stored_status(stored: str | None) -> str:
    """Vocabulary word for a stored metrics.status value.

    Degrades to "unknown" for anything unrecognised: the collector is one
    change away from writing a fifth status (the `_fold_status_summary`
    docstring documents that exact hole), and a KeyError here would 500 a
    page render over a value the UI merely did not recognise.
    """
    return _STORED_TO_WORD.get(stored or "", "unknown")


# ── the tooltip grammar ───────────────────────────────────────────────────

def compose_tooltip(state_word: str, cause: str, duration: str | None = None) -> str:
    """One line, one grammar: "State — winning cause · duration".

    Ratified clause: only the rule that actually set the state may appear;
    competing causes live in the detail drawer, never the tooltip. A cause
    with a newline is a wall of text wearing a tooltip's clothes, so it is
    refused rather than trimmed — trimming would hide that a caller is
    trying to say too much.
    """
    if "\n" in cause:
        raise ValueError("a tooltip cause is one line; put detail in the drawer")
    line = f"{state_word} — {cause}"
    if duration:
        line += f" · {duration}"
    return line


# ── reason codes: the reducer's contract, registered ahead of it ──────────
#
# Phase 3's reducer returns (state, reason_code); the tooltip is
# reason_text(code, **params). Registering the codes WITH their templates
# now means the reducer cannot invent stringly-typed reasons ad hoc, and
# each branch of the reducer gets exactly one code — which is what makes
# them one-test-per-branch mutation targets.
#
# code -> (state it accompanies, one-line template)
REASON_CODES: dict[str, tuple[str, str]] = {
    "own_down":       ("down",     "no response on {transport}"),
    "impacted_by":    ("impacted", "upstream {root} is down ({dep_kind})"),
    "flapping":       ("unsteady", "{count} status changes in {window}; showing worst recent"),
    "breach":         ("degraded", "{metric} at {value} ({grade} threshold)"),
    "chronic":        ("chronic",  "{metric} unchanged {duration}; weighted down at estate level"),
    "no_data":        ("unknown",  "no measurements yet"),
    "all_clear":      ("healthy",  "all checks passing"),
    # Maintenance is not one of the six render states — it is an exclusion
    # with its own badge — but its reason line goes through the same
    # grammar, so it registers here rather than growing a second registry.
    "in_maintenance": ("maintenance", "maintenance until {until} (auto-expires)"),
}


def reason_text(code: str, **params) -> str:
    """The tooltip line for a reason code. KeyError on an unregistered code,
    on purpose — an ad-hoc reason is a vocabulary escape hatch."""
    _state, template = REASON_CODES[code]
    return template.format(**params)
