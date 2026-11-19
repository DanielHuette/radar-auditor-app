import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from radarauditor.collectors.patterns import collapse_whitespace, host_of, site_key
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.serp import MAX_AI_MENTIONS, ai_overview_names
from radarauditor.collectors.snapshot import SerpResult
from radarauditor.collectors.transport import send
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import (
    CollectionError,
    MissingInputError,
    SourceDisabledError,
    TransientCollectionError,
)

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.dataforseo.com"
USER_DATA_PATH = "/v3/appendix/user_data"
ORGANIC_TASKS = "/v3/serp/google/organic"
TASK_POST = "/task_post"
TASKS_READY = "/tasks_ready"
TASK_GET = "/task_get"
TASK_GET_ADVANCED = "/task_get/advanced"
HIGH_PRIORITY = 2
RESULTS_PER_PAGE = 10
GOOGLE_PAGES = 3
VOLUME_PATH = "/v3/keywords_data/google_ads/search_volume/live"
REQUEST_TIMEOUT_SECONDS = 120
CONCURRENT_REQUESTS = 4
LANGUAGE_CODE = "de"
GERMANY_LOCATION_CODE = 2276
SEARCH_ENGINE_DOMAIN = "google.de"
ORGANIC_RADIUS = 200
GRID_SPACING_KM = 1.0
KM_PER_DEGREE_LATITUDE = 111.32
VOLUME_BATCH_SIZE = 1000
STATUS_OK = 20000
STATUS_TASK_CREATED = 20100
TASK_BATCH_SIZE = 100
FIRST_SERVER_STATUS = 50000
CONCURRENT_RESULT_READS = 4
CAP_REACHED = "daily cap of the dataforseo source reached"
NO_CREDIT_FOR_REPEAT = "not repeated, the DataForSEO credit does not cover it"
UNREADABLE_CREDIT = "not repeated, the DataForSEO credit could not be read"
BREADCRUMB_SEPARATOR = "›"
GOOGLE_REDIRECT_PATHS = frozenset({"/goto", "/url"})
REDIRECT_TARGET_PARAMETERS = ("url", "q")


class _Reference(BaseModel):
    source: str | None = None
    title: str | None = None
    domain: str | None = None


class _SerpItem(BaseModel):
    type: str
    rank_group: int | None = None
    domain: str | None = None
    url: str | None = None
    breadcrumb: str | None = None
    title: str | None = None
    website_name: str | None = None
    text: str | None = None
    markdown: str | None = None
    items: list["_SerpItem | str"] | None = None
    references: list[_Reference] | None = None

    def elements(self) -> list["_SerpItem"]:
        return [element for element in self.items or [] if isinstance(element, _SerpItem)]

    def site(self) -> str | None:
        redirected = bool(self.url) and urlsplit(self.url or "").path in GOOGLE_REDIRECT_PATHS
        target = _redirect_target(self.url) if redirected else None
        if target is not None:
            return target
        if self.breadcrumb:
            shown = host_of(self.breadcrumb.split(BREADCRUMB_SEPARATOR)[0])
            if shown is not None:
                return shown
        if redirected:
            return host_of(self.website_name)
        return self.domain


def _redirect_target(url: str | None) -> str | None:
    parameters = parse_qs(urlsplit(url or "").query)
    for name in REDIRECT_TARGET_PARAMETERS:
        for value in parameters.get(name, []):
            if value.startswith(("http://", "https://")):
                return host_of(value)
    return None


class _SerpPayload(BaseModel):
    items: list[_SerpItem] | None = None


class _VolumePayload(BaseModel):
    keyword: str
    search_volume: int | None = None


class _Task(BaseModel):
    id: str | None = None
    status_code: int
    status_message: str = ""
    result: list[dict[str, Any]] | None = None


class _Money(BaseModel):
    balance: Decimal


class _UserData(BaseModel):
    money: _Money


class _Envelope(BaseModel):
    status_code: int
    status_message: str = ""
    cost: Decimal = Decimal(0)
    tasks: list[_Task] = []


@dataclass(frozen=True)
class TaskTicket:
    task_id: str | None
    reason: str | None = None


@dataclass(frozen=True)
class TaskBatch:
    results: dict[int, list[dict[str, Any]]]
    failed: dict[int, str]
    repeated: int = 0


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class PaidSearchBatch:
    results: list[SerpResult]
    failed: list[str]
    reason: str | None = None
    repeated: int = 0


def is_configured(settings: Settings) -> bool:
    return bool(settings.dataforseo_login) and settings.dataforseo_password is not None


class DataForSeoClient:
    def __init__(self, settings: Settings, gate: SourceGate) -> None:
        if not gate.is_enabled(Source.DATAFORSEO):
            raise SourceDisabledError(Source.DATAFORSEO.value, "switched off in the app settings")
        if not settings.dataforseo_login or settings.dataforseo_password is None:
            raise MissingInputError("DataForSEO credentials are missing")
        self._login = settings.dataforseo_login
        self._password: SecretStr = settings.dataforseo_password
        self._eur_per_usd = settings.dataforseo_eur_per_usd
        self._base_url = settings.dataforseo_url.rstrip("/")
        self._gate = gate
        self._unit_cost_usd: dict[str, Decimal] = {}
        self.timeout_seconds = settings.dataforseo_task_timeout_seconds
        self.poll_seconds = settings.dataforseo_poll_seconds
        self.sleep: Sleep = asyncio.sleep

    async def post(self, path: str, task: dict[str, Any]) -> list[dict[str, Any]]:
        self._gate.acquire(Source.DATAFORSEO)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await send(
                client,
                "POST",
                f"{self._base_url}{path}",
                source="DataForSEO",
                check=_check_envelope,
                auth=self._auth(),
                json=[task],
            )
        self._gate.keep_raw(_raw_name(path), response.content)
        envelope = _envelope(response)
        self._gate.record_cost(Source.DATAFORSEO, envelope.cost * self._eur_per_usd, service=path)
        return envelope.tasks[0].result or []

    async def post_tasks(self, path: str, tasks: list[dict[str, Any]]) -> list[TaskTicket]:
        tickets: list[TaskTicket] = []
        for start in range(0, len(tasks), TASK_BATCH_SIZE):
            batch = tasks[start : start + TASK_BATCH_SIZE]
            allowed = self._gate.acquire_up_to(Source.DATAFORSEO, len(batch))
            tickets.extend(await self._post_batch(path, batch[:allowed]))
            tickets.extend(TaskTicket(None, CAP_REACHED) for _ in batch[allowed:])
        return tickets

    async def ready_ids(self, path: str) -> set[str]:
        envelope = _envelope(await self._get(path))
        return {row["id"] for task in envelope.tasks for row in task.result or [] if "id" in row}

    async def task_result(self, path: str) -> list[dict[str, Any]]:
        response = await self._get(path)
        self._gate.keep_raw(_raw_name(path), response.content)
        return _envelope(response).tasks[0].result or []

    async def balance_usd(self) -> Decimal:
        return await read_balance(self._base_url, self._auth())

    def unit_cost_usd(self, path: str) -> Decimal | None:
        return self._unit_cost_usd.get(path)

    async def _post_batch(self, path: str, batch: list[dict[str, Any]]) -> list[TaskTicket]:
        if not batch:
            return []
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await send(
                client,
                "POST",
                f"{self._base_url}{path}",
                source="DataForSEO",
                check=_check_envelope_only,
                auth=self._auth(),
                json=batch,
            )
        envelope = _envelope(response)
        created = [task for task in envelope.tasks if task.status_code == STATUS_TASK_CREATED]
        if created:
            self._unit_cost_usd[path] = envelope.cost / len(created)
        self._gate.record_cost(
            Source.DATAFORSEO,
            envelope.cost * self._eur_per_usd,
            service=path,
            calls=len(created),
        )
        return [_ticket(task) for task in envelope.tasks]

    async def _get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            return await send(
                client,
                "GET",
                f"{self._base_url}{path}",
                source="DataForSEO",
                check=_check_envelope,
                auth=self._auth(),
            )

    def _auth(self) -> tuple[str, str]:
        return (self._login, self._password.get_secret_value())


async def read_balance(base_url: str, auth: tuple[str, str]) -> Decimal:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await send(
            client,
            "GET",
            f"{base_url}{USER_DATA_PATH}",
            source="DataForSEO",
            check=_check_envelope,
            auth=auth,
        )
    try:
        rows = _envelope(response).tasks[0].result or []
        return _UserData.model_validate(rows[0]).money.balance
    except (IndexError, ValidationError) as error:
        raise CollectionError("DataForSEO user data carry no balance") from error


def _raw_name(path: str) -> str:
    return path.removeprefix("/v3/").replace("/", "_") + ".json"


async def run_tasks(
    client: DataForSeoClient,
    base: str,
    tasks: list[dict[str, Any]],
    *,
    result_path: str = TASK_GET_ADVANCED,
    priority: int | None = HIGH_PRIORITY,
    repeat_failed: bool = True,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
    sleep: Sleep | None = None,
    clock: Clock = time.monotonic,
) -> TaskBatch:
    prioritised = [{**task, "priority": priority} if priority else task for task in tasks]
    timing = _Timing(
        timeout_seconds if timeout_seconds is not None else client.timeout_seconds,
        poll_seconds if poll_seconds is not None else client.poll_seconds,
        sleep or client.sleep,
        clock,
    )
    first = await _run_round(client, base, dict(enumerate(prioritised)), result_path, timing)
    if not first.failed or not repeat_failed:
        return first
    again = sorted(first.failed)
    refusal = await _repeat_refusal(client, f"{base}{TASK_POST}", len(again))
    if refusal is not None:
        failed = {index: f"{reason}; {refusal}" for index, reason in first.failed.items()}
        return TaskBatch(first.results, failed)
    logger.info("Repeating %d failed DataForSEO tasks of %s once", len(again), base)
    second = await _run_round(
        client, base, {index: prioritised[index] for index in again}, result_path, timing
    )
    return TaskBatch({**first.results, **second.results}, second.failed, repeated=len(again))


@dataclass(frozen=True)
class _Timing:
    timeout_seconds: float
    poll_seconds: float
    sleep: Sleep
    clock: Clock


async def _repeat_refusal(client: DataForSeoClient, post_path: str, count: int) -> str | None:
    try:
        balance = await client.balance_usd()
    except CollectionError:
        return UNREADABLE_CREDIT
    unit = client.unit_cost_usd(post_path) or Decimal(0)
    if balance <= 0 or balance < unit * count:
        return NO_CREDIT_FOR_REPEAT
    return None


async def _run_round(
    client: DataForSeoClient,
    base: str,
    tasks: dict[int, dict[str, Any]],
    result_path: str,
    timing: _Timing,
) -> TaskBatch:
    results: dict[int, list[dict[str, Any]]] = {}
    failed: dict[int, str] = {}
    pending = await _post_round(client, f"{base}{TASK_POST}", tasks, failed)
    deadline = timing.clock() + timing.timeout_seconds
    while pending and timing.clock() < deadline:
        await timing.sleep(timing.poll_seconds)
        try:
            ready = await client.ready_ids(f"{base}{TASKS_READY}")
        except CollectionError as error:
            failed.update(dict.fromkeys(pending.values(), f"results not readable: {error}"))
            return TaskBatch(results, failed)
        done = {key: pending.pop(key) for key in pending.keys() & ready}
        await _read_round(client, f"{base}{result_path}", done, results, failed)
    failed.update(
        dict.fromkeys(pending.values(), f"no result within {timing.timeout_seconds:.0f} s")
    )
    return TaskBatch(results, failed)


async def _post_round(
    client: DataForSeoClient,
    path: str,
    tasks: dict[int, dict[str, Any]],
    failed: dict[int, str],
) -> dict[str, int]:
    indexes = list(tasks)
    try:
        tickets = await client.post_tasks(path, [tasks[index] for index in indexes])
    except CollectionError as error:
        failed.update(dict.fromkeys(indexes, str(error)))
        return {}
    pending: dict[str, int] = {}
    for index, ticket in zip(indexes, tickets, strict=True):
        if ticket.task_id is None:
            failed[index] = ticket.reason or "task was not created"
        else:
            pending[ticket.task_id] = index
    return pending


async def _read_round(
    client: DataForSeoClient,
    path: str,
    ready: dict[str, int],
    results: dict[int, list[dict[str, Any]]],
    failed: dict[int, str],
) -> None:
    semaphore = asyncio.Semaphore(CONCURRENT_RESULT_READS)

    async def read(task_id: str, index: int) -> None:
        try:
            async with semaphore:
                results[index] = await client.task_result(f"{path}/{task_id}")
        except CollectionError as error:
            failed[index] = str(error)

    await asyncio.gather(*(read(task_id, index) for task_id, index in ready.items()))


def _ticket(task: _Task) -> TaskTicket:
    if task.status_code == STATUS_TASK_CREATED and task.id:
        return TaskTicket(task.id)
    return TaskTicket(None, f"DataForSEO status {task.status_code}: {task.status_message}")


def _check_envelope_only(response: httpx.Response) -> None:
    envelope = _envelope(response)
    if envelope.status_code >= FIRST_SERVER_STATUS:
        raise TransientCollectionError(
            f"DataForSEO status {envelope.status_code}: {envelope.status_message}"
        )
    if envelope.status_code != STATUS_OK:
        raise CollectionError(
            f"DataForSEO status {envelope.status_code}: {envelope.status_message}"
        )
    for task in envelope.tasks:
        if task.status_code >= FIRST_SERVER_STATUS:
            raise TransientCollectionError(
                f"DataForSEO status {task.status_code}: {task.status_message}"
            )


def _envelope(response: httpx.Response) -> _Envelope:
    try:
        return _Envelope.model_validate(response.json())
    except (ValueError, ValidationError) as error:
        raise CollectionError("DataForSEO answer is malformed") from error


def _check_envelope(response: httpx.Response) -> None:
    envelope = _envelope(response)
    statuses = [(envelope.status_code, envelope.status_message)]
    statuses.extend((task.status_code, task.status_message) for task in envelope.tasks)
    for code, message in statuses:
        if code >= FIRST_SERVER_STATUS:
            raise TransientCollectionError(f"DataForSEO status {code}: {message}")
        if code != STATUS_OK:
            raise CollectionError(f"DataForSEO status {code}: {message}")
    if not envelope.tasks:
        raise CollectionError("DataForSEO answer contains no task")


async def search_google(
    client: DataForSeoClient,
    phrases: list[str],
    latitude: float | None,
    longitude: float | None,
) -> PaidSearchBatch:
    task = {
        "se_domain": SEARCH_ENGINE_DOMAIN,
        "language_code": LANGUAGE_CODE,
        "depth": GOOGLE_PAGES * RESULTS_PER_PAGE,
        **_search_location(latitude, longitude),
    }
    return await _search(client, ORGANIC_TASKS, phrases, task, "google")


async def _search(
    client: DataForSeoClient,
    base: str,
    phrases: list[str],
    task: dict[str, object],
    engine: Literal["google", "bing"],
) -> PaidSearchBatch:
    batch = await run_tasks(client, base, [{**task, "keyword": phrase} for phrase in phrases])
    answers: list[SerpResult | CollectionError] = []
    for index, phrase in enumerate(phrases):
        if index in batch.failed:
            answers.append(CollectionError(batch.failed[index]))
            continue
        try:
            answer = _serp_from_items(phrase, _serp_items(batch.results[index]), engine)
            answers.append(_with_request_echo(answer, batch.results[index]))
        except CollectionError as error:
            answers.append(error)
    return _search_batch(phrases, answers, batch.repeated)


def _with_request_echo(answer: SerpResult, rows: list[dict[str, Any]]) -> SerpResult:
    echo = rows[0] if rows else {}
    location = echo.get("location_code")
    return answer.model_copy(
        update={
            "returned_keyword": echo.get("keyword")
            if isinstance(echo.get("keyword"), str)
            else None,
            "language_code": echo.get("language_code")
            if isinstance(echo.get("language_code"), str)
            else None,
            "location_code": location if isinstance(location, int) else None,
        }
    )


def _search_location(latitude: float | None, longitude: float | None) -> dict[str, object]:
    if latitude is not None and longitude is not None:
        return {"location_coordinate": f"{latitude:.7f},{longitude:.7f},{ORGANIC_RADIUS}"}
    return {"location_code": GERMANY_LOCATION_CODE}


def _search_batch(
    phrases: list[str], answers: list[SerpResult | CollectionError], repeated: int = 0
) -> PaidSearchBatch:
    results = [answer for answer in answers if isinstance(answer, SerpResult)]
    failures = [
        (phrase, answer)
        for phrase, answer in zip(phrases, answers, strict=True)
        if isinstance(answer, CollectionError)
    ]
    reason = str(failures[0][1]) if failures else None
    if failures:
        logger.warning("DataForSEO could not search %d of %d phrases", len(failures), len(phrases))
    return PaidSearchBatch(results, [phrase for phrase, _ in failures], reason, repeated)


def _serp_from_items(
    phrase: str, items: list[_SerpItem], engine: Literal["google", "bing"] = "google"
) -> SerpResult:
    ranked = sorted(items, key=lambda item: item.rank_group or 0)
    local_names = [item.title for item in ranked if item.type == "local_pack" and item.title]
    overviews = [item for item in items if item.type == "ai_overview"]
    return SerpResult(
        phrase=phrase,
        organic=_organic_positions(ranked),
        local_pack=list(enumerate(dict.fromkeys(local_names), start=1)),
        ai_overview_present=bool(overviews),
        ai_overview_mentions=_overview_mentions(overviews),
        people_also_ask=_people_also_ask(items),
        measured_at=datetime.now(UTC),
        ads=_advertisers(ranked),
        knowledge_panel=any(item.type == "knowledge_graph" for item in items),
        engine=engine,
        route="dataforseo",
    )


def _organic_positions(ranked: list[_SerpItem]) -> list[tuple[int, str]]:
    positions: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item in ranked:
        site = item.site() if item.type == "organic" and item.rank_group else None
        if site and item.rank_group and site_key(site) not in seen:
            seen.add(site_key(site))
            positions.append((item.rank_group, site_key(site)))
    return positions


def organic_pages(rows: list[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
    return [
        (site_key(site), item.url or "", item.title)
        for item in _serp_items(rows)
        if item.type == "organic" and (site := item.site())
    ]


def _people_also_ask(items: list[_SerpItem]) -> list[str]:
    questions = [
        collapse_whitespace(element.title)
        for item in items
        if item.type == "people_also_ask"
        for element in item.elements()
        if element.title
    ]
    return list(dict.fromkeys(questions))


def _advertisers(ranked: list[_SerpItem]) -> list[str]:
    advertisers = [
        site_key(site) if (site := item.site()) else item.title
        for item in ranked
        if item.type == "paid"
    ]
    return [advertiser for advertiser in dict.fromkeys(advertisers) if advertiser]


def _overview_mentions(overviews: list[_SerpItem]) -> list[str]:
    names: list[str] = []
    texts: list[str] = []
    for overview in overviews:
        for element in [overview, *overview.elements()]:
            names.extend(
                reference.source or reference.title or "" for reference in element.references or []
            )
            texts.append(element.markdown or element.text or "")
    names.extend(ai_overview_names(" ".join(texts)))
    unique = [name for name in dict.fromkeys(names) if name]
    return unique[:MAX_AI_MENTIONS]


def grid_key(index: int, grid_size: int) -> str:
    if not 0 <= index < grid_size * grid_size:
        raise ValueError(f"index {index} lies outside a {grid_size}x{grid_size} grid")
    row_from_south, column = divmod(index, grid_size)
    return f"r{grid_size - row_from_south}c{column + 1}"


def grid_points(latitude: float, longitude: float, grid_size: int) -> list[tuple[float, float]]:
    if grid_size < 1:
        raise ValueError(f"grid_size must be at least 1, got {grid_size}")
    latitude_step = GRID_SPACING_KM / KM_PER_DEGREE_LATITUDE
    longitude_step = GRID_SPACING_KM / (KM_PER_DEGREE_LATITUDE * math.cos(math.radians(latitude)))
    offsets = [index - (grid_size - 1) / 2 for index in range(grid_size)]
    return [
        (latitude + row * latitude_step, longitude + column * longitude_step)
        for row in offsets
        for column in offsets
    ]


async def monthly_volumes(client: DataForSeoClient, phrases: list[str]) -> dict[str, int]:
    by_lowercase = {phrase.lower(): phrase for phrase in phrases}
    volumes: dict[str, int] = {}
    for start in range(0, len(phrases), VOLUME_BATCH_SIZE):
        task = {
            "keywords": phrases[start : start + VOLUME_BATCH_SIZE],
            "location_code": GERMANY_LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
        }
        for row in await client.post(VOLUME_PATH, task):
            entry = _parse(_VolumePayload, row)
            phrase = by_lowercase.get(entry.keyword.lower())
            if phrase is not None and entry.search_volume is not None:
                volumes[phrase] = entry.search_volume
    return volumes


def _serp_items(result: list[dict[str, Any]]) -> list[_SerpItem]:
    items: list[_SerpItem] = []
    for row in result:
        items.extend(_parse(_SerpPayload, row).items or [])
    return items


def _parse[ModelT: BaseModel](model: type[ModelT], row: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(row)
    except ValidationError as error:
        raise CollectionError("DataForSEO result is malformed") from error
