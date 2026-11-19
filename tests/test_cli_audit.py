import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from radarauditor import cli
from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditKind,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    Job,
    JobKind,
    JobStatus,
)
from radarauditor.db.repositories import Repositories
from radarauditor.feedback.events import Result
from radarauditor.jobs import audit_inspect, audit_run
from radarauditor.jobs.audit_inspect import AuditView

runner = CliRunner()
AUDIT_ID = UUID("00000000-0000-4000-8000-0000000000a1")
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def _job(kind: JobKind, status: JobStatus, run_after: datetime | None = None) -> Job:
    return Job(
        id=uuid4(),
        kind=kind,
        status=status,
        audit_id=AUDIT_ID,
        customer_id=uuid4(),
        attempts=1,
        error=None,
        created_at=NOW,
        updated_at=NOW,
        run_after=run_after,
    )


def _audit(status: AuditStatus = AuditStatus.REPORTED) -> Audit:
    return Audit(
        id=AUDIT_ID,
        customer_id=uuid4(),
        kind=AuditKind.REMOTE,
        status=status,
        requested_at=NOW,
        finished_at=None,
        total_score=Decimal("70"),
        raw_data_path=None,
        report_path=None,
    )


class FakeRepositories:
    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.status = AuditStatus.REPORTED
        self.rows: list[dict[str, Any]] = []
        self.events: list[tuple[UUID, Result, str, str | None]] = []

    def audit(self, audit_id: UUID) -> Audit:
        return _audit(self.status)

    def jobs_of_audit(self, audit_id: UUID) -> list[Job]:
        return list(self.jobs)

    def audit_event_rows(self, audit_id: UUID) -> list[dict[str, Any]]:
        return list(self.rows)

    def add_manager_event(
        self, audit_id: UUID, result: Result, label: str, reason: str | None
    ) -> None:
        self.events.append((audit_id, result, label, reason))


@pytest.fixture
def repositories(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FakeRepositories:
    fake = FakeRepositories()
    monkeypatch.setattr(cli, "_context", lambda: (settings, cast(Repositories, fake)))
    return fake


@pytest.fixture
def root_handlers() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    levels = [handler.level for handler in before]
    yield
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
            root.removeHandler(handler)
    for handler, level in zip(before, levels, strict=True):
        handler.setLevel(level)


def test_status_shows_the_audit_and_its_jobs(repositories: FakeRepositories) -> None:
    repositories.jobs = [_job(JobKind.COLLECT, JobStatus.DONE)]

    result = runner.invoke(cli.app, ["audit", "status", str(AUDIT_ID)])

    assert result.exit_code == 0
    assert f"audit {AUDIT_ID} status reported, score 70" in result.stdout
    assert "job collect done attempts 1" in result.stdout


def test_events_lists_all_events_or_only_the_problems(repositories: FakeRepositories) -> None:
    repositories.rows = [
        {
            "occurred_at": "2026-11-13T09:00:00+00:00",
            "kind": "route",
            "phase": "messung",
            "result": result,
            "label": label,
            "reason": None,
        }
        for result, label in (("gemessen", "Website"), ("fehlgeschlagen", "Bing"))
    ]

    everything = runner.invoke(cli.app, ["audit", "events", str(AUDIT_ID)])
    problems = runner.invoke(cli.app, ["audit", "events", str(AUDIT_ID), "--problems"])

    assert everything.stdout.count("route/messung") == 2
    assert problems.stdout.count("route/messung") == 1
    assert "Bing" in problems.stdout
    assert "Website" not in problems.stdout


def test_inspect_prints_what_the_analysis_and_report_contain(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        CheckResult(
            id=uuid4(),
            audit_id=AUDIT_ID,
            check_id="a1",
            area="a",
            outcome=CheckOutcome.PASSED,
            measured={},
            weight=Decimal(1),
        )
    ]
    view = AuditView(_audit(), [], results, [], {"summary": "Gut"}, b"%PDF /Type /Page")
    monkeypatch.setattr(audit_inspect, "load_view", lambda audit_id, repositories: view)

    result = runner.invoke(cli.app, ["audit", "inspect", str(AUDIT_ID)])

    assert result.exit_code == 0
    assert "checks without a result: a2" in result.stdout
    assert "report: 16 bytes, 1 pages" in result.stdout


def test_note_writes_an_open_event_or_a_problem(repositories: FakeRepositories) -> None:
    runner.invoke(cli.app, ["audit", "note", str(AUDIT_ID), "Sammler neu gestartet"])
    runner.invoke(cli.app, ["audit", "note", str(AUDIT_ID), "Bing blockt", "--problem"])

    assert repositories.events == [
        (AUDIT_ID, Result.OPEN, "Audit-Manager", "Sammler neu gestartet"),
        (AUDIT_ID, Result.FAILED, "Audit-Manager", "Bing blockt"),
    ]


@pytest.mark.parametrize(
    ("verdict", "result"),
    [
        ("ok", Result.MEASURED),
        ("incomplete", Result.SUSPENDED),
        ("failed", Result.FAILED),
    ],
)
def test_conclude_writes_the_verdict_as_the_result_event(
    repositories: FakeRepositories, verdict: str, result: Result
) -> None:
    outcome = runner.invoke(
        cli.app, ["audit", "conclude", str(AUDIT_ID), "Alles da", "--verdict", verdict]
    )

    assert outcome.exit_code == 0
    assert repositories.events == [(AUDIT_ID, result, "Audit-Manager: Ergebnis", "Alles da")]


def test_conclude_refuses_an_unknown_verdict(repositories: FakeRepositories) -> None:
    outcome = runner.invoke(
        cli.app, ["audit", "conclude", str(AUDIT_ID), "Text", "--verdict", "great"]
    )

    assert outcome.exit_code == 2
    assert repositories.events == []


def test_conclude_needs_a_verdict(repositories: FakeRepositories) -> None:
    outcome = runner.invoke(cli.app, ["audit", "conclude", str(AUDIT_ID), "Text"])

    assert outcome.exit_code == 2
    assert repositories.events == []


@pytest.mark.parametrize("command", ["status", "events", "inspect", "run-jobs"])
def test_the_audit_commands_refuse_an_id_that_is_no_uuid(
    repositories: FakeRepositories, command: str
) -> None:
    outcome = runner.invoke(cli.app, ["audit", command, "not-a-uuid"])

    assert outcome.exit_code == 2


def _run_jobs(
    monkeypatch: pytest.MonkeyPatch, steps: list[audit_run.Step], put_back: list[JobKind]
) -> list[str]:
    calls: list[str] = []

    async def run(audit_id: UUID, repositories: Repositories, settings: Settings) -> list[Any]:
        calls.append("run")
        return steps

    def retry(audit_id: UUID, repositories: Repositories) -> list[JobKind]:
        calls.append("retry")
        return put_back

    monkeypatch.setattr(audit_run, "run_audit_jobs", run)
    monkeypatch.setattr(audit_run, "retry_failed", retry)
    return calls


def test_run_jobs_prints_every_step_the_status_and_the_log_file(
    repositories: FakeRepositories,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    root_handlers: None,
) -> None:
    repositories.jobs = [_job(JobKind.COLLECT, JobStatus.DONE)]
    _run_jobs(monkeypatch, [audit_run.Step(JobKind.COLLECT, JobStatus.DONE, None, 90)], [])

    result = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID)])

    log = settings.radar_work_dir / "manager" / f"{AUDIT_ID}.log"
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "collect done after 90 s",
        "audit status: reported",
        f"log: {log}",
    ]
    assert log.parent.is_dir()


def test_run_jobs_names_the_jobs_that_wait_and_until_when(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch, root_handlers: None
) -> None:
    repositories.status = AuditStatus.AWAITING_ANALYSIS
    repositories.jobs = [
        _job(JobKind.ANALYSE, JobStatus.QUEUED, NOW),
        _job(JobKind.REPORT, JobStatus.QUEUED),
    ]
    _run_jobs(monkeypatch, [], [])

    result = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID)])

    assert result.exit_code == 0
    assert "waiting: analyse not before 2026-11-13T09:00:00+00:00" in result.stdout
    assert "waiting: report\n" in result.stdout
    assert "audit status: awaiting_analysis" in result.stdout


def test_run_jobs_fails_with_code_one_when_a_job_of_the_audit_failed(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch, root_handlers: None
) -> None:
    repositories.status = AuditStatus.FAILED
    repositories.jobs = [_job(JobKind.COLLECT, JobStatus.FAILED)]
    _run_jobs(monkeypatch, [audit_run.Step(JobKind.COLLECT, JobStatus.FAILED, "boom", 3)], [])

    result = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID)])

    assert result.exit_code == 1
    assert "collect failed after 3 s: boom" in result.stdout


def test_run_jobs_puts_failed_jobs_back_first_only_when_asked(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch, root_handlers: None
) -> None:
    calls = _run_jobs(monkeypatch, [], [JobKind.SCORE, JobKind.REPORT])

    plain = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID)])
    assert calls == ["run"]
    assert "put back" not in plain.stdout

    calls.clear()
    retried = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID), "--retry-failed"])

    assert calls == ["retry", "run"]
    assert retried.stdout.splitlines()[0] == "put back: score, report"


def test_run_jobs_says_when_nothing_had_failed(
    repositories: FakeRepositories, monkeypatch: pytest.MonkeyPatch, root_handlers: None
) -> None:
    _run_jobs(monkeypatch, [], [])

    result = runner.invoke(cli.app, ["audit", "run-jobs", str(AUDIT_ID), "--retry-failed"])

    assert result.stdout.splitlines()[0] == "put back: nothing failed"


def test_the_console_shows_only_warnings_while_the_file_keeps_everything(
    tmp_path: Path, root_handlers: None
) -> None:
    root = logging.getLogger()
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    root.addHandler(console)
    previous_level = root.level
    root.setLevel(logging.INFO)
    path = tmp_path / "deep" / "audit.log"

    try:
        cli._log_to_file(path)
        logging.getLogger("radarauditor.test").info("a detail")
    finally:
        root.setLevel(previous_level)

    for handler in root.handlers:
        handler.flush()
    assert console.level == logging.WARNING
    assert "a detail" in path.read_text(encoding="utf-8")
