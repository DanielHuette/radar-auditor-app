import asyncio
import logging
import string
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic.alias_generators import to_camel

from radarauditor.collectors import (
    assistant_handoff,
    assistant_pages,
    chatgpt_scraper,
    google_ai_mode,
    openai_price,
    paid_rankings,
    raw_answers,
)
from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.mention import mentions_business
from radarauditor.collectors.patterns import find_urls
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import AnswerRoute, AssistantAnswer, PhaseTiming
from radarauditor.collectors.transport import send
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import (
    ClaudeCodeError,
    ClaudeCodeFailure,
    CollectionError,
    ConfigurationError,
    LoginWallError,
    SearchBlockedError,
    TransientCollectionError,
)
from radarauditor.jobs import claude_code

logger = logging.getLogger(__name__)

PROVIDER_SOURCES: dict[str, Source] = {
    "chatgpt": Source.CHATGPT,
    "google_ai": Source.GOOGLE_AI,
    "claude": Source.CLAUDE,
}
LEGACY_PROVIDER_SOURCES: dict[str, Source] = {"openai": Source.CHATGPT}
CLAUDE = "claude"
CHATGPT = "chatgpt"
GOOGLE_AI = "google_ai"
NO_PAID_KEY = "no paid interface key is set to take over"
NO_PRICE = "Preis nicht hinterlegt"
DAILY_CAP_REASON = "daily cap reached"
MONTHLY_CAP_REASON = "monthly cost cap reached"
DATAFORSEO_MISSING = "DataForSEO credentials are missing"
HANDOFF_REPEATS = assistant_handoff.HANDOFF_REPEATS
REPEATS_PER_QUESTION = 10
CONCURRENT_REQUESTS = 4
REQUEST_TIMEOUT_SECONDS = 60
BROWSER_FAILURES_BEFORE_FALLBACK = 2
CLAUDE_CODE_UNAVAILABLE = frozenset(
    {ClaudeCodeFailure.NOT_INSTALLED, ClaudeCodeFailure.NOT_LOGGED_IN}
)
TEMPLATE_PLACEHOLDERS = frozenset({"industry", "city", "service"})

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class QuestionTemplates(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    recommendation: list[str] = Field(alias="empfehlung", min_length=1)
    problem: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class _Reply:
    text: str
    citations: list[str]
    tokens_in: int = 0
    tokens_out: int = 0


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class _UrlCitation(_ApiModel):
    url: str


class _Annotation(_ApiModel):
    url_citation: _UrlCitation | None = None


class _ChatMessage(_ApiModel):
    content: str | None = None
    annotations: list[_Annotation] = []


class _ChatChoice(_ApiModel):
    message: _ChatMessage


class _ChatUsage(_ApiModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class _ChatResponse(_ApiModel):
    choices: list[_ChatChoice]
    usage: _ChatUsage


Asker = Callable[[httpx.AsyncClient, str, SecretStr, str], Awaitable[_Reply]]


@dataclass(frozen=True)
class Handoff:
    browser_for: Callable[[str], BrowserService]
    notify: Callable[[str], None]
    manual: frozenset[str] = frozenset()


def provider_source(provider: str) -> Source | None:
    return PROVIDER_SOURCES.get(provider) or LEGACY_PROVIDER_SOURCES.get(provider)


def has_api_fallback(settings: Settings, provider: str) -> bool:
    return not isinstance(_paid_route(settings, provider), str)


def enabled_providers(settings: Settings, gate: SourceGate) -> list[str]:
    return [name for name, source in PROVIDER_SOURCES.items() if gate.is_enabled(source)]


async def ask_all(
    questions: list[str],
    business_name: str,
    settings: Settings,
    gate: SourceGate,
    browser: BrowserService,
    browser_for: Callable[[str], BrowserService] | None = None,
    timings: list[PhaseTiming] | None = None,
    handoff: Handoff | None = None,
    reasons: dict[str, str] | None = None,
    skip: frozenset[str] = frozenset(),
    repeats: int | None = None,
    domain: str | None = None,
) -> list[AssistantAnswer]:
    providers = [name for name in enabled_providers(settings, gate) if name not in skip]
    if not providers or not questions:
        return []
    calls = [question for question in questions for _ in range(repeats or REPEATS_PER_QUESTION)]
    with tempfile.TemporaryDirectory(prefix="radar-claude-") as empty_dir:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            async with AsyncExitStack() as stack:
                own_browsers = {
                    provider: [
                        await stack.enter_async_context(browser_for(name))
                        for name in _window_names(provider, settings)
                    ]
                    for provider in providers
                    if browser_for is not None and provider not in (CLAUDE, GOOGLE_AI)
                }
                run = _Run(
                    business_name,
                    settings,
                    gate,
                    _Tools(browser, client, Path(empty_dir), own_browsers, handoff),
                    domain,
                )
                grouped = await asyncio.gather(
                    *(_timed(run, provider, calls, timings) for provider in providers)
                )
    answers = [answer for group in grouped for answer in group]
    if reasons is not None:
        reasons.update(run.reasons)
    if not answers:
        raise CollectionError(f"no assistant answered: {run.last_error}")
    return answers


async def probe(
    question: str,
    settings: Settings,
    gate: SourceGate,
    browser_for: Callable[[str], BrowserService],
) -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory(prefix="radar-claude-") as empty_dir:
        for provider in enabled_providers(settings, gate):
            if provider == GOOGLE_AI or (provider == CHATGPT and _scraper_ready(settings, gate)):
                configured = paid_rankings.is_configured(settings)
                results[provider] = None if configured else DATAFORSEO_MISSING
                continue
            if not gate.try_acquire(PROVIDER_SOURCES[provider], counts_cost=False):
                results[provider] = DAILY_CAP_REASON
                continue
            results[provider] = await _probe_one(
                provider, question, settings, browser_for, Path(empty_dir)
            )
    return results


async def _probe_one(
    provider: str,
    question: str,
    settings: Settings,
    browser_for: Callable[[str], BrowserService],
    empty_dir: Path,
) -> str | None:
    try:
        if provider == CLAUDE:
            executable = claude_code.resolve_executable(settings)
            command = claude_code.assistant_command(executable, question, settings.claude_model)
            text = await asyncio.to_thread(
                claude_code.run_headless, command, empty_dir, settings.assistant_timeout_seconds
            )
            return None if text.strip() else "Claude Code gave an empty answer"
        async with browser_for(provider) as browser:
            await assistant_pages.ask_on_site(
                browser,
                assistant_pages.SITES[provider],
                question,
                settings.assistant_timeout_seconds,
            )
    except (ClaudeCodeError, CollectionError, PlaywrightError) as error:
        logger.warning("%s: probe question failed: %s", provider, error)
        return str(error) or type(error).__name__
    return None


def _scraper_ready(settings: Settings, gate: SourceGate) -> bool:
    return paid_rankings.is_configured(settings) and gate.is_enabled(Source.DATAFORSEO)


def _window_names(provider: str, settings: Settings) -> list[str]:
    count = settings.chatgpt_windows if provider == CHATGPT else 1
    return [provider, *(f"{provider}-{number}" for number in range(2, count + 1))]


async def _timed(
    run: "_Run", provider: str, calls: list[str], timings: list[PhaseTiming] | None
) -> list[AssistantAnswer]:
    started = time.monotonic()
    answers = await run.provider_answers(provider, calls)
    seconds = round(time.monotonic() - started, 1)
    logger.info("%s: %d of %d answers in %.1f s", provider, len(answers), len(calls), seconds)
    if timings is not None:
        phase = f"assistant:{provider}"
        timings.append(PhaseTiming(phase=phase, seconds=seconds, answers=len(answers)))
    return answers


@dataclass(frozen=True)
class _Tools:
    browser: BrowserService
    client: httpx.AsyncClient
    claude_dir: Path
    own_browsers: dict[str, list[BrowserService]]
    handoff: Handoff | None = None


class _Run:
    def __init__(
        self,
        business_name: str,
        settings: Settings,
        gate: SourceGate,
        tools: _Tools,
        domain: str | None = None,
    ) -> None:
        self.business_name = business_name
        self.domain = domain
        self.settings = settings
        self.gate = gate
        self.tools = tools
        self.paid_slots = asyncio.Semaphore(CONCURRENT_REQUESTS)
        self.claude_slots = asyncio.Semaphore(settings.claude_parallel_questions)
        self.rejected: set[str] = set()
        self.browsers_used: set[int] = set()
        self.claude_executable: str | None = None
        self.last_error = "no call was made"
        self.reasons: dict[str, str] = {}

    async def provider_answers(self, provider: str, calls: list[str]) -> list[AssistantAnswer]:
        if provider == CLAUDE:
            return _present(await asyncio.gather(*(self._claude(question) for question in calls)))
        if provider == GOOGLE_AI:
            return await self._ai_mode(calls)
        answers: list[AssistantAnswer] = []
        if provider == CHATGPT:
            answers, calls = await self._scraped(calls)
            if not calls:
                return answers
        handoff = self.tools.handoff
        if handoff is not None and provider in handoff.manual:
            return answers + await self._with_person(handoff, provider, calls)
        remaining = await self._browser_answers(provider, calls, answers)
        if remaining and handoff is not None and not has_api_fallback(self.settings, provider):
            return answers + await self._with_person(handoff, provider, remaining)
        fallback = await asyncio.gather(*(self._paid(provider, question) for question in remaining))
        return answers + _present(fallback)

    async def _ai_mode(self, calls: list[str]) -> list[AssistantAnswer]:
        try:
            client = paid_rankings.DataForSeoClient(self.settings, self.gate)
        except CollectionError as error:
            self._reject(GOOGLE_AI, error)
            return []
        allowed = [question for question in calls if self._acquire(GOOGLE_AI)]
        if not allowed:
            return []
        batch = await google_ai_mode.ask_ai_mode(
            client,
            allowed,
            timeout_seconds=self.settings.ai_mode_timeout_seconds,
            poll_seconds=self.settings.ai_mode_poll_seconds,
        )
        if batch.failed:
            first = batch.failed[min(batch.failed)]
            self._note(
                GOOGLE_AI, f"{len(batch.failed)} of {len(allowed)} AI mode questions failed", first
            )
        return [
            self._answer(
                GOOGLE_AI, allowed[index], _Reply(answer.text, answer.citations), "dataforseo"
            )
            for index, answer in sorted(batch.answers.items())
        ]

    async def _scraped(self, calls: list[str]) -> tuple[list[AssistantAnswer], list[str]]:
        if not _scraper_ready(self.settings, self.gate):
            return [], calls
        client = paid_rankings.DataForSeoClient(self.settings, self.gate)
        allowed = [question for question in calls if self._acquire(CHATGPT)]
        if not allowed:
            return [], []
        batch = await chatgpt_scraper.ask_chatgpt(client, allowed)
        if batch.failed:
            first = batch.failed[min(batch.failed)]
            self._note(
                CHATGPT,
                f"DataForSEO answered {len(batch.answers)} of {len(allowed)} questions, "
                "the browser takes the rest",
                first,
            )
        answers = [
            self._answer(
                CHATGPT, allowed[index], _Reply(answer.text, answer.citations), "dataforseo"
            )
            for index, answer in sorted(batch.answers.items())
        ]
        return answers, [allowed[index] for index in sorted(batch.failed)]

    async def _with_person(
        self, handoff: Handoff, provider: str, calls: list[str]
    ) -> list[AssistantAnswer]:
        questions = list(dict.fromkeys(calls))
        site = assistant_pages.SITES[provider]
        handoff.notify(
            f"Sichtbarkeitsaudit: {provider} ist automatisch gesperrt. "
            f"{len(questions) * HANDOFF_REPEATS} Fragen warten nacheinander in einem "
            "Browserfenster auf dem PC; bitte jeweils Senden drücken."
        )
        answers: list[AssistantAnswer] = []
        async with handoff.browser_for(provider) as browser:
            for question in questions:
                for _ in range(HANDOFF_REPEATS):
                    if not self._acquire(provider):
                        return answers
                    try:
                        page = await assistant_handoff.ask_with_person(
                            browser, site, question, self.settings.handoff_timeout_seconds
                        )
                    except (TransientCollectionError, PlaywrightError) as error:
                        self._note(provider, "fallback with a person ended", error)
                        return answers
                    reply = _Reply(page.text, page.sources)
                    answers.append(self._answer(provider, question, reply, "manual"))
        return answers

    async def _browser_answers(
        self, provider: str, calls: list[str], answers: list[AssistantAnswer]
    ) -> list[str]:
        site = assistant_pages.SITES[provider]
        browsers = self.tools.own_browsers.get(provider) or [self.tools.browser]
        lanes = [calls[index :: len(browsers)] for index in range(len(browsers))]
        left = await asyncio.gather(
            *(
                self._lane(site, lane, browser, answers)
                for lane, browser in zip(lanes, browsers, strict=True)
            )
        )
        return [question for part in left for question in part]

    async def _lane(
        self,
        site: assistant_pages.AssistantSite,
        calls: list[str],
        browser: BrowserService,
        answers: list[AssistantAnswer],
    ) -> list[str]:
        provider = site.provider
        failures = 0
        for index, question in enumerate(calls):
            if not self._acquire(provider):
                return []
            try:
                answers.append(await self._ask_page(site, question, browser))
                failures = 0
            except (LoginWallError, SearchBlockedError) as error:
                self._note(provider, "browser blocked for the rest of the run", error)
                return calls[index:]
            except (CollectionError, PlaywrightError) as error:
                failures += 1
                self._note(provider, "browser gave no answer", error)
                if failures >= BROWSER_FAILURES_BEFORE_FALLBACK:
                    return calls[index:]
                fallback = await self._paid(provider, question)
                if fallback is not None:
                    answers.append(fallback)
        return []

    async def _ask_page(
        self, site: assistant_pages.AssistantSite, question: str, browser: BrowserService
    ) -> AssistantAnswer:
        if id(browser) in self.browsers_used:
            await browser.pause()
        self.browsers_used.add(id(browser))
        page = await assistant_pages.ask_on_site(
            browser, site, question, self.settings.assistant_timeout_seconds
        )
        return self._answer(site.provider, question, _Reply(page.text, page.sources), "browser")

    async def _paid(self, provider: str, question: str) -> AssistantAnswer | None:
        async with self.paid_slots:
            route = _paid_route(self.settings, provider)
            if isinstance(route, str):
                self._add_reason(provider, route)
                return None
            if provider in self.rejected:
                return None
            if not self._acquire(provider, paid=True):
                return None
            try:
                reply = await _ASKERS[provider](
                    self.tools.client,
                    question,
                    route.key,
                    _provider_model(self.settings, provider),
                )
            except TransientCollectionError as error:
                self._note(provider, "paid interface gave no answer", error)
                return None
            except CollectionError as error:
                self._reject(provider, error)
                return None
            self._book_paid_cost(provider, route.price, reply)
        return self._answer(provider, question, reply, "api")

    def _book_paid_cost(self, provider: str, price: openai_price.TokenPrice, reply: _Reply) -> None:
        cost_usd = openai_price.call_cost_usd(price, reply.tokens_in, reply.tokens_out)
        self.gate.record_cost(
            PROVIDER_SOURCES[provider],
            cost_usd * self.settings.openai_eur_per_usd,
            service=openai_price.OPENAI_SERVICE,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
        )

    async def _claude(self, question: str) -> AssistantAnswer | None:
        async with self.claude_slots:
            if CLAUDE in self.rejected or not self._acquire(CLAUDE):
                return None
            try:
                text = await asyncio.to_thread(self._run_claude_code, question)
            except ClaudeCodeError as error:
                if error.failure in CLAUDE_CODE_UNAVAILABLE:
                    self._reject(CLAUDE, error)
                else:
                    self._note(CLAUDE, "Claude Code gave no answer", error)
                return None
        if not text.strip():
            self._note(CLAUDE, "Claude Code gave no answer", "empty result")
            return None
        return self._answer(CLAUDE, question, _Reply(text, []), "claude_code")

    def _run_claude_code(self, question: str) -> str:
        if self.claude_executable is None:
            self.claude_executable = claude_code.resolve_executable(self.settings)
        command = claude_code.assistant_command(
            self.claude_executable, question, self.settings.claude_model
        )
        return claude_code.run_headless(
            command, self.tools.claude_dir, self.settings.assistant_timeout_seconds
        )

    def _acquire(self, provider: str, *, paid: bool = False) -> bool:
        source = PROVIDER_SOURCES[provider]
        if self.gate.try_acquire(source, counts_cost=paid):
            return True
        cost_capped = paid and source in self.gate.cost_capped
        reason = MONTHLY_CAP_REASON if cost_capped else DAILY_CAP_REASON
        self.last_error = f"{provider}: {reason}"
        self.reasons.setdefault(provider, reason)
        return False

    def _note(self, provider: str, what: str, error: object) -> None:
        logger.warning("%s: %s: %s", provider, what, error)
        self.last_error = f"{provider}: {error}"
        self.reasons.setdefault(provider, f"{what}: {error}")

    def _add_reason(self, provider: str, reason: str) -> None:
        known = self.reasons.get(provider)
        if known is None:
            self.reasons[provider] = reason
        elif reason not in known:
            self.reasons[provider] = f"{known}; {reason}"

    def _reject(self, provider: str, error: Exception) -> None:
        logger.warning("%s disabled for this run: %s", provider, error)
        self.rejected.add(provider)
        self.last_error = f"{provider}: {error}"
        self.reasons.setdefault(provider, str(error))

    def _answer(
        self, provider: str, question: str, reply: _Reply, route: AnswerRoute
    ) -> AssistantAnswer:
        raw_answers.keep(raw_answers.file_name("assistants", provider, route, "txt"), reply.text)
        return AssistantAnswer(
            provider=provider,
            question=question,
            mentioned=mentions_business(
                reply.text, reply.citations, self.business_name, self.domain
            ),
            cited_sources=_merge_sources(find_urls(reply.text), reply.citations),
            answer_excerpt=reply.text,
            route=route,
        )


def _present(answers: list[AssistantAnswer | None]) -> list[AssistantAnswer]:
    return [answer for answer in answers if answer is not None]


def load_question_templates(path: Path) -> QuestionTemplates:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        templates = QuestionTemplates.model_validate(document)
    except OSError as error:
        raise ConfigurationError(f"Cannot read question templates {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(
            f"Question templates {path} are not valid YAML: {error}"
        ) from error
    except ValidationError as error:
        raise ConfigurationError(f"Question templates {path} are malformed: {error}") from error
    for template in [*templates.recommendation, *templates.problem]:
        _check_placeholders(template, path)
    return templates


def _check_placeholders(template: str, path: Path) -> None:
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError as error:
        raise ConfigurationError(f"{path}: template {template!r} is malformed") from error
    unknown = names - TEMPLATE_PLACEHOLDERS
    if unknown:
        raise ConfigurationError(
            f"{path}: template {template!r} uses unknown placeholders {sorted(unknown)}"
        )


def trade_from_keyword(phrase: str, city: str) -> str | None:
    city_words = {word.casefold() for word in city.split()}
    words = [word for word in phrase.split() if word.casefold() not in city_words]
    trade = " ".join(words).strip(" ,")
    return trade or None


def build_questions(
    industry: str, city: str, services: list[str], templates: QuestionTemplates
) -> list[str]:
    industry = _capitalised(industry)
    topics = [_capitalised(service) for service in services if service.strip()]
    topics = topics[: len(templates.problem)] or [industry]
    recommendations = [
        template.format(industry=industry, city=city, service=topics[0])
        for template in templates.recommendation
    ]
    problems = [
        template.format(industry=industry, city=city, service=topics[index % len(topics)])
        for index, template in enumerate(templates.problem)
    ]
    return recommendations + problems


def _capitalised(text: str) -> str:
    stripped = text.strip()
    return stripped[:1].upper() + stripped[1:]


async def _ask_openai(
    client: httpx.AsyncClient, question: str, key: SecretStr, model: str
) -> _Reply:
    payload = await _post_json(
        client,
        OPENAI_URL,
        "OpenAI",
        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": question}],
            "max_completion_tokens": openai_price.MAX_OUTPUT_TOKENS,
        },
    )
    response = _validate(_ChatResponse, payload, "OpenAI")
    message = _first_message(response, "OpenAI")
    citations = [item.url_citation.url for item in message.annotations if item.url_citation]
    return _Reply(
        message.content or "",
        citations,
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
    )


_ASKERS: dict[str, Asker] = {"chatgpt": _ask_openai}


def _provider_key(settings: Settings, provider: str) -> SecretStr | None:
    keys = {"chatgpt": settings.openai_api_key}
    key = keys.get(provider)
    return key if key is not None and key.get_secret_value().strip() else None


def _provider_model(settings: Settings, provider: str) -> str:
    models = {"chatgpt": settings.openai_model}
    return models[provider]


@dataclass(frozen=True)
class _PaidRoute:
    key: SecretStr
    price: openai_price.TokenPrice


def _paid_route(settings: Settings, provider: str) -> _PaidRoute | str:
    key = _provider_key(settings, provider)
    if key is None:
        return NO_PAID_KEY
    model = _provider_model(settings, provider)
    price = openai_price.load_token_price(settings.routes_path, model)
    if price is None:
        return f"{NO_PRICE}: {model}"
    return _PaidRoute(key, price)


async def _post_json(
    client: httpx.AsyncClient, url: str, source: str, **request_arguments: Any
) -> object:
    response = await send(client, "POST", url, source=source, **request_arguments)
    try:
        payload: object = response.json()
    except ValueError as error:
        raise CollectionError(f"{source} answer is not JSON") from error
    return payload


def _validate[ModelT: BaseModel](model: type[ModelT], payload: object, source: str) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise CollectionError(f"{source} answer is malformed") from error


def _first_message(response: _ChatResponse, source: str) -> _ChatMessage:
    if not response.choices:
        raise TransientCollectionError(f"{source} returned no choice")
    return response.choices[0].message


def _merge_sources(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for url in group:
            if url not in merged:
                merged.append(url)
    return merged
