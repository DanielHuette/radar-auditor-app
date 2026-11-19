import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from weasyprint import HTML  # type: ignore[import-untyped]

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.config import Settings
from radarauditor.db.models import (
    Audit,
    AuditStatus,
    CheckOutcome,
    CheckResult,
    ControlState,
    Finding,
    Keyword,
    KeywordScope,
    Location,
    Ranking,
)
from radarauditor.documents import review_replies
from radarauditor.errors import RenderError
from radarauditor.rendering import render_template
from radarauditor.reporting import report
from radarauditor.reporting.texts import ReportTexts
from radarauditor.scoring.catalog import Catalogue

AUDIT_ID = UUID("00000000-0000-4000-8000-000000000002")
PREVIOUS_AUDIT_ID = UUID("00000000-0000-4000-8000-000000000004")
SALES_HEADINGS = [
    "So sichtbar sind Sie heute",
    "Was KI-Assistenten über Sie sagen",
    "Ihre Wettbewerber im Vergleich",
    "Die drei Befunde, die Sie am meisten kosten",
    "Alle Befunde",
    "Was nicht gemessen wurde",
    "Warum eine laufende Betreuung",
    "Ihr Angebot",
    "Begriffe kurz erklärt",
]


TICKS = {
    "control_state": ControlState.DONE,
    "verdict_collected_ok": True,
    "verdict_data_ok": True,
    "verdict_evaluation_ok": True,
}


def _count(repositories: MagicMock, *check_ids: str, **updates: object) -> None:
    repositories.check_results.return_value = [
        result.model_copy(update={"outcome": CheckOutcome.PASSED, **TICKS, **updates})
        if result.check_id in check_ids
        else result
        for result in repositories.check_results.return_value
    ]


def _count_gap(repositories: MagicMock, raw: RawAudit, how_many: int) -> None:
    assert raw.paid is not None and raw.paid.keyword_gap
    picked = [gap.model_dump() for gap in raw.paid.keyword_gap[:how_many]]
    _count(repositories, "c12", measured={"top_gap_keywords": picked})


def sales_html(repositories: MagicMock, settings: Settings, scratch: Path) -> str:
    context = report.sales_report_context(AUDIT_ID, repositories, settings, scratch)
    return render_template(settings.templates_dir / "report", "sales.html", {"report": context})


def test_sales_report_is_a_pdf_named_after_the_customer_website_in_the_project_folder(
    report_repositories: MagicMock, report_settings: Settings
) -> None:
    pdf = report.build_sales_report(AUDIT_ID, report_repositories, report_settings)

    assert pdf.parent == report_settings.analysis_project_dir
    assert pdf.name == "Auswertung_dachdecker-muster.example.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")


def test_a_project_folder_that_cannot_be_written_is_a_reported_error(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    missing = report_settings.model_copy(update={"analysis_project_dir": tmp_path / "gone"})

    with pytest.raises(RenderError, match="ANALYSIS_PROJECT_DIR"):
        report.build_sales_report(AUDIT_ID, report_repositories, missing)

    assert not (missing.radar_work_dir / "berichte").exists()


@pytest.mark.parametrize(
    ("website", "name", "expected"),
    [
        ("https://www.katjakarsch.de/kontakt", "Katja Karsch", "katjakarsch.de"),
        ("https://Dach-Mueller.example", "Dach Müller", "dach-mueller.example"),
        (None, "Dachdeckerei Müller & Söhne", "Dachdeckerei_Mueller_Soehne"),
    ],
)
def test_the_site_name_is_the_host_without_www_or_the_customer_name(
    website: object, name: str, expected: str
) -> None:
    assert report.site_name(website, name) == expected


def test_sales_html_has_every_section_in_order(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_texts: ReportTexts,
    tmp_path: Path,
) -> None:
    html = sales_html(report_repositories, report_settings, tmp_path)

    positions = [html.index(f"<h2>{heading}</h2>") for heading in SALES_HEADINGS]
    assert positions == sorted(positions)
    assert report_texts.offer_text in html
    assert report_texts.subscription_reason in html
    assert "RepoCity · Daniel Hütte · Münster · repocity.speedofthespirit.dev" in html


def test_sales_context_draws_every_chart(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    _count(report_repositories, "j1", "j2", "j3")
    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    uris = [context.charts.areas, context.charts.competitors, context.charts.grid]
    uris.append(context.charts.rankings)
    assert all(uri and uri.startswith("file://") for uri in uris)
    assert all(Path(uri.removeprefix("file://")).read_bytes()[:4] == b"\x89PNG" for uri in uris)


def test_top_findings_put_severity_three_first(
    report_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    severities = [finding.severity for finding in context.top_findings]
    assert len(severities) <= 3
    assert severities == sorted(severities, reverse=True)
    assert severities[0] == 3


def test_missing_texts_fall_back_to_standard_sentences(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_storage: dict[str, bytes],
    tmp_path: Path,
) -> None:
    del report_storage[f"raw/{AUDIT_ID}/texte.json"]

    html = sales_html(report_repositories, report_settings, tmp_path)

    assert "Wir haben" in html
    assert "Ein schriftliches Angebot mit festen Preisen" in html


def test_invalid_texts_fall_back_to_standard_sentences(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_storage: dict[str, bytes],
    tmp_path: Path,
) -> None:
    report_storage[f"raw/{AUDIT_ID}/texte.json"] = b'{"summary": ""}'

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)

    assert context.texts is None


def test_report_renders_without_raw_data_and_rankings(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_storage: dict[str, bytes],
    tmp_path: Path,
) -> None:
    del report_storage[f"raw/{AUDIT_ID}/raw.json"]
    del report_repositories.rankings

    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)
    pdf = report.build_sales_report(AUDIT_ID, report_repositories, report_settings)

    assert context.charts.competitors is None
    assert context.charts.rankings is None
    assert not hasattr(context, "forecast")
    assert pdf.read_bytes().startswith(b"%PDF")


def test_sales_report_shows_profile_figures_and_keyword_gap(
    report_repositories: MagicMock,
    report_settings: Settings,
    raw_audit_full: RawAudit,
    tmp_path: Path,
) -> None:
    _count_gap(report_repositories, raw_audit_full, 2)
    context = report.sales_report_context(AUDIT_ID, report_repositories, report_settings, tmp_path)
    html = render_template(
        report_settings.templates_dir / "report", "sales.html", {"report": context}
    )

    labels = [line.label for line in context.profile]
    assert labels[:2] == [
        "Aufrufe in der Google-Suche auf dem Handy",
        "Aufrufe in der Google-Suche am Computer",
    ]
    calls = next(line for line in context.profile if line.label == "Anrufe")
    assert (calls.value, calls.previous, calls.change_percent, calls.trend) == (42, 35, 20.0, "up")
    bookings = next(line for line in context.profile if line.label == "Buchungen")
    assert (bookings.change_percent, bookings.trend) == (None, "same")
    assert html.index("So sichtbar sind Sie heute") < html.index(
        "Was Ihr Google-Profil heute bringt"
    )
    assert "+20 %" in html
    assert [keyword.phrase for keyword in context.keyword_gap] == [
        "dachrinne reinigen münster",
        "gaubenverkleidung münster",
    ]
    assert "Suchbegriffe, bei denen Ihre Wettbewerber gefunden werden und Sie nicht" in html
    assert "dach-schmidt.de" in html


def test_sales_report_leaves_out_profile_and_gap_without_data(
    report_repositories: MagicMock,
    report_settings: Settings,
    report_storage: dict[str, bytes],
    raw_audit_full: RawAudit,
    tmp_path: Path,
) -> None:
    plain = raw_audit_full.model_copy(update={"access": None, "paid": None})
    report_storage[f"raw/{AUDIT_ID}/raw.json"] = plain.model_dump_json().encode()

    html = sales_html(report_repositories, report_settings, tmp_path)
    pdf = report.build_sales_report(AUDIT_ID, report_repositories, report_settings)

    assert "Was Ihr Google-Profil heute bringt" not in html
    assert "und Sie nicht" not in html
    assert pdf.read_bytes().startswith(b"%PDF")


def test_profile_lines_follow_the_labels(raw_audit_full: RawAudit) -> None:
    labels = report.load_labels(Path(__file__).resolve().parents[1] / "templates")
    labels = labels.model_copy(update={"profile_metrics": {"CALL_CLICKS": "Anrufe", "X": "Fehlt"}})
    assert raw_audit_full.access is not None
    access = raw_audit_full.access.model_copy(
        update={"profile_metrics_previous": {"CALL_CLICKS": 0}}
    )
    raw = raw_audit_full.model_copy(update={"access": access})

    lines = report.profile_lines(raw, labels)

    assert [(line.label, line.change_percent, line.trend) for line in lines] == [
        ("Anrufe", None, "up")
    ]
    assert report.profile_lines(None, labels) == []
    assert report.gap_keywords([]) == []


def test_audit_rankings_uses_the_reader_when_present(
    report_repositories: MagicMock, report_rankings: list[Ranking]
) -> None:
    assert report.audit_rankings(report_repositories, AUDIT_ID) == report_rankings
    report_repositories.rankings.assert_called_once_with(AUDIT_ID)


def test_load_labels_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match=r"labels\.yaml"):
        report.load_labels(tmp_path)


def test_grid_points_prefer_the_primary_keyword(
    report_keywords: list[Keyword], report_rankings: list[Ranking]
) -> None:
    points = report.grid_points(report_keywords, report_rankings)

    assert len(points) == 9
    assert None in points.values()


def test_group_findings_follows_catalogue_order(
    report_findings: list[Finding],
    report_check_results: list[CheckResult],
    catalogue: Catalogue,
) -> None:
    lines = report.finding_lines(report_findings, report_check_results, catalogue)
    groups = report.group_findings(lines, catalogue)
    area_order = [area.name for area in catalogue.areas]

    assert [area_order.index(group.area_name) for group in groups] == sorted(
        area_order.index(group.area_name) for group in groups
    )
    assert sum(len(group.findings) for group in groups) == len(report_findings)


def _previous_audit(sample_audit: Audit) -> Audit:
    return sample_audit.model_copy(
        update={
            "id": PREVIOUS_AUDIT_ID,
            "status": AuditStatus.REPORTED,
            "requested_at": sample_audit.requested_at - timedelta(days=30),
            "total_score": Decimal("48.00"),
        }
    )


@pytest.fixture
def monthly_repositories(
    report_repositories: MagicMock,
    sample_audit: Audit,
    report_check_results: list[CheckResult],
    report_findings: list[Finding],
    report_rankings: list[Ranking],
    report_make_ranking: object,
) -> MagicMock:
    previous = _previous_audit(sample_audit)
    passed = next(r for r in report_check_results if r.outcome is CheckOutcome.PASSED)
    previous_results = [
        result.model_copy(update={"audit_id": PREVIOUS_AUDIT_ID, "outcome": CheckOutcome.FAILED})
        if result.check_id == passed.check_id
        else result.model_copy(update={"audit_id": PREVIOUS_AUDIT_ID})
        for result in report_check_results
    ]
    previous_finding = Finding(
        id=uuid4(),
        audit_id=PREVIOUS_AUDIT_ID,
        check_id=passed.check_id,
        severity=3,
        title="Behobener Befund",
        explanation="Gemessen vorher",
        customer_impact="Kostete Anfragen",
        fix="Behobene Maßnahme",
        fix_payload=None,
    )
    previous_rankings = [
        ranking.model_copy(update={"organic_position": 7, "audit_id": PREVIOUS_AUDIT_ID})
        for ranking in report_rankings
        if ranking.grid_point is None
    ]
    by_audit = {
        AUDIT_ID: (report_check_results, report_findings, report_rankings),
        PREVIOUS_AUDIT_ID: (previous_results, [previous_finding], previous_rankings),
    }
    report_repositories.previous_audit.return_value = previous
    report_repositories.audits_of_customer.return_value = [previous, sample_audit]
    report_repositories.check_results.side_effect = lambda audit_id: by_audit[audit_id][0]
    report_repositories.findings_of_audit.side_effect = lambda audit_id: by_audit[audit_id][1]
    report_repositories.rankings = MagicMock(side_effect=lambda audit_id: by_audit[audit_id][2])
    return report_repositories


def test_monthly_report_is_a_pdf(
    monthly_repositories: MagicMock, report_settings: Settings
) -> None:
    pdf = report.build_monthly_report(AUDIT_ID, monthly_repositories, report_settings)

    assert pdf.name.startswith("Monatsbericht_Dachdeckerei_Muster_")
    assert pdf.read_bytes().startswith(b"%PDF")


def test_monthly_context_compares_both_audits(
    monthly_repositories: MagicMock, report_settings: Settings, tmp_path: Path
) -> None:
    context = report.monthly_report_context(
        AUDIT_ID, monthly_repositories, report_settings, tmp_path
    )
    html = render_template(
        report_settings.templates_dir / "report", "monthly.html", {"report": context}
    )

    assert context.total.before == Decimal("48.00")
    assert context.total.trend == "up"
    assert [finding.title for finding in context.done] == ["Behobener Befund"]
    assert {change.organic_trend for change in context.rankings} == {"up"}
    assert context.charts.trend is not None
    assert "Das haben wir für Sie erledigt" in html
    assert "Das nehmen wir uns als Nächstes vor" in html
    assert "Wir versprechen diese Arbeit, keine bestimmte Platzierung" in html
    assert "▲" in html
    assert "Was Ihr Google-Profil heute bringt" in html
    assert len(context.profile) == 9


def test_monthly_report_needs_a_previous_audit(
    report_repositories: MagicMock, report_settings: Settings
) -> None:
    with pytest.raises(RenderError, match="no previous reported audit"):
        report.build_monthly_report(AUDIT_ID, report_repositories, report_settings)


def test_ranking_changes_cover_lost_and_new_positions(
    report_keywords: list[Keyword], report_make_ranking: object
) -> None:
    make = report_make_ranking
    assert callable(make)
    primary, second, _ = report_keywords
    before = [make(primary.id, "customer", 3, None), make(second.id, "customer", None, 2)]
    after = [make(primary.id, "customer", None, None), make(second.id, "customer", 4, 2)]

    changes = report.ranking_changes(report_keywords, before, after)

    assert [(c.organic_trend, c.local_trend) for c in changes] == [
        ("down", "same"),
        ("up", "same"),
    ]


def test_score_history_keeps_reported_audits_and_the_current_one(sample_audit: Audit) -> None:
    previous = _previous_audit(sample_audit)
    failed = previous.model_copy(update={"id": uuid4(), "status": AuditStatus.FAILED})

    history = report.score_history([failed, previous], sample_audit)

    assert [score for _, score in history] == [Decimal("48.00"), Decimal("61.50")]
    assert history[0][0] == "01.09.2026"


def test_check_order_is_numeric_within_an_area() -> None:
    assert sorted(["b10", "a3", "b2", "a12"], key=report.check_order) == ["a3", "a12", "b2", "b10"]


def test_open_checks_name_every_check_without_a_measured_value(
    catalogue: Catalogue, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    results = [
        CheckResult(
            id=uuid4(),
            audit_id=AUDIT_ID,
            check_id=check.id,
            area=area.id,
            outcome=CheckOutcome.NOT_MEASURABLE if check.id == "h1" else CheckOutcome.PASSED,
            measured={"reason": "directory_not_checked"} if check.id == "h1" else {},
            weight=Decimal(1),
        )
        for area in catalogue.areas
        for check in area.checks
        if check.id not in {"i1", "m1"}
    ]

    lines = report.open_checks(results, catalogue, labels, frozenset({"i1"}))

    assert [(line.check_id, line.reason, line.decided_by_daniel) for line in lines] == [
        ("i1", "nur mit einer bezahlten Datenquelle messbar", True),
        (
            "m1",
            "nur mit Zugang zu Ihren Konten bei den Anbietern (zum Beispiel Google, Meta) messbar",
            False,
        ),
        ("h1", "Verzeichnis wurde nicht abgefragt", False),
    ]


def test_pending_checks_come_from_the_file_next_to_the_catalogue(
    report_settings: Settings,
) -> None:
    pending = report.load_pending(report_settings.catalogue_path)

    assert pending == frozenset()


def test_assistant_lines_count_answers_per_assistant(
    raw_audit_full: RawAudit, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    heard = [
        answer for answer in raw_audit_full.assistant_answers if answer.provider != "google_ai"
    ]
    raw = raw_audit_full.model_copy(
        update={
            "assistant_answers": heard,
            "warnings": ["assistants: no answers from google_ai"],
        }
    )

    lines = report.assistant_lines(raw, labels)

    by_name = {line.name: line for line in lines}
    assert by_name["Google KI-Modus"].answers == 0
    assert by_name["Google KI-Modus"].route == "keine Antwort erhalten"
    assert sum(line.answers for line in lines) == len(raw.assistant_answers)


def test_assistant_answers_from_dataforseo_name_that_route(
    raw_audit_full: RawAudit, report_settings: Settings
) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    answers = [
        answer.model_copy(update={"route": "dataforseo"})
        for answer in raw_audit_full.assistant_answers
        if answer.provider == "google_ai"
    ]
    raw = raw_audit_full.model_copy(update={"assistant_answers": answers})

    (line,) = report.assistant_lines(raw, labels)

    assert line.route == "automatisch über DataForSEO, ohne Anmeldung"


def test_every_reason_code_of_the_checks_has_customer_wording(report_settings: Settings) -> None:
    labels = report.load_labels(report_settings.templates_dir)
    checks_dir = Path(report.__file__).resolve().parents[1] / "scoring" / "checks"
    codes = {
        code
        for module in checks_dir.glob("*.py")
        for code in re.findall(
            r'not_measurable\(\s*"([a-z_0-9]+)"', module.read_text(encoding="utf-8")
        )
    }

    assert codes
    assert codes - set(labels.reasons) == set()


def test_address_without_street_has_no_stray_comma(report_location: Location) -> None:
    empty = report_location.model_copy(update={"street": "", "postal_code": ""})

    assert report._address(empty) == "Münster"  # noqa: SLF001


def test_ranking_changes_mark_nationwide_keywords(
    report_keywords: list[Keyword], report_make_ranking: object
) -> None:
    make = report_make_ranking
    assert callable(make)
    primary, second, _ = report_keywords
    keywords = [primary, second.model_copy(update={"scope": KeywordScope.NATIONWIDE})]
    before = [make(primary.id, "customer", 3, None), make(second.id, "customer", 4, 2)]

    changes = report.ranking_changes(keywords, before, before)

    assert [change.nationwide for change in changes] == [False, True]


def test_the_ranking_chart_names_nationwide_keywords_as_such(
    report_keywords: list[Keyword],
    report_make_ranking: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    make = report_make_ranking
    assert callable(make)
    primary, second, _ = report_keywords
    keywords = [primary, second.model_copy(update={"scope": KeywordScope.NATIONWIDE})]
    drawn: dict[UUID, str] = {}

    def draw(
        rankings: object, output: Path, *, keyword_phrases: dict[UUID, str], **_: object
    ) -> Path:
        drawn.update(keyword_phrases)
        return output

    monkeypatch.setattr(report.charts, "ranking_table_image", draw)
    labels = report.load_labels(Path(__file__).resolve().parents[1] / "templates")

    report._ranking_chart(keywords, [make(primary.id, "customer", 3, None)], labels, tmp_path)

    assert drawn == {primary.id: primary.phrase, second.id: f"{second.phrase} (bundesweit)"}


def test_every_paid_service_of_the_steps_has_an_everyday_name(report_settings: Settings) -> None:
    from radarauditor.jobs import rerun

    labels = report.load_labels(report_settings.templates_dir)
    services = {service for names in rerun.STEP_SERVICES.values() for service in names}
    services.add("control")

    assert sorted(services - set(labels.cost_services)) == []


AI_METADATA_VALUE = "Erstellt mit künstlicher Intelligenz"
AI_FOOTER_TEXT = "Die Texte dieses Berichts wurden mit künstlicher Intelligenz erstellt."
AI_COVER_NOTICE = (
    "Die Texte zu den Befunden, Auswertungen und Empfehlungen in diesem Bericht wurden mit "
    "künstlicher Intelligenz erstellt. Die Messwerte und Punkte berechnet ein Programm. Jede "
    "gemessene Prüfung wurde danach in einem eigenen Kontrollschritt, ebenfalls mit künstlicher "
    "Intelligenz, anhand der unveränderten Antworten der Anbieter geprüft."
)


def recommendation_html(repositories: MagicMock, settings: Settings) -> str:
    context = report.recommendation_context(AUDIT_ID, repositories, settings)
    return render_template(
        settings.templates_dir / "report", "recommendation.html", {"report": context}
    )


def monthly_html(repositories: MagicMock, settings: Settings, scratch: Path) -> str:
    context = report.monthly_report_context(AUDIT_ID, repositories, settings, scratch)
    return render_template(settings.templates_dir / "report", "monthly.html", {"report": context})


@pytest.fixture(params=["sales", "recommendation", "monthly"])
def report_html(
    request: pytest.FixtureRequest,
    monthly_repositories: MagicMock,
    report_settings: Settings,
    tmp_path: Path,
) -> str:
    if request.param == "sales":
        return sales_html(monthly_repositories, report_settings, tmp_path)
    if request.param == "recommendation":
        return recommendation_html(monthly_repositories, report_settings)
    return monthly_html(monthly_repositories, report_settings, tmp_path)


def test_every_report_says_on_the_cover_that_its_texts_come_from_artificial_intelligence(
    report_html: str,
) -> None:
    notice = f"<strong>Hinweis zur künstlichen Intelligenz:</strong> {AI_COVER_NOTICE}"

    assert notice in report_html
    assert report_html.index(notice) < report_html.index("<h2>")


def test_every_report_footer_carries_the_notice_on_each_page(report_html: str) -> None:
    footer = f'repocity.speedofthespirit.dev" "\\A" "{AI_FOOTER_TEXT}";'

    assert footer in report_html


def test_every_report_carries_the_machine_readable_marking(report_html: str) -> None:
    assert f'<meta name="keywords" content="{AI_METADATA_VALUE}">' in report_html
    assert f'<meta name="description" content="{AI_FOOTER_TEXT}">' in report_html


def test_the_pdf_metadata_carries_the_marking(report_html: str) -> None:
    metadata = HTML(string=report_html).render().metadata

    assert metadata.keywords == [AI_METADATA_VALUE]
    assert metadata.description == AI_FOOTER_TEXT


def test_reply_drafts_claim_no_measurement_control(report_settings: Settings) -> None:
    page = review_replies.ReplyPage(
        customer_name="Dachdeckerei Muster",
        audit_date=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        items=[
            review_replies.ReplyItem(
                date="09.09.2026", stars=2, review="Zu spät gekommen.", reply="Das tut uns leid."
            )
        ],
    )

    html = review_replies.replies_html(report_settings.templates_dir, page)

    assert (
        "<strong>Hinweis zur künstlichen Intelligenz:</strong> "
        "Die Antwortentwürfe in diesem Dokument wurden mit künstlicher Intelligenz erstellt."
    ) in html
    assert "Kontrollschritt" not in html
    assert f'<meta name="keywords" content="{AI_METADATA_VALUE}">' in html
