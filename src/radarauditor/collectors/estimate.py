import math
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from radarauditor.collectors import (
    ads,
    chatgpt_scraper,
    dataforseo_maps,
    google_ai_mode,
    listings,
    openai_price,
    paid_data,
    paid_rankings,
    places,
)
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.serp import IMAGE_RESULT_LIMIT
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.db.models import AuditEstimate
from radarauditor.errors import CollectionError

REQUEST_TIMEOUT_SECONDS = 60
DATAFORSEO_USER_DATA_URL = f"{paid_rankings.API_BASE_URL}{paid_rankings.USER_DATA_PATH}"
APIFY_LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
COVERED = "covered"
INSUFFICIENT = "insufficient"
UNCHECKED = "unchecked"
PRICE_MISSING = "Preis nicht hinterlegt"

DATAFORSEO_VOLUME_USD = Decimal("0.09")
DATAFORSEO_BACKLINK_TASK_USD = Decimal("0.024")
DATAFORSEO_BACKLINK_ROW_USD = Decimal("0.000036")
DATAFORSEO_LABS_TASK_USD = Decimal("0.012")
DATAFORSEO_LABS_ITEM_USD = Decimal("0.00012")
DATAFORSEO_HISTORY_TASK_USD = Decimal("0.12")
DATAFORSEO_HISTORY_ITEM_USD = Decimal("0.0012")
DATAFORSEO_AI_MODE_USD = Decimal("0.0012")
DATAFORSEO_SERP_PAGE_USD = Decimal("0.0012")
DATAFORSEO_MAPS_PAGE_USD = Decimal("0.0012")
DATAFORSEO_PROFILE_USD = Decimal("0.003")
DATAFORSEO_REVIEWS_PER_UNIT_USD = Decimal("0.0015")
DATAFORSEO_REVIEWS_UNIT = 10
DATAFORSEO_QUESTIONS_PER_UNIT_USD = Decimal("0.0015")
DATAFORSEO_QUESTIONS_UNIT = 20
DATAFORSEO_CHATGPT_USD = Decimal("0.0024")

APIFY_IMAGE_USD = Decimal("0.0001")

CLAUDE_SERVICE = "assistants"


class _Usage(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    max_monthly_usage_usd: Decimal = Decimal(0)
    monthly_usage_usd: Decimal = Decimal(0)


class _Limits(BaseModel):
    limits: _Usage
    current: _Usage


@dataclass(frozen=True)
class Plan:
    phrases: int
    image_phrases: int
    competitors: int
    questions: int
    repeats: int
    map_searches: int = 0
    question_bytes: int = 0


@dataclass(frozen=True)
class Balances:
    dataforseo_usd: Decimal | None
    apify_usd: Decimal | None
    unreadable: list[str]


@dataclass(frozen=True)
class Verdict:
    status: str
    note: str


def build_estimate(plan: Plan, settings: Settings) -> list[AuditEstimate]:
    dataforseo = settings.dataforseo_eur_per_usd
    domains = 1 + plan.competitors
    backlink_rows = DATAFORSEO_BACKLINK_ROW_USD * paid_data.REFERRING_DOMAINS_LIMIT
    paid: list[tuple[str, int, Decimal]] = [
        (paid_rankings.VOLUME_PATH, 1, DATAFORSEO_VOLUME_USD),
        (
            paid_data.SUMMARY_PATH,
            domains,
            DATAFORSEO_BACKLINK_TASK_USD + DATAFORSEO_BACKLINK_ROW_USD,
        ),
        (paid_data.REFERRING_DOMAINS_PATH, domains, DATAFORSEO_BACKLINK_TASK_USD + backlink_rows),
        (
            paid_data.RANKED_KEYWORDS_PATH,
            domains,
            DATAFORSEO_LABS_TASK_USD + DATAFORSEO_LABS_ITEM_USD,
        ),
        (
            paid_data.INTERSECTION_PATH,
            plan.competitors,
            DATAFORSEO_LABS_TASK_USD + DATAFORSEO_LABS_ITEM_USD * paid_data.GAP_LIMIT,
        ),
        (
            paid_data.HISTORY_PATH,
            1,
            DATAFORSEO_HISTORY_TASK_USD + DATAFORSEO_HISTORY_ITEM_USD * paid_data.HISTORY_MONTHS,
        ),
        (google_ai_mode.POST_PATH, plan.questions * plan.repeats, DATAFORSEO_AI_MODE_USD),
        *_priority_lines(plan),
    ]
    ads_price = ads.load_google_ads_price(settings.routes_path)
    if ads_price is not None:
        paid.append((ads.GOOGLE_ADS_POST_PATH, 1, ads_price))
    lines = [
        _line(Source.DATAFORSEO, service, calls, unit * dataforseo * calls)
        for service, calls, unit in paid
    ]
    lines.extend(_places_lines(plan, settings))
    lines.extend(_apify_lines(plan, settings))
    lines.extend(_openai_lines(plan, settings))
    lines.append(_line(Source.CLAUDE, CLAUDE_SERVICE, plan.questions * plan.repeats, Decimal(0)))
    return lines


def _priority_lines(plan: Plan) -> list[tuple[str, int, Decimal]]:
    profiles = 1 + plan.competitors
    reviews = math.ceil(dataforseo_maps.REVIEW_DEPTH / DATAFORSEO_REVIEWS_UNIT)
    questions = math.ceil(dataforseo_maps.QUESTION_DEPTH / DATAFORSEO_QUESTIONS_UNIT)
    return [
        (
            task_post(paid_rankings.ORGANIC_TASKS),
            plan.phrases,
            DATAFORSEO_SERP_PAGE_USD * paid_rankings.GOOGLE_PAGES,
        ),
        (task_post(dataforseo_maps.MAPS_TASKS), plan.map_searches, DATAFORSEO_MAPS_PAGE_USD),
        (task_post(dataforseo_maps.INFO_TASKS), profiles, DATAFORSEO_PROFILE_USD),
        (
            task_post(dataforseo_maps.REVIEWS_TASKS),
            profiles,
            DATAFORSEO_REVIEWS_PER_UNIT_USD * reviews,
        ),
        (
            task_post(dataforseo_maps.QUESTIONS_TASKS),
            profiles,
            DATAFORSEO_QUESTIONS_PER_UNIT_USD * questions,
        ),
        (
            task_post(chatgpt_scraper.SCRAPER_TASKS),
            plan.questions * plan.repeats,
            DATAFORSEO_CHATGPT_USD,
        ),
    ]


def task_post(base: str) -> str:
    return f"{base}{paid_rankings.TASK_POST}"


def _line(source: Source, service: str, calls: int, cost: Decimal) -> AuditEstimate:
    unit = cost / calls if calls else Decimal(0)
    return AuditEstimate(
        source=source.value, service=service, calls=calls, unit_cost=unit, cost=cost
    )


def _places_lines(plan: Plan, settings: Settings) -> list[AuditEstimate]:
    if settings.google_api_key is None:
        return []
    profiles = 1 + plan.competitors
    sku = places.PlacesSku
    specs = [
        (sku.PLACE_DETAILS_ENTERPRISE_ATMOSPHERE, profiles),
        (sku.TEXT_SEARCH_ENTERPRISE_ATMOSPHERE, 1),
        (sku.TEXT_SEARCH_ENTERPRISE, 1),
        (sku.TEXT_SEARCH_IDS_ONLY, plan.map_searches + profiles),
    ]
    return [
        _line(
            Source.PLACES,
            tier.value,
            calls,
            places.request_cost_eur(tier, settings.google_eur_per_usd) * calls,
        )
        for tier, calls in specs
    ]


def _apify_lines(plan: Plan, settings: Settings) -> list[AuditEstimate]:
    rate = settings.apify_eur_per_usd
    images = plan.image_phrases * IMAGE_RESULT_LIMIT * APIFY_IMAGE_USD
    specs = [
        ("google_images", plan.image_phrases, images),
        (listings.APPLE_SERVICE, 1, _maps_cost(listings.APPLE_START_USD, listings.APPLE_PLACE_USD)),
        (listings.BING_SERVICE, 1, _maps_cost(listings.BING_START_USD, listings.BING_RESULT_USD)),
    ]
    lines = [_line(Source.APIFY, service, calls, usd * rate) for service, calls, usd in specs]
    meta = ads.load_meta_route(settings.routes_path)
    if meta.confirmed and meta.price_usd is not None:
        calls = 1 + plan.competitors
        lines.append(_line(Source.APIFY, ads.META_SERVICE, calls, meta.price_usd * rate * calls))
    return lines


def _openai_lines(plan: Plan, settings: Settings) -> list[AuditEstimate]:
    if settings.openai_api_key is None:
        return []
    price = openai_price.load_token_price(settings.routes_path, settings.openai_model)
    if price is None:
        return []
    calls = plan.questions * plan.repeats
    usd = openai_price.max_call_cost_usd(price, plan.question_bytes) * calls
    return [
        _line(Source.CHATGPT, openai_price.OPENAI_SERVICE, calls, usd * settings.openai_eur_per_usd)
    ]


def unpriced_steps(settings: Settings) -> list[str]:
    missing: list[str] = []
    if ads.load_google_ads_price(settings.routes_path) is None:
        missing.append(ads.GOOGLE_ADS_POST_PATH)
    meta = ads.load_meta_route(settings.routes_path)
    if meta.confirmed and meta.price_usd is None:
        missing.append(ads.META_SERVICE)
    if (
        settings.openai_api_key is not None
        and openai_price.load_token_price(settings.routes_path, settings.openai_model) is None
    ):
        missing.append(f"{openai_price.OPENAI_SERVICE} ({settings.openai_model})")
    return missing


def _maps_cost(start: Decimal, per_result: Decimal) -> Decimal:
    return start + listings.MAPS_MAX_RESULTS * per_result


async def read_balances(settings: Settings) -> Balances:
    unreadable: list[str] = []
    dataforseo = await _dataforseo_balance(settings, unreadable)
    apify = await _apify_remaining(settings, unreadable)
    return Balances(dataforseo, apify, unreadable)


async def _dataforseo_balance(settings: Settings, unreadable: list[str]) -> Decimal | None:
    if not settings.dataforseo_login or settings.dataforseo_password is None:
        return None
    auth = (settings.dataforseo_login, settings.dataforseo_password.get_secret_value())
    try:
        return await paid_rankings.read_balance(settings.dataforseo_url.rstrip("/"), auth)
    except CollectionError as error:
        unreadable.append(f"DataForSEO: {error}")
        return None


async def _apify_remaining(settings: Settings, unreadable: list[str]) -> Decimal | None:
    if settings.apify_token is None:
        return None
    headers = {"Authorization": f"Bearer {settings.apify_token.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(APIFY_LIMITS_URL, headers=headers)
        response.raise_for_status()
        usage = _Limits.model_validate(response.json()["data"])
        return usage.limits.max_monthly_usage_usd - usage.current.monthly_usage_usd
    except (httpx.HTTPError, ValueError, KeyError, ValidationError) as error:
        unreadable.append(f"Apify: {type(error).__name__}")
        return None


def judge(
    lines: list[AuditEstimate], balances: Balances, settings: Settings, gate: SourceGate
) -> Verdict:
    totals: dict[str, Decimal] = {}
    for line in lines:
        totals[line.source] = totals.get(line.source, Decimal(0)) + line.cost
    available: dict[str, tuple[str, Decimal | None]] = {
        Source.DATAFORSEO.value: (
            "Guthaben",
            _eur(balances.dataforseo_usd, settings.dataforseo_eur_per_usd),
        ),
        Source.APIFY.value: (
            "Restguthaben im Monat",
            _eur(balances.apify_usd, settings.apify_eur_per_usd),
        ),
    }
    problems: list[str] = []
    for source, total in totals.items():
        label, credit = available.get(source, ("", None))
        if credit is not None and total > credit:
            problems.append(f"{source}: Voranschlag {_money(total)}, {label} {_money(credit)}")
        room = gate.remaining_cost(Source(source))
        if room is not None and total > room:
            problems.append(
                f"{source}: Voranschlag {_money(total)}, Monatsgrenze lässt {_money(room)} übrig"
            )
    unpriced = unpriced_steps(settings)
    suffix = f"; {PRICE_MISSING}: {', '.join(unpriced)}" if unpriced else ""
    if problems:
        return Verdict(INSUFFICIENT, "; ".join(problems) + suffix)
    if balances.unreadable:
        note = "Guthaben nicht lesbar: " + "; ".join(balances.unreadable)
        return Verdict(UNCHECKED, note + suffix)
    grand_total = _money(sum(totals.values(), Decimal(0)))
    return Verdict(COVERED, f"Voranschlag {grand_total} gedeckt{suffix}")


def _eur(usd: Decimal | None, rate: Decimal) -> Decimal | None:
    return None if usd is None else usd * rate


def _money(amount: Decimal) -> str:
    return f"{amount:.2f} EUR".replace(".", ",")
