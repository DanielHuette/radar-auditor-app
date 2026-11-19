from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from radarauditor.errors import ConfigurationError

SOURCES_KEY = "sources"
WORKER_HEARTBEAT_KEY = "worker_heartbeat"
WORKER_SETTINGS_KEYS = (SOURCES_KEY,)


class Source(StrEnum):
    PLACES = "places"
    CHATGPT = "chatgpt"
    CLAUDE = "claude"
    GOOGLE_AI = "google_ai"
    GOOGLE_SEARCH = "google_search"
    GOOGLE_MAPS = "google_maps"
    DATAFORSEO = "dataforseo"
    APIFY = "apify"
    PROSPECT_SEARCH = "prospect_search"


class SourceSetting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    enabled: bool
    daily_cap: int = Field(ge=0)
    monthly_cost_cap_eur: Decimal | None = Field(default=None, ge=0)


DEFAULT_SOURCES: dict[Source, SourceSetting] = {
    Source.PLACES: SourceSetting(enabled=True, daily_cap=500, monthly_cost_cap_eur=Decimal(25)),
    Source.CHATGPT: SourceSetting(enabled=True, daily_cap=200, monthly_cost_cap_eur=Decimal(5)),
    Source.CLAUDE: SourceSetting(enabled=True, daily_cap=200),
    Source.GOOGLE_AI: SourceSetting(enabled=True, daily_cap=200),
    Source.GOOGLE_SEARCH: SourceSetting(enabled=True, daily_cap=300),
    Source.GOOGLE_MAPS: SourceSetting(enabled=True, daily_cap=500),
    Source.DATAFORSEO: SourceSetting(
        enabled=True, daily_cap=1000, monthly_cost_cap_eur=Decimal(50)
    ),
    Source.APIFY: SourceSetting(enabled=True, daily_cap=1000, monthly_cost_cap_eur=Decimal(10)),
    Source.PROSPECT_SEARCH: SourceSetting(
        enabled=True, daily_cap=15, monthly_cost_cap_eur=Decimal(10)
    ),
}


def _without_lifted_cap(given: Mapping[str, object], default: SourceSetting) -> dict[str, object]:
    if default.monthly_cost_cap_eur is not None and given.get("monthly_cost_cap_eur") is None:
        return {key: item for key, item in given.items() if key != "monthly_cost_cap_eur"}
    return dict(given)


class SourcesSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    places: SourceSetting = DEFAULT_SOURCES[Source.PLACES]
    chatgpt: SourceSetting = DEFAULT_SOURCES[Source.CHATGPT]
    claude: SourceSetting = DEFAULT_SOURCES[Source.CLAUDE]
    google_ai: SourceSetting = DEFAULT_SOURCES[Source.GOOGLE_AI]
    google_search: SourceSetting = DEFAULT_SOURCES[Source.GOOGLE_SEARCH]
    google_maps: SourceSetting = DEFAULT_SOURCES[Source.GOOGLE_MAPS]
    dataforseo: SourceSetting = DEFAULT_SOURCES[Source.DATAFORSEO]
    apify: SourceSetting = DEFAULT_SOURCES[Source.APIFY]
    prospect_search: SourceSetting = DEFAULT_SOURCES[Source.PROSPECT_SEARCH]

    @model_validator(mode="before")
    @classmethod
    def _merge_onto_defaults(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        merged: dict[str, object] = {}
        for source, default in DEFAULT_SOURCES.items():
            given = value.get(source.value)
            if isinstance(given, Mapping):
                stored = _without_lifted_cap(given, default)
                merged[source.value] = {**default.model_dump(), **stored}
            elif given is not None:
                merged[source.value] = given
        return merged

    def of(self, source: Source) -> SourceSetting:
        setting: SourceSetting = getattr(self, source.value)
        return setting


class WorkerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    sources: SourcesSettings = SourcesSettings()


class WorkerCapabilities(BaseModel):
    chromium: bool
    claude_cli: bool
    claude_logged_in: bool
    browser_profile: bool
    analysis_available: bool
    analysis_reason: str | None = None
    mail_reading_available: bool = False
    mail_reading_reason: str | None = None


class WorkerHeartbeat(BaseModel):
    at: datetime
    version: str
    capabilities: WorkerCapabilities


def parse_worker_settings(values: Mapping[str, object]) -> WorkerSettings:
    present = {key: values[key] for key in WORKER_SETTINGS_KEYS if values.get(key) is not None}
    try:
        return WorkerSettings.model_validate(present)
    except ValidationError as exc:
        keys = ", ".join(present)
        raise ConfigurationError(f"settings rows {keys} are malformed: {exc}") from exc
