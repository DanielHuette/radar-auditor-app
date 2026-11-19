import pytest

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.p_werbung import CHECKS, run

NOT_BUILT = {
    "p5": "ads_account_not_connected",
    "p6": "ads_account_not_connected",
}
ADS = ["konkurrent-dach.de", "www.dachdeckerei-muster.de", "dach-schmidt.de"]


def _with_ads(raw: RawAudit, ads: list[str]) -> RawAudit:
    return raw.model_copy(
        update={"serps": [serp.model_copy(update={"ads": ads}) for serp in raw.serps]}
    )


def test_the_area_has_exactly_the_six_checks() -> None:
    assert sorted(CHECKS) == ["p1", "p2", "p3", "p4", "p5", "p6"]


@pytest.mark.parametrize("check_id", sorted(NOT_BUILT))
def test_checks_without_a_data_source_are_not_measured_and_say_why(
    raw_audit_full: RawAudit, check_id: str
) -> None:
    assert run(check_id, raw_audit_full, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": NOT_BUILT[check_id]},
    )


@pytest.mark.parametrize("check_id", ["p3", "p4"])
def test_minimal_audit_is_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "no_website"},
    )


def test_p3_names_the_own_ad_and_the_competitors_per_search_term(
    raw_audit_full: RawAudit,
) -> None:
    outcome, measured = run("p3", _with_ads(raw_audit_full, ADS), {})
    assert outcome is CheckOutcome.PASSED
    entry = measured["phrases"]["Dachsanierung Münster"]
    assert entry == {
        "customer_shown": True,
        "customer_position": 2,
        "competitors": ["konkurrent-dach.de", "dach-schmidt.de"],
        "paid_entries": 3,
    }
    assert measured["phrases_with_own_ad"] == measured["phrases_total"]


def test_p3_leaves_out_the_search_for_the_business_name(raw_audit_full: RawAudit) -> None:
    measured = run("p3", _with_ads(raw_audit_full, ADS), {})[1]
    assert "Dachdeckerei Muster" not in measured["phrases"]


def test_p3_without_ads_reports_that_the_customer_is_not_shown(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("p3", _with_ads(raw_audit_full, []), {})
    assert outcome is CheckOutcome.PASSED
    assert measured["phrases_with_own_ad"] == 0
    assert measured["phrases"]["Dachsanierung Münster"]["customer_position"] is None


def test_p3_without_any_google_search_is_not_measured(raw_audit_full: RawAudit) -> None:
    raw = raw_audit_full.model_copy(update={"serps": []})
    assert run("p3", raw, {}) == (CheckOutcome.NOT_MEASURABLE, {"reason": "no_serps"})


def test_p4_names_the_competitors_that_pay_for_the_brand_name(raw_audit_full: RawAudit) -> None:
    outcome, measured = run("p4", _with_ads(raw_audit_full, ADS), {})
    assert outcome is CheckOutcome.PASSED
    assert measured["competitors_on_brand"] == ["konkurrent-dach.de", "dach-schmidt.de"]
    assert measured["competitors_on_brand_count"] == 2


def test_p4_without_the_search_for_the_business_name_is_not_measured(
    raw_audit_full: RawAudit,
) -> None:
    serps = [serp for serp in raw_audit_full.serps if serp.phrase != "Dachdeckerei Muster"]
    raw = raw_audit_full.model_copy(update={"serps": serps})
    assert run("p4", raw, {}) == (CheckOutcome.NOT_MEASURABLE, {"reason": "no_brand_search"})
