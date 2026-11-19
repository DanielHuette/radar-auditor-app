"""Tests for the four places a failure goes to: file, database, board and the phone through ntfy.

Context: Temporary folders, fake repositories and a faked HTTP server; nothing is sent over the
network. | Input: Events. | Output: Assertions.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.errors import StorageError
from radarauditor.feedback import sinks
from radarauditor.feedback.events import AffectedCheck, AuditEvent, Result

AUDIT_ID = UUID("1a2b3c4d-0000-4000-8000-000000000001")
NTFY_SERVER = "https://ntfy.sh"
TOPIC = "repocity-radar-test-0123456789abcdef"
NOTICE_TEXT = "Audit X: Messung fehlgeschlagen – Google-Suche: Sicherheitsabfrage"
BOARD = "---\nArt: Leitstand\n---\n\n## Eingang\n\n\n\n## Test\n"


def _event(result: Result = Result.FAILED) -> AuditEvent:
    return AuditEvent(
        time=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        kind="route",
        phase="messung",
        result=result,
        reason="Google zeigt eine Sicherheitsabfrage",
        stage=1,
        route="google_search",
        label="Google-Suche",
        checks=[AffectedCheck(number=8, id="j1", name="Platz")],
    )


def test_file_sink_appends_one_json_line_per_event(tmp_path: Path) -> None:
    """Each audit has its own file; every event is one line a program can read."""
    write = sinks.file_sink(tmp_path / "ereignisse", AUDIT_ID)

    write(_event(Result.MEASURED))
    write(_event())

    lines = (tmp_path / "ereignisse" / f"{AUDIT_ID}.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["result"] for line in lines] == ["gemessen", "fehlgeschlagen"]
    assert json.loads(lines[1])["checks"] == [{"number": 8, "id": "j1", "name": "Platz"}]


def test_file_sink_keeps_umlauts_readable(tmp_path: Path) -> None:
    """The file stays readable for a person: no escaped umlauts."""
    sinks.file_sink(tmp_path, AUDIT_ID)(_event().model_copy(update={"reason": "Prüfung nötig"}))

    assert "Prüfung nötig" in (tmp_path / f"{AUDIT_ID}.jsonl").read_text(encoding="utf-8")


def test_file_sink_logs_instead_of_failing_when_the_folder_is_not_writable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that cannot be written does not stop the audit, and the log says so."""
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        sinks.file_sink(blocker, AUDIT_ID)(_event())

    assert "Event file" in caplog.text


def test_database_sink_hands_the_events_to_the_repositories() -> None:
    """The table audit_events gets every event of the audit."""
    repositories = MagicMock()

    sinks.database_sink(repositories, AUDIT_ID)([_event(), _event(Result.MEASURED)])

    repositories.add_audit_events.assert_called_once()
    audit_id, events = repositories.add_audit_events.call_args.args
    assert audit_id == AUDIT_ID
    assert [event.result for event in events] == [Result.FAILED, Result.MEASURED]


def test_database_sink_logs_instead_of_failing_when_the_table_is_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database without migration 017 does not stop the audit."""
    repositories = MagicMock()
    repositories.add_audit_events.side_effect = StorageError("save audit events failed")

    with caplog.at_level(logging.WARNING):
        sinks.database_sink(repositories, AUDIT_ID)([_event()])

    assert "Events not saved" in caplog.text


def test_board_sink_puts_one_card_per_audit_and_updates_it(tmp_path: Path) -> None:
    """A second failure of the same audit changes the card instead of adding another."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")
    write = sinks.board_sink(board, "A-1a2b3c4d")

    write("Audit Katja Karsch [A-1a2b3c4d]: Google-Suche: Sicherheitsabfrage")
    write(
        "Audit Katja Karsch [A-1a2b3c4d]: Google-Suche: Sicherheitsabfrage | Bing-Suche: gesperrt"
    )

    cards = [
        line for line in board.read_text(encoding="utf-8").splitlines() if line.startswith("- [ ]")
    ]
    assert len(cards) == 1
    assert cards[0].endswith(
        "Radar Auditor: Audit Katja Karsch [A-1a2b3c4d]: "
        "Google-Suche: Sicherheitsabfrage | Bing-Suche: gesperrt"
    )


def test_board_sink_starts_a_new_card_when_the_old_one_is_ticked_off(tmp_path: Path) -> None:
    """Daniel ticked the card off, so the next failure is a new card."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")
    write = sinks.board_sink(board, "A-1a2b3c4d")
    write("Audit X [A-1a2b3c4d]: erster Fehler")
    ticked = board.read_text(encoding="utf-8").replace("- [ ] **", "- [x] **")
    board.write_text(ticked, encoding="utf-8")

    write("Audit X [A-1a2b3c4d]: zweiter Fehler")

    lines = board.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("- [x]") for line in lines) == 1
    assert sum(line.startswith("- [ ]") for line in lines) == 1


def test_board_sink_leaves_cards_of_other_audits_alone(tmp_path: Path) -> None:
    """Two audits have two cards."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")

    sinks.board_sink(board, "A-11111111")("Audit A [A-11111111]: Fehler")
    sinks.board_sink(board, "A-22222222")("Audit B [A-22222222]: Fehler")

    assert (
        sum(line.startswith("- [ ]") for line in board.read_text(encoding="utf-8").splitlines())
        == 2
    )


def test_board_sink_without_a_board_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Without a board path the text goes to the log, like for the fallback with a person."""
    with caplog.at_level(logging.WARNING):
        sinks.board_sink(None, "A-1a2b3c4d")("Audit X [A-1a2b3c4d]: Fehler")

    assert "Audit X" in caplog.text


def test_board_sink_logs_a_board_that_cannot_be_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A board without the lane is reported in the log and does not stop the audit."""
    board = tmp_path / "kanban.md"
    board.write_text("## Test\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        sinks.board_sink(board, "A-1a2b3c4d")("Audit X [A-1a2b3c4d]: Fehler")

    assert "Card not put on the board" in caplog.text


def test_overview_sink_writes_the_table_next_to_the_events(tmp_path: Path) -> None:
    """The overview is a Markdown file per audit."""
    sinks.overview_sink(tmp_path, AUDIT_ID)("| Nr |\n")

    assert (tmp_path / f"{AUDIT_ID}_uebersicht.md").read_text(encoding="utf-8") == "| Nr |\n"


def _posted(route: respx.Route) -> httpx.Request:
    (call,) = route.calls
    return call.request


@respx.mock
def test_ntfy_notice_posts_the_text_with_title_priority_and_tag() -> None:
    route = respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, False)

    request = _posted(route)
    assert request.content.decode("utf-8") == NOTICE_TEXT
    assert request.headers["X-Title"] == "Radar Auditor"
    assert request.headers["X-Priority"] == "default"
    assert request.headers["X-Tags"] == "warning"


@respx.mock
def test_ntfy_notice_of_a_halted_audit_has_high_priority() -> None:
    route = respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, True)

    request = _posted(route)
    assert request.headers["X-Priority"] == "high"
    assert request.headers["X-Tags"] == "rotating_light"


@respx.mock
def test_ntfy_notice_sends_the_token_as_bearer() -> None:
    route = respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice(NTFY_SERVER, TOPIC, SecretStr("tk_secret"))(NOTICE_TEXT, False)

    assert _posted(route).headers["Authorization"] == "Bearer tk_secret"


@respx.mock
def test_ntfy_notice_without_a_token_sends_no_authorization() -> None:
    route = respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, False)

    assert "Authorization" not in _posted(route).headers


@respx.mock
def test_ntfy_notice_goes_to_the_own_server_of_the_setting() -> None:
    route = respx.post(f"https://ntfy.example.org/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice("https://ntfy.example.org/", TOPIC, None)(NOTICE_TEXT, False)

    assert route.call_count == 1


@respx.mock
def test_ntfy_notice_waits_ten_seconds_at_most() -> None:
    route = respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(200))

    sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, False)

    timeout = _posted(route).extensions["timeout"]
    assert set(timeout.values()) == {10}


@pytest.mark.parametrize("topic", [None, ""])
@respx.mock
def test_ntfy_notice_without_a_topic_sends_nothing(topic: str | None) -> None:
    sinks.ntfy_notice(NTFY_SERVER, topic, None)(NOTICE_TEXT, True)

    assert respx.calls.call_count == 0


@respx.mock
def test_ntfy_notice_logs_a_failed_send_without_the_topic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(side_effect=httpx.ConnectTimeout("too slow"))

    with caplog.at_level(logging.WARNING):
        sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, False)

    assert "ntfy notice not sent" in caplog.text
    assert TOPIC not in caplog.text


@respx.mock
def test_ntfy_notice_logs_a_refusal_with_its_status_and_without_the_topic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{NTFY_SERVER}/{TOPIC}").mock(return_value=httpx.Response(403))

    with caplog.at_level(logging.WARNING):
        sinks.ntfy_notice(NTFY_SERVER, TOPIC, None)(NOTICE_TEXT, False)

    assert "ntfy notice not sent" in caplog.text and "403" in caplog.text
    assert TOPIC not in caplog.text
