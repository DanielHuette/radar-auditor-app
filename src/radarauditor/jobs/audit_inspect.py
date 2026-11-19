import json
import re
import zlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from radarauditor.db.models import Audit, CheckOutcome, CheckResult, Finding, Job
from radarauditor.db.repositories import Repositories
from radarauditor.errors import StorageError
from radarauditor.feedback.events import Result
from radarauditor.jobs.control_agent import is_planned
from radarauditor.jobs.handoff import RAW_BUCKET, TEXTS_FILE
from radarauditor.jobs.worker import REPORTS_BUCKET
from radarauditor.scoring.catalog import Catalogue

PROBLEM_RESULTS = frozenset({Result.FAILED.value, Result.SUSPENDED.value, Result.WITH_PERSON.value})
PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
UNMEASURED = (CheckOutcome.NOT_MEASURABLE, CheckOutcome.SKIPPED)


@dataclass(frozen=True)
class AuditView:
    audit: Audit
    jobs: list[Job]
    results: list[CheckResult]
    findings: list[Finding]
    texts: Mapping[str, Any] | None
    report: bytes | None


def load_view(audit_id: UUID, repositories: Repositories) -> AuditView:
    audit = repositories.audit(audit_id)
    return AuditView(
        audit=audit,
        jobs=repositories.jobs_of_audit(audit_id),
        results=repositories.check_results(audit_id),
        findings=repositories.findings_of_audit(audit_id),
        texts=_texts(audit_id, repositories),
        report=_report(audit, repositories),
    )


def _texts(audit_id: UUID, repositories: Repositories) -> Mapping[str, Any] | None:
    try:
        content = json.loads(
            repositories.download_bytes(RAW_BUCKET, f"{audit_id}/{TEXTS_FILE}").decode("utf-8")
        )
    except StorageError:
        return None
    return content if isinstance(content, dict) else None


def _report(audit: Audit, repositories: Repositories) -> bytes | None:
    if audit.report_path is None:
        return None
    try:
        return repositories.download_bytes(REPORTS_BUCKET, audit.report_path)
    except StorageError:
        return None


def event_lines(rows: list[dict[str, Any]], *, problems_only: bool) -> list[str]:
    selected = [row for row in rows if not problems_only or row["result"] in PROBLEM_RESULTS]
    return [
        f"{str(row['occurred_at'])[11:19]} {row['kind']}/{row['phase']} {row['result']} "
        f"{row['label']}" + (f" - {row['reason']}" if row.get("reason") else "")
        for row in selected
    ]


def status_lines(audit: Audit, jobs: list[Job]) -> list[str]:
    lines = [f"audit {audit.id} status {audit.status.value}, score {audit.total_score}"]
    lines.extend(
        f"job {job.kind.value} {job.status.value} attempts {job.attempts}"
        + (f" run_after {job.run_after.isoformat()}" if job.run_after else "")
        + (f" error: {job.error}" if job.error else "")
        for job in jobs
    )
    return lines


def inspect_lines(view: AuditView, catalogue: Catalogue) -> list[str]:
    audit = view.audit
    lines = [
        f"status {audit.status.value}, total score {audit.total_score}",
        _outcome_line(view.results),
    ]
    expected = {check.id for area in catalogue.areas for check in area.checks} - set(
        audit.disabled_checks
    )
    missing = sorted(expected - {result.check_id for result in view.results})
    lines.append(f"checks without a result: {', '.join(missing) if missing else 'none'}")
    lines.extend(
        f"planned not measurable {result.check_id}: {_reason(result)}"
        for result in view.results
        if result.outcome is CheckOutcome.NOT_MEASURABLE and is_planned(result)
    )
    lines.extend(
        f"unmeasured {result.check_id} ({result.outcome.value}): {_reason(result)}"
        for result in view.results
        if result.outcome in UNMEASURED and not is_planned(result)
    )
    lines.append(f"findings: {len(view.findings)}")
    lines.extend(
        f"finding severity {finding.severity} {finding.check_id}: {finding.title}"
        for finding in sorted(view.findings, key=lambda item: -item.severity)[:5]
    )
    lines.append(_texts_line(view.texts))
    lines.append(_report_line(view.report))
    return lines


def _reason(result: CheckResult) -> str:
    reason = result.measured.get("reason") or result.measured.get("note")
    return str(reason) if reason else "no reason stored"


def _outcome_line(results: list[CheckResult]) -> str:
    counts = Counter(result.outcome.value for result in results)
    return "check outcomes: " + ", ".join(
        f"{name} {count}" for name, count in sorted(counts.items())
    )


def _texts_line(texts: Mapping[str, Any] | None) -> str:
    if texts is None:
        return "texts: missing"
    empty = sorted(key for key, value in texts.items() if value in (None, "", [], {}))
    return f"texts: {len(texts)} fields, empty: {', '.join(empty) if empty else 'none'}"


def _report_line(report: bytes | None) -> str:
    if report is None:
        return "report: missing"
    return f"report: {len(report)} bytes, {count_pages(report)} pages"


def count_pages(report: bytes) -> int:
    inflated = [report]
    for stream in PDF_STREAM.findall(report):
        try:
            inflated.append(zlib.decompress(stream))
        except zlib.error:
            continue
    return sum(len(PDF_PAGE.findall(part)) for part in inflated)
