from pathlib import Path

import pytest
import yaml
from conftest import CHECK_COUNT

from radarauditor.errors import ScoringError
from radarauditor.feedback.routes import RouteMap, load_route_map, route_label
from radarauditor.scoring.catalog import Catalogue, load_catalogue

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="module")
def real_catalogue() -> Catalogue:
    return load_catalogue(CONFIG / "pruefkatalog.yaml")


@pytest.fixture(scope="module")
def routes(real_catalogue: Catalogue) -> RouteMap:
    return load_route_map(CONFIG / "messwege.yaml", real_catalogue)


def _ids(routes: RouteMap, route: str) -> list[str]:
    return [check.id for check in routes.checks_of(route)]


def test_every_check_has_its_number_from_the_list(
    routes: RouteMap, real_catalogue: Catalogue
) -> None:
    ids = [check.id for area in real_catalogue.areas for check in area.checks]
    numbers = sorted(routes.number_of(check_id) for check_id in ids)

    assert numbers == list(range(1, CHECK_COUNT + 1))
    assert (routes.number_of("a2"), routes.number_of("j1"), routes.number_of("h1")) == (1, 19, 48)
    assert routes.number_of("p6") == CHECK_COUNT


@pytest.mark.parametrize(
    ("route", "stage"),
    [
        ("dataforseo", 0),
        ("google_search", 1),
        ("bing", 1),
        ("google_images", 1),
        ("google_maps", 1),
        ("assistant:chatgpt", 1),
        ("assistant:claude", 1),
        ("apple_maps", 2),
        ("bing_maps", 2),
        ("google_ads", 2),
        ("meta_ads", 2),
        ("website", 3),
        ("pagespeed", 3),
        ("access", 4),
    ],
)
def test_stage_of_a_route_is_the_lowest_stage_of_its_checks(
    routes: RouteMap, route: str, stage: int
) -> None:
    assert routes.stage_of(route) == stage


def test_routes_serve_the_checks_of_their_source(routes: RouteMap) -> None:
    assert _ids(routes, "pagespeed") == ["a2"]
    assert _ids(routes, "bing") == ["j7"]
    assert _ids(routes, "google_images") == ["j8"]
    assert _ids(routes, "assistant:google_ai") == ["k1", "k2", "k6", "k7"]
    assert _ids(routes, "access") == ["m1", "m2", "p5", "p6"]
    assert _ids(routes, "apple_maps") == ["h1"]
    assert _ids(routes, "bing_maps") == ["h2"]
    assert _ids(routes, "google_ads") == ["p1"]
    assert _ids(routes, "meta_ads") == ["p2"]
    assert _ids(routes, "dataforseo") == ["i1", "j5", "c12"]
    assert "j7" not in _ids(routes, "google_search")
    assert {"j1", "j2", "j4", "j6", "p3", "p4"} <= set(_ids(routes, "google_search"))


def test_checks_come_in_the_order_of_their_numbers(routes: RouteMap) -> None:
    numbers = [check.number for check in routes.checks_of("google_maps")]

    assert numbers == sorted(numbers)
    assert (
        routes.checks_of("google_search")[0].name
        == "Platz bei den wichtigsten Suchen im eigenen Ort"
    )


def test_route_of_a_check_gives_its_route(routes: RouteMap) -> None:
    assert routes.route_of("h1") == "apple_maps"
    assert routes.route_of("a4") == "website"
    assert routes.route_of("k1") == "assistants"


def test_a_route_without_checks_is_an_error(routes: RouteMap) -> None:
    with pytest.raises(ScoringError, match="serves no check"):
        routes.stage_of("nowhere")


def test_labels_are_german_and_name_assistants(routes: RouteMap) -> None:
    assert route_label("google_search") == "Google-Suche"
    assert route_label("assistant:google_ai") == "Googles KI-Modus"
    assert route_label("assistant:chatgpt") == "ChatGPT"
    assert route_label("something_new") == "something_new"


def _write(tmp_path: Path, checks: dict[str, str]) -> Path:
    document = yaml.safe_load((CONFIG / "messwege.yaml").read_text(encoding="utf-8"))
    document["checks"] = {check_id: yaml.safe_load(entry) for check_id, entry in checks.items()}
    path = tmp_path / "messwege.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def _entries(real_catalogue: Catalogue) -> dict[str, str]:
    ids = [check.id for area in real_catalogue.areas for check in area.checks]
    return {
        check_id: f"{{number: {number}, stage: 3}}" for number, check_id in enumerate(ids, start=1)
    }


def test_a_check_without_an_entry_is_an_error(tmp_path: Path, real_catalogue: Catalogue) -> None:
    entries = _entries(real_catalogue)
    del entries["a2"]
    path = _write(tmp_path, entries)

    with pytest.raises(ScoringError, match="a2"):
        load_route_map(path, real_catalogue)


def test_a_number_used_twice_is_an_error(tmp_path: Path, real_catalogue: Catalogue) -> None:
    entries = _entries(real_catalogue)
    entries["a4"] = "{number: 1, stage: 3}"
    path = _write(tmp_path, entries)

    with pytest.raises(ScoringError, match="number 1"):
        load_route_map(path, real_catalogue)


def test_a_stage_above_four_is_an_error(tmp_path: Path, real_catalogue: Catalogue) -> None:
    entries = _entries(real_catalogue)
    entries["a2"] = "{number: 1, stage: 5}"
    path = _write(tmp_path, entries)

    with pytest.raises(ScoringError, match="malformed"):
        load_route_map(path, real_catalogue)


def test_a_stage_of_four_is_allowed(tmp_path: Path, real_catalogue: Catalogue) -> None:
    entries = _entries(real_catalogue)
    entries["a2"] = "{number: 1, stage: 4}"
    path = _write(tmp_path, entries)

    assert load_route_map(path, real_catalogue).entry("a2").stage == 4


def test_a_missing_file_is_an_error(tmp_path: Path, real_catalogue: Catalogue) -> None:
    with pytest.raises(ScoringError, match="cannot read"):
        load_route_map(tmp_path / "nope.yaml", real_catalogue)


def test_entries_for_checks_not_in_the_catalogue_are_allowed(
    tmp_path: Path, real_catalogue: Catalogue
) -> None:
    entries = {**_entries(real_catalogue), "z99": "{number: 500, stage: 3}"}
    path = _write(tmp_path, entries)

    assert load_route_map(path, real_catalogue).number_of("a2") == 1
