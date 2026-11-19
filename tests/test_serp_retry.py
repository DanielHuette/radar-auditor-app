from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock

import pytest
from conftest import CAPTCHA_PAGE, BrowserFactory, JobFactory
from pydantic import SecretStr

from radarauditor.collectors import apify_serp
from radarauditor.collectors.paid_rankings import PaidSearchBatch
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import DeferredSearch, RawAudit, SerpResult
from radarauditor.config import Settings
from radarauditor.db.app_settings import SourcesSettings
from radarauditor.db.models import JobKind, JobStatus
from radarauditor.errors import SourceDisabledError
from radarauditor.jobs import serp_retry

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
IMAGES_HTML = '<div id="search"><div data-lpage="https://bilder.example.de/a"></div></div>'


@pytest.fixture
def google_html() -> str:
    return (FIXTURES / "google_dachdecker_muenster.html").read_text(encoding="utf-8")


def _serp(phrase: str, vertical: Literal["web", "images"] = "web") -> SerpResult:
    return SerpResult(
        phrase=phrase,
        organic=[(1, "old.example.de")],
        local_pack=[],
        ai_overview_present=False,
        ai_overview_mentions=[],
        people_also_ask=[],
        measured_at=NOW,
        vertical=vertical,
    )


def _deferred_raw(raw: RawAudit, searches: list[DeferredSearch]) -> RawAudit:
    return raw.model_copy(
        update={
            "serps": [_serp("Dachdecker Münster")],
            "deferred_searches": searches,
            "warnings": ["website: slow", "serps: 2 searches deferred to a retry: bot check"],
        }
    )


def _gate(allowed: int = 1000, enabled: bool = True) -> SourceGate:
    sources = SourcesSettings.model_validate({"google_search": {"enabled": enabled}})
    used: list[str] = []

    def consume(source: str, cap: int) -> bool:
        used.append(source)
        return len(used) <= allowed

    return SourceGate(sources, consume)


@pytest.mark.parametrize(
    ("attempts", "delay"),
    [(1, timedelta(hours=1)), (2, timedelta(hours=4)), (3, timedelta(hours=12))],
)
def test_next_run_follows_the_backoff(attempts: int, delay: timedelta) -> None:
    assert serp_retry.next_run_at(attempts, NOW) == NOW + delay


def test_backoff_starts_after_fifteen_minutes_and_ends_after_four_runs() -> None:
    assert serp_retry.first_run_at(NOW) == NOW + timedelta(minutes=15)
    assert serp_retry.next_run_at(4, NOW) is None
    assert serp_retry.next_run_at(0, NOW) is None


def test_schedule_queues_a_deferred_job_for_the_audit(
    fake_repositories: MagicMock, make_job: JobFactory
) -> None:
    job = make_job(JobKind.SERP_RETRY)
    assert job.audit_id is not None

    serp_retry.schedule(fake_repositories, job.customer_id, job.audit_id, NOW)

    fake_repositories.enqueue.assert_called_once_with(
        JobKind.SERP_RETRY, job.customer_id, job.audit_id, run_after=NOW + timedelta(minutes=15)
    )


def test_record_failure_requeues_for_the_next_step(
    fake_repositories: MagicMock, make_job: JobFactory
) -> None:
    job = make_job(JobKind.SERP_RETRY, attempts=2)

    serp_retry.record_failure(job, fake_repositories, "still blocked", NOW)

    fake_repositories.requeue_job.assert_called_once_with(
        job.id, "still blocked", run_after=NOW + timedelta(hours=4)
    )
    fake_repositories.set_audit_status.assert_not_called()


def test_record_failure_gives_up_after_the_last_step(
    fake_repositories: MagicMock, make_job: JobFactory
) -> None:
    job = make_job(JobKind.SERP_RETRY, attempts=4)

    serp_retry.record_failure(job, fake_repositories, "still blocked", NOW)

    fake_repositories.requeue_job.assert_not_called()
    fake_repositories.finish_job.assert_called_once()
    job_id, status, reason = fake_repositories.finish_job.call_args.args
    assert (job_id, status) == (job.id, JobStatus.FAILED)
    assert reason == "Google search still blocked after 4 attempts, giving up: still blocked"
    fake_repositories.set_audit_status.assert_not_called()


async def test_search_deferred_runs_everything_and_clears_the_deferral(
    fake_browser: BrowserFactory,
    collector_settings: Settings,
    sample_raw_audit: RawAudit,
    google_html: str,
) -> None:
    raw = _deferred_raw(
        sample_raw_audit,
        [
            DeferredSearch(phrase="Dachdecker Münster"),
            DeferredSearch(phrase="Dachdecker Münster", vertical="images"),
        ],
    )
    fake = fake_browser([google_html, IMAGES_HTML])

    async with fake.service as service:
        run = await serp_retry.search_deferred(raw, service, _gate(), collector_settings)

    assert run.found == 2
    assert run.remaining == []
    assert run.reason is None
    assert [(serp.phrase, serp.vertical) for serp in run.raw.serps] == [
        ("Dachdecker Münster", "web"),
        ("Dachdecker Münster", "images"),
    ]
    assert run.raw.serps[0].organic[0] == (1, "dachdecker-hollenbeck.de")
    assert run.raw.deferred_searches == []
    assert run.raw.warnings == ["website: slow"]


async def test_search_deferred_keeps_what_google_still_blocks(
    fake_browser: BrowserFactory,
    collector_settings: Settings,
    sample_raw_audit: RawAudit,
    google_html: str,
) -> None:
    raw = _deferred_raw(
        sample_raw_audit,
        [
            DeferredSearch(phrase="Dachdecker Münster"),
            DeferredSearch(phrase="Flachdach Münster"),
            DeferredSearch(phrase="Dachdecker Münster", vertical="images"),
        ],
    )
    fake = fake_browser([google_html, CAPTCHA_PAGE])

    async with fake.service as service:
        run = await serp_retry.search_deferred(raw, service, _gate(), collector_settings)

    assert run.found == 1
    assert run.remaining == [
        DeferredSearch(phrase="Flachdach Münster"),
        DeferredSearch(phrase="Dachdecker Münster", vertical="images"),
    ]
    assert run.reason == "www.google.de shows a bot check"
    assert len(fake.page.visited) == 2
    assert run.raw.deferred_searches == run.remaining
    assert run.raw.warnings == [
        "website: slow",
        "serps: 2 searches deferred to a retry: www.google.de shows a bot check",
    ]


async def test_search_deferred_leaves_searches_over_the_cap_deferred(
    fake_browser: BrowserFactory,
    collector_settings: Settings,
    sample_raw_audit: RawAudit,
    google_html: str,
) -> None:
    raw = _deferred_raw(
        sample_raw_audit,
        [DeferredSearch(phrase="Dachdecker Münster"), DeferredSearch(phrase="Flachdach Münster")],
    )
    fake = fake_browser([google_html])

    async with fake.service as service:
        run = await serp_retry.search_deferred(raw, service, _gate(allowed=1), collector_settings)

    assert run.found == 1
    assert run.remaining == [DeferredSearch(phrase="Flachdach Münster")]
    assert run.reason == serp_retry.CAP_REASON


async def test_search_deferred_refuses_when_google_search_is_switched_off(
    fake_browser: BrowserFactory, collector_settings: Settings, sample_raw_audit: RawAudit
) -> None:
    raw = _deferred_raw(sample_raw_audit, [DeferredSearch(phrase="Dachdecker Münster")])
    fake = fake_browser([])

    async with fake.service as service:
        with pytest.raises(SourceDisabledError):
            await serp_retry.search_deferred(raw, service, _gate(enabled=False), collector_settings)

    assert fake.context.opened_with == []


def test_merge_keeps_other_engines_and_verticals(sample_raw_audit: RawAudit) -> None:
    bing = _serp("Dachdecker Münster").model_copy(update={"engine": "bing"})
    raw = sample_raw_audit.model_copy(update={"serps": [_serp("Dachdecker Münster"), bing]})
    fresh = _serp("Dachdecker Münster").model_copy(update={"organic": [(1, "new.example.de")]})

    merged = serp_retry.merge(raw, [fresh], [], None)

    assert [(serp.engine, serp.organic) for serp in merged.serps] == [
        ("bing", [(1, "old.example.de")]),
        ("google", [(1, "new.example.de")]),
    ]


def _apify_result(phrase: str, vertical: Literal["web", "images"] = "web") -> SerpResult:
    return _serp(phrase, vertical).model_copy(update={"route": "apify", "organic": [(1, "new.de")]})


async def test_search_deferred_reads_through_apify_first_and_leaves_the_browser_unused(
    fake_browser: BrowserFactory,
    sample_raw_audit: RawAudit,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _deferred_raw(
        sample_raw_audit,
        [
            DeferredSearch(phrase="Dachdecker Münster"),
            DeferredSearch(phrase="Dachdecker Münster", vertical="images"),
        ],
    )
    asked: list[tuple[list[str], frozenset[str]]] = []

    async def web(
        settings: Settings, gate: SourceGate, phrases: list[str], ad_phrases: frozenset[str]
    ) -> PaidSearchBatch:
        asked.append((phrases, ad_phrases))
        return PaidSearchBatch([_apify_result(phrase) for phrase in phrases], [])

    async def images(settings: Settings, gate: SourceGate, phrases: list[str]) -> PaidSearchBatch:
        return PaidSearchBatch([_apify_result(phrase, "images") for phrase in phrases], [])

    monkeypatch.setattr(apify_serp, "search_web", web)
    monkeypatch.setattr(apify_serp, "search_images", images)
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("token")})
    fake = fake_browser([])

    async with fake.service as service:
        run = await serp_retry.search_deferred(raw, service, _gate(allowed=0), settings)

    assert run.found == 2
    assert run.remaining == []
    assert run.raw.deferred_searches == []
    assert [(serp.phrase, serp.vertical, serp.route) for serp in run.raw.serps] == [
        ("Dachdecker Münster", "web", "apify"),
        ("Dachdecker Münster", "images", "apify"),
    ]
    assert fake.context.opened_with == []
    assert asked == [(["Dachdecker Münster"], frozenset())]


async def test_search_deferred_sends_what_apify_could_not_answer_to_the_browser(
    fake_browser: BrowserFactory,
    sample_raw_audit: RawAudit,
    collector_settings: Settings,
    google_html: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _deferred_raw(
        sample_raw_audit,
        [DeferredSearch(phrase="Dachdecker Münster"), DeferredSearch(phrase="Flachdach Münster")],
    )

    async def web(
        settings: Settings, gate: SourceGate, phrases: list[str], ad_phrases: frozenset[str]
    ) -> PaidSearchBatch:
        return PaidSearchBatch([_apify_result(phrases[0])], phrases[1:], "Apify cap")

    async def images(settings: Settings, gate: SourceGate, phrases: list[str]) -> PaidSearchBatch:
        return PaidSearchBatch([], [])

    monkeypatch.setattr(apify_serp, "search_web", web)
    monkeypatch.setattr(apify_serp, "search_images", images)
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("token")})
    fake = fake_browser([google_html])

    async with fake.service as service:
        run = await serp_retry.search_deferred(raw, service, _gate(), settings)

    assert run.found == 2
    assert run.remaining == []
    assert len(fake.page.visited) == 1
    assert "Flachdach" in fake.page.visited[0]
