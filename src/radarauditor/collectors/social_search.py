from collections.abc import Sequence
from functools import partial
from typing import Any

import httpx
from pydantic import SecretStr

from radarauditor.collectors.apify import ServiceCostSink, run_actor

GOOGLE_SEARCH_ACTOR = "apify~google-search-scraper"
SEARCH_TIMEOUT_SECONDS = 120
COST_SERVICE = "google_search"
SEARCH_COUNTRY = "de"
SEARCH_LANGUAGE = "de"


async def search_links(
    client: httpx.AsyncClient,
    token: SecretStr,
    phrases: Sequence[str],
    book: ServiceCostSink | None = None,
) -> dict[str, list[str]]:
    payload = {
        "queries": "\n".join(phrases),
        "maxPagesPerQuery": 1,
        "countryCode": SEARCH_COUNTRY,
        "languageCode": SEARCH_LANGUAGE,
    }
    items = await run_actor(
        client,
        token,
        GOOGLE_SEARCH_ACTOR,
        payload,
        SEARCH_TIMEOUT_SECONDS,
        None if book is None else partial(book, COST_SERVICE),
    )
    return links_by_phrase(items)


def links_by_phrase(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    for item in items:
        query = item.get("searchQuery")
        results = item.get("organicResults")
        if item.get("#error") or not isinstance(query, dict) or not isinstance(results, list):
            continue
        phrase = query.get("term")
        if isinstance(phrase, str):
            links[phrase] = [
                result["url"]
                for result in results
                if isinstance(result, dict) and isinstance(result.get("url"), str)
            ]
    return links
