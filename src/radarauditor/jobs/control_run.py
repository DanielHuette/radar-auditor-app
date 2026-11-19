import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.collectors.steps import RAW_BUCKET, raw_answer_path
from radarauditor.collectors.validation import steps_of_check
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.db.models import AuditStatus, CheckResult, ControlState, JobKind, Verdict
from radarauditor.db.repositories import MigrationMissingError, Repositories
from radarauditor.errors import ClaudeCodeError, RadarError, StorageError
from radarauditor.feedback.events import Result
from radarauditor.feedback.hub import Feedback
from radarauditor.jobs import claude_code
from radarauditor.jobs.control_agent import (
    Ask,
    Bundle,
    ControlAnswerError,
    apply_step_verdicts,
    ask_claude,
    bundles_of,
    examine,
    examine_steps,
    fingerprint,
    needs_control,
)
from radarauditor.jobs.control_data import (
    RawAnswer,
    answer_name,
    answer_paths,
    step_answer_keys,
    step_parts,
    step_request,
)
from radarauditor.scoring.catalog import Catalogue, find_check, load_catalogue
from radarauditor.scoring.evaluator import total_score_or_none

logger = logging.getLogger(__name__)

RETRY_DELAYS = (10, 30, 60)
RETRY_DELAY_LATER = 300
POLL_SECONDS = 15
CONTROL_SERVICE = "control"
RUNNING_STAGE = "kontrolle"
WAITING_STAGE = "kontrolle_wartet"
DONE_STAGE = "fertig"
FORMAT_ERROR_PREFIX = "Formfehler der Antwort: "
CONTROL_LABEL = "Kontrolle"
TOTAL_LABEL = "Kontrolle: Gesamtwert"
EMPTY_TOTAL_REASON = (
    "Der Gesamtwert ist leer: Nach der Kontrolle blieb keine Prüfung übrig, die zählt"
)

type Pause = Callable[[float], Awaitable[bool]]
type Clock = Callable[[], float]


@dataclass(frozen=True)
class AuditPass:
    finished: bool
    wait: int | None = None


def retry_delay(failures: int) -> int:
    return RETRY_DELAYS[failures - 1] if failures <= len(RETRY_DELAYS) else RETRY_DELAY_LATER


def stop_pause(stop: asyncio.Event) -> Pause:
    async def pause(seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        return stop.is_set()

    return pause


async def run_control_loop(
    repositories: Repositories,
    settings: Settings,
    stop: asyncio.Event,
    ask: Ask | None = None,
    clock: Clock = time.monotonic,
) -> None:
    pause = stop_pause(stop)
    answer = ask if ask is not None else ask_claude(settings)
    not_before: dict[UUID, float] = {}
    failures: dict[UUID, int] = {}
    halted: set[UUID] = set()
    while not stop.is_set():
        try:
            audit_ids = await asyncio.to_thread(
                repositories.audit_ids_with_status, AuditStatus.CONTROLLING
            )
        except Exception:
            logger.exception("Control pass could not list the audits, trying again")
            audit_ids = []
        for stale in set(not_before) - set(audit_ids):
            not_before.pop(stale)
            failures.pop(stale, None)
        for audit_id in audit_ids:
            if stop.is_set():
                return
            if audit_id in halted or not_before.get(audit_id, 0.0) > clock():
                continue
            wait = await _guarded_pass(audit_id, repositories, settings, answer, failures, halted)
            if wait is None:
                not_before.pop(audit_id, None)
            else:
                not_before[audit_id] = clock() + wait
        await pause(_sleep_seconds(not_before, clock()))


def _sleep_seconds(not_before: dict[UUID, float], now: float) -> float:
    waits = [moment - now for moment in not_before.values() if moment > now]
    return min([POLL_SECONDS, *waits])


async def _guarded_pass(
    audit_id: UUID,
    repositories: Repositories,
    settings: Settings,
    ask: Ask,
    failures: dict[UUID, int],
    halted: set[UUID],
) -> int | None:
    try:
        outcome = await control_audit(audit_id, repositories, settings, ask)
    except MigrationMissingError as error:
        halted.add(audit_id)
        logger.error("Control of audit %s stopped: %s", audit_id, error)
        _note_halted(audit_id, error, repositories)
        return None
    except Exception as error:
        failures[audit_id] = failures.get(audit_id, 0) + 1
        delay = retry_delay(failures[audit_id])
        logger.exception("Control of audit %s failed unexpectedly", audit_id)
        _note_unexpected(audit_id, failures[audit_id], delay, error, repositories)
        return delay
    failures.pop(audit_id, None)
    return outcome.wait


def _note_halted(audit_id: UUID, error: Exception, repositories: Repositories) -> None:
    reason = f"Kontrolle angehalten: {error}. Nach dem Einspielen den Dienst neu starten."
    try:
        repositories.add_manager_event(audit_id, Result.FAILED, CONTROL_LABEL, reason)
    except RadarError as event_error:
        logger.error("Control event of audit %s could not be written: %s", audit_id, event_error)


def _note_unexpected(
    audit_id: UUID, attempt: int, delay: int, error: Exception, repositories: Repositories
) -> None:
    reason = (
        f"Kontrolle wartet – Versuch {attempt}: unerwarteter Fehler {type(error).__name__}: "
        f"{error}; neuer Versuch in {delay} Sekunden"
    )
    try:
        repositories.add_manager_event(audit_id, Result.WARNING, CONTROL_LABEL, reason)
    except RadarError as event_error:
        logger.error("Control event of audit %s could not be written: %s", audit_id, event_error)


async def control_audit(
    audit_id: UUID, repositories: Repositories, settings: Settings, ask: Ask
) -> AuditPass:
    from radarauditor.jobs.worker import load_raw

    audit = repositories.audit(audit_id)
    if audit.status is not AuditStatus.CONTROLLING:
        return AuditPass(False)
    missing = await asyncio.to_thread(repositories.missing_migrations)
    if missing:
        raise StorageError(
            f"Datenbank-Änderung {', '.join(missing)} fehlt; die Kontrolle wartet, bis sie "
            "eingespielt ist"
        )
    raw = await asyncio.to_thread(load_raw, audit, repositories)
    catalogue = load_catalogue(settings.catalogue_path)
    business = repositories.customer(audit.customer_id).name
    feedback = Feedback.create(audit_id, business, settings, repositories)
    pending = [
        result
        for result in repositories.check_results(audit_id)
        if needs_control(result) and result.control_state is ControlState.OPEN
    ]
    steps_of = {
        result.check_id: steps_of_check(
            result.check_id, find_check(catalogue, result.check_id).measurement
        )
        for result in pending
    }
    by_step = await asyncio.to_thread(
        _stored_by_step,
        audit_id,
        sorted({s for v in steps_of.values() for s in v}),
        settings,
        repositories,
    )
    answers = _answers_per_check(steps_of, by_step)
    bundles = bundles_of(
        pending,
        raw,
        catalogue,
        settings.control_bundle_size,
        settings.routes_path,
        settings.templates_dir,
        answers,
    )
    by_id = {result.check_id: result for result in pending}
    with claude_code.counting_usage(_usage_book(audit_id, repositories)):
        step_verdicts, step_wait = await _examine_steps(
            audit_id, raw, by_step, pending, settings, repositories, feedback, ask
        )
        if step_wait is not None:
            return AuditPass(False, step_wait)
        for bundle in bundles:
            if repositories.audit(audit_id).status is not AuditStatus.CONTROLLING:
                return AuditPass(False)
            wait = await _examine_bundle(
                audit_id,
                bundle,
                [by_id[check_id] for check_id in bundle.check_ids],
                repositories,
                feedback,
                ask,
                steps_of,
                step_verdicts,
            )
            if wait is not None:
                return AuditPass(False, wait)
    return AuditPass(_finish(audit_id, audit.customer_id, catalogue, repositories))


def _stored_by_step(
    audit_id: UUID, steps: list[str], settings: Settings, repositories: Repositories
) -> dict[str, list[RawAnswer]]:
    if not steps:
        return {}
    try:
        paths = repositories.list_paths(RAW_BUCKET, raw_answer_path(audit_id, "").rstrip("/"))
    except RadarError as error:
        logger.warning("Stored answers of audit %s not readable: %s", audit_id, error)
        return {}
    cache: dict[str, RawAnswer] = {}
    found: dict[str, list[RawAnswer]] = {}
    for step in steps:
        keys = step_answer_keys((step,), settings.routes_path)
        for path in answer_paths(keys, paths, step):
            if path not in cache:
                try:
                    body = repositories.download_bytes(RAW_BUCKET, path)
                except RadarError as error:
                    logger.warning("Stored answer %s not readable: %s", path, error)
                    continue
                cache[path] = RawAnswer(answer_name(path), body.decode("utf-8", errors="replace"))
            found.setdefault(step, []).append(cache[path])
    return found


def _answers_per_check(
    steps_of: dict[str, tuple[str, ...]], by_step: dict[str, list[RawAnswer]]
) -> dict[str, list[RawAnswer]]:
    found: dict[str, list[RawAnswer]] = {}
    for check_id, steps in steps_of.items():
        merged = {id(answer): answer for step in steps for answer in by_step.get(step, ())}
        if merged:
            found[check_id] = list(merged.values())
    return found


async def _examine_steps(
    audit_id: UUID,
    raw: RawAudit,
    by_step: dict[str, list[RawAnswer]],
    pending: list[CheckResult],
    settings: Settings,
    repositories: Repositories,
    feedback: Feedback,
    ask: Ask,
) -> tuple[dict[str, Verdict], int | None]:
    parts = [
        part
        for step, stored in by_step.items()
        for part in step_parts(step, step_request(step, raw, settings.routes_path), stored)
    ]
    if not parts:
        return {}, None
    attempts = max(result.control_attempt for result in pending)
    try:
        verdicts = await asyncio.to_thread(examine_steps, parts, ask, _format_error_of(pending))
    except ClaudeCodeError as error:
        attempts += 1
        delay = retry_delay(attempts)
        is_format = isinstance(error, ControlAnswerError)
        stored = f"{FORMAT_ERROR_PREFIX}{error}" if is_format else str(error)
        check_ids = [result.check_id for result in pending]
        _note_waiting(audit_id, check_ids, attempts, delay, stored, repositories, feedback)
        return {}, delay
    return verdicts, None


async def _examine_bundle(
    audit_id: UUID,
    bundle: Bundle,
    results: list[CheckResult],
    repositories: Repositories,
    feedback: Feedback,
    ask: Ask,
    steps_of: dict[str, tuple[str, ...]],
    step_verdicts: dict[str, Verdict],
) -> int | None:
    attempts = max(result.control_attempt for result in results)
    for check_id in bundle.check_ids:
        feedback.control_event(
            check_id, Result.RUNNING, None, {"stage": RUNNING_STAGE, "attempt": attempts + 1}
        )
    try:
        examined = await asyncio.to_thread(examine, bundle, ask, _format_error_of(results))
    except ClaudeCodeError as error:
        attempts += 1
        delay = retry_delay(attempts)
        is_format = isinstance(error, ControlAnswerError)
        stored = f"{FORMAT_ERROR_PREFIX}{error}" if is_format else str(error)
        _note_waiting(
            audit_id, list(bundle.check_ids), attempts, delay, stored, repositories, feedback
        )
        return delay
    examined = apply_step_verdicts(examined, steps_of, step_verdicts)
    current = {result.check_id: result for result in repositories.check_results(audit_id)}
    for check_id, verdicts in examined.items():
        row = current.get(check_id)
        if (
            row is None
            or row.control_state is not ControlState.OPEN
            or fingerprint(row) != bundle.fingerprints[check_id]
        ):
            logger.warning(
                "Check %s of audit %s changed during control, verdict dropped", check_id, audit_id
            )
            continue
        repositories.save_check_verdicts(
            audit_id, check_id, verdicts.collected, verdicts.data, verdicts.evaluation
        )
        feedback.control_event(check_id, Result.MEASURED, None, {"stage": DONE_STAGE})
    return None


def _format_error_of(results: list[CheckResult]) -> str | None:
    for result in results:
        error = result.control_last_error
        if error is None or result.control_attempt == 0:
            continue
        if error.startswith(FORMAT_ERROR_PREFIX):
            return error.removeprefix(FORMAT_ERROR_PREFIX)
    return None


def _note_waiting(
    audit_id: UUID,
    check_ids: list[str],
    attempt: int,
    delay: int,
    error: str,
    repositories: Repositories,
    feedback: Feedback,
) -> None:
    logger.warning("Control of audit %s waits, attempt %d: %s", audit_id, attempt, error)
    protocol = {
        "stage": WAITING_STAGE,
        "attempt": attempt,
        "last_error": error,
        "next_attempt_in_seconds": delay,
    }
    for check_id in check_ids:
        repositories.save_control_attempt(audit_id, check_id, attempt, error)
        feedback.control_event(
            check_id, Result.WARNING, f"Kontrolle wartet – Versuch {attempt}: {error}", protocol
        )


def _finish(
    audit_id: UUID, customer_id: UUID, catalogue: Catalogue, repositories: Repositories
) -> bool:
    results = repositories.check_results(audit_id)
    if _open(results):
        return False
    total: Decimal | None = total_score_or_none(results, catalogue)
    repositories.set_total_score(audit_id, total)
    if total is None:
        _note_empty_total(audit_id, repositories)
    repositories.enqueue(JobKind.ANALYSE, customer_id, audit_id)
    repositories.set_audit_status(audit_id, AuditStatus.AWAITING_ANALYSIS)
    return True


def _note_empty_total(audit_id: UUID, repositories: Repositories) -> None:
    try:
        repositories.add_manager_event(audit_id, Result.WARNING, TOTAL_LABEL, EMPTY_TOTAL_REASON)
    except RadarError as error:
        logger.error("Total note of audit %s could not be written: %s", audit_id, error)


def _open(results: list[CheckResult]) -> list[CheckResult]:
    return [
        result
        for result in results
        if needs_control(result) and result.control_state is ControlState.OPEN
    ]


def _usage_book(audit_id: UUID, repositories: Repositories) -> claude_code.UsageSink:
    def book(tokens_in: int, tokens_out: int) -> None:
        repositories.add_audit_cost(
            audit_id,
            Source.CLAUDE.value,
            CONTROL_SERVICE,
            calls=1,
            cost=Decimal(0),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    return book
