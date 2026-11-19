from typing import Any

import pytest
from conftest import (
    CUSTOMER_DOMAIN,
    SITE,
    build_answers,
    build_paid,
    build_place,
    build_serps,
    build_website,
)
from pydantic import HttpUrl

from radarauditor.collectors import validation
from radarauditor.collectors.identity import identity, same_business
from radarauditor.collectors.snapshot import (
    BusinessFacts,
    DirectoryHit,
    PageSpeedField,
    PageSpeedLab,
    PageSpeedSnapshot,
    SerpResult,
)

REQUESTED = HttpUrl(f"{SITE}/")
BUSINESS = BusinessFacts(
    name="Dachdeckerei Muster GmbH",
    city="Münster",
    street="Hauptstraße 5",
    postal_code="48143",
    phone="0251 123456",
)


def _speed(**lab: Any) -> PageSpeedSnapshot:
    values: dict[str, Any] = {"runs": 5, "lcp_ms": 2300, "cls": 0.06, "tbt_ms": 150}
    values.update(lab)
    return PageSpeedSnapshot(
        url=REQUESTED,
        mobile_performance=85,
        desktop_performance=97,
        accessibility=92,
        lab=PageSpeedLab(**values),
        field_data=PageSpeedField(scope="url", lcp_ms=2100, cls=0.05, inp_ms=150),
    )


def _serp(**updates: Any) -> SerpResult:
    values: dict[str, Any] = {
        "phrase": "dachdecker münster",
        "returned_keyword": "Dachdecker Münster",
        "language_code": "de",
        "location_code": 2276,
        "organic": [(1, "a.de"), (3, "b.de")],
    }
    values.update(updates)
    return build_serps()[0].model_copy(update=values)


def test_website_with_start_page_text_and_host_is_valid() -> None:
    record = validation.validate_website(build_website(), REQUESTED)
    assert record.valid is True
    assert record.record_count == len(build_website().pages)


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"status_code": 404}, "Status 404"),
        ({"text_excerpt": "  "}, "keinen sichtbaren Text"),
        ({"url": HttpUrl("https://fremd.example/")}, "anderen Host"),
    ],
)
def test_website_problems_make_the_record_invalid(update: dict[str, Any], expected: str) -> None:
    site = build_website()
    site = site.model_copy(
        update={"pages": [site.pages[0].model_copy(update=update), *site.pages[1:]]}
    )
    record = validation.validate_website(site, REQUESTED)
    assert record.valid is False
    assert expected in record.reason


def test_website_missing_is_invalid() -> None:
    record = validation.validate_website(None, REQUESTED)
    assert (record.valid, record.record_count) == (False, 0)


def test_www_redirect_to_the_same_site_is_valid() -> None:
    site = build_website()
    moved = site.pages[0].model_copy(update={"url": HttpUrl(f"{SITE.replace('://', '://www.')}/")})
    record = validation.validate_website(
        site.model_copy(update={"pages": [moved, *site.pages[1:]]}), REQUESTED
    )
    assert record.valid is True


def test_company_needs_name_postal_code_and_city() -> None:
    assert validation.validate_company(BUSINESS).valid is True
    for missing in ("name", "postal_code", "city"):
        incomplete = BUSINESS.model_copy(update={missing: None})
        assert validation.validate_company(incomplete).valid is False
    assert validation.validate_company(None).valid is False


def test_pagespeed_in_range_with_enough_runs_is_valid() -> None:
    assert validation.validate_pagespeed(_speed(), REQUESTED).valid is True


@pytest.mark.parametrize(
    ("lab", "expected"),
    [
        ({"runs": 2}, "Nur 2 von 5"),
        ({"lcp_ms": 120001}, "LCP"),
        ({"lcp_ms": -1}, "LCP"),
        ({"cls": 10.1}, "CLS"),
        ({"tbt_ms": 120001}, "TBT"),
    ],
)
def test_pagespeed_outside_limits_is_invalid(lab: dict[str, Any], expected: str) -> None:
    record = validation.validate_pagespeed(_speed(**lab), REQUESTED)
    assert record.valid is False
    assert expected in record.reason


def test_pagespeed_limits_are_inclusive_and_three_runs_suffice() -> None:
    edge = _speed(runs=3, lcp_ms=120000, cls=10, tbt_ms=0)
    assert validation.validate_pagespeed(edge, REQUESTED).valid is True


def test_pagespeed_of_another_address_is_invalid() -> None:
    record = validation.validate_pagespeed(_speed(), HttpUrl("https://anders.example/"))
    assert record.valid is False
    assert "angefragt war" in record.reason


def test_pagespeed_inp_out_of_range_is_invalid() -> None:
    snapshot = _speed()
    assert snapshot.field_data is not None
    broken = snapshot.model_copy(
        update={"field_data": snapshot.field_data.model_copy(update={"inp_ms": 200000})}
    )
    assert validation.validate_pagespeed(broken, REQUESTED).valid is False


def test_pagespeed_missing_is_invalid() -> None:
    assert validation.validate_pagespeed(None, REQUESTED).valid is False


def _search(results: list[SerpResult], phrases: list[str], **options: Any) -> Any:
    return validation.validate_search(
        validation.STEP_GOOGLE_SEARCH, "DataForSEO", results, phrases, **options
    )


def test_search_with_matching_echo_and_ascending_positions_is_valid() -> None:
    record = _search([_serp()], ["dachdecker münster"], expected_location_code=2276)
    assert record.valid is True
    assert record.record_count == 2


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"returned_keyword": "Elektriker Münster"}, "zurückgegeben"),
        ({"language_code": "en"}, "Sprache"),
        ({"location_code": 2840}, "Ort"),
        ({"organic": [], "local_pack": []}, "leer"),
        ({"organic": [(3, "a.de"), (1, "b.de")]}, "aufsteigend"),
        ({"organic": [(1, "a.de"), (1, "b.de")]}, "eindeutig"),
        ({"organic": [(1, "google.de")]}, "nur aus Adressen der Suchmaschine"),
        ({"organic": [(1, "www.google.com"), (2, "https://google.de/x")]}, "Suchmaschine"),
        ({"engine": "bing", "organic": [(1, "bing.com")]}, "Suchmaschine"),
    ],
)
def test_search_problems_are_named(update: dict[str, Any], expected: str) -> None:
    record = _search([_serp(**update)], ["dachdecker münster"], expected_location_code=2276)
    assert record.valid is False
    assert expected in record.reason


@pytest.mark.parametrize(
    ("engine", "organic"),
    [
        ("google", [(1, "google.de"), (2, "a.de")]),
        ("google", [(1, "bing.com")]),
        ("bing", [(1, "google.de")]),
    ],
)
def test_a_list_that_is_not_only_made_of_the_own_search_engine_is_valid(
    engine: str, organic: list[tuple[int, str]]
) -> None:
    record = _search([_serp(engine=engine, organic=organic)], ["dachdecker münster"])
    assert record.valid is True


def test_search_provider_status_other_than_ok_is_invalid() -> None:
    record = _search([_serp()], ["dachdecker münster"], status="40501")
    assert record.valid is False
    assert "40501" in record.reason


def test_a_list_of_only_maps_google_addresses_is_not_a_delivered_list() -> None:
    serp = _serp(organic=[(1, "maps.google.de"), (2, "www.maps.google.com")])
    record = _search([serp], ["dachdecker münster"])
    assert record.valid is False
    assert "Suchmaschine" in record.reason


def test_search_without_answer_for_one_phrase_stays_valid_for_the_others() -> None:
    record = _search([_serp()], ["dachdecker münster", "dachreparatur"])
    assert record.valid is True
    assert "1 von 2 Suchbegriffen" in record.reason
    assert "dachreparatur" in record.reason


def test_search_without_any_answer_for_every_phrase_is_invalid() -> None:
    record = _search([], ["dachdecker münster", "dachreparatur"])
    assert record.valid is False


def test_35_of_40_valid_lists_keep_the_step_valid_and_name_the_five() -> None:
    phrases = [f"dachdecker ort{number}" for number in range(40)]
    results = [_serp(phrase=phrase, returned_keyword=phrase) for phrase in phrases[:35]]
    results += [
        _serp(phrase=phrase, returned_keyword=phrase, organic=[(1, "google.de")])
        for phrase in phrases[35:]
    ]
    record = _search(results, phrases)
    assert record.valid is True
    assert "35 von 40 Suchbegriffen" in record.reason
    assert all(phrase in record.reason for phrase in phrases[35:])


def test_place_of_the_company_is_valid_by_postal_code_or_phone() -> None:
    assert validation.validate_place(build_place(), BUSINESS, "places_api").valid is True
    by_phone = build_place(formatted_address="Hauptstraße 5, Münster")
    assert validation.validate_place(by_phone, BUSINESS, "places_api").valid is True


@pytest.mark.parametrize(
    "place",
    [
        build_place(name="Bäckerei Schmitz", phone="+49 251 999999"),
        build_place(formatted_address="Hauptstraße 5, 80331 München", phone="+49 89 111111"),
    ],
)
def test_place_of_another_company_is_a_foreign_profile(place: Any) -> None:
    record = validation.validate_place(place, BUSINESS, "browser")
    assert record.valid is False
    assert "fremdes Profil" in record.reason


def test_place_needs_a_reference() -> None:
    assert validation.validate_place(build_place(), None, "browser").valid is False
    assert validation.validate_place(None, BUSINESS, "browser").valid is False


def test_a_searched_place_that_does_not_exist_is_a_valid_measurement() -> None:
    record = validation.validate_place(None, BUSINESS, "places_api", none_found=True)

    assert record.valid is True
    assert record.reason == validation.NO_PROFILE_REASON
    assert record.record_count == 0


@pytest.mark.parametrize(
    "facts",
    [
        None,
        BusinessFacts(name="Dachdeckerei Muster GmbH", city="Münster"),
        BusinessFacts(name="Dachdeckerei Muster GmbH", postal_code="48143"),
        BusinessFacts(city="Münster", street="Hauptstraße 5"),
    ],
)
def test_no_profile_is_not_claimed_when_the_business_data_of_the_search_was_incomplete(
    facts: BusinessFacts | None,
) -> None:
    record = validation.validate_place(None, facts, "places_api", none_found=True)

    assert record.valid is False
    assert record.reason == validation.INCOMPLETE_FACTS_REASON


@pytest.mark.parametrize(
    ("candidate", "same"),
    [
        ("Atelier Katja Karsch", True),
        ("Atelier Katja Karsch Keramik GmbH", True),
        ("Katja Karsch Atelier", True),
        ("Atelier", False),
        ("Karsch", False),
        ("Atelier Katja", True),
        ("", False),
    ],
)
def test_names_match_by_the_name_rule_of_the_one_identity_function(
    candidate: str, same: bool
) -> None:
    own = identity(name="Atelier Katja Karsch GmbH")
    assert same_business(own, identity(name=candidate)) is same


def test_listings_that_delivered_nothing_are_not_a_valid_measurement() -> None:
    def hit(directory: str, found: bool, delivered: int | None) -> DirectoryHit:
        return DirectoryHit(
            directory=directory,
            found=found,
            listing_url=None,
            name=None,
            address=None,
            phone=None,
            delivered=delivered,
        )

    nothing = [hit("Apple Business Connect", False, 0), hit("Bing Places", False, 0)]
    assert validation.validate_listings(nothing).valid is False
    answered = [hit("Apple Business Connect", False, 0), hit("Bing Places", False, 4)]
    assert validation.validate_listings(answered).valid is True
    assert validation.validate_listings([]).valid is False


def test_a_place_that_was_not_searched_stays_invalid() -> None:
    record = validation.validate_place(None, BUSINESS, "places_api")

    assert record.valid is False
    assert record.reason == "Es wurde kein Google-Profil gelesen"


def test_a_step_nobody_delivered_is_an_invalid_failed_record() -> None:
    record = validation.not_delivered(validation.STEP_LINKS, "DataForSEO", "no credentials")

    assert (record.step, record.supplier, record.technical_status) == (
        validation.STEP_LINKS,
        "DataForSEO",
        validation.FAILED_STATUS,
    )
    assert record.valid is False
    assert record.reason == "no credentials"
    assert record.record_count == 0


@pytest.mark.parametrize(
    ("step", "supplier"),
    [
        (validation.STEP_PLACE, "Google Places API"),
        (validation.STEP_PAGESPEED, "PageSpeed"),
        (validation.STEP_WEBSITE, "Website"),
    ],
)
def test_the_first_supplier_of_a_step_comes_from_the_route_file(step: str, supplier: str) -> None:
    assert validation.start_supplier(step) == supplier


def test_links_of_the_checked_host_are_valid() -> None:
    assert validation.validate_links(build_paid(), CUSTOMER_DOMAIN).valid is True


def test_links_of_another_host_or_failed_status_are_invalid() -> None:
    assert validation.validate_links(build_paid(), "anders.example").valid is False
    assert validation.validate_links(build_paid(), CUSTOMER_DOMAIN, status="40401").valid is False
    assert validation.validate_links(None, CUSTOMER_DOMAIN).valid is False


def test_answers_with_text_and_asked_question_are_valid() -> None:
    answers = build_answers()
    questions = [answer.question for answer in answers]
    record = validation.validate_assistants(answers, questions)
    assert record.valid is True
    assert record.record_count == len(answers)


def test_answers_without_text_or_with_foreign_question_are_invalid() -> None:
    answers = build_answers()
    questions = [answer.question for answer in answers]
    empty = [answers[0].model_copy(update={"answer_excerpt": " "}), *answers[1:]]
    assert validation.validate_assistants(empty, questions).valid is False
    assert validation.validate_assistants(answers, ["Eine andere Frage"]).valid is False
    assert validation.validate_assistants([], questions).valid is False


def test_route_text_joins_distinct_routes_in_order() -> None:
    assert validation.route_text(["dataforseo", "apify", "dataforseo", "browser"]) == (
        "DataForSEO → Apify → Browser"
    )


def test_check_steps_prefer_the_check_over_its_measurement() -> None:
    assert validation.steps_of_check("j7", "serp") == (validation.STEP_BING_SEARCH,)
    assert validation.steps_of_check("a4", "crawler") == (validation.STEP_WEBSITE,)
    assert validation.steps_of_check("c12", "paid") == (validation.STEP_LINKS,)
    assert validation.steps_of_check("j5", "paid") == (validation.STEP_LINKS,)
    assert validation.steps_of_check("j9", "serp") == (validation.STEP_GOOGLE_SEARCH,)
    assert validation.steps_of_check("j3", "places") == (validation.STEP_PLACE,)
    assert validation.steps_of_check("f4", "places") == (validation.STEP_PLACE,)


def test_pagespeed_in_the_older_flat_format_is_invalid_with_its_own_reason() -> None:
    older = PageSpeedSnapshot.model_validate(
        {
            "url": REQUESTED,
            "mobile_performance": 85,
            "desktop_performance": 97,
            "accessibility": 92,
            "lcp_ms": 2300,
            "cls": 0.06,
            "inp_ms": 150,
        }
    )
    record = validation.validate_pagespeed(older, REQUESTED)
    assert older.older_format is True
    assert record.valid is False
    assert record.reason == validation.OLDER_PAGESPEED_REASON


def test_pagespeed_without_lab_values_in_the_new_format_is_no_older_measurement() -> None:
    snapshot = _speed().model_copy(update={"lab": None})
    assert snapshot.older_format is False
    assert "keine Laborwerte" in validation.validate_pagespeed(snapshot, REQUESTED).reason
