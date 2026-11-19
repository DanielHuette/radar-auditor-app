import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from conftest import keep_files_in_memory
from pydantic import HttpUrl, SecretStr
from test_feedback import Places

from radarauditor.collectors import (
    access,
    ads,
    apify_serp,
    assistants,
    dataforseo_maps,
    directories,
    estimate,
    geocode,
    google_auth,
    legal_notice,
    listings,
    maps,
    openai_price,
    pagespeed,
    paid_data,
    paid_rankings,
    places,
    run,
    serp,
    social,
    wayback,
    website,
)
from radarauditor.collectors.browser import BrowserService, Geolocation
from radarauditor.collectors.maps import ProfileTarget
from radarauditor.collectors.maps_pages import MapListing
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import (
    AccessSnapshot,
    AssistantAnswer,
    BusinessFacts,
    DeferredSearch,
    DirectoryHit,
    GoogleAdsSnapshot,
    MetaAdsSnapshot,
    PageSpeedLab,
    PageSpeedSnapshot,
    PaidSnapshot,
    PhaseTiming,
    PlaceSnapshot,
    SerpResult,
    WebsiteSnapshot,
)
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.db.models import (
    Audit,
    AuditKind,
    AuditStatus,
    CheckOutcome,
    Competitor,
    Customer,
    CustomerAccess,
    Keyword,
    KeywordScope,
    Location,
)
from radarauditor.errors import (
    AuditHalted,
    AuthorizationError,
    CollectionError,
    ConfigurationError,
    PreflightError,
    ProfileNotFoundError,
    SearchBlockedError,
    StorageError,
    TransientCollectionError,
)
from radarauditor.feedback.events import AuditEvent, Result
from radarauditor.feedback.hub import Feedback
from radarauditor.feedback.routes import load_route_map
from radarauditor.scoring.catalog import load_catalogue
from radarauditor.scoring.evaluator import evaluate

CUSTOMER_ID = uuid4()
AUDIT_ID = uuid4()
SITE = HttpUrl("https://www.dachdecker-hollenbeck.de/")
CATALOGUE = Path(__file__).parents[1] / "config" / "verzeichnisse.yaml"
SWITCHED_OFF_COLLECTORS = (
    "check_directories",
    "search_through_google",
    "first_capture",
    "collect_listings",
    "collect_social",
)


def _customer(website_url: HttpUrl | None = SITE) -> Customer:
    return Customer(
        id=CUSTOMER_ID,
        name="Dachdeckerei Hollenbeck GmbH",
        website=website_url,
        industry="Dachdecker",
        city="Münster",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _location(place_id: str | None = None) -> Location:
    return Location(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        name="Dachdeckerei Hollenbeck GmbH",
        street="Hiltruper Straße 210",
        postal_code="48165",
        city="Münster",
        phone="0251 123456",
        google_place_id=place_id,
        latitude=51.9,
        longitude=7.63,
    )


def _keyword(phrase: str, primary: bool) -> Keyword:
    return Keyword(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        phrase=phrase,
        city="Münster",
        monthly_volume=None,
        is_primary=primary,
    )


MAIN_PHRASES = ["Dachdeckerei Hollenbeck GmbH", "Dachdecker Münster", "Flachdach Münster"]

LISTED_NAMES = [
    "Dachdeckerei Hollenbeck GmbH",
    "Bedachungen Overmann",
    "Dach Service Kemper",
    "Zimmerei Lüttke",
    "Noch ein Betrieb",
]
GRID_POINTS = 25
GRID_BESIDE_CENTRE = GRID_POINTS - 1
GRID_CENTRE_SEARCHED_BEFORE = 1


def _listing(name: str) -> MapListing:
    return MapListing(name, f"https://www.google.com/maps/place/{name}", None, 4.5, 10)


def _place(place_id: str, name: str) -> PlaceSnapshot:
    return PlaceSnapshot(
        place_id=place_id,
        name=name,
        formatted_address="Münster",
        phone=None,
        website=None,
        primary_type="roofing_contractor",
        types=[],
        rating=4.5,
        review_count=10,
        has_opening_hours=True,
        photo_count=3,
        recent_reviews=[],
        business_status="OPERATIONAL",
    )


def _paid_listing(name: str) -> MapListing:
    return MapListing(name, "", f"ChIJ-{name}", 4.5, 10)


def _paid_place(keyword: str) -> PlaceSnapshot:
    name = keyword.removeprefix("place_id:ChIJ-")
    for suffix in (" Hiltruper Straße 210, 48165 Münster", " Münster"):
        name = name.removesuffix(suffix)
    return _place(f"ChIJ-{name}", name).model_copy(
        update={"value_routes": {"name": "dataforseo", "recent_reviews": "dataforseo"}}
    )


def _serp(
    phrase: str, engine: str = "google", vertical: str = "web", route: str = "browser"
) -> SerpResult:
    return SerpResult.model_validate(
        {
            "phrase": phrase,
            "organic": [(1, "dachdecker-hollenbeck.de")],
            "local_pack": [],
            "ai_overview_present": False,
            "ai_overview_mentions": [],
            "people_also_ask": [],
            "measured_at": datetime(2026, 10, 16, tzinfo=UTC),
            "engine": engine,
            "vertical": vertical,
            "route": route,
        }
    )


WEBSITE = WebsiteSnapshot(
    start_url=SITE,
    uses_https=True,
    redirects_to_single_host=True,
    robots_txt=None,
    sitemap_urls=[],
    legal_notice_url=None,
    privacy_url=None,
    footer_nap={},
    opening_hours_text=None,
    pages=[],
    crawl_errors=[],
)
BUSINESS_OF_THE_NOTICE = BusinessFacts(
    name="Dachdeckerei Hollenbeck GmbH",
    street="Hiltruper Straße 210",
    postal_code="48165",
    city="Münster",
    phone="0251 123456",
)
SPEED = PageSpeedSnapshot(
    url=SITE,
    mobile_performance=40,
    desktop_performance=80,
    accessibility=90,
    lab=PageSpeedLab(runs=5, lcp_ms=3000, cls=0.1, tbt_ms=300),
)


async def no_pause(browser: BrowserService) -> None:
    pass


def _legal_notice_says(monkeypatch: pytest.MonkeyPatch, facts: BusinessFacts) -> None:
    async def read_site(site: WebsiteSnapshot) -> BusinessFacts:
        return facts

    monkeypatch.setattr(legal_notice, "read_site", read_site)


class Recorder:
    def __init__(self) -> None:
        self.calls: dict[str, list[tuple[object, ...]]] = {}

    def record(self, name: str, *arguments: object) -> None:
        self.calls.setdefault(name, []).append(arguments)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    calls = Recorder()

    async def crawl(start_url: HttpUrl, browser: object = None) -> WebsiteSnapshot:
        calls.record("crawl", start_url)
        return WEBSITE

    async def measure(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
        calls.record("measure", url)
        return SPEED

    async def find_place(name: str, address: str, api_key: SecretStr) -> str | None:
        calls.record("find_place", name, address)
        return f"id-{name}"

    async def place_details(place_id: str, api_key: SecretStr) -> PlaceSnapshot:
        calls.record("place_details", place_id)
        return _place(place_id, place_id.removeprefix("id-").removeprefix("ChIJ-"))

    async def top_competitors(
        phrase: str, latitude: float, longitude: float, api_key: SecretStr, limit: int = 3
    ) -> list[PlaceSnapshot]:
        calls.record("top_competitors", phrase, limit)
        return [
            _place("id-Dachdeckerei Hollenbeck GmbH", "Dachdeckerei Hollenbeck GmbH"),
            _place("id-Bedachungen Overmann", "Bedachungen Overmann"),
            _place("ChIJ-kemper", "Dach Service Kemper"),
            _place("ChIJ-luettke", "Zimmerei Lüttke"),
            _place("ChIJ-extra", "Noch ein Betrieb"),
        ]

    async def read_profile(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        calls.record("read_profile", target)
        return _place(target.place_id or f"maps-{target.name}", target.name)

    async def search_listings(
        browser: BrowserService, phrase: str, at: Geolocation | None = None
    ) -> list[MapListing]:
        calls.record("search_listings", phrase, at)
        return [_listing(name) for name in LISTED_NAMES]

    async def ranked_place_ids(
        phrase: str, latitude: float, longitude: float, api_key: SecretStr, radius: float
    ) -> list[str]:
        calls.record("ranked_place_ids", phrase, radius)
        return ["ChIJ-a", "id-Dachdeckerei Hollenbeck GmbH"]

    async def listed_businesses(
        phrase: str, latitude: float, longitude: float, api_key: SecretStr, radius: float
    ) -> list[places.ListedPlace]:
        calls.record("listed_businesses", phrase, radius)
        return [
            places.ListedPlace("ChIJ-a", "Bedachungen Overmann", 154, 4.8),
            places.ListedPlace(
                "id-Dachdeckerei Hollenbeck GmbH", "Dachdeckerei Hollenbeck GmbH", 86, 4.7
            ),
        ]

    async def search_all(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        calls.record("search_all", phrases)
        return serp.SearchBatch([_serp(phrase) for phrase in phrases], [])

    async def search_bing(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        calls.record("search_bing", phrases)
        return serp.SearchBatch([_serp(phrase, "bing") for phrase in phrases], [])

    async def search_images(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        calls.record("search_images", phrases)
        return serp.SearchBatch([_serp(phrase, vertical="images") for phrase in phrases], [])

    async def check_directories(location: Location, catalogue: Path) -> list[DirectoryHit]:
        calls.record("check_directories", location.name, catalogue)
        return [
            DirectoryHit(
                directory=name,
                found=False,
                listing_url=None,
                name=None,
                address=None,
                phone=None,
            )
            for name in ("Gelbe Seiten", "Das Örtliche", "11880")
        ]

    async def ask_all(
        questions: list[str],
        business_name: str,
        settings: Settings,
        gate: SourceGate,
        browser: BrowserService,
        browser_for: Callable[[str], BrowserService] | None = None,
        timings: list[PhaseTiming] | None = None,
        handoff: assistants.Handoff | None = None,
        reasons: dict[str, str] | None = None,
        domain: str | None = None,
    ) -> list[AssistantAnswer]:
        calls.record("ask_all", questions, business_name)
        calls.record("ask_all_browsers", browser_for)
        calls.record("ask_all_handoff", handoff)
        if timings is not None:
            timings.append(PhaseTiming(phase="assistant:chatgpt", seconds=0.5, answers=3))
        return [
            AssistantAnswer(
                provider=provider,
                question=questions[0],
                mentioned=True,
                cited_sources=[],
                answer_excerpt="Hollenbeck",
                route="browser",
            )
            for provider in assistants.enabled_providers(settings, gate)
        ]

    async def read_site(site: WebsiteSnapshot) -> BusinessFacts:
        return BUSINESS_OF_THE_NOTICE

    monkeypatch.setattr(website, "crawl_website", crawl)
    monkeypatch.setattr(legal_notice, "read_site", read_site)
    monkeypatch.setattr(pagespeed, "measure", measure)
    monkeypatch.setattr(places, "find_place", find_place)
    monkeypatch.setattr(places, "place_details", place_details)
    monkeypatch.setattr(places, "top_competitors", top_competitors)
    monkeypatch.setattr(places, "ranked_place_ids", ranked_place_ids)
    monkeypatch.setattr(places, "listed_businesses", listed_businesses)
    monkeypatch.setattr(maps, "read_profile", read_profile)
    monkeypatch.setattr(maps, "search_listings", search_listings)
    monkeypatch.setattr(BrowserService, "pause", no_pause)
    monkeypatch.setattr(serp, "search_all", search_all)
    monkeypatch.setattr(serp, "search_bing", search_bing)
    monkeypatch.setattr(serp, "search_images", search_images)
    monkeypatch.setattr(directories, "check_directories", check_directories)

    async def first_capture(domain: str) -> date | None:
        calls.record("first_capture", domain)
        return None

    async def collect_listings(*arguments: object) -> object:
        calls.record("collect_listings", *arguments)
        raise AssertionError("the portal listings are switched off")

    async def collect_social(*arguments: object) -> object:
        calls.record("collect_social", *arguments)
        raise AssertionError("the social profiles are switched off")

    async def search_google(
        client: paid_rankings.DataForSeoClient,
        phrases: list[str],
        latitude: float | None,
        longitude: float | None,
    ) -> paid_rankings.PaidSearchBatch:
        calls.record("search_google", phrases, latitude, longitude)
        return paid_rankings.PaidSearchBatch(
            [_serp(phrase, route="dataforseo") for phrase in phrases], []
        )

    async def search_maps(
        client: paid_rankings.DataForSeoClient, queries: list[dataforseo_maps.MapsQuery]
    ) -> dataforseo_maps.MapsBatch:
        calls.record("search_maps", [query.phrase for query in queries])
        listings = [_paid_listing(name) for name in LISTED_NAMES]
        return dataforseo_maps.MapsBatch(dict.fromkeys(range(len(queries)), listings), {})

    async def read_profiles(
        client: paid_rankings.DataForSeoClient, keywords: list[str]
    ) -> list[dataforseo_maps.ProfileRead]:
        calls.record("read_profiles", keywords)
        return [dataforseo_maps.ProfileRead(_paid_place(keyword), None, []) for keyword in keywords]

    async def search_through_google(
        client: paid_rankings.DataForSeoClient,
        entries: list[directories.DirectoryEntry],
        location: Location,
    ) -> tuple[list[DirectoryHit], dict[str, str]]:
        calls.record("search_through_google", [entry.name for entry in entries])
        return [], {}

    async def collect_google_ads(*arguments: object) -> GoogleAdsSnapshot:
        return GoogleAdsSnapshot(
            target="dachdecker-hollenbeck.example",
            window_days=90,
            location_code=2276,
            returned_location_code=2276,
            ads=[],
            measured_at=datetime(2026, 10, 16, tzinfo=UTC),
        )

    async def collect_meta_ads(*arguments: object) -> MetaAdsSnapshot:
        return MetaAdsSnapshot(
            searched_name="Hollenbeck",
            actor="apify/facebook-ads-scraper",
            confirmed=False,
            measured_at=datetime(2026, 10, 16, tzinfo=UTC),
        )

    monkeypatch.setattr(assistants, "ask_all", ask_all)
    monkeypatch.setattr(ads, "collect_google_ads", collect_google_ads)
    monkeypatch.setattr(ads, "collect_meta_ads", collect_meta_ads)
    monkeypatch.setattr(wayback, "first_capture", first_capture)
    monkeypatch.setattr(listings, "collect_listings", collect_listings)
    monkeypatch.setattr(social, "collect_social", collect_social)
    monkeypatch.setattr(paid_rankings, "search_google", search_google)
    monkeypatch.setattr(dataforseo_maps, "search_maps", search_maps)
    monkeypatch.setattr(dataforseo_maps, "read_profiles", read_profiles)
    monkeypatch.setattr(directories, "search_through_google", search_through_google)
    monkeypatch.setattr(paid_data, "collect_paid", _fake_paid([]))
    return calls


@pytest.fixture
def repositories(fake_repositories: MagicMock) -> MagicMock:
    keep_files_in_memory(fake_repositories)
    fake_repositories.audit.return_value = Audit(
        id=AUDIT_ID,
        customer_id=CUSTOMER_ID,
        kind=AuditKind.REMOTE,
        status=AuditStatus.COLLECTING,
        requested_at=datetime(2026, 10, 16, tzinfo=UTC),
        finished_at=None,
        total_score=None,
        raw_data_path=None,
        report_path=None,
    )
    fake_repositories.customer.return_value = _customer()
    fake_repositories.locations.return_value = [_location()]
    fake_repositories.competitors.return_value = [
        Competitor(
            id=uuid4(),
            customer_id=CUSTOMER_ID,
            name="Bedachungen Overmann",
            google_place_id=None,
            website=None,
        )
    ]
    fake_repositories.keywords.return_value = [
        _keyword("Flachdach", primary=False),
        _keyword("Dachdecker", primary=True),
    ]
    return fake_repositories


@pytest.fixture
def assistant_settings(collector_settings: Settings) -> Settings:
    return collector_settings.model_copy(update={"openai_api_key": SecretStr("openai-key")})


async def test_collect_audit_merges_every_source(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    settings = _paid_settings(assistant_settings)

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    assert raw.warnings == []
    assert raw.paid is not None
    assert raw.domain_first_capture is None
    for switched_off in SWITCHED_OFF_COLLECTORS:
        assert switched_off not in recorder.calls
    assert "search_all" not in recorder.calls
    assert [(result.engine, result.vertical, result.route) for result in raw.serps] == [
        *[("google", "web", "dataforseo")] * 3,
        *[("bing", "web", "browser")] * 3,
        *[("google", "images", "browser")] * 3,
    ]
    assert raw.website == WEBSITE
    assert raw.pagespeed == SPEED
    assert raw.place is not None
    assert raw.place.name == "Dachdeckerei Hollenbeck GmbH"
    assert [place.name for place in raw.competitor_places] == [
        "Bedachungen Overmann",
        "Dach Service Kemper",
        "Zimmerei Lüttke",
    ]
    assert [(result.phrase, result.engine, result.vertical) for result in raw.serps] == [
        ("Flachdach Münster", "google", "web"),
        ("Dachdecker Münster", "google", "web"),
        ("Dachdeckerei Hollenbeck GmbH", "google", "web"),
        ("Dachdeckerei Hollenbeck GmbH", "bing", "web"),
        ("Dachdecker Münster", "bing", "web"),
        ("Flachdach Münster", "bing", "web"),
        ("Dachdeckerei Hollenbeck GmbH", "google", "images"),
        ("Dachdecker Münster", "google", "images"),
        ("Flachdach Münster", "google", "images"),
    ]
    assert raw.directories == []
    assert [answer.provider for answer in raw.assistant_answers] == [
        "chatgpt",
        "google_ai",
        "claude",
    ]
    assert raw.collected_at.tzinfo is UTC
    repositories.customer.assert_called_once_with(CUSTOMER_ID)
    assert [call[0] for call in recorder.calls["find_place"]] == [
        "Dachdeckerei Hollenbeck GmbH",
        "Bedachungen Overmann",
    ]
    assert [call[0] for call in recorder.calls["place_details"]] == [
        "id-Dachdeckerei Hollenbeck GmbH",
        "id-Bedachungen Overmann",
        "ChIJ-Dach Service Kemper",
        "ChIJ-Zimmerei Lüttke",
    ]
    assert recorder.calls["read_profiles"] == [
        (["Dachdeckerei Hollenbeck GmbH Hiltruper Straße 210, 48165 Münster"],)
    ]
    ((own,),) = recorder.calls["read_profile"]
    assert isinstance(own, ProfileTarget)
    assert own.name == "Dachdeckerei Hollenbeck GmbH"
    assert [(item.phrase, item.position, item.route) for item in raw.map_rankings] == [
        ("Flachdach Münster", 1, "dataforseo"),
        ("Dachdecker Münster", 1, "dataforseo"),
    ]
    assert len(raw.grid) == GRID_POINTS
    assert {point.route for point in raw.grid} == {"dataforseo"}
    assert recorder.calls["search_maps"] == [
        (["Flachdach Münster", "Dachdecker Münster", *["Dachdecker Münster"] * GRID_BESIDE_CENTRE],)
    ]
    assert "search_listings" not in recorder.calls
    assert raw.map_results is not None
    assert (raw.map_results.phrase, raw.map_results.route) == ("Dachdecker Münster", "dataforseo")
    assert [
        (item.name, item.review_count, item.is_customer) for item in raw.map_results.businesses
    ] == [(name, 10, name == "Dachdeckerei Hollenbeck GmbH") for name in LISTED_NAMES]
    assert own.with_photos is True
    questions, business_name = recorder.calls["ask_all"][0]
    assert business_name == "Dachdeckerei Hollenbeck GmbH"
    assert isinstance(questions, list)
    assert questions[3] == "Flachdach in Münster, wen beauftragen?"


async def test_collect_audit_times_every_phase(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    raw = await run.collect_audit(AUDIT_ID, repositories, assistant_settings, open_gate)

    phases = [timing.phase for timing in raw.timings]
    assert {"website", "pagespeed", "google_profiles", "searches", "listings"} <= set(phases)
    assert "directories" not in phases
    assert {"assistants", "assistant:chatgpt", "total"} <= set(phases)
    assert phases[-1] == "total"
    assert all(timing.seconds >= 0 for timing in raw.timings)
    chatgpt = next(timing for timing in raw.timings if timing.phase == "assistant:chatgpt")
    assert chatgpt.answers == 3


async def test_collect_audit_gives_every_assistant_its_own_browser_profile(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    settings = assistant_settings.model_copy(update={"browser_channel": "chrome"})

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    (browser_for,) = recorder.calls["ask_all_browsers"][0]
    assert callable(browser_for)
    chatgpt, google = browser_for("chatgpt"), browser_for("google_ai")
    assert chatgpt._channel == google._channel == "chrome"
    profiles = assistant_settings.browser_profile_dir
    assert chatgpt._profile_dir == profiles / run.ASSISTANT_PROFILES / "chatgpt"
    assert google._profile_dir == profiles / run.ASSISTANT_PROFILES / "google_ai"


async def test_collect_audit_goes_on_when_crawl_fails(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def broken_crawl(start_url: HttpUrl, browser: object = None) -> WebsiteSnapshot:
        raise CollectionError("website start page answered HTTP 500")

    monkeypatch.setattr(website, "crawl_website", broken_crawl)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.website is None
    assert "website: website start page answered HTTP 500" in raw.warnings
    assert raw.pagespeed == SPEED


async def test_collect_audit_goes_on_when_a_route_fails_unexpectedly(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def broken(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
        raise RuntimeError("unexpected answer")

    monkeypatch.setattr(pagespeed, "measure", broken)
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places, ())
    )

    assert raw.pagespeed is None
    assert "pagespeed: RuntimeError('unexpected answer')" in raw.warnings
    assert ("pagespeed", Result.FAILED) in _results(places)
    assert raw.website == WEBSITE


async def test_collect_audit_turns_optional_failures_into_warnings(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def rate_limited(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
        raise TransientCollectionError("PageSpeed Insights (mobile) answered HTTP 429")

    async def not_listed(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        if target.url is None:
            raise ProfileNotFoundError(f"Google Maps lists no profile for {target.name}")
        return _place(target.url, target.name)

    async def unknown(name: str, address: str, api_key: SecretStr) -> str | None:
        return None

    async def captcha(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([_serp(phrases[0])], phrases[1:], "www.google.de shows a bot check")

    async def bing_blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.bing.com shows a bot check")

    async def no_answers(
        questions: list[str],
        business_name: str,
        settings: Settings,
        gate: SourceGate,
        browser: BrowserService,
        browser_for: Callable[[str], BrowserService] | None = None,
        timings: list[PhaseTiming] | None = None,
        handoff: assistants.Handoff | None = None,
        reasons: dict[str, str] | None = None,
        domain: str | None = None,
    ) -> list[AssistantAnswer]:
        raise CollectionError("no assistant answered: OpenAI rejected the request with HTTP 401")

    monkeypatch.setattr(pagespeed, "measure", rate_limited)
    monkeypatch.setattr(maps, "read_profile", not_listed)
    monkeypatch.setattr(places, "find_place", unknown)
    monkeypatch.setattr(serp, "search_all", captcha)
    monkeypatch.setattr(serp, "search_bing", bing_blocked)
    monkeypatch.setattr(assistants, "ask_all", no_answers)

    raw = await run.collect_audit(AUDIT_ID, repositories, assistant_settings, open_gate)

    assert raw.warnings == [
        "pagespeed: PageSpeed Insights (mobile) answered HTTP 429",
        "places: no Google Business Profile found for Dachdeckerei Hollenbeck GmbH",
        "competitors: Bedachungen Overmann not found on Google",
        "serps: 2 searches deferred to a retry: www.google.de shows a bot check",
        "bing: 3 of 3 not searched: www.bing.com shows a bot check",
        "images: 3 searches deferred to a retry: Google blocked the web searches before",
        "assistants: no assistant answered: OpenAI rejected the request with HTTP 401",
        "paid: DataForSEO credentials are missing",
        "ads_google: DataForSEO credentials are missing",
    ]
    assert raw.website == WEBSITE
    assert raw.pagespeed is None
    assert raw.place is None
    assert [place.name for place in raw.competitor_places] == [
        "Bedachungen Overmann",
        "Dach Service Kemper",
        "Zimmerei Lüttke",
    ]
    assert [(result.phrase, result.engine) for result in raw.serps] == [
        ("Flachdach Münster", "google")
    ]
    assert raw.deferred_searches == [
        DeferredSearch(phrase="Dachdecker Münster"),
        DeferredSearch(phrase="Dachdeckerei Hollenbeck GmbH"),
        DeferredSearch(phrase="Dachdeckerei Hollenbeck GmbH", vertical="images"),
        DeferredSearch(phrase="Dachdecker Münster", vertical="images"),
        DeferredSearch(phrase="Flachdach Münster", vertical="images"),
    ]
    assert "search_images" not in recorder.calls
    assert raw.assistant_answers == []


async def test_collect_audit_without_website_or_location(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    repositories.customer.return_value = _customer(website_url=None)
    repositories.locations.return_value = []
    repositories.competitors.return_value = []

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.website is None
    assert raw.warnings == [
        "website: customer has no website",
        "identify: kein Impressum gefunden",
        "pagespeed: customer has no website",
        "places: customer has no location",
        "competitors: no primary keyword or location coordinates to find ranked competitors",
        "map_rankings: customer has no location",
        "map_results: customer has no location",
        "grid: customer has no location",
        "paid: DataForSEO credentials are missing",
        "ads_google: customer has no website",
    ]
    assert raw.competitor_places == []
    assert raw.domain_first_capture is None
    assert "crawl" not in recorder.calls
    assert "first_capture" not in recorder.calls
    assert "ask_all" in recorder.calls


async def test_collect_audit_skips_assistants_switched_off(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings
) -> None:
    assistant_sources = {
        Source.CHATGPT,
        Source.GOOGLE_AI,
        Source.CLAUDE,
    }
    sources = SourcesSettings.model_validate(
        {source.value: {"enabled": source not in assistant_sources} for source in Source}
    )
    gate = SourceGate(sources, lambda source, cap: True)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, gate)

    assert raw.assistant_answers == []
    assert "ask_all" not in recorder.calls


async def test_collect_audit_without_keywords_warns_for_searches(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    repositories.keywords.return_value = []
    repositories.locations.return_value = [_location(place_id="ChIJ-own")]

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert [serp_result.phrase for serp_result in raw.serps] == ["Dachdeckerei Hollenbeck GmbH"]
    assert not any(warning.startswith("serps:") for warning in raw.warnings)
    assert "bing: customer has no keywords" in raw.warnings
    assert "images: customer has no keywords" in raw.warnings
    assert "map_rankings: customer has no keywords" in raw.warnings
    assert "grid: customer has no keywords" in raw.warnings
    own_target = recorder.calls["read_profile"][0][0]
    assert isinstance(own_target, ProfileTarget) and own_target.place_id == "ChIJ-own"
    assert raw.place is not None and raw.place.place_id == "ChIJ-own"


async def test_collect_audit_records_the_business_facts_and_a_missing_profile(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def no_profile(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        raise ProfileNotFoundError("no place page")

    async def unknown(name: str, address: str, api_key: SecretStr) -> str | None:
        return None

    monkeypatch.setattr(maps, "read_profile", no_profile)
    monkeypatch.setattr(places, "find_place", unknown)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.place is None and raw.profile_missing is True
    assert raw.business is not None
    assert (raw.business.name, raw.business.city, raw.business.postal_code) == (
        "Dachdeckerei Hollenbeck GmbH",
        "Münster",
        "48165",
    )


async def test_preflight_stops_the_audit_when_google_blocks_the_search(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.md"
    board.write_text("## Eingang\n", encoding="utf-8")

    async def blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.google.de shows a bot check")

    async def all_answer(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(serp, "search_all", blocked)
    monkeypatch.setattr(assistants, "probe", all_answer)
    settings = collector_settings.model_copy(
        update={"preflight_routes": True, "handoff_board_path": board, "halt_stages": (0, 1)}
    )

    with pytest.raises(PreflightError, match="Google-Suche: Google zeigt eine Sicherheitsabfrage"):
        await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    assert "angehalten, wartet auf Daniel" in board.read_text(encoding="utf-8")
    assert "read_profile" not in recorder.calls


async def test_preflight_sends_an_assistant_whose_probe_failed_to_the_person(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def google_ai_blocked(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {"chatgpt": None, "google_ai": "www.google.com shows a bot check", "claude": None}

    monkeypatch.setattr(assistants, "probe", google_ai_blocked)
    settings = assistant_settings.model_copy(update={"preflight_routes": True})

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    (handoff,) = recorder.calls["ask_all_handoff"][0]
    assert isinstance(handoff, assistants.Handoff)
    assert handoff.manual == frozenset({"google_ai"})
    assert [(check.route, check.ok) for check in raw.preflight] == [
        ("assistant:chatgpt", True),
        ("assistant:google_ai", False),
        ("assistant:claude", True),
        ("google_search", True),
        ("pagespeed", True),
    ]


async def _blocked_after_first(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
    return serp.SearchBatch([_serp(phrases[0])], phrases[1:], "www.google.de shows a bot check")


async def _paid_answers_first(
    client: paid_rankings.DataForSeoClient,
    phrases: list[str],
    latitude: float | None,
    longitude: float | None,
) -> paid_rankings.PaidSearchBatch:
    return paid_rankings.PaidSearchBatch(
        [_serp(phrases[0], route="dataforseo")],
        phrases[1:],
        "no result within 900 s; not repeated, the DataForSEO credit does not cover it",
    )


async def test_collect_audit_searches_google_through_dataforseo_and_the_browser_takes_the_rest(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:
    monkeypatch.setattr(paid_rankings, "search_google", _paid_answers_first)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert recorder.calls["search_all"] == [
        (["Dachdecker Münster", "Dachdeckerei Hollenbeck GmbH"],)
    ]
    google_web = [
        (result.phrase, result.route)
        for result in raw.serps
        if result.engine == "google" and result.vertical == "web"
    ]
    assert google_web == [
        ("Flachdach Münster", "dataforseo"),
        ("Dachdecker Münster", "browser"),
        ("Dachdeckerei Hollenbeck GmbH", "browser"),
    ]
    assert raw.deferred_searches == []
    assert (
        "serps: DataForSEO did not answer 2 of 3 searches, the next route takes them: "
        "no result within 900 s; not repeated, the DataForSEO credit does not cover it"
    ) in raw.warnings


async def test_collect_audit_defers_what_neither_dataforseo_nor_the_browser_searched(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:
    monkeypatch.setattr(paid_rankings, "search_google", _paid_answers_first)
    monkeypatch.setattr(serp, "search_all", _blocked_after_first)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert raw.deferred_searches[0] == DeferredSearch(phrase="Dachdeckerei Hollenbeck GmbH")
    assert "serps: 1 searches deferred to a retry: www.google.de shows a bot check" in raw.warnings
    stored = run.StepStore(AUDIT_ID, repositories).stored("serps")
    assert stored is not None and stored.failure is not None


async def test_collect_audit_defers_blocked_searches_when_dataforseo_is_off(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serp, "search_all", _blocked_after_first)
    sources = SourcesSettings.model_validate({"dataforseo": {"enabled": False}})

    raw = await run.collect_audit(
        AUDIT_ID,
        repositories,
        _paid_settings(collector_settings),
        SourceGate(sources, lambda source, cap: True),
    )

    assert "search_google" not in recorder.calls
    assert [search.phrase for search in raw.deferred_searches if search.vertical == "web"] == [
        "Dachdecker Münster",
        "Dachdeckerei Hollenbeck GmbH",
    ]
    assert raw.paid is None
    assert len(raw.grid) == GRID_POINTS


async def test_collect_audit_reports_a_reached_monthly_cost_cap(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class SpentLedger:
        def month_source_cost(self, source: str) -> Decimal:
            return Decimal(50) if source == Source.DATAFORSEO.value else Decimal(0)

        def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
            if source == Source.DATAFORSEO.value:
                raise AssertionError("no request may run")
            return cost

    async def collect(
        client: paid_rankings.DataForSeoClient,
        request: paid_data.PaidRequest,
        notes: list[str],
        today: object = None,
    ) -> PaidSnapshot:
        await client.post(paid_data.SUMMARY_PATH, {"target": "x"})
        raise AssertionError("the cap must stop the request")

    monkeypatch.setattr(paid_data, "collect_paid", collect)
    gate = SourceGate(SourcesSettings(), lambda source, cap: True, SpentLedger())

    raw = await run.collect_audit(AUDIT_ID, repositories, _paid_settings(collector_settings), gate)

    assert raw.cost_capped_sources == [Source.DATAFORSEO]
    assert "paid: dataforseo: monthly cost cap of 50 EUR reached" in raw.warnings


async def test_collect_audit_measures_the_grid_in_the_browser(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert [point.key for point in raw.grid][:3] == ["r1c1", "r1c2", "r1c3"]
    assert all(point.position == 1 for point in raw.grid)
    grid_searches = recorder.calls["search_listings"][-GRID_BESIDE_CENTRE:]
    assert {phrase for phrase, _ in grid_searches} == {"Dachdecker Münster"}
    positions = {at for _, at in grid_searches}
    assert len(positions) == GRID_BESIDE_CENTRE
    assert not any(warning.startswith("grid") for warning in raw.warnings)


async def test_collect_audit_warns_when_grid_lacks_coordinates(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories.locations.return_value = [
        _location().model_copy(update={"latitude": None, "longitude": None})
    ]

    async def unknown(street: str, postal_code: str, city: str) -> tuple[float, float] | None:
        return None

    monkeypatch.setattr(geocode, "geocode", unknown)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert "geocode: no coordinates found for the address of the location" in raw.warnings
    assert raw.grid == []
    assert raw.map_rankings == []
    assert "grid: location has no coordinates for the map searches" in raw.warnings
    assert "map_rankings: location has no coordinates for the map searches" in raw.warnings


async def test_collect_audit_geocodes_a_location_without_coordinates(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = _location().model_copy(update={"latitude": None, "longitude": None})
    repositories.locations.return_value = [location]

    async def found(street: str, postal_code: str, city: str) -> tuple[float, float] | None:
        recorder.record("geocode", street, postal_code, city)
        return 51.96, 7.63

    monkeypatch.setattr(geocode, "geocode", found)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert recorder.calls["geocode"] == [(location.street, location.postal_code, location.city)]
    repositories.update_location.assert_any_call(location.id, latitude=51.96, longitude=7.63)
    assert not any("no coordinates" in warning for warning in raw.warnings)
    assert len(raw.grid) == GRID_POINTS


async def test_collect_audit_does_not_geocode_a_location_without_street(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories.locations.return_value = [
        _location().model_copy(
            update={"street": " ", "postal_code": "", "latitude": None, "longitude": None}
        )
    ]

    async def found(street: str, postal_code: str, city: str) -> tuple[float, float] | None:
        recorder.record("geocode", street, postal_code, city)
        return 51.96, 7.63

    monkeypatch.setattr(geocode, "geocode", found)
    _legal_notice_says(monkeypatch, BusinessFacts())

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert "geocode" not in recorder.calls
    assert "geocode: location has no street address to look up" in raw.warnings
    assert raw.grid == []


async def test_collect_audit_does_not_geocode_a_location_with_coordinates(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert "geocode" not in recorder.calls


async def test_collect_audit_warns_when_the_address_lookup_fails(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories.locations.return_value = [
        _location().model_copy(update={"latitude": None, "longitude": None})
    ]

    async def broken(street: str, postal_code: str, city: str) -> tuple[float, float] | None:
        raise CollectionError("offline")

    monkeypatch.setattr(geocode, "geocode", broken)

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert "geocode: offline" in raw.warnings
    assert "grid: location has no coordinates for the map searches" in raw.warnings


async def _paid_profiles_fail(
    client: paid_rankings.DataForSeoClient, keywords: list[str]
) -> list[dataforseo_maps.ProfileRead]:
    return [dataforseo_maps.ProfileRead(None, "DataForSEO status 40501", []) for _ in keywords]


async def _paid_maps_fail(
    client: paid_rankings.DataForSeoClient, queries: list[dataforseo_maps.MapsQuery]
) -> dataforseo_maps.MapsBatch:
    return dataforseo_maps.MapsBatch({}, dict.fromkeys(range(len(queries)), "status 40501"))


async def test_collect_audit_keeps_the_places_profile_when_the_other_ways_add_nothing(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def blocked(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        raise SearchBlockedError("www.google.com shows a bot check")

    monkeypatch.setattr(maps, "read_profile", blocked)
    monkeypatch.setattr(dataforseo_maps, "read_profiles", _paid_profiles_fail)
    monkeypatch.setattr(dataforseo_maps, "search_maps", _paid_maps_fail)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert raw.warnings == [
        "places: Dachdeckerei Hollenbeck GmbH: DataForSEO profile failed (DataForSEO status 40501)",
        "places: Google Maps blocked the browser (www.google.com shows a bot check), "
        "no profile data from the browser",
        "map_rankings: DataForSEO Maps answered 0 of 26 searches, the browser takes the rest: "
        "status 40501",
    ]
    assert raw.place is not None and raw.place.place_id == "id-Dachdeckerei Hollenbeck GmbH"
    assert [place.name for place in raw.competitor_places] == [
        "Bedachungen Overmann",
        "Dach Service Kemper",
        "Zimmerei Lüttke",
    ]
    assert {(item.position, item.route) for item in raw.map_rankings} == {(2, "places_api")}
    assert len(raw.grid) == GRID_POINTS
    assert {(point.position, point.route) for point in raw.grid} == {(2, "places_api")}
    assert "search_listings" not in recorder.calls
    assert len(recorder.calls["ranked_place_ids"]) == 2 + GRID_POINTS
    assert raw.map_results is not None and raw.map_results.route == "places_api"
    assert [
        (item.name, item.review_count, item.is_customer) for item in raw.map_results.businesses
    ] == [
        ("Bedachungen Overmann", 154, False),
        ("Dachdeckerei Hollenbeck GmbH", 86, True),
    ]


async def test_collect_audit_warns_when_paid_credentials_missing(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert "paid: DataForSEO credentials are missing" in raw.warnings
    assert raw.paid is None
    assert "search_all" in recorder.calls


async def test_collect_audit_does_not_repeat_a_brand_keyword(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legal_notice_says(monkeypatch, BusinessFacts())
    repositories.keywords.return_value = [_keyword("Dachdeckerei Hollenbeck GmbH", primary=True)]
    repositories.customer.return_value = _customer().model_copy(
        update={"name": "dachdeckerei hollenbeck gmbh münster"}
    )

    await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert recorder.calls["search_all"] == [(["Dachdeckerei Hollenbeck GmbH Münster"],)]


async def test_collect_audit_reads_question_templates_from_settings(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    tmp_path: Path,
    open_gate: SourceGate,
) -> None:
    questions_file = tmp_path / "fragen.yaml"
    questions_file.write_text(
        "empfehlung: ['Bester {industry} in {city}?']\nproblem: ['{service} {city}']\n",
        encoding="utf-8",
    )
    settings = assistant_settings.model_copy(update={"questions_path": questions_file})

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    questions, _ = recorder.calls["ask_all"][0]
    assert questions == ["Bester Dachdecker in Münster?", "Flachdach Münster"]


async def test_questions_name_the_trade_of_the_industry_group(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    tmp_path: Path,
    open_gate: SourceGate,
) -> None:
    questions_file = tmp_path / "fragen.yaml"
    questions_file.write_text(
        "empfehlung: ['Bester {industry} in {city}?']\nproblem: ['{service} {city}']\n",
        encoding="utf-8",
    )
    settings = assistant_settings.model_copy(update={"questions_path": questions_file})
    customer = repositories.customer.return_value
    repositories.customer.return_value = customer.model_copy(update={"industry": "handwerk"})
    repositories.keywords.return_value = []
    repositories.add_keywords.return_value = []

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    questions, _ = recorder.calls["ask_all"][0]
    assert questions[0] == "Bester Handwerksbetrieb in Münster?"


def _paid_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"dataforseo_login": "login", "dataforseo_password": SecretStr("secret")}
    )


def _fake_paid(
    requests: list[paid_data.PaidRequest], note: str | None = None
) -> Callable[..., Awaitable[PaidSnapshot]]:
    async def collect(
        client: paid_rankings.DataForSeoClient,
        request: paid_data.PaidRequest,
        notes: list[str],
        today: object = None,
    ) -> PaidSnapshot:
        requests.append(request)
        if note is not None:
            notes.append(note)
        return PaidSnapshot(
            customer_domain=request.customer_domain,
            volumes={"Dachdecker Münster": 880, "Flachdach Münster": 40},
            primary_phrases=request.primary_phrases,
            collected_at=datetime(2026, 10, 16, tzinfo=UTC),
        )

    return collect


async def test_collect_audit_measures_paid_data_and_stores_volumes(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:
    requests: list[paid_data.PaidRequest] = []
    monkeypatch.setattr(paid_data, "collect_paid", _fake_paid(requests, "volumes: slow"))
    keywords = repositories.keywords.return_value
    repositories.keywords.return_value = [
        keywords[0].model_copy(update={"monthly_volume": 40}),
        keywords[1],
    ]
    repositories.competitors.return_value = [
        repositories.competitors.return_value[0].model_copy(
            update={"website": HttpUrl("https://www.overmann-dach.de/leistungen")}
        )
    ]

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert raw.paid is not None
    assert raw.paid.volumes["Dachdecker Münster"] == 880
    assert raw.warnings == ["paid: volumes: slow"]
    request = requests[0]
    assert request.customer_domain == "dachdecker-hollenbeck.de"
    assert request.competitor_domains == ["overmann-dach.de"]
    assert request.phrases == ["Flachdach Münster", "Dachdecker Münster"]
    assert request.primary_phrases == ["Dachdecker Münster"]
    assert request.city == "Münster"
    repositories.update_keyword_volume.assert_called_once_with(keywords[1].id, 880)


async def test_collect_audit_warns_when_volumes_cannot_be_saved(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:
    repositories.update_keyword_volume.side_effect = StorageError("write refused")

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert raw.paid is not None
    assert "paid: volume of Dachdecker not saved: write refused" in raw.warnings


async def test_collect_audit_has_no_paid_or_access_data_by_default(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.paid is None
    assert raw.access is None
    repositories.customer_access.assert_not_called()


ACCESS_RECORD = CustomerAccess(
    customer_id=CUSTOMER_ID,
    google_business_profile_granted=True,
    search_console_property="sc-domain:dachdecker-hollenbeck.de",
    wordpress_url=None,
    wordpress_user=None,
    granted_at=datetime(2026, 10, 1, tzinfo=UTC),
    google_location_name="locations/123",
    analytics_property_id="987",
)


@pytest.fixture
def access_repositories(repositories: MagicMock) -> MagicMock:
    repositories.audit.return_value = repositories.audit.return_value.model_copy(
        update={"kind": AuditKind.WITH_ACCESS}
    )
    repositories.customer_access.return_value = ACCESS_RECORD
    return repositories


async def test_collect_audit_reads_customer_data_with_access(
    access_repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:
    seen: list[tuple[str, CustomerAccess]] = []

    async def token(repositories: object, settings: Settings) -> str:
        return "access-token"

    async def collect(
        access_token: str, record: CustomerAccess, notes: list[str], today: object = None
    ) -> AccessSnapshot:
        seen.append((access_token, record))
        notes.append("analytics: no identifier recorded for the customer")
        return AccessSnapshot(
            profile_metrics={"CALL_CLICKS": 3}, collected_at=datetime(2026, 10, 16, tzinfo=UTC)
        )

    monkeypatch.setattr(google_auth, "access_token", token)
    monkeypatch.setattr(access, "collect_access", collect)

    settings = _paid_settings(collector_settings)

    raw = await run.collect_audit(AUDIT_ID, access_repositories, settings, open_gate)

    assert raw.access is not None
    assert raw.access.profile_metrics == {"CALL_CLICKS": 3}
    assert seen == [("access-token", ACCESS_RECORD)]
    assert raw.warnings == ["access: analytics: no identifier recorded for the customer"]
    access_repositories.customer_access.assert_called_once_with(CUSTOMER_ID)


async def test_collect_audit_warns_without_authorisation_or_record(
    access_repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def no_grant(repositories: object, settings: Settings) -> str:
        raise AuthorizationError("no Google authorisation stored; run radarauditor google auth")

    monkeypatch.setattr(google_auth, "access_token", no_grant)
    settings = _paid_settings(collector_settings)

    raw = await run.collect_audit(AUDIT_ID, access_repositories, settings, open_gate)

    assert raw.access is None
    assert raw.warnings == ["access: no Google authorisation stored; run radarauditor google auth"]
    access_repositories.customer_access.return_value = None
    raw = await run.collect_audit(AUDIT_ID, access_repositories, settings, open_gate)
    assert raw.warnings == ["access: customer has recorded no access"]


class CountingQuota:
    def __init__(self, allowed: dict[str, int]) -> None:
        self.allowed = allowed
        self.used: dict[str, int] = {}

    def consume(self, source: str, cap: int) -> bool:
        self.used[source] = self.used.get(source, 0) + 1
        return self.used[source] <= self.allowed.get(source, cap)


async def test_collect_audit_by_default_reads_profiles_through_places_and_maps_for_the_rest(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings
) -> None:
    quota = CountingQuota({})

    raw = await run.collect_audit(
        AUDIT_ID, repositories, collector_settings, SourceGate(SourcesSettings(), quota.consume)
    )

    assert "top_competitors" not in recorder.calls
    assert raw.place is not None
    assert quota.used["places"] == len(recorder.calls["find_place"]) + len(
        recorder.calls["place_details"]
    )
    assert quota.used["google_maps"] == 1 + 2 + GRID_POINTS - GRID_CENTRE_SEARCHED_BEFORE
    assert quota.used["google_search"] == 6
    assert raw.capped_sources == []


async def test_collect_audit_stops_google_searches_at_the_daily_cap(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings
) -> None:
    quota = CountingQuota({"google_search": 2})

    raw = await run.collect_audit(
        AUDIT_ID, repositories, collector_settings, SourceGate(SourcesSettings(), quota.consume)
    )

    assert recorder.calls["search_all"] == [(["Flachdach Münster", "Dachdecker Münster"],)]
    assert recorder.calls["search_images"] == [([],)]
    assert "serps: daily cap reached, 1 of 3 not searched" in raw.warnings
    assert raw.capped_sources == [Source.GOOGLE_SEARCH]


async def test_collect_audit_keeps_competitors_found_before_the_cap(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings
) -> None:
    sources = SourcesSettings.model_validate({"google_maps": {"enabled": False}})
    quota = CountingQuota({"places": 4})

    raw = await run.collect_audit(
        AUDIT_ID, repositories, collector_settings, SourceGate(sources, quota.consume)
    )

    assert raw.place is not None
    assert [place.name for place in raw.competitor_places] == ["Bedachungen Overmann"]
    assert "competitors: places: daily cap of 500 reached" in raw.warnings
    assert "grid: places: daily cap of 500 reached" in raw.warnings
    assert Source.PLACES in raw.capped_sources


async def test_questions_use_the_trade_of_the_primary_keyword(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    tmp_path: Path,
    open_gate: SourceGate,
) -> None:
    questions_file = tmp_path / "fragen.yaml"
    questions_file.write_text(
        "empfehlung: ['Bester {industry} in {city}?']\nproblem: ['{service} {city}']\n",
        encoding="utf-8",
    )
    settings = assistant_settings.model_copy(update={"questions_path": questions_file})
    repositories.keywords.return_value = [_keyword("Elektriker Münster", primary=True)]

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    questions, _ = recorder.calls["ask_all"][0]
    assert questions[0] == "Bester Elektriker in Münster?"


async def test_collect_audit_identifies_a_business_started_with_only_its_website(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_customer = _customer().model_copy(
        update={"name": "dachdecker-hollenbeck.de", "city": "", "industry": ""}
    )
    repositories.customer.return_value = host_customer
    repositories.locations.return_value = [
        _location().model_copy(
            update={"name": "dachdecker-hollenbeck.de", "city": "", "street": ""}
        )
    ]
    repositories.competitors.return_value = []
    repositories.keywords.return_value = []
    repositories.link_crm_customer.return_value = None

    def add(
        customer_id: object, phrases: list[str], city: str, *, first_primary: bool
    ) -> list[Keyword]:
        return [_keyword(phrase, primary=index == 0) for index, phrase in enumerate(phrases)]

    repositories.add_keywords.side_effect = add
    site = WEBSITE.model_copy(
        update={
            "legal_notice_url": None,
            "footer_nap": {
                "name": "Dachdeckerei Hollenbeck GmbH",
                "street": "Hiltruper Straße 210",
                "postal_code": "48165",
                "city": "Münster",
            },
        }
    )

    async def crawl(start_url: HttpUrl, browser: object = None) -> WebsiteSnapshot:
        return site

    async def search_businesses(query: str, api_key: SecretStr) -> list[places.BusinessCandidate]:
        recorder.record("search_businesses", query)
        return []

    monkeypatch.setattr(website, "crawl_website", crawl)
    monkeypatch.setattr(places, "search_businesses", search_businesses)
    _legal_notice_says(
        monkeypatch,
        BusinessFacts(
            name="Dachdeckerei Hollenbeck GmbH",
            street="Hiltruper Straße 210",
            postal_code="48165",
            city="Münster",
        ),
    )

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert recorder.calls["search_businesses"] == [("Dachdeckerei Hollenbeck GmbH Münster",)]
    assert [(item.field, item.source) for item in raw.identified] == [
        ("name", "impressum"),
        ("street", "impressum"),
        ("city", "impressum"),
        ("industry", "name_words"),
        ("keywords", "industry_services"),
    ]
    own = recorder.calls["read_profile"][0][0]
    assert isinstance(own, ProfileTarget)
    assert (own.name, own.address) == (
        "Dachdeckerei Hollenbeck GmbH",
        "Hiltruper Straße 210, 48165 Münster",
    )
    assert [result.phrase for result in raw.serps if result.engine == "google"][:3] == [
        "Dachdecker Münster",
        "Maler Münster",
        "Fliesenleger Münster",
    ]
    assert "identify: no Google profile clearly belongs to Dachdeckerei Hollenbeck GmbH" in (
        raw.warnings
    )


async def test_collect_audit_of_a_recorded_customer_identifies_nothing_new(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
) -> None:
    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.identified == []
    assert raw.business == BUSINESS_OF_THE_NOTICE
    repositories.update_customer.assert_not_called()


NOW = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
CONFIG = Path(__file__).parents[1] / "config"
ALL_ASSISTANTS = ["chatgpt", "google_ai", "claude"]
TOO_MANY_REQUESTS = "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"


def _feedback(places: Places, halt_stages: tuple[int, ...] = (0, 1)) -> Feedback:
    catalogue = load_catalogue(CONFIG / "pruefkatalog.yaml")
    return Feedback(
        audit_id=AUDIT_ID,
        business="Dachdeckerei Hollenbeck GmbH",
        routes=load_route_map(CONFIG / "messwege.yaml", catalogue),
        halt_stages=halt_stages,
        channels=places.channels(),
        clock=lambda: NOW,
    )


def _route_events(places: Places) -> list[AuditEvent]:
    return [event for event in places.file if event.kind != "measurement"]


def _results(places: Places) -> set[tuple[str | None, Result]]:
    return {(event.route, event.result) for event in _route_events(places)}


async def test_collect_audit_reports_every_route_when_it_is_done(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
) -> None:
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
    )

    assert _results(places) == {
        ("website", Result.MEASURED),
        ("pagespeed", Result.MEASURED),
        ("google_maps", Result.MEASURED),
        ("google_search", Result.MEASURED),
        ("bing", Result.MEASURED),
        ("google_images", Result.MEASURED),
        *((f"assistant:{name}", Result.MEASURED) for name in ALL_ASSISTANTS),
        ("dataforseo", Result.SUSPENDED),
        ("google_ads", Result.SUSPENDED),
        ("meta_ads", Result.SUSPENDED),
    }
    assert len(_route_events(places)) == len(_results(places))
    assert places.notices == [] and places.board == []
    assert {(report.route, report.result) for report in raw.route_reports} == _results(places)


async def test_collect_audit_reports_the_paid_service_as_measured_when_it_runs(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
) -> None:
    places = Places()

    await run.collect_audit(
        AUDIT_ID,
        repositories,
        _paid_settings(assistant_settings),
        open_gate,
        feedback=_feedback(places),
    )

    assert ("dataforseo", Result.MEASURED) in _results(places)


async def test_collect_audit_goes_on_after_a_failed_route_of_stage_three(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def limited(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
        raise TransientCollectionError("PageSpeed Insights (mobile) answered HTTP 429")

    monkeypatch.setattr(pagespeed, "measure", limited)
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
    )

    assert raw.pagespeed is None and raw.website == WEBSITE
    assert [
        (event.route, event.reason) for event in places.file if event.result is Result.FAILED
    ] == [("pagespeed", TOO_MANY_REQUESTS)]
    assert places.notices == [
        "Audit Dachdeckerei Hollenbeck GmbH: Messung fehlgeschlagen – "
        f"PageSpeed (Nr 1 a2): {TOO_MANY_REQUESTS}"
    ]
    assert ("google_search", Result.MEASURED) in _results(places)


async def test_collect_audit_reports_a_failed_crawl_and_goes_on(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def unreachable(start_url: HttpUrl, browser: object = None) -> WebsiteSnapshot:
        raise CollectionError("website unreachable: connection refused")

    monkeypatch.setattr(website, "crawl_website", unreachable)
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places, ())
    )

    assert raw.website is None
    assert _route_events(places)[0].route == "website"
    assert ("website", Result.FAILED) in _results(places)
    assert ("pagespeed", Result.MEASURED) in _results(places)


async def test_collect_audit_reports_a_customer_without_website(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
) -> None:
    repositories.customer.return_value = _customer(website_url=None)
    places = Places()

    await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
    )

    (website_event,) = [event for event in places.file if event.route == "website"]
    assert (website_event.result, website_event.reason) == (
        Result.FAILED,
        "Der Kunde hat keine Website angegeben",
    )


async def test_collect_audit_stops_at_once_when_google_blocks_a_search_during_the_run(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished: list[str] = []

    async def blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.google.de shows a bot check")

    async def slow_directories(location: Location, catalogue: Path) -> list[DirectoryHit]:
        await asyncio.sleep(5)
        finished.append("directories")
        return []

    monkeypatch.setattr(serp, "search_all", blocked)
    monkeypatch.setattr(directories, "check_directories", slow_directories)
    places = Places()

    with pytest.raises(AuditHalted, match="Google-Suche: Google zeigt eine Sicherheitsabfrage"):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )

    assert finished == []
    assert ("google_search", Result.FAILED) in _results(places)
    assert ("directories", Result.MEASURED) not in _results(places)
    assert places.notices[0].startswith(
        "Audit Dachdeckerei Hollenbeck GmbH: Messung fehlgeschlagen – Google-Suche"
    )
    assert "angehalten, wartet auf Daniel" in places.board[-1]


async def test_collect_audit_reports_a_blocked_bing_search_as_failed_and_halts(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def bing_blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.bing.com shows a bot check")

    monkeypatch.setattr(serp, "search_bing", bing_blocked)
    places = Places()

    with pytest.raises(AuditHalted, match="Bing-Suche: Bing zeigt eine Sicherheitsabfrage"):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )


async def test_collect_audit_reports_a_failed_google_map_route(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
) -> None:
    repositories.keywords.return_value = []
    places = Places()

    with pytest.raises(AuditHalted):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )

    (event,) = [event for event in places.file if event.route == "google_maps"]
    assert (event.result, event.reason) == (
        Result.FAILED,
        "Für den Kunden sind keine Suchbegriffe hinterlegt",
    )


async def test_collect_audit_does_not_count_a_missing_google_profile_as_a_failed_route(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def not_listed(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        if target.url is None:
            raise ProfileNotFoundError(f"Google Maps lists no profile for {target.name}")
        return _place(target.url, target.name)

    async def unknown(name: str, address: str, api_key: SecretStr) -> str | None:
        return None

    monkeypatch.setattr(maps, "read_profile", not_listed)
    monkeypatch.setattr(places, "find_place", unknown)
    recorded = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(recorded)
    )

    assert raw.profile_missing is True
    assert ("google_maps", Result.MEASURED) in _results(recorded)


def _assistant_answers(
    providers: list[str], route: str = "browser"
) -> Callable[..., Awaitable[list[AssistantAnswer]]]:
    async def ask_all(
        questions: list[str], *args: object, **kwargs: object
    ) -> list[AssistantAnswer]:
        return [
            AssistantAnswer(
                provider=provider,
                question=questions[0],
                mentioned=True,
                cited_sources=[],
                answer_excerpt="Hollenbeck",
                route=route if provider == "google_ai" else "browser",
            )
            for provider in providers
        ]

    return ask_all


async def test_collect_audit_reports_an_assistant_asked_with_a_person(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assistants, "ask_all", _assistant_answers(ALL_ASSISTANTS, route="manual"))
    places = Places()

    await run.collect_audit(
        AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
    )

    assert ("assistant:google_ai", Result.WITH_PERSON) in _results(places)
    assert ("assistant:chatgpt", Result.MEASURED) in _results(places)
    assert places.notices == []


async def test_collect_audit_reports_a_silent_assistant_as_failed_and_halts(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assistants, "ask_all", _assistant_answers(["chatgpt"]))
    places = Places()

    with pytest.raises(AuditHalted, match="Googles KI-Modus: Keine Antwort erhalten"):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )

    failed = {event.route for event in places.file if event.result is Result.FAILED}
    assert failed == {"assistant:google_ai", "assistant:claude"}


async def test_collect_audit_without_halt_stages_records_a_silent_assistant_and_goes_on(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assistants, "ask_all", _assistant_answers(["chatgpt"]))
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID,
        repositories,
        assistant_settings,
        open_gate,
        feedback=_feedback(places, halt_stages=()),
    )

    failed = {event.route: event.reason for event in places.file if event.result is Result.FAILED}
    assert set(failed) == {"assistant:google_ai", "assistant:claude"}
    assert "Keine Antwort erhalten" in failed["assistant:google_ai"]
    assert not any("wartet auf Daniel" in message for message in places.board)
    assert [answer.provider for answer in raw.assistant_answers] == ["chatgpt"]


async def test_collect_audit_reports_assistants_when_none_answers(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def no_answers(*args: object, **kwargs: object) -> list[AssistantAnswer]:
        raise CollectionError("no assistant answered: OpenAI rejected the request with HTTP 401")

    monkeypatch.setattr(assistants, "ask_all", no_answers)
    places = Places()

    with pytest.raises(AuditHalted):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )

    reasons = {event.reason for event in places.file if event.result is Result.FAILED}
    assert reasons == {"Kein Assistent hat geantwortet"}


async def test_collect_audit_reports_the_customer_access_route(
    access_repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    open_gate: SourceGate,
) -> None:

    async def no_grant(repositories: object, settings: Settings) -> str:
        raise AuthorizationError("no Google authorisation stored; run radarauditor google auth")

    monkeypatch.setattr(google_auth, "access_token", no_grant)
    places = Places()

    await run.collect_audit(
        AUDIT_ID, access_repositories, collector_settings, open_gate, feedback=_feedback(places)
    )

    (event,) = [event for event in places.file if event.route == "access"]
    assert event.result is Result.FAILED
    assert places.notices != []


async def test_collect_audit_does_not_report_the_access_route_without_access(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
) -> None:
    places = Places()

    await run.collect_audit(
        AUDIT_ID, repositories, collector_settings, open_gate, feedback=_feedback(places)
    )

    assert "access" not in {event.route for event in places.file}


async def test_preflight_reports_the_probes_and_halts_on_the_failed_search(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.google.de shows a bot check")

    async def all_answer(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(serp, "search_all", blocked)
    monkeypatch.setattr(assistants, "probe", all_answer)
    settings = collector_settings.model_copy(update={"preflight_routes": True})
    places = Places()

    with pytest.raises(AuditHalted, match="Google-Suche: Google zeigt eine Sicherheitsabfrage"):
        await run.collect_audit(
            AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(places)
        )

    assert {(event.route, event.phase, event.result) for event in _route_events(places)} == {
        ("google_search", "vorab", Result.FAILED),
        ("pagespeed", "vorab", Result.MEASURED),
    }
    assert "read_profile" not in recorder.calls


async def test_preflight_reports_a_blocked_assistant_as_a_step_with_daniel(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def google_ai_blocked(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {"chatgpt": None, "google_ai": "www.google.com shows a bot check", "claude": None}

    monkeypatch.setattr(assistants, "probe", google_ai_blocked)
    settings = assistant_settings.model_copy(update={"preflight_routes": True})
    places = Places()

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(places))

    (probe,) = [
        event
        for event in places.file
        if event.route == "assistant:google_ai" and event.phase == "vorab"
    ]
    assert (probe.result, probe.reason) == (
        Result.WITH_PERSON,
        "Google zeigt eine Sicherheitsabfrage",
    )


async def test_preflight_does_not_stop_the_audit_when_pagespeed_fails(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def limited(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
        raise TransientCollectionError("PageSpeed Insights (mobile) answered HTTP 429")

    async def all_answer(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(pagespeed, "measure", limited)
    monkeypatch.setattr(assistants, "probe", all_answer)
    settings = collector_settings.model_copy(update={"preflight_routes": True})
    places = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(places)
    )

    assert raw.website == WEBSITE
    assert ("pagespeed", "vorab", Result.FAILED) in {
        (event.route, event.phase, event.result) for event in places.file
    }


async def test_collect_audit_writes_events_to_file_and_table_by_default(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    tmp_path: Path,
) -> None:
    settings = collector_settings.model_copy(update={"radar_work_dir": tmp_path / "work"})

    await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    events_file = tmp_path / "work" / "ereignisse" / f"{AUDIT_ID}.jsonl"
    assert len(events_file.read_text(encoding="utf-8").splitlines()) >= 7
    repositories.add_audit_events.assert_called()
    assert all(call.args[0] == AUDIT_ID for call in repositories.add_audit_events.call_args_list)


APIFY_TOKEN = SecretStr("apify-secret-token")


def _with_apify(settings: Settings) -> Settings:
    return settings.model_copy(update={"apify_token": APIFY_TOKEN})


class ApifyCalls:
    def __init__(self) -> None:
        self.web: list[tuple[list[str], frozenset[str], int]] = []
        self.images: list[list[str]] = []


def _fake_apify(
    monkeypatch: pytest.MonkeyPatch,
    web_failed: int = 0,
    images_failed: int = 0,
) -> ApifyCalls:
    calls = ApifyCalls()

    async def web(
        settings: Settings,
        gate: SourceGate,
        phrases: list[str],
        ad_phrases: frozenset[str] = frozenset(),
        pages: int = apify_serp.WEB_PAGES,
    ) -> paid_rankings.PaidSearchBatch:
        calls.web.append((phrases, ad_phrases, pages))
        answered = phrases[: len(phrases) - web_failed]
        return paid_rankings.PaidSearchBatch(
            [_serp(phrase, route="apify") for phrase in answered],
            phrases[len(answered) :],
            "Apify daily or monthly cap reached" if web_failed else None,
        )

    async def images(
        settings: Settings, gate: SourceGate, phrases: list[str]
    ) -> paid_rankings.PaidSearchBatch:
        calls.images.append(phrases)
        answered = phrases[: len(phrases) - images_failed]
        return paid_rankings.PaidSearchBatch(
            [_serp(phrase, vertical="images", route="apify") for phrase in answered],
            phrases[len(answered) :],
            "Apify returned no image results" if images_failed else None,
        )

    monkeypatch.setattr(apify_serp, "search_web", web)
    monkeypatch.setattr(apify_serp, "search_images", images)
    return calls


async def test_collect_audit_reads_google_through_apify_before_the_browser(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_apify(monkeypatch)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _with_apify(collector_settings), open_gate
    )

    assert calls.web == [
        (
            ["Flachdach Münster", "Dachdecker Münster", "Dachdeckerei Hollenbeck GmbH"],
            frozenset({"Dachdeckerei Hollenbeck GmbH"}),
            apify_serp.WEB_PAGES,
        )
    ]
    assert calls.images == [MAIN_PHRASES]
    assert "search_all" not in recorder.calls
    assert "search_images" not in recorder.calls
    assert {
        (result.vertical, result.route) for result in raw.serps if result.engine == "google"
    } == {("web", "apify"), ("images", "apify")}
    assert raw.deferred_searches == []


async def test_collect_audit_sends_only_what_apify_did_not_answer_to_the_browser(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_apify(monkeypatch, web_failed=1, images_failed=1)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _with_apify(collector_settings), open_gate
    )

    assert recorder.calls["search_all"] == [(["Dachdeckerei Hollenbeck GmbH"],)]
    assert recorder.calls["search_images"] == [(["Flachdach Münster"],)]
    google = {(result.phrase, result.vertical, result.route) for result in raw.serps}
    assert ("Flachdach Münster", "web", "apify") in google
    assert ("Dachdeckerei Hollenbeck GmbH", "web", "browser") in google
    assert ("Flachdach Münster", "images", "browser") in google
    assert (
        "serps: Apify did not answer 1 of 3 Google searches: Apify daily or monthly cap reached"
    ) in raw.warnings


async def test_collect_audit_defers_what_neither_apify_nor_the_browser_could_search(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_apify(monkeypatch, web_failed=3)

    async def blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.google.de shows a bot check")

    monkeypatch.setattr(serp, "search_all", blocked)

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _with_apify(collector_settings), open_gate
    )

    assert [search.phrase for search in raw.deferred_searches if search.vertical == "web"] == [
        "Flachdach Münster",
        "Dachdecker Münster",
        "Dachdeckerei Hollenbeck GmbH",
    ]


async def test_preflight_passes_when_apify_answers_although_google_blocks_the_browser(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.md"
    board.write_text("## Eingang\n", encoding="utf-8")
    calls = _fake_apify(monkeypatch)

    async def blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.google.de shows a bot check")

    async def all_answer(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {}

    monkeypatch.setattr(serp, "search_all", blocked)
    monkeypatch.setattr(assistants, "probe", all_answer)
    settings = _with_apify(collector_settings).model_copy(
        update={"preflight_routes": True, "handoff_board_path": board, "halt_stages": (0, 1)}
    )

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    assert calls.web[0] == (["Dachdeckerei Hollenbeck GmbH"], frozenset(), run.PROBE_PAGES)
    assert board.read_text(encoding="utf-8") == "## Eingang\n"
    assert raw.deferred_searches == []


async def test_nationwide_keywords_are_searched_without_the_city(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    nationwide = _keyword("Flachdach", primary=False).model_copy(
        update={"scope": KeywordScope.NATIONWIDE}
    )
    repositories.keywords.return_value = [nationwide, _keyword("Dachdecker", primary=True)]

    raw = await run.collect_audit(AUDIT_ID, repositories, assistant_settings, open_gate)

    google_web = [
        result.phrase
        for result in raw.serps
        if (result.engine, result.vertical) == ("google", "web")
    ]
    assert google_web[:2] == ["Flachdach", "Dachdecker M\u00fcnster"]


async def test_unattended_audit_gives_a_blocked_chatgpt_to_the_api_and_to_nobody(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def chatgpt_blocked(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {"chatgpt": "redirected to the sign-in page", "google_ai": None, "claude": None}

    monkeypatch.setattr(assistants, "probe", chatgpt_blocked)
    settings = assistant_settings.model_copy(
        update={"preflight_routes": True, "assistant_handoff": False}
    )

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    assert recorder.calls["ask_all_handoff"][0] == (None,)
    assert ("assistant:chatgpt", True) in [(check.route, check.ok) for check in raw.preflight]


async def test_unattended_audit_without_an_api_key_halts_on_a_blocked_chatgpt(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def chatgpt_blocked(*args: object, **kwargs: object) -> dict[str, str | None]:
        return {"chatgpt": "redirected to the sign-in page", "google_ai": None, "claude": None}

    monkeypatch.setattr(assistants, "probe", chatgpt_blocked)
    settings = assistant_settings.model_copy(
        update={"preflight_routes": True, "assistant_handoff": False, "openai_api_key": None}
    )
    places = Places()

    with pytest.raises(AuditHalted, match="redirected to the sign-in page"):
        await run.collect_audit(
            AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(places)
        )

    assert "ask_all_handoff" not in recorder.calls


async def test_collect_audit_reports_the_reason_of_a_silent_assistant(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ask_all(
        questions: list[str], *args: object, reasons: dict[str, str], **kwargs: object
    ) -> list[AssistantAnswer]:
        reasons["google_ai"] = "2 of 2 AI mode questions failed: no AI mode result within 1200 s"
        return await _assistant_answers(["chatgpt", "claude"])(questions)

    async def collect_google_ads(*arguments: object) -> GoogleAdsSnapshot:
        return GoogleAdsSnapshot(
            target="dachdecker-hollenbeck.example",
            window_days=90,
            location_code=2276,
            returned_location_code=2276,
            ads=[],
            measured_at=datetime(2026, 10, 16, tzinfo=UTC),
        )

    async def collect_meta_ads(*arguments: object) -> MetaAdsSnapshot:
        return MetaAdsSnapshot(
            searched_name="Hollenbeck",
            actor="apify/facebook-ads-scraper",
            confirmed=False,
            measured_at=datetime(2026, 10, 16, tzinfo=UTC),
        )

    monkeypatch.setattr(assistants, "ask_all", ask_all)
    monkeypatch.setattr(ads, "collect_google_ads", collect_google_ads)
    monkeypatch.setattr(ads, "collect_meta_ads", collect_meta_ads)
    places = Places()

    with pytest.raises(AuditHalted, match="no AI mode result within 1200 s"):
        await run.collect_audit(
            AUDIT_ID, repositories, assistant_settings, open_gate, feedback=_feedback(places)
        )

    assert any(
        event.route == "assistant:google_ai"
        and event.result is Result.FAILED
        and "no AI mode result within 1200 s" in str(event.reason)
        for event in places.file
    )


async def test_collect_audit_stores_the_estimate_before_the_first_paid_request(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def balances(settings: Settings) -> estimate.Balances:
        return estimate.Balances(Decimal(5), Decimal(5), [])

    monkeypatch.setattr(estimate, "read_balances", balances)
    settings = assistant_settings.model_copy(update={"estimate_before_start": True})

    await run.collect_audit(
        AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(Places())
    )

    ((audit_id, lines, status, note),) = [
        call.args for call in repositories.save_estimate.call_args_list
    ]
    assert audit_id == AUDIT_ID
    assert status == estimate.COVERED
    assert note.startswith("Voranschlag")
    assert {line.source for line in lines} == {
        "dataforseo",
        "apify",
        "claude",
        "places",
        "chatgpt",
    }
    (chatgpt,) = [line for line in lines if line.source == "chatgpt"]
    price = openai_price.load_token_price(settings.routes_path, settings.openai_model)
    assert price is not None
    assert chatgpt.unit_cost > openai_price.max_call_cost_usd(price, 0)


async def test_collect_audit_refuses_to_start_when_the_credit_does_not_cover_the_estimate(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def balances(settings: Settings) -> estimate.Balances:
        return estimate.Balances(Decimal("0.10"), None, [])

    monkeypatch.setattr(estimate, "read_balances", balances)
    settings = assistant_settings.model_copy(update={"estimate_before_start": True})

    with pytest.raises(PreflightError, match="Kostenvoranschlag nicht gedeckt: dataforseo"):
        await run.collect_audit(
            AUDIT_ID, repositories, settings, open_gate, feedback=_feedback(Places())
        )

    ((_, _, status, _),) = [call.args for call in repositories.save_estimate.call_args_list]
    assert status == estimate.INSUFFICIENT
    assert "ask_all_handoff" not in recorder.calls


async def test_collect_audit_prices_only_the_sources_that_are_switched_on(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def balances(settings: Settings) -> estimate.Balances:
        return estimate.Balances(Decimal(5), Decimal(5), [])

    monkeypatch.setattr(estimate, "read_balances", balances)
    sources = SourcesSettings.model_validate({"dataforseo": {"enabled": False, "daily_cap": 10}})
    gate = SourceGate(sources, lambda source, cap: True)
    settings = assistant_settings.model_copy(update={"estimate_before_start": True})

    await run.collect_audit(AUDIT_ID, repositories, settings, gate, feedback=_feedback(Places()))

    ((_, lines, _, _),) = [call.args for call in repositories.save_estimate.call_args_list]
    assert "dataforseo" not in {line.source for line in lines}


async def test_a_restart_measures_only_the_step_that_failed(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bing_blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.bing.com shows a bot check")

    monkeypatch.setattr(serp, "search_bing", bing_blocked)
    await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)
    assert set(run.StepStore(AUDIT_ID, repositories).failed()) == {"bing"}

    async def bing_answers(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        recorder.record("search_bing_again", phrases)
        return serp.SearchBatch([_serp(phrase, "bing") for phrase in phrases], [])

    monkeypatch.setattr(serp, "search_bing", bing_answers)
    measured_before = {name: len(calls) for name, calls in recorder.calls.items()}

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    measured_after = {name: len(calls) for name, calls in recorder.calls.items()}
    repeated = {
        name
        for name, count in measured_after.items()
        if count != measured_before.get(name, 0) and name != "search_bing_again"
    }
    assert repeated == set()
    assert recorder.calls["search_bing_again"] == [(MAIN_PHRASES,)]
    assert [result.phrase for result in raw.serps if result.engine == "bing"] == MAIN_PHRASES
    assert raw.website == WEBSITE
    assert raw.pagespeed == SPEED
    assert run.StepStore(AUDIT_ID, repositories).failed() == {}


async def test_a_collection_cut_off_midway_does_not_repeat_the_finished_steps(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def server_stops(*args: object, **kwargs: object) -> PaidSnapshot:
        raise SystemExit("container stopped")

    monkeypatch.setattr(paid_data, "collect_paid", server_stops)
    with pytest.raises(SystemExit):
        await run.collect_audit(
            AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
        )
    finished = {name: len(calls) for name, calls in recorder.calls.items()}
    monkeypatch.setattr(paid_data, "collect_paid", _fake_paid([]))

    raw = await run.collect_audit(
        AUDIT_ID, repositories, _paid_settings(collector_settings), open_gate
    )

    assert {name: len(calls) for name, calls in recorder.calls.items()} == finished
    assert raw.paid is not None
    assert raw.website == WEBSITE
    assert len(raw.grid) == GRID_POINTS


async def test_a_probe_measures_one_phrase_and_stores_nothing(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bing_blocked(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        return serp.SearchBatch([], phrases, "www.bing.com shows a bot check")

    monkeypatch.setattr(serp, "search_bing", bing_blocked)
    await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)
    steps = run.StepStore(AUDIT_ID, repositories)
    probed: list[list[str]] = []

    async def bing_answers(browser: BrowserService, phrases: list[str]) -> serp.SearchBatch:
        probed.append(phrases)
        return serp.SearchBatch([_serp(phrase, "bing") for phrase in phrases], [])

    monkeypatch.setattr(serp, "search_bing", bing_answers)

    await run.collect_audit(
        AUDIT_ID,
        repositories,
        collector_settings,
        open_gate,
        only=frozenset({"bing"}),
        probe=True,
        steps=steps,
    )

    assert probed == [MAIN_PHRASES[:1]]
    assert steps.probe_failure("bing") is None
    assert set(run.StepStore(AUDIT_ID, repositories).failed()) == {"bing"}


MEASURED_STEPS = {
    "company",
    "website",
    "pagespeed",
    "search_google",
    "search_bing",
    "search_images",
    "place",
    "links",
    "assistants",
    "listings",
    "ads_google",
    "ads_meta",
}


def _measurement_events(recorded: Places) -> list[AuditEvent]:
    return [event for event in recorded.file if event.kind == "measurement"]


async def test_collect_audit_protocols_every_measurement_step(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    settings = _paid_settings(assistant_settings).model_copy(
        update={"apify_token": SecretStr("token")}
    )
    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    assert sorted(record.step for record in raw.measurements) == sorted(MEASURED_STEPS)
    assert all(record.measured_at.tzinfo is UTC for record in raw.measurements)
    place = next(record for record in raw.measurements if record.step == "place")
    assert (place.technical_status, place.record_count) == ("ok", 1)
    assert raw.has_location is True


async def test_collect_audit_marks_a_customer_without_location(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    repositories.customer.return_value = _customer(website_url=None)
    repositories.locations.return_value = []

    raw = await run.collect_audit(AUDIT_ID, repositories, collector_settings, open_gate)

    assert raw.has_location is False


async def test_collect_audit_reports_every_step_as_running_then_valid_or_invalid(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    recorded = Places()
    settings = _paid_settings(assistant_settings).model_copy(
        update={"apify_token": SecretStr("token")}
    )

    raw = await run.collect_audit(
        AUDIT_ID,
        repositories,
        settings,
        open_gate,
        feedback=_feedback(recorded),
    )

    events = _measurement_events(recorded)
    for step in MEASURED_STEPS:
        own = [event for event in events if event.route == f"messung:{step}"]
        if step in HELD_BACK_STEPS:
            assert [event.result for event in own] == [Result.INVALID], step
            continue
        assert [event.result for event in own][:2] == [Result.RUNNING, Result.RUNNING], step
        assert [event.protocol["stage"] for event in own[:2] if event.protocol] == [
            "holen",
            "pruefen",
        ], step
        assert len(own) == 3, step
        assert own[2].result in (Result.VALID, Result.INVALID), step
        assert own[2].protocol is not None and own[2].protocol["step"] == step
    valid = {record.step: record.valid for record in raw.measurements}
    for event in events:
        if event.result is not Result.RUNNING:
            assert (event.result is Result.VALID) == valid[event.route.removeprefix("messung:")]


HELD_BACK_STEPS = {"ads_meta"}


async def test_the_meta_ads_step_is_held_back_and_logged_while_its_format_is_unconfirmed(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    async def collect_meta_ads(*arguments: object) -> MetaAdsSnapshot:
        asked.append("meta")
        raise AssertionError("a paid call must not run for an unconfirmed route")

    monkeypatch.setattr(ads, "collect_meta_ads", collect_meta_ads)
    settings = _paid_settings(assistant_settings).model_copy(
        update={"apify_token": SecretStr("token")}
    )

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    meta = next(record for record in raw.measurements if record.step == "ads_meta")
    assert asked == []
    assert meta.valid is False
    assert meta.reason == "nicht gemessen: Ausgabeformat noch nicht an echtem Abruf bestätigt"
    assert raw.ads_meta is None


async def test_the_google_ads_step_is_held_back_and_logged_without_a_price(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    async def collect_google_ads(*arguments: object) -> GoogleAdsSnapshot:
        asked.append("google")
        raise AssertionError("a paid call must not run without a price")

    monkeypatch.setattr(ads, "collect_google_ads", collect_google_ads)
    routes = tmp_path / "messwege.yaml"
    text = (CONFIG / "messwege.yaml").read_text(encoding="utf-8")
    routes.write_text(text.replace('price_usd_per_task: "0.0006", ', ""), encoding="utf-8")
    settings = _paid_settings(assistant_settings).model_copy(update={"routes_path": routes})

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    google = next(record for record in raw.measurements if record.step == "ads_google")
    assert asked == []
    assert google.valid is False
    assert google.reason.startswith("nicht gemessen: Preis nicht hinterlegt")
    assert raw.ads_google is None


async def test_a_failing_ads_step_ends_as_an_invalid_record_and_never_stops_the_audit(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect_google_ads(*arguments: object) -> GoogleAdsSnapshot:
        raise RuntimeError("endpoint unknown")

    monkeypatch.setattr(ads, "collect_google_ads", collect_google_ads)
    settings = _paid_settings(assistant_settings)

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    google = next(record for record in raw.measurements if record.step == "ads_google")
    assert google.valid is False
    assert raw.ads_google is None
    results = evaluate(AUDIT_ID, raw, load_catalogue(CONFIG / "pruefkatalog.yaml"))
    p1 = next(item for item in results if item.check_id == "p1")
    assert p1.outcome is CheckOutcome.NOT_MEASURABLE


async def test_collect_audit_logs_a_step_that_could_not_run_as_invalid(
    repositories: MagicMock, recorder: Recorder, collector_settings: Settings, open_gate: SourceGate
) -> None:
    recorded = Places()

    raw = await run.collect_audit(
        AUDIT_ID, repositories, collector_settings, open_gate, feedback=_feedback(recorded)
    )

    links = next(record for record in raw.measurements if record.step == "links")
    assert (links.valid, links.technical_status) == (False, "failed")
    links_events = [
        event.result for event in _measurement_events(recorded) if event.route == "messung:links"
    ]
    assert links_events == [Result.RUNNING, Result.RUNNING, Result.INVALID]


async def test_collect_audit_does_not_call_the_switched_off_collectors(
    repositories: MagicMock, recorder: Recorder, assistant_settings: Settings, open_gate: SourceGate
) -> None:
    settings = assistant_settings.model_copy(update={"apify_token": SecretStr("token")})

    raw = await run.collect_audit(AUDIT_ID, repositories, settings, open_gate)

    for switched_off in SWITCHED_OFF_COLLECTORS:
        assert switched_off not in recorder.calls
    assert raw.social is None
    assert raw.domain_first_capture is None
    assert raw.directories == []


async def test_collect_audit_hands_apple_and_bing_to_the_listing_routes(
    repositories: MagicMock,
    recorder: Recorder,
    assistant_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    async def map_listings_of(context: object, notes: object) -> list[DirectoryHit]:
        asked.append("map_listings")
        return [
            DirectoryHit(
                directory="Apple Maps",
                found=True,
                listing_url=None,
                name=None,
                address=None,
                phone=None,
            )
        ]

    monkeypatch.setattr(run, "_map_listings", map_listings_of)

    raw = await run.collect_audit(AUDIT_ID, repositories, assistant_settings, open_gate)

    assert asked == ["map_listings"]
    assert [hit.directory for hit in raw.directories] == ["Apple Maps"]


def test_the_profile_ways_follow_the_route_file(collector_settings: Settings) -> None:
    assert run._profile_ways(collector_settings) == ("places_api", "dataforseo", "browser")


def test_a_profile_way_the_worker_cannot_run_is_refused(
    collector_settings: Settings, tmp_path: Path
) -> None:
    text = (CONFIG / "messwege.yaml").read_text(encoding="utf-8")
    browser_way = '{supplier: browser, endpoint: "Google Maps im Browser"'
    assert browser_way in text
    changed = tmp_path / "messwege.yaml"
    changed.write_text(
        text.replace(browser_way, '{supplier: apify, endpoint: "Google Maps im Browser"'),
        encoding="utf-8",
    )
    settings = collector_settings.model_copy(update={"routes_path": changed})

    with pytest.raises(ConfigurationError, match="cannot run"):
        run._profile_ways(settings)


async def _profile_case(
    monkeypatch: pytest.MonkeyPatch,
    repositories: MagicMock,
    settings: Settings,
    gate: SourceGate,
    *,
    find: Callable[..., Awaitable[str | None]],
    browser: Callable[..., Awaitable[PlaceSnapshot]],
) -> dict[str, tuple[CheckOutcome, dict[str, object]]]:
    monkeypatch.setattr(places, "find_place", find)
    monkeypatch.setattr(maps, "read_profile", browser)
    raw = await run.collect_audit(AUDIT_ID, repositories, settings, gate)
    catalogue = load_catalogue(CONFIG / "pruefkatalog.yaml")
    results = evaluate(AUDIT_ID, raw, catalogue)
    wanted = {"f1", "f2", "g1", "g2", "j3"}
    return {
        item.check_id: (item.outcome, item.measured) for item in results if item.check_id in wanted
    }


async def _not_found(*arguments: object) -> str | None:
    return None


async def _unreadable(*arguments: object) -> str | None:
    raise CollectionError("Places API answered HTTP 503")


async def _gone(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
    raise ProfileNotFoundError("no place page")


async def _broken_page(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
    raise CollectionError("layout changed")


async def test_a_profile_that_was_searched_and_does_not_exist_leaves_every_profile_check_unmeasured(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = await _profile_case(
        monkeypatch,
        repositories,
        collector_settings,
        open_gate,
        find=_not_found,
        browser=_gone,
    )

    assert outcomes["f1"] == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "profile_not_provable", "profile_found": False},
    )
    for check_id in ("f2", "g1", "g2", "j3"):
        assert outcomes[check_id] == (
            CheckOutcome.NOT_MEASURABLE,
            {"reason": "profile_not_provable", "profile_found": False},
        )


async def test_no_profile_is_not_claimed_when_the_business_data_of_the_search_was_incomplete(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run, "_business_facts", lambda context: BusinessFacts(name="Dachdeckerei", city="Münster")
    )

    outcomes = await _profile_case(
        monkeypatch,
        repositories,
        collector_settings,
        open_gate,
        find=_not_found,
        browser=_gone,
    )

    assert {outcome for outcome, _ in outcomes.values()} == {CheckOutcome.NOT_MEASURABLE}
    assert {measured["reason"] for _, measured in outcomes.values()} == {"measurement_invalid"}


async def test_a_failed_profile_search_measures_none_of_the_profile_checks(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = await _profile_case(
        monkeypatch,
        repositories,
        collector_settings,
        open_gate,
        find=_unreadable,
        browser=_broken_page,
    )

    assert {outcome for outcome, _ in outcomes.values()} == {CheckOutcome.NOT_MEASURABLE}
    assert {measured["reason"] for _, measured in outcomes.values()} == {"measurement_invalid"}


async def test_a_foreign_profile_is_invalid_and_measures_none_of_the_profile_checks(
    repositories: MagicMock,
    recorder: Recorder,
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def other_company(name: str, address: str, api_key: SecretStr) -> str | None:
        return "id-Bäckerei Schmitz"

    async def unreachable(browser: BrowserService, target: ProfileTarget) -> PlaceSnapshot:
        raise CollectionError("layout changed")

    outcomes = await _profile_case(
        monkeypatch,
        repositories,
        collector_settings,
        open_gate,
        find=other_company,
        browser=unreachable,
    )

    assert {outcome for outcome, _ in outcomes.values()} == {CheckOutcome.NOT_MEASURABLE}
    assert {measured["reason"] for _, measured in outcomes.values()} == {"measurement_invalid"}
    assert "fremdes Profil" in str(outcomes["f1"][1]["invalid_reason"])
