import asyncio
import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from conftest import AUDIT_ID, CATALOGUE_PATH, CUSTOMER_ID, REPORT_ROOT

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    ControlState,
    JobKind,
    Verdict,
)
from radarauditor.db.repositories import MigrationMissingError
from radarauditor.errors import ClaudeCodeError, StorageError
from radarauditor.feedback.events import INVALID_REASON_CODE, Result
from radarauditor.jobs import control_run
from radarauditor.jobs.control_data import ECHO_KEYS
from radarauditor.jobs.control_run import AuditPass
from radarauditor.scoring.catalog import Catalogue
from radarauditor.scoring.evaluator import total_score


def _result(
    check_id: str,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    *,
    state: ControlState = ControlState.OPEN,
    attempt: int = 0,
    error: str | None = None,
    ticks: tuple[bool | None, bool | None, bool | None] = (None, None, None),
    measured: dict[str, object] | None = None,
) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="google",
        outcome=outcome,
        measured=measured if measured is not None else {"lcp_ms": 3100},
        weight=Decimal("1.000"),
        control_state=state,
        control_attempt=attempt,
        control_last_error=error,
        verdict_collected_ok=ticks[0],
        verdict_data_ok=ticks[1],
        verdict_evaluation_ok=ticks[2],
    )


def _leaf(value: Any, path: str = "") -> tuple[str, Any] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ECHO_KEYS:
                continue
            found = _leaf(child, f"{path}.{key}" if path else key)
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            found = _leaf(child, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str) and len(value) >= 4 or isinstance(value, int | float):
        return path, value
    return None


def _answer(material: str) -> str:
    entries = []
    for check in json.loads(material)["checks"]:
        path, value = _leaf(check["data"]) or ("", "")
        verdict = {"ok": True, "reason": "", "evidence": f"{path}: {value}"}
        entry: dict[str, Any] = {
            "check_id": check["check_id"],
            "part": check["part"],
            "collected": verdict,
        }
        if check["calculated_result"]["outcome"] != "not_measurable":
            entry["data"] = verdict
            entry["evaluation"] = verdict | {"recomputed": check["calculated_result"]["outcome"]}
        entries.append(entry)
    return json.dumps({"checks": entries})


class Model:
    def __init__(self, *outcomes: str | Exception) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self._outcomes = list(outcomes)

    def __call__(self, prompt: str, material: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else _answer(material)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "catalogue_path": CATALOGUE_PATH,
            "routes_path": CATALOGUE_PATH.parent / "messwege.yaml",
            "templates_dir": REPORT_ROOT / "templates",
        }
    )


@pytest.fixture
def feedback(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    created = MagicMock()
    monkeypatch.setattr(control_run.Feedback, "create", MagicMock(return_value=created))
    return created


@pytest.fixture
def controlled(
    fake_repositories: MagicMock,
    sample_audit: Audit,
    raw_audit_full: RawAudit,
    monkeypatch: pytest.MonkeyPatch,
) -> MagicMock:
    fake_repositories.audit.return_value = sample_audit.model_copy(
        update={"status": AuditStatus.CONTROLLING, "total_score": None}
    )
    monkeypatch.setattr(
        "radarauditor.jobs.worker.load_raw", lambda audit, repositories: raw_audit_full
    )
    return fake_repositories


def _stateful(repositories: MagicMock, results: list[CheckResult]) -> dict[str, CheckResult]:
    rows = {result.check_id: result for result in results}

    def save(
        audit_id: UUID,
        check_id: str,
        collected: Verdict,
        data: Verdict | None,
        evaluation: Verdict | None,
    ) -> None:
        rows[check_id] = rows[check_id].model_copy(
            update={
                "control_state": ControlState.DONE,
                "verdict_collected_ok": collected.ok,
                "verdict_data_ok": None if data is None else data.ok,
                "verdict_evaluation_ok": None if evaluation is None else evaluation.ok,
            }
        )

    repositories.check_results.side_effect = lambda audit_id: list(rows.values())
    repositories.save_check_verdicts.side_effect = save
    return rows


async def _control(repositories: MagicMock, settings: Settings, model: Model) -> AuditPass:
    return await control_run.control_audit(AUDIT_ID, repositories, settings, model)


@pytest.mark.parametrize(
    ("failures", "seconds"), [(1, 10), (2, 30), (3, 60), (4, 300), (5, 300), (500, 300)]
)
def test_waiting_time_grows_to_five_minutes_and_stays_there(failures: int, seconds: int) -> None:
    assert control_run.retry_delay(failures) == seconds


async def test_a_successful_control_saves_the_verdicts_and_hands_over_to_the_analysis(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(controlled, [_result("a4"), _result("a7")])
    model = Model()

    outcome = await _control(controlled, settings, model)

    assert outcome == AuditPass(True)
    saved = sorted(call.args[1] for call in controlled.save_check_verdicts.call_args_list)
    assert saved == ["a4", "a7"]
    controlled.enqueue.assert_called_once_with(JobKind.ANALYSE, CUSTOMER_ID, AUDIT_ID)
    controlled.set_audit_status.assert_called_once_with(AUDIT_ID, AuditStatus.AWAITING_ANALYSIS)
    controlled.set_total_score.assert_called_once()


async def test_a_failing_agent_returns_a_wait_and_stores_the_attempt(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(controlled, [_result("a4")])
    model = Model(ClaudeCodeError("no answer"))

    outcome = await _control(controlled, settings, model)

    assert outcome == AuditPass(False, 10)
    controlled.save_control_attempt.assert_called_once_with(AUDIT_ID, "a4", 1, "no answer")
    controlled.enqueue.assert_not_called()


@pytest.mark.parametrize(("attempt", "wait"), [(1, 30), (2, 60), (3, 300), (40, 300)])
async def test_the_wait_grows_with_the_stored_attempts(
    controlled: MagicMock, feedback: MagicMock, settings: Settings, attempt: int, wait: int
) -> None:
    _stateful(controlled, [_result("a4", attempt=attempt, error="limit")])

    outcome = await _control(controlled, settings, Model(ClaudeCodeError("limit")))

    assert outcome == AuditPass(False, wait)


async def test_the_waiting_event_carries_error_and_next_attempt(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(controlled, [_result("a4")])

    await _control(controlled, settings, Model(ClaudeCodeError("no answer")))

    warning = next(
        call for call in feedback.control_event.call_args_list if call.args[1] is Result.WARNING
    )
    assert warning.args[2] == "Kontrolle wartet – Versuch 1: no answer"
    assert warning.args[3] == {
        "stage": "kontrolle_wartet",
        "attempt": 1,
        "last_error": "no answer",
        "next_attempt_in_seconds": 10,
    }


async def test_a_wrong_answer_is_stored_as_a_format_error_and_handed_to_the_retry(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    rows = _stateful(controlled, [_result("a4")])

    first = await _control(controlled, settings, Model("not json"))

    stored = controlled.save_control_attempt.call_args.args[3]
    assert first.wait == 10
    assert stored.startswith(control_run.FORMAT_ERROR_PREFIX)
    rows["a4"] = rows["a4"].model_copy(update={"control_attempt": 1, "control_last_error": stored})
    retry = Model()

    second = await _control(controlled, settings, retry)

    assert second == AuditPass(True)
    assert stored.removeprefix(control_run.FORMAT_ERROR_PREFIX) in retry.prompts[0]


async def test_a_restart_resumes_open_checks_and_leaves_finished_ones_alone(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(
        controlled,
        [
            _result("a4", attempt=2),
            _result("a7", state=ControlState.DONE, ticks=(True, True, True)),
        ],
    )

    await _control(controlled, settings, Model())

    running = next(
        call for call in feedback.control_event.call_args_list if call.args[1] is Result.RUNNING
    )
    assert running.args[0] == "a4"
    assert running.args[3] == {"stage": "kontrolle", "attempt": 3}
    assert [call.args[1] for call in controlled.save_check_verdicts.call_args_list] == ["a4"]


async def test_an_abort_by_the_user_ends_the_control_without_further_attempts(
    controlled: MagicMock, feedback: MagicMock, settings: Settings, sample_audit: Audit
) -> None:
    _stateful(controlled, [_result("a4")])
    status = {"now": AuditStatus.CONTROLLING}
    controlled.audit.side_effect = lambda audit_id: sample_audit.model_copy(
        update={"status": status["now"]}
    )

    def fail(prompt: str, material: str) -> str:
        status["now"] = AuditStatus.FAILED
        raise ClaudeCodeError("down")

    outcome = await control_run.control_audit(AUDIT_ID, controlled, settings, fail)

    assert outcome.finished is False
    controlled.enqueue.assert_not_called()
    controlled.set_audit_status.assert_not_called()


async def test_a_verdict_for_a_result_that_changed_during_the_control_is_dropped(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    rows = _stateful(controlled, [_result("a4")])

    def rescore(prompt: str, material: str) -> str:
        rows["a4"] = _result("a4", CheckOutcome.FAILED)
        return _answer(material)

    outcome = await control_run.control_audit(AUDIT_ID, controlled, settings, rescore)

    controlled.save_check_verdicts.assert_not_called()
    assert outcome.finished is False
    assert rows["a4"].control_state is ControlState.OPEN


async def test_a_planned_not_measurable_check_gets_no_agent_call_and_does_not_block(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(
        controlled,
        [_result("m1", CheckOutcome.NOT_MEASURABLE, measured={"reason": "no_access"})],
    )
    model = Model()

    outcome = await _control(controlled, settings, model)

    assert model.calls == 0
    assert outcome == AuditPass(True)
    controlled.enqueue.assert_called_once_with(JobKind.ANALYSE, CUSTOMER_ID, AUDIT_ID)


async def test_a_failed_measurement_is_judged_on_the_first_verdict_only(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(
        controlled,
        [_result("a2", CheckOutcome.NOT_MEASURABLE, measured={"reason": INVALID_REASON_CODE})],
    )

    outcome = await _control(controlled, settings, Model())

    assert outcome == AuditPass(True)
    call = controlled.save_check_verdicts.call_args
    assert call.args[3] is None
    assert call.args[4] is None


async def test_a_cross_removes_the_check_from_the_total(
    controlled: MagicMock, feedback: MagicMock, settings: Settings, catalogue: Catalogue
) -> None:
    ticked = (True, True, True)
    final = [
        _result("a4", state=ControlState.DONE, ticks=ticked),
        _result("a7", CheckOutcome.FAILED, state=ControlState.DONE, ticks=(False, None, None)),
    ]
    controlled.check_results.return_value = final

    await _control(controlled, settings, Model())

    stored = controlled.set_total_score.call_args.args[1]
    assert stored == total_score(final, catalogue)
    all_ticked = [
        final[0],
        _result("a7", CheckOutcome.FAILED, state=ControlState.DONE, ticks=ticked),
    ]
    assert stored != total_score(all_ticked, catalogue)


async def test_an_empty_total_is_stored_with_a_reason_and_the_audit_goes_on(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    controlled.check_results.return_value = [
        _result("a4", state=ControlState.DONE, ticks=(False, None, None))
    ]

    outcome = await _control(controlled, settings, Model())

    assert outcome == AuditPass(True)
    controlled.set_total_score.assert_called_once_with(AUDIT_ID, None)
    controlled.add_manager_event.assert_called_once_with(
        AUDIT_ID, Result.WARNING, control_run.TOTAL_LABEL, control_run.EMPTY_TOTAL_REASON
    )
    controlled.enqueue.assert_called_once_with(JobKind.ANALYSE, CUSTOMER_ID, AUDIT_ID)
    controlled.set_audit_status.assert_called_once_with(AUDIT_ID, AuditStatus.AWAITING_ANALYSIS)


async def test_the_control_stays_open_while_a_check_has_no_verdict(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    controlled.check_results.return_value = [_result("a4")]

    outcome = await _control(controlled, settings, Model())

    assert outcome.finished is False
    controlled.enqueue.assert_not_called()


async def _run_loop(
    repositories: MagicMock,
    settings: Settings,
    stop: asyncio.Event,
    clock: Any = lambda: 0.0,
) -> None:
    await asyncio.wait_for(
        control_run.run_control_loop(repositories, settings, stop, Model(), clock), timeout=5
    )


async def test_one_waiting_audit_does_not_block_another(
    fake_repositories: MagicMock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    other: UUID = uuid4()
    fake_repositories.audit_ids_with_status.return_value = [AUDIT_ID, other]
    seen: list[UUID] = []
    stop = asyncio.Event()

    async def fake_control(audit_id: UUID, repositories: Any, config: Any, ask: Any) -> AuditPass:
        seen.append(audit_id)
        if audit_id == AUDIT_ID:
            return AuditPass(False, 300)
        if seen.count(other) >= 2:
            stop.set()
        return AuditPass(False, None)

    monkeypatch.setattr(control_run, "control_audit", fake_control)
    monkeypatch.setattr(control_run, "POLL_SECONDS", 0)

    await _run_loop(fake_repositories, settings, stop)

    assert seen.count(AUDIT_ID) == 1
    assert seen.count(other) >= 2


async def test_an_unexpected_error_is_logged_retried_and_does_not_end_the_loop(
    fake_repositories: MagicMock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    other: UUID = uuid4()
    fake_repositories.audit_ids_with_status.return_value = [AUDIT_ID, other]
    seen: list[UUID] = []
    stop = asyncio.Event()

    async def fake_control(audit_id: UUID, repositories: Any, config: Any, ask: Any) -> AuditPass:
        seen.append(audit_id)
        if audit_id == AUDIT_ID:
            raise ValueError("unexpected")
        stop.set()
        return AuditPass(True)

    monkeypatch.setattr(control_run, "control_audit", fake_control)
    monkeypatch.setattr(control_run, "POLL_SECONDS", 0)

    await _run_loop(fake_repositories, settings, stop)

    assert seen == [AUDIT_ID, other]
    event = fake_repositories.add_manager_event.call_args.args
    assert event[0] == AUDIT_ID
    assert event[1] is Result.WARNING
    assert "unerwarteter Fehler ValueError: unexpected" in event[3]
    assert "Versuch 1" in event[3]


async def test_the_loop_survives_a_failing_pass(
    fake_repositories: MagicMock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    calls = {"count": 0}

    def look(status: AuditStatus) -> list[UUID]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise StorageError("database down")
        stop.set()
        return []

    fake_repositories.audit_ids_with_status.side_effect = look
    monkeypatch.setattr(control_run, "POLL_SECONDS", 0)

    await _run_loop(fake_repositories, settings, stop)

    assert calls["count"] == 2


async def test_a_missing_migration_stops_the_control_with_its_name(
    controlled: MagicMock, feedback: MagicMock, settings: Settings
) -> None:
    _stateful(controlled, [_result("a4")])
    controlled.missing_migrations.return_value = ["025", "030"]
    model = Model()

    with pytest.raises(StorageError, match="025, 030"):
        await _control(controlled, settings, model)

    assert model.calls == 0
    controlled.save_check_verdicts.assert_not_called()


async def test_the_loop_survives_a_plain_error_when_listing_the_audits(
    controlled: MagicMock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    calls = 0

    def listing(status: AuditStatus) -> list[UUID]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("network down")
        stop.set()
        return []

    async def no_pause(seconds: float) -> bool:
        return stop.is_set()

    controlled.audit_ids_with_status.side_effect = listing
    monkeypatch.setattr(control_run, "stop_pause", lambda event: no_pause)

    await asyncio.wait_for(
        control_run.run_control_loop(controlled, settings, stop, Model()), timeout=5
    )

    assert calls == 2


async def test_the_loop_survives_a_plain_error_inside_a_pass(
    controlled: MagicMock,
    feedback: MagicMock,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    controlled.audit_ids_with_status.return_value = [AUDIT_ID]
    controlled.audit.side_effect = RuntimeError("unexpected")

    async def stop_now(seconds: float) -> bool:
        stop.set()
        return True

    monkeypatch.setattr(control_run, "stop_pause", lambda event: stop_now)

    await asyncio.wait_for(
        control_run.run_control_loop(controlled, settings, stop, Model()), timeout=5
    )

    assert controlled.audit.call_count == 1


async def test_a_missing_migration_halts_the_audit_with_plain_text_and_no_repeated_passes(
    fake_repositories: MagicMock, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_repositories.audit_ids_with_status.return_value = [AUDIT_ID]
    stop = asyncio.Event()
    passes = 0

    async def fake_control(audit_id: UUID, repositories: Any, config: Any, ask: Any) -> AuditPass:
        nonlocal passes
        passes += 1
        raise MigrationMissingError("migration 025 missing")

    async def pause(seconds: float) -> bool:
        if fake_repositories.audit_ids_with_status.call_count >= 4:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(control_run, "control_audit", fake_control)
    monkeypatch.setattr(control_run, "stop_pause", lambda event: pause)

    await _run_loop(fake_repositories, settings, stop)

    assert passes == 1
    events = fake_repositories.add_manager_event.call_args_list
    assert len(events) == 1
    reason = events[0].args[3]
    assert "025" in reason
    assert reason.startswith("Kontrolle angehalten")
