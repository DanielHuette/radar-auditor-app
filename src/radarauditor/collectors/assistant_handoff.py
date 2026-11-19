"""Ask an assistant together with a person when its automatic browser route is blocked.

Context: Fallback of decision d of the Vorhaben Radar Auditor. When a security check or a login
wall stops the automatic route, a visible browser window opens the assistant, answers a cookie
dialog with the most data-saving choice, types the question and waits until a person presses send.
A security check shown in that window is answered by the person, never by the program. The answer
is then read like on the automatic route. The person is told through a card in the lane "Eingang"
of the Leitstand board until the cockpit is connected.
Input: A visible BrowserService, the site, the question and the longest wait; the board file.
Output: PageAnswer; HandoffBoard writes the card.
"""

import logging
import math
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from radarauditor.collectors import assistant_pages
from radarauditor.collectors.assistant_pages import AssistantSite, PageAnswer
from radarauditor.collectors.browser import NAVIGATION_TIMEOUT_MS, BrowserService
from radarauditor.errors import CollectionError, TransientCollectionError

logger = logging.getLogger(__name__)

HANDOFF_REPEATS = 2
BOARD_LANE = "## Eingang"
CARD_SENDER = "Radar Auditor"
OPEN_CARD = "- [ ] "
CARD_SENDER_MARK = f"** {CARD_SENDER}: "


async def ask_with_person(
    browser: BrowserService, site: AssistantSite, question: str, wait_seconds: int
) -> PageAnswer:
    """Prepare the question in a visible window and read the answer once a person sent it.

    Args:
        browser: Visible browser of this assistant.
        site: The assistant website.
        question: Question in German, typed in but not sent.
        wait_seconds: Longest wait for the person to send and the answer to finish.

    Returns:
        Answer text and cited sources.

    Raises:
        TransientCollectionError: If no complete answer appears within wait_seconds.
        playwright.async_api.Error: If the window cannot open the site.
    """
    async with browser.page() as page:
        await page.goto(site.url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        await assistant_pages.settle(browser, page)
        await _prepare(browser, page, site, question)
        start = page.url
        return await _wait_for_person(browser, page, site, wait_seconds, start)


async def _prepare(browser: BrowserService, page: Page, site: AssistantSite, question: str) -> None:
    """Switch the mode and type the question; a page that does not allow it is left to the person.

    A security check in front of the assistant hides the mode button and the question field; the
    person answers it and types the question in the window.
    """
    try:
        await assistant_pages.switch_mode(browser, page, site)
        await browser.type_text(page.locator(site.composer).first, question)
    except PlaywrightError as error:
        logger.warning("%s: question not typed in, left to the person: %s", site.provider, error)


async def _wait_for_person(
    browser: BrowserService, page: Page, site: AssistantSite, wait_seconds: int, start: str
) -> PageAnswer:
    previous = ""
    for _ in range(max(1, math.ceil(wait_seconds / assistant_pages.POLL_SECONDS))):
        await browser.pacing.sleep(assistant_pages.POLL_SECONDS)
        reading = assistant_pages.read_answer(await page.content(), site)
        # A site without a completion mark has to leave the start page first, so the text of the
        # unsent page is never taken for an answer.
        sent = site.done is not None or page.url != start
        if sent and assistant_pages.is_finished(reading, site, previous):
            return PageAnswer(reading.text, reading.sources)
        previous = reading.text
    raise TransientCollectionError(
        f"{site.provider}: nobody sent the question within {wait_seconds} s"
    )


class HandoffBoard:
    """Tells the person that questions wait in browser windows: a card on the Leitstand board."""

    def __init__(self, path: Path | None, now: Callable[[], datetime] = datetime.now) -> None:
        """Remember the board file; None only logs the message.

        Args:
            path: kanban.md of the Leitstand, setting HANDOFF_BOARD_PATH.
            now: Clock for the card id.
        """
        self.path = path
        self.now = now

    def notify(self, text: str) -> None:
        """Log the message and add it as a card at the top of the lane "Eingang".

        Args:
            text: Message in German.

        Raises:
            CollectionError: If the board cannot be read or written, or has no lane "Eingang".
        """
        logger.warning("Waiting for a person: %s", text)
        if self.path is None:
            return
        label = f"Meldung R-{self.now():%Y%m%d-%H%M%S}"
        card = f"{OPEN_CARD}**{label}{CARD_SENDER_MARK}{' '.join(text.split())}"
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise CollectionError(f"board {self.path} unreadable: {error}") from error
        if BOARD_LANE not in lines:
            raise CollectionError(f"board {self.path} has no lane {BOARD_LANE!r}")
        index = lines.index(BOARD_LANE) + 1
        lines[index:index] = ["", card]
        _write_atomically(self.path, "\n".join(lines) + "\n")

    def notify_audit(self, key: str, text: str) -> None:
        """Keep one card per audit: change its open card in place, or add one.

        A card the person ticked off is left alone and the next message becomes a new card.

        Args:
            key: Mark of the audit, such as A-1a2b3c4d; the open card whose text names it in
                square brackets is the card of the audit.
            text: The whole card text in German, replacing the earlier one.

        Raises:
            CollectionError: If the board cannot be read or written, or has no lane "Eingang".
        """
        if self.path is None:
            logger.warning("Message of audit %s for the board: %s", key, text)
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise CollectionError(f"board {self.path} unreadable: {error}") from error
        mark = f"[{key}]"
        for index, line in enumerate(lines):
            if line.startswith(OPEN_CARD) and mark in line and CARD_SENDER_MARK in line:
                head = line.split(CARD_SENDER_MARK, 1)[0]
                lines[index] = f"{head}{CARD_SENDER_MARK}{' '.join(text.split())}"
                _write_atomically(self.path, "\n".join(lines) + "\n")
                return
        self.notify(text)


def _write_atomically(path: Path, text: str) -> None:
    try:
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".kanban-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        Path(temporary).replace(path)
    except OSError as error:
        raise CollectionError(f"board {path} not written: {error}") from error
