from collections.abc import Callable

import pytest

from radarauditor.collectors.snapshot import GapKeyword, LinkInfo, PageSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.c_inhalt import run

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "c4": {},
    "c12": {},
}
PAID = {"c12"}
Edit = Callable[..., RawAudit]


def _pages(raw: RawAudit) -> list[PageSnapshot]:
    assert raw.website is not None
    return list(raw.website.pages)


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS[check_id])[0] is (
        CheckOutcome.NOT_MEASURABLE
    )


@pytest.mark.parametrize("check_id", sorted(PAID))
def test_paid_checks_without_paid_data(raw_audit_full: RawAudit, check_id: str) -> None:
    raw = raw_audit_full.model_copy(update={"paid": None})
    assert run(check_id, raw, THRESHOLDS[check_id])[1] == {"reason": "no_paid_data"}


def _with_paid(raw: RawAudit, **updates: object) -> RawAudit:
    assert raw.paid is not None
    return raw.model_copy(update={"paid": raw.paid.model_copy(update=updates)})


def _gap(count: int, volume: int, topic: str = "dachdecker") -> list[GapKeyword]:
    return [
        GapKeyword(
            phrase=f"{topic} begriff {index}",
            volume=volume,
            competitor_domain="dach-schmidt.de",
            competitor_position=index + 1,
        )
        for index in range(count)
    ]


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (_gap(3, 9), CheckOutcome.PASSED),
        (_gap(9, 10), CheckOutcome.PARTIAL),
        (_gap(12, 50), CheckOutcome.FAILED),
    ],
)
def test_c12_counts_gap_keywords_with_volume(
    raw_audit_full: RawAudit, gap: list[GapKeyword], expected: CheckOutcome
) -> None:
    outcome, measured = run("c12", _with_paid(raw_audit_full, keyword_gap=gap), {})
    assert outcome is expected
    assert len(measured["top_gap_keywords"]) <= 10


def test_c12_keywords_that_have_nothing_to_do_with_the_offer_are_ignored(
    raw_audit_full: RawAudit,
) -> None:
    gap = _gap(12, 4400, topic="keramik bemalen") + _gap(2, 50)
    outcome, measured = run("c12", _with_paid(raw_audit_full, keyword_gap=gap), {})
    assert outcome is CheckOutcome.PARTIAL
    assert measured["gap_keywords"] == 2
    assert measured["ignored_without_offer_link"] == 12
    assert all("keramik" not in item["phrase"] for item in measured["top_gap_keywords"])


def test_c12_only_unrelated_keywords_leave_nothing_to_report(raw_audit_full: RawAudit) -> None:
    gap = _gap(12, 4400, topic="feuershow")
    outcome, measured = run("c12", _with_paid(raw_audit_full, keyword_gap=gap), {})
    assert outcome is CheckOutcome.PASSED
    assert measured["gap_keywords"] == 0
    assert measured["ignored_without_offer_link"] == 12


def test_c12_without_gap_data(raw_audit_full: RawAudit) -> None:
    assert run("c12", _with_paid(raw_audit_full, keyword_gap=None), {})[1] == {
        "reason": "no_keyword_gap"
    }


def _with_phrase(raw: RawAudit, phrase: str) -> RawAudit:
    return raw.model_copy(
        update={"serps": [serp.model_copy(update={"phrase": phrase}) for serp in raw.serps]}
    )


def test_c4_a_phrase_that_appears_nowhere_fails(raw_audit_full: RawAudit) -> None:
    raw = _with_phrase(raw_audit_full, "Zimmerei Münster")
    outcome, measured = run("c4", raw, THRESHOLDS["c4"])
    assert outcome is CheckOutcome.FAILED
    places = measured["by_phrase"]["Zimmerei Münster"]
    assert places["text"] == []
    assert places["title"] == []
    assert places["main_heading"] == []
    assert places["image_description"] == []
    assert places["link_text"] == []


def test_c4_a_phrase_in_the_text_only_is_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    pages = [
        page.model_copy(update={"title": f"Seite {index}", "headings": [(1, "Willkommen")]})
        for index, page in enumerate(_pages(raw_audit_full))
    ]
    raw = with_website(_with_phrase(raw_audit_full, "Dachdecker Münster"), pages=pages)
    outcome, measured = run("c4", raw, THRESHOLDS["c4"])
    assert outcome is CheckOutcome.PARTIAL
    places = measured["by_phrase"]["Dachdecker Münster"]
    assert places["title_or_heading_with_city"] == []
    assert places["text_with_city"] != []


def test_c4_a_phrase_in_title_and_text_passes_and_names_every_place(
    raw_audit_full: RawAudit,
) -> None:
    raw = _with_phrase(raw_audit_full, "Dachdecker Münster")
    outcome, measured = run("c4", raw, THRESHOLDS["c4"])
    assert outcome is CheckOutcome.PASSED
    places = measured["by_phrase"]["Dachdecker Münster"]
    assert places["image_description"] != []
    assert places["title"] != []


def test_c4_a_phrase_in_the_link_text_is_named(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    pages = [
        page.model_copy(
            update={"links": [LinkInfo(kind="href", text="Zimmerei", url="https://x.de/")]}
        )
        for page in _pages(raw_audit_full)
    ]
    raw = with_website(_with_phrase(raw_audit_full, "Zimmerei Münster"), pages=pages)
    places = run("c4", raw, THRESHOLDS["c4"])[1]["by_phrase"]["Zimmerei Münster"]
    assert places["link_text"] != []


def test_c4_a_title_shared_by_two_pages_lowers_a_passing_result_to_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    pages = _pages(raw_audit_full)
    pages[3] = pages[3].model_copy(update={"title": pages[2].title})
    raw = with_website(_with_phrase(raw_audit_full, "Dachdecker Münster"), pages=pages)
    outcome, measured = run("c4", raw, THRESHOLDS["c4"])
    assert outcome is CheckOutcome.PARTIAL
    assert measured["title_problem_count"] == 2
    assert list(measured["pages_with_same_title"].values()) == [
        [str(pages[2].url), str(pages[3].url)]
    ]


def test_c4_a_page_without_title_lowers_a_passing_result_to_partial(
    raw_audit_full: RawAudit, with_website: Edit
) -> None:
    pages = _pages(raw_audit_full)
    pages[3] = pages[3].model_copy(update={"title": " "})
    raw = with_website(_with_phrase(raw_audit_full, "Dachdecker Münster"), pages=pages)
    outcome, measured = run("c4", raw, THRESHOLDS["c4"])
    assert outcome is CheckOutcome.PARTIAL
    assert measured["pages_without_title"] == [str(pages[3].url)]


def test_c4_has_no_keyword_density_rule(raw_audit_full: RawAudit, with_website: Edit) -> None:
    pages = _pages(raw_audit_full)
    stuffed = " ".join(["dachdecker"] * 90)
    pages[3] = pages[3].model_copy(update={"body_text": stuffed})
    raw = with_website(_with_phrase(raw_audit_full, "Dachdecker Münster"), pages=pages)
    assert run("c4", raw, THRESHOLDS["c4"])[0] is CheckOutcome.PASSED


def _gap_of(*phrases: str, volume: int = 50) -> list[GapKeyword]:
    return [
        GapKeyword(
            phrase=phrase,
            volume=volume,
            competitor_domain="dach-schmidt.de",
            competitor_position=1,
        )
        for phrase in phrases
    ]


def _c12_phrases(raw: RawAudit, own_phrase: str, *gap_phrases: str) -> list[str]:
    raw = _with_phrase(raw, own_phrase).model_copy(update={"map_rankings": []})
    outcome, measured = run("c12", _with_paid(raw, keyword_gap=_gap_of(*gap_phrases)), {})
    assert outcome is not CheckOutcome.NOT_MEASURABLE
    return [item["phrase"] for item in measured["top_gap_keywords"]]


@pytest.mark.parametrize(
    ("own_phrase", "gap_phrase"),
    [
        ("maler in münster", "töpfern in münster"),
        ("maler münster", "keramik in münster"),
        ("kunst kaufen münster", "keramik kaufen münster"),
        ("maler münster", "malerei kurs münster"),
        ("maler münster", "kaufen"),
        ("kunst kaufen münster", "kunst"),
        ("kunst kaufen münster", "kaufen"),
    ],
)
def test_c12_a_word_that_only_a_gap_keyword_shares_with_the_offer_is_no_link_to_it(
    raw_audit_full: RawAudit, own_phrase: str, gap_phrase: str
) -> None:
    assert _c12_phrases(raw_audit_full, own_phrase, gap_phrase) == []


@pytest.mark.parametrize(
    ("own_phrase", "gap_phrase"),
    [
        ("maler in münster", "maler kosten"),
        ("maler münster", "maler in münster"),
        ("kunst kaufen münster", "kaufen kunst online"),
        ("atelier münster", "atelier augusta"),
        ("abstrakte kunst münster", "abstrakte kunst kaufen"),
    ],
)
def test_c12_a_gap_keyword_with_every_word_of_an_own_search_phrase_is_about_the_offer(
    raw_audit_full: RawAudit, own_phrase: str, gap_phrase: str
) -> None:
    assert _c12_phrases(raw_audit_full, own_phrase, gap_phrase) == [gap_phrase]


@pytest.mark.parametrize(
    "gap_phrase",
    [
        "maler münster bielefeld",
        "maler münster frankfurt am main",
        "maler münster gehalt",
        "maler ausbildung münster",
        "maler münster stellenangebote",
        "maler jobs münster",
    ],
)
def test_c12_a_foreign_place_or_a_salary_training_or_job_search_is_not_the_offer(
    raw_audit_full: RawAudit, gap_phrase: str
) -> None:
    assert _c12_phrases(raw_audit_full, "maler münster", gap_phrase) == []


def test_c12_keeps_the_place_of_the_business_and_ignores_a_word_that_is_part_of_the_offer(
    raw_audit_full: RawAudit,
) -> None:
    phrases = _c12_phrases(raw_audit_full, "maler münster", "maler münster kosten")
    assert phrases == ["maler münster kosten"]


def test_c12_keeps_every_term_when_the_place_of_the_business_is_not_known(
    raw_audit_full: RawAudit,
) -> None:
    assert raw_audit_full.business is not None
    raw = raw_audit_full.model_copy(
        update={"business": raw_audit_full.business.model_copy(update={"city": None})}
    )
    unknown = "maler münster bielefeld"
    assert _c12_phrases(raw, "maler münster", unknown) == [unknown]
    assert _c12_phrases(raw, "maler münster", "maler münster gehalt") == []


def test_c12_function_words_alone_are_no_offer(raw_audit_full: RawAudit) -> None:
    raw = _with_phrase(raw_audit_full, "in münster").model_copy(update={"map_rankings": []})
    result = run("c12", _with_paid(raw, keyword_gap=_gap_of("in münster")), {})
    assert result == (CheckOutcome.NOT_MEASURABLE, {"reason": "no_search_phrases"})
