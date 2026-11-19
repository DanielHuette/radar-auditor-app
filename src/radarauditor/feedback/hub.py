import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from radarauditor.collectors.snapshot import MeasurementRecord
from radarauditor.config import Settings
from radarauditor.db.models import CheckOutcome, CheckResult
from radarauditor.db.repositories import Repositories
from radarauditor.errors import AuditHalted, ScoringError
from radarauditor.feedback import sinks
from radarauditor.feedback.events import (
    INVALID_REASON_CODE,
    MEASUREMENT_LABELS,
    NOT_BUILT_REASON,
    UNKNOWN_REASON,
    AffectedCheck,
    AuditEvent,
    Result,
    RouteReport,
    check_line,
    failure_line,
    overview_table,
    plain_reason,
    reason_of_code,
)
from radarauditor.feedback.routes import (
    ASSISTANT_GROUP,
    ASSISTANT_PREFIX,
    RouteMap,
    load_route_map,
    route_label,
)
from radarauditor.scoring.catalog import (
    AGENT_PENDING_REASON,
    Catalogue,
    CheckDefinition,
    load_catalogue,
)

logger = logging.getLogger(__name__)

EVENTS_FOLDER = "ereignisse"
MEASURED_OUTCOMES = frozenset({CheckOutcome.PASSED, CheckOutcome.PARTIAL, CheckOutcome.FAILED})
NOT_YET_MEASURABLE = frozenset({"needs_history", AGENT_PENDING_REASON, NOT_BUILT_REASON})
OPEN_REASON = "Offen, Entscheidung Daniel"
PAID_REASON = "Der Bezahldienst DataForSEO ist ausgesetzt"
ACCESS_REASON = "Nur mit Kundenzugang möglich"
NO_RESULT_REASON = "Für diese Prüfung liegt kein Ergebnis vor"
HALT_NOTE = "Der Audit ist angehalten und wartet auf Daniel"
NOTICE_CHECKS = 3
CARD_CHECKS = 5
FETCH_STAGE = "holen"
CHECK_STAGE = "pruefen"
EVALUATION_ROUTE = "auswertung"
EVALUATION_LABEL = "Auswertung"
CONTROL_ROUTE = "kontrolle"


def card_key(audit_id: UUID) -> str:
    return f"A-{str(audit_id)[:8]}"


@dataclass(frozen=True)
class Channels:
    file: Callable[[AuditEvent], None]
    database: Callable[[list[AuditEvent]], None]
    board: Callable[[str], None]
    notice: Callable[[str, bool], None]
    overview: Callable[[str], None]


@dataclass(frozen=True)
class RouteOutcome:
    route: str
    result: Result
    reason: str | BaseException | None = None


class Feedback:
    def __init__(
        self,
        *,
        audit_id: UUID,
        business: str,
        routes: RouteMap,
        halt_stages: tuple[int, ...],
        channels: Channels,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.audit_id = audit_id
        self.business = business
        self.reports: list[RouteReport] = []
        self._routes = routes
        self._halt_stages = frozenset(halt_stages)
        self._channels = channels
        self._clock = clock
        self._announced: set[tuple[str, str]] = set()
        self._lines: list[str] = []

    @classmethod
    def create(
        cls, audit_id: UUID, business: str, settings: Settings, repositories: Repositories
    ) -> "Feedback":
        catalogue = load_catalogue(settings.catalogue_path)
        routes = load_route_map(settings.routes_path, catalogue)
        folder = settings.radar_work_dir / EVENTS_FOLDER
        channels = Channels(
            file=sinks.file_sink(folder, audit_id),
            database=sinks.database_sink(repositories, audit_id),
            board=sinks.board_sink(settings.handoff_board_path, card_key(audit_id)),
            notice=sinks.ntfy_notice(
                settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token
            ),
            overview=sinks.overview_sink(folder, audit_id),
        )
        return cls(
            audit_id=audit_id,
            business=business,
            routes=routes,
            halt_stages=settings.halt_stages,
            channels=channels,
        )

    @classmethod
    def quiet(cls, audit_id: UUID, business: str, settings: Settings) -> "Feedback":
        catalogue = load_catalogue(settings.catalogue_path)

        def ignore(*_: object) -> None:
            return None

        return cls(
            audit_id=audit_id,
            business=business,
            routes=load_route_map(settings.routes_path, catalogue),
            halt_stages=(),
            channels=Channels(ignore, ignore, ignore, ignore, ignore),
        )

    def route_done(
        self,
        route: str,
        result: Result,
        reason: str | BaseException | None = None,
        *,
        phase: str = "messung",
    ) -> None:
        self.routes_done([RouteOutcome(route, result, reason)], phase=phase)

    def measurement_running(self, step: str, supplier: str, stage: str = FETCH_STAGE) -> None:
        self._write_measurement(
            step,
            supplier,
            Result.RUNNING,
            None,
            {"step": step, "supplier": supplier, "stage": stage},
        )

    def evaluation_running(self) -> None:
        self._write(
            AuditEvent(
                time=self._clock(),
                kind="measurement",
                phase="auswertung",
                result=Result.RUNNING,
                route=EVALUATION_ROUTE,
                label=EVALUATION_LABEL,
                protocol={"stage": EVALUATION_ROUTE},
            )
        )

    def control_event(
        self, check_id: str, result: Result, reason: str | None, protocol: dict[str, Any]
    ) -> None:
        entry = self._routes.entry(check_id)
        self._write(
            AuditEvent(
                time=self._clock(),
                kind="measurement",
                phase="auswertung",
                result=result,
                reason=reason,
                route=f"{CONTROL_ROUTE}:{check_id}",
                label=entry.name,
                check_number=entry.number,
                check_id=check_id,
                protocol=protocol,
            )
        )

    def _write(self, event: AuditEvent) -> None:
        self._channels.file(event)
        self._channels.database([event])

    def measurement_done(self, record: MeasurementRecord) -> None:
        result = Result.VALID if record.valid else Result.INVALID
        self._write_measurement(
            record.step, record.supplier, result, record.reason, record.model_dump(mode="json")
        )

    def _write_measurement(
        self,
        step: str,
        supplier: str,
        result: Result,
        reason: str | None,
        protocol: dict[str, Any],
    ) -> None:
        self._write(
            AuditEvent(
                time=self._clock(),
                kind="measurement",
                phase="messung",
                result=result,
                reason=reason,
                route=f"messung:{step}",
                label=MEASUREMENT_LABELS.get(step, step),
                protocol=protocol,
            )
        )

    def routes_done(self, outcomes: list[RouteOutcome], *, phase: str = "messung") -> None:
        events = [self._route_event(outcome, phase) for outcome in outcomes]
        for event in events:
            self._channels.file(event)
            self._keep_report(event)
        self._channels.database(events)
        failures = [event for event in events if event.result is Result.FAILED]
        halting = [event for event in failures if event.stage in self._halt_stages]
        self._announce_failures(failures, halted=bool(halting))
        if halting:
            reasons = "; ".join(f"{event.label}: {event.reason}" for event in halting)
            raise AuditHalted(
                f"Sichtbarkeitsaudit für {self.business} angehalten, wartet auf Daniel: {reasons}"
            )

    def checks_done(
        self,
        results: list[CheckResult],
        catalogue: Catalogue,
        pending: frozenset[str],
        reports: list[RouteReport],
        paid_enabled: bool,
    ) -> list[AuditEvent]:
        by_id = {result.check_id: result for result in results}
        failed_routes = _failed_routes(reports)
        events = sorted(
            (
                self._check_event(definition, by_id.get(definition.id), pending, failed_routes)
                for area in catalogue.areas
                for definition in area.checks
            ),
            key=lambda event: event.check_number or 0,
        )
        events = [self._with_paid_rule(event, catalogue, paid_enabled) for event in events]
        for event in events:
            self._channels.file(event)
        self._channels.database(events)
        table = overview_table(events)
        logger.info("Prüfübersicht des Audits %s:\n%s", self.audit_id, table)
        self._channels.overview(table)
        self._announce_unmeasured(events, failed_routes)
        return events

    def _with_paid_rule(
        self, event: AuditEvent, catalogue: Catalogue, paid_enabled: bool
    ) -> AuditEvent:
        if paid_enabled or event.result is Result.OPEN:
            return event
        paid = {
            definition.id
            for area in catalogue.areas
            for definition in area.checks
            if definition.requires_paid
        }
        if event.check_id not in paid:
            return event
        return event.model_copy(update={"result": Result.SUSPENDED, "reason": PAID_REASON})

    def _stage_of_route(self, route: str) -> int | None:
        try:
            return self._routes.stage_of(route)
        except ScoringError:
            return None

    def _route_event(self, outcome: RouteOutcome, phase: str) -> AuditEvent:
        reason: str | None
        if outcome.result is Result.MEASURED:
            reason = None
        elif outcome.result is Result.SUSPENDED and isinstance(outcome.reason, str):
            reason = str(outcome.reason)
        else:
            reason = plain_reason(outcome.reason)
        checks = [
            AffectedCheck(number=check.number, id=check.id, name=check.name)
            for check in self._routes.checks_of(outcome.route)
        ]
        return AuditEvent.model_validate(
            {
                "time": self._clock(),
                "kind": "route",
                "phase": phase,
                "result": outcome.result,
                "reason": reason,
                "stage": self._stage_of_route(outcome.route),
                "route": outcome.route,
                "label": route_label(outcome.route),
                "checks": checks,
            }
        )

    def _keep_report(self, event: AuditEvent) -> None:
        if event.route is None:
            return
        report = RouteReport(route=event.route, result=event.result, reason=event.reason)
        self.reports = [kept for kept in self.reports if kept.route != event.route]
        self.reports.append(report)

    def _check_event(
        self,
        definition: CheckDefinition,
        result: CheckResult | None,
        pending: frozenset[str],
        failed_routes: dict[str, RouteReport],
    ) -> AuditEvent:
        entry = self._routes.entry(definition.id)
        outcome, reason = self._check_outcome(definition, result, pending, failed_routes)
        return AuditEvent.model_validate(
            {
                "time": self._clock(),
                "kind": "check",
                "phase": "auswertung",
                "result": outcome,
                "reason": reason,
                "stage": entry.stage,
                "route": entry.route,
                "label": entry.name,
                "check_number": entry.number,
                "check_id": definition.id,
            }
        )

    def _check_outcome(
        self,
        definition: CheckDefinition,
        result: CheckResult | None,
        pending: frozenset[str],
        failed_routes: dict[str, RouteReport],
    ) -> tuple[Result, str | None]:
        if definition.id in pending:
            return Result.OPEN, OPEN_REASON
        if result is None:
            if definition.requires_access:
                return Result.SUSPENDED, ACCESS_REASON
            return Result.FAILED, self._route_reason(
                definition.id, failed_routes
            ) or NO_RESULT_REASON
        if result.outcome in MEASURED_OUTCOMES:
            return Result.MEASURED, None
        code = result.measured.get("reason")
        if code == INVALID_REASON_CODE:
            invalid_reason = result.measured.get("invalid_reason")
            return Result.SUSPENDED, str(invalid_reason or reason_of_code(code))
        if result.outcome in (CheckOutcome.SKIPPED, CheckOutcome.NOT_APPLICABLE):
            return Result.SUSPENDED, reason_of_code(code)
        if code in NOT_YET_MEASURABLE:
            return Result.SUSPENDED, reason_of_code(code)
        return Result.FAILED, self._route_reason(definition.id, failed_routes) or reason_of_code(
            code
        )

    def _route_reason(self, check_id: str, failed_routes: dict[str, RouteReport]) -> str | None:
        report = failed_routes.get(self._routes.route_of(check_id))
        if report is None:
            return None
        return f"{route_label(report.route)} ausgefallen: {report.reason or UNKNOWN_REASON}"

    def _announce_failures(self, failures: list[AuditEvent], *, halted: bool) -> None:
        fresh = [event for event in failures if self._fresh(event)]
        for event in fresh:
            suffix = f". {HALT_NOTE}" if event.stage in self._halt_stages else ""
            self._channels.notice(
                f"Audit {self.business}: Messung fehlgeschlagen – {failure_line(event)}{suffix}",
                event.stage in self._halt_stages,
            )
            self._lines.append(failure_line(event))
        if fresh or halted:
            self._channels.board(self._card(halted=halted))

    def _fresh(self, event: AuditEvent) -> bool:
        key = (event.route or event.label, event.reason or "")
        if key in self._announced:
            return False
        self._announced.add(key)
        return True

    def _announce_unmeasured(
        self, events: list[AuditEvent], failed_routes: dict[str, RouteReport]
    ) -> None:
        missing = [
            event
            for event in events
            if event.result is Result.FAILED and event.route not in failed_routes
        ]
        if not missing:
            return
        count = len(missing)
        noun = "Prüfung ohne Messwert" if count == 1 else "Prüfungen ohne Messwert"
        listed = ", ".join(
            f"Nr {event.check_number} {event.check_id}" for event in missing[:NOTICE_CHECKS]
        )
        rest = count - NOTICE_CHECKS
        more = f" und {rest} weitere" if rest > 0 else ""
        self._channels.notice(f"Audit {self.business}: {count} {noun} – {listed}{more}", False)
        lines = "; ".join(check_line(event) for event in missing[:CARD_CHECKS])
        left = count - CARD_CHECKS
        tail = f" und {left} weitere" if left > 0 else ""
        self._lines.append(f"{count} {noun} – {lines}{tail}")
        self._channels.board(self._card(halted=False))

    def _card(self, *, halted: bool) -> str:
        head = f"Audit {self.business} [{card_key(self.audit_id)}]"
        lead = f"{head} angehalten, wartet auf Daniel: " if halted else f"{head}: "
        return lead + " | ".join(self._lines)


def _failed_routes(reports: list[RouteReport]) -> dict[str, RouteReport]:
    failed: dict[str, RouteReport] = {}
    for report in reports:
        if report.result is not Result.FAILED:
            continue
        key = ASSISTANT_GROUP if report.route.startswith(ASSISTANT_PREFIX) else report.route
        failed.setdefault(key, report)
    return failed
