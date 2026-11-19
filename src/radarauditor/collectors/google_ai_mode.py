import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from radarauditor.collectors.paid_rankings import (
    GERMANY_LOCATION_CODE,
    LANGUAGE_CODE,
    Clock,
    DataForSeoClient,
    Sleep,
    run_tasks,
)
from radarauditor.errors import CollectionError

logger = logging.getLogger(__name__)

AI_MODE_PATH = "/v3/serp/google/ai_mode"
POST_PATH = f"{AI_MODE_PATH}/task_post"
READY_PATH = f"{AI_MODE_PATH}/tasks_ready"
RESULT_PATH = f"{AI_MODE_PATH}/task_get/advanced"
DEVICE = "desktop"
NO_ANSWER_REASON = "Google showed no AI mode answer for this question"


class _Reference(BaseModel):
    url: str | None = None


class _Element(BaseModel):
    text: str | None = None
    references: list[_Reference] | None = None


class _Overview(BaseModel):
    type: str
    markdown: str | None = None
    items: list[_Element] | None = None
    references: list[_Reference] | None = None


class _Result(BaseModel):
    items: list[_Overview] | None = None


@dataclass(frozen=True)
class AiModeAnswer:
    text: str
    citations: list[str]


@dataclass(frozen=True)
class AiModeBatch:
    answers: dict[int, AiModeAnswer]
    failed: dict[int, str]


async def ask_ai_mode(
    client: DataForSeoClient,
    questions: list[str],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> AiModeBatch:
    tasks = [
        {
            "keyword": question,
            "location_code": GERMANY_LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "device": DEVICE,
            "tag": str(index),
        }
        for index, question in enumerate(questions)
    ]
    batch = await run_tasks(
        client,
        AI_MODE_PATH,
        tasks,
        priority=None,
        repeat_failed=False,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        sleep=sleep,
        clock=clock,
    )
    answers: dict[int, AiModeAnswer] = {}
    failed = dict(batch.failed)
    for index, rows in batch.results.items():
        try:
            answer = _answer(rows)
        except CollectionError as error:
            failed[index] = str(error)
            continue
        if answer is None:
            failed[index] = NO_ANSWER_REASON
        else:
            answers[index] = answer
    return AiModeBatch(answers, failed)


def _answer(rows: list[dict[str, Any]]) -> AiModeAnswer | None:
    texts: list[str] = []
    citations: list[str] = []
    for row in rows:
        for overview in _parse(row).items or []:
            if overview.type != "ai_overview":
                continue
            elements = overview.items or []
            texts.append(overview.markdown or "\n".join(e.text or "" for e in elements))
            for reference in [
                *(overview.references or []),
                *(r for e in elements for r in e.references or []),
            ]:
                if reference.url and reference.url not in citations:
                    citations.append(reference.url)
    text = "\n".join(part for part in texts if part.strip()).strip()
    return AiModeAnswer(text, citations) if text else None


def _parse(row: dict[str, Any]) -> _Result:
    try:
        return _Result.model_validate(row)
    except ValidationError as error:
        raise CollectionError("DataForSEO AI mode result is malformed") from error
