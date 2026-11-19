import asyncio
import math
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from radarauditor.collectors import raw_answers
from radarauditor.collectors.transport import raise_for_status
from radarauditor.errors import CollectionError, TransientCollectionError

START_URL = "https://api.apify.com/v2/acts/{actor}/runs"
RUN_URL = "https://api.apify.com/v2/actor-runs/{run_id}"
ITEMS_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"
RUN_TIMEOUT_SECONDS = 60
CLIENT_TIMEOUT_MARGIN_SECONDS = 15
WAIT_SECONDS = 60
COST_SETTLE_SECONDS = 1.5
COST_READS = 6
RUNNING = frozenset({"READY", "RUNNING"})
TIMED_OUT = "TIMED-OUT"
SUCCEEDED = "SUCCEEDED"

CostSink = Callable[[Decimal], None]
ServiceCostSink = Callable[[str, Decimal], None]


async def run_actor(
    client: httpx.AsyncClient,
    token: SecretStr,
    actor: str,
    payload: Mapping[str, object],
    run_timeout_seconds: int = RUN_TIMEOUT_SECONDS,
    on_cost: CostSink | None = None,
    step: str | None = None,
) -> list[dict[str, Any]]:
    source = f"Apify {actor}"
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
    first_wait = min(run_timeout_seconds, WAIT_SECONDS)
    run = await _call(
        client,
        "POST",
        START_URL.format(actor=actor),
        source,
        headers=headers,
        params={"timeout": run_timeout_seconds, "waitForFinish": first_wait},
        json=payload,
        timeout=first_wait + CLIENT_TIMEOUT_MARGIN_SECONDS,
    )
    waits = math.ceil((run_timeout_seconds + CLIENT_TIMEOUT_MARGIN_SECONDS) / WAIT_SECONDS)
    for _ in range(waits):
        if _status(run, source) not in RUNNING:
            break
        run = await _read_run(client, headers, run, source, WAIT_SECONDS)
    status = _status(run, source)
    if status in RUNNING or status == TIMED_OUT:
        raise TransientCollectionError(f"{source} run did not finish in {run_timeout_seconds} s")
    if status != SUCCEEDED:
        raise CollectionError(f"{source} run ended with status {status}")
    raw_name = raw_answers.file_name(step or "apify", "apify", actor)
    items = await _items(client, headers, run, source, raw_name)
    if on_cost is not None:
        on_cost(await _settled_cost(client, headers, run, source))
    return items


async def _call(
    client: httpx.AsyncClient, method: str, url: str, source: str, **options: Any
) -> dict[str, Any]:
    try:
        response = await client.request(method, url, **options)
    except httpx.TransportError as error:
        raise TransientCollectionError(f"{source} unreachable: {type(error).__name__}") from error
    raise_for_status(response, source)
    try:
        body = response.json()
    except ValueError as error:
        raise CollectionError(f"{source} answered with text that is not JSON") from error
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise CollectionError(f"{source} did not answer with a run")
    return data


async def _read_run(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    run: Mapping[str, Any],
    source: str,
    wait_seconds: int,
) -> dict[str, Any]:
    return await _call(
        client,
        "GET",
        RUN_URL.format(run_id=run["id"]),
        source,
        headers=headers,
        params={"waitForFinish": wait_seconds} if wait_seconds else None,
        timeout=wait_seconds + CLIENT_TIMEOUT_MARGIN_SECONDS,
    )


def _status(run: Mapping[str, Any], source: str) -> str:
    status = run.get("status")
    if not isinstance(status, str) or not isinstance(run.get("id"), str):
        raise CollectionError(f"{source} answered with a run without id or status")
    return status


async def _items(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    run: Mapping[str, Any],
    source: str,
    raw_name: str,
) -> list[dict[str, Any]]:
    dataset = run.get("defaultDatasetId")
    if not isinstance(dataset, str):
        raise CollectionError(f"{source} run has no dataset")
    try:
        response = await client.get(
            ITEMS_URL.format(dataset_id=dataset),
            headers=headers,
            params={"format": "json"},
            timeout=RUN_TIMEOUT_SECONDS + CLIENT_TIMEOUT_MARGIN_SECONDS,
        )
    except httpx.TransportError as error:
        raise TransientCollectionError(f"{source} unreachable: {type(error).__name__}") from error
    raise_for_status(response, source)
    raw_answers.keep(raw_name, response.content)
    try:
        items = response.json()
    except ValueError as error:
        raise CollectionError(f"{source} answered with text that is not JSON") from error
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise CollectionError(f"{source} did not answer with a list of items")
    return items


async def _settled_cost(
    client: httpx.AsyncClient, headers: Mapping[str, str], run: Mapping[str, Any], source: str
) -> Decimal:
    previous: Decimal | None = None
    for _ in range(COST_READS):
        await asyncio.sleep(COST_SETTLE_SECONDS)
        current = _cost_of(await _read_run(client, headers, run, source, 0), source)
        if current == previous:
            return current
        previous = current
    raise CollectionError(f"{source} run cost did not settle after {COST_READS} reads")


def _cost_of(run: Mapping[str, Any], source: str) -> Decimal:
    value = run.get("usageTotalUsd")
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise CollectionError(f"{source} run reports no cost")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise CollectionError(f"{source} run reports the cost {value!r}") from error
