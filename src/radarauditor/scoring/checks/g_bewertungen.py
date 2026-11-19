from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, median

from dateutil.parser import isoparse

from radarauditor.collectors.identity import Identity, identity, same_business
from radarauditor.collectors.snapshot import ListedBusiness, MapResultList, PlaceSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    dispatch,
    listed,
    not_measurable,
    number,
    own_identity,
    place_routes,
    route_entry,
)

STRONGEST_COMPETITORS = 3
REPLY_KEY_MARKERS = ("reply", "response")
REPLY_TIME_KEYS = ("publishTime", "updateTime", "createTime", "time")


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = isoparse(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def review_text(review: Mapping[str, object]) -> str:
    for key in ("text", "originalText"):
        value = review.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            return str(value["text"])
    return ""


def review_rating(review: Mapping[str, object]) -> float | None:
    value = review.get("rating")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def has_reply_field(review: Mapping[str, object]) -> bool:
    return any(marker in key.casefold() for key in review for marker in REPLY_KEY_MARKERS)


def reply_data_available(reviews: list[dict[str, object]]) -> bool:
    return any(has_reply_field(review) for review in reviews)


def owner_reply(review: Mapping[str, object]) -> object | None:
    for key, value in review.items():
        if value and any(marker in key.casefold() for marker in REPLY_KEY_MARKERS):
            return value
    return None


def reply_delay_days(review: Mapping[str, object]) -> float | None:
    reply = owner_reply(review)
    posted = parse_time(review.get("publishTime"))
    if not isinstance(reply, dict) or posted is None:
        return None
    for key in REPLY_TIME_KEYS:
        replied = parse_time(reply.get(key))
        if replied is not None:
            return max((replied - posted).total_seconds() / 86400, 0.0)
    return None


def judged_reviews(raw: RawAudit) -> tuple[list[dict[str, object]], str] | None:
    if raw.access is not None and raw.access.owner_reviews:
        return raw.access.owner_reviews, "profile"
    if raw.place is None:
        return None
    return raw.place.recent_reviews, sample_source(raw)


def sample_source(raw: RawAudit) -> str:
    route = raw.place.route_of("recent_reviews") if raw.place is not None else None
    return route or "places_api"


def _is_customer_listing(business: ListedBusiness, own: Identity) -> bool:
    return business.is_customer or same_business(
        own, identity(name=business.name, place_ids=[business.place_id])
    )


def _unique_names(pairs: list[tuple[str, int]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, count in pairs:
        key, running_number = name, 2
        while key in counts:
            key = f"{name} ({running_number})"
            running_number += 1
        counts[key] = count
    return counts


@dataclass(frozen=True)
class _ReviewComparison:
    pairs: list[tuple[str, int]]
    uncounted: list[str]
    source: str


def _listed_comparison(results: MapResultList, own: Identity) -> _ReviewComparison | CheckReturn:
    others = [
        business for business in results.businesses if not _is_customer_listing(business, own)
    ]
    if not others:
        return not_measurable("no_listed_businesses", phrase=results.phrase)
    pairs = [
        (business.name, business.review_count)
        for business in others
        if business.review_count is not None
    ]
    uncounted = [business.name for business in others if business.review_count is None]
    if not pairs:
        return not_measurable("no_review_counts", without_review_count=listed(uncounted))
    return _ReviewComparison(pairs, uncounted, "maps_result_list")


def _profile_comparison(competitors: list[PlaceSnapshot]) -> _ReviewComparison | CheckReturn:
    pairs = [(place.name, place.review_count) for place in competitors[:STRONGEST_COMPETITORS]]
    if not pairs:
        return not_measurable("no_competitors")
    return _ReviewComparison(pairs, [], "competitor_profiles")


def review_count_vs_competitors(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    comparison = (
        _listed_comparison(raw.map_results, own_identity(raw))
        if raw.map_results is not None
        else _profile_comparison(raw.competitor_places)
    )
    if not isinstance(comparison, _ReviewComparison):
        return comparison
    counts = _unique_names(comparison.pairs)
    average = mean(counts.values())
    measured: dict[str, object] = {
        "customer_reviews": raw.place.review_count,
        "competitors": counts,
        "competitor_average": round(average, 1),
        "compared_businesses": len(counts),
        "comparison_source": comparison.source,
    }
    if raw.map_results is not None:
        measured["phrase"] = raw.map_results.phrase
    if comparison.uncounted:
        measured["without_review_count"] = listed(comparison.uncounted)
    outcome = CheckOutcome.PASSED if raw.place.review_count >= average else CheckOutcome.FAILED
    return outcome, measured


@dataclass(frozen=True)
class _Peers:
    ratings: dict[str, float]
    source: str


def _peer_ratings(raw: RawAudit) -> _Peers | CheckReturn:
    if raw.map_results is not None:
        own = own_identity(raw)
        peers = [
            (business.name, business.rating)
            for business in raw.map_results.businesses
            if not _is_customer_listing(business, own) and business.rating is not None
        ]
        source = "maps_result_list"
    else:
        peers = [
            (competitor.name, competitor.rating)
            for competitor in raw.competitor_places[:STRONGEST_COMPETITORS]
            if competitor.rating is not None
        ]
        source = "competitor_profiles"
    if not peers:
        return not_measurable("no_peer_ratings")
    ratings: dict[str, float] = {}
    for name, value in peers:
        key, running_number = name, 2
        while key in ratings:
            key = f"{name} ({running_number})"
            running_number += 1
        ratings[key] = float(value or 0)
    return _Peers(ratings, source)


def average_rating(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    peers = _peer_ratings(raw)
    if not isinstance(peers, _Peers):
        return peers
    ratings, source = peers.ratings, peers.source
    margin = number(thresholds, "partial_margin_stars")
    middle = median(ratings.values())
    rating = raw.place.rating
    if rating is None:
        outcome = CheckOutcome.FAILED
    elif rating >= middle:
        outcome = CheckOutcome.PASSED
    elif round(middle - rating, 9) <= margin:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    measured: dict[str, object] = {
        "rating": rating,
        "review_count": raw.place.review_count,
        "median_rating": middle,
        "partial_margin_stars": margin,
        "peers": ratings,
        "compared_businesses": len(ratings),
        "comparison_source": source,
    }
    if raw.map_results is not None:
        measured["phrase"] = raw.map_results.phrase
    return outcome, measured


CHECKS: dict[str, CheckFunction] = {
    "g1": review_count_vs_competitors,
    "g2": average_rating,
}


PROFILE_FIELDS = {
    "g1": "review_count",
    "g2": "rating",
}
COMPARED_CHECKS = frozenset({"g1", "g2"})


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    outcome, measured = dispatch(CHECKS, check_id, raw, thresholds)
    field = PROFILE_FIELDS.get(check_id)
    if (
        raw.place is None
        or field is None
        or outcome is CheckOutcome.NOT_MEASURABLE
        or measured.get("review_source") == "profile"
    ):
        return outcome, measured
    if check_id in COMPARED_CHECKS and measured.get("comparison_source") == "maps_result_list":
        list_route = raw.map_results.route if raw.map_results is not None else None
        return outcome, {**measured, **route_entry([raw.place.route_of(field), list_route])}
    compared = raw.competitor_places[:STRONGEST_COMPETITORS] if check_id in COMPARED_CHECKS else []
    return outcome, {**measured, **place_routes([raw.place, *compared], field)}
