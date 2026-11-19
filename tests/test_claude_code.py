import contextlib
import json
from pathlib import Path, PureWindowsPath

import pytest

from radarauditor.config import Settings
from radarauditor.errors import ClaudeCodeError, ClaudeCodeFailure
from radarauditor.jobs.claude_code import (
    absolute_rule_path,
    assistant_command,
    check_result,
    claude_command,
    counting_usage,
    failure_reason,
    ping_claude_code,
    run_claude_code,
    toolless_command,
)

SUCCESS = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "1"})


@pytest.fixture
def job_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "work" / "analyse" / "job"
    folder.mkdir(parents=True)
    audit = {"audit": {"id": "00000000-0000-4000-8000-000000000002"}}
    (folder / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
    return folder


def test_absolute_rule_path_on_posix() -> None:
    assert absolute_rule_path(Path("/srv/work/x/findings.json")) == "//srv/work/x/findings.json"


def test_absolute_rule_path_on_windows() -> None:
    path = PureWindowsPath(r"C:\KI\work\x\texte.json")

    assert absolute_rule_path(Path(path.as_posix())) == "//c/KI/work/x/texte.json"


def test_command_allows_reading_and_only_the_three_output_files(settings: Settings) -> None:
    folder = Path("/srv/work/analyse/job")

    command = claude_command("/usr/bin/claude", folder, settings)

    assert command[:3] == ["/usr/bin/claude", "-p", "/auswerten /srv/work/analyse/job"]
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep,Write"
    allowed = command[command.index("--allowedTools") + 1 : command.index("--add-dir")]
    assert allowed == [
        "Read",
        "Glob",
        "Grep",
        "Edit(//srv/work/analyse/job/findings.json)",
        "Edit(//srv/work/analyse/job/texte.json)",
        "Edit(//srv/work/analyse/job/urteile.json)",
    ]
    assert command[-2:] == ["--add-dir", str(folder)]
    assert "--model" not in command


def test_command_passes_model_when_set(settings: Settings) -> None:
    with_model = settings.model_copy(update={"analysis_model": "sonnet"})

    command = claude_command("claude", Path("/w"), with_model)

    assert command[command.index("--model") + 1] == "sonnet"


def test_check_result_accepts_success() -> None:
    assert check_result(0, SUCCESS, "") == "1"


def test_assistant_command_allows_only_the_web_tools() -> None:
    command = assistant_command("/usr/bin/claude", "Wer ist ein guter Dachdecker?", "haiku")

    assert command[:3] == ["/usr/bin/claude", "-p", "Wer ist ein guter Dachdecker?"]
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--model") + 1] == "haiku"
    assert command[-3:] == ["--allowedTools", "WebSearch", "WebFetch"]
    assert "--no-session-persistence" in command


def test_toolless_command_offers_no_tool_and_asks_for_nothing() -> None:
    command = toolless_command("/usr/bin/claude", "Ordne diese Mail ein.", "sonnet")

    assert command[:3] == ["/usr/bin/claude", "-p", "Ordne diese Mail ein."]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in command
    assert "--allowedTools" not in command


def test_toolless_command_loads_no_settings_servers_or_skills() -> None:
    command = toolless_command("/usr/bin/claude", "Frage", "sonnet")

    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--mcp-config" not in command
    assert "--disable-slash-commands" in command


def test_assistant_command_starts_lean() -> None:
    command = assistant_command("/usr/bin/claude", "Frage", "haiku")

    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--mcp-config" not in command
    assert "--disable-slash-commands" in command


def test_folder_command_loads_only_the_project_and_no_mcp_server(settings: Settings) -> None:
    command = claude_command("/usr/bin/claude", Path("/srv/work/analyse/job"), settings)

    assert command[command.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in command
    assert "--mcp-config" not in command
    assert "--disable-slash-commands" not in command


def test_assistant_command_without_model_uses_the_login_default() -> None:
    assert "--model" not in assistant_command("claude", "Frage", None)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "problem"),
    [
        (
            1,
            json.dumps({"subtype": "success", "is_error": True, "result": "Not logged in"}),
            "",
            "Not logged in",
        ),
        (0, json.dumps({"subtype": "error_max_turns", "is_error": False}), "", "error_max_turns"),
        (2, SUCCESS, "", "exit code 2"),
        (1, "", "command crashed", "no JSON result: command crashed"),
        (0, "[1, 2]", "", "no JSON result"),
    ],
)
def test_check_result_rejects_failures(
    returncode: int, stdout: str, stderr: str, problem: str
) -> None:
    with pytest.raises(ClaudeCodeError, match=problem):
        check_result(returncode, stdout, stderr)


def test_run_writes_files_through_the_cli(
    claude_settings: Settings, job_folder: Path, fake_claude: Path, tmp_path: Path
) -> None:
    run_claude_code(job_folder, claude_settings)

    assert (job_folder / "findings.json").exists() and (job_folder / "texte.json").exists()
    argv = json.loads(fake_claude.with_name("argv.json").read_text(encoding="utf-8"))
    assert argv[:2] == ["-p", f"/auswerten {job_folder.resolve().as_posix()}"]


def test_folder_run_does_not_load_the_project_instructions(
    claude_settings: Settings, job_folder: Path, fake_claude: Path
) -> None:
    run_claude_code(job_folder, claude_settings)

    seen = fake_claude.with_name("claude_mds.txt").read_text(encoding="utf-8")
    assert seen == "1"


@pytest.mark.parametrize(
    ("mode", "problem", "failure"),
    [
        ("not_logged_in", "Not logged in", ClaudeCodeFailure.NOT_LOGGED_IN),
        ("error", "529 Overloaded", ClaudeCodeFailure.FAILED),
        ("garbage", "no JSON result", ClaudeCodeFailure.FAILED),
    ],
)
def test_run_reports_cli_errors(
    claude_settings: Settings,
    job_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    problem: str,
    failure: ClaudeCodeFailure,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)

    with pytest.raises(ClaudeCodeError, match=problem) as caught:
        run_claude_code(job_folder, claude_settings)
    assert caught.value.failure is failure


@pytest.mark.parametrize(
    "message",
    ["Invalid API key · Please run /login", "OAuth token has expired", "Not logged in"],
)
def test_login_problems_are_recognised(message: str) -> None:
    stdout = json.dumps({"subtype": "success", "is_error": True, "result": message})

    with pytest.raises(ClaudeCodeError) as caught:
        check_result(1, stdout, "")
    assert caught.value.failure is ClaudeCodeFailure.NOT_LOGGED_IN


@pytest.mark.parametrize(
    "message",
    [
        "Claude AI usage limit reached|1749924000",
        "Claude usage limit reached. Your limit will reset at 1pm (Etc/GMT+5).",
        "You've hit your session limit · resets 3:50pm (Asia/Tokyo)",
        "You've hit your limit · resets 3pm (Europe/Berlin)",
        "You've hit your weekly limit · resets Oct 28, 9am",
        "Usage credits are required for this model.",
    ],
)
def test_usage_limits_of_the_subscription_are_recognised(message: str) -> None:
    stdout = json.dumps({"subtype": "success", "is_error": True, "result": message})

    with pytest.raises(ClaudeCodeError) as caught:
        check_result(1, stdout, "")

    assert caught.value.failure is ClaudeCodeFailure.USAGE_LIMIT


def test_failure_reasons_are_german() -> None:
    assert failure_reason(ClaudeCodeFailure.NOT_LOGGED_IN) == "Claude Code ist nicht angemeldet"
    assert failure_reason(ClaudeCodeFailure.TIMEOUT) == (
        "Claude Code hat nicht rechtzeitig geantwortet"
    )
    assert failure_reason(ClaudeCodeFailure.USAGE_LIMIT) == (
        "Das Kontingent des Claude-Abos ist aufgebraucht"
    )


def test_ping_asks_without_tools_outside_the_project(
    claude_settings: Settings, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")

    ping_claude_code(claude_settings)

    argv = json.loads(fake_claude.with_name("argv.json").read_text(encoding="utf-8"))
    assert argv[:2] == ["-p", "Antworte nur mit OK."]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in argv


def test_run_stops_at_the_timeout(
    claude_settings: Settings, job_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    quick = claude_settings.model_copy(update={"analysis_timeout_seconds": 1})

    with pytest.raises(ClaudeCodeError, match="no result within 1 seconds") as caught:
        run_claude_code(job_folder, quick)
    assert caught.value.failure is ClaudeCodeFailure.TIMEOUT


def test_run_without_executable(settings: Settings, job_folder: Path) -> None:
    missing = settings.model_copy(update={"analysis_claude_command": "claude-not-installed"})

    with pytest.raises(ClaudeCodeError, match="not found") as caught:
        run_claude_code(job_folder, missing)
    assert caught.value.failure is ClaudeCodeFailure.NOT_INSTALLED


USAGE_RESULT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "1",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 3000,
            "output_tokens": 40,
        },
    }
)


def _counted(stdout: str, returncode: int = 0) -> list[tuple[int, int]]:
    seen: list[tuple[int, int]] = []
    with counting_usage(lambda tokens_in, tokens_out: seen.append((tokens_in, tokens_out))):
        with contextlib.suppress(ClaudeCodeError):
            check_result(returncode, stdout, "")
    return seen


def test_a_result_reports_its_tokens_with_the_cache_counted_as_input() -> None:
    assert _counted(USAGE_RESULT) == [(3210, 40)]


def test_a_result_without_usage_reports_no_tokens() -> None:
    assert _counted(SUCCESS) == [(0, 0)]


@pytest.mark.parametrize("usage", [None, "many", [1], {"input_tokens": "9", "output_tokens": 2.5}])
def test_an_unusable_usage_counts_as_no_tokens(usage: object) -> None:
    result = json.dumps({"subtype": "success", "is_error": False, "result": "1", "usage": usage})

    assert _counted(result) == [(0, 0)]


def test_a_failed_run_still_reports_the_tokens_it_used() -> None:
    failed = json.dumps(
        {"subtype": "error", "is_error": True, "result": "529", "usage": {"output_tokens": 4}}
    )

    assert _counted(failed, returncode=1) == [(0, 4)]


def test_output_that_is_not_json_reports_nothing() -> None:
    assert _counted("garbage", returncode=1) == []


def test_without_a_listener_the_result_is_read_as_before() -> None:
    assert check_result(0, USAGE_RESULT, "") == "1"


def test_the_listener_is_removed_after_the_block() -> None:
    seen: list[tuple[int, int]] = []
    with counting_usage(lambda tokens_in, tokens_out: seen.append((tokens_in, tokens_out))):
        pass

    check_result(0, USAGE_RESULT, "")

    assert seen == []
