import httpx
import pytest
import respx

from radarauditor.collectors import geocode
from radarauditor.errors import CollectionError


@respx.mock
async def test_the_first_hit_gives_the_coordinates() -> None:
    route = respx.get(geocode.NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "51.96", "lon": "7.63"}])
    )

    assert await geocode.geocode("Hammer Str. 1", "48153", "Münster") == (51.96, 7.63)
    assert route.calls.last.request.url.params["q"] == "Hammer Str. 1, 48153 Münster"
    assert route.calls.last.request.headers["User-Agent"] == geocode.USER_AGENT


@respx.mock
async def test_a_city_alone_is_enough_for_the_query() -> None:
    route = respx.get(geocode.NOMINATIM_URL).mock(
        return_value=httpx.Response(200, json=[{"lat": "51.96", "lon": "7.63"}])
    )

    await geocode.geocode("", "", "Münster")

    assert route.calls.last.request.url.params["q"] == "Münster"


@respx.mock
async def test_no_hit_gives_none() -> None:
    respx.get(geocode.NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))

    assert await geocode.geocode("", "", "Nirgendwo") is None


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[{"lat": "x", "lon": "7"}]),
        httpx.Response(200, json=[{"lon": "7"}]),
    ],
)
async def test_a_failed_or_unreadable_answer_is_a_collection_error(
    response: httpx.Response,
) -> None:
    respx.get(geocode.NOMINATIM_URL).mock(return_value=response)

    with pytest.raises(CollectionError, match="address lookup"):
        await geocode.geocode("", "", "Münster")
