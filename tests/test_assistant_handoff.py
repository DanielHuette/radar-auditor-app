"""Tests for asking an assistant together with a person.

Context: Unit tests on fake pages and a temporary board file; no Chromium, no network. |
Input: Fixtures. | Output: Assertions.
"""

from datetime import datetime
from pathlib import Path

import pytest
from conftest import BrowserFactory, FakeDocument
from test_assistant_pages import CHATGPT_DONE, CHATGPT_START, GOOGLE_AI_ANSWER, GOOGLE_HOME

from radarauditor.collectors import assistant_handoff
from radarauditor.collectors.assistant_pages import CHATGPT, GOOGLE_AI
from radarauditor.errors import CollectionError, TransientCollectionError

QUESTION = "Wer ist ein guter Dachdecker in Münster?"
BOARD = "---\nArt: Leitstand\n---\n\n## Freigabe-Anfragen\n\n\n\n## Eingang\n\n\n\n## Test\n"


async def test_ask_with_person_types_the_question_and_reads_the_answer_once_sent(
    fake_browser: BrowserFactory,
) -> None:
    """The question is typed in but never sent; the answer is read once it is complete."""
    fake = fake_browser(
        [FakeDocument(CHATGPT_START, later=[CHATGPT_START, CHATGPT_START, CHATGPT_DONE])]
    )

    async with fake.service as service:
        answer = await assistant_handoff.ask_with_person(service, CHATGPT, QUESTION, 60)

    assert answer.text.startswith("Empfehlenswert ist die Dachdeckerei Hollenbeck GmbH")
    assert "".join(fake.page.typed) == QUESTION
    assert fake.page.pressed == []


async def test_ask_with_person_never_takes_the_unsent_start_page_for_an_answer(
    fake_browser: BrowserFactory,
) -> None:
    """A site without a completion mark must leave its start page before text counts."""
    fake = fake_browser([FakeDocument(GOOGLE_HOME, later=[GOOGLE_AI_ANSWER] * 5)])

    async with fake.service as service:
        with pytest.raises(TransientCollectionError, match="nobody sent the question"):
            await assistant_handoff.ask_with_person(service, GOOGLE_AI, QUESTION, 9)


def test_board_card_goes_to_the_top_of_the_lane_eingang(tmp_path: Path) -> None:
    """The card follows the board's form: checkbox, bold id, sender, message."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")
    notify = assistant_handoff.HandoffBoard(board, now=lambda: datetime(2026, 11, 10, 9, 30, 5))

    notify.notify("chatgpt ist gesperrt.  12 Fragen warten.")

    lines = board.read_text(encoding="utf-8").splitlines()
    card = (
        "- [ ] **Meldung R-20261110-093005** Radar Auditor: chatgpt ist gesperrt. 12 Fragen warten."
    )
    assert lines[lines.index("## Eingang") + 2] == card
    assert lines.index("## Test") > lines.index(card)


def test_one_card_per_audit_is_changed_in_place_and_keeps_its_label(tmp_path: Path) -> None:
    """A second message of the same audit rewrites its open card and keeps the card id."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")
    first = assistant_handoff.HandoffBoard(board, now=lambda: datetime(2026, 11, 10, 9, 30, 5))
    later = assistant_handoff.HandoffBoard(board, now=lambda: datetime(2026, 11, 10, 9, 45, 0))

    first.notify_audit("A-1a2b3c4d", "Audit X [A-1a2b3c4d]: erster Fehler")
    later.notify_audit("A-1a2b3c4d", "Audit X [A-1a2b3c4d]: erster Fehler | zweiter Fehler")

    cards = [
        line for line in board.read_text(encoding="utf-8").splitlines() if "A-1a2b3c4d" in line
    ]
    assert cards == [
        "- [ ] **Meldung R-20261110-093005** Radar Auditor: "
        "Audit X [A-1a2b3c4d]: erster Fehler | zweiter Fehler"
    ]


def test_a_card_ticked_off_by_the_person_is_not_reopened(tmp_path: Path) -> None:
    """After the tick the next message of the audit is a new card."""
    board = tmp_path / "kanban.md"
    board.write_text(BOARD, encoding="utf-8")
    notify = assistant_handoff.HandoffBoard(board, now=lambda: datetime(2026, 11, 10, 9, 30, 5))
    notify.notify_audit("A-1a2b3c4d", "Audit X [A-1a2b3c4d]: alt")
    board.write_text(board.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8")

    notify.notify_audit("A-1a2b3c4d", "Audit X [A-1a2b3c4d]: neu")

    lines = board.read_text(encoding="utf-8").splitlines()
    assert [line[:5] for line in lines if "A-1a2b3c4d" in line] == ["- [ ]", "- [x]"]


def test_board_without_lane_eingang_is_an_error(tmp_path: Path) -> None:
    """A board that lacks the lane is reported instead of silently skipped."""
    board = tmp_path / "kanban.md"
    board.write_text("## Test\n", encoding="utf-8")

    with pytest.raises(CollectionError, match="no lane"):
        assistant_handoff.HandoffBoard(board).notify("x")


def test_board_without_path_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Without HANDOFF_BOARD_PATH the message goes to the log."""
    assistant_handoff.HandoffBoard(None).notify("chatgpt wartet")

    assert "chatgpt wartet" in caplog.text
