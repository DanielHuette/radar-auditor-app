import asyncio
import contextlib
import logging
import os
import signal
import sys
from asyncio.subprocess import PIPE, Process
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from radarauditor.config import Settings
from radarauditor.errors import ClaudeCodeError, ClaudeCodeFailure
from radarauditor.jobs.claude_code import (
    NO_PROJECT_INSTRUCTIONS,
    PROJECT_ONLY,
    absolute_rule_path,
    check_result,
    resolve_executable,
)

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = "/audit-begleiten {audit_id}"
TOOLS = "Bash,Read"
AUDIT_COMMANDS = ("run-jobs", "status", "events", "inspect", "note", "conclude")
MANAGER_DIR = "manager"
CLAUDE_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_TOKEN_ALIAS = "RADAR_CLAUDE_OAUTH_TOKEN"


def manager_log_path(settings: Settings, audit_id: UUID) -> Path:
    return settings.radar_work_dir / MANAGER_DIR / f"{audit_id}.log"


def allowed_tools(settings: Settings) -> list[str]:
    commands = [f"Bash(radarauditor audit {name}:*)" for name in AUDIT_COMMANDS]
    log_dir = absolute_rule_path((settings.radar_work_dir / MANAGER_DIR).resolve())
    return [*commands, f"Read({log_dir}/**)"]


def manager_command(executable: str, audit_id: UUID, settings: Settings) -> list[str]:
    command = [
        executable,
        "-p",
        PROMPT_TEMPLATE.format(audit_id=audit_id),
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        *PROJECT_ONLY,
        "--tools",
        TOOLS,
    ]
    if settings.audit_manager_model:
        command += ["--model", settings.audit_manager_model]
    return [*command, "--allowedTools", *allowed_tools(settings)]


def claude_login_environment(environment: Mapping[str, str]) -> dict[str, str]:
    token = environment.get(CLAUDE_TOKEN_VARIABLE)
    return {CLAUDE_TOKEN_ALIAS: token} if token else {}


def restore_claude_login() -> None:
    token = os.environ.get(CLAUDE_TOKEN_ALIAS)
    if token and not os.environ.get(CLAUDE_TOKEN_VARIABLE):
        os.environ[CLAUDE_TOKEN_VARIABLE] = token


def _stop(process: Process) -> None:
    if sys.platform == "win32":
        process.kill()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def run_manager_agent(audit_id: UUID, settings: Settings) -> None:
    command = manager_command(resolve_executable(settings), audit_id, settings)
    result = await run_agent(command, settings, settings.audit_manager_timeout_seconds)
    logger.info("Audit manager of audit %s finished: %s", audit_id, result[:300])


async def run_agent(command: list[str], settings: Settings, timeout: int) -> str:
    bash_limit = str(timeout * 1000)
    environment = {
        **os.environ,
        **NO_PROJECT_INSTRUCTIONS,
        **claude_login_environment(os.environ),
        "BASH_DEFAULT_TIMEOUT_MS": bash_limit,
        "BASH_MAX_TIMEOUT_MS": bash_limit,
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=settings.analysis_project_dir,
            env=environment,
            stdout=PIPE,
            stderr=PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ClaudeCodeError(f"cannot start {command[0]}: {error}") from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
        _stop(process)
        await process.wait()
        raise ClaudeCodeError(
            f"no result within {timeout} seconds", ClaudeCodeFailure.TIMEOUT
        ) from error
    except asyncio.CancelledError:
        _stop(process)
        await process.wait()
        raise
    return check_result(
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
