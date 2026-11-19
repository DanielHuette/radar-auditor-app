from collections.abc import Callable

import pytest

from radarauditor.collectors.identity import identity, is_name_search, same_business
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    ListedBusiness,
    MapResultList,
    RawAudit,
    SerpResult,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks import g_bewertungen, j_rankings

LONG_IMPRINT_NAME = "Malerbetrieb Schmidt GmbH & Co. KG Inh. Peter Schmidt"
Edit = Callable[..., RawAudit]


@pytest.mark.parametrize(
    ("own", "other", "same"),
    [
        ("Peter Schmidt Malerbetrieb", "Malerbetrieb Schmidt", True),
        ("Katja Karsch Kunstmalerei", "Katja Karsch", True),
        (LONG_IMPRINT_NAME, "Malerbetrieb Schmidt", True),
        ("Malerbetrieb Schmidt Inhaber Peter Schmidt", "Schmidt Malerbetrieb", True),
        ("Malerbetrieb Schmidt e.K. Inhaberin Petra Schmidt", "Malerbetrieb Schmidt", True),
        ("Holz-Bau Kühn", "Holz-Bau Kühn GmbH", True),
        ("Atelier Katja Karsch", "Atelier Katja Karsch GmbH", True),
        ("Malerbetrieb Schmidt Bielefeld", "Schmidt Malerbetrieb", True),
        ("Schmidt", "Schmidt GmbH", True),
        ("Malerbetrieb Müller", "Malerbetrieb Müller-Lüdenscheidt", False),
        ("Maler", "Malerbetrieb Schmidt", False),
        ("Holz-Bau Kühn", "Holzbau Kühn", False),
        ("Atelier Katja Karsch", "Atelier", False),
        ("Atelier Katja Karsch", "Atelier Müller", False),
        ("Atelier Katja Karsch", "Katja Müller", False),
        ("Malerbetrieb Schmidt", "Sanitär Schmidt", False),
        ("Malerbetrieb Schmidt", "Maler Schmidt", False),
        ("Schmidt", "Sanitär Schmidt GmbH", False),
        (LONG_IMPRINT_NAME, "Malerbetrieb Meier", False),
        (None, "Malerbetrieb Schmidt", False),
        ("Malerbetrieb Schmidt", None, False),
        ("", "", False),
    ],
)
def test_a_business_is_recognised_by_its_name_when_no_feature_decides(
    own: str | None, other: str | None, same: bool
) -> None:
    assert same_business(identity(name=own), identity(name=other)) is same


@pytest.mark.parametrize(
    ("own", "other", "same"),
    [
        (
            {"name": "Malerbetrieb Schmidt", "phone": "0521 123456"},
            {"name": "Malerbetrieb Schmidt", "phone": "0521 999999"},
            False,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "website": "https://schmidt-maler.de"},
            {"name": "Malerbetrieb Schmidt", "website": "https://maler-schmidt.com"},
            False,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "phone": "+49 521 123456"},
            {"name": "Maler Meier", "phone": "0521/123456"},
            True,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "website": "https://www.schmidt-maler.de/"},
            {"name": "Meier", "website": "https://schmidt-maler.de/kontakt"},
            True,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "place_ids": ["ChIJabc"]},
            {"name": "Malerbetrieb Schmidt", "place_ids": ["ChIJxyz"]},
            False,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "place_ids": ["ChIJabc"]},
            {"name": "Meier", "place_ids": ["ChIJabc"]},
            True,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "street": "Hauptstraße 5", "postal_code": "33602"},
            {"name": "Meier", "address": "Hauptstr. 5, 33602 Bielefeld"},
            True,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "street": "Hauptstraße 5", "postal_code": "33602"},
            {"name": "Malerbetrieb Schmidt", "address": "Nebenweg 7, 33602 Bielefeld"},
            False,
        ),
        (
            {"name": "Malerbetrieb Schmidt", "phone": "0521 123456"},
            {"name": "Malerbetrieb Schmidt"},
            True,
        ),
    ],
)
def test_a_feature_present_on_both_sides_decides_before_the_name(
    own: dict[str, object], other: dict[str, object], same: bool
) -> None:
    assert same_business(identity(**own), identity(**other)) is same


def test_a_search_phrase_is_a_name_search_only_by_the_name_rule() -> None:
    assert is_name_search("Peter Schmidt Malerbetrieb", "Malerbetrieb Schmidt")
    assert not is_name_search("dachdeckerei", "Dachdeckerei Müller")
    assert not is_name_search("Maler", "Malerbetrieb Schmidt")


def _raw_with_business(raw: RawAudit, name: str, city: str) -> RawAudit:
    return raw.model_copy(update={"business": BusinessFacts(name=name, city=city)})


def _serp(phrase: str, local_pack: list[tuple[int, str]]) -> SerpResult:
    return SerpResult.model_validate(
        {
            "phrase": phrase,
            "organic": [],
            "local_pack": local_pack,
            "ai_overview_present": False,
            "ai_overview_mentions": [],
            "people_also_ask": [],
            "measured_at": "2026-10-01T12:00:00Z",
        }
    )


def test_j2_finds_the_own_business_when_the_imprint_name_is_longer_than_the_map_entry(
    raw_audit_full: RawAudit,
) -> None:
    raw = _raw_with_business(raw_audit_full, LONG_IMPRINT_NAME, "Bielefeld")
    raw = raw.model_copy(
        update={
            "serps": [_serp("maler bielefeld", [(1, "Malerbetrieb Schmidt"), (2, "Maler Meier")])],
            "map_rankings": [],
        }
    )
    outcome, measured = j_rankings.run("j2", raw, {"top": 3})
    assert outcome is CheckOutcome.PASSED
    assert measured["positions"] == {"maler bielefeld": 1}


def test_j2_does_not_take_a_business_with_one_shared_word_for_the_own_one(
    raw_audit_full: RawAudit,
) -> None:
    raw = _raw_with_business(raw_audit_full, LONG_IMPRINT_NAME, "Bielefeld")
    raw = raw.model_copy(
        update={
            "serps": [_serp("maler bielefeld", [(1, "Sanitär Schmidt"), (2, "Schmidt Elektro")])],
            "map_rankings": [],
        }
    )
    outcome, measured = j_rankings.run("j2", raw, {"top": 3})
    assert outcome is CheckOutcome.FAILED
    assert measured["positions"] == {"maler bielefeld": None}


def _listing(*businesses: ListedBusiness) -> MapResultList:
    return MapResultList(phrase="maler bielefeld", businesses=list(businesses), route="browser")


def test_g1_does_not_count_the_own_entry_without_place_id_as_a_competitor(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    raw = with_place(raw_audit_full, name="Malerbetrieb Schmidt Bielefeld", review_count=40)
    raw = _raw_with_business(raw, LONG_IMPRINT_NAME, "Bielefeld").model_copy(
        update={
            "map_results": _listing(
                ListedBusiness(name="Schmidt Malerbetrieb", review_count=500),
                ListedBusiness(name="Sanitär Schmidt", review_count=10),
                ListedBusiness(name="Bau Krause", review_count=20),
            )
        }
    )
    outcome, measured = g_bewertungen.run("g1", raw, {})
    assert measured["competitors"] == {"Sanitär Schmidt": 10, "Bau Krause": 20}
    assert outcome is CheckOutcome.PASSED


def test_g2_does_not_count_the_own_entry_without_place_id_as_a_peer(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    raw = with_place(raw_audit_full, name="Malerbetrieb Schmidt Bielefeld", rating=4.5)
    raw = _raw_with_business(raw, LONG_IMPRINT_NAME, "Bielefeld").model_copy(
        update={
            "access": None,
            "map_results": _listing(
                ListedBusiness(name="Schmidt Malerbetrieb", review_count=5, rating=1.0),
                ListedBusiness(name="Bau Krause", review_count=20, rating=4.0),
            ),
        }
    )
    outcome, measured = g_bewertungen.run("g2", raw, {"partial_margin_stars": 0.3})
    assert measured["peers"] == {"Bau Krause": 4.0}
    assert outcome is CheckOutcome.PASSED


@pytest.mark.parametrize(
    ("own", "other", "same"),
    [
        (
            {"name": "Atelier Nord", "website": "https://atelier-nord.de", "phone": "0251 123456"},
            {
                "name": "Atelier Nord",
                "website": "https://www.instagram.com/ateliernord/",
                "phone": "+49 251 123456",
            },
            True,
        ),
        (
            {"name": "Atelier Nord", "website": "https://www.facebook.com/ateliernord"},
            {"name": "Tischlerei Süd", "website": "https://www.facebook.com/tischlereisued"},
            False,
        ),
        (
            {"name": "Atelier Nord", "website": "https://atelier-nord.de", "phone": "0251 123456"},
            {"name": "Atelier Nord", "website": "https://atelier-sued.de", "phone": "0251 123456"},
            True,
        ),
        (
            {"name": "Atelier Nord", "website": "https://atelier-nord.de", "phone": "0251 123456"},
            {"name": "Atelier Nord", "website": "https://atelier-sued.de", "phone": "0251 999999"},
            False,
        ),
        (
            {
                "name": "Atelier Nord",
                "website": "https://atelier-nord.de",
                "street": "Hauptstraße 5",
                "postal_code": "48143",
            },
            {
                "name": "Atelier Nord Münster",
                "website": "https://linktr.ee/ateliernord",
                "address": "Hauptstr. 5, 48143 Münster",
            },
            True,
        ),
    ],
)
def test_one_matching_feature_is_enough_and_shared_hosts_decide_nothing(
    own: dict[str, object], other: dict[str, object], same: bool
) -> None:
    assert same_business(identity(**own), identity(**other)) is same
