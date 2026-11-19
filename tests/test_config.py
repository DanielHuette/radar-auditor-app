from decimal import Decimal
from pathlib import Path

import pytest

from radarauditor.config import Settings, load_settings
from radarauditor.errors import ConfigurationError


def test_load_settings_reads_environment(settings: Settings, environment: Path) -> None:
    assert str(settings.supabase_url) == "https://example.supabase.co/"
    assert settings.supabase_service_key.get_secret_value() == "service-key-for-tests"
    assert settings.radar_work_dir == environment
    assert settings.dataforseo_eur_per_usd == 1
    assert settings.event_endpoint is None


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises(ValueError, match="frozen"):
        settings.dataforseo_login = "login"


def test_missing_variables_are_named(environment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("GOOGLE_API_KEY")

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    message = str(caught.value)
    assert "missing environment variables" in message
    assert "SUPABASE_URL" in message
    assert "GOOGLE_API_KEY" in message
    assert "SUPABASE_SERVICE_KEY" not in message


def test_malformed_variables_are_named(environment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "not a url")
    monkeypatch.setenv("DATAFORSEO_EUR_PER_USD", "maybe")

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    message = str(caught.value)
    assert message.startswith("malformed environment variables")
    assert "SUPABASE_URL" in message
    assert "DATAFORSEO_EUR_PER_USD" in message


@pytest.mark.parametrize("start", [".", "20_Firma_RepoCity/28_Produkte/281_RadarAuditor"])
def test_dotenv_file_of_the_main_directory_is_read(
    environment: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, start: str
) -> None:
    main = tmp_path / "zentrale"
    (main / "00_System").mkdir(parents=True)
    (main / ".env").write_text("GOOGLE_API_KEY=from-main\nUNRELATED=1\n", encoding="utf-8")
    (main / start).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(main / start)
    monkeypatch.delenv("GOOGLE_API_KEY")

    assert load_settings().google_api_key.get_secret_value() == "from-main"


def test_dotenv_file_outside_a_main_directory_is_ignored(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(".env").write_text("GOOGLE_API_KEY=from-app-folder\n", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_API_KEY")

    with pytest.raises(ConfigurationError, match="GOOGLE_API_KEY"):
        load_settings()


def test_a_file_named_like_the_marker_is_no_main_directory(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("00_System").write_text("", encoding="utf-8")
    Path(".env").write_text("GOOGLE_API_KEY=from-main\n", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_API_KEY")

    with pytest.raises(ConfigurationError, match="GOOGLE_API_KEY"):
        load_settings()


def test_environment_variables_win_over_the_main_directory_file(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("00_System").mkdir()
    Path(".env").write_text("GOOGLE_API_KEY=from-main\n", encoding="utf-8")

    assert load_settings().google_api_key.get_secret_value() == "google-key-for-tests"


def test_questions_file_defaults_to_catalogue_folder(settings: Settings, environment: Path) -> None:
    assert settings.questions_path == settings.catalogue_path.parent / "fragen.yaml"


def test_packages_file_defaults_to_catalogue_folder(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.packages_path == settings.catalogue_path.parent / "pakete.yaml"
    monkeypatch.setenv("PACKAGES_PATH", "eigene/pakete.yaml")

    assert load_settings().packages_path == Path("eigene/pakete.yaml")


def test_industries_file_defaults_to_catalogue_folder(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INDUSTRIES_PATH")
    assert load_settings().industries_path == settings.catalogue_path.parent / "branchen.yaml"
    monkeypatch.setenv("INDUSTRIES_PATH", "eigene/branchen.yaml")

    assert load_settings().industries_path == Path("eigene/branchen.yaml")


def test_questions_path_and_models_can_be_set(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUESTIONS_PATH", "eigene/fragen.yaml")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")

    settings = load_settings()

    assert settings.questions_path == Path("eigene/fragen.yaml")
    assert settings.openai_model == "gpt-4o"


def test_openai_rate_defaults_to_one_and_must_be_above_zero(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings().openai_eur_per_usd == 1
    monkeypatch.setenv("OPENAI_EUR_PER_USD", "0.92")
    assert load_settings().openai_eur_per_usd == Decimal("0.92")
    monkeypatch.setenv("OPENAI_EUR_PER_USD", "0")

    with pytest.raises(ConfigurationError, match="OPENAI_EUR_PER_USD"):
        load_settings()


def test_feedback_settings_have_cautious_defaults(settings: Settings) -> None:
    assert settings.halt_stages == ()
    assert settings.ntfy_server == "https://ntfy.sh"
    assert settings.ntfy_topic is None
    assert settings.ntfy_token is None
    assert settings.routes_path == settings.catalogue_path.parent / "messwege.yaml"


def test_feedback_settings_come_from_the_environment(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HALT_STAGES", "[1, 2]")
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.example.org")
    monkeypatch.setenv("NTFY_TOPIC", "repocity-radar-test")
    monkeypatch.setenv("NTFY_TOKEN", "tk_secret")
    monkeypatch.setenv("ROUTES_PATH", "eigene/messwege.yaml")

    loaded = load_settings()

    assert loaded.halt_stages == (1, 2)
    assert loaded.ntfy_server == "https://ntfy.example.org"
    assert loaded.ntfy_topic == "repocity-radar-test"
    assert loaded.ntfy_token is not None and loaded.ntfy_token.get_secret_value() == "tk_secret"
    assert loaded.routes_path == Path("eigene/messwege.yaml")


def test_no_critical_stage_at_all_is_allowed(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HALT_STAGES", "[]")

    assert load_settings().halt_stages == ()


def test_a_stage_above_four_is_refused(environment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HALT_STAGES", "[5]")

    with pytest.raises(ConfigurationError, match="HALT_STAGES"):
        load_settings()


def test_env_example_documents_every_setting() -> None:
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    lines = example.splitlines()
    for name in Settings.model_fields:
        variable = name.upper()
        index = next(i for i, line in enumerate(lines) if line.startswith(f"{variable}="))
        assert lines[index - 1].startswith("#"), variable


def test_empty_variables_count_as_unset(environment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENT_ENDPOINT", "")
    monkeypatch.setenv("QUESTIONS_PATH", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    settings = load_settings()

    assert settings.event_endpoint is None
    assert settings.questions_path == settings.catalogue_path.parent / "fragen.yaml"
    assert settings.openai_api_key is None


def test_analysis_settings_defaults_and_validation(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANALYSIS_CLAUDE_COMMAND")
    defaults = load_settings()
    assert defaults.analysis_claude_command == "claude"
    assert defaults.analysis_timeout_seconds == 900
    assert defaults.analysis_permission_mode == "dontAsk"
    assert defaults.analysis_model is None

    monkeypatch.setenv("ANALYSIS_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("ANALYSIS_PERMISSION_MODE", "everything")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "ANALYSIS_TIMEOUT_SECONDS" in str(caught.value)
    assert "ANALYSIS_PERMISSION_MODE" in str(caught.value)


def test_assistant_timeout_default_and_validation(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings().assistant_timeout_seconds == 120
    monkeypatch.setenv("ASSISTANT_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigurationError, match="ASSISTANT_TIMEOUT_SECONDS"):
        load_settings()


def test_mail_triage_defaults_and_validation(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    defaults = load_settings()
    assert defaults.mail_triage_batch == 5
    assert defaults.mail_triage_claim_seconds == 900
    assert defaults.mail_triage_timeout_seconds == 120
    assert defaults.mail_triage_model == "sonnet"

    monkeypatch.setenv("MAIL_TRIAGE_BATCH", "0")
    assert load_settings().mail_triage_batch == 0

    monkeypatch.setenv("MAIL_TRIAGE_BATCH", "-1")
    monkeypatch.setenv("MAIL_TRIAGE_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "MAIL_TRIAGE_BATCH" in str(caught.value)
    assert "MAIL_TRIAGE_TIMEOUT_SECONDS" in str(caught.value)


@pytest.mark.parametrize(
    ("values", "variable"),
    [
        ({"MAIL_TRIAGE_BATCH": "51", "MAIL_TRIAGE_TIMEOUT_SECONDS": "60"}, "MAIL_TRIAGE_BATCH"),
        (
            {"MAIL_TRIAGE_BATCH": "0", "MAIL_TRIAGE_CLAIM_SECONDS": "29"},
            "MAIL_TRIAGE_CLAIM_SECONDS",
        ),
        ({"MAIL_TRIAGE_CLAIM_SECONDS": "3601"}, "MAIL_TRIAGE_CLAIM_SECONDS"),
    ],
)
def test_mail_triage_bounds(
    environment: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str], variable: str
) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    assert [part.split(" (")[0] for part in str(caught.value).split(": ", 1)[1].split(", ")] == [
        variable
    ]


def test_a_batch_may_take_at_most_one_hour(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAIL_TRIAGE_BATCH", "31")
    monkeypatch.setenv("MAIL_TRIAGE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("MAIL_TRIAGE_CLAIM_SECONDS", "3600")

    with pytest.raises(ConfigurationError, match=r"MAIL_TRIAGE_TIMEOUT_SECONDS \(.*3600"):
        load_settings()


def test_a_claim_ends_after_the_answer_time(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAIL_TRIAGE_CLAIM_SECONDS", "60")

    with pytest.raises(ConfigurationError, match="MAIL_TRIAGE_CLAIM_SECONDS"):
        load_settings()


def test_alive_file_defaults_to_the_work_dir(settings: Settings, environment: Path) -> None:
    assert settings.worker_alive_file == environment / "worker-alive"


def test_cockpit_assistant_defaults(environment: Path) -> None:
    defaults = load_settings()

    assert defaults.cockpit_assistant_poll_seconds == 10
    assert defaults.cockpit_assistant_timeout_seconds == 120
    assert defaults.cockpit_assistant_model == "sonnet"
    assert defaults.speech_url is None
    assert defaults.speech_timeout_seconds == 60


def test_cockpit_assistant_can_be_switched_off_and_pointed_at_the_speech_service(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COCKPIT_ASSISTANT_POLL_SECONDS", "0")
    monkeypatch.setenv("SPEECH_URL", "http://host.docker.internal:8090")

    settings = load_settings()

    assert settings.cockpit_assistant_poll_seconds == 0
    assert str(settings.speech_url) == "http://host.docker.internal:8090/"


@pytest.mark.parametrize(
    ("values", "variable"),
    [
        ({"COCKPIT_ASSISTANT_POLL_SECONDS": "-1"}, "COCKPIT_ASSISTANT_POLL_SECONDS"),
        ({"COCKPIT_ASSISTANT_TIMEOUT_SECONDS": "0"}, "COCKPIT_ASSISTANT_TIMEOUT_SECONDS"),
        ({"SPEECH_TIMEOUT_SECONDS": "0"}, "SPEECH_TIMEOUT_SECONDS"),
        ({"SPEECH_URL": "kein-link"}, "SPEECH_URL"),
    ],
)
def test_cockpit_assistant_bounds(
    environment: Path, monkeypatch: pytest.MonkeyPatch, values: dict[str, str], variable: str
) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=variable):
        load_settings()


def test_one_request_is_finished_before_the_crm_hands_it_out_again(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPEECH_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("COCKPIT_ASSISTANT_TIMEOUT_SECONDS", "300")

    with pytest.raises(ConfigurationError, match=r"COCKPIT_ASSISTANT_TIMEOUT_SECONDS \(.*600"):
        load_settings()


def test_audit_manager_defaults_to_on_with_a_three_hour_limit(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AUDIT_MANAGER_ENABLED")

    defaults = load_settings()

    assert defaults.audit_manager_enabled is True
    assert defaults.audit_manager_timeout_seconds == 10800
    assert defaults.audit_manager_model is None


def test_audit_manager_can_be_switched_off_and_tuned(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_MANAGER_ENABLED", "false")
    monkeypatch.setenv("AUDIT_MANAGER_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("AUDIT_MANAGER_MODEL", "opus")

    tuned = load_settings()

    assert tuned.audit_manager_enabled is False
    assert tuned.audit_manager_timeout_seconds == 60
    assert tuned.audit_manager_model == "opus"


@pytest.mark.parametrize("seconds", ["0", "-5"])
def test_audit_manager_timeout_must_be_positive(
    environment: Path, monkeypatch: pytest.MonkeyPatch, seconds: str
) -> None:
    monkeypatch.setenv("AUDIT_MANAGER_TIMEOUT_SECONDS", seconds)

    with pytest.raises(ConfigurationError, match="AUDIT_MANAGER_TIMEOUT_SECONDS"):
        load_settings()


def test_audit_manager_timeout_of_one_second_is_allowed(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_MANAGER_TIMEOUT_SECONDS", "1")

    assert load_settings().audit_manager_timeout_seconds == 1


def test_the_report_shows_the_costs_unless_switched_off(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings().report_show_costs is True

    monkeypatch.setenv("REPORT_SHOW_COSTS", "0")

    assert load_settings().report_show_costs is False
