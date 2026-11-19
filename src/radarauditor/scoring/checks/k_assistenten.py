import re

from radarauditor.collectors.identity import fold
from radarauditor.collectors.mention import mentions_business
from radarauditor.collectors.snapshot import AssistantAnswer, PlaceSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    customer_host,
    dispatch,
    not_measurable,
    same_site,
)

RECOMMENDATION_MARKERS = ("empfiehlst", "guter", "top 3")
PHONE_PATTERN = re.compile(r"(?:\+49|0)[\d\s/()-]{6,}\d")
PAID_ROUTE_IDS = {"chatgpt": "openai_api"}
CHATGPT_PROVIDERS = frozenset({"chatgpt", "openai"})
CLAUDE_PROVIDERS = frozenset({"claude"})
MAX_EXAMPLES = 6
EXAMPLE_CHARS = 300


def is_recommendation_question(question: str) -> bool:
    folded = fold(question)
    return any(marker in folded for marker in RECOMMENDATION_MARKERS)


def answer_routes(answers: list[AssistantAnswer]) -> dict[str, str]:
    used: dict[str, set[str]] = {}
    for answer in answers:
        if answer.route is not None:
            used.setdefault(answer.provider, set()).add(answer.route)
    return {
        provider: PAID_ROUTE_IDS.get(provider, "api") if "api" in kinds else min(kinds)
        for provider, kinds in used.items()
    }


def _with_routes(result: CheckReturn, answers: list[AssistantAnswer]) -> CheckReturn:
    outcome, measured = result
    routes = answer_routes(answers)
    return outcome, ({**measured, "routes": routes} if routes else measured)


def _mentioned(answer: AssistantAnswer, host: str | None) -> bool:
    if answer.mentioned:
        return True
    return host is not None and any(same_site(source, host) for source in answer.cited_sources)


def _examples(answers: list[AssistantAnswer], host: str | None) -> list[dict[str, object]]:
    first_per_question = {answer.question: answer for answer in reversed(answers)}
    return [
        {
            "provider": answer.provider,
            "question": answer.question,
            "mentioned": _mentioned(answer, host),
            "excerpt": answer.answer_excerpt[:EXAMPLE_CHARS],
        }
        for answer in list(first_per_question.values())[:MAX_EXAMPLES]
    ]


def _competitor_mentions(
    answers: list[AssistantAnswer], competitors: list[PlaceSnapshot]
) -> list[dict[str, object]]:
    counted = [
        {
            "name": competitor.name,
            "mentioned": sum(
                1
                for answer in answers
                if mentions_business(
                    answer.answer_excerpt,
                    answer.cited_sources,
                    competitor.name,
                    str(competitor.website) if competitor.website else None,
                )
            ),
            "answers": len(answers),
        }
        for competitor in competitors
    ]
    return sorted(counted, key=lambda entry: (-int(str(entry["mentioned"])), str(entry["name"])))


def _mention_rate(
    answers: list[AssistantAnswer], host: str | None, competitors: list[PlaceSnapshot]
) -> CheckReturn:
    mentioned = sum(1 for answer in answers if _mentioned(answer, host))
    by_provider = {
        provider: {
            "mentioned": sum(1 for item in items if _mentioned(item, host)),
            "answers": len(items),
        }
        for provider, items in _grouped(answers).items()
    }
    ranked = _competitor_mentions(answers, competitors)
    measured: dict[str, object] = {
        "mentioned": mentioned,
        "answers": len(answers),
        "by_provider": by_provider,
        "competitors": ranked,
        "strongest_competitor": ranked[0] if ranked else None,
        "examples": _examples(answers, host),
    }
    return _with_routes((CheckOutcome.PASSED, measured), answers)


def _grouped(answers: list[AssistantAnswer]) -> dict[str, list[AssistantAnswer]]:
    grouped: dict[str, list[AssistantAnswer]] = {}
    for answer in answers:
        grouped.setdefault(answer.provider, []).append(answer)
    return grouped


def _provider_rate(
    raw: RawAudit,
    providers: frozenset[str],
    recommendation: bool,
) -> CheckReturn:
    answers = [
        answer
        for answer in raw.assistant_answers
        if answer.provider in providers
        and is_recommendation_question(answer.question) is recommendation
    ]
    if not answers:
        reason = "no_recommendation_answers" if recommendation else "no_problem_answers"
        return not_measurable(reason)
    return _mention_rate(answers, customer_host(raw), raw.competitor_places)


def chatgpt_recommendation_mentions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _provider_rate(raw, CHATGPT_PROVIDERS, True)


def chatgpt_problem_mentions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _provider_rate(raw, CHATGPT_PROVIDERS, False)


def claude_recommendation_mentions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _provider_rate(raw, CLAUDE_PROVIDERS, True)


def claude_problem_mentions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _provider_rate(raw, CLAUDE_PROVIDERS, False)


CHECKS: dict[str, CheckFunction] = {
    "k1": chatgpt_recommendation_mentions,
    "k2": chatgpt_problem_mentions,
    "k6": claude_recommendation_mentions,
    "k7": claude_problem_mentions,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
