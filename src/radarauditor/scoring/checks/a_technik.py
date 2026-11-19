from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import HttpUrl

from radarauditor.collectors import crawl_extras
from radarauditor.collectors.snapshot import (
    AgentFetch,
    PageSnapshot,
    PageSpeedField,
    RawAudit,
    WebsiteSnapshot,
)
from radarauditor.db.models import CheckOutcome
from radarauditor.scoring.checks.common import (
    CheckFunction,
    CheckReturn,
    Thresholds,
    ceiling_outcome,
    content_pages,
    dispatch,
    listed,
    not_measurable,
    number,
    tokens,
    url_key,
)
from radarauditor.scoring.checks.crawl import (
    BLOCK_STATUSES,
    HTTP_OK,
    agent_fetches,
    crawled,
    gradation,
    has_extended_data,
    is_system_path,
    is_transient_status,
    looks_like_block_page,
    no_extended_data,
    origin_root,
    robots_blocked_pages,
    robots_unavailable,
    similarity,
    start_key,
)
from radarauditor.scoring.checks.robots import (
    BINGBOT,
    CLAUDE_SEARCHBOT,
    CLAUDE_USER,
    GOOGLE_EXTENDED,
    GOOGLEBOT,
    INDEX_BLOCKING,
    OAI_SEARCHBOT,
    SNIPPET_BLOCKING,
    RobotsRules,
    directives_for,
)

LAB_SOURCE = "lab"
SPEED_LIMITS = (
    "lcp_good_ms",
    "lcp_poor_ms",
    "cls_good",
    "cls_poor",
    "inp_good_ms",
    "inp_poor_ms",
    "tbt_good_ms",
    "tbt_poor_ms",
)
SPEED_RATINGS = {
    CheckOutcome.PASSED: "good",
    CheckOutcome.PARTIAL: "needs_improvement",
    CheckOutcome.FAILED: "poor",
}
GOOGLE_EXTENDED_NOTE = "betrifft nicht die Google-Suche"
PageMeta = Callable[[PageSnapshot], str | None]


def loading_speed(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    speed = raw.pagespeed
    if speed is None:
        return not_measurable("no_pagespeed")
    if speed.lab is None:
        return not_measurable("pagespeed_data_outdated" if speed.older_format else "no_pagespeed")
    lab, visitors = speed.lab, speed.field_data
    limits = {key: number(thresholds, key) for key in SPEED_LIMITS}
    lcp_ms, lcp_source = _preferred(visitors, visitors.lcp_ms if visitors else None, lab.lcp_ms)
    cls, cls_source = _preferred(visitors, visitors.cls if visitors else None, lab.cls)
    lcp = ceiling_outcome(lcp_ms, limits["lcp_good_ms"], limits["lcp_poor_ms"])
    layout = ceiling_outcome(cls, limits["cls_good"], limits["cls_poor"])
    measured: dict[str, object] = {
        "lcp_ms": lcp_ms,
        "lcp_source": lcp_source,
        "lcp_rating": SPEED_RATINGS[lcp],
        "cls": cls,
        "cls_source": cls_source,
        "cls_rating": SPEED_RATINGS[layout],
    }
    if visitors is not None and visitors.inp_ms is not None:
        reaction = ceiling_outcome(visitors.inp_ms, limits["inp_good_ms"], limits["inp_poor_ms"])
        measured |= {
            "inp_ms": visitors.inp_ms,
            "inp_source": _visitor_source(visitors),
            "inp_rating": SPEED_RATINGS[reaction],
        }
    else:
        reaction = ceiling_outcome(lab.tbt_ms, limits["tbt_good_ms"], limits["tbt_poor_ms"])
        measured |= {
            "tbt_ms": lab.tbt_ms,
            "tbt_source": LAB_SOURCE,
            "tbt_rating": SPEED_RATINGS[reaction],
        }
    measured |= {
        "lab_runs": lab.runs,
        "mobile_performance": speed.mobile_performance,
        "desktop_performance": speed.desktop_performance,
    }
    return _speed_outcome((lcp, layout, reaction)), measured


def _preferred(
    visitors: PageSpeedField | None, visitor_value: float | None, lab_value: float
) -> tuple[float, str]:
    if visitors is None or visitor_value is None:
        return lab_value, LAB_SOURCE
    return visitor_value, _visitor_source(visitors)


def _visitor_source(visitors: PageSpeedField) -> str:
    return f"field_{visitors.scope}"


def _speed_outcome(ratings: tuple[CheckOutcome, ...]) -> CheckOutcome:
    if CheckOutcome.FAILED in ratings:
        return CheckOutcome.FAILED
    if CheckOutcome.PARTIAL in ratings:
        return CheckOutcome.PARTIAL
    return CheckOutcome.PASSED


def _address(url: str | HttpUrl) -> str:
    parts = urlsplit(str(url))
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/') or '/'}{query}"


def _google_meta(page: PageSnapshot) -> str | None:
    return page.meta_robots


def _bing_meta(page: PageSnapshot) -> str | None:
    return page.meta_robots_bing


def _index_problem(
    page: PageSnapshot, bot: str, meta: PageMeta, rules: RobotsRules, linked: set[str], start: str
) -> str | None:
    address = str(page.url)
    if not rules.allows(bot, address):
        return f"robots.txt sperrt {bot}"
    if page.status_code != HTTP_OK:
        if url_key(page.url) == start or url_key(page.url) in linked:
            return f"Status {page.status_code}"
        return None
    for source, value in (("meta", meta(page)), ("X-Robots-Tag", page.x_robots_tag)):
        if directives_for(value, bot) & INDEX_BLOCKING:
            return f"noindex ({source})"
    if page.canonical is not None and _address(page.canonical) != _address(page.url):
        return f"canonical zeigt auf {page.canonical}"
    return None


def _can_index(raw: RawAudit, bot: str, meta: PageMeta) -> CheckReturn:
    if raw.website is None or not raw.website.pages:
        return not_measurable("no_website")
    website = raw.website
    if not has_extended_data(website):
        return no_extended_data()
    if (unavailable := robots_unavailable(website)) is not None:
        return unavailable
    rules = RobotsRules(website.robots_txt)
    linked = {url_key(link) for page in website.pages for link in page.internal_links}
    start = start_key(website)
    reasons: dict[str, str] = {
        url: f"robots.txt sperrt {bot}"
        for url in website.robots_skipped
        if not is_system_path(url) and not rules.allows(bot, url)
    }
    for page in website.pages:
        problem = _index_problem(page, bot, meta, rules, linked, start)
        if problem is not None:
            reasons.setdefault(str(page.url), problem)
    affected = list(reasons)
    outcome = gradation(affected, website)
    return outcome, {
        "pages_total": len(website.pages) + len(website.robots_skipped),
        "start_page_affected": any(url_key(url) == start for url in affected),
        "pages_affected": listed(affected),
        "pages_affected_count": len(affected),
        "pages_affected_reasons": dict(list(reasons.items())[:10]),
        "blocked_by_robots": listed(url for url, why in reasons.items() if "robots.txt" in why),
        "wrong_status": listed(url for url, why in reasons.items() if why.startswith("Status")),
        "noindex_pages": listed(url for url, why in reasons.items() if why.startswith("noindex")),
        "other_canonical_pages": listed(
            url for url, why in reasons.items() if why.startswith("canonical")
        ),
        "robots_txt_present": website.robots_txt is not None,
        "system_paths_blocked": listed(
            url
            for url in website.robots_skipped
            if is_system_path(url) and not rules.allows(bot, url)
        ),
    }


def google_can_index(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _can_index(raw, GOOGLEBOT, _google_meta)


def bing_can_index(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _can_index(raw, BINGBOT, _bing_meta)


def snippet_not_blocked(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if not has_extended_data(website):
        return no_extended_data()
    reasons: dict[str, str] = {}
    for page in content_pages(website):
        found = directives_for(page.meta_robots, GOOGLEBOT) | directives_for(
            page.x_robots_tag, GOOGLEBOT
        )
        blocking = sorted(found & SNIPPET_BLOCKING)
        if blocking:
            reasons[str(page.url)] = ", ".join(blocking)
    affected = list(reasons)
    return gradation(affected, website), {
        "pages_total": len(content_pages(website)),
        "pages_affected": listed(affected),
        "pages_affected_count": len(affected),
        "pages_affected_reasons": dict(list(reasons.items())[:10]),
    }


def _agent_robots(website: WebsiteSnapshot, agent: str) -> tuple[CheckOutcome, dict[str, object]]:
    rules = RobotsRules(website.robots_txt)
    root_blocked = not rules.allows(agent, origin_root(website))
    blocked, system_paths = robots_blocked_pages(website, rules, agent)
    if root_blocked:
        outcome = CheckOutcome.FAILED
    elif blocked:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.PASSED
    return outcome, {
        "agent": agent,
        "site_blocked": root_blocked,
        "pages_blocked": listed(blocked),
        "pages_blocked_count": len(blocked),
        "system_paths_blocked": listed(system_paths),
        "system_paths_blocked_count": len(system_paths),
    }


def _robots_presence(website: WebsiteSnapshot) -> dict[str, object]:
    return {"robots_txt_present": website.robots_txt is not None}


def gemini_allowed(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if (unavailable := robots_unavailable(website)) is not None:
        return unavailable
    outcome, measured = _agent_robots(website, GOOGLE_EXTENDED)
    return outcome, {**measured, **_robots_presence(website), "note": GOOGLE_EXTENDED_NOTE}


def chatgpt_search_allowed(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if (unavailable := robots_unavailable(website)) is not None:
        return unavailable
    outcome, measured = _agent_robots(website, OAI_SEARCHBOT)
    return outcome, {**measured, **_robots_presence(website), "changes_take_effect_hours": 24}


def claude_allowed(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    if (unavailable := robots_unavailable(website)) is not None:
        return unavailable
    results = {agent: _agent_robots(website, agent) for agent in (CLAUDE_SEARCHBOT, CLAUDE_USER)}
    outcomes = [outcome for outcome, _ in results.values()]
    failed = sum(1 for outcome in outcomes if outcome is CheckOutcome.FAILED)
    if failed == len(outcomes):
        outcome = CheckOutcome.FAILED
    elif failed or CheckOutcome.PARTIAL in outcomes:
        outcome = CheckOutcome.PARTIAL
    else:
        outcome = CheckOutcome.PASSED
    return outcome, {
        "agents": {agent: measured for agent, (_, measured) in results.items()},
        **_robots_presence(website),
    }


@dataclass(frozen=True)
class _Pair:
    visitor: AgentFetch
    bot: AgentFetch


def _pair(website: WebsiteSnapshot, agent: str) -> _Pair | CheckReturn:
    fetches = agent_fetches(website)
    if fetches is None or crawl_extras.VISITOR not in fetches or agent not in fetches:
        return not_measurable("no_agent_fetches")
    visitor, bot = fetches[crawl_extras.VISITOR], fetches[agent]
    if visitor.status != HTTP_OK:
        return not_measurable("visitor_fetch_not_200", visitor_status=visitor.status)
    if bot.status is None:
        return not_measurable("bot_fetch_failed", bot_error=bot.error)
    return _Pair(visitor, bot)


def _same_page(raw: RawAudit, agent: str, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pair = _pair(website, agent)
    if not isinstance(pair, _Pair):
        return pair
    measured: dict[str, object] = {
        "agent": agent,
        "visitor_status": pair.visitor.status,
        "bot_status": pair.bot.status,
    }
    if pair.bot.status != HTTP_OK:
        return not_measurable("bot_fetch_not_200", bot_status=pair.bot.status)
    visitor_words, bot_words = len(tokens(pair.visitor.text)), len(tokens(pair.bot.text))
    main_text = number(thresholds, "main_text_words")
    no_text = number(thresholds, "no_text_words")
    min_overlap = number(thresholds, "min_overlap")
    compare_from = number(thresholds, "compare_from_words")
    score = round(similarity(pair.visitor.text, pair.bot.text), 3)
    measured |= {
        "similarity": score,
        "visitor_words": visitor_words,
        "bot_words": bot_words,
        "min_overlap": min_overlap,
        "compare_from_words": compare_from,
    }
    if (visitor_words >= main_text and bot_words <= no_text) or (
        bot_words >= main_text and visitor_words <= no_text
    ):
        return CheckOutcome.FAILED, {**measured, "difference": "one_version_without_text"}
    if min(visitor_words, bot_words) >= compare_from and score < min_overlap:
        return CheckOutcome.FAILED, {**measured, "difference": "text_differs"}
    if max(visitor_words, bot_words) <= no_text:
        return not_measurable("no_comparable_text", **measured)
    return CheckOutcome.PASSED, measured


def _firewall(raw: RawAudit, agent: str) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pair = _pair(website, agent)
    if not isinstance(pair, _Pair):
        return pair
    measured: dict[str, object] = {"agent": agent, "bot_status": pair.bot.status}
    if pair.bot.status in BLOCK_STATUSES:
        return CheckOutcome.FAILED, {**measured, "reason_blocked": f"Status {pair.bot.status}"}
    if is_transient_status(pair.bot.status):
        return not_measurable("bot_fetch_unavailable", **measured)
    if pair.bot.status != HTTP_OK:
        return not_measurable("bot_status_unclear", **measured)
    if looks_like_block_page(pair.bot.text) and not looks_like_block_page(pair.visitor.text):
        return CheckOutcome.FAILED, {**measured, "reason_blocked": "block page"}
    return CheckOutcome.PASSED, measured


def google_same_page(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _same_page(raw, crawl_extras.GOOGLEBOT, thresholds)


def bing_same_page(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _same_page(raw, crawl_extras.BINGBOT, thresholds)


def chatgpt_firewall(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _firewall(raw, crawl_extras.OAI_SEARCHBOT)


def claude_firewall(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    return _firewall(raw, crawl_extras.CLAUDE_SEARCHBOT)


def images_described(raw: RawAudit, thresholds: Thresholds) -> CheckReturn:
    website = crawled(raw)
    if website is None:
        return not_measurable("no_website")
    pages = content_pages(website)
    if all(page.images is None for page in pages):
        return no_extended_data()
    shown = [(page, image) for page in pages for image in page.images or [] if not image.decorative]
    if not shown:
        return CheckOutcome.NOT_APPLICABLE, {"reason": "no_content_images"}
    missing = [(page, image) for page, image in shown if not (image.alt or "").strip()]
    by_page: dict[str, int] = {}
    for page, _ in missing:
        by_page[str(page.url)] = by_page.get(str(page.url), 0) + 1
    return gradation(list(by_page), website), {
        "images_total": len(shown),
        "images_described": len(shown) - len(missing),
        "images_without_alt": len(missing),
        "pages_with_issues": listed(by_page),
        "missing_by_page": dict(list(by_page.items())[:10]),
        "missing_files": listed(image.file_name or image.source or "" for _, image in missing),
    }


CHECKS: dict[str, CheckFunction] = {
    "a2": loading_speed,
    "a4": google_can_index,
    "a7": images_described,
    "a11": gemini_allowed,
    "a12": snippet_not_blocked,
    "a13": google_same_page,
    "a14": bing_can_index,
    "a15": chatgpt_search_allowed,
    "a16": chatgpt_firewall,
    "a17": claude_allowed,
    "a18": claude_firewall,
}


def run(check_id: str, raw: RawAudit, thresholds: dict[str, float | int | str]) -> CheckReturn:
    return dispatch(CHECKS, check_id, raw, thresholds)
