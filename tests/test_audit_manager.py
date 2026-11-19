import asyncio
import functools
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from radarauditor.config import Settings
from radarauditor.db.models import AuditStatus, Job, JobKind, JobStatus
from radarauditor.db.repositories import Repositories
from radarauditor.errors import ClaudeCodeError, ClaudeCodeFailure, StorageError
from radarauditor.feedback.events import Result
from radarauditor.jobs import audit_manager, job_signal
from radarauditor.jobs.audit_manager import (
    AUDIT_KINDS,
    CONCLUSION_LABEL,
    MAX_UNFINISHED_RUNS,
    NO_CONCLUSION_REASON,
    NOTE_LABEL,
    QUEUE_CHECK_SECONDS,
    AuditManager,
    next_check_seconds,
    run_audit_manager,
)
from radarauditor.jobs.notify import Notifier

AUDIT = UUID("00000000-0000-4000-8000-0000000000a1")
OTHER = UUID("00000000-0000-4000-8000-0000000000a2")


class FakeRepositories:
    def __init__(self) -> None:
        self.due: list[UUID] = []
        self.deferred: datetime | None = None
        self.events: list[tuple[UUID, Result, str, str | None]] = []
        self.event_rows: list[dict[str, Any]] = []
        self.requeued: list[UUID] = []
        self.jobs: list[Job] = []
        self.finished: list[tuple[UUID, JobStatus, str | None]] = []
        self.statuses: list[tuple[UUID, AuditStatus, str | None]] = []
        self.event_failure: StorageError | None = None
        self.read_failure: StorageError | None = None
        self.due_failures: list[StorageError] = []

    def due_audit_ids(self, kinds: Sequence[JobKind], now: datetime) -> list[UUID]:
        assert tuple(kinds) == AUDIT_KINDS
        if self.due_failures:
            raise self.due_failures.pop(0)
        if self.deferred is not None and now >= self.deferred:
            self.deferred = None
            self.due = [AUDIT]
        return list(self.due)

    def next_job_due(self, kinds: Sequence[JobKind], now: datetime) -> datetime | None:
        return self.deferred

    def requeue_running_jobs_of_audit(self, audit_id: UUID, error: str) -> int:
        self.requeued.append(audit_id)
        return 0

    def add_manager_event(
        self, audit_id: UUID, result: Result, label: str, reason: str | None
    ) -> None:
        if self.event_failure is not None:
            raise self.event_failure
        self.events.append((audit_id, result, label, reason))

    def audit_event_rows(self, audit_id: UUID) -> list[dict[str, Any]]:
        if self.read_failure is not None:
            raise self.read_failure
        return list(self.event_rows)

    def jobs_of_audit(self, audit_id: UUID) -> list[Job]:
        return list(self.jobs)

    def finish_job(self, job_id: UUID, status: JobStatus, error: str | None = None) -> None:
        self.finished.append((job_id, status, error))

    def set_audit_status(
        self, audit_id: UUID, status: AuditStatus, error: str | None = None
    ) -> None:
        self.statuses.append((audit_id, status, error))


def _job(kind: JobKind, status: JobStatus) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        kind=kind,
        status=status,
        audit_id=AUDIT,
        customer_id=uuid4(),
        attempts=0,
        error=None,
        created_at=now,
        updated_at=now,
    )


def _concluded(started_after: datetime | None = None) -> dict[str, Any]:
    when = (started_after or datetime.now(UTC)) + timedelta(seconds=5)
    return {
        "label": CONCLUSION_LABEL,
        "occurred_at": when.isoformat(),
        "result": Result.MEASURED.value,
        "reason": "Alles gemessen",
    }


def _manager(
    repositories: FakeRepositories, settings: Settings, launch: Callable[..., Any]
) -> AuditManager:
    return AuditManager(cast(Repositories, repositories), settings, launch)


class Launches:
    def __init__(self, repositories: FakeRepositories, finish: bool = True) -> None:
        self.repositories = repositories
        self.finish = finish
        self.calls: list[UUID] = []
        self.failure: ClaudeCodeError | None = None
        self.conclude = True

    async def __call__(self, audit_id: UUID, settings: Settings) -> None:
        self.calls.append(audit_id)
        if self.failure is not None:
            raise self.failure
        if self.conclude:
            self.repositories.event_rows = [_concluded()]
        if self.finish and audit_id in self.repositories.due:
            self.repositories.due.remove(audit_id)


async def test_run_due_starts_the_manager_of_every_due_audit_one_after_the_other(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT, OTHER]
    launches = Launches(repositories)

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert launches.calls == [AUDIT, OTHER]


async def test_run_due_starts_nothing_without_due_audits(settings: Settings) -> None:
    repositories = FakeRepositories()
    launches = Launches(repositories)

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert launches.calls == []


async def test_run_due_stops_between_audits_when_asked_to_stop(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT, OTHER]
    stop = asyncio.Event()
    launches = Launches(repositories)

    async def launch_and_stop(audit_id: UUID, settings: Settings) -> None:
        await launches(audit_id, settings)
        stop.set()

    await _manager(repositories, settings, launch_and_stop).run_due(stop)

    assert launches.calls == [AUDIT]


async def test_jobs_cut_off_by_the_end_of_the_manager_go_back_into_the_queue(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]

    await _manager(repositories, settings, Launches(repositories)).run_due(asyncio.Event())

    assert repositories.requeued == [AUDIT]


async def test_a_manager_that_could_not_start_is_reported_and_waits_before_the_next_try(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.failure = ClaudeCodeError("not logged in", ClaudeCodeFailure.NOT_LOGGED_IN)
    manager = _manager(repositories, settings, launches)

    await manager.run_due(asyncio.Event())
    await manager.run_due(asyncio.Event())

    assert launches.calls == [AUDIT]
    assert repositories.requeued == [AUDIT]
    ((audit_id, result, label, reason),) = repositories.events
    assert (audit_id, result, label) == (AUDIT, Result.FAILED, NOTE_LABEL)
    assert reason is not None
    assert "not logged in" in reason
    seconds = manager.wait_seconds(datetime.now(UTC))
    assert seconds is not None
    assert 0 < seconds <= 600


async def test_the_pause_after_a_failed_start_grows_with_every_failure(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.failure = ClaudeCodeError("down")
    manager = _manager(repositories, settings, launches)
    waits: list[float] = []

    for _ in range(3):
        await manager.run_due(asyncio.Event())
        seconds = manager.wait_seconds(datetime.now(UTC))
        assert seconds is not None
        waits.append(seconds)
        manager._not_before.clear()

    assert waits[0] < waits[1] < waits[2]
    assert launches.calls == [AUDIT, AUDIT, AUDIT]


async def test_a_manager_that_ran_again_after_a_failure_starts_the_count_afresh(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.failure = ClaudeCodeError("down")
    manager = _manager(repositories, settings, launches)
    await manager.run_due(asyncio.Event())
    manager._not_before.clear()
    launches.failure = None

    await manager.run_due(asyncio.Event())

    assert manager.wait_seconds(datetime.now(UTC)) is None
    assert manager._launch_failures == {}


async def test_a_manager_that_ends_without_a_verdict_is_reported(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert repositories.events == [(AUDIT, Result.FAILED, CONCLUSION_LABEL, NO_CONCLUSION_REASON)]


async def test_a_manager_that_wrote_its_verdict_adds_no_report(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]

    await _manager(repositories, settings, Launches(repositories)).run_due(asyncio.Event())

    assert repositories.events == []


async def test_a_verdict_of_an_earlier_run_does_not_count(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False
    repositories.event_rows = [
        {
            "label": CONCLUSION_LABEL,
            "occurred_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }
    ]

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert repositories.events == [(AUDIT, Result.FAILED, CONCLUSION_LABEL, NO_CONCLUSION_REASON)]


async def test_other_events_are_no_verdict(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False
    repositories.event_rows = [
        {"label": NOTE_LABEL, "occurred_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat()}
    ]

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert [event[3] for event in repositories.events] == [NO_CONCLUSION_REASON]


async def test_unreadable_events_do_not_hide_the_run(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    repositories.read_failure = StorageError("no table")
    launches = Launches(repositories)
    launches.conclude = False

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert launches.calls == [AUDIT]
    assert repositories.events == []


async def test_an_event_that_cannot_be_written_does_not_stop_the_manager(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT, OTHER]
    repositories.event_failure = StorageError("no table")
    launches = Launches(repositories)
    launches.conclude = False

    await _manager(repositories, settings, launches).run_due(asyncio.Event())

    assert launches.calls == [AUDIT, OTHER]


async def test_an_audit_with_due_jobs_after_a_run_is_tried_again_after_a_pause(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories, finish=False)
    manager = _manager(repositories, settings, launches)

    await manager.run_due(asyncio.Event())
    await manager.run_due(asyncio.Event())

    assert launches.calls == [AUDIT]
    seconds = manager.wait_seconds(datetime.now(UTC))
    assert seconds is not None
    assert 0 < seconds <= audit_manager.UNFINISHED_PAUSE.total_seconds()


async def test_an_audit_the_manager_cannot_finish_is_given_up_after_the_last_run(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    repositories.jobs = [
        _job(JobKind.SCORE, JobStatus.QUEUED),
        _job(JobKind.REPORT, JobStatus.DONE),
        _job(JobKind.OPTIMIZE, JobStatus.QUEUED),
    ]
    launches = Launches(repositories, finish=False)
    manager = _manager(repositories, settings, launches)

    for run in range(MAX_UNFINISHED_RUNS):
        assert len(launches.calls) == run
        await manager.run_due(asyncio.Event())
        manager._not_before.clear()

    assert len(launches.calls) == MAX_UNFINISHED_RUNS
    assert [(status, job_id) for job_id, status, _ in repositories.finished] == [
        (JobStatus.FAILED, repositories.jobs[0].id)
    ]
    ((_, audit_status, reason),) = repositories.statuses
    assert audit_status is AuditStatus.FAILED
    assert reason is not None
    ((_, result, label, event_reason),) = repositories.events
    assert (result, label, event_reason) == (Result.FAILED, CONCLUSION_LABEL, reason)


async def test_a_run_that_finished_the_audit_clears_the_count_of_unfinished_runs(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories, finish=False)
    manager = _manager(repositories, settings, launches)
    await manager.run_due(asyncio.Event())
    manager._not_before.clear()
    launches.finish = True

    await manager.run_due(asyncio.Event())

    assert manager._unfinished_runs == {}


def test_wait_seconds_is_open_without_deferred_jobs_and_pauses(settings: Settings) -> None:
    repositories = FakeRepositories()

    assert (
        _manager(repositories, settings, Launches(repositories)).wait_seconds(datetime.now(UTC))
        is None
    )


def test_wait_seconds_runs_to_the_next_deferred_job(settings: Settings) -> None:
    repositories = FakeRepositories()
    now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    repositories.deferred = now + timedelta(minutes=15)

    seconds = _manager(repositories, settings, Launches(repositories)).wait_seconds(now)

    assert seconds == 900


def test_wait_seconds_takes_the_nearest_of_pause_and_deferred_job(settings: Settings) -> None:
    repositories = FakeRepositories()
    now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    repositories.deferred = now + timedelta(minutes=15)
    manager = _manager(repositories, settings, Launches(repositories))
    manager._not_before[AUDIT] = now + timedelta(seconds=30)

    assert manager.wait_seconds(now) == 30


def test_wait_seconds_ignores_moments_that_have_passed(settings: Settings) -> None:
    repositories = FakeRepositories()
    now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    manager = _manager(repositories, settings, Launches(repositories))
    manager._not_before[AUDIT] = now

    assert manager.wait_seconds(now) is None


class SignalStub:
    def __init__(self) -> None:
        self.on_change: Callable[[], None] | None = None
        self.opened = asyncio.Event()

    async def __call__(self, on_change: Callable[[], None]) -> Any:
        self.on_change = on_change
        self.opened.set()
        return Subscription()


class Subscription:
    def alive(self) -> bool:
        return True

    async def close(self) -> None:
        return None


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never became true")


async def test_the_manager_reads_the_queue_at_the_start_and_sleeps_until_a_signal(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    signal = SignalStub()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_audit_manager(cast(Repositories, repositories), settings, stop, signal, launches)
    )
    await _until(lambda: launches.calls == [AUDIT])
    await asyncio.sleep(0.1)
    assert launches.calls == [AUDIT]

    repositories.due = [OTHER]
    assert signal.on_change is not None
    signal.on_change()
    await _until(lambda: launches.calls == [AUDIT, OTHER])

    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_the_manager_wakes_at_the_time_of_a_deferred_job_without_any_signal(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    due_at = datetime.now(UTC) + timedelta(seconds=0.3)
    repositories.deferred = due_at
    launches = Launches(repositories)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_audit_manager(cast(Repositories, repositories), settings, stop, SignalStub(), launches)
    )

    await _until(lambda: launches.calls == [AUDIT])

    assert datetime.now(UTC) >= due_at
    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_the_manager_goes_on_after_a_failing_queue_read(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories = FakeRepositories()
    launches = Launches(repositories)
    monkeypatch.setattr(audit_manager, "QUEUE_RETRY_SECONDS", 0.02)
    repositories.due = [AUDIT]
    repositories.due_failures = [StorageError("down")]
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_audit_manager(cast(Repositories, repositories), settings, stop, SignalStub(), launches)
    )

    await _until(lambda: launches.calls == [AUDIT])

    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_the_manager_ends_when_asked_to_stop_while_it_sleeps(settings: Settings) -> None:
    repositories = FakeRepositories()
    stop = asyncio.Event()
    signal = SignalStub()
    task = asyncio.create_task(
        run_audit_manager(
            cast(Repositories, repositories), settings, stop, signal, Launches(repositories)
        )
    )
    await signal.opened.wait()

    stop.set()

    await asyncio.wait_for(task, timeout=3)


class NoticeSpy:
    def __init__(self) -> None:
        self.concluded: list[tuple[UUID, Result, str | None]] = []

    def audit_concluded(self, audit_id: UUID, verdict: Result, reason: str | None) -> None:
        self.concluded.append((audit_id, verdict, reason))


def _spied(manager: AuditManager) -> NoticeSpy:
    spy = NoticeSpy()
    manager._notifier = cast(Notifier, spy)
    return spy


async def test_the_verdict_of_the_manager_is_announced_with_its_reason(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    manager = _manager(repositories, settings, Launches(repositories))
    spy = _spied(manager)

    await manager.run_due(asyncio.Event())

    assert spy.concluded == [(AUDIT, Result.MEASURED, "Alles gemessen")]


async def test_a_manager_that_ends_without_a_verdict_is_announced_as_failed(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False
    manager = _manager(repositories, settings, launches)
    spy = _spied(manager)

    await manager.run_due(asyncio.Event())

    assert spy.concluded == [(AUDIT, Result.FAILED, NO_CONCLUSION_REASON)]


async def test_a_verdict_of_an_earlier_run_is_not_announced(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False
    repositories.event_rows = [
        {
            **_concluded(),
            "occurred_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "result": Result.SUSPENDED.value,
        }
    ]
    manager = _manager(repositories, settings, launches)
    spy = _spied(manager)

    await manager.run_due(asyncio.Event())

    assert [verdict for _, verdict, _ in spy.concluded] == [Result.FAILED]


async def test_the_newest_verdict_of_a_run_is_the_one_announced(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    launches.conclude = False
    first, second = _concluded(), _concluded()
    first["result"] = Result.SUSPENDED.value
    second["occurred_at"] = (datetime.now(UTC) + timedelta(seconds=9)).isoformat()
    repositories.event_rows = [first, second]
    manager = _manager(repositories, settings, launches)
    spy = _spied(manager)

    await manager.run_due(asyncio.Event())

    assert [verdict for _, verdict, _ in spy.concluded] == [Result.MEASURED]


async def test_an_audit_given_up_is_announced_as_failed(settings: Settings) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories, finish=False)
    manager = _manager(repositories, settings, launches)
    spy = _spied(manager)

    for _ in range(MAX_UNFINISHED_RUNS):
        await manager.run_due(asyncio.Event())
        manager._not_before.clear()

    verdicts = [verdict for _, verdict, _ in spy.concluded]
    assert verdicts == [Result.MEASURED] * MAX_UNFINISHED_RUNS + [Result.FAILED]
    assert spy.concluded[-1][2] == repositories.statuses[0][2]


@pytest.mark.parametrize(
    ("wait", "expected"),
    [
        (None, QUEUE_CHECK_SECONDS),
        (0.0, 0.0),
        (30.0, 30.0),
        (QUEUE_CHECK_SECONDS, QUEUE_CHECK_SECONDS),
        (900.0, QUEUE_CHECK_SECONDS),
    ],
)
def test_the_queue_is_looked_at_least_once_per_check_period(
    wait: float | None, expected: float
) -> None:
    assert next_check_seconds(wait) == expected


class DyingSubscription:
    def __init__(self) -> None:
        self.dead = False

    def alive(self) -> bool:
        return not self.dead

    async def close(self) -> None:
        return None


class ReconnectingSignal:
    def __init__(self) -> None:
        self.subscriptions: list[DyingSubscription] = []

    async def __call__(self, on_change: Callable[[], None]) -> Any:
        subscription = DyingSubscription()
        self.subscriptions.append(subscription)
        return subscription


async def test_an_audit_started_while_the_connection_was_down_is_taken_up_after_the_reconnect(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast_watch = functools.partial(
        job_signal.watch, check_seconds=0.005, first_delay=0.01, longest_delay=0.04
    )
    monkeypatch.setattr(audit_manager, "watch", fast_watch)
    repositories = FakeRepositories()
    repositories.due = [AUDIT]
    launches = Launches(repositories)
    signal = ReconnectingSignal()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_audit_manager(cast(Repositories, repositories), settings, stop, signal, launches)
    )
    await _until(lambda: launches.calls == [AUDIT])

    signal.subscriptions[0].dead = True
    repositories.due = [OTHER]
    await _until(lambda: launches.calls == [AUDIT, OTHER])

    assert len(signal.subscriptions) == 2
    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_an_audit_is_taken_up_even_when_the_connection_never_reports_its_death(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_manager, "QUEUE_CHECK_SECONDS", 0.05)
    repositories = FakeRepositories()
    launches = Launches(repositories)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_audit_manager(cast(Repositories, repositories), settings, stop, SignalStub(), launches)
    )
    await asyncio.sleep(0.02)

    repositories.due = [AUDIT]
    await _until(lambda: launches.calls == [AUDIT])

    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_jobs_of_a_manager_run_that_broke_off_go_back_into_the_queue(
    settings: Settings,
) -> None:
    repositories = FakeRepositories()
    repositories.due = [AUDIT]

    async def broken_off(audit_id: UUID, settings: Settings) -> None:
        raise RuntimeError("the machine went to sleep")

    with pytest.raises(RuntimeError):
        await _manager(repositories, settings, broken_off).run_due(asyncio.Event())

    assert repositories.requeued == [AUDIT]
    assert repositories.due == [AUDIT]
