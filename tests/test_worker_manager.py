import asyncio
import functools
from typing import Any
from unittest.mock import MagicMock

import pytest

from radarauditor.config import Settings
from radarauditor.db.app_settings import WorkerCapabilities
from radarauditor.db.models import Job, JobKind
from radarauditor.jobs import audit_manager, job_signal, manager_agent, worker

AVAILABLE = WorkerCapabilities(
    chromium=False,
    claude_cli=True,
    claude_logged_in=True,
    browser_profile=False,
    analysis_available=True,
    mail_reading_available=True,
)


@pytest.fixture(autouse=True)
def no_document_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "run_document_requests", lambda repositories, settings: None)


@pytest.fixture
def managed(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"audit_manager_enabled": True, "cockpit_assistant_poll_seconds": 0}
    )


@pytest.fixture
def unmanaged(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"audit_manager_enabled": False, "cockpit_assistant_poll_seconds": 0}
    )


def _record_claims(fake_repositories: MagicMock, stop: asyncio.Event) -> list[JobKind]:
    kinds: list[JobKind] = []

    def claim(kind: JobKind, limit: int) -> list[Job]:
        kinds.append(kind)
        if kind is worker.PRIORITY[-1]:
            stop.set()
        return []

    fake_repositories.claim_jobs.side_effect = claim
    return kinds


async def test_with_the_audit_manager_the_loop_leaves_the_audit_jobs_to_it(
    fake_repositories: MagicMock, managed: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    monkeypatch.setattr(worker, "detect_capabilities", lambda settings: AVAILABLE)
    monkeypatch.setattr(worker, "_manage_audits", _idle)
    kinds = _record_claims(fake_repositories, stop)

    await asyncio.wait_for(worker.serve(fake_repositories, managed, stop), timeout=2)

    assert kinds == [JobKind.APPLY, JobKind.OPTIMIZE]


async def test_without_the_audit_manager_the_loop_claims_every_kind(
    fake_repositories: MagicMock, unmanaged: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    monkeypatch.setattr(worker, "detect_capabilities", lambda settings: AVAILABLE)
    kinds = _record_claims(fake_repositories, stop)

    await asyncio.wait_for(worker.serve(fake_repositories, unmanaged, stop), timeout=2)

    assert kinds == list(worker.PRIORITY)


async def _idle(repositories: Any, settings: Settings, stop: asyncio.Event) -> None:
    await stop.wait()


async def test_serve_starts_the_audit_manager_when_it_is_enabled(
    fake_repositories: MagicMock, managed: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    started: list[Settings] = []

    async def manage(repositories: Any, settings: Settings, current: asyncio.Event) -> None:
        started.append(settings)
        await current.wait()

    monkeypatch.setattr(worker, "detect_capabilities", lambda settings: AVAILABLE)
    monkeypatch.setattr(worker, "_manage_audits", manage)
    _record_claims(fake_repositories, stop)

    await asyncio.wait_for(worker.serve(fake_repositories, managed, stop), timeout=2)

    assert started == [managed]


async def test_serve_does_not_start_the_audit_manager_when_it_is_off(
    fake_repositories: MagicMock, unmanaged: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    started: list[Settings] = []

    async def manage(repositories: Any, settings: Settings, current: asyncio.Event) -> None:
        started.append(settings)

    monkeypatch.setattr(worker, "detect_capabilities", lambda settings: AVAILABLE)
    monkeypatch.setattr(worker, "_manage_audits", manage)
    _record_claims(fake_repositories, stop)

    await asyncio.wait_for(worker.serve(fake_repositories, unmanaged, stop), timeout=2)

    assert started == []


async def test_the_audit_manager_gets_the_realtime_opener_and_the_agent_launcher(
    fake_repositories: MagicMock, managed: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    received: list[Any] = []

    async def run(*arguments: Any) -> None:
        received.extend(arguments)

    monkeypatch.setattr(audit_manager, "run_audit_manager", run)

    await worker._manage_audits(fake_repositories, managed, stop)

    repositories, settings, current, opener, launch = received
    assert (repositories, settings, current) == (fake_repositories, managed, stop)
    assert isinstance(opener, functools.partial)
    assert opener.func is job_signal.open_jobs_subscription
    assert opener.args == (managed,)
    assert launch is manager_agent.run_manager_agent


def test_the_kinds_of_the_audit_manager_are_the_audit_stages_repair_and_search_retry() -> None:
    assert worker.MANAGED_KINDS == {
        JobKind.COLLECT,
        JobKind.REPAIR,
        JobKind.SCORE,
        JobKind.ANALYSE,
        JobKind.REPORT,
        JobKind.SERP_RETRY,
    }
    assert frozenset(audit_manager.AUDIT_KINDS) == worker.MANAGED_KINDS
