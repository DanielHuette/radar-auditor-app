import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx
from conftest import BrowserFactory, FakeBrowser, FakeDocument
from pydantic import SecretStr
from test_assistant_pages import (
    CHATGPT_DONE,
    CHATGPT_START,
    CHATGPT_STREAMING,
    CHATGPT_WALL,
)
from test_openai_price import PRICE_LINE, routes_with

from radarauditor.collectors import assistant_pages, assistants, google_ai_mode, openai_price
from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import PhaseTiming
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import CollectionError, ConfigurationError, LoginWallError

pytestmark = pytest.mark.usefixtures("fast_retries")

QUESTIONS = ["Wer ist ein guter Dachdecker in Münster?"]
QUESTIONS_FILE = Path(__file__).resolve().parents[1] / "config" / "fragen.yaml"
BUSINESS = "Dachdeckerei Hollenbeck GmbH"
AUDIT_ID = UUID("00000000-0000-0000-0000-0000000000a1")
USAGE = {"prompt_tokens": 1000, "completion_tokens": 2000}


class Ledger:
    def __init__(self, spent: Decimal = Decimal(0)) -> None:
        self.spent = spent
        self.audit_rows: list[tuple[UUID, str, str, int, Decimal, int, int]] = []

    def month_source_cost(self, source: str) -> Decimal:
        return self.spent

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        self.spent += cost
        return self.spent

    def add_audit_cost(
        self,
        audit_id: UUID,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.audit_rows.append((audit_id, source, service, calls, cost, tokens_in, tokens_out))


def _gate(
    *sources: Source, cap: int | None = None, ledger: Ledger | None = None
) -> tuple[SourceGate, list[str]]:
    counted: list[str] = []
    settings = SourcesSettings.model_validate(
        {source.value: {"enabled": source in sources} for source in Source}
    )

    def consume(source: str, daily_cap: int) -> bool:
        counted.append(source)
        return cap is None or counted.count(source) <= cap

    audit_id = AUDIT_ID if ledger is not None else None
    return SourceGate(settings, consume, ledger, audit_id), counted


@pytest.fixture
def templates() -> assistants.QuestionTemplates:
    return assistants.load_question_templates(QUESTIONS_FILE)


@pytest.fixture
def two_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 2)


@pytest.fixture
def paid_keys(collector_settings: Settings, fake_claude: Path) -> Settings:
    return collector_settings.model_copy(
        update={
            "openai_api_key": SecretStr("openai-key"),
            "analysis_claude_command": str(fake_claude),
            "assistant_timeout_seconds": 6,
        }
    )


@pytest.fixture
def walled_browser(fake_browser: BrowserFactory) -> FakeBrowser:
    fake = fake_browser([])
    fake.page.failure = LoginWallError("redirected to the sign-in page")
    return fake


def _chat(content: str, **extra: object) -> httpx.Response:
    body = {"choices": [{"message": {"content": content}}], "usage": USAGE, **extra}
    return httpx.Response(200, json=body)


def _chatgpt_page() -> FakeDocument:
    return FakeDocument(CHATGPT_START, after_submit=[CHATGPT_STREAMING, CHATGPT_DONE])


def test_enabled_providers_follows_the_switches(paid_keys: Settings) -> None:
    gate, _ = _gate(Source.GOOGLE_AI, Source.CLAUDE)

    assert assistants.enabled_providers(paid_keys, gate) == ["google_ai", "claude"]
    assert assistants.enabled_providers(paid_keys, _gate()[0]) == []


def test_build_questions_creates_three_recommendation_and_three_problem_questions(
    templates: assistants.QuestionTemplates,
) -> None:
    questions = assistants.build_questions(
        "Dachdecker",
        "Münster",
        ["Flachdach", "Dachsanierung", "Dachfenster", "Notdienst"],
        templates,
    )

    assert questions == [
        "Dachdecker in Münster: Wen empfiehlst du?",
        "Wen empfiehlst du in Münster, wenn ich zum Thema „Dachdecker“ jemanden suche?",
        "Top 3 Dachdecker Münster",
        "Flachdach in Münster, wen beauftragen?",
        "Ich brauche Dachsanierung in Münster, wer macht das gut?",
        "Dachfenster Münster Erfahrungen",
    ]


def test_build_questions_capitalise_trade_and_services_for_any_noun(
    templates: assistants.QuestionTemplates,
) -> None:
    questions = assistants.build_questions(
        "künstlerin", "Münster", ["kunst kaufen", " atelier "], templates
    )

    assert questions == [
        "Künstlerin in Münster: Wen empfiehlst du?",
        "Wen empfiehlst du in Münster, wenn ich zum Thema „Künstlerin“ jemanden suche?",
        "Top 3 Künstlerin Münster",
        "Kunst kaufen in Münster, wen beauftragen?",
        "Ich brauche Atelier in Münster, wer macht das gut?",
        "Kunst kaufen Münster Erfahrungen",
    ]


def test_shipped_recommendation_questions_name_a_word_check_k1_recognises(
    templates: assistants.QuestionTemplates,
) -> None:
    words = ("empfiehlst", "guter", "Top 3")

    assert all(any(word in template for word in words) for template in templates.recommendation)


def test_build_questions_without_services_uses_industry(
    templates: assistants.QuestionTemplates,
) -> None:
    questions = assistants.build_questions("Maler", "Münster", [" "], templates)

    assert len(questions) == 6
    assert questions[3:] == [
        "Maler in Münster, wen beauftragen?",
        "Ich brauche Maler in Münster, wer macht das gut?",
        "Maler Münster Erfahrungen",
    ]


def test_build_questions_cycles_services_over_problem_templates() -> None:
    templates = assistants.QuestionTemplates(
        recommendation=["{industry} {city} {service}?"],
        problem=["{service} A", "{service} B", "{service} C"],
    )

    questions = assistants.build_questions("Maler", "Münster", ["Fassade", "Lack"], templates)

    assert questions == ["Maler Münster Fassade?", "Fassade A", "Lack B", "Fassade C"]


def test_load_question_templates_reads_both_lists(
    templates: assistants.QuestionTemplates,
) -> None:
    assert len(templates.recommendation) == 3
    assert len(templates.problem) == 3


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("empfehlung: [a\n", "not valid YAML"),
        ("empfehlung: ['Wer?']\n", "malformed"),
        ("empfehlung: []\nproblem: ['x']\n", "malformed"),
        ("empfehlung: ['{branche} in {city}']\nproblem: ['x']\n", "unknown placeholders"),
        ("empfehlung: ['{city']\nproblem: ['x']\n", "malformed"),
    ],
)
def test_load_question_templates_rejects_bad_files(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "fragen.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        assistants.load_question_templates(path)


def test_load_question_templates_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Cannot read"):
        assistants.load_question_templates(tmp_path / "fehlt.yaml")


async def test_ask_all_without_providers_returns_nothing(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([])

    assert await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, _gate()[0], fake.service) == []
    assert fake.page.visited == []


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_asks_chatgpt_in_the_browser(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([_chatgpt_page(), _chatgpt_page()])
    gate, counted = _gate(Source.CHATGPT)

    async with fake.service as browser, respx.mock:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert [answer.route for answer in answers] == ["browser", "browser"]
    assert all(answer.provider == "chatgpt" and answer.mentioned for answer in answers)
    assert answers[0].cited_sources == ["https://hollenbeck.de/", "https://www.gelbeseiten.de"]
    assert answers[0].question == QUESTIONS[0]
    assert fake.page.visited == [assistant_pages.CHATGPT.url] * 2
    assert counted == ["chatgpt", "chatgpt"]
    pause = fake.service.pacing.between_actions
    assert sum(1 for wait in fake.waits if wait >= pause) == 1


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_falls_back_to_the_paid_interface_after_a_login_wall(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([FakeDocument(CHATGPT_START, after_submit=[CHATGPT_WALL])])
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat(f"{BUSINESS} [1]"))
    gate, counted = _gate(Source.CHATGPT)

    async with fake.service as browser:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert [answer.route for answer in answers] == ["api", "api"]
    assert openai.call_count == 2
    assert fake.page.visited == [assistant_pages.CHATGPT.url]
    assert counted == ["chatgpt"] * 3


async def test_ask_all_without_key_reports_the_blocked_browser(
    collector_settings: Settings, walled_browser: FakeBrowser
) -> None:
    gate, counted = _gate(Source.CHATGPT)

    with pytest.raises(CollectionError, match="no assistant answered: chatgpt: redirected"):
        await assistants.ask_all(
            QUESTIONS, BUSINESS, collector_settings, gate, walled_browser.service
        )

    assert counted == ["chatgpt"]


@respx.mock
async def test_ask_all_sends_a_single_timeout_to_the_paid_interface(
    paid_keys: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 3)
    stuck = FakeDocument(CHATGPT_START, after_submit=[CHATGPT_STREAMING])
    fake = fake_browser([stuck, _chatgpt_page(), _chatgpt_page()])
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat("Bedachungen Overmann"))
    gate, _ = _gate(Source.CHATGPT)

    async with fake.service as browser:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert [answer.route for answer in answers] == ["api", "browser", "browser"]
    assert answers[0].mentioned is False
    assert openai.call_count == 1


@respx.mock
async def test_ask_all_leaves_the_browser_after_repeated_failures(
    paid_keys: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 4)
    stuck = FakeDocument(CHATGPT_START, after_submit=[CHATGPT_STREAMING])
    fake = fake_browser([stuck, stuck])
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat(BUSINESS))
    gate, counted = _gate(Source.CHATGPT)

    async with fake.service as browser:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert [answer.route for answer in answers] == ["api"] * 4
    assert len(fake.page.visited) == assistants.BROWSER_FAILURES_BEFORE_FALLBACK
    assert openai.call_count == 4
    assert counted.count("chatgpt") == 2 + 4


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_stops_a_provider_at_its_daily_cap(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([_chatgpt_page(), _chatgpt_page()])
    gate, _ = _gate(Source.CHATGPT, cap=1)

    async with fake.service as browser:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert len(answers) == 1
    assert len(fake.page.visited) == 1
    assert gate.exhausted == {Source.CHATGPT}


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_asks_claude_through_claude_code(
    paid_keys: Settings, fake_browser: BrowserFactory, fake_claude: Path
) -> None:
    gate, counted = _gate(Source.CLAUDE)

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, paid_keys, gate, fake_browser([]).service
    )

    assert [answer.route for answer in answers] == ["claude_code", "claude_code"]
    assert answers[0].mentioned is True
    assert answers[0].cited_sources == [
        "https://hollenbeck.de",
        "https://www.gelbeseiten.de/dachdecker/muenster",
    ]
    argv = json.loads(fake_claude.with_name("argv.json").read_text(encoding="utf-8"))
    assert argv[:2] == ["-p", QUESTIONS[0]]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--allowedTools") + 1 :] == ["WebSearch", "WebFetch"]
    working_dir = Path(fake_claude.with_name("cwd.txt").read_text(encoding="utf-8"))
    assert working_dir.name.startswith("radar-claude-")
    assert not working_dir.exists()
    assert counted == ["claude", "claude"]


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("not_logged_in", "Not logged in"),
        ("error", "529 Overloaded"),
        ("empty", "empty result"),
    ],
)
async def test_ask_all_reports_claude_code_failures(
    paid_keys: Settings,
    fake_browser: BrowserFactory,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    message: str,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 1)

    with pytest.raises(CollectionError, match=message):
        await assistants.ask_all(
            QUESTIONS, BUSINESS, paid_keys, _gate(Source.CLAUDE)[0], fake_browser([]).service
        )


def _peak_overlap(log: Path) -> int:
    events = sorted(
        (float(stamp), 1 if kind == "start" else -1)
        for kind, stamp in (line.split() for line in log.read_text(encoding="utf-8").splitlines())
    )
    open_now = peak = 0
    for _, change in events:
        open_now += change
        peak = max(peak, open_now)
    return peak


@pytest.mark.parametrize(("limit", "parallel"), [(1, False), (4, True)])
async def test_ask_all_asks_claude_in_parallel_up_to_the_configured_limit(
    paid_keys: Settings,
    fake_browser: BrowserFactory,
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    parallel: bool,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 4)
    settings = paid_keys.model_copy(update={"claude_parallel_questions": limit})

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, settings, _gate(Source.CLAUDE)[0], fake_browser([]).service
    )

    assert len(answers) == 4
    peak = _peak_overlap(fake_claude.with_name("calls.log"))
    assert (peak > 1) is parallel
    assert peak <= limit


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_gives_every_browser_assistant_its_own_browser(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    shared = fake_browser([])
    chatgpt = fake_browser([_chatgpt_page(), _chatgpt_page()])
    made: list[str] = []

    def browser_for(provider: str) -> BrowserService:
        made.append(provider)
        return {"chatgpt": chatgpt}[provider].service

    gate, _ = _gate(Source.CHATGPT)
    one_window = collector_settings.model_copy(update={"chatgpt_windows": 1})
    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, one_window, gate, shared.service, browser_for=browser_for
    )

    assert made == ["chatgpt"]
    assert {answer.provider for answer in answers} == {"chatgpt"}
    assert chatgpt.page.visited == [assistant_pages.CHATGPT.url] * 2
    assert shared.page.visited == []
    pause = chatgpt.service.pacing.between_actions
    assert sum(1 for wait in chatgpt.waits if wait >= pause) == 1
    assert chatgpt.context.closed


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_asks_chatgpt_in_several_windows_at_once(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    first = fake_browser([_chatgpt_page()])
    second = fake_browser([_chatgpt_page()])
    made: list[str] = []

    def browser_for(name: str) -> BrowserService:
        made.append(name)
        return {"chatgpt": first, "chatgpt-2": second}[name].service

    gate, _ = _gate(Source.CHATGPT)
    two_windows = collector_settings.model_copy(update={"chatgpt_windows": 2})
    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, two_windows, gate, fake_browser([]).service, browser_for=browser_for
    )

    assert made == ["chatgpt", "chatgpt-2"]
    assert [answer.route for answer in answers] == ["browser", "browser"]
    assert first.page.visited == [assistant_pages.CHATGPT.url]
    assert second.page.visited == [assistant_pages.CHATGPT.url]


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_hands_a_blocked_assistant_to_a_person(
    collector_settings: Settings, walled_browser: FakeBrowser, fake_browser: BrowserFactory
) -> None:
    waiting = [CHATGPT_START, CHATGPT_START, CHATGPT_DONE]
    visible = fake_browser(
        [FakeDocument(CHATGPT_START, later=waiting), FakeDocument(CHATGPT_START, later=waiting)]
    )
    told: list[str] = []
    opened: list[str] = []

    def person_browser(provider: str) -> BrowserService:
        opened.append(provider)
        return visible.service

    handoff = assistants.Handoff(browser_for=person_browser, notify=told.append)
    gate, _ = _gate(Source.CHATGPT)

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, collector_settings, gate, walled_browser.service, handoff=handoff
    )

    assert [answer.route for answer in answers] == ["manual"] * assistants.HANDOFF_REPEATS
    assert all(answer.mentioned for answer in answers)
    assert opened == ["chatgpt"]
    assert len(told) == 1 and "chatgpt" in told[0].lower()
    assert visible.page.pressed == []
    assert "".join(visible.page.typed) == QUESTIONS[0] * assistants.HANDOFF_REPEATS


async def test_ask_all_sends_an_assistant_whose_probe_failed_straight_to_a_person(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    automatic = fake_browser([])
    waiting = [CHATGPT_START, CHATGPT_DONE]
    visible = fake_browser(
        [FakeDocument(CHATGPT_START, later=waiting), FakeDocument(CHATGPT_START, later=waiting)]
    )
    handoff = assistants.Handoff(
        browser_for=lambda provider: visible.service,
        notify=lambda text: None,
        manual=frozenset({"chatgpt"}),
    )
    gate, _ = _gate(Source.CHATGPT)

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, collector_settings, gate, automatic.service, handoff=handoff
    )

    assert len(answers) == assistants.HANDOFF_REPEATS
    assert automatic.page.visited == []


async def test_ask_all_stops_the_fallback_when_nobody_sends(
    collector_settings: Settings, walled_browser: FakeBrowser, fake_browser: BrowserFactory
) -> None:
    visible = fake_browser([FakeDocument(CHATGPT_START)])
    handoff = assistants.Handoff(browser_for=lambda provider: visible.service, notify=print)
    settings = collector_settings.model_copy(update={"handoff_timeout_seconds": 6})
    gate, _ = _gate(Source.CHATGPT)

    with pytest.raises(CollectionError, match="nobody sent"):
        await assistants.ask_all(
            QUESTIONS, BUSINESS, settings, gate, walled_browser.service, handoff=handoff
        )

    assert len(visible.page.visited) == 1


async def test_probe_reports_which_assistants_answered(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    chatgpt = fake_browser([_chatgpt_page()])
    blocked = fake_browser(["<html><body>Bitte anmelden</body></html>"])
    gate, counted = _gate(Source.CHATGPT, Source.GOOGLE_AI)
    configured = collector_settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("password")}
    )

    result = await assistants.probe(
        QUESTIONS[0],
        configured,
        gate,
        browser_for=lambda name: {"chatgpt": chatgpt, "google_ai": blocked}[name].service,
    )

    assert result == {"chatgpt": None, "google_ai": None}
    assert counted == ["chatgpt"]
    assert blocked.page.visited == []


async def test_probe_reports_missing_dataforseo_credentials_for_google_ai(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    without = collector_settings.model_copy(update={"dataforseo_login": None})
    gate, counted = _gate(Source.GOOGLE_AI, Source.DATAFORSEO)

    result = await assistants.probe(
        QUESTIONS[0], without, gate, browser_for=lambda name: fake_browser([]).service
    )

    assert result == {"google_ai": assistants.DATAFORSEO_MISSING}
    assert counted == []


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_times_every_assistant(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([_chatgpt_page(), _chatgpt_page()])
    gate, _ = _gate(Source.CHATGPT, Source.CLAUDE)
    timings: list[PhaseTiming] = []

    async with fake.service as browser:
        await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser, timings=timings)

    by_phase = {timing.phase: timing for timing in timings}
    assert set(by_phase) == {"assistant:chatgpt", "assistant:claude"}
    assert all(timing.answers == 2 and timing.seconds >= 0 for timing in timings)


async def test_ask_all_skips_claude_without_the_cli(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    settings = paid_keys.model_copy(update={"analysis_claude_command": "claude-not-installed"})
    gate, counted = _gate(Source.CLAUDE)

    with pytest.raises(CollectionError, match="claude-not-installed"):
        await assistants.ask_all(QUESTIONS, BUSINESS, settings, gate, fake_browser([]).service)

    assert counted.count("claude") <= assistants.CONCURRENT_REQUESTS


async def test_ask_all_drops_a_claude_answer_after_the_timeout(
    paid_keys: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setattr(assistants, "REPEATS_PER_QUESTION", 1)
    settings = paid_keys.model_copy(update={"assistant_timeout_seconds": 1})

    with pytest.raises(CollectionError, match="no result within 1 seconds"):
        await assistants.ask_all(
            QUESTIONS, BUSINESS, settings, _gate(Source.CLAUDE)[0], fake_browser([]).service
        )


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_uses_configured_models(
    paid_keys: Settings, walled_browser: FakeBrowser, tmp_path: Path
) -> None:
    routes = routes_with(tmp_path, PRICE_LINE, 'gpt-x: {input: "1", output: "2"}')
    settings = paid_keys.model_copy(update={"openai_model": "gpt-x", "routes_path": routes})
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat("a"))
    gate, _ = _gate(Source.CHATGPT)

    await assistants.ask_all(QUESTIONS, BUSINESS, settings, gate, walled_browser.service)

    assert json.loads(openai.calls[0].request.content)["model"] == "gpt-x"


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_reads_the_paid_interface(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    openai = respx.post(assistants.OPENAI_URL).mock(
        return_value=_chat("Die **Dachdeckerei Hollenbeck** ist beliebt (https://hollenbeck.de).")
    )
    gate, _ = _gate(Source.CHATGPT)

    answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, walled_browser.service)

    assert [answer.provider for answer in answers] == ["chatgpt"] * 2
    assert {answer.route for answer in answers} == {"api"}
    assert answers[0].mentioned is True
    assert answers[0].cited_sources == ["https://hollenbeck.de"]
    assert openai.call_count == 2
    openai_body = json.loads(openai.calls[0].request.content)
    assert openai_body["model"] == "gpt-4o-mini"
    assert openai_body["messages"] == [{"role": "user", "content": QUESTIONS[0]}]
    assert openai_body["max_completion_tokens"] == openai_price.MAX_OUTPUT_TOKENS
    assert openai.calls[0].request.headers["Authorization"] == "Bearer openai-key"


@pytest.mark.usefixtures("two_repeats")
@pytest.mark.parametrize(
    ("reply", "domain", "mentioned"),
    [
        ("Hollenbeck Dachdeckerei in Münster ist gut.", None, True),
        ("Dachdeckerei Hollenbeck, Münster.", None, True),
        ("Empfehlung: https://www.hollenbeck.de/dach", "https://hollenbeck.de", True),
        ("Empfehlung: https://www.hollenbeck.de/dach", None, False),
        ("Empfehlung: Bedachungen Overmann", "https://hollenbeck.de", False),
    ],
)
@respx.mock
async def test_ask_all_recognises_name_variants_and_the_domain(
    paid_keys: Settings,
    walled_browser: FakeBrowser,
    reply: str,
    domain: str | None,
    mentioned: bool,
) -> None:
    respx.post(assistants.OPENAI_URL).mock(return_value=_chat(reply))
    gate, _ = _gate(Source.CHATGPT)

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, paid_keys, gate, walled_browser.service, domain=domain
    )

    assert {answer.mentioned for answer in answers} == {mentioned}


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_skips_a_provider_that_rejects_its_key(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=httpx.Response(401))
    gate, _ = _gate(Source.CHATGPT)

    with pytest.raises(CollectionError, match="no assistant answered: chatgpt"):
        await assistants.ask_all(QUESTIONS, "Hollenbeck", paid_keys, gate, walled_browser.service)

    assert openai.call_count == 1


@pytest.mark.usefixtures("two_repeats")
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "OpenAI answer is malformed"),
        ({"choices": [{"message": {"content": "a"}}]}, "OpenAI answer is malformed"),
        ({"choices": [], "usage": USAGE}, "OpenAI returned no choice"),
    ],
)
@respx.mock
async def test_ask_all_reports_unusable_paid_answers(
    paid_keys: Settings, walled_browser: FakeBrowser, body: dict[str, object], message: str
) -> None:
    respx.post(assistants.OPENAI_URL).mock(return_value=httpx.Response(200, json=body))
    gate, _ = _gate(Source.CHATGPT)

    with pytest.raises(CollectionError, match=message):
        await assistants.ask_all(QUESTIONS, "Hollenbeck", paid_keys, gate, walled_browser.service)


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_keeps_long_answers_whole(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    respx.post(assistants.OPENAI_URL).mock(return_value=_chat("x" * 2000 + " Hollenbeck"))
    gate, _ = _gate(Source.CHATGPT)

    answers = await assistants.ask_all(
        QUESTIONS, "Hollenbeck", paid_keys, gate, walled_browser.service
    )

    assert {answer.provider for answer in answers} == {"chatgpt"}
    assert answers[0].answer_excerpt == "x" * 2000 + " Hollenbeck"
    assert answers[0].mentioned is True


def test_provider_source_knows_current_and_legacy_names() -> None:
    assert assistants.provider_source("chatgpt") is Source.CHATGPT
    assert assistants.provider_source("openai") is Source.CHATGPT
    assert assistants.provider_source("claude") is Source.CLAUDE
    assert assistants.provider_source("copilot") is None


@pytest.mark.parametrize(
    ("phrase", "city", "expected"),
    [
        ("Elektriker Münster", "Münster", "Elektriker"),
        ("münster elektriker", "Münster", "elektriker"),
        ("Heizung Notdienst Münster", "Münster", "Heizung Notdienst"),
        ("Friseur Bad Oeynhausen", "Bad Oeynhausen", "Friseur"),
        ("Münster", "Münster", None),
        ("", "Münster", None),
    ],
)
def test_trade_from_keyword_drops_the_city(phrase: str, city: str, expected: str | None) -> None:
    assert assistants.trade_from_keyword(phrase, city) == expected


def _ai_mode_settings(collector_settings: Settings) -> Settings:
    return collector_settings.model_copy(
        update={
            "dataforseo_login": "login",
            "dataforseo_password": SecretStr("secret"),
            "ai_mode_timeout_seconds": 77,
            "ai_mode_poll_seconds": 3,
        }
    )


def _fake_ai_mode(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[int, google_ai_mode.AiModeAnswer],
    failed: dict[int, str],
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def ask(
        client: object, questions: list[str], *, timeout_seconds: float, poll_seconds: float
    ) -> google_ai_mode.AiModeBatch:
        calls.append({"questions": questions, "timeout": timeout_seconds, "poll": poll_seconds})
        return google_ai_mode.AiModeBatch(answers, failed)

    monkeypatch.setattr(google_ai_mode, "ask_ai_mode", ask)
    return calls


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_asks_google_ai_through_dataforseo_without_a_browser(
    collector_settings: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = google_ai_mode.AiModeAnswer(
        "**Dachdeckerei Hollenbeck** ist gut.", ["https://hollenbeck.de"]
    )
    calls = _fake_ai_mode(monkeypatch, {0: named, 1: named}, {})
    gate, counted = _gate(Source.GOOGLE_AI, Source.DATAFORSEO)
    opened: list[str] = []

    def browser_for(name: str) -> BrowserService:
        opened.append(name)
        return fake_browser([]).service

    answers = await assistants.ask_all(
        QUESTIONS,
        BUSINESS,
        _ai_mode_settings(collector_settings),
        gate,
        fake_browser([]).service,
        browser_for=browser_for,
    )

    assert [(answer.provider, answer.route, answer.mentioned) for answer in answers] == [
        ("google_ai", "dataforseo", True)
    ] * 2
    assert answers[0].cited_sources == ["https://hollenbeck.de"]
    assert calls == [{"questions": QUESTIONS * 2, "timeout": 77, "poll": 3}]
    assert opened == []
    assert counted == ["google_ai"] * 2


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_reports_why_every_ai_mode_task_failed(
    collector_settings: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ai_mode(monkeypatch, {}, {0: "no AI mode result within 77 s", 1: "second"})
    reasons: dict[str, str] = {}

    with pytest.raises(CollectionError, match="no assistant answered"):
        await assistants.ask_all(
            QUESTIONS,
            BUSINESS,
            _ai_mode_settings(collector_settings),
            _gate(Source.GOOGLE_AI, Source.DATAFORSEO)[0],
            fake_browser([]).service,
            reasons=reasons,
        )

    assert reasons == {
        "google_ai": "2 of 2 AI mode questions failed: no AI mode result within 77 s"
    }


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_keeps_the_ai_mode_answers_and_names_the_failed_ones(
    collector_settings: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ai_mode(monkeypatch, {1: google_ai_mode.AiModeAnswer("Antwort", [])}, {0: "Task failed"})
    reasons: dict[str, str] = {}

    answers = await assistants.ask_all(
        QUESTIONS,
        BUSINESS,
        _ai_mode_settings(collector_settings),
        _gate(Source.GOOGLE_AI, Source.DATAFORSEO)[0],
        fake_browser([]).service,
        reasons=reasons,
    )

    assert len(answers) == 1
    assert reasons == {"google_ai": "1 of 2 AI mode questions failed: Task failed"}


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_reports_missing_dataforseo_credentials_for_google_ai(
    collector_settings: Settings, fake_browser: BrowserFactory
) -> None:
    reasons: dict[str, str] = {}

    with pytest.raises(CollectionError, match="no assistant answered"):
        await assistants.ask_all(
            QUESTIONS,
            BUSINESS,
            collector_settings,
            _gate(Source.GOOGLE_AI, Source.DATAFORSEO)[0],
            fake_browser([]).service,
            reasons=reasons,
        )

    assert reasons == {"google_ai": "DataForSEO credentials are missing"}


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_sends_only_the_ai_mode_questions_the_daily_cap_allows(
    collector_settings: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_ai_mode(monkeypatch, {0: google_ai_mode.AiModeAnswer("Antwort", [])}, {})

    answers = await assistants.ask_all(
        QUESTIONS,
        BUSINESS,
        _ai_mode_settings(collector_settings),
        _gate(Source.GOOGLE_AI, Source.DATAFORSEO, cap=1)[0],
        fake_browser([]).service,
    )

    assert len(answers) == 1
    assert calls[0]["questions"] == QUESTIONS


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_switches_chatgpt_to_the_api_when_the_page_breaks_in_any_way(
    paid_keys: Settings, fake_browser: BrowserFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> object:
        raise CollectionError("the page layout changed")

    monkeypatch.setattr(assistants.assistant_pages, "ask_on_site", broken)
    respx.post(assistants.OPENAI_URL).mock(return_value=_chat("Antwort"))

    answers = await assistants.ask_all(
        QUESTIONS, BUSINESS, paid_keys, _gate(Source.CHATGPT)[0], fake_browser([]).service
    )

    assert {answer.route for answer in answers} == {"api"}
    assert len(answers) == 2


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_names_the_block_and_the_missing_key_for_chatgpt(
    collector_settings: Settings, walled_browser: FakeBrowser
) -> None:
    reasons: dict[str, str] = {}

    with pytest.raises(CollectionError):
        await assistants.ask_all(
            QUESTIONS,
            BUSINESS,
            collector_settings,
            _gate(Source.CHATGPT)[0],
            walled_browser.service,
            reasons=reasons,
        )

    assert reasons["chatgpt"].endswith(assistants.NO_PAID_KEY)
    assert "redirected" in reasons["chatgpt"]


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_books_every_paid_answer_from_the_reported_usage(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    settings = paid_keys.model_copy(update={"openai_eur_per_usd": Decimal("0.5")})
    respx.post(assistants.OPENAI_URL).mock(return_value=_chat("a"))
    ledger = Ledger()
    gate, _ = _gate(Source.CHATGPT, ledger=ledger)

    await assistants.ask_all(QUESTIONS, BUSINESS, settings, gate, walled_browser.service)

    row = (AUDIT_ID, "chatgpt", openai_price.OPENAI_SERVICE, 1, Decimal("0.000675"), 1000, 2000)
    assert ledger.audit_rows == [row, row]
    assert ledger.spent == Decimal("0.00135")


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_books_nothing_for_an_answer_the_interface_rejected(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    respx.post(assistants.OPENAI_URL).mock(return_value=httpx.Response(401))
    ledger = Ledger()
    gate, _ = _gate(Source.CHATGPT, ledger=ledger)

    with pytest.raises(CollectionError):
        await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, walled_browser.service)

    assert ledger.audit_rows == []
    assert ledger.spent == 0


@pytest.mark.usefixtures("two_repeats")
@pytest.mark.parametrize(("spent", "answered"), [("4.99", 2), ("5", 0)])
@respx.mock
async def test_ask_all_stops_the_paid_interface_at_the_monthly_limit(
    paid_keys: Settings, walled_browser: FakeBrowser, spent: str, answered: int
) -> None:
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat("a"))
    gate, _ = _gate(Source.CHATGPT, ledger=Ledger(Decimal(spent)))
    run = assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, walled_browser.service)

    if answered:
        assert len(await run) == answered
    else:
        with pytest.raises(CollectionError, match="chatgpt: monthly cost cap reached"):
            await run
    assert openai.call_count == answered


@pytest.mark.usefixtures("two_repeats")
async def test_ask_all_keeps_the_free_chatgpt_routes_when_the_monthly_limit_is_reached(
    paid_keys: Settings, fake_browser: BrowserFactory
) -> None:
    fake = fake_browser([_chatgpt_page(), _chatgpt_page()])
    gate, _ = _gate(Source.CHATGPT, ledger=Ledger(Decimal(5)))

    async with fake.service as browser, respx.mock:
        answers = await assistants.ask_all(QUESTIONS, BUSINESS, paid_keys, gate, browser)

    assert [answer.route for answer in answers] == ["browser", "browser"]


@pytest.mark.usefixtures("two_repeats")
@respx.mock
async def test_ask_all_locks_the_paid_interface_for_a_model_without_a_price(
    paid_keys: Settings, walled_browser: FakeBrowser
) -> None:
    settings = paid_keys.model_copy(update={"openai_model": "gpt-x"})
    openai = respx.post(assistants.OPENAI_URL).mock(return_value=_chat("a"))
    reasons: dict[str, str] = {}

    with pytest.raises(CollectionError):
        await assistants.ask_all(
            QUESTIONS,
            BUSINESS,
            settings,
            _gate(Source.CHATGPT)[0],
            walled_browser.service,
            reasons=reasons,
        )

    assert openai.call_count == 0
    assert reasons["chatgpt"].endswith("Preis nicht hinterlegt: gpt-x")


def test_a_model_without_a_price_is_no_paid_fallback(paid_keys: Settings) -> None:
    unpriced = paid_keys.model_copy(update={"openai_model": "gpt-x"})

    assert assistants.has_api_fallback(paid_keys, "chatgpt") is True
    assert assistants.has_api_fallback(unpriced, "chatgpt") is False
