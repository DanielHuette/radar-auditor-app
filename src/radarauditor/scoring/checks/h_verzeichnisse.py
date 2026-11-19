from radarauditor.collectors.identity import fold
from radarauditor.collectors.snapshot import DirectoryHit, RawAudit
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    address_key,
    combine,
    dispatch,
    not_measurable,
    phone_key,
)

APPLE = "Apple Business Connect"
BING = "Bing Places"


def hits_named(raw: RawAudit, names: tuple[str, ...]) -> list[DirectoryHit]:
    wanted = {fold(name) for name in names}
    return [hit for hit in raw.directories if fold(hit.directory) in wanted]


def reference_nap(raw: RawAudit) -> tuple[str | None, str | None]:
    business = raw.business
    if business is None:
        return None, None
    line = " ".join(part for part in (business.street, business.postal_code) if part)
    return phone_key(business.phone), address_key(line or None)


def nap_mismatches(raw: RawAudit, hit: DirectoryHit) -> list[str]:
    phone, address = reference_nap(raw)
    mismatches: list[str] = []
    listed_phone = phone_key(hit.phone)
    if phone is not None and listed_phone is not None and listed_phone != phone:
        mismatches.append("phone")
    listed_address = address_key(hit.address)
    if address is not None and listed_address is not None and listed_address != address:
        mismatches.append("address")
    return mismatches


def _search_documented(hit: DirectoryHit) -> bool:
    return hit.delivered is not None and bool((hit.search_text or "").strip())


def _listing_check(raw: RawAudit, names: tuple[str, ...]) -> CheckReturn:
    hits = hits_named(raw, names)
    if not hits:
        return not_measurable("directory_not_checked")
    if all(not hit.found and hit.delivered == 0 for hit in hits):
        return not_measurable("service_delivered_nothing")
    if any(not hit.found and not _search_documented(hit) for hit in hits):
        return not_measurable("directory_search_not_documented")
    outcomes: list[CheckOutcome] = []
    details: dict[str, object] = {}
    for hit in hits:
        mismatches = nap_mismatches(raw, hit) if hit.found else []
        if not hit.found:
            outcomes.append(CheckOutcome.FAILED)
        elif mismatches:
            outcomes.append(CheckOutcome.PARTIAL)
        else:
            outcomes.append(CheckOutcome.PASSED)
        details[hit.directory] = {
            "found": hit.found,
            "listing_url": str(hit.listing_url) if hit.listing_url else None,
            "mismatches": mismatches,
            "delivered": hit.delivered,
            "search_text": hit.search_text,
            "entries": (
                None if hit.entries is None else [entry.model_dump() for entry in hit.entries]
            ),
        }
    return combine(outcomes), {"listings": details}


def apple_maps(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _listing_check(raw, (APPLE,))


def bing_places(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _listing_check(raw, (BING,))


CHECKS: dict[str, CheckFunction] = {
    "h1": apple_maps,
    "h2": bing_places,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
