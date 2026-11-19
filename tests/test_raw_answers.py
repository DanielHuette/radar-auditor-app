import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import apify, raw_answers
from radarauditor.errors import StorageError

ACTOR = "apify~google-search-scraper"
START_URL = f"https://api.apify.com/v2/acts/{ACTOR}/runs"
RUN_URL = "https://api.apify.com/v2/actor-runs/run-1"
ITEMS_URL = "https://api.apify.com/v2/datasets/data-1/items"
RUN = {"id": "run-1", "status": "SUCCEEDED", "defaultDatasetId": "data-1", "usageTotalUsd": 0.001}


@pytest.fixture(autouse=True)
def unbound() -> None:
    raw_answers.bind(None)


def test_the_file_name_carries_step_supplier_and_what() -> None:
    assert raw_answers.file_name("pagespeed", "google_pagespeed", "mobile") == (
        "pagespeed-google_pagespeed-mobile.json"
    )
    assert raw_answers.file_name("assistants", "chatgpt", "api", "txt") == (
        "assistants-chatgpt-api.txt"
    )
    assert raw_answers.file_name("search_google", "apify", "apify/google search") == (
        "search_google-apify-apify_google_search.json"
    )


def test_a_kept_answer_goes_unchanged_to_the_bound_sink() -> None:
    stored: dict[str, bytes] = {}
    raw_answers.bind(stored.__setitem__)
    raw_answers.keep("a.json", b'{"x": 1}')
    raw_answers.keep("b.txt", "Antwort ä")
    assert stored == {"a.json": b'{"x": 1}', "b.txt": "Antwort ä".encode()}


def test_nothing_is_kept_without_a_sink() -> None:
    raw_answers.keep("a.json", b"{}")


def test_a_storage_error_does_not_stop_the_measurement(caplog: pytest.LogCaptureFixture) -> None:
    def failing(name: str, body: bytes) -> None:
        raise StorageError("bucket down")

    raw_answers.bind(failing)
    raw_answers.keep("a.json", b"{}")
    assert "a.json" in caplog.text


@respx.mock
async def test_the_unchanged_answer_of_an_apify_run_is_kept_under_its_step() -> None:
    stored: dict[str, bytes] = {}
    raw_answers.bind(stored.__setitem__)
    items = [{"organicResults": [{"url": "https://a.de"}]}]
    respx.post(START_URL).mock(return_value=httpx.Response(201, json={"data": RUN}))
    respx.get(RUN_URL).mock(return_value=httpx.Response(200, json={"data": RUN}))
    respx.get(ITEMS_URL).mock(return_value=httpx.Response(200, json=items))

    async with httpx.AsyncClient() as client:
        result = await apify.run_actor(
            client, SecretStr("t"), ACTOR, {"queries": "x"}, step="search_google"
        )

    assert result == items
    assert list(stored) == ["search_google-apify-apify~google-search-scraper.json"]
    assert b"organicResults" in stored["search_google-apify-apify~google-search-scraper.json"]
