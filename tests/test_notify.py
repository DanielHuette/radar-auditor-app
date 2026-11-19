from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from conftest import AUDIT_ID, CUSTOMER_ID, JobFactory

from radarauditor.config import Settings
from radarauditor.db.app_settings import WorkerCapabilities
from radarauditor.db.models import JobKind
from radarauditor.errors import StorageError
from radarauditor.feedback.events import Result
from radarauditor.jobs import worker
from radarauditor.jobs.notify import (
    APP_URL,
    REASON_LIMIT,
    Notifier,
    audit_link,
    score_words,
    shortened,
    verdict_text,
)

SERVER = "https://ntfy.sh"
TOPIC = "radar-test-topic-0123456789"
TOPIC_URL = f"{SERVER}/{TOPIC}"
LINK = f"{APP_URL}/auswertung/{AUDIT_ID}"
SUBJECT = "Audit für Dachdeckerei Muster"
AVAILABLE = WorkerCapabilities(
    chromium=False,
    claude_cli=True,
    claude_logged_in=True,
    browser_profile=False,
    analysis_available=True,
    mail_reading_available=True,
)


@pytest.fixture
def ntfy_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"ntfy_topic": TOPIC})


def _only_request(route: respx.Route) -> httpx.Request:
    (call,) = route.calls
    request: httpx.Request = call.request
    return request


def test_the_link_leads_to_the_evaluation_of_the_audit() -> None:
    assert audit_link(AUDIT_ID) == LINK


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, None),
        (Decimal("61.50"), "61,5 von 100 Punkten"),
        (Decimal("70.00"), "70 von 100 Punkten"),
        (Decimal("0"), "0 von 100 Punkten"),
    ],
)
def test_score_in_words(score: Decimal | None, expected: str | None) -> None:
    assert score_words(score) == expected


@pytest.mark.parametrize(
    ("length", "kept"),
    [
        (REASON_LIMIT - 1, REASON_LIMIT - 1),
        (REASON_LIMIT, REASON_LIMIT),
        (REASON_LIMIT + 1, REASON_LIMIT),
    ],
)
def test_a_reason_is_cut_after_the_limit(length: int, kept: int) -> None:
    assert len(shortened("x" * length)) == kept


def test_a_cut_reason_ends_with_an_ellipsis() -> None:
    assert shortened("x" * (REASON_LIMIT + 1)).endswith("…")


def test_a_reason_loses_line_breaks_and_double_spaces() -> None:
    assert shortened("Bing fehlt.\n\nGoogle  auch") == "Bing fehlt. Google auch"


@pytest.mark.parametrize(
    ("verdict", "points", "reason", "expected"),
    [
        (
            Result.MEASURED,
            "70 von 100 Punkten",
            "ignored",
            f"{SUBJECT} fertig: vollständig, 70 von 100 Punkten.",
        ),
        (Result.MEASURED, None, None, f"{SUBJECT} fertig: vollständig."),
        (
            Result.SUSPENDED,
            "50 von 100 Punkten",
            "Bing fehlt",
            f"{SUBJECT} unvollständig (50 von 100 Punkten): Bing fehlt",
        ),
        (Result.SUSPENDED, None, "Bing fehlt", f"{SUBJECT} unvollständig: Bing fehlt"),
        (Result.SUSPENDED, None, None, f"{SUBJECT} unvollständig."),
        (Result.FAILED, "50 von 100 Punkten", "Kein Netz", f"{SUBJECT} fehlgeschlagen: Kein Netz"),
        (Result.FAILED, None, "  ", f"{SUBJECT} fehlgeschlagen."),
    ],
)
def test_the_words_of_a_verdict(
    verdict: Result, points: str | None, reason: str | None, expected: str
) -> None:
    assert verdict_text(SUBJECT, verdict, points, reason) == expected


@respx.mock
def test_a_verdict_goes_to_the_phone_with_the_link_to_the_evaluation(
    fake_repositories: MagicMock, ntfy_settings: Settings
) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    Notifier(fake_repositories, ntfy_settings).audit_concluded(
        AUDIT_ID, Result.MEASURED, "Alles gemessen"
    )

    request = _only_request(route)
    assert request.content.decode("utf-8") == (
        f"{SUBJECT} fertig: vollständig, 61,5 von 100 Punkten.\n{LINK}"
    )
    assert request.headers["X-Click"] == LINK
    assert request.headers["X-Priority"] == "default"


@respx.mock
@pytest.mark.parametrize("verdict", [Result.SUSPENDED, Result.FAILED])
def test_an_audit_that_is_not_complete_is_announced_with_high_priority(
    fake_repositories: MagicMock, ntfy_settings: Settings, verdict: Result
) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    Notifier(fake_repositories, ntfy_settings).audit_concluded(AUDIT_ID, verdict, "Grund")

    assert _only_request(route).headers["X-Priority"] == "high"


@respx.mock
def test_a_verdict_is_announced_even_when_the_audit_cannot_be_read(
    fake_repositories: MagicMock, ntfy_settings: Settings
) -> None:
    fake_repositories.audit.side_effect = StorageError("offline")
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    Notifier(fake_repositories, ntfy_settings).audit_concluded(AUDIT_ID, Result.FAILED, "Kein Netz")

    assert (
        _only_request(route).content.decode("utf-8") == f"Audit fehlgeschlagen: Kein Netz\n{LINK}"
    )


@respx.mock
def test_a_verdict_is_announced_even_when_the_customer_cannot_be_read(
    fake_repositories: MagicMock, ntfy_settings: Settings
) -> None:
    fake_repositories.customer.side_effect = StorageError("offline")
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    Notifier(fake_repositories, ntfy_settings).audit_concluded(AUDIT_ID, Result.MEASURED, None)

    assert _only_request(route).content.decode("utf-8") == (
        f"Audit fertig: vollständig, 61,5 von 100 Punkten.\n{LINK}"
    )


@respx.mock
def test_without_a_topic_nothing_is_sent_and_nothing_is_read(
    fake_repositories: MagicMock, settings: Settings
) -> None:
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))
    notifier = Notifier(fake_repositories, settings)

    notifier.audit_started(AUDIT_ID, CUSTOMER_ID)
    notifier.audit_concluded(AUDIT_ID, Result.FAILED, "Grund")

    assert not route.called
    fake_repositories.audit.assert_not_called()
    fake_repositories.customer.assert_not_called()


@respx.mock
async def test_a_collect_job_announces_the_start_of_the_audit_once(
    fake_repositories: MagicMock,
    ntfy_settings: Settings,
    make_job: JobFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "run_job", AsyncMock())
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    await worker.process_job(
        make_job(JobKind.COLLECT, attempts=1), fake_repositories, ntfy_settings
    )

    request = _only_request(route)
    assert request.content.decode("utf-8") == f"{SUBJECT} gestartet.\n{LINK}"
    assert request.headers["X-Click"] == LINK


@respx.mock
@pytest.mark.parametrize(
    ("kind", "attempts"),
    [(JobKind.COLLECT, 2), (JobKind.SCORE, 1), (JobKind.ANALYSE, 1), (JobKind.REPORT, 1)],
)
async def test_other_jobs_and_later_attempts_announce_no_start(
    fake_repositories: MagicMock,
    ntfy_settings: Settings,
    make_job: JobFactory,
    monkeypatch: pytest.MonkeyPatch,
    kind: JobKind,
    attempts: int,
) -> None:
    monkeypatch.setattr(worker, "run_job", AsyncMock())
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    await worker.process_job(make_job(kind, attempts=attempts), fake_repositories, ntfy_settings)

    assert not route.called


@respx.mock
async def test_a_collect_job_without_an_audit_announces_no_start(
    fake_repositories: MagicMock,
    ntfy_settings: Settings,
    make_job: JobFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "run_job", AsyncMock())
    route = respx.post(TOPIC_URL).mock(return_value=httpx.Response(200))

    await worker.process_job(
        make_job(JobKind.COLLECT, attempts=1, audit_id=None), fake_repositories, ntfy_settings
    )

    assert not route.called
