import httpx

from radarauditor.errors import CollectionError

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "RadarAuditor/1.0 (visibility audit; address lookup)"
REQUEST_TIMEOUT_SECONDS = 20


async def geocode(street: str, postal_code: str, city: str) -> tuple[float, float] | None:
    query = ", ".join(part for part in (street, f"{postal_code} {city}".strip()) if part)
    params = {"q": query, "format": "jsonv2", "limit": "1", "countrycodes": "de"}
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = await client.get(NOMINATIM_URL, params=params)
            response.raise_for_status()
            hits = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise CollectionError(f"address lookup for {query!r} failed: {error}") from error
    if not hits:
        return None
    try:
        return float(hits[0]["lat"]), float(hits[0]["lon"])
    except (KeyError, TypeError, ValueError) as error:
        raise CollectionError(f"address lookup for {query!r} returned {hits[0]!r}") from error
