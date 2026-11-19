import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from radarauditor.config import Settings
from radarauditor.errors import ClaudeCodeError, ClaudeCodeFailure

logger = logging.getLogger(__name__)

UsageSink = Callable[[int, int], None]
USAGE_INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
USAGE_OUTPUT_FIELDS = ("output_tokens",)
_usage_sink: ContextVar[UsageSink | None] = ContextVar("claude_usage_sink", default=None)


@contextmanager
def counting_usage(sink: UsageSink | None) -> Iterator[None]:
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


def _tokens(usage: object, fields: Sequence[str]) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(value for field in fields if isinstance(value := usage.get(field), int))


def _report_usage(result: Mapping[str, object]) -> None:
    sink = _usage_sink.get()
    if sink is None:
        return
    usage = result.get("usage")
    sink(_tokens(usage, USAGE_INPUT_FIELDS), _tokens(usage, USAGE_OUTPUT_FIELDS))


PROMPT_TEMPLATE = "/auswerten {folder}"
TOOLS = "Read,Glob,Grep,Write"
READ_TOOLS = ("Read", "Glob", "Grep")
OUTPUT_FILES = ("findings.json", "texte.json", "urteile.json")
WEB_TOOLS = ("WebSearch", "WebFetch")
LEAN_START = (
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--disable-slash-commands",
)
PROJECT_ONLY = ("--setting-sources", "project", "--strict-mcp-config")
NO_PROJECT_INSTRUCTIONS = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
MESSAGE_LIMIT = 300
WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):/(.*)$")
LOGIN_PROBLEM = re.compile(
    r"not logged in|/login|invalid api key|authentication|oauth token", re.IGNORECASE
)
USAGE_LIMIT = re.compile(
    r"usage limit|hit your (?:\w+ )?limit|usage credits are required", re.IGNORECASE
)
PING_PROMPT = "Antworte nur mit OK."
PING_MODEL = "haiku"
PING_TIMEOUT_SECONDS = 60
FAILURE_REASONS = {
    ClaudeCodeFailure.NOT_INSTALLED: "Claude Code ist nicht installiert",
    ClaudeCodeFailure.NOT_LOGGED_IN: "Claude Code ist nicht angemeldet",
    ClaudeCodeFailure.USAGE_LIMIT: "Das Kontingent des Claude-Abos ist aufgebraucht",
    ClaudeCodeFailure.TIMEOUT: "Claude Code hat nicht rechtzeitig geantwortet",
    ClaudeCodeFailure.FAILED: "Claude Code hat mit einem Fehler geantwortet",
}


def failure_reason(failure: ClaudeCodeFailure) -> str:
    return FAILURE_REASONS[failure]


def absolute_rule_path(path: Path) -> str:
    posix = path.as_posix()
    drive = WINDOWS_DRIVE.match(posix)
    if drive is not None:
        return f"//{drive.group(1).lower()}/{drive.group(2)}"
    return f"/{posix}"


def claude_command(
    executable: str,
    folder: Path,
    settings: Settings,
    *,
    prompt_template: str = PROMPT_TEMPLATE,
    output_files: Sequence[str] = OUTPUT_FILES,
) -> list[str]:
    writable = [f"Edit({absolute_rule_path(folder / name)})" for name in output_files]
    allowed = [*READ_TOOLS, *writable]
    command = [
        executable,
        "-p",
        prompt_template.format(folder=folder.as_posix()),
        "--output-format",
        "json",
        "--permission-mode",
        settings.analysis_permission_mode,
        "--no-session-persistence",
        *PROJECT_ONLY,
        "--tools",
        TOOLS,
    ]
    if settings.analysis_model:
        command += ["--model", settings.analysis_model]
    return [*command, "--allowedTools", *allowed, "--add-dir", str(folder)]


def run_claude_code(
    folder: Path,
    settings: Settings,
    *,
    prompt_template: str = PROMPT_TEMPLATE,
    output_files: Sequence[str] = OUTPUT_FILES,
) -> None:
    executable = resolve_executable(settings)
    command = claude_command(
        executable,
        folder.resolve(),
        settings,
        prompt_template=prompt_template,
        output_files=output_files,
    )
    timeout = settings.analysis_timeout_seconds
    logger.info("Running Claude Code on %s (timeout %d s)", folder.name, timeout)
    result = run_headless(
        command, settings.analysis_project_dir, timeout, environment=NO_PROJECT_INSTRUCTIONS
    )
    logger.info("Claude Code finished: %s", _short(result))


def toolless_command(executable: str, prompt: str, model: str) -> list[str]:
    return [
        executable,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        *LEAN_START,
        "--tools",
        "",
        "--model",
        model,
    ]


def ask_without_tools(
    prompt: str, settings: Settings, model: str, timeout: int, material: str | None = None
) -> str:
    command = toolless_command(resolve_executable(settings), prompt, model)
    with tempfile.TemporaryDirectory() as scratch:
        return run_headless(command, Path(scratch), timeout, material=material)


def ping_claude_code(settings: Settings) -> None:
    ask_without_tools(PING_PROMPT, settings, PING_MODEL, PING_TIMEOUT_SECONDS)


def resolve_executable(settings: Settings) -> str:
    executable = shutil.which(settings.analysis_claude_command)
    if executable is None:
        raise ClaudeCodeError(
            f"executable {settings.analysis_claude_command!r} not found",
            ClaudeCodeFailure.NOT_INSTALLED,
        )
    return executable


def assistant_command(executable: str, question: str, model: str | None) -> list[str]:
    command = [
        executable,
        "-p",
        question,
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        *LEAN_START,
        "--tools",
        ",".join(WEB_TOOLS),
    ]
    if model:
        command += ["--model", model]
    return [*command, "--allowedTools", *WEB_TOOLS]


def run_headless(
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    environment: Mapping[str, str] | None = None,
    material: str | None = None,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(environment or {})},
            input=material,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeError(
            f"no result within {timeout} seconds", ClaudeCodeFailure.TIMEOUT
        ) from exc
    except OSError as exc:
        raise ClaudeCodeError(f"cannot start {command[0]}: {exc}") from exc
    return check_result(completed.returncode, completed.stdout, completed.stderr)


def check_result(returncode: int, stdout: str, stderr: str) -> str:
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        detail = _short(stderr or stdout) or "no output"
        raise ClaudeCodeError(
            f"exit code {returncode}, no JSON result: {detail}", _failure_of(detail)
        )
    _report_usage(result)
    if returncode != 0 or result.get("is_error") is True or result.get("subtype") != "success":
        detail = _short(str(result.get("result") or result.get("subtype") or stderr))
        raise ClaudeCodeError(f"exit code {returncode}: {detail}", _failure_of(detail))
    return str(result.get("result") or "")


def _failure_of(detail: str) -> ClaudeCodeFailure:
    if USAGE_LIMIT.search(detail):
        return ClaudeCodeFailure.USAGE_LIMIT
    if LOGIN_PROBLEM.search(detail):
        return ClaudeCodeFailure.NOT_LOGGED_IN
    return ClaudeCodeFailure.FAILED


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MESSAGE_LIMIT else f"{flat[:MESSAGE_LIMIT]}..."
