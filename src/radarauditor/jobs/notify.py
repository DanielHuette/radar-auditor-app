import logging
from decimal import Decimal
from uuid import UUID

from radarauditor.config import Settings
from radarauditor.db.models import Audit
from radarauditor.db.repositories import Repositories
from radarauditor.errors import RadarError
from radarauditor.feedback import sinks
from radarauditor.feedback.events import Result

logger = logging.getLogger(__name__)

APP_URL = "https://app.radarauditor.speedofthespirit.dev"
REASON_LIMIT = 280
MAX_SCORE = 100


def audit_link(audit_id: UUID) -> str:
    return f"{APP_URL}/auswertung/{audit_id}"


def shortened(reason: str) -> str:
    text = " ".join(reason.split())
    if len(text) <= REASON_LIMIT:
        return text
    return text[: REASON_LIMIT - 1].rstrip() + "…"


def score_words(score: Decimal | None) -> str | None:
    if score is None:
        return None
    return f"{format(score.normalize(), 'f').replace('.', ',')} von {MAX_SCORE} Punkten"


def reason_tail(reason: str | None) -> str:
    return f": {shortened(reason)}" if reason and reason.strip() else "."


def verdict_text(subject: str, verdict: Result, points: str | None, reason: str | None) -> str:
    if verdict is Result.MEASURED:
        return f"{subject} fertig: vollständig" + (f", {points}." if points else ".")
    if verdict is Result.SUSPENDED:
        return f"{subject} unvollständig" + (f" ({points})" if points else "") + reason_tail(reason)
    return f"{subject} fehlgeschlagen" + reason_tail(reason)


class Notifier:
    def __init__(self, repositories: Repositories, settings: Settings) -> None:
        self._repositories = repositories
        self._enabled = bool(settings.ntfy_topic)
        self._send = sinks.ntfy_notice(
            settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token
        )

    def audit_started(self, audit_id: UUID, customer_id: UUID) -> None:
        if not self._enabled:
            return
        self._notify(f"{self._subject(customer_id)} gestartet.", False, audit_link(audit_id))

    def audit_concluded(self, audit_id: UUID, verdict: Result, reason: str | None) -> None:
        if not self._enabled:
            return
        audit = self._audit(audit_id)
        subject = self._subject(audit.customer_id if audit is not None else None)
        points = score_words(audit.total_score) if audit is not None else None
        text = verdict_text(subject, verdict, points, reason)
        self._notify(text, verdict is not Result.MEASURED, audit_link(audit_id))

    def _notify(self, text: str, urgent: bool, link: str) -> None:
        self._send(f"{text}\n{link}", urgent, link)

    def _subject(self, customer_id: UUID | None) -> str:
        if customer_id is None:
            return "Audit"
        try:
            return f"Audit für {self._repositories.customer(customer_id).name}"
        except RadarError as error:
            logger.warning("Customer %s for the notice not read: %s", customer_id, error)
            return "Audit"

    def _audit(self, audit_id: UUID) -> Audit | None:
        try:
            return self._repositories.audit(audit_id)
        except RadarError as error:
            logger.warning("Audit %s for the notice not read: %s", audit_id, error)
            return None
