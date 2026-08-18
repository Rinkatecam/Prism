"""Impact roles: what a server MEANS to the estate, as a word.

WP-1 of the 2026-08 restructure (docs/plans/SEVERITY_MODEL_SPEC.md).
Three words — critical_infrastructure / important / background — with
weights 10/4/1, seeded from the server TYPE so a fresh install already
gets the required outcomes (a DC down reads estate-critical, a print
server down cannot escalate past warning) with zero configuration.

Precedence is ONE pure function, most-specific first:

    per-server `criticality` override  >  type seed  >  background

Two explicit non-decisions, both ratified:

  * `tier` is NEVER consulted. Tier is RBAC — who may act on a server —
    and the room rejected coupling authorisation to alerting: a locked-down
    jump box is tier-0 and still Background for availability.
  * The words are the interface. Tooltips and incidents cite
    "Critical infrastructure", never "10" — the numbers exist so the fold
    can do arithmetic, and they surface only as an audit footnote.

Schema home (the folded ex-M29 clauses): the `severity_model` settings key
(weights + type_roles, both optional overrides of the code constants) and
the per-server `criticality` field on the config.json server entry. Servers
live in config.json, not the database, so there is NO migration and CI
without a config file stays green — defaults are code constants.
"""

from __future__ import annotations

ROLES: tuple[str, ...] = ("critical_infrastructure", "important", "background")

WEIGHTS: dict[str, int] = {
    "critical_infrastructure": 10,
    "important": 4,
    "background": 1,
}

# Type seeds. Every type in models.DEFAULT_THRESHOLDS must appear here
# (tests enforce it); anything OUTSIDE the map — including the custom type
# strings real fleets use — falls through to background, the ratified
# default. Rationale per row is availability, not importance-to-the-admin:
#   domain_controller  auth+DNS for everything; its loss IS an estate event
#   database/mail/file/app/web  line-of-business; degraded estate, not dead
#   print/backup/other  nobody's morning stops when they blip
TYPE_ROLES: dict[str, str] = {
    "domain_controller": "critical_infrastructure",
    "database_server": "important",
    "mail_server": "important",
    "file_server": "important",
    "app_server": "important",
    "web_server": "important",
    "print_server": "background",
    "backup_server": "background",
    "other": "background",
}

_DEFAULT_ROLE = "background"


def _field(server, name: str, default=""):
    """Read a field off a ServerConfig object or a plain dict — the two
    shapes callers actually hold (collector vs config API)."""
    if isinstance(server, dict):
        return server.get(name, default)
    return getattr(server, name, default)


def resolve_role(server, settings: dict | None) -> tuple[str, str]:
    """(role, source) for a server. Pure; no I/O; both call shapes.

    `source` ∈ {"override", "type", "default"} and is part of the contract:
    the tooltip says WHERE the role came from ("Important — from type
    'file server'; override in server settings"), and an honest source is
    what lets an admin trust the calmer estate needle.

    A garbage `criticality` value is IGNORED, not honoured and not raised:
    config.json is hand-editable, and a typo must degrade to the seed while
    the source keeps telling the truth. Raising would take the dashboard
    down over one misspelled word in one server entry.
    """
    settings = settings or {}
    model = settings.get("severity_model") or {}

    override = (_field(server, "criticality") or "").strip()
    if override in ROLES:
        return override, "override"

    server_type = (_field(server, "type") or "").strip()
    type_map = {**TYPE_ROLES, **(model.get("type_roles") or {})}
    seeded = type_map.get(server_type)
    if seeded in ROLES:
        return seeded, "type"

    return _DEFAULT_ROLE, "default"


def weight_for(role: str, settings: dict | None) -> int:
    """The fold's number for a role word.

    Settings may override per-site (`severity_model.weights`) — the MSP
    copies that block between customers. An unknown role raises KeyError:
    weights are only ever looked up for words `resolve_role` produced, so
    an unknown word here is a programming error, not operator input.
    """
    settings = settings or {}
    overrides = (settings.get("severity_model") or {}).get("weights") or {}
    if role not in WEIGHTS:
        raise KeyError(role)
    return int(overrides.get(role, WEIGHTS[role]))
