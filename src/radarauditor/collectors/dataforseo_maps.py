import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from radarauditor.collectors.maps_pages import MapListing
from radarauditor.collectors.paid_rankings import (
    GERMANY_LOCATION_CODE,
    LANGUAGE_CODE,
    TASK_GET,
    DataForSeoClient,
    TaskBatch,
    run_tasks,
)
from radarauditor.collectors.patterns import parse_http_url
from radarauditor.collectors.snapshot import PlaceSnapshot
from radarauditor.errors import CollectionError

logger = logging.getLogger(__name__)

MAPS_TASKS = "/v3/serp/google/maps"
INFO_TASKS = "/v3/business_data/google/my_business_info"
REVIEWS_TASKS = "/v3/business_data/google/reviews"
QUESTIONS_TASKS = "/v3/business_data/google/questions_and_answers"
MAPS_DEPTH = 20
MAPS_ZOOM = 14
REVIEW_DEPTH = 20
QUESTION_DEPTH = 20
ORGANIC_LISTING = "maps_search"
BUSINESS_INFO = "google_business_info"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S %z"
MAPS_CID_URL = "https://maps.google.com/?cid={cid}"
DATAFORSEO_ROUTE = "dataforseo"
BUSINESS_STATUSES = {
    "open": "OPERATIONAL",
    "close": "OPERATIONAL",
    "temporarily_closed": "CLOSED_TEMPORARILY",
    "closed_forever": "CLOSED_PERMANENTLY",
}
NO_PROFILE = "DataForSEO found no Google Business Profile"


class _Rating(BaseModel):
    value: float | None = None
    votes_count: int | None = None


class _Listing(BaseModel):
    type: str
    title: str | None = None
    url: str | None = None
    place_id: str | None = None
    cid: str | None = None
    rating: _Rating | None = None


class _WorkHours(BaseModel):
    timetable: dict[str, Any] | None = None
    current_status: str | None = None


class _WorkTime(BaseModel):
    work_hours: _WorkHours | None = None


class _Attributes(BaseModel):
    available_attributes: dict[str, list[str]] | None = None


class _Info(BaseModel):
    type: str
    title: str | None = None
    description: str | None = None
    category: str | None = None
    additional_categories: list[str] | None = None
    cid: str | None = None
    address: str | None = None
    place_id: str | None = None
    phone: str | None = None
    url: str | None = None
    total_photos: int | None = None
    attributes: _Attributes | None = None
    rating: _Rating | None = None
    work_time: _WorkTime | None = None


class _Review(BaseModel):
    review_text: str | None = None
    time_ago: str | None = None
    timestamp: str | None = None
    rating: _Rating | None = None
    owner_answer: str | None = None
    owner_time_ago: str | None = None
    owner_timestamp: str | None = None


class _Result(BaseModel):
    items: list[dict[str, Any]] | None = None
    items_without_answers: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class MapsQuery:
    phrase: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class MapsBatch:
    listings: dict[int, list[MapListing]]
    failed: dict[int, str]


@dataclass(frozen=True)
class ProfileRead:
    snapshot: PlaceSnapshot | None
    failure: str | None
    notes: list[str]


async def search_maps(client: DataForSeoClient, queries: list[MapsQuery]) -> MapsBatch:
    tasks = [
        {
            "keyword": query.phrase,
            "language_code": LANGUAGE_CODE,
            "location_coordinate": f"{query.latitude:.7f},{query.longitude:.7f},{MAPS_ZOOM}z",
            "depth": MAPS_DEPTH,
        }
        for query in queries
    ]
    batch = await run_tasks(client, MAPS_TASKS, tasks)
    listings: dict[int, list[MapListing]] = {}
    failed = dict(batch.failed)
    for index, rows in batch.results.items():
        try:
            listings[index] = _listings(rows)
        except CollectionError as error:
            failed[index] = str(error)
    return MapsBatch(listings, failed)


def profile_keyword(name: str, address: str, place_id: str | None) -> str:
    if place_id and place_id.startswith("ChIJ"):
        return f"place_id:{place_id}"
    return f"{name} {address}".strip()


async def read_profiles(client: DataForSeoClient, keywords: list[str]) -> list[ProfileRead]:
    base = {"language_code": LANGUAGE_CODE, "location_code": GERMANY_LOCATION_CODE}
    info, reviews, questions = await asyncio.gather(
        run_tasks(
            client, INFO_TASKS, [{**base, "keyword": key} for key in keywords], result_path=TASK_GET
        ),
        run_tasks(
            client,
            REVIEWS_TASKS,
            [
                {**base, "keyword": key, "depth": REVIEW_DEPTH, "sort_by": "newest"}
                for key in keywords
            ],
            result_path=TASK_GET,
        ),
        run_tasks(
            client,
            QUESTIONS_TASKS,
            [{**base, "keyword": key, "depth": QUESTION_DEPTH} for key in keywords],
            result_path=TASK_GET,
        ),
    )
    return [_profile(index, info, reviews, questions) for index in range(len(keywords))]


def _profile(index: int, info: TaskBatch, reviews: TaskBatch, questions: TaskBatch) -> ProfileRead:
    if index in info.failed:
        return ProfileRead(None, info.failed[index], [])
    try:
        found = _business_info(info.results[index])
    except CollectionError as error:
        return ProfileRead(None, str(error), [])
    if found is None:
        return ProfileRead(None, None, [NO_PROFILE])
    values = _info_values(found)
    notes: list[str] = []
    review_rows = _part(reviews, index, "reviews", notes)
    question_rows = _part(questions, index, "questions", notes)
    try:
        if review_rows is not None:
            values["recent_reviews"] = [_review(item) for item in _items(review_rows, _Review)]
        if question_rows is not None:
            values["question_count"] = _question_count(question_rows)
    except CollectionError as error:
        notes.append(f"DataForSEO reviews or questions not readable: {error}")
    read = sorted(field for field, value in values.items() if value is not None)
    snapshot = PlaceSnapshot.model_validate(
        {"recent_reviews": [], **values, "value_routes": dict.fromkeys(read, DATAFORSEO_ROUTE)}
    )
    return ProfileRead(snapshot, None, notes)


def _part(batch: TaskBatch, index: int, name: str, notes: list[str]) -> list[dict[str, Any]] | None:
    if index in batch.failed:
        notes.append(f"DataForSEO {name} not read: {batch.failed[index]}")
        return None
    return batch.results[index]


def _listings(rows: list[dict[str, Any]]) -> list[MapListing]:
    found: list[MapListing] = []
    for listing in _items(rows, _Listing):
        if listing.type != ORGANIC_LISTING or not listing.title:
            continue
        rating = listing.rating or _Rating()
        found.append(
            MapListing(
                name=listing.title,
                url="",
                place_id=listing.place_id or (f"cid:{listing.cid}" if listing.cid else None),
                rating=rating.value,
                review_count=rating.votes_count,
            )
        )
    return found


def _business_info(rows: list[dict[str, Any]]) -> _Info | None:
    return next((item for item in _items(rows, _Info) if item.type == BUSINESS_INFO), None)


def _info_values(info: _Info) -> dict[str, object]:
    rating = info.rating or _Rating()
    hours = info.work_time.work_hours if info.work_time is not None else None
    categories = [item for item in [info.category, *(info.additional_categories or [])] if item]
    attributes = info.attributes.available_attributes if info.attributes is not None else None
    return {
        "place_id": info.place_id or f"cid:{info.cid}",
        "name": info.title or "",
        "formatted_address": info.address or "",
        "phone": info.phone,
        "website": parse_http_url(info.url) if info.url else None,
        "primary_type": info.category,
        "types": categories,
        "rating": rating.value,
        "review_count": rating.votes_count or 0,
        "has_opening_hours": bool(hours and hours.timetable and any(hours.timetable.values())),
        "photo_count": info.total_photos or 0,
        "business_status": BUSINESS_STATUSES.get(hours.current_status or "") if hours else None,
        "google_maps_uri": parse_http_url(MAPS_CID_URL.format(cid=info.cid)) if info.cid else None,
        "description": info.description or "",
        "attributes": [value for group in (attributes or {}).values() for value in group],
    }


def _review(review: _Review) -> dict[str, object]:
    rating = review.rating or _Rating()
    reply = (
        {
            "text": review.owner_answer,
            "publishTime": _moment(review.owner_timestamp),
            "relativePublishTimeDescription": review.owner_time_ago,
        }
        if review.owner_answer
        else None
    )
    return {
        "publishTime": _moment(review.timestamp),
        "relativePublishTimeDescription": review.time_ago,
        "rating": rating.value,
        "text": review.review_text or "",
        "reviewReply": reply,
    }


def _question_count(rows: list[dict[str, Any]]) -> int:
    results = [_parse(_Result, row) for row in rows]
    return sum(
        len(result.items or []) + len(result.items_without_answers or []) for result in results
    )


def _moment(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).isoformat()
    except ValueError as error:
        raise CollectionError(f"DataForSEO time {value!r} is malformed") from error


def _items[ModelT: BaseModel](rows: list[dict[str, Any]], model: type[ModelT]) -> list[ModelT]:
    return [_parse(model, item) for row in rows for item in _parse(_Result, row).items or []]


def _parse[ModelT: BaseModel](model: type[ModelT], row: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(row)
    except ValidationError as error:
        raise CollectionError("DataForSEO business data are malformed") from error
