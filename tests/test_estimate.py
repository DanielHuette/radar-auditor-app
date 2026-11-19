from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import respx
from pydantic import SecretStr
from test_openai_price import PRICE_LINE, routes_with

from radarauditor.collectors import (
    chatgpt_scraper,
    dataforseo_maps,
    estimate,
    google_ai_mode,
    openai_price,
    paid_data,
    paid_rankings,
)
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.db.models import AuditEstimate

TOTAL_DATAFORSEO = Decimal("1.000824")
CATALOGUE_PATH = Path(__file__).resolve().parents[1] / "config" / "pruefkatalog.yaml"

PLAN = estimate.Plan(
    phrases=40, image_phrases=6, competitors=3, questions=6, repeats=10, map_searches=64
)


def _settings(**update: object) -> Settings:
    base: dict[str, object] = dict(
        _env_file=None,
        radar_work_dir=Path("work"),
        catalogue_path=CATALOGUE_PATH,
        questions_path=CATALOGUE_PATH.with_name("fragen.yaml"),
        industries_path=CATALOGUE_PATH.with_name("branchen.yaml"),
        supabase_url="https://example.supabase.co",
        supabase_service_key=SecretStr("key"),
        google_api_key=SecretStr("google-key"),
        dataforseo_login=None,
        dataforseo_password=None,
        apify_token=None,
    )
    return Settings(**{**base, **update})


def _gate(ledger: MagicMock | None = None) -> SourceGate:
    return SourceGate(SourcesSettings.model_validate({}), lambda source, cap: True, ledger)


def _by_service(lines: list[AuditEstimate]) -> dict[str, AuditEstimate]:
    return {line.service: line for line in lines}


def _line(source: str, cost: str) -> AuditEstimate:
    return AuditEstimate(
        source=source, service="x", calls=1, unit_cost=Decimal(cost), cost=Decimal(cost)
    )


def _balances(
    dataforseo: str | None, apify: str | None, unreadable: list[str] | None = None
) -> estimate.Balances:
    return estimate.Balances(
        None if dataforseo is None else Decimal(dataforseo),
        None if apify is None else Decimal(apify),
        unreadable or [],
    )


def test_estimate_prices_every_dataforseo_call_from_the_quantities() -> None:
    lines = _by_service(estimate.build_estimate(PLAN, _settings()))

    assert lines[google_ai_mode.POST_PATH].calls == 60
    assert lines[google_ai_mode.POST_PATH].cost == Decimal("0.072")
    assert lines[paid_rankings.VOLUME_PATH].cost == Decimal("0.09")
    assert lines[paid_data.SUMMARY_PATH].calls == 4
    assert lines[paid_data.INTERSECTION_PATH].calls == 3
    assert lines[paid_data.INTERSECTION_PATH].cost == Decimal("0.054")
    assert lines[paid_data.HISTORY_PATH].cost == Decimal("0.1344")


def test_estimate_prices_the_priority_queue_routes() -> None:
    lines = _by_service(estimate.build_estimate(PLAN, _settings()))

    organic = lines[estimate.task_post(paid_rankings.ORGANIC_TASKS)]
    assert (organic.calls, organic.cost) == (40, Decimal("0.1440"))
    maps = lines[estimate.task_post(dataforseo_maps.MAPS_TASKS)]
    assert (maps.calls, maps.cost) == (64, Decimal("0.0768"))
    assert lines[estimate.task_post(dataforseo_maps.INFO_TASKS)].cost == Decimal("0.012")
    assert lines[estimate.task_post(dataforseo_maps.REVIEWS_TASKS)].cost == Decimal("0.0120")
    assert lines[estimate.task_post(dataforseo_maps.QUESTIONS_TASKS)].cost == Decimal("0.0060")
    chatgpt = lines[estimate.task_post(chatgpt_scraper.SCRAPER_TASKS)]
    assert (chatgpt.calls, chatgpt.cost) == (60, Decimal("0.1440"))


def test_estimate_total_for_dataforseo_for_katja() -> None:
    lines = estimate.build_estimate(PLAN, _settings())

    total = sum(line.cost for line in lines if line.source == Source.DATAFORSEO.value)

    assert total == TOTAL_DATAFORSEO


def test_estimate_converts_the_prices_with_the_configured_rates() -> None:
    lines = _by_service(
        estimate.build_estimate(PLAN, _settings(dataforseo_eur_per_usd=Decimal("0.5")))
    )

    assert lines[google_ai_mode.POST_PATH].unit_cost == Decimal("0.0006")


def test_estimate_counts_claude_only_as_a_quantity() -> None:
    claude = _by_service(estimate.build_estimate(PLAN, _settings()))[estimate.CLAUDE_SERVICE]

    assert (claude.calls, claude.cost) == (60, Decimal(0))


def test_estimate_prices_only_the_apify_measurements_that_still_run() -> None:
    lines = estimate.build_estimate(PLAN, _settings())

    apify = [line.service for line in lines if line.source == Source.APIFY.value]

    assert apify == ["google_images", "apple_maps", "bing_maps"]


def test_estimate_prices_the_google_places_requests_by_tier() -> None:
    settings = _settings(google_eur_per_usd=Decimal("0.5"))

    lines = [line for line in estimate.build_estimate(PLAN, settings) if line.source == "places"]
    by_tier = {line.service: line for line in lines}

    details = by_tier["place_details_enterprise_atmosphere"]
    assert (details.calls, details.cost) == (4, Decimal("0.05"))
    assert by_tier["text_search_enterprise_atmosphere"].cost == Decimal("0.020")
    assert by_tier["text_search_enterprise"].cost == Decimal("0.0175")
    ids_only = by_tier["text_search_essentials_ids_only"]
    assert (ids_only.calls, ids_only.cost) == (68, Decimal(0))


def test_estimate_has_no_places_lines_without_a_google_key() -> None:
    lines = estimate.build_estimate(PLAN, _settings().model_copy(update={"google_api_key": None}))

    assert not [line for line in lines if line.source == "places"]


OPENAI_PLAN = replace(PLAN, question_bytes=100)


def _openai_lines(settings: Settings) -> list[AuditEstimate]:
    lines = estimate.build_estimate(OPENAI_PLAN, settings)
    return [line for line in lines if line.source == Source.CHATGPT.value]


def test_estimate_prices_the_openai_interface_for_every_question_at_the_worst_case() -> None:
    settings = _settings(openai_api_key=SecretStr("key"), openai_eur_per_usd=Decimal(2))

    (line,) = _openai_lines(settings)

    assert line.service == openai_price.OPENAI_SERVICE
    assert line.calls == 60
    assert line.unit_cost == Decimal("0.0018348")
    assert line.cost == Decimal("0.110088")


def test_estimate_grows_with_the_longest_question() -> None:
    settings = _settings(openai_api_key=SecretStr("key"))
    longer = replace(OPENAI_PLAN, question_bytes=OPENAI_PLAN.question_bytes + 1000)

    (short,) = _openai_lines(settings)
    (long,) = [
        line
        for line in estimate.build_estimate(longer, settings)
        if line.source == Source.CHATGPT.value
    ]

    assert long.cost - short.cost == Decimal("0.15") * 1000 * 60 / 1_000_000


def test_estimate_has_no_openai_line_without_an_openai_key() -> None:
    assert _openai_lines(_settings()) == []


def test_estimate_leaves_out_a_model_without_a_price_and_names_it(tmp_path: Path) -> None:
    path = routes_with(tmp_path, PRICE_LINE, 'gpt-5: {input: "1", output: "2"}')
    settings = _settings(openai_api_key=SecretStr("key"), routes_path=path)

    assert _openai_lines(settings) == []
    assert estimate.unpriced_steps(settings) == ["openai_chat_completions (gpt-4o-mini)"]


def test_a_priced_model_is_not_reported_as_unpriced() -> None:
    assert estimate.unpriced_steps(_settings(openai_api_key=SecretStr("key"))) == []


def test_the_missing_price_of_the_openai_interface_is_named_in_the_verdict(tmp_path: Path) -> None:
    path = routes_with(tmp_path, PRICE_LINE, 'gpt-5: {input: "1", output: "2"}')
    settings = _settings(openai_api_key=SecretStr("key"), routes_path=path)
    lines = estimate.build_estimate(OPENAI_PLAN, settings)

    verdict = estimate.judge(lines, _balances("5", "6"), settings, _gate())

    assert verdict.note.endswith("Preis nicht hinterlegt: openai_chat_completions (gpt-4o-mini)")


def test_judge_refuses_when_the_openai_estimate_exceeds_the_monthly_cap_of_chatgpt() -> None:
    settings = _settings(openai_api_key=SecretStr("key"))
    lines = _openai_lines(settings)
    ledger = MagicMock()
    ledger.month_source_cost.return_value = Decimal("4.98")

    verdict = estimate.judge(lines, _balances(None, None), settings, _gate(ledger))

    assert verdict.status == estimate.INSUFFICIENT
    assert "chatgpt: Voranschlag 0,06 EUR, Monatsgrenze lässt 0,02 EUR übrig" in verdict.note


def test_judge_covers_the_openai_estimate_when_the_monthly_cap_leaves_enough() -> None:
    settings = _settings(openai_api_key=SecretStr("key"))
    ledger = MagicMock()
    ledger.month_source_cost.return_value = Decimal("4.90")

    verdict = estimate.judge(
        _openai_lines(settings), _balances(None, None), settings, _gate(ledger)
    )

    assert verdict.status == estimate.COVERED


def test_judge_names_the_paid_steps_that_have_no_price(tmp_path: Path) -> None:
    routes = tmp_path / "messwege.yaml"
    text = (CATALOGUE_PATH.parent / "messwege.yaml").read_text(encoding="utf-8")
    routes.write_text(text.replace('price_usd_per_task: "0.0006", ', ""), encoding="utf-8")
    settings = _settings(routes_path=routes)
    lines = estimate.build_estimate(PLAN, settings)

    verdict = estimate.judge(lines, _balances("5", "6"), settings, _gate())

    assert verdict.status == estimate.COVERED
    assert verdict.note.endswith("Preis nicht hinterlegt: " + estimate.ads.GOOGLE_ADS_POST_PATH)


def test_judge_covers_the_estimate_when_the_credit_is_enough() -> None:
    lines = estimate.build_estimate(PLAN, _settings())

    verdict = estimate.judge(lines, _balances("5", "6"), _settings(), _gate())

    assert verdict.status == estimate.COVERED


def test_judge_refuses_when_the_dataforseo_credit_is_too_small() -> None:
    lines = estimate.build_estimate(PLAN, _settings())

    verdict = estimate.judge(lines, _balances("0.5", "6"), _settings(), _gate())

    assert verdict.status == estimate.INSUFFICIENT
    assert verdict.note.startswith("dataforseo: Voranschlag 1,00 EUR, Guthaben 0,50 EUR")


def test_judge_accepts_a_credit_equal_to_the_estimate_and_refuses_one_cent_less() -> None:
    lines = [_line("dataforseo", "1")]

    covered = estimate.judge(lines, _balances("1", None), _settings(), _gate())
    short = estimate.judge(lines, _balances("0.99", None), _settings(), _gate())

    assert (covered.status, short.status) == (estimate.COVERED, estimate.INSUFFICIENT)


def test_judge_refuses_when_the_monthly_cap_leaves_too_little() -> None:
    ledger = MagicMock()
    ledger.month_source_cost.return_value = Decimal("9.9")

    verdict = estimate.judge(
        [_line("apify", "0.5")], _balances(None, None), _settings(), _gate(ledger)
    )

    assert verdict.status == estimate.INSUFFICIENT
    assert "Monatsgrenze lässt 0,10 EUR übrig" in verdict.note


def test_judge_marks_an_unreadable_balance_as_unchecked() -> None:
    lines = estimate.build_estimate(PLAN, _settings())
    balances = _balances(None, "6", ["DataForSEO: HTTPStatusError"])

    verdict = estimate.judge(lines, balances, _settings(), _gate())

    assert verdict.status == estimate.UNCHECKED
    assert "DataForSEO: HTTPStatusError" in verdict.note


@respx.mock
async def test_read_balances_reads_both_providers() -> None:
    respx.get(estimate.DATAFORSEO_USER_DATA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": 20000,
                "tasks": [{"status_code": 20000, "result": [{"money": {"balance": 0.9948}}]}],
            },
        )
    )
    respx.get(estimate.APIFY_LIMITS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "limits": {"maxMonthlyUsageUsd": 7},
                    "current": {"monthlyUsageUsd": 0.5},
                }
            },
        )
    )
    settings = _settings(
        dataforseo_login="login",
        dataforseo_password=SecretStr("password"),
        apify_token=SecretStr("token"),
    )

    balances = await estimate.read_balances(settings)

    assert balances == _balances("0.9948", "6.5")


@respx.mock
async def test_read_balances_reports_a_provider_that_does_not_answer() -> None:
    respx.get(estimate.DATAFORSEO_USER_DATA_URL).mock(return_value=httpx.Response(401))
    settings = _settings(dataforseo_login="login", dataforseo_password=SecretStr("password"))

    balances = await estimate.read_balances(settings)

    assert balances.dataforseo_usd is None
    assert balances.unreadable == ["DataForSEO: DataForSEO rejected the request with HTTP 401"]
