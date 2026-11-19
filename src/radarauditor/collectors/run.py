import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import HttpUrl, TypeAdapter

from radarauditor.collectors import (
    access,
    ads,
    apify_serp,
    assistant_handoff,
    assistants,
    chatgpt_scraper,
    estimate,
    geocode,
    google_ai_mode,
    google_auth,
    identify,
    legal_notice,
    listings,
    map_listings,
    pagespeed,
    paid_data,
    paid_rankings,
    raw_answers,
    serp,
    validation,
    website,
)
from radarauditor.collectors.browser import BrowserService
from radarauditor.collectors.maps import ProfileTarget
from radarauditor.collectors.paid_rankings import PaidSearchBatch
from radarauditor.collectors.patterns import host_of
from radarauditor.collectors.profiles import PROFILE_WAYS, Business, ProfileRoute
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import (
    AccessSnapshot,
    AssistantAnswer,
    BusinessFacts,
    DataRoute,
    DeferredSearch,
    DirectoryHit,
    GoogleAdsSnapshot,
    GridPoint,
    IdentifiedValue,
    MapRanking,
    MapResultList,
    MeasurementRecord,
    MetaAdsSnapshot,
    PageSpeedSnapshot,
    PaidSnapshot,
    PhaseTiming,
    PlaceSnapshot,
    RawAudit,
    RouteCheck,
    SerpResult,
    WebsiteSnapshot,
)
from radarauditor.collectors.steps import StepStore, StoredStep
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.db.models import AuditKind, Competitor, Customer, Keyword, Location
from radarauditor.db.repositories import Repositories
from radarauditor.errors import (
    AuditHalted,
    CollectionError,
    ConfigurationError,
    DailyCapReachedError,
    MissingInputError,
    PreflightError,
    ProfileNotFoundError,
    ScoringError,
    SourceDisabledError,
    SourceUnavailableError,
    StorageError,
)
from radarauditor.feedback.events import Plain, Result, plain_reason
from radarauditor.feedback.hub import CHECK_STAGE, Feedback, RouteOutcome
from radarauditor.feedback.routes import load_measurements
from radarauditor.industries import load_industries
from radarauditor.scoring.catalog import load_catalogue

logger = logging.getLogger(__name__)

COMPETITOR_COUNT = 3
MAIN_PHRASE_COUNT = 5
GRID_CHECK_ID = "j3"
PROBE_PAGES = 1
ASSISTANT_PROFILES = "assistants"
PERSON_PROFILE_SUFFIX = "-person"
PAID_SWITCHED_OFF = Plain("Der Bezahldienst DataForSEO ist in den Einstellungen ausgeschaltet")
NO_PRICE_REASON = Plain(
    "nicht gemessen: Preis nicht hinterlegt, ein bezahlter Abruf ohne Preis wird nicht ausgeführt"
)
PAID_WITHOUT_CREDENTIALS = Plain(
    "Der Bezahldienst DataForSEO ist ausgesetzt, die Zugangsdaten fehlen"
)
NO_ANSWER = Plain("Keine Antwort erhalten")
ASKED_WITH_PERSON = Plain("Die Frage wurde zusammen mit Daniel gestellt")
NO_PROFILE_FOUND = "no Google Business Profile found"
ASSISTANT_ROUTE = "assistant:"
PLACES_MEASUREMENT = "places"
DEFAULT_PLACE_ROUTE = "places_api"
NOT_RUN_REASON = "Die Messung wurde nicht ausgeführt"
PROBE_ITEMS = 1
ASSISTANT_SERVICES = {
    google_ai_mode.POST_PATH: Source.GOOGLE_AI,
    estimate.task_post(chatgpt_scraper.SCRAPER_TASKS): Source.CHATGPT,
}
STEP_TYPES: dict[str, Any] = {
    "website": WebsiteSnapshot | None,
    "pagespeed": PageSpeedSnapshot,
    "places": PlaceSnapshot,
    "competitors": list[PlaceSnapshot],
    "map_rankings": list[MapRanking],
    "map_results": MapResultList,
    "grid": list[GridPoint],
    "serps": list[SerpResult],
    "bing": list[SerpResult],
    "images": list[SerpResult],
    "listings": list[DirectoryHit],
    "assistants": list[AssistantAnswer],
    "access": AccessSnapshot,
    "paid": PaidSnapshot,
    "ads_google": GoogleAdsSnapshot,
    "ads_meta": MetaAdsSnapshot,
}


@dataclass(frozen=True)
class _Context:
    customer: Customer
    location: Location | None
    competitors: list[Competitor]
    keywords: list[Keyword]
    settings: Settings
    kind: AuditKind
    repositories: Repositories
    gate: SourceGate
    browser: BrowserService
    profiles: ProfileRoute
    feedback: Feedback
    steps: StepStore
    timings: list[PhaseTiming] = field(default_factory=list)
    records: list[MeasurementRecord] = field(default_factory=list)
    manual: frozenset[str] = frozenset()
    only: frozenset[str] | None = None
    probe: bool = False
    legal_notice_business: BusinessFacts | None = None

    @property
    def paid_switched_on(self) -> bool:
        return self.gate.is_enabled(Source.DATAFORSEO)

    @property
    def paid_active(self) -> bool:
        return self.paid_switched_on and paid_rankings.is_configured(self.settings)

    def dataforseo(self) -> paid_rankings.DataForSeoClient:
        return paid_rankings.DataForSeoClient(self.settings, self.gate)


@dataclass(frozen=True)
class _Outcome[T]:
    value: T | None
    warnings: list[str]
    failure: str | BaseException | None = None
    restored: bool = False


class _Notes(list[str]):
    def __init__(self, previous: object = None) -> None:
        super().__init__()
        self.failure: str | BaseException | None = None
        self.previous = previous


@dataclass(frozen=True)
class _ProfileOutcomes:
    place: _Outcome[PlaceSnapshot]
    competitors: _Outcome[list[PlaceSnapshot]]
    map_rankings: _Outcome[list[MapRanking]]
    map_results: _Outcome[MapResultList]
    grid: _Outcome[list[GridPoint]]
    missing: bool = False
    place_valid: bool = False


@dataclass(frozen=True)
class _SearchOutcomes:
    google: _Outcome[list[SerpResult]]
    bing: _Outcome[list[SerpResult]]
    images: _Outcome[list[SerpResult]]
    deferred: list[DeferredSearch]

    def serps(self) -> list[SerpResult]:
        return (self.google.value or []) + (self.bing.value or []) + (self.images.value or [])


@dataclass(frozen=True)
class _SourceOutcomes:
    speed: _Outcome[PageSpeedSnapshot]
    profiles: _ProfileOutcomes
    searches: _SearchOutcomes
    listings: _Outcome[list[DirectoryHit]]
    answers: _Outcome[list[AssistantAnswer]]
    own_data: _Outcome[AccessSnapshot]
    paid: _Outcome[PaidSnapshot]
    ads_google: _Outcome[GoogleAdsSnapshot]
    ads_meta: _Outcome[MetaAdsSnapshot]

    def warnings(self) -> list[str]:
        profiles, searches = self.profiles, self.searches
        outcomes = (
            self.speed,
            profiles.place,
            profiles.competitors,
            profiles.map_rankings,
            profiles.map_results,
            profiles.grid,
            searches.google,
            searches.bing,
            searches.images,
            self.listings,
            self.answers,
            self.own_data,
            self.paid,
            self.ads_google,
            self.ads_meta,
        )
        return [warning for outcome in outcomes for warning in outcome.warnings]


@dataclass
class _GoogleState:
    deferred: list[DeferredSearch] = field(default_factory=list)
    blocked_reason: str | None = None


async def collect_audit(
    audit_id: UUID,
    repositories: Repositories,
    settings: Settings,
    gate: SourceGate,
    feedback: Feedback | None = None,
    *,
    only: frozenset[str] | None = None,
    probe: bool = False,
    steps: StepStore | None = None,
) -> RawAudit:
    raw_answers.bind(gate.keep_raw)
    audit = repositories.audit(audit_id)
    customer = repositories.customer(audit.customer_id)
    locations = repositories.locations(customer.id)
    if feedback is None:
        feedback = Feedback.create(audit_id, customer.name, settings, repositories)
    if steps is None:
        steps = StepStore(audit_id, repositories)
    feedback.reports = steps.reports
    dataforseo = (
        paid_rankings.DataForSeoClient(settings, gate)
        if gate.is_enabled(Source.DATAFORSEO) and paid_rankings.is_configured(settings)
        else None
    )
    async with BrowserService(
        settings.browser_profile_dir, channel=settings.browser_channel
    ) as browser:
        context = _Context(
            customer=customer,
            location=locations[0] if locations else None,
            competitors=repositories.competitors(customer.id),
            keywords=repositories.keywords(customer.id),
            settings=settings,
            kind=audit.kind,
            repositories=repositories,
            gate=gate,
            browser=browser,
            profiles=ProfileRoute(
                browser,
                gate,
                settings.google_api_key,
                settings.google_eur_per_usd,
                dataforseo,
                _profile_ways(settings),
            ),
            feedback=feedback,
            steps=steps,
            only=only,
            probe=probe,
        )
        logger.info("Collecting audit %s for %s", audit_id, customer.name)
        raw = await _collect(audit_id, context)
    if not probe:
        steps.keep_reports(feedback.reports)
    return raw


async def _phase[T](context: _Context, name: str, work: Awaitable[T]) -> T:
    started = time.monotonic()
    try:
        return await work
    finally:
        seconds = round(time.monotonic() - started, 1)
        context.timings.append(PhaseTiming(phase=name, seconds=seconds))


async def _collect(audit_id: UUID, context: _Context) -> RawAudit:
    started = time.monotonic()
    warnings: list[str] = []
    crawled = await _phase(context, "website", _website(context, warnings))
    site = crawled.value
    context, identified = await _phase(context, "identify", _identify(context, site, warnings))
    context = await _phase(context, "geocode", _geocoded(context, warnings))
    await _phase(context, "estimate", _estimate(audit_id, context))
    context, preflight = await _phase(context, "preflight", _preflight(context))
    if site is not None and not crawled.restored:
        context.feedback.route_done("website", Result.MEASURED)
    sources = await _collect_sources(context, site)
    warnings.extend(sources.warnings())
    for warning in warnings:
        logger.warning("Audit %s: %s", audit_id, warning)
    total = PhaseTiming(phase="total", seconds=round(time.monotonic() - started, 1))
    timings = [*context.timings, total]
    logger.info(
        "Audit %s timings: %s",
        audit_id,
        ", ".join(f"{timing.phase} {timing.seconds} s" for timing in timings),
    )
    profiles, searches = sources.profiles, sources.searches
    return RawAudit(
        website=site,
        pagespeed=sources.speed.value,
        place=profiles.place.value,
        competitor_places=profiles.competitors.value or [],
        serps=searches.serps(),
        directories=sources.listings.value or [],
        assistant_answers=sources.answers.value or [],
        collected_at=datetime.now(UTC),
        warnings=warnings,
        grid=profiles.grid.value or [],
        access=sources.own_data.value,
        paid=sources.paid.value,
        ads_google=sources.ads_google.value,
        ads_meta=sources.ads_meta.value,
        capped_sources=sorted(context.gate.exhausted),
        cost_capped_sources=sorted(context.gate.cost_capped),
        deferred_searches=searches.deferred,
        map_rankings=profiles.map_rankings.value or [],
        map_results=profiles.map_results.value,
        identified=identified,
        timings=timings,
        preflight=preflight,
        business=_business_facts(context),
        profile_missing=profiles.missing,
        route_reports=list(context.feedback.reports),
        measurements=list(context.records),
        has_location=context.location is not None,
    )


def _business_facts(context: _Context) -> BusinessFacts:
    if context.legal_notice_business is not None:
        return context.legal_notice_business
    location = context.location
    customer = context.customer

    def given(value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    return BusinessFacts(
        name=given(customer.name),
        city=given(customer.city),
        street=given(location.street) if location is not None else None,
        postal_code=given(location.postal_code) if location is not None else None,
        phone=given(location.phone) if location is not None else None,
    )


async def _estimate(audit_id: UUID, context: _Context) -> None:
    if not context.settings.estimate_before_start or context.steps.resuming:
        return
    settings, gate = context.settings, context.gate
    phrases = list(dict.fromkeys(keyword.search_phrase for keyword in context.keywords))
    brand = context.customer.name.strip()
    if brand and brand.casefold() not in {phrase.casefold() for phrase in phrases}:
        phrases.append(brand)
    map_phrases = {keyword.search_phrase for keyword in context.keywords}
    questions = _questions(context)
    plan = estimate.Plan(
        phrases=len(phrases),
        image_phrases=len(_main_phrases(context)) if context.keywords else 0,
        map_searches=len(map_phrases) + _grid_size(context.settings) ** 2 if map_phrases else 0,
        competitors=COMPETITOR_COUNT,
        questions=len(questions),
        repeats=assistants.REPEATS_PER_QUESTION,
        question_bytes=max((len(question.encode()) for question in questions), default=0),
    )
    lines = [
        line
        for line in estimate.build_estimate(plan, settings)
        if gate.is_enabled(Source(line.source))
        and all(
            gate.is_enabled(source)
            for service, source in ASSISTANT_SERVICES.items()
            if line.service == service
        )
    ]
    balances = await estimate.read_balances(settings)
    verdict = estimate.judge(lines, balances, settings, gate)
    context.repositories.save_estimate(audit_id, lines, verdict.status, verdict.note)
    logger.info("Audit %s estimate: %s", audit_id, verdict.note)
    if verdict.status == estimate.INSUFFICIENT:
        raise PreflightError(f"Kostenvoranschlag nicht gedeckt: {verdict.note}")


async def _preflight(context: _Context) -> tuple[_Context, list[RouteCheck]]:
    if not context.settings.preflight_routes or context.steps.resuming:
        return context, []
    checks = [
        *await _probe_assistants(context),
        *await _probe_search(context),
        *await _probe_speed(context),
    ]
    manual = frozenset(
        check.route.removeprefix(ASSISTANT_ROUTE)
        for check in checks
        if context.settings.assistant_handoff
        and not check.ok
        and check.route.startswith(ASSISTANT_ROUTE)
        and check.route != f"{ASSISTANT_ROUTE}{assistants.CLAUDE}"
    )
    for check in checks:
        logger.info("Probe %s: %s %s", check.route, "ok" if check.ok else "failed", check.detail)
    outcomes = [_probe_outcome(check, manual) for check in checks]
    context.feedback.routes_done(outcomes, phase="vorab")
    return dataclasses.replace(context, manual=manual), checks


def _probe_outcome(check: RouteCheck, manual: frozenset[str]) -> RouteOutcome:
    if check.ok:
        return RouteOutcome(check.route, Result.MEASURED)
    if check.route.removeprefix(ASSISTANT_ROUTE) in manual:
        return RouteOutcome(check.route, Result.WITH_PERSON, check.detail)
    return RouteOutcome(check.route, Result.FAILED, check.detail)


async def _probe_assistants(context: _Context) -> list[RouteCheck]:
    if not assistants.enabled_providers(context.settings, context.gate):
        return []
    question = _questions(context)[0]
    results = await assistants.probe(
        question, context.settings, context.gate, _assistant_browser_for(context)
    )
    return [
        RouteCheck(
            route=f"{ASSISTANT_ROUTE}{provider}",
            ok=error is None or _api_takes_over(context.settings, provider),
            detail=error,
        )
        for provider, error in results.items()
    ]


def _api_takes_over(settings: Settings, provider: str) -> bool:
    return not settings.assistant_handoff and assistants.has_api_fallback(settings, provider)


async def _probe_search(context: _Context) -> list[RouteCheck]:
    brand = context.customer.name.strip()
    if not brand or not context.gate.is_enabled(Source.GOOGLE_SEARCH):
        return []
    if context.paid_active:
        return [RouteCheck(route="google_search", ok=True)]
    if apify_serp.is_available(context.settings, context.gate):
        paid = await apify_serp.search_web(
            context.settings, context.gate, [brand], pages=PROBE_PAGES
        )
        if paid.results:
            return [RouteCheck(route="google_search", ok=True)]
    batch = await serp.search_all(context.browser, [brand])
    ok = bool(batch.results) and not batch.deferred
    return [RouteCheck(route="google_search", ok=ok, detail=None if ok else batch.reason)]


async def _probe_speed(context: _Context) -> list[RouteCheck]:
    if context.customer.website is None:
        return []
    try:
        await pagespeed.measure(context.customer.website, context.settings.google_api_key)
    except CollectionError as error:
        return [RouteCheck(route="pagespeed", ok=False, detail=str(error))]
    return [RouteCheck(route="pagespeed", ok=True)]


def _tell(context: _Context, text: str) -> None:
    try:
        assistant_handoff.HandoffBoard(context.settings.handoff_board_path).notify(text)
    except CollectionError as error:
        logger.warning("Message not put on the board: %s", error)


async def _collect_sources(context: _Context, site: WebsiteSnapshot | None) -> _SourceOutcomes:
    try:
        async with asyncio.TaskGroup() as group:
            speed = group.create_task(_phase(context, "pagespeed", _measured_speed(context)))
            profiles = group.create_task(
                _phase(context, "google_profiles", _google_profiles(context))
            )
            searches = group.create_task(_phase(context, "searches", _searches(context)))
            map_hits = group.create_task(_phase(context, "listings", _measured_listings(context)))
            answers = group.create_task(_phase(context, "assistants", _measured_answers(context)))
            own_data = group.create_task(_customer_access(context))
            google_ads = group.create_task(
                _phase(context, "ads_google", _measured_google_ads(context))
            )
            meta_ads = group.create_task(_phase(context, "ads_meta", _measured_meta_ads(context)))
    except BaseExceptionGroup as failed:
        raise _first_failure(failed) from None
    found = profiles.result()
    paid = await _phase(
        context,
        "paid",
        _paid_outcome(
            context,
            found.competitors.value or [],
            found.place.value if found.place_valid else None,
        ),
    )
    return _SourceOutcomes(
        speed.result(),
        found,
        searches.result(),
        map_hits.result(),
        answers.result(),
        own_data.result(),
        paid,
        google_ads.result(),
        meta_ads.result(),
    )


def _first_failure(group: BaseExceptionGroup[BaseException]) -> BaseException:
    leaves: list[BaseException] = []
    pending: list[BaseException] = [group]
    while pending:
        item = pending.pop(0)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        else:
            leaves.append(item)
    return next((leaf for leaf in leaves if isinstance(leaf, AuditHalted)), leaves[0])


async def _identify(
    context: _Context, site: WebsiteSnapshot | None, warnings: list[str]
) -> tuple[_Context, list[IdentifiedValue]]:
    notes: list[str] = []
    context.feedback.measurement_running(validation.STEP_COMPANY, validation.COMPANY_SUPPLIER)
    result = await identify.identify_customer(
        context.customer,
        context.location,
        context.keywords,
        site,
        repositories=context.repositories,
        industries=load_industries(context.settings.industries_path),
        gate=context.gate,
        api_key=context.settings.google_api_key,
        notes=notes,
        eur_per_usd=context.settings.google_eur_per_usd,
    )
    warnings.extend(f"identify: {note}" for note in notes)
    context.feedback.measurement_running(
        validation.STEP_COMPANY, validation.COMPANY_SUPPLIER, CHECK_STAGE
    )
    for value in result.identified:
        logger.info("Identified %s from %s: %s", value.field, value.source, value.value)
    updated = dataclasses.replace(
        context,
        customer=result.customer,
        location=result.location,
        keywords=result.keywords,
        legal_notice_business=result.business,
    )
    _record(updated, validation.validate_company(result.business))
    return updated, result.identified


async def _geocoded(context: _Context, warnings: list[str]) -> _Context:
    location = context.location
    if location is None or _centre(context) is not None:
        return context
    if not location.street.strip():
        warnings.append("geocode: location has no street address to look up")
        return context
    try:
        found = await geocode.geocode(location.street, location.postal_code, location.city)
    except CollectionError as error:
        warnings.append(f"geocode: {error}")
        return context
    if found is None:
        warnings.append("geocode: no coordinates found for the address of the location")
        return context
    latitude, longitude = found
    context.repositories.update_location(location.id, latitude=latitude, longitude=longitude)
    logger.info("Location %s geocoded from its address", location.id)
    located = location.model_copy(update={"latitude": latitude, "longitude": longitude})
    return dataclasses.replace(context, location=located)


async def _website(context: _Context, warnings: list[str]) -> _Outcome[WebsiteSnapshot | None]:
    outcome = await _logged(
        context,
        validation.STEP_WEBSITE,
        _optional("website", _crawl, context),
        lambda site: validation.validate_website(site, _require_website(context)),
    )
    warnings.extend(outcome.warnings)
    if outcome.failure is not None and not outcome.restored:
        context.feedback.route_done("website", Result.FAILED, outcome.failure)
    return outcome


def _require_website(context: _Context) -> HttpUrl:
    if context.customer.website is None:
        raise MissingInputError("customer has no website")
    return context.customer.website


async def _crawl(context: _Context, notes: _Notes) -> WebsiteSnapshot | None:
    return await website.crawl_website(
        _require_website(context), browser=legal_notice.BrowserReader(context.browser)
    )


async def _optional[T](
    step: str, action: Callable[[_Context, _Notes], Awaitable[T]], context: _Context
) -> _Outcome[T]:
    stored = context.steps.stored(step)
    if stored is not None and (
        stored.failure is None or (context.only is not None and step not in context.only)
    ):
        return _restored(step, stored)
    notes = _Notes(_stored_value(step, stored.value) if stored is not None else None)
    try:
        value = await action(context, notes)
    except AuditHalted:
        raise
    except CollectionError as error:
        warnings = [*(f"{step}: {note}" for note in notes), f"{step}: {error}"]
        return _kept(context, step, _Outcome(None, warnings, error))
    except Exception as error:
        logger.exception("Route %s failed unexpectedly", step)
        warnings = [*(f"{step}: {note}" for note in notes), f"{step}: {error!r}"]
        return _kept(context, step, _Outcome(None, warnings, error))
    outcome = _Outcome(value, [f"{step}: {note}" for note in notes], notes.failure)
    return _kept(context, step, outcome)


def _restored[T](step: str, stored: StoredStep) -> _Outcome[T]:
    value: T | None = _stored_value(step, stored.value)
    failure = None if stored.failure is None else Plain(stored.failure)
    return _Outcome(value, stored.warnings, failure, True)


def _adapter(step: str) -> TypeAdapter[Any]:
    return TypeAdapter(STEP_TYPES[step] | None)


def _stored_value(step: str, value: object) -> Any:
    return _adapter(step).validate_python(value)


def _kept[T](context: _Context, step: str, outcome: _Outcome[T]) -> _Outcome[T]:
    failure = None if outcome.failure is None else plain_reason(outcome.failure)
    if context.probe:
        context.steps.keep_probe(step, failure)
        return outcome
    value = _adapter(step).dump_python(outcome.value, mode="json")
    inherent = isinstance(
        outcome.failure, (MissingInputError, SourceDisabledError, ProfileNotFoundError)
    )
    context.steps.keep(step, value, outcome.warnings, failure, suspended=inherent)
    return outcome


async def _routed[T](
    context: _Context,
    route: str,
    action: Callable[[_Context, _Notes], Awaitable[T]],
    step: str | None = None,
) -> _Outcome[T]:
    outcome = await _optional(step or route, action, context)
    if not outcome.restored:
        _report(context, route, outcome.failure)
    return outcome


def _report(context: _Context, route: str, failure: str | BaseException | None) -> None:
    if failure is None:
        context.feedback.route_done(route, Result.MEASURED)
    elif isinstance(failure, SourceUnavailableError):
        context.feedback.route_done(route, Result.SUSPENDED, plain_reason(failure))
    else:
        context.feedback.route_done(route, Result.FAILED, failure)


def _record(context: _Context, record: MeasurementRecord) -> None:
    context.records.append(record)
    context.feedback.measurement_done(record)


def _missing_reason(failure: str | BaseException | None) -> str:
    return NOT_RUN_REASON if failure is None else plain_reason(failure)


async def _logged[T](
    context: _Context,
    step: str,
    work: Awaitable[_Outcome[T]],
    build: Callable[[T], MeasurementRecord],
) -> _Outcome[T]:
    supplier = validation.start_supplier(step, context.settings.routes_path)
    context.feedback.measurement_running(step, supplier)
    outcome = await work
    context.feedback.measurement_running(step, supplier, CHECK_STAGE)
    if outcome.value is None:
        record = validation.not_delivered(step, supplier, _missing_reason(outcome.failure))
    else:
        record = build(outcome.value)
    _record(context, record)
    return outcome


def _profile_ways(settings: Settings) -> tuple[DataRoute, ...]:
    suppliers = load_measurements(settings.routes_path)[PLACES_MEASUREMENT].routes
    names = [item.supplier for item in suppliers]
    unknown = [name for name in names if name not in PROFILE_WAYS]
    if unknown:
        raise ConfigurationError(
            f"measurement {PLACES_MEASUREMENT} names routes the worker cannot run: {unknown}"
        )
    return tuple(way for name in names for way in PROFILE_WAYS if way == name)


async def _pagespeed(context: _Context, notes: list[str]) -> PageSpeedSnapshot:
    return await pagespeed.measure(_require_website(context), context.settings.google_api_key)


async def _measured_speed(context: _Context) -> _Outcome[PageSpeedSnapshot]:
    return await _logged(
        context,
        validation.STEP_PAGESPEED,
        _routed(context, "pagespeed", _pagespeed),
        lambda snapshot: validation.validate_pagespeed(snapshot, _require_website(context)),
    )


async def _measured_answers(context: _Context) -> _Outcome[list[AssistantAnswer]]:
    return await _logged(
        context,
        validation.STEP_ASSISTANTS,
        _optional("assistants", _assistants, context),
        lambda answers: validation.validate_assistants(answers, _questions(context)),
    )


async def _google_profiles(context: _Context) -> _ProfileOutcomes:
    prefetch_notes = await _prefetch_maps(context)
    place, place_valid = await _measured_place(context)
    missing = _profile_missing(place) and place_valid
    own = _business(context, place.value if place_valid else None)

    async def competitors(inner: _Context, notes: list[str]) -> list[PlaceSnapshot]:
        return await _competitors(inner, notes, own)

    async def rankings(inner: _Context, notes: _Notes) -> list[MapRanking]:
        return await _map_rankings(inner, notes, own)

    async def results(inner: _Context, notes: list[str]) -> MapResultList:
        return await _map_results(inner, notes, own)

    async def grid(inner: _Context, notes: list[str]) -> list[GridPoint]:
        return await _grid(inner, notes, own)

    rivals = await _optional("competitors", competitors, context)
    map_rankings = await _optional("map_rankings", rankings, context)
    outcomes = _ProfileOutcomes(
        place,
        rivals,
        dataclasses.replace(map_rankings, warnings=[*prefetch_notes, *map_rankings.warnings]),
        await _optional("map_results", results, context),
        await _optional("grid", grid, context),
        missing,
        place_valid,
    )
    parts = (place, outcomes.competitors, map_rankings, outcomes.map_results, outcomes.grid)
    if not all(part.restored for part in parts):
        _report(context, "google_maps", _map_failure(outcomes))
    return outcomes


def _profile_missing(place: _Outcome[PlaceSnapshot]) -> bool:
    return place.value is None and any(
        warning.startswith(f"places: {NO_PROFILE_FOUND}") for warning in place.warnings
    )


def _place_record(
    context: _Context, place: _Outcome[PlaceSnapshot], supplier: str
) -> MeasurementRecord:
    if place.value is None and not _profile_missing(place):
        reason = _missing_reason(place.failure)
        return validation.not_delivered(validation.STEP_PLACE, supplier, reason)
    read = place.value
    found: list[str] = list(dict.fromkeys(read.value_routes.values())) if read else []
    routes = found or ([DEFAULT_PLACE_ROUTE] if read else [])
    return validation.validate_place(
        read,
        _business_facts(context),
        validation.route_text(routes),
        none_found=place.value is None,
    )


async def _measured_place(context: _Context) -> tuple[_Outcome[PlaceSnapshot], bool]:
    supplier = validation.start_supplier(validation.STEP_PLACE, context.settings.routes_path)
    context.feedback.measurement_running(validation.STEP_PLACE, supplier)
    place = await _optional("places", _own_place, context)
    context.feedback.measurement_running(validation.STEP_PLACE, supplier, CHECK_STAGE)
    record = _place_record(context, place, supplier)
    _record(context, record)
    return place, record.valid


async def _prefetch_maps(context: _Context) -> list[str]:
    wanted = ("map_rankings", "map_results", "grid", "competitors")
    if (
        all(context.steps.finished(step) for step in wanted)
        or context.only is not None
        and not (context.only & set(wanted))
    ):
        return []
    centre = _centre(context)
    keyword = _primary_keyword(context.keywords)
    if centre is None or keyword is None:
        return []
    phrases = list(dict.fromkeys(keyword.search_phrase for keyword in context.keywords))
    notes: list[str] = []
    try:
        size = _grid_size(context.settings)
        if context.probe:
            phrases, grid = phrases[:PROBE_ITEMS], None
        else:
            grid = (keyword.search_phrase, size)
        await context.profiles.prefetch(phrases, centre, grid, notes)
    except CollectionError as error:
        notes.append(f"DataForSEO Maps not asked: {error}")
    return [f"map_rankings: {note}" for note in notes]


def _map_failure(profiles: _ProfileOutcomes) -> str | BaseException | None:
    parts = (
        None if profiles.missing else profiles.place,
        profiles.competitors,
        profiles.map_rankings,
        profiles.map_results,
        profiles.grid,
    )
    return next((part.failure for part in parts if part is not None and part.failure), None)


def _business(context: _Context, own_place: PlaceSnapshot | None) -> Business:
    location = context.location
    ids = {own_place.place_id} if own_place is not None else set()
    if location is not None and location.google_place_id:
        ids.add(location.google_place_id)
    return Business(
        name=own_place.name if own_place is not None else context.customer.name,
        address=_address(location) if location is not None else context.customer.city,
        city=context.customer.city,
        place_ids=frozenset(ids),
    )


def _address(location: Location) -> str:
    return f"{location.street}, {location.postal_code} {location.city}"


async def _own_place(context: _Context, notes: list[str]) -> PlaceSnapshot:
    location = _require_location(context)
    target = ProfileTarget(
        location.name, _address(location), location.google_place_id, with_photos=True
    )
    place = await context.profiles.profile(target, notes)
    if place is None:
        raise ProfileNotFoundError(f"{NO_PROFILE_FOUND} for {location.name}")
    return place


async def _competitors(context: _Context, notes: list[str], own: Business) -> list[PlaceSnapshot]:
    found: list[PlaceSnapshot] = []
    try:
        for competitor in context.competitors:
            await _add_competitor(context, notes, competitor, found)
        if len(found) < COMPETITOR_COUNT:
            found.extend(await _ranked_competitors(context, notes, own, found))
    except SourceUnavailableError as error:
        notes.append(str(error))
    return found


async def _add_competitor(
    context: _Context, notes: list[str], competitor: Competitor, found: list[PlaceSnapshot]
) -> None:
    target = ProfileTarget(competitor.name, context.customer.city, competitor.google_place_id)
    try:
        place = await context.profiles.profile(target, notes)
    except SourceUnavailableError:
        raise
    except CollectionError as error:
        notes.append(f"{competitor.name}: {error}")
        return
    if place is None:
        notes.append(f"{competitor.name} not found on Google")
        return
    found.append(place)


async def _ranked_competitors(
    context: _Context, notes: list[str], own: Business, known: list[PlaceSnapshot]
) -> list[PlaceSnapshot]:
    keyword = _primary_keyword(context.keywords)
    centre = _centre(context)
    if keyword is None or centre is None:
        notes.append("no primary keyword or location coordinates to find ranked competitors")
        return []
    return await context.profiles.competitors(
        keyword.search_phrase, centre, known, own, COMPETITOR_COUNT - len(known), notes
    )


async def _map_rankings(context: _Context, notes: _Notes, own: Business) -> list[MapRanking]:
    phrases = list(dict.fromkeys(keyword.search_phrase for keyword in context.keywords))
    if not phrases:
        raise MissingInputError("customer has no keywords")
    centre = _require_centre(context)
    known = {item.phrase: item for item in _previous(notes, MapRanking)}
    if context.probe:
        phrases = [phrase for phrase in phrases if phrase not in known][:PROBE_ITEMS]
    return [
        known.get(phrase) or await context.profiles.map_ranking(phrase, centre, own, notes)
        for phrase in phrases
    ]


async def _map_results(context: _Context, notes: list[str], own: Business) -> MapResultList:
    keyword = _primary_keyword(context.keywords)
    if keyword is None:
        raise MissingInputError("customer has no keywords")
    centre = _require_centre(context)
    return await context.profiles.result_list(keyword.search_phrase, centre, own, notes)


async def _grid(context: _Context, notes: list[str], own: Business) -> list[GridPoint]:
    keyword = _primary_keyword(context.keywords)
    if keyword is None:
        raise MissingInputError("customer has no keywords")
    centre = _require_centre(context)
    size = _grid_size(context.settings)
    return await context.profiles.grid(keyword.search_phrase, centre, own, size, notes)


def _grid_size(settings: Settings) -> int:
    try:
        catalogue = load_catalogue(settings.catalogue_path)
    except ScoringError as error:
        raise CollectionError(f"grid size unreadable: {error}") from error
    for area in catalogue.areas:
        for check in area.checks:
            size = check.thresholds.get("grid")
            if check.id == GRID_CHECK_ID and isinstance(size, int):
                return size
    raise CollectionError(f"catalogue check {GRID_CHECK_ID} has no integer threshold grid")


def _centre(context: _Context) -> tuple[float, float] | None:
    location = context.location
    if location is None or location.latitude is None or location.longitude is None:
        return None
    return location.latitude, location.longitude


def _require_centre(context: _Context) -> tuple[float, float]:
    location = _require_location(context)
    if location.latitude is None or location.longitude is None:
        raise MissingInputError("location has no coordinates for the map searches")
    return location.latitude, location.longitude


async def _searches(context: _Context) -> _SearchOutcomes:
    state = _GoogleState()

    async def google(inner: _Context, notes: _Notes) -> list[SerpResult]:
        return await _google_searches(inner, notes, state)

    async def images(inner: _Context, notes: _Notes) -> list[SerpResult]:
        return await _image_searches(inner, notes, state)

    google_outcome = await _logged(
        context,
        validation.STEP_GOOGLE_SEARCH,
        _optional("serps", google, context),
        lambda results: _search_record(
            validation.STEP_GOOGLE_SEARCH,
            results,
            _google_phrases(context),
            _expected_location_code(context),
        ),
    )
    _report(context, "google_search", google_outcome.failure)
    bing = await _logged(
        context,
        validation.STEP_BING_SEARCH,
        _routed(context, "bing", _bing_searches),
        lambda results: _search_record(
            validation.STEP_BING_SEARCH, results, _main_phrases(context)
        ),
    )
    images_outcome = await _logged(
        context,
        validation.STEP_IMAGE_SEARCH,
        _routed(context, "google_images", images, "images"),
        lambda results: _search_record(
            validation.STEP_IMAGE_SEARCH, results, _main_phrases(context)
        ),
    )
    return _SearchOutcomes(google_outcome, bing, images_outcome, state.deferred)


def _search_record(
    step: str, results: list[SerpResult], phrases: list[str], expected_location: int | None = None
) -> MeasurementRecord:
    supplier = validation.route_text(result.route for result in results)
    return validation.validate_search(
        step, supplier, results, phrases, expected_location_code=expected_location
    )


def _expected_location_code(context: _Context) -> int | None:
    latitude, _ = _coordinates(context)
    return None if latitude is not None else paid_rankings.GERMANY_LOCATION_CODE


def _google_phrases(context: _Context) -> list[str]:
    phrases = [keyword.search_phrase for keyword in context.keywords]
    brand = context.customer.name.strip()
    if brand and brand.casefold() not in {phrase.casefold() for phrase in phrases}:
        phrases.append(brand)
    return phrases


async def _google_searches(
    context: _Context, notes: _Notes, state: _GoogleState
) -> list[SerpResult]:
    phrases = _google_phrases(context)
    if not phrases:
        raise MissingInputError("customer has no keywords and no name")
    measured, left = _resume(notes, phrases, context.probe)
    if left and context.paid_active:
        paid = await paid_rankings.search_google(context.dataforseo(), left, *_coordinates(context))
        _note_paid(notes, "DataForSEO", paid, len(left))
        measured, left = [*measured, *paid.results], paid.failed
    if left:
        apify = await _apify_web(context, left)
        _note_apify_failures(notes, apify, len(left))
        measured, left = [*measured, *apify.results], apify.failed
    if not left:
        return measured
    batch = await serp.search_all(context.browser, _granted_google_phrases(context, left, notes))
    if batch.deferred:
        state.blocked_reason = batch.reason or "Google blocked the browser"
        notes.failure = state.blocked_reason
    _defer(batch, "web", state.deferred, notes)
    return [*measured, *batch.results]


def _resume(notes: _Notes, phrases: list[str], probe: bool) -> tuple[list[SerpResult], list[str]]:
    measured = [result for result in _previous(notes, SerpResult) if result.phrase in phrases]
    done = {result.phrase for result in measured}
    left = [phrase for phrase in phrases if phrase not in done]
    return measured, left[:PROBE_ITEMS] if probe else left


def _previous[T](notes: _Notes, kind: type[T]) -> list[T]:
    previous = notes.previous
    if not isinstance(previous, list):
        return []
    return [item for item in previous if isinstance(item, kind)]


def _coordinates(context: _Context) -> tuple[float | None, float | None]:
    location = context.location
    if location is None:
        return None, None
    return location.latitude, location.longitude


def _note_paid(notes: list[str], service: str, batch: PaidSearchBatch, total: int) -> None:
    if batch.repeated:
        notes.append(f"{service}: {batch.repeated} failed searches repeated once at once")
    if batch.failed and batch.reason is not None:
        notes.append(
            f"{service} did not answer {len(batch.failed)} of {total} searches, the next route "
            f"takes them: {batch.reason}"
        )


async def _apify_web(context: _Context, phrases: list[str]) -> PaidSearchBatch:
    if not phrases or not apify_serp.is_available(context.settings, context.gate):
        return PaidSearchBatch([], phrases)
    ad_phrases = apify_serp.brand_phrases(phrases, context.customer.name)
    return await apify_serp.search_web(context.settings, context.gate, phrases, ad_phrases)


def _note_apify_failures(notes: list[str], paid: PaidSearchBatch, total: int) -> None:
    if paid.failed and paid.reason is not None:
        notes.append(
            f"Apify did not answer {len(paid.failed)} of {total} Google searches: {paid.reason}"
        )


def _granted_google_phrases(context: _Context, phrases: list[str], notes: _Notes) -> list[str]:
    granted = context.gate.acquire_up_to(Source.GOOGLE_SEARCH, len(phrases))
    if granted < len(phrases):
        notes.append(f"daily cap reached, {len(phrases) - granted} of {len(phrases)} not searched")
        notes.failure = DailyCapReachedError(Source.GOOGLE_SEARCH.value, "daily cap reached")
    return phrases[:granted]


def _defer(
    batch: serp.SearchBatch,
    vertical: Literal["web", "images"],
    deferred: list[DeferredSearch],
    notes: list[str],
) -> None:
    if not batch.deferred:
        return
    deferred.extend(DeferredSearch(phrase=phrase, vertical=vertical) for phrase in batch.deferred)
    notes.append(serp.deferral_note(len(batch.deferred), batch.reason))


async def _bing_searches(context: _Context, notes: _Notes) -> list[SerpResult]:
    phrases = _main_phrases(context)
    measured, left = _resume(notes, phrases, context.probe)
    if not left:
        return measured
    batch = await serp.search_bing(context.browser, left)
    if batch.deferred:
        notes.append(f"{len(batch.deferred)} of {len(phrases)} not searched: {batch.reason}")
        notes.failure = batch.reason or Plain("Die Suche wurde nicht ausgeführt")
    return [*measured, *batch.results]


async def _image_searches(
    context: _Context, notes: _Notes, state: _GoogleState
) -> list[SerpResult]:
    measured, phrases = _resume(notes, _main_phrases(context), context.probe)
    if not phrases:
        return measured
    paid = PaidSearchBatch([], phrases)
    if apify_serp.is_available(context.settings, context.gate):
        paid = await apify_serp.search_images(context.settings, context.gate, phrases)
        _note_apify_failures(notes, paid, len(phrases))
    paid = PaidSearchBatch([*measured, *paid.results], paid.failed, paid.reason)
    if not paid.failed:
        return paid.results
    if state.blocked_reason is not None:
        blocked = serp.SearchBatch([], paid.failed, "Google blocked the web searches before")
        _defer(blocked, "images", state.deferred, notes)
        notes.failure = state.blocked_reason
        return paid.results
    batch = await serp.search_images(
        context.browser, _granted_google_phrases(context, paid.failed, notes)
    )
    _defer(batch, "images", state.deferred, notes)
    return [*paid.results, *batch.results]


def _main_phrases(context: _Context) -> list[str]:
    ranked = sorted(
        context.keywords,
        key=lambda keyword: (not keyword.is_primary, -(keyword.monthly_volume or 0)),
    )
    phrases = [keyword.search_phrase for keyword in ranked[:MAIN_PHRASE_COUNT]]
    if not phrases:
        raise MissingInputError("customer has no keywords")
    brand = context.customer.name.strip()
    if brand and brand.casefold() not in {phrase.casefold() for phrase in phrases}:
        phrases.insert(0, brand)
    return phrases


def _listing_target(context: _Context) -> listings.Target:
    customer, location = context.customer, context.location
    group = (
        load_industries(context.settings.industries_path).resolve(customer.industry)
        if customer.industry
        else None
    )
    return listings.Target(
        name=customer.name.strip(),
        city=(location.city if location is not None and location.city else customer.city),
        street=location.street if location is not None else None,
        postal_code=location.postal_code if location is not None else None,
        phone=location.phone if location is not None else None,
        website=str(customer.website) if customer.website is not None else None,
        industry=group.id if group is not None else None,
    )


def _listing_outcome(status: listings.RouteStatus) -> RouteOutcome:
    if status.ok:
        return RouteOutcome(status.route, Result.MEASURED)
    if status.ok is None:
        return RouteOutcome(status.route, Result.SUSPENDED, status.reason)
    return RouteOutcome(status.route, Result.FAILED, status.reason)


async def _measured_listings(context: _Context) -> _Outcome[list[DirectoryHit]]:
    return await _logged(
        context,
        validation.STEP_LISTINGS,
        _optional("listings", _map_listings, context),
        validation.validate_listings,
    )


async def _map_listings(context: _Context, notes: _Notes) -> list[DirectoryHit]:
    result = await map_listings.collect_map_listings(
        context.settings, context.gate, _listing_target(context)
    )
    notes.extend(
        f"{status.route}: {status.reason}" for status in result.routes if status.reason is not None
    )
    context.feedback.routes_done([_listing_outcome(status) for status in result.routes])
    return result.hits


async def _assistants(context: _Context, notes: _Notes) -> list[AssistantAnswer]:
    earlier = _previous(notes, AssistantAnswer)
    answered = frozenset(answer.provider for answer in earlier)
    providers = [
        name
        for name in assistants.enabled_providers(context.settings, context.gate)
        if name not in answered
    ]
    if not providers:
        return earlier
    questions = _questions(context)
    limits: dict[str, Any] = {}
    if answered:
        limits["skip"] = answered
    if context.probe:
        questions = questions[:PROBE_ITEMS]
        limits["repeats"] = PROBE_ITEMS
    profiles_root = context.settings.browser_profile_dir / ASSISTANT_PROFILES
    channel = context.settings.browser_channel
    handoff = (
        assistants.Handoff(
            browser_for=lambda provider: BrowserService(
                profiles_root / f"{provider}{PERSON_PROFILE_SUFFIX}",
                headless=False,
                channel=channel,
            ),
            notify=lambda text: _tell(context, text),
            manual=context.manual,
        )
        if context.settings.assistant_handoff
        else None
    )
    reasons: dict[str, str] = {}
    try:
        answers = await assistants.ask_all(
            questions,
            context.customer.name,
            context.settings,
            context.gate,
            context.browser,
            browser_for=_assistant_browser_for(context),
            timings=context.timings,
            handoff=handoff,
            reasons=reasons,
            domain=host_of(context.customer.website),
            **limits,
        )
    except AuditHalted:
        raise
    except CollectionError as error:
        failed = [
            RouteOutcome(f"{ASSISTANT_ROUTE}{name}", Result.FAILED, reasons.get(name, str(error)))
            for name in providers
        ]
        context.feedback.routes_done(failed)
        notes.append(str(error))
        notes.failure = error
        return earlier
    replied = {answer.provider for answer in answers}
    silent = [provider for provider in providers if provider not in replied]
    if silent:
        notes.append(f"no answers from {', '.join(silent)}")
        notes.failure = Plain(f"Keine Antworten von {', '.join(silent)}")
    context.feedback.routes_done(_assistant_outcomes(providers, answers, reasons))
    return [*earlier, *answers]


def _assistant_outcomes(
    providers: list[str], answers: list[AssistantAnswer], reasons: dict[str, str]
) -> list[RouteOutcome]:
    outcomes: list[RouteOutcome] = []
    for name in providers:
        route = f"{ASSISTANT_ROUTE}{name}"
        own = [answer for answer in answers if answer.provider == name]
        if not own:
            outcomes.append(RouteOutcome(route, Result.FAILED, reasons.get(name, NO_ANSWER)))
        elif any(answer.route == "manual" for answer in own):
            outcomes.append(RouteOutcome(route, Result.WITH_PERSON, ASKED_WITH_PERSON))
        else:
            outcomes.append(RouteOutcome(route, Result.MEASURED))
    return outcomes


def _assistant_browser_for(context: _Context) -> Callable[[str], BrowserService]:
    profiles_root = context.settings.browser_profile_dir / ASSISTANT_PROFILES
    channel = context.settings.browser_channel
    return lambda name: BrowserService(profiles_root / name, channel=channel)


def _questions(context: _Context) -> list[str]:
    customer = context.customer
    primary = next((keyword.phrase for keyword in context.keywords if keyword.is_primary), "")
    keyword_trade = assistants.trade_from_keyword(primary, customer.city)
    if keyword_trade is not None and keyword_trade.casefold() in customer.name.casefold():
        keyword_trade = None
    trade = keyword_trade or load_industries(context.settings.industries_path).question_term(
        customer.industry
    )
    if not trade:
        raise CollectionError("neither keywords nor industry name the trade to ask about")
    services = list(
        dict.fromkeys(
            keyword.phrase
            for keyword in sorted(context.keywords, key=lambda item: not item.is_primary)
            if keyword.phrase.lower() not in {trade.lower(), customer.industry.lower()}
        )
    )
    templates = assistants.load_question_templates(context.settings.questions_path)
    return assistants.build_questions(trade, customer.city, services, templates)


async def _customer_access(context: _Context) -> _Outcome[AccessSnapshot]:
    if context.kind is not AuditKind.WITH_ACCESS:
        return _Outcome(None, [])
    return await _routed(context, "access", _read_access)


async def _read_access(context: _Context, notes: list[str]) -> AccessSnapshot:
    record = context.repositories.customer_access(context.customer.id)
    if record is None:
        raise MissingInputError("customer has recorded no access")
    token = await google_auth.access_token(context.repositories, context.settings)
    return await access.collect_access(token, record, notes)


async def _paid_outcome(
    context: _Context, rivals: list[PlaceSnapshot], own_place: PlaceSnapshot | None
) -> _Outcome[PaidSnapshot]:
    if not context.paid_switched_on:
        context.feedback.route_done("dataforseo", Result.SUSPENDED, PAID_SWITCHED_OFF)
        _record(
            context,
            validation.not_delivered(
                validation.STEP_LINKS,
                validation.start_supplier(validation.STEP_LINKS, context.settings.routes_path),
                PAID_SWITCHED_OFF,
            ),
        )
        return _Outcome(None, [])

    async def measure(inner: _Context, notes: list[str]) -> PaidSnapshot:
        return await _paid(inner, notes, rivals, own_place)

    own_host = paid_data.domain_of(_own_site(context, own_place))
    outcome = await _logged(
        context,
        validation.STEP_LINKS,
        _optional("paid", measure, context),
        lambda snapshot: validation.validate_links(snapshot, own_host),
    )
    if outcome.failure is not None and not paid_rankings.is_configured(context.settings):
        context.feedback.route_done("dataforseo", Result.SUSPENDED, PAID_WITHOUT_CREDENTIALS)
    else:
        _report(context, "dataforseo", outcome.failure)
    return outcome


async def _paid(
    context: _Context,
    notes: list[str],
    rivals: list[PlaceSnapshot],
    own_place: PlaceSnapshot | None,
) -> PaidSnapshot:
    client = paid_rankings.DataForSeoClient(context.settings, context.gate)
    phrases = {keyword.id: keyword.search_phrase for keyword in context.keywords}
    request = paid_data.PaidRequest(
        customer_domain=paid_data.domain_of(_own_site(context, own_place)),
        competitor_domains=_competitor_domains(context, rivals),
        phrases=list(dict.fromkeys(phrases.values())),
        primary_phrases=[phrases[keyword.id] for keyword in context.keywords if keyword.is_primary],
        city=context.customer.city,
    )
    snapshot = await paid_data.collect_paid(client, request, notes)
    for keyword in context.keywords:
        volume = snapshot.volumes.get(phrases[keyword.id])
        if volume is None or volume == keyword.monthly_volume:
            continue
        try:
            context.repositories.update_keyword_volume(keyword.id, volume)
        except StorageError as error:
            notes.append(f"volume of {keyword.phrase} not saved: {error}")
    return snapshot


async def _measured_google_ads(context: _Context) -> _Outcome[GoogleAdsSnapshot]:
    domain = paid_data.domain_of(_own_site(context, None))
    if not context.paid_switched_on:
        supplier = validation.start_supplier(
            validation.STEP_ADS_GOOGLE, context.settings.routes_path
        )
        _record(
            context,
            validation.not_delivered(validation.STEP_ADS_GOOGLE, supplier, PAID_SWITCHED_OFF),
        )
        context.feedback.route_done("google_ads", Result.SUSPENDED, PAID_SWITCHED_OFF)
        return _Outcome(None, [])
    if ads.load_google_ads_price(context.settings.routes_path) is None:
        supplier = validation.start_supplier(
            validation.STEP_ADS_GOOGLE, context.settings.routes_path
        )
        _record(
            context,
            validation.not_delivered(validation.STEP_ADS_GOOGLE, supplier, NO_PRICE_REASON),
        )
        context.feedback.route_done("google_ads", Result.SUSPENDED, NO_PRICE_REASON)
        return _Outcome(None, [])

    async def measure(inner: _Context, notes: list[str]) -> GoogleAdsSnapshot:
        if domain is None:
            raise MissingInputError("customer has no website")
        return await ads.collect_google_ads(inner.dataforseo(), domain, datetime.now(UTC))

    outcome = await _logged(
        context,
        validation.STEP_ADS_GOOGLE,
        _optional("ads_google", measure, context),
        lambda snapshot: validation.validate_google_ads(snapshot, domain),
    )
    if outcome.failure is not None and not paid_rankings.is_configured(context.settings):
        context.feedback.route_done("google_ads", Result.SUSPENDED, PAID_WITHOUT_CREDENTIALS)
    elif not outcome.restored:
        _report(context, "google_ads", outcome.failure)
    return outcome


async def _measured_meta_ads(context: _Context) -> _Outcome[MetaAdsSnapshot]:
    unavailable = map_listings.unavailable_reason(context.settings, context.gate)
    if unavailable is not None:
        supplier = validation.start_supplier(validation.STEP_ADS_META, context.settings.routes_path)
        _record(context, validation.not_delivered(validation.STEP_ADS_META, supplier, unavailable))
        context.feedback.route_done("meta_ads", Result.SUSPENDED, unavailable)
        return _Outcome(None, [])
    meta_route = ads.load_meta_route(context.settings.routes_path)
    held_back = (
        Plain(validation.META_ADS_FORMAT_REASON)
        if not meta_route.confirmed
        else NO_PRICE_REASON
        if meta_route.price_usd is None
        else None
    )
    if held_back is not None:
        supplier = validation.start_supplier(validation.STEP_ADS_META, context.settings.routes_path)
        _record(context, validation.not_delivered(validation.STEP_ADS_META, supplier, held_back))
        context.feedback.route_done("meta_ads", Result.SUSPENDED, held_back)
        return _Outcome(None, [])

    async def measure(inner: _Context, notes: list[str]) -> MetaAdsSnapshot:
        name = (_business_facts(inner).name or inner.customer.name).strip()
        if not name:
            raise MissingInputError("the business has no name to search for")
        rivals = [competitor.name for competitor in inner.competitors][:COMPETITOR_COUNT]
        route = ads.load_meta_route(inner.settings.routes_path)
        return await ads.collect_meta_ads(
            inner.settings, inner.gate, route, name, rivals, datetime.now(UTC)
        )

    outcome = await _logged(
        context,
        validation.STEP_ADS_META,
        _optional("ads_meta", measure, context),
        validation.validate_meta_ads,
    )
    if not outcome.restored:
        _report(context, "meta_ads", outcome.failure)
    return outcome


def _own_site(context: _Context, own_place: PlaceSnapshot | None) -> str | None:
    site = context.customer.website or (own_place.website if own_place else None)
    return str(site) if site else None


def _competitor_domains(context: _Context, rivals: list[PlaceSnapshot]) -> list[str]:
    sites = [str(item.website) for item in context.competitors if item.website is not None]
    sites.extend(str(place.website) for place in rivals if place.website is not None)
    domains = [domain for site in sites if (domain := paid_data.domain_of(site)) is not None]
    return list(dict.fromkeys(domains))[:COMPETITOR_COUNT]


def _require_location(context: _Context) -> Location:
    if context.location is None:
        raise MissingInputError("customer has no location")
    return context.location


def _primary_keyword(keywords: list[Keyword]) -> Keyword | None:
    return next((keyword for keyword in keywords if keyword.is_primary), None) or (
        keywords[0] if keywords else None
    )
