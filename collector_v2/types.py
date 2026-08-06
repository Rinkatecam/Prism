"""Data model for the v2 collector. Shared by supervisor / workers /
aggregator. All three components only depend on these types — they do not
import each other directly.

Design rules:
  * Dataclasses, not dicts — typing helps catch wiring bugs at write time.
  * `frozen=False` because Result will be enriched as it flows through the
    aggregator (e.g., status decision, transition flags).
  * All timestamps are timezone-aware UTC datetimes. The DB stores ISO
    strings; conversion at the boundary.
  * CheckType is an Enum to prevent typos. Adding a new check is: add an
    enum member + a function in checks.py + a `next_<x>_at` field in
    ServerHealth + a case in supervisor.py and workers.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any


class CheckType(str, Enum):
    """The kinds of work the collector dispatches per server.

    Inheriting from str so the enum value is JSON-serializable and
    log-friendly without manual conversion.
    """

    METRICS = "metrics"        # CPU/RAM/disk/status — runs every poll_interval
    LOGS = "logs"              # Event-log scrape — every log_collection_interval_minutes
    UPDATES = "updates"        # Windows Update query — every update_check_interval_minutes
    HARDWARE = "hardware"      # Win32 CIM hardware info — every 60 min by default


# Per-check default deadlines. These are MAX time a worker will wait for
# the WinRM call to return before treating the check as failed. They are
# DELIBERATELY generous because WU calls on slow servers can take 30-40 s
# legitimately. Workers measure wall-clock from item.enqueued_at; if the
# item sat in the queue too long, we drop it (supervisor will reschedule).
DEFAULT_DEADLINES_S: dict[CheckType, int] = {
    CheckType.METRICS: 30,
    CheckType.LOGS: 60,
    CheckType.UPDATES: 120,
    CheckType.HARDWARE: 30,
}


# Per-check default intervals — how often this check runs PER SERVER.
# These get OVERRIDDEN by settings['poll_interval_seconds'] for metrics
# and the various *_interval_minutes settings for the others. Defaults
# below are the fallback when settings are missing or invalid.
DEFAULT_INTERVALS_S: dict[CheckType, int] = {
    CheckType.METRICS: 60,        # poll_interval_seconds
    CheckType.LOGS: 300,          # log_collection_interval_minutes * 60
    CheckType.UPDATES: 1800,      # update_check_interval_minutes * 60
    CheckType.HARDWARE: 3600,     # 60 min — hardcoded; rarely changes
}


@dataclass
class WorkItem:
    """One unit of work in the supervisor → worker queue.

    A WorkItem says: "Run this check_type against this server. If you can't
    start within max_queue_wait_s, drop me (the supervisor will reschedule).
    If you can't finish within deadline_s, abort and report failure."
    """

    server_name: str
    check_type: CheckType
    enqueued_at: datetime               # When supervisor put this on the queue
    deadline_s: int                     # Max time the WinRM call may take
    max_queue_wait_s: int = 60          # Drop if this long in queue
    reason: str = "schedule"            # "schedule" | "force_sync" | "accelerated" | "retry"
    attempt: int = 1                    # 1 = first try, 2 = retry, etc.

    @property
    def age_in_queue(self) -> timedelta:
        return datetime.now(timezone.utc) - self.enqueued_at

    @property
    def is_stale(self) -> bool:
        """True if this item has been in the queue past its max_queue_wait_s."""
        return self.age_in_queue.total_seconds() > self.max_queue_wait_s

    def __repr__(self) -> str:
        return (
            f"WorkItem({self.server_name} {self.check_type.value} "
            f"attempt={self.attempt} aged={self.age_in_queue.total_seconds():.1f}s)"
        )


@dataclass
class Result:
    """One unit of work returning from worker → aggregator queue.

    A Result is the full record of one check attempt: what was tried, what
    came back, how long it took, whether it succeeded. The aggregator uses
    this to (a) persist data, (b) update server health, (c) decide on
    alerts/transitions, (d) feed the supervisor's per-server bookkeeping.
    """

    item: WorkItem                      # The item this Result is for
    started_at: datetime
    finished_at: datetime
    ok: bool                            # True if the check yielded valid data
    data: dict[str, Any] | None = None  # Parsed result (e.g. metrics dict, log list)
    error: str | None = None            # Truncated error string if !ok
    error_kind: str | None = None       # "offline" | "timeout" | "winrm" | "ps" | "parse" | "exception"

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def __repr__(self) -> str:
        status = "ok" if self.ok else f"FAIL({self.error_kind})"
        return (
            f"Result({self.item.server_name} {self.item.check_type.value} "
            f"{status} {self.duration_s:.1f}s)"
        )


@dataclass
class CheckState:
    """Per-(server, check_type) scheduling state.

    Stored inside ``ServerHealth.checks`` keyed by ``CheckType``. Replaces
    the previous per-check fields (``next_metrics_at``, ``last_logs_ok_at``,
    etc.) which required updating four switch statements every time a new
    check was added.

    Backwards compatibility: ``ServerHealth.next_metrics_at`` and friends
    are exposed as ``@property`` proxies so callers that read them
    directly (legacy tests, the v1 compat shim) keep working.
    """

    next_due_at: datetime
    last_ok_at: datetime | None = None
    consecutive_failures: int = 0
    pending: bool = False


@dataclass
class ServerHealth:
    """Per-server bookkeeping owned by the Supervisor.

    Tracks when each check is next due, how many consecutive failures the
    server has had per check type (for backoff), and whether the server
    is currently in accelerated mode.

    The aggregator updates this struct on every Result it processes (via
    state.mark_check_completed). The supervisor reads it on every tick to
    decide what to enqueue.

    Storage shape (audit M2 refactor): per-check state lives in a single
    ``checks: dict[CheckType, CheckState]``. Adding a new check type now
    requires only adding the enum member — no edits to switch statements
    in this class. The old per-check fields
    (``next_metrics_at``, ``last_logs_ok_at`` …) remain as ``@property``
    proxies so external readers see no change.
    """

    name: str

    # Per-check state — keyed by CheckType, so adding a new check is a
    # one-line enum edit, not a dataclass surgery.
    checks: dict[CheckType, CheckState] = field(default_factory=dict)

    # Acceleration — when set, supervisor enqueues ALL checks every tick
    # until this passes. Acceleration is "right now I want fresh data for
    # this server."
    accelerated_until: datetime | None = None
    accelerated_reason: str = ""

    # ── Compatibility proxies for the old per-check field names ─────────
    # External code that read `health.next_metrics_at` directly keeps
    # working. The properties resolve to `self.checks[METRICS].next_due_at`.

    @property
    def next_metrics_at(self) -> datetime:
        return self.checks[CheckType.METRICS].next_due_at

    @next_metrics_at.setter
    def next_metrics_at(self, value: datetime) -> None:
        self._ensure_check(CheckType.METRICS, value).next_due_at = value

    @property
    def next_logs_at(self) -> datetime:
        return self.checks[CheckType.LOGS].next_due_at

    @next_logs_at.setter
    def next_logs_at(self, value: datetime) -> None:
        self._ensure_check(CheckType.LOGS, value).next_due_at = value

    @property
    def next_updates_at(self) -> datetime:
        return self.checks[CheckType.UPDATES].next_due_at

    @next_updates_at.setter
    def next_updates_at(self, value: datetime) -> None:
        self._ensure_check(CheckType.UPDATES, value).next_due_at = value

    @property
    def next_hardware_at(self) -> datetime:
        return self.checks[CheckType.HARDWARE].next_due_at

    @next_hardware_at.setter
    def next_hardware_at(self, value: datetime) -> None:
        self._ensure_check(CheckType.HARDWARE, value).next_due_at = value

    @property
    def last_metrics_ok_at(self) -> datetime | None:
        return self.checks.get(CheckType.METRICS, CheckState(next_due_at=datetime.now(timezone.utc))).last_ok_at if CheckType.METRICS in self.checks else None

    @property
    def last_logs_ok_at(self) -> datetime | None:
        cs = self.checks.get(CheckType.LOGS)
        return cs.last_ok_at if cs else None

    @property
    def last_updates_ok_at(self) -> datetime | None:
        cs = self.checks.get(CheckType.UPDATES)
        return cs.last_ok_at if cs else None

    @property
    def last_hardware_ok_at(self) -> datetime | None:
        cs = self.checks.get(CheckType.HARDWARE)
        return cs.last_ok_at if cs else None

    @property
    def consecutive_failures(self) -> dict[CheckType, int]:
        """Read-as-dict view, for back-compat with old callers."""
        return _FailuresView(self.checks)

    @consecutive_failures.setter
    def consecutive_failures(self, value: dict[CheckType, int]) -> None:
        """Bulk-set per-check failure counts. Used by tests that need to
        prime the backoff state. Each entry creates (or updates) the
        corresponding CheckState."""
        for ct, count in (value or {}).items():
            cs = self._ensure_check(ct, datetime.now(timezone.utc))
            cs.consecutive_failures = count

    @property
    def pending(self) -> dict[CheckType, bool]:
        """Read-as-dict view, for back-compat with old callers."""
        return _PendingView(self.checks)

    @pending.setter
    def pending(self, value: dict[CheckType, bool]) -> None:
        """Bulk-set pending flags. Mainly used by tests that pre-populate
        a server as 'check X is in flight' to verify the supervisor
        skips re-enqueueing."""
        for ct, flag in (value or {}).items():
            cs = self._ensure_check(ct, datetime.now(timezone.utc))
            cs.pending = bool(flag)

    # ── Core API ────────────────────────────────────────────────────────

    def _ensure_check(self, ct: CheckType, when: datetime) -> CheckState:
        """Get or create the CheckState entry — keeps the proxy setters
        cheap when the entry doesn't exist yet (e.g. brand new server)."""
        if ct not in self.checks:
            self.checks[ct] = CheckState(next_due_at=when)
        return self.checks[ct]

    def next_due_for(self, ct: CheckType) -> datetime:
        return self.checks[ct].next_due_at

    def set_next_due_for(self, ct: CheckType, when: datetime) -> None:
        self._ensure_check(ct, when).next_due_at = when

    def is_accelerated(self) -> bool:
        if self.accelerated_until is None:
            return False
        return datetime.now(timezone.utc) < self.accelerated_until

    def record_failure(self, ct: CheckType) -> int:
        """Increment + return the consecutive-failures counter for this check."""
        cs = self._ensure_check(ct, datetime.now(timezone.utc))
        cs.consecutive_failures += 1
        return cs.consecutive_failures

    def record_success(self, ct: CheckType, when: datetime) -> None:
        """Reset failure counter + stamp last-OK time."""
        cs = self._ensure_check(ct, when)
        cs.consecutive_failures = 0
        cs.last_ok_at = when


class _FailuresView:
    """Dict-like view of consecutive_failures per check, backed by the
    CheckState entries. Read-only via __getitem__ + .get; writes are the
    job of record_failure / record_success. Exists for backwards
    compatibility with callers like ``backoff_delay_s(health.consecutive_failures[ct])``.
    """

    def __init__(self, checks: dict[CheckType, CheckState]) -> None:
        self._checks = checks

    def __getitem__(self, ct: CheckType) -> int:
        cs = self._checks.get(ct)
        return cs.consecutive_failures if cs else 0

    def get(self, ct: CheckType, default: int = 0) -> int:
        cs = self._checks.get(ct)
        return cs.consecutive_failures if cs else default

    def __contains__(self, ct: CheckType) -> bool:
        cs = self._checks.get(ct)
        return cs is not None and cs.consecutive_failures > 0

    def __setitem__(self, ct: CheckType, value: int) -> None:
        # Used by tests that pre-populate failure counts. Write-through.
        if ct in self._checks:
            self._checks[ct].consecutive_failures = value


class _PendingView:
    """Dict-like view of pending flags per check, backed by CheckState.
    Read AND write — the supervisor sets pending[ct]=True on enqueue,
    the aggregator sets pending[ct]=False on Result. Mirrors the
    original ``dict[CheckType, bool]`` shape so the call sites need no
    changes."""

    def __init__(self, checks: dict[CheckType, CheckState]) -> None:
        self._checks = checks

    def __getitem__(self, ct: CheckType) -> bool:
        cs = self._checks.get(ct)
        return cs.pending if cs else False

    def get(self, ct: CheckType, default: bool = False) -> bool:
        cs = self._checks.get(ct)
        return cs.pending if cs else default

    def __setitem__(self, ct: CheckType, value: bool) -> None:
        if ct not in self._checks:
            # Setter MUST work even before _ensure_check has been called
            # (the supervisor sets pending[METRICS]=True immediately on
            # the first enqueue for a brand-new server). Use a sentinel
            # next_due_at; the supervisor will overwrite it on the next
            # tick.
            self._checks[ct] = CheckState(next_due_at=datetime.now(timezone.utc))
        self._checks[ct].pending = value

    def __contains__(self, ct: CheckType) -> bool:
        return ct in self._checks


def backoff_delay_s(consecutive_failures: int, base_s: int = 60, cap_s: int = 3600) -> int:
    """Exponential backoff with cap.

    failures = 1 → base (no extra backoff yet, normal next-due)
    failures = 2 → 2 × base
    failures = 3 → 4 × base
    failures = 4 → 8 × base
    failures = N → 2^(N-1) × base, capped at cap_s

    Default base=60s, cap=1h. So a server that's been failing for an hour
    gets retried hourly, not every minute.
    """
    if consecutive_failures <= 1:
        return base_s
    return min(cap_s, base_s * (2 ** (consecutive_failures - 1)))
