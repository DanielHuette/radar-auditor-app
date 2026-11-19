import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from playwright.async_api import Error as PlaywrightError
from pydantic import SecretStr

from radarauditor.collectors import dataforseo_maps, maps, places
from radarauditor.collectors.browser import BrowserService, Geolocation
from radarauditor.collectors.dataforseo_maps import MapsQuery
from radarauditor.collectors.identity import (
    Identity,
    identity,
    place_identity,
    same_business,
)
from radarauditor.collectors.maps_pages import MapListing, listing_identity, listing_position
from radarauditor.collectors.paid_rankings import DataForSeoClient, grid_key, grid_points
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import (
    DataRoute,
    GridPoint,
    ListedBusiness,
    MapRanking,
    MapResultList,
    PlaceSnapshot,
)
from radarauditor.db.app_settings import Source
from radarauditor.errors import (
    CollectionError,
    DailyCapReachedError,
    ProfileNotFoundError,
    SearchBlockedError,
    SourceDisabledError,
    SourceUnavailableError,
)

logger = logging.getLogger(__name__)

RANKING_RADIUS_METERS = 10000.0
GRID_RADIUS_METERS = 1000.0
PLACES_ID_PREFIX = "ChIJ"
SEARCH_SPARE = 2
PROFILE_WAYS: tuple[DataRoute, ...] = ("places_api", "dataforseo", "browser")
NOT_READ_BY_BROWSER = "no profile data from the browser"


class _Fallback(enum.Enum):
    USE_PLACES = enum.auto()


USE_PLACES = _Fallback.USE_PLACES

type _Point = tuple[str, float, float]


@dataclass(frozen=True)
class Business:
    name: str
    address: str
    city: str
    place_ids: frozenset[str] = frozenset()


@dataclass
class _WayRead:
    snapshot: PlaceSnapshot | None = None
    none: bool = False
    failure: str | None = None
    unavailable: SourceUnavailableError | None = None


@dataclass
class _Reading:
    snapshot: PlaceSnapshot | None = None
    none: bool = False
    failures: list[str] = field(default_factory=list)
    unavailable: SourceUnavailableError | None = None

    def take(self, read: _WayRead) -> None:
        if read.snapshot is not None:
            self.snapshot = (
                read.snapshot if self.snapshot is None else supplement(self.snapshot, read.snapshot)
            )
        elif self.snapshot is not None:
            return
        elif read.none:
            self.none = True
        elif read.failure is not None:
            self.failures.append(read.failure)
        elif read.unavailable is not None and self.unavailable is None:
            self.unavailable = read.unavailable

    def verdict(self, target: maps.ProfileTarget) -> PlaceSnapshot | None:
        if self.snapshot is not None:
            return self.snapshot
        if self.failures:
            raise CollectionError(
                f"profile of {target.name} could not be read: {'; '.join(self.failures)}"
            )
        if self.none:
            return None
        raise self.unavailable or CollectionError(
            f"no route is available to read the profile of {target.name}"
        )


def wants_all_fields(target: maps.ProfileTarget) -> bool:
    return target.with_photos


class ProfileRoute:
    def __init__(
        self,
        browser: BrowserService,
        gate: SourceGate,
        api_key: SecretStr,
        eur_per_usd: Decimal = Decimal(1),
        dataforseo: DataForSeoClient | None = None,
        ways: tuple[DataRoute, ...] = PROFILE_WAYS,
    ) -> None:
        self.browser = browser
        self.gate = gate
        self.api_key = api_key
        self.dataforseo = dataforseo
        self.ways = ways
        self._eur_per_usd = eur_per_usd
        self._blocked: str | None = None
        self._browser_used = False
        self._listings: dict[_Point, tuple[list[MapListing], DataRoute]] = {}
        self._places_ids: dict[str, str | None] = {}

    async def prefetch(
        self,
        phrases: list[str],
        centre: tuple[float, float],
        grid: tuple[str, int] | None,
        notes: list[str],
    ) -> None:
        if self.dataforseo is None:
            return
        points: list[_Point] = [(phrase, *centre) for phrase in dict.fromkeys(phrases)]
        if grid is not None:
            phrase, size = grid
            points.extend((phrase, *point) for point in grid_points(*centre, size))
        wanted = list(dict.fromkeys(point for point in points if point not in self._listings))
        if not wanted:
            return
        batch = await dataforseo_maps.search_maps(
            self.dataforseo, [MapsQuery(*point) for point in wanted]
        )
        for index, listings in batch.listings.items():
            self._listings[wanted[index]] = (listings, "dataforseo")
        if batch.failed:
            first = batch.failed[min(batch.failed)]
            notes.append(
                f"DataForSEO Maps answered {len(batch.listings)} of {len(wanted)} searches, "
                f"the browser takes the rest: {first}"
            )

    async def profile(self, target: maps.ProfileTarget, notes: list[str]) -> PlaceSnapshot | None:
        return (await self._readings([target], notes))[0].verdict(target)

    async def profiles(
        self, targets: list[maps.ProfileTarget], notes: list[str]
    ) -> list[PlaceSnapshot | None]:
        found: list[PlaceSnapshot | None] = []
        for target, reading in zip(targets, await self._readings(targets, notes), strict=True):
            try:
                found.append(reading.verdict(target))
            except SourceUnavailableError:
                raise
            except CollectionError as error:
                notes.append(str(error))
                found.append(None)
        return found

    async def _readings(
        self, targets: list[maps.ProfileTarget], notes: list[str]
    ) -> list[_Reading]:
        readings = [_Reading() for _ in targets]
        for way in self.ways:
            wanted = [
                index
                for index, target in enumerate(targets)
                if readings[index].snapshot is None or wants_all_fields(target)
            ]
            if not wanted:
                continue
            known = [readings[index].snapshot for index in wanted]
            reads = await self._read_way(way, [targets[index] for index in wanted], known, notes)
            for index, read in zip(wanted, reads, strict=True):
                readings[index].take(read)
        return readings

    async def _read_way(
        self,
        way: DataRoute,
        targets: list[maps.ProfileTarget],
        known: list[PlaceSnapshot | None],
        notes: list[str],
    ) -> list[_WayRead]:
        if way == "places_api":
            return [await self._read_places(target, notes) for target in targets]
        if way == "dataforseo":
            return await self._read_dataforseo(targets, notes)
        return [
            await self._read_browser(target, snapshot, notes)
            for target, snapshot in zip(targets, known, strict=True)
        ]

    async def _read_places(self, target: maps.ProfileTarget, notes: list[str]) -> _WayRead:
        try:
            place_id = await self._places_id(target.name, target.address, target.place_id)
            if place_id is None:
                return _WayRead(none=True)
            self.gate.acquire(Source.PLACES)
            snapshot = await places.place_details(place_id, self.api_key)
            self._charge(places.PLACE_DETAILS_SKU)
        except SourceUnavailableError as error:
            return _WayRead(unavailable=error)
        except CollectionError as error:
            notes.append(f"{target.name}: Places API profile failed ({error})")
            return _WayRead(failure=f"Places API: {error}")
        return _WayRead(snapshot=snapshot)

    async def _read_dataforseo(
        self, targets: list[maps.ProfileTarget], notes: list[str]
    ) -> list[_WayRead]:
        if self.dataforseo is None:
            return [_WayRead() for _ in targets]
        keywords = [
            dataforseo_maps.profile_keyword(target.name, target.address, target.place_id)
            for target in targets
        ]
        try:
            reads = await dataforseo_maps.read_profiles(self.dataforseo, keywords)
        except SourceUnavailableError as error:
            return [_WayRead(unavailable=error) for _ in targets]
        except CollectionError as error:
            notes.append(f"DataForSEO profile read failed ({error})")
            return [_WayRead(failure=f"DataForSEO: {error}") for _ in targets]
        results: list[_WayRead] = []
        for target, read in zip(targets, reads, strict=True):
            notes.extend(f"{target.name}: {note}" for note in read.notes)
            if read.failure is not None:
                notes.append(f"{target.name}: DataForSEO profile failed ({read.failure})")
                results.append(_WayRead(failure=f"DataForSEO: {read.failure}"))
            else:
                results.append(_WayRead(snapshot=read.snapshot, none=read.snapshot is None))
        return results

    async def _read_browser(
        self, target: maps.ProfileTarget, known: PlaceSnapshot | None, notes: list[str]
    ) -> _WayRead:
        if self._blocked is not None:
            return _WayRead(failure=f"Google Maps: {self._blocked}")
        place_id = (
            known.place_id
            if known is not None and known.place_id.startswith(PLACES_ID_PREFIX)
            else target.place_id
        )
        browser_target = maps.ProfileTarget(
            target.name, target.address, place_id, target.url, target.with_photos
        )
        try:
            read = await self._in_browser(
                lambda: maps.read_profile(self.browser, browser_target),
                notes,
                target.name,
                fallback=NOT_READ_BY_BROWSER,
            )
        except ProfileNotFoundError:
            return _WayRead(none=True)
        if read is USE_PLACES:
            return self._browser_gap()
        return _WayRead(snapshot=read)

    def _browser_gap(self) -> _WayRead:
        source = Source.GOOGLE_MAPS
        if not self.gate.is_enabled(source):
            return _WayRead(
                unavailable=SourceDisabledError(source.value, "switched off in the app settings")
            )
        if source in self.gate.exhausted or source in self.gate.cost_capped:
            return _WayRead(unavailable=DailyCapReachedError(source.value, "cap reached"))
        return _WayRead(failure="Google Maps: profile not readable")

    async def competitors(
        self,
        phrase: str,
        centre: tuple[float, float],
        exclude: list[PlaceSnapshot],
        own: Business,
        count: int,
        notes: list[str],
    ) -> list[PlaceSnapshot]:
        searched = await self._search(phrase, centre, notes)
        if searched is USE_PLACES:
            return await self._places_competitors(phrase, centre, exclude, own, count)
        listings, _ = searched
        chosen = [item for item in listings if not _is_known(item, exclude, own)][:count]
        targets = [
            maps.ProfileTarget(listing.name, own.city, place_id=listing.place_id, url=listing.url)
            for listing in chosen
        ]
        found: list[PlaceSnapshot] = []
        for listing, snapshot in zip(chosen, await self.profiles(targets, notes), strict=True):
            if snapshot is None:
                notes.append(f"{listing.name}: profile not readable")
                continue
            found.append(snapshot)
        return found

    async def map_ranking(
        self, phrase: str, centre: tuple[float, float], own: Business, notes: list[str]
    ) -> MapRanking:
        searched = await self._search(phrase, centre, notes)
        if searched is not USE_PLACES:
            listings, route = searched
            return MapRanking(
                phrase=phrase,
                position=listing_position(listings, _identity_of(own)),
                listed=[listing.name for listing in listings],
                route=route,
            )
        position = await self._places_position(phrase, centre, own, RANKING_RADIUS_METERS)
        return MapRanking(phrase=phrase, position=position, route="places_api")

    async def result_list(
        self, phrase: str, centre: tuple[float, float], own: Business, notes: list[str]
    ) -> MapResultList:
        searched = await self._search(phrase, centre, notes)
        if searched is not USE_PLACES:
            listings, route = searched
            return _result_list(phrase, listings, own, route)
        self.gate.acquire(Source.PLACES)
        found = await places.listed_businesses(phrase, *centre, self.api_key, RANKING_RADIUS_METERS)
        self._charge(places.LISTED_BUSINESSES_SKU)
        listed = [
            MapListing(item.name, "", item.place_id, item.rating, item.review_count)
            for item in found
        ]
        return _result_list(phrase, listed, own, "places_api")

    async def grid(
        self, phrase: str, centre: tuple[float, float], own: Business, size: int, notes: list[str]
    ) -> list[GridPoint]:
        points: list[GridPoint] = []
        for index, (latitude, longitude) in enumerate(grid_points(*centre, size)):
            position, route = await self._point(phrase, latitude, longitude, own, notes)
            points.append(
                GridPoint(
                    key=grid_key(index, size),
                    latitude=latitude,
                    longitude=longitude,
                    position=position,
                    route=route,
                )
            )
        return sorted(points, key=lambda point: _row_and_column(point.key))

    async def _point(
        self, phrase: str, latitude: float, longitude: float, own: Business, notes: list[str]
    ) -> tuple[int | None, DataRoute]:
        known = self._listings.get((phrase, latitude, longitude))
        if known is not None:
            listings, route = known
            return listing_position(listings, _identity_of(own)), route
        position = Geolocation(latitude, longitude)
        read = await self._in_browser(
            lambda: maps.search_listings(self.browser, phrase, position), notes, phrase
        )
        if read is not USE_PLACES:
            return listing_position(read, _identity_of(own)), "browser"
        found = await self._places_position(phrase, (latitude, longitude), own, GRID_RADIUS_METERS)
        return found, "places_api"

    async def _search(
        self, phrase: str, centre: tuple[float, float], notes: list[str]
    ) -> tuple[list[MapListing], DataRoute] | _Fallback:
        key = (phrase, *centre)
        if key in self._listings:
            return self._listings[key]
        read = await self._in_browser(
            lambda: maps.search_listings(self.browser, phrase, Geolocation(*centre)), notes, phrase
        )
        if read is USE_PLACES:
            return USE_PLACES
        self._listings[key] = (read, "browser")
        return self._listings[key]

    async def _in_browser[T](
        self,
        action: Callable[[], Awaitable[T]],
        notes: list[str],
        subject: str,
        *,
        fallback: str = "Places API used",
    ) -> T | _Fallback:
        if self._blocked is not None or not self.gate.try_acquire(Source.GOOGLE_MAPS):
            return USE_PLACES
        if self._browser_used:
            await self.browser.pause()
        self._browser_used = True
        try:
            return await action()
        except SearchBlockedError as error:
            self._blocked = str(error)
            logger.warning("Google Maps blocked the browser: %s", error)
            notes.append(f"Google Maps blocked the browser ({error}), {fallback}")
            return USE_PLACES
        except ProfileNotFoundError:
            raise
        except (CollectionError, PlaywrightError) as error:
            reason = error.message if isinstance(error, PlaywrightError) else str(error)
            notes.append(f"Google Maps not readable for {subject} ({reason}), {fallback}")
            return USE_PLACES

    async def _places_competitors(
        self,
        phrase: str,
        centre: tuple[float, float],
        exclude: list[PlaceSnapshot],
        own: Business,
        count: int,
    ) -> list[PlaceSnapshot]:
        self.gate.acquire(Source.PLACES)
        candidates = await places.top_competitors(
            phrase, *centre, self.api_key, limit=min(count + SEARCH_SPARE, places.MAX_PAGE_SIZE)
        )
        self._charge(places.TOP_COMPETITORS_SKU)
        return [place for place in candidates if not _is_known_place(place, exclude, own)][:count]

    async def _places_position(
        self, phrase: str, centre: tuple[float, float], own: Business, radius: float
    ) -> int | None:
        own_id = await self._places_id(own.name, own.address, *own.place_ids)
        if own_id is None:
            raise CollectionError(f"the Places API knows no profile of {own.name} to rank")
        self.gate.acquire(Source.PLACES)
        ranked = await places.ranked_place_ids(phrase, *centre, self.api_key, radius)
        self._charge(places.RANKED_PLACE_IDS_SKU)
        return ranked.index(own_id) + 1 if own_id in ranked else None

    async def _places_id(self, name: str, address: str, *known: str | None) -> str | None:
        found = next((item for item in known if item and item.startswith(PLACES_ID_PREFIX)), None)
        if found is not None:
            return found
        key = f"{name}|{address}"
        if key not in self._places_ids:
            self.gate.acquire(Source.PLACES)
            self._places_ids[key] = await places.find_place(name, address, self.api_key)
            self._charge(places.FIND_PLACE_SKU)
        return self._places_ids[key]

    def _charge(self, sku: places.PlacesSku) -> None:
        self.gate.record_cost(
            Source.PLACES, places.request_cost_eur(sku, self._eur_per_usd), service=sku.value
        )


def supplement(paid: PlaceSnapshot, read: PlaceSnapshot) -> PlaceSnapshot:
    values = paid.model_dump()
    routes = dict(paid.value_routes)
    for name, route in read.value_routes.items():
        if name not in routes:
            values[name] = getattr(read, name)
            routes[name] = route
    values["value_routes"] = routes
    return PlaceSnapshot.model_validate(values)


def _identity_of(own: Business) -> Identity:
    return identity(name=own.name, place_ids=own.place_ids, address=own.address)


def _is_known_candidate(candidate: Identity, exclude: list[PlaceSnapshot], own: Business) -> bool:
    known = [_identity_of(own), *(place_identity(place) for place in exclude)]
    return any(same_business(item, candidate) for item in known)


def _is_known(listing: MapListing, exclude: list[PlaceSnapshot], own: Business) -> bool:
    return _is_known_candidate(listing_identity(listing), exclude, own)


def _is_known_place(place: PlaceSnapshot, exclude: list[PlaceSnapshot], own: Business) -> bool:
    return _is_known_candidate(place_identity(place), exclude, own)


def _row_and_column(key: str) -> tuple[int, int]:
    row, column = key[1:].split("c")
    return int(row), int(column)


def _result_list(
    phrase: str, listings: list[MapListing], own: Business, route: DataRoute
) -> MapResultList:
    businesses = [
        ListedBusiness(
            name=listing.name,
            review_count=listing.review_count,
            rating=listing.rating,
            place_id=listing.place_id,
            is_customer=_is_known(listing, [], own),
        )
        for listing in listings
    ]
    return MapResultList(phrase=phrase, businesses=businesses, route=route)
