from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from radarauditor.collectors import openai_price
from radarauditor.collectors.quota import SourceGate
from radarauditor.config import Settings
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import ConfigurationError, MonthlyCostCapReachedError
from radarauditor.feedback.routes import load_measurements
from radarauditor.reporting import report

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "config" / "messwege.yaml"
PRICE_LINE = 'gpt-4o-mini: {input: "0.15", output: "0.60"}'
PRICE = openai_price.TokenPrice(Decimal("0.15"), Decimal("0.60"))
SOURCE_ENTRY = 'price_source: "https://developers.openai.com/api/docs/pricing"'
DATE_ENTRY = 'price_read_on: "2026-11-15"\n          usd_per_million_tokens'


def routes_with(tmp_path: Path, old: str, new: str) -> Path:
    text = ROUTES.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path = tmp_path / "messwege.yaml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_the_shipped_price_of_the_default_model_is_the_standard_price_of_the_page() -> None:
    assert openai_price.load_token_price(ROUTES, "gpt-4o-mini") == PRICE


def test_the_default_model_of_the_settings_has_a_price() -> None:
    default_model = Settings.model_fields["openai_model"].default

    assert openai_price.load_token_price(ROUTES, default_model) is not None


def test_the_shipped_price_names_its_source_and_the_day_it_was_read() -> None:
    routes = load_measurements(ROUTES)["assistants"].variants["chatgpt"]
    route = next(item for item in routes if item.supplier == "openai_api")
    extra = route.model_extra or {}

    assert extra["price_source"] == "https://developers.openai.com/api/docs/pricing"
    assert extra["price_read_on"] == "2026-11-15"


def test_a_model_that_is_not_in_the_price_list_has_no_price() -> None:
    assert openai_price.load_token_price(ROUTES, "gpt-x") is None


@pytest.mark.parametrize(
    ("old", "new"),
    [(SOURCE_ENTRY, ""), (DATE_ENTRY, "usd_per_million_tokens")],
)
def test_a_price_without_source_or_reading_day_is_not_used(
    tmp_path: Path, old: str, new: str
) -> None:
    path = routes_with(tmp_path, old, new)

    assert openai_price.load_token_price(path, "gpt-4o-mini") is None


def test_a_file_without_the_assistants_measurement_has_no_price(tmp_path: Path) -> None:
    path = tmp_path / "messwege.yaml"
    path.write_text("suppliers: {}\nchecks: {}\nmeasurements: {}\n", encoding="utf-8")

    assert openai_price.load_token_price(path, "gpt-4o-mini") is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('input: "0.15"', 'input: "gratis"'),
        ('input: "0.15"', 'input: "0"'),
        ('output: "0.60"', 'output: "-1"'),
        ('output: "0.60"', 'output: "NaN"'),
        (PRICE_LINE, 'gpt-4o-mini: {input: "0.15"}'),
        (PRICE_LINE, 'gpt-4o-mini: "0.15"'),
    ],
)
def test_a_malformed_price_is_a_configuration_error(tmp_path: Path, old: str, new: str) -> None:
    path = routes_with(tmp_path, old, new)

    with pytest.raises(ConfigurationError, match="gpt-4o-mini"):
        openai_price.load_token_price(path, "gpt-4o-mini")


def test_a_call_costs_the_reported_tokens_at_the_listed_prices() -> None:
    assert openai_price.call_cost_usd(PRICE, 1000, 2000) == Decimal("0.00135")


def test_a_million_tokens_cost_exactly_the_listed_price() -> None:
    assert openai_price.call_cost_usd(PRICE, 1_000_000, 0) == Decimal("0.15")
    assert openai_price.call_cost_usd(PRICE, 0, 1_000_000) == Decimal("0.60")
    assert openai_price.call_cost_usd(PRICE, 0, 0) == 0


def test_the_worst_case_call_counts_question_bytes_message_overhead_and_the_output_limit() -> None:
    assert openai_price.max_call_cost_usd(PRICE, 100) == Decimal("0.0009174")


class Ledger:
    def __init__(self, spent: Decimal) -> None:
        self.spent = spent
        self.audit_rows: list[tuple[str, str, int, Decimal, int, int]] = []

    def month_source_cost(self, source: str) -> Decimal:
        return self.spent

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        self.spent += cost
        return self.spent

    def add_audit_cost(
        self,
        audit_id: object,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.audit_rows.append((source, service, calls, cost, tokens_in, tokens_out))


def _gate(ledger: Ledger) -> SourceGate:
    return SourceGate(SourcesSettings(), lambda source, cap: True, ledger, uuid4())


def test_the_cost_of_a_paid_call_is_booked_with_its_tokens() -> None:
    ledger = Ledger(Decimal(0))

    _gate(ledger).record_cost(
        Source.CHATGPT, Decimal("0.001"), service="x", tokens_in=7, tokens_out=9
    )

    assert ledger.audit_rows == [("chatgpt", "x", 1, Decimal("0.001"), 7, 9)]


def test_the_cost_cap_of_chatgpt_blocks_at_the_limit_and_not_a_cent_before() -> None:
    below = _gate(Ledger(Decimal("4.99")))
    at_limit = _gate(Ledger(Decimal(5)))

    below.acquire(Source.CHATGPT)
    with pytest.raises(MonthlyCostCapReachedError):
        at_limit.acquire(Source.CHATGPT)


def test_a_free_route_of_chatgpt_ignores_the_cost_cap() -> None:
    gate = _gate(Ledger(Decimal(5)))

    gate.acquire(Source.CHATGPT, counts_cost=False)

    assert gate.try_acquire(Source.CHATGPT, counts_cost=False) is True
    assert gate.try_acquire(Source.CHATGPT) is False


def test_the_statement_of_costs_names_the_openai_interface_in_plain_words() -> None:
    labels = report.load_labels(ROOT / "templates")

    service = labels.cost_services[openai_price.OPENAI_SERVICE]

    assert labels.cost_sources["chatgpt"] == "OpenAI-Schnittstelle (Antworten von ChatGPT)"
    assert service == "Fragen an ChatGPT, abgerechnet nach Verbrauch"
