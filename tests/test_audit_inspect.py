import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    Finding,
    Job,
    JobKind,
    JobStatus,
)
from radarauditor.db.repositories import Repositories
from radarauditor.errors import StorageError
from radarauditor.jobs import audit_inspect
from radarauditor.jobs.audit_inspect import AuditView
from radarauditor.scoring.catalog import load_catalogue

AUDIT_ID = UUID("00000000-0000-4000-8000-0000000000a1")


def _audit(**updates: Any) -> Audit:
    values: dict[str, Any] = {
        "id": AUDIT_ID,
        "customer_id": uuid4(),
        "kind": "remote",
        "status": AuditStatus.REPORTED,
        "requested_at": datetime(2026, 11, 13, 9, 0, tzinfo=UTC),
        "finished_at": None,
        "total_score": Decimal("61.5"),
        "raw_data_path": None,
        "report_path": "a/report.pdf",
    }
    return Audit.model_validate({**values, **updates})


def _result(check_id: str, outcome: CheckOutcome, **measured: object) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="a",
        outcome=outcome,
        measured=dict(measured),
        weight=Decimal(1),
    )


def _finding(check_id: str, severity: int, title: str) -> Finding:
    return Finding(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        severity=severity,
        title=title,
        explanation="e",
        customer_impact="i",
        fix="f",
        fix_payload=None,
    )


def _view(**updates: Any) -> AuditView:
    values: dict[str, Any] = {
        "audit": _audit(),
        "jobs": [],
        "results": [
            _result("a1", CheckOutcome.PASSED),
            _result("a2", CheckOutcome.FAILED),
        ],
        "findings": [_finding("a2", 3, "Ladezeit")],
        "texts": {"introduction": "Hallo", "summary": "Gut"},
        "report": b"%PDF /Type /Page /Type /Pages /Type/Page",
    }
    return AuditView(**{**values, **updates})


def test_a_complete_audit_shows_no_gap(settings: Settings) -> None:
    lines = audit_inspect.inspect_lines(_view(), load_catalogue(settings.catalogue_path))

    assert lines == [
        "status reported, total score 61.5",
        "check outcomes: failed 1, passed 1",
        "checks without a result: none",
        "findings: 1",
        "finding severity 3 a2: Ladezeit",
        "texts: 2 fields, empty: none",
        "report: 40 bytes, 2 pages",
    ]


def test_checks_without_a_result_are_named(settings: Settings) -> None:
    view = _view(results=[_result("a1", CheckOutcome.PASSED)])

    lines = audit_inspect.inspect_lines(view, load_catalogue(settings.catalogue_path))

    assert "checks without a result: a2" in lines


def test_switched_off_checks_are_no_gap(settings: Settings) -> None:
    view = _view(results=[_result("a1", CheckOutcome.PASSED)], audit=_audit(disabled_checks=["a2"]))

    lines = audit_inspect.inspect_lines(view, load_catalogue(settings.catalogue_path))

    assert "checks without a result: none" in lines


def test_unmeasured_checks_are_listed_with_their_reason(settings: Settings) -> None:
    view = _view(
        results=[
            _result("a1", CheckOutcome.NOT_MEASURABLE, reason="no_serps"),
            _result("a2", CheckOutcome.SKIPPED),
        ]
    )

    lines = audit_inspect.inspect_lines(view, load_catalogue(settings.catalogue_path))

    assert "unmeasured a1 (not_measurable): no_serps" in lines
    assert "unmeasured a2 (skipped): no reason stored" in lines


def test_only_the_five_most_severe_findings_are_listed(settings: Settings) -> None:
    findings = [_finding("a1", severity, f"F{severity}") for severity in range(1, 8)]

    lines = audit_inspect.inspect_lines(
        _view(findings=findings), load_catalogue(settings.catalogue_path)
    )

    listed = [line for line in lines if line.startswith("finding severity")]
    assert [line.split(": ")[1] for line in listed] == ["F7", "F6", "F5", "F4", "F3"]
    assert "findings: 7" in lines


def test_missing_texts_and_report_are_said_so(settings: Settings) -> None:
    lines = audit_inspect.inspect_lines(
        _view(texts=None, report=None), load_catalogue(settings.catalogue_path)
    )

    assert lines[-2:] == ["texts: missing", "report: missing"]


def test_empty_text_fields_are_named() -> None:
    texts: dict[str, Any] = {
        "introduction": "Hallo",
        "summary": "",
        "talk_text": None,
        "list": [],
        "map": {},
    }

    assert (
        audit_inspect._texts_line(texts) == "texts: 5 fields, empty: list, map, summary, talk_text"
    )


def test_a_report_that_is_no_pdf_has_no_pages() -> None:
    assert audit_inspect._report_line(b"nothing") == "report: 7 bytes, 0 pages"


def test_events_are_listed_with_time_kind_result_label_and_reason() -> None:
    rows: list[dict[str, Any]] = [
        {
            "occurred_at": "2026-11-13T09:15:30+00:00",
            "kind": "route",
            "phase": "messung",
            "result": "gemessen",
            "label": "Website",
            "reason": None,
        },
        {
            "occurred_at": "2026-11-13T09:16:00+00:00",
            "kind": "route",
            "phase": "messung",
            "result": "fehlgeschlagen",
            "label": "Bing",
            "reason": "Bing blockt",
        },
    ]

    assert audit_inspect.event_lines(rows, problems_only=False) == [
        "09:15:30 route/messung gemessen Website",
        "09:16:00 route/messung fehlgeschlagen Bing - Bing blockt",
    ]


def test_only_problems_leave_out_what_was_measured() -> None:
    rows: list[dict[str, Any]] = [
        {
            "occurred_at": "2026-11-13T09:00:00+00:00",
            "kind": "route",
            "phase": "messung",
            "result": result,
            "label": result,
            "reason": None,
        }
        for result in ("gemessen", "offen", "fehlgeschlagen", "ausgesetzt", "Schritt mit Daniel")
    ]

    lines = audit_inspect.event_lines(rows, problems_only=True)

    assert [line.split(" ", 2)[2] for line in lines] == [
        "fehlgeschlagen fehlgeschlagen",
        "ausgesetzt ausgesetzt",
        "Schritt mit Daniel Schritt mit Daniel",
    ]


def test_status_lists_the_audit_and_every_job_with_its_wait_and_error() -> None:
    now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    jobs = [
        Job(
            id=uuid4(),
            kind=JobKind.ANALYSE,
            status=JobStatus.QUEUED,
            audit_id=AUDIT_ID,
            customer_id=uuid4(),
            attempts=1,
            error="Claude Code ist nicht angemeldet",
            created_at=now,
            updated_at=now,
            run_after=now,
        ),
        Job(
            id=uuid4(),
            kind=JobKind.COLLECT,
            status=JobStatus.DONE,
            audit_id=AUDIT_ID,
            customer_id=uuid4(),
            attempts=1,
            error=None,
            created_at=now,
            updated_at=now,
        ),
    ]

    assert audit_inspect.status_lines(_audit(), jobs) == [
        f"audit {AUDIT_ID} status reported, score 61.5",
        "job analyse queued attempts 1 run_after 2026-11-13T09:00:00+00:00 "
        "error: Claude Code ist nicht angemeldet",
        "job collect done attempts 1",
    ]


class FakeRepositories:
    def __init__(self, audit: Audit) -> None:
        self.audit_value = audit
        self.files: dict[tuple[str, str], bytes] = {}

    def audit(self, audit_id: UUID) -> Audit:
        return self.audit_value

    def jobs_of_audit(self, audit_id: UUID) -> list[Job]:
        return []

    def check_results(self, audit_id: UUID) -> list[CheckResult]:
        return [_result("a1", CheckOutcome.PASSED)]

    def findings_of_audit(self, audit_id: UUID) -> list[Finding]:
        return []

    def download_bytes(self, bucket: str, remote_path: str) -> bytes:
        try:
            return self.files[(bucket, remote_path)]
        except KeyError:
            raise StorageError(f"no file {bucket}/{remote_path}") from None


def test_the_view_loads_texts_and_report_from_storage() -> None:
    fake = FakeRepositories(_audit())
    fake.files[("raw", f"{AUDIT_ID}/texte.json")] = json.dumps({"summary": "Gut"}).encode()
    fake.files[("reports", "a/report.pdf")] = b"%PDF"

    view = audit_inspect.load_view(AUDIT_ID, cast(Repositories, fake))

    assert view.texts == {"summary": "Gut"}
    assert view.report == b"%PDF"
    assert [result.check_id for result in view.results] == ["a1"]


def test_the_view_notes_missing_files_instead_of_failing() -> None:
    view = audit_inspect.load_view(AUDIT_ID, cast(Repositories, FakeRepositories(_audit())))

    assert view.texts is None
    assert view.report is None


def test_an_audit_without_a_report_path_has_no_report() -> None:
    fake = FakeRepositories(_audit(report_path=None))
    fake.files[("reports", "a/report.pdf")] = b"%PDF"

    assert audit_inspect.load_view(AUDIT_ID, cast(Repositories, fake)).report is None


def test_texts_that_are_no_object_count_as_missing() -> None:
    fake = FakeRepositories(_audit())
    fake.files[("raw", f"{AUDIT_ID}/texte.json")] = b"[1, 2]"

    assert audit_inspect.load_view(AUDIT_ID, cast(Repositories, fake)).texts is None


def test_planned_not_measurable_checks_are_not_listed_as_defects(settings: Settings) -> None:
    view = _view(
        results=[
            _result("m1", CheckOutcome.NOT_MEASURABLE, reason="no_access"),
            _result("p5", CheckOutcome.NOT_MEASURABLE, reason="not_verifiable_from_outside"),
            _result("a1", CheckOutcome.NOT_MEASURABLE, reason="no_serps"),
        ]
    )

    lines = audit_inspect.inspect_lines(view, load_catalogue(settings.catalogue_path))

    assert "planned not measurable m1: no_access" in lines
    assert "planned not measurable p5: not_verifiable_from_outside" in lines
    assert not any(line.startswith("unmeasured m1") for line in lines)
    assert "unmeasured a1 (not_measurable): no_serps" in lines


def test_the_pages_of_a_compressed_report_are_counted() -> None:
    from weasyprint import HTML

    body = "".join("<p style='page-break-after: always'>Seite</p>" for _ in range(3))
    report = HTML(string=f"<html><body>{body}</body></html>").write_pdf()

    assert audit_inspect.count_pages(report) == 3
