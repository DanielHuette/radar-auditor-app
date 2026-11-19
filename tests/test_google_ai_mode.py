import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import google_ai_mode, paid_rankings
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings

pytestmark = pytest.mark.usefixtures("fast_retries")

BASE = paid_rankings.API_BASE_URL
POST_URL = f"{BASE}{google_ai_mode.POST_PATH}"
READY_URL = f"{BASE}{google_ai_mode.READY_PATH}"


def _result_url(task_id: str) -> str:
    return f"{BASE}{google_ai_mode.RESULT_PATH}/{task_id}"


class Spend:
    def __init__(self) -> None:
        self.booked: list[tuple[str, int]] = []

    def month_source_cost(self, source: str) -> Decimal:
        return Decimal(0)

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        return cost

    def add_audit_cost(
        self,
        audit_id: UUID,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.booked.append((service, calls))


@pytest.fixture
def client(collector_settings: Settings, open_gate: SourceGate) -> paid_rankings.DataForSeoClient:
    settings = collector_settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("secret")}
    )
    return paid_rankings.DataForSeoClient(settings, open_gate)


async def _no_sleep(seconds: float) -> None:
    return None


def _created(*ids: str, cost: float = 0.0012) -> httpx.Response:
    tasks = [
        {"id": task_id, "status_code": 20100, "status_message": "Task Created.", "cost": cost}
        for task_id in ids
    ]
    return httpx.Response(
        200,
        json={
            "status_code": 20000,
            "status_message": "Ok.",
            "cost": cost * len(ids),
            "tasks": tasks,
        },
    )


def _ready(*ids: str) -> httpx.Response:
    result = [{"id": task_id} for task_id in ids]
    body = {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [{"status_code": 20000, "result": result}],
    }
    return httpx.Response(200, json=body)


def _overview(markdown: str, urls: list[str], element_text: str = "") -> httpx.Response:
    item = {
        "type": "ai_overview",
        "markdown": markdown,
        "items": [
            {"type": "ai_overview_element", "text": element_text, "references": [{"url": urls[0]}]}
        ]
        if urls
        else [],
        "references": [{"url": url} for url in urls],
    }
    body = {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": [item]}]}],
    }
    return httpx.Response(200, json=body)


@respx.mock
async def test_ask_ai_mode_posts_german_desktop_tasks_and_reads_the_answers(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = respx.post(POST_URL).mock(return_value=_created("a", "b"))
    respx.get(READY_URL).mock(return_value=_ready("a", "b"))
    respx.get(_result_url("a")).mock(
        return_value=_overview("**Galerie Nord** ist gut.", ["https://nord.de"])
    )
    respx.get(_result_url("b")).mock(return_value=_overview("Zweite Antwort", []))

    batch = await google_ai_mode.ask_ai_mode(
        client, ["Frage eins", "Frage zwei"], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    tasks = httpx_json(post)
    assert [task["keyword"] for task in tasks] == ["Frage eins", "Frage zwei"]
    assert tasks[0]["location_code"] == 2276
    assert tasks[0]["language_code"] == "de"
    assert tasks[0]["device"] == "desktop"
    assert batch.failed == {}
    assert batch.answers[0] == google_ai_mode.AiModeAnswer(
        "**Galerie Nord** ist gut.", ["https://nord.de"]
    )
    assert batch.answers[1].text == "Zweite Antwort"


def httpx_json(route: respx.Route) -> list[dict[str, object]]:
    body: list[dict[str, object]] = json.loads(route.calls[0].request.content)
    return body


@respx.mock
async def test_ask_ai_mode_books_the_reported_cost_once_per_batch(
    collector_settings: Settings,
) -> None:
    spend = Spend()
    gate = SourceGate(SourcesSettings.model_validate({}), lambda source, cap: True, spend, uuid4())
    settings = collector_settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("secret")}
    )
    client = paid_rankings.DataForSeoClient(settings, gate)
    respx.post(POST_URL).mock(return_value=_created("a", "b"))
    respx.get(READY_URL).mock(return_value=_ready("a", "b"))
    respx.get(_result_url("a")).mock(return_value=_overview("x", []))
    respx.get(_result_url("b")).mock(return_value=_overview("y", []))

    await google_ai_mode.ask_ai_mode(
        client, ["a", "b"], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    assert spend.booked == [(google_ai_mode.POST_PATH, 2)]


@respx.mock
async def test_ask_ai_mode_polls_until_every_task_is_ready(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(return_value=_created("a", "b"))
    ready = respx.get(READY_URL).mock(side_effect=[_ready(), _ready("a"), _ready("a", "b")])
    respx.get(_result_url("a")).mock(return_value=_overview("x", []))
    respx.get(_result_url("b")).mock(return_value=_overview("y", []))

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a", "b"], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    assert ready.call_count == 3
    assert sorted(batch.answers) == [0, 1]


@respx.mock
async def test_ask_ai_mode_records_a_task_without_ai_answer_as_failed(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(return_value=_created("a"))
    respx.get(READY_URL).mock(return_value=_ready("a"))
    empty = httpx.Response(
        200,
        json={
            "status_code": 20000,
            "tasks": [{"status_code": 20000, "result": [{"items": []}]}],
        },
    )
    respx.get(_result_url("a")).mock(return_value=empty)

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a"], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    assert batch.answers == {}
    assert batch.failed == {0: google_ai_mode.NO_ANSWER_REASON}


@respx.mock
async def test_ask_ai_mode_falls_back_to_element_text_without_markdown(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(return_value=_created("a"))
    respx.get(READY_URL).mock(return_value=_ready("a"))
    respx.get(_result_url("a")).mock(return_value=_overview("", ["https://x.de"], "Nur Text"))

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a"], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    assert batch.answers[0].text == "Nur Text"
    assert batch.answers[0].citations == ["https://x.de"]


@respx.mock
async def test_ask_ai_mode_fails_the_tasks_that_were_not_created_with_the_reason(
    client: paid_rankings.DataForSeoClient,
) -> None:
    body = {
        "status_code": 20000,
        "cost": 0.0012,
        "tasks": [
            {"id": "a", "status_code": 20100, "status_message": "Task Created.", "cost": 0.0012},
            {"status_code": 40501, "status_message": "Invalid Field: 'keyword'.", "cost": 0},
        ],
    }
    respx.post(POST_URL).mock(return_value=httpx.Response(200, json=body))
    respx.get(READY_URL).mock(return_value=_ready("a"))
    respx.get(_result_url("a")).mock(return_value=_overview("x", []))

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a", ""], timeout_seconds=60, poll_seconds=1, sleep=_no_sleep
    )

    assert sorted(batch.answers) == [0]
    assert batch.failed == {1: "DataForSEO status 40501: Invalid Field: 'keyword'."}


@respx.mock
async def test_ask_ai_mode_fails_what_is_not_ready_within_the_timeout(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(return_value=_created("a", "b"))
    respx.get(READY_URL).mock(return_value=_ready("a"))
    respx.get(_result_url("a")).mock(return_value=_overview("x", []))
    ticks = iter([0.0, 1.0, 2.0, 100.0])

    batch = await google_ai_mode.ask_ai_mode(
        client,
        ["a", "b"],
        timeout_seconds=10,
        poll_seconds=1,
        sleep=_no_sleep,
        clock=lambda: next(ticks),
    )

    assert sorted(batch.answers) == [0]
    assert batch.failed == {1: "no result within 10 s"}


@respx.mock
async def test_ask_ai_mode_fails_everything_when_the_post_is_rejected(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(
        return_value=httpx.Response(
            200, json={"status_code": 40100, "status_message": "Unauthorized", "tasks": []}
        )
    )

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a", "b"], timeout_seconds=10, poll_seconds=1, sleep=_no_sleep
    )

    assert batch.answers == {}
    assert batch.failed == {
        0: "DataForSEO status 40100: Unauthorized",
        1: "DataForSEO status 40100: Unauthorized",
    }


@respx.mock
async def test_ask_ai_mode_fails_the_task_whose_result_cannot_be_read(
    client: paid_rankings.DataForSeoClient,
) -> None:
    respx.post(POST_URL).mock(return_value=_created("a", "b"))
    respx.get(READY_URL).mock(return_value=_ready("a", "b"))
    respx.get(_result_url("a")).mock(return_value=_overview("x", []))
    respx.get(_result_url("b")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [{"status_code": 40400, "status_message": "Task Not Found."}],
            },
        )
    )

    batch = await google_ai_mode.ask_ai_mode(
        client, ["a", "b"], timeout_seconds=10, poll_seconds=1, sleep=_no_sleep
    )

    assert sorted(batch.answers) == [0]
    assert batch.failed == {1: "DataForSEO status 40400: Task Not Found."}


@respx.mock
async def test_ask_ai_mode_posts_in_batches_of_one_hundred(
    client: paid_rankings.DataForSeoClient,
) -> None:
    post = respx.post(POST_URL).mock(
        side_effect=lambda request: _created(*["t"] * len(json.loads(request.content)))
    )
    respx.get(READY_URL).mock(return_value=_ready())

    await google_ai_mode.ask_ai_mode(
        client,
        [str(number) for number in range(101)],
        timeout_seconds=0,
        poll_seconds=1,
        sleep=_no_sleep,
    )

    assert post.call_count == 2


async def test_ask_ai_mode_fails_the_tasks_beyond_the_daily_cap(
    collector_settings: Settings,
) -> None:
    sources = SourcesSettings.model_validate(
        {Source.DATAFORSEO.value: {"enabled": True, "daily_cap": 1}}
    )
    counted: list[str] = []

    def consume(source: str, cap: int) -> bool:
        counted.append(source)
        return len(counted) <= cap

    gate = SourceGate(sources, consume)
    settings = collector_settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("secret")}
    )
    client = paid_rankings.DataForSeoClient(settings, gate)
    with respx.mock:
        respx.post(POST_URL).mock(return_value=_created("a"))
        respx.get(READY_URL).mock(return_value=_ready("a"))
        respx.get(_result_url("a")).mock(return_value=_overview("x", []))

        batch = await google_ai_mode.ask_ai_mode(
            client, ["a", "b"], timeout_seconds=10, poll_seconds=1, sleep=_no_sleep
        )

    assert sorted(batch.answers) == [0]
    assert batch.failed == {1: paid_rankings.CAP_REACHED}
