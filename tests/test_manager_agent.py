import asyncio
import json
import os
import signal
import sys
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from radarauditor.config import Settings
from radarauditor.errors import ClaudeCodeError, ClaudeCodeFailure
from radarauditor.jobs import manager_agent

AUDIT = UUID("00000000-0000-4000-8000-0000000000a1")
SUCCESS = json.dumps({"subtype": "success", "is_error": False, "result": "ok 93.5"})


class FakeProcess:
    def __init__(
        self, stdout: str = SUCCESS, stderr: str = "", code: int | None = 0, hang: bool = False
    ) -> None:
        self.pid = 4242
        self.returncode = code
        self.killed = False
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(60)
        return self._stdout.encode(), self._stderr.encode()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


class Spawner:
    def __init__(self, process: FakeProcess | OSError) -> None:
        self.process = process
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.args = args
        self.kwargs = kwargs
        if isinstance(self.process, OSError):
            raise self.process
        return self.process


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    pids: list[int] = []
    monkeypatch.setattr(manager_agent, "_stop", lambda process: pids.append(process.pid))
    return pids


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> Spawner:
    spawner = Spawner(FakeProcess())
    monkeypatch.setattr(manager_agent, "resolve_executable", lambda settings: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    return spawner


def test_the_command_runs_the_audit_command_headless_without_asking_for_permissions(
    settings: Settings,
) -> None:
    command = manager_agent.manager_command("/usr/bin/claude", AUDIT, settings)

    assert command[:3] == ["/usr/bin/claude", "-p", f"/audit-begleiten {AUDIT}"]
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in command
    assert command[command.index("--setting-sources") + 1] == "project"
    assert command[command.index("--tools") + 1] == "Bash,Read"


def test_the_command_allows_only_the_audit_commands_and_the_log_folder(
    settings: Settings,
) -> None:
    command = manager_agent.manager_command("claude", AUDIT, settings)
    allowed = command[command.index("--allowedTools") + 1 :]

    assert allowed == manager_agent.allowed_tools(settings)
    assert [rule for rule in allowed if rule.startswith("Bash(")] == [
        f"Bash(radarauditor audit {name}:*)"
        for name in ("run-jobs", "status", "events", "inspect", "note", "conclude")
    ]
    reads = [rule for rule in allowed if rule.startswith("Read(")]
    assert len(reads) == 1
    assert reads[0].endswith("/manager/**)")
    assert len(allowed) == 7


def test_the_command_takes_the_model_only_when_one_is_set(settings: Settings) -> None:
    plain = manager_agent.manager_command("claude", AUDIT, settings)
    chosen = manager_agent.manager_command(
        "claude", AUDIT, settings.model_copy(update={"audit_manager_model": "opus"})
    )

    assert "--model" not in plain
    assert chosen[chosen.index("--model") + 1] == "opus"


def test_the_log_of_an_audit_lies_in_the_manager_folder_of_the_work_directory(
    settings: Settings,
) -> None:
    path = manager_agent.manager_log_path(settings, AUDIT)

    assert path == settings.radar_work_dir / "manager" / f"{AUDIT}.log"


async def test_a_run_starts_the_command_in_the_project_with_long_bash_limits(
    settings: Settings, spawn: Spawner, tmp_path: Path
) -> None:
    configured = settings.model_copy(
        update={"analysis_project_dir": tmp_path, "audit_manager_timeout_seconds": 7200}
    )

    await manager_agent.run_manager_agent(AUDIT, configured)

    assert spawn.args == tuple(manager_agent.manager_command("/usr/bin/claude", AUDIT, configured))
    assert spawn.kwargs["cwd"] == tmp_path
    assert spawn.kwargs["start_new_session"] is True
    environment = spawn.kwargs["env"]
    assert environment["BASH_DEFAULT_TIMEOUT_MS"] == "7200000"
    assert environment["BASH_MAX_TIMEOUT_MS"] == "7200000"
    assert environment["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert environment["PATH"] == os.environ["PATH"]


async def test_a_run_ends_without_error_when_claude_reports_success(
    settings: Settings, spawn: Spawner
) -> None:
    await manager_agent.run_manager_agent(AUDIT, settings)


async def test_a_run_reports_a_failure_of_claude(settings: Settings, spawn: Spawner) -> None:
    spawn.process = FakeProcess(
        stdout=json.dumps({"subtype": "error", "is_error": True, "result": "API Error: 529"}),
        code=1,
    )

    with pytest.raises(ClaudeCodeError, match="529"):
        await manager_agent.run_manager_agent(AUDIT, settings)


async def test_a_run_reports_a_missing_login(settings: Settings, spawn: Spawner) -> None:
    spawn.process = FakeProcess(stdout="", stderr="Not logged in", code=1)

    with pytest.raises(ClaudeCodeError) as raised:
        await manager_agent.run_manager_agent(AUDIT, settings)

    assert raised.value.failure is ClaudeCodeFailure.NOT_LOGGED_IN


async def test_a_run_reports_an_exit_without_a_code(settings: Settings, spawn: Spawner) -> None:
    spawn.process = FakeProcess(stdout="", code=None)

    with pytest.raises(ClaudeCodeError, match="exit code -1"):
        await manager_agent.run_manager_agent(AUDIT, settings)


async def test_a_run_that_cannot_start_reports_it(settings: Settings, spawn: Spawner) -> None:
    spawn.process = OSError("exec format error")

    with pytest.raises(ClaudeCodeError, match="exec format error"):
        await manager_agent.run_manager_agent(AUDIT, settings)


async def test_a_run_without_claude_reports_that_it_is_not_installed(settings: Settings) -> None:
    with pytest.raises(ClaudeCodeError) as raised:
        await manager_agent.run_manager_agent(AUDIT, settings)

    assert raised.value.failure is ClaudeCodeFailure.NOT_INSTALLED


async def test_a_run_that_takes_too_long_is_stopped_with_everything_it_started(
    settings: Settings, spawn: Spawner, stopped: list[int]
) -> None:
    spawn.process = FakeProcess(hang=True)
    short = settings.model_copy(update={"audit_manager_timeout_seconds": 1})

    with pytest.raises(ClaudeCodeError) as raised:
        await manager_agent.run_manager_agent(AUDIT, short)

    assert raised.value.failure is ClaudeCodeFailure.TIMEOUT
    assert stopped == [4242]


async def test_a_cancelled_run_stops_the_process_and_stays_cancelled(
    settings: Settings, spawn: Spawner, stopped: list[int]
) -> None:
    spawn.process = FakeProcess(hang=True)
    task = asyncio.create_task(manager_agent.run_manager_agent(AUDIT, settings))
    await asyncio.sleep(0.05)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [4242]


def test_on_a_posix_host_the_whole_process_group_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    process = FakeProcess()

    manager_agent._stop(cast(Process, process))

    assert killed == [(4242, 9)]
    assert not process.killed


def test_a_process_that_is_already_gone_is_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "killpg", gone, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    manager_agent._stop(cast(Process, FakeProcess()))


def test_on_windows_only_the_process_itself_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    process = FakeProcess()

    manager_agent._stop(cast(Process, process))

    assert process.killed


def test_the_claude_login_is_handed_on_under_a_name_the_bash_tool_keeps() -> None:
    assert manager_agent.claude_login_environment({"CLAUDE_CODE_OAUTH_TOKEN": "secret"}) == {
        "RADAR_CLAUDE_OAUTH_TOKEN": "secret"
    }


@pytest.mark.parametrize("environment", [{}, {"CLAUDE_CODE_OAUTH_TOKEN": ""}])
def test_without_a_claude_login_nothing_is_handed_on(environment: dict[str, str]) -> None:
    assert manager_agent.claude_login_environment(environment) == {}


def test_restoring_the_login_sets_the_variable_claude_code_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("RADAR_CLAUDE_OAUTH_TOKEN", "secret")

    manager_agent.restore_claude_login()

    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "secret"


def test_restoring_the_login_keeps_a_variable_that_is_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "own")
    monkeypatch.setenv("RADAR_CLAUDE_OAUTH_TOKEN", "alias")

    manager_agent.restore_claude_login()

    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "own"


def test_restoring_the_login_without_an_alias_sets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("RADAR_CLAUDE_OAUTH_TOKEN", raising=False)

    manager_agent.restore_claude_login()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


async def test_a_run_gives_the_agent_the_login_under_the_alias(
    settings: Settings, spawn: Spawner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")

    await manager_agent.run_manager_agent(AUDIT, settings)

    assert spawn.kwargs["env"]["RADAR_CLAUDE_OAUTH_TOKEN"] == "secret"
