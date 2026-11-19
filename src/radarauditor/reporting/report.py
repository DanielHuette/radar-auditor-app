import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, ValidationError

from radarauditor.collectors.assistants import provider_source
from radarauditor.collectors.patterns import host_of
from radarauditor.collectors.snapshot import (
    GapKeyword,
    PlaceSnapshot,
    RawAudit,
)
from radarauditor.collectors.validation import steps_of_check
from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    ControlState,
    Finding,
    Keyword,
    KeywordScope,
    Location,
    Ranking,
)
from radarauditor.db.repositories import Repositories
from radarauditor.errors import RenderError, StorageError
from radarauditor.feedback.events import INVALID_REASON_CODE
from radarauditor.jobs.control_agent import is_planned
from radarauditor.rendering import filename_slug, german_date, render_template, write_pdf
from radarauditor.reporting import charts
from radarauditor.reporting.costs import CostStatement, cost_statement
from radarauditor.reporting.forecast import best_positions
from radarauditor.reporting.texts import ReportTexts, parse_texts
from radarauditor.scoring.catalog import Catalogue, CheckDefinition, load_catalogue
from radarauditor.scoring.evaluator import POINTS, area_scores, total_score

logger = logging.getLogger(__name__)

REPORT_DIR = "berichte"
REPORT_TEMPLATE_DIR = "report"
SALES_TEMPLATE = "sales.html"
RECOMMENDATION_TEMPLATE = "recommendation.html"
MONTHLY_TEMPLATE = "monthly.html"
LABELS_FILE = "labels.yaml"
SALES_FILE_PREFIX = "Auswertung"
RECOMMENDATION_FILE_PREFIX = "Empfehlung"
PAID_CHECKS = ("p1", "p2", "p3", "p4")
UNRATED_OUTCOMES = (CheckOutcome.SKIPPED, CheckOutcome.NOT_APPLICABLE)
SHORT_REASON_CHARS = 200
SHORT_REASON_SUFFIX = "… (vollständig unter „Was nicht gemessen wurde“)"
MONTHLY_FILE_PREFIX = "Monatsbericht"
RAW_BUCKET = "raw"
RAW_FILE = "raw.json"
TEXTS_FILE = "texte.json"
PENDING_FILE = "offene_pruefungen.yaml"
PENDING_KEY = "daniel_entscheidet"
UNKNOWN_REASON = "Der Grund wurde nicht gespeichert (Kennung: {code})"
NO_CODE = "ohne Angabe"
NOT_MEASURED_PREFIX = "nicht gemessen: "
FINDING_OUTCOME = "finding"
PROFILE_STEP = "place"
GRID_CHECK = "j3"
GAP_CHECK = "c12"
PROFILE_CHECK = "f1"
RANKING_CHECKS = ("j1", "j2")
SITE_FILE_CHARACTERS = re.compile(r"[^a-z0-9.-]+")
PROJECT_FILE_SUFFIX = ".pdf"
ASSISTANT_ROUTE = "assistant:"
SILENT_PREFIX = "assistants: no answers from "
TOP_FINDINGS = 3
MAX_COMPETITORS = 3
MAX_GAP_KEYWORDS = 10
PERCENT = 100
CUSTOMER_SUBJECT = "customer"
BAD_BELOW = Decimal(charts.BAD_BELOW)
WARN_BELOW = Decimal(charts.WARN_BELOW)

Trend = Literal["up", "down", "same"]
Level = Literal["ok", "warn", "bad"]


class ChartTitles(BaseModel):
    area_traffic_lights: str
    competitor_comparison: str
    grid_map: str
    score_trend: str
    ranking_table: str
    ranking_legend: str
    nationwide_keyword: str


class SubjectLabels(BaseModel):
    customer: str


class MetricLabels(BaseModel):
    reviews: str
    rating: str
    photos: str


class ReportLabels(BaseModel):
    charts: ChartTitles
    subjects: SubjectLabels
    metrics: MetricLabels
    profile_metrics: dict[str, str] = Field(default_factory=dict)
    assistants: dict[str, str] = Field(default_factory=dict)
    answer_routes: dict[str, str] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)
    outcomes: dict[str, str] = Field(default_factory=dict)
    paid_visibility: dict[str, str] = Field(default_factory=dict)
    cost_sources: dict[str, str] = Field(default_factory=dict)
    cost_services: dict[str, str] = Field(default_factory=dict)
    notes: dict[str, str] = Field(default_factory=dict)


class ProfileMetricLine(BaseModel):
    label: str
    value: int
    previous: int
    change_percent: float | None
    trend: Literal["up", "down", "same"]


class AreaLine(BaseModel):
    area_id: str
    name: str
    score: Decimal
    level: Level
    counted: int = 0
    ratable: int = 0


class FindingLine(BaseModel):
    area_name: str
    check_id: str
    severity: int
    title: str
    explanation: str
    customer_impact: str
    fix: str


class FindingGroup(BaseModel):
    area_name: str
    findings: list[FindingLine]


class VerdictLine(BaseModel):
    check_id: str
    name: str
    result: str
    measured: bool
    collected: bool | None
    data: bool | None
    evaluation: bool | None
    reason: str | None
    source: str = ""
    value: str | None = None


class OpenCheckLine(BaseModel):
    check_id: str
    name: str
    reason: str
    decided_by_daniel: bool
    failed: bool = False


class OpenGroup(BaseModel):
    names: list[str]
    reason: str
    decided_by_daniel: bool


class PaidLine(BaseModel):
    check_id: str
    name: str
    text: str
    measured: bool


class RecommendationReport(BaseModel):
    customer_name: str
    customer_address: str
    report_date: date
    site: str
    actions: list[FindingLine]


class AssistantLine(BaseModel):
    name: str
    answers: int
    mentioned: int
    route: str


class ChartFiles(BaseModel):
    areas: str | None = None
    competitors: str | None = None
    grid: str | None = None
    rankings: str | None = None
    trend: str | None = None


class SalesReport(BaseModel):
    customer_name: str
    customer_address: str
    report_date: date
    site: str
    total_score: Decimal | None
    total_counted: int = 0
    total_ratable: int = 0
    unfound_phrases: int = 0
    texts: ReportTexts | None
    areas: list[AreaLine]
    unrated_areas: list[str]
    strongest_area: AreaLine | None
    weakest_area: AreaLine | None
    charts: ChartFiles
    top_findings: list[FindingLine]
    finding_groups: list[FindingGroup]
    profile: list[ProfileMetricLine] = Field(default_factory=list)
    keyword_gap: list[GapKeyword] = Field(default_factory=list)
    open_checks: list[OpenCheckLine] = Field(default_factory=list)
    open_groups: list[OpenGroup] = Field(default_factory=list)
    verdicts: list[VerdictLine] = Field(default_factory=list)
    paid_visibility: list[PaidLine] = Field(default_factory=list)
    assistants: list[AssistantLine] = Field(default_factory=list)
    comparison_note: str | None = None
    costs: CostStatement | None = None

    @property
    def failed_count(self) -> int:
        return sum(1 for line in self.open_checks if line.failed)

    @property
    def decision_count(self) -> int:
        return sum(1 for line in self.open_checks if line.decided_by_daniel)


class ScoreChange(BaseModel):
    label: str
    before: Decimal | None
    after: Decimal | None
    trend: Trend


class RankingChange(BaseModel):
    phrase: str
    organic_before: int | None
    organic_after: int | None
    organic_trend: Trend
    local_before: int | None
    local_after: int | None
    local_trend: Trend
    nationwide: bool = False


class MonthlyReport(BaseModel):
    customer_name: str
    report_date: date
    previous_date: date
    total: ScoreChange
    areas: list[ScoreChange]
    rankings: list[RankingChange]
    done: list[FindingLine]
    next_steps: list[FindingLine]
    texts: ReportTexts | None
    charts: ChartFiles
    profile: list[ProfileMetricLine] = Field(default_factory=list)


class AuditBundle(BaseModel):
    audit: Audit
    results: list[CheckResult]
    findings: list[Finding]
    rankings: list[Ranking]


def _today() -> date:
    return datetime.now(UTC).date()


def load_labels(templates_dir: Path) -> ReportLabels:
    path = templates_dir / REPORT_TEMPLATE_DIR / LABELS_FILE
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ReportLabels.model_validate(content)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RenderError(f"report labels {path} cannot be read: {exc}") from exc


def audit_rankings(repositories: Repositories, audit_id: UUID) -> list[Ranking]:
    reader: Callable[[UUID], list[Ranking]] | None = getattr(repositories, "rankings", None)
    if reader is None:
        logger.warning("Repositories cannot read rankings; report shows none")
        return []
    return list(reader(audit_id))


def _bundle(repositories: Repositories, audit: Audit) -> AuditBundle:
    return AuditBundle(
        audit=audit,
        results=repositories.check_results(audit.id),
        findings=repositories.findings_of_audit(audit.id),
        rankings=audit_rankings(repositories, audit.id),
    )


def _download_text(
    repositories: Repositories, remote_path: str, scratch: Path, name: str
) -> str | None:
    try:
        local = repositories.download(RAW_BUCKET, remote_path, scratch / name)
        return local.read_text(encoding="utf-8")
    except (StorageError, OSError) as exc:
        logger.warning("%s/%s not available: %s", RAW_BUCKET, remote_path, exc)
        return None


def load_texts(repositories: Repositories, audit_id: UUID, scratch: Path) -> ReportTexts | None:
    content = _download_text(repositories, f"{audit_id}/{TEXTS_FILE}", scratch, TEXTS_FILE)
    if content is None:
        return None
    try:
        return parse_texts(content)
    except RenderError as exc:
        logger.warning("Audit %s: %s; standard sentences are used", audit_id, exc)
        return None


def load_raw(repositories: Repositories, audit: Audit, scratch: Path) -> RawAudit | None:
    if audit.raw_data_path is None:
        return None
    content = _download_text(repositories, audit.raw_data_path, scratch, RAW_FILE)
    if content is None:
        return None
    try:
        return RawAudit.model_validate_json(content)
    except ValidationError as exc:
        logger.warning("Audit %s has invalid raw data: %s", audit.id, exc.errors()[0]["msg"])
        return None


def _level(score: Decimal) -> Level:
    if score < BAD_BELOW:
        return "bad"
    if score < WARN_BELOW:
        return "warn"
    return "ok"


def area_lines(results: list[CheckResult], catalogue: Catalogue) -> list[AreaLine]:
    names = {area.id: area.name for area in catalogue.areas}
    lines: list[AreaLine] = []
    for area_id, score in area_scores(results, catalogue).items():
        counted, ratable = coverage(results, catalogue, area_id)
        lines.append(
            AreaLine(
                area_id=area_id,
                name=names[area_id],
                score=score,
                level=_level(score),
                counted=counted,
                ratable=ratable,
            )
        )
    return lines


def coverage(
    results: list[CheckResult], catalogue: Catalogue, area_id: str | None = None
) -> tuple[int, int]:
    by_id = {result.check_id: result for result in results}
    counted = ratable = 0
    for area in catalogue.areas:
        if area_id is not None and area.id != area_id:
            continue
        for check in area.checks:
            result = by_id.get(check.id)
            if not check.counts_in_score or result is None or result.outcome in UNRATED_OUTCOMES:
                continue
            ratable += 1
            counted += _counted(result)
    return counted, ratable


def unrated_areas(lines: list[AreaLine], catalogue: Catalogue) -> list[str]:
    scored = {line.area_id for line in lines}
    return [
        area.name
        for area in catalogue.areas
        if area.id not in scored and any(check.counts_in_score for check in area.checks)
    ]


def load_pending(catalogue_path: Path) -> frozenset[str]:
    path = catalogue_path.parent / PENDING_FILE
    if not path.exists():
        return frozenset()
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        ids = content[PENDING_KEY]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise RenderError(f"open checks {path} cannot be read: {exc}") from exc
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise RenderError(f"open checks {path}: {PENDING_KEY} must list check ids")
    return frozenset(ids)


def open_checks(
    results: list[CheckResult],
    catalogue: Catalogue,
    labels: ReportLabels,
    pending: frozenset[str],
) -> list[OpenCheckLine]:
    by_id = {result.check_id: result for result in results}
    lines: list[OpenCheckLine] = []
    for area in catalogue.areas:
        for check in area.checks:
            result = by_id.get(check.id)
            if result is not None and result.outcome is not CheckOutcome.NOT_MEASURABLE:
                continue
            lines.append(
                OpenCheckLine(
                    check_id=check.id,
                    name=check.name,
                    reason=_open_reason(check, result, labels),
                    decided_by_daniel=check.id in pending,
                    failed=result is not None and not is_planned(result),
                )
            )
    return lines


def group_open_checks(lines: list[OpenCheckLine]) -> list[OpenGroup]:
    groups: dict[tuple[str, bool], list[str]] = {}
    for line in lines:
        groups.setdefault((line.reason, line.decided_by_daniel), []).append(line.name)
    return [
        OpenGroup(names=names, reason=reason, decided_by_daniel=decided)
        for (reason, decided), names in groups.items()
    ]


def short_reason(reason: str) -> str:
    if len(reason) <= SHORT_REASON_CHARS:
        return reason
    cut = reason[:SHORT_REASON_CHARS].rsplit(" ", 1)[0].rstrip(";,: ")
    return f"{cut} {SHORT_REASON_SUFFIX}"


def _open_reason(check: CheckDefinition, result: CheckResult | None, labels: ReportLabels) -> str:
    if result is None:
        code = "paid" if check.requires_paid else "access" if check.requires_access else ""
    else:
        code = str(result.measured.get("reason", ""))
    return reason_text(code, result, labels)


def _plain(reason: str) -> str:
    text = reason.strip()
    while text.lower().startswith(NOT_MEASURED_PREFIX):
        text = text[len(NOT_MEASURED_PREFIX) :].lstrip()
    return text


def reason_text(code: str, result: CheckResult | None, labels: ReportLabels) -> str:
    label = None if code == INVALID_REASON_CODE else labels.reasons.get(code)
    if label:
        return label
    if result is not None:
        invalid = result.measured.get("invalid_reason")
        if invalid:
            return _plain(str(invalid))
        failed = _failed_reason(result)
        if failed:
            return _plain(failed)
    return UNKNOWN_REASON.format(code=code or NO_CODE)


def assistant_lines(raw: RawAudit | None, labels: ReportLabels) -> list[AssistantLine]:
    if raw is None:
        return []
    answered = [(_provider_key(answer.provider), answer) for answer in raw.assistant_answers]
    asked = {provider for provider, _ in answered}
    asked.update(
        check.route.removeprefix(ASSISTANT_ROUTE)
        for check in raw.preflight
        if check.route.startswith(ASSISTANT_ROUTE)
    )
    for warning in raw.warnings:
        if warning.startswith(SILENT_PREFIX):
            names = warning.removeprefix(SILENT_PREFIX).split(",")
            asked.update(name.strip() for name in names)
    lines: list[AssistantLine] = []
    for provider, name in labels.assistants.items():
        if provider not in asked:
            continue
        answers = [answer for key, answer in answered if key == provider]
        routes = {answer.route for answer in answers if answer.route is not None}
        route = "manual" if "manual" in routes else min(routes) if routes else "none"
        lines.append(
            AssistantLine(
                name=name,
                answers=len(answers),
                mentioned=sum(1 for answer in answers if answer.mentioned),
                route=labels.answer_routes.get(route, route),
            )
        )
    return lines


def _provider_key(provider: str) -> str:
    source = provider_source(provider)
    return source.value if source is not None else provider


def check_order(check_id: str) -> tuple[str, int]:
    digits = check_id[1:]
    return check_id[:1], int(digits) if digits.isdigit() else 0


def verdict_lines(
    results: list[CheckResult], catalogue: Catalogue, labels: ReportLabels
) -> list[VerdictLine]:
    checks = {check.id: check for area in catalogue.areas for check in area.checks}
    lines: list[VerdictLine] = []
    for result in sorted(results, key=lambda item: check_order(item.check_id)):
        if result.control_state is not ControlState.DONE or is_planned(result):
            continue
        check = checks.get(result.check_id)
        measured = result.outcome is not CheckOutcome.NOT_MEASURABLE
        outcome = (
            FINDING_OUTCOME
            if measured and check is not None and not check.counts_in_score
            else result.outcome.value
        )
        lines.append(
            VerdictLine(
                check_id=result.check_id,
                name=check.name if check else result.check_id,
                result=labels.outcomes.get(outcome, outcome),
                measured=measured,
                collected=result.verdict_collected_ok,
                data=result.verdict_data_ok if measured else None,
                evaluation=result.verdict_evaluation_ok if measured else None,
                reason=_line_reason(result, measured),
                source=check.source if check else "",
                value=comparison_text(result) if measured else None,
            )
        )
    return lines


def comparison_text(result: CheckResult) -> str | None:
    measured = result.measured
    if result.check_id == "j3":
        return _grid_text(measured)
    if "mentioned" not in measured or "answers" not in measured:
        return None
    text = f"{measured['mentioned']} von {measured['answers']} Antworten nennen den Betrieb"
    rivals = measured.get("competitors")
    if isinstance(rivals, list) and rivals and isinstance(rivals[0], dict):
        best = rivals[0]
        return (
            f"{text}; stärkster Wettbewerber ({best.get('name')}): "
            f"{best.get('mentioned')} von {best.get('answers')}"
        )
    return f"{text}; Wettbewerberwerte liegen nicht vor"


def _grid_text(measured: dict[str, object]) -> str | None:
    if "points_in_top" not in measured or "points_total" not in measured:
        return None
    text = (
        f"{measured['points_in_top']} von {measured['points_total']} Rasterpunkten "
        f"in den ersten {measured.get('top')}"
    )
    if measured.get("competitor_values") is None:
        return f"{text}; Wettbewerberwerte liegen nicht vor"
    return text


def _line_reason(result: CheckResult, measured: bool) -> str | None:
    failed = _failed_reason(result)
    if measured:
        return failed and short_reason(failed)
    invalid = result.measured.get("invalid_reason")
    parts = [_plain(str(invalid))] if invalid else []
    if failed and not any(_plain(failed) in part for part in parts):
        parts.append(_plain(failed))
    joined = "; ".join(parts)
    return short_reason(joined) if joined else None


def paid_lines(
    results: list[CheckResult], catalogue: Catalogue, labels: ReportLabels
) -> list[PaidLine]:
    names = {check.id: check.name for area in catalogue.areas for check in area.checks}
    by_id = {result.check_id: result for result in results}
    lines: list[PaidLine] = []
    for check_id in PAID_CHECKS:
        result = by_id.get(check_id)
        if result is None or is_planned(result):
            continue
        name = names.get(check_id, check_id)
        if result.outcome is CheckOutcome.NOT_MEASURABLE:
            reason = short_reason(
                reason_text(str(result.measured.get("reason", "")), result, labels)
            )
            lines.append(
                PaidLine(
                    check_id=check_id,
                    name=name,
                    text=labels.paid_visibility["not_measured"].format(reason=reason),
                    measured=False,
                )
            )
            continue
        text = _paid_text(check_id, result.measured, labels)
        if text:
            lines.append(PaidLine(check_id=check_id, name=name, text=text, measured=True))
    return lines


def _paid_text(check_id: str, measured: dict[str, object], labels: ReportLabels) -> str:
    texts = labels.paid_visibility
    if check_id == "p1":
        key = "p1_shown" if measured.get("shows_ads") else "p1_none"
        return texts[key].format(count=measured.get("ads_found", 0))
    if check_id == "p2":
        key = "p2_shown" if measured.get("shows_ads") else "p2_none"
        return texts[key].format(count=measured.get("own_ads", 0))
    if check_id == "p3":
        return texts["p3"].format(
            shown=measured.get("phrases_with_own_ad", 0), total=measured.get("phrases_total", 0)
        )
    rivals = measured.get("competitors_on_brand") or []
    if not rivals:
        return texts["p4_none"]
    names = ", ".join(str(item) for item in rivals) if isinstance(rivals, list) else str(rivals)
    return texts["p4_some"].format(count=measured.get("competitors_on_brand_count", 0), names=names)


def _failed_reason(result: CheckResult) -> str | None:
    for flag, reason in (
        (result.verdict_collected_ok, result.verdict_collected_reason),
        (result.verdict_data_ok, result.verdict_data_reason),
        (result.verdict_evaluation_ok, result.verdict_evaluation_reason),
    ):
        if flag is False:
            return reason
    return None


def finding_lines(
    findings: list[Finding],
    results: list[CheckResult],
    catalogue: Catalogue,
    *,
    confirmed_only: bool = True,
) -> list[FindingLine]:
    names = {area.id: area.name for area in catalogue.areas}
    area_of = {result.check_id: result.area for result in results}
    confirmed = {
        result.check_id for result in results if result.verdicts_all_ok or not confirmed_only
    }
    ordered = sorted(
        (finding for finding in findings if finding.check_id in confirmed),
        key=lambda finding: (-finding.severity, check_order(finding.check_id)),
    )
    return [
        FindingLine(
            area_name=names.get(area_of.get(finding.check_id, finding.check_id[:1]), ""),
            **finding.model_dump(
                include={"check_id", "severity", "title", "explanation", "customer_impact", "fix"}
            ),
        )
        for finding in ordered
    ]


def group_findings(lines: list[FindingLine], catalogue: Catalogue) -> list[FindingGroup]:
    groups: list[FindingGroup] = []
    for area in catalogue.areas:
        members = sorted(
            (line for line in lines if line.area_name == area.name),
            key=lambda line: check_order(line.check_id),
        )
        if members:
            groups.append(FindingGroup(area_name=area.name, findings=members))
    return groups


def competitor_values(
    raw: RawAudit | None, labels: ReportLabels
) -> dict[str, dict[str, float]] | None:
    if raw is None or raw.place is None or not raw.competitor_places:
        return None
    metrics = labels.metrics

    def row(place: PlaceSnapshot) -> dict[str, float]:
        return {
            metrics.reviews: float(place.review_count),
            metrics.rating: place.rating or 0.0,
            metrics.photos: float(place.photo_count),
        }

    values = {labels.subjects.customer: row(raw.place)}
    for place in raw.competitor_places[:MAX_COMPETITORS]:
        values[place.name] = row(place)
    return values


def profile_lines(raw: RawAudit | None, labels: ReportLabels) -> list[ProfileMetricLine]:
    if raw is None or raw.access is None or not raw.access.profile_metrics:
        return []
    now = raw.access.profile_metrics
    before = raw.access.profile_metrics_previous
    lines: list[ProfileMetricLine] = []
    for metric, label in labels.profile_metrics.items():
        if metric not in now:
            continue
        value, previous = now[metric], before.get(metric, 0)
        change = round((value - previous) / previous * PERCENT, 1) if previous else None
        trend: Trend = "same" if value == previous else ("up" if value > previous else "down")
        lines.append(
            ProfileMetricLine(
                label=label, value=value, previous=previous, change_percent=change, trend=trend
            )
        )
    return lines


def _counted(result: CheckResult | None) -> bool:
    return result is not None and result.outcome in POINTS and result.verdicts_all_ok


def gap_keywords(results: list[CheckResult]) -> list[GapKeyword]:
    result = next((item for item in results if item.check_id == GAP_CHECK), None)
    if not _counted(result) or result is None:
        return []
    entries = result.measured.get("top_gap_keywords")
    if not isinstance(entries, list):
        return []
    keywords: list[GapKeyword] = []
    for entry in entries:
        try:
            keywords.append(GapKeyword.model_validate(entry))
        except ValidationError:
            logger.warning("A gap keyword of check %s is unreadable and left out", GAP_CHECK)
    return sorted(keywords, key=lambda keyword: -keyword.volume)[:MAX_GAP_KEYWORDS]


def profile_invalid_reason(
    results: list[CheckResult], catalogue: Catalogue, labels: ReportLabels
) -> str | None:
    definitions = {check.id: check for area in catalogue.areas for check in area.checks}
    for result in results:
        check = definitions.get(result.check_id)
        if check is None or PROFILE_STEP not in steps_of_check(check.id, check.measurement):
            continue
        if result.measured.get("reason") == INVALID_REASON_CODE:
            return reason_text(INVALID_REASON_CODE, result, labels)
    return None


def comparison_note(
    raw: RawAudit | None, results: list[CheckResult], catalogue: Catalogue, labels: ReportLabels
) -> str | None:
    invalid = profile_invalid_reason(results, catalogue, labels)
    if invalid is not None:
        return labels.notes["profile_invalid"].format(reason=invalid)
    profile = next((item for item in results if item.check_id == PROFILE_CHECK), None)
    if raw is not None and raw.place is None and profile is not None:
        if profile.outcome is CheckOutcome.NOT_MEASURABLE:
            reason = reason_text(str(profile.measured.get("reason", "")), profile, labels)
            return labels.notes["profile_unread"].format(reason=reason)
    return None


def grid_points(keywords: list[Keyword], rankings: list[Ranking]) -> dict[str, int | None]:
    gridded = [r for r in rankings if r.subject == CUSTOMER_SUBJECT and r.grid_point is not None]
    if not gridded:
        return {}
    measured = {ranking.keyword_id for ranking in gridded}
    candidates = sorted(
        (keyword for keyword in keywords if keyword.id in measured),
        key=lambda keyword: not keyword.is_primary,
    )
    chosen = candidates[0].id if candidates else gridded[0].keyword_id
    return {
        r.grid_point: r.local_pack_position
        for r in gridded
        if r.keyword_id == chosen and r.grid_point
    }


def _draw_optional(draw: Callable[[], Path], name: str) -> str | None:
    try:
        return draw().as_uri()
    except RenderError as exc:
        logger.warning("Chart %s left out: %s", name, exc)
        return None


def _sales_charts(
    lines: list[AreaLine],
    raw: RawAudit | None,
    keywords: list[Keyword],
    rankings: list[Ranking],
    labels: ReportLabels,
    chart_dir: Path,
    results: list[CheckResult],
    catalogue: Catalogue,
) -> ChartFiles:
    titles = labels.charts
    files = ChartFiles()
    if lines:
        files.areas = charts.area_traffic_lights(
            {line.area_id: line.score for line in lines},
            chart_dir / "bereiche.png",
            area_names={line.area_id: line.name for line in lines},
            title=titles.area_traffic_lights,
        ).as_uri()
    by_id = {result.check_id: result for result in results}
    values = (
        competitor_values(raw, labels)
        if profile_invalid_reason(results, catalogue, labels) is None
        else None
    )
    if values:
        files.competitors = charts.competitor_comparison(
            values, chart_dir / "wettbewerber.png", title=titles.competitor_comparison
        ).as_uri()
    points = grid_points(keywords, rankings) if _counted(by_id.get(GRID_CHECK)) else {}
    if points:
        files.grid = _draw_optional(
            lambda: charts.grid_map(points, chart_dir / "karte.png", title=titles.grid_map),
            "grid map",
        )
    if any(_counted(by_id.get(check_id)) for check_id in RANKING_CHECKS):
        files.rankings = _ranking_chart(keywords, rankings, labels, chart_dir)
    return files


def _ranking_chart(
    keywords: list[Keyword], rankings: list[Ranking], labels: ReportLabels, chart_dir: Path
) -> str | None:
    phrases = {
        keyword.id: (
            f"{keyword.phrase} ({labels.charts.nationwide_keyword})"
            if keyword.scope is KeywordScope.NATIONWIDE
            else keyword.phrase
        )
        for keyword in keywords
    }
    found = _found_keywords(rankings)
    return _draw_optional(
        lambda: charts.ranking_table_image(
            [ranking for ranking in rankings if ranking.keyword_id in found],
            chart_dir / "plaetze.png",
            keyword_phrases=phrases,
            subject_labels={CUSTOMER_SUBJECT: labels.subjects.customer},
            title=labels.charts.ranking_table,
            legend=labels.charts.ranking_legend,
        ),
        "ranking table",
    )


def _found_keywords(rankings: list[Ranking]) -> set[UUID]:
    return {
        ranking.keyword_id
        for ranking in rankings
        if ranking.grid_point is None
        and (ranking.organic_position is not None or ranking.local_pack_position is not None)
    }


def unfound_phrase_count(rankings: list[Ranking]) -> int:
    listed = {ranking.keyword_id for ranking in rankings if ranking.grid_point is None}
    return len(listed - _found_keywords(rankings))


def _costs(
    repositories: Repositories, audit_id: UUID, labels: ReportLabels, settings: Settings
) -> CostStatement | None:
    if not settings.report_show_costs:
        return None
    return cost_statement(
        repositories.audit_costs(audit_id), labels.cost_sources, labels.cost_services
    )


def _address(location: Location) -> str:
    town = f"{location.postal_code} {location.city}".strip()
    return ", ".join(part for part in (location.street.strip(), town) if part)


def _total(audit: Audit, results: list[CheckResult], catalogue: Catalogue) -> Decimal | None:
    if audit.total_score is not None:
        return audit.total_score
    if not any(result.outcome in POINTS for result in results):
        return None
    return total_score(results, catalogue)


def sales_report_context(
    audit_id: UUID, repositories: Repositories, settings: Settings, work_dir: Path
) -> SalesReport:
    audit = repositories.audit(audit_id)
    customer = repositories.customer(audit.customer_id)
    keywords = repositories.keywords(audit.customer_id)
    locations = repositories.locations(audit.customer_id)
    bundle = _bundle(repositories, audit)
    catalogue = load_catalogue(settings.catalogue_path)
    labels = load_labels(settings.templates_dir)
    lines = area_lines(bundle.results, catalogue)
    findings = finding_lines(bundle.findings, bundle.results, catalogue)
    ranked = sorted(lines, key=lambda line: line.score)
    raw = load_raw(repositories, audit, work_dir)
    pending = load_pending(settings.catalogue_path)
    total_counted, total_ratable = coverage(bundle.results, catalogue)
    open_lines = open_checks(bundle.results, catalogue, labels, pending)
    return SalesReport(
        open_checks=open_lines,
        open_groups=group_open_checks(open_lines),
        verdicts=verdict_lines(bundle.results, catalogue, labels),
        paid_visibility=paid_lines(bundle.results, catalogue, labels),
        assistants=assistant_lines(raw, labels),
        comparison_note=comparison_note(raw, bundle.results, catalogue, labels),
        customer_name=customer.name,
        customer_address=_address(locations[0]) if locations else customer.city,
        report_date=_today(),
        site=site_name(customer.website, customer.name),
        total_score=_total(audit, bundle.results, catalogue),
        total_counted=total_counted,
        total_ratable=total_ratable,
        unfound_phrases=unfound_phrase_count(bundle.rankings),
        texts=load_texts(repositories, audit_id, work_dir),
        areas=lines,
        unrated_areas=unrated_areas(lines, catalogue),
        strongest_area=ranked[-1] if ranked else None,
        weakest_area=ranked[0] if ranked else None,
        charts=_sales_charts(
            lines, raw, keywords, bundle.rankings, labels, work_dir, bundle.results, catalogue
        ),
        top_findings=findings[:TOP_FINDINGS],
        finding_groups=group_findings(findings, catalogue),
        profile=profile_lines(raw, labels),
        keyword_gap=gap_keywords(bundle.results),
        costs=_costs(repositories, audit_id, labels, settings),
    )


def _report_path(settings: Settings, audit_id: UUID, prefix: str, name: str, day: date) -> Path:
    file_name = f"{prefix}_{filename_slug(name)}_{day.isoformat()}.pdf"
    return settings.radar_work_dir / REPORT_DIR / str(audit_id) / file_name


def site_name(website: object, customer_name: str) -> str:
    host = host_of(str(website) if website is not None else None)
    if host:
        return SITE_FILE_CHARACTERS.sub("_", host).strip("_.")
    return filename_slug(customer_name)


def deliverable_path(settings: Settings, audit_id: UUID, prefix: str, site: str) -> Path:
    name = f"{prefix}_{site}{PROJECT_FILE_SUFFIX}"
    folder = settings.analysis_project_dir
    if not (folder.is_dir() and os.access(folder, os.W_OK)):
        raise RenderError(
            f"cannot write {name} of audit {audit_id}: the project folder {folder} "
            "does not exist or is not writable; set ANALYSIS_PROJECT_DIR to a writable folder"
        )
    return folder / name


def _render(settings: Settings, template: str, report: BaseModel, output: Path) -> Path:
    html = render_template(
        settings.templates_dir / REPORT_TEMPLATE_DIR, template, {"report": report}
    )
    return write_pdf(html, output)


def build_sales_report(audit_id: UUID, repositories: Repositories, settings: Settings) -> Path:
    with tempfile.TemporaryDirectory(prefix="radar-report-") as scratch:
        report = sales_report_context(audit_id, repositories, settings, Path(scratch))
        output = deliverable_path(settings, audit_id, SALES_FILE_PREFIX, report.site)
        return _render(settings, SALES_TEMPLATE, report, output)


def recommendation_context(
    audit_id: UUID, repositories: Repositories, settings: Settings
) -> RecommendationReport:
    audit = repositories.audit(audit_id)
    customer = repositories.customer(audit.customer_id)
    locations = repositories.locations(audit.customer_id)
    bundle = _bundle(repositories, audit)
    catalogue = load_catalogue(settings.catalogue_path)
    return RecommendationReport(
        customer_name=customer.name,
        customer_address=_address(locations[0]) if locations else customer.city,
        report_date=_today(),
        site=site_name(customer.website, customer.name),
        actions=_one_action_per_check(finding_lines(bundle.findings, bundle.results, catalogue)),
    )


def _one_action_per_check(lines: list[FindingLine]) -> list[FindingLine]:
    seen: set[str] = set()
    actions: list[FindingLine] = []
    for line in lines:
        if line.check_id not in seen:
            seen.add(line.check_id)
            actions.append(line)
    return actions


def build_recommendation(audit_id: UUID, repositories: Repositories, settings: Settings) -> Path:
    report = recommendation_context(audit_id, repositories, settings)
    output = deliverable_path(settings, audit_id, RECOMMENDATION_FILE_PREFIX, report.site)
    return _render(settings, RECOMMENDATION_TEMPLATE, report, output)


def _score_trend(before: Decimal | None, after: Decimal | None) -> Trend:
    if before is None or after is None or before == after:
        return "same"
    return "up" if after > before else "down"


def _position_trend(before: int | None, after: int | None) -> Trend:
    if before == after:
        return "same"
    if after is None:
        return "down"
    if before is None or after < before:
        return "up"
    return "down"


def score_changes(
    before: list[CheckResult], after: list[CheckResult], catalogue: Catalogue
) -> list[ScoreChange]:
    old = area_scores(before, catalogue)
    new = area_scores(after, catalogue)
    return [
        ScoreChange(
            label=area.name,
            before=old.get(area.id),
            after=new.get(area.id),
            trend=_score_trend(old.get(area.id), new.get(area.id)),
        )
        for area in catalogue.areas
        if area.id in old or area.id in new
    ]


def ranking_changes(
    keywords: list[Keyword], before: list[Ranking], after: list[Ranking]
) -> list[RankingChange]:
    old = best_positions(before)
    new = best_positions(after)
    changes: list[RankingChange] = []
    for keyword in keywords:
        if keyword.id not in old and keyword.id not in new:
            continue
        organic_before, local_before = old.get(keyword.id, (None, None))
        organic_after, local_after = new.get(keyword.id, (None, None))
        changes.append(
            RankingChange(
                phrase=keyword.phrase,
                nationwide=keyword.scope is KeywordScope.NATIONWIDE,
                organic_before=organic_before,
                organic_after=organic_after,
                organic_trend=_position_trend(organic_before, organic_after),
                local_before=local_before,
                local_after=local_after,
                local_trend=_position_trend(local_before, local_after),
            )
        )
    return changes


def resolved_findings(previous: list[Finding], current: list[CheckResult]) -> list[Finding]:
    passed = {r.check_id for r in current if r.outcome is CheckOutcome.PASSED}
    return [finding for finding in previous if finding.check_id in passed]


def score_history(audits: list[Audit], current: Audit) -> list[tuple[str, Decimal]]:
    shown = {audit.id: audit for audit in audits if audit.status is AuditStatus.REPORTED}
    shown[current.id] = current
    ordered = sorted(shown.values(), key=lambda audit: audit.requested_at)
    return [
        (german_date(audit.requested_at), audit.total_score)
        for audit in ordered
        if audit.total_score is not None
    ]


def monthly_report_context(
    audit_id: UUID, repositories: Repositories, settings: Settings, work_dir: Path
) -> MonthlyReport:
    audit = repositories.audit(audit_id)
    previous_audit = repositories.previous_audit(audit)
    if previous_audit is None:
        raise RenderError(f"audit {audit_id} has no previous reported audit to compare with")
    customer = repositories.customer(audit.customer_id)
    keywords = repositories.keywords(audit.customer_id)
    catalogue = load_catalogue(settings.catalogue_path)
    labels = load_labels(settings.templates_dir)
    current = _bundle(repositories, audit)
    previous = _bundle(repositories, previous_audit)
    history = score_history(repositories.audits_of_customer(audit.customer_id), audit)
    files = _trend_charts(history, work_dir, labels)
    total = _total_change(previous, current, catalogue)
    raw = load_raw(repositories, audit, work_dir)
    return MonthlyReport(
        customer_name=customer.name,
        report_date=_today(),
        previous_date=previous_audit.requested_at.date(),
        total=total,
        areas=score_changes(previous.results, current.results, catalogue),
        rankings=ranking_changes(keywords, previous.rankings, current.rankings),
        done=finding_lines(
            resolved_findings(previous.findings, current.results),
            previous.results,
            catalogue,
            confirmed_only=False,
        ),
        next_steps=finding_lines(current.findings, current.results, catalogue),
        texts=load_texts(repositories, audit_id, work_dir),
        charts=files,
        profile=profile_lines(raw, labels),
    )


def _trend_charts(
    history: list[tuple[str, Decimal]], work_dir: Path, labels: ReportLabels
) -> ChartFiles:
    files = ChartFiles()
    if history:
        files.trend = charts.score_trend(
            history, work_dir / "verlauf.png", title=labels.charts.score_trend
        ).as_uri()
    return files


def _total_change(previous: AuditBundle, current: AuditBundle, catalogue: Catalogue) -> ScoreChange:
    before = _total(previous.audit, previous.results, catalogue)
    after = _total(current.audit, current.results, catalogue)
    return ScoreChange(label="", before=before, after=after, trend=_score_trend(before, after))


def build_monthly_report(audit_id: UUID, repositories: Repositories, settings: Settings) -> Path:
    with tempfile.TemporaryDirectory(prefix="radar-report-") as scratch:
        report = monthly_report_context(audit_id, repositories, settings, Path(scratch))
        output = _report_path(
            settings, audit_id, MONTHLY_FILE_PREFIX, report.customer_name, report.report_date
        )
        return _render(settings, MONTHLY_TEMPLATE, report, output)
