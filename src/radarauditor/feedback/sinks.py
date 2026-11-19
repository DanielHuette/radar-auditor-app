import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import SecretStr

from radarauditor.collectors.assistant_handoff import HandoffBoard
from radarauditor.db.repositories import Repositories
from radarauditor.errors import CollectionError, RadarError
from radarauditor.feedback.events import AuditEvent

logger = logging.getLogger(__name__)

NTFY_TITLE = "Radar Auditor"
NTFY_TIMEOUT_SECONDS = 10
NTFY_NORMAL = ("default", "warning")
NTFY_URGENT = ("high", "rotating_light")

type Notice = Callable[..., None]


def file_sink(folder: Path, audit_id: UUID) -> Callable[[AuditEvent], None]:
    target = folder / f"{audit_id}.jsonl"

    def write(event: AuditEvent) -> None:
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{line}\n")
        except OSError as error:
            logger.warning("Event file %s not written: %s", target, error)

    return write


def database_sink(repositories: Repositories, audit_id: UUID) -> Callable[[list[AuditEvent]], None]:
    def save(events: list[AuditEvent]) -> None:
        try:
            repositories.add_audit_events(audit_id, events)
        except RadarError as error:
            logger.warning("Events not saved: %s", error)

    return save


def board_sink(path: Path | None, key: str) -> Callable[[str], None]:
    board = HandoffBoard(path)

    def put(text: str) -> None:
        try:
            board.notify_audit(key, text)
        except CollectionError as error:
            logger.warning("Card not put on the board: %s", error)

    return put


def overview_sink(folder: Path, audit_id: UUID) -> Callable[[str], None]:
    target = folder / f"{audit_id}_uebersicht.md"

    def write(table: str) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(table, encoding="utf-8", newline="\n")
        except OSError as error:
            logger.warning("Overview %s not written: %s", target, error)

    return write


def ntfy_notice(server: str, topic: str | None, token: SecretStr | None) -> Notice:
    if not topic:
        return lambda text, urgent, click=None: None
    url = f"{server.rstrip('/')}/{topic}"
    authorization = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}

    def send(text: str, urgent: bool, click: str | None = None) -> None:
        priority, tag = NTFY_URGENT if urgent else NTFY_NORMAL
        headers = {
            "X-Title": NTFY_TITLE,
            "X-Priority": priority,
            "X-Tags": tag,
            **({"X-Click": click} if click else {}),
            **authorization,
        }
        try:
            response = httpx.post(
                url, content=text.encode("utf-8"), headers=headers, timeout=NTFY_TIMEOUT_SECONDS
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            logger.warning("ntfy notice not sent: HTTP %s", error.response.status_code)
        except httpx.HTTPError as error:
            logger.warning("ntfy notice not sent: %s", type(error).__name__)

    return send
