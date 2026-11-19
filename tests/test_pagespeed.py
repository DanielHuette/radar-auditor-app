from collections.abc import Callable, Iterator

import httpx
import pytest
import respx
from pydantic import HttpUrl, SecretStr

from radarauditor.collectors import pagespeed
from radarauditor.collectors.snapshot import PageSpeedField, PageSpeedLab
from radarauditor.errors import CollectionError, TransientCollectionError

URL = HttpUrl("https://www.example.de/")
KEY = SecretStr("google-key")
MOBILE_LAB = [
    (0.42, 3000.0, 0.1, 100.0),
    (0.50, 2000.0, 0.05, 300.0),
    (0.30, 5000.0, 0.3, 250.0),
    (0.60, 2500.0, 0.02, 50.0),
    (0.45, 2600.0, 0.12, 400.0),
]
VISITORS = {
    "metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2900},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 7},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 210},
    }
}

pytestmark = pytest.mark.usefixtures("fast_retries")

Responder = Callable[[httpx.Request], httpx.Response]


def _lighthouse(
    performance: float, lcp_ms: float = 3120.4, cls: float = 0.1234, tbt_ms: float = 180.2
) -> dict[str, object]:
    return {
        "categories": {
            "performance": {"score": performance},
            "accessibility": {"score": 0.87},
        },
        "audits": {
            "largest-contentful-paint": {"numericValue": lcp_ms},
            "cumulative-layout-shift": {"numericValue": cls},
            "total-blocking-time": {"numericValue": tbt_ms},
        },
    }


def _responder(mobile: Iterator[httpx.Response]) -> Responder:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["strategy"] == "desktop":
            return httpx.Response(200, json={"lighthouseResult": _lighthouse(0.91)})
        return next(mobile)

    return respond


def _mobile(**experiences: object) -> Iterator[httpx.Response]:
    for performance, lcp_ms, cls, tbt_ms in MOBILE_LAB:
        body = {"lighthouseResult": _lighthouse(performance, lcp_ms, cls, tbt_ms), **experiences}
        yield httpx.Response(200, json=body)


@respx.mock
async def test_measure_takes_the_median_of_five_mobile_runs() -> None:
    route = respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(_mobile(loadingExperience=VISITORS))
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.lab == PageSpeedLab(runs=5, lcp_ms=2600, cls=0.1, tbt_ms=250)
    assert snapshot.mobile_performance == 45
    assert snapshot.desktop_performance == 91
    assert snapshot.accessibility == 87
    mobile_calls = [call for call in route.calls if call.request.url.params["strategy"] == "mobile"]
    assert len(mobile_calls) == pagespeed.MOBILE_RUNS
    request = route.calls[0].request
    assert request.url.params.get_list("category") == ["performance", "accessibility"]
    assert request.headers["X-Goog-Api-Key"] == "google-key"
    assert "key=" not in str(request.url)


@respx.mock
async def test_measure_keeps_visitor_data_of_the_page_apart_from_the_lab() -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(_mobile(loadingExperience=VISITORS))
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.field_data == PageSpeedField(scope="url", lcp_ms=2900, cls=0.07, inp_ms=210)


@respx.mock
async def test_measure_takes_domain_visitor_data_when_the_page_has_none() -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(
            _mobile(loadingExperience={"metrics": {}}, originLoadingExperience=VISITORS)
        )
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.field_data == PageSpeedField(scope="origin", lcp_ms=2900, cls=0.07, inp_ms=210)


@respx.mock
async def test_measure_marks_page_data_that_google_filled_from_the_domain() -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(_mobile(loadingExperience={**VISITORS, "origin_fallback": True}))
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.field_data is not None
    assert snapshot.field_data.scope == "origin"


@respx.mock
async def test_measure_keeps_partial_visitor_data() -> None:
    lcp_only = {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1800}}}
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(_mobile(loadingExperience=lcp_only))
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.field_data == PageSpeedField(scope="url", lcp_ms=1800, cls=None, inp_ms=None)


@respx.mock
async def test_measure_without_visitor_data_keeps_the_lab_values() -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        side_effect=_responder(_mobile(loadingExperience={}))
    )

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.field_data is None
    assert snapshot.lab is not None
    assert snapshot.lab.lcp_ms == 2600


@respx.mock
async def test_measure_counts_only_the_runs_that_succeeded() -> None:
    no_tbt = _lighthouse(0.2)
    audits = no_tbt["audits"]
    assert isinstance(audits, dict)
    del audits["total-blocking-time"]
    good = list(_mobile())
    mobile = iter(
        [
            httpx.Response(200, json={"lighthouseResult": no_tbt}),
            httpx.Response(200, text="<html>"),
            good[0],
            good[1],
            good[2],
        ]
    )
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(side_effect=_responder(mobile))

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.lab == PageSpeedLab(runs=3, lcp_ms=3000, cls=0.1, tbt_ms=250)
    assert snapshot.mobile_performance == 42


@respx.mock
async def test_measure_retries_rate_limits_then_raises_transient() -> None:
    route = respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(return_value=httpx.Response(429))

    with pytest.raises(TransientCollectionError):
        await pagespeed.measure(URL, KEY)

    assert route.call_count == 3 * (pagespeed.MOBILE_RUNS + 1)


@respx.mock
async def test_measure_recovers_after_server_error() -> None:
    desktop = iter([httpx.Response(503), httpx.Response(503)])
    mobile = _mobile()

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.params["strategy"] == "mobile":
            return next(mobile)
        return next(desktop, None) or httpx.Response(
            200, json={"lighthouseResult": _lighthouse(0.91)}
        )

    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(side_effect=flaky)

    snapshot = await pagespeed.measure(URL, KEY)

    assert snapshot.desktop_performance == 91


@pytest.mark.parametrize("status", [400, 403])
@respx.mock
async def test_measure_raises_collection_error_on_rejected_request(status: int) -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(
        return_value=httpx.Response(status, json={"error": {"message": "API key not valid"}})
    )

    with pytest.raises(CollectionError, match="API key not valid") as raised:
        await pagespeed.measure(URL, KEY)

    assert not isinstance(raised.value, TransientCollectionError)


@respx.mock
async def test_measure_raises_when_no_mobile_run_has_a_score() -> None:
    body = {"lighthouseResult": {"categories": {"performance": {"score": None}}}}
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(CollectionError, match="performance"):
        await pagespeed.measure(URL, KEY)


@respx.mock
async def test_measure_raises_when_every_mobile_run_lacks_lab_values() -> None:
    body = {"lighthouseResult": {"categories": _lighthouse(0.5)["categories"]}}
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(CollectionError, match="largest-contentful-paint"):
        await pagespeed.measure(URL, KEY)


@respx.mock
async def test_measure_raises_when_desktop_fails_after_good_mobile_runs() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["strategy"] == "desktop":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"lighthouseResult": _lighthouse(0.5)})

    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(side_effect=respond)

    with pytest.raises(CollectionError, match=r"\(desktop\) answer is malformed"):
        await pagespeed.measure(URL, KEY)


@respx.mock
async def test_measure_raises_on_malformed_answer() -> None:
    respx.get(pagespeed.PAGESPEED_ENDPOINT).mock(return_value=httpx.Response(200, text="<html>"))

    with pytest.raises(CollectionError, match="malformed"):
        await pagespeed.measure(URL, KEY)
