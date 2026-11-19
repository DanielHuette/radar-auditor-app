from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID, uuid4, uuid5

import pytest

from radarauditor.collectors.snapshot import MeasurementRecord, RankingHistory, RawAudit
from radarauditor.db.app_settings import Source
from radarauditor.db.models import CheckOutcome, CheckResult
from radarauditor.errors import ScoringError
from radarauditor.feedback.events import NOT_BUILT_REASON
from radarauditor.scoring.catalog import AreaDefinition, Catalogue, CheckDefinition, find_check
from radarauditor.scoring.evaluator import (
    DAILY_CAP_REASON,
    MONTHLY_CAP_REASON,
    SKIPPED_REASON,
    area_module,
    area_scores,
    evaluate,
    invalid_measurement,
    is_built,
    measure_check,
    provider_scores,
    run_check,
    total_fraction,
    total_score,
)

AUDIT_ID = UUID("00000000-0000-4000-8000-000000000001")
ALLOWED_NOT_MEASURABLE = {
    "ownership_not_verifiable",
    "not_verifiable_from_outside",
    "ads_google_not_measured",
    "ads_meta_not_measured",
    "ads_account_not_connected",
    "manual",
    "needs_history",
    "paid_disabled",
    "no_access",
    "no_grid",
    "reply_data_unavailable",
}
SECTIONS_WITH_BUILT_CHECKS = {"google", "bing", "apple", "openai", "anthropic"}
SOURCE = "https://developers.google.com/search/docs/essentials"


def _definition(
    check_id: str, source: str = "crawler", kind: Literal["kriterium", "ergebnis"] = "kriterium"
) -> CheckDefinition:
    return CheckDefinition(
        id=check_id,
        area=check_id[0],
        name=check_id,
        why="Weil.",
        provider="google",
        kind=kind,
        source=SOURCE if kind == "kriterium" else "",
        measurement=source,
        requires_paid=source == "paid",
        requires_access=source == "access",
        thresholds={},
        customer_question="?",
        customer_impact=".",
    )


def _result(
    check_id: str,
    outcome: CheckOutcome,
    area: str = "google",
    weight: Decimal = Decimal(1),
) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area=area,
        outcome=outcome,
        measured={},
        weight=weight,
    )


def _check_weight(catalogue: Catalogue, check_id: str) -> Decimal:
    weight = find_check(catalogue, check_id).weight
    assert weight is not None
    return weight


def _area_weight(catalogue: Catalogue, area_id: str) -> Decimal:
    return next(area.weight for area in catalogue.areas if area.id == area_id)


def _catalogue_result(
    catalogue: Catalogue, check_id: str, outcome: CheckOutcome, area: str = "google"
) -> CheckResult:
    return _result(check_id, outcome, area, _check_weight(catalogue, check_id))


def test_full_audit_scores_every_section_with_a_built_check_at_100(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue)
    scores = area_scores(results, catalogue)
    assert set(scores) == SECTIONS_WITH_BUILT_CHECKS
    assert all(score == Decimal("100.00") for score in scores.values())
    assert total_score(results, catalogue) == Decimal("100.00")


def test_full_audit_measures_every_check_or_names_the_reason(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue)
    for result in results:
        assert is_built(result.check_id), result.check_id
        if result.outcome is CheckOutcome.NOT_MEASURABLE:
            assert result.measured["reason"] in ALLOWED_NOT_MEASURABLE, result.check_id
        else:
            assert result.outcome is CheckOutcome.PASSED, (result.check_id, result.measured)


def test_every_check_of_the_catalogue_has_a_scoring_function(catalogue: Catalogue) -> None:
    ids = [check.id for area in catalogue.areas for check in area.checks]
    assert len(ids) == 62
    assert [check_id for check_id in ids if not is_built(check_id)] == []


def test_a_check_without_a_scoring_function_is_not_measured(raw_audit_full: RawAudit) -> None:
    assert not is_built("a99")
    assert run_check(_definition("a99"), raw_audit_full) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": NOT_BUILT_REASON},
    )


def test_results_are_ordered_idempotent_and_skip_paid_and_access(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue)
    ids = [result.check_id for result in results]
    assert ids[:3] == ["a2", "a4", "a12"]
    by_id = {result.check_id: result for result in results}
    for check_id in ("c12", "i1", "j5", "m1", "m2", "p1", "p5", "p6"):
        row = by_id[check_id]
        assert row.outcome is CheckOutcome.NOT_MEASURABLE, check_id
        assert row.measured["reason"] in {"no_access", "paid_disabled"}, check_id
        assert row.weight == Decimal(0), check_id
    assert {r.check_id for r in results} == {
        check.id for area in catalogue.areas for check in area.checks
    }
    assert "j3" in ids
    assert results[0].id == uuid5(AUDIT_ID, "a2")
    assert [result.id for result in evaluate(AUDIT_ID, raw_audit_full, catalogue)] == [
        result.id for result in results
    ]


def test_check_weights_come_from_the_catalogue_whether_or_not_paid_is_enabled(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    without_paid = {r.check_id: r.weight for r in evaluate(AUDIT_ID, raw_audit_full, catalogue)}
    with_paid = {
        r.check_id: r.weight
        for r in evaluate(AUDIT_ID, raw_audit_full, catalogue, paid_enabled=True)
    }
    for check_id in ("a2", "a12", "a14", "h1", "k1"):
        assert without_paid[check_id] == _check_weight(catalogue, check_id), check_id
        assert with_paid[check_id] == _check_weight(catalogue, check_id), check_id
    assert with_paid["i1"] == _check_weight(catalogue, "i1")
    assert without_paid["a2"] != without_paid["a12"]
    assert without_paid["i1"] == Decimal(0)


def test_counted_check_weights_add_up_to_the_area_weight_when_everything_is_enabled(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue, paid_enabled=True, with_access=True)
    for area in catalogue.areas:
        total = sum((r.weight for r in results if r.area == area.id), Decimal(0))
        assert total == area.weight, area.id
    assert sum((r.weight for r in results), Decimal(0)) == Decimal(100)


def test_info_checks_carry_no_weight(raw_audit_full: RawAudit, catalogue: Catalogue) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue, paid_enabled=True, with_access=True)
    info_weights = {r.weight for r in results if r.area == "ads"}
    assert len([r for r in results if r.area == "ads"]) == 6
    assert info_weights == {Decimal(0)}


def test_paid_and_access_checks_are_measured_when_enabled(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue, paid_enabled=True, with_access=True)
    by_id = {r.check_id: r for r in results}
    for check_id in ("c12", "i1", "j5", "m1", "m2"):
        assert by_id[check_id].outcome is CheckOutcome.PASSED, (check_id, by_id[check_id])
    assert by_id["j3"].measured == {"reason": "no_grid"}
    assert area_scores(results, catalogue)["google"] == Decimal("100.00")
    assert total_score(results, catalogue) == Decimal("100.00")


def test_paid_and_access_checks_without_data_are_not_measurable(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    raw = raw_audit_full.model_copy(update={"paid": None, "access": None})
    results = {
        r.check_id: r
        for r in evaluate(AUDIT_ID, raw, catalogue, paid_enabled=True, with_access=True)
    }
    assert results["i1"].measured == {"reason": "no_paid_data"}
    assert results["m1"].measured == {"reason": "no_access"}
    assert results["m2"].measured == {"reason": "no_access"}
    assert total_score(list(results.values()), catalogue) == Decimal("100.00")


def test_impact_results_without_a_catalogue_weight_do_not_move_the_google_score(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    assert raw_audit_full.access is not None
    access = raw_audit_full.access.model_copy(
        update={"analytics": None, "owner_reviews": [], "search_queries": []}
    )
    raw = raw_audit_full.model_copy(update={"access": access})
    results = evaluate(AUDIT_ID, raw, catalogue, with_access=True)
    by_id = {r.check_id: r for r in results}
    assert {by_id["m1"].outcome, by_id["m2"].outcome} != {CheckOutcome.PASSED}
    assert by_id["m1"].weight == by_id["m2"].weight == _check_weight(catalogue, "m1") == 0
    assert area_scores(results, catalogue)["google"] == Decimal("100.00")


def test_minimal_audit_with_a_missing_profile_scores_nothing(
    raw_audit_minimal: RawAudit, catalogue: Catalogue
) -> None:
    searched = raw_audit_minimal.model_copy(update={"profile_missing": True})
    results = evaluate(AUDIT_ID, searched, catalogue)
    measurable = [r for r in results if r.outcome in (CheckOutcome.PASSED, CheckOutcome.FAILED)]
    assert measurable == []
    assert area_scores(results, catalogue) == {}
    with pytest.raises(ScoringError):
        total_score(results, catalogue)


def test_area_and_total_arithmetic(catalogue: Catalogue) -> None:
    results = [
        _catalogue_result(catalogue, "a2", CheckOutcome.PASSED),
        _catalogue_result(catalogue, "a4", CheckOutcome.PARTIAL),
        _catalogue_result(catalogue, "a7", CheckOutcome.FAILED),
        _catalogue_result(catalogue, "a11", CheckOutcome.NOT_MEASURABLE),
        _catalogue_result(catalogue, "a14", CheckOutcome.PASSED, "bing"),
        _catalogue_result(catalogue, "b3", CheckOutcome.FAILED, "bing"),
        _catalogue_result(catalogue, "b4", CheckOutcome.FAILED, "bing"),
        _catalogue_result(catalogue, "b10", CheckOutcome.NOT_MEASURABLE, "bing"),
    ]
    assert area_scores(results, catalogue) == {
        "google": Decimal("50.00"),
        "bing": Decimal("33.33"),
    }
    google = _area_weight(catalogue, "google")
    bing = _area_weight(catalogue, "bing")
    expected = (google * Decimal("0.5") + bing * Decimal(1) / 3) / (google + bing) * 100
    assert total_score(results, catalogue) == expected.quantize(Decimal("0.01"), ROUND_HALF_UP)


def test_checks_with_a_higher_weight_move_the_area_score_further(catalogue: Catalogue) -> None:
    heavy = _check_weight(catalogue, "a2")
    light = _check_weight(catalogue, "a12")
    assert heavy > light
    heavy_passes = [
        _catalogue_result(catalogue, "a2", CheckOutcome.PASSED),
        _catalogue_result(catalogue, "a12", CheckOutcome.FAILED),
    ]
    light_passes = [
        _catalogue_result(catalogue, "a2", CheckOutcome.FAILED),
        _catalogue_result(catalogue, "a12", CheckOutcome.PASSED),
    ]
    expected_heavy = (heavy / (heavy + light) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    expected_light = (light / (heavy + light) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    assert area_scores(heavy_passes, catalogue) == {"google": expected_heavy}
    assert area_scores(light_passes, catalogue) == {"google": expected_light}
    assert expected_heavy > Decimal(50) > expected_light


def test_area_score_weights_every_check_by_its_own_weight(catalogue: Catalogue) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED, weight=Decimal(3)),
        _result("a4", CheckOutcome.FAILED, weight=Decimal(1)),
        _result("a7", CheckOutcome.PARTIAL, weight=Decimal(4)),
    ]
    assert area_scores(results, catalogue) == {"google": Decimal("62.50")}


def test_area_score_falls_back_to_the_plain_mean_when_all_weights_are_zero(
    catalogue: Catalogue,
) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED, weight=Decimal(0)),
        _result("a4", CheckOutcome.FAILED, weight=Decimal(0)),
    ]
    assert area_scores(results, catalogue) == {"google": Decimal("50.00")}


def test_skipped_checks_do_not_count(catalogue: Catalogue) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED),
        _result("a4", CheckOutcome.SKIPPED),
        _result("b3", CheckOutcome.SKIPPED, "bing"),
    ]
    assert area_scores(results, catalogue) == {"google": Decimal("100.00")}


def test_info_checks_stay_out_of_every_score(catalogue: Catalogue) -> None:
    results = [
        _catalogue_result(catalogue, "a2", CheckOutcome.FAILED),
        _catalogue_result(catalogue, "j1", CheckOutcome.PARTIAL),
        _catalogue_result(catalogue, "p3", CheckOutcome.PASSED, "ads"),
        _catalogue_result(catalogue, "p4", CheckOutcome.PASSED, "ads"),
        _catalogue_result(catalogue, "k1", CheckOutcome.PASSED, "openai"),
        _catalogue_result(catalogue, "j3", CheckOutcome.PASSED),
    ]
    assert area_scores(results, catalogue) == {"google": Decimal("13.63")}
    assert provider_scores(results, catalogue) == {"google": Decimal("13.63")}
    a2 = _check_weight(catalogue, "a2")
    j1 = _check_weight(catalogue, "j1")
    google_fraction = j1 * Decimal("0.5") / (a2 + j1)
    expected = google_fraction * 100
    assert total_score(results, catalogue) == expected.quantize(Decimal("0.01"), ROUND_HALF_UP)


def test_results_count_like_criteria_in_the_provider_score(catalogue: Catalogue) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED),
        _result("j1", CheckOutcome.PASSED),
        _result("g2", CheckOutcome.FAILED),
        _result("j7", CheckOutcome.PASSED, "bing"),
    ]
    assert provider_scores(results, catalogue) == {
        "google": Decimal("66.67"),
        "bing": Decimal("100.00"),
    }


def test_provider_score_weights_every_check_by_its_own_weight(catalogue: Catalogue) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED, weight=Decimal(3)),
        _result("a4", CheckOutcome.FAILED, weight=Decimal(1)),
        _result("a7", CheckOutcome.PARTIAL, weight=Decimal(4)),
        _result("j7", CheckOutcome.PASSED, "bing", Decimal(2)),
        _result("b3", CheckOutcome.FAILED, "bing", Decimal(6)),
    ]
    assert provider_scores(results, catalogue) == {
        "google": Decimal("62.50"),
        "bing": Decimal("25.00"),
    }


def test_provider_score_falls_back_to_the_plain_mean_when_all_weights_are_zero(
    catalogue: Catalogue,
) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED, weight=Decimal(0)),
        _result("a4", CheckOutcome.FAILED, weight=Decimal(0)),
    ]
    assert provider_scores(results, catalogue) == {"google": Decimal("50.00")}


def test_only_unmeasurable_results_raise(catalogue: Catalogue) -> None:
    results = [_result("a2", CheckOutcome.NOT_MEASURABLE)]
    assert area_scores(results, catalogue) == {}
    with pytest.raises(ScoringError, match="No weighted area"):
        total_score(results, catalogue)
    assert total_fraction(results, catalogue) is None


def test_only_info_results_raise(catalogue: Catalogue) -> None:
    results = [_result("p3", CheckOutcome.PASSED, "ads")]
    assert area_scores(results, catalogue) == {}
    with pytest.raises(ScoringError, match="No weighted area"):
        total_score(results, catalogue)
    assert total_fraction(results, catalogue) is None


def test_total_fraction_is_the_unrounded_total(catalogue: Catalogue) -> None:
    results = [_result("a2", CheckOutcome.PASSED), _result("a14", CheckOutcome.FAILED, "bing")]

    fraction = total_fraction(results, catalogue)

    assert fraction is not None
    assert total_score(results, catalogue) == (fraction * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert Decimal(0) < fraction < Decimal(1)


def test_run_check_measures_every_check_itself(raw_audit_full: RawAudit) -> None:
    outcome, measured = run_check(_definition("c4", "agent"), raw_audit_full)
    assert outcome is CheckOutcome.PASSED
    assert "reason" not in measured


def test_run_check_handles_access_and_paid_sources(raw_audit_full: RawAudit) -> None:
    raw = raw_audit_full.model_copy(update={"paid": None, "access": None})
    assert run_check(_definition("m1", "access"), raw)[1] == {"reason": "no_access"}
    assert run_check(_definition("i1", "paid"), raw)[1] == {"reason": "no_paid_data"}
    assert run_check(_definition("m1", "access"), raw_audit_full)[0] is CheckOutcome.PASSED
    assert run_check(_definition("i1", "paid"), raw_audit_full)[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", ["a99", "z9", "b99"])
def test_run_check_without_a_function_is_not_measurable(
    raw_audit_full: RawAudit, check_id: str
) -> None:
    assert run_check(_definition(check_id), raw_audit_full) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": NOT_BUILT_REASON},
    )


def test_area_without_module_raises() -> None:
    with pytest.raises(ScoringError, match="no check module"):
        area_module("z")


def test_invalid_module_result_raises(
    raw_audit_full: RawAudit, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = area_module("a")
    monkeypatch.setattr(module, "run", lambda *_: ("passed", {}))
    with pytest.raises(ScoringError, match="invalid result"):
        run_check(_definition("a2", "pagespeed"), raw_audit_full)


def test_evaluate_with_custom_catalogue(raw_audit_minimal: RawAudit) -> None:
    catalogue = Catalogue(
        areas=[
            AreaDefinition(id="a", name="A", weight=70, checks=[_definition("a4")]),
            AreaDefinition(id="f", name="F", weight=30, checks=[_definition("f1", "places")]),
        ]
    )
    searched = raw_audit_minimal.model_copy(update={"profile_missing": True})
    results = evaluate(AUDIT_ID, searched, catalogue)
    assert [r.weight for r in results] == [Decimal("70.000"), Decimal("30.000")]
    assert [r.outcome for r in results] == [CheckOutcome.NOT_MEASURABLE] * 2


def test_disabled_checks_are_skipped_without_weight(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    results = evaluate(AUDIT_ID, raw_audit_full, catalogue, disabled_checks=frozenset({"a2"}))
    by_id = {r.check_id: r for r in results}

    assert by_id["a2"].outcome is CheckOutcome.SKIPPED
    assert by_id["a2"].measured == {"reason": SKIPPED_REASON}
    assert by_id["a2"].weight == Decimal(0)
    full = {r.check_id: r.weight for r in evaluate(AUDIT_ID, raw_audit_full, catalogue)}
    assert by_id["a4"].weight == full["a4"]
    assert total_score(results, catalogue) == Decimal("100.00")


def test_capped_source_makes_its_checks_not_measurable(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    raw = raw_audit_full.model_copy(update={"capped_sources": [Source.GOOGLE_SEARCH]})
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, raw, catalogue)}

    assert by_id["j1"].outcome is CheckOutcome.NOT_MEASURABLE
    assert by_id["j1"].measured == {"reason": DAILY_CAP_REASON, "sources": ["google_search"]}
    assert by_id["a2"].outcome is CheckOutcome.PASSED


def test_monthly_cost_cap_makes_every_dataforseo_check_not_measurable(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    raw = raw_audit_full.model_copy(
        update={
            "cost_capped_sources": [Source.DATAFORSEO],
            "capped_sources": [Source.DATAFORSEO],
        }
    )
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, raw, catalogue, paid_enabled=True)}

    for check_id in ("c12", "i1", "j5"):
        assert by_id[check_id].outcome is CheckOutcome.NOT_MEASURABLE, check_id
        assert by_id[check_id].measured == {
            "reason": MONTHLY_CAP_REASON,
            "sources": ["dataforseo"],
        }
    assert by_id["j1"].outcome is CheckOutcome.PASSED


def test_monthly_cost_cap_of_chatgpt_keeps_the_answers_of_the_free_routes(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    capped = raw_audit_full.model_copy(update={"cost_capped_sources": [Source.CHATGPT]})
    expected = {r.check_id: r for r in evaluate(AUDIT_ID, raw_audit_full, catalogue)}
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, capped, catalogue)}

    for check_id in ("k1", "k2", "k6", "k7"):
        assert by_id[check_id].outcome is expected[check_id].outcome, check_id
        assert by_id[check_id].measured == expected[check_id].measured, check_id


def test_paid_checks_name_the_dataforseo_route(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    raw = raw_audit_full.model_copy(update={"domain_first_capture": date(2012, 1, 1)})
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, raw, catalogue, paid_enabled=True)}

    for check_id in ("c12", "i1", "j5"):
        assert by_id[check_id].measured["route"] == "dataforseo", check_id
    assert "route" not in by_id["j1"].measured
    assert "routes" not in by_id["j1"].measured


def test_search_checks_list_the_route_per_phrase_after_a_fallback(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    google = [s for s in raw_audit_full.serps if s.engine == "google" and s.vertical == "web"]
    first = google[0]
    serps = [
        s.model_copy(update={"route": "dataforseo"}) if s is first else s
        for s in raw_audit_full.serps
    ]
    raw = raw_audit_full.model_copy(update={"serps": serps})
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, raw, catalogue)}

    routes = by_id["j1"].measured["routes"]
    assert isinstance(routes, dict)
    assert routes[first.phrase] == "dataforseo"
    assert set(routes.values()) <= {"browser", "dataforseo"}
    assert "routes" not in by_id["j7"].measured


def test_search_checks_list_the_route_per_phrase_after_an_apify_read(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    google = [s for s in raw_audit_full.serps if s.engine == "google" and s.vertical == "web"]
    serps = [
        s.model_copy(update={"route": "apify"}) if s is google[0] else s
        for s in raw_audit_full.serps
    ]
    raw = raw_audit_full.model_copy(update={"serps": serps})
    by_id = {r.check_id: r for r in evaluate(AUDIT_ID, raw, catalogue)}

    routes = by_id["j1"].measured["routes"]
    assert isinstance(routes, dict)
    assert routes[google[0].phrase] == "apify"
    assert "routes" not in by_id["j7"].measured


def test_a_capped_assistant_leaves_the_checks_of_the_others_alone(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    raw = raw_audit_full.model_copy(update={"capped_sources": [Source.GOOGLE_AI, Source.CLAUDE]})
    k1 = find_check(catalogue, "k1")

    assert measure_check(k1, raw) == run_check(k1, raw_audit_full)


@pytest.mark.parametrize(("check_id", "capped"), [("k1", "chatgpt"), ("k6", "claude")])
def test_a_capped_assistant_makes_its_own_checks_not_measurable(
    raw_audit_full: RawAudit, catalogue: Catalogue, check_id: str, capped: str
) -> None:
    raw = raw_audit_full.model_copy(update={"capped_sources": [Source(capped)]})

    outcome, measured = measure_check(find_check(catalogue, check_id), raw)

    assert outcome is CheckOutcome.NOT_MEASURABLE
    assert measured == {"reason": DAILY_CAP_REASON, "sources": [capped]}


@pytest.mark.parametrize("check_id", ["f2", "f4", "f5", "f7", "f8", "f13", "g1", "g2", "j3"])
def test_profile_checks_are_not_measured_when_the_profile_is_not_provable(
    raw_audit_minimal: RawAudit, check_id: str
) -> None:
    searched = raw_audit_minimal.model_copy(update={"profile_missing": True})

    assert run_check(_definition(check_id, "places"), searched) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "profile_not_provable", "profile_found": False},
    )


def test_f1_and_the_others_give_the_same_reason_when_the_profile_is_not_provable(
    raw_audit_minimal: RawAudit,
) -> None:
    searched = raw_audit_minimal.model_copy(update={"profile_missing": True})

    assert run_check(_definition("f1", "places"), searched) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "profile_not_provable", "profile_found": False},
    )


def test_the_map_check_of_the_profile_is_not_applicable_without_a_location(
    raw_audit_minimal: RawAudit,
) -> None:
    searched = raw_audit_minimal.model_copy(update={"has_location": False})

    assert run_check(_definition("j3", "places"), searched) == (
        CheckOutcome.NOT_APPLICABLE,
        {"reason": "no_location"},
    )


def test_a_failed_profile_search_makes_the_profile_checks_not_measurable(
    raw_audit_minimal: RawAudit,
) -> None:
    for check_id in ("f1", "f2", "g1", "j3"):
        outcome, _ = run_check(_definition(check_id, "places"), raw_audit_minimal)
        assert outcome is not CheckOutcome.NOT_APPLICABLE, check_id


def test_every_result_names_the_provider_of_its_check(
    raw_audit_full: RawAudit, catalogue: Catalogue
) -> None:
    providers = {check.id: check.provider for area in catalogue.areas for check in area.checks}

    results = evaluate(AUDIT_ID, raw_audit_full, catalogue)

    assert results
    assert all(result.provider == providers[result.check_id] for result in results)


def test_profile_checks_are_not_applicable_without_a_location(
    raw_audit_minimal: RawAudit,
) -> None:
    searched = raw_audit_minimal.model_copy(update={"has_location": False})

    assert run_check(_definition("g1", "places"), searched) == (
        CheckOutcome.NOT_APPLICABLE,
        {"reason": "no_location"},
    )
    assert run_check(_definition("f1", "places"), searched) == (
        CheckOutcome.NOT_APPLICABLE,
        {"reason": "no_location"},
    )


def _record(step: str, valid: bool) -> MeasurementRecord:
    return MeasurementRecord(
        step=step,
        supplier="Test",
        route="test",
        measured_at=datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        technical_status="ok",
        record_count=1,
        valid=valid,
        reason="Alle Plausibilitätsprüfungen bestanden" if valid else "ungültig",
    )


def test_j9_at_the_first_audit_is_planned_not_measurable_and_not_a_failed_measurement(
    raw_audit_minimal: RawAudit,
) -> None:
    development = _definition("j9", "serp", kind="ergebnis")
    search_invalid = raw_audit_minimal.model_copy(
        update={"measurements": [_record("search_google", False)]}
    )

    assert invalid_measurement(development, search_invalid) is None
    assert run_check(development, search_invalid) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "needs_history"},
    )


def test_j9_is_blocked_by_an_invalid_google_search_and_not_by_invalid_links(
    raw_audit_full: RawAudit,
) -> None:
    development = _definition("j9", "serp", kind="ergebnis")
    history = RankingHistory(
        previous_audit_id=uuid4(),
        previous_requested_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        changes=[],
    )
    only_links_invalid = raw_audit_full.model_copy(
        update={
            "ranking_history": history,
            "measurements": [_record("links", False), _record("search_google", True)],
        }
    )
    search_invalid = raw_audit_full.model_copy(
        update={
            "ranking_history": history,
            "measurements": [_record("links", True), _record("search_google", False)],
        }
    )

    assert invalid_measurement(development, only_links_invalid) is None
    blocked = invalid_measurement(development, search_invalid)
    assert blocked is not None
    assert blocked[1]["step"] == "search_google"
