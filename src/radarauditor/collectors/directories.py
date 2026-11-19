import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, HttpUrl, ValidationError

from radarauditor.collectors.paid_rankings import (
    GERMANY_LOCATION_CODE,
    LANGUAGE_CODE,
    ORGANIC_TASKS,
    RESULTS_PER_PAGE,
    DataForSeoClient,
    organic_pages,
    run_tasks,
)
from radarauditor.collectors.patterns import (
    POSTAL_CITY_PATTERN,
    collapse_whitespace,
    find_address,
    find_phone,
    host_of,
    normalise_address,
    normalise_name,
    normalise_phone,
    parse_http_url,
    site_key,
)
from radarauditor.collectors.snapshot import DirectoryHit
from radarauditor.collectors.transport import USER_AGENT, send
from radarauditor.db.models import Location
from radarauditor.errors import CollectionError, ConfigurationError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
CONCURRENT_DIRECTORIES = 4
MAX_LISTING_CLIMB = 8
INVISIBLE_TAGS = ("script", "style", "noscript", "template", "svg")


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    name: str
    kind: str
    search_url: str | None = None
    no_hits_status: int | None = None


class DirectoryCatalogue(BaseModel):
    directories: list[DirectoryEntry]


def load_catalogue(path: Path) -> DirectoryCatalogue:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return DirectoryCatalogue.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ConfigurationError(f"directory catalogue {path} is unreadable: {error}") from error


async def check_directories(location: Location, catalogue: Path) -> list[DirectoryHit]:
    entries = load_catalogue(catalogue).directories
    semaphore = asyncio.Semaphore(CONCURRENT_DIRECTORIES)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        hits = await asyncio.gather(
            *(_check_one(client, semaphore, entry, location) for entry in entries)
        )
    return [hit for hit in hits if hit is not None]


async def _check_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    entry: DirectoryEntry,
    location: Location,
) -> DirectoryHit | None:
    if entry.search_url is None:
        return None
    url = entry.search_url.format(
        name=quote(location.name, safe=""), city=quote(location.city, safe="")
    )
    no_hits = frozenset() if entry.no_hits_status is None else frozenset({entry.no_hits_status})
    async with semaphore:
        try:
            response = await send(client, "GET", url, source=entry.name, accepted_statuses=no_hits)
        except CollectionError as error:
            logger.warning("Directory %s could not be checked: %s", entry.name, error)
            return None
    if response.status_code == entry.no_hits_status:
        return _not_found(entry.name)
    return find_listing(entry.name, str(response.url), response.text, location.name)


async def search_through_google(
    client: DataForSeoClient, entries: list[DirectoryEntry], location: Location
) -> tuple[list[DirectoryHit], dict[str, str]]:
    searchable = [entry for entry in entries if entry.search_url is not None]
    hosts = [site_key(urlsplit(entry.search_url or "").netloc) for entry in searchable]
    tasks = [
        {
            "keyword": f'site:{host} "{location.name}"',
            "language_code": LANGUAGE_CODE,
            "location_code": GERMANY_LOCATION_CODE,
            "depth": RESULTS_PER_PAGE,
        }
        for host in hosts
    ]
    batch = await run_tasks(client, ORGANIC_TASKS, tasks)
    hits: list[DirectoryHit] = []
    failed = {searchable[index].name: reason for index, reason in batch.failed.items()}
    for index, rows in sorted(batch.results.items()):
        try:
            pages = organic_pages(rows)
        except CollectionError as error:
            failed[searchable[index].name] = str(error)
            continue
        hits.append(_google_hit(searchable[index].name, hosts[index], pages))
    return hits, failed


def _google_hit(
    directory: str, host: str, pages: list[tuple[str, str, str | None]]
) -> DirectoryHit:
    match = next((page for page in pages if page[0] == host), None)
    if match is None:
        return _not_found(directory)
    _, url, title = match
    on_host = host_of(url) is not None and site_key(host_of(url) or "") == host
    return DirectoryHit(
        directory=directory,
        found=True,
        listing_url=parse_http_url(url) if on_host else None,
        name=collapse_whitespace(title) if title else None,
        address=None,
        phone=None,
    )


def find_listing(directory: str, page_url: str, html: str, business_name: str) -> DirectoryHit:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(INVISIBLE_TAGS):
        tag.decompose()
    located = _locate_listing(soup, _name_variants(business_name))
    if located is None:
        return _not_found(directory)
    match, container = located
    lines = [
        line
        for line in (collapse_whitespace(part) for part in container.get_text("\n").splitlines())
        if line
    ]
    return DirectoryHit(
        directory=directory,
        found=True,
        listing_url=_listing_url(match, container, page_url),
        name=collapse_whitespace(match.get_text(" ")),
        address=_address_line(find_address(lines)),
        phone=find_phone("\n".join(lines)),
    )


def _address_line(parts: dict[str, str]) -> str | None:
    if not parts:
        return None
    town = f"{parts['postal_code']} {parts['city']}"
    return f"{parts['street']}, {town}" if "street" in parts else town


def _name_variants(business_name: str) -> list[str]:
    exact = collapse_whitespace(business_name).lower()
    variants = [exact]
    without_form = normalise_name(business_name)
    if without_form and without_form != exact:
        variants.append(without_form)
    return variants


def _locate_listing(soup: BeautifulSoup, variants: list[str]) -> tuple[Tag, Tag] | None:
    fallback: tuple[Tag, Tag] | None = None
    for match in _deepest_matches(soup, variants):
        container = _listing_container(match)
        if container is not None:
            return match, container
        if fallback is None:
            fallback = (match, match)
    return fallback


def _deepest_matches(soup: BeautifulSoup, variants: list[str]) -> Iterator[Tag]:
    root = soup.body or soup
    for variant in variants:
        for element in root.find_all(True):
            if variant not in collapse_whitespace(element.get_text(" ")).lower():
                continue
            if not any(
                variant in collapse_whitespace(child.get_text(" ")).lower()
                for child in element.find_all(True, recursive=False)
            ):
                yield element


def _listing_container(match: Tag) -> Tag | None:
    current = match
    for _ in range(MAX_LISTING_CLIMB):
        if POSTAL_CITY_PATTERN.search(collapse_whitespace(current.get_text(" "))):
            return current
        if not isinstance(current.parent, Tag) or current.parent.name in ("body", "html"):
            return None
        current = current.parent
    return None


def _listing_url(match: Tag, container: Tag, page_url: str) -> HttpUrl | None:
    anchor = match if match.name == "a" else match.find_parent("a") or container.find("a")
    if not isinstance(anchor, Tag):
        return None
    href = anchor.get("href")
    if not isinstance(href, str) or not href.strip():
        return None
    return parse_http_url(urljoin(page_url, href.strip()))


def _not_found(directory: str) -> DirectoryHit:
    return DirectoryHit(
        directory=directory, found=False, listing_url=None, name=None, address=None, phone=None
    )


def nap_matches(expected: Location, found: DirectoryHit) -> dict[str, bool]:
    expected_address = f"{expected.street} {expected.postal_code} {expected.city}"
    return {
        "name": found.name is not None
        and normalise_name(found.name) == normalise_name(expected.name),
        "address": found.address is not None
        and normalise_address(found.address) == normalise_address(expected_address),
        "phone": found.phone is not None
        and normalise_phone(found.phone) == normalise_phone(expected.phone),
    }
