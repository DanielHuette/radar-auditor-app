import logging
import time
from dataclasses import dataclass
from uuid import UUID

from radarauditor.config import Settings
from radarauditor.db.models import AuditStatus, JobKind, JobStatus
from radarauditor.db.repositories import Repositories
from radarauditor.jobs.audit_manager import AUDIT_KINDS
from radarauditor.jobs.worker import process_job

logger = logging.getLogger(__name__)

STATUS_BEFORE_JOB = {
    JobKind.COLLECT: AuditStatus.REQUESTED,
    JobKind.SCORE: AuditStatus.COLLECTED,
    JobKind.ANALYSE: AuditStatus.AWAITING_ANALYSIS,
    JobKind.REPORT: AuditStatus.ANALYSED,
}


@dataclass(frozen=True)
class Step:
    kind: JobKind
    status: JobStatus
    error: str | None
    seconds: int

    def line(self) -> str:
        detail = f": {self.error}" if self.error else ""
        return f"{self.kind.value} {self.status.value} after {self.seconds} s{detail}"


async def run_audit_jobs(
    audit_id: UUID, repositories: Repositories, settings: Settings
) -> list[Step]:
    steps: list[Step] = []
    while (job := repositories.claim_audit_job(audit_id, AUDIT_KINDS)) is not None:
        started = time.monotonic()
        await process_job(job, repositories, settings)
        after = next(item for item in repositories.jobs_of_audit(audit_id) if item.id == job.id)
        steps.append(Step(job.kind, after.status, after.error, round(time.monotonic() - started)))
    return steps


def retry_failed(audit_id: UUID, repositories: Repositories) -> list[JobKind]:
    reset = repositories.reset_failed_jobs(audit_id, AUDIT_KINDS)
    kinds = [kind for kind in AUDIT_KINDS if kind in {job.kind for job in reset}]
    earliest = next((kind for kind in kinds if kind in STATUS_BEFORE_JOB), None)
    if earliest is not None:
        repositories.set_audit_status(audit_id, STATUS_BEFORE_JOB[earliest])
    return kinds
