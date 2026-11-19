import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from apify_fake import apify_post
from conftest import SITE, build_website
from pydantic import HttpUrl, SecretStr

from radarauditor.collectors import legal_notice, social
from radarauditor.collectors import social_profiles as sp
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    SocialAccount,
    SocialSnapshot,
    WebsiteSnapshot,
)
from radarauditor.collectors.social_search import GOOGLE_SEARCH_ACTOR
from radarauditor.errors import CollectionError, ConfigurationError

TOKEN = SecretStr("apify-secret-token")
LEGAL_URL = f"{SITE}/impressum/"
IMPRESSUM = """
<html><body><main><h1>Impressum</h1><p>Angaben gemäß § 5 TMG</p>
<p>Max Muster Dachdeckerei</p><p>Hauptstraße 5</p><p>48143 Münster</p>
<p>Telefon: 0251 123456</p></main></body></html>
"""
OWN_INSTAGRAM = "https://www.instagram.com/dachdeckereimuster/"
SEARCHED_PLATFORMS = ["instagram", "facebook", "linkedin_company", "linkedin_person", "x"]

Answer = list[dict[str, Any]] | Callable[[dict[str, Any]], list[dict[str, Any]]] | httpx.Response


def _site(
    links: list[str], legal_links: list[str] | None = None, **changes: Any
) -> WebsiteSnapshot:
    site = build_website()
    pages = []
    for page in site.pages:
        found: list[str] = []
        if str(page.url) == f"{SITE}/":
            found = links
        elif str(page.url) == LEGAL_URL:
            found = legal_links or []
        pages.append(page.model_copy(update={"external_links": [HttpUrl(link) for link in found]}))
    return site.model_copy(
        update={"pages": pages, "legal_notice_url": HttpUrl(LEGAL_URL), **changes}
    )


def _bare_site(**page_changes: Any) -> WebsiteSnapshot:
    page = (
        build_website()
        .pages[0]
        .model_copy(update={"tel_links": [], "external_links": [], "json_ld": [], **page_changes})
    )
    return build_website().model_copy(
        update={"pages": [page], "legal_notice_url": None, "footer_nap": {}}
    )


def _actor_url(actor: str) -> str:
    return f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _mock_apify(answers: dict[str, Answer]) -> dict[str, respx.Route]:
    def respond(request: httpx.Request, answer: Answer) -> httpx.Response:
        if isinstance(answer, httpx.Response):
            return answer
        items = answer(json.loads(request.content)) if callable(answer) else answer
        return httpx.Response(201, json=items)

    return {
        actor: apify_post(_actor_url(actor)).mock(
            side_effect=lambda request, answer=answer: respond(request, answer)
        )
        for actor, answer in answers.items()
    }


def _mock_legal_notice() -> respx.Route:
    return respx.get(LEGAL_URL).mock(return_value=httpx.Response(200, text=IMPRESSUM))


def _result_page(phrase: str, links: list[str]) -> dict[str, Any]:
    return {
        "#error": False,
        "searchQuery": {"term": phrase},
        "organicResults": [{"url": link, "title": "hit"} for link in links],
    }


def _mock_search(hits: dict[str, list[str]] | None = None) -> list[list[str]]:
    rounds: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        phrases = json.loads(request.content)["queries"].split("\n")
        rounds.append(phrases)
        pages = []
        for phrase in phrases:
            scope = phrase.split(" ")[0].removeprefix("site:")
            found = (hits or {}).get(phrase, (hits or {}).get(scope, []))
            pages.append(_result_page(phrase, found))
        return httpx.Response(201, json=pages)

    apify_post(_actor_url(GOOGLE_SEARCH_ACTOR)).mock(side_effect=respond)
    return rounds


def _instagram(**overrides: Any) -> dict[str, Any]:
    return {
        "username": "dachdeckereimuster",
        "followersCount": 159,
        "followsCount": 10,
        "postsCount": 8,
        "externalUrl": None,
        "biography": "Dächer",
        "latestPosts": [{"timestamp": "2026-10-31T17:38:34.000Z", "likesCount": 4}],
        **overrides,
    }


def _by_platform(snapshot: SocialSnapshot) -> dict[str, SocialAccount]:
    return {account.platform: account for account in snapshot.accounts}


def _gaps(snapshot: SocialSnapshot) -> list[tuple[str, str]]:
    return [(gap.platform, gap.reason) for gap in snapshot.gaps]


@respx.mock
async def test_linked_accounts_are_confirmed_measured_and_never_searched() -> None:
    rounds = _mock_search()
    routes = _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: [_instagram()],
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster", "followers": 410}],
            sp.FACEBOOK_POSTS_ACTOR: [{"time": "2026-11-12T20:16:47.000Z", "likes": 8}],
            sp.LINKEDIN_COMPANY_ACTOR: [{"companyName": "Muster", "followerCount": 50}],
            sp.LINKEDIN_PERSON_ACTOR: [{"publicIdentifier": "max", "followerCount": 303}],
            sp.LINKEDIN_PERSON_POSTS_ACTOR: [
                {"type": "post", "postedAt": {"date": "2026-11-02T08:00:00.000Z"}}
            ],
            sp.X_ACTOR: lambda body: (
                [{"username": "muster", "followers": 7}]
                if body["mode"] == "profiles"
                else [{"createdAt": "Thu Nov 12 12:35:52 +0000 2026", "likeCount": 2}]
            ),
        }
    )
    site = _site(
        [
            "https://x.com/muster",
            OWN_INSTAGRAM,
            "https://www.facebook.com/dachdeckereimuster",
            "https://www.linkedin.com/company/dachdeckerei-muster",
        ],
        legal_links=["https://www.linkedin.com/in/max"],
    )

    snapshot = await social.collect_social(site, TOKEN)

    assert rounds == []
    assert snapshot.gaps == []
    assert [account.platform for account in snapshot.accounts] == SEARCHED_PLATFORMS
    accounts = _by_platform(snapshot)
    assert {account.status for account in snapshot.accounts} == {"confirmed"}
    assert accounts["instagram"].source == "website_link"
    assert accounts["instagram"].proof == "linked from the website"
    assert accounts["linkedin_person"].source == "legal_notice_link"
    assert accounts["linkedin_person"].proof == "linked from the legal notice"
    assert accounts["instagram"].followers == 159
    assert accounts["instagram"].posts[0].likes == 4
    assert accounts["facebook"].posts[0].likes == 8
    assert accounts["linkedin_person"].posts[0].kind == "post"
    assert accounts["x"].followers == 7
    assert accounts["x"].posts[0].likes == 2
    assert routes[sp.X_ACTOR].call_count == 2
    assert routes[sp.INSTAGRAM_ACTOR].call_count == 1


@respx.mock
async def test_platforms_without_link_are_searched_with_every_name_before_they_count_as_gaps() -> (
    None
):
    _mock_legal_notice()
    rounds = _mock_search()

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert snapshot.accounts == []
    assert _gaps(snapshot) == [(platform, "no_account_found") for platform in SEARCHED_PLATFORMS]
    assert rounds == [
        [
            'site:instagram.com "Max Muster Dachdeckerei"',
            'site:facebook.com "Max Muster Dachdeckerei"',
            'site:linkedin.com/company "Max Muster Dachdeckerei"',
            'site:linkedin.com/in "Max Muster Dachdeckerei"',
            'site:x.com "Max Muster Dachdeckerei"',
        ],
        [
            'site:instagram.com "dachdeckerei-muster"',
            'site:facebook.com "dachdeckerei-muster"',
            'site:linkedin.com/company "dachdeckerei-muster"',
            'site:linkedin.com/in "dachdeckerei-muster"',
            'site:x.com "dachdeckerei-muster"',
        ],
    ]


@respx.mock
async def test_only_the_platforms_without_a_link_are_searched() -> None:
    _mock_legal_notice()
    rounds = _mock_search()
    _mock_apify({sp.INSTAGRAM_ACTOR: [_instagram()]})

    snapshot = await social.collect_social(_site([OWN_INSTAGRAM]), TOKEN)

    assert [account.platform for account in snapshot.accounts] == ["instagram"]
    assert _gaps(snapshot) == [
        (platform, "no_account_found") for platform in SEARCHED_PLATFORMS[1:]
    ]
    assert all(len(phrases) == 4 for phrases in rounds)
    assert not any("instagram.com" in phrase for phrases in rounds for phrase in phrases)


@respx.mock
async def test_given_names_come_first_and_only_three_variants_are_searched() -> None:
    _mock_legal_notice()
    rounds = _mock_search()

    await social.collect_social(_site([]), TOKEN, ("Katja Karsch", "Atelier Karsch"))

    assert [phrases[0] for phrases in rounds] == [
        'site:instagram.com "Katja Karsch"',
        'site:instagram.com "Atelier Karsch"',
        'site:instagram.com "Max Muster Dachdeckerei"',
    ]


@respx.mock
async def test_without_markup_the_legal_notice_gives_the_name() -> None:
    _mock_legal_notice()
    rounds = _mock_search()
    site = _site([])
    start = site.pages[0].model_copy(update={"json_ld": []})

    await social.collect_social(site.model_copy(update={"pages": [start, *site.pages[1:]]}), TOKEN)

    assert rounds[0][0] == 'site:instagram.com "Max Muster Dachdeckerei"'


@respx.mock
async def test_without_a_legal_notice_only_the_domain_gives_the_name() -> None:
    rounds = _mock_search()
    site = _bare_site().model_copy(update={"footer_nap": {"name": "Muster Dach"}})

    await social.collect_social(site, TOKEN)

    assert [phrases[0] for phrases in rounds] == ['site:instagram.com "dachdeckerei-muster"']


@respx.mock
async def test_a_legal_notice_that_cannot_be_read_is_logged_and_the_search_goes_on(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreadable(site: WebsiteSnapshot) -> BusinessFacts:
        raise CollectionError("legal notice answered HTTP 404")

    monkeypatch.setattr(legal_notice, "read_site", unreadable)
    rounds = _mock_search()

    with caplog.at_level(logging.WARNING, logger=social.__name__):
        snapshot = await social.collect_social(_site([]), TOKEN)

    assert len(rounds) == 1
    assert _gaps(snapshot) == [(platform, "no_account_found") for platform in SEARCHED_PLATFORMS]
    assert "legal notice was not read" in caplog.text
    assert "HTTP 404" in caplog.text


@respx.mock
async def test_without_any_name_nothing_is_searched_and_the_gaps_say_so() -> None:
    search = apify_post(_actor_url(GOOGLE_SEARCH_ACTOR)).mock(
        return_value=httpx.Response(201, json=[])
    )
    site = _bare_site().model_copy(update={"start_url": HttpUrl("https://ab.de/")})

    snapshot = await social.collect_social(site, TOKEN)

    assert search.call_count == 0
    assert _gaps(snapshot) == [(platform, "search_failed") for platform in SEARCHED_PLATFORMS]
    assert {gap.detail for gap in snapshot.gaps} == {"no name to search for"}


@respx.mock
async def test_the_search_goes_on_with_the_next_name_until_an_account_is_confirmed() -> None:
    _mock_legal_notice()
    rounds = _mock_search(
        {
            'site:instagram.com "Max Muster Dachdeckerei"': [
                "https://www.instagram.com/namensvetter/"
            ],
            'site:instagram.com "dachdeckerei-muster"': [OWN_INSTAGRAM],
        }
    )
    _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: lambda body: [
                _instagram(
                    username=body["usernames"][0],
                    externalUrl=f"{SITE}/start"
                    if body["usernames"][0] == "dachdeckereimuster"
                    else None,
                )
            ]
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert [(account.handle, account.status) for account in snapshot.accounts] == [
        ("dachdeckereimuster", "confirmed")
    ]
    assert len(rounds) == 2


@respx.mock
async def test_a_confirmed_platform_is_not_searched_again() -> None:
    _mock_legal_notice()
    rounds = _mock_search({"instagram.com": [OWN_INSTAGRAM]})
    _mock_apify({sp.INSTAGRAM_ACTOR: [_instagram(externalUrl=SITE)]})

    await social.collect_social(_site([]), TOKEN)

    assert len(rounds[0]) == 5
    assert len(rounds[1]) == 4
    assert not any("instagram.com" in phrase for phrase in rounds[1])


@respx.mock
async def test_when_every_platform_is_settled_the_remaining_names_are_not_searched() -> None:
    _mock_legal_notice()
    rounds = _mock_search({"instagram.com": [OWN_INSTAGRAM]})
    _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: [_instagram(externalUrl=SITE)],
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster"}],
            sp.FACEBOOK_POSTS_ACTOR: [],
            sp.LINKEDIN_COMPANY_ACTOR: [{"companyName": "Muster"}],
            sp.LINKEDIN_PERSON_ACTOR: [{"publicIdentifier": "max"}],
            sp.LINKEDIN_PERSON_POSTS_ACTOR: [],
            sp.X_ACTOR: [{"username": "muster"}],
        }
    )
    site = _site(
        [
            "https://www.facebook.com/a",
            "https://www.linkedin.com/company/b",
            "https://www.linkedin.com/in/c",
            "https://x.com/d",
        ]
    )

    await social.collect_social(site, TOKEN)

    assert rounds == [['site:instagram.com "Max Muster Dachdeckerei"']]


@respx.mock
async def test_result_links_to_posts_of_other_accounts_are_no_candidates() -> None:
    _mock_legal_notice()
    _mock_search(
        {
            "facebook.com": [
                "https://www.facebook.com/clueso.musik/posts/pack-die-decke-ein/3842831872421428/",
                "https://www.facebook.com/groups/1637967833218978/posts/2257699574579131/",
            ],
            "x.com": ["https://x.com/someone/status/1259811365751074818"],
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert snapshot.accounts == []
    assert _gaps(snapshot) == [(platform, "no_account_found") for platform in SEARCHED_PLATFORMS]


@respx.mock
@pytest.mark.parametrize(
    ("overrides", "proof"),
    [
        ({"externalUrl": f"{SITE}/start"}, "the profile links to dachdeckerei-muster.de"),
        ({"biography": "Dachdecker in Münster"}, "the profile carries the name and names Münster"),
        ({"biography": "Dachdecker in MÜNSTER"}, "the profile carries the name and names Münster"),
    ],
)
async def test_a_google_hit_counts_when_the_profile_links_to_the_domain_or_has_name_and_place(
    overrides: dict[str, Any], proof: str
) -> None:
    _mock_legal_notice()
    _mock_search({"instagram.com": [OWN_INSTAGRAM]})
    _mock_apify({sp.INSTAGRAM_ACTOR: [_instagram(**overrides)]})

    snapshot = await social.collect_social(_site([]), TOKEN)

    account = _by_platform(snapshot)["instagram"]
    assert account.status == "confirmed"
    assert account.source == "google_search"
    assert account.proof == proof
    assert account.followers == 159
    assert [gap.platform for gap in snapshot.gaps] == SEARCHED_PLATFORMS[1:]


@respx.mock
@pytest.mark.parametrize(
    ("handle", "full_name", "biography", "status"),
    [
        ("namensvetter", "Bedachungen Meier", "Dachdecker in Münster", "unsure"),
        ("namensvetter", "Max Muster Dachdeckerei", "Dachdecker in Münster", "confirmed"),
        ("dachdeckereimuster", None, "Dachdecker im Münsterland", "unsure"),
        ("dachdeckereimuster", None, "Dachdecker in Berlin", "unsure"),
    ],
)
async def test_the_place_counts_only_as_a_whole_word_and_together_with_the_name(
    handle: str, full_name: str | None, biography: str, status: str
) -> None:
    _mock_legal_notice()
    _mock_search({"instagram.com": [f"https://www.instagram.com/{handle}/"]})
    _mock_apify(
        {sp.INSTAGRAM_ACTOR: [_instagram(username=handle, fullName=full_name, biography=biography)]}
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert _by_platform(snapshot)["instagram"].status == status


@respx.mock
async def test_a_google_hit_counts_when_the_phone_number_matches() -> None:
    _mock_legal_notice()
    _mock_search({"facebook.com": ["https://www.facebook.com/muster-dach"]})
    routes = _mock_apify(
        {
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster", "phone": "+49 (0) 251 / 123 456"}],
            sp.FACEBOOK_POSTS_ACTOR: [{"time": "2026-11-12T20:16:47.000Z"}],
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    account = _by_platform(snapshot)["facebook"]
    assert account.status == "confirmed"
    assert account.proof == "the phone number matches"
    assert routes[sp.FACEBOOK_POSTS_ACTOR].call_count == 1
    assert len(account.posts) == 1


@respx.mock
@pytest.mark.parametrize(
    ("phone", "status"),
    [("0251 12345", "unsure"), ("0251 123456", "confirmed"), ("tel:+49251123456", "confirmed")],
)
async def test_the_phone_number_must_match_completely(phone: str, status: str) -> None:
    _mock_legal_notice()
    _mock_search({"facebook.com": ["https://www.facebook.com/muster-dach"]})
    _mock_apify(
        {
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster", "phone": phone}],
            sp.FACEBOOK_POSTS_ACTOR: [],
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert _by_platform(snapshot)["facebook"].status == status


@respx.mock
@pytest.mark.parametrize(("digits", "status"), [("123456", "unsure"), ("1234567", "confirmed")])
async def test_a_phone_number_needs_seven_digits_to_count(digits: str, status: str) -> None:
    _mock_search({"facebook.com": ["https://www.facebook.com/muster-dach"]})
    _mock_apify(
        {
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster", "phone": digits}],
            sp.FACEBOOK_POSTS_ACTOR: [],
        }
    )

    snapshot = await social.collect_social(_bare_site(), TOKEN, phones=(digits,))

    assert _by_platform(snapshot)["facebook"].status == status


@respx.mock
async def test_without_a_known_city_the_place_does_not_count() -> None:
    _mock_search({"instagram.com": [OWN_INSTAGRAM]})
    _mock_apify({sp.INSTAGRAM_ACTOR: [_instagram(biography="Dachdecker in Münster")]})

    snapshot = await social.collect_social(_bare_site(), TOKEN)

    assert _by_platform(snapshot)["instagram"].status == "unsure"


@respx.mock
async def test_city_and_phone_handed_in_count_like_those_of_the_website() -> None:
    _mock_search(
        {
            "instagram.com": [OWN_INSTAGRAM],
            "facebook.com": ["https://www.facebook.com/muster-dach"],
        }
    )
    _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: [_instagram(biography="Dachdecker in Münster")],
            sp.FACEBOOK_PAGE_ACTOR: [{"title": "Muster", "phone": "0251 7654321"}],
            sp.FACEBOOK_POSTS_ACTOR: [],
        }
    )

    snapshot = await social.collect_social(
        _bare_site(), TOKEN, city="Münster", phones=["+49 251 7654321"]
    )

    accounts = _by_platform(snapshot)
    assert accounts["instagram"].proof == "the profile carries the name and names Münster"
    assert accounts["facebook"].proof == "the phone number matches"


@respx.mock
async def test_a_google_hit_without_proof_is_unsure_with_a_reason_and_is_not_measured() -> None:
    _mock_legal_notice()
    _mock_search({"facebook.com": ["https://www.facebook.com/someone-else"]})
    routes = _mock_apify(
        {
            sp.FACEBOOK_PAGE_ACTOR: [
                {
                    "title": "Someone",
                    "followers": 99,
                    "website": "https://other.de",
                    "phone": "+49 30 1234567",
                    "address": "Berlin",
                }
            ],
            sp.FACEBOOK_POSTS_ACTOR: [{"time": "2026-11-12T20:16:47.000Z"}],
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    account = _by_platform(snapshot)["facebook"]
    assert account.status == "unsure"
    assert account.reason is not None and "dachdeckerei-muster.de" in account.reason
    assert account.followers is None
    assert account.posts == []
    assert account.proof is None
    assert routes[sp.FACEBOOK_POSTS_ACTOR].call_count == 0
    assert "facebook" not in [gap.platform for gap in snapshot.gaps]


@respx.mock
async def test_the_first_confirmed_candidate_wins_and_the_others_are_left_out() -> None:
    _mock_legal_notice()
    _mock_search(
        {
            "instagram.com": [
                "https://www.instagram.com/first/",
                "https://www.instagram.com/second/",
                "https://www.instagram.com/third/",
            ]
        }
    )
    routes = _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: lambda body: [
                _instagram(
                    username=body["usernames"][0],
                    externalUrl="https://dachdeckerei-muster.de"
                    if body["usernames"][0] == "second"
                    else None,
                )
            ]
        }
    )

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert [account.handle for account in snapshot.accounts] == ["second"]
    assert routes[sp.INSTAGRAM_ACTOR].call_count == 2


@respx.mock
async def test_at_most_two_candidates_per_platform_are_read_over_all_names() -> None:
    _mock_legal_notice()
    rounds = _mock_search(
        {
            'site:instagram.com "Muster Bedachungen"': ["https://www.instagram.com/first/"],
            'site:instagram.com "Max Muster Dachdeckerei"': [
                "https://www.instagram.com/first/",
                "https://www.instagram.com/second/",
                "https://www.instagram.com/third/",
            ],
        }
    )
    routes = _mock_apify(
        {sp.INSTAGRAM_ACTOR: lambda body: [_instagram(username=body["usernames"][0])]}
    )

    snapshot = await social.collect_social(_site([]), TOKEN, ("Muster Bedachungen",))

    assert [(account.handle, account.status) for account in snapshot.accounts] == [
        ("first", "unsure"),
        ("second", "unsure"),
    ]
    assert routes[sp.INSTAGRAM_ACTOR].call_count == 2
    assert not any("instagram.com" in phrase for phrase in rounds[2])


@respx.mock
async def test_a_hit_whose_profile_is_not_available_is_unreachable() -> None:
    _mock_legal_notice()
    _mock_search({"facebook.com": ["https://www.facebook.com/gone"]})
    _mock_apify({sp.FACEBOOK_PAGE_ACTOR: [{"error": "not_available"}]})

    snapshot = await social.collect_social(_site([]), TOKEN)

    account = _by_platform(snapshot)["facebook"]
    assert (account.status, account.reason) == ("unreachable", "profile not available")


@respx.mock
async def test_a_failed_apify_run_makes_that_account_unreachable_and_the_rest_continues() -> None:
    _mock_search()
    _mock_legal_notice()
    _mock_apify(
        {
            sp.INSTAGRAM_ACTOR: httpx.Response(
                403, json={"error": {"message": "Monthly usage hard limit exceeded"}}
            ),
            sp.LINKEDIN_PERSON_ACTOR: [{"publicIdentifier": "max", "followerCount": 3}],
            sp.LINKEDIN_PERSON_POSTS_ACTOR: [],
        }
    )
    site = _site([OWN_INSTAGRAM, "https://www.linkedin.com/in/max"])

    snapshot = await social.collect_social(site, TOKEN)

    accounts = _by_platform(snapshot)
    assert accounts["instagram"].status == "unreachable"
    assert accounts["instagram"].reason is not None
    assert "HTTP 403: Monthly usage hard limit exceeded" in accounts["instagram"].reason
    assert accounts["linkedin_person"].status == "confirmed"


@respx.mock
async def test_posts_that_cannot_be_read_leave_the_measured_account_with_a_reason() -> None:
    _mock_search()
    _mock_legal_notice()
    _mock_apify(
        {
            sp.LINKEDIN_PERSON_ACTOR: [{"publicIdentifier": "max", "followerCount": 303}],
            sp.LINKEDIN_PERSON_POSTS_ACTOR: httpx.Response(500),
        }
    )

    snapshot = await social.collect_social(_site(["https://www.linkedin.com/in/max"]), TOKEN)

    account = _by_platform(snapshot)["linkedin_person"]
    assert account.status == "confirmed"
    assert account.followers == 303
    assert account.posts == []
    assert account.reason is not None and account.reason.startswith("posts not read:")


@respx.mock
async def test_a_failed_search_run_leaves_the_searched_platforms_as_failed_gaps() -> None:
    _mock_legal_notice()
    search = apify_post(_actor_url(GOOGLE_SEARCH_ACTOR)).mock(
        return_value=httpx.Response(
            403, json={"error": {"message": "Monthly usage hard limit exceeded"}}
        )
    )
    _mock_apify({sp.INSTAGRAM_ACTOR: [_instagram()]})

    snapshot = await social.collect_social(_site([OWN_INSTAGRAM]), TOKEN)

    assert [account.platform for account in snapshot.accounts] == ["instagram"]
    assert _gaps(snapshot) == [(platform, "search_failed") for platform in SEARCHED_PLATFORMS[1:]]
    assert snapshot.gaps[0].detail is not None
    assert "HTTP 403: Monthly usage hard limit exceeded" in snapshot.gaps[0].detail
    assert search.call_count == 1


@respx.mock
async def test_a_phrase_without_result_page_is_a_failed_gap_and_the_others_are_searched() -> None:
    _mock_legal_notice()

    def respond(request: httpx.Request) -> httpx.Response:
        phrases = json.loads(request.content)["queries"].split("\n")
        pages = [_result_page(phrase, []) for phrase in phrases if "x.com" not in phrase]
        return httpx.Response(201, json=pages)

    apify_post(_actor_url(GOOGLE_SEARCH_ACTOR)).mock(side_effect=respond)

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert _gaps(snapshot) == [
        ("instagram", "no_account_found"),
        ("facebook", "no_account_found"),
        ("linkedin_company", "no_account_found"),
        ("linkedin_person", "no_account_found"),
        ("x", "search_failed"),
    ]
    assert snapshot.gaps[4].detail == "Google answered without a result page"


async def test_measure_website_needs_the_token_before_it_crawls(
    collector_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crawl(url: HttpUrl, max_pages: int) -> WebsiteSnapshot:
        raise AssertionError("crawled")

    monkeypatch.setattr(social, "crawl_website", crawl)

    with pytest.raises(ConfigurationError, match="APIFY_TOKEN"):
        await social.measure_website(collector_settings, HttpUrl(SITE), [], 5)


async def test_measure_website_crawls_the_site_and_collects_with_the_token(
    collector_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _site([])
    crawled: list[tuple[str, int]] = []
    collected: list[tuple[WebsiteSnapshot, str, tuple[str, ...]]] = []
    expected = SocialSnapshot(accounts=[], gaps=[], collected_at=datetime.now(UTC))

    async def crawl(url: HttpUrl, max_pages: int) -> WebsiteSnapshot:
        crawled.append((str(url), max_pages))
        return site

    async def collect(found: WebsiteSnapshot, token: SecretStr, names: Any) -> SocialSnapshot:
        collected.append((found, token.get_secret_value(), tuple(names)))
        return expected

    monkeypatch.setattr(social, "crawl_website", crawl)
    monkeypatch.setattr(social, "collect_social", collect)
    settings = collector_settings.model_copy(update={"apify_token": TOKEN})

    result = await social.measure_website(settings, HttpUrl(SITE), ["Katja"], 7)

    assert result is expected
    assert crawled == [(f"{SITE}/", 7)]
    assert collected == [(site, "apify-secret-token", ("Katja",))]


@respx.mock
async def test_a_platform_searched_with_a_later_name_counts_as_searched() -> None:
    _mock_legal_notice()
    rounds: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        phrases = json.loads(request.content)["queries"].split("\n")
        rounds.append(phrases)
        answered = [phrase for phrase in phrases if len(rounds) > 1 or "x.com" not in phrase]
        return httpx.Response(201, json=[_result_page(phrase, []) for phrase in answered])

    apify_post(_actor_url(GOOGLE_SEARCH_ACTOR)).mock(side_effect=respond)

    snapshot = await social.collect_social(_site([]), TOKEN)

    assert len(rounds) == 2
    assert _gaps(snapshot) == [(platform, "no_account_found") for platform in SEARCHED_PLATFORMS]
