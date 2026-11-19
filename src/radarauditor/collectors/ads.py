import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from radarauditor.collectors.apify import run_actor
from radarauditor.collectors.identity import names_agree
from radarauditor.collectors.paid_rankings import GERMANY_LOCATION_CODE, DataForSeoClient, run_tasks
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import GoogleAd, GoogleAdsSnapshot, MetaAd, MetaAdsSnapshot
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.errors import CollectionError, ConfigurationError
from radarauditor.feedback.routes import load_measurements

logger = logging.getLogger(__name__)

GOOGLE_ADS_BASE = "/v3/serp/google/ads_search"
GOOGLE_ADS_POST_PATH = f"{GOOGLE_ADS_BASE}/task_post"
GOOGLE_ADS_ITEM_TYPE = "ads_search"
GOOGLE_ADS_WINDOW_DAYS = 90
RAW_ITEM_LIMIT = 20
MAX_FORMAT_PROBLEMS = 3
GOOGLE_ADS_MEASUREMENT = "ads_google"
GOOGLE_ADS_PRICE_KEY = "price_usd_per_task"
META_PRICE_KEY = "price_usd_per_run"
META_MEASUREMENT = "ads_meta"
META_SERVICE = "meta_ads"
META_RESULT_LIMIT = 50
META_RUN_TIMEOUT_SECONDS = 300
PAGE_NAME_KEY = "page_name"
AD_ID_KEY = "ad_id"


@dataclass(frozen=True)
class MetaRoute:
    actor: str
    input_url: str
    field_mapping: dict[str, str]
    confirmed: bool
    price_usd: Decimal | None = None


def _price(path: Path, measurement: str, key: str) -> Decimal | None:
    routes = load_measurements(path)[measurement].routes
    value = (routes[0].model_extra or {}).get(key) if routes else None
    if value is None:
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation as error:
        raise ConfigurationError(
            f"{path}: {measurement} {key} is not a number: {value!r}"
        ) from error
    if price <= 0:
        raise ConfigurationError(f"{path}: {measurement} {key} must be above zero, got {price}")
    return price


def load_google_ads_price(path: Path) -> Decimal | None:
    return _price(path, GOOGLE_ADS_MEASUREMENT, GOOGLE_ADS_PRICE_KEY)


def load_meta_route(path: Path) -> MetaRoute:
    measurement = load_measurements(path)[META_MEASUREMENT]
    if not measurement.routes:
        raise ConfigurationError(f"{path}: measurement {META_MEASUREMENT} has no route")
    route = measurement.routes[0]
    extra = route.model_extra or {}
    input_url = extra.get("input_url")
    mapping = extra.get("field_mapping")
    confirmed = extra.get("bestaetigt")
    if not isinstance(input_url, str) or "{name}" not in input_url:
        raise ConfigurationError(f"{path}: {META_MEASUREMENT} needs an input_url with {{name}}")
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
    ):
        raise ConfigurationError(f"{path}: {META_MEASUREMENT} needs field_mapping as a mapping")
    if not isinstance(confirmed, bool):
        raise ConfigurationError(f"{path}: {META_MEASUREMENT} needs bestaetigt true or false")
    if confirmed and not {PAGE_NAME_KEY, AD_ID_KEY} <= mapping.keys():
        raise ConfigurationError(
            f"{path}: {META_MEASUREMENT} is confirmed but field_mapping lacks "
            f"{PAGE_NAME_KEY} or {AD_ID_KEY}"
        )
    price = _price(path, META_MEASUREMENT, META_PRICE_KEY)
    return MetaRoute(route.endpoint, input_url, dict(mapping), confirmed, price)


async def collect_google_ads(
    client: DataForSeoClient, domain: str, now: datetime
) -> GoogleAdsSnapshot:
    today = now.date()
    task = {
        "target": domain,
        "location_code": GERMANY_LOCATION_CODE,
        "date_from": (today - timedelta(days=GOOGLE_ADS_WINDOW_DAYS)).isoformat(),
        "date_to": today.isoformat(),
    }
    batch = await run_tasks(client, GOOGLE_ADS_BASE, [task], priority=None)
    if 0 in batch.failed:
        raise CollectionError(f"DataForSEO Google-Anzeigen: {batch.failed[0]}")
    return google_ads_from_rows(batch.results[0], domain, now)


def google_ads_from_rows(
    rows: list[dict[str, Any]], domain: str, now: datetime
) -> GoogleAdsSnapshot:
    problems: list[str] = []
    items: list[dict[str, Any]] = []
    returned_location: int | None = None
    items_count: int | None = None
    if not rows:
        problems.append("Die Antwort enthält kein Ergebnisobjekt")
    for row in rows:
        location = row.get("location_code")
        if isinstance(location, int) and returned_location is None:
            returned_location = location
        count = row.get("items_count")
        if isinstance(count, int):
            items_count = (items_count or 0) + count
        row_items = row.get("items")
        if row_items is None:
            continue
        if not isinstance(row_items, list):
            problems.append("items ist keine Liste")
            continue
        for item in row_items:
            if isinstance(item, dict):
                items.append(item)
            else:
                problems.append("Ein Element der Antwort ist kein Objekt")
    ads: list[GoogleAd] = []
    for item in items:
        if item.get("type") != GOOGLE_ADS_ITEM_TYPE:
            problems.append(f"Element vom Typ {item.get('type')!r} statt {GOOGLE_ADS_ITEM_TYPE!r}")
            continue
        ad = _google_ad(item)
        if ad.advertiser_id is None or ad.creative_id is None:
            problems.append("Anzeige ohne advertiser_id oder creative_id")
            continue
        ads.append(ad)
    if items_count is not None and items_count != len(items):
        problems.append(f"items_count {items_count} passt nicht zu {len(items)} Elementen")
    unique = list(dict.fromkeys(problems))[:MAX_FORMAT_PROBLEMS]
    return GoogleAdsSnapshot(
        target=domain,
        window_days=GOOGLE_ADS_WINDOW_DAYS,
        location_code=GERMANY_LOCATION_CODE,
        returned_location_code=returned_location,
        ads=ads,
        items_count=items_count,
        format_problem="; ".join(unique) if unique else None,
        raw_items=items[:RAW_ITEM_LIMIT],
        measured_at=now,
    )


def _text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    return value if isinstance(value, str) and value else None


def _google_ad(item: dict[str, Any]) -> GoogleAd:
    verified = item.get("verified")
    return GoogleAd(
        advertiser_id=_text(item, "advertiser_id"),
        creative_id=_text(item, "creative_id"),
        advertiser_name=_text(item, "title"),
        format=_text(item, "format"),
        verified=verified if isinstance(verified, bool) else None,
        first_shown=_text(item, "first_shown"),
        last_shown=_text(item, "last_shown"),
        url=_text(item, "url"),
    )


async def collect_meta_ads(
    settings: Settings,
    gate: SourceGate,
    route: MetaRoute,
    name: str,
    competitors: list[str],
    now: datetime,
) -> MetaAdsSnapshot:
    async with httpx.AsyncClient() as client:
        items = await _meta_items(client, settings, gate, route, name)
        counts: dict[str, int] = {}
        if route.confirmed:
            for rival in competitors:
                rival_items = await _meta_items(client, settings, gate, route, rival)
                counts[rival] = len(_meta_ads(rival_items, route, rival)[0])
    own, others = _meta_ads(items, route, name) if route.confirmed else ([], [])
    return MetaAdsSnapshot(
        searched_name=name,
        actor=route.actor,
        confirmed=route.confirmed,
        own_ads=own,
        other_pages=list(dict.fromkeys(others)),
        competitor_counts=counts,
        items_total=len(items),
        raw_items=items[:RAW_ITEM_LIMIT],
        measured_at=now,
    )


async def _meta_items(
    client: httpx.AsyncClient, settings: Settings, gate: SourceGate, route: MetaRoute, name: str
) -> list[dict[str, Any]]:
    if settings.apify_token is None:
        raise CollectionError("Apify-Zugangsdaten fehlen")
    gate.acquire(Source.APIFY)
    payload = {
        "startUrls": [{"url": route.input_url.format(name=quote_plus(name))}],
        "resultsLimit": META_RESULT_LIMIT,
    }

    def book(cost_usd: Decimal) -> None:
        gate.record_cost(Source.APIFY, cost_usd * settings.apify_eur_per_usd, service=META_SERVICE)

    return await run_actor(
        client,
        settings.apify_token,
        route.actor.replace("/", "~"),
        payload,
        META_RUN_TIMEOUT_SECONDS,
        book,
        step="ads_meta",
    )


def _pick(item: dict[str, Any], path: str | None) -> str | None:
    if path is None:
        return None
    value: object = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _meta_ads(
    items: list[dict[str, Any]], route: MetaRoute, name: str
) -> tuple[list[MetaAd], list[str]]:
    page_field = route.field_mapping.get(PAGE_NAME_KEY)
    id_field = route.field_mapping.get(AD_ID_KEY)
    own: list[MetaAd] = []
    others: list[str] = []
    for item in items:
        page = _pick(item, page_field)
        if page is None:
            continue
        if names_agree(name, page):
            own.append(MetaAd(ad_id=_pick(item, id_field), page_name=page))
        else:
            others.append(page)
    return own, others
