"""Tests for asking ChatGPT and the Google AI mode on their websites.

Context: Unit tests on saved-shape fake pages and a fake browser; no Chromium, no network. |
Input: Fixtures. | Output: Assertions.
"""

import pytest
from conftest import BrowserFactory, FakeDocument

from radarauditor.collectors import assistant_pages
from radarauditor.collectors.assistant_pages import CHATGPT, GOOGLE_AI
from radarauditor.errors import LoginWallError, SearchBlockedError, TransientCollectionError

QUESTION = "Wer ist ein guter Dachdecker in Münster?"
CHATGPT_START = (
    '<html><body><form><textarea name="prompt" placeholder="ChatGPT fragen"></textarea>'
    "</form></body></html>"
)
CHATGPT_ANSWER = """
<html><body><ol>
<li data-message-role="user" id="u1">Wer ist ein guter Dachdecker in Münster?</li>
<li data-message-role="assistant" id="a1" data-assistant-content-started="" {done}>
<p>Empfehlenswert ist die <strong>Dachdeckerei Hollenbeck GmbH</strong>, Tel. 0251 123456.</p>
<button><img src="https://www.google.com/s2/favicons?domain=https://www.gelbeseiten.de&amp;sz=32">
<span>gelbeseiten.de</span></button>
<a href="https://www.mapbox.com/about/maps">Mapbox</a>
<a href="https://hollenbeck.de/">Website</a>
<script>window.x = 1</script>
</li></ol>
<form><textarea name="prompt"></textarea></form></body></html>
"""
CHATGPT_STREAMING = CHATGPT_ANSWER.format(done="").replace(", Tel. 0251 123456", "")
CHATGPT_DONE = CHATGPT_ANSWER.format(done="data-message-complete")
CHATGPT_WALL = """
<html><body><ol>
<li data-message-role="assistant" id="a1" data-message-complete="">
<p>Melde dich an und wiederhole deine Anfrage.</p></li></ol>
<form><textarea name="prompt"></textarea></form></body></html>
"""


def test_read_answer_of_chatgpt_takes_links_and_favicon_citations() -> None:
    """Favicon pills become sources; map credits and scripts are left out."""
    reading = assistant_pages.read_answer(CHATGPT_DONE, CHATGPT)

    assert reading.complete is True
    assert reading.text.startswith("Empfehlenswert ist die Dachdeckerei Hollenbeck GmbH")
    assert "window.x" not in reading.text
    assert reading.sources == ["https://hollenbeck.de/", "https://www.gelbeseiten.de"]


def test_read_answer_of_chatgpt_is_incomplete_while_streaming() -> None:
    """Without the completion mark the answer is still being written."""
    assert assistant_pages.read_answer(CHATGPT_STREAMING, CHATGPT).complete is False


def test_read_answer_without_an_answer_is_empty() -> None:
    """A page without an answer container reads as empty and incomplete."""
    reading = assistant_pages.read_answer(CHATGPT_START, CHATGPT)

    assert (reading.text, reading.sources, reading.complete) == ("", [], False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Melde dich an und wiederhole deine Anfrage.", True),
        ("Please sign in to continue.", True),
        ("Die Dachdeckerei Hollenbeck ist gut.", False),
        ("Melde dich an " + "x" * 300, False),
    ],
)
def test_is_login_request_only_matches_short_login_answers(text: str, expected: bool) -> None:
    """A long answer that happens to contain a phrase is a real answer."""
    assert assistant_pages.is_login_request(text) is expected


def test_ensure_answerable_detects_login_pages_and_challenges() -> None:
    """A sign-in address is a login wall; a Cloudflare challenge is a bot check."""
    with pytest.raises(LoginWallError, match="auth.openai.com"):
        assistant_pages.ensure_answerable("https://auth.openai.com/log-in", "<html></html>")
    with pytest.raises(SearchBlockedError, match="bot check"):
        assistant_pages.ensure_answerable(
            "https://chatgpt.com/", "<html><title>Just a moment...</title></html>"
        )
    assistant_pages.ensure_answerable("https://chatgpt.com/", CHATGPT_START)


async def test_ask_on_site_types_the_question_and_waits_for_the_complete_answer(
    fake_browser: BrowserFactory,
) -> None:
    """The question is typed key by key, sent with Enter and read once ChatGPT marks it done."""
    fake = fake_browser(
        [FakeDocument(CHATGPT_START, after_submit=["", CHATGPT_STREAMING, CHATGPT_DONE])]
    )

    async with fake.service as service:
        answer = await assistant_pages.ask_on_site(service, CHATGPT, QUESTION, 60)

    assert "0251 123456" in answer.text
    assert answer.sources == ["https://hollenbeck.de/", "https://www.gelbeseiten.de"]
    assert fake.page.visited == [CHATGPT.url]
    assert "".join(fake.page.typed) == QUESTION
    assert fake.page.clicked == [CHATGPT.composer]
    assert fake.page.pressed == ["Enter"]
    assert fake.waits.count(assistant_pages.POLL_SECONDS) == 1 + 3
    assert fake.page.closed is True


async def test_ask_on_site_raises_on_a_login_answer(fake_browser: BrowserFactory) -> None:
    """A site that answers only with a login request is a login wall."""
    fake = fake_browser([FakeDocument(CHATGPT_START, after_submit=[CHATGPT_WALL])])

    async with fake.service as service:
        with pytest.raises(LoginWallError, match="chatgpt asks to sign in"):
            await assistant_pages.ask_on_site(service, CHATGPT, QUESTION, 60)


async def test_ask_on_site_gives_up_after_the_timeout(fake_browser: BrowserFactory) -> None:
    """An answer that never completes is a temporary failure after timeout / poll interval."""
    fake = fake_browser([FakeDocument(CHATGPT_START, after_submit=[CHATGPT_STREAMING])])

    async with fake.service as service:
        with pytest.raises(TransientCollectionError, match="did not finish within 9 s"):
            await assistant_pages.ask_on_site(service, CHATGPT, QUESTION, 9)

    assert fake.waits.count(assistant_pages.POLL_SECONDS) == 1 + 3


async def test_ask_on_site_without_question_field_is_a_login_wall(
    fake_browser: BrowserFactory,
) -> None:
    """A page that never shows the question field is treated as a login page."""
    fake = fake_browser(["<html><body>Bitte anmelden</body></html>"])

    async with fake.service as service:
        with pytest.raises(LoginWallError, match="no question field"):
            await assistant_pages.ask_on_site(service, CHATGPT, QUESTION, 60)

    assert fake.page.typed == []
    assert fake.waits.count(assistant_pages.POLL_SECONDS) == assistant_pages.COMPOSER_POLLS


async def test_ask_on_site_answers_a_cookie_dialog_that_appears_after_loading(
    fake_browser: BrowserFactory,
) -> None:
    """Google lays its cookie dialog over the page a moment after it loaded.

    The dialog is answered with the most data-saving choice before the AI mode is opened.
    """
    fake = fake_browser(
        [
            FakeDocument(GOOGLE_HOME, later=[GOOGLE_HOME, GOOGLE_COOKIE_DIALOG]),
            FakeDocument(GOOGLE_HOME, after_submit=["", GOOGLE_AI_ANSWER, GOOGLE_AI_ANSWER]),
        ]
    )

    async with fake.service as service:
        answer = await assistant_pages.ask_on_site(service, GOOGLE_AI, QUESTION, 60)

    assert answer.text.startswith("In Münster empfiehlt sich die Dachdeckerei Hollenbeck")
    assert fake.page.clicked == ["Alle ablehnen", GOOGLE_AI.mode_button, GOOGLE_AI.composer]


GOOGLE_HOME = (
    '<html><body><textarea name="q"></textarea><button>KI\u2011Modus</button></body></html>'
)
GOOGLE_AI_ANSWER = """
<html><body><div role="main"><p>In Münster empfiehlt sich die Dachdeckerei Hollenbeck.</p>
<a href="https://www.hollenbeck.de/">hollenbeck.de</a>
<a href="https://www.google.com/maps/place/x">Karte</a></div>
<textarea name="q"></textarea></body></html>
"""
GOOGLE_COOKIE_DIALOG = GOOGLE_HOME.replace(
    "</body>",
    '<div class="consent"><button>Alle annehmen</button>'
    "<button>Alle ablehnen</button></div></body>",
)


async def test_ask_on_site_switches_google_to_its_ai_mode_and_waits_for_a_steady_answer(
    fake_browser: BrowserFactory,
) -> None:
    """Google's AI mode is opened with its button.

    Without a completion mark the answer counts as complete once it stays the same between two
    reads.
    """
    fake = fake_browser(
        [FakeDocument(GOOGLE_HOME, after_submit=["", GOOGLE_AI_ANSWER, GOOGLE_AI_ANSWER])]
    )

    async with fake.service as service:
        answer = await assistant_pages.ask_on_site(service, GOOGLE_AI, QUESTION, 60)

    assert answer.text.startswith("In Münster empfiehlt sich die Dachdeckerei Hollenbeck")
    assert answer.sources == ["https://www.hollenbeck.de/"]
    assert fake.page.clicked == ["KI\u2011Modus", GOOGLE_AI.composer]
    # Google marks the mode button up with the role of a link.
    assert fake.page.roles == ["link"]


def test_read_answer_without_completion_mark_is_never_complete_by_itself() -> None:
    """A site without a completion mark leaves the decision to the steadiness of the text."""
    assert assistant_pages.read_answer(GOOGLE_AI_ANSWER, GOOGLE_AI).complete is False


def test_ensure_answerable_detects_googles_sorry_page() -> None:
    """Google sends a browser it takes for a robot to /sorry/ with a captcha."""
    with pytest.raises(SearchBlockedError, match="bot check"):
        assistant_pages.ensure_answerable(
            "https://www.google.com/sorry/index?continue=x", "<html></html>"
        )


async def test_ask_on_site_detects_a_redirect_to_the_sign_in_page(
    fake_browser: BrowserFactory,
) -> None:
    """Google sending the browser to accounts.google.com is a login wall."""
    fake = fake_browser(
        [FakeDocument("<html><body>Anmelden</body></html>", url="https://accounts.google.com/x")]
    )

    async with fake.service as service:
        with pytest.raises(LoginWallError, match="accounts.google.com"):
            await assistant_pages.ask_on_site(service, GOOGLE_AI, QUESTION, 60)
