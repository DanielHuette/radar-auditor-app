from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from radarauditor.collectors import listings
from radarauditor.collectors.listings import Portal, Target
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import DirectoryHit
from radarauditor.config import Settings
from radarauditor.errors import CollectionError, ConfigurationError

CATALOGUE_PATH = Path(__file__).parent.parent / "config" / "verzeichnisse.yaml"

KATJA = Target(
    name="Katja Karsch",
    city="Münster",
    street="Hauptstraße 5",
    postal_code="48143",
    phone="0251 123456",
    website="https://www.katjakarsch.de/",
    industry="kunst_kreativ",
)


def _hit(found: bool) -> DirectoryHit:
    return DirectoryHit(
        directory="x", found=found, listing_url=None, name=None, address=None, phone=None
    )


def test_the_catalogue_loads_and_names_only_portals_it_describes() -> None:
    catalogue = listings.load_portals(CATALOGUE_PATH)
    trade, reviews = listings.portals_for(catalogue, KATJA)
    assert trade
    assert reviews
    assert set(trade + reviews) <= set(catalogue.portale)


def test_a_catalogue_with_an_unknown_portal_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "verzeichnisse.yaml"
    path.write_text(
        "portale: {}\nindustry_specific: {x: [Nirgends]}\nbewertungsportale: {alle: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown portals"):
        listings.load_portals(path)


def test_portals_of_another_city_are_left_out() -> None:
    catalogue = listings.load_portals(CATALOGUE_PATH)
    in_city = Target(name="x", city="Münster", industry="kunst_kreativ")
    elsewhere = Target(name="x", city="Hamburg", industry="kunst_kreativ")
    local = {name for name, portal in catalogue.portale.items() if portal.cities}
    assert local
    assert local & set(listings.portals_for(catalogue, in_city)[0])
    assert not local & set(listings.portals_for(catalogue, elsewhere)[0])


def test_the_query_count_adds_a_second_search_by_domain_only_with_a_website() -> None:
    catalogue = listings.load_portals(CATALOGUE_PATH)
    with_site = Target(name="x", city="Münster", website="https://x.de", industry="kunst_kreativ")
    without_site = Target(name="x", city="Münster", industry="kunst_kreativ")
    trade, reviews = listings.portals_for(catalogue, with_site)
    names = list(dict.fromkeys([*trade, *reviews]))
    assert listings.search_query_count(catalogue, without_site) == len(names)
    assert listings.search_query_count(catalogue, with_site) > len(names)


def test_a_name_alone_is_not_the_business() -> None:
    assert not listings.business_matches(
        KATJA, name="Katja Karsch", website=None, phone=None, address="Hamburg"
    )


def test_name_with_postal_code_is_the_business() -> None:
    assert listings.business_matches(
        KATJA, name="Katja Karsch", website=None, phone=None, address="48143 Münster"
    )
    assert not listings.business_matches(
        KATJA, name="Katja Karsch", website=None, phone=None, address="20095 Münster"
    )


def test_another_street_with_the_same_name_is_another_business() -> None:
    assert not listings.business_matches(
        KATJA, name="Katja Karsch", website=None, phone=None, address="Weg 1, 48143 Münster"
    )
    assert listings.business_matches(
        KATJA,
        name="Katja Karsch",
        website=None,
        phone=None,
        address="Hauptstr. 5, 48143 Münster",
    )


def test_the_same_name_with_another_phone_or_domain_is_another_business() -> None:
    assert not listings.business_matches(
        KATJA, name="Katja Karsch", website=None, phone="040 999999", address="48143 Münster"
    )
    assert not listings.business_matches(
        KATJA,
        name="Katja Karsch",
        website="https://katja-karsch.com",
        phone=None,
        address="48143 Münster",
    )


def test_the_city_decides_only_when_no_postal_code_is_known() -> None:
    without_code = Target(name="Katja Karsch", city="Münster")
    assert listings.business_matches(
        without_code, name="Katja Karsch", website=None, phone=None, address="Weg 1, Münster"
    )
    assert not listings.business_matches(
        without_code, name="Katja Karsch", website=None, phone=None, address="Weg 1, Hamburg"
    )


@pytest.mark.parametrize("found", ["Katja", "Karsch", "Atelier", "Katja K"])
def test_a_name_that_is_only_a_part_of_ours_is_not_the_business(found: str) -> None:
    assert not listings.business_matches(
        KATJA, name=found, website=None, phone=None, address="Weg 1, 48143 Münster"
    )


def test_our_whole_name_with_further_words_is_the_business() -> None:
    assert listings.business_matches(
        KATJA,
        name="Atelier Katja Karsch Keramik",
        website=None,
        phone=None,
        address="48143 Münster",
    )


def test_a_hit_keeps_what_was_searched_and_how_many_entries_came_back() -> None:
    hit = listings.apple_hit([{"name": "Anders", "address": {"full": "Hamburg"}}], KATJA)
    assert (hit.found, hit.delivered) == (False, 1)
    assert hit.search_text is not None and "Katja Karsch" in hit.search_text
    empty = listings.bing_hit([], KATJA)
    assert (empty.found, empty.delivered) == (False, 0)
    assert empty.search_text is not None and "Münster" in empty.search_text


def test_every_delivered_apple_entry_is_kept_with_address_and_phone() -> None:
    items = [
        {
            "name": "Anders",
            "address": {"full": "Hamburg"},
            "phone": "040 123",
            "website": "https://a.de",
        },
        {"name": "  "},
        {"address": {"full": "ohne Namen"}},
        {"name": "Noch anders"},
    ]
    hit = listings.apple_hit(items, KATJA)
    assert hit.delivered == 4
    assert hit.entries is not None
    assert [entry.name for entry in hit.entries] == ["Anders", None, None, "Noch anders"]
    assert hit.entries[0].address == "Hamburg"
    assert hit.entries[0].phone == "040 123"
    assert hit.entries[2].address == "ohne Namen"


def test_every_delivered_bing_entry_is_kept_with_address_and_phone() -> None:
    items = [
        {"name": "Anders", "address": "Weg 1 · Hamburg", "phone_number": "040 123"},
        {"address": "ohne Namen"},
        {"name": "Noch anders"},
    ]
    hit = listings.bing_hit(items, KATJA)
    assert hit.delivered == 3
    assert hit.entries is not None
    assert [entry.name for entry in hit.entries] == ["Anders", None, "Noch anders"]
    assert hit.entries[0].address == "Weg 1, Hamburg"
    assert hit.entries[0].phone == "040 123"


def test_website_or_phone_alone_is_the_business() -> None:
    assert listings.business_matches(
        KATJA, name="Anders", website="https://katjakarsch.de/kontakt", phone=None, address=None
    )
    assert listings.business_matches(
        KATJA, name="Anders", website=None, phone="0251/123456", address=None
    )
    assert not listings.business_matches(
        KATJA, name="Anders", website="https://anders.de", phone="0251 999", address="Münster"
    )


def test_apple_hit_reads_the_matching_place() -> None:
    items: list[dict[str, Any]] = [
        {"name": "Katja Karsch", "address": {"full": "Hamburg"}},
        {
            "name": "Katja Karsch",
            "website": "https://www.katjakarsch.de",
            "phone": "0251 123456",
            "appleUrl": "https://maps.apple.com/place?auid=1",
            "address": {"full": "Hauptstraße 5, 48143 Münster"},
        },
    ]
    hit = listings.apple_hit(items, KATJA)
    assert hit.found
    assert hit.directory == listings.APPLE_NAME
    assert str(hit.listing_url) == "https://maps.apple.com/place?auid=1"
    assert not listings.apple_hit(items[:1], KATJA).found
    assert not listings.apple_hit([], KATJA).found


def test_bing_hit_reads_the_matching_place() -> None:
    items: list[dict[str, Any]] = [
        {
            "name": "Katja Karsch",
            "website": "https://www.katjakarsch.de/",
            "phone_number": "0251 123456",
            "address": "Hauptstraße 5 · 48143 Münster",
            "listing_url": "https://www.bing.com/maps?cp=1",
        }
    ]
    hit = listings.bing_hit(items, KATJA)
    assert hit.found
    assert hit.address == "Hauptstraße 5, 48143 Münster"
    assert not listings.bing_hit([{"name": "Katja Karsch", "address": "Berlin"}], KATJA).found


def test_search_results_are_grouped_by_term_and_errors_are_dropped() -> None:
    items = [
        {"searchQuery": {"term": "a  b"}, "organicResults": [{"url": "u1"}]},
        {"searchQuery": {"term": "a b"}, "organicResults": [{"url": "u2"}]},
        {"#error": True, "searchQuery": {"term": "c"}, "organicResults": [{"url": "u3"}]},
        {"searchQuery": {"term": "d"}},
    ]
    assert listings.search_results(items) == {"a b": [{"url": "u1"}, {"url": "u2"}]}


def test_portal_hit_needs_the_portal_and_the_business() -> None:
    portal = Portal(site="provenexpert.com")
    results = [
        {"url": "https://www.provenexpert.com/search/?q=x", "title": "Katja Karsch"},
        {"url": "https://www.provenexpert.com/katja-karsch/", "title": "Katja Karsch Bewertungen"},
    ]
    hit = listings.portal_hit("ProvenExpert", portal, results, KATJA)
    assert hit.found
    assert str(hit.listing_url) == "https://www.provenexpert.com/katja-karsch/"
    other = [{"url": "https://www.provenexpert.com/schmidt-bau/", "title": "Schmidt Bau"}]
    assert not listings.portal_hit("ProvenExpert", portal, other, KATJA).found
    wrong_site = [{"url": "https://www.example.com/katja-karsch/", "title": "Katja Karsch"}]
    assert not listings.portal_hit("ProvenExpert", portal, wrong_site, KATJA).found


def test_wikidata_hit_by_website_or_by_name_and_place() -> None:
    claim = {"mainsnak": {"datavalue": {"value": "https://katjakarsch.de/"}}}
    by_website = {"Q1": {"labels": {"de": {"value": "Irgendwer"}}, "claims": {"P856": [claim]}}}
    hit = listings.wikidata_hit(by_website, KATJA)
    assert hit.found
    assert str(hit.listing_url) == "https://www.wikidata.org/wiki/Q1"
    by_name = {
        "Q2": {
            "labels": {"de": {"value": "Katja Karsch"}},
            "descriptions": {"de": {"value": "Künstlerin aus Münster"}},
        }
    }
    assert listings.wikidata_hit(by_name, KATJA).found
    namesake = {
        "Q3": {
            "labels": {"de": {"value": "Katja Karsch"}},
            "descriptions": {"de": {"value": "Politikerin aus Hamburg"}},
        }
    }
    assert not listings.wikidata_hit(namesake, KATJA).found


@respx.mock
async def test_find_wikidata_searches_by_name_and_website_then_reads_the_candidates() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        action = params.get("action")
        if action == "wbsearchentities":
            return httpx.Response(200, json={"search": [{"id": "Q9"}]})
        if action == "query":
            assert "haswbstatement:P856=" in params["srsearch"]
            return httpx.Response(200, json={"query": {"search": []}})
        assert params["ids"] == "Q9"
        return httpx.Response(
            200,
            json={
                "entities": {
                    "Q9": {
                        "labels": {"de": {"value": "Katja Karsch"}},
                        "descriptions": {"de": {"value": "Malerin in Münster"}},
                    }
                }
            },
        )

    respx.get(listings.WIKIDATA_API).mock(side_effect=answer)
    async with httpx.AsyncClient() as client:
        hit = await listings.find_wikidata(client, KATJA)
    assert hit.found
    assert hit.directory == listings.WIKIDATA_NAME


@respx.mock
async def test_find_wikidata_without_candidates_is_not_found() -> None:
    respx.get(listings.WIKIDATA_API).mock(
        return_value=httpx.Response(200, json={"search": [], "query": {"search": []}})
    )
    async with httpx.AsyncClient() as client:
        hit = await listings.find_wikidata(client, KATJA)
    assert not hit.found


@respx.mock
async def test_a_wikidata_error_answer_is_a_collection_error() -> None:
    respx.get(listings.WIKIDATA_API).mock(
        return_value=httpx.Response(200, json={"error": {"code": "x"}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(CollectionError, match="Wikidata"):
            await listings.find_wikidata(client, KATJA)


def _wikidata_off(monkeypatch: pytest.MonkeyPatch, found: bool = True) -> None:
    async def fake(client: httpx.AsyncClient, target: Target) -> DirectoryHit:
        return _hit(found)

    monkeypatch.setattr(listings, "find_wikidata", fake)


async def test_without_a_token_only_wikidata_runs_and_the_rest_is_suspended_with_reason(
    monkeypatch: pytest.MonkeyPatch, collector_settings: Settings, open_gate: SourceGate
) -> None:
    _wikidata_off(monkeypatch)
    settings = collector_settings.model_copy(update={"apify_token": None})

    result = await listings.collect_listings(settings, open_gate, CATALOGUE_PATH, KATJA)

    by_route = {status.route: status for status in result.routes}
    assert by_route[listings.WIKIDATA_ROUTE].ok is True
    for route in (
        listings.APPLE_ROUTE,
        listings.BING_ROUTE,
        listings.INDUSTRY_ROUTE,
        listings.REVIEW_ROUTE,
    ):
        assert by_route[route].ok is None
        assert by_route[route].reason == listings.NO_TOKEN
    assert len(result.hits) == 1


async def test_a_failed_route_does_not_stop_the_others_and_names_its_reason(
    monkeypatch: pytest.MonkeyPatch, collector_settings: Settings, open_gate: SourceGate
) -> None:
    _wikidata_off(monkeypatch, found=False)

    async def broken(*args: object, **kwargs: object) -> DirectoryHit:
        raise CollectionError("Apple Maps: Actor failed")

    async def bing(*args: object, **kwargs: object) -> DirectoryHit:
        return _hit(True)

    async def portals(*args: object, **kwargs: object) -> list[DirectoryHit]:
        return [_hit(False)]

    monkeypatch.setattr(listings, "find_apple", broken)
    monkeypatch.setattr(listings, "find_bing", bing)
    monkeypatch.setattr(listings, "find_portals", portals)
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("t")})

    result = await listings.collect_listings(settings, open_gate, CATALOGUE_PATH, KATJA)

    by_route = {status.route: status for status in result.routes}
    assert by_route[listings.APPLE_ROUTE].ok is False
    assert by_route[listings.APPLE_ROUTE].reason == "Apple Maps: Actor failed"
    assert by_route[listings.BING_ROUTE].ok is True
    assert by_route[listings.INDUSTRY_ROUTE].ok is True
    assert by_route[listings.REVIEW_ROUTE].ok is True
    assert [hit.found for hit in result.hits] == [False, True, False]


async def test_an_industry_without_trade_portals_is_listed_as_not_run_with_reason(
    monkeypatch: pytest.MonkeyPatch, collector_settings: Settings, open_gate: SourceGate
) -> None:
    _wikidata_off(monkeypatch)

    async def hit(*args: object, **kwargs: object) -> DirectoryHit:
        return _hit(True)

    async def portals(*args: object, **kwargs: object) -> list[DirectoryHit]:
        return []

    monkeypatch.setattr(listings, "find_apple", hit)
    monkeypatch.setattr(listings, "find_bing", hit)
    monkeypatch.setattr(listings, "find_portals", portals)
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("t")})
    nobody = Target(name="x", city="Münster", website="https://x.de", industry="gibt_es_nicht")

    result = await listings.collect_listings(settings, open_gate, CATALOGUE_PATH, nobody)

    industry = next(s for s in result.routes if s.route == listings.INDUSTRY_ROUTE)
    assert industry.ok is None
    assert industry.reason == listings.NO_INDUSTRY_PORTALS
