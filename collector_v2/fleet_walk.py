"""Bounded-concurrency fleet walks for the periodics thread.

Collector audit finding 2 (HIGH at scale). Five periodic jobs — failed logins,
health checks, security status, TLS, drift — each walked their whole list
SEQUENTIALLY inside the single periodics thread. This is not the worker pool and
it does not benefit from it.

WHY IT BREAKS, and it breaks before anything else does. The cost of one item is
dominated by a TIMEOUT, not by work: an unreachable host costs ~15s of waiting.
Serial, a fleet of N hosts with U unreachable costs about U×15s per job — so
~20 unreachable hosts push a job with a 300s cadence past its own cadence, and
it never catches up. The audit puts that wall at roughly 100–150 servers, which
is BEFORE the worker pool saturates: the first scale ceiling in Prism was one
`for` loop, not a lack of parallelism where parallelism already existed.

WHAT THIS IS. One helper, so all five walks get the same behaviour and the same
telemetry rather than five slightly different loops. Concurrency is bounded and
configurable; `workers=1` reproduces the old serial walk exactly, which is the
setting a site uses to rule this out as a cause.

THREE THINGS IT MUST DO, all of them learned from what the serial loops did
right and what they did not:

  * ISOLATE. One bad host must not end the pass. Every unit is try/excepted
    individually, exactly as the serial loops did — this is the property most
    easily lost when moving to futures, because an unhandled exception in a
    future is silent until someone reads its result.
  * REPORT. The pass returns what it did and logs a warning when it exceeds its
    own budget. The failure this replaces was INVISIBLE: a job quietly taking
    longer than its cadence looks exactly like a job running normally.
  * NOT LIE ABOUT CONCURRENCY. `max_in_flight` is observed, not assumed, so a
    test can prove the walk actually overlapped instead of asserting that the
    code contains a thread pool. Passing an executor is not the same as using
    one — a walk over a lock-serialised body runs concurrently and finishes
    serially, and only a measurement can tell the difference.

WHAT IT DOES NOT DO. It does not make the DATABASE parallel. Writes still
serialise on the process-global write lock, and that is fine: the wait being
overlapped here is the network, which is where the 15 seconds are.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger("prism.collector_v2.fleet_walk")

#: Default concurrency for a periodic fleet walk. Eight is chosen against the
#: cost being managed — an unreachable host is ~15s of idle waiting, so eight
#: turns a 20-host stall from ~300s into ~40s — and kept modest because these
#: walks open WinRM sessions and share one SQLite writer. Raise it with
#: `settings.collector_v2_periodic_workers` on a big fleet.
DEFAULT_WORKERS = 8


@dataclass(frozen=True)
class WalkResult:
    """What one pass actually did. `max_in_flight` is observed."""
    total: int
    ok: int
    failed: int
    elapsed_s: float
    max_in_flight: int


def worker_count(settings: dict | None) -> int:
    """Concurrency for a periodic walk, from settings.

    Floored at 1, so a misconfiguration degrades to the old serial behaviour
    rather than to no walk at all. An unreadable value falls back to the default
    instead of raising: config.json is hand-edited, and one bad character must
    not stop every periodic job in the process.
    """
    try:
        return max(1, int((settings or {}).get("collector_v2_periodic_workers",
                                               DEFAULT_WORKERS)))
    except (TypeError, ValueError):
        logger.debug("collector_v2_periodic_workers is unreadable; using %d",
                     DEFAULT_WORKERS)
        return DEFAULT_WORKERS


def walk(items, fn, *, workers: int = DEFAULT_WORKERS, label: str = "walk",
         budget_s: float | None = None, name_of=None) -> WalkResult:
    """Run `fn(item)` over `items` with at most `workers` in flight.

    Exceptions from `fn` are logged against the item and counted; they never
    propagate, because a fleet walk that stops at the first broken host is the
    behaviour the serial loops were careful to avoid and the one a naive
    futures rewrite reintroduces.

    `budget_s`, when given, is the cadence this job is supposed to fit inside.
    Exceeding it is logged at WARNING with the numbers, because a job silently
    running longer than its own cadence is indistinguishable from a healthy one.
    """
    items = list(items or ())
    if not items:
        return WalkResult(0, 0, 0, 0.0, 0)

    name_of = name_of or (lambda item: getattr(item, "name", None) or str(item))
    workers = max(1, min(int(workers), len(items)))

    lock = threading.Lock()
    state = {"in_flight": 0, "max_in_flight": 0, "ok": 0, "failed": 0}
    start = time.monotonic()

    def _run(item):
        with lock:
            state["in_flight"] += 1
            if state["in_flight"] > state["max_in_flight"]:
                state["max_in_flight"] = state["in_flight"]
        try:
            fn(item)
            with lock:
                state["ok"] += 1
        except Exception:
            with lock:
                state["failed"] += 1
            logger.exception("[%s] %s failed", name_of(item), label)
        finally:
            with lock:
                state["in_flight"] -= 1

    if workers == 1:
        # This is the documented way to rule the pool out as a cause of a
        # problem, so it runs the ACTUAL old code path rather than a pool of one.
        #
        # NOT MUTATION-TESTABLE, and that is a property of the code rather than
        # a gap in the tests: a ThreadPoolExecutor with max_workers=1 is
        # behaviourally identical to this loop — same order, same peak
        # concurrency of one — so swapping either for the other changes nothing
        # observable. What matters (order preserved, no overlap) is tested; the
        # branch is a thread-creation optimisation plus the plain-reading
        # guarantee an operator gets when they set the worker count to 1. Do not
        # delete it as dead code, and do not expect the harness to defend it.
        for item in items:
            _run(item)
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix=f"prism-{label}") as pool:
            list(pool.map(_run, items))

    elapsed = time.monotonic() - start
    result = WalkResult(total=len(items), ok=state["ok"], failed=state["failed"],
                        elapsed_s=round(elapsed, 2),
                        max_in_flight=state["max_in_flight"])
    if budget_s and elapsed > budget_s:
        logger.warning(
            "%s took %.0fs for %d items (%d failed) with %d workers — longer "
            "than its own %.0fs cadence, so this job can no longer keep up. "
            "Raise settings.collector_v2_periodic_workers, or find out why "
            "hosts are timing out.",
            label, elapsed, len(items), state["failed"], workers, budget_s)
    elif state["failed"]:
        logger.info("%s: %d/%d items failed in %.1fs (%d workers)",
                    label, state["failed"], len(items), elapsed, workers)
    return result
