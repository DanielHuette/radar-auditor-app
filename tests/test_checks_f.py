from collections.abc import Callable

import pytest

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.f_google_profil import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "f1": {},
    "f2": {},
    "f4": {},
    "f5": {},
    "f7": {},
    "f8": {},
    "f13": {},
}
OTHER_THAN_F1 = [key for key in THRESHOLDS if key != "f1"]
Edit = Callable[..., RawAudit]


@pytest.mark.parametrize("check_id", OTHER_THAN_F1)
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", OTHER_THAN_F1)
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS[check_id])[1] == {"reason": "no_place"}


def test_f1_a_found_profile_cannot_be_checked_for_ownership(
    raw_audit_full: RawAudit,
) -> None:
    outcome, measured = run("f1", raw_audit_full, {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {
        "reason": "ownership_not_verifiable",
        "profile_found": True,
        "business_status": "OPERATIONAL",
    }


@pytest.mark.parametrize("check_id", ["f4", "f5", "f13"])
def test_a_field_the_route_does_not_deliver_is_not_measured(
    raw_audit_full: RawAudit, with_place: Edit, check_id: str
) -> None:
    emptied = {"f4": "description", "f5": "services", "f13": "attributes"}[check_id]
    raw = with_place(raw_audit_full, **{emptied: None})
    outcome, measured = run(check_id, raw, {})
    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {"reason": "field_not_delivered", "field": emptied}


@pytest.mark.parametrize("check_id", ["f4", "f5", "f13"])
def test_an_empty_field_of_a_route_that_delivers_it_fails(
    raw_audit_full: RawAudit, with_place: Edit, check_id: str
) -> None:
    emptied = {"f4": "description", "f5": "services", "f13": "attributes"}[check_id]
    raw = with_place(raw_audit_full, **{emptied: None}, value_routes={emptied: "browser"})
    assert run(check_id, raw, {})[0] is CheckOutcome.FAILED


def test_f4_names_the_length_of_the_description(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("f4", raw_audit_full, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["description_length"] > 0


@pytest.mark.parametrize(
    ("check_id", "field", "filled", "empty", "count"),
    [
        ("f5", "services", ["Flachdach"], [], "entries"),
        ("f13", "attributes", ["Barzahlung"], [], "entries"),
    ],
)
def test_a_list_with_one_entry_passes_and_an_empty_list_fails(
    raw_audit_full: RawAudit,
    with_place: Edit,
    check_id: str,
    field: str,
    filled: list[str],
    empty: list[str],
    count: str,
) -> None:
    delivered = {field: "browser"}
    one = run(check_id, with_place(raw_audit_full, **{field: filled}, value_routes=delivered), {})
    none = run(check_id, with_place(raw_audit_full, **{field: empty}, value_routes=delivered), {})
    assert one[0] is CheckOutcome.PASSED
    assert one[1][count] == 1
    assert none[0] is CheckOutcome.FAILED


@pytest.mark.parametrize(
    ("hours", "expected"), [(True, CheckOutcome.PASSED), (False, CheckOutcome.FAILED)]
)
def test_f7_opening_hours(
    raw_audit_full: RawAudit, with_place: Edit, hours: bool, expected: CheckOutcome
) -> None:
    outcome, measured = run("f7", with_place(raw_audit_full, has_opening_hours=hours), {})
    assert outcome is expected
    assert measured["special_hours"] == "not_determinable"


@pytest.mark.parametrize(
    ("photos", "expected"), [(1, CheckOutcome.PASSED), (0, CheckOutcome.FAILED)]
)
def test_f8_photos(
    raw_audit_full: RawAudit, with_place: Edit, photos: int, expected: CheckOutcome
) -> None:
    outcome, measured = run("f8", with_place(raw_audit_full, photo_count=photos), {})
    assert outcome is expected
    assert measured["photo_count"] == photos


def test_f2_lists_only_competitor_categories_of_the_same_route(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    browser_only = with_place(
        raw_audit_full, primary_type="Dachdecker", value_routes={"primary_type": "browser"}
    )
    outcome, measured = run("f2", browser_only, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["competitor_types"] == {}


def test_f1_is_not_provable_when_the_search_found_no_profile(raw_audit_minimal: RawAudit) -> None:
    searched = raw_audit_minimal.model_copy(
        update={
            "profile_missing": True,
            "warnings": ["places: no Google Business Profile found for Muster"],
        }
    )
    assert run("f1", searched, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "profile_not_provable", "profile_found": False},
    )


def test_f1_is_not_measurable_when_the_profile_search_did_not_finish(
    raw_audit_minimal: RawAudit,
) -> None:
    assert run("f1", raw_audit_minimal, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "collector_failed"},
    )
    failed = raw_audit_minimal.model_copy(update={"warnings": ["places: HTTP 503"]})
    assert run("f1", failed, {})[1] == {"reason": "collector_failed"}


def test_f2_passes_when_a_main_category_is_set_whatever_the_competitors_have(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    other = with_place(raw_audit_full, primary_type="plumber")
    outcome, measured = run("f2", other, {})
    assert outcome is CheckOutcome.PASSED
    assert measured["primary_type"] == "plumber"
    assert measured["competitor_types"]["Bau Krause"] == "general_contractor"
    alone = raw_audit_full.model_copy(update={"competitor_places": []})
    assert run("f2", alone, {})[0] is CheckOutcome.PASSED


def test_f2_fails_without_a_main_category_and_says_so_when_the_field_was_not_delivered(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    empty = with_place(raw_audit_full, primary_type=None)
    assert run("f2", empty, {})[0] is CheckOutcome.FAILED
    undelivered = with_place(raw_audit_full, primary_type=None, value_routes={"name": "browser"})
    assert run("f2", undelivered, {})[1]["reason"] == "field_not_delivered"


def test_f7_and_f8_say_so_when_the_source_did_not_deliver_the_field(
    raw_audit_full: RawAudit, with_place: Edit
) -> None:
    route = {"value_routes": {"name": "browser"}}
    hours = with_place(raw_audit_full, has_opening_hours=False, **route)
    photos = with_place(raw_audit_full, photo_count=0, **route)
    assert run("f7", hours, {})[1] == {
        "reason": "field_not_delivered",
        "field": "has_opening_hours",
    }
    assert run("f8", photos, {})[1] == {"reason": "field_not_delivered", "field": "photo_count"}
    assert run("f7", with_place(raw_audit_full, has_opening_hours=False), {})[0] is (
        CheckOutcome.FAILED
    )
