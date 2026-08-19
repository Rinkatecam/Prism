"""The periodics fleet walk (collector audit finding 2, HIGH at scale).

THE FINDING. Five periodic jobs walked the fleet SEQUENTIALLY in one thread. The
cost per host is a TIMEOUT, not work — ~15s for an unreachable box — so with U
unreachable hosts a job costs about U×15s. At ~20 unreachable hosts a job with a
300s cadence exceeds its own cadence and never catches up again. The audit puts
the wall at roughly 100–150 servers, which is BEFORE the worker pool saturates:
Prism's first scale ceiling was a `for` loop, not a shortage of threads.

HOW THIS IS TESTED, and why not with a stopwatch. "It is faster now" measured by
wall-clock is exactly the flaky test this repo has been bitten by twice. What is
actually being claimed is that the walk OVERLAPS, so the walk observes its own
peak concurrency and the tests assert on that. A duration assertion appears once,
with a wide margin, and only to catch the case where overlap is reported but the
work is serialised behind something anyway — which is the failure a concurrency
counter alone cannot see, because a pool whose body takes a shared lock still
reports several tasks in flight.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector_v2 import fleet_walk                        # noqa: E402


class _Host:
    def __init__(self, name):
        self.name = name


def _fleet(n):
    return [_Host(f"SRV{i:02d}") for i in range(n)]


# ── the walk does the work ────────────────────────────────────────────────

def test_every_item_is_visited():
    seen = []
    lock = threading.Lock()

    def _do(host):
        with lock:
            seen.append(host.name)

    result = fleet_walk.walk(_fleet(20), _do, workers=5, label="t")
    assert sorted(seen) == sorted(h.name for h in _fleet(20))
    assert result.total == 20 and result.ok == 20 and result.failed == 0


def test_an_empty_fleet_is_a_no_op():
    result = fleet_walk.walk([], lambda h: None, workers=5)
    assert result.total == 0 and result.max_in_flight == 0


# ── the actual claim: it overlaps ─────────────────────────────────────────

def test_the_walk_overlaps_its_items():
    """The claim under test, measured rather than assumed. A barrier makes it
    deterministic: five items each wait until all five have arrived, which can
    only happen if all five are in flight at once."""
    barrier = threading.Barrier(5, timeout=10)

    def _do(host):
        barrier.wait()

    result = fleet_walk.walk(_fleet(5), _do, workers=5, label="t")
    assert result.ok == 5
    assert result.max_in_flight == 5


def test_concurrency_is_bounded_by_the_worker_count():
    """A bound that does not bind is not a bound. These walks open WinRM
    sessions and share one SQLite writer, so unbounded fan-out over a 500-server
    fleet would replace a slow job with a resource storm."""
    gate = threading.Event()
    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()

    def _do(host):
        with lock:
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
        time.sleep(0.02)
        with lock:
            peak["cur"] -= 1

    result = fleet_walk.walk(_fleet(30), _do, workers=4, label="t")
    assert result.max_in_flight <= 4
    assert peak["n"] <= 4, "more work was in flight than workers were allowed"
    gate.set()


def test_one_worker_is_a_plain_serial_walk():
    """The documented way to rule the pool out as a cause of a problem, so it
    has to be genuinely serial — not a pool of one that resembles it."""
    order = []
    result = fleet_walk.walk(_fleet(6), lambda h: order.append(h.name),
                             workers=1, label="t")
    assert order == [h.name for h in _fleet(6)]
    assert result.max_in_flight == 1


def test_a_wall_clock_sanity_check_on_the_overlap():
    """The one duration assertion, with a wide margin.

    A concurrency counter alone cannot catch a walk that dispatches
    concurrently and then serialises on a shared lock inside `fn` — several
    tasks are genuinely in flight while only one progresses. Ten items of 100ms
    at five workers is ~0.2s overlapped and ~1.0s serial; asserting under 0.7s
    distinguishes them without being a timing race.
    """
    result = fleet_walk.walk(_fleet(10), lambda h: time.sleep(0.1),
                             workers=5, label="t")
    assert result.elapsed_s < 0.7, (
        f"10 x 100ms at 5 workers took {result.elapsed_s}s — that is serial")


# ── isolation: the property a futures rewrite loses first ────────────────

def test_one_failing_item_does_not_stop_the_pass():
    """The serial loops were careful about this and a naive futures rewrite
    drops it, because an exception inside a future is silent until somebody
    reads its result — and `pool.map` only raises when iterated."""
    visited = []
    lock = threading.Lock()

    def _do(host):
        with lock:
            visited.append(host.name)
        if host.name == "SRV02":
            raise RuntimeError("this host is broken")

    result = fleet_walk.walk(_fleet(6), _do, workers=3, label="t")
    assert len(visited) == 6, "the pass stopped at the broken host"
    assert result.failed == 1 and result.ok == 5


def test_every_item_can_fail_without_raising():
    result = fleet_walk.walk(_fleet(4),
                             lambda h: (_ for _ in ()).throw(RuntimeError("x")),
                             workers=2, label="t")
    assert result.failed == 4 and result.ok == 0


# ── the budget warning ────────────────────────────────────────────────────

def test_exceeding_the_cadence_is_logged_as_a_warning(caplog):
    """A job quietly taking longer than its own cadence looks exactly like a
    healthy job. That invisibility is half of what made the finding a finding."""
    with caplog.at_level("WARNING"):
        fleet_walk.walk(_fleet(2), lambda h: time.sleep(0.05), workers=1,
                        label="slowjob", budget_s=0.01)
    assert any("slowjob" in r.message or "slowjob" in r.getMessage()
               for r in caplog.records), caplog.text
    assert any("cadence" in r.getMessage() for r in caplog.records)


def test_staying_inside_the_budget_logs_no_warning(caplog):
    with caplog.at_level("WARNING"):
        fleet_walk.walk(_fleet(2), lambda h: None, workers=2,
                        label="fastjob", budget_s=30)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ── the setting ───────────────────────────────────────────────────────────

def test_the_worker_count_comes_from_settings():
    assert fleet_walk.worker_count({"collector_v2_periodic_workers": 16}) == 16


def test_the_default_applies_when_unset():
    assert fleet_walk.worker_count({}) == fleet_walk.DEFAULT_WORKERS
    assert fleet_walk.worker_count(None) == fleet_walk.DEFAULT_WORKERS


def test_a_zero_worker_count_degrades_to_serial_not_to_nothing():
    """Zero threads would mean the job never runs. Flooring at one makes a
    misconfiguration slow instead of silent, which is the only safe direction
    for a monitoring job."""
    assert fleet_walk.worker_count({"collector_v2_periodic_workers": 0}) == 1
    assert fleet_walk.worker_count({"collector_v2_periodic_workers": -4}) == 1


def test_an_unreadable_worker_count_falls_back_to_the_default():
    assert fleet_walk.worker_count(
        {"collector_v2_periodic_workers": "many"}) == fleet_walk.DEFAULT_WORKERS


def test_the_shipped_config_default_matches_the_module_s():
    """One number, two files. A config block that says 8 while the code uses a
    different number is worse than no config block at all."""
    from config_manager import ConfigManager
    assert (ConfigManager._DEFAULT_SETTINGS["collector_v2_periodic_workers"]
            == fleet_walk.DEFAULT_WORKERS)


def test_every_converted_walk_reads_the_setting():
    """The seam, asserted over the call sites. A helper that exists and is not
    called is this repo's most-repeated failure — and here it would be worse
    than useless, because the serial loop it replaced still looks fine."""
    import ast
    from pathlib import Path
    for rel in ("drift.py", "healthchecks.py", "tls_monitor.py",
                "failed_logins.py", "collector_v2/periodics.py"):
        tree = ast.parse((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "walk"
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "fleet_walk"]
        assert calls, f"{rel} does not call fleet_walk.walk"
        kwargs = {k.arg for c in calls for k in c.keywords}
        assert "workers" in kwargs, f"{rel} calls walk without a worker count"


def test_no_converted_module_still_walks_its_fleet_serially():
    """The other half: the `for` loop has to be GONE, not merely joined by a
    pool. A conversion that leaves both in place runs the fleet twice."""
    import ast
    from pathlib import Path
    targets = {
        "drift.py": "_collect_drift_snapshots",
        "healthchecks.py": "_run_health_checks",
        "tls_monitor.py": "_check_tls_certificates",
        "failed_logins.py": "_collect_all_failed_logins",
    }
    for rel, fname in targets.items():
        tree = ast.parse((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        top_level_loops = [n for n in fn.body if isinstance(n, ast.For)]
        assert not top_level_loops, (
            f"{rel}::{fname} still walks its list with a top-level for loop")


def test_more_workers_than_items_does_not_spawn_idle_threads():
    result = fleet_walk.walk(_fleet(2), lambda h: None, workers=64, label="t")
    assert result.max_in_flight <= 2
