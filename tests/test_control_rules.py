import re
from collections.abc import Callable

import pytest
from conftest import AUDIT_ID, REPORT_ROOT

from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import ScoringError
from radarauditor.jobs import control_data
from radarauditor.jobs.control_rules import PLACEHOLDER, constant, load_rules, read_rules, render
from radarauditor.scoring.catalog import Catalogue
from radarauditor.scoring.checks import a_technik, c_inhalt, g_bewertungen, i_links
from radarauditor.scoring.evaluator import evaluate

RULES_PATH = REPORT_ROOT / "templates" / control_data.RULES_FILE
NUMBER = re.compile(r"(?<![\w%])\d+(?:,\d+)?(?![\w])")
FIXED_NUMBERS = frozenset(
    {"0", "1", "2", "7", "12", "200", "302", "303", "307", "400", "401", "403", "429", "499"}
    | {"500", "599"}
)
Edit = Callable[..., RawAudit]


def _ids(catalogue: Catalogue) -> set[str]:
    return {check.id for area in catalogue.areas for check in area.checks}


def test_every_check_of_the_catalogue_has_a_rule_and_no_rule_is_left_over(
    catalogue: Catalogue,
) -> None:
    _, checks = read_rules(RULES_PATH)

    assert set(checks) == _ids(catalogue)


def test_every_placeholder_resolves_and_none_is_left_in_the_text(catalogue: Catalogue) -> None:
    rules = load_rules(RULES_PATH, catalogue)

    assert all(len(text) > 20 for text in rules.values())
    assert not [check for check, text in rules.items() if "{" in text or "}" in text]


def test_a_rule_holds_no_number_that_the_code_also_holds() -> None:
    blocks, checks = read_rules(RULES_PATH)
    literal: dict[str, set[str]] = {}
    for name, text in {**blocks, **checks}.items():
        bare = PLACEHOLDER.sub("", text)
        numbers = set(NUMBER.findall(bare)) - FIXED_NUMBERS
        if numbers:
            literal[name] = numbers

    assert literal == {}


def test_every_threshold_of_a_check_is_named_in_its_rule(catalogue: Catalogue) -> None:
    blocks, checks = read_rules(RULES_PATH)
    unnamed: dict[str, list[str]] = {}
    for area in catalogue.areas:
        for check in area.checks:
            expanded = PLACEHOLDER.sub(
                lambda match: (
                    blocks.get(match.group(2), match.group(0))
                    if match.group(1) == "block"
                    else match.group(0)
                ),
                checks[check.id],
            )
            missing = [key for key in check.thresholds if f"{{t:{key}" not in expanded]
            if missing:
                unnamed[check.id] = missing

    assert unnamed == {}


def test_a_changed_limit_in_the_catalogue_changes_the_rule(catalogue: Catalogue) -> None:
    changed = catalogue.model_copy(deep=True)
    j1 = next(check for area in changed.areas for check in area.checks if check.id == "j1")
    j1.thresholds["good"] = 7

    before = load_rules(RULES_PATH, catalogue)["j1"]
    after = load_rules(RULES_PATH, changed)["j1"]

    assert before != after
    assert "Platz 7 oder besser" in after


def test_a_changed_constant_of_the_code_changes_the_rule(
    catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(c_inhalt, "GAP_FAIL_COUNT", 11)

    rule = load_rules(RULES_PATH, catalogue)["c12"]

    assert "weniger als 11: partial; 11 oder mehr: failed" in rule


def test_the_tolerance_of_the_assistant_checks_comes_from_the_shared_constant(
    catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from radarauditor.scoring.checks import common

    monkeypatch.setattr(common, "TOLERANCE", 0.35)

    assert "höchstens 35 Prozent weniger" in load_rules(RULES_PATH, catalogue)["m1"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{block:nowhere}", "unknown block"),
        ("{t:nothing}", "no threshold"),
        ("{c:common.NOTHING}", "no constant"),
        ("{c:nowhere.X}", "no constant"),
        ("{t:good|loud}", "unknown style"),
    ],
)
def test_a_placeholder_without_a_value_is_an_error_not_a_blank(text: str, message: str) -> None:
    with pytest.raises(ScoringError, match=message):
        render(text, {}, {"good": 3}, "x1")


def test_numbers_are_shown_the_german_way() -> None:
    assert render("{t:a}|{t:b|pct}|{t:c}", {}, {"a": 0.25, "b": 0.5, "c": 3}, "x") == "0,25|50|3"
    assert constant("k_assistenten.RECOMMENDATION_MARKERS")
    shown = render("{c:k_assistenten.RECOMMENDATION_MARKERS|list}", {}, {}, "x")
    assert shown.startswith("„") and "“" in shown


def _rule(catalogue: Catalogue, check_id: str) -> str:
    return load_rules(RULES_PATH, catalogue)[check_id]


def _alt_missing_on(raw: RawAudit, with_website: Edit, pages_hit: int) -> RawAudit:
    assert raw.website is not None
    pages = [
        page.model_copy(
            update={
                "images": [
                    image.model_copy(update={"alt": "" if 12 - pages_hit <= index else image.alt})
                    for image in page.images or []
                ]
            }
        )
        for index, page in enumerate(raw.website.pages)
    ]
    return with_website(raw, pages=pages)


def test_a7_the_start_page_decides_and_the_share_of_pages_does_not(
    catalogue: Catalogue, raw_audit_full: RawAudit, with_website: Edit
) -> None:
    sub_pages_only = run_check("a7", _alt_missing_on(raw_audit_full, with_website, 11))
    with_start = run_check("a7", _alt_missing_on(raw_audit_full, with_website, 12))
    rule = _rule(catalogue, "a7")

    assert (sub_pages_only, with_start) == (CheckOutcome.PARTIAL, CheckOutcome.FAILED)
    assert "Ist die Startseite betroffen, gilt failed" in rule
    assert "Sind nur Unterseiten betroffen, gilt partial" in rule
    assert "keine Anteilsregel" in rule


def run_check(check_id: str, raw: RawAudit) -> CheckOutcome:
    from radarauditor.scoring.catalog import load_catalogue
    from radarauditor.scoring.evaluator import measure_check

    catalogue = load_catalogue(REPORT_ROOT / "config" / "pruefkatalog.yaml")
    definition = next(
        check for area in catalogue.areas for check in area.checks if check.id == check_id
    )
    return measure_check(definition, raw)[0]


def test_a11_one_blocked_content_page_is_partial_as_the_rule_says(
    catalogue: Catalogue, raw_audit_full: RawAudit, with_website: Edit
) -> None:
    robots = "User-agent: Google-Extended\nDisallow: /ueber-uns/\n"

    outcome = run_check("a11", with_website(raw_audit_full, robots_txt=robots))
    rule = _rule(catalogue, "a11")

    assert outcome is CheckOutcome.PARTIAL
    assert "Sind nur einzelne Inhaltsseiten gesperrt, gilt partial" in rule
    assert "Ist die Wurzeladresse der Website für die Kennung gesperrt, gilt failed" in rule


def test_a11_a_robots_file_that_answers_with_a_server_error_is_not_measured(
    catalogue: Catalogue, raw_audit_full: RawAudit, with_website: Edit
) -> None:
    raw = with_website(raw_audit_full, robots_txt=None, robots_status=503)

    assert run_check("a11", raw) is CheckOutcome.NOT_MEASURABLE
    assert "Status 429, 500 bis 599 oder gar nicht, ist die Prüfung nicht gemessen" in _rule(
        catalogue, "a11"
    )


def test_i1_compares_with_the_median_and_has_no_partial(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    assert raw_audit_full.paid is not None and raw_audit_full.paid.customer_domain is not None
    own = raw_audit_full.paid.backlinks[raw_audit_full.paid.customer_domain]

    def with_links(domains: int) -> RawAudit:
        assert raw_audit_full.paid is not None
        changed = own.model_copy(update={"referring_domains": domains})
        paid = raw_audit_full.paid.model_copy(
            update={"backlinks": {**raw_audit_full.paid.backlinks, changed.domain: changed}}
        )
        return raw_audit_full.model_copy(update={"paid": paid})

    rule = _rule(catalogue, "i1")

    assert i_links.run("i1", with_links(46), {})[0] is CheckOutcome.PASSED
    assert i_links.run("i1", with_links(45), {})[0] is CheckOutcome.FAILED
    assert "Median der Wettbewerber" in rule
    assert "kein partial" in rule


def test_g1_compares_with_the_mean_and_has_no_partial(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    assert raw_audit_full.place is not None

    def with_count(count: int) -> RawAudit:
        assert raw_audit_full.place is not None
        place = raw_audit_full.place.model_copy(update={"review_count": count})
        return raw_audit_full.model_copy(update={"place": place})

    rule = _rule(catalogue, "g1")

    assert g_bewertungen.run("g1", with_count(80), {})[0] is CheckOutcome.PASSED
    assert g_bewertungen.run("g1", with_count(79), {})[0] is CheckOutcome.FAILED
    assert "Mittelwert" in rule
    assert "kein partial" in rule


def test_b19_has_no_age_limit_in_code_or_rule(
    catalogue: Catalogue, raw_audit_full: RawAudit, with_website: Edit
) -> None:
    assert "365" not in _rule(catalogue, "b19")
    assert "keine Altersgrenze" in _rule(catalogue, "b19")


def test_c12_names_the_relation_to_the_offer(catalogue: Catalogue) -> None:
    rule = _rule(catalogue, "c12")

    assert "alle Wörter eines Suchbegriffs des Betriebs" in rule
    assert c_inhalt.FUNCTION_WORDS


def test_the_robots_status_rule_matches_the_check_modules(catalogue: Catalogue) -> None:
    for check_id in ("a4", "a11", "a15", "a17"):
        assert "429" in _rule(catalogue, check_id)

    assert a_technik.gemini_allowed


JUDGED = {CheckOutcome.PASSED, CheckOutcome.PARTIAL, CheckOutcome.FAILED}


def _variants(raw: RawAudit) -> list[RawAudit]:
    return [
        raw,
        raw.model_copy(update={"website": None}),
        raw.model_copy(update={"place": None, "map_results": None, "competitor_places": []}),
        raw.model_copy(update={"serps": [], "map_rankings": [], "assistant_answers": []}),
        raw.model_copy(update={"directories": [], "paid": None, "ranking_history": None}),
    ]


def test_every_judged_outcome_the_code_produces_is_named_in_the_rule_of_its_check(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    rules = load_rules(RULES_PATH, catalogue)
    unscored = {c.id for area in catalogue.areas for c in area.checks if not c.counts_in_score}
    produced: dict[str, set[str]] = {}
    for variant in _variants(raw_audit_full):
        for result in evaluate(AUDIT_ID, variant, catalogue, paid_enabled=True, with_access=False):
            if result.outcome in JUDGED and result.check_id not in unscored:
                produced.setdefault(result.check_id, set()).add(result.outcome.value)

    unnamed = {
        check_id: sorted(word for word in words if word not in rules[check_id])
        for check_id, words in produced.items()
        if any(word not in rules[check_id] for word in words)
    }

    assert unnamed == {}
    assert len(produced) >= 25


def _examples(
    catalogue: Catalogue, raw: RawAudit
) -> dict[str, list[tuple[str, dict[str, object]]]]:
    found: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for with_access in (False, True):
        for variant in _variants(raw):
            for result in evaluate(
                AUDIT_ID, variant, catalogue, paid_enabled=True, with_access=with_access
            ):
                found.setdefault(result.check_id, []).append(
                    (result.outcome.value, dict(result.measured))
                )
    return found


def test_every_check_has_a_worked_example_produced_by_its_code(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    ids = {check.id for area in catalogue.areas for check in area.checks}
    examples = _examples(catalogue, raw_audit_full)

    assert len(ids) == 62
    assert sorted(ids - examples.keys()) == []


def test_every_not_measured_example_is_covered_by_the_rule_of_its_check(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    rules = load_rules(RULES_PATH, catalogue)
    silent = sorted(
        check_id
        for check_id, found in _examples(catalogue, raw_audit_full).items()
        if any(outcome == CheckOutcome.NOT_MEASURABLE.value for outcome, _ in found)
        and "nicht gemessen" not in rules[check_id]
    )

    assert silent == []


def test_every_scored_check_with_an_example_names_each_outcome_it_produced(
    catalogue: Catalogue, raw_audit_full: RawAudit
) -> None:
    rules = load_rules(RULES_PATH, catalogue)
    scored = {c.id for area in catalogue.areas for c in area.checks if c.counts_in_score}
    judged = {outcome.value for outcome in JUDGED}
    missing = {
        check_id: sorted({o for o, _ in found if o in judged and o not in rules[check_id]})
        for check_id, found in _examples(catalogue, raw_audit_full).items()
        if check_id in scored
    }

    assert {check_id: words for check_id, words in missing.items() if words} == {}
