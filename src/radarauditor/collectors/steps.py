import logging
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from radarauditor.errors import StorageError
from radarauditor.feedback.events import RouteReport

logger = logging.getLogger(__name__)

RAW_BUCKET = "raw"
STEPS_FILE = "steps.json"
REMOVED_STEPS = frozenset({"directories", "social", "wayback"})


class StepStorage(Protocol):
    def download_bytes(self, bucket: str, remote_path: str) -> bytes: ...

    def upload_bytes(self, bucket: str, remote_path: str, payload: bytes) -> str: ...


class StoredStep(BaseModel):
    value: Any = None
    warnings: list[str] = Field(default_factory=list)
    failure: str | None = None
    suspended: bool = False
    attempts: int = 0
    credit_short: str | None = None


class _StepFile(BaseModel):
    steps: dict[str, StoredStep] = Field(default_factory=dict)
    reports: list[RouteReport] = Field(default_factory=list)


def steps_path(audit_id: UUID) -> str:
    return f"{audit_id}/{STEPS_FILE}"


def raw_answer_path(audit_id: UUID, name: str) -> str:
    return f"{audit_id}/antworten/{name}"


class StepStore:
    def __init__(self, audit_id: UUID, storage: StepStorage) -> None:
        self.audit_id = audit_id
        self._storage = storage
        self._file = self._load()
        self.resuming = bool(self._file.steps)
        self._probes: dict[str, str | None] = {}

    def reload(self) -> None:
        self._file = self._load()

    @property
    def reports(self) -> list[RouteReport]:
        return list(self._file.reports)

    def stored(self, step: str) -> StoredStep | None:
        return self._file.steps.get(step)

    def finished(self, step: str) -> StoredStep | None:
        stored = self.stored(step)
        return stored if stored is not None and stored.failure is None else None

    def failed(self) -> dict[str, str]:
        return {
            step: stored.failure
            for step, stored in self._file.steps.items()
            if stored.failure is not None and not stored.suspended and step not in REMOVED_STEPS
        }

    def keep(
        self,
        step: str,
        value: Any,
        warnings: list[str],
        failure: str | None,
        *,
        suspended: bool = False,
    ) -> None:
        known = self._file.steps.get(step, StoredStep())
        self._file.steps[step] = known.model_copy(
            update={
                "value": value,
                "warnings": warnings,
                "failure": failure,
                "suspended": suspended,
                "credit_short": None,
            }
        )
        self._save()

    def keep_probe(self, step: str, failure: str | None) -> None:
        self._probes[step] = failure

    def probe_failure(self, step: str) -> str | None:
        return self._probes.get(step, "no probe ran")

    def keep_reports(self, reports: list[RouteReport]) -> None:
        self._file.reports = list(reports)
        self._save()

    def count_attempt(self, step: str) -> int:
        known = self._file.steps.get(step, StoredStep())
        attempts = known.attempts + 1
        self._file.steps[step] = known.model_copy(update={"attempts": attempts})
        self._save()
        return attempts

    def mark_credit_short(self, step: str, note: str) -> None:
        known = self._file.steps.get(step, StoredStep())
        self._file.steps[step] = known.model_copy(update={"credit_short": note})
        self._save()

    def _load(self) -> _StepFile:
        try:
            content = self._storage.download_bytes(RAW_BUCKET, steps_path(self.audit_id))
        except StorageError:
            return _StepFile()
        try:
            return _StepFile.model_validate_json(content)
        except ValidationError as error:
            raise StorageError(f"stored steps of audit {self.audit_id} are unreadable") from error

    def _save(self) -> None:
        self._storage.upload_bytes(
            RAW_BUCKET, steps_path(self.audit_id), self._file.model_dump_json().encode("utf-8")
        )
