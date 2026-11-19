import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from realtime import RealtimePostgresChangesListenEvent, RealtimeSubscribeStates
from realtime._async.client import AsyncRealtimeClient
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from radarauditor.config import Settings
from radarauditor.jobs import job_signal
from radarauditor.jobs.job_signal import RealtimeSubscription, SignalError, watch

FAST = {"check_seconds": 0.005, "first_delay": 0.01, "longest_delay": 0.04, "stable_seconds": 60.0}


@dataclass
class FakeSubscription:
    lives: int = 1
    closed: bool = False
    close_error: Exception | None = None

    def alive(self) -> bool:
        self.lives -= 1
        return self.lives >= 0

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@dataclass
class Script:
    outcomes: list[FakeSubscription | Exception]
    stop: asyncio.Event
    opened: list[FakeSubscription] = field(default_factory=list)
    callbacks: list[Callable[[], None]] = field(default_factory=list)

    async def __call__(self, on_change: Callable[[], None]) -> FakeSubscription:
        self.callbacks.append(on_change)
        if not self.outcomes:
            self.stop.set()
            raise OSError("script is over")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.opened.append(outcome)
        return outcome


async def _run(script: Script, ready: list[str], **overrides: float) -> None:
    await asyncio.wait_for(
        watch(
            script,
            lambda: ready.append("change"),
            lambda: ready.append("ready"),
            script.stop,
            **{**FAST, **overrides},
        ),
        timeout=3,
    )


async def test_watch_reads_the_queue_once_per_connection() -> None:
    stop = asyncio.Event()
    script = Script([FakeSubscription(lives=1), FakeSubscription(lives=1)], stop)
    ready: list[str] = []

    await _run(script, ready)

    assert ready == ["ready", "ready"]
    assert all(subscription.closed for subscription in script.opened)


async def test_watch_reconnects_after_a_connection_that_ended_cleanly() -> None:
    stop = asyncio.Event()
    first = FakeSubscription(lives=0)
    second = FakeSubscription(lives=2)
    script = Script([first, second], stop)

    await _run(script, [])

    assert script.opened == [first, second]
    assert first.closed


async def test_watch_hands_the_change_callback_to_every_connection() -> None:
    stop = asyncio.Event()
    script = Script([FakeSubscription(lives=0), FakeSubscription(lives=0)], stop)
    ready: list[str] = []

    await _run(script, ready)

    for callback in script.callbacks[:2]:
        callback()
    assert ready.count("change") == 2


async def test_watch_retries_a_failed_connection_with_a_growing_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    script = Script(
        [OSError("a"), TimeoutError(), SignalError("c"), OSError("d"), OSError("e")], stop
    )
    delays: list[float] = []

    async def record(_: asyncio.Event, seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(job_signal, "_pause", record)

    await _run(script, [])

    assert delays[:5] == [0.01, 0.02, 0.04, 0.04, 0.04]


async def test_watch_reports_a_websocket_failure_and_goes_on() -> None:
    stop = asyncio.Event()
    refusal = ConnectionClosedError(None, None)
    script = Script([refusal, FakeSubscription(lives=1)], stop)
    ready: list[str] = []

    await _run(script, ready)

    assert ready == ["ready"]


async def test_watch_keeps_the_growing_delay_for_a_connection_that_does_not_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    script = Script([FakeSubscription(lives=0), FakeSubscription(lives=0)], stop)
    delays: list[float] = []

    async def record(_: asyncio.Event, seconds: float) -> None:
        if seconds != FAST["check_seconds"]:
            delays.append(seconds)

    monkeypatch.setattr(job_signal, "_pause", record)

    await _run(script, [])

    assert delays[:2] == [0.01, 0.02]


async def test_watch_starts_again_with_the_first_delay_after_a_long_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    script = Script([OSError("a"), FakeSubscription(lives=0), FakeSubscription(lives=0)], stop)
    delays: list[float] = []

    async def record(_: asyncio.Event, seconds: float) -> None:
        if seconds != FAST["check_seconds"]:
            delays.append(seconds)

    monkeypatch.setattr(job_signal, "_pause", record)

    await _run(script, [], stable_seconds=0.0)

    assert delays[:3] == [0.01, 0.01, 0.01]


async def test_watch_closes_the_connection_and_returns_when_stopped() -> None:
    stop = asyncio.Event()
    subscription = FakeSubscription(lives=10**6)
    script = Script([subscription], stop)
    ready: list[str] = []
    task = asyncio.create_task(
        watch(script, lambda: None, lambda: ready.append("ready"), stop, **FAST)
    )
    while not ready:
        await asyncio.sleep(0.005)

    stop.set()
    await asyncio.wait_for(task, timeout=3)

    assert subscription.closed


async def test_watch_survives_a_failing_close() -> None:
    stop = asyncio.Event()
    broken = FakeSubscription(lives=0, close_error=OSError("already gone"))
    script = Script([broken, FakeSubscription(lives=0)], stop)
    ready: list[str] = []

    await _run(script, ready)

    assert ready == ["ready", "ready"]


def _connection(state: State) -> SimpleNamespace:
    return SimpleNamespace(state=state)


def _client(*, state: State | None = State.OPEN, listening: bool = True) -> SimpleNamespace:
    listener = SimpleNamespace(done=lambda: not listening)
    connection = None if state is None else _connection(state)
    return SimpleNamespace(_ws_connection=connection, _listen_task=listener)


def _subscription(client: SimpleNamespace, joined: bool = True) -> RealtimeSubscription:
    return RealtimeSubscription(cast(AsyncRealtimeClient, client), lambda: joined)


def test_a_healthy_subscription_is_alive() -> None:
    assert _subscription(_client()).alive()


@pytest.mark.parametrize(
    "client",
    [
        _client(state=None),
        _client(state=State.CLOSED),
        _client(state=State.CLOSING),
        _client(listening=False),
    ],
    ids=["no connection", "closed", "closing", "listener ended"],
)
def test_a_subscription_with_a_dead_part_is_not_alive(client: SimpleNamespace) -> None:
    assert not _subscription(client).alive()


def test_a_subscription_whose_channel_left_is_not_alive() -> None:
    assert not _subscription(_client(), joined=False).alive()


def test_a_subscription_without_listener_task_is_not_alive() -> None:
    client = _client()
    client._listen_task = None
    assert not _subscription(client).alive()


class FakeChannel:
    def __init__(self, answer: RealtimeSubscribeStates | None, error: Exception | None) -> None:
        self.answer = answer
        self.error = error
        self.bindings: list[dict[str, Any]] = []
        self.callbacks: list[Callable[[Any], None]] = []
        self.is_joined = True

    def on_postgres_changes(
        self,
        event: RealtimePostgresChangesListenEvent,
        callback: Callable[[Any], None],
        table: str,
        schema: str,
        filter: str,
    ) -> "FakeChannel":
        self.bindings.append({"event": event, "table": table, "schema": schema, "filter": filter})
        self.callbacks.append(callback)
        return self

    async def subscribe(
        self, callback: Callable[[RealtimeSubscribeStates, Exception | None], None]
    ) -> "FakeChannel":
        if self.answer is not None:
            callback(self.answer, self.error)
        return self


class FakeClient:
    instances: ClassVar[list["FakeClient"]] = []
    channel_answer: ClassVar[RealtimeSubscribeStates | None] = RealtimeSubscribeStates.SUBSCRIBED
    channel_error: ClassVar[Exception | None] = None

    def __init__(self, url: str, token: str, auto_reconnect: bool, max_retries: int) -> None:
        self.url = url
        self.token = token
        self.auto_reconnect = auto_reconnect
        self.max_retries = max_retries
        self.connected = False
        self.closed = False
        self.opened_channel: FakeChannel | None = None
        self._ws_connection = _connection(State.OPEN)
        self._listen_task = SimpleNamespace(done=lambda: False)
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    def channel(self, name: str) -> FakeChannel:
        self.opened_channel = FakeChannel(type(self).channel_answer, type(self).channel_error)
        return self.opened_channel

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances = []
    FakeClient.channel_answer = RealtimeSubscribeStates.SUBSCRIBED
    FakeClient.channel_error = None
    monkeypatch.setattr(job_signal, "AsyncRealtimeClient", FakeClient)
    return FakeClient


async def test_open_subscribes_to_new_and_requeued_jobs_with_the_service_key(
    fake_client: type[FakeClient], settings: Settings
) -> None:
    changes: list[str] = []

    subscription = await job_signal.open_jobs_subscription(settings, lambda: changes.append("x"))

    client = fake_client.instances[0]
    assert client.url == f"{str(settings.supabase_url).rstrip('/')}/realtime/v1"
    assert client.token == settings.supabase_service_key.get_secret_value()
    assert client.auto_reconnect is False
    assert client.max_retries == 1
    assert client.connected
    channel = client.opened_channel
    assert channel is not None
    assert channel.bindings == [
        {
            "event": RealtimePostgresChangesListenEvent.Insert,
            "table": "jobs",
            "schema": "public",
            "filter": "status=eq.queued",
        },
        {
            "event": RealtimePostgresChangesListenEvent.Update,
            "table": "jobs",
            "schema": "public",
            "filter": "status=eq.queued",
        },
    ]
    for callback in channel.callbacks:
        callback({"data": {}})
    assert changes == ["x", "x"]
    assert subscription.alive()


@pytest.mark.parametrize(
    "answer",
    [
        RealtimeSubscribeStates.CHANNEL_ERROR,
        RealtimeSubscribeStates.TIMED_OUT,
        RealtimeSubscribeStates.CLOSED,
    ],
)
async def test_open_refuses_a_subscription_the_server_did_not_accept(
    fake_client: type[FakeClient], settings: Settings, answer: RealtimeSubscribeStates
) -> None:
    fake_client.channel_answer = answer
    fake_client.channel_error = RuntimeError("denied")

    with pytest.raises(SignalError, match=answer.value):
        await job_signal.open_jobs_subscription(settings, lambda: None)

    assert fake_client.instances[0].closed


async def test_open_gives_up_when_the_server_never_answers(
    fake_client: type[FakeClient], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client.channel_answer = None
    monkeypatch.setattr(job_signal, "SUBSCRIBE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        await job_signal.open_jobs_subscription(settings, lambda: None)

    assert fake_client.instances[0].closed
