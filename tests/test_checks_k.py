from collections.abc import Callable

import pytest

from radarauditor.collectors.snapshot import AnswerRoute, AssistantAnswer, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.k_assistenten import (
    answer_routes,
    is_recommendation_question,
    run,
)

THRESHOLDS: dict[str, dict[str, float | int | str]] = {
    "k1": {},
    "k2": {},
    "k6": {},
    "k7": {},
}
RECOMMENDATION = "Welchen Dachdecker in Münster empfiehlst du?"
PROBLEM = "Mein Dach ist undicht, was tun?"
Edit = Callable[..., RawAudit]


def _answer(
    question: str,
    mentioned: bool,
    provider: str = "openai",
    excerpt: str = "",
    sources: list[str] | None = None,
) -> AssistantAnswer:
    return AssistantAnswer(
        provider=provider,
        question=question,
        mentioned=mentioned,
        cited_sources=sources or [],
        answer_excerpt=excerpt,
    )


def _with_answers(raw: RawAudit, answers: list[AssistantAnswer]) -> RawAudit:
    return raw.model_copy(update={"assistant_answers": answers})


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_full_audit_passes(raw_audit_full: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_full, THRESHOLDS[check_id])[0] is CheckOutcome.PASSED


@pytest.mark.parametrize("check_id", list(THRESHOLDS))
def test_minimal_audit_not_measurable(raw_audit_minimal: RawAudit, check_id: str) -> None:
    assert run(check_id, raw_audit_minimal, THRESHOLDS[check_id])[0] is (
        CheckOutcome.NOT_MEASURABLE
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (RECOMMENDATION, True),
        ("Wer ist ein guter Dachdecker in Münster?", True),
        ("Nenne die Top 3 Dachdecker in Münster", True),
        (PROBLEM, False),
    ],
)
def test_question_classification(question: str, expected: bool) -> None:
    assert is_recommendation_question(question) is expected


def test_k1_counts_only_chatgpt_answers(raw_audit_full: RawAudit) -> None:
    answers = [
        _answer(RECOMMENDATION, mentioned=index < 1, provider="openai") for index in range(5)
    ]
    answers += [_answer(RECOMMENDATION, mentioned=True, provider="google_ai") for _ in range(5)]
    answers += [_answer(RECOMMENDATION, mentioned=True, provider="claude") for _ in range(5)]
    answers.append(_answer(PROBLEM, mentioned=True))
    outcome, measured = run("k1", _with_answers(raw_audit_full, answers), THRESHOLDS["k1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["mentioned"] == 1
    assert measured["answers"] == 5
    assert measured["by_provider"] == {"openai": {"mentioned": 1, "answers": 5}}
    assert "mention_rate" not in measured


def test_k1_counts_a_chatgpt_provider_named_chatgpt(raw_audit_full: RawAudit) -> None:
    answers = [_answer(RECOMMENDATION, mentioned=True, provider="chatgpt")]
    outcome, measured = run("k1", _with_answers(raw_audit_full, answers), THRESHOLDS["k1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["answers"] == 1


def test_k6_counts_only_claude_answers(raw_audit_full: RawAudit) -> None:
    answers = [
        _answer(RECOMMENDATION, mentioned=index < 2, provider="claude") for index in range(5)
    ]
    answers += [_answer(RECOMMENDATION, mentioned=True, provider="openai") for _ in range(5)]
    outcome, measured = run("k6", _with_answers(raw_audit_full, answers), THRESHOLDS["k6"])
    assert outcome is CheckOutcome.PASSED
    assert measured["mentioned"] == 2
    assert measured["answers"] == 5


def test_k6_gives_no_verdict_however_rarely_the_business_is_named(
    raw_audit_full: RawAudit,
) -> None:
    answers = [
        _answer(RECOMMENDATION, mentioned=index < 1, provider="claude") for index in range(10)
    ]
    outcome, measured = run("k6", _with_answers(raw_audit_full, answers), THRESHOLDS["k6"])
    assert outcome is CheckOutcome.PASSED
    assert (measured["mentioned"], measured["answers"]) == (1, 10)


def test_k7_counts_problem_questions_of_claude_only(raw_audit_full: RawAudit) -> None:
    answers = [
        _answer(PROBLEM, mentioned=False, provider="claude"),
        _answer(PROBLEM, mentioned=True, provider="openai"),
        _answer(RECOMMENDATION, mentioned=True, provider="claude"),
    ]
    outcome, measured = run("k7", _with_answers(raw_audit_full, answers), THRESHOLDS["k7"])
    assert outcome is CheckOutcome.PASSED
    assert (measured["mentioned"], measured["answers"]) == (0, 1)


def test_k6_and_k7_are_not_measured_without_claude_answers(raw_audit_full: RawAudit) -> None:
    answers = [_answer(RECOMMENDATION, mentioned=True), _answer(PROBLEM, mentioned=True)]
    raw = _with_answers(raw_audit_full, answers)
    assert run("k6", raw, THRESHOLDS["k6"])[1] == {"reason": "no_recommendation_answers"}
    assert run("k7", raw, THRESHOLDS["k7"])[1] == {"reason": "no_problem_answers"}


def test_a_cited_page_of_the_customer_counts_as_a_mention(raw_audit_full: RawAudit) -> None:
    cited = ["https://www.dachdeckerei-muster.de/dachsanierung/"]
    answers = [_answer(RECOMMENDATION, mentioned=False, sources=cited)]
    outcome, measured = run("k1", _with_answers(raw_audit_full, answers), THRESHOLDS["k1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["mentioned"] == 1


def test_an_unrelated_source_is_no_mention(raw_audit_full: RawAudit) -> None:
    answers = [_answer(RECOMMENDATION, mentioned=False, sources=["https://www.gelbeseiten.de/"])]
    _, measured = run("k1", _with_answers(raw_audit_full, answers), THRESHOLDS["k1"])
    assert measured["mentioned"] == 0


def test_the_rate_names_examples_with_question_and_excerpt(raw_audit_full: RawAudit) -> None:
    answers = [_answer(RECOMMENDATION, mentioned=False, excerpt="Dach Schmidt ist bekannt.")]
    _, measured = run("k1", _with_answers(raw_audit_full, answers), THRESHOLDS["k1"])
    assert measured["examples"] == [
        {
            "provider": "openai",
            "question": RECOMMENDATION,
            "mentioned": False,
            "excerpt": "Dach Schmidt ist bekannt.",
        }
    ]


def test_the_competitors_of_the_same_audit_are_counted_in_the_same_answers(
    raw_audit_full: RawAudit,
) -> None:
    template = raw_audit_full.competitor_places[0]
    competitors = [
        template.model_copy(update={"name": "Dach Schmidt", "website": None}),
        template.model_copy(update={"name": "Bedachung Weber", "website": None}),
    ]
    answers = [
        _answer(RECOMMENDATION, mentioned=index < 3, excerpt=text)
        for index, text in enumerate(
            ["Dach Schmidt", "Dach Schmidt und Bedachung Weber", "Dach Schmidt"] + [""] * 7
        )
    ]
    raw = _with_answers(raw_audit_full, answers)
    raw = raw.model_copy(update={"competitor_places": competitors})
    outcome, measured = run("k1", raw, THRESHOLDS["k1"])
    assert outcome is CheckOutcome.PASSED
    assert measured["competitors"] == [
        {"name": "Dach Schmidt", "mentioned": 3, "answers": 10},
        {"name": "Bedachung Weber", "mentioned": 1, "answers": 10},
    ]
    strongest = {"name": "Dach Schmidt", "mentioned": 3, "answers": 10}
    assert measured["strongest_competitor"] == strongest


def test_without_competitors_the_comparison_is_empty(raw_audit_full: RawAudit) -> None:
    raw = _with_answers(raw_audit_full, [_answer(RECOMMENDATION, mentioned=True)])
    raw = raw.model_copy(update={"competitor_places": []})
    measured = run("k1", raw, THRESHOLDS["k1"])[1]
    assert measured["competitors"] == []
    assert measured["strongest_competitor"] is None


def test_k2_problem_questions_only(raw_audit_full: RawAudit) -> None:
    answers = [_answer(RECOMMENDATION, mentioned=True), _answer(PROBLEM, mentioned=False)]
    outcome, measured = run("k2", _with_answers(raw_audit_full, answers), THRESHOLDS["k2"])
    assert outcome is CheckOutcome.PASSED
    assert measured["answers"] == 1
    only_recommendations = _with_answers(raw_audit_full, answers[:1])
    assert run("k2", only_recommendations, THRESHOLDS["k2"])[1] == {"reason": "no_problem_answers"}


def _routed(provider: str, route: AnswerRoute, **fields: object) -> AssistantAnswer:
    answer = _answer(RECOMMENDATION, mentioned=True, provider=provider)
    return answer.model_copy(update={"route": route, **fields})


def test_answer_routes_name_the_paid_interface_when_any_answer_used_it() -> None:
    answers = [
        _routed("chatgpt", "browser"),
        _routed("chatgpt", "api"),
        _routed("google_ai", "browser"),
        _routed("claude", "claude_code"),
        _answer(RECOMMENDATION, mentioned=True, provider="openai"),
    ]

    assert answer_routes(answers) == {
        "chatgpt": "openai_api",
        "google_ai": "browser",
        "claude": "claude_code",
    }


def test_answers_without_routes_record_none(raw_audit_full: RawAudit) -> None:
    raw = _with_answers(raw_audit_full, [_answer(RECOMMENDATION, mentioned=True)])

    assert "routes" not in run("k1", raw, THRESHOLDS["k1"])[1]
