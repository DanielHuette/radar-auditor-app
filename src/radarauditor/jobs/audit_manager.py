import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from radarauditor.config import Settings
from radarauditor.db.models import AuditStatus, JobKind, JobStatus
from radarauditor.db.repositories import Repositories
from radarauditor.errors import ClaudeCodeError, RadarError
from radarauditor.feedback.events import Result
from radarauditor.jobs.analysis import retry_at
from radarauditor.jobs.job_signal import Opener, watch
from radarauditor.jobs.notify import Notifier

logger = logging.getLogger(__name__)

AUDIT_KINDS = (
    JobKind.COLLECT,
    JobKind.REPAIR,
    JobKind.SCORE,
    JobKind.ANALYSE,
    JobKind.REPORT,
    JobKind.SERP_RETRY,
)
MAX_UNFINISHED_RUNS = 3
UNFINISHED_PAUSE = timedelta(seconds=30)
QUEUE_RETRY_SECONDS = 30.0
QUEUE_CHECK_SECONDS = 60.0
CONCLUSION_LABEL = "Audit-Manager: Ergebnis"
NOTE_LABEL = "Audit-Manager"
INTERRUPTED_ERROR = "Der Audit-Manager wurde während des Auftrags beendet"
NO_CONCLUSION_REASON = "Der Audit-Manager ist beendet, ohne sein Ergebnis zu schreiben"

type Launcher = Callable[[UUID, Settings], Awaitable[None]]


class AuditManager:
    def __init__(self, repositories: Repositories, settings: Settings, launch: Launcher) -> None:
        self._repositories = repositories
        self._settings = settings
        self._launch = launch
        self._notifier = Notifier(repositories, settings)
        self._not_before: dict[UUID, datetime] = {}
        self._launch_failures: dict[UUID, int] = {}
        self._unfinished_runs: dict[UUID, int] = {}

    def wait_seconds(self, now: datetime) -> float | None:
        moments = list(self._not_before.values())
        deferred = self._repositories.next_job_due(AUDIT_KINDS, now)
        if deferred is not None:
            moments.append(deferred)
        later = [moment for moment in moments if moment > now]
        if not later:
            return None
        return max((min(later) - now).total_seconds(), 0.0)

    async def run_due(self, stop: asyncio.Event) -> None:
        now = datetime.now(UTC)
        for audit_id in self._repositories.due_audit_ids(AUDIT_KINDS, now):
            if stop.is_set():
                return
            if self._not_before.get(audit_id, now) > now:
                continue
            await self._run_audit(audit_id)

    async def _run_audit(self, audit_id: UUID) -> None:
        started = datetime.now(UTC)
        logger.info("Starting the audit manager for audit %s", audit_id)
        try:
            await self._launch(audit_id, self._settings)
        except ClaudeCodeError as error:
            self._launch_failed(audit_id, error)
            return
        finally:
            self._repositories.requeue_running_jobs_of_audit(audit_id, INTERRUPTED_ERROR)
        self._launch_failures.pop(audit_id, None)
        self._not_before.pop(audit_id, None)
        self._check_conclusion(audit_id, started)
        if audit_id in self._repositories.due_audit_ids(AUDIT_KINDS, datetime.now(UTC)):
            self._count_unfinished_run(audit_id)
        else:
            self._unfinished_runs.pop(audit_id, None)

    def _launch_failed(self, audit_id: UUID, error: ClaudeCodeError) -> None:
        failures = self._launch_failures.get(audit_id, 0) + 1
        self._launch_failures[audit_id] = failures
        now = datetime.now(UTC)
        self._not_before[audit_id] = retry_at(failures, now)
        logger.warning("Audit manager of audit %s did not run: %s", audit_id, error)
        self._write_event(
            audit_id,
            Result.FAILED,
            NOTE_LABEL,
            f"Der Audit-Manager konnte nicht laufen ({error}); neuer Versuch um "
            f"{self._not_before[audit_id]:%H:%M} Uhr UTC",
        )

    def _check_conclusion(self, audit_id: UUID, started: datetime) -> None:
        try:
            rows = self._repositories.audit_event_rows(audit_id)
        except RadarError as error:
            logger.error("Events of audit %s could not be read: %s", audit_id, error)
            return
        conclusions = [
            row
            for row in rows
            if row["label"] == CONCLUSION_LABEL
            and datetime.fromisoformat(str(row["occurred_at"])) >= started - timedelta(seconds=1)
        ]
        if not conclusions:
            self._write_event(audit_id, Result.FAILED, CONCLUSION_LABEL, NO_CONCLUSION_REASON)
            self._notifier.audit_concluded(audit_id, Result.FAILED, NO_CONCLUSION_REASON)
            return
        newest = conclusions[-1]
        self._notifier.audit_concluded(audit_id, Result(newest["result"]), newest.get("reason"))

    def _count_unfinished_run(self, audit_id: UUID) -> None:
        runs = self._unfinished_runs.get(audit_id, 0) + 1
        self._unfinished_runs[audit_id] = runs
        if runs < MAX_UNFINISHED_RUNS or self._repairing(audit_id):
            logger.warning("Audit %s still has due jobs after manager run %d", audit_id, runs)
            self._not_before[audit_id] = datetime.now(UTC) + UNFINISHED_PAUSE
            return
        self._unfinished_runs.pop(audit_id, None)
        self._give_up(audit_id)

    def _repairing(self, audit_id: UUID) -> bool:
        return any(
            job.kind is JobKind.REPAIR and job.status is JobStatus.QUEUED
            for job in self._repositories.jobs_of_audit(audit_id)
        )

    def _give_up(self, audit_id: UUID) -> None:
        reason = (
            f"Der Audit-Manager hat den Audit in {MAX_UNFINISHED_RUNS} Läufen nicht zu Ende "
            "gebracht; die offenen Aufträge sind abgebrochen"
        )
        logger.error("Audit %s: %s", audit_id, reason)
        try:
            for job in self._repositories.jobs_of_audit(audit_id):
                if job.status is JobStatus.QUEUED and job.kind in AUDIT_KINDS:
                    self._repositories.finish_job(job.id, JobStatus.FAILED, reason)
            self._repositories.set_audit_status(audit_id, AuditStatus.FAILED, reason)
        except RadarError as error:
            logger.error("Giving up audit %s could not be recorded: %s", audit_id, error)
        self._write_event(audit_id, Result.FAILED, CONCLUSION_LABEL, reason)
        self._notifier.audit_concluded(audit_id, Result.FAILED, reason)

    def _write_event(self, audit_id: UUID, result: Result, label: str, reason: str) -> None:
        try:
            self._repositories.add_manager_event(audit_id, result, label, reason)
        except RadarError as error:
            logger.error("Manager event of audit %s could not be written: %s", audit_id, error)


def next_check_seconds(wait: float | None) -> float:
    return QUEUE_CHECK_SECONDS if wait is None else min(wait, QUEUE_CHECK_SECONDS)


async def _sleep_until(wake: asyncio.Event, stop: asyncio.Event, seconds: float | None) -> None:
    waiting = {asyncio.create_task(wake.wait()), asyncio.create_task(stop.wait())}
    try:
        await asyncio.wait(waiting, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiting:
            task.cancel()
        for task in waiting:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def run_audit_manager(
    repositories: Repositories,
    settings: Settings,
    stop: asyncio.Event,
    open_subscription: Opener,
    launch: Launcher,
) -> None:
    wake = asyncio.Event()
    watcher = asyncio.create_task(watch(open_subscription, wake.set, wake.set, stop))
    manager = AuditManager(repositories, settings, launch)
    try:
        while not stop.is_set():
            wake.clear()
            try:
                await manager.run_due(stop)
                seconds = manager.wait_seconds(datetime.now(UTC))
            except Exception:
                logger.exception("The audit manager loop failed, trying again")
                seconds = QUEUE_RETRY_SECONDS
            await _sleep_until(wake, stop, next_check_seconds(seconds))
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
