import httpx
import pytest
import respx

from radarauditor.collectors import crawl_extras
from radarauditor.collectors.snapshot import SitemapEntry

SITE = "https://www.example.de"


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


@respx.mock
async def test_fetch_as_agents_sends_each_identity_and_reads_its_answer(
    client: httpx.AsyncClient,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if "Googlebot" in request.headers["user-agent"]:
            return httpx.Response(403, text="forbidden")
        return httpx.Response(200, html="<html><body><main><p>Hallo Welt</p></main></body></html>")

    respx.get(f"{SITE}/").mock(side_effect=answer)

    async with client:
        fetches = await crawl_extras.fetch_as_agents(client, f"{SITE}/", 0)

    by_agent = {fetch.agent: fetch for fetch in fetches}
    assert list(by_agent) == list(crawl_extras.AGENT_USER_AGENTS)
    assert by_agent[crawl_extras.GOOGLEBOT].status == 403
    assert by_agent[crawl_extras.GOOGLEBOT].text == ""
    assert by_agent[crawl_extras.VISITOR].status == 200
    assert "Hallo Welt" in by_agent[crawl_extras.VISITOR].text


@respx.mock
async def test_fetch_as_agents_records_a_failed_connection(client: httpx.AsyncClient) -> None:
    respx.get(f"{SITE}/").mock(side_effect=httpx.ConnectTimeout("slow"))

    async with client:
        fetches = await crawl_extras.fetch_as_agents(client, f"{SITE}/", 0)

    assert {fetch.status for fetch in fetches} == {None}
    assert {fetch.error for fetch in fetches} == {"ConnectTimeout"}


@respx.mock
async def test_probe_url_follows_a_chain_and_keeps_the_first_status(
    client: httpx.AsyncClient,
) -> None:
    respx.get("http://example.de/").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.de/"})
    )
    respx.get("https://example.de/").mock(
        return_value=httpx.Response(301, headers={"location": f"{SITE}/"})
    )
    respx.get(f"{SITE}/").mock(return_value=httpx.Response(200))

    async with client:
        probe = await crawl_extras.probe_url(client, "http://example.de/", 0)

    assert probe.status == 301
    assert probe.chain == [301, 301, 200]
    assert probe.location == "https://example.de/"
    assert probe.final_url == f"{SITE}/"
    assert probe.error is None


@respx.mock
async def test_probe_url_stops_after_the_maximum_number_of_hops(
    client: httpx.AsyncClient,
) -> None:
    route = respx.get(f"{SITE}/loop").mock(
        return_value=httpx.Response(302, headers={"location": f"{SITE}/loop"})
    )

    async with client:
        probe = await crawl_extras.probe_url(client, f"{SITE}/loop", 0)

    assert route.call_count == crawl_extras.MAX_REDIRECT_HOPS
    assert probe.chain == [302] * crawl_extras.MAX_REDIRECT_HOPS
    assert probe.error == "too many redirects"


@respx.mock
async def test_probe_url_ends_on_the_last_allowed_hop_without_error(
    client: httpx.AsyncClient,
) -> None:
    for step in range(crawl_extras.MAX_REDIRECT_HOPS - 1):
        respx.get(f"{SITE}/{step}").mock(
            return_value=httpx.Response(302, headers={"location": f"{SITE}/{step + 1}"})
        )
    respx.get(f"{SITE}/{crawl_extras.MAX_REDIRECT_HOPS - 1}").mock(return_value=httpx.Response(200))

    async with client:
        probe = await crawl_extras.probe_url(client, f"{SITE}/0", 0)

    assert probe.error is None
    assert probe.chain[-1] == 200


@respx.mock
async def test_probe_url_records_a_failed_connection(client: httpx.AsyncClient) -> None:
    respx.get(f"{SITE}/x").mock(side_effect=httpx.ConnectError("down"))

    async with client:
        probe = await crawl_extras.probe_url(client, f"{SITE}/x", 0)

    assert probe.status is None
    assert probe.error == "ConnectError"


def test_variant_urls_without_a_subpage_lists_the_four_start_variants() -> None:
    assert crawl_extras.variant_urls(f"{SITE}/", []) == [
        "http://example.de/",
        "http://www.example.de/",
        "https://example.de/",
        "https://www.example.de/",
    ]


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, 1),
        (crawl_extras.SITEMAP_PROBE_SAMPLE, crawl_extras.SITEMAP_PROBE_SAMPLE),
        (crawl_extras.SITEMAP_PROBE_SAMPLE + 1, crawl_extras.SITEMAP_PROBE_SAMPLE),
        (500, crawl_extras.SITEMAP_PROBE_SAMPLE),
    ],
)
def test_sitemap_sample_is_capped(count: int, expected: int) -> None:
    entries = [SitemapEntry(url=f"{SITE}/{index}/") for index in range(count)]

    assert len(crawl_extras.sitemap_sample(entries)) == expected


def test_sitemap_sample_of_nothing_is_empty() -> None:
    assert crawl_extras.sitemap_sample([]) == []
