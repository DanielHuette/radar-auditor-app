import json
import logging
import shutil
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    Competitor,
    Customer,
    Finding,
    Job,
    JobKind,
    JobStatus,
    Keyword,
    Location,
    Ranking,
)
from radarauditor.db.repositories import Repositories
from radarauditor.errors import HandoffError, RenderError, ScoringError, StorageError
from radarauditor.reporting.texts import parse_texts
from radarauditor.scoring.catalog import AGENT_PENDING_REASON, Catalogue, catalogue_from_document
from radarauditor.scoring.evaluator import total_score

logger = logging.getLogger(__name__)

PULL_LIMIT = 20
ANALYSE_DIR = "analyse"
DONE_DIR = "erledigt"
RAW_BUCKET = "raw"
AUDIT_FILE = "audit.json"
RAW_FILE = "raw.json"
CHECK_RESULTS_FILE = "check_results.json"
CATALOGUE_FILE = "katalog.json"
PREVIOUS_AUDIT_FILE = "vorheriges_audit.json"
RANKINGS_FILE = "rankings.json"
FINDINGS_FILE = "findings.json"
TEXTS_FILE = "texte.json"
ANALYSIS_FILE = "auswertung.json"
JUDGEMENTS_FILE = "urteile.json"
JUDGED_BY_AGENT = "audit_agent"

_FINDINGS_ADAPTER = TypeAdapter(list[Finding])
_CHECK_RESULTS_ADAPTER = TypeAdapter(list[CheckResult])


class Judgement(BaseModel):
    check_id: str
    outcome: Literal[CheckOutcome.PASSED, CheckOutcome.PARTIAL, CheckOutcome.FAILED]
    evidence: str = Field(min_length=1)


_JUDGEMENTS_ADAPTER = TypeAdapter(list[Judgement])


class AnalysisPath(StrEnum):
    CLAUDE_CODE = "claude_code"
    MANUAL = "manual"


class AnalysisRecord(BaseModel):
    path: AnalysisPath


class AnalysisInput(BaseModel):
    audit: Audit
    customer: Customer
    locations: list[Location]
    competitors: list[Competitor]
    keywords: list[Keyword]


class PreviousAudit(BaseModel):
    audit: Audit
    total_score: Decimal | None
    check_results: list[CheckResult]


class RankingHistory(BaseModel):
    audit_id: UUID
    rankings: list[Ranking]
    previous_audit_id: UUID | None
    previous_rankings: list[Ranking]


def pull(repositories: Repositories, settings: Settings) -> list[Path]:
    catalogue_json = load_catalogue_json(settings.catalogue_path)
    folders: list[Path] = []
    for job in repositories.claim_jobs(JobKind.ANALYSE, PULL_LIMIT):
        folder = settings.radar_work_dir / ANALYSE_DIR / str(job.id)
        try:
            materialise(job, folder, catalogue_json, repositories)
        except (HandoffError, StorageError) as exc:
            logger.error("Analyse job %s could not be prepared: %s", job.id, exc)
            shutil.rmtree(folder, ignore_errors=True)
            repositories.finish_job(job.id, JobStatus.FAILED, str(exc))
            continue
        logger.info("Analyse job %s prepared in %s", job.id, folder)
        folders.append(folder)
    return folders


def load_catalogue_json(path: Path) -> str:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HandoffError(f"{path}: catalogue cannot be read: {exc}") from exc
    return json.dumps(content, ensure_ascii=False, indent=2)


def materialise(job: Job, folder: Path, catalogue_json: str, repositories: Repositories) -> None:
    if job.audit_id is None:
        raise HandoffError(f"job {job.id} has no audit id")
    audit = repositories.audit(job.audit_id)
    if audit.raw_data_path is None:
        raise HandoffError(f"audit {audit.id} has no raw data path")
    folder.mkdir(parents=True, exist_ok=True)
    write_atomic(folder / AUDIT_FILE, analysis_input(audit, repositories).model_dump_json(indent=2))
    repositories.download(RAW_BUCKET, audit.raw_data_path, folder / RAW_FILE)
    results = repositories.check_results(audit.id)
    write_atomic(
        folder / CHECK_RESULTS_FILE,
        _CHECK_RESULTS_ADAPTER.dump_json(results, indent=2).decode(),
    )
    write_atomic(folder / CATALOGUE_FILE, catalogue_json)
    previous = repositories.previous_audit(audit)
    if previous is not None:
        previous_audit = PreviousAudit(
            audit=previous,
            total_score=previous.total_score,
            check_results=repositories.check_results(previous.id),
        )
        write_atomic(folder / PREVIOUS_AUDIT_FILE, previous_audit.model_dump_json(indent=2))
    history = _ranking_history(audit, previous, repositories)
    write_atomic(folder / RANKINGS_FILE, history.model_dump_json(indent=2))


def analysis_input(audit: Audit, repositories: Repositories) -> AnalysisInput:
    return AnalysisInput(
        audit=audit,
        customer=repositories.customer(audit.customer_id),
        locations=repositories.locations(audit.customer_id),
        competitors=repositories.competitors(audit.customer_id),
        keywords=repositories.keywords(audit.customer_id),
    )


def _ranking_history(
    audit: Audit, previous: Audit | None, repositories: Repositories
) -> RankingHistory:
    return RankingHistory(
        audit_id=audit.id,
        rankings=repositories.rankings(audit.id),
        previous_audit_id=previous.id if previous is not None else None,
        previous_rankings=repositories.rankings(previous.id) if previous is not None else [],
    )


def write_atomic(target: Path, text: str) -> None:
    partial = target.with_name(f".{target.name}.part")
    partial.write_text(text, encoding="utf-8")
    partial.replace(target)


def push(repositories: Repositories, settings: Settings) -> list[Path]:
    analyse_root = settings.radar_work_dir / ANALYSE_DIR
    if not analyse_root.is_dir():
        return []
    done: list[Path] = []
    problems: list[str] = []
    for folder in sorted(path for path in analyse_root.iterdir() if path.is_dir()):
        if not (folder / FINDINGS_FILE).exists() or not (folder / TEXTS_FILE).exists():
            continue
        try:
            done.append(_push_folder(folder, repositories, settings.radar_work_dir / DONE_DIR))
        except HandoffError as exc:
            logger.error("Hand-off folder %s rejected: %s", folder.name, exc)
            problems.append(str(exc))
    if problems:
        raise HandoffError("; ".join(problems))
    return done


def _push_folder(folder: Path, repositories: Repositories, done_root: Path) -> Path:
    job_id = _job_id(folder)
    target = deliver(folder, repositories, done_root, AnalysisRecord(path=AnalysisPath.MANUAL))
    repositories.finish_job(job_id, JobStatus.DONE)
    return target


def deliver(
    folder: Path, repositories: Repositories, done_root: Path, record: AnalysisRecord
) -> Path:
    job_id = _job_id(folder)
    audit = read_analysis_input(folder).audit
    findings = validate_outputs(folder)
    judgements = validate_judgements(folder)
    write_atomic(folder / ANALYSIS_FILE, record.model_dump_json(indent=2))
    repositories.upload(RAW_BUCKET, f"{audit.id}/{TEXTS_FILE}", folder / TEXTS_FILE)
    repositories.upload(RAW_BUCKET, f"{audit.id}/{ANALYSIS_FILE}", folder / ANALYSIS_FILE)
    if judgements:
        repositories.upload(RAW_BUCKET, f"{audit.id}/{JUDGEMENTS_FILE}", folder / JUDGEMENTS_FILE)
        results = judged_results(read_check_results(folder), judgements)
        repositories.save_check_results(results)
        repositories.update_audit(audit.id, total_score=total_score(results, _catalogue(folder)))
    repositories.save_findings(findings)
    repositories.set_audit_status(audit.id, AuditStatus.ANALYSED)
    repositories.enqueue(JobKind.REPORT, audit.customer_id, audit.id)
    target = done_root / folder.name
    done_root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.move(folder, target)
    logger.info("Analyse job %s pushed with %d findings", job_id, len(findings))
    return target


def _job_id(folder: Path) -> UUID:
    try:
        return UUID(folder.name)
    except ValueError as exc:
        raise HandoffError(f"{folder}: folder name is not a job id") from exc


def read_analysis_input(folder: Path) -> AnalysisInput:
    path = folder / AUDIT_FILE
    try:
        return AnalysisInput.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HandoffError(f"{path}: cannot be read: {exc}") from exc
    except ValidationError as exc:
        raise HandoffError(f"{path}: invalid: {_first_error(exc)}") from exc


def validate_outputs(folder: Path) -> list[Finding]:
    findings = validate_findings(folder)
    _validate_texts(folder)
    validate_judgements(folder)
    return findings


def validate_judgements(folder: Path) -> list[Judgement]:
    waiting = sorted(
        result.check_id
        for result in read_check_results(folder)
        if result.measured.get("reason") == AGENT_PENDING_REASON
    )
    if not waiting:
        return []
    path = folder / JUDGEMENTS_FILE
    try:
        judgements = _JUDGEMENTS_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HandoffError(f"{path}: cannot be read: {exc}") from exc
    except ValidationError as exc:
        raise HandoffError(f"{path}: invalid: {_first_error(exc)}") from exc
    judged = sorted(judgement.check_id for judgement in judgements)
    if judged != waiting:
        raise HandoffError(f"{path}: judges {judged}, the audit waits for {waiting}")
    return judgements


def judged_results(results: list[CheckResult], judgements: list[Judgement]) -> list[CheckResult]:
    by_check = {judgement.check_id: judgement for judgement in judgements}
    return [
        result.model_copy(
            update={
                "outcome": judgement.outcome,
                "measured": {"judged_by": JUDGED_BY_AGENT, "evidence": judgement.evidence},
            }
        )
        if (judgement := by_check.get(result.check_id)) is not None
        else result
        for result in results
    ]


def _catalogue(folder: Path) -> Catalogue:
    path = folder / CATALOGUE_FILE
    try:
        return catalogue_from_document(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError, ScoringError) as exc:
        raise HandoffError(f"{path}: catalogue unreadable: {exc}") from exc


def _validate_texts(folder: Path) -> None:
    path = folder / TEXTS_FILE
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffError(f"{path}: cannot be read: {exc}") from exc
    try:
        texts = parse_texts(content)
    except RenderError as exc:
        raise HandoffError(f"{path}: {exc}") from exc
    if texts.talk_text is None:
        raise HandoffError(f"{path}: texte.json invalid at talk_text: Field required")


def validate_findings(folder: Path) -> list[Finding]:
    path = folder / FINDINGS_FILE
    audit_id = read_analysis_input(folder).audit.id
    known_check_ids = _known_check_ids(folder)
    try:
        findings = _FINDINGS_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HandoffError(f"{path}: cannot be read: {exc}") from exc
    except ValidationError as exc:
        raise HandoffError(f"{path}: invalid: {_first_error(exc)}") from exc
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        location = f"{path}: entry {index} ({finding.check_id})"
        if finding.audit_id != audit_id:
            raise HandoffError(f"{location}: audit_id {finding.audit_id} is not {audit_id}")
        if finding.check_id not in known_check_ids:
            raise HandoffError(f"{location}: unknown check id")
        if finding.check_id in seen:
            raise HandoffError(f"{location}: duplicate check id")
        if not 1 <= finding.severity <= 3:
            raise HandoffError(f"{location}: severity {finding.severity} is not between 1 and 3")
        seen.add(finding.check_id)
    return findings


def _known_check_ids(folder: Path) -> set[str]:
    return {result.check_id for result in read_check_results(folder)}


def read_check_results(folder: Path) -> list[CheckResult]:
    path = folder / CHECK_RESULTS_FILE
    try:
        return _CHECK_RESULTS_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HandoffError(f"{path}: cannot be read: {exc}") from exc
    except ValidationError as exc:
        raise HandoffError(f"{path}: invalid: {_first_error(exc)}") from exc


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "<root>"
    return f"{location}: {error['msg']} ({exc.error_count()} error(s) in total)"
