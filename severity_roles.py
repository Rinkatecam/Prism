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

import logging
from datetime import datetime

logger = logging.getLogger("prism.severity")

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

# AMENDED 2026-08-18 by owner-mandated research (three sysadmin personas +
# community evidence): an UNKNOWN type fails UP to "important", not down to
# "background". The solo-admin persona's non-negotiable — "if I learn about
# an outage from a user while Prism sat green because a default classified
# the box as Background, the tool is uninstalled that day" — plus the
# MSP persona's everything-box evidence both argue up. The cost is nothing
# while a box is healthy (weight only matters when it is DOWN, which is
# exactly when fail-up is right); "other" stays Background because choosing
# it IS a classification. The round table's original Background default is
# thereby amended for unknown types only.
_DEFAULT_ROLE = "important"


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


# ── the business-hours weight profile ─────────────────────────────────────
#
# Parked by the round table, un-parked by the owner at WP-1's close. All
# three sysadmin personas asked for it independently and all three asked for
# the same small thing: outside working hours the line-of-business machines
# should not fold as hard. Nobody asked for holidays, on-call rotas or
# per-server schedules — so this is a WINDOW and a second WEIGHT TABLE, and
# nothing else. It reuses `severity_model.weights`' mechanism, which means
# the fold learns no new concept: same arithmetic, different numbers.
#
# WHY THIS IS SAFE, and it is the only reason it was allowed in: weight
# scales ACCUMULATION only. The fold's rails do not read weight, so an
# Important server going down at 3am still arms its rail and still forces
# `elevated` with the machine named. Overnight damping can lower a score; it
# cannot produce an overnight silence.

#: Monday=0, matching `datetime.weekday()` and the rest of the app.
_DEFAULT_BUSINESS_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4)

#: The weights that apply OUTSIDE the window when the site names none.
#: Only `important` softens. A domain controller at 3am is exactly as much of
#: an emergency as at 3pm, and `background` is already at the floor — so the
#: line-of-business tier is the only one with anywhere to go.
OUTSIDE_WEIGHTS: dict[str, int] = {
    "critical_infrastructure": 10,
    "important": 2,
    "background": 1,
}


def is_business_hours(now: float, settings: dict | None) -> bool | None:
    """Inside the configured window? True / False / None for "no opinion".

    Three-valued deliberately. None means the site has not configured a
    window (or has configured one this function cannot read), and it is a
    DIFFERENT answer from False: False damps the weights, None leaves them
    alone. Collapsing the two would make a typo in `timezone` silently damp
    every weight in the estate at every hour of the day.

    `now` is an epoch float and a PARAMETER — no clock is read here. The
    zone comes from the configured `timezone`, because every timestamp this
    app reasons about or displays does, and the severity model is not an
    exception. A window whose start is after its end wraps midnight, which
    is what a night-shift site configures; without that a `22 → 6` window
    would read as "never inside" and damp around the clock.
    """
    settings = settings or {}
    window = (settings.get("severity_model") or {}).get("business_hours") or {}
    if not window.get("enabled"):
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.get("timezone") or "UTC")
        start = int(window.get("start_hour", 8))
        end = int(window.get("end_hour", 18))
        days = tuple(int(d) for d in (window.get("days")
                                      or _DEFAULT_BUSINESS_DAYS))
        local = datetime.fromtimestamp(float(now), tz)
    except Exception:
        logger.debug("business_hours is unreadable; no opinion", exc_info=True)
        return None
    if local.weekday() not in days:
        return False
    if start <= end:
        return start <= local.hour < end
    return local.hour >= start or local.hour < end     # wraps midnight


def weight_for(role: str, settings: dict | None, now: float | None = None) -> int:
    """The fold's number for a role word.

    Settings may override per-site (`severity_model.weights`) — the MSP
    copies that block between customers. An unknown role raises KeyError:
    weights are only ever looked up for words `resolve_role` produced, so
    an unknown word here is a programming error, not operator input.

    `now`, when given, applies the business-hours profile. Omitted, the base
    weights come back unchanged — this function must never consult a clock
    of its own, because both of this repo's flaky tests trace to wall-time
    buried inside logic, and a weight that changes without its caller asking
    is the same defect wearing a feature's name.
    """
    settings = settings or {}
    model = settings.get("severity_model") or {}
    overrides = model.get("weights") or {}
    if role not in WEIGHTS:
        raise KeyError(role)
    base = int(overrides.get(role, WEIGHTS[role]))
    if now is None or is_business_hours(now, settings) is not False:
        return base
    outside = {**OUTSIDE_WEIGHTS, **((model.get("business_hours") or {})
                                     .get("outside_weights") or {})}
    try:
        # Floored at 1: a weight of 0 removes the unit from the denominator
        # too, which would make a machine invisible rather than quieter.
        return max(1, int(outside[role]))
    except (KeyError, TypeError, ValueError):
        logger.debug("outside_weights unreadable for %s; using base", role)
        return base
