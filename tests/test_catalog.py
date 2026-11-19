from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from radarauditor.errors import ScoringError
from radarauditor.feedback.routes import load_measurements, load_route_map
from radarauditor.scoring.catalog import (
    PROVIDER_SECTIONS,
    SOURCE_LIST,
    Catalogue,
    checks_for_context,
    find_check,
    load_catalogue,
)

CONFIG = Path(__file__).resolve().parent.parent / "config"
SECTION_ORDER = ["google", "bing", "apple", "openai", "anthropic", "ads"]
OFFICIAL_SOURCE = "https://developers.google.com/search/docs/essentials/technical"


def _check(
    check_id: str, provider: str = "google", measurement: str = "crawler", **overrides: object
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id,
        "name": f"Check {check_id}",
        "why": "Weil.",
        "provider": provider,
        "kind": "kriterium",
        "source": OFFICIAL_SOURCE,
        "measurement": measurement,
        "requires_paid": measurement in {"paid", "ads_google", "ads_meta"},
        "requires_access": measurement == "access",
        "thresholds": {"limit": 3},
        "customer_question": "Frage?",
        "customer_impact": "Wirkung.",
    }
    check.update(overrides)
    return check


def _document() -> dict[str, Any]:
    return {
        "areas": [
            {
                "id": "google",
                "name": "Google",
                "weight": 60,
                "checks": [
                    _check("a1"),
                    _check("a2", measurement="ads_google"),
                    _check("m1", measurement="access"),
                ],
            },
            {
                "id": "openai",
                "name": "ChatGPT",
                "weight": 40,
                "checks": [_check("k1", "openai", "assistants")],
            },
            {
                "id": "ads",
                "name": "Bezahlte Sichtbarkeit",
                "weight": 0,
                "checks": [_check("p2", "meta_ads", "ads_meta")],
            },
        ]
    }


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "katalog.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def _load(tmp_path: Path, document: dict[str, Any]) -> Catalogue:
    return load_catalogue(_write(tmp_path, document))


def test_real_catalogue_sections_and_weights(catalogue: Catalogue) -> None:
    assert [area.id for area in catalogue.areas] == SECTION_ORDER
    assert sum(area.weight for area in catalogue.areas) == 100
    assert find_check(catalogue, "a2").thresholds["lcp_good_ms"] == 2500
    assert all(check.area == area.id for area in catalogue.areas for check in area.checks)


def test_real_catalogue_every_check_has_a_known_provider_and_section(
    catalogue: Catalogue,
) -> None:
    checks = [check for area in catalogue.areas for check in area.checks]
    assert checks
    for check in checks:
        assert check.provider in PROVIDER_SECTIONS, check.id
        assert PROVIDER_SECTIONS[check.provider] == check.area, check.id


def test_real_catalogue_every_criterion_has_a_source_from_the_source_list(
    catalogue: Catalogue,
) -> None:
    criteria = [
        check for area in catalogue.areas for check in area.checks if check.kind == "kriterium"
    ]
    assert criteria
    assert all(check.source in SOURCE_LIST for check in criteria)


def test_real_catalogue_results_and_infos_name_no_source_or_a_listed_one(
    catalogue: Catalogue,
) -> None:
    others = [
        check for area in catalogue.areas for check in area.checks if check.kind != "kriterium"
    ]
    assert others
    assert all(check.source in {"", *SOURCE_LIST} for check in others)


@pytest.mark.parametrize("check_id", ["j1", "j2", "j7"])
def test_the_place_limits_name_their_source(catalogue: Catalogue, check_id: str) -> None:
    assert find_check(catalogue, check_id).source in SOURCE_LIST
    assert find_check(catalogue, check_id).source.startswith("https://")


REAL_CATALOGUE_IDS = {
    "google": [
        "a2",
        "a4",
        "a12",
        "a11",
        "a13",
        "a7",
        "b5",
        "c4",
        "i1",
        "f1",
        "f2",
        "f4",
        "f5",
        "f7",
        "f8",
        "f13",
        "g1",
        "g2",
        "j1",
        "j2",
        "j3",
        "j4",
        "j5",
        "j6",
        "j8",
        "c12",
        "j9",
        "m1",
        "m2",
    ],
    "bing": [
        "a14",
        "b10",
        "b11",
        "b12",
        "b13",
        "b14",
        "b15",
        "b3",
        "b4",
        "b16",
        "b17",
        "b18",
        "b19",
        "b20",
        "b21",
        "b22",
        "j7",
        "h2",
    ],
    "apple": ["h1"],
    "openai": ["a15", "a16", "k1", "k2"],
    "anthropic": ["a17", "a18", "k6", "k7"],
    "ads": ["p1", "p3", "p4", "p5", "p2", "p6"],
}


@pytest.mark.parametrize("section", list(REAL_CATALOGUE_IDS))
def test_real_catalogue_holds_exactly_the_ids_of_the_build_order(
    catalogue: Catalogue, section: str
) -> None:
    area = next(area for area in catalogue.areas if area.id == section)
    assert [check.id for check in area.checks] == REAL_CATALOGUE_IDS[section]


def test_real_catalogue_only_the_paid_visibility_checks_are_infos(catalogue: Catalogue) -> None:
    infos = {check.id for area in catalogue.areas for check in area.checks if check.kind == "info"}
    assert infos == set(REAL_CATALOGUE_IDS["ads"]) | {"j3", "k1", "k2", "k6", "k7"}
    assert not any(
        check.counts_in_score
        for area in catalogue.areas
        for check in area.checks
        if check.id in infos
    )


def test_real_catalogue_ids_are_unique(catalogue: Catalogue) -> None:
    ids = [check.id for area in catalogue.areas for check in area.checks]
    assert len(ids) == len(set(ids))


def test_real_catalogue_measurements_exist_in_routes_file(catalogue: Catalogue) -> None:
    measurements = load_measurements(CONFIG / "messwege.yaml")
    used = {check.measurement for area in catalogue.areas for check in area.checks}
    assert used <= set(measurements)


def test_real_catalogue_has_a_route_entry_for_every_check(catalogue: Catalogue) -> None:
    route_map = load_route_map(CONFIG / "messwege.yaml", catalogue)
    ids = {check.id for area in catalogue.areas for check in area.checks}
    assert {route_map.entry(check_id).id for check_id in ids} == ids


def test_minimal_catalogue_loads_with_section_ids(tmp_path: Path) -> None:
    catalogue = _load(tmp_path, _document())
    assert [area.id for area in catalogue.areas] == ["google", "openai", "ads"]
    assert find_check(catalogue, "k1").area == "openai"
    assert find_check(catalogue, "p2").provider == "meta_ads"


def test_google_ads_and_meta_ads_share_the_ads_section(tmp_path: Path) -> None:
    document = _document()
    document["areas"][2]["checks"].append(_check("p1", "google_ads", "ads_google"))
    catalogue = _load(tmp_path, document)
    assert {check.provider for check in catalogue.areas[2].checks} == {"meta_ads", "google_ads"}


def test_weights_not_100_raise(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["weight"] = 50
    with pytest.raises(ScoringError, match="sum to 90"):
        _load(tmp_path, document)


def test_weights_of_101_raise(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["weight"] = 61
    with pytest.raises(ScoringError, match="sum to 101"):
        _load(tmp_path, document)


def test_duplicate_id_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"].append(_check("a1"))
    with pytest.raises(ScoringError, match="twice"):
        _load(tmp_path, document)


@pytest.mark.parametrize("bad_id", ["a", "a1b", "A1", "1a", "aa1"])
def test_malformed_id_raises(tmp_path: Path, bad_id: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["id"] = bad_id
    with pytest.raises(ScoringError, match="does not match"):
        _load(tmp_path, document)


@pytest.mark.parametrize("good_id", ["a9", "x3", "z10"])
def test_id_letter_does_not_have_to_match_the_section(tmp_path: Path, good_id: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["id"] = good_id
    assert find_check(_load(tmp_path, document), good_id).area == "google"


def test_unknown_provider_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["provider"] = "yahoo"
    with pytest.raises(ScoringError, match="unknown provider"):
        _load(tmp_path, document)


def test_provider_in_wrong_section_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["provider"] = "bing"
    with pytest.raises(ScoringError, match="filed under section google"):
        _load(tmp_path, document)


def test_missing_provider_raises(tmp_path: Path) -> None:
    document = _document()
    del document["areas"][0]["checks"][0]["provider"]
    with pytest.raises(ScoringError, match="malformed"):
        _load(tmp_path, document)


def test_missing_source_raises(tmp_path: Path) -> None:
    document = _document()
    del document["areas"][0]["checks"][0]["source"]
    with pytest.raises(ScoringError, match="source list"):
        _load(tmp_path, document)


@pytest.mark.parametrize(
    "bad_source",
    [
        "",
        "crawler",
        "http://example.org",
        "www.google.com",
        "https://example.org/official",
        "https://developers.google.com/search/docs/essentials/technical/",
    ],
)
def test_criterion_without_a_source_from_the_list_raises(tmp_path: Path, bad_source: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["source"] = bad_source
    with pytest.raises(ScoringError, match="source list"):
        _load(tmp_path, document)


@pytest.mark.parametrize("source", sorted(SOURCE_LIST))
def test_criterion_accepts_every_address_of_the_source_list(tmp_path: Path, source: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["source"] = source
    assert find_check(_load(tmp_path, document), "a1").source == source


@pytest.mark.parametrize("kind", ["ergebnis", "info"])
def test_result_and_info_may_leave_the_source_empty(tmp_path: Path, kind: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0].update({"kind": kind, "source": ""})
    assert find_check(_load(tmp_path, document), "a1").source == ""


@pytest.mark.parametrize("kind", ["ergebnis", "info"])
def test_result_and_info_with_a_foreign_source_raise(tmp_path: Path, kind: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0].update({"kind": kind, "source": "https://example.org/x"})
    with pytest.raises(ScoringError, match="source list"):
        _load(tmp_path, document)


@pytest.mark.parametrize("bad_kind", ["", "Kriterium", "hinweis"])
def test_unknown_kind_raises(tmp_path: Path, bad_kind: str) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["kind"] = bad_kind
    with pytest.raises(ScoringError, match="malformed"):
        _load(tmp_path, document)


def test_missing_kind_raises(tmp_path: Path) -> None:
    document = _document()
    del document["areas"][0]["checks"][0]["kind"]
    with pytest.raises(ScoringError, match="malformed"):
        _load(tmp_path, document)


@pytest.mark.parametrize(
    ("kind", "counts"), [("kriterium", True), ("ergebnis", True), ("info", False)]
)
def test_only_info_checks_stay_out_of_the_score(tmp_path: Path, kind: str, counts: bool) -> None:
    document = _document()
    source = OFFICIAL_SOURCE if kind == "kriterium" else ""
    document["areas"][0]["checks"][0].update({"kind": kind, "source": source})
    assert find_check(_load(tmp_path, document), "a1").counts_in_score is counts


def test_unknown_measurement_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"][0]["measurement"] = "guesswork"
    with pytest.raises(ScoringError, match="unknown measurement"):
        _load(tmp_path, document)


def test_paid_flag_disagreeing_with_measurement_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"][1]["requires_paid"] = False
    with pytest.raises(ScoringError, match="requires_paid"):
        _load(tmp_path, document)


def test_access_flag_disagreeing_with_measurement_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][0]["checks"][2]["requires_access"] = False
    with pytest.raises(ScoringError, match="requires_access"):
        _load(tmp_path, document)


def test_negative_weight_raises(tmp_path: Path) -> None:
    document = _document()
    document["areas"][2]["weight"] = -10
    document["areas"][1]["weight"] = 50
    with pytest.raises(ScoringError, match="negative"):
        _load(tmp_path, document)


def test_missing_field_raises(tmp_path: Path) -> None:
    document = _document()
    del document["areas"][0]["checks"][0]["customer_question"]
    with pytest.raises(ScoringError, match="malformed"):
        _load(tmp_path, document)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "katalog.yaml"
    path.write_text('areas:\n  - id: a\n    name: "Zitat" danach\n', encoding="utf-8")
    with pytest.raises(ScoringError, match="not valid YAML"):
        load_catalogue(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="Cannot read"):
        load_catalogue(tmp_path / "fehlt.yaml")


def test_document_without_areas_raises(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="no list of areas"):
        load_catalogue(_write(tmp_path, {"bereiche": []}))


def test_find_check_unknown_raises(tmp_path: Path) -> None:
    catalogue = _load(tmp_path, _document())
    with pytest.raises(ScoringError, match="z9"):
        find_check(catalogue, "z9")


@pytest.mark.parametrize(
    ("paid", "access", "expected"),
    [
        (False, False, ["a1", "k1"]),
        (True, False, ["a1", "a2", "k1", "p2"]),
        (False, True, ["a1", "m1", "k1"]),
        (True, True, ["a1", "a2", "m1", "k1", "p2"]),
    ],
)
def test_checks_for_context(tmp_path: Path, paid: bool, access: bool, expected: list[str]) -> None:
    catalogue = _load(tmp_path, _document())
    assert [check.id for check in checks_for_context(catalogue, paid, access)] == expected


def _weighted(weights: list[str | None], area_weight: str = "100") -> dict[str, Any]:
    checks = [
        _check(f"a{index}", weight=weight) if weight is not None else _check(f"a{index}")
        for index, weight in enumerate(weights, start=1)
    ]
    return {"areas": [{"id": "google", "name": "Google", "weight": area_weight, "checks": checks}]}


def test_check_weights_must_add_up_to_the_area_weight(tmp_path: Path) -> None:
    catalogue = _load(tmp_path, _weighted(["60.5", "39.5"]))
    assert [check.weight for check in catalogue.areas[0].checks] == [
        Decimal("60.5"),
        Decimal("39.5"),
    ]
    with pytest.raises(ScoringError, match="check weights sum to"):
        _load(tmp_path, _weighted(["60.5", "39.4"]))


def test_a_weight_on_some_checks_only_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="every check has a weight or none"):
        _load(tmp_path, _weighted(["100", None]))


def test_a_negative_check_weight_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="negative weight"):
        _load(tmp_path, _weighted(["110", "-10"]))


def test_area_weights_must_add_up_to_exactly_one_hundred(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match="Area weights sum to"):
        _load(tmp_path, _weighted([None], area_weight="99.999"))


def test_the_shipped_catalogue_gives_every_check_a_weight_and_sums_to_one_hundred() -> None:
    catalogue = load_catalogue(CONFIG / "pruefkatalog.yaml")
    checks = [check for area in catalogue.areas for check in area.checks]
    assert all(check.weight is not None for check in checks)
    assert sum((check.weight or Decimal(0)) for check in checks) == Decimal(100)
    assert all(check.weight == 0 for check in checks if not check.counts_in_score)


def test_the_shipped_catalogue_keeps_the_assistants_at_their_old_share() -> None:
    catalogue = load_catalogue(CONFIG / "pruefkatalog.yaml")
    areas = {area.id: area.weight for area in catalogue.areas}
    assert areas["openai"] + areas["anthropic"] < Decimal(15)
    assert areas["apple"] < Decimal(2)


def test_the_shipped_meta_ads_check_counts_as_paid() -> None:
    catalogue = load_catalogue(CONFIG / "pruefkatalog.yaml")
    assert find_check(catalogue, "p2").requires_paid is True
