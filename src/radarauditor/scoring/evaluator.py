import importlib
import logging
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from types import ModuleType
from uuid import UUID, uuid5

from radarauditor.collectors.assistants import provider_source
from radarauditor.collectors.snapshot import RawAudit
from radarauditor.collectors.validation import steps_of_check
from radarauditor.db.app_settings import Source
from radarauditor.db.models import CheckOutcome, CheckResult
from radarauditor.errors import ScoringError
from radarauditor.feedback.events import NOT_BUILT_REASON
from radarauditor.scoring.catalog import Catalogue, CheckDefinition, checks_for_context
from radarauditor.scoring.checks.common import PROFILE_NOT_PROVABLE, CheckReturn, not_measurable

logger = logging.getLogger(__name__)

AREA_MODULES = {
    "a": "a_technik",
    "b": "b_struktur",
    "c": "c_inhalt",
    "f": "f_google_profil",
    "g": "g_bewertungen",
    "h": "h_verzeichnisse",
    "i": "i_links",
    "j": "j_rankings",
    "k": "k_assistenten",
    "m": "m_wirkung",
    "p": "p_werbung",
}
POINTS = {
    CheckOutcome.PASSED: Decimal(1),
    CheckOutcome.PARTIAL: Decimal("0.5"),
    CheckOutcome.FAILED: Decimal(0),
}
DAILY_CAP_REASON = "Tageslimit erreicht"
MONTHLY_CAP_REASON = "Monatslimit erreicht"
SKIPPED_REASON = "In den Einstellungen abgeschaltet"
ASSISTANT_SOURCE = "assistants"
CATALOGUE_SOURCE_FEEDS: dict[str, frozenset[Source]] = {
    "places": frozenset({Source.PLACES, Source.GOOGLE_MAPS}),
    "serp": frozenset({Source.GOOGLE_SEARCH}),
    ASSISTANT_SOURCE: frozenset({Source.CHATGPT, Source.CLAUDE, Source.GOOGLE_AI}),
    "paid": frozenset({Source.DATAFORSEO}),
    "ads_google": frozenset({Source.DATAFORSEO}),
    "ads_meta": frozenset({Source.APIFY}),
}
CHECK_SOURCE_FEEDS: dict[str, frozenset[Source]] = {
    "k1": frozenset({Source.CHATGPT}),
    "k2": frozenset({Source.CHATGPT}),
    "k6": frozenset({Source.CLAUDE}),
    "k7": frozenset({Source.CLAUDE}),
}
PAID_SOURCE = "paid"
PLACES_SOURCE = "places"
PROFILE_CHECK = "f1"
HISTORY_CHECK = "j9"
PROFILE_AREAS = frozenset({"f", "g"})
PROFILE_CHECKS = frozenset({"j3"})
NO_LOCATION_REASON = "no_location"
MEASUREMENT_INVALID_REASON = "measurement_invalid"
NO_ACCESS_REASON = "no_access"
PAID_DISABLED_REASON = "paid_disabled"
SERP_SOURCE = "serp"
DATAFORSEO_ROUTE = "dataforseo"
APIFY_ROUTE = "apify"
PAID_SERP_ROUTES = frozenset({DATAFORSEO_ROUTE, APIFY_ROUTE})
NON_GOOGLE_WEB_SERP_CHECKS = frozenset({"j7", "j8"})
WEIGHT_PLACES = Decimal("0.001")
SCORE_PLACES = Decimal("0.01")
HUNDRED = Decimal(100)


@cache
def area_module(area_id: str) -> ModuleType:
    name = AREA_MODULES.get(area_id)
    if name is None:
        raise ScoringError(f"Area {area_id} has no check module")
    return importlib.import_module(f"radarauditor.scoring.checks.{name}")


def is_built(check_id: str) -> bool:
    name = AREA_MODULES.get(check_id[0])
    return name is not None and check_id in area_module(check_id[0]).CHECKS


def run_check(check: CheckDefinition, raw: RawAudit) -> CheckReturn:
    if not is_built(check.id):
        return CheckOutcome.NOT_MEASURABLE, {"reason": NOT_BUILT_REASON}
    if check.measurement == "access" and raw.access is None:
        return CheckOutcome.NOT_MEASURABLE, {"reason": "no_access"}
    if check.measurement == PLACES_SOURCE and (
        check.id[0] in PROFILE_AREAS or check.id in PROFILE_CHECKS
    ):
        if raw.has_location is False:
            return CheckOutcome.NOT_APPLICABLE, {"reason": NO_LOCATION_REASON}
        if raw.profile_missing and check.id != PROFILE_CHECK:
            return not_measurable(PROFILE_NOT_PROVABLE, profile_found=False)
    module = area_module(check.id[0])
    outcome, measured = module.run(check.id, raw, check.thresholds)
    if not isinstance(outcome, CheckOutcome) or not isinstance(measured, dict):
        raise ScoringError(f"Check {check.id} returned an invalid result")
    return outcome, measured


def evaluate(
    audit_id: UUID,
    raw: RawAudit,
    catalogue: Catalogue,
    paid_enabled: bool = False,
    with_access: bool = False,
    disabled_checks: frozenset[str] = frozenset(),
) -> list[CheckResult]:
    in_context = checks_for_context(catalogue, paid_enabled, with_access)
    weights = _check_weights(
        catalogue, [c for c in in_context if c.id not in disabled_checks and c.counts_in_score]
    )
    context_ids = {check.id for check in in_context}
    results: list[CheckResult] = []
    for check in (check for area in catalogue.areas for check in area.checks):
        outcome, measured = _outcome_of(
            check, raw, check.id in context_ids, disabled_checks, with_access
        )
        results.append(
            CheckResult(
                id=uuid5(audit_id, check.id),
                audit_id=audit_id,
                check_id=check.id,
                area=check.area,
                outcome=outcome,
                measured=measured,
                provider=check.provider,
                weight=weights.get(check.id, Decimal(0)),
            )
        )
    scored = sum(1 for result in results if result.outcome in POINTS)
    logger.info("Evaluated %d checks for audit %s, %d scored", len(results), audit_id, scored)
    return results


def _outcome_of(
    check: CheckDefinition,
    raw: RawAudit,
    in_context: bool,
    disabled_checks: frozenset[str],
    with_access: bool,
) -> CheckReturn:
    if check.id in disabled_checks:
        return CheckOutcome.SKIPPED, {"reason": SKIPPED_REASON}
    if in_context:
        return measure_check(check, raw)
    reason = NO_ACCESS_REASON if check.requires_access and not with_access else PAID_DISABLED_REASON
    return CheckOutcome.NOT_MEASURABLE, {"reason": reason}


def _check_weights(catalogue: Catalogue, counted: list[CheckDefinition]) -> dict[str, Decimal]:
    per_area: dict[str, int] = defaultdict(int)
    for check in counted:
        per_area[check.area] += 1
    area_weights = {area.id: area.weight for area in catalogue.areas}
    return {
        check.id: (
            Decimal(area_weights[check.area]) / per_area[check.area]
            if check.weight is None
            else check.weight
        ).quantize(WEIGHT_PLACES, rounding=ROUND_HALF_UP)
        for check in counted
    }


def invalid_measurement(check: CheckDefinition, raw: RawAudit) -> CheckReturn | None:
    if check.id == HISTORY_CHECK and raw.ranking_history is None:
        return None
    wanted = steps_of_check(check.id, check.measurement)
    for record in raw.measurements:
        if record.step in wanted and not record.valid:
            return CheckOutcome.NOT_MEASURABLE, {
                "reason": MEASUREMENT_INVALID_REASON,
                "step": record.step,
                "invalid_reason": record.reason,
            }
    return None


def measure_check(check: CheckDefinition, raw: RawAudit) -> CheckReturn:
    invalid = invalid_measurement(check, raw)
    if invalid is not None:
        return invalid
    feeds = CHECK_SOURCE_FEEDS.get(check.id) or CATALOGUE_SOURCE_FEEDS.get(
        check.measurement, frozenset()
    )
    cost_capped = sorted(feeds.intersection(raw.cost_capped_sources))
    if cost_capped and check.measurement != ASSISTANT_SOURCE:
        names = [source.value for source in cost_capped]
        return CheckOutcome.NOT_MEASURABLE, {"reason": MONTHLY_CAP_REASON, "sources": names}
    capped = sorted(feeds.intersection(raw.capped_sources))
    if not capped:
        outcome, measured = run_check(check, raw)
        return outcome, {**measured, **data_routes(check, raw, outcome)}
    capped_names = [source.value for source in capped]
    capped_measured: dict[str, object] = {"reason": DAILY_CAP_REASON, "sources": capped_names}
    if check.measurement != ASSISTANT_SOURCE:
        return CheckOutcome.NOT_MEASURABLE, capped_measured
    outcome, measured = run_check(check, _without_answers_of(raw, frozenset(capped)))
    if outcome is CheckOutcome.NOT_MEASURABLE:
        return outcome, capped_measured
    return outcome, {**measured, "capped_sources": capped_names}


def data_routes(check: CheckDefinition, raw: RawAudit, outcome: CheckOutcome) -> dict[str, object]:
    if outcome in (CheckOutcome.NOT_MEASURABLE, CheckOutcome.SKIPPED):
        return {}
    if check.measurement == PAID_SOURCE:
        return {"route": DATAFORSEO_ROUTE}
    if check.measurement != SERP_SOURCE or check.id in NON_GOOGLE_WEB_SERP_CHECKS:
        return {}
    routes = {
        serp.phrase: serp.route
        for serp in raw.serps
        if serp.engine == "google" and serp.vertical == "web"
    }
    if PAID_SERP_ROUTES.isdisjoint(routes.values()):
        return {}
    return {"routes": routes}


def _without_answers_of(raw: RawAudit, capped: frozenset[Source]) -> RawAudit:
    kept = [
        answer for answer in raw.assistant_answers if provider_source(answer.provider) not in capped
    ]
    return raw.model_copy(update={"assistant_answers": kept})


def _area_fractions(results: list[CheckResult]) -> dict[str, Decimal]:
    scored: dict[str, list[CheckResult]] = defaultdict(list)
    for result in results:
        if result.outcome in POINTS:
            scored[result.area].append(result)
    return {area: _weighted_mean(members) for area, members in scored.items()}


def _weighted_mean(results: list[CheckResult]) -> Decimal:
    weight_sum = sum((result.weight for result in results), Decimal(0))
    if weight_sum == 0:
        return sum((POINTS[result.outcome] for result in results), Decimal(0)) / len(results)
    weighted = sum((result.weight * POINTS[result.outcome] for result in results), Decimal(0))
    return weighted / weight_sum


def _scored_results(results: list[CheckResult], catalogue: Catalogue) -> list[CheckResult]:
    info_ids = {
        check.id for area in catalogue.areas for check in area.checks if not check.counts_in_score
    }
    return [
        result
        for result in results
        if result.check_id not in info_ids and not result.control_failed
    ]


def area_scores(results: list[CheckResult], catalogue: Catalogue) -> dict[str, Decimal]:
    fractions = _area_fractions(_scored_results(results, catalogue))
    return {
        area.id: (fractions[area.id] * HUNDRED).quantize(SCORE_PLACES, rounding=ROUND_HALF_UP)
        for area in catalogue.areas
        if area.id in fractions
    }


def provider_scores(results: list[CheckResult], catalogue: Catalogue) -> dict[str, Decimal]:
    provider_of = {check.id: check.provider for area in catalogue.areas for check in area.checks}
    scored: dict[str, list[CheckResult]] = defaultdict(list)
    for result in _scored_results(results, catalogue):
        if result.outcome in POINTS:
            scored[provider_of[result.check_id]].append(result)
    return {
        provider: (_weighted_mean(members) * HUNDRED).quantize(SCORE_PLACES, rounding=ROUND_HALF_UP)
        for provider, members in scored.items()
    }


def total_score_or_none(results: list[CheckResult], catalogue: Catalogue) -> Decimal | None:
    fraction = total_fraction(results, catalogue)
    if fraction is None:
        return None
    return (fraction * HUNDRED).quantize(SCORE_PLACES, rounding=ROUND_HALF_UP)


def total_score(results: list[CheckResult], catalogue: Catalogue) -> Decimal:
    score = total_score_or_none(results, catalogue)
    if score is None:
        raise ScoringError("No weighted area has a measurable result")
    return score


def total_fraction(results: list[CheckResult], catalogue: Catalogue) -> Decimal | None:
    fractions = _area_fractions(_scored_results(results, catalogue))
    weights = {area.id: Decimal(area.weight) for area in catalogue.areas if area.id in fractions}
    weight_sum = sum(weights.values(), Decimal(0))
    if weight_sum == 0:
        return None
    weighted = sum((weights[area] * fractions[area] for area in weights), Decimal(0))
    return weighted / weight_sum
