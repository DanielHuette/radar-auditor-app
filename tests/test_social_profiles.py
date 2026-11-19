from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from apify_fake import apify_post
from pydantic import SecretStr

from radarauditor.collectors import social_profiles as sp
from radarauditor.collectors.social_links import AccountRef

TOKEN = SecretStr("t")
Items = list[dict[str, Any]]


INSTAGRAM: Items = [
    {
        "username": "katjakarsch",
        "fullName": "Katja Karsch",
        "biography": "Art • Objects • Light",
        "externalUrl": "http://www.katjakarsch.de",
        "followersCount": 159,
        "followsCount": 143,
        "postsCount": 80,
        "isBusinessAccount": False,
        "businessCategoryName": None,
        "private": False,
        "latestPosts": [
            {
                "type": "Video",
                "timestamp": "2026-10-31T17:38:34.000Z",
                "likesCount": 19,
                "commentsCount": 1,
            },
            {"type": "Image", "timestamp": "2026-10-23T09:00:00.000Z", "likesCount": -1},
        ],
    }
]


def test_instagram_reading_takes_figures_posts_and_evidence() -> None:
    reading = sp.instagram_reading(INSTAGRAM)

    assert reading is not None
    measurement = reading.measurement
    assert measurement.name == "Katja Karsch"
    assert (measurement.followers, measurement.following, measurement.posts_count) == (159, 143, 80)
    assert measurement.is_business is False
    assert measurement.is_private is False
    assert measurement.category is None
    assert measurement.website == "http://www.katjakarsch.de"
    assert measurement.posts[0].published_at == datetime(2026, 10, 31, 17, 38, 34, tzinfo=UTC)
    assert (measurement.posts[0].kind, measurement.posts[0].likes) == ("Video", 19)
    assert measurement.posts[0].comments == 1
    assert measurement.posts[1].likes is None
    assert reading.evidence.websites == ["http://www.katjakarsch.de"]
    assert reading.evidence.places == ["Art • Objects • Light"]


def test_instagram_business_account_keeps_the_category() -> None:
    item = {**INSTAGRAM[0], "isBusinessAccount": True, "businessCategoryName": "Artist"}

    reading = sp.instagram_reading([item])

    assert reading is not None
    assert reading.measurement.is_business is True
    assert reading.measurement.category == "Artist"


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"error": "not_found"}],
        [{"username": "", "followersCount": 3}],
        [{"followersCount": 3}],
    ],
)
def test_instagram_reading_is_none_without_a_profile(items: Items) -> None:
    assert sp.instagram_reading(items) is None


FACEBOOK: Items = [
    {
        "title": "Dachdeckerei Muster",
        "intro": "Seit 1990",
        "categories": ["Page", "Roofing contractor"],
        "followers": 410,
        "followings": 2,
        "likes": 395,
        "ad_status": "This Page isn't currently running ads.",
        "rating": 4.5,
        "website": "https://dachdeckerei-muster.de",
        "websites": ["https://dachdeckerei-muster.de", "https://wa.me/49251"],
        "phone": "+49 251 123456",
        "address": "Hafenstraße 12, 48155 Münster",
    }
]


def test_facebook_reading_takes_figures_and_evidence() -> None:
    reading = sp.facebook_reading(FACEBOOK)

    assert reading is not None
    measurement = reading.measurement
    assert measurement.name == "Dachdeckerei Muster"
    assert (measurement.followers, measurement.following, measurement.page_likes) == (410, 2, 395)
    assert measurement.category == "Roofing contractor"
    assert measurement.rating == 4.5
    assert measurement.advertising == "This Page isn't currently running ads."
    assert reading.evidence.websites == [
        "https://dachdeckerei-muster.de",
        "https://dachdeckerei-muster.de",
        "https://wa.me/49251",
    ]
    assert reading.evidence.phones == ["+49 251 123456"]
    assert reading.evidence.places == ["Hafenstraße 12, 48155 Münster"]


def test_facebook_reading_prefers_the_category_field_over_the_list() -> None:
    reading = sp.facebook_reading([{**FACEBOOK[0], "category": "Artist"}])

    assert reading is not None
    assert reading.measurement.category == "Artist"


def test_facebook_reading_without_categories_has_none() -> None:
    reading = sp.facebook_reading([{"title": "X", "categories": []}])

    assert reading is not None
    assert reading.measurement.category is None


def test_facebook_reading_is_none_for_a_page_that_is_not_available() -> None:
    item = {"url": "https://www.facebook.com/x", "error": "not_available"}

    assert sp.facebook_reading([item]) is None
    assert sp.facebook_reading([]) is None


def test_facebook_posts_take_time_and_interactions() -> None:
    posts = sp.facebook_posts(
        [
            {"time": "2026-11-12T20:16:47.000Z", "likes": 831, "comments": 29, "shares": 92},
            {"time": "kaputt", "likes": 1},
            {"error": "not_available"},
        ]
    )

    assert len(posts) == 2
    assert posts[0].published_at == datetime(2026, 11, 12, 20, 16, 47, tzinfo=UTC)
    assert (posts[0].likes, posts[0].comments, posts[0].shares) == (831, 29, 92)
    assert posts[1].published_at is None


COMPANY: Items = [
    {
        "companyName": "Apify",
        "slogan": "Thousands of Actors",
        "description": "Marketplace",
        "website": "https://apify.com/",
        "industry": "Technology",
        "followerCount": 48688,
        "exactEmployeeCount": None,
        "headquarters": "Prague",
        "address": {"city": "Prague", "postalCode": "11100", "state": None},
        "officeLocations": [{"fullAddress": "Vodickova 704/36, Prague"}, "x"],
        "recentPosts": [
            {
                "datePublished": "2026-11-12T20:43:30.947Z",
                "reactions": 21,
                "comments": 0,
                "isRepost": False,
            },
            {"datePublished": "2026-11-11T10:00:00.000Z", "reactions": 16, "isRepost": True},
        ],
    }
]


def test_linkedin_company_reading_takes_figures_posts_and_places() -> None:
    reading = sp.linkedin_company_reading(COMPANY)

    assert reading is not None
    measurement = reading.measurement
    assert measurement.name == "Apify"
    assert measurement.description == "Thousands of Actors"
    assert measurement.followers == 48688
    assert measurement.employees is None
    assert measurement.category == "Technology"
    assert [(post.kind, post.likes, post.comments) for post in measurement.posts] == [
        ("post", 21, 0),
        ("repost", 16, None),
    ]
    assert reading.evidence.websites == ["https://apify.com/"]
    assert reading.evidence.places == ["Prague", "Vodickova 704/36, Prague", "Prague", "11100"]


def test_linkedin_company_description_falls_back_to_the_long_text() -> None:
    reading = sp.linkedin_company_reading([{"companyName": "A", "description": "Long text"}])

    assert reading is not None
    assert reading.measurement.description == "Long text"
    assert reading.measurement.posts == []


def test_linkedin_company_reading_is_none_without_a_company() -> None:
    assert sp.linkedin_company_reading([]) is None
    assert sp.linkedin_company_reading([{"error": "x"}]) is None


PERSON: Items = [
    {
        "publicIdentifier": "katja-karsch-009aa23b0",
        "firstName": "Katja",
        "lastName": "Karsch",
        "headline": "Freischaffende Künstlerin",
        "followerCount": 303,
        "connectionsCount": 298,
        "location": {"linkedinText": "Münster, North Rhine-Westphalia, Germany"},
    }
]


def test_linkedin_person_reading_takes_name_headline_and_counts() -> None:
    reading = sp.linkedin_person_reading(PERSON)

    assert reading is not None
    measurement = reading.measurement
    assert measurement.name == "Katja Karsch"
    assert measurement.description == "Freischaffende Künstlerin"
    assert (measurement.followers, measurement.connections) == (303, 298)
    assert reading.evidence.places == ["Münster, North Rhine-Westphalia, Germany"]
    assert reading.evidence.websites == []


def test_linkedin_person_reading_without_location_has_no_places() -> None:
    reading = sp.linkedin_person_reading([{"publicIdentifier": "a", "location": None}])

    assert reading is not None
    assert reading.evidence.places == []
    assert reading.measurement.name is None


def test_linkedin_person_reading_is_none_without_a_profile() -> None:
    assert sp.linkedin_person_reading([]) is None


def test_linkedin_person_posts_take_date_type_and_engagement() -> None:
    posts = sp.linkedin_person_posts(
        [
            {
                "type": "post",
                "postedAt": {"date": "2026-11-02T08:00:00.000Z", "timestamp": 1},
                "engagement": {"likes": 3, "comments": 1, "shares": 0},
            },
            {"type": "repost", "postedAt": None, "engagement": None},
        ]
    )

    assert posts[0].published_at == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert (posts[0].kind, posts[0].likes, posts[0].comments, posts[0].shares) == ("post", 3, 1, 0)
    assert posts[1].published_at is None
    assert posts[1].likes is None


X_PROFILE: Items = [
    {
        "username": "apify",
        "name": "Apify",
        "bio": "Thousands of Actors",
        "location": "The Interwebz",
        "website": "https://apify.com",
        "followers": 22870,
        "following": 308,
        "tweetsCount": 5364,
    }
]


def test_x_reading_takes_figures_and_evidence() -> None:
    reading = sp.x_reading(X_PROFILE)

    assert reading is not None
    measurement = reading.measurement
    assert (measurement.followers, measurement.following, measurement.posts_count) == (
        22870,
        308,
        5364,
    )
    assert measurement.website == "https://apify.com"
    assert reading.evidence.places == ["The Interwebz", "Thousands of Actors"]


def test_x_reading_is_none_without_a_profile() -> None:
    assert sp.x_reading([]) is None


@pytest.mark.parametrize(
    ("flags", "kind"),
    [
        ({}, "tweet"),
        ({"isReply": True}, "reply"),
        ({"isQuote": True}, "quote"),
        ({"isRetweet": True, "isQuote": True}, "retweet"),
    ],
)
def test_x_posts_name_the_kind_of_post(flags: dict[str, bool], kind: str) -> None:
    posts = sp.x_posts([{"createdAt": "Thu Nov 12 12:35:52 +0000 2026", **flags}])

    assert posts[0].kind == kind
    assert posts[0].published_at == datetime(2026, 11, 12, 12, 35, 52, tzinfo=UTC)


def test_x_posts_take_interactions_and_views() -> None:
    posts = sp.x_posts([{"likeCount": 19, "replyCount": 2, "retweetCount": 3, "viewCount": 109}])

    assert (posts[0].likes, posts[0].comments, posts[0].shares, posts[0].views) == (19, 2, 3, 109)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (5, 5),
        (5.9, 5),
        (-1, None),
        (True, None),
        ("5", None),
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_count_of_accepts_non_negative_numbers_only(value: object, expected: int | None) -> None:
    assert sp.count_of(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-31T17:38:34.000Z", datetime(2026, 10, 31, 17, 38, 34, tzinfo=UTC)),
        ("2026-10-31T17:38:34", datetime(2026, 10, 31, 17, 38, 34, tzinfo=UTC)),
        ("Thu Nov 12 12:35:52 +0000 2026", datetime(2026, 11, 12, 12, 35, 52, tzinfo=UTC)),
        (1793006352, datetime(2026, 10, 26, 9, 19, 12, tzinfo=UTC)),
        (1793006352206, datetime(2026, 10, 26, 9, 19, 12, 206000, tzinfo=UTC)),
        ("gestern", None),
        (None, None),
        (True, None),
        (10**18, None),
        (float("nan"), None),
    ],
)
def test_date_of_reads_iso_x_and_epoch_dates(value: object, expected: datetime | None) -> None:
    assert sp.date_of(value) == expected


def test_epoch_threshold_separates_seconds_from_milliseconds() -> None:
    threshold = sp.MILLISECOND_EPOCH_THRESHOLD
    assert sp.date_of(threshold) == datetime.fromtimestamp(threshold, UTC)
    assert sp.date_of(threshold + 1) == datetime.fromtimestamp((threshold + 1) / 1000, UTC)


def test_text_of_trims_and_drops_empty_values() -> None:
    assert sp.text_of("  a ") == "a"
    assert sp.text_of("   ") is None
    assert sp.text_of(3) is None


def _actor_url(actor: str) -> str:
    return f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _same_body(request: httpx.Request, payload: dict[str, object]) -> bool:
    return request.content == httpx.Request("POST", "https://x", json=payload).content


PROFILE_CASES = [
    (
        AccountRef("instagram", "katjakarsch", "https://www.instagram.com/katjakarsch/"),
        sp.INSTAGRAM_ACTOR,
        {"usernames": ["katjakarsch"]},
    ),
    (
        AccountRef("facebook", "muster", "https://www.facebook.com/muster"),
        sp.FACEBOOK_PAGE_ACTOR,
        {"startUrls": [{"url": "https://www.facebook.com/muster"}]},
    ),
    (
        AccountRef("linkedin_company", "apify", "https://www.linkedin.com/company/apify"),
        sp.LINKEDIN_COMPANY_ACTOR,
        {"companies": ["apify"], "maxResults": 1},
    ),
    (
        AccountRef("linkedin_person", "katja", "https://www.linkedin.com/in/katja"),
        sp.LINKEDIN_PERSON_ACTOR,
        {"urls": ["https://www.linkedin.com/in/katja"]},
    ),
    (
        AccountRef("x", "apify", "https://x.com/apify"),
        sp.X_ACTOR,
        {"mode": "profiles", "usernames": ["apify"], "maxResults": 1},
    ),
]

POSTS_CASES = [
    (
        AccountRef("facebook", "muster", "https://www.facebook.com/muster"),
        sp.FACEBOOK_POSTS_ACTOR,
        {"startUrls": [{"url": "https://www.facebook.com/muster"}], "resultsLimit": 10},
    ),
    (
        AccountRef("linkedin_person", "katja", "https://www.linkedin.com/in/katja"),
        sp.LINKEDIN_PERSON_POSTS_ACTOR,
        {"targetUrls": ["https://www.linkedin.com/in/katja"], "maxPosts": 10},
    ),
    (
        AccountRef("x", "apify", "https://x.com/apify"),
        sp.X_ACTOR,
        {"mode": "user-tweets", "usernames": ["apify"], "maxResults": 20},
    ),
]


@respx.mock
@pytest.mark.parametrize(("ref", "actor", "payload"), PROFILE_CASES)
async def test_read_profile_sends_the_input_of_the_platform_actor(
    ref: AccountRef, actor: str, payload: dict[str, object]
) -> None:
    route = apify_post(_actor_url(actor)).mock(return_value=httpx.Response(201, json=[]))

    async with httpx.AsyncClient() as client:
        reading = await sp.read_profile(client, TOKEN, ref)

    assert reading is None
    assert _same_body(route.calls.last.request, payload)


@respx.mock
@pytest.mark.parametrize(("ref", "actor", "payload"), POSTS_CASES)
async def test_read_posts_sends_the_input_of_the_posts_actor(
    ref: AccountRef, actor: str, payload: dict[str, object]
) -> None:
    route = apify_post(_actor_url(actor)).mock(return_value=httpx.Response(201, json=[]))

    async with httpx.AsyncClient() as client:
        posts = await sp.read_posts(client, TOKEN, ref)

    assert posts == []
    assert _same_body(route.calls.last.request, payload)


def test_only_facebook_linkedin_person_and_x_read_posts_in_a_second_run() -> None:
    assert sorted(sp.POSTS_ROUTES) == ["facebook", "linkedin_person", "x"]
    assert sorted(sp.PROFILE_ROUTES) == [
        "facebook",
        "instagram",
        "linkedin_company",
        "linkedin_person",
        "x",
    ]
