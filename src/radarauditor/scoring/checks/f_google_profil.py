from radarauditor.collectors.snapshot import RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    PROFILE_NOT_PROVABLE,
    CheckFunction,
    CheckReturn,
    Thresholds,
    dispatch,
    not_measurable,
    place_routes,
)

STRONGEST_COMPETITORS = 3
MAX_EXAMPLES = 5


def profile_exists(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        if raw.profile_missing:
            return not_measurable(PROFILE_NOT_PROVABLE, profile_found=False)
        return not_measurable("collector_failed")
    return not_measurable(
        "ownership_not_verifiable",
        profile_found=True,
        business_status=raw.place.business_status,
    )


def primary_category(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    own = (raw.place.primary_type or "").strip()
    competitor_types = {
        place.name: place.primary_type
        for place in raw.competitor_places[:STRONGEST_COMPETITORS]
        if place.route_of("primary_type") == raw.place.route_of("primary_type")
    }
    if own:
        return CheckOutcome.PASSED, {"primary_type": own, "competitor_types": competitor_types}
    if raw.place.route_of("primary_type") is None:
        return not_measurable("field_not_delivered", field="primary_type")
    return CheckOutcome.FAILED, {"primary_type": None, "competitor_types": competitor_types}


def _entered(raw: RawAudit, field: str, values: list[str] | None) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    if values:
        return CheckOutcome.PASSED, {"entries": len(values), "examples": values[:MAX_EXAMPLES]}
    if raw.place.route_of(field) is None:
        return not_measurable("field_not_delivered", field=field)
    return CheckOutcome.FAILED, {"entries": 0}


def description_present(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    text = (raw.place.description or "").strip()
    if text:
        return CheckOutcome.PASSED, {"description_length": len(text)}
    if raw.place.route_of("description") is None:
        return not_measurable("field_not_delivered", field="description")
    return CheckOutcome.FAILED, {"description_length": 0}


def services_entered(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _entered(raw, "services", raw.place.services if raw.place else None)


def attributes_entered(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _entered(raw, "attributes", raw.place.attributes if raw.place else None)


def opening_hours_entered(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    if not raw.place.has_opening_hours and raw.place.route_of("has_opening_hours") is None:
        return not_measurable("field_not_delivered", field="has_opening_hours")
    outcome = CheckOutcome.PASSED if raw.place.has_opening_hours else CheckOutcome.FAILED
    return outcome, {
        "has_opening_hours": raw.place.has_opening_hours,
        "special_hours": "not_determinable",
    }


def photos_present(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.place is None:
        return not_measurable("no_place")
    if raw.place.photo_count == 0 and raw.place.route_of("photo_count") is None:
        return not_measurable("field_not_delivered", field="photo_count")
    outcome = CheckOutcome.PASSED if raw.place.photo_count > 0 else CheckOutcome.FAILED
    return outcome, {"photo_count": raw.place.photo_count}


CHECKS: dict[str, CheckFunction] = {
    "f1": profile_exists,
    "f2": primary_category,
    "f4": description_present,
    "f5": services_entered,
    "f7": opening_hours_entered,
    "f8": photos_present,
    "f13": attributes_entered,
}


PROFILE_FIELDS = {
    "f1": "business_status",
    "f2": "primary_type",
    "f4": "description",
    "f5": "services",
    "f7": "has_opening_hours",
    "f8": "photo_count",
    "f13": "attributes",
}
COMPARED_CHECKS = frozenset({"f2"})


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    outcome, measured = dispatch(CHECKS, check_id, raw, thresholds)
    field = PROFILE_FIELDS.get(check_id)
    if raw.place is None or field is None or outcome is CheckOutcome.NOT_MEASURABLE:
        return outcome, measured
    compared = raw.competitor_places[:STRONGEST_COMPETITORS] if check_id in COMPARED_CHECKS else []
    return outcome, {**measured, **place_routes([raw.place, *compared], field)}
