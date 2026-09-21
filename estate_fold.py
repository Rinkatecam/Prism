"""The weighted estate fold: score, bands, rails, dwell, freeze, latch.

WP-1 phase 2 (docs/plans/SEVERITY_MODEL_SPEC.md, ratified 10-0). This
module is the estate's single answer machine. Everything here is a PURE
function of its arguments — the clock is a parameter, never a read —
because the dwell and the freeze ARE clocks, and the repo's two flaky
tests both trace to wall-time inside logic.

Three clocks, three jobs (calibrated by the round table):

  * `fold()` runs on every aggregator merge (≤29/min, O(n) arithmetic,
    zero queries). Its verdict plus `advance()` decide the display.
  * The 5s supervisor tick calls `advance()` to move timers only.
  * The dashboard reads the cached result and evaluates NOTHING.

The division of honesty: `fold()` is stateless truth about NOW; `advance()`
is the damping state machine that decides what the operator SEES — enter
fast, exit only on evidence, and if the answer keeps changing, hold the
cautious one and say "settling". Rails pierce everything upward. The freeze
can only ever over-warn, never under-warn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# Severity points per stored status. `impacted` is phase 3's reducer
# plugging into the same table — registered now so the fold's contract is
# complete before the cascade exists. Unknown/unmeasured contribute nothing:
# absence of evidence is not evidence of trouble (the pre-band gates in the
# reader still SAY "unmeasured"; they just don't score it).
SEVERITY_POINTS: dict[str, float] = {
    "critical": 1.0,
    "offline": 1.0,
    "down": 1.0,          # alias: the vocabulary word for offline
    "warning": 0.35,
    "impacted": 0.15,
    "healthy": 0.0,
    "unknown": 0.0,
}

# (band, inclusive lower threshold), most urgent first. S ≥ threshold wins.
# 0.03: one Important warning (4·0.35/113 ≈ 0.012) stays calm, one Important
# critical (4/113 ≈ 0.035) elevates. 0.25: it takes real accumulation or a
# rail to reach urgent — which is the point; rails answer the promises.
BANDS: tuple[tuple[str, float], ...] = (
    ("urgent", 0.25),
    ("elevated", 0.03),
    ("calm", 0.0),
)

_BAND_RANK = {"calm": 0, "elevated": 1, "urgent": 2}

# Damping constants, ratified with defenses (spec §"The three clocks"):
_FREEZE_TRIP_CHANGES = 3          # the 3rd change in the window trips it
_FREEZE_WINDOW_S = 30 * 60
_FREEZE_RELEASE_STABLE_S = 10 * 60
_LATCH_K = 6                      # transitions that latch…
_LATCH_WINDOW_S = 15 * 60         # …inside this window
_LATCH_RELEASE_MAX = 1            # release when ≤ this many in the window


@dataclass(frozen=True)
class RawVerdict:
    """`fold()`'s stateless answer. band/score are the maths; rail is the
    sentence-ready fact when one binds; the rest is the dossier."""
    band: str
    score: float
    rail: str | None
    contributors: list[dict]
    why_not_higher: str
    excluded_maintenance: int
    monitored_weight: float


def _points_for(server: dict) -> float:
    """A server's severity points — the latch substitutes its worst recent
    state for the bouncing stored one, so Unsteady is honest about severity
    even while the word holds still."""
    status = server.get("latched_worst") or server.get("status") or "unknown"
    return SEVERITY_POINTS.get(status, 0.0)


def _is_down(server: dict) -> bool:
    status = server.get("latched_worst") or server.get("status") or ""
    return status in ("offline", "down")


def _is_critical_or_down(server: dict) -> bool:
    status = server.get("latched_worst") or server.get("status") or ""
    return status in ("critical", "offline", "down")


def fold(servers: list[dict], services: list[dict], settings: dict | None) -> RawVerdict:
    """The weighted fold over one snapshot. Pure; O(n); zero queries.

    servers: [{name, status, role, weight, in_maintenance, latched_worst}]
    services: [{name, status ("up"/"down"/"unknown"), weight}] — weight is
      the bound server's, or a per-check criticality override upstream.
      Services carry score, never rails: rails are host-scoped because
      "the estate is critical" must always name a MACHINE someone can go
      look at.

    Maintenance excludes a unit from score AND rails — patch night must not
    be a cardiac event — and the exclusion is counted, not silent, because
    an invisible exclusion is a forever-mute wearing a feature's name.
    """
    total_w = 0.0
    weighted = 0.0
    contribs: list[dict] = []
    rail: str | None = None
    rail_rank = -1
    excluded = 0

    for s in servers:
        if s.get("in_maintenance"):
            excluded += 1
            continue
        w = float(s.get("weight", 1))
        total_w += w
        p = _points_for(s)
        if p > 0:
            weighted += w * p
            contribs.append({"name": s.get("name", "?"), "role": s.get("role", ""),
                             "weighted_points": w * p,
                             "status": s.get("latched_worst") or s.get("status")})
        # Rails, most severe wins the sentence:
        if s.get("role") == "critical_infrastructure" and _is_critical_or_down(s):
            if rail_rank < 2:
                rail, rail_rank = (
                    f"{s.get('name', '?')}, Critical infrastructure, is "
                    f"{'down' if _is_down(s) else 'critical'}", 2)
        elif _is_down(s) and rail_rank < 1:
            rail, rail_rank = f"{s.get('name', '?')} is down", 1

    for svc in services:
        status = (svc.get("status") or "unknown").lower()
        if status == "unknown":
            continue                     # unprobed: not in the denominator
        w = float(svc.get("weight", 1))
        total_w += w
        if status == "down":
            weighted += w * 1.0
            contribs.append({"name": svc.get("name", "?"), "role": "service",
                             "weighted_points": w * 1.0, "status": "down"})

    score = (weighted / total_w) if total_w > 0 else 0.0
    # Round away float dust so the inclusive band boundaries behave: the
    # spec's ≥ is about VALUES like 0.03, not about 0.029999999999.
    score = round(score, 9)

    band = "calm"
    for name, threshold in BANDS:
        if score >= threshold:
            band = name
            break

    # Rails raise, never lower.
    if rail_rank == 2 and _BAND_RANK[band] < 2:
        band = "urgent"
    elif rail_rank == 1 and _BAND_RANK[band] < 1:
        band = "elevated"

    contribs.sort(key=lambda c: c["weighted_points"], reverse=True)
    contribs = contribs[:3]

    why = _why_not_higher(band, score, rail)

    return RawVerdict(band=band, score=score, rail=rail,
                      contributors=contribs, why_not_higher=why,
                      excluded_maintenance=excluded,
                      monitored_weight=total_w)


def _why_not_higher(band: str, score: float, rail: str | None) -> str:
    """The anti-mistrust clause: the calmer needle explains itself, always.
    Names the distance to the next band and whether a rail is armed."""
    if band == "urgent":
        return ""            # nothing is higher; the dossier shows the rail
    nxt, threshold = ("elevated", 0.03) if band == "calm" else ("urgent", 0.25)
    gap = max(0.0, round(threshold - score, 9))
    rail_part = "a rail is armed" if rail else "no rail is armed"
    return (f"not {nxt}: score {score:g} is {gap:g} below {threshold:g} "
            f"and {rail_part}")


# ── the damping state machine ─────────────────────────────────────────────

@dataclass(frozen=True)
class FoldState:
    """What the operator SEES, plus the evidence for why. Immutable —
    `advance()` returns a new state — so the aggregator can publish it to
    the hot cache without a lock dance, and a restart simply starts from
    FoldState() and adopts the first raw verdict (ratified as acceptable).
    """
    displayed_band: str = "calm"
    seeded: bool = False              # first verdict adopts raw, no "change"
    change_times: tuple[float, ...] = field(default_factory=tuple)
    # dwell (open when raw < displayed):
    dwell_opened_at: float | None = None
    dwell_target: str | None = None
    # freeze:
    frozen: bool = False
    raw_stable_band: str | None = None
    raw_stable_since: float | None = None

    @property
    def settling(self) -> bool:
        return self.frozen


def _record_change(state: FoldState, band: str, now: float) -> FoldState:
    times = tuple(t for t in state.change_times
                  if now - t <= _FREEZE_WINDOW_S) + (now,)
    frozen = state.frozen
    if not frozen and len(times) >= _FREEZE_TRIP_CHANGES:
        frozen = True
    return replace(state, displayed_band=band, change_times=times,
                   frozen=frozen, dwell_opened_at=None, dwell_target=None)


def advance(state: FoldState, raw, now: float, poll_interval: int = 60,
            fresh_counts: dict[str, int] | None = None) -> FoldState:
    """One evaluation: fold's raw verdict in, what-to-display out.

    Upward is instant and pierces the freeze — the freeze may only ever
    over-warn. Downward needs the dwell: max(2×poll_interval, 60s) AND
    every recovering contributor answered at least twice since the dwell
    opened ("the estate calms down only after every recovering server has
    answered twice"). A relapse cancels the dwell — recovery evidence
    restarts from zero, because the relapse just proved it wasn't evidence.
    """
    fresh_counts = fresh_counts or {}
    raw_band = raw.band

    # Track raw stability for the freeze release, independent of display.
    if state.raw_stable_band == raw_band:
        stable_since = state.raw_stable_since
    else:
        stable_since = now
    state = replace(state, raw_stable_band=raw_band, raw_stable_since=stable_since)

    if not state.seeded:
        # Restart / first merge: adopt raw silently. No change is recorded —
        # a process start is not the estate changing its mind.
        return replace(state, displayed_band=raw_band, seeded=True)

    cur = _BAND_RANK[state.displayed_band]
    new = _BAND_RANK[raw_band]

    if new > cur:
        # Upward: instant, freeze pierced by construction.
        return _record_change(state, raw_band, now)

    if new == cur:
        # Agreement: any open dwell is moot; check the freeze release.
        state = replace(state, dwell_opened_at=None, dwell_target=None)
        return _maybe_release_freeze(state, now)

    # Downward. Frozen displays hold; the release path is raw stability.
    if state.frozen:
        return _maybe_release_freeze(state, now)

    if state.dwell_opened_at is None or state.dwell_target != raw_band:
        # Open (or retarget) the dwell. Retarget also covers raw moving
        # between two lower bands while we wait.
        return replace(state, dwell_opened_at=now, dwell_target=raw_band)

    dwell_needed = max(2 * poll_interval, 60)
    time_ok = (now - state.dwell_opened_at) >= dwell_needed
    answers_ok = bool(fresh_counts) and all(v >= 2 for v in fresh_counts.values())
    if time_ok and answers_ok:
        return _record_change(state, raw_band, now)
    return state


def _maybe_release_freeze(state: FoldState, now: float) -> FoldState:
    if not state.frozen:
        return state
    if (state.raw_stable_since is not None
            and now - state.raw_stable_since >= _FREEZE_RELEASE_STABLE_S):
        # Released: adopt the stable raw band. This is not a "change" for
        # freeze accounting — counting the release as instability would
        # re-trip the freeze it just left, forever.
        return replace(state, frozen=False, change_times=(),
                       displayed_band=state.raw_stable_band,
                       dwell_opened_at=None, dwell_target=None)
    return state


# ── the latch (pure; persistence belongs to the aggregator) ───────────────

def latch_should_latch(transition_times: list[float], now: float) -> bool:
    """≥6 severity transitions inside 15 minutes. Transitions that happened
    inside a maintenance window must never reach this list — the caller
    filters them, because this function cannot know the windows."""
    recent = [t for t in transition_times if now - t <= _LATCH_WINDOW_S]
    return len(recent) >= _LATCH_K


def latch_should_release(transition_times: list[float], now: float) -> bool:
    """Release only when the window has gone quiet: ≤1 transition in 15min.
    Hysteresis on the latch itself, so the label cannot flap either."""
    recent = [t for t in transition_times if now - t <= _LATCH_WINDOW_S]
    return len(recent) <= _LATCH_RELEASE_MAX
