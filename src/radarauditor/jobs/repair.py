import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from radarauditor.collectors.steps import StepStore
from radarauditor.config import Settings
from radarauditor.db.repositories import Repositories
from radarauditor.errors import ClaudeCodeError, RadarError
from radarauditor.feedback.events import Result
from radarauditor.jobs.analysis import retry_at

logger = logging.getLogger(__name__)

WARNING_EVERY = 5
NON_BLOCKING_STEPS = frozenset({"ads_google", "ads_meta"})
CREDIT_WAIT = timedelta(hours=1)
TAKEOVER_LABEL = "Audit-Agent übernimmt"
DONE_LABEL = "Audit-Agent: Teil erledigt"
WARNING_LABEL = "Audit-Agent: Teil scheitert weiter"
STEP_LABELS = {
    "website": "Website",
    "pagespeed": "PageSpeed",
    "places": "Google-Profil",
    "competitors": "Wettbewerber auf Google Maps",
    "map_rankings": "Kartenplätze je Suchbegriff",
    "map_results": "Kartenliste",
    "grid": "Kartenraster",
    "serps": "Google-Suche",
    "bing": "Bing-Suche",
    "images": "Google-Bildersuche",
    "directories": "Verzeichnisse",
    "listings": "Portale und Karten",
    "social": "Soziale Netzwerke",
    "assistants": "KI-Assistenten",
    "access": "Kundenzugang",
    "wayback": "Alter der Domain",
    "paid": "DataForSEO-Kennzahlen",
    "ads_google": "Google-Werbeanzeigen",
    "ads_meta": "Meta-Werbeanzeigen",
    "control": "Kontrolle",
}

type RepairLauncher = Callable[[UUID, str, str, Settings], Awaitable[None]]
type Clock = Callable[[], datetime]


@dataclass(frozen=True)
class RepairOutcome:
    done: bool
    run_after: datetime | None = None


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


def blocking_failures(store: StepStore) -> dict[str, str]:
    return {
        step: reason for step, reason in store.failed().items() if step not in NON_BLOCKING_STEPS
    }


async def repair_failed_steps(
    audit_id: UUID,
    repositories: Repositories,
    settings: Settings,
    launch: RepairLauncher,
    clock: Clock = lambda: datetime.now(UTC),
) -> RepairOutcome:
    store = StepStore(audit_id, repositories)
    for step, reason in blocking_failures(store).items():
        attempt = store.count_attempt(step)
        label = step_label(step)
        _event(
            repositories,
            audit_id,
            Result.RESTARTED,
            TAKEOVER_LABEL,
            f"{label}, Versuch {attempt}: {reason}",
            step,
        )
        try:
            await launch(audit_id, step, reason, settings)
        except ClaudeCodeError as error:
            logger.warning("Repair agent of audit %s did not run: %s", audit_id, error)
            _event(
                repositories,
                audit_id,
                Result.FAILED,
                TAKEOVER_LABEL,
                f"{label}: Der Audit-Agent konnte nicht laufen ({error})",
                step,
            )
            return RepairOutcome(False, retry_at(attempt, clock()))
        store.reload()
        if store.finished(step) is not None:
            _event(
                repositories,
                audit_id,
                Result.MEASURED,
                DONE_LABEL,
                f"{label} nach Versuch {attempt} gemessen",
                step,
            )
        elif attempt % WARNING_EVERY == 0:
            still = blocking_failures(store).get(step, reason)
            _event(
                repositories,
                audit_id,
                Result.WARNING,
                WARNING_LABEL,
                f"{label} scheitert nach {attempt} Versuchen weiter ({still}); "
                "der Audit-Agent arbeitet weiter",
                step,
            )
    left = blocking_failures(store)
    if not left:
        return RepairOutcome(True)
    waiting = any(
        (stored := store.stored(step)) is not None and stored.credit_short for step in left
    )
    return RepairOutcome(False, clock() + CREDIT_WAIT if waiting else None)


def _event(
    repositories: Repositories,
    audit_id: UUID,
    result: Result,
    label: str,
    reason: str,
    step: str,
) -> None:
    try:
        repositories.add_repair_event(audit_id, result, label, reason, step)
    except RadarError as error:
        logger.error("Repair event of audit %s could not be written: %s", audit_id, error)
