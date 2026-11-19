import asyncio
from statistics import median
from typing import Literal, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError
from pydantic.alias_generators import to_camel

from radarauditor.collectors import raw_answers
from radarauditor.collectors.snapshot import (
    FieldScope,
    PageSpeedField,
    PageSpeedLab,
    PageSpeedSnapshot,
)
from radarauditor.collectors.transport import send
from radarauditor.errors import CollectionError

PAGESPEED_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
REQUEST_TIMEOUT_SECONDS = 120
MOBILE_RUNS = 5
CATEGORIES = ("performance", "accessibility")
FIELD_LCP = "LARGEST_CONTENTFUL_PAINT_MS"
FIELD_CLS = "CUMULATIVE_LAYOUT_SHIFT_SCORE"
FIELD_INP = "INTERACTION_TO_NEXT_PAINT"
LAB_LCP = "largest-contentful-paint"
LAB_CLS = "cumulative-layout-shift"
LAB_TBT = "total-blocking-time"
FIELD_CLS_SCALE = 100

Strategy = Literal["mobile", "desktop"]


class _ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class _Category(_ApiModel):
    score: float | None = None


class _LabAudit(_ApiModel):
    numeric_value: float | None = None


class _Lighthouse(_ApiModel):
    categories: dict[str, _Category]
    audits: dict[str, _LabAudit] = {}


class _FieldMetric(_ApiModel):
    percentile: float | None = None


class _LoadingExperience(_ApiModel):
    metrics: dict[str, _FieldMetric] = {}
    origin_fallback: bool = Field(default=False, alias="origin_fallback")


class _PageSpeedResponse(_ApiModel):
    lighthouse_result: _Lighthouse
    loading_experience: _LoadingExperience | None = None
    origin_loading_experience: _LoadingExperience | None = None


class _MobileRun(NamedTuple):
    performance: int
    accessibility: int
    lcp_ms: float
    cls: float
    tbt_ms: float
    field_data: PageSpeedField | None


async def measure(url: HttpUrl, api_key: SecretStr) -> PageSpeedSnapshot:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        runs, desktop = await asyncio.gather(
            _mobile_runs(client, url, api_key),
            _run(client, url, api_key, "desktop"),
            return_exceptions=True,
        )
    if isinstance(runs, BaseException):
        raise runs
    if isinstance(desktop, BaseException):
        raise desktop
    return PageSpeedSnapshot(
        url=url,
        mobile_performance=round(median(run.performance for run in runs)),
        desktop_performance=_category_score(desktop, "performance"),
        accessibility=round(median(run.accessibility for run in runs)),
        lab=PageSpeedLab(
            runs=len(runs),
            lcp_ms=round(median(run.lcp_ms for run in runs)),
            cls=round(median(run.cls for run in runs), 3),
            tbt_ms=round(median(run.tbt_ms for run in runs)),
        ),
        field_data=runs[0].field_data,
    )


async def _mobile_runs(
    client: httpx.AsyncClient, url: HttpUrl, api_key: SecretStr
) -> list[_MobileRun]:
    runs: list[_MobileRun] = []
    errors: list[CollectionError] = []
    for _ in range(MOBILE_RUNS):
        try:
            runs.append(_mobile_run(await _run(client, url, api_key, "mobile")))
        except CollectionError as error:
            errors.append(error)
    if not runs:
        raise errors[-1]
    return runs


async def _run(
    client: httpx.AsyncClient, url: HttpUrl, api_key: SecretStr, strategy: Strategy
) -> _PageSpeedResponse:
    params = [("url", str(url)), ("strategy", strategy)]
    params.extend(("category", category) for category in CATEGORIES)
    response = await send(
        client,
        "GET",
        PAGESPEED_ENDPOINT,
        source=f"PageSpeed Insights ({strategy})",
        params=params,
        headers={"X-Goog-Api-Key": api_key.get_secret_value()},
    )
    raw_name = raw_answers.file_name("pagespeed", "google_pagespeed", strategy)
    raw_answers.keep(raw_name, response.content)
    try:
        return _PageSpeedResponse.model_validate(response.json())
    except (ValueError, ValidationError) as error:
        raise CollectionError(f"PageSpeed Insights ({strategy}) answer is malformed") from error


def _mobile_run(result: _PageSpeedResponse) -> _MobileRun:
    return _MobileRun(
        performance=_category_score(result, "performance"),
        accessibility=_category_score(result, "accessibility"),
        lcp_ms=_lab(result, LAB_LCP),
        cls=_lab(result, LAB_CLS),
        tbt_ms=_lab(result, LAB_TBT),
        field_data=_field_data(result),
    )


def _category_score(result: _PageSpeedResponse, category: str) -> int:
    entry = result.lighthouse_result.categories.get(category)
    if entry is None or entry.score is None:
        raise CollectionError(f"PageSpeed Insights returned no {category} score")
    return round(entry.score * 100)


def _lab(result: _PageSpeedResponse, audit: str) -> float:
    entry = result.lighthouse_result.audits.get(audit)
    if entry is None or entry.numeric_value is None:
        raise CollectionError(f"PageSpeed Insights returned no {audit}")
    return entry.numeric_value


def _field_data(result: _PageSpeedResponse) -> PageSpeedField | None:
    page = result.loading_experience
    page_scope: FieldScope = "origin" if page is not None and page.origin_fallback else "url"
    experiences: list[tuple[FieldScope, _LoadingExperience | None]] = [
        (page_scope, page),
        ("origin", result.origin_loading_experience),
    ]
    for scope, experience in experiences:
        if experience is None:
            continue
        cls = _percentile(experience, FIELD_CLS)
        field_data = PageSpeedField(
            scope=scope,
            lcp_ms=_rounded(_percentile(experience, FIELD_LCP)),
            cls=None if cls is None else round(cls / FIELD_CLS_SCALE, 3),
            inp_ms=_rounded(_percentile(experience, FIELD_INP)),
        )
        if (field_data.lcp_ms, field_data.cls, field_data.inp_ms) != (None, None, None):
            return field_data
    return None


def _percentile(experience: _LoadingExperience, metric: str) -> float | None:
    entry = experience.metrics.get(metric)
    return entry.percentile if entry is not None else None


def _rounded(value: float | None) -> int | None:
    return round(value) if value is not None else None
