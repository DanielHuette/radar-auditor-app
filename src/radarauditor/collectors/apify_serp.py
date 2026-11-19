import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from radarauditor.collectors.apify import CostSink, run_actor
from radarauditor.collectors.paid_rankings import PaidSearchBatch
from radarauditor.collectors.patterns import collapse_whitespace, site_key
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.serp import (
    GOOGLE_HOST_PATTERN,
    IMAGE_RESULT_LIMIT,
    MAX_AI_MENTIONS,
    ai_overview_names,
)
from radarauditor.collectors.snapshot import SerpResult
from radarauditor.collectors.social_search import (
    GOOGLE_SEARCH_ACTOR,
    SEARCH_COUNTRY,
    SEARCH_LANGUAGE,
)
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import CollectionError

logger = logging.getLogger(__name__)

IMAGE_ACTOR = "johnvc~google-images-api"
WEB_PAGES = 3
AD_PAGES = 1
RUN_PHRASE_LIMIT = 25
RUN_TIMEOUT_SECONDS = 150
SECONDS_PER_RESULT_PAGE = 4
WEB_SERVICE = "google_search"
IMAGE_SERVICE = "google_images"
AD_URL_PATTERN = re.compile(r"https?://[^\s›]+")
CAP_REASON = "Apify daily or monthly cap reached"
NO_PAGE_REASON = "Apify returned no result page"
NO_IMAGES_REASON = "Apify returned no image results"

type Answers = dict[str, SerpResult | str]


def is_available(settings: Settings, gate: SourceGate) -> bool:
    return (
        settings.apify_token is not None
        and gate.is_enabled(Source.APIFY)
        and gate.is_enabled(Source.GOOGLE_SEARCH)
    )


def brand_phrases(phrases: list[str], brand: str | None) -> frozenset[str]:
    if brand is None or not brand.strip():
        return frozenset()
    wanted = _key(brand)
    return frozenset(phrase for phrase in phrases if _key(phrase) == wanted)


async def search_web(
    settings: Settings,
    gate: SourceGate,
    phrases: list[str],
    ad_phrases: frozenset[str] = frozenset(),
    pages: int = WEB_PAGES,
) -> PaidSearchBatch:
    plain = [phrase for phrase in phrases if phrase not in ad_phrases]
    with_ads = [phrase for phrase in phrases if phrase in ad_phrases]
    answers: Answers = {}
    async with httpx.AsyncClient() as client:
        for group, group_pages, ads in ((plain, pages, False), (with_ads, AD_PAGES, True)):
            for chunk in _chunks(group):
                answers.update(await _web_run(client, settings, gate, chunk, group_pages, ads))
    return _batch(phrases, answers)


async def search_images(
    settings: Settings, gate: SourceGate, phrases: list[str]
) -> PaidSearchBatch:
    answers: Answers = {}
    async with httpx.AsyncClient() as client:
        for chunk in _chunks(phrases):
            answers.update(await _image_run(client, settings, gate, chunk))
    return _batch(phrases, answers)


def _book(settings: Settings, gate: SourceGate, service: str, calls: int) -> CostSink:
    def book(cost_usd: Decimal) -> None:
        gate.record_cost(
            Source.APIFY, cost_usd * settings.apify_eur_per_usd, service=service, calls=calls
        )

    return book


def _run_timeout(phrases: int, pages: int) -> int:
    return max(RUN_TIMEOUT_SECONDS, phrases * pages * SECONDS_PER_RESULT_PAGE)


def _chunks(phrases: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(phrases), RUN_PHRASE_LIMIT):
        yield phrases[start : start + RUN_PHRASE_LIMIT]


def _split_by_cap(gate: SourceGate, chunk: list[str]) -> tuple[list[str], Answers]:
    granted = gate.acquire_up_to(Source.APIFY, len(chunk))
    return chunk[:granted], {phrase: CAP_REASON for phrase in chunk[granted:]}


def _key(phrase: str) -> str:
    return collapse_whitespace(phrase).casefold()


async def _web_run(
    client: httpx.AsyncClient,
    settings: Settings,
    gate: SourceGate,
    chunk: list[str],
    pages: int,
    with_ads: bool,
) -> Answers:
    allowed, answers = _split_by_cap(gate, chunk)
    if not allowed or settings.apify_token is None:
        return answers
    payload = {
        "queries": "\n".join(allowed),
        "maxPagesPerQuery": pages,
        "countryCode": SEARCH_COUNTRY,
        "languageCode": SEARCH_LANGUAGE,
        "searchLanguage": SEARCH_LANGUAGE,
        "focusOnPaidAds": with_ads,
    }
    try:
        items = await run_actor(
            client,
            settings.apify_token,
            GOOGLE_SEARCH_ACTOR,
            payload,
            _run_timeout(len(allowed), pages),
            _book(settings, gate, WEB_SERVICE, len(allowed)),
            step="search_google",
        )
    except CollectionError as error:
        return {**answers, **{phrase: str(error) for phrase in allowed}}
    pages_by_phrase: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if _is_result_page(item):
            pages_by_phrase.setdefault(_key(item["searchQuery"]["term"]), []).append(item)
    for phrase in allowed:
        found = sorted(pages_by_phrase.get(_key(phrase), []), key=_page_number)
        if found and _page_number(found[0]) == 1:
            answers[phrase] = _serp_from_pages(phrase, found)
        else:
            answers[phrase] = NO_PAGE_REASON
    return answers


async def _image_run(
    client: httpx.AsyncClient, settings: Settings, gate: SourceGate, chunk: list[str]
) -> Answers:
    allowed, answers = _split_by_cap(gate, chunk)
    if not allowed or settings.apify_token is None:
        return answers
    payload = {
        "queries": allowed,
        "maxResultsPerQuery": IMAGE_RESULT_LIMIT,
        "gl": SEARCH_COUNTRY,
        "hl": SEARCH_LANGUAGE,
        "reuseOlderResults": False,
    }
    try:
        items = await run_actor(
            client,
            settings.apify_token,
            IMAGE_ACTOR,
            payload,
            RUN_TIMEOUT_SECONDS,
            _book(settings, gate, IMAGE_SERVICE, len(allowed)),
            step="search_images",
        )
    except CollectionError as error:
        return {**answers, **{phrase: str(error) for phrase in allowed}}
    images_by_phrase: dict[str, list[dict[str, Any]]] = {}
    missing_reason = NO_IMAGES_REASON
    for item in items:
        query = item.get("query")
        notice = item.get("notice")
        if isinstance(query, str):
            images_by_phrase.setdefault(_key(query), []).append(item)
        elif isinstance(notice, str):
            missing_reason = f"Apify stopped the image search early: {notice}"
    for phrase in allowed:
        found = images_by_phrase.get(_key(phrase), [])
        answers[phrase] = _image_serp(phrase, found) if found else missing_reason
    return answers


def _batch(phrases: list[str], answers: Answers) -> PaidSearchBatch:
    results = [answer for phrase in phrases if isinstance(answer := answers[phrase], SerpResult)]
    failures = [
        (phrase, answer) for phrase in phrases if isinstance(answer := answers[phrase], str)
    ]
    if failures:
        logger.warning(
            "Apify could not search %d of %d phrases: %s",
            len(failures),
            len(phrases),
            failures[0][1],
        )
    return PaidSearchBatch(
        results, [phrase for phrase, _ in failures], failures[0][1] if failures else None
    )


def _is_result_page(item: dict[str, Any]) -> bool:
    query = item.get("searchQuery")
    return (
        not item.get("#error")
        and isinstance(query, dict)
        and isinstance(query.get("term"), str)
        and isinstance(item.get("organicResults"), list)
    )


def _page_number(item: dict[str, Any]) -> int:
    page = item["searchQuery"].get("page")
    return page if isinstance(page, int) else 1


def _serp_from_pages(phrase: str, pages: list[dict[str, Any]]) -> SerpResult:
    results = [r for page in pages for r in page["organicResults"] if isinstance(r, dict)]
    overview = next((page["aiOverview"] for page in pages if _has_overview(page)), None)
    return SerpResult(
        phrase=phrase,
        organic=list(enumerate(_distinct_domains(r.get("url") for r in results), start=1)),
        local_pack=[],
        ai_overview_present=overview is not None,
        ai_overview_mentions=_overview_mentions(overview) if overview is not None else [],
        people_also_ask=_questions(pages),
        measured_at=datetime.now(UTC),
        ads=_advertisers(pages),
        route="apify",
    )


def _image_serp(phrase: str, images: list[dict[str, Any]]) -> SerpResult:
    ranked = sorted(images, key=lambda image: _int(image.get("position")))[:IMAGE_RESULT_LIMIT]
    return SerpResult(
        phrase=phrase,
        organic=list(enumerate(_distinct_domains(image.get("link") for image in ranked), start=1)),
        local_pack=[],
        ai_overview_present=False,
        ai_overview_mentions=[],
        people_also_ask=[],
        measured_at=datetime.now(UTC),
        vertical="images",
        route="apify",
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _host(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    host = urlsplit(url).hostname
    if not host or GOOGLE_HOST_PATTERN.search(host):
        return None
    return site_key(host)


def _distinct_domains(urls: Iterable[object]) -> list[str]:
    domains: list[str] = []
    for url in urls:
        host = _host(url)
        if host is not None and host not in domains:
            domains.append(host)
    return domains


def _has_overview(page: dict[str, Any]) -> bool:
    overview = page.get("aiOverview")
    return isinstance(overview, dict) and bool(overview.get("content") or overview.get("sources"))


def _overview_mentions(overview: dict[str, Any]) -> list[str]:
    sources = [s for s in overview.get("sources") or [] if isinstance(s, dict)]
    names = [host for source in sources if (host := _host(source.get("url"))) is not None]
    names.extend(
        collapse_whitespace(source["title"])
        for source in sources
        if isinstance(source.get("title"), str)
    )
    content = overview.get("content")
    if isinstance(content, str):
        names.extend(ai_overview_names(content))
    return [name for name in dict.fromkeys(names) if name][:MAX_AI_MENTIONS]


def _questions(pages: list[dict[str, Any]]) -> list[str]:
    questions = [
        collapse_whitespace(entry["question"])
        for page in pages
        for entry in page.get("peopleAlsoAsk") or []
        if isinstance(entry, dict) and isinstance(entry.get("question"), str)
    ]
    return [question for question in dict.fromkeys(questions) if question]


def _advertisers(pages: list[dict[str, Any]]) -> list[str]:
    advertisers = [
        advertiser
        for page in pages
        for ad in page.get("paidResults") or []
        if isinstance(ad, dict) and (advertiser := _advertiser(ad)) is not None
    ]
    return list(dict.fromkeys(advertisers))


def _advertiser(ad: dict[str, Any]) -> str | None:
    for field in ("directUrl", "displayedUrl"):
        value = ad.get(field)
        words = value.split() if isinstance(value, str) else []
        if words:
            host = _host(words[0] if "://" in words[0] else f"https://{words[0]}")
            if host is not None:
                return host
    description = ad.get("description")
    if isinstance(description, str):
        match = AD_URL_PATTERN.search(description)
        if match is not None and (host := _host(match.group(0))) is not None:
            return host
    title = ad.get("title")
    return collapse_whitespace(title) or None if isinstance(title, str) else None
