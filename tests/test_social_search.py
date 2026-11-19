import json
from typing import Any

import httpx
import pytest
import respx
from apify_fake import apify_post
from pydantic import SecretStr

from radarauditor.collectors import social_search
from radarauditor.errors import CollectionError

URL = (
    f"https://api.apify.com/v2/acts/{social_search.GOOGLE_SEARCH_ACTOR}/run-sync-get-dataset-items"
)
TOKEN = SecretStr("apify-secret-token")
FACEBOOK = 'site:facebook.com "Katja Karsch"'
X = 'site:x.com "Katja Karsch"'


def _page(phrase: str, urls: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "#error": False,
        "searchQuery": {"term": phrase, "page": 1},
        "organicResults": [{"url": url, "title": "hit"} for url in urls],
        **overrides,
    }


@respx.mock
async def test_search_links_sends_all_phrases_in_one_run_and_returns_the_links_per_phrase() -> None:
    route = apify_post(URL).mock(
        return_value=httpx.Response(
            201,
            json=[
                _page(X, []),
                _page(FACEBOOK, ["https://www.facebook.com/a", "https://www.facebook.com/b"]),
            ],
        )
    )

    async with httpx.AsyncClient() as client:
        links = await social_search.search_links(client, TOKEN, [FACEBOOK, X])

    assert links == {
        FACEBOOK: ["https://www.facebook.com/a", "https://www.facebook.com/b"],
        X: [],
    }
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.url.params["timeout"] == "120"
    assert json.loads(request.content) == {
        "queries": f"{FACEBOOK}\n{X}",
        "maxPagesPerQuery": 1,
        "countryCode": "de",
        "languageCode": "de",
    }


@respx.mock
async def test_search_links_passes_a_failed_run_on() -> None:
    apify_post(URL).mock(
        return_value=httpx.Response(402, json={"error": {"message": "limit reached"}})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(CollectionError, match="HTTP 402: limit reached"):
            await social_search.search_links(client, TOKEN, [FACEBOOK])


@pytest.mark.parametrize(
    "page",
    [
        _page(FACEBOOK, ["https://www.facebook.com/a"], **{"#error": True}),
        {"#error": False, "organicResults": []},
        {"#error": False, "searchQuery": "text", "organicResults": []},
        {"#error": False, "searchQuery": {"term": 5}, "organicResults": []},
        {"#error": False, "searchQuery": {"term": FACEBOOK}},
        {"#error": False, "searchQuery": {"term": FACEBOOK}, "organicResults": "none"},
    ],
)
def test_a_page_with_an_error_or_without_phrase_or_results_is_left_out(
    page: dict[str, Any],
) -> None:
    assert social_search.links_by_phrase([page]) == {}


def test_results_without_an_address_are_left_out() -> None:
    page = _page(FACEBOOK, ["https://www.facebook.com/a"])
    page["organicResults"] += [{"title": "no address"}, {"url": None}, "text"]

    assert social_search.links_by_phrase([page]) == {FACEBOOK: ["https://www.facebook.com/a"]}
