from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
from postgrest import APIError
from tenacity import wait_none
from test_repositories import AUDIT_ID, FakeClient

from radarauditor.config import Settings
from radarauditor.db import repositories as repositories_module
from radarauditor.db.models import AuditStatus, CheckOutcome, CheckResult, Verdict
from radarauditor.db.repositories import Repositories
from radarauditor.errors import StorageError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClient]:
    fake = FakeClient()
    monkeypatch.setattr(repositories_module, "create_client", MagicMock(return_value=fake))
    monkeypatch.setattr(repositories_module._execute_with_retry.retry, "wait", wait_none())
    yield fake


@pytest.fixture
def repositories(client: FakeClient, settings: Settings) -> Repositories:
    return Repositories(settings)


def _filters(client: FakeClient) -> list[tuple[Any, ...]]:
    return [call[1] for call in client.queries[0].called("eq")]


def test_a_failed_control_attempt_is_stored_with_its_error(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.save_control_attempt(AUDIT_ID, "f1", 2, "model did not answer")

    query = client.queries[0]
    assert query.target == "check_results"
    assert query.called("update") == [
        (
            "update",
            ({"control_attempt": 2, "control_last_error": "model did not answer"},),
            {},
        )
    ]
    assert _filters(client) == [("audit_id", str(AUDIT_ID)), ("check_id", "f1")]


def test_the_three_verdicts_close_the_control_of_a_check(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.save_check_verdicts(
        AUDIT_ID,
        "f2",
        collected=Verdict(ok=True, reason=None, evidence="protocol of the place step"),
        data=Verdict(ok=False, reason="category missing", evidence=None),
        evaluation=Verdict(ok=True, reason=None, evidence="threshold 0.5"),
    )

    query = client.queries[0]
    assert query.called("update") == [
        (
            "update",
            (
                {
                    "control_state": "done",
                    "control_last_error": None,
                    "verdict_collected_ok": True,
                    "verdict_collected_reason": None,
                    "verdict_collected_evidence": "protocol of the place step",
                    "verdict_data_ok": False,
                    "verdict_data_reason": "category missing",
                    "verdict_data_evidence": None,
                    "verdict_evaluation_ok": True,
                    "verdict_evaluation_reason": None,
                    "verdict_evaluation_evidence": "threshold 0.5",
                },
            ),
            {},
        )
    ]
    assert _filters(client) == [("audit_id", str(AUDIT_ID)), ("check_id", "f2")]


def test_a_failing_database_is_reported_for_the_check(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [httpx.ConnectError("down")] * 3

    with pytest.raises(StorageError, match="save control attempt of check f1"):
        repositories.save_control_attempt(AUDIT_ID, "f1", 1, None)


def test_a_check_result_starts_with_an_open_control(
    sample_check_results: list[CheckResult],
) -> None:
    result = sample_check_results[0]

    assert result.control_state.value == "open"
    assert result.control_attempt == 0
    assert result.control_last_error is None
    assert result.provider is None


def test_a_not_applicable_outcome_is_stored_as_its_word(
    sample_check_results: list[CheckResult],
) -> None:
    result = sample_check_results[0].model_copy(update={"outcome": CheckOutcome.NOT_APPLICABLE})

    assert result.model_dump(mode="json")["outcome"] == "not_applicable"


def test_a_crossed_first_verdict_leaves_the_other_two_columns_empty(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.save_check_verdicts(
        AUDIT_ID, "f2", Verdict(ok=False, reason="no answer", evidence=None), None, None
    )

    (update,) = client.queries[0].called("update")
    values = update[1][0]
    assert values["control_state"] == "done"
    assert values["verdict_collected_ok"] is False
    assert values["verdict_collected_reason"] == "no answer"
    for name in ("data", "evaluation"):
        assert [values[f"verdict_{name}_{part}"] for part in ("ok", "reason", "evidence")] == [
            None,
            None,
            None,
        ]


def test_reopening_the_control_resets_state_attempts_and_all_verdicts(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.reopen_control(AUDIT_ID)

    query = client.queries[0]
    assert query.target == "check_results"
    (update,) = query.called("update")
    values = update[1][0]
    assert values["control_state"] == "open"
    assert values["control_attempt"] == 0
    assert values["control_last_error"] is None
    verdict_columns = {key: value for key, value in values.items() if key.startswith("verdict_")}
    assert len(verdict_columns) == 9
    assert set(verdict_columns.values()) == {None}
    assert _filters(client) == [("audit_id", str(AUDIT_ID))]


@pytest.mark.parametrize(("total", "stored"), [(Decimal("61.5"), "61.5"), (None, None)])
def test_the_total_score_can_be_set_and_cleared(
    repositories: Repositories, client: FakeClient, total: Decimal | None, stored: str | None
) -> None:
    client.responses = [[]]

    repositories.set_total_score(AUDIT_ID, total)

    assert client.queries[0].called("update") == [("update", ({"total_score": stored},), {})]
    assert _filters(client) == [("id", str(AUDIT_ID))]


def test_audits_in_a_state_are_listed_oldest_first(
    repositories: Repositories, client: FakeClient
) -> None:
    first, second = UUID(int=1), UUID(int=2)
    client.responses = [[{"id": str(first)}, {"id": str(second)}]]

    found = repositories.audit_ids_with_status(AuditStatus.CONTROLLING)

    assert found == [first, second]
    assert _filters(client) == [("status", "controlling")]
    assert client.queries[0].called("order") == [("order", ("requested_at",), {})]


def _probe_responses(
    failing: dict[int, str], old_areas: list[dict[str, Any]] | None = None
) -> list[Any]:
    responses: list[Any] = []
    for index in range(3):
        code = failing.get(index)
        responses.append([] if code is None else APIError({"message": "missing", "code": code}))
    responses.append(old_areas if old_areas is not None else [])
    return responses


def test_no_missing_migration_is_reported_when_every_probe_succeeds(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = _probe_responses({})

    assert repositories.missing_migrations() == []
    assert [query.target for query in client.queries] == [
        "check_results",
        "audit_events",
        "audits",
        "products",
    ]
    assert client.schemas == ["crm"]


@pytest.mark.parametrize("code", ["42703", "22P02", "PGRST204"])
def test_a_missing_column_or_status_value_names_its_migration_once(
    repositories: Repositories, client: FakeClient, code: str
) -> None:
    client.responses = _probe_responses({0: code, 1: code})

    assert repositories.missing_migrations() == ["023"]


def test_a_missing_status_value_names_migration_024(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = _probe_responses({2: "22P02"})

    assert repositories.missing_migrations() == ["024"]
    assert _filters_of(client.queries[2]) == [("status", "controlling")]


def test_another_database_error_is_a_storage_error_not_a_missing_migration(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = _probe_responses({0: "42501"})

    with pytest.raises(StorageError, match="migration 023"):
        repositories.missing_migrations()


def test_old_area_letters_in_the_product_list_name_migration_030(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = _probe_responses({}, old_areas=[{"id": "p1"}])

    assert repositories.missing_migrations() == ["030"]
    overlaps = client.queries[3].called("overlaps")
    assert overlaps == [("overlaps", ("radar_areas", list("abcdefghijklmno")), {})]


def test_a_refused_verdict_without_the_025_rule_names_the_migration(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [APIError({"message": "violates check constraint", "code": "23514"})]

    with pytest.raises(StorageError, match="Datenbank-Änderung 025 fehlt"):
        repositories.save_check_verdicts(
            AUDIT_ID, "f1", Verdict(ok=False, reason="x", evidence=None), None, None
        )


def test_another_error_when_saving_verdicts_stays_unchanged(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [APIError({"message": "nope", "code": "42501"})]

    with pytest.raises(StorageError) as raised:
        repositories.save_check_verdicts(
            AUDIT_ID, "f1", Verdict(ok=False, reason="x", evidence=None), None, None
        )

    assert "025" not in str(raised.value)


def _filters_of(query: Any) -> list[tuple[Any, ...]]:
    return [call[1] for call in query.called("eq")]
