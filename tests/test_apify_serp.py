import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from apify_fake import apify_post
from pydantic import SecretStr

from radarauditor.collectors import apify_serp, social_search
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import SourceDisabledError

WEB_URL = (
    f"https://api.apify.com/v2/acts/{social_search.GOOGLE_SEARCH_ACTOR}/run-sync-get-dataset-items"
)
IMAGE_URL = f"https://api.apify.com/v2/acts/{apify_serp.IMAGE_ACTOR}/run-sync-get-dataset-items"
PHRASE = "Abstrakte Kunst Münster"
OTHER = "Lichtkunst Münster"
BRAND = "Katja Karsch"


class Ledger:
    def __init__(self) -> None:
        self.added: list[Decimal] = []
        self.audited: list[tuple[UUID, str, str, int, Decimal]] = []

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
        self.audited.append((audit_id, source, service, calls, cost))

    def month_source_cost(self, source: str) -> Decimal:
        return sum(self.added, Decimal(0))

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        self.added.append(cost)
        return self.month_source_cost(source)


class Counter:
    def __init__(self, allowed: int = 1000) -> None:
        self.allowed = allowed
        self.used = 0

    def consume(self, source: str, cap: int) -> bool:
        self.used += 1
        return self.used <= self.allowed


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()


@pytest.fixture
def settings(collector_settings: Settings) -> Settings:
    return collector_settings.model_copy(
        update={"apify_token": SecretStr("apify-token"), "apify_eur_per_usd": Decimal(2)}
    )


def _gate(
    ledger: Ledger, allowed: int = 1000, audit_id: UUID | None = None, **switches: dict[str, Any]
) -> SourceGate:
    sources = SourcesSettings.model_validate(switches)
    return SourceGate(sources, Counter(allowed).consume, ledger, audit_id)


def _page(phrase: str, urls: list[str], page: int = 1, **extra: Any) -> dict[str, Any]:
    first = (page - 1) * 10
    return {
        "searchQuery": {"term": phrase, "page": page},
        "organicResults": [
            {"url": url, "position": first + index} for index, url in enumerate(urls, start=1)
        ],
        **extra,
    }


def _queries(request: httpx.Request) -> list[str]:
    queries: list[str] = json.loads(request.content)["queries"].split("\n")
    return queries


def _body(route: respx.Route, call: int = -1) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[call].request.content)
    return body


@respx.mock
async def test_web_search_counts_positions_over_both_pages_by_distinct_domain(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(WEB_URL).mock(
        return_value=httpx.Response(
            201,
            json=[
                _page(
                    PHRASE,
                    ["https://www.instagram.com/a", "https://instagram.com/b", "https://x.de/"],
                ),
                _page(PHRASE, ["https://www.katjakarsch.de/werke", "https://x.de/2"], page=2),
            ],
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    assert batch.failed == []
    [result] = batch.results
    assert result.organic == [(1, "instagram.com"), (2, "x.de"), (3, "katjakarsch.de")]
    assert (result.route, result.vertical, result.engine) == ("apify", "web", "google")
    assert result.local_pack == []
    assert result.knowledge_panel is False


@respx.mock
async def test_web_search_sends_all_phrases_in_one_run_with_the_german_settings(
    settings: Settings, ledger: Ledger
) -> None:
    route = apify_post(WEB_URL).mock(
        return_value=httpx.Response(
            201, json=[_page(PHRASE, ["https://a.de/"]), _page(OTHER, ["https://b.de/"])]
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE, OTHER])

    assert [result.phrase for result in batch.results] == [PHRASE, OTHER]
    assert route.call_count == 1
    assert _body(route) == {
        "queries": f"{PHRASE}\n{OTHER}",
        "maxPagesPerQuery": 3,
        "countryCode": "de",
        "languageCode": "de",
        "searchLanguage": "de",
        "focusOnPaidAds": False,
    }
    assert route.calls[0].request.headers["Authorization"] == "Bearer apify-token"


@respx.mock
async def test_web_search_reads_ads_in_a_separate_one_page_run_for_the_brand_phrase(
    settings: Settings, ledger: Ledger
) -> None:
    ads = [
        {"title": "Kunst kaufen", "directUrl": "https://www.shop.de/kunst", "description": ""},
        {"title": "Galerie", "directUrl": None, "displayedUrl": "galerie.de › kunst"},
        {
            "title": "Termine",
            "directUrl": None,
            "displayedUrl": "",
            "description": "Dental21 https://www.dental21.de › praxis        Termine",
        },
        {"title": "Nur Titel", "directUrl": None, "displayedUrl": "", "description": ""},
        {"title": "Kunst kaufen", "directUrl": "https://shop.de/other"},
    ]
    route = apify_post(WEB_URL).mock(
        side_effect=[
            httpx.Response(201, json=[_page(PHRASE, ["https://a.de/"])]),
            httpx.Response(201, json=[_page(BRAND, ["https://b.de/"], paidResults=ads)]),
        ]
    )

    batch = await apify_serp.search_web(
        settings, _gate(ledger), [PHRASE, BRAND], frozenset({BRAND})
    )

    assert _body(route, 0)["focusOnPaidAds"] is False
    assert _body(route, 0)["queries"] == PHRASE
    assert _body(route, 1) == {
        "queries": BRAND,
        "maxPagesPerQuery": 1,
        "countryCode": "de",
        "languageCode": "de",
        "searchLanguage": "de",
        "focusOnPaidAds": True,
    }
    assert [result.phrase for result in batch.results] == [PHRASE, BRAND]
    assert batch.results[1].ads == ["shop.de", "galerie.de", "dental21.de", "Nur Titel"]
    assert batch.results[0].ads == []


@respx.mock
async def test_web_search_reads_ai_overview_sources_and_questions(
    settings: Settings, ledger: Ledger
) -> None:
    overview = {
        "type": "live",
        "content": "Empfohlen wird oft Galerie Kunstraum Münster für Ausstellungen.",
        "sources": [
            {"url": "https://www.katjakarsch.de/a", "title": "Katja Karsch - Kunst"},
            {"url": "https://www.google.com/x", "title": "Google"},
        ],
    }
    questions = [{"question": "  Was ist  Lichtkunst? "}, {"question": "Was ist Lichtkunst?"}]
    apify_post(WEB_URL).mock(
        return_value=httpx.Response(
            201,
            json=[
                _page(PHRASE, ["https://a.de/"], aiOverview=overview, peopleAlsoAsk=questions),
                _page(PHRASE, ["https://b.de/"], page=2),
            ],
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    [result] = batch.results
    assert result.ai_overview_present is True
    assert result.ai_overview_mentions[:2] == ["katjakarsch.de", "Katja Karsch - Kunst"]
    assert "Galerie Kunstraum Münster" in result.ai_overview_mentions
    assert result.people_also_ask == ["Was ist Lichtkunst?"]


@respx.mock
@pytest.mark.parametrize("overview", [None, {"type": "live", "content": "", "sources": []}])
async def test_web_search_reports_no_ai_overview_when_none_or_an_empty_one_came(
    settings: Settings, ledger: Ledger, overview: dict[str, Any] | None
) -> None:
    page = _page(PHRASE, ["https://a.de/"])
    if overview is not None:
        page["aiOverview"] = overview
    apify_post(WEB_URL).mock(return_value=httpx.Response(201, json=[page]))

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    assert batch.results[0].ai_overview_present is False
    assert batch.results[0].ai_overview_mentions == []


@respx.mock
async def test_web_search_books_the_cost_the_run_reported_in_euros(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(WEB_URL, cost_usd=Decimal("0.0140")).mock(
        return_value=httpx.Response(
            201,
            json=[
                _page(PHRASE, ["https://a.de/"]),
                _page(PHRASE, ["https://b.de/"], page=2),
                _page(OTHER, ["https://c.de/"]),
                {"#error": True, "searchQuery": {"term": "Kaputt", "page": 1}},
            ],
        )
    )

    await apify_serp.search_web(settings, _gate(ledger), [PHRASE, OTHER, "Kaputt"])

    assert ledger.added == [Decimal("0.0280")]


@respx.mock
async def test_web_search_books_the_brand_run_apart_from_the_plain_run(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(WEB_URL, cost_usd=Decimal("0.011")).mock(
        side_effect=lambda request: httpx.Response(
            201, json=[_page(phrase, ["https://a.de/"]) for phrase in _queries(request)]
        )
    )

    await apify_serp.search_web(settings, _gate(ledger), [PHRASE, BRAND], frozenset({BRAND}))

    assert ledger.added == [Decimal("0.022"), Decimal("0.022")]


@respx.mock
async def test_web_search_fails_a_phrase_without_a_first_page_and_a_phrase_without_items(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(WEB_URL).mock(
        return_value=httpx.Response(
            201,
            json=[
                _page(PHRASE, ["https://a.de/"]),
                _page(OTHER, ["https://b.de/"], page=2),
                {"#error": True, "searchQuery": {"term": "Kaputt", "page": 1}},
            ],
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE, OTHER, "Kaputt"])

    assert [result.phrase for result in batch.results] == [PHRASE]
    assert batch.failed == [OTHER, "Kaputt"]
    assert batch.reason == apify_serp.NO_PAGE_REASON


@respx.mock
async def test_web_search_matches_the_returned_term_ignoring_case_and_spacing(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(WEB_URL).mock(
        return_value=httpx.Response(
            201, json=[_page("abstrakte  kunst münster", ["https://a.de/"])]
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    assert [result.phrase for result in batch.results] == [PHRASE]


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
async def test_web_search_hands_every_phrase_back_when_the_run_fails_and_books_nothing(
    settings: Settings, ledger: Ledger, status: int
) -> None:
    apify_post(WEB_URL).mock(return_value=httpx.Response(status))

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE, OTHER])

    assert batch.results == []
    assert batch.failed == [PHRASE, OTHER]
    assert f"HTTP {status}" in (batch.reason or "")
    assert ledger.added == []


@respx.mock
async def test_web_search_leaves_phrases_over_the_daily_cap_for_the_browser(
    settings: Settings, ledger: Ledger
) -> None:
    route = apify_post(WEB_URL).mock(
        return_value=httpx.Response(201, json=[_page(PHRASE, ["https://a.de/"])])
    )

    batch = await apify_serp.search_web(settings, _gate(ledger, allowed=1), [PHRASE, OTHER])

    assert _body(route)["queries"] == PHRASE
    assert [result.phrase for result in batch.results] == [PHRASE]
    assert batch.failed == [OTHER]
    assert batch.reason == apify_serp.CAP_REASON


@respx.mock
async def test_web_search_makes_no_request_once_the_monthly_cost_cap_is_reached(
    settings: Settings, ledger: Ledger
) -> None:
    ledger.added.append(Decimal(10))
    route = apify_post(WEB_URL)

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    assert not route.called
    assert batch.failed == [PHRASE]
    assert batch.reason == apify_serp.CAP_REASON


@respx.mock
@pytest.mark.parametrize(("count", "timeout"), [(1, "150"), (12, "150"), (13, "156"), (25, "300")])
async def test_web_search_gives_the_run_four_seconds_per_result_page(
    settings: Settings, ledger: Ledger, count: int, timeout: str
) -> None:
    phrases = [f"Phrase {index}" for index in range(count)]
    route = apify_post(WEB_URL).mock(
        side_effect=lambda request: httpx.Response(
            201,
            json=[
                _page(term, ["https://a.de/"])
                for term in json.loads(request.content)["queries"].split("\n")
            ],
        )
    )

    await apify_serp.search_web(settings, _gate(ledger), phrases)

    assert route.calls[0].request.url.params["timeout"] == timeout


@respx.mock
@pytest.mark.parametrize(("count", "runs"), [(25, 1), (26, 2)])
async def test_web_search_starts_a_new_run_after_twenty_five_phrases(
    settings: Settings, ledger: Ledger, count: int, runs: int
) -> None:
    phrases = [f"Phrase {index}" for index in range(count)]
    route = apify_post(WEB_URL).mock(
        side_effect=lambda request: httpx.Response(
            201,
            json=[
                _page(term, ["https://a.de/"])
                for term in json.loads(request.content)["queries"].split("\n")
            ],
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), phrases)

    assert route.call_count == runs
    assert len(batch.results) == count


@respx.mock
async def test_image_search_lists_source_domains_by_position_without_google_hosts(
    settings: Settings, ledger: Ledger
) -> None:
    items = [
        {"query": PHRASE, "position": 3, "link": "https://www.b.de/x"},
        {"query": PHRASE, "position": 1, "link": "https://www.a.de/x"},
        {"query": PHRASE, "position": 2, "link": "https://www.google.com/imgres"},
        {"query": PHRASE, "position": 4, "link": "https://b.de/y"},
        {"query": PHRASE, "position": 5, "link": None},
    ]
    route = apify_post(IMAGE_URL, cost_usd=Decimal("0.0045")).mock(
        return_value=httpx.Response(201, json=items)
    )

    batch = await apify_serp.search_images(settings, _gate(ledger), [PHRASE])

    [result] = batch.results
    assert result.organic == [(1, "a.de"), (2, "b.de")]
    assert (result.vertical, result.route) == ("images", "apify")
    assert _body(route) == {
        "queries": [PHRASE],
        "maxResultsPerQuery": 40,
        "gl": "de",
        "hl": "de",
        "reuseOlderResults": False,
    }
    assert ledger.added == [Decimal("0.0090")]


@respx.mock
@pytest.mark.parametrize(("images", "domains"), [(40, 40), (41, 40)])
async def test_image_search_reads_the_first_forty_results_only(
    settings: Settings, ledger: Ledger, images: int, domains: int
) -> None:
    items = [
        {"query": PHRASE, "position": index, "link": f"https://site{index}.de/"}
        for index in range(1, images + 1)
    ]
    apify_post(IMAGE_URL).mock(return_value=httpx.Response(201, json=items))

    batch = await apify_serp.search_images(settings, _gate(ledger), [PHRASE])

    assert len(batch.results[0].organic) == domains


@respx.mock
async def test_image_search_fails_a_phrase_without_images(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(IMAGE_URL).mock(
        return_value=httpx.Response(
            201, json=[{"query": PHRASE, "position": 1, "link": "https://a.de/"}]
        )
    )

    batch = await apify_serp.search_images(settings, _gate(ledger), [PHRASE, OTHER])

    assert [result.phrase for result in batch.results] == [PHRASE]
    assert batch.failed == [OTHER]
    assert batch.reason == apify_serp.NO_IMAGES_REASON


@respx.mock
async def test_image_search_names_the_notice_of_a_run_that_stopped_early(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(IMAGE_URL).mock(
        return_value=httpx.Response(
            201,
            json=[
                {"query": PHRASE, "position": 1, "link": "https://a.de/"},
                {"free_tier_notice": True, "notice": "free_tier_limit_reached"},
            ],
        )
    )

    batch = await apify_serp.search_images(settings, _gate(ledger), [PHRASE, OTHER])

    assert batch.failed == [OTHER]
    assert batch.reason == "Apify stopped the image search early: free_tier_limit_reached"


@respx.mock
async def test_image_search_hands_phrases_back_when_the_run_fails(
    settings: Settings, ledger: Ledger
) -> None:
    apify_post(IMAGE_URL).mock(return_value=httpx.Response(500))

    batch = await apify_serp.search_images(settings, _gate(ledger), [PHRASE])

    assert batch.failed == [PHRASE]
    assert ledger.added == []


def test_apify_is_available_only_with_a_token_and_both_switches_on(
    settings: Settings, collector_settings: Settings, ledger: Ledger
) -> None:
    assert apify_serp.is_available(settings, _gate(ledger))
    assert not apify_serp.is_available(collector_settings, _gate(ledger))
    assert not apify_serp.is_available(settings, _gate(ledger, apify={"enabled": False}))
    assert not apify_serp.is_available(settings, _gate(ledger, google_search={"enabled": False}))


@pytest.mark.parametrize(
    ("brand", "expected"),
    [
        ("katja  karsch", {BRAND}),
        (BRAND, {BRAND}),
        ("Katja Karsch Kunst", set()),
        ("", set()),
        ("   ", set()),
        (None, set()),
    ],
)
def test_brand_phrases_pick_the_phrase_equal_to_the_brand(
    brand: str | None, expected: set[str]
) -> None:
    assert apify_serp.brand_phrases([PHRASE, BRAND], brand) == expected


def test_the_apify_source_is_switched_on_with_a_monthly_cost_cap_by_default() -> None:
    setting = SourcesSettings().of(Source.APIFY)

    assert (setting.enabled, setting.daily_cap, setting.monthly_cost_cap_eur) == (
        True,
        1000,
        Decimal(10),
    )


def test_a_switched_off_apify_source_refuses_requests() -> None:
    gate = _gate(Ledger(), apify={"enabled": False})

    with pytest.raises(SourceDisabledError):
        gate.acquire(Source.APIFY)


@respx.mock
async def test_web_search_adds_the_queries_and_the_cost_to_the_statement_of_the_audit(
    settings: Settings, ledger: Ledger
) -> None:
    audit_id = uuid4()
    apify_post(WEB_URL, cost_usd=Decimal("0.009")).mock(
        side_effect=lambda request: httpx.Response(
            201, json=[_page(phrase, ["https://a.de/"]) for phrase in _queries(request)]
        )
    )

    await apify_serp.search_web(settings, _gate(ledger, audit_id=audit_id), [PHRASE, OTHER])

    assert ledger.audited == [(audit_id, "apify", "google_search", 2, Decimal("0.018"))]


@respx.mock
async def test_image_search_adds_the_queries_and_the_cost_to_the_statement_of_the_audit(
    settings: Settings, ledger: Ledger
) -> None:
    audit_id = uuid4()
    apify_post(IMAGE_URL, cost_usd=Decimal("0.002")).mock(
        return_value=httpx.Response(
            201, json=[{"query": PHRASE, "position": 1, "link": "https://a.de/"}]
        )
    )

    await apify_serp.search_images(settings, _gate(ledger, audit_id=audit_id), [PHRASE])

    assert ledger.audited == [(audit_id, "apify", "google_images", 1, Decimal("0.004"))]


@respx.mock
async def test_a_run_without_a_reported_cost_fails_its_phrases_instead_of_booking_nothing(
    settings: Settings, ledger: Ledger
) -> None:
    route = apify_post(WEB_URL)
    route.mock(return_value=httpx.Response(201, json=[_page(PHRASE, ["https://a.de/"])]))
    respx.get(url__regex=r".*/actor-runs/.*").mock(
        return_value=httpx.Response(
            200, json={"data": {"id": "x", "status": "SUCCEEDED", "defaultDatasetId": "d"}}
        )
    )

    batch = await apify_serp.search_web(settings, _gate(ledger), [PHRASE])

    assert batch.failed == [PHRASE]
    assert "reports no cost" in (batch.reason or "")
    assert ledger.added == []
