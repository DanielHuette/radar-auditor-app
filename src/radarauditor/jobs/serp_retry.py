import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from radarauditor.collectors import apify_serp, serp
from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import DeferredSearch, RawAudit, SerpResult
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.db.models import Job, JobKind, JobStatus
from radarauditor.db.repositories import Repositories

logger = logging.getLogger(__name__)

BACKOFF = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
CAP_REASON = "daily cap of Google searches reached"


@dataclass(frozen=True)
class RetryRun:
    raw: RawAudit
    found: int
    remaining: list[DeferredSearch]
    reason: str | None


def first_run_at(now: datetime) -> datetime:
    return now + BACKOFF[0]


def next_run_at(attempts: int, now: datetime) -> datetime | None:
    if attempts < 1 or attempts >= len(BACKOFF):
        return None
    return now + BACKOFF[attempts]


def schedule(repositories: Repositories, customer_id: UUID, audit_id: UUID, now: datetime) -> Job:
    run_after = first_run_at(now)
    logger.info("Google searches of audit %s deferred, retry after %s", audit_id, run_after)
    return repositories.enqueue(JobKind.SERP_RETRY, customer_id, audit_id, run_after=run_after)


def record_failure(job: Job, repositories: Repositories, message: str, now: datetime) -> None:
    run_after = next_run_at(job.attempts, now)
    if run_after is not None:
        logger.info("Retry job %s requeued for %s: %s", job.id, run_after, message)
        repositories.requeue_job(job.id, message, run_after=run_after)
        return
    reason = f"Google search still blocked after {job.attempts} attempts, giving up: {message}"
    logger.warning("Retry job %s failed: %s", job.id, reason)
    repositories.finish_job(job.id, JobStatus.FAILED, reason)


async def search_deferred(
    raw: RawAudit, browser: BrowserService, gate: SourceGate, settings: Settings
) -> RetryRun:
    web = [search.phrase for search in raw.deferred_searches if search.vertical == "web"]
    images = [search.phrase for search in raw.deferred_searches if search.vertical == "images"]
    results: list[SerpResult] = []
    reason: str | None = None
    if apify_serp.is_available(settings, gate):
        brand = raw.business.name if raw.business is not None else None
        web_paid = await apify_serp.search_web(
            settings, gate, web, apify_serp.brand_phrases(web, brand)
        )
        image_paid = await apify_serp.search_images(settings, gate, images)
        results = [*web_paid.results, *image_paid.results]
        web, images = web_paid.failed, image_paid.failed
        reason = web_paid.reason or image_paid.reason
    pending = [
        *(DeferredSearch(phrase=phrase) for phrase in web),
        *(DeferredSearch(phrase=phrase, vertical="images") for phrase in images),
    ]
    if not pending:
        return RetryRun(merge(raw, results, [], None), len(results), [], None)
    granted = gate.acquire_up_to(Source.GOOGLE_SEARCH, len(pending))
    allowed, over_cap = pending[:granted], pending[granted:]
    web = [search.phrase for search in allowed if search.vertical == "web"]
    images = [search.phrase for search in allowed if search.vertical == "images"]
    web_batch = await serp.search_all(browser, web)
    results.extend(web_batch.results)
    remaining = [DeferredSearch(phrase=phrase) for phrase in web_batch.deferred]
    reason = web_batch.reason or reason
    if web_batch.deferred:
        remaining.extend(DeferredSearch(phrase=phrase, vertical="images") for phrase in images)
    else:
        image_batch = await serp.search_images(browser, images)
        results.extend(image_batch.results)
        remaining.extend(
            DeferredSearch(phrase=phrase, vertical="images") for phrase in image_batch.deferred
        )
        reason = image_batch.reason
    if over_cap:
        remaining.extend(over_cap)
        reason = reason or CAP_REASON
    return RetryRun(merge(raw, results, remaining, reason), len(results), remaining, reason)


def merge(
    raw: RawAudit, results: list[SerpResult], remaining: list[DeferredSearch], reason: str | None
) -> RawAudit:
    new_keys = {(result.engine, result.vertical, result.phrase) for result in results}
    serps = [
        result
        for result in raw.serps
        if (result.engine, result.vertical, result.phrase) not in new_keys
    ]
    warnings = [warning for warning in raw.warnings if serp.DEFERRAL_NOTE not in warning]
    if remaining:
        warnings.append(f"serps: {serp.deferral_note(len(remaining), reason)}")
    return raw.model_copy(
        update={
            "serps": [*serps, *results],
            "deferred_searches": list(remaining),
            "warnings": warnings,
        }
    )
