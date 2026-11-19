import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radarauditor.collectors.apify import run_actor
from radarauditor.collectors.identity import compare_features, identity, names_agree
from radarauditor.collectors.patterns import (
    collapse_whitespace,
    host_of,
    parse_http_url,
    site_key,
)
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import DirectoryEntry, DirectoryHit
from radarauditor.collectors.social_links import carries_name, name_variants
from radarauditor.collectors.social_search import (
    GOOGLE_SEARCH_ACTOR,
    SEARCH_COUNTRY,
    SEARCH_LANGUAGE,
)
from radarauditor.collectors.transport import USER_AGENT, send
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import CollectionError, ConfigurationError

logger = logging.getLogger(__name__)

APPLE_ACTOR = "zen-studio~apple-maps-scraper-pro"
BING_ACTOR = "karamelo~bing-maps-scraper"
APPLE_SERVICE = "apple_maps"
BING_SERVICE = "bing_maps"
SEARCH_SERVICE = "directory_search"
APPLE_NAME = "Apple Business Connect"
BING_NAME = "Bing Places"
WIKIDATA_NAME = "Wikidata"
APPLE_ROUTE = "apple_maps"
BING_ROUTE = "bing_maps"
WIKIDATA_ROUTE = "wikidata"
INDUSTRY_ROUTE = "industry_directories"
REVIEW_ROUTE = "review_portals"
MAPS_MAX_RESULTS = 8
SEARCH_RUN_TIMEOUT_SECONDS = 150
MAPS_RUN_TIMEOUT_SECONDS = 170
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/{qid}"
WIKIDATA_TIMEOUT_SECONDS = 20
WIKIDATA_CANDIDATE_LIMIT = 30
APPLE_START_USD = Decimal("0.02")
APPLE_PLACE_USD = Decimal("0.004")
BING_START_USD = Decimal("0.005")
BING_RESULT_USD = Decimal("0.0005")
SEARCH_PAGE_USD = Decimal("0.0045")
SEARCH_START_USD = Decimal("0.00005")
NOT_A_LISTING = re.compile(r"/(search|suche|categor|kategorie|branchen?|rubrik)(/|\?|$)", re.I)
NO_TOKEN = "Der Apify-Zugang fehlt, APIFY_TOKEN ist nicht gesetzt"
APIFY_OFF = "Apify ist in den Einstellungen ausgeschaltet"
CAP_REACHED = "Apify: Tages- oder Monatslimit erreicht"
NO_INDUSTRY_PORTALS = "Für diese Branche sind keine Fachverzeichnisse hinterlegt"


class Portal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str | None = None
    hosts: list[str] = Field(default_factory=list)
    path: str | None = None
    cities: list[str] = Field(default_factory=list)


class ReviewPortals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alle: list[str]
    branchen: dict[str, list[str]] = Field(default_factory=dict)


class PortalCatalogue(BaseModel):
    portale: dict[str, Portal]
    industry_specific: dict[str, list[str]]
    bewertungsportale: ReviewPortals


@dataclass(frozen=True)
class Target:
    name: str
    city: str
    street: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    website: str | None = None
    industry: str | None = None

    @property
    def domain(self) -> str | None:
        return host_of(self.website)

    @property
    def variants(self) -> list[str]:
        return name_variants([self.name], self.domain or "")

    @property
    def location_text(self) -> str:
        parts = [self.street, " ".join(p for p in (self.postal_code, self.city) if p)]
        return ", ".join(part for part in parts if part) or self.city


@dataclass(frozen=True)
class RouteStatus:
    route: str
    ok: bool | None
    reason: str | None = None


@dataclass
class ListingResult:
    hits: list[DirectoryHit] = field(default_factory=list)
    routes: list[RouteStatus] = field(default_factory=list)


def load_portals(path: Path) -> PortalCatalogue:
    try:
        catalogue = PortalCatalogue.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ConfigurationError(f"portal catalogue {path} is unreadable: {error}") from error
    listed = {
        *(name for names in catalogue.industry_specific.values() for name in names),
        *catalogue.bewertungsportale.alle,
        *(name for names in catalogue.bewertungsportale.branchen.values() for name in names),
    }
    unknown = sorted(listed - set(catalogue.portale))
    if unknown:
        raise ConfigurationError(f"portal catalogue {path} lists unknown portals: {unknown}")
    return catalogue


def portals_for(catalogue: PortalCatalogue, target: Target) -> tuple[list[str], list[str]]:
    industry = target.industry or ""
    trade = catalogue.industry_specific.get(industry, [])
    reviews = [
        *catalogue.bewertungsportale.alle,
        *catalogue.bewertungsportale.branchen.get(industry, []),
    ]
    return (
        [name for name in dict.fromkeys(trade) if _in_city(catalogue.portale[name], target)],
        [name for name in dict.fromkeys(reviews) if _in_city(catalogue.portale[name], target)],
    )


def _in_city(portal: Portal, target: Target) -> bool:
    return not portal.cities or target.city.casefold() in {c.casefold() for c in portal.cities}


def search_query_count(catalogue: PortalCatalogue, target: Target) -> int:
    trade, reviews = portals_for(catalogue, target)
    names = list(dict.fromkeys([*trade, *reviews]))
    by_domain = sum(1 for name in names if catalogue.portale[name].site and target.domain)
    return len(names) + by_domain


def business_matches(
    target: Target, *, name: str | None, website: str | None, phone: str | None, address: str | None
) -> bool:
    own = identity(
        name=target.name,
        website=target.website,
        phone=target.phone,
        street=target.street,
        postal_code=target.postal_code,
    )
    entry = identity(name=name, website=website, phone=phone, address=address)
    by_features = compare_features(own, entry)
    if by_features is not None:
        return by_features
    if not names_agree(target.name, name):
        return False
    text = (address or "").casefold()
    if target.postal_code:
        return target.postal_code in text
    return bool(target.city) and target.city.casefold() in text


def _listing(
    directory: str,
    url: str | None,
    name: str | None,
    address: str | None,
    phone: str | None,
    delivered: int | None = None,
    search_text: str | None = None,
    entries: list[DirectoryEntry] | None = None,
) -> DirectoryHit:
    return DirectoryHit(
        directory=directory,
        found=True,
        listing_url=parse_http_url(url) if url else None,
        name=name,
        address=address,
        phone=phone,
        delivered=delivered,
        search_text=search_text,
        entries=entries,
    )


def _absent(
    directory: str,
    delivered: int | None = None,
    search_text: str | None = None,
    entries: list[DirectoryEntry] | None = None,
) -> DirectoryHit:
    return DirectoryHit(
        directory=directory,
        found=False,
        listing_url=None,
        name=None,
        address=None,
        phone=None,
        delivered=delivered,
        search_text=search_text,
        entries=entries,
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _apple_entry(item: dict[str, Any]) -> DirectoryEntry:
    address = item.get("address")
    return DirectoryEntry(
        name=_text(item.get("name")),
        address=_text(address.get("full")) if isinstance(address, dict) else None,
        phone=_text(item.get("phoneFormatted")) or _text(item.get("phone")),
        website=_text(item.get("website")),
    )


def _bing_entry(item: dict[str, Any]) -> DirectoryEntry:
    address = _text(item.get("address"))
    return DirectoryEntry(
        name=_text(item.get("name")),
        address=address and address.replace(" · ", ", "),
        phone=_text(item.get("phone_number")),
        website=_text(item.get("website")) or _text(item.get("domain")),
    )


def _entry_matches(target: Target, entry: DirectoryEntry) -> bool:
    return business_matches(
        target,
        name=entry.name,
        website=entry.website,
        phone=entry.phone,
        address=entry.address,
    )


def apple_hit(items: list[dict[str, Any]], target: Target) -> DirectoryHit:
    searched = f"{target.name}, {target.location_text}"
    entries = [_apple_entry(item) for item in items]
    for item, entry in zip(items, entries, strict=True):
        if _entry_matches(target, entry):
            return _listing(
                APPLE_NAME,
                _text(item.get("appleUrl")),
                entry.name,
                entry.address,
                entry.phone,
                len(items),
                searched,
                entries,
            )
    return _absent(APPLE_NAME, len(items), searched, entries)


def bing_hit(items: list[dict[str, Any]], target: Target) -> DirectoryHit:
    searched = f"{target.name} {target.city}"
    entries = [_bing_entry(item) for item in items]
    for item, entry in zip(items, entries, strict=True):
        if _entry_matches(target, entry):
            return _listing(
                BING_NAME,
                _text(item.get("listing_url")),
                entry.name,
                entry.address,
                entry.phone,
                len(items),
                searched,
                entries,
            )
    return _absent(BING_NAME, len(items), searched, entries)


async def _apify_ready(settings: Settings, gate: SourceGate) -> str | None:
    if settings.apify_token is None:
        return NO_TOKEN
    if not gate.is_enabled(Source.APIFY):
        return APIFY_OFF
    return None


def _book(
    settings: Settings, gate: SourceGate, service: str, calls: int
) -> Callable[[Decimal], None]:
    def book(cost_usd: Decimal) -> None:
        gate.record_cost(
            Source.APIFY, cost_usd * settings.apify_eur_per_usd, service=service, calls=calls
        )

    return book


async def _run(
    client: httpx.AsyncClient,
    settings: Settings,
    gate: SourceGate,
    actor: str,
    payload: dict[str, object],
    service: str,
    calls: int,
    timeout: int,
) -> list[dict[str, Any]]:
    if gate.acquire_up_to(Source.APIFY, 1) < 1 or settings.apify_token is None:
        raise CollectionError(CAP_REACHED)
    return await run_actor(
        client,
        settings.apify_token,
        actor,
        payload,
        timeout,
        _book(settings, gate, service, calls),
        step="listings",
    )


async def find_apple(
    client: httpx.AsyncClient, settings: Settings, gate: SourceGate, target: Target
) -> DirectoryHit:
    payload: dict[str, object] = {
        "searchQueries": [
            {"query": target.name, "location": target.location_text, "maxResults": MAPS_MAX_RESULTS}
        ],
        "countryCode": "DE",
        "language": "de-DE",
    }
    items = await _run(
        client, settings, gate, APPLE_ACTOR, payload, APPLE_SERVICE, 1, MAPS_RUN_TIMEOUT_SECONDS
    )
    return apple_hit(items, target)


async def find_bing(
    client: httpx.AsyncClient, settings: Settings, gate: SourceGate, target: Target
) -> DirectoryHit:
    payload: dict[str, object] = {
        "query": f"{target.name} {target.city} Deutschland",
        "maxLeads": MAPS_MAX_RESULTS,
    }
    items = await _run(
        client, settings, gate, BING_ACTOR, payload, BING_SERVICE, 1, MAPS_RUN_TIMEOUT_SECONDS
    )
    return bing_hit(items, target)


def _search_term(portal_name: str, portal: Portal, query_name: str) -> str:
    if portal.site:
        return f'site:{portal.site} "{query_name}"'
    return f'"{query_name}" {portal_name}'


def _on_portal(portal: Portal, url: str) -> bool:
    parts = urlsplit(url)
    host = site_key(parts.hostname or "")
    if NOT_A_LISTING.search(parts.path):
        return False
    if portal.path and portal.path.casefold() not in parts.path.casefold():
        return False
    if portal.site:
        return host == portal.site or host.endswith(f".{portal.site}")
    return any(word in host for word in portal.hosts)


def portal_hit(
    portal_name: str, portal: Portal, results: list[dict[str, Any]], target: Target
) -> DirectoryHit:
    variants = target.variants
    for result in results:
        url, title = _text(result.get("url")), _text(result.get("title"))
        if url is None or not _on_portal(portal, url):
            continue
        description = _text(result.get("description")) or ""
        handle = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        written = f"{url} {title or ''} {description}".casefold()
        named = carries_name(variants, title, handle) or any(
            v.casefold() in description.casefold() for v in variants
        )
        if named or (target.domain is not None and target.domain in written):
            return _listing(portal_name, url, title, None, None)
    return _absent(portal_name)


def search_results(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        query = item.get("searchQuery")
        results = item.get("organicResults")
        if item.get("#error") or not isinstance(query, dict) or not isinstance(results, list):
            continue
        term = query.get("term")
        if isinstance(term, str):
            found.setdefault(collapse_whitespace(term), []).extend(
                result for result in results if isinstance(result, dict)
            )
    return found


async def _search(
    client: httpx.AsyncClient,
    settings: Settings,
    gate: SourceGate,
    terms: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not terms:
        return {}
    payload: dict[str, object] = {
        "queries": "\n".join(terms),
        "maxPagesPerQuery": 1,
        "countryCode": SEARCH_COUNTRY,
        "languageCode": SEARCH_LANGUAGE,
    }
    items = await _run(
        client,
        settings,
        gate,
        GOOGLE_SEARCH_ACTOR,
        payload,
        SEARCH_SERVICE,
        len(terms),
        SEARCH_RUN_TIMEOUT_SECONDS,
    )
    return search_results(items)


async def find_portals(
    client: httpx.AsyncClient,
    settings: Settings,
    gate: SourceGate,
    catalogue: PortalCatalogue,
    names: list[str],
    target: Target,
) -> list[DirectoryHit]:
    variant = target.variants[0] if target.variants else target.name
    first = {name: _search_term(name, catalogue.portale[name], variant) for name in names}
    answers = await _search(client, settings, gate, list(first.values()))
    hits = {
        name: portal_hit(
            name, catalogue.portale[name], answers.get(collapse_whitespace(term), []), target
        )
        for name, term in first.items()
    }
    domain = target.domain
    second = {
        name: _search_term(name, catalogue.portale[name], domain)
        for name, hit in hits.items()
        if domain and not hit.found and catalogue.portale[name].site
    }
    if second:
        answers = await _search(client, settings, gate, list(second.values()))
        for name, term in second.items():
            hits[name] = portal_hit(
                name, catalogue.portale[name], answers.get(collapse_whitespace(term), []), target
            )
    return list(hits.values())


def website_variants(domain: str) -> list[str]:
    return [
        f"{scheme}://{host}{slash}"
        for scheme in ("https", "http")
        for host in (f"www.{domain}", domain)
        for slash in ("/", "")
    ]


async def _wikidata(client: httpx.AsyncClient, **params: str | int) -> dict[str, Any]:
    response = await send(
        client,
        "GET",
        WIKIDATA_API,
        source="Wikidata",
        params={**params, "format": "json"},
        headers={"User-Agent": USER_AGENT},
    )
    body = response.json()
    if not isinstance(body, dict) or "error" in body:
        raise CollectionError(f"Wikidata answered with an error: {str(body)[:200]}")
    return body


async def _wikidata_candidates(client: httpx.AsyncClient, target: Target) -> list[str]:
    ids: dict[str, None] = {}
    for variant in target.variants:
        body = await _wikidata(
            client,
            action="wbsearchentities",
            search=variant,
            language="de",
            uselang="de",
            type="item",
            limit=10,
        )
        ids.update({entry["id"]: None for entry in body.get("search", []) if "id" in entry})
    if target.domain:
        for url in website_variants(target.domain):
            body = await _wikidata(
                client, action="query", list="search", srsearch=f"haswbstatement:P856={url}"
            )
            ids.update({entry["title"]: None for entry in body.get("query", {}).get("search", [])})
    return list(ids)[:WIKIDATA_CANDIDATE_LIMIT]


def _claim_urls(entity: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for claim in entity.get("claims", {}).get("P856", []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str):
            urls.append(value)
    return urls


def _label(entity: dict[str, Any], key: str) -> str:
    for language in ("de", "en"):
        value = entity.get(key, {}).get(language, {}).get("value")
        if isinstance(value, str):
            return value
    return ""


def wikidata_hit(entities: dict[str, Any], target: Target) -> DirectoryHit:
    for qid, entity in entities.items():
        label = _label(entity, "labels")
        by_website = bool(target.domain) and any(
            host_of(url) == target.domain for url in _claim_urls(entity)
        )
        description = _label(entity, "descriptions").casefold()
        by_name = (
            bool(label)
            and names_agree(label, target.name)
            and target.city.casefold() in description
        )
        if by_website or by_name:
            return _listing(
                WIKIDATA_NAME, WIKIDATA_ENTITY.format(qid=qid), label or None, None, None
            )
    return _absent(WIKIDATA_NAME)


async def find_wikidata(client: httpx.AsyncClient, target: Target) -> DirectoryHit:
    candidates = await _wikidata_candidates(client, target)
    if not candidates:
        return _absent(WIKIDATA_NAME)
    body = await _wikidata(
        client,
        action="wbgetentities",
        ids="|".join(candidates),
        props="labels|descriptions|claims",
        languages="de|en",
    )
    entities = body.get("entities", {})
    return wikidata_hit(entities if isinstance(entities, dict) else {}, target)


async def collect_listings(
    settings: Settings, gate: SourceGate, catalogue_path: Path, target: Target
) -> ListingResult:
    catalogue = load_portals(catalogue_path)
    trade, reviews = portals_for(catalogue, target)
    result = ListingResult()
    unavailable = await _apify_ready(settings, gate)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=WIKIDATA_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        await _route(result, WIKIDATA_ROUTE, find_wikidata(client, target))
        if unavailable is not None:
            for route in (APPLE_ROUTE, BING_ROUTE, INDUSTRY_ROUTE, REVIEW_ROUTE):
                result.routes.append(RouteStatus(route, None, unavailable))
            return result
        await _route(result, APPLE_ROUTE, find_apple(client, settings, gate, target))
        await _route(result, BING_ROUTE, find_bing(client, settings, gate, target))
        names = list(dict.fromkeys([*trade, *reviews]))
        try:
            hits = (
                {
                    hit.directory: hit
                    for hit in await find_portals(client, settings, gate, catalogue, names, target)
                }
                if names
                else {}
            )
        except CollectionError as error:
            for route in (INDUSTRY_ROUTE, REVIEW_ROUTE):
                result.routes.append(RouteStatus(route, False, str(error)))
            return result
        result.hits.extend(hits.values())
        result.routes.append(
            RouteStatus(
                INDUSTRY_ROUTE, True if trade else None, None if trade else NO_INDUSTRY_PORTALS
            )
        )
        result.routes.append(RouteStatus(REVIEW_ROUTE, True))
    return result


async def _route(result: ListingResult, route: str, work: Awaitable[DirectoryHit]) -> None:
    try:
        hit = await work
    except CollectionError as error:
        logger.warning("Listing route %s failed: %s", route, error)
        result.routes.append(RouteStatus(route, False, str(error)))
        return
    result.hits.append(hit)
    result.routes.append(RouteStatus(route, True))
