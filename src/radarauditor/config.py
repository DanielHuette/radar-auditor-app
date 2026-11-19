from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, HttpUrl, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from radarauditor.errors import ConfigurationError
from radarauditor.industries import INDUSTRIES_FILE

DEFAULT_CATALOGUE_PATH = Path("config/pruefkatalog.yaml")
MAIN_DIRECTORY_MARKER = "00_System"
ENV_FILE_NAME = ".env"
QUESTIONS_FILE = "fragen.yaml"
ROUTES_FILE = "messwege.yaml"
HIGHEST_STAGE = 4
PACKAGES_FILE = "pakete.yaml"
PROSPECT_CRITERIA_FILE = "kundensuche.yaml"
ALIVE_FILE = "worker-alive"
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 900
DEFAULT_ASSISTANT_TIMEOUT_SECONDS = 120
DEFAULT_AUDIT_MANAGER_TIMEOUT_SECONDS = 10800
DEFAULT_CLAUDE_PARALLEL_QUESTIONS = 4
DEFAULT_CHATGPT_WINDOWS = 2
MAX_CHATGPT_WINDOWS = 3
DEFAULT_HANDOFF_TIMEOUT_SECONDS = 1800
DEFAULT_AI_MODE_TIMEOUT_SECONDS = 1200
DEFAULT_AI_MODE_POLL_SECONDS = 15
DEFAULT_DATAFORSEO_URL = "https://api.dataforseo.com"
DEFAULT_DATAFORSEO_TASK_TIMEOUT_SECONDS = 900
DEFAULT_DATAFORSEO_POLL_SECONDS = 3
DEFAULT_REPAIR_TIMEOUT_SECONDS = 3600
DEFAULT_CONTROL_MODEL = "claude-fable-5-2"
DEFAULT_CONTROL_TIMEOUT_SECONDS = 1800
DEFAULT_CONTROL_BUNDLE_SIZE = 8
MAX_CONTROL_BUNDLE_SIZE = 20
DEFAULT_MAIL_TRIAGE_BATCH = 5
DEFAULT_MAIL_TRIAGE_TIMEOUT_SECONDS = 120
DEFAULT_MAIL_TRIAGE_CLAIM_SECONDS = 900
MAX_MAIL_TRIAGE_BATCH = 50
MIN_MAIL_TRIAGE_CLAIM_SECONDS = 30
MAX_MAIL_TRIAGE_CLAIM_SECONDS = 3600
DEFAULT_COCKPIT_ASSISTANT_POLL_SECONDS = 10
DEFAULT_COCKPIT_ASSISTANT_TIMEOUT_SECONDS = 120
DEFAULT_SPEECH_TIMEOUT_SECONDS = 60
SPEECH_ATTEMPTS = 3
ASSISTANT_CLAIM_SECONDS = 600

PermissionMode = Literal["dontAsk", "acceptEdits", "auto", "bypassPermissions", "manual", "plan"]


def _questions_next_to_catalogue(values: dict[str, Any]) -> Path:
    catalogue = values.get("catalogue_path", DEFAULT_CATALOGUE_PATH)
    return Path(catalogue).parent / QUESTIONS_FILE


def _routes_next_to_catalogue(values: dict[str, Any]) -> Path:
    catalogue = values.get("catalogue_path", DEFAULT_CATALOGUE_PATH)
    return Path(catalogue).parent / ROUTES_FILE


def _packages_next_to_catalogue(values: dict[str, Any]) -> Path:
    catalogue = values.get("catalogue_path", DEFAULT_CATALOGUE_PATH)
    return Path(catalogue).parent / PACKAGES_FILE


def _prospect_criteria_next_to_catalogue(values: dict[str, Any]) -> Path:
    catalogue = values.get("catalogue_path", DEFAULT_CATALOGUE_PATH)
    return Path(catalogue).parent / PROSPECT_CRITERIA_FILE


def _industries_next_to_catalogue(values: dict[str, Any]) -> Path:
    catalogue = values.get("catalogue_path", DEFAULT_CATALOGUE_PATH)
    return Path(catalogue).parent / INDUSTRIES_FILE


def _alive_file_in_work_dir(values: dict[str, Any]) -> Path:
    return Path(values.get("radar_work_dir", Path("work"))) / ALIVE_FILE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", env_ignore_empty=True)

    supabase_url: HttpUrl
    supabase_service_key: SecretStr
    google_api_key: SecretStr
    radar_work_dir: Path = Path("work")
    worker_alive_file: Path = Field(default_factory=_alive_file_in_work_dir)
    browser_profile_dir: Path = Path("browser-profile")
    browser_channel: str = "chromium"
    forecast_path: Path = Path("config/prognose.yaml")
    templates_dir: Path = Path("templates")
    catalogue_path: Path = DEFAULT_CATALOGUE_PATH
    questions_path: Path = Field(default_factory=_questions_next_to_catalogue)
    packages_path: Path = Field(default_factory=_packages_next_to_catalogue)
    industries_path: Path = Field(default_factory=_industries_next_to_catalogue)
    prospect_criteria_path: Path = Field(default_factory=_prospect_criteria_next_to_catalogue)
    event_endpoint: HttpUrl | None = None
    dataforseo_login: str | None = None
    dataforseo_password: SecretStr | None = None
    apify_token: SecretStr | None = None
    dataforseo_eur_per_usd: Decimal = Field(default=Decimal(1), gt=0)
    dataforseo_url: str = DEFAULT_DATAFORSEO_URL
    dataforseo_task_timeout_seconds: int = Field(
        default=DEFAULT_DATAFORSEO_TASK_TIMEOUT_SECONDS, gt=0
    )
    dataforseo_poll_seconds: int = Field(default=DEFAULT_DATAFORSEO_POLL_SECONDS, gt=0)
    apify_eur_per_usd: Decimal = Field(default=Decimal(1), gt=0)
    google_eur_per_usd: Decimal = Field(default=Decimal(1), gt=0)
    openai_eur_per_usd: Decimal = Field(default=Decimal(1), gt=0)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    claude_model: str = "claude-haiku-4-5"
    claude_parallel_questions: int = Field(default=DEFAULT_CLAUDE_PARALLEL_QUESTIONS, ge=1, le=8)
    assistant_timeout_seconds: int = Field(default=DEFAULT_ASSISTANT_TIMEOUT_SECONDS, gt=0)
    chatgpt_windows: int = Field(default=DEFAULT_CHATGPT_WINDOWS, ge=1, le=MAX_CHATGPT_WINDOWS)
    handoff_timeout_seconds: int = Field(default=DEFAULT_HANDOFF_TIMEOUT_SECONDS, gt=0)
    handoff_board_path: Path | None = None
    assistant_handoff: bool = False
    estimate_before_start: bool = True
    ai_mode_timeout_seconds: int = Field(default=DEFAULT_AI_MODE_TIMEOUT_SECONDS, gt=0)
    ai_mode_poll_seconds: int = Field(default=DEFAULT_AI_MODE_POLL_SECONDS, gt=0)
    preflight_routes: bool = True
    halt_stages: tuple[int, ...] = ()
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_token: SecretStr | None = None
    routes_path: Path = Field(default_factory=_routes_next_to_catalogue)
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    analysis_claude_command: str = "claude"
    analysis_timeout_seconds: int = Field(default=DEFAULT_ANALYSIS_TIMEOUT_SECONDS, gt=0)
    analysis_project_dir: Path = Path(".")
    analysis_permission_mode: PermissionMode = "dontAsk"
    analysis_model: str | None = None
    audit_manager_enabled: bool = True
    audit_manager_timeout_seconds: int = Field(default=DEFAULT_AUDIT_MANAGER_TIMEOUT_SECONDS, gt=0)
    audit_manager_model: str | None = None
    repair_timeout_seconds: int = Field(default=DEFAULT_REPAIR_TIMEOUT_SECONDS, gt=0)
    control_model: str = DEFAULT_CONTROL_MODEL
    control_timeout_seconds: int = Field(default=DEFAULT_CONTROL_TIMEOUT_SECONDS, gt=0)
    control_bundle_size: int = Field(
        default=DEFAULT_CONTROL_BUNDLE_SIZE, ge=1, le=MAX_CONTROL_BUNDLE_SIZE
    )
    report_show_costs: bool = True
    mail_triage_batch: int = Field(
        default=DEFAULT_MAIL_TRIAGE_BATCH, ge=0, le=MAX_MAIL_TRIAGE_BATCH
    )
    mail_triage_timeout_seconds: int = Field(default=DEFAULT_MAIL_TRIAGE_TIMEOUT_SECONDS, gt=0)
    mail_triage_claim_seconds: int = Field(
        default=DEFAULT_MAIL_TRIAGE_CLAIM_SECONDS,
        ge=MIN_MAIL_TRIAGE_CLAIM_SECONDS,
        le=MAX_MAIL_TRIAGE_CLAIM_SECONDS,
    )
    mail_triage_model: str = "sonnet"
    speech_url: HttpUrl | None = None
    speech_timeout_seconds: int = Field(default=DEFAULT_SPEECH_TIMEOUT_SECONDS, gt=0)
    cockpit_assistant_poll_seconds: int = Field(
        default=DEFAULT_COCKPIT_ASSISTANT_POLL_SECONDS, ge=0, le=3600
    )
    cockpit_assistant_timeout_seconds: int = Field(
        default=DEFAULT_COCKPIT_ASSISTANT_TIMEOUT_SECONDS, gt=0
    )
    cockpit_assistant_model: str = "sonnet"

    @field_validator("halt_stages")
    @classmethod
    def _stages_exist(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        unknown = [stage for stage in value if not 0 <= stage <= HIGHEST_STAGE]
        if unknown:
            raise ValueError(f"stages run from 0 to {HIGHEST_STAGE}, not {unknown}")
        return value

    @field_validator("mail_triage_timeout_seconds")
    @classmethod
    def _batch_fits_the_longest_claim(cls, value: int, info: ValidationInfo) -> int:
        batch = info.data.get("mail_triage_batch", 0)
        if batch * value > MAX_MAIL_TRIAGE_CLAIM_SECONDS:
            raise ValueError(
                f"MAIL_TRIAGE_BATCH times MAIL_TRIAGE_TIMEOUT_SECONDS ({batch * value}) is above "
                f"the longest claim of {MAX_MAIL_TRIAGE_CLAIM_SECONDS} seconds"
            )
        return value

    @field_validator("mail_triage_claim_seconds")
    @classmethod
    def _claim_outlasts_the_batch(cls, value: int, info: ValidationInfo) -> int:
        batch = info.data.get("mail_triage_batch", 0)
        timeout = info.data.get("mail_triage_timeout_seconds", 0)
        if value < batch * timeout:
            raise ValueError(
                f"must be at least MAIL_TRIAGE_BATCH times MAIL_TRIAGE_TIMEOUT_SECONDS "
                f"({batch * timeout})"
            )
        return value

    @field_validator("cockpit_assistant_timeout_seconds")
    @classmethod
    def _request_ends_before_it_is_claimed_again(cls, value: int, info: ValidationInfo) -> int:
        speech = info.data.get("speech_timeout_seconds", 0) * SPEECH_ATTEMPTS
        if speech + value >= ASSISTANT_CLAIM_SECONDS:
            raise ValueError(
                f"{SPEECH_ATTEMPTS} times SPEECH_TIMEOUT_SECONDS plus "
                f"COCKPIT_ASSISTANT_TIMEOUT_SECONDS ({speech + value}) must stay below the "
                f"{ASSISTANT_CLAIM_SECONDS} seconds after which the CRM hands a request out again"
            )
        return value


def main_directory_env_file(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / MAIN_DIRECTORY_MARKER).is_dir():
            return directory / ENV_FILE_NAME
    return None


def load_settings() -> Settings:
    try:
        return Settings(_env_file=main_directory_env_file(Path.cwd()))  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigurationError(describe_validation_error(exc)) from exc


def describe_validation_error(exc: ValidationError) -> str:
    missing: list[str] = []
    malformed: list[str] = []
    for error in exc.errors():
        variable = str(error["loc"][0]).upper() if error["loc"] else "<settings>"
        if error["type"] == "missing":
            missing.append(variable)
        else:
            malformed.append(f"{variable} ({error['msg']})")
    parts: list[str] = []
    if missing:
        parts.append("missing environment variables: " + ", ".join(missing))
    if malformed:
        parts.append("malformed environment variables: " + ", ".join(malformed))
    return "; ".join(parts)
