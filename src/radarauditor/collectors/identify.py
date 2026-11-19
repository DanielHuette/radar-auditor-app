import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

from pydantic import SecretStr

from radarauditor.collectors import legal_notice, places
from radarauditor.collectors.identity import SHARED_HOSTS, identity, same_business
from radarauditor.collectors.patterns import host_of
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    IdentificationSource,
    IdentifiedField,
    IdentifiedValue,
    WebsiteSnapshot,
)
from radarauditor.db.app_settings import Source
from radarauditor.db.models import Customer, Keyword, Location
from radarauditor.db.repositories import Repositories
from radarauditor.errors import CollectionError, IdentificationError, StorageError
from radarauditor.industries import IndustryCatalogue, IndustryGroup

logger = logging.getLogger(__name__)

CITY_NOT_FOUND = (
    "Ort nicht gefunden: Das Impressum nennt keinen Ort und beim Start wurde keiner eingegeben. "
    "Bitte Ort in der Auswertung ergänzen und neu prüfen."
)
NO_LEGAL_NOTICE = "kein Impressum gefunden"
CUSTOMER_FIELDS = frozenset({"name", "city"})
LEGAL_NOTICE_FIELDS: tuple[IdentifiedField, ...] = (
    "name",
    "street",
    "postal_code",
    "city",
    "phone",
)
PLACES_ID_PREFIX = "ChIJ"
Mark = Callable[[IdentifiedField, str, IdentificationSource], None]


@dataclass(frozen=True)
class Identification:
    customer: Customer
    location: Location | None
    keywords: list[Keyword]
    identified: list[IdentifiedValue] = field(default_factory=list)
    business: BusinessFacts | None = None


@dataclass(frozen=True)
class _Missing:
    name: bool
    city: bool
    industry: bool
    keywords: bool

    @property
    def any(self) -> bool:
        return self.name or self.city or self.industry or self.keywords


def _missing(customer: Customer, keywords: list[Keyword]) -> _Missing:
    host = host_of(customer.website)
    return _Missing(
        name=host is not None and customer.name.strip().lower() == host,
        city=not customer.city.strip(),
        industry=not customer.industry.strip(),
        keywords=not keywords or any(not keyword.city.strip() for keyword in keywords),
    )


async def identify_customer(
    customer: Customer,
    location: Location | None,
    keywords: list[Keyword],
    site: WebsiteSnapshot | None,
    *,
    repositories: Repositories,
    industries: IndustryCatalogue,
    gate: SourceGate,
    api_key: SecretStr,
    notes: list[str],
    eur_per_usd: Decimal = Decimal(1),
) -> Identification:
    identified: list[IdentifiedValue] = []

    def mark(name: IdentifiedField, value: str, source: IdentificationSource) -> None:
        identified.append(IdentifiedValue(field=name, value=value, source=source))

    business = await _read_legal_notice(site, notes)
    if business is None:
        notes.append(NO_LEGAL_NOTICE)
    else:
        customer, location = _apply_legal_notice(customer, location, business, repositories, mark)
    missing = _missing(customer, keywords)
    if not missing.any:
        return Identification(customer, location, keywords, identified, business)
    if missing.name:
        notes.append("business name not found, the website host stays the name")
    if missing.city:
        raise IdentificationError(CITY_NOT_FOUND)
    place = await _ask_google(customer, location, missing, gate, api_key, eur_per_usd, notes)
    group = _industry(customer, place, missing, industries, mark, notes)
    if missing.industry and group is not None:
        repositories.update_customer(customer.id, industry=group.id)
        customer = customer.model_copy(update={"industry": group.id})
    if customer.crm_customer_id is None and business is not None:
        _link_crm(repositories, customer, notes)
    stored_location = _store_place(customer, location, place, repositories, mark)
    stored_keywords = _store_keywords(customer, keywords, group, repositories, mark, notes)
    return Identification(customer, stored_location, stored_keywords, identified, business)


async def _read_legal_notice(
    site: WebsiteSnapshot | None, notes: list[str]
) -> BusinessFacts | None:
    if site is None:
        return None
    try:
        business = await legal_notice.read_site(site)
    except CollectionError as error:
        notes.append(f"legal notice not readable: {error}")
        return None
    return business if business != BusinessFacts() else None


def _apply_legal_notice(
    customer: Customer,
    location: Location | None,
    business: BusinessFacts,
    repositories: Repositories,
    mark: Mark,
) -> tuple[Customer, Location | None]:
    stated = {key: value for key in LEGAL_NOTICE_FIELDS if (value := getattr(business, key))}
    customer_texts: dict[str, str] = {
        key: value
        for key, value in stated.items()
        if key in CUSTOMER_FIELDS and value != getattr(customer, key)
    }
    if customer_texts:
        repositories.update_customer(
            customer.id, name=customer_texts.get("name"), city=customer_texts.get("city")
        )
        customer = customer.model_copy(update=customer_texts)
    current = location or _empty_location(customer)
    location_texts: dict[str, str] = {
        key: value for key, value in stated.items() if value != getattr(current, key)
    }
    for key, value in stated.items():
        if key in customer_texts or key in location_texts:
            mark(key, value, "impressum")
    if not location_texts:
        return customer, location
    updated = current.model_copy(update=location_texts)
    if location is None:
        return customer, repositories.create_location(updated)
    repositories.update_location(
        location.id,
        name=location_texts.get("name"),
        street=location_texts.get("street"),
        postal_code=location_texts.get("postal_code"),
        city=location_texts.get("city"),
        phone=location_texts.get("phone"),
    )
    return customer, updated


async def _ask_google(
    customer: Customer,
    location: Location | None,
    missing: _Missing,
    gate: SourceGate,
    api_key: SecretStr,
    eur_per_usd: Decimal,
    notes: list[str],
) -> places.BusinessCandidate | None:
    needs_coordinates = location is None or location.latitude is None
    if not (missing.industry or needs_coordinates):
        return None
    host = host_of(customer.website)
    name = None if missing.name else customer.name
    city = customer.city.strip()
    query = f"{name} {city}" if name else host
    if not query:
        return None
    try:
        gate.acquire(Source.PLACES)
        candidates = await places.search_businesses(query, api_key)
        gate.record_cost(
            Source.PLACES,
            places.request_cost_eur(places.SEARCH_BUSINESSES_SKU, eur_per_usd),
            service=places.SEARCH_BUSINESSES_SKU.value,
        )
    except CollectionError as error:
        notes.append(f"Google profile not searched: {error}")
        return None
    place = next((item for item in candidates if _is_customer(item, host, name, city)), None)
    if place is None:
        notes.append(f"no Google profile clearly belongs to {name or host}")
    return place


def _is_customer(
    place: places.BusinessCandidate, host: str | None, name: str | None, city: str | None
) -> bool:
    website_host = host_of(place.website)
    if host is not None and host not in SHARED_HOSTS and website_host is not None:
        return website_host == host
    if name is None or city is None:
        return False
    if not same_business(identity(name=name), identity(name=place.name)):
        return False
    return city.casefold() in place.formatted_address.casefold()


def _industry(
    customer: Customer,
    place: places.BusinessCandidate | None,
    missing: _Missing,
    industries: IndustryCatalogue,
    mark: Mark,
    notes: list[str],
) -> IndustryGroup | None:
    if not missing.industry:
        return industries.resolve(customer.industry)
    source: IdentificationSource = "google_types"
    group = (
        industries.by_google_types([*filter(None, [place.primary_type]), *place.types])
        if place is not None
        else None
    )
    if group is None:
        source = "name_words"
        texts = [customer.name, *(item for item in [place.category if place else None] if item)]
        group = industries.by_words(texts)
    if group is None:
        notes.append("industry not recognised, checks that need it are not measurable")
        return None
    mark("industry", group.id, source)
    return group


def _link_crm(repositories: Repositories, customer: Customer, notes: list[str]) -> None:
    try:
        repositories.link_crm_customer(customer.id)
    except StorageError as error:
        notes.append(f"customer not linked to the CRM: {error}")


def _store_place(
    customer: Customer,
    location: Location | None,
    place: places.BusinessCandidate | None,
    repositories: Repositories,
    mark: Mark,
) -> Location | None:
    current = location or _empty_location(customer)
    texts: dict[str, str] = {}
    if not current.name.strip() or current.name.strip().lower() == host_of(customer.website):
        texts["name"] = customer.name
    if not current.city.strip():
        texts["city"] = customer.city
    if (
        place is not None
        and not current.google_place_id
        and place.place_id.startswith(PLACES_ID_PREFIX)
    ):
        texts["google_place_id"] = place.place_id
        mark("google_place_id", place.place_id, "google_profile")
    coordinates = _coordinates(current, place)
    updated = current.model_copy(update={**texts, **coordinates})
    if location is None:
        return repositories.create_location(updated)
    if texts or coordinates:
        repositories.update_location(
            location.id,
            name=texts.get("name"),
            city=texts.get("city"),
            google_place_id=texts.get("google_place_id"),
            latitude=coordinates.get("latitude"),
            longitude=coordinates.get("longitude"),
        )
    return updated


def _empty_location(customer: Customer) -> Location:
    return Location(
        id=uuid4(),
        customer_id=customer.id,
        name="",
        street="",
        postal_code="",
        city="",
        phone="",
        google_place_id=None,
        latitude=None,
        longitude=None,
    )


def _coordinates(current: Location, place: places.BusinessCandidate | None) -> dict[str, float]:
    if place is None or current.latitude is not None:
        return {}
    if place.latitude is None or place.longitude is None:
        return {}
    return {"latitude": place.latitude, "longitude": place.longitude}


def keyword_suggestions(group: IndustryGroup | None, city: str) -> list[str]:
    place = city.strip()
    if not place:
        return []
    return [f"{service} {place}" for service in _services(group)]


def _services(group: IndustryGroup | None) -> list[str]:
    return list(dict.fromkeys(service.strip() for service in group.services)) if group else []


def _store_keywords(
    customer: Customer,
    keywords: list[Keyword],
    group: IndustryGroup | None,
    repositories: Repositories,
    mark: Mark,
    notes: list[str],
) -> list[Keyword]:
    if keywords:
        return _with_city(keywords, customer.city, repositories, notes)
    services = _services(group)
    if not services:
        notes.append("no keywords: the industry is not known")
        return []
    added = repositories.add_keywords(customer.id, services, customer.city, first_primary=True)
    mark("keywords", ", ".join(keyword_suggestions(group, customer.city)), "industry_services")
    return added


def _with_city(
    keywords: list[Keyword], city: str, repositories: Repositories, notes: list[str]
) -> list[Keyword]:
    pending = _cityless(keywords, city, notes)
    stored = {keyword.id: keyword for keyword in _store_city(pending, city, repositories, notes)}
    return [
        stored.get(keyword.id, keyword)
        for keyword in keywords
        if keyword.city.strip() or keyword.id in stored
    ]


def _cityless(keywords: list[Keyword], city: str, notes: list[str]) -> list[Keyword]:
    tracked = {keyword.phrase for keyword in keywords if keyword.city == city}
    pending: list[Keyword] = []
    for keyword in keywords:
        if keyword.city.strip():
            continue
        if keyword.phrase in tracked:
            notes.append(f"keyword {keyword.phrase} keeps no city: it is already tracked in {city}")
        else:
            pending.append(keyword)
    return pending


def _store_city(
    keywords: list[Keyword], city: str, repositories: Repositories, notes: list[str]
) -> list[Keyword]:
    if not keywords:
        return []
    try:
        repositories.update_keywords_city([keyword.id for keyword in keywords], city)
    except StorageError as error:
        phrases = ", ".join(keyword.phrase for keyword in keywords)
        notes.append(f"keywords {phrases} keep no city: {error}")
        return []
    return [keyword.model_copy(update={"city": city}) for keyword in keywords]
