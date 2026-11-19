from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from radarauditor.collectors.snapshot import (
    ListedBusiness,
    MapResultList,
    RawAudit,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.g_bewertungen import (
    owner_reply,
    parse_time,
    reply_data_available,
    reply_delay_days,
    review_text,
    run,
)

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "g1": {},
    "g2": {"partial_margin_stars": 0.3},
}
Edit = Callable[..., RawAudit]


@pytest.fixture
def raw_audit_full(raw_audit_full: RawAudit) -> RawAudit:
    return raw_audit_full.model_copy(update={"access": None})


def _review(
    day: str, rating: int = 5, text: str = "Gut", reply: object = None
) -> dict[str, object]:
    review: dict[str, object] = {"publishTime": f"{day}T10:00:00Z", "rating": rating, "text": text}
    if reply is not None:
        review["ownerReply"] = reply
    return review


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS[check_id])[0] is (
        CheckOutcome.NOT_MEASURABLE
    )


def test_review_helpers_are_defensive() -> None:
    assert parse_time("kaputt") is None
    assert parse_time(42) is None
    assert parse_time("2026-09-01T10:00:00") == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert review_text({"text": {"text": "Hallo"}}) == "Hallo"
    assert review_text({"originalText": "Original"}) == "Original"
    assert review_text({}) == ""
    assert owner_reply({"reviewReply": {"comment": "Danke"}}) == {"comment": "Danke"}
    assert owner_reply({"ownerResponse": ""}) is None
    reply = {"publishTime": "2026-09-04T10:00:00Z"}
    assert reply_delay_days(_review("2026-09-01", reply=reply)) == 3.0
    assert reply_delay_days(_review("2026-09-01", reply="Danke")) is None


def test_g1_review_count_against_competitor_average(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    outcome, measured = run("g1", with_place(raw_audit_full, review_count=11), {})
    assert outcome is CheckOutcome.FAILED
    assert measured["customer_reviews"] == 11
    assert measured["competitors"] == {
        "Dach Schmidt": 84,
        "Bedachungen Meier": 95,
        "Bau Krause": 60,
    }
    assert measured["competitor_average"] == 79.7
    assert run("g1", with_place(raw_audit_full, review_count=80), {})[0] is CheckOutcome.PASSED
    assert run("g1", with_place(raw_audit_full, review_count=79), {})[0] is CheckOutcome.FAILED
    alone = raw_audit_full.model_copy(update={"competitor_places": []})
    assert run("g1", alone, {})[1] == {"reason": "no_competitors"}


def _rivals_rated(raw: RawAudit, ratings: list[float | None]) -> RawAudit:
    rivals = [
        rival.model_copy(update={"rating": rating})
        for rival, rating in zip(raw.competitor_places, ratings, strict=True)
    ]
    return raw.model_copy(update={"competitor_places": rivals})


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (4.7, CheckOutcome.PASSED),
        (4.6, CheckOutcome.PASSED),
        (4.3, CheckOutcome.PARTIAL),
        (4.29, CheckOutcome.FAILED),
        (None, CheckOutcome.FAILED),
    ],
)
def test_g2_rating_against_the_median_of_the_competitors(
    raw_audit_full: RawAudit, with_place: Edit, rating: float | None, expected: CheckOutcome
) -> None:
    raw = _rivals_rated(with_place(raw_audit_full, rating=rating), [4.4, 4.6, 4.8])
    outcome, measured = run("g2", raw, THRESHOLDS["g2"])
    assert outcome is expected
    assert measured["rating"] == rating
    assert measured["median_rating"] == 4.6
    assert measured["partial_margin_stars"] == 0.3
    assert measured["compared_businesses"] == 3


def test_g2_uses_the_businesses_of_the_map_list_and_leaves_out_the_customer(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    listed = [
        ListedBusiness(name="Dach Schmidt", review_count=10, rating=4.0),
        ListedBusiness(name="Bedachungen Meier", review_count=20, rating=4.4),
        ListedBusiness(name="Dachdeckerei Muster GmbH", review_count=120, rating=1.0),
        ListedBusiness(name="Ohne Sterne", review_count=3, rating=None),
    ]
    results = MapResultList(phrase="Dachdecker Münster", businesses=listed, route="browser")
    raw = with_place(raw_audit_full, rating=4.2).model_copy(update={"map_results": results})
    outcome, measured = run("g2", raw, THRESHOLDS["g2"])
    assert outcome is CheckOutcome.PASSED
    assert measured["median_rating"] == 4.2
    assert measured["compared_businesses"] == 2
    assert measured["comparison_source"] == "maps_result_list"


def test_g2_a_business_whose_name_is_only_part_of_ours_is_a_competitor(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    listed = [
        ListedBusiness(name="Dachdeckerei", review_count=10, rating=3.0),
        ListedBusiness(name="Muster", review_count=20, rating=3.2),
        ListedBusiness(name="Dachdeckerei Muster Münster", review_count=120, rating=1.0),
    ]
    results = MapResultList(phrase="Dachdecker Münster", businesses=listed, route="browser")
    raw = with_place(raw_audit_full, rating=4.2).model_copy(update={"map_results": results})
    outcome, measured = run("g2", raw, THRESHOLDS["g2"])
    assert outcome is CheckOutcome.PASSED
    assert measured["compared_businesses"] == 2


def test_g2_is_not_measured_without_any_competitor_rating(
    raw_audit_full: RawAudit,
) -> None:
    raw = _rivals_rated(raw_audit_full, [None, None, None])
    assert run("g2", raw, THRESHOLDS["g2"])[1] == {"reason": "no_peer_ratings"}


ReviewFactory = Callable[[str, int, str | None], dict[str, object]]


def test_reply_data_available_counts_empty_reply_fields() -> None:
    assert reply_data_available([{"rating": 5, "ownerResponse": ""}]) is True
    assert reply_data_available([{"rating": 5, "text": "Gut"}]) is False
    assert reply_data_available([]) is False


def test_g1_names_the_routes_of_customer_and_competitors(raw_audit_full: RawAudit) -> None:
    rival = raw_audit_full.competitor_places[0].model_copy(
        update={"value_routes": {"review_count": "browser"}}
    )
    raw = raw_audit_full.model_copy(
        update={"competitor_places": [rival, *raw_audit_full.competitor_places[1:]]}
    )

    measured = run("g1", raw, {})[1]

    assert measured["routes"] == ["browser", "places_api"]


def _listed(name: str, reviews: int | None, **extra: object) -> ListedBusiness:
    return ListedBusiness(name=name, review_count=reviews, **extra)


def _with_list(raw: RawAudit, businesses: list[ListedBusiness], route: str = "browser") -> RawAudit:
    results = MapResultList(phrase="Dachdecker Münster", businesses=businesses, route=route)
    return raw.model_copy(update={"map_results": results})


def test_g1_compares_with_every_business_on_the_maps_list(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    businesses = [
        _listed("Dach Schmidt", 84),
        _listed("Dachdeckerei Muster", 120, is_customer=True),
        _listed("Bedachungen Meier", 95),
        _listed("Bau Krause", 60),
        _listed("Dach Neu", 0),
        _listed("Zimmerei Lüttke", 41),
    ]
    raw = _with_list(with_place(raw_audit_full, review_count=56), businesses)
    outcome, measured = run("g1", raw, THRESHOLDS["g1"])
    assert outcome is CheckOutcome.PASSED
    assert measured == {
        "customer_reviews": 56,
        "competitors": {
            "Dach Schmidt": 84,
            "Bedachungen Meier": 95,
            "Bau Krause": 60,
            "Dach Neu": 0,
            "Zimmerei Lüttke": 41,
        },
        "competitor_average": 56.0,
        "compared_businesses": 5,
        "comparison_source": "maps_result_list",
        "phrase": "Dachdecker Münster",
        "routes": ["browser", "places_api"],
    }
    below = _with_list(with_place(raw_audit_full, review_count=44), businesses)
    assert run("g1", below, THRESHOLDS["g1"])[0] is CheckOutcome.FAILED


def test_g1_recognises_the_customer_by_place_id_or_name(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    businesses = [
        _listed("Muster Dach", 500, place_id="place-customer"),
        _listed("Dachdeckerei Muster GmbH", 500),
        _listed("Dach Schmidt", 84),
        _listed("Ohne Zahl", None),
    ]
    raw = _with_list(raw_audit_full, businesses, route="places_api")
    outcome, measured = run("g1", raw, THRESHOLDS["g1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["competitors"] == {"Dach Schmidt": 84}
    assert measured["without_review_count"] == ["Ohne Zahl"]
    assert measured["route"] == "places_api"


def test_g1_keeps_two_businesses_of_the_same_name(raw_audit_full: RawAudit) -> None:
    businesses = [_listed("Dach Schmidt", 10), _listed("Dach Schmidt", 30)]
    measured = run("g1", _with_list(raw_audit_full, businesses), THRESHOLDS["g1"])[1]
    assert measured["competitors"] == {"Dach Schmidt": 10, "Dach Schmidt (2)": 30}
    assert measured["competitor_average"] == 20.0


def test_g1_list_without_others_or_counts_is_not_measurable(raw_audit_full: RawAudit) -> None:
    only_customer = _with_list(raw_audit_full, [_listed("Dachdeckerei Muster", 120)])
    assert run("g1", only_customer, THRESHOLDS["g1"]) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "no_listed_businesses", "phrase": "Dachdecker Münster"},
    )
    uncounted = _with_list(raw_audit_full, [_listed("Dach Schmidt", None)])
    assert run("g1", uncounted, THRESHOLDS["g1"]) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "no_review_counts", "without_review_count": ["Dach Schmidt"]},
    )
