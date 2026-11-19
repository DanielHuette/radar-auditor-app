from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from conftest import keep_files_in_memory

from radarauditor.collectors import estimate
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.steps import StepStore
from radarauditor.config import Settings
from radarauditor.db.models import AuditEstimate, AuditStatus
from radarauditor.errors import RadarError
from radarauditor.feedback.events import Result
from radarauditor.jobs import rerun

AUDIT_ID = uuid4()
SERPS = estimate.task_post("/v3/serp/google/organic")


@pytest.fixture
def repositories(fake_repositories: MagicMock) -> MagicMock:
    keep_files_in_memory(fake_repositories)
    StepStore(AUDIT_ID, fake_repositories).keep(
        "serps", [], [], "Bing zeigt eine Sicherheitsabfrage"
    )
    fake_repositories.estimates.return_value = [
        AuditEstimate(
            source="dataforseo",
            service=SERPS,
            calls=6,
            unit_cost=Decimal("0.0012"),
            cost=Decimal("0.0072"),
        )
    ]
    return fake_repositories


def _credit(monkeypatch: pytest.MonkeyPatch, usd: str) -> None:
    async def balances(settings: Settings) -> estimate.Balances:
        return estimate.Balances(Decimal(usd), None, [])

    monkeypatch.setattr(estimate, "read_balances", balances)


class Collector:
    def __init__(self, probe_fails: str | None, full_fails: str | None) -> None:
        self.runs: list[tuple[frozenset[str] | None, bool]] = []
        self._probe_fails = probe_fails
        self._full_fails = full_fails

    async def __call__(self, audit_id: object, *args: object, **kwargs: object) -> None:
        only = kwargs.get("only")
        probe = bool(kwargs.get("probe"))
        steps = kwargs["steps"]
        assert isinstance(steps, StepStore)
        self.runs.append((only if isinstance(only, frozenset) else None, probe))
        if probe:
            steps.keep_probe("serps", self._probe_fails)
        else:
            steps.keep("serps", [], [], self._full_fails)


async def test_a_paid_step_is_not_repeated_without_a_change_at_the_cause(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate
) -> None:
    with pytest.raises(RadarError, match="--change"):
        await rerun.rerun_step(AUDIT_ID, "serps", " ", open_gate, repositories, collector_settings)


async def test_a_step_waits_for_credit_and_names_the_missing_amount(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credit(monkeypatch, "0.005")
    collector = Collector(None, None)
    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collector)

    result = await rerun.rerun_step(
        AUDIT_ID, "serps", "Standort korrigiert", open_gate, repositories, collector_settings
    )

    assert result.measured is False
    assert "es fehlen 0,0022 EUR" in result.message
    assert collector.runs == []
    stored = StepStore(AUDIT_ID, repositories).stored("serps")
    assert stored is not None and stored.credit_short == result.message
    (event,) = repositories.add_repair_event.call_args_list
    assert event.args[1] is Result.WARNING


async def test_the_step_runs_in_full_only_after_a_successful_probe(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credit(monkeypatch, "1")
    collector = Collector(None, None)
    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collector)

    result = await rerun.rerun_step(
        AUDIT_ID, "serps", "Standort korrigiert", open_gate, repositories, collector_settings
    )

    assert result.measured is True
    assert collector.runs == [(frozenset({"serps"}), True), (frozenset({"serps"}), False)]
    assert StepStore(AUDIT_ID, repositories).failed() == {}


async def test_a_failed_probe_stops_before_the_full_run(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credit(monkeypatch, "1")
    collector = Collector("Bing zeigt eine Sicherheitsabfrage", None)
    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collector)

    result = await rerun.rerun_step(
        AUDIT_ID, "serps", "Standort korrigiert", open_gate, repositories, collector_settings
    )

    assert result == rerun.RerunResult(
        False, "Probeabruf gescheitert: Bing zeigt eine Sicherheitsabfrage"
    )
    assert collector.runs == [(frozenset({"serps"}), True)]


async def test_a_free_step_needs_no_change_and_no_credit(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    StepStore(AUDIT_ID, repositories).keep("pagespeed", None, [], "PageSpeed hat nicht geantwortet")
    collector = Collector(None, None)

    async def pagespeed(audit_id: object, *args: object, **kwargs: object) -> None:
        steps = kwargs["steps"]
        assert isinstance(steps, StepStore)
        if kwargs.get("probe"):
            steps.keep_probe("pagespeed", None)
        else:
            steps.keep("pagespeed", None, [], None)
        collector.runs.append((None, bool(kwargs.get("probe"))))

    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", pagespeed)

    result = await rerun.rerun_step(
        AUDIT_ID, "pagespeed", "", open_gate, repositories, collector_settings
    )

    assert result.measured is True
    assert len(collector.runs) == 2


async def test_an_unknown_step_is_refused(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate
) -> None:
    with pytest.raises(RadarError, match="unknown step"):
        await rerun.rerun_step(AUDIT_ID, "fax", "x", open_gate, repositories, collector_settings)


@pytest.mark.parametrize("step", ["directories", "social", "wayback"])
async def test_a_removed_step_is_refused_with_a_clear_message(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate, step: str
) -> None:
    with pytest.raises(RadarError, match=f"step {step} was removed from the audit"):
        await rerun.rerun_step(AUDIT_ID, step, "x", open_gate, repositories, collector_settings)


async def test_the_control_can_be_started_again_without_a_change(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate
) -> None:
    result = await rerun.rerun_step(
        AUDIT_ID, "control", "", open_gate, repositories, collector_settings
    )

    assert result == rerun.RerunResult(True, "Kontrolle neu gestartet")
    repositories.reopen_control.assert_called_once_with(AUDIT_ID)
    repositories.set_audit_status.assert_called_once_with(AUDIT_ID, AuditStatus.CONTROLLING)
    (event,) = repositories.add_repair_event.call_args_list
    assert event.args[1] is Result.RESTARTED
    assert event.args[4] == "control"


@pytest.mark.parametrize("step", ["ads_google", "ads_meta", "map_listings", "listings"])
async def test_the_new_paid_steps_need_a_change_like_the_other_paid_steps(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate, step: str
) -> None:
    with pytest.raises(RadarError, match="--change"):
        await rerun.rerun_step(AUDIT_ID, step, " ", open_gate, repositories, collector_settings)


async def test_map_listings_is_the_listings_step(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _credit(monkeypatch, "1")
    seen: list[frozenset[str] | None] = []

    async def collect(audit_id: object, *args: object, **kwargs: object) -> None:
        only = kwargs.get("only")
        seen.append(only if isinstance(only, frozenset) else None)
        steps = kwargs["steps"]
        assert isinstance(steps, StepStore)
        if kwargs.get("probe"):
            steps.keep_probe("listings", None)
        else:
            steps.keep("listings", [], [], None)

    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collect)

    result = await rerun.rerun_step(
        AUDIT_ID, "map_listings", "Konto aufgeladen", open_gate, repositories, collector_settings
    )

    assert result.measured is True
    assert seen == [frozenset({"listings"}), frozenset({"listings"})]


async def test_the_unknown_step_message_lists_the_control(
    repositories: MagicMock, collector_settings: Settings, open_gate: SourceGate
) -> None:
    with pytest.raises(RadarError, match="ads_google.*control"):
        await rerun.rerun_step(AUDIT_ID, "fax", "x", open_gate, repositories, collector_settings)


@pytest.mark.parametrize(("attempts", "refused"), [(3, False), (4, True), (30, True)])
async def test_paid_retries_stop_after_the_cap(
    repositories: MagicMock,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
    refused: bool,
) -> None:
    _credit(monkeypatch, "1")
    store = StepStore(AUDIT_ID, repositories)
    for _ in range(attempts):
        store.count_attempt("serps")
    collector = Collector(None, None)
    monkeypatch.setattr("radarauditor.collectors.run.collect_audit", collector)

    result = await rerun.rerun_step(
        AUDIT_ID, "serps", "Standort korrigiert", open_gate, repositories, collector_settings
    )

    assert (collector.runs == []) is refused
    assert (result.measured is False) is refused
    if refused:
        assert "Obergrenze" in result.message
