"""The cascade: closure, cycle rejection, the reducer, root election.

WP-1 phase 3 (docs/plans/SEVERITY_MODEL_SPEC.md, ratified 10-0). Pure
functions only — no database, no clock, no I/O. The DB slice persists what
these compute; the aggregator feeds them observed state.

THE OUTCOME: an N-deep chain going down produces ONE incident naming the
root, with the machines below it marked Impacted — muted, NEVER hidden.
Nagios proved the suppression works and proved the failure mode too:
operators filtered UNREACHABLE out of view entirely and stopped seeing real
outages. So Impacted stays visible as a muted row, always.

WHY REJECTION INSTEAD OF CYCLE HANDLING. `would_create_cycle` runs at
dependency-write time (rare, human-driven). The runtime reducer therefore
never walks a graph that can loop, which is what keeps it a dictionary
lookup on a 5-second path. Tarjan condensation was considered and rejected:
it spends an algorithm on user error that a clear error message prevents.

REDUNDANCY GROUPS are the day-one activation clause. An edge may point at a
GROUP (name prefixed `group:`), and a group is failed only when EVERY
member is failed. Two DCs with one down mutes nobody — which is what makes
shipping a seeded, assumed "every domain member depends on Domain Services"
edge safe without asking the operator first.
"""

from __future__ import annotations

from collections import deque

GROUP_PREFIX = "group:"

#: The one seeded group. Prefixed so it can never collide with a server name.
DOMAIN_GROUP = GROUP_PREFIX + "domain-services"

#: Server types treated as domain controllers for seeding purposes.
_DC_TYPES = frozenset({"domain_controller"})


def seeded_domain_edges(servers, settings: dict | None):
    """The day-one activation clause: assumed edges to a Domain Services group.

    Returns (edges, groups). Every non-DC server gets an ASSUMED edge to a
    group whose members are the domain controllers; the group fails only
    when ALL of them are down, so one DC rebooting mutes nobody.

    This is the one opinionated default in the model, and it exists because
    the cascade is otherwise dead code on every fresh install — nobody
    hand-draws a dependency graph. It is made safe by three things, all
    implemented here or beside it:

      * edges are labelled `assumed` with a reason, so a muted server can
        always say the mute rests on an assumption rather than on something
        the operator drew;
      * `severity_model.severed_assumed_edges` removes one per server —
        the one-click escape hatch, shipping in the same phase as the
        seeding, non-negotiable;
      * `severity_model.seed_domain_group: false` switches the whole thing
        off.

    A DC never depends on its own group: otherwise the last one standing
    would be muted by the failure of its siblings, which is a
    self-referential cascade. A fleet with no DC seeds nothing — edges to a
    group that can never fail are noise with no signal.
    """
    settings = settings or {}
    model = settings.get("severity_model") or {}
    if model.get("seed_domain_group") is False:
        return [], {}

    dcs, others = [], []
    for s in servers:
        stype = (getattr(s, "type", None) if not isinstance(s, dict)
                 else s.get("type")) or ""
        name = (getattr(s, "name", None) if not isinstance(s, dict)
                else s.get("name")) or ""
        if not name:
            continue
        (dcs if stype in _DC_TYPES else others).append(name)

    if not dcs:
        return [], {}

    severed = set(model.get("severed_assumed_edges") or [])
    edges = [{"server_name": n, "depends_on": DOMAIN_GROUP,
              "dependency_type": "auth", "assumed": True,
              "reason": "assumed — domain membership"}
             for n in others if n not in severed]
    return edges, {DOMAIN_GROUP: sorted(dcs)}

# The statuses that count as a FAILURE for cascade purposes. A warning is
# deliberately not one: a threshold breach is an independent condition and
# muting it would hide a real signal behind an unrelated outage.
_FAILED = frozenset({"offline", "down", "critical"})


def _is_failed(name: str, states: dict, groups: dict | None) -> bool:
    """Is this node failed? Groups fail only when all members do.

    An EMPTY group never fails. Without that guard `all([])` is True and a
    group defined before any member is classified would read as failed and
    mute the entire fleet — vacuous truth as an outage.
    """
    if groups and name.startswith(GROUP_PREFIX):
        members = groups.get(name) or []
        if not members:
            return False
        return all(states.get(m) in _FAILED for m in members)
    return states.get(name) in _FAILED


def build_closure(edges) -> dict:
    """Reachability in both directions, with shortest depth.

    edges: iterable of {"server_name": dependent, "depends_on": upstream}

    Returns {"downstream": {upstream: {dependent: depth}},
             "upstream":   {dependent: {upstream: depth}}}

    Depth is the SHORTEST path: "how far downstream" should not depend on
    which redundant route you happen to trace. Computed at dependency-CRUD
    time and cached; the runtime path only ever reads it.
    """
    direct_up: dict[str, set[str]] = {}
    direct_down: dict[str, set[str]] = {}
    for e in edges:
        dep = e.get("server_name")
        up = e.get("depends_on")
        if not dep or not up or dep == up:
            continue                     # self-edge: ignore, never loop
        direct_up.setdefault(dep, set()).add(up)
        direct_down.setdefault(up, set()).add(dep)

    def _bfs(start: str, adjacency: dict) -> dict[str, int]:
        seen: dict[str, int] = {}
        q = deque((n, 1) for n in adjacency.get(start, ()))
        while q:
            node, depth = q.popleft()
            if node == start or node in seen:
                continue
            seen[node] = depth
            for nxt in adjacency.get(node, ()):
                if nxt not in seen and nxt != start:
                    q.append((nxt, depth + 1))
        return seen

    nodes = set(direct_up) | set(direct_down)
    return {
        "downstream": {n: _bfs(n, direct_down) for n in nodes if direct_down.get(n)},
        "upstream": {n: _bfs(n, direct_up) for n in nodes if direct_up.get(n)},
    }


def would_create_cycle(edges, dependent: str, upstream: str) -> bool:
    """Would adding `dependent → upstream` close a loop?

    Called by the dependency writer. True ⇒ reject with a clear message.
    A self-edge is a cycle of length one and is rejected the same way.
    """
    if dependent == upstream:
        return True
    # A cycle forms iff `upstream` can already reach `dependent` by
    # following depends-on edges — i.e. dependent is already upstream of
    # upstream.
    closure = build_closure(edges)
    return dependent in closure["upstream"].get(upstream, {})


def effective_severity(server: str, own_status: str, closure: dict,
                       states: dict, groups: dict | None = None):
    """The reducer: (state, reason_code, root) for one server.

    Impacted iff this server is ITSELF failed AND some upstream of it is
    failed. The root reported is the DEEPEST failed upstream — naming the
    nearest would produce a chain of blame instead of one cause.

    Returns vocabulary words (severity_vocab) and a registered reason code,
    so the tooltip is `reason_text(code, ...)` and every branch here is one
    mutation target.
    """
    if own_status not in _FAILED:
        # Not failing: nothing to explain away. A healthy machine under a
        # dead upstream is healthy, not impacted.
        if own_status == "warning":
            return "degraded", "breach", None
        return ("healthy" if own_status == "healthy" else "unknown",
                "all_clear" if own_status == "healthy" else "no_data", None)

    ups = closure.get("upstream", {}).get(server, {})
    failed_ups = [(name, depth) for name, depth in ups.items()
                  if _is_failed(name, states, groups)]
    if not failed_ups:
        return "down", "own_down", None

    # Deepest failed upstream = the root of this cascade branch.
    root = max(failed_ups, key=lambda pair: pair[1])[0]
    return "impacted", "impacted_by", root


def promotion_candidates(closure: dict, states: dict, open_subjects,
                         fresh=None, groups: dict | None = None) -> dict:
    """{child: origin_root} — children whose root recovered while they stayed
    down. Pure; no clock, no database.

    A muted machine must not stay muted once its excuse is gone. This is the
    other half of "Impacted is never hidden": the mute has an EXIT, and the
    exit fires exactly once, as the child's first and only notification.

    A server X is a candidate iff all five hold:

      * X is failed — obviously;
      * X is FRESH. `fresh` is the set of servers whose latest sample is
        recent. This fails CLOSED: an absent or empty `fresh` promotes
        nothing, because a stale `offline` may describe a machine that is
        already back, and a first-and-only page cannot be un-sent;
      * X has NO failed upstream any more — otherwise it is still explained,
        merely by someone else now;
      * X does not already own an incident — the row IS the idempotence
        guard, which is why there is no second stamp to make;
      * some upstream of X owns an incident. That is what separates "your
        root recovered and left you behind" from "you just went down", and
        it is why an ordinary lone failure is not reported here.

    The origin named is the DEEPEST such upstream, matching
    `effective_severity` — so the promoted incident's origin is the same
    machine the child was muted under, not the nearest hop.

    A redundancy group can never be an origin: it is not a server, so it
    owns no incident row. A member fleet recovering therefore leaves the
    child with an ordinary failure, which is correct — nobody was ever paged
    for the group.
    """
    # Normalised to sets FIRST, then tested ONCE. A second early-out on an
    # empty `fresh` would be unreachable-by-behaviour: removing it alone
    # changes nothing, so it would read as dead code to the next person and as
    # a blind test to the mutation harness. `build_closure` above carries the
    # same warning for the same reason.
    fresh = set(fresh or ())
    owners = set(open_subjects or ())
    ups_map = closure.get("upstream", {})
    out: dict[str, str] = {}
    for name, status in states.items():
        if status not in _FAILED or name not in fresh or name in owners:
            continue
        ups = ups_map.get(name, {})
        if not ups:
            continue
        if any(_is_failed(u, states, groups) for u in ups):
            continue
        origins = [(u, d) for u, d in ups.items() if u in owners]
        if not origins:
            continue
        out[name] = max(origins, key=lambda pair: pair[1])[0]
    return out


def elect_roots(closure: dict, states: dict, onsets: dict,
                weights: dict, groups: dict | None = None) -> dict:
    """Group the failed servers into {root: [children]} — one root per outage.

    A ROOT is a failed server with no failed upstream. Ties (two candidate
    roots both upstream of the same dependent) break on earliest OBSERVED
    onset, then on higher impact weight — the earlier failure is the more
    likely cause, and the heavier machine is where to send someone first.

    Independent failures elect independent roots: unrelated outages must
    not be collapsed just because they overlap in time, which is exactly
    how the retrospective 60-second window rule got it wrong.
    """
    failed = [s for s, st in states.items() if st in _FAILED]
    if not failed:
        return {}

    ups_map = closure.get("upstream", {})
    roots: list[str] = []
    children: dict[str, str] = {}          # child → its elected root

    for s in failed:
        ups = ups_map.get(s, {})
        failed_ups = [(n, d) for n, d in ups.items()
                      if _is_failed(n, states, groups)]
        if not failed_ups:
            roots.append(s)
            continue
        # Deepest first; ties by earliest onset, then heavier weight.
        best = sorted(
            failed_ups,
            key=lambda pair: (-pair[1],
                              onsets.get(pair[0], float("inf")),
                              -weights.get(pair[0], 0)))[0][0]
        children[s] = best

    out: dict[str, list] = {r: [] for r in roots}
    for child, root in children.items():
        # A root reached through a group edge is the group itself; keep it,
        # the incident names the group ("Domain Services").
        out.setdefault(root, []).append(child)
    return out
