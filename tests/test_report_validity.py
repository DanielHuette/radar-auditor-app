from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from conftest import AUDIT_ID
from markupsafe import escape
from test_report import TICKS, _count, _count_gap, sales_html

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import CheckOutcome, CheckResult, ControlState
from radarauditor.rendering import render_template
from radarauditor.reporting import report
from radarauditor.scoring.catalog import Catalogue

INVALID = {
    "reason": "measurement_invalid",
    "invalid_reason": "nicht gemessen: Profil mit unvollständigen Firmendaten gesucht",
}
PLACE_CHECKS = ("f1", "g1", "j3")


def _update(repositories: MagicMock, check_ids: tuple[str, ...], **updates: object) -> None:
    repositories.check_results.return_value = [
        result.model_copy(update=updates) if result.check_id in check_ids else result
        for result in repositories.check_results.return_value
    ]


def _unmeasured(check_id: str, measured: dict[str, object], **updates: object) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="google",
        outcome=CheckOutcome.NOT_MEASURABLE,
        measured=measured,
        weight=Decimal(1),
        **updates,
    )


def test_every_unmeasured_check_names_its_reason_in_plain_words(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    results = [
        _unmeasured("a2", INVALID),
        _unmeasured("f1", {"reason": "a_code_nobody_labelled"}),
        _unmeasured("f2", {}),
        _unmeasured(
            "f4",
            {"reason": "a_code_nobody_labelled"},
            control_state=ControlState.DONE,
            verdict_collected_ok=False,
            verdict_collected_reason="Die Daten fehlen in der Antwort",
        ),
    ]

    opened = report.open_checks(results, catalogue, labels, frozenset())
    lines = {line.check_id: line for line in opened}

    assert lines["a2"].reason == "Profil mit unvollständigen Firmendaten gesucht"
    assert lines["f1"].reason == (
        "Der Grund wurde nicht gespeichert (Kennung: a_code_nobody_labelled)"
    )
    assert lines["f2"].reason == "Der Grund wurde nicht gespeichert (Kennung: ohne Angabe)"
    assert lines["f4"].reason == "Die Daten fehlen in der Antwort"
    assert all("kein Messwert" not in line.reason for line in lines.values())


def test_an_unmeasured_paid_check_gets_the_stored_reason_too(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    paid = _unmeasured(
        "p3",
        INVALID,
        control_state=ControlState.DONE,
        verdict_collected_ok=True,
    )

    lines = report.paid_lines([paid], catalogue, labels)

    assert "Profil mit unvollständigen Firmendaten gesucht" in lines[0].text
    assert "kein Messwert" not in lines[0].text
    assert "nicht gemessen: nicht gemessen" not in lines[0].text


def test_the_control_table_does_not_print_the_not_measured_prefix_twice(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    result = _unmeasured(
        "a2",
        INVALID,
        control_state=ControlState.DONE,
        verdict_collected_ok=False,
        verdict_collected_reason="nicht gemessen: Profil mit unvollständigen Firmendaten gesucht",
    )

    lines = report.verdict_lines(
        [result], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert lines[0].reason == "Profil mit unvollständigen Firmendaten gesucht"


def test_a_finding_without_a_rating_is_not_called_passed_in_the_control_table(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    result = CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id="p3",
        area="paid",
        outcome=CheckOutcome.PASSED,
        measured={"phrases_total": 39, "phrases_with_own_ad": 0},
        weight=Decimal(0),
        **TICKS,
    )

    lines = report.verdict_lines(
        [result], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert lines[0].result == "Feststellung ohne Bewertung"


def test_an_invalid_profile_measurement_leaves_out_the_comparison_and_the_claim(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    _update(
        report_repositories, PLACE_CHECKS, outcome=CheckOutcome.NOT_MEASURABLE, measured=INVALID
    )

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)
    html = sales_html(report_repositories, report_settings, tmp_path)

    assert context.charts.competitors is None
    assert context.charts.grid is None
    assert context.comparison_note is not None
    assert "Profil mit unvollständigen Firmendaten gesucht" in context.comparison_note
    assert "Profil mit unvollständigen Firmendaten gesucht" in html
    assert "Ohne eigenes Google-Unternehmensprofil" not in html
    assert "Hier zeigt Google Maps Sie" not in html


def test_a_profile_that_was_not_read_is_never_called_missing(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_storage: dict[str, bytes],
    raw_audit_full: RawAudit,
    tmp_path: Path,
) -> None:
    unread = raw_audit_full.model_copy(update={"place": None, "profile_missing": True})
    report_storage[f"raw/{AUDIT_ID}/raw.json"] = unread.model_dump_json().encode()
    _update(
        report_repositories,
        ("f1",),
        outcome=CheckOutcome.NOT_MEASURABLE,
        measured={"reason": "profile_not_provable"},
    )

    html = sales_html(report_repositories, report_settings, tmp_path)

    assert "das beweist nicht, dass keines existiert" in html
    assert "Ohne eigenes Google-Unternehmensprofil" not in html
    assert "kein Google-Unternehmensprofil" not in html


def test_a_valid_profile_keeps_the_comparison(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.comparison_note is None
    assert context.charts.competitors is not None


@pytest.mark.parametrize(
    "state",
    [
        {"outcome": CheckOutcome.NOT_MEASURABLE, "measured": {"reason": "no_grid"}},
        {"verdict_evaluation_ok": False},
        {"verdict_data_ok": None},
    ],
)
def test_the_red_grid_needs_a_measured_and_controlled_j3(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path, state: dict
) -> None:
    _count(report_repositories, "j1", "j2", "j3")
    _update(report_repositories, ("j3",), **state)

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.charts.grid is None
    assert context.charts.rankings is not None


def test_the_grid_is_drawn_when_j3_is_measured_and_controlled(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    _count(report_repositories, "j3")

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.charts.grid is not None


def test_the_ranking_table_needs_a_valid_search_check(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    _update(
        report_repositories,
        ("j1", "j2"),
        outcome=CheckOutcome.NOT_MEASURABLE,
        measured={"reason": "no_valid_result_lists"},
    )

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.charts.rankings is None


def test_the_gap_table_lists_only_what_check_c12_counted(
    report_repositories: MagicMock,
    report_settings: Settings,
    raw_audit_full: RawAudit,
    tmp_path: Path,
) -> None:
    assert raw_audit_full.paid is not None
    assert len(raw_audit_full.paid.keyword_gap or []) > 1
    _count_gap(report_repositories, raw_audit_full, 1)

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert [keyword.phrase for keyword in context.keyword_gap] == [
        (raw_audit_full.paid.keyword_gap or [])[0].phrase
    ]


@pytest.mark.parametrize(
    "state",
    [
        {"verdict_evaluation_ok": False},
        {"outcome": CheckOutcome.NOT_MEASURABLE, "measured": {"reason": "no_keyword_gap"}},
    ],
)
def test_the_gap_table_is_empty_unless_c12_has_three_ticks(
    report_repositories: MagicMock,
    report_settings: Settings,
    raw_audit_full: RawAudit,
    tmp_path: Path,
    state: dict,
) -> None:
    _count_gap(report_repositories, raw_audit_full, 2)
    _update(report_repositories, ("c12",), **state)

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.keyword_gap == []


def test_the_report_makes_no_forecast_without_a_provable_basis(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    html = sales_html(report_repositories, report_settings, tmp_path)

    assert "Was eine Optimierung bringen kann" not in html
    assert "Anfragen über Google pro Monat" not in html
    assert "Diese Annahmen stecken in der Schätzung" not in html


def test_the_cover_and_the_recommendation_state_only_what_is_checked(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    sales = sales_html(report_repositories, report_settings, tmp_path)
    context = report.recommendation_context(AUDIT_ID, report_repositories, report_settings)
    advice = render_template(
        report_settings.templates_dir / "report", "recommendation.html", {"report": context}
    )

    assert "in Verzeichnissen" not in sales
    assert "zweifach" not in advice
    assert advice.count('<span class="label">Gemessen:</span>') == len(context.actions) > 0
    assert all(str(escape(action.explanation)) in advice for action in context.actions)


def test_an_area_without_a_valid_check_is_named_as_unrated(
    catalogue: Catalogue,
    report_repositories: MagicMock,
    report_settings: Settings,
    tmp_path: Path,
) -> None:
    google = next(area for area in catalogue.areas if area.id == "google")
    own = {check.id for check in google.checks}
    others = tuple(
        check.id for area in catalogue.areas if area.id != "google" for check in area.checks
    )
    _update(report_repositories, others, outcome=CheckOutcome.NOT_MEASURABLE, measured={})
    assert own

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)
    html = sales_html(report_repositories, report_settings, tmp_path)

    assert [line.area_id for line in context.areas] == ["google"]
    assert context.unrated_areas == [
        area.name
        for area in catalogue.areas
        if area.id != "google" and any(check.counts_in_score for check in area.checks)
    ]
    assert "Ohne Punktwert blieben" in html


def test_the_total_and_every_area_name_how_many_checks_they_come_from(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)
    html = sales_html(report_repositories, report_settings, tmp_path)

    assert context.areas
    assert all(0 < line.counted <= line.ratable for line in context.areas)
    assert context.total_counted == sum(line.counted for line in context.areas)
    assert context.total_ratable >= sum(line.ratable for line in context.areas)
    assert f"aus {context.total_counted} von {context.total_ratable} wertbaren Pr" in html
    first = context.areas[0]
    assert f"aus {first.counted} von {first.ratable} Pr" in html


def test_a_skipped_check_is_not_ratable_and_a_crossed_one_is_not_counted(
    catalogue: Catalogue, report_repositories: MagicMock
) -> None:
    results = report_repositories.check_results.return_value
    before = report.coverage(results, catalogue)

    _update(report_repositories, ("a2",), outcome=CheckOutcome.SKIPPED, measured={})
    skipped = report.coverage(report_repositories.check_results.return_value, catalogue)

    assert skipped[1] == before[1] - 1
    assert skipped[0] == before[0] - 1


def test_a_long_reason_is_shortened_at_a_word_and_a_short_one_stays() -> None:
    long = "Die Trefferliste besteht nur aus Adressen der Suchmaschine; " * 10

    shortened = report.short_reason(long)

    assert report.short_reason("kurz") == "kurz"
    assert len(shortened) < len(long)
    assert shortened.endswith(report.SHORT_REASON_SUFFIX)
    assert shortened.startswith("Die Trefferliste besteht")


def test_checks_with_the_same_reason_are_listed_once_with_all_their_names() -> None:
    lines = [
        report.OpenCheckLine(check_id=check_id, name=name, reason=reason, decided_by_daniel=False)
        for check_id, name, reason in (
            ("j1", "Plätze", "Suche ungültig"),
            ("j2", "Karte", "Suche ungültig"),
            ("a2", "Ladezeit", "ältere Messung"),
        )
    ]

    groups = report.group_open_checks(lines)

    assert [(group.names, group.reason) for group in groups] == [
        (["Plätze", "Karte"], "Suche ungültig"),
        (["Ladezeit"], "ältere Messung"),
    ]


def _comparison(check_id: str, measured: dict[str, object]) -> str | None:
    result = CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="openai",
        outcome=CheckOutcome.PASSED,
        measured=measured,
        weight=Decimal(0),
    )
    return report.comparison_text(result)


def test_an_assistant_check_shows_own_value_and_strongest_competitor() -> None:
    measured: dict[str, object] = {
        "mentioned": 3,
        "answers": 60,
        "competitors": [
            {"name": "Dach GmbH", "mentioned": 22, "answers": 60},
            {"name": "Zweit", "mentioned": 5, "answers": 60},
        ],
    }

    assert _comparison("k1", measured) == (
        "3 von 60 Antworten nennen den Betrieb; stärkster Wettbewerber (Dach GmbH): 22 von 60"
    )


def test_an_assistant_check_without_competitor_values_says_so() -> None:
    text = _comparison("k6", {"mentioned": 1, "answers": 4, "competitors": []})

    assert text == "1 von 4 Antworten nennen den Betrieb; Wettbewerberwerte liegen nicht vor"


def test_the_grid_shows_its_visible_points_and_does_not_invent_competitor_values() -> None:
    measured: dict[str, object] = {
        "points_in_top": 4,
        "points_total": 25,
        "top": 3,
        "competitor_values": None,
    }

    assert _comparison("j3", measured) == (
        "4 von 25 Rasterpunkten in den ersten 3; Wettbewerberwerte liegen nicht vor"
    )


def test_a_check_without_such_a_measurement_shows_no_comparison() -> None:
    assert _comparison("a2", {}) is None


@pytest.mark.parametrize("check_id", ["k1", "k2", "k6", "k7", "j3"])
def test_a_check_without_a_verdict_is_never_called_passed_in_the_control_table(
    check_id: str, catalogue: Catalogue, report_settings: Settings
) -> None:
    result = CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area="openai",
        outcome=CheckOutcome.PASSED,
        measured={"mentioned": 3, "answers": 60, "points_in_top": 3, "points_total": 25},
        weight=Decimal(0),
        **TICKS,
    )

    lines = report.verdict_lines(
        [result], catalogue, report.load_labels(report_settings.templates_dir)
    )

    assert lines[0].result == "Feststellung ohne Bewertung"
    assert "bestanden" not in (lines[0].result + (lines[0].value or ""))
