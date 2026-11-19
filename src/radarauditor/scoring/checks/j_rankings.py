from radarauditor.collectors.identity import Identity, fold, identity, is_name_search, same_business
from radarauditor.collectors.snapshot import PositionChange, RawAudit, SerpResult
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    NO_PAID_DATA,
    CheckFunction,
    CheckReturn,
    Thresholds,
    band_outcome,
    brand_serps,
    business_name,
    combine,
    customer_host,
    dispatch,
    google_web_serps,
    not_measurable,
    number,
    own_identity,
    route_entry,
    same_site,
    serp_coverage,
    share_of_best,
    usable,
)

GRID_TOP = 3
RANKED_PASS_SHARE = 0.8
RANKED_PARTIAL_SHARE = 0.4


def organic_position(serp: SerpResult, host: str) -> int | None:
    positions = [position for position, url in serp.organic if same_site(url, host)]
    return min(positions) if positions else None


def local_pack_position(serp: SerpResult, own: Identity) -> int | None:
    positions = [
        position for position, entry in serp.local_pack if same_business(own, identity(name=entry))
    ]
    return min(positions) if positions else None


def _non_brand_serps(raw: RawAudit) -> list[SerpResult]:
    name = business_name(raw)
    return [
        serp
        for serp in google_web_serps(raw)
        if name is None or not is_name_search(serp.phrase, name)
    ]


def _no_valid_list(considered: list[SerpResult], used: list[SerpResult]) -> CheckReturn | None:
    if used:
        return None
    return not_measurable("no_valid_result_lists", **serp_coverage(considered, used))


def _ranked_everywhere(positions: dict[str, int | None], limit: float) -> CheckOutcome:
    return combine(
        CheckOutcome.PASSED if position is not None and position <= limit else CheckOutcome.FAILED
        for position in positions.values()
    )


def organic_rankings(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    considered = _non_brand_serps(raw)
    if not considered:
        return not_measurable("no_serps")
    host = customer_host(raw)
    if host is None:
        return not_measurable("no_website")
    serps = usable(considered)
    if (empty := _no_valid_list(considered, serps)) is not None:
        return empty
    top, good, visible = (number(thresholds, key) for key in ("top", "good", "visible"))
    positions = {serp.phrase: organic_position(serp, host) for serp in serps}
    ranked = [position for position in positions.values() if position is not None]
    in_good = sum(1 for position in ranked if position <= good)
    in_visible = sum(1 for position in ranked if position <= visible)
    if in_good == len(positions):
        outcome = CheckOutcome.PASSED
    elif in_visible:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    measured: dict[str, object] = {
        "positions": positions,
        "in_top": sum(1 for position in ranked if position <= top),
        "in_good": in_good,
        "in_visible": in_visible,
        "phrases_total": len(positions),
        "top": top,
        "good": good,
        "visible": visible,
        **serp_coverage(considered, serps),
    }
    return outcome, measured


def map_pack(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    name = business_name(raw)
    if name is None:
        return not_measurable("no_business_name")
    serps = [serp for serp in _non_brand_serps(raw) if serp.local_pack]
    own = own_identity(raw)
    positions = {serp.phrase: local_pack_position(serp, own) for serp in serps}
    from_maps = [ranking for ranking in raw.map_rankings if ranking.phrase not in positions]
    positions.update({ranking.phrase: ranking.position for ranking in from_maps})
    if not positions:
        return not_measurable("no_local_pack")
    top = number(thresholds, "top")
    in_top = sum(1 for position in positions.values() if position is not None and position <= top)
    return _ranked_everywhere(positions, top), {
        "positions": positions,
        "in_top": in_top,
        "phrases_with_map": len(positions),
        "maps_phrases": [ranking.phrase for ranking in from_maps],
        "top": top,
        "business_name": name,
        **route_entry(ranking.route for ranking in from_maps),
    }


def _optional_number(thresholds: Thresholds, key: str, default: float) -> float:
    return number(thresholds, key) if key in thresholds else default


def grid_visibility(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if not raw.grid:
        return not_measurable("no_grid")
    top = _optional_number(thresholds, "top", GRID_TOP)
    in_top = [p.key for p in raw.grid if p.position is not None and p.position <= top]
    share = len(in_top) / len(raw.grid)
    return CheckOutcome.PASSED, {
        "points_total": len(raw.grid),
        "points_in_top": len(in_top),
        "share_in_top": round(share, 2),
        "positions": {point.key: point.position for point in raw.grid},
        "top": top,
        "competitor_values": None,
        **route_entry(point.route for point in raw.grid),
    }


def brand_search(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    host = customer_host(raw)
    if business_name(raw) is None or host is None:
        return not_measurable("no_business_name")
    considered = brand_serps(raw)
    if not considered:
        return not_measurable("no_brand_search")
    serps = usable(considered)
    if (empty := _no_valid_list(considered, serps)) is not None:
        return empty
    positions = [organic_position(serp, host) for serp in serps]
    best = min((position for position in positions if position is not None), default=None)
    panel = any(serp.knowledge_panel for serp in serps)
    panel_readable = any(serp.route != "apify" for serp in serps)
    outcome = CheckOutcome.PASSED if best == 1 else CheckOutcome.FAILED
    measured: dict[str, object] = {
        "brand_phrase": serps[0].phrase,
        "position": best,
        **serp_coverage(considered, serps),
    }
    if panel or panel_readable:
        measured["knowledge_panel"] = panel
    return outcome, measured


def ai_overviews(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    shown = [serp for serp in google_web_serps(raw) if serp.ai_overview_present]
    if not shown:
        return not_measurable("no_ai_overview")
    overviews = usable(shown)
    if (empty := _no_valid_list(shown, overviews)) is not None:
        return empty
    name = business_name(raw)
    own = own_identity(raw)
    host = customer_host(raw)

    def mentions_business(serp: SerpResult) -> bool:
        return any(
            (name is not None and same_business(own, identity(name=mention)))
            or (host is not None and host in fold(mention))
            for mention in serp.ai_overview_mentions
        )

    mentioned = [serp.phrase for serp in overviews if mentions_business(serp)]
    outcome = combine(
        CheckOutcome.PASSED if serp.phrase in mentioned else CheckOutcome.FAILED
        for serp in overviews
    )
    return outcome, {
        "phrases_with_overview": len(overviews),
        "phrases_mentioning_business": mentioned,
        **serp_coverage(shown, overviews),
    }


def bing_rankings(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    serps = [serp for serp in raw.serps if serp.engine == "bing"]
    if not serps:
        return not_measurable("no_bing_serp")
    host = customer_host(raw)
    if host is None:
        return not_measurable("no_website")
    good = number(thresholds, "good")
    positions = {serp.phrase: organic_position(serp, host) for serp in serps}
    return _ranked_everywhere(positions, good), {
        "positions": positions,
        "phrases_total": len(positions),
        "good": good,
    }


def image_search(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    serps = [serp for serp in raw.serps if serp.vertical == "images"]
    if not serps:
        return not_measurable("no_image_serp")
    host = customer_host(raw)
    if host is None:
        return not_measurable("no_website")
    positions = {serp.phrase: organic_position(serp, host) for serp in serps}
    outcome = combine(
        CheckOutcome.FAILED if position is None else CheckOutcome.PASSED
        for position in positions.values()
    )
    return outcome, {
        "source_positions": positions,
        "phrases_total": len(positions),
        "sources_seen": {serp.phrase: len(serp.organic) for serp in serps},
    }


def ranked_keywords(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    if raw.paid is None:
        return not_measurable(NO_PAID_DATA)
    customer = raw.paid.customer_domain
    counts = raw.paid.ranked_keywords
    if customer is None or customer not in counts:
        return not_measurable("no_ranked_keywords")
    rivals = {domain: float(count) for domain, count in counts.items() if domain != customer}
    if not rivals:
        return not_measurable("no_competitor_keywords")
    share, leader = share_of_best(counts[customer], rivals)
    return band_outcome(share, RANKED_PASS_SHARE, RANKED_PARTIAL_SHARE), {
        "ranked_keywords": counts[customer],
        "competitors": {domain: int(count) for domain, count in rivals.items()},
        "best_competitor": leader,
        "share_of_best": share,
    }


def _moved_up(change: PositionChange) -> bool:
    if change.after is None:
        return False
    return change.before is None or change.after < change.before


def _moved_down(change: PositionChange) -> bool:
    if change.before is None:
        return False
    return change.after is None or change.after > change.before


def development(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    history = raw.ranking_history
    if history is None:
        return not_measurable("needs_history")
    if not history.changes:
        return not_measurable("no_shared_rankings")
    improved = [change for change in history.changes if _moved_up(change)]
    worsened = [change for change in history.changes if _moved_down(change)]
    visible = any(change.after is not None for change in history.changes)
    if len(improved) > len(worsened):
        outcome = CheckOutcome.PASSED
    elif len(improved) == len(worsened) and visible:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.FAILED
    return outcome, {
        "previous_audit_id": str(history.previous_audit_id),
        "previous_requested_at": history.previous_requested_at.isoformat(),
        "improved": len(improved),
        "worsened": len(worsened),
        "unchanged": len(history.changes) - len(improved) - len(worsened),
        "changes": [change.model_dump() for change in history.changes],
    }


CHECKS: dict[str, CheckFunction] = {
    "j1": organic_rankings,
    "j2": map_pack,
    "j3": grid_visibility,
    "j4": brand_search,
    "j5": ranked_keywords,
    "j6": ai_overviews,
    "j7": bing_rankings,
    "j8": image_search,
    "j9": development,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
