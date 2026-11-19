"""Tests for the identify step of an audit started with only the website address.

Context: Saved legal notice pages, a faked legal notice reader, Places Text Search replaced by a
fake and fake repositories. | Input: Fixtures. | Output: Assertions.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, call
from uuid import uuid4

import pytest
from pydantic import HttpUrl, SecretStr

from radarauditor.collectors import identify, legal_notice, places
from radarauditor.collectors.places import BusinessCandidate
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import BusinessFacts, IdentifiedValue, WebsiteSnapshot
from radarauditor.db.app_settings import SourcesSettings
from radarauditor.db.models import Customer, Keyword, Location
from radarauditor.errors import CollectionError, IdentificationError, StorageError
from radarauditor.industries import IndustryCatalogue, IndustryGroup, load_industries

FIXTURES = Path(__file__).parent / "fixtures" / "identify"
INDUSTRIES = Path(__file__).resolve().parents[1] / "config" / "branchen.yaml"
KEY = SecretStr("google-key")
CUSTOMER_ID = uuid4()
HOLLENBECK = "https://www.dachdecker-hollenbeck.de/"
HOST = "dachdecker-hollenbeck.de"

pytestmark = pytest.mark.usefixtures("fast_retries")


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _facts(name: str) -> BusinessFacts:
    return legal_notice.read_business(legal_notice.notice_lines(_html(name)))


class FakeNotice:
    """Stands in for legal_notice.read_site: gives the prepared facts or raises the error."""

    def __init__(self, facts: BusinessFacts | None = None, error: CollectionError | None = None):
        self.facts = facts or BusinessFacts()
        self.error = error
        self.sites: list[WebsiteSnapshot] = []

    async def __call__(self, site: WebsiteSnapshot) -> BusinessFacts:
        self.sites.append(site)
        if self.error is not None:
            raise self.error
        return self.facts


def _notice(monkeypatch: pytest.MonkeyPatch, notice: FakeNotice) -> FakeNotice:
    monkeypatch.setattr(legal_notice, "read_site", notice)
    return notice


@pytest.fixture(autouse=True)
def no_legal_notice(monkeypatch: pytest.MonkeyPatch) -> FakeNotice:
    return _notice(monkeypatch, FakeNotice())


def _customer(
    name: str = HOST,
    city: str = "",
    industry: str = "",
    website: str = HOLLENBECK,
) -> Customer:
    return Customer(
        id=CUSTOMER_ID,
        name=name,
        website=HttpUrl(website),
        industry=industry,
        city=city,
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
    )


def _location(**values: object) -> Location:
    row: dict[str, object] = {
        "id": uuid4(),
        "customer_id": CUSTOMER_ID,
        "name": HOST,
        "street": "",
        "postal_code": "",
        "city": "",
        "phone": "",
        "google_place_id": None,
        "latitude": None,
        "longitude": None,
    }
    row.update(values)
    return Location.model_validate(row)


def _site() -> WebsiteSnapshot:
    return WebsiteSnapshot(
        start_url=HttpUrl(HOLLENBECK),
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


def _candidate(**values: object) -> BusinessCandidate:
    fields: dict[str, object] = {
        "place_id": "ChIJ-hollenbeck",
        "name": "Dachdeckerei Hollenbeck GmbH",
        "formatted_address": "Hiltruper Str. 210, 48165 Münster, Deutschland",
        "phone": "0251 123456",
        "website": HOLLENBECK,
        "primary_type": "roofing_contractor",
        "category": "Dachdecker",
        "types": ("roofing_contractor", "point_of_interest", "establishment"),
        "latitude": 51.92,
        "longitude": 7.63,
    }
    fields.update(values)
    return BusinessCandidate(**fields)  # type: ignore[arg-type]


class FakeSearch:
    """Stands in for places.search_businesses and records the queries."""

    def __init__(self, *answers: list[BusinessCandidate]) -> None:
        """Answer the queries in order; without answers every search finds nothing."""
        self.answers = list(answers)
        self.queries: list[str] = []

    async def __call__(self, query: str, api_key: SecretStr) -> list[BusinessCandidate]:
        """Record the query and return the next answer."""
        self.queries.append(query)
        return self.answers.pop(0) if self.answers else []


@pytest.fixture
def industries() -> IndustryCatalogue:
    """The real industry groups."""
    return load_industries(INDUSTRIES)


@pytest.fixture
def repositories(fake_repositories: MagicMock) -> MagicMock:
    """Repositories that store rows and give back what they were handed."""
    fake_repositories.create_location.side_effect = lambda location: location
    fake_repositories.link_crm_customer.return_value = uuid4()

    def add(
        customer_id: object, phrases: list[str], city: str, *, first_primary: bool
    ) -> list[Keyword]:
        return [
            Keyword(
                id=uuid4(),
                customer_id=CUSTOMER_ID,
                phrase=phrase,
                city=city,
                monthly_volume=None,
                is_primary=first_primary and index == 0,
            )
            for index, phrase in enumerate(phrases)
        ]

    fake_repositories.add_keywords.side_effect = add
    return fake_repositories


async def _run(
    customer: Customer,
    site: WebsiteSnapshot | None,
    repositories: MagicMock,
    industries: IndustryCatalogue,
    gate: SourceGate,
    *,
    location: Location | None = None,
    keywords: list[Keyword] | None = None,
    notes: list[str] | None = None,
) -> identify.Identification:
    return await identify.identify_customer(
        customer,
        location if location is not None else _location(),
        keywords or [],
        site,
        repositories=repositories,
        industries=industries,
        gate=gate,
        api_key=KEY,
        notes=notes if notes is not None else [],
    )


def _keyword(phrase: str, city: str) -> Keyword:
    return Keyword(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        phrase=phrase,
        city=city,
        monthly_volume=None,
        is_primary=False,
    )


def _sources(result: identify.Identification) -> dict[str, tuple[str, str]]:
    return {item.field: (item.value, item.source) for item in result.identified}


def test_legal_notice_with_company_street_and_postal_line_on_own_lines() -> None:
    """The company stands above the street, the street above the postal code and city."""
    facts = _facts("impressum_lines.html")

    assert facts.name == "Dachdeckerei Hollenbeck GmbH"
    assert (facts.street, facts.postal_code, facts.city) == (
        "Hiltruper Straße 210",
        "48165",
        "Münster",
    )
    assert facts.phone == "0251 123456"


def test_legal_notice_with_labelled_company_and_address_on_one_line() -> None:
    """A labelled company and "Weseler Str. 7, D-48151 Münster" on one line are understood."""
    facts = _facts("impressum_one_line.html")

    assert facts.name == "Malerbetrieb Kortmann e.K."
    assert (facts.street, facts.postal_code, facts.city) == ("Weseler Str. 7", "48151", "Münster")
    assert facts.phone == "+49 (0) 251 / 77 88 99"


def test_legal_notice_takes_no_label_as_company() -> None:
    """A label such as "Anschrift:" above the street is no company name."""
    facts = _facts("impressum_label_above.html")

    assert facts.name is None
    assert (facts.street, facts.city) == ("Musterstraße 12a", "Münster")


def test_legal_notice_without_postal_address_says_nothing() -> None:
    """A post office box gives postal code and city of the business, but no street."""
    facts = _facts("impressum_without_address.html")

    assert facts.street is None
    assert (facts.postal_code, facts.city) == ("48001", "Münster")


async def test_identifies_from_the_legal_notice_and_confirms_on_google(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name and address from the legal notice; Google adds place, categories and coordinates."""
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    search = FakeSearch([_candidate()])
    monkeypatch.setattr(places, "search_businesses", search)
    location = _location()

    result = await _run(
        _customer(), _site(), repositories, industries, open_gate, location=location
    )

    assert search.queries == ["Dachdeckerei Hollenbeck GmbH Münster"]
    assert _sources(result) == {
        "name": ("Dachdeckerei Hollenbeck GmbH", "impressum"),
        "city": ("Münster", "impressum"),
        "industry": ("bauhandwerk", "google_types"),
        "street": ("Hiltruper Straße 210", "impressum"),
        "postal_code": ("48165", "impressum"),
        "phone": ("0251 123456", "impressum"),
        "google_place_id": ("ChIJ-hollenbeck", "google_profile"),
        "keywords": (
            "Dachdecker Münster, Maler Münster, Fliesenleger Münster",
            "industry_services",
        ),
    }
    assert repositories.update_customer.call_args_list == [
        call(CUSTOMER_ID, name="Dachdeckerei Hollenbeck GmbH", city="Münster"),
        call(CUSTOMER_ID, industry="bauhandwerk"),
    ]
    repositories.link_crm_customer.assert_called_once_with(CUSTOMER_ID)
    assert repositories.update_location.call_args_list[0] == call(
        location.id,
        name="Dachdeckerei Hollenbeck GmbH",
        street="Hiltruper Straße 210",
        postal_code="48165",
        city="Münster",
        phone="0251 123456",
    )
    repositories.add_keywords.assert_called_once_with(
        CUSTOMER_ID, ["Dachdecker", "Maler", "Fliesenleger"], "Münster", first_primary=True
    )
    assert result.customer.city == "Münster"
    assert result.location is not None and result.location.latitude == 51.92
    assert result.business == _facts("impressum_lines.html")
    assert [(item.phrase, item.city, item.is_primary) for item in result.keywords] == [
        ("Dachdecker", "Münster", True),
        ("Maler", "Münster", False),
        ("Fliesenleger", "Münster", False),
    ]


async def test_a_recorded_customer_without_street_gets_it_from_the_legal_notice(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded customer without street gets street, postal code and phone from the notice.

    Name, city, industry and keywords are recorded, so Google is not asked.
    """
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    search = FakeSearch()
    monkeypatch.setattr(places, "search_businesses", search)
    name = "Dachdeckerei Hollenbeck GmbH"
    customer = _customer(name=name, city="Münster", industry="bauhandwerk")
    location = _location(name=name, city="Münster", latitude=51.92, longitude=7.63)

    result = await _run(
        customer,
        _site(),
        repositories,
        industries,
        open_gate,
        location=location,
        keywords=[_keyword("Dachdecker", "Münster")],
    )

    assert _sources(result) == {
        "street": ("Hiltruper Straße 210", "impressum"),
        "postal_code": ("48165", "impressum"),
        "phone": ("0251 123456", "impressum"),
    }
    repositories.update_location.assert_called_once_with(
        location.id,
        name=None,
        street="Hiltruper Straße 210",
        postal_code="48165",
        city=None,
        phone="0251 123456",
    )
    assert result.location is not None and result.location.street == "Hiltruper Straße 210"
    assert search.queries == []


async def test_the_website_host_is_searched_when_the_notice_names_only_the_city(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a name in the notice the host is searched; a profile linking it is the customer."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(city="Münster")))
    search = FakeSearch([_candidate(website="http://dachdecker-hollenbeck.de")])
    monkeypatch.setattr(places, "search_businesses", search)
    notes: list[str] = []

    result = await _run(_customer(), _site(), repositories, industries, open_gate, notes=notes)

    assert search.queries == [HOST]
    sources = _sources(result)
    assert sources["city"] == ("Münster", "impressum")
    assert sources["industry"] == ("bauhandwerk", "google_types")
    assert sources["google_place_id"] == ("ChIJ-hollenbeck", "google_profile")
    assert "name" not in sources
    assert "business name not found, the website host stays the name" in notes


async def test_a_google_profile_of_another_business_is_not_taken(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with another website and another name is not taken, and the audit goes on."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(city="Münster")))
    other = _candidate(name="Bedachungen Overmann", website="https://overmann.de/")
    monkeypatch.setattr(places, "search_businesses", FakeSearch([other]))
    notes: list[str] = []

    result = await _run(_customer(), _site(), repositories, industries, open_gate, notes=notes)

    assert f"no Google profile clearly belongs to {HOST}" in notes
    assert "google_place_id" not in _sources(result)
    assert result.location is not None and result.location.latitude is None


async def test_a_failed_google_search_is_noted_and_the_notice_decides(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Places error (e.g. the daily cap) leaves a note; the city of the notice stays."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(city="Münster")))

    async def failing_search(query: str, api_key: SecretStr) -> list[BusinessCandidate]:
        raise CollectionError("Places API: daily limit reached")

    monkeypatch.setattr(places, "search_businesses", failing_search)
    notes: list[str] = []

    result = await _run(_customer(), _site(), repositories, industries, open_gate, notes=notes)

    assert "Google profile not searched: Places API: daily limit reached" in notes
    assert result.customer.city == "Münster"


def test_a_shared_platform_host_alone_does_not_confirm_a_profile() -> None:
    """A website on facebook.com confirms no Google profile that also links facebook.com."""
    foreign = _candidate(name="Bedachungen Overmann", website="https://www.facebook.com/overmann")

    assert (
        identify._is_customer(foreign, "facebook.com", "Dachdeckerei Hollenbeck", "Münster")
        is False
    )
    assert identify._is_customer(foreign, HOST, None, None) is False
    own = _candidate(website="https://www.facebook.com/hollenbeck")
    assert identify._is_customer(own, "facebook.com", "Dachdeckerei Hollenbeck GmbH", "Münster")


async def test_missing_city_fails_with_the_german_message(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notice without a city and no city from the start leave the city unknown."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(name="Studio Lichtblick")))
    search = FakeSearch([_candidate()])
    monkeypatch.setattr(places, "search_businesses", search)

    with pytest.raises(IdentificationError) as raised:
        await _run(_customer(), _site(), repositories, industries, open_gate)

    assert str(raised.value) == identify.CITY_NOT_FOUND
    assert search.queries == []


async def test_a_missing_legal_notice_is_noted_and_fails_without_a_city(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a legal notice nothing is read from the website; the start city is needed."""
    monkeypatch.setattr(places, "search_businesses", FakeSearch([_candidate()]))
    notes: list[str] = []

    with pytest.raises(IdentificationError) as raised:
        await _run(_customer(), _site(), repositories, industries, open_gate, notes=notes)

    assert str(raised.value) == identify.CITY_NOT_FOUND
    assert identify.NO_LEGAL_NOTICE in notes


async def test_given_values_stay_and_only_keywords_are_added(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name, city and industry entered in the app are kept; Google is asked for coordinates only."""
    search = FakeSearch([_candidate()])
    monkeypatch.setattr(places, "search_businesses", search)
    customer = _customer(name="Dachdeckerei Hollenbeck GmbH", city="Münster", industry="kfz")
    location = _location(name="Dachdeckerei Hollenbeck GmbH", city="Münster")

    result = await _run(customer, _site(), repositories, industries, open_gate, location=location)

    assert search.queries == ["Dachdeckerei Hollenbeck GmbH Münster"]
    assert result.customer == customer
    assert [item.field for item in result.identified] == ["google_place_id", "keywords"]
    repositories.update_customer.assert_not_called()
    repositories.link_crm_customer.assert_not_called()
    repositories.add_keywords.assert_called_once_with(
        CUSTOMER_ID, ["Autowerkstatt", "Reifenwechsel", "Autohaus"], "Münster", first_primary=True
    )


async def test_nothing_missing_changes_nothing(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully recorded customer is neither searched nor written."""
    search = FakeSearch([_candidate()])
    monkeypatch.setattr(places, "search_businesses", search)
    customer = _customer(name="Dachdeckerei Hollenbeck", city="Münster", industry="bauhandwerk")
    keyword = Keyword(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        phrase="Dachdecker",
        city="Münster",
        monthly_volume=None,
        is_primary=True,
    )
    location = _location(
        name="Dachdeckerei Hollenbeck", city="Münster", latitude=51.9, longitude=7.6
    )

    result = await _run(
        customer, None, repositories, industries, open_gate, location=location, keywords=[keyword]
    )

    assert result == identify.Identification(customer, location, [keyword])
    assert search.queries == []
    repositories.update_customer.assert_not_called()
    repositories.update_location.assert_not_called()
    repositories.add_keywords.assert_not_called()


async def test_unknown_industry_leaves_it_empty_and_creates_no_keywords(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile category outside every group and a neutral name give no industry; it goes on."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(city="Münster")))
    neutral = _candidate(
        name="Lichtblick Fotografie",
        website="https://lichtblick-fotografie.de/",
        primary_type="event_venue",
        category="Eventlocation",
        types=("event_venue",),
    )
    monkeypatch.setattr(places, "search_businesses", FakeSearch([neutral]))
    notes: list[str] = []

    result = await _run(
        _customer(name="lichtblick-fotografie.de", website="https://lichtblick-fotografie.de/"),
        _site(),
        repositories,
        industries,
        open_gate,
        location=_location(name="lichtblick-fotografie.de"),
        notes=notes,
    )

    assert result.customer.industry == ""
    assert result.keywords == []
    assert "industry" not in _sources(result)
    assert "industry not recognised, checks that need it are not measurable" in notes
    assert "no keywords: the industry is not known" in notes
    repositories.add_keywords.assert_not_called()


async def test_keywords_stored_without_city_get_the_identified_city(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keywords given at the start without a city receive it; no service keywords are added."""
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    monkeypatch.setattr(places, "search_businesses", FakeSearch([_candidate()]))
    flat, gutter = _keyword("Flachdach", ""), _keyword("Dachrinne", "")

    result = await _run(
        _customer(), _site(), repositories, industries, open_gate, keywords=[flat, gutter]
    )

    repositories.update_keywords_city.assert_called_once_with([flat.id, gutter.id], "Münster")
    repositories.add_keywords.assert_not_called()
    assert [(item.phrase, item.city) for item in result.keywords] == [
        ("Flachdach", "Münster"),
        ("Dachrinne", "Münster"),
    ]


async def test_a_keyword_already_tracked_in_the_city_keeps_no_city(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same phrase cannot be tracked twice in one city, so the copy without city is left."""
    monkeypatch.setattr(places, "search_businesses", FakeSearch())
    customer = _customer(name="Dachdeckerei Hollenbeck", city="Münster", industry="bauhandwerk")
    without_city, tracked = _keyword("Flachdach", ""), _keyword("Flachdach", "Münster")
    notes: list[str] = []

    result = await _run(
        customer,
        None,
        repositories,
        industries,
        open_gate,
        location=_location(
            name="Dachdeckerei Hollenbeck", city="Münster", latitude=51.9, longitude=7.6
        ),
        keywords=[without_city, tracked],
        notes=notes,
    )

    repositories.update_keywords_city.assert_not_called()
    assert result.keywords == [tracked]
    assert "keyword Flachdach keeps no city: it is already tracked in Münster" in notes


async def test_keyword_cities_that_cannot_be_stored_are_noted(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write leaves the keywords without city out of the audit and says why."""
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    monkeypatch.setattr(places, "search_businesses", FakeSearch([_candidate()]))
    repositories.update_keywords_city.side_effect = StorageError("update rejected")
    notes: list[str] = []

    result = await _run(
        _customer(),
        _site(),
        repositories,
        industries,
        open_gate,
        keywords=[_keyword("Flachdach", ""), _keyword("Dachrinne", "")],
        notes=notes,
    )

    assert result.keywords == []
    assert "keywords Flachdach, Dachrinne keep no city: update rejected" in notes


async def test_an_unreadable_legal_notice_is_noted_and_the_start_city_is_used(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal notice that cannot be read adds a note; a city given at the start still counts."""
    _notice(monkeypatch, FakeNotice(error=CollectionError("legal notice answered HTTP 404")))
    monkeypatch.setattr(places, "search_businesses", FakeSearch())
    notes: list[str] = []

    result = await _run(
        _customer(city="Münster"), _site(), repositories, industries, open_gate, notes=notes
    )

    assert "legal notice not readable: legal notice answered HTTP 404" in notes
    assert identify.NO_LEGAL_NOTICE in notes
    assert result.customer.city == "Münster"


async def test_a_failed_crm_link_is_noted_and_the_audit_goes_on(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identified values stay stored when the CRM cannot be linked."""
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    monkeypatch.setattr(places, "search_businesses", FakeSearch([_candidate()]))
    repositories.link_crm_customer.side_effect = StorageError("crm unreachable")
    notes: list[str] = []

    result = await _run(_customer(), _site(), repositories, industries, open_gate, notes=notes)

    assert "customer not linked to the CRM: crm unreachable" in notes
    assert result.customer.city == "Münster"
    assert repositories.update_customer.called


async def test_a_customer_without_location_gets_one(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name and address of the legal notice become the first location."""
    _notice(monkeypatch, FakeNotice(_facts("impressum_lines.html")))
    monkeypatch.setattr(places, "search_businesses", FakeSearch([_candidate()]))

    result = await identify.identify_customer(
        _customer(),
        None,
        [],
        _site(),
        repositories=repositories,
        industries=industries,
        gate=open_gate,
        api_key=KEY,
        notes=[],
    )

    created = repositories.create_location.call_args.args[0]
    assert (created.name, created.street, created.postal_code, created.city) == (
        "Dachdeckerei Hollenbeck GmbH",
        "Hiltruper Straße 210",
        "48165",
        "Münster",
    )
    assert result.location is not None and result.location.street == created.street


def test_keyword_suggestions_follow_the_app() -> None:
    """Same rule as keywordSuggestions in app/src/lib/fieldAudit.ts."""
    groups = load_industries(INDUSTRIES)
    group = groups.resolve("haustechnik")

    assert identify.keyword_suggestions(group, " Münster ") == [
        "Elektriker Münster",
        "Heizung Notdienst Münster",
        "Sanitär Notdienst Münster",
    ]
    assert identify.keyword_suggestions(group, "  ") == []
    assert identify.keyword_suggestions(None, "Münster") == []


def test_keyword_suggestions_name_each_stored_service_once() -> None:
    """The noted suggestions are the stored phrases plus the city, trimmed and without repeats."""
    group = IndustryGroup(
        id="bauhandwerk",
        label="Bauhandwerk",
        examples=[],
        services=[" Dachdecker", "Dachdecker", "Maler "],
        question_term="Handwerker",
    )

    assert identify.keyword_suggestions(group, "Münster") == ["Dachdecker Münster", "Maler Münster"]


def test_identified_values_round_trip_in_the_raw_audit() -> None:
    """The app reads field, value and source from the stored raw data."""
    value = IdentifiedValue(field="city", value="Münster", source="impressum")

    assert IdentifiedValue.model_validate_json(value.model_dump_json()) == value


async def test_the_google_search_books_its_price(
    repositories: MagicMock,
    industries: IndustryCatalogue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identify search asks for phone and website, so it costs a Text Search Enterprise."""
    _notice(monkeypatch, FakeNotice(BusinessFacts(city="Münster")))
    monkeypatch.setattr(
        places,
        "search_businesses",
        FakeSearch([_candidate(website="http://dachdecker-hollenbeck.de")]),
    )
    ledger = MagicMock()
    ledger.month_source_cost.return_value = Decimal(0)
    ledger.add_source_cost.return_value = Decimal(1)
    gate = SourceGate(SourcesSettings(), lambda source, cap: True, ledger)

    await identify.identify_customer(
        _customer(),
        _location(),
        [],
        _site(),
        repositories=repositories,
        industries=industries,
        gate=gate,
        api_key=KEY,
        notes=[],
        eur_per_usd=Decimal("0.9"),
    )

    ledger.add_source_cost.assert_called_once_with("places", Decimal("0.0315"))
