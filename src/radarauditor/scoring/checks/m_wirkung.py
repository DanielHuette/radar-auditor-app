from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    TOLERANCE,
    CheckFunction,
    CheckReturn,
    Thresholds,
    dispatch,
    listed,
    not_measurable,
    service_terms,
    tokens,
)

ACTION_METRICS = ("CALL_CLICKS", "BUSINESS_DIRECTION_REQUESTS", "WEBSITE_CLICKS")
PERCENT = 100


def change_percent(now: int, before: int) -> float | None:
    if before == 0:
        return None
    return round((now - before) / before * PERCENT, 1)


def profile_actions(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    access = raw.access
    if access is None:
        return not_measurable("no_access")
    if not access.profile_metrics:
        return not_measurable("no_profile_metrics")
    now = sum(access.profile_metrics.get(name, 0) for name in ACTION_METRICS)
    before = sum(access.profile_metrics_previous.get(name, 0) for name in ACTION_METRICS)
    if now >= before:
        outcome = CheckOutcome.PASSED
    elif now >= before * (1 - TOLERANCE):
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    return outcome, {
        "metrics": access.profile_metrics,
        "metrics_previous": access.profile_metrics_previous,
        "change_percent": {
            name: change_percent(value, access.profile_metrics_previous.get(name, 0))
            for name, value in access.profile_metrics.items()
        },
        "actions": now,
        "actions_previous": before,
        "actions_change_percent": change_percent(now, before),
    }


def _matches(query: str, phrases: dict[str, list[str]]) -> list[str]:
    words = set(tokens(query))
    return [phrase for phrase, terms in phrases.items() if set(terms) <= words]


def search_console_clicks(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    access = raw.access
    if access is None:
        return not_measurable("no_access")
    if not access.search_totals:
        return not_measurable("no_search_console")
    phrases = service_terms(raw)
    if not phrases:
        return not_measurable("no_search_phrases")
    matching = [
        query
        for query in access.search_queries
        if query.clicks > 0 and _matches(query.query, phrases)
    ]
    if matching:
        outcome = CheckOutcome.PASSED
    elif access.search_queries:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    return outcome, {
        "matching_queries": {query.query: query.clicks for query in matching[:10]},
        "top_queries": {query.query: query.clicks for query in access.search_queries[:10]},
        "queries_total": len(access.search_queries),
        "totals": access.search_totals,
        "tracked_phrases": listed(phrases),
    }


CHECKS: dict[str, CheckFunction] = {
    "m1": profile_actions,
    "m2": search_console_clicks,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
