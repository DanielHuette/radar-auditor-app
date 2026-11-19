from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml
from conftest import AUDIT_ID, REPORT_ROOT
from pydantic import ValidationError

from radarauditor.config import DEFAULT_CONTROL_MODEL, Settings
from radarauditor.db.models import CheckOutcome, CheckResult, ControlState, Finding
from radarauditor.reporting import report
from radarauditor.scoring.catalog import Catalogue
from radarauditor.scoring.evaluator import total_score


def _result(
    check_id: str,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    ticks: tuple[bool | None, bool | None, bool | None] = (True, True, True),
    state: ControlState = ControlState.DONE,
    reasons: tuple[str | None, str | None, str | None] = (None, None, None),
) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="google",
        outcome=outcome,
        measured={},
        weight=Decimal("1.000"),
        control_state=state,
        verdict_collected_ok=ticks[0],
        verdict_data_ok=ticks[1],
        verdict_evaluation_ok=ticks[2],
        verdict_collected_reason=reasons[0],
        verdict_data_reason=reasons[1],
        verdict_evaluation_reason=reasons[2],
    )


def _finding(check_id: str) -> Finding:
    return Finding(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        severity=2,
        title=f"Befund {check_id}",
        explanation="e",
        customer_impact="i",
        fix="f",
        fix_payload=None,
    )


@pytest.mark.parametrize(
    ("ticks", "confirmed"),
    [
        ((True, True, True), True),
        ((False, None, None), False),
        ((True, False, True), False),
        ((True, True, False), False),
        ((None, None, None), False),
        ((True, True, None), False),
    ],
)
def test_only_checks_with_three_ticks_give_recommendations(
    catalogue: Catalogue, ticks: tuple[bool | None, bool | None, bool | None], confirmed: bool
) -> None:
    lines = report.finding_lines([_finding("a4")], [_result("a4", ticks=ticks)], catalogue)

    assert [line.check_id for line in lines] == (["a4"] if confirmed else [])


def test_findings_of_earlier_audits_can_be_listed_without_verdicts(catalogue: Catalogue) -> None:
    previous = _result("a4", ticks=(None, None, None), state=ControlState.OPEN)

    lines = report.finding_lines([_finding("a4")], [previous], catalogue, confirmed_only=False)

    assert [line.check_id for line in lines] == ["a4"]


def test_the_report_shows_the_three_verdicts_and_the_reason_of_the_first_cross(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    results = [
        _result("a4"),
        _result("a7", ticks=(True, False, None), reasons=(None, "falscher Betrieb", None)),
        _result("a2", state=ControlState.OPEN, ticks=(None, None, None)),
    ]

    lines = report.verdict_lines(
        results, catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert [(line.check_id, line.collected, line.data, line.evaluation) for line in lines] == [
        ("a4", True, True, True),
        ("a7", True, False, None),
    ]
    assert [line.reason for line in lines] == [None, "falscher Betrieb"]
    assert [line.result for line in lines] == ["bestanden", "bestanden"]


def _not_measurable(check_id: str, measured: dict[str, object], **updates: object) -> CheckResult:
    return _result(check_id, CheckOutcome.NOT_MEASURABLE, (True, None, None)).model_copy(
        update={"measured": measured, **updates}
    )


def test_a_failed_measurement_shows_not_measured_with_its_reason_and_never_three_ticks(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    failed = _not_measurable(
        "a2",
        {"reason": "measurement_invalid", "invalid_reason": "Messwerte außerhalb des Bereichs"},
    )

    lines = report.verdict_lines(
        [failed], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert len(lines) == 1
    assert lines[0].measured is False
    assert lines[0].result == "nicht gemessen"
    assert (lines[0].data, lines[0].evaluation) == (None, None)
    assert lines[0].reason == "Messwerte außerhalb des Bereichs"


def test_a_planned_not_measurable_check_is_not_in_the_control_table(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    planned = _not_measurable("m1", {"reason": "no_access"})

    lines = report.verdict_lines(
        [planned], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert lines == []


def test_a_measured_line_carries_the_official_source_of_its_criterion(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    lines = report.verdict_lines(
        [_result("a2")], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert lines[0].source.startswith("https://")


def test_only_failed_measurements_make_the_report_a_draft(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    planned = _not_measurable("m1", {"reason": "no_access"})
    failed = _not_measurable("a2", {"reason": "measurement_invalid"})
    results = [planned, failed]

    lines = report.open_checks(results, catalogue, labels, frozenset())

    by_id = {line.check_id: line.failed for line in lines}
    assert by_id["m1"] is False
    assert by_id["a2"] is True


def test_the_paid_section_states_findings_without_a_rating(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    results = [
        _result("p1").model_copy(update={"measured": {"shows_ads": True, "ads_found": 4}}),
        _result("p2").model_copy(update={"measured": {"shows_ads": False, "own_ads": 0}}),
        _result("p3").model_copy(
            update={"measured": {"phrases_with_own_ad": 1, "phrases_total": 5}}
        ),
        _result("p4").model_copy(
            update={
                "measured": {
                    "competitors_on_brand": ["a.de", "b.de"],
                    "competitors_on_brand_count": 2,
                }
            }
        ),
    ]

    lines = report.paid_lines(results, catalogue, labels)

    assert [line.check_id for line in lines] == ["p1", "p2", "p3", "p4"]
    assert "4 Anzeigen" in lines[0].text
    assert "keine Anzeigen" in lines[1].text
    assert "1 von 5" in lines[2].text
    assert "2 Wettbewerber: a.de, b.de" in lines[3].text


def test_a_paid_check_that_failed_technically_is_stated_as_not_measured(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    failed = _not_measurable("p1", {"reason": "ads_google_not_measured"})

    lines = report.paid_lines([failed], catalogue, labels)

    assert [(line.measured, line.text) for line in lines] == [
        (False, "Nicht gemessen: die Google-Werbung konnte nicht gemessen werden.")
    ]


def test_the_recommendation_names_one_action_per_check_and_only_for_three_ticks(
    catalogue: Catalogue,
    report_repositories: MagicMock,
    report_settings: Settings,
) -> None:
    context = report.recommendation_context(AUDIT_ID, report_repositories, report_settings)

    ids = [action.check_id for action in context.actions]
    assert len(ids) == len(set(ids))
    assert all(action.fix for action in context.actions)


def test_the_recommendation_is_a_pdf_named_after_the_website(
    report_repositories: MagicMock, report_settings: Settings
) -> None:
    pdf = report.build_recommendation(AUDIT_ID, report_repositories, report_settings)

    assert pdf.parent == report_settings.analysis_project_dir
    assert pdf.name == "Empfehlung_dachdecker-muster.example.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")


def test_the_traffic_light_limits_of_the_report_match_the_shared_limits_file() -> None:
    document = yaml.safe_load((REPORT_ROOT / "config" / "ampel.yaml").read_text(encoding="utf-8"))

    assert (report.BAD_BELOW, report.WARN_BELOW) == (
        Decimal(document["yellow_from"]),
        Decimal(document["green_from"]),
    )


def test_a_crossed_check_does_not_count_in_the_total(catalogue: Catalogue) -> None:
    sound = _result("a4")
    crossed_bad = _result("a7", CheckOutcome.FAILED, ticks=(False, None, None))
    counted_bad = _result("a7", CheckOutcome.FAILED)

    assert total_score([sound, crossed_bad], catalogue) > total_score(
        [sound, counted_bad], catalogue
    )


def test_a_check_with_a_missing_verdict_is_not_a_cross_and_still_counts(
    catalogue: Catalogue,
) -> None:
    open_bad = _result("a7", CheckOutcome.FAILED, ticks=(None, None, None))
    counted_bad = _result("a7", CheckOutcome.FAILED)

    assert total_score([_result("a4"), open_bad], catalogue) == total_score(
        [_result("a4"), counted_bad], catalogue
    )


def test_the_control_settings_have_their_defaults(settings: Settings) -> None:
    assert settings.control_model == DEFAULT_CONTROL_MODEL == "claude-fable-5-2"
    assert settings.control_bundle_size == 8
    assert settings.control_timeout_seconds == 1800


@pytest.mark.parametrize(("size", "valid"), [(0, False), (1, True), (20, True), (21, False)])
def test_the_bundle_size_must_be_between_one_and_twenty(
    settings: Settings, size: int, valid: bool
) -> None:
    update = {"control_bundle_size": size}
    if valid:
        assert Settings.model_validate(settings.model_dump() | update).control_bundle_size == size
    else:
        with pytest.raises(ValidationError):
            Settings.model_validate(settings.model_dump() | update)
