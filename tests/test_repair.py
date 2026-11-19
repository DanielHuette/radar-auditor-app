from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from conftest import keep_files_in_memory

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.collectors.steps import StepStore
from radarauditor.config import Settings
from radarauditor.db.app_settings import WorkerSettings
from radarauditor.db.models import AuditStatus, JobKind
from radarauditor.errors import ClaudeCodeError
from radarauditor.feedback.events import Result
from radarauditor.jobs import repair, worker

AUDIT_ID = uuid4()
CUSTOMER_ID = uuid4()
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def repositories(fake_repositories: MagicMock) -> MagicMock:
    keep_files_in_memory(fake_repositories)
    return fake_repositories


def _fail(repositories: MagicMock, step: str, reason: str) -> None:
    StepStore(AUDIT_ID, repositories).keep(step, [], [], reason)


def _events(repositories: MagicMock) -> list[tuple[Result, str, str]]:
    return [
        (call.args[1], call.args[2], call.args[4])
        for call in repositories.add_repair_event.call_args_list
    ]


class Agent:
    def __init__(self, repositories: MagicMock, fixes: bool) -> None:
        self.calls: list[tuple[UUID, str, str]] = []
        self._repositories = repositories
        self._fixes = fixes

    async def __call__(self, audit_id: UUID, step: str, reason: str, settings: Settings) -> None:
        self.calls.append((audit_id, step, reason))
        if self._fixes:
            StepStore(audit_id, self._repositories).keep(step, [], [], None)


async def test_the_agent_takes_over_exactly_the_failed_step(
    repositories: MagicMock, collector_settings: Settings
) -> None:
    _fail(repositories, "bing", "Bing zeigt eine Sicherheitsabfrage")
    StepStore(AUDIT_ID, repositories).keep("serps", [], [], None)
    agent = Agent(repositories, fixes=True)

    outcome = await repair.repair_failed_steps(AUDIT_ID, repositories, collector_settings, agent)

    assert agent.calls == [(AUDIT_ID, "bing", "Bing zeigt eine Sicherheitsabfrage")]
    assert outcome == repair.RepairOutcome(True)
    assert _events(repositories) == [
        (Result.RESTARTED, repair.TAKEOVER_LABEL, "bing"),
        (Result.MEASURED, repair.DONE_LABEL, "bing"),
    ]


async def test_a_step_the_agent_cannot_fix_waits_for_the_next_run_and_warns_every_fifth_time(
    repositories: MagicMock, collector_settings: Settings
) -> None:
    _fail(repositories, "bing", "Bing zeigt eine Sicherheitsabfrage")
    agent = Agent(repositories, fixes=False)

    outcomes = [
        await repair.repair_failed_steps(AUDIT_ID, repositories, collector_settings, agent)
        for _ in range(repair.WARNING_EVERY)
    ]

    assert outcomes[-1] == repair.RepairOutcome(False, None)
    warnings = [event for event in _events(repositories) if event[0] is Result.WARNING]
    assert warnings == [(Result.WARNING, repair.WARNING_LABEL, "bing")]
    stored = StepStore(AUDIT_ID, repositories).stored("bing")
    assert stored is not None and stored.attempts == repair.WARNING_EVERY


async def test_a_step_waiting_for_credit_is_tried_again_an_hour_later(
    repositories: MagicMock, collector_settings: Settings
) -> None:
    _fail(repositories, "serps", "DataForSEO: Guthaben aufgebraucht")
    StepStore(AUDIT_ID, repositories).mark_credit_short("serps", "es fehlen 0,10 EUR")

    outcome = await repair.repair_failed_steps(
        AUDIT_ID, repositories, collector_settings, Agent(repositories, False), lambda: NOW
    )

    assert outcome == repair.RepairOutcome(False, NOW + repair.CREDIT_WAIT)


async def test_an_agent_that_cannot_start_is_reported_and_tried_later(
    repositories: MagicMock, collector_settings: Settings
) -> None:
    _fail(repositories, "bing", "Bing zeigt eine Sicherheitsabfrage")

    async def broken(audit_id: UUID, step: str, reason: str, settings: Settings) -> None:
        raise ClaudeCodeError("Claude Code ist nicht angemeldet")

    outcome = await repair.repair_failed_steps(
        AUDIT_ID, repositories, collector_settings, broken, lambda: NOW
    )

    assert outcome.done is False
    assert outcome.run_after is not None and outcome.run_after > NOW
    assert _events(repositories)[-1][0] is Result.FAILED


async def test_a_failed_part_goes_to_the_agent_and_the_audit_is_scored_after_the_repair(
    repositories: MagicMock,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = RawAudit(
        website=None,
        pagespeed=None,
        place=None,
        competitor_places=[],
        serps=[],
        directories=[],
        assistant_answers=[],
        collected_at=NOW,
        warnings=[],
    )
    runs: list[int] = []

    async def collect(audit_id: UUID, *args: object, **kwargs: object) -> RawAudit:
        runs.append(1)
        if len(runs) == 1:
            _fail(repositories, "bing", "Bing zeigt eine Sicherheitsabfrage")
        return raw

    agent = Agent(repositories, fixes=True)
    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collect)
    monkeypatch.setattr("radarauditor.jobs.repair_agent.run_repair_agent", agent)
    settings = WorkerSettings()

    await worker._collect(AUDIT_ID, CUSTOMER_ID, repositories, collector_settings, settings)

    queued = [call.args[0] for call in repositories.enqueue.call_args_list]
    assert queued == [JobKind.REPAIR]
    await worker._repair(AUDIT_ID, CUSTOMER_ID, repositories, collector_settings, settings)

    assert agent.calls == [(AUDIT_ID, "bing", "Bing zeigt eine Sicherheitsabfrage")]
    assert len(runs) == 2
    assert [call.args[0] for call in repositories.enqueue.call_args_list] == [
        JobKind.REPAIR,
        JobKind.SCORE,
    ]
    repositories.set_audit_status.assert_any_call(AUDIT_ID, AuditStatus.COLLECTED)


@pytest.mark.parametrize("step", ["directories", "social", "wayback"])
async def test_a_failed_removed_step_is_neither_repaired_nor_counted_as_open(
    repositories: MagicMock, collector_settings: Settings, step: str
) -> None:
    _fail(repositories, step, "alter Fehler")
    agent = Agent(repositories, fixes=True)

    outcome = await repair.repair_failed_steps(AUDIT_ID, repositories, collector_settings, agent)

    assert agent.calls == []
    assert outcome.done is True
    assert StepStore(AUDIT_ID, repositories).failed() == {}


@pytest.mark.parametrize(
    ("step", "label"),
    [
        ("ads_google", "Google-Werbeanzeigen"),
        ("ads_meta", "Meta-Werbeanzeigen"),
        ("control", "Kontrolle"),
    ],
)
def test_the_new_steps_have_a_german_label(step: str, label: str) -> None:
    assert repair.step_label(step) == label


async def test_a_failed_ads_step_is_not_handed_to_the_repair_agent(
    repositories: MagicMock, collector_settings: Settings
) -> None:
    _fail(repositories, "ads_google", "ads interface down")
    _fail(repositories, "ads_meta", "ads interface down")
    agent = Agent(repositories, fixes=False)

    outcome = await repair.repair_failed_steps(AUDIT_ID, repositories, collector_settings, agent)

    assert agent.calls == []
    assert outcome == repair.RepairOutcome(True)
