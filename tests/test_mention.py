import pytest

from radarauditor.collectors.mention import mentions_business, name_forms

NAME = "Dachdeckerei Muster GmbH"
DOMAIN = "https://www.dachdeckerei-muster.de/"


def test_a_two_word_name_has_its_reverse_and_its_joined_form() -> None:
    assert name_forms(NAME) == [
        "dachdeckerei muster",
        "muster dachdeckerei",
        "dachdeckereimuster",
    ]


def test_a_three_word_name_is_reordered_too() -> None:
    assert name_forms("Max Muster Dach") == ["max muster dach", "dach muster max", "maxmusterdach"]


def test_a_long_name_keeps_only_its_written_form() -> None:
    assert name_forms("Erste Zweite Dritte Vierte Dach") == ["erste zweite dritte vierte dach"]


@pytest.mark.parametrize("name", ["", "   ", "GmbH"])
def test_a_name_without_words_has_no_form(name: str) -> None:
    assert name_forms(name) == []


@pytest.mark.parametrize(
    "text",
    [
        "Ich empfehle die Dachdeckerei Muster aus Münster.",
        "Empfehlenswert: Muster Dachdeckerei, Telefon 0251 123456",
        "Bei DACHDECKEREI MUSTER GmbH bekommen Sie ein Angebot.",
        "Die Dachdeckereimuster hat gute Bewertungen.",
        "**Dachdeckerei Muster** (Münster)",
    ],
)
def test_the_name_is_found_in_its_written_forms(text: str) -> None:
    assert mentions_business(text, [], NAME) is True


@pytest.mark.parametrize(
    "text",
    [
        "Fragen Sie einen Dachdecker in Münster.",
        "Die Dachdeckerei Musterhausen ist bekannt.",
        "Dachdeckerei Schmidt und Muster Bau.",
        "",
    ],
)
def test_other_text_is_no_mention(text: str) -> None:
    assert mentions_business(text, [], NAME) is False


def test_the_domain_in_the_text_counts_when_the_domain_is_known() -> None:
    text = "Mehr Informationen gibt es auf dachdeckerei-muster.de im Netz."
    assert mentions_business(text, [], "Muster Bedachungen", DOMAIN) is True
    assert mentions_business(text, [], "Muster Bedachungen") is False


def test_a_cited_page_of_the_domain_counts() -> None:
    sources = ["https://www.gelbeseiten.de/x", "https://dachdeckerei-muster.de/leistungen/"]
    assert mentions_business("Kein Name im Text.", sources, "Muster Bedachungen", DOMAIN) is True


def test_an_unrelated_cited_page_does_not_count() -> None:
    sources = ["https://www.gelbeseiten.de/x", "https://dach-schmidt.de/"]
    assert mentions_business("Kein Name im Text.", sources, "Muster Bedachungen", DOMAIN) is False


def test_a_citation_of_the_domain_without_the_known_domain_does_not_count() -> None:
    sources = ["https://dachdeckerei-muster.de/leistungen/"]
    assert mentions_business("Kein Name im Text.", sources, "Muster Bedachungen") is False
