from statistics import median

from radarauditor.collectors.snapshot import BacklinkSummary, PaidSnapshot, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    NO_PAID_DATA,
    CheckFunction,
    CheckReturn,
    Thresholds,
    dispatch,
    not_measurable,
    share_of_best,
)


def _own_links(paid: PaidSnapshot) -> BacklinkSummary | None:
    if paid.customer_domain is None:
        return None
    return paid.backlinks.get(paid.customer_domain)


def referring_domains(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.paid is None:
        return not_measurable(NO_PAID_DATA)
    own = _own_links(raw.paid)
    if own is None:
        return not_measurable("no_backlink_data")
    rivals = {
        domain: float(summary.referring_domains)
        for domain, summary in raw.paid.backlinks.items()
        if domain != own.domain
    }
    if not rivals:
        return not_measurable("no_competitor_backlinks")
    share, leader = share_of_best(own.referring_domains, rivals)
    middle = median(rivals.values())
    outcome = CheckOutcome.PASSED if own.referring_domains >= middle else CheckOutcome.FAILED
    return outcome, {
        "referring_domains": own.referring_domains,
        "competitors": {domain: int(count) for domain, count in rivals.items()},
        "competitor_median": middle,
        "best_competitor": leader,
        "share_of_best": share,
    }


CHECKS: dict[str, CheckFunction] = {
    "i1": referring_domains,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
