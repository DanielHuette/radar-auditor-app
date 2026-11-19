from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import CHECK_COUNT

from radarauditor.db.models import CheckOutcome, CheckResult
from radarauditor.errors import AuditHalted
from radarauditor.feedback.events import AuditEvent, Result, RouteReport
from radarauditor.feedback.hub import Channels, Feedback, RouteOutcome
from radarauditor.feedback.routes import RouteMap, load_route_map
from radarauditor.scoring.catalog import Catalogue, load_catalogue

CONFIG = Path(__file__).resolve().parents[1] / "config"
AUDIT_ID = UUID("1a2b3c4d-0000-4000-8000-000000000001")
NOW = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
GOOGLE_BLOCKED = "www.google.de shows a bot check"
GOOGLE_BLOCKED_PLAIN = "Google zeigt eine Sicherheitsabfrage"


class Places:
    def __init__(self) -> None:
        self.file: list[AuditEvent] = []
        self.database: list[list[AuditEvent]] = []
        self.board: list[str] = []
        self.notices: list[str] = []
        self.urgent: list[bool] = []
        self.overview: list[str] = []

    def _notice(self, text: str, urgent: bool) -> None:
        self.notices.append(text)
        self.urgent.append(urgent)

    def channels(self) -> Channels:
        return Channels(
            file=self.file.append,
            database=self.database.append,
            board=self.board.append,
            notice=self._notice,
            overview=self.overview.append,
        )


@pytest.fixture(scope="module")
def catalogue() -> Catalogue:
    return load_catalogue(CONFIG / "pruefkatalog.yaml")


@pytest.fixture(scope="module")
def routes(catalogue: Catalogue) -> RouteMap:
    return load_route_map(CONFIG / "messwege.yaml", catalogue)


@pytest.fixture
def places() -> Places:
    return Places()


def _feedback(routes: RouteMap, places: Places, halt_stages: tuple[int, ...] = (0, 1)) -> Feedback:
    return Feedback(
        audit_id=AUDIT_ID,
        business="Katja Karsch",
        routes=routes,
        halt_stages=halt_stages,
        channels=places.channels(),
        clock=lambda: NOW,
    )


def test_a_measured_route_is_written_down_but_not_announced(
    routes: RouteMap, places: Places
) -> None:
    _feedback(routes, places).route_done("pagespeed", Result.MEASURED)

    (event,) = places.file
    assert (event.kind, event.route, event.result, event.stage, event.phase) == (
        "route",
        "pagespeed",
        Result.MEASURED,
        3,
        "messung",
    )
    assert event.time == NOW
    assert [(check.number, check.id) for check in event.checks] == [(1, "a2")]
    assert places.database == [[event]]
    assert places.board == [] and places.notices == []


def test_a_failed_route_is_announced_at_once_in_every_place(
    routes: RouteMap, places: Places
) -> None:
    _feedback(routes, places).route_done("pagespeed", Result.FAILED, "PageSpeed answered HTTP 429")

    (event,) = places.file
    assert event.reason == "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"
    assert places.database == [[event]]
    assert places.notices == [
        "Audit Katja Karsch: Messung fehlgeschlagen – PageSpeed (Nr 1 a2): "
        "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"
    ]
    assert places.board == [
        "Audit Katja Karsch [A-1a2b3c4d]: PageSpeed (Nr 1 a2): "
        "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"
    ]


def test_a_failure_of_a_critical_stage_is_urgent_and_any_other_is_not(
    routes: RouteMap, places: Places
) -> None:
    _feedback(routes, places).route_done("pagespeed", Result.FAILED, "HTTP 429")
    with pytest.raises(AuditHalted):
        _feedback(routes, places).route_done("google_search", Result.FAILED, GOOGLE_BLOCKED)

    assert places.urgent == [False, True]


def test_the_notice_names_the_first_checks_and_counts_the_rest(
    routes: RouteMap, places: Places
) -> None:
    with pytest.raises(AuditHalted):
        _feedback(routes, places).route_done("google_search", Result.FAILED, GOOGLE_BLOCKED)

    assert places.notices[0].startswith(
        "Audit Katja Karsch: Messung fehlgeschlagen – "
        "Google-Suche (Nr 19 j1, Nr 20 j2, Nr 22 j4 und 4 weitere): " + GOOGLE_BLOCKED_PLAIN
    )


def test_a_failed_route_of_stage_one_stops_the_audit(routes: RouteMap, places: Places) -> None:
    feedback = _feedback(routes, places)

    with pytest.raises(AuditHalted) as halted:
        feedback.route_done("google_search", Result.FAILED, GOOGLE_BLOCKED)

    assert str(halted.value) == (
        "Sichtbarkeitsaudit für Katja Karsch angehalten, wartet auf Daniel: "
        f"Google-Suche: {GOOGLE_BLOCKED_PLAIN}"
    )
    assert places.file[0].result is Result.FAILED
    assert places.board[0].startswith(
        "Audit Katja Karsch [A-1a2b3c4d] angehalten, wartet auf Daniel: Google-Suche"
    )


@pytest.mark.parametrize("route", ["pagespeed", "website", "apple_maps"])
def test_a_failed_route_of_stage_two_or_three_lets_the_audit_go_on(
    routes: RouteMap, places: Places, route: str
) -> None:
    _feedback(routes, places).route_done(route, Result.FAILED, "customer has no website")

    assert places.notices != [] and places.board != []


def test_a_route_done_with_a_person_is_no_failure(routes: RouteMap, places: Places) -> None:
    _feedback(routes, places).route_done("assistant:google_ai", Result.WITH_PERSON, GOOGLE_BLOCKED)

    (event,) = places.file
    assert (event.result, event.reason) == (Result.WITH_PERSON, GOOGLE_BLOCKED_PLAIN)
    assert places.notices == [] and places.board == []


def test_a_suspended_route_of_stage_zero_is_no_failure(routes: RouteMap, places: Places) -> None:
    _feedback(routes, places).route_done(
        "dataforseo", Result.SUSPENDED, "Der Bezahldienst ist ausgesetzt"
    )

    assert places.file[0].reason == "Der Bezahldienst ist ausgesetzt"
    assert places.notices == []


def test_a_failed_route_of_stage_zero_stops_the_audit(routes: RouteMap, places: Places) -> None:
    with pytest.raises(AuditHalted):
        _feedback(routes, places).route_done("dataforseo", Result.FAILED, "answered HTTP 403")


def test_the_critical_stages_are_configurable(routes: RouteMap, places: Places) -> None:
    _feedback(routes, places, halt_stages=()).route_done(
        "google_search", Result.FAILED, GOOGLE_BLOCKED
    )

    with pytest.raises(AuditHalted):
        _feedback(routes, Places(), halt_stages=(3,)).route_done(
            "website", Result.FAILED, "customer has no website"
        )


def test_every_outcome_of_a_probe_is_written_before_the_audit_stops(
    routes: RouteMap, places: Places
) -> None:
    feedback = _feedback(routes, places)

    with pytest.raises(AuditHalted) as halted:
        feedback.routes_done(
            [
                RouteOutcome("assistant:claude", Result.FAILED, "Claude Code gave an empty answer"),
                RouteOutcome("google_search", Result.FAILED, GOOGLE_BLOCKED),
                RouteOutcome("pagespeed", Result.MEASURED, None),
            ],
            phase="vorab",
        )

    assert [(event.route, event.phase) for event in places.file] == [
        ("assistant:claude", "vorab"),
        ("google_search", "vorab"),
        ("pagespeed", "vorab"),
    ]
    assert "Claude: " in str(halted.value) and f"Google-Suche: {GOOGLE_BLOCKED_PLAIN}" in str(
        halted.value
    )


def test_the_same_failure_is_announced_once(routes: RouteMap, places: Places) -> None:
    feedback = _feedback(routes, places)

    feedback.route_done("pagespeed", Result.FAILED, "HTTP 429", phase="vorab")
    feedback.route_done("pagespeed", Result.FAILED, "HTTP 429")

    assert len(places.file) == 2
    assert len(places.notices) == 1


def test_the_card_of_the_audit_collects_all_failures(routes: RouteMap, places: Places) -> None:
    feedback = _feedback(routes, places)

    feedback.route_done("pagespeed", Result.FAILED, "HTTP 429")
    feedback.route_done("apple_maps", Result.FAILED, "not checked: Apple Maps")

    assert len(places.board) == 2
    assert "PageSpeed" in places.board[1] and "Apple Karten" in places.board[1]
    assert places.board[1].count(" | ") == 1


def test_the_route_results_are_kept_for_the_scoring(routes: RouteMap, places: Places) -> None:
    feedback = _feedback(routes, places)

    feedback.route_done("pagespeed", Result.FAILED, "HTTP 429")
    feedback.route_done("website", Result.MEASURED)

    assert feedback.reports == [
        RouteReport(
            route="pagespeed",
            result=Result.FAILED,
            reason="Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)",
        ),
        RouteReport(route="website", result=Result.MEASURED, reason=None),
    ]


def _result(check_id: str, outcome: CheckOutcome, **measured: object) -> CheckResult:
    return CheckResult(
        id=uuid4(),
        audit_id=AUDIT_ID,
        check_id=check_id,
        area=check_id[0],
        outcome=outcome,
        measured=dict(measured),
        weight=Decimal(1),
    )


def _by_id(events: list[AuditEvent]) -> dict[str, AuditEvent]:
    return {event.check_id: event for event in events if event.check_id is not None}


def test_checks_done_writes_one_event_per_check_of_the_catalogue(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    events = _feedback(routes, places).checks_done(
        [], catalogue, frozenset(), [], paid_enabled=False
    )

    assert [event.check_number for event in events] == list(range(1, CHECK_COUNT + 1))
    assert places.file == events
    assert places.database == [events]
    assert {event.phase for event in events} == {"auswertung"}


def test_a_check_with_a_value_counts_as_measured(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED),
        _result("a4", CheckOutcome.PARTIAL),
        _result("a12", CheckOutcome.FAILED),
        _result("f2", CheckOutcome.FAILED, reason="no_profile"),
    ]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)
    )

    for check_id in ("a2", "a4", "a12", "f2"):
        assert (events[check_id].result, events[check_id].reason) == (Result.MEASURED, None)


def test_a_check_without_a_value_counts_as_failed_with_its_reason(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [_result("a4", CheckOutcome.NOT_MEASURABLE, reason="no_website")]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)
    )

    assert (events["a4"].result, events["a4"].reason) == (
        Result.FAILED,
        "Die Website konnte nicht gelesen werden",
    )
    assert events["a4"].stage == 3


def test_a_check_of_a_failed_route_names_the_route_and_its_reason(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [_result("a2", CheckOutcome.NOT_MEASURABLE, reason="no_pagespeed")]
    reports = [
        RouteReport(
            route="pagespeed",
            result=Result.FAILED,
            reason="Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)",
        )
    ]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), reports, True)
    )

    assert (
        events["a2"].reason
        == "PageSpeed ausgefallen: Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)"
    )


def test_a_check_whose_route_is_not_built_fails_with_a_clear_reason(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [_result("h1", CheckOutcome.NOT_MEASURABLE, reason="directory_not_checked")]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)
    )

    assert (events["h1"].result, events["h1"].reason) == (
        Result.FAILED,
        "Das Verzeichnis wurde nicht abgefragt",
    )


def test_a_check_measurable_from_the_second_audit_on_is_suspended_not_failed(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [
        _result("j9", CheckOutcome.NOT_MEASURABLE, reason="needs_history"),
        _result("a4", CheckOutcome.NOT_MEASURABLE, reason="short_history"),
    ]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)
    )

    assert (events["j9"].result, events["j9"].reason) == (
        Result.SUSPENDED,
        "Erst ab der zweiten Prüfung messbar",
    )
    assert events["a4"].result is Result.FAILED


def test_open_checks_are_open_and_not_failed(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [_result("j9", CheckOutcome.NOT_MEASURABLE, reason="short_history")]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset({"j9", "m1"}), [], True)
    )

    assert (events["j9"].result, events["j9"].reason) == (Result.OPEN, "Offen, Entscheidung Daniel")
    assert events["m1"].result is Result.OPEN


def test_paid_checks_are_suspended_while_the_paid_service_is_off(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    events = _by_id(
        _feedback(routes, places).checks_done([], catalogue, frozenset(), [], paid_enabled=False)
    )

    assert events["i1"].result is Result.SUSPENDED
    assert events["i1"].reason == "Der Bezahldienst DataForSEO ist ausgesetzt"


def test_a_check_switched_off_in_the_settings_is_suspended(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [_result("a4", CheckOutcome.SKIPPED, reason="In den Einstellungen abgeschaltet")]

    events = _by_id(
        _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)
    )

    assert (events["a4"].result, events["a4"].reason) == (
        Result.SUSPENDED,
        "In den Einstellungen abgeschaltet",
    )


def test_a_check_that_needs_customer_access_is_suspended_without_it(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    events = _by_id(_feedback(routes, places).checks_done([], catalogue, frozenset(), [], True))

    assert events["m1"].result is Result.SUSPENDED
    assert events["m1"].reason == "Nur mit Kundenzugang möglich"


def _all_results(catalogue: Catalogue, **replaced: CheckOutcome) -> list[CheckResult]:
    return [
        _result(
            check.id,
            replaced.get(check.id, CheckOutcome.PASSED),
            **({"reason": "directory_not_checked"} if check.id in replaced else {}),
        )
        for area in catalogue.areas
        for check in area.checks
        if not (check.requires_paid or check.requires_access)
    ]


def test_failed_checks_are_announced_once_as_a_summary(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    missing = CheckOutcome.NOT_MEASURABLE
    results = _all_results(catalogue, f2=missing, g1=missing, h1=missing, h2=missing)

    _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], paid_enabled=False)

    assert places.notices == [
        "Audit Katja Karsch: 4 Prüfungen ohne Messwert – Nr 11 f2, Nr 17 g1, Nr 47 h2 und 1 weitere"
    ]
    assert places.urgent == [False]
    assert places.board[0].startswith(
        "Audit Katja Karsch [A-1a2b3c4d]: 4 Prüfungen ohne Messwert – "
    )
    assert (
        "Nr 11 f2 Die Hauptkategorie ist gesetzt: Das Verzeichnis wurde nicht abgefragt"
        in places.board[0]
    )


def test_checks_of_a_failed_route_are_not_announced_again(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = _all_results(catalogue, a2=CheckOutcome.NOT_MEASURABLE)
    reports = [RouteReport(route="pagespeed", result=Result.FAILED, reason="HTTP 429")]

    _feedback(routes, places).checks_done(
        results, catalogue, frozenset(), reports, paid_enabled=False
    )

    assert places.notices == [] and places.board == []


def test_nothing_is_announced_when_every_check_has_a_value_or_is_open(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = _all_results(catalogue)

    _feedback(routes, places).checks_done(
        results, catalogue, frozenset({"j9"}), [], paid_enabled=False
    )

    assert places.notices == [] and places.board == []


def test_the_overview_lists_every_check_with_number_id_name_result_and_reason(
    routes: RouteMap, places: Places, catalogue: Catalogue
) -> None:
    results = [
        _result("a2", CheckOutcome.PASSED),
        _result("a4", CheckOutcome.NOT_MEASURABLE, reason="no_website"),
    ]

    _feedback(routes, places).checks_done(results, catalogue, frozenset(), [], True)

    (table,) = places.overview
    lines = table.splitlines()
    assert lines[0] == "| Nr | Kennung | Prüfung | Ergebnis | Grund |"
    assert lines[1] == "|---|---|---|---|---|"
    assert len(lines) == 2 + CHECK_COUNT
    assert lines[2] == "| 1 | a2 | Die Seite lädt am Handy schnell und ruhig | gemessen |  |"
    assert lines[3].startswith("| 2 | a4 | Google kann jede Seite aufnehmen | fehlgeschlagen | ")


def test_a_measurement_running_event_names_step_supplier_and_stage(
    routes: RouteMap, places: Places
) -> None:
    feedback = _feedback(routes, places)

    feedback.measurement_running("website", "Website")
    feedback.measurement_running("website", "Website", "pruefen")

    first, second = places.file
    assert first.route == second.route == "messung:website"
    assert (first.phase, first.result, first.reason) == ("messung", Result.RUNNING, None)
    assert first.protocol == {"step": "website", "supplier": "Website", "stage": "holen"}
    assert second.protocol == {"step": "website", "supplier": "Website", "stage": "pruefen"}


def test_the_new_steps_have_a_label_in_their_event(routes: RouteMap, places: Places) -> None:
    feedback = _feedback(routes, places)

    for step in ("listings", "ads_google", "ads_meta"):
        feedback.measurement_running(step, "x")

    assert [event.label for event in places.file] == [
        "Karten-Einträge (Apple, Bing)",
        "Google-Anzeigen",
        "Facebook- und Instagram-Anzeigen",
    ]


def test_the_evaluation_event_marks_the_start_of_the_evaluation(
    routes: RouteMap, places: Places
) -> None:
    _feedback(routes, places).evaluation_running()

    (event,) = places.file
    assert (event.phase, event.result, event.route) == (
        "auswertung",
        Result.RUNNING,
        "auswertung",
    )
    assert event.protocol == {"stage": "auswertung"}
    assert places.database == [[event]]


def test_a_control_event_belongs_to_its_check_with_name_and_number(
    routes: RouteMap, places: Places
) -> None:
    protocol = {"stage": "kontrolle_wartet", "attempt": 2}

    _feedback(routes, places).control_event(
        "a2", Result.WARNING, "Kontrolle wartet – Versuch 2: down", protocol
    )

    (event,) = places.file
    assert event.route == "kontrolle:a2"
    assert (event.check_id, event.check_number, event.label) == (
        "a2",
        1,
        "Die Seite lädt am Handy schnell und ruhig",
    )
    assert (event.phase, event.result) == ("auswertung", Result.WARNING)
    assert event.reason == "Kontrolle wartet – Versuch 2: down"
    assert event.protocol == protocol
