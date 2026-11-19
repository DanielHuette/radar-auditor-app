from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from pydantic.alias_generators import to_camel

from radarauditor.collectors import raw_answers
from radarauditor.collectors.patterns import parse_http_url
from radarauditor.collectors.snapshot import PLACES_API_FIELDS, PlaceSnapshot
from radarauditor.collectors.transport import send
from radarauditor.errors import CollectionError

PLACES_BASE_URL = "https://places.googleapis.com/v1"
SEARCH_TEXT_URL = f"{PLACES_BASE_URL}/places:searchText"
REQUEST_TIMEOUT_SECONDS = 20
LANGUAGE_CODE = "de"
REGION_CODE = "DE"
COMPETITOR_RADIUS_METERS = 10000.0
MAX_PAGE_SIZE = 20
DETAIL_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "websiteUri",
    "primaryType",
    "types",
    "rating",
    "userRatingCount",
    "regularOpeningHours",
    "photos",
    "reviews",
    "businessStatus",
    "googleMapsUri",
)
DETAIL_FIELD_MASK = ",".join(DETAIL_FIELDS)
IDENTIFY_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "websiteUri",
    "primaryType",
    "primaryTypeDisplayName",
    "types",
    "location",
)
IDENTIFY_FIELD_MASK = ",".join(f"places.{name}" for name in IDENTIFY_FIELDS)
IDENTIFY_PAGE_SIZE = 5
SEARCH_FIELD_MASK = ",".join(f"places.{name}" for name in DETAIL_FIELDS)
LISTING_FIELD_MASK = ",".join(
    f"places.{name}" for name in ("id", "displayName", "rating", "userRatingCount")
)
PROSPECT_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "regularOpeningHours",
    "photos",
    "businessStatus",
    "googleMapsUri",
)
PROSPECT_FIELD_MASK = ",".join([*(f"places.{name}" for name in PROSPECT_FIELDS), "nextPageToken"])


class PlacesSku(StrEnum):
    TEXT_SEARCH_IDS_ONLY = "text_search_essentials_ids_only"
    TEXT_SEARCH_ENTERPRISE = "text_search_enterprise"
    TEXT_SEARCH_ENTERPRISE_ATMOSPHERE = "text_search_enterprise_atmosphere"
    PLACE_DETAILS_ENTERPRISE_ATMOSPHERE = "place_details_enterprise_atmosphere"


PRICE_USD_PER_THOUSAND: dict[PlacesSku, Decimal] = {
    PlacesSku.TEXT_SEARCH_IDS_ONLY: Decimal(0),
    PlacesSku.TEXT_SEARCH_ENTERPRISE: Decimal(35),
    PlacesSku.TEXT_SEARCH_ENTERPRISE_ATMOSPHERE: Decimal(40),
    PlacesSku.PLACE_DETAILS_ENTERPRISE_ATMOSPHERE: Decimal(25),
}
FIND_PLACE_SKU = PlacesSku.TEXT_SEARCH_IDS_ONLY
SEARCH_BUSINESSES_SKU = PlacesSku.TEXT_SEARCH_ENTERPRISE
PLACE_DETAILS_SKU = PlacesSku.PLACE_DETAILS_ENTERPRISE_ATMOSPHERE
TOP_COMPETITORS_SKU = PlacesSku.TEXT_SEARCH_ENTERPRISE_ATMOSPHERE
RANKED_PLACE_IDS_SKU = PlacesSku.TEXT_SEARCH_IDS_ONLY
LISTED_BUSINESSES_SKU = PlacesSku.TEXT_SEARCH_ENTERPRISE
SEARCH_PROSPECTS_SKU = PlacesSku.TEXT_SEARCH_ENTERPRISE


def request_cost_eur(sku: PlacesSku, eur_per_usd: Decimal) -> Decimal:
    return PRICE_USD_PER_THOUSAND[sku] / 1000 * eur_per_usd


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class _LocalizedText(_ApiModel):
    text: str = ""


class _Review(_ApiModel):
    rating: float | None = None
    text: _LocalizedText | None = None
    publish_time: str | None = None
    relative_publish_time_description: str | None = None


class _LatLng(_ApiModel):
    latitude: float
    longitude: float


class _Place(_ApiModel):
    id: str
    location: _LatLng | None = None
    display_name: _LocalizedText | None = None
    primary_type_display_name: _LocalizedText | None = None
    formatted_address: str = ""
    national_phone_number: str | None = None
    website_uri: str | None = None
    primary_type: str | None = None
    types: list[str] = []
    rating: float | None = None
    user_rating_count: int = 0
    regular_opening_hours: dict[str, Any] | None = None
    photos: list[dict[str, Any]] = []
    reviews: list[_Review] = []
    business_status: str | None = None
    google_maps_uri: str | None = None


@dataclass(frozen=True)
class BusinessCandidate:
    place_id: str
    name: str
    formatted_address: str
    phone: str | None
    website: str | None
    primary_type: str | None
    category: str | None
    types: tuple[str, ...]
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class ListedPlace:
    place_id: str
    name: str
    review_count: int
    rating: float | None


@dataclass(frozen=True)
class ProspectPlace:
    place_id: str
    name: str
    formatted_address: str
    phone: str | None
    website: str | None
    maps_url: str | None
    rating: float | None
    review_count: int
    photo_count: int
    has_opening_hours: bool
    business_status: str | None


@dataclass(frozen=True)
class ProspectPage:
    places: list[ProspectPlace]
    next_page_token: str | None


class _PlaceId(_ApiModel):
    id: str


class _IdSearchResponse(_ApiModel):
    places: list[_PlaceId] = []


class _SearchResponse(_ApiModel):
    places: list[_Place] = []


class _ProspectResponse(_SearchResponse):
    next_page_token: str | None = None


async def find_place(name: str, address: str, api_key: SecretStr) -> str | None:
    body = {
        "textQuery": f"{name} {address}".strip(),
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
    }
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, "places.id", source="Places text search", json=body
    )
    result = _validate(_IdSearchResponse, payload, "Places text search")
    return result.places[0].id if result.places else None


async def search_businesses(query: str, api_key: SecretStr) -> list[BusinessCandidate]:
    body = {
        "textQuery": query.strip(),
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
        "pageSize": IDENTIFY_PAGE_SIZE,
    }
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, IDENTIFY_FIELD_MASK, source="Places identify", json=body
    )
    result = _validate(_SearchResponse, payload, "Places identify")
    return [_candidate(place) for place in result.places[:IDENTIFY_PAGE_SIZE]]


def _candidate(place: _Place) -> BusinessCandidate:
    category = place.primary_type_display_name
    return BusinessCandidate(
        place_id=place.id,
        name=place.display_name.text if place.display_name else "",
        formatted_address=place.formatted_address,
        phone=place.national_phone_number,
        website=place.website_uri,
        primary_type=place.primary_type,
        category=category.text if category is not None and category.text else None,
        types=tuple(place.types),
        latitude=place.location.latitude if place.location else None,
        longitude=place.location.longitude if place.location else None,
    )


async def place_details(place_id: str, api_key: SecretStr) -> PlaceSnapshot:
    url = f"{PLACES_BASE_URL}/places/{quote(place_id, safe='')}"
    payload = await _request(
        "GET",
        url,
        api_key,
        DETAIL_FIELD_MASK,
        source="Places details",
        params={"languageCode": LANGUAGE_CODE, "regionCode": REGION_CODE},
    )
    return _to_snapshot(_validate(_Place, payload, "Places details"))


async def top_competitors(
    phrase: str, latitude: float, longitude: float, api_key: SecretStr, limit: int = 3
) -> list[PlaceSnapshot]:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}, got {limit}")
    body = {
        "textQuery": phrase,
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
        "pageSize": limit,
        "locationBias": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": COMPETITOR_RADIUS_METERS,
            }
        },
    }
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, SEARCH_FIELD_MASK, source="Places nearby", json=body
    )
    result = _validate(_SearchResponse, payload, "Places nearby")
    return [_to_snapshot(place) for place in result.places[:limit]]


async def ranked_place_ids(
    phrase: str, latitude: float, longitude: float, api_key: SecretStr, radius_meters: float
) -> list[str]:
    body = {
        "textQuery": phrase,
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
        "pageSize": MAX_PAGE_SIZE,
        "locationBias": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": radius_meters,
            }
        },
    }
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, "places.id", source="Places ranking", json=body
    )
    return [place.id for place in _validate(_IdSearchResponse, payload, "Places ranking").places]


async def listed_businesses(
    phrase: str, latitude: float, longitude: float, api_key: SecretStr, radius_meters: float
) -> list[ListedPlace]:
    body = {
        "textQuery": phrase,
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
        "pageSize": MAX_PAGE_SIZE,
        "locationBias": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": radius_meters,
            }
        },
    }
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, LISTING_FIELD_MASK, source="Places listing", json=body
    )
    result = _validate(_SearchResponse, payload, "Places listing")
    return [
        ListedPlace(
            place_id=place.id,
            name=place.display_name.text if place.display_name else "",
            review_count=place.user_rating_count,
            rating=place.rating,
        )
        for place in result.places
    ]


async def search_prospects(
    phrase: str, api_key: SecretStr, page_token: str | None = None
) -> ProspectPage:
    body: dict[str, object] = {
        "textQuery": phrase.strip(),
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
        "pageSize": MAX_PAGE_SIZE,
    }
    if page_token is not None:
        body["pageToken"] = page_token
    payload = await _request(
        "POST", SEARCH_TEXT_URL, api_key, PROSPECT_FIELD_MASK, source="Places prospects", json=body
    )
    result = _validate(_ProspectResponse, payload, "Places prospects")
    return ProspectPage(
        places=[_prospect(place) for place in result.places],
        next_page_token=result.next_page_token or None,
    )


def _prospect(place: _Place) -> ProspectPlace:
    return ProspectPlace(
        place_id=place.id,
        name=place.display_name.text if place.display_name else "",
        formatted_address=place.formatted_address,
        phone=place.national_phone_number or None,
        website=place.website_uri or None,
        maps_url=place.google_maps_uri or None,
        rating=place.rating,
        review_count=place.user_rating_count,
        photo_count=len(place.photos),
        has_opening_hours=bool(place.regular_opening_hours),
        business_status=place.business_status,
    )


async def _request(
    method: str,
    url: str,
    api_key: SecretStr,
    field_mask: str,
    *,
    source: str,
    **request_arguments: Any,
) -> object:
    headers = {
        "X-Goog-Api-Key": api_key.get_secret_value(),
        "X-Goog-FieldMask": field_mask,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await send(
            client, method, url, source=source, headers=headers, **request_arguments
        )
    raw_answers.keep(raw_answers.file_name("place", "places_api", source), response.content)
    try:
        payload: object = response.json()
    except ValueError as error:
        raise CollectionError(f"{source} answer is not JSON") from error
    return payload


def _validate[ModelT: BaseModel](model: type[ModelT], payload: object, source: str) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise CollectionError(f"{source} answer is malformed") from error


def _to_snapshot(place: _Place) -> PlaceSnapshot:
    return PlaceSnapshot(
        place_id=place.id,
        name=place.display_name.text if place.display_name else "",
        formatted_address=place.formatted_address,
        phone=place.national_phone_number,
        website=parse_http_url(place.website_uri) if place.website_uri else None,
        primary_type=place.primary_type,
        types=place.types,
        rating=place.rating,
        review_count=place.user_rating_count,
        has_opening_hours=bool(place.regular_opening_hours),
        photo_count=len(place.photos),
        recent_reviews=[_review(review) for review in place.reviews],
        business_status=place.business_status,
        google_maps_uri=parse_http_url(place.google_maps_uri) if place.google_maps_uri else None,
        value_routes=dict.fromkeys(sorted(PLACES_API_FIELDS), "places_api"),
    )


def _review(review: _Review) -> dict[str, object]:
    return {
        "publishTime": review.publish_time,
        "rating": review.rating,
        "text": review.text.text if review.text else "",
        "relativePublishTimeDescription": review.relative_publish_time_description,
    }
