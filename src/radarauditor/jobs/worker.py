import asyncio
import contextlib
import functools
import itertools
import logging
import signal
import tempfile
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import RawAudit
from radarauditor.collectors.steps import StepStore, raw_answer_path
from radarauditor.config import Settings
from radarauditor.db.app_settings import (
    WORKER_SETTINGS_KEYS,
    Source,
    WorkerCapabilities,
    WorkerSettings,
    parse_worker_settings,
)
from radarauditor.db.models import (
    Audit,
    AuditKind,
    AuditPurpose,
    AuditStatus,
    CheckResult,
    Job,
    JobKind,
    JobStatus,
)
from radarauditor.db.repositories import Repositories
from radarauditor.errors import (
    AnalysisUnavailableError,
    AuditHalted,
    IdentificationError,
    RadarError,
    SearchBlockedError,
)
from radarauditor.jobs import claude_code, serp_retry
from radarauditor.jobs.analysis import retry_at as analysis_retry_at
from radarauditor.jobs.heartbeat import (
    CapabilityProbe,
    build_heartbeat,
    detect_capabilities,
    run_watchdog,
    touch_alive_file,
    with_mail_problem,
    write_heartbeat,
)
from radarauditor.jobs.notify import Notifier
from radarauditor.jobs.repair import blocking_failures

if TYPE_CHECKING:
    from radarauditor.jobs.mail_triage import MailReader, ReadOutcome
    from radarauditor.scoring.catalog import Catalogue

logger = logging.getLogger(__name__)

POLL_SECONDS = 15
MAX_ATTEMPTS = 3
PRIORITY = (
    JobKind.COLLECT,
    JobKind.REPAIR,
    JobKind.SCORE,
    JobKind.ANALYSE,
    JobKind.REPORT,
    JobKind.SERP_RETRY,
    JobKind.APPLY,
    JobKind.OPTIMIZE,
)
NEEDS_ANALYSIS = frozenset({JobKind.COLLECT})
RESCORED_STATUSES = frozenset(
    {
        AuditStatus.SCORED,
        AuditStatus.CONTROLLING,
        AuditStatus.AWAITING_ANALYSIS,
        AuditStatus.ANALYSED,
        AuditStatus.REPORTED,
    }
)
AUDIT_PIPELINE = frozenset({JobKind.COLLECT, JobKind.SCORE, JobKind.ANALYSE, JobKind.REPORT})
MANAGED_KINDS = AUDIT_PIPELINE | {JobKind.SERP_RETRY, JobKind.REPAIR}
RAW_BUCKET = "raw"
NO_UPLIFT = (0.0, 0.0)
REPORTS_BUCKET = "reports"
RAW_FILE = "raw.json"
AUDIT_REPORTED_EVENT = "audit.reported"
INTERRUPTED_ERROR = "Der Server wurde während des Auftrags neu gestartet"


async def run_forever(settings: Settings) -> None:
    repositories = Repositories(settings)
    stop = asyncio.Event()
    installed = _install_stop_handlers(stop)
    logger.info("Worker started, polling every %d seconds", POLL_SECONDS)
    try:
        await serve(repositories, settings, stop)
    finally:
        _remove_stop_handlers(installed)
    logger.info("Worker stopped")


def _install_stop_handlers(stop: asyncio.Event) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, stop.set)
        except NotImplementedError:
            signal.signal(stop_signal, lambda *_: loop.call_soon_threadsafe(stop.set))
            continue
        installed.append(stop_signal)
    return installed


def _remove_stop_handlers(installed: list[signal.Signals]) -> None:
    loop = asyncio.get_running_loop()
    for stop_signal in installed:
        loop.remove_signal_handler(stop_signal)


async def serve(repositories: Repositories, settings: Settings, stop: asyncio.Event) -> None:
    _requeue_interrupted(repositories)
    probe = CapabilityProbe(functools.partial(detect_capabilities, settings))
    pulse = Pulse(repositories, probe, settings.worker_alive_file)
    await pulse.beat()
    watchdog_stop = _start_watchdog(settings)
    background = [asyncio.create_task(pulse.run(stop))]
    if settings.cockpit_assistant_poll_seconds > 0:
        background.append(asyncio.create_task(_answer_assistant(repositories, settings, stop)))
    if settings.audit_manager_enabled:
        background.append(asyncio.create_task(_manage_audits(repositories, settings, stop)))
    background.append(asyncio.create_task(_control_audits(repositories, settings, stop)))
    try:
        await _claim_and_run(repositories, settings, stop, pulse)
    finally:
        watchdog_stop.set()
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _start_watchdog(settings: Settings) -> threading.Event:
    stop = threading.Event()
    if settings.worker_alive_file is not None:
        threading.Thread(
            target=run_watchdog, args=(settings.worker_alive_file, stop), daemon=True
        ).start()
    return stop


async def _answer_assistant(
    repositories: Repositories, settings: Settings, stop: asyncio.Event
) -> None:
    from radarauditor.jobs import assistant

    desk = assistant.AssistantDesk(repositories, settings)
    await assistant.run_assistant(desk, settings.cockpit_assistant_poll_seconds, stop)


async def _control_audits(
    repositories: Repositories, settings: Settings, stop: asyncio.Event
) -> None:
    from radarauditor.jobs.control_run import run_control_loop

    await run_control_loop(repositories, settings, stop)


async def _manage_audits(
    repositories: Repositories, settings: Settings, stop: asyncio.Event
) -> None:
    from radarauditor.jobs.audit_manager import run_audit_manager
    from radarauditor.jobs.job_signal import open_jobs_subscription
    from radarauditor.jobs.manager_agent import run_manager_agent

    await run_audit_manager(
        repositories,
        settings,
        stop,
        functools.partial(open_jobs_subscription, settings),
        run_manager_agent,
    )


def _requeue_interrupted(repositories: Repositories) -> None:
    try:
        count = repositories.requeue_running_jobs(INTERRUPTED_ERROR)
    except RadarError as exc:
        logger.error("Interrupted jobs could not be put back into the queue: %s", exc)
        return
    if count:
        logger.warning("Put %d jobs interrupted by the last stop back into the queue", count)


async def _claim_and_run(
    repositories: Repositories, settings: Settings, stop: asyncio.Event, pulse: "Pulse"
) -> None:
    from radarauditor.jobs.mail_triage import MailReader

    mails = MailReader(repositories, settings)
    paused: frozenset[JobKind] = frozenset()
    while not stop.is_set():
        await _run_customer_search(repositories, settings)
        now_paused = paused_kinds(pulse.capabilities)
        if now_paused != paused:
            _log_pause(now_paused, pulse.capabilities)
            paused = now_paused
        held = paused | MANAGED_KINDS if settings.audit_manager_enabled else paused
        try:
            job = claim_next(repositories, held)
        except RadarError as exc:
            logger.error("Claiming jobs failed: %s", exc)
            job = None
        if job is not None:
            await process_job(job, repositories, settings)
            continue
        await asyncio.to_thread(run_document_requests, repositories, settings)
        await _deliver_events(repositories, settings)
        await _read_mail(mails, settings, pulse)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)


async def _run_customer_search(repositories: Repositories, settings: Settings) -> None:
    from radarauditor.prospects import search

    await search.run_next_search(repositories, settings)


class MailProblem(NamedTuple):
    reason: str
    since: datetime


class Pulse:
    def __init__(
        self,
        repositories: Repositories,
        probe: CapabilityProbe,
        alive_file: Path | None,
    ) -> None:
        self._repositories = repositories
        self._probe = probe
        self._alive_file = alive_file
        self.capabilities: WorkerCapabilities | None = None
        self.mail_problem: MailProblem | None = None

    async def beat(self) -> None:
        problem = self.mail_problem.reason if self.mail_problem is not None else None
        self.capabilities = await beat(self._repositories, self._probe, self._alive_file, problem)

    def may_read_mail(self, enabled: bool) -> bool:
        if not enabled or self.capabilities is None or not self.capabilities.mail_reading_available:
            return False
        if self.mail_problem is None:
            return True
        detected = self._probe.detected_at
        return detected is not None and detected > self.mail_problem.since

    def record_mail(self, outcome: "ReadOutcome", now: datetime) -> None:
        self.mail_problem = None if outcome.problem is None else MailProblem(outcome.problem, now)
        if outcome.claude_unavailable:
            self._probe.invalidate()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            if not stop.is_set():
                await self.beat()


async def beat(
    repositories: Repositories,
    probe: CapabilityProbe,
    alive_file: Path | None = None,
    mail_problem: str | None = None,
) -> WorkerCapabilities | None:
    now = datetime.now(UTC)
    try:
        detected = await asyncio.to_thread(probe.current, now)
    except (RadarError, OSError) as exc:
        logger.error("Capabilities could not be detected: %s", exc)
        return None
    try:
        write_heartbeat(
            repositories, build_heartbeat(now, with_mail_problem(detected, mail_problem))
        )
    except (RadarError, OSError) as exc:
        logger.error("Heartbeat could not be written: %s", exc)
        return detected
    if alive_file is not None:
        try:
            touch_alive_file(alive_file)
        except OSError as exc:
            logger.error("Alive file %s could not be written: %s", alive_file, exc)
    return detected


def _log_pause(paused: frozenset[JobKind], capabilities: WorkerCapabilities | None) -> None:
    if paused:
        reason = capabilities.analysis_reason if capabilities is not None else None
        logger.warning("No new audits while Claude Code is unavailable: %s", reason)
    else:
        logger.info("Claude Code is available again, new audits are claimed")


def paused_kinds(capabilities: WorkerCapabilities | None) -> frozenset[JobKind]:
    return frozenset() if claude_code_answers(capabilities) else NEEDS_ANALYSIS


def claude_code_answers(capabilities: WorkerCapabilities | None) -> bool:
    return capabilities is None or capabilities.analysis_available


def read_worker_settings(repositories: Repositories) -> WorkerSettings:
    return parse_worker_settings(repositories.settings_values(WORKER_SETTINGS_KEYS))


def claim_next(repositories: Repositories, paused: frozenset[JobKind] = frozenset()) -> Job | None:
    for kind in PRIORITY:
        if kind in paused:
            continue
        jobs = repositories.claim_jobs(kind, 1)
        if jobs:
            return jobs[0]
    return None


async def _read_mail(mails: "MailReader", settings: Settings, pulse: Pulse) -> None:
    if not pulse.may_read_mail(settings.mail_triage_batch > 0):
        return
    now = datetime.now(UTC)
    try:
        outcome = await asyncio.to_thread(mails.read_one, now)
    except RadarError as exc:
        logger.error("Reading mails failed: %s", exc)
        return
    pulse.record_mail(outcome, now)
    if outcome.problem is not None:
        logger.warning("Mail reading pauses: %s", outcome.problem)


async def _deliver_events(repositories: Repositories, settings: Settings) -> None:
    if settings.event_endpoint is None:
        return
    from radarauditor.integrations.events import deliver_pending

    try:
        delivered = await deliver_pending(repositories, str(settings.event_endpoint))
    except RadarError as exc:
        logger.error("Event delivery failed: %s", exc)
        return
    if delivered:
        logger.info("Delivered %d events", delivered)


def run_document_requests(repositories: Repositories, settings: Settings) -> None:
    from radarauditor.documents.requests import process_pending_requests

    try:
        issued = process_pending_requests(repositories, settings)
    except RadarError as exc:
        logger.error("Document requests could not be processed: %s", exc)
        return
    if issued:
        logger.info("Issued %d requested documents", issued)


async def process_job(job: Job, repositories: Repositories, settings: Settings) -> None:
    logger.info("Running %s job %s (attempt %d)", job.kind.value, job.id, job.attempts)
    if job.kind is JobKind.COLLECT and job.attempts == 1 and job.audit_id is not None:
        Notifier(repositories, settings).audit_started(job.audit_id, job.customer_id)
    try:
        await run_job(job, repositories, settings)
    except SearchBlockedError as exc:
        logger.warning("Job %s: %s", job.id, exc)
        _record_failure(job, repositories, str(exc))
        return
    except AnalysisUnavailableError as exc:
        _requeue_analysis(job, repositories, str(exc))
        return
    except AuditHalted as exc:
        logger.warning("Job %s: %s", job.id, exc)
        _hold(job, repositories, str(exc))
    except IdentificationError as exc:
        logger.warning("Job %s: %s", job.id, exc)
        _fail_at_once(job, repositories, str(exc))
        return
    except Exception as exc:
        logger.exception("Job %s failed", job.id)
        _record_failure(job, repositories, f"{type(exc).__name__}: {exc}")
        return
    try:
        repositories.finish_job(job.id, JobStatus.DONE)
    except RadarError as exc:
        logger.error("Job %s finished but could not be marked done: %s", job.id, exc)


def _hold(job: Job, repositories: Repositories, message: str) -> None:
    if job.audit_id is None:
        return
    try:
        repositories.set_audit_status(job.audit_id, AuditStatus.HALTED, message)
    except RadarError as exc:
        logger.error("Halt of audit %s could not be recorded: %s", job.audit_id, exc)


def _requeue_analysis(job: Job, repositories: Repositories, reason: str) -> None:
    run_after = analysis_retry_at(job.attempts, datetime.now(UTC))
    logger.warning("Analyse job %s waits until %s: %s", job.id, run_after, reason)
    try:
        repositories.requeue_job(job.id, reason, run_after=run_after)
    except RadarError as exc:
        logger.error("Analyse job %s could not be re-queued: %s", job.id, exc)


def _fail_at_once(job: Job, repositories: Repositories, message: str) -> None:
    try:
        repositories.finish_job(job.id, JobStatus.FAILED, message)
        if job.audit_id is not None and job.kind in AUDIT_PIPELINE:
            repositories.set_audit_status(job.audit_id, AuditStatus.FAILED, message)
    except RadarError as exc:
        logger.error("Failure of job %s could not be recorded: %s", job.id, exc)


def _record_failure(job: Job, repositories: Repositories, message: str) -> None:
    try:
        if job.kind is JobKind.REPAIR:
            repositories.requeue_job(job.id, message)
            return
        if job.kind is JobKind.SERP_RETRY:
            serp_retry.record_failure(job, repositories, message, datetime.now(UTC))
            return
        if job.kind is JobKind.ANALYSE:
            _requeue_analysis(job, repositories, message)
            return
        if job.attempts < MAX_ATTEMPTS:
            repositories.requeue_job(job.id, message)
            return
        repositories.finish_job(job.id, JobStatus.FAILED, message)
        if job.audit_id is not None and job.kind in AUDIT_PIPELINE:
            repositories.set_audit_status(job.audit_id, AuditStatus.FAILED, message)
    except RadarError as exc:
        logger.error("Failure of job %s could not be recorded: %s", job.id, exc)


CLAUDE_SERVICES = {JobKind.COLLECT: "assistants", JobKind.ANALYSE: "analysis"}


def _usage_sink(job: Job, repositories: Repositories) -> claude_code.UsageSink | None:
    service = CLAUDE_SERVICES.get(job.kind)
    if job.audit_id is None or service is None:
        return None
    audit_id = job.audit_id

    def book(tokens_in: int, tokens_out: int) -> None:
        repositories.add_audit_cost(
            audit_id,
            Source.CLAUDE.value,
            service,
            calls=1,
            cost=Decimal(0),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    return book


async def run_job(job: Job, repositories: Repositories, settings: Settings) -> None:
    with claude_code.counting_usage(_usage_sink(job, repositories)):
        await _dispatch(job, repositories, settings)


async def _dispatch(job: Job, repositories: Repositories, settings: Settings) -> None:
    job_settings = read_worker_settings(repositories)
    if job.kind is JobKind.OPTIMIZE:
        from radarauditor.optimization.prepare import optimize_order

        await optimize_order(job, repositories, settings)
        return
    if job.kind is JobKind.APPLY:
        from radarauditor.optimization.apply import apply_order

        await apply_order(job, repositories, settings)
        return
    if job.kind is JobKind.ANALYSE:
        await asyncio.to_thread(_analyse, job, repositories, settings)
        return
    if job.audit_id is None:
        raise RadarError(f"job {job.id}: {job.kind.value} job has no audit id")
    if job.kind is JobKind.COLLECT:
        await _collect(job.audit_id, job.customer_id, repositories, settings, job_settings)
    elif job.kind is JobKind.REPAIR:
        await _repair(job.audit_id, job.customer_id, repositories, settings, job_settings)
    elif job.kind is JobKind.SCORE:
        _score(job.audit_id, job.customer_id, repositories, settings, job_settings)
    elif job.kind is JobKind.SERP_RETRY:
        await _retry_searches(job.audit_id, job.customer_id, repositories, settings, job_settings)
    else:
        _report(job.audit_id, job.customer_id, repositories, settings)


async def _collect(
    audit_id: UUID,
    customer_id: UUID,
    repositories: Repositories,
    settings: Settings,
    job_settings: WorkerSettings,
) -> None:
    from radarauditor.collectors.run import collect_audit

    _require_migrations(repositories)
    repositories.set_audit_status(audit_id, AuditStatus.COLLECTING)
    gate = audit_gate(job_settings, repositories, audit_id)
    unattended = settings.model_copy(update={"assistant_handoff": False, "halt_stages": ()})
    raw = await collect_audit(audit_id, repositories, unattended, gate)
    _store_raw(audit_id, raw, repositories)
    if blocking_failures(StepStore(audit_id, repositories)):
        repositories.enqueue(JobKind.REPAIR, customer_id, audit_id)
        return
    _save_rankings(audit_id, customer_id, raw, repositories)
    if raw.deferred_searches:
        serp_retry.schedule(repositories, customer_id, audit_id, datetime.now(UTC))
    repositories.set_audit_status(audit_id, AuditStatus.COLLECTED)
    repositories.enqueue(JobKind.SCORE, customer_id, audit_id)


def _require_migrations(repositories: Repositories) -> None:
    missing = repositories.missing_migrations()
    if missing:
        raise AuditHalted(
            f"Datenbank-Änderung {', '.join(missing)} fehlt. Der Audit startet erst, wenn sie "
            "eingespielt ist; es wurde nichts gesammelt und nichts bezahlt"
        )


def audit_gate(
    job_settings: WorkerSettings, repositories: Repositories, audit_id: UUID
) -> SourceGate:
    counter = itertools.count(1)

    def keep_raw(name: str, body: bytes) -> None:
        stamped = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{next(counter):05d}-{name}"
        repositories.upload_bytes(RAW_BUCKET, raw_answer_path(audit_id, stamped), body)

    return SourceGate(
        job_settings.sources, repositories.consume_source_quota, repositories, audit_id, keep_raw
    )


async def _repair(
    audit_id: UUID,
    customer_id: UUID,
    repositories: Repositories,
    settings: Settings,
    job_settings: WorkerSettings,
) -> None:
    from radarauditor.jobs.repair import repair_failed_steps
    from radarauditor.jobs.repair_agent import run_repair_agent

    outcome = await repair_failed_steps(audit_id, repositories, settings, run_repair_agent)
    if not outcome.done:
        repositories.enqueue(JobKind.REPAIR, customer_id, audit_id, run_after=outcome.run_after)
        return
    await _collect(audit_id, customer_id, repositories, settings, job_settings)


def _store_raw(audit_id: UUID, raw: RawAudit, repositories: Repositories) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        raw_file = Path(scratch) / RAW_FILE
        raw_file.write_text(raw.model_dump_json(), encoding="utf-8")
        raw_path = repositories.upload(RAW_BUCKET, f"{audit_id}/{RAW_FILE}", raw_file)
    social = raw.social.model_dump(mode="json") if raw.social is not None else None
    repositories.update_audit(audit_id, raw_data_path=raw_path, social=social)


def load_raw(audit: Audit, repositories: Repositories) -> RawAudit:
    if audit.raw_data_path is None:
        raise RadarError(f"audit {audit.id} has no raw data")
    with tempfile.TemporaryDirectory() as scratch:
        raw_file = repositories.download(RAW_BUCKET, audit.raw_data_path, Path(scratch) / RAW_FILE)
        return RawAudit.model_validate_json(raw_file.read_text(encoding="utf-8"))


async def _retry_searches(
    audit_id: UUID,
    customer_id: UUID,
    repositories: Repositories,
    settings: Settings,
    job_settings: WorkerSettings,
) -> None:
    audit = repositories.audit(audit_id)
    raw = load_raw(audit, repositories)
    if not raw.deferred_searches:
        logger.info("Audit %s has no deferred Google searches left", audit_id)
        return
    gate = SourceGate(
        job_settings.sources, repositories.consume_source_quota, repositories, audit_id
    )
    async with BrowserService(
        settings.browser_profile_dir, channel=settings.browser_channel
    ) as browser:
        run = await serp_retry.search_deferred(raw, browser, gate, settings)
    if run.found:
        logger.info("Retry found %d Google results for audit %s", run.found, audit_id)
        _store_raw(audit_id, run.raw, repositories)
        _save_rankings(audit_id, customer_id, run.raw, repositories)
        if audit.status in RESCORED_STATUSES:
            _score(audit_id, customer_id, repositories, settings, job_settings)
    if run.remaining:
        raise SearchBlockedError(
            f"{len(run.remaining)} Google searches still deferred: {run.reason or 'unknown reason'}"
        )


def _save_rankings(
    audit_id: UUID, customer_id: UUID, raw: RawAudit, repositories: Repositories
) -> None:
    from radarauditor.scoring.rankings import rankings_from_raw

    customer = repositories.customer(customer_id)
    if customer.website is not None:
        hostname = customer.website.host
    elif raw.website is not None:
        hostname = raw.website.start_url.host
    else:
        hostname = None
    place_name = raw.place.name if raw.place is not None else customer.name
    rankings = rankings_from_raw(
        audit_id,
        raw,
        repositories.keywords(customer_id),
        repositories.competitors(customer_id),
        hostname,
        place_name,
    )
    repositories.save_rankings(rankings)


def _score(
    audit_id: UUID,
    customer_id: UUID,
    repositories: Repositories,
    settings: Settings,
    job_settings: WorkerSettings,
) -> None:
    from radarauditor.scoring.catalog import load_catalogue
    from radarauditor.scoring.evaluator import evaluate
    from radarauditor.scoring.potential import with_potential

    audit = repositories.audit(audit_id)
    _announce_evaluation(audit, settings, repositories)
    raw = _with_ranking_history(audit, load_raw(audit, repositories), repositories)
    catalogue = load_catalogue(settings.catalogue_path)
    results = evaluate(
        audit_id,
        raw,
        catalogue,
        paid_enabled=job_settings.sources.dataforseo.enabled,
        with_access=audit.kind is AuditKind.WITH_ACCESS,
        disabled_checks=disabled_checks_of(audit),
    )
    results = with_potential(results, catalogue, NO_UPLIFT)
    repositories.save_check_results(results)
    _report_checks(audit, raw, results, catalogue, settings, repositories, job_settings)
    repositories.set_total_score(audit_id, None)
    repositories.set_audit_status(audit_id, AuditStatus.CONTROLLING)


def _announce_evaluation(audit: Audit, settings: Settings, repositories: Repositories) -> None:
    from radarauditor.feedback.hub import Feedback

    try:
        business = repositories.customer(audit.customer_id).name
        Feedback.create(audit.id, business, settings, repositories).evaluation_running()
    except RadarError as exc:
        logger.warning("Evaluation event of audit %s not written: %s", audit.id, exc)


def _with_ranking_history(audit: Audit, raw: RawAudit, repositories: Repositories) -> RawAudit:
    from radarauditor.scoring.rankings import ranking_history

    previous = repositories.previous_audit(audit)
    if previous is None:
        return raw
    phrases = {keyword.id: keyword.phrase for keyword in repositories.keywords(audit.customer_id)}
    history = ranking_history(
        previous, repositories.rankings(previous.id), repositories.rankings(audit.id), phrases
    )
    return raw.model_copy(update={"ranking_history": history})


def _report_checks(
    audit: Audit,
    raw: RawAudit,
    results: list[CheckResult],
    catalogue: "Catalogue",
    settings: Settings,
    repositories: Repositories,
    job_settings: WorkerSettings,
) -> None:
    from radarauditor.feedback.hub import Feedback
    from radarauditor.reporting.report import load_pending

    try:
        business = repositories.customer(audit.customer_id).name
        feedback = Feedback.create(audit.id, business, settings, repositories)
        feedback.checks_done(
            results,
            catalogue,
            load_pending(settings.catalogue_path),
            raw.route_reports,
            paid_enabled=job_settings.sources.dataforseo.enabled,
        )
    except RadarError as exc:
        logger.warning("Check events of audit %s not written: %s", audit.id, exc)


def disabled_checks_of(audit: Audit) -> frozenset[str]:
    return frozenset(audit.disabled_checks)


def _analyse(job: Job, repositories: Repositories, settings: Settings) -> None:
    from radarauditor.jobs.analysis import analyse_job

    analyse_job(job, repositories, settings)


def is_follow_up(audit: Audit, repositories: Repositories) -> bool:
    if audit.purpose is not AuditPurpose.MONTHLY:
        return False
    return repositories.previous_audit(audit) is not None


def _queue_monthly_service(audit_id: UUID, repositories: Repositories) -> None:
    job_id = repositories.queue_monthly_service(audit_id)
    if job_id is None:
        logger.warning(
            "Audit %s: no monthly drafts, the CRM has no subscription order or its first "
            "optimisation is not finished yet",
            audit_id,
        )
    else:
        logger.info("Audit %s: monthly service queued as job %s", audit_id, job_id)


def _report(
    audit_id: UUID, customer_id: UUID, repositories: Repositories, settings: Settings
) -> None:
    from radarauditor.reporting.report import (
        build_monthly_report,
        build_recommendation,
        build_sales_report,
    )

    audit = repositories.audit(audit_id)
    if is_follow_up(audit, repositories):
        pdf = build_monthly_report(audit_id, repositories, settings)
    else:
        pdf = build_sales_report(audit_id, repositories, settings)
        recommendation = build_recommendation(audit_id, repositories, settings)
        repositories.upload(REPORTS_BUCKET, f"{audit_id}/{recommendation.name}", recommendation)
    report_path = repositories.upload(REPORTS_BUCKET, f"{audit_id}/{pdf.name}", pdf)
    repositories.update_audit(audit_id, report_path=report_path)
    if audit.purpose is AuditPurpose.MONTHLY:
        _queue_monthly_service(audit_id, repositories)
    repositories.set_audit_status(audit_id, AuditStatus.REPORTED)
    repositories.new_event(
        AUDIT_REPORTED_EVENT,
        {"customer_id": str(customer_id), "audit_id": str(audit_id), "report_path": report_path},
    )
