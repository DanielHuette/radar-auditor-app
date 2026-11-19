from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from radarauditor.collectors import ads, estimate
from radarauditor.collectors.paid_rankings import DataForSeoClient, TaskBatch
from radarauditor.collectors.quota import SourceGate
from radarauditor.collectors.snapshot import (
    GoogleAd,
    GoogleAdsSnapshot,
    MetaAd,
    MetaAdsSnapshot,
    RawAudit,
)
from radarauditor.collectors.validation import (
    GOOGLE_ADS_FORMAT_REASON,
    META_ADS_FORMAT_REASON,
    validate_google_ads,
    validate_meta_ads,
)
from radarauditor.config import Settings
from radarauditor.db.models import CheckOutcome
from radarauditor.errors import CollectionError, ConfigurationError
from radarauditor.scoring.checks.p_werbung import run

NOW = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
ROUTES = Path(__file__).resolve().parents[1] / "config" / "messwege.yaml"
DOMAIN = "dachdecker-muster.example"


def _item(**changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "ads_search",
        "advertiser_id": "AR1",
        "creative_id": "CR1",
        "title": "Dachdeckerei Muster",
        "format": "text",
        "verified": True,
        "first_shown": "2026-09-01",
        "last_shown": "2026-11-10",
        "url": "https://adstransparency.google.com/x",
    }
    return item | changes


def _row(items: list[Any], **changes: Any) -> dict[str, Any]:
    return {"location_code": 2276, "items_count": len(items), "items": items} | changes


def _batch(rows: list[dict[str, Any]] | None, failure: str | None = None) -> TaskBatch:
    if failure is not None:
        return TaskBatch(results={}, failed={0: failure})
    return TaskBatch(results={0: rows or []}, failed={})


def test_the_google_ads_row_is_read_into_ads_with_the_documented_fields() -> None:
    snapshot = ads.google_ads_from_rows([_row([_item(), _item(creative_id="CR2")])], DOMAIN, NOW)

    assert [ad.creative_id for ad in snapshot.ads] == ["CR1", "CR2"]
    assert snapshot.ads[0] == GoogleAd(
        advertiser_id="AR1",
        creative_id="CR1",
        advertiser_name="Dachdeckerei Muster",
        format="text",
        verified=True,
        first_shown="2026-09-01",
        last_shown="2026-11-10",
        url="https://adstransparency.google.com/x",
    )
    assert snapshot.format_problem is None
    assert snapshot.returned_location_code == 2276
    assert snapshot.target == DOMAIN
    assert snapshot.window_days == 90


def test_an_empty_item_list_is_a_valid_answer_without_ads() -> None:
    snapshot = ads.google_ads_from_rows([_row([])], DOMAIN, NOW)

    assert snapshot.ads == []
    assert snapshot.format_problem is None


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ([], "kein Ergebnisobjekt"),
        ([_row(["text"], items_count=1)], "kein Objekt"),
        ([_row([_item(type="other")])], "Typ 'other'"),
        ([_row([_item(creative_id=None)])], "ohne advertiser_id oder creative_id"),
        ([_row([_item()], items_count=2)], "items_count 2 passt nicht zu 1"),
        ([{"location_code": 2276, "items": "x"}], "keine Liste"),
    ],
)
def test_a_deviating_answer_format_is_named_instead_of_guessed(
    rows: list[dict[str, Any]], problem: str
) -> None:
    snapshot = ads.google_ads_from_rows(rows, DOMAIN, NOW)

    assert snapshot.format_problem is not None
    assert problem in snapshot.format_problem


def test_at_most_three_format_problems_are_kept() -> None:
    items = [_item(type=f"t{index}") for index in range(5)]

    snapshot = ads.google_ads_from_rows([_row(items)], DOMAIN, NOW)

    assert snapshot.format_problem is not None
    assert snapshot.format_problem.count(";") == 2


def test_raw_items_are_limited() -> None:
    items = [_item(creative_id=f"C{index}") for index in range(25)]

    snapshot = ads.google_ads_from_rows([_row(items)], DOMAIN, NOW)

    assert len(snapshot.raw_items) == ads.RAW_ITEM_LIMIT
    assert len(snapshot.ads) == 25


async def test_google_ads_are_requested_for_the_domain_in_germany_for_ninety_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(client: Any, base: str, tasks: list[dict[str, Any]], **kwargs: Any) -> Any:
        seen.update(base=base, tasks=tasks, kwargs=kwargs)
        return _batch([_row([_item()])])

    monkeypatch.setattr(ads, "run_tasks", fake_run)

    snapshot = await ads.collect_google_ads(MagicMock(spec=DataForSeoClient), DOMAIN, NOW)

    assert seen["base"] == "/v3/serp/google/ads_search"
    assert seen["tasks"] == [
        {
            "target": DOMAIN,
            "location_code": 2276,
            "date_from": "2026-08-17",
            "date_to": "2026-11-15",
        }
    ]
    assert len(snapshot.ads) == 1


async def test_a_failed_google_ads_task_is_an_error_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(*args: Any, **kwargs: Any) -> TaskBatch:
        return _batch(None, "40501 invalid field")

    monkeypatch.setattr(ads, "run_tasks", fake_run)

    with pytest.raises(CollectionError, match="40501 invalid field"):
        await ads.collect_google_ads(MagicMock(spec=DataForSeoClient), DOMAIN, NOW)


def _snapshot(**changes: Any) -> GoogleAdsSnapshot:
    base = GoogleAdsSnapshot(
        target=DOMAIN,
        window_days=90,
        location_code=2276,
        returned_location_code=2276,
        ads=[GoogleAd(advertiser_id="A", creative_id="C", advertiser_name="Muster")],
        measured_at=NOW,
    )
    return base.model_copy(update=changes)


def test_a_clean_google_ads_snapshot_is_valid() -> None:
    record = validate_google_ads(_snapshot(), DOMAIN)

    assert record.valid is True
    assert record.step == "ads_google"
    assert record.record_count == 1


@pytest.mark.parametrize(
    ("changes", "host", "expected"),
    [
        ({"format_problem": "items ist keine Liste"}, DOMAIN, GOOGLE_ADS_FORMAT_REASON),
        ({"returned_location_code": 2840}, DOMAIN, "Ort 2840, angefragt war 2276"),
        ({}, "andere-firma.example", "Geprüft wurde dachdecker-muster.example"),
        ({}, None, "angefragt war None"),
    ],
)
def test_a_google_ads_snapshot_that_does_not_fit_the_request_is_invalid(
    changes: dict[str, Any], host: str | None, expected: str
) -> None:
    record = validate_google_ads(_snapshot(**changes), host)

    assert record.valid is False
    assert expected in record.reason


def test_the_www_prefix_does_not_make_the_target_a_different_site() -> None:
    assert validate_google_ads(_snapshot(), f"www.{DOMAIN}").valid is True


def test_a_failed_supplier_status_makes_the_google_ads_record_invalid() -> None:
    assert validate_google_ads(_snapshot(), DOMAIN, "failed").valid is False


def _meta(confirmed: bool, **changes: Any) -> MetaAdsSnapshot:
    base = MetaAdsSnapshot(
        searched_name="Dachdeckerei Muster",
        actor="apify/facebook-ads-scraper",
        confirmed=confirmed,
        items_total=3,
        measured_at=NOW,
    )
    return base.model_copy(update=changes)


def test_an_unconfirmed_meta_format_is_never_valid() -> None:
    record = validate_meta_ads(_meta(False))

    assert record.valid is False
    assert META_ADS_FORMAT_REASON in record.reason
    assert (
        META_ADS_FORMAT_REASON
        == "nicht gemessen: Ausgabeformat noch nicht an echtem Abruf bestätigt"
    )


def test_a_confirmed_meta_snapshot_is_valid() -> None:
    assert validate_meta_ads(_meta(True)).valid is True


def test_the_configured_meta_route_is_unconfirmed_and_has_the_name_placeholder() -> None:
    route = ads.load_meta_route(ROUTES)

    assert route.confirmed is False
    assert "{name}" in route.input_url
    assert route.actor == "apify/facebook-ads-scraper"


def _routes(tmp_path: Path, extra: str) -> Path:
    text = ROUTES.read_text(encoding="utf-8")
    start = text.index("  ads_meta:")
    block = (
        "  ads_meta:\n    label: Meta\n    routes:\n      - supplier: apify\n"
        '        endpoint: "apify/facebook-ads-scraper"\n        key: APIFY_TOKEN\n'
        "        built: true\n" + extra
    )
    path = tmp_path / "messwege.yaml"
    path.write_text(text[:start] + block, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "extra",
    [
        '        input_url: "https://x/?q=fixed"\n        field_mapping: {}\n'
        "        bestaetigt: false\n",
        '        input_url: "https://x/?q={name}"\n        bestaetigt: false\n',
        '        input_url: "https://x/?q={name}"\n        field_mapping: {}\n',
        '        input_url: "https://x/?q={name}"\n        field_mapping: {page_name: a}\n'
        "        bestaetigt: true\n",
    ],
)
def test_a_meta_route_with_missing_or_inconsistent_fields_is_a_configuration_error(
    tmp_path: Path, extra: str
) -> None:
    with pytest.raises(ConfigurationError):
        ads.load_meta_route(_routes(tmp_path, extra))


def _route(confirmed: bool) -> ads.MetaRoute:
    return ads.MetaRoute(
        actor="apify/facebook-ads-scraper",
        input_url="https://x/?q={name}",
        field_mapping={"page_name": "pageInfo.name", "ad_id": "adArchiveID"},
        confirmed=confirmed,
    )


def _meta_rows(*pages: str) -> list[dict[str, Any]]:
    return [
        {"pageInfo": {"name": page}, "adArchiveID": 100 + index} for index, page in enumerate(pages)
    ]


def test_meta_ads_are_matched_to_the_business_by_page_name() -> None:
    items = _meta_rows("Dachdeckerei Muster GmbH", "Dach Schmidt", "Muster Dachdeckerei")

    own, others = ads._meta_ads(items, _route(True), "Dachdeckerei Muster")

    assert [ad.page_name for ad in own] == ["Dachdeckerei Muster GmbH", "Muster Dachdeckerei"]
    assert [ad.ad_id for ad in own] == ["100", "102"]
    assert others == ["Dach Schmidt"]


def test_an_item_without_a_page_name_is_skipped() -> None:
    own, others = ads._meta_ads([{"adArchiveID": 1}], _route(True), "Dachdeckerei Muster")

    assert (own, others) == ([], [])


@pytest.fixture
def apify_settings(collector_settings: Settings) -> Settings:
    return collector_settings.model_copy(update={"apify_token": SecretStr("token")})


async def test_an_unconfirmed_meta_route_makes_one_call_and_assigns_no_ads(
    apify_settings: Settings, open_gate: SourceGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_actor(
        client: Any, token: Any, actor: str, payload: dict[str, Any], *args: Any, **options: Any
    ) -> list[dict[str, Any]]:
        calls.append(payload)
        return _meta_rows("Dachdeckerei Muster", "Dach Schmidt")

    monkeypatch.setattr(ads, "run_actor", fake_actor)

    snapshot = await ads.collect_meta_ads(
        apify_settings, open_gate, _route(False), "Dachdeckerei Muster", ["Dach Schmidt"], NOW
    )

    assert len(calls) == 1
    assert calls[0]["startUrls"] == [{"url": "https://x/?q=Dachdeckerei+Muster"}]
    assert snapshot.own_ads == []
    assert snapshot.competitor_counts == {}
    assert snapshot.items_total == 2
    assert snapshot.confirmed is False


async def test_a_confirmed_meta_route_counts_the_ads_of_every_competitor(
    apify_settings: Settings, open_gate: SourceGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {
        "Dachdeckerei+Muster": _meta_rows("Dachdeckerei Muster"),
        "Dach+Schmidt": _meta_rows("Dach Schmidt", "Dach Schmidt"),
        "Bau+Krause": [],
    }

    async def fake_actor(
        client: Any, token: Any, actor: str, payload: dict[str, Any], *args: Any, **options: Any
    ) -> list[dict[str, Any]]:
        return answers[payload["startUrls"][0]["url"].rsplit("=", 1)[1]]

    monkeypatch.setattr(ads, "run_actor", fake_actor)

    snapshot = await ads.collect_meta_ads(
        apify_settings,
        open_gate,
        _route(True),
        "Dachdeckerei Muster",
        ["Dach Schmidt", "Bau Krause"],
        NOW,
    )

    assert [ad.page_name for ad in snapshot.own_ads] == ["Dachdeckerei Muster"]
    assert snapshot.competitor_counts == {"Dach Schmidt": 2, "Bau Krause": 0}


async def test_meta_ads_without_an_apify_token_fail_with_a_clear_message(
    collector_settings: Settings, open_gate: SourceGate
) -> None:
    settings = collector_settings.model_copy(update={"apify_token": None})

    with pytest.raises(CollectionError, match="Apify"):
        await ads.collect_meta_ads(settings, open_gate, _route(True), "X", [], NOW)


def test_p1_reports_the_ads_found_in_the_window(raw_audit_full: RawAudit) -> None:
    snapshot = _snapshot(
        ads=[
            GoogleAd(
                advertiser_id="A",
                creative_id="C1",
                advertiser_name="Muster",
                format="text",
                first_shown="2026-09-01",
                last_shown="2026-10-01",
            ),
            GoogleAd(
                advertiser_id="A",
                creative_id="C2",
                advertiser_name="Muster",
                format="image",
                first_shown="2026-08-20",
                last_shown="2026-11-10",
            ),
        ]
    )

    outcome, measured = run("p1", raw_audit_full.model_copy(update={"ads_google": snapshot}), {})

    assert outcome is CheckOutcome.PASSED
    assert measured["shows_ads"] is True
    assert measured["ads_found"] == 2
    assert measured["advertisers"] == ["Muster"]
    assert measured["formats"] == ["image", "text"]
    assert measured["first_shown"] == "2026-08-20"
    assert measured["last_shown"] == "2026-11-10"


def test_p1_without_ads_in_the_window_says_so(raw_audit_full: RawAudit) -> None:
    raw = raw_audit_full.model_copy(update={"ads_google": _snapshot(ads=[])})

    outcome, measured = run("p1", raw, {})

    assert outcome is CheckOutcome.PASSED
    assert measured["shows_ads"] is False
    assert measured["first_shown"] is None


def test_p1_without_a_measurement_is_not_measured_and_says_why(raw_audit_full: RawAudit) -> None:
    assert run("p1", raw_audit_full, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "ads_google_not_measured"},
    )


def test_p2_reports_the_own_ads_and_the_competitor_counts(raw_audit_full: RawAudit) -> None:
    snapshot = _meta(
        True,
        own_ads=[MetaAd(ad_id="1", page_name="Muster"), MetaAd(ad_id="2", page_name="Muster")],
        competitor_counts={"Dach Schmidt": 4},
    )

    outcome, measured = run("p2", raw_audit_full.model_copy(update={"ads_meta": snapshot}), {})

    assert outcome is CheckOutcome.PASSED
    assert measured["shows_ads"] is True
    assert measured["own_ads"] == 2
    assert measured["own_pages"] == ["Muster"]
    assert measured["competitor_ad_counts"] == {"Dach Schmidt": 4}


def test_p2_without_a_measurement_is_not_measured_and_says_why(raw_audit_full: RawAudit) -> None:
    assert run("p2", raw_audit_full, {}) == (
        CheckOutcome.NOT_MEASURABLE,
        {"reason": "ads_meta_not_measured"},
    )


CONFIRMED_META = (
    '        input_url: "https://x/?q={name}"\n'
    "        field_mapping: {page_name: a, ad_id: b}\n"
    "        bestaetigt: true\n"
)


def _with_routes(settings: Settings, path: Path) -> Settings:
    return settings.model_copy(update={"routes_path": path})


def test_the_estimate_prices_the_google_ads_call_from_the_price_list(settings: Settings) -> None:
    lines = estimate.build_estimate(estimate.Plan(1, 1, 1, 1, 1), _with_routes(settings, ROUTES))

    google = next(line for line in lines if line.service == ads.GOOGLE_ADS_POST_PATH)

    assert (google.calls, google.unit_cost, google.cost) == (
        1,
        Decimal("0.0006"),
        Decimal("0.0006"),
    )
    assert ads.META_SERVICE not in {line.service for line in lines}


def test_the_estimate_counts_one_meta_call_for_us_and_one_for_each_competitor(
    settings: Settings, tmp_path: Path
) -> None:
    path = _routes(tmp_path, CONFIRMED_META + '        price_usd_per_run: "0.05"\n')

    lines = estimate.build_estimate(estimate.Plan(1, 1, 3, 1, 1), _with_routes(settings, path))

    meta = next(line for line in lines if line.service == ads.META_SERVICE)

    assert (meta.calls, meta.unit_cost, meta.cost) == (4, Decimal("0.05"), Decimal("0.20"))


def test_a_paid_call_without_a_price_is_left_out_and_named_instead_of_costing_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    path = _routes(tmp_path, CONFIRMED_META)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('price_usd_per_task: "0.0006", ', ""), encoding="utf-8")
    unpriced = _with_routes(settings, path)

    lines = estimate.build_estimate(estimate.Plan(1, 1, 3, 1, 1), unpriced)

    services = {line.service for line in lines}
    assert services.isdisjoint({ads.GOOGLE_ADS_POST_PATH, ads.META_SERVICE})
    assert estimate.unpriced_steps(unpriced) == [ads.GOOGLE_ADS_POST_PATH, ads.META_SERVICE]


def test_the_price_of_a_route_must_be_a_positive_number(tmp_path: Path) -> None:
    path = _routes(tmp_path, CONFIRMED_META + '        price_usd_per_run: "gratis"\n')
    with pytest.raises(ConfigurationError):
        ads.load_meta_route(path)
    path = _routes(tmp_path, CONFIRMED_META + '        price_usd_per_run: "0"\n')
    with pytest.raises(ConfigurationError):
        ads.load_meta_route(path)


def test_the_shipped_routes_carry_a_dated_source_for_the_google_ads_price() -> None:
    assert ads.load_google_ads_price(ROUTES) == Decimal("0.0006")
    assert ads.load_meta_route(ROUTES).price_usd is None
