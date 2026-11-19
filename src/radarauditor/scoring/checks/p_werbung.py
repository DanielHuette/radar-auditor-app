from radarauditor.collectors.identity import is_name_search
from radarauditor.collectors.snapshot import RawAudit, SerpResult
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    brand_serps,
    business_name,
    customer_host,
    dispatch,
    google_web_serps,
    listed,
    not_measurable,
    same_site,
    serp_coverage,
    usable,
)


def _paid_listings(serp: SerpResult, host: str) -> dict[str, object]:
    positions = [
        position for position, domain in enumerate(serp.ads, start=1) if same_site(domain, host)
    ]
    rivals = [domain for domain in serp.ads if not same_site(domain, host)]
    return {
        "customer_shown": bool(positions),
        "customer_position": min(positions) if positions else None,
        "competitors": listed(dict.fromkeys(rivals)),
        "paid_entries": len(serp.ads),
    }


def google_ads_shown(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    snapshot = raw.ads_google
    if snapshot is None:
        return not_measurable("ads_google_not_measured")
    advertisers = dict.fromkeys(ad.advertiser_name for ad in snapshot.ads if ad.advertiser_name)
    first = [ad.first_shown for ad in snapshot.ads if ad.first_shown]
    last = [ad.last_shown for ad in snapshot.ads if ad.last_shown]
    return CheckOutcome.PASSED, {
        "target": snapshot.target,
        "window_days": snapshot.window_days,
        "shows_ads": bool(snapshot.ads),
        "ads_found": len(snapshot.ads),
        "advertisers": listed(advertisers),
        "formats": sorted({ad.format for ad in snapshot.ads if ad.format}),
        "first_shown": min(first) if first else None,
        "last_shown": max(last) if last else None,
    }


def meta_ads_shown(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    snapshot = raw.ads_meta
    if snapshot is None:
        return not_measurable("ads_meta_not_measured")
    pages = dict.fromkeys(ad.page_name for ad in snapshot.own_ads if ad.page_name)
    return CheckOutcome.PASSED, {
        "searched_name": snapshot.searched_name,
        "shows_ads": bool(snapshot.own_ads),
        "own_ads": len(snapshot.own_ads),
        "own_pages": listed(pages),
        "competitor_ad_counts": snapshot.competitor_counts,
    }


def account_not_connected(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return not_measurable("ads_account_not_connected")


def ads_on_search_terms(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    host = customer_host(raw)
    if host is None:
        return not_measurable("no_website")
    name = business_name(raw)
    considered = [
        serp
        for serp in google_web_serps(raw)
        if name is None or not is_name_search(serp.phrase, name)
    ]
    if not considered:
        return not_measurable("no_serps")
    serps = usable(considered)
    coverage = serp_coverage(considered, serps)
    if not serps:
        return not_measurable("no_valid_result_lists", **coverage)
    phrases = {serp.phrase: _paid_listings(serp, host) for serp in serps}
    shown = sum(1 for entry in phrases.values() if entry["customer_shown"])
    return CheckOutcome.PASSED, {
        "phrases": phrases,
        "phrases_total": len(phrases),
        "phrases_with_own_ad": shown,
        **coverage,
    }


def competitors_on_brand(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    host = customer_host(raw)
    if host is None:
        return not_measurable("no_website")
    considered = brand_serps(raw)
    if not considered:
        return not_measurable("no_brand_search")
    serps = usable(considered)
    coverage = serp_coverage(considered, serps)
    if not serps:
        return not_measurable("no_valid_result_lists", **coverage)
    phrases = {serp.phrase: _paid_listings(serp, host) for serp in serps}
    rivals = list(
        dict.fromkeys(
            domain for serp in serps for domain in serp.ads if not same_site(domain, host)
        )
    )
    return CheckOutcome.PASSED, {
        "phrases": phrases,
        "competitors_on_brand": rivals,
        "competitors_on_brand_count": len(rivals),
        **coverage,
    }


CHECKS: dict[str, CheckFunction] = {
    "p1": google_ads_shown,
    "p2": meta_ads_shown,
    "p3": ads_on_search_terms,
    "p4": competitors_on_brand,
    "p5": account_not_connected,
    "p6": account_not_connected,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
