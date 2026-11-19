from collections.abc import Callable
from functools import cache
from importlib import resources

from radarauditor.collectors.identity import fold
from radarauditor.collectors.snapshot import PageSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    NO_PAID_DATA,
    CheckFunction,
    CheckReturn,
    Thresholds,
    business_city,
    combine,
    content_pages,
    dispatch,
    h1_texts,
    listed,
    not_measurable,
    service_terms,
    tokens,
)
from radarauditor.scoring.checks.crawl import crawled, duplicates, worst

GAP_MIN_VOLUME = 10
GAP_FAIL_COUNT = 10
FUNCTION_WORDS = frozenset(
    {"in", "im", "an", "am", "auf", "aus", "bei", "mit", "von", "vom", "zu", "zum", "zur"}
    | {"fuer", "und", "oder", "der", "die", "das", "den", "dem", "ein", "eine", "nach"}
    | {"um", "ueber", "unter"}
)
PLACE_LIST_PACKAGE = "radarauditor.scoring.checks"
PLACE_LIST_FILE = "orte_de.txt"
MAX_PLACE_WORDS = 4
NON_OFFER_WORDS = frozenset(
    {"gehalt", "gehaelter", "verdienst", "lohn", "ausbildung", "azubi", "lehrstelle"}
    | {"stellen", "stellenangebote", "stellenanzeigen", "job", "jobs", "jobangebote"}
    | {"karriere", "bewerbung", "praktikum", "minijob", "werkstudent", "umschulung"}
    | {"weiterbildung", "studium", "duales"}
)


@cache
def _place_names() -> frozenset[str]:
    text = resources.files(PLACE_LIST_PACKAGE).joinpath(PLACE_LIST_FILE).read_text(encoding="utf-8")
    return frozenset(
        " ".join(tokens(line)) for line in text.splitlines() if line and not line.startswith("#")
    )


def _places_in(phrase: str, offer_word_sets: list[set[str]]) -> set[str]:
    words = tokens(phrase)
    offer_words = set[str]().union(*offer_word_sets) | FUNCTION_WORDS
    found: set[str] = set()
    for start in range(len(words)):
        for length in range(1, MAX_PLACE_WORDS + 1):
            candidate = " ".join(words[start : start + length])
            if len(words[start : start + length]) == length and candidate in _place_names():
                if not set(candidate.split()) <= offer_words:
                    found.add(candidate)
    return found


def _not_about_offer(phrase: str, city: str | None, offer_word_sets: list[set[str]]) -> bool:
    if set(tokens(phrase)) & NON_OFFER_WORDS:
        return True
    if city is None:
        return False
    own = " ".join(tokens(city))
    return any(place != own for place in _places_in(phrase, offer_word_sets))


def _offer_word_sets(terms: dict[str, list[str]]) -> list[set[str]]:
    word_sets = [set(words) - FUNCTION_WORDS for words in terms.values()]
    return [words for words in word_sets if words]


def _about_offer(phrase: str, offer_word_sets: list[set[str]]) -> bool:
    words = set(tokens(phrase))
    return any(offer_words <= words for offer_words in offer_word_sets)


def _contains(text: str | None, words: list[str]) -> bool:
    folded = fold(text or "")
    return all(word in folded for word in words)


def _body(page: PageSnapshot) -> str:
    return page.body_text if page.body_text is not None else page.text_excerpt


def _title_problems(pages: list[PageSnapshot]) -> dict[str, object]:
    shared = duplicates((str(page.url), page.title) for page in pages)
    return {
        "pages_without_title": listed(
            str(page.url) for page in pages if not (page.title or "").strip()
        ),
        "pages_with_same_title": {key: listed(urls) for key, urls in list(shared.items())[:5]},
        "title_problem_count": sum(1 for page in pages if not (page.title or "").strip())
        + sum(len(urls) for urls in shared.values()),
    }


def _term_places(pages: list[PageSnapshot], words: list[str], city: str) -> dict[str, object]:
    def hits(texts: Callable[[PageSnapshot], list[str]]) -> list[str]:
        return [
            str(page.url) for page in pages if any(_contains(text, words) for text in texts(page))
        ]

    prominent = [
        str(page.url)
        for page in pages
        if (focus := " ".join([page.title or "", *h1_texts(page)]))
        and _contains(focus, words)
        and _contains(focus, [city])
    ]
    in_text = [
        str(page.url)
        for page in pages
        if _contains(_body(page), words) and _contains(_body(page), [city])
    ]
    return {
        "title": hits(lambda page: [page.title or ""]),
        "main_heading": hits(lambda page: h1_texts(page)),
        "text": hits(lambda page: [_body(page)]),
        "image_description": hits(lambda page: [image.alt or "" for image in page.images or []]),
        "link_text": hits(lambda page: [link.text for link in page.links or []]),
        "title_or_heading_with_city": prominent,
        "text_with_city": in_text,
    }


def terms_in_text(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    terms = service_terms(raw)
    if not terms:
        return not_measurable("no_search_phrases")
    city = business_city(raw)
    if city is None:
        return not_measurable("no_city")
    pages = content_pages(website)
    folded_city = fold(city)
    breakdown: dict[str, object] = {}
    outcomes: list[CheckOutcome] = []
    for phrase, words in terms.items():
        places = _term_places(pages, words, folded_city)
        prominent, in_text = (
            bool(places["title_or_heading_with_city"]),
            bool(places["text_with_city"]),
        )
        if prominent and in_text:
            outcomes.append(CheckOutcome.PASSED)
        elif prominent or in_text:
            outcomes.append(CheckOutcome.PARTIAL)
        else:
            outcomes.append(CheckOutcome.FAILED)
        breakdown[phrase] = {
            place: listed(urls) if isinstance(urls, list) else urls
            for place, urls in places.items()
        }
    title_problems = _title_problems(pages)
    outcome = combine(outcomes)
    if title_problems["title_problem_count"]:
        outcome = worst(outcome, CheckOutcome.PARTIAL)
    return outcome, {
        "phrases_total": len(terms),
        "city": city,
        "by_phrase": breakdown,
        **title_problems,
    }


def paid_keyword_gap(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.paid is None:
        return not_measurable(NO_PAID_DATA)
    if raw.paid.keyword_gap is None:
        return not_measurable("no_keyword_gap")
    offer_word_sets = _offer_word_sets(service_terms(raw))
    if not offer_word_sets:
        return not_measurable("no_search_phrases")
    city = business_city(raw)
    relevant = [
        gap
        for gap in raw.paid.keyword_gap
        if gap.volume >= GAP_MIN_VOLUME
        and _about_offer(gap.phrase, offer_word_sets)
        and not _not_about_offer(gap.phrase, city, offer_word_sets)
    ]
    if not relevant:
        outcome = CheckOutcome.PASSED
    elif len(relevant) < GAP_FAIL_COUNT:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    return outcome, {
        "gap_keywords": len(relevant),
        "top_gap_keywords": [gap.model_dump() for gap in relevant[:10]],
        "min_volume": GAP_MIN_VOLUME,
        "ignored_without_offer_link": sum(
            1
            for gap in raw.paid.keyword_gap
            if gap.volume >= GAP_MIN_VOLUME
            and (
                not _about_offer(gap.phrase, offer_word_sets)
                or _not_about_offer(gap.phrase, city, offer_word_sets)
            )
        ),
    }


CHECKS: dict[str, CheckFunction] = {
    "c4": terms_in_text,
    "c12": paid_keyword_gap,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
