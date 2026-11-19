from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from radarauditor.collectors.paid_rankings import (
    GERMANY_LOCATION_CODE,
    LANGUAGE_CODE,
    DataForSeoClient,
    run_tasks,
)
from radarauditor.errors import CollectionError

SCRAPER_TASKS = "/v3/ai_optimization/chat_gpt/llm_scraper"
NO_ANSWER_REASON = "ChatGPT gave no answer through DataForSEO"


class _Source(BaseModel):
    url: str | None = None


class _Item(BaseModel):
    sources: list[_Source] | None = None


class _Result(BaseModel):
    markdown: str | None = None
    sources: list[_Source] | None = None
    items: list[_Item] | None = None


@dataclass(frozen=True)
class ScrapedAnswer:
    text: str
    citations: list[str]


@dataclass(frozen=True)
class ScrapedBatch:
    answers: dict[int, ScrapedAnswer]
    failed: dict[int, str]
    repeated: int = 0


async def ask_chatgpt(client: DataForSeoClient, questions: list[str]) -> ScrapedBatch:
    tasks = [
        {
            "keyword": question,
            "location_code": GERMANY_LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "force_web_search": True,
        }
        for question in questions
    ]
    batch = await run_tasks(client, SCRAPER_TASKS, tasks)
    answers: dict[int, ScrapedAnswer] = {}
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
    return ScrapedBatch(answers, failed, batch.repeated)


def _answer(rows: list[dict[str, Any]]) -> ScrapedAnswer | None:
    texts: list[str] = []
    citations: list[str] = []
    for row in rows:
        result = _parse(row)
        texts.append(result.markdown or "")
        sources = [
            *(result.sources or []),
            *(s for i in result.items or [] for s in i.sources or []),
        ]
        for source in sources:
            if source.url and source.url not in citations:
                citations.append(source.url)
    text = "\n".join(part for part in texts if part.strip()).strip()
    return ScrapedAnswer(text, citations) if text else None


def _parse(row: dict[str, Any]) -> _Result:
    try:
        return _Result.model_validate(row)
    except ValidationError as error:
        raise CollectionError("DataForSEO ChatGPT result is malformed") from error
