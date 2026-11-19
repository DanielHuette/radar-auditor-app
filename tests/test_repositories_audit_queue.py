from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
from tenacity import wait_none
from test_repositories import AUDIT_ID, FakeClient, job_row

from radarauditor.config import Settings
from radarauditor.db import repositories as repositories_module
from radarauditor.db.models import JobKind, KeywordScope
from radarauditor.db.repositories import Repositories
from radarauditor.errors import StorageError
from radarauditor.feedback.events import Result

CUSTOMER_ID = UUID("00000000-0000-4000-8000-00000000c001")
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
KINDS = (JobKind.COLLECT, JobKind.SCORE)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClient]:
    fake = FakeClient()
    monkeypatch.setattr(repositories_module, "create_client", MagicMock(return_value=fake))
    monkeypatch.setattr(repositories_module._execute_with_retry.retry, "wait", wait_none())
    yield fake


@pytest.fixture
def repositories(client: FakeClient, settings: Settings) -> Repositories:
    return Repositories(settings)


def _args(client: FakeClient, method: str, index: int = 0) -> tuple[Any, ...]:
    return client.queries[0].called(method)[index][1]


def test_due_audit_ids_asks_for_queued_due_jobs_of_the_kinds(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.due_audit_ids(KINDS, NOW)

    assert _args(client, "eq") == ("status", "queued")
    assert _args(client, "in_") == ("kind", ["collect", "score"])
    assert _args(client, "filter") == ("audit_id", "not.is", "null")
    assert _args(client, "or_") == (f"run_after.is.null,run_after.lte.{NOW.isoformat()}",)


def test_due_audit_ids_lists_every_audit_once_in_queue_order(
    repositories: Repositories, client: FakeClient
) -> None:
    other = UUID("00000000-0000-4000-8000-000000000009")
    client.responses = [
        [
            {"audit_id": str(AUDIT_ID)},
            {"audit_id": str(other)},
            {"audit_id": str(AUDIT_ID)},
        ]
    ]

    assert repositories.due_audit_ids(KINDS, NOW) == [AUDIT_ID, other]


def test_due_audit_ids_is_empty_without_due_jobs(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    assert repositories.due_audit_ids(KINDS, NOW) == []


def test_next_job_due_returns_the_earliest_future_time(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[{"run_after": "2026-11-13T09:15:00+00:00"}]]

    assert repositories.next_job_due(KINDS, NOW) == datetime(2026, 11, 13, 9, 15, tzinfo=UTC)
    assert _args(client, "gt") == ("run_after", NOW.isoformat())
    assert _args(client, "order") == ("run_after",)
    assert _args(client, "limit") == (1,)


def test_next_job_due_is_none_without_deferred_jobs(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    assert repositories.next_job_due(KINDS, NOW) is None


def test_claim_audit_job_returns_the_claimed_job(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[job_row(kind="collect")]]

    job = repositories.claim_audit_job(AUDIT_ID, KINDS)

    assert job is not None
    assert job.kind is JobKind.COLLECT
    assert client.queries[0].calls[0] == (
        "rpc",
        ("claim_audit_job", {"p_audit_id": str(AUDIT_ID), "p_kinds": ["collect", "score"]}),
        {},
    )


def test_claim_audit_job_is_none_when_nothing_is_due(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    assert repositories.claim_audit_job(AUDIT_ID, KINDS) is None


def test_claim_audit_job_is_never_retried(repositories: Repositories, client: FakeClient) -> None:
    client.responses = [httpx.ConnectError("down"), [job_row()]]

    with pytest.raises(StorageError):
        repositories.claim_audit_job(AUDIT_ID, KINDS)

    assert client.executions == 1


def test_jobs_of_audit_lists_the_jobs_oldest_first(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[job_row(kind="collect"), job_row(kind="score")]]

    jobs = repositories.jobs_of_audit(AUDIT_ID)

    assert [job.kind for job in jobs] == [JobKind.COLLECT, JobKind.SCORE]
    assert _args(client, "eq") == ("audit_id", str(AUDIT_ID))
    assert _args(client, "order") == ("created_at",)


def test_requeue_running_jobs_of_audit_touches_only_its_running_jobs(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[job_row(), job_row()]]

    count = repositories.requeue_running_jobs_of_audit(AUDIT_ID, "cut off")

    query = client.queries[0]
    values = query.called("update")[0][1][0]
    assert values["status"] == "queued"
    assert values["error"] == "cut off"
    assert [call[1] for call in query.called("eq")] == [
        ("status", "running"),
        ("audit_id", str(AUDIT_ID)),
    ]
    assert count == 2


def test_reset_failed_jobs_starts_their_attempts_again(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[job_row(kind="score")]]

    jobs = repositories.reset_failed_jobs(AUDIT_ID, KINDS)

    query = client.queries[0]
    values = query.called("update")[0][1][0]
    assert values["status"] == "queued"
    assert values["attempts"] == 0
    assert values["error"] is None
    assert values["run_after"] is None
    assert [call[1] for call in query.called("eq")] == [
        ("audit_id", str(AUDIT_ID)),
        ("status", "failed"),
    ]
    assert query.called("in_")[0][1] == ("kind", ["collect", "score"])
    assert [job.kind for job in jobs] == [JobKind.SCORE]


def test_audit_event_rows_come_in_time_order(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[{"label": "a"}, {"label": "b"}]]

    rows = repositories.audit_event_rows(AUDIT_ID)

    assert [row["label"] for row in rows] == ["a", "b"]
    assert _args(client, "order") == ("occurred_at",)


def test_add_manager_event_inserts_a_manager_row(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[{}]]

    repositories.add_manager_event(AUDIT_ID, Result.FAILED, "Audit-Manager", "no login")

    row = client.queries[0].called("insert")[0][1][0]
    assert row == {
        "audit_id": str(AUDIT_ID),
        "kind": "manager",
        "phase": "auswertung",
        "result": "fehlgeschlagen",
        "label": "Audit-Manager",
        "reason": "no login",
    }


def test_add_keywords_stores_the_scope(repositories: Repositories, client: FakeClient) -> None:
    client.responses = [[]]

    repositories.add_keywords(
        CUSTOMER_ID, ["kunst"], "M\u00fcnster", first_primary=False, scope=KeywordScope.NATIONWIDE
    )

    assert _args(client, "upsert")[0][0]["scope"] == "nationwide"


def test_add_keywords_stores_local_keywords_by_default(
    repositories: Repositories, client: FakeClient
) -> None:
    client.responses = [[]]

    repositories.add_keywords(CUSTOMER_ID, ["kunst"], "M\u00fcnster", first_primary=False)

    assert _args(client, "upsert")[0][0]["scope"] == "local"
