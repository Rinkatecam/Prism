"""The six-word server-state vocabulary — `severity_vocab`.

WP-1 phase 1 (Language & Levers). The round table ratified a closed
vocabulary of server states and ONE total precedence order, owned by one
module that everything else imports. The reducer (phase 3) consumes the
order; templates render the words; tooltips follow one grammar.

The ratified order, most-urgent-to-display first:

    Unsteady > Down > Impacted > Degraded > Chronic > Unknown > Healthy

Each adjacency was argued (see docs/plans/SEVERITY_MODEL_SPEC.md); these
tests pin the ORDER ITSELF, because the reducer and the UI must agree on it
and a silent reorder would make them disagree without anything failing.

WHAT THESE ARE BLIND TO: whether the words render anywhere yet. Phase 1
ships the language; the carriers arrive with phases 2-3 and get their own
guards then (same staging as the `unmeasured` severity, whose carrier
guards only began to bite once _VITALS_BPM grew the key).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from severity_vocab import (            # noqa: E402
    STATES,
    PRECEDENCE,
    worst,
    word_for_stored_status,
    compose_tooltip,
    REASON_CODES,
    reason_text,
)


# ── the vocabulary is closed and the order is total ───────────────────────

def test_the_vocabulary_is_exactly_the_ratified_seven():
    """Six words plus Unknown. The UX expert's ceiling is binding: the
    seventh word is the day admins stop reading. Adding a state means a
    round-table argument, not an append."""
    assert STATES == ("unsteady", "down", "impacted", "degraded",
                      "chronic", "unknown", "healthy")


def test_the_precedence_order_is_total_and_matches_ratification():
    """One rank per state, no ties, no gaps. Ties are how the reducer and
    the UI end up showing different words for the same server."""
    ranks = [PRECEDENCE[s] for s in STATES]
    assert ranks == sorted(ranks), "PRECEDENCE must list STATES most-urgent first"
    assert len(set(ranks)) == len(ranks), "two states share a rank — order is not total"


@pytest.mark.parametrize("more_urgent,less_urgent", [
    ("unsteady", "down"),      # the latch's whole job is a word that stops changing
    ("down", "impacted"),      # your own confirmed outage beats an inherited suspicion
    ("impacted", "degraded"),  # losing the upstream threatens the whole function
    ("degraded", "chronic"),   # a fresh breach is news; chronic is accepted old news
    ("chronic", "unknown"),    # a known condition still beats silence
    ("unknown", "healthy"),    # never claim health that was not measured
])
def test_each_ratified_adjacency_holds(more_urgent, less_urgent):
    assert PRECEDENCE[more_urgent] < PRECEDENCE[less_urgent]


def test_worst_picks_by_precedence_not_by_argument_order():
    assert worst(["healthy", "down", "degraded"]) == "down"
    assert worst(["degraded", "unsteady"]) == "unsteady"
    assert worst(["healthy"]) == "healthy"


def test_worst_of_nothing_is_unknown():
    """An empty input is an absence of measurements, and the vocabulary
    already has the honest word for that."""
    assert worst([]) == "unknown"


def test_worst_rejects_a_word_outside_the_vocabulary():
    """A typo must fail loudly at the caller, not silently rank last —
    silently ranking last is how 'helathy' would read as worse than down."""
    with pytest.raises(KeyError):
        worst(["healthy", "helathy"])


# ── the stored-status bridge ──────────────────────────────────────────────
#
# Stored enums are history and are NEVER rewritten; words map at render.

@pytest.mark.parametrize("stored,word", [
    ("healthy", "healthy"),
    ("warning", "degraded"),
    ("critical", "degraded"),   # the WORD is the state; severity colour carries warning-vs-critical
    ("offline", "down"),
    ("unknown", "unknown"),
])
def test_every_stored_status_maps_to_a_vocabulary_word(stored, word):
    assert word_for_stored_status(stored) == word


def test_an_unrecognised_stored_status_maps_to_unknown():
    """The collector is one change away from writing a fifth status (the
    _fold_status_summary docstring documents this exact hole). The bridge
    must degrade to the honest word rather than KeyError a page render."""
    assert word_for_stored_status("rebooting") == "unknown"
    assert word_for_stored_status("") == "unknown"
    assert word_for_stored_status(None) == "unknown"


# ── the tooltip grammar ───────────────────────────────────────────────────

def test_the_grammar_is_state_dash_cause_dot_duration():
    """"State — winning cause · duration", ratified. One line, always."""
    line = compose_tooltip("Down", "no response on WinRM", "14 min")
    assert line == "Down — no response on WinRM · 14 min"


def test_the_grammar_omits_a_missing_duration_cleanly():
    assert compose_tooltip("Healthy", "all checks passing", None) == \
        "Healthy — all checks passing"


def test_the_grammar_refuses_a_multiline_cause():
    """Only the rule that set the state may appear, in one line. A cause
    carrying a newline is a wall of text wearing a tooltip's clothes."""
    with pytest.raises(ValueError):
        compose_tooltip("Down", "line one\nline two", "1 min")


# ── reason codes: the reducer's future contract, registered now ───────────

def test_every_reason_code_names_a_state_and_a_template():
    """The reducer (phase 3) returns (state, reason_code); the tooltip is
    reason_text(code, **params). Registering codes with their templates NOW
    means phase 3 cannot invent stringly-typed reasons ad hoc."""
    assert set(REASON_CODES) >= {
        "own_down", "impacted_by", "flapping", "breach",
        "chronic", "no_data", "all_clear", "in_maintenance",
    }
    for code, (state, template) in REASON_CODES.items():
        assert state in STATES or state == "maintenance", (
            f"reason code {code!r} names a state outside the vocabulary")
        assert "{" not in template or "}" in template, f"broken template on {code!r}"


def test_reason_text_fills_its_template():
    assert "DC01" in reason_text("impacted_by", root="DC01", dep_kind="domain auth")


def test_reason_text_rejects_an_unregistered_code():
    with pytest.raises(KeyError):
        reason_text("vibes")


# ── i18n: the words exist in every locale ─────────────────────────────────

def test_every_state_word_exists_in_every_locale():
    """Same rule as vitals_state_*: a word the UI renders must exist in all
    five locales, driven off STATES so a new state fails this immediately."""
    from i18n import TRANSLATIONS
    keys = [f"srv_state_{s}" for s in STATES]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated server-state words: " + ", ".join(missing)
