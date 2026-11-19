import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from realtime import RealtimePostgresChangesListenEvent, RealtimeSubscribeStates
from realtime._async.client import AsyncRealtimeClient
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from radarauditor.config import Settings

logger = logging.getLogger(__name__)

CHANNEL_NAME = "audit-manager-jobs"
JOBS_TABLE = "jobs"
QUEUED_FILTER = "status=eq.queued"
REALTIME_PATH = "/realtime/v1"
CHECK_SECONDS = 5.0
SUBSCRIBE_TIMEOUT_SECONDS = 15.0
FIRST_DELAY_SECONDS = 1.0
LONGEST_DELAY_SECONDS = 60.0
STABLE_SECONDS = 60.0


class SignalError(Exception):
    pass


class Subscription(Protocol):
    def alive(self) -> bool: ...

    async def close(self) -> None: ...


type Opener = Callable[[Callable[[], None]], Awaitable[Subscription]]


class RealtimeSubscription:
    def __init__(self, client: AsyncRealtimeClient, channel_joined: Callable[[], bool]) -> None:
        self._client = client
        self._channel_joined = channel_joined

    def alive(self) -> bool:
        connection = self._client._ws_connection
        listener = self._client._listen_task
        return (
            connection is not None
            and connection.state is State.OPEN
            and listener is not None
            and not listener.done()
            and self._channel_joined()
        )

    async def close(self) -> None:
        await self._client.close()


async def open_jobs_subscription(
    settings: Settings, on_change: Callable[[], None]
) -> RealtimeSubscription:
    base = str(settings.supabase_url).rstrip("/")
    client = AsyncRealtimeClient(
        f"{base}{REALTIME_PATH}",
        token=settings.supabase_service_key.get_secret_value(),
        auto_reconnect=False,
        max_retries=1,
    )
    await client.connect()
    channel = client.channel(CHANNEL_NAME)
    for event in (
        RealtimePostgresChangesListenEvent.Insert,
        RealtimePostgresChangesListenEvent.Update,
    ):
        channel.on_postgres_changes(
            event,
            lambda _payload: on_change(),
            table=JOBS_TABLE,
            schema="public",
            filter=QUEUED_FILTER,
        )
    answered: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def on_state(state: RealtimeSubscribeStates, error: Exception | None) -> None:
        if answered.done():
            return
        if state is RealtimeSubscribeStates.SUBSCRIBED:
            answered.set_result(None)
        else:
            answered.set_exception(SignalError(f"subscription answered {state.value}: {error}"))

    try:
        await channel.subscribe(on_state)
        await asyncio.wait_for(answered, SUBSCRIBE_TIMEOUT_SECONDS)
    except BaseException:
        await client.close()
        raise
    return RealtimeSubscription(client, lambda: channel.is_joined)


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _close_quietly(subscription: Subscription) -> None:
    try:
        await subscription.close()
    except (OSError, WebSocketException) as error:
        logger.warning("Closing the Realtime connection failed: %s", error)


async def watch(
    open_subscription: Opener,
    on_change: Callable[[], None],
    on_ready: Callable[[], None],
    stop: asyncio.Event,
    *,
    check_seconds: float = CHECK_SECONDS,
    first_delay: float = FIRST_DELAY_SECONDS,
    longest_delay: float = LONGEST_DELAY_SECONDS,
    stable_seconds: float = STABLE_SECONDS,
) -> None:
    loop = asyncio.get_running_loop()
    delay = first_delay
    while not stop.is_set():
        try:
            subscription = await open_subscription(on_change)
        except (OSError, TimeoutError, WebSocketException, SignalError) as error:
            logger.warning("Realtime connection failed, next attempt in %.0f s: %s", delay, error)
            await _pause(stop, delay)
            delay = min(delay * 2, longest_delay)
            continue
        opened = loop.time()
        logger.info("Listening for new jobs through Realtime")
        on_ready()
        while not stop.is_set() and subscription.alive():
            await _pause(stop, check_seconds)
        await _close_quietly(subscription)
        if stop.is_set():
            return
        if loop.time() - opened >= stable_seconds:
            delay = first_delay
        logger.warning("Realtime connection lost, reconnecting in %.0f s", delay)
        await _pause(stop, delay)
        delay = min(delay * 2, longest_delay)
