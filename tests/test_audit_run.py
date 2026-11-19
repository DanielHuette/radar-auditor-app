from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from radarauditor.config import Settings
from radarauditor.db.models import AuditStatus, Job, JobKind, JobStatus
from radarauditor.db.repositories import Repositories
from radarauditor.jobs import audit_run
from radarauditor.jobs.audit_manager import AUDIT_KINDS

AUDIT = UUID("00000000-0000-4000-8000-0000000000a1")


def _job(kind: JobKind, status: JobStatus = JobStatus.QUEUED, error: str | None = None) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        kind=kind,
        status=status,
        audit_id=AUDIT,
        customer_id=uuid4(),
        attempts=1,
        error=error,
        created_at=now,
        updated_at=now,
    )


class FakeRepositories:
    def __init__(self, queue: list[Job]) -> None:
        self.queue = queue
        self.jobs = {job.id: job for job in queue}
        self.claimed_kinds: list[tuple[UUID, tuple[JobKind, ...]]] = []
        self.reset: list[Job] = []
        self.statuses: list[AuditStatus] = []

    def claim_audit_job(self, audit_id: UUID, kinds: tuple[JobKind, ...]) -> Job | None:
        self.claimed_kinds.append((audit_id, tuple(kinds)))
        return self.queue.pop(0) if self.queue else None

    def jobs_of_audit(self, audit_id: UUID) -> list[Job]:
        return list(self.jobs.values())

    def reset_failed_jobs(self, audit_id: UUID, kinds: tuple[JobKind, ...]) -> list[Job]:
        assert kinds == AUDIT_KINDS
        return self.reset

    def set_audit_status(self, audit_id: UUID, status: AuditStatus) -> None:
        self.statuses.append(status)


def _repositories(fake: FakeRepositories) -> Repositories:
    return cast(Repositories, fake)


async def test_every_claimed_job_of_the_audit_runs_in_the_order_of_the_queue(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRepositories([_job(JobKind.COLLECT), _job(JobKind.SCORE), _job(JobKind.REPORT)])
    ran: list[JobKind] = []

    async def process(job: Job, repositories: Repositories, settings: Settings) -> None:
        ran.append(job.kind)
        fake.jobs[job.id] = job.model_copy(update={"status": JobStatus.DONE})

    monkeypatch.setattr(audit_run, "process_job", process)

    steps = await audit_run.run_audit_jobs(AUDIT, _repositories(fake), settings)

    assert ran == [JobKind.COLLECT, JobKind.SCORE, JobKind.REPORT]
    assert [step.kind for step in steps] == ran
    assert [step.status for step in steps] == [JobStatus.DONE] * 3
    assert {call[0] for call in fake.claimed_kinds} == {AUDIT}
    assert {call[1] for call in fake.claimed_kinds} == {AUDIT_KINDS}
    assert len(fake.claimed_kinds) == 4


async def test_an_audit_without_due_jobs_runs_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def process(job: Job, repositories: Repositories, settings: Settings) -> None:
        raise AssertionError("no job is due")

    monkeypatch.setattr(audit_run, "process_job", process)

    assert (
        await audit_run.run_audit_jobs(AUDIT, _repositories(FakeRepositories([])), settings) == []
    )


async def test_a_step_carries_what_became_of_its_job(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRepositories([_job(JobKind.COLLECT)])

    async def process(job: Job, repositories: Repositories, settings: Settings) -> None:
        fake.jobs[job.id] = job.model_copy(
            update={"status": JobStatus.FAILED, "error": "Google blocked"}
        )

    monkeypatch.setattr(audit_run, "process_job", process)

    (step,) = await audit_run.run_audit_jobs(AUDIT, _repositories(fake), settings)

    assert (step.kind, step.status, step.error) == (
        JobKind.COLLECT,
        JobStatus.FAILED,
        "Google blocked",
    )
    assert step.line().startswith("collect failed after ")
    assert step.line().endswith(": Google blocked")


def test_a_step_without_error_names_only_its_result() -> None:
    step = audit_run.Step(JobKind.SCORE, JobStatus.DONE, None, 12)

    assert step.line() == "score done after 12 s"


def test_retry_puts_back_the_failed_jobs_and_sets_the_status_before_the_earliest() -> None:
    fake = FakeRepositories([])
    fake.reset = [_job(JobKind.REPORT), _job(JobKind.SCORE)]

    kinds = audit_run.retry_failed(AUDIT, _repositories(fake))

    assert kinds == [JobKind.SCORE, JobKind.REPORT]
    assert fake.statuses == [AuditStatus.COLLECTED]


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (JobKind.COLLECT, AuditStatus.REQUESTED),
        (JobKind.SCORE, AuditStatus.COLLECTED),
        (JobKind.ANALYSE, AuditStatus.AWAITING_ANALYSIS),
        (JobKind.REPORT, AuditStatus.ANALYSED),
    ],
)
def test_retry_of_a_stage_sets_the_status_the_stage_starts_from(
    kind: JobKind, status: AuditStatus
) -> None:
    fake = FakeRepositories([])
    fake.reset = [_job(kind)]

    audit_run.retry_failed(AUDIT, _repositories(fake))

    assert fake.statuses == [status]


def test_retry_of_a_deferred_search_leaves_the_status_alone() -> None:
    fake = FakeRepositories([])
    fake.reset = [_job(JobKind.SERP_RETRY)]

    kinds = audit_run.retry_failed(AUDIT, _repositories(fake))

    assert kinds == [JobKind.SERP_RETRY]
    assert fake.statuses == []


def test_retry_without_failed_jobs_changes_nothing() -> None:
    fake = FakeRepositories([])

    assert audit_run.retry_failed(AUDIT, _repositories(fake)) == []
    assert fake.statuses == []
