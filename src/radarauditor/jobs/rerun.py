import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from radarauditor.collectors import (
    ads,
    chatgpt_scraper,
    dataforseo_maps,
    estimate,
    google_ai_mode,
    listings,
    paid_data,
    paid_rankings,
)
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.steps import REMOVED_STEPS, StepStore
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source
from radarauditor.db.models import AuditStatus
from radarauditor.db.repositories import Repositories
from radarauditor.errors import RadarError
from radarauditor.feedback.events import Result
from radarauditor.feedback.hub import Feedback
from radarauditor.jobs.repair import step_label

logger = logging.getLogger(__name__)

CONTROL_STEP = "control"
MAX_PAID_RERUNS = 3
STEP_ALIASES = {"map_listings": "listings"}
RESTART_LABEL = "Audit-Agent startet den Teil neu"
CREDIT_LABEL = "Audit-Agent: Guthaben reicht nicht"
MAPS = (
    estimate.task_post(dataforseo_maps.MAPS_TASKS),
    estimate.task_post(dataforseo_maps.INFO_TASKS),
    estimate.task_post(dataforseo_maps.REVIEWS_TASKS),
    estimate.task_post(dataforseo_maps.QUESTIONS_TASKS),
)
STEP_SERVICES: dict[str, tuple[str, ...]] = {
    "serps": (estimate.task_post(paid_rankings.ORGANIC_TASKS),),
    "places": MAPS,
    "competitors": MAPS,
    "map_rankings": MAPS,
    "map_results": MAPS,
    "grid": MAPS,
    "directories": (estimate.task_post(paid_rankings.ORGANIC_TASKS),),
    "images": ("google_images",),
    "social": ("instagram", "facebook", "linkedin", "x", "google_search"),
    "listings": (listings.APPLE_SERVICE, listings.BING_SERVICE, listings.SEARCH_SERVICE),
    "assistants": (google_ai_mode.POST_PATH, estimate.task_post(chatgpt_scraper.SCRAPER_TASKS)),
    "paid": (
        paid_rankings.VOLUME_PATH,
        paid_data.SUMMARY_PATH,
        paid_data.REFERRING_DOMAINS_PATH,
        paid_data.RANKED_KEYWORDS_PATH,
        paid_data.INTERSECTION_PATH,
        paid_data.HISTORY_PATH,
    ),
    "ads_google": (ads.GOOGLE_ADS_POST_PATH,),
    "ads_meta": (ads.META_SERVICE,),
}


@dataclass(frozen=True)
class RerunResult:
    measured: bool
    message: str


async def rerun_step(
    audit_id: UUID,
    step: str,
    change: str,
    gate: SourceGate,
    repositories: Repositories,
    settings: Settings,
) -> RerunResult:
    from radarauditor.collectors.run import STEP_TYPES, collect_audit

    step = STEP_ALIASES.get(step, step)
    if step in REMOVED_STEPS:
        raise RadarError(
            f"step {step} was removed from the audit and is not measured any more; "
            f"known steps: {', '.join([*STEP_TYPES, CONTROL_STEP])}"
        )
    if step == CONTROL_STEP:
        return _rerun_control(audit_id, repositories)
    if step not in STEP_TYPES:
        raise RadarError(
            f"unknown step {step}; known steps: {', '.join([*STEP_TYPES, CONTROL_STEP])}"
        )
    store = StepStore(audit_id, repositories)
    stored = store.stored(step)
    if stored is not None and stored.failure is None:
        return RerunResult(True, f"{step_label(step)} ist bereits gemessen")
    paid = step in STEP_SERVICES
    if paid and not change.strip():
        raise RadarError(
            "Ein bezahlter Neuversuch braucht unter --change, was an der Ursache geändert wurde"
        )
    label = step_label(step)
    if paid and stored is not None and stored.attempts > MAX_PAID_RERUNS:
        return RerunResult(
            False,
            f"{label}: Obergrenze von {MAX_PAID_RERUNS} bezahlten Neuversuchen erreicht, "
            "kein weiterer bezahlter Abruf",
        )
    if paid:
        shortfall = await _shortfall(audit_id, step, repositories, settings)
        if shortfall is not None:
            store.mark_credit_short(step, shortfall)
            repositories.add_repair_event(
                audit_id, Result.WARNING, CREDIT_LABEL, f"{label}: {shortfall}", step
            )
            return RerunResult(False, shortfall)
    repositories.add_repair_event(
        audit_id, Result.RESTARTED, RESTART_LABEL, f"{label}: {change}", step
    )
    customer = repositories.customer(repositories.audit(audit_id).customer_id)
    quiet = Feedback.quiet(audit_id, customer.name, settings)
    unattended = settings.model_copy(update={"assistant_handoff": False, "halt_stages": ()})
    only = frozenset({step})
    await collect_audit(
        audit_id, repositories, unattended, gate, quiet, only=only, probe=True, steps=store
    )
    probe = store.probe_failure(step)
    if probe is not None:
        return RerunResult(False, f"Probeabruf gescheitert: {probe}")
    await collect_audit(audit_id, repositories, unattended, gate, only=only, steps=store)
    failure = store.failed().get(step)
    if failure is not None:
        return RerunResult(False, f"{label} scheitert weiter: {failure}")
    return RerunResult(True, f"{label} gemessen")


def _rerun_control(audit_id: UUID, repositories: Repositories) -> RerunResult:
    repositories.reopen_control(audit_id)
    repositories.set_audit_status(audit_id, AuditStatus.CONTROLLING)
    repositories.add_repair_event(
        audit_id, Result.RESTARTED, RESTART_LABEL, "Kontrolle startet neu", CONTROL_STEP
    )
    return RerunResult(True, "Kontrolle neu gestartet")


async def _shortfall(
    audit_id: UUID, step: str, repositories: Repositories, settings: Settings
) -> str | None:
    services = STEP_SERVICES[step]
    planned = [line for line in repositories.estimates(audit_id) if line.service in services]
    balances = await estimate.read_balances(settings)
    credit = {
        Source.DATAFORSEO.value: _euro(balances.dataforseo_usd, settings.dataforseo_eur_per_usd),
        Source.APIFY.value: _euro(balances.apify_usd, settings.apify_eur_per_usd),
    }
    needed: dict[str, Decimal] = {}
    for line in planned:
        needed[line.source] = needed.get(line.source, Decimal(0)) + line.cost
    gaps = [
        f"{source}: nötig {_money(cost)}, Guthaben {_money(available)}, es fehlen "
        f"{_money(cost - available)}"
        for source, cost in needed.items()
        if (available := credit.get(source)) is not None and cost > available
    ]
    if gaps:
        return "; ".join(gaps)
    if balances.unreadable and any(credit.get(source) is None for source in needed):
        return "Guthaben nicht lesbar: " + "; ".join(balances.unreadable)
    return None


def _euro(usd: Decimal | None, rate: Decimal) -> Decimal | None:
    return None if usd is None else usd * rate


def _money(amount: Decimal) -> str:
    return f"{amount:.4f} EUR".replace(".", ",")
