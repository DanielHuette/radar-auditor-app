import pytest
from pydantic import SecretStr

from radarauditor.collectors import listings, map_listings
from radarauditor.collectors.listings import Target
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import DirectoryHit
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import CollectionError

KATJA = Target(
    name="Katja Karsch",
    city="Münster",
    street="Hauptstraße 5",
    postal_code="48143",
    phone="0251 123456",
    website="https://www.katjakarsch.de/",
    industry="kunst_kreativ",
)


def _hit(directory: str, found: bool) -> DirectoryHit:
    return DirectoryHit(
        directory=directory,
        found=found,
        listing_url=None,
        name=None,
        address=None,
        phone=None,
    )


@pytest.fixture
def finders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    async def apple(*arguments: object) -> DirectoryHit:
        asked.append("apple")
        return _hit("Apple Maps", True)

    async def bing(*arguments: object) -> DirectoryHit:
        asked.append("bing")
        return _hit("Bing Places", False)

    async def never(*arguments: object) -> object:
        raise AssertionError("this finder is switched off")

    monkeypatch.setattr(map_listings, "find_apple", apple)
    monkeypatch.setattr(map_listings, "find_bing", bing)
    monkeypatch.setattr(listings, "find_wikidata", never)
    monkeypatch.setattr(listings, "find_portals", never)
    monkeypatch.setattr(listings, "collect_listings", never)
    return asked


async def test_only_apple_and_bing_are_asked(
    finders: list[str], collector_settings: Settings, open_gate: SourceGate
) -> None:
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("token")})

    result = await map_listings.collect_map_listings(settings, open_gate, KATJA)

    assert finders == ["apple", "bing"]
    assert [(hit.directory, hit.found) for hit in result.hits] == [
        ("Apple Maps", True),
        ("Bing Places", False),
    ]
    assert [(status.route, status.ok) for status in result.routes] == [
        (listings.APPLE_ROUTE, True),
        (listings.BING_ROUTE, True),
    ]


async def test_without_a_token_both_routes_are_suspended_with_the_reason(
    finders: list[str], collector_settings: Settings, open_gate: SourceGate
) -> None:
    settings = collector_settings.model_copy(update={"apify_token": None})

    result = await map_listings.collect_map_listings(settings, open_gate, KATJA)

    assert finders == []
    assert result.hits == []
    assert [(status.route, status.ok, status.reason) for status in result.routes] == [
        (listings.APPLE_ROUTE, None, listings.NO_TOKEN),
        (listings.BING_ROUTE, None, listings.NO_TOKEN),
    ]


async def test_with_apify_switched_off_both_routes_are_suspended_with_the_reason(
    finders: list[str], collector_settings: Settings
) -> None:
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("token")})
    sources = SourcesSettings.model_validate({Source.APIFY.value: {"enabled": False}})
    gate = SourceGate(sources, lambda source, cap: True)

    result = await map_listings.collect_map_listings(settings, gate, KATJA)

    assert finders == []
    assert [status.reason for status in result.routes] == [listings.APIFY_OFF] * 2


async def test_a_failed_route_does_not_stop_the_other_and_names_its_reason(
    finders: list[str],
    collector_settings: Settings,
    open_gate: SourceGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*arguments: object) -> DirectoryHit:
        raise CollectionError("Apple Maps: Actor failed")

    monkeypatch.setattr(map_listings, "find_apple", broken)
    settings = collector_settings.model_copy(update={"apify_token": SecretStr("token")})

    result = await map_listings.collect_map_listings(settings, open_gate, KATJA)

    assert [(status.route, status.ok, status.reason) for status in result.routes] == [
        (listings.APPLE_ROUTE, False, "Apple Maps: Actor failed"),
        (listings.BING_ROUTE, True, None),
    ]
    assert [hit.directory for hit in result.hits] == ["Bing Places"]
