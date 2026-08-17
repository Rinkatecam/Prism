"""The status summary read from the collector's hot cache instead of SQLite.

`Database.get_status_summary()` measures 7.37 ms against the live database: it
scans all 83,549 metrics rows through a covering index to answer a question
about 29 servers. Two endpoints needed it on the same prismRefresh — the hero
banner and the vitals quadrant — so the dashboard paid it twice every five
seconds. `partial_critical_issues` and `partial_server_grid` had been avoiding
exactly this for far longer by reading `state.latest_by_server`; the vitals
context copied the wrong neighbour.

THE THING THAT MAKES THIS SAFE OR NOT is whether the cache-derived summary is
the same summary. That is asserted directly here, over real metrics rows, and
it is the reason this file exists rather than the timing — a performance fix
that quietly changes the dashboard's headline numbers is a worse defect than
the cost it removes.

WHAT THESE ARE BLIND TO:

  * The aggregator. That `update_latest_metric` is called AFTER the row is
    persisted — so the cache can never be ahead of the database — is read out
    of collector_v2/state.py, not exercised. If that order ever inverts, the
    cache could carry a status the database has not stored and nothing here
    would notice.
  * Timing. No assertion is made about how long anything takes; the
    equivalence is the contract and the speed is a consequence.
  * Concurrency. The read takes `_state_lock` and snapshots, matching the
    pattern `routes/api/misc.py` adopted after an intermittent 500 in the
    topology pane. Nothing here runs two threads.
"""

from __future__ import annotations

import pytest

import state
from routes import views


@pytest.fixture()
def cache():
    """A clean `state.latest_by_server`, restored afterwards.

    The module-level dict is shared process-wide, so a test that leaves rows
    in it changes what every later test sees."""
    with state._state_lock:
        original = dict(state.latest_by_server)
        state.latest_by_server.clear()
    yield state.latest_by_server
    with state._state_lock:
        state.latest_by_server.clear()
        state.latest_by_server.update(original)


def _row(name, status):
    return {"server_name": name, "status": status, "cpu_percent": 1.0,
            "ram_percent": 2.0, "disk_c_percent": 3.0, "disk_d_percent": None,
            "timestamp": "2026-08-17T09:00:00Z"}


# ── the equivalence, which is the whole question ─────────────────────────

def test_the_cache_and_the_query_produce_the_same_summary(tmp_db, cache):
    """Over the same data, byte-for-byte. Every status the collector writes is
    represented, plus more than one server per bucket so a transposition
    would show up."""
    fleet = {
        "a": "healthy", "b": "healthy", "c": "healthy",
        "d": "warning", "e": "warning",
        "f": "critical",
        "g": "offline", "h": "offline", "i": "offline", "j": "offline",
    }
    for name, status in fleet.items():
        # Two rows each, so the query has a MAX(id) to resolve and would be
        # caught double-counting.
        tmp_db.insert_metric(name, 1, 2, 3, None, "healthy")
        tmp_db.insert_metric(name, 1, 2, 3, None, status)
        cache[name] = _row(name, status)

    from_db = tmp_db.get_status_summary()
    from_cache = views._status_summary_from_cache(set(fleet))

    assert from_cache == from_db, (
        "the cache-derived summary differs from the query's; the dashboard's "
        "headline numbers would change with this optimisation")
    assert from_db == {"total": 10, "healthy": 3, "warning": 2,
                       "critical": 1, "offline": 4}


def test_the_query_resolves_the_latest_row_and_so_does_the_cache(tmp_db, cache):
    """A server that WAS healthy and is now critical must read critical from
    both. The query does it with MAX(id); the cache does it by holding only
    the newest row, which is the property being relied on."""
    tmp_db.insert_metric("a", 1, 2, 3, None, "healthy")
    tmp_db.insert_metric("a", 1, 2, 3, None, "critical")
    cache["a"] = _row("a", "critical")

    assert tmp_db.get_status_summary()["critical"] == 1
    assert views._status_summary_from_cache({"a"})["critical"] == 1


# ── declining, which is the careful part ─────────────────────────────────

def test_a_cold_cache_declines(cache):
    """Right after a restart there is nothing to fold. Returning zeroes would
    report an empty fleet as an empty fleet."""
    assert views._status_summary_from_cache({"a", "b"}) is None


def test_a_partial_cache_declines_rather_than_understating(cache):
    """The cache fills one server per Result, so for up to a poll cycle it
    covers part of the fleet. Folding that would report "2 of 2 healthy" on a
    fleet of three — and with no warning or critical among the two that had
    reported, the hero banner would vanish while a server was in trouble.

    This is the one behaviour the other cache readers do NOT need: a server
    missing from the topology pane is simply not drawn yet, but a summary is
    a claim about the whole fleet."""
    cache["a"] = _row("a", "healthy")
    cache["b"] = _row("b", "healthy")

    assert views._status_summary_from_cache({"a", "b", "c"}) is None, (
        "a partial cache produced a summary; it understates the fleet and the "
        "understatement looks like good news")
    # And the moment it completes, it answers.
    cache["c"] = _row("c", "critical")
    assert views._status_summary_from_cache({"a", "b", "c"}) == {
        "total": 3, "healthy": 2, "warning": 0, "critical": 1, "offline": 0}


def test_an_orphaned_cache_entry_is_not_counted(tmp_db, cache):
    """The deliberate divergence from the query, and it is an improvement: a
    server deleted from the config stops counting here immediately, while the
    query keeps counting its metrics rows until retention prunes them.

    Asserted rather than left implicit, because it means the two are NOT
    identical in every state and the equivalence test above would otherwise
    look like a stronger guarantee than it is."""
    tmp_db.insert_metric("a", 1, 2, 3, None, "healthy")
    tmp_db.insert_metric("gone", 1, 2, 3, None, "critical")
    cache["a"] = _row("a", "healthy")
    cache["gone"] = _row("gone", "critical")

    assert tmp_db.get_status_summary() == {"total": 2, "healthy": 1, "warning": 0,
                                           "critical": 1, "offline": 0}
    assert views._status_summary_from_cache({"a"}) == {
        "total": 1, "healthy": 1, "warning": 0, "critical": 0, "offline": 0}


# ── the fold's contract ──────────────────────────────────────────────────

def test_every_row_counts_toward_the_total_but_only_known_statuses_bucket():
    """Inherited from `get_status_summary` on purpose, including the hole: a
    fifth status inflates `total` and lands in no bucket, so `_estate_vitals`
    — which derives `unknown` from `server_count - total` — loses it from
    `monitored` altogether. Pinned here so that the day a collector adds one,
    this test is what explains where the server went."""
    folded = views._fold_status_summary([
        _row("a", "healthy"), _row("b", "stabilising")])
    assert folded == {"total": 2, "healthy": 1, "warning": 0,
                      "critical": 0, "offline": 0}
    assert folded["healthy"] + folded["warning"] + folded["critical"] \
        + folded["offline"] == 1 < folded["total"]


def test_a_status_named_like_a_bucket_key_cannot_clobber_the_total():
    """`get_status_summary` buckets with `if status in summary`, which would
    also match the literal key "total" and overwrite the running count. It
    cannot happen with today's four statuses, and it is one collector change
    from happening silently, so this fold matches against an explicit tuple
    instead."""
    folded = views._fold_status_summary([_row("a", "total"), _row("b", "healthy")])
    assert folded["total"] == 2, (
        "a row whose status is the string 'total' corrupted the total")
    assert folded["healthy"] == 1


def test_the_bucket_list_matches_the_statuses_the_collector_writes():
    """Measured against the live database: 83,549 metrics rows hold exactly
    these four. If the collector gains a fifth, the previous two tests
    describe what happens, and this one is where the list is updated."""
    assert set(views._STATUS_BUCKETS) == {"healthy", "warning", "critical", "offline"}


# ── the point of the exercise ────────────────────────────────────────────

def test_the_cache_is_snapshotted_under_the_lock():
    """Asserted STRUCTURALLY, and the reason is worth stating: no
    single-threaded test can see a missing lock, and a test that races two
    threads to provoke a `KeyError` would pass or fail on timing. A flaky
    guard is worse than a structural one, because it gets disabled.

    The mechanism matters. `routes/api/misc.py` adopted exactly this pattern
    after an intermittent 500 in the topology pane — a concurrent writer
    mutating `latest_by_server` while a reader iterated it — and the comment
    there records it. Reading `configured - cache.keys()` and then
    `cache[name]` against the LIVE dict has the same window: the aggregator can
    remove a server between the two.

    Blind spot, stated: this proves the lock is taken and a copy is made. It
    cannot prove the copy is what gets read afterwards."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(views._status_summary_from_cache))
    tree = ast.parse(src)

    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
    assert withs, "the cache is read without taking _state_lock"
    locked = [w for w in withs
              if any(isinstance(item.context_expr, ast.Attribute)
                     and item.context_expr.attr == "_state_lock"
                     for item in w.items)]
    assert locked, "the `with` block no longer acquires _state_lock"

    # A copy, inside that block. `dict(...)` around the live mapping is what
    # makes every later read immune to a concurrent write.
    copies = [n for n in ast.walk(locked[0])
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "dict"]
    assert copies, (
        "the cache is aliased rather than snapshotted inside the lock, so the "
        "reads after it race the aggregator")


def test_only_one_place_reads_the_status_summary_for_the_dashboard():
    """The duplication itself, asserted structurally.

    Both dashboard endpoints used to call `get_status_summary()` — the hero
    banner and the quadrant, five seconds apart, for the same numbers. They now
    share `_vitals_context()`. A second direct call reintroduces the cost the
    cache path exists to remove, and no behavioural test would notice: the
    numbers would be right, just paid for twice.

    Scoped to the three dashboard views by name rather than to the whole file,
    because `reports.py` and `routes/api/metrics.py` are legitimate callers of
    the query and are nothing to do with this.

    PARSED, not grepped. The first version searched the function's source text
    and failed immediately — on `partial_verdict_header`'s own docstring, which
    explains that it USED to call `get_status_summary()` directly. That is the
    shape recorded three times in docs/OPS-LEARNINGS.md: a text-scanning check
    fires on its own documentation, and the cheapest way to make it pass is to
    delete the explanation. Walking the AST counts real attribute accesses and
    cannot see prose at all."""
    import ast
    import inspect
    import textwrap

    def _calls(fn) -> int:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return sum(1 for node in ast.walk(tree)
                   if isinstance(node, ast.Attribute)
                   and node.attr == "get_status_summary")

    for fn in (views.dashboard, views.partial_vitals, views.partial_verdict_header):
        assert _calls(fn) == 0, (
            f"{fn.__name__} reads the status summary directly again; it must go "
            "through _vitals_context() so the query is paid for at most once "
            "per refresh, and only when the cache cannot answer")
    # And exactly one place may fall back to it.
    assert _calls(views._vitals_context) == 1, (
        "expected exactly one fallback call to the query in _vitals_context")


def test_a_complete_cache_means_the_query_never_runs(tmp_db, cache, monkeypatch):
    """The reason for all of the above. With the cache warm, neither endpoint
    may touch `get_status_summary` — otherwise the duplication is still there
    and only the comments changed."""
    calls = []

    class _Spy:
        def get_status_summary(self):
            calls.append("status")
            return {"total": 0, "healthy": 0, "warning": 0, "critical": 0, "offline": 0}

        def get_health_check_summary(self):
            return {"total": 0, "up": 0, "down": 0, "unknown": 0}

    class _Cfg:
        def get_servers(self):
            return [type("S", (), {"name": n})() for n in ("a", "b")]

    monkeypatch.setattr(views, "_db", _Spy())
    monkeypatch.setattr(views, "_config", _Cfg())
    cache["a"] = _row("a", "healthy")
    cache["b"] = _row("b", "warning")

    ctx = views._vitals_context()
    assert calls == [], "the query ran despite a complete cache"
    assert ctx["summary"] == {"total": 2, "healthy": 1, "warning": 1,
                             "critical": 0, "offline": 0}

    # And the fallback still works when the cache cannot answer.
    cache.pop("b")
    views._vitals_context()
    assert calls == ["status"], (
        "the cache declined and nothing fell back to the query, so the "
        "dashboard would render a summary of None")
