import logging

import httpx

from radarauditor.collectors.listings import (
    APIFY_OFF,
    APPLE_ROUTE,
    BING_ROUTE,
    NO_TOKEN,
    WIKIDATA_TIMEOUT_SECONDS,
    ListingResult,
    RouteStatus,
    Target,
    find_apple,
    find_bing,
)
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.transport import USER_AGENT
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import CollectionError

logger = logging.getLogger(__name__)


def unavailable_reason(settings: Settings, gate: SourceGate) -> str | None:
    if settings.apify_token is None:
        return NO_TOKEN
    if not gate.is_enabled(Source.APIFY):
        return APIFY_OFF
    return None


async def collect_map_listings(
    settings: Settings, gate: SourceGate, target: Target
) -> ListingResult:
    result = ListingResult()
    reason = unavailable_reason(settings, gate)
    if reason is not None:
        result.routes.extend(
            RouteStatus(route, None, reason) for route in (APPLE_ROUTE, BING_ROUTE)
        )
        return result
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=WIKIDATA_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        for route, find in ((APPLE_ROUTE, find_apple), (BING_ROUTE, find_bing)):
            try:
                hit = await find(client, settings, gate, target)
            except CollectionError as error:
                logger.warning("Listing route %s failed: %s", route, error)
                result.routes.append(RouteStatus(route, False, str(error)))
                continue
            result.hits.append(hit)
            result.routes.append(RouteStatus(route, True))
    return result
