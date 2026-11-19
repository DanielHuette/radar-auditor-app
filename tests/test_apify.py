import json
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import apify
from radarauditor.errors import CollectionError, TransientCollectionError

ACTOR = "apify~instagram-profile-scraper"
START_URL = f"https://api.apify.com/v2/acts/{ACTOR}/runs"
RUN_URL = "https://api.apify.com/v2/actor-runs/run-1"
ITEMS_URL = "https://api.apify.com/v2/datasets/data-1/items"
TOKEN = SecretStr("apify-secret-token")


def _run(status: str = "SUCCEEDED", **fields: object) -> httpx.Response:
    data = {"id": "run-1", "status": status, "defaultDatasetId": "data-1", "usageTotalUsd": 0.0045}
    return httpx.Response(201, json={"data": {**data, **fields}})


def _read(status: str = "SUCCEEDED", **fields: object) -> httpx.Response:
    return httpx.Response(200, json={"data": _run(status, **fields).json()["data"]})


async def _call(
    payload: dict[str, object] | None = None,
    timeout: int = apify.RUN_TIMEOUT_SECONDS,
    on_cost: apify.CostSink | None = None,
) -> list[dict[str, object]]:
    async with httpx.AsyncClient() as client:
        return await apify.run_actor(
            client, TOKEN, ACTOR, payload or {"usernames": ["a"]}, timeout, on_cost
        )


def _finished(items: list[dict[str, object]], **fields: object) -> respx.Route:
    respx.get(RUN_URL).mock(return_value=_read(**fields))
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=items))
    return respx.post(START_URL).mock(return_value=_run(**fields))


@respx.mock
async def test_the_run_is_started_with_input_token_and_timeout_and_its_items_are_returned() -> None:
    route = _finished([{"username": "a"}])

    items = await _call({"usernames": ["a"]})

    assert items == [{"username": "a"}]
    request = route.calls.last.request
    assert request.url.params["timeout"] == "60"
    assert request.url.params["waitForFinish"] == "60"
    assert request.headers["Authorization"] == "Bearer apify-secret-token"
    assert json.loads(request.content) == {"usernames": ["a"]}


@respx.mock
async def test_a_run_that_found_nothing_gives_an_empty_list() -> None:
    _finished([])

    assert await _call() == []


@respx.mock
async def test_the_run_timeout_is_passed_on_and_the_request_waits_for_a_minute_at_most() -> None:
    route = _finished([], status="SUCCEEDED")

    await _call(timeout=120)

    request = route.calls.last.request
    assert request.url.params["timeout"] == "120"
    assert request.url.params["waitForFinish"] == "60"
    assert request.extensions["timeout"]["read"] == 75


@respx.mock
async def test_a_short_run_timeout_is_also_the_wait() -> None:
    route = _finished([])

    await _call(timeout=20)

    assert route.calls.last.request.url.params["waitForFinish"] == "20"


@respx.mock
async def test_a_run_still_running_after_the_first_wait_is_waited_for() -> None:
    respx.post(START_URL).mock(return_value=_run("RUNNING"))
    reads = respx.get(RUN_URL).mock(side_effect=[_read("RUNNING"), _read("SUCCEEDED")])
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[{"a": 1}]))

    assert await _call() == [{"a": 1}]
    assert reads.call_count == 2
    assert reads.calls[0].request.url.params["waitForFinish"] == "60"


@respx.mock
async def test_a_run_that_never_finishes_is_given_up_as_temporary() -> None:
    respx.post(START_URL).mock(return_value=_run("RUNNING"))
    respx.get(RUN_URL).mock(return_value=_read("RUNNING"))

    with pytest.raises(TransientCollectionError, match="did not finish in 60 s"):
        await _call()


@respx.mock
async def test_a_run_that_timed_out_is_temporary() -> None:
    _finished([], status="TIMED-OUT")

    with pytest.raises(TransientCollectionError, match="did not finish"):
        await _call()


@respx.mock
@pytest.mark.parametrize("status", ["FAILED", "ABORTED", "ABORTING"])
async def test_a_run_that_did_not_succeed_is_an_error_with_its_status(status: str) -> None:
    _finished([], status=status)

    with pytest.raises(CollectionError, match=f"ended with status {status}") as caught:
        await _call()

    assert not isinstance(caught.value, TransientCollectionError)


@respx.mock
async def test_the_cost_is_reported_once_it_reads_the_same_twice() -> None:
    respx.post(START_URL).mock(return_value=_run(usageTotalUsd=0.00005))
    reads = respx.get(RUN_URL).mock(
        side_effect=[
            _read(usageTotalUsd=0.00005),
            _read(usageTotalUsd=0.0045),
            _read(usageTotalUsd=0.00455),
            _read(usageTotalUsd=0.00455),
        ]
    )
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))
    costs: list[Decimal] = []

    await _call(on_cost=costs.append)

    assert costs == [Decimal("0.00455")]
    assert reads.call_count == 4


@respx.mock
async def test_the_cost_is_not_read_without_a_listener() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))
    reads = respx.get(RUN_URL).mock(return_value=_read())

    await _call()

    assert reads.call_count == 0


@respx.mock
async def test_a_cost_that_never_settles_is_an_error() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(RUN_URL).mock(
        side_effect=[_read(usageTotalUsd=number) for number in range(apify.COST_READS)]
    )
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(CollectionError, match="did not settle"):
        await _call(on_cost=lambda cost: None)


@respx.mock
@pytest.mark.parametrize("cost", [None, True, "many", [1]])
async def test_a_run_without_a_usable_cost_is_an_error(cost: object) -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(RUN_URL).mock(return_value=_read(usageTotalUsd=cost))
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(CollectionError, match="cost"):
        await _call(on_cost=lambda cost: None)


@respx.mock
async def test_a_cost_given_as_text_is_read_exactly() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(RUN_URL).mock(return_value=_read(usageTotalUsd="0.0045"))
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=[]))
    costs: list[Decimal] = []

    await _call(on_cost=costs.append)

    assert costs == [Decimal("0.0045")]


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_no_second_paid_run_is_started_after_a_temporary_error(status: int) -> None:
    route = respx.post(START_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(TransientCollectionError, match=f"HTTP {status}"):
        await _call()

    assert route.call_count == 1


@respx.mock
async def test_no_second_paid_run_is_started_after_a_timeout() -> None:
    route = respx.post(START_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(TransientCollectionError, match="unreachable: ReadTimeout"):
        await _call()

    assert route.call_count == 1


@respx.mock
async def test_a_failing_read_of_the_items_is_temporary_when_the_server_fails() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(TransientCollectionError, match="HTTP 503"):
        await _call()


@respx.mock
async def test_an_unreachable_dataset_is_temporary() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(ITEMS_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(TransientCollectionError, match="unreachable: ConnectError"):
        await _call()


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 408])
async def test_client_errors_are_not_retried(status: int) -> None:
    route = respx.post(START_URL).mock(
        return_value=httpx.Response(status, json={"error": {"message": "Monthly usage hard limit"}})
    )

    with pytest.raises(CollectionError, match=f"HTTP {status}: Monthly usage hard limit") as caught:
        await _call()

    assert not isinstance(caught.value, TransientCollectionError)
    assert route.call_count == 1


@respx.mock
async def test_the_token_never_appears_in_an_error() -> None:
    respx.post(START_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad"}}))

    with pytest.raises(CollectionError) as caught:
        await _call()

    assert "apify-secret-token" not in str(caught.value)


@respx.mock
async def test_an_answer_that_is_not_json_is_rejected() -> None:
    respx.post(START_URL).mock(return_value=httpx.Response(201, text="<html>"))

    with pytest.raises(CollectionError, match="not JSON"):
        await _call()


@respx.mock
@pytest.mark.parametrize("body", [["run"], {"error": "x"}, {"data": "run"}])
async def test_an_answer_without_a_run_is_rejected(body: object) -> None:
    respx.post(START_URL).mock(return_value=httpx.Response(201, json=body))

    with pytest.raises(CollectionError, match="did not answer with a run"):
        await _call()


@respx.mock
@pytest.mark.parametrize("fields", [{"id": None}, {"status": None}])
async def test_a_run_without_id_or_status_is_rejected(fields: dict[str, object]) -> None:
    respx.post(START_URL).mock(return_value=_run(**fields))

    with pytest.raises(CollectionError, match="without id or status"):
        await _call()


@respx.mock
async def test_a_run_without_a_dataset_is_rejected() -> None:
    respx.post(START_URL).mock(return_value=_run(defaultDatasetId=None))

    with pytest.raises(CollectionError, match="has no dataset"):
        await _call()


@respx.mock
async def test_items_that_are_not_json_are_rejected() -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, text="<html>"))

    with pytest.raises(CollectionError, match="not JSON"):
        await _call()


@respx.mock
@pytest.mark.parametrize("body", [{"error": "x"}, ["text"], [{"a": 1}, 3]])
async def test_items_that_are_not_a_list_of_items_are_rejected(body: object) -> None:
    respx.post(START_URL).mock(return_value=_run())
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(CollectionError, match="list of items"):
        await _call()
