from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from radarauditor import cli, keyword_import
from radarauditor.db.models import Customer, Keyword, KeywordScope
from radarauditor.keyword_import import PlannedKeyword

runner = CliRunner()
CUSTOMER_ID = uuid4()
CUSTOMER = Customer(
    id=CUSTOMER_ID,
    name="Katja Karsch",
    website=None,
    industry="",
    city="Münster",
    created_at=datetime(2026, 11, 13, tzinfo=UTC),
)
PLANNER_EXPORT = (
    "Keyword Stats 2026-11-13 at 19_02_11\n"
    "1. November 2025 - 31. Oktober 2026\n"
    "Keyword\tCurrency\tAvg. monthly searches\tThree month change\tCompetition\n"
    "abstrakte kunst\tEUR\t12100\t0%\tHoch\n"
    "lichtkunst  münster\tEUR\t1.300\t0%\tNiedrig\n"
    "kunst kaufen\tEUR\t1K – 10K\t0%\tHoch\n"
    "Abstrakte Kunst\tEUR\t50\t0%\tHoch\n"
    "atelier besuch\tEUR\t\t\t\n"
    "\tEUR\t10\t\t\n"
)


def _keyword(phrase: str, primary: bool = False) -> Keyword:
    return Keyword(
        id=uuid4(),
        customer_id=CUSTOMER_ID,
        phrase=phrase,
        city="Münster",
        monthly_volume=None,
        is_primary=primary,
    )


def _repositories(stored: list[Keyword]) -> MagicMock:
    repositories = MagicMock()
    repositories.customer.return_value = CUSTOMER
    repositories.keywords.return_value = stored
    repositories.add_keywords.side_effect = (
        lambda customer_id, phrases, city, first_primary, scope: [
            _keyword(phrase) for phrase in phrases
        ]
    )
    return repositories


def test_the_planner_export_in_utf16_gives_phrases_and_volumes_once_each(tmp_path: Path) -> None:
    export = tmp_path / "Keyword Stats.csv"
    export.write_text(PLANNER_EXPORT, encoding="utf-16")

    assert keyword_import.read_planner_export(export) == [
        PlannedKeyword("abstrakte kunst", 12100),
        PlannedKeyword("lichtkunst münster", 1300),
        PlannedKeyword("kunst kaufen", 1000),
        PlannedKeyword("atelier besuch", None),
    ]


def test_a_comma_separated_export_with_german_headings_is_read(tmp_path: Path) -> None:
    export = tmp_path / "ideen.csv"
    export.write_text(
        'Keyword,Währung,Durchschn. Suchanfragen pro Monat\n"kunst, abstrakt",EUR,"2.400"\n',
        encoding="utf-8-sig",
    )

    assert keyword_import.read_planner_export(export) == [PlannedKeyword("kunst, abstrakt", 2400)]


def test_an_export_without_volume_column_gives_phrases_without_volume(tmp_path: Path) -> None:
    export = tmp_path / "liste.csv"
    export.write_text("Keyword\nlichtkunst\n", encoding="utf-8")

    assert keyword_import.read_planner_export(export) == [PlannedKeyword("lichtkunst", None)]


def test_a_file_without_keyword_column_is_refused(tmp_path: Path) -> None:
    export = tmp_path / "falsch.csv"
    export.write_text("Begriff;Volumen\nkunst;10\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no table with a column named Keyword"):
        keyword_import.read_planner_export(export)


@pytest.mark.parametrize(
    ("text", "volume"),
    [
        ("0", 0),
        ("880", 880),
        ("1.300", 1300),
        ("12,100", 12100),
        ("10 – 100", 10),
        ("1K – 10K", 1000),
        ("1 Tsd. – 10 Tsd.", 1000),
        ("1M – 10M", 1_000_000),
        ("1 Mio. – 10 Mio.", 1_000_000),
        ("", None),
        ("–", None),
    ],
)
def test_volume_of_reads_numbers_and_takes_the_lower_end_of_a_range(
    text: str, volume: int | None
) -> None:
    assert keyword_import.volume_of(text) == volume


def test_import_adds_only_new_phrases_and_sets_the_volumes_of_all() -> None:
    kept = _keyword("Abstrakte Kunst", primary=True)
    repositories = _repositories([kept])
    planned = [
        PlannedKeyword("abstrakte kunst", 12100),
        PlannedKeyword("lichtkunst", 1300),
        PlannedKeyword("atelier besuch", None),
    ]

    count = keyword_import.import_keywords(repositories, CUSTOMER, planned)

    assert count == keyword_import.ImportCount(added=2, already_stored=1, with_volume=2)
    repositories.add_keywords.assert_called_once_with(
        CUSTOMER_ID,
        ["lichtkunst", "atelier besuch"],
        "Münster",
        first_primary=False,
        scope=KeywordScope.LOCAL,
    )
    volumes = {call.args for call in repositories.update_keyword_volume.call_args_list}
    assert (kept.id, 12100) in volumes
    assert {volume for _, volume in volumes} == {12100, 1300}
    assert {call[0] for call in repositories.method_calls} == {
        "keywords",
        "add_keywords",
        "update_keyword_volume",
    }


def test_import_makes_the_first_phrase_primary_when_the_customer_has_none() -> None:
    repositories = _repositories([_keyword("alt")])

    keyword_import.import_keywords(repositories, CUSTOMER, [PlannedKeyword("neu", 5)])

    assert repositories.add_keywords.call_args.kwargs == {
        "first_primary": True,
        "scope": KeywordScope.LOCAL,
    }


def test_the_command_imports_the_file_for_the_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "Keyword Stats.csv"
    export.write_text(PLANNER_EXPORT, encoding="utf-16")
    repositories = _repositories([])
    monkeypatch.setattr(cli, "_context", lambda: (None, repositories))

    result = runner.invoke(cli.app, ["keywords", "import", str(CUSTOMER_ID), str(export)])

    assert result.exit_code == 0
    assert result.stdout.strip() == "4 added, 0 already stored, 3 with search volume"
    repositories.customer.assert_called_once_with(UUID(str(CUSTOMER_ID)))


@pytest.mark.parametrize("content", [None, "Begriff\nkunst\n"])
def test_the_command_refuses_a_missing_or_unusable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    export = tmp_path / "export.csv"
    if content is not None:
        export.write_text(content, encoding="utf-8")
    repositories = _repositories([])
    monkeypatch.setattr(cli, "_context", lambda: (None, repositories))

    result = runner.invoke(cli.app, ["keywords", "import", str(CUSTOMER_ID), str(export)])

    assert result.exit_code != 0
    repositories.add_keywords.assert_not_called()


@pytest.mark.parametrize(
    ("text", "volume"),
    [
        ("1,5 Tsd. – 10 Tsd.", 1500),
        ("1.5K", 1500),
        ("2,25 Mio.", 2_250_000),
        ("1.300 Tsd.", 1_300_000),
        ("1,5", 15),
    ],
)
def test_volume_of_reads_a_fraction_before_a_shortened_thousand_or_million(
    text: str, volume: int
) -> None:
    assert keyword_import.volume_of(text) == volume


@pytest.mark.parametrize(
    ("phrase", "city", "expected"),
    [
        ("lichtkunst münster", "Münster", "lichtkunst"),
        ("Münster lichtkunst", "Münster", "lichtkunst"),
        ("kunst münster kaufen", "Münster", "kunst münster kaufen"),
        ("münster", "Münster", "münster"),
        ("galerie bad oeynhausen", "Bad Oeynhausen", "galerie"),
        ("abstrakte kunst in münster", "Münster", "abstrakte kunst"),
        ("in münster", "Münster", "in"),
        ("münsterland kunst", "Münster", "münsterland kunst"),
        ("lichtkunst münster", "", "lichtkunst münster"),
    ],
)
def test_the_city_of_the_customer_is_left_out_at_the_start_or_end_of_a_phrase(
    phrase: str, city: str, expected: str
) -> None:
    kept = keyword_import.without_city([PlannedKeyword(phrase, 5)], city)

    assert kept == [PlannedKeyword(expected, 5)]


def test_phrases_that_differ_only_by_the_city_are_stored_once_with_the_first_volume() -> None:
    planned = [PlannedKeyword("lichtkunst", 90), PlannedKeyword("lichtkunst münster", 10)]

    assert keyword_import.without_city(planned, "Münster") == [PlannedKeyword("lichtkunst", 90)]


def test_import_stores_phrases_without_the_city_of_the_customer() -> None:
    repositories = _repositories([])

    keyword_import.import_keywords(
        repositories, CUSTOMER, [PlannedKeyword("lichtkunst münster", 10)]
    )

    assert repositories.add_keywords.call_args.args[1] == ["lichtkunst"]


def test_nationwide_import_keeps_the_city_in_the_phrase_and_stores_the_scope() -> None:
    repositories = _repositories([])

    keyword_import.import_keywords(
        repositories,
        CUSTOMER,
        [PlannedKeyword("kunst kaufen m\u00fcnster", 10)],
        KeywordScope.NATIONWIDE,
    )

    assert repositories.add_keywords.call_args.args[1] == ["kunst kaufen m\u00fcnster"]
    assert repositories.add_keywords.call_args.kwargs["scope"] is KeywordScope.NATIONWIDE


def test_the_command_stores_nationwide_phrases_with_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "planner.tsv"
    export.write_text(PLANNER_EXPORT, encoding="utf-8")
    repositories = _repositories([])
    monkeypatch.setattr(cli, "_context", lambda: (MagicMock(), repositories))

    result = runner.invoke(
        cli.app, ["keywords", "import", str(CUSTOMER_ID), str(export), "--nationwide"]
    )

    assert result.exit_code == 0, result.output
    assert repositories.add_keywords.call_args.kwargs["scope"] is KeywordScope.NATIONWIDE


def test_the_command_stores_local_phrases_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "planner.tsv"
    export.write_text(PLANNER_EXPORT, encoding="utf-8")
    repositories = _repositories([])
    monkeypatch.setattr(cli, "_context", lambda: (MagicMock(), repositories))

    result = runner.invoke(cli.app, ["keywords", "import", str(CUSTOMER_ID), str(export)])

    assert result.exit_code == 0, result.output
    assert repositories.add_keywords.call_args.kwargs["scope"] is KeywordScope.LOCAL
