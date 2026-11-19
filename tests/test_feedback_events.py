"""Tests for the wording of the feedback after every measurement.

Context: Pure functions and models; no files, no network. | Input: Reasons and events. |
Output: Assertions.
"""

from datetime import UTC, datetime

import pytest

from radarauditor.errors import LoginWallError, SearchBlockedError, TransientCollectionError
from radarauditor.feedback.events import (
    AffectedCheck,
    AuditEvent,
    Result,
    check_line,
    failure_line,
    plain_reason,
    reason_of_code,
)

NOW = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def _checks(count: int) -> list[AffectedCheck]:
    return [
        AffectedCheck(number=number, id=f"j{number}", name=f"Prüfung {number}")
        for number in range(8, 8 + count)
    ]


def _route_event(
    checks: list[AffectedCheck], reason: str | None = "Google zeigt eine Sicherheitsabfrage"
) -> AuditEvent:
    return AuditEvent(
        time=NOW,
        kind="route",
        phase="messung",
        result=Result.FAILED,
        reason=reason,
        stage=1,
        route="google_search",
        label="Google-Suche",
        checks=checks,
    )


def test_results_carry_the_german_words_of_the_decision() -> None:
    assert [result.value for result in Result] == [
        "gemessen",
        "fehlgeschlagen",
        "ausgesetzt",
        "Schritt mit Daniel",
        "offen",
        "neu gestartet",
        "Warnung",
        "läuft",
        "gültig",
        "ungültig",
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("www.google.de shows a bot check", "Google zeigt eine Sicherheitsabfrage"),
        ("www.bing.com shows a bot check", "Bing zeigt eine Sicherheitsabfrage"),
        ("www.example.org shows a bot check", "www.example.org zeigt eine Sicherheitsabfrage"),
        (
            SearchBlockedError("www.google.com shows a bot check"),
            "Google zeigt eine Sicherheitsabfrage",
        ),
        (LoginWallError("chatgpt asks to sign in"), "Die Seite verlangt eine Anmeldung"),
        (
            "PageSpeed Insights (mobile) answered HTTP 429",
            "Der Dienst hat zu viele Anfragen abgelehnt (Fehler 429)",
        ),
        ("answered HTTP 403", "Der Dienst hat den Zugriff verweigert (Fehler 403)"),
        ("daily cap reached", "Das Tageslimit dieser Quelle ist erreicht"),
        ("DataForSEO credentials are missing", "Die Zugangsdaten fehlen"),
        ("customer has no keywords", "Für den Kunden sind keine Suchbegriffe hinterlegt"),
        ("customer has no website", "Der Kunde hat keine Website angegeben"),
        ("customer has no location", "Für den Kunden ist kein Standort hinterlegt"),
        (
            TransientCollectionError("the page timed out"),
            "Die Antwort kam nicht rechtzeitig (Zeitüberschreitung)",
        ),
        ("no assistant answered: rejected", "Kein Assistent hat geantwortet"),
        ("something unexpected", "Technischer Fehler: something unexpected"),
        (None, "Grund unbekannt"),
    ],
)
def test_plain_reason_turns_technical_errors_into_everyday_words(
    error: str | BaseException | None, expected: str
) -> None:
    """Known errors get a plain sentence, unknown ones keep their text behind a clear label."""
    assert plain_reason(error) == expected


def test_reason_of_code_explains_the_codes_of_the_checks() -> None:
    """A check that found nothing to measure names why, in plain German."""
    assert reason_of_code("no_website") == "Die Website konnte nicht gelesen werden"
    assert reason_of_code("directory_not_checked") == "Das Verzeichnis wurde nicht abgefragt"
    assert (
        reason_of_code("never_heard_of_it")
        == "Es lagen keine auswertbaren Daten vor (never_heard_of_it)"
    )


def test_failure_line_names_route_checks_and_reason() -> None:
    """The line lists the first three checks and counts the rest."""
    event = _route_event(_checks(5))

    assert failure_line(event) == (
        "Google-Suche (Nr 8 j8, Nr 9 j9, Nr 10 j10 und 2 weitere): "
        "Google zeigt eine Sicherheitsabfrage"
    )


def test_failure_line_with_three_checks_lists_them_all() -> None:
    """Exactly three checks need no counting."""
    assert failure_line(_route_event(_checks(3))).startswith(
        "Google-Suche (Nr 8 j8, Nr 9 j9, Nr 10 j10): "
    )


def test_failure_line_with_four_checks_counts_the_fourth() -> None:
    """One check more than three is counted, not listed."""
    assert failure_line(_route_event(_checks(4))).startswith(
        "Google-Suche (Nr 8 j8, Nr 9 j9, Nr 10 j10 und 1 weitere): "
    )


def test_failure_line_without_reason_says_so() -> None:
    """A failure never goes out without a reason."""
    assert failure_line(_route_event(_checks(1), reason=None)).endswith(": Grund unbekannt")


def test_check_line_names_number_id_name_and_reason() -> None:
    """A check line reads like the note of the board."""
    event = AuditEvent(
        time=NOW,
        kind="check",
        phase="auswertung",
        result=Result.FAILED,
        reason="Das Verzeichnis wurde nicht abgefragt",
        stage=1,
        route="social_profiles",
        label="Facebook und Instagram mit denselben Daten",
        check_number=45,
        check_id="h5",
    )

    assert check_line(event) == (
        "Nr 45 h5 Facebook und Instagram mit denselben Daten: Das Verzeichnis wurde nicht abgefragt"
    )
