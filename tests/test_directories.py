from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from pydantic import HttpUrl

from radarauditor.collectors import directories
from radarauditor.collectors.snapshot import DirectoryHit
from radarauditor.db.models import Location
from radarauditor.errors import ConfigurationError

REPOSITORY_CATALOGUE = Path(__file__).parents[1] / "config" / "verzeichnisse.yaml"

pytestmark = pytest.mark.usefixtures("fast_retries")

CATALOGUE = """directories:
  - name: Gelbe Seiten
    kind: core
    search_url: https://www.gelbeseiten.de/suche/{name}/{city}
    no_hits_status: 404
  - name: Das Örtliche
    kind: core
    search_url: https://www.dasoertliche.de/?kw={name}&ci={city}
  - name: 11880
    kind: core
    search_url: https://www.11880.com/suche/{name}/{city}
  - name: Apple Business Connect
    kind: core
industry_specific:
  bauhandwerk: [MyHammer]
"""

GELBE_SEITEN_HTML = """<html><body>
<h1>Suchergebnisse für Dachdeckerei Hollenbeck in Münster</h1>
<article class="mod-Treffer">
  <a href="/gsbiz/abc123"><h2 class="mod-Treffer__name">Dachdeckerei   Hollenbeck GmbH</h2></a>
  <address><p>Hiltruper Straße 210,</p><p>48165 Münster (Hiltrup)</p></address>
  <span class="mod-Treffer__phone">0251 12 34 56</span>
</article>
<article class="mod-Treffer">
  <a href="/gsbiz/def456"><h2>Bedachungen Overmann</h2></a>
  <address>Roxeler Str. 12a, 48161 Münster</address>
</article>
</body></html>"""

OERTLICHE_HTML = """<html><body><div class="hit">
<a class="hitname" href="https://www.dasoertliche.de/Themen/x">Dachdeckerei Hollenbeck</a>
<div class="left">Hiltruper Str. 210 48165 Münster</div><div>Tel. (0251) 123456</div>
</div></body></html>"""

EMPTY_HTML = "<html><body><p>Keine Treffer für Ihre Suche.</p></body></html>"


@pytest.fixture
def location() -> Location:
    return Location(
        id=uuid4(),
        customer_id=uuid4(),
        name="Dachdeckerei Hollenbeck GmbH",
        street="Hiltruper Straße 210",
        postal_code="48165",
        city="Münster",
        phone="+49 251 123456",
        google_place_id=None,
        latitude=51.9,
        longitude=7.63,
    )


@pytest.fixture
def catalogue(tmp_path: Path) -> Path:
    path = tmp_path / "verzeichnisse.yaml"
    path.write_text(CATALOGUE, encoding="utf-8")
    return path


def _hit(**values: object) -> DirectoryHit:
    defaults: dict[str, object] = {
        "directory": "Gelbe Seiten",
        "found": True,
        "listing_url": None,
        "name": None,
        "address": None,
        "phone": None,
    }
    defaults.update(values)
    return DirectoryHit.model_validate(defaults)


def test_load_catalogue_reads_repository_file() -> None:
    catalogue = directories.load_catalogue(REPOSITORY_CATALOGUE)

    names = [entry.name for entry in catalogue.directories]
    assert "Gelbe Seiten" in names
    assert any(entry.search_url for entry in catalogue.directories)


def test_load_catalogue_rejects_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        directories.load_catalogue(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("directories: [{kind: core}]", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        directories.load_catalogue(broken)


@respx.mock
async def test_check_directories_reads_listings_and_leaves_out_unsearchable(
    location: Location, catalogue: Path
) -> None:
    gelbe = respx.get(url__startswith="https://www.gelbeseiten.de/suche/").mock(
        return_value=httpx.Response(200, html=GELBE_SEITEN_HTML)
    )
    respx.get(url__startswith="https://www.dasoertliche.de/").mock(
        return_value=httpx.Response(200, html=OERTLICHE_HTML)
    )
    respx.get(url__startswith="https://www.11880.com/").mock(
        return_value=httpx.Response(200, html=EMPTY_HTML)
    )

    hits = await directories.check_directories(location, catalogue)

    assert [hit.directory for hit in hits] == [
        "Gelbe Seiten",
        "Das Örtliche",
        "11880",
    ]
    gelbe_hit, oertliche_hit, empty_hit = hits
    assert gelbe_hit.found is True
    assert gelbe_hit.name == "Dachdeckerei Hollenbeck GmbH"
    assert gelbe_hit.address == "Hiltruper Straße 210, 48165 Münster (Hiltrup)"
    assert gelbe_hit.phone == "0251 12 34 56"
    assert gelbe_hit.listing_url == HttpUrl("https://www.gelbeseiten.de/gsbiz/abc123")
    assert oertliche_hit.found is True
    assert oertliche_hit.name == "Dachdeckerei Hollenbeck"
    assert oertliche_hit.phone == "(0251) 123456"
    assert empty_hit.found is False
    requested = str(gelbe.calls[0].request.url)
    assert requested.endswith("/Dachdeckerei%20Hollenbeck%20GmbH/M%C3%BCnster")


@respx.mock
async def test_check_directories_leaves_out_failed_directories(
    location: Location, catalogue: Path
) -> None:
    respx.get(url__startswith="https://www.gelbeseiten.de/").mock(return_value=httpx.Response(403))
    oertliche = respx.get(url__startswith="https://www.dasoertliche.de/").mock(
        return_value=httpx.Response(503)
    )
    respx.get(url__startswith="https://www.11880.com/").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )

    hits = await directories.check_directories(location, catalogue)

    assert hits == []
    assert oertliche.call_count == 3


GELBE_SEITEN_NO_HITS_HTML = """<html><body>
<h1>Dachdeckerei Hollenbeck GmbH in Münster ( 0 Treffer)</h1>
<p>Leider finden wir zu Ihrer Suchanfrage keine passenden Einträge.</p>
</body></html>"""


@respx.mock
async def test_check_directories_takes_the_no_hits_status_as_not_listed(
    location: Location, catalogue: Path
) -> None:
    respx.get(url__startswith="https://www.gelbeseiten.de/").mock(
        return_value=httpx.Response(404, html=GELBE_SEITEN_NO_HITS_HTML)
    )
    respx.get(url__startswith="https://www.dasoertliche.de/").mock(
        return_value=httpx.Response(404, html=GELBE_SEITEN_NO_HITS_HTML)
    )
    respx.get(url__startswith="https://www.11880.com/").mock(
        return_value=httpx.Response(200, html=EMPTY_HTML)
    )

    hits = await directories.check_directories(location, catalogue)

    assert hits == [
        _hit(directory="Gelbe Seiten", found=False),
        _hit(directory="11880", found=False),
    ]


def test_find_listing_without_address_still_reports_found() -> None:
    html = "<html><body><ul><li><a href='/x'>Dachdeckerei Hollenbeck</a></li></ul></body></html>"

    hit = directories.find_listing("Cylex", "https://cylex.de/s", html, "Dachdeckerei Hollenbeck")

    assert hit.found is True
    assert hit.address is None
    assert hit.listing_url == HttpUrl("https://cylex.de/x")


def test_nap_matches_ignores_legal_form_spacing_and_phone_format(location: Location) -> None:
    found = _hit(
        name="dachdeckerei  hollenbeck",
        address="Hiltruper Str. 210, 48165 Münster",
        phone="0251 / 12 34 56",
    )

    assert directories.nap_matches(location, found) == {
        "name": True,
        "address": True,
        "phone": True,
    }


def test_nap_matches_detects_differences(location: Location) -> None:
    found = _hit(
        name="Hollenbeck Bedachungen",
        address="Hiltruper Straße 21, 48165 Münster",
        phone="0251 999999",
    )

    assert directories.nap_matches(location, found) == {
        "name": False,
        "address": False,
        "phone": False,
    }


def test_nap_matches_with_missing_values_is_false(location: Location) -> None:
    assert directories.nap_matches(location, _hit(found=False)) == {
        "name": False,
        "address": False,
        "phone": False,
    }
