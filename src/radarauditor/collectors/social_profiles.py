import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

import httpx
from pydantic import SecretStr

from radarauditor.collectors.apify import CostSink, ServiceCostSink, run_actor
from radarauditor.collectors.snapshot import SocialMeasurement, SocialPlatform, SocialPost
from radarauditor.collectors.social_links import AccountRef

INSTAGRAM_ACTOR = "apify~instagram-profile-scraper"
FACEBOOK_PAGE_ACTOR = "apify~facebook-pages-scraper"
FACEBOOK_POSTS_ACTOR = "apify~facebook-posts-scraper"
LINKEDIN_COMPANY_ACTOR = "sourabhbgp~linkedin-company-scraper"
LINKEDIN_PERSON_ACTOR = "harvestapi~linkedin-profile-scraper"
LINKEDIN_PERSON_POSTS_ACTOR = "harvestapi~linkedin-profile-posts"
X_ACTOR = "automation-lab~twitter-scraper"
COST_SERVICES: dict[SocialPlatform, str] = {
    "instagram": "instagram",
    "facebook": "facebook",
    "linkedin_company": "linkedin",
    "linkedin_person": "linkedin",
    "x": "x",
}
FACEBOOK_POST_LIMIT = 10
LINKEDIN_PERSON_POST_LIMIT = 10
X_POST_LIMIT = 20
X_DATE_FORMAT = "%a %b %d %H:%M:%S %z %Y"
MILLISECOND_EPOCH_THRESHOLD = 10_000_000_000

Item = Mapping[str, Any]


@dataclass(frozen=True)
class Evidence:
    websites: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Reading:
    measurement: SocialMeasurement
    evidence: Evidence


def text_of(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def count_of(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) and value >= 0 else None


def number_of(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def date_of(value: object) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = value / 1000 if value > MILLISECOND_EPOCH_THRESHOLD else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, X_DATE_FORMAT)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def flag_of(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def texts_of(*values: object) -> list[str]:
    found: list[str] = []
    for value in values:
        if isinstance(value, list):
            found.extend(text for text in map(text_of, value) if text)
        elif text := text_of(value):
            found.append(text)
    return found


def _usable(items: list[dict[str, Any]], key: str) -> Item | None:
    first = items[0] if items else None
    if first is None or first.get("error") or text_of(first.get(key)) is None:
        return None
    return first


def instagram_reading(items: list[dict[str, Any]]) -> Reading | None:
    item = _usable(items, "username")
    if item is None:
        return None
    posts = [
        SocialPost(
            published_at=date_of(post.get("timestamp")),
            kind=text_of(post.get("type")),
            likes=count_of(post.get("likesCount")),
            comments=count_of(post.get("commentsCount")),
        )
        for post in item.get("latestPosts") or []
        if isinstance(post, dict)
    ]
    website = text_of(item.get("externalUrl"))
    biography = text_of(item.get("biography"))
    measurement = SocialMeasurement(
        name=text_of(item.get("fullName")),
        description=biography,
        followers=count_of(item.get("followersCount")),
        following=count_of(item.get("followsCount")),
        posts_count=count_of(item.get("postsCount")),
        is_business=flag_of(item.get("isBusinessAccount")),
        category=text_of(item.get("businessCategoryName")),
        is_private=flag_of(item.get("private")),
        website=website,
        posts=posts,
    )
    return Reading(measurement, Evidence(texts_of(website), [], texts_of(biography)))


def facebook_reading(items: list[dict[str, Any]]) -> Reading | None:
    item = _usable(items, "title")
    if item is None:
        return None
    labels = texts_of(item.get("categories"))
    category = text_of(item.get("category")) or (labels[-1] if labels else None)
    websites = texts_of(item.get("website"), item.get("websites"))
    measurement = SocialMeasurement(
        name=text_of(item.get("title")),
        description=text_of(item.get("intro")),
        followers=count_of(item.get("followers")),
        following=count_of(item.get("followings")),
        page_likes=count_of(item.get("likes")),
        category=category,
        advertising=text_of(item.get("ad_status")),
        rating=number_of(item.get("rating")),
        website=websites[0] if websites else None,
    )
    evidence = Evidence(
        websites, texts_of(item.get("phone")), texts_of(item.get("address"), item.get("info"))
    )
    return Reading(measurement, evidence)


def facebook_posts(items: list[dict[str, Any]]) -> list[SocialPost]:
    return [
        SocialPost(
            published_at=date_of(item.get("time")),
            likes=count_of(item.get("likes")),
            comments=count_of(item.get("comments")),
            shares=count_of(item.get("shares")),
        )
        for item in items
        if not item.get("error")
    ]


def _company_places(item: Item) -> list[str]:
    address = item.get("address")
    offices = item.get("officeLocations")
    office_addresses = [
        office.get("fullAddress") for office in offices or [] if isinstance(office, dict)
    ]
    address_parts = list(address.values()) if isinstance(address, dict) else []
    return texts_of(item.get("headquarters"), office_addresses, address_parts)


def linkedin_company_reading(items: list[dict[str, Any]]) -> Reading | None:
    item = _usable(items, "companyName")
    if item is None:
        return None
    posts = [
        SocialPost(
            published_at=date_of(post.get("datePublished")),
            kind="repost" if post.get("isRepost") is True else "post",
            likes=count_of(post.get("reactions")),
            comments=count_of(post.get("comments")),
        )
        for post in item.get("recentPosts") or []
        if isinstance(post, dict)
    ]
    places = _company_places(item)
    website = text_of(item.get("website"))
    measurement = SocialMeasurement(
        name=text_of(item.get("companyName")),
        description=text_of(item.get("slogan")) or text_of(item.get("description")),
        followers=count_of(item.get("followerCount")),
        employees=count_of(item.get("exactEmployeeCount")),
        category=text_of(item.get("industry")),
        website=website,
        posts=posts,
    )
    return Reading(measurement, Evidence(texts_of(website), [], places))


def linkedin_person_reading(items: list[dict[str, Any]]) -> Reading | None:
    item = _usable(items, "publicIdentifier")
    if item is None:
        return None
    location = item.get("location")
    name = " ".join(texts_of(item.get("firstName"), item.get("lastName"))) or None
    measurement = SocialMeasurement(
        name=name,
        description=text_of(item.get("headline")),
        followers=count_of(item.get("followerCount")),
        connections=count_of(item.get("connectionsCount")),
    )
    places = texts_of(location.get("linkedinText")) if isinstance(location, dict) else []
    return Reading(measurement, Evidence([], [], places))


def linkedin_person_posts(items: list[dict[str, Any]]) -> list[SocialPost]:
    posts: list[SocialPost] = []
    for item in items:
        engagement = item.get("engagement")
        posted = item.get("postedAt")
        if not isinstance(engagement, dict):
            engagement = {}
        posts.append(
            SocialPost(
                published_at=date_of(posted.get("date")) if isinstance(posted, dict) else None,
                kind=text_of(item.get("type")),
                likes=count_of(engagement.get("likes")),
                comments=count_of(engagement.get("comments")),
                shares=count_of(engagement.get("shares")),
            )
        )
    return posts


def x_reading(items: list[dict[str, Any]]) -> Reading | None:
    item = _usable(items, "username")
    if item is None:
        return None
    website = text_of(item.get("website"))
    bio = text_of(item.get("bio"))
    measurement = SocialMeasurement(
        name=text_of(item.get("name")),
        description=bio,
        followers=count_of(item.get("followers")),
        following=count_of(item.get("following")),
        posts_count=count_of(item.get("tweetsCount")),
        website=website,
    )
    return Reading(
        measurement, Evidence(texts_of(website), [], texts_of(item.get("location"), bio))
    )


def x_posts(items: list[dict[str, Any]]) -> list[SocialPost]:
    posts: list[SocialPost] = []
    for item in items:
        if item.get("isRetweet") is True:
            kind = "retweet"
        elif item.get("isQuote") is True:
            kind = "quote"
        elif item.get("isReply") is True:
            kind = "reply"
        else:
            kind = "tweet"
        posts.append(
            SocialPost(
                published_at=date_of(item.get("createdAt")),
                kind=kind,
                likes=count_of(item.get("likeCount")),
                comments=count_of(item.get("replyCount")),
                shares=count_of(item.get("retweetCount")),
                views=count_of(item.get("viewCount")),
            )
        )
    return posts


@dataclass(frozen=True)
class _Route:
    actor: str
    payload: Callable[[AccountRef], dict[str, object]]
    reading: Callable[[list[dict[str, Any]]], Reading | None]


@dataclass(frozen=True)
class _PostsRoute:
    actor: str
    payload: Callable[[AccountRef], dict[str, object]]
    posts: Callable[[list[dict[str, Any]]], list[SocialPost]]


PROFILE_ROUTES: dict[SocialPlatform, _Route] = {
    "instagram": _Route(
        INSTAGRAM_ACTOR, lambda ref: {"usernames": [ref.handle]}, instagram_reading
    ),
    "facebook": _Route(
        FACEBOOK_PAGE_ACTOR, lambda ref: {"startUrls": [{"url": ref.url}]}, facebook_reading
    ),
    "linkedin_company": _Route(
        LINKEDIN_COMPANY_ACTOR,
        lambda ref: {"companies": [ref.handle], "maxResults": 1},
        linkedin_company_reading,
    ),
    "linkedin_person": _Route(
        LINKEDIN_PERSON_ACTOR, lambda ref: {"urls": [ref.url]}, linkedin_person_reading
    ),
    "x": _Route(
        X_ACTOR,
        lambda ref: {"mode": "profiles", "usernames": [ref.handle], "maxResults": 1},
        x_reading,
    ),
}

POSTS_ROUTES: dict[SocialPlatform, _PostsRoute] = {
    "facebook": _PostsRoute(
        FACEBOOK_POSTS_ACTOR,
        lambda ref: {"startUrls": [{"url": ref.url}], "resultsLimit": FACEBOOK_POST_LIMIT},
        facebook_posts,
    ),
    "linkedin_person": _PostsRoute(
        LINKEDIN_PERSON_POSTS_ACTOR,
        lambda ref: {"targetUrls": [ref.url], "maxPosts": LINKEDIN_PERSON_POST_LIMIT},
        linkedin_person_posts,
    ),
    "x": _PostsRoute(
        X_ACTOR,
        lambda ref: {"mode": "user-tweets", "usernames": [ref.handle], "maxResults": X_POST_LIMIT},
        x_posts,
    ),
}


def _sink(book: ServiceCostSink | None, platform: SocialPlatform) -> CostSink | None:
    return None if book is None else partial(book, COST_SERVICES[platform])


async def read_profile(
    client: httpx.AsyncClient,
    token: SecretStr,
    ref: AccountRef,
    book: ServiceCostSink | None = None,
) -> Reading | None:
    route = PROFILE_ROUTES[ref.platform]
    items = await run_actor(
        client,
        token,
        route.actor,
        route.payload(ref),
        on_cost=_sink(book, ref.platform),
    )
    return route.reading(items)


async def read_posts(
    client: httpx.AsyncClient,
    token: SecretStr,
    ref: AccountRef,
    book: ServiceCostSink | None = None,
) -> list[SocialPost]:
    route = POSTS_ROUTES[ref.platform]
    items = await run_actor(
        client,
        token,
        route.actor,
        route.payload(ref),
        on_cost=_sink(book, ref.platform),
    )
    return route.posts(items)
