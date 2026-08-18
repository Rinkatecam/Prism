"""Impact roles — `severity_roles` and its config/model plumbing.

WP-1 phase 1. The round table's decision, in three binding clauses:

  * Roles are WORDS — Critical infrastructure / Important / Background —
    with weights 10/4/1. Tooltips and incident text cite the words, never
    the numbers.
  * Seeded from server TYPE so a fresh install already satisfies the
    required outcomes (DC down ⇒ estate critical) with zero configuration.
  * Precedence is ONE pure function: explicit per-server override >
    type-seeded > Background. `tier` is NOT consulted — it is RBAC and the
    room explicitly rejected coupling authorisation to alerting.

The schema home (folded ex-M29): a `severity_model` settings key and an
optional per-server `criticality` field on the server entry. Servers live
in config.json, not the DB — so there is NO migration, and CI without a
config.json must stay green, which is why defaults are code constants.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from severity_roles import (        # noqa: E402
    ROLES,
    WEIGHTS,
    TYPE_ROLES,
    resolve_role,
    weight_for,
)


# ── the three words and their weights ─────────────────────────────────────

def test_the_roles_are_exactly_three_words():
    assert ROLES == ("critical_infrastructure", "important", "background")


def test_the_ratified_weights():
    assert WEIGHTS == {"critical_infrastructure": 10, "important": 4,
                       "background": 1}


def test_weight_for_resolves_through_settings_overrides():
    """The MSP copies a severity_model block between customers; a site that
    genuinely differs edits the weights there. Absent settings = the code
    constants, so CI without config.json is green."""
    assert weight_for("important", {}) == 4
    assert weight_for("important",
                      {"severity_model": {"weights": {"important": 6}}}) == 6


def test_weight_for_rejects_a_word_outside_the_roles():
    with pytest.raises(KeyError):
        weight_for("mission_critical", {})


# ── type seeding: the zero-config story ───────────────────────────────────

def test_every_known_server_type_is_seeded():
    """Every type in models.DEFAULT_THRESHOLDS has a seeded role, so no
    server ever resolves through surprise. (`_default` is the fallback
    alias, not a type.)"""
    from models import DEFAULT_THRESHOLDS
    types = set(DEFAULT_THRESHOLDS) - {"_default"}
    assert types <= set(TYPE_ROLES), (
        f"unseeded types: {types - set(TYPE_ROLES)}")


def test_the_required_outcomes_fall_out_of_the_seeds():
    """The worked examples, at the seeding layer: a DC anchors the estate,
    a print server cannot escalate it past warning."""
    assert TYPE_ROLES["domain_controller"] == "critical_infrastructure"
    assert TYPE_ROLES["print_server"] == "background"
    assert TYPE_ROLES["database_server"] == "important"
    assert TYPE_ROLES["file_server"] == "important"


# ── precedence: one pure function ─────────────────────────────────────────

def _server(**kw):
    base = {"name": "S1", "type": "file_server", "criticality": ""}
    base.update(kw)
    return base


def test_an_explicit_override_wins_over_the_type_seed():
    role, source = resolve_role(_server(type="print_server",
                                        criticality="critical_infrastructure"), {})
    assert role == "critical_infrastructure"
    assert source == "override"


def test_the_type_seed_wins_when_no_override_is_set():
    role, source = resolve_role(_server(type="domain_controller"), {})
    assert (role, source) == ("critical_infrastructure", "type")


def test_an_unknown_type_defaults_to_background():
    """The live fleet uses types outside DEFAULT_THRESHOLDS (they fall
    through to _default thresholds); the role must fall through just as
    quietly — to Background, the ratified default."""
    role, source = resolve_role(_server(type="management"), {})
    assert (role, source) == ("background", "default")


def test_a_garbage_override_is_ignored_not_honoured():
    """A typo in config must not invent a role. Fall through to the seed
    and report the source honestly — the tooltip says where the role came
    from, and lying there poisons the one trust surface this model has."""
    role, source = resolve_role(_server(type="domain_controller",
                                        criticality="critcal_infra"), {})
    assert (role, source) == ("critical_infrastructure", "type")


def test_settings_can_reseed_a_type_fleet_wide():
    """severity_model.type_roles lets a site say 'our print servers matter'
    once, instead of per-server overrides on twelve of them."""
    settings = {"severity_model": {"type_roles": {"print_server": "important"}}}
    role, source = resolve_role(_server(type="print_server"), settings)
    assert (role, source) == ("important", "type")


def test_tier_is_never_consulted():
    """The room's explicit rejection: RBAC tier must not couple to alerting.
    A tier-0 jump box is not availability-critical, and proving tier changes
    nothing here is the cheapest way to keep the two apart forever."""
    a = resolve_role(_server(type="file_server", tier=0), {})
    b = resolve_role(_server(type="file_server", tier=2), {})
    assert a == b == ("important", "type")


# ── the settings key is visible through the merge ─────────────────────────

def test_severity_model_survives_the_settings_merge():
    """config_manager only exposes top-level keys present in
    _DEFAULT_SETTINGS — an unlisted key on disk is invisible to every
    reader. This is the exact trap the round table's schema clause named."""
    from config_manager import ConfigManager
    assert "severity_model" in ConfigManager._DEFAULT_SETTINGS, (
        "severity_model missing from _DEFAULT_SETTINGS — the merge will "
        "hide it from get_settings() even if a config file carries it")


# ── the per-server field round-trips ──────────────────────────────────────

def test_serverconfig_carries_criticality_and_defaults_empty():
    from models import ServerConfig
    s = ServerConfig(name="S1", host="s1.example.com", username="u",
                     password="p", type="file_server")
    assert s.criticality == ""
    assert "criticality" in s.to_dict()


def test_serverconfig_round_trips_criticality():
    from models import ServerConfig
    s = ServerConfig.from_dict({
        "name": "S1", "host": "s1.example.com", "username": "u",
        "password": "p", "type": "print_server",
        "criticality": "critical_infrastructure",
    })
    assert s.criticality == "critical_infrastructure"
    assert s.to_dict()["criticality"] == "critical_infrastructure"


def test_resolve_role_accepts_a_serverconfig_too():
    """Callers hold ServerConfig objects (collector) or dicts (config API);
    the pure function serves both so nobody writes a second copy."""
    from models import ServerConfig
    s = ServerConfig(name="S1", host="s1.example.com", username="u",
                     password="p", type="domain_controller")
    assert resolve_role(s, {}) == ("critical_infrastructure", "type")


# ── i18n ──────────────────────────────────────────────────────────────────

def test_every_role_word_exists_in_every_locale():
    from i18n import TRANSLATIONS
    keys = [f"role_{r}" for r in ROLES]
    missing = [f"{lang}:{k}" for lang in TRANSLATIONS for k in keys
               if k not in TRANSLATIONS[lang]]
    assert not missing, "untranslated role words: " + ", ".join(missing)


# ── the writer path validates ─────────────────────────────────────────────

def test_the_config_api_rejects_an_unknown_criticality():
    """resolve_role degrades on garbage from a HAND-EDITED config.json, but
    the validated writer must refuse it: the operator typed the word
    expecting an effect, and persisting a no-op is the verify_tls lesson
    (a setting that silently does nothing) wearing a new key."""
    import ast
    import inspect
    import textwrap
    import routes.api.config as c

    src = textwrap.dedent(inspect.getsource(c))
    tree = ast.parse(src)
    # Parse, don't grep: find the validation by shape — a membership test
    # of the criticality value against ROLES — so this cannot fire on a
    # comment and cannot pass on one either.
    found_guard = any(
        isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.NotIn, ast.In)) for op in node.ops)
        and "ROLES" in ast.dump(node)
        and "crit" in ast.dump(node).lower()
        for node in ast.walk(tree)
    )
    assert found_guard, (
        "routes/api/config.py no longer validates criticality against "
        "severity_roles.ROLES — an unknown word would persist and silently "
        "do nothing")
