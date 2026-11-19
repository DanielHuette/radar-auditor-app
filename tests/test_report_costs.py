from decimal import Decimal
from uuid import uuid4

from radarauditor.db.models import AuditCost
from radarauditor.reporting.costs import CostLine, UsageLine, cost_statement

AUDIT = uuid4()
SOURCES = {"apify": "Apify", "claude": "Claude"}
SERVICES = {"instagram": "Instagram", "assistants": "Fragen"}


def _row(
    source: str, service: str, calls: int, cost: str, tokens_in: int = 0, tokens_out: int = 0
) -> AuditCost:
    return AuditCost(
        audit_id=AUDIT,
        source=source,
        service=service,
        calls=calls,
        cost=Decimal(cost),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def test_a_paid_row_shows_calls_unit_price_and_total_under_the_german_names() -> None:
    statement = cost_statement([_row("apify", "instagram", 4, "0.0200")], SOURCES, SERVICES)

    assert statement.paid == [
        CostLine(
            source="Apify",
            service="Instagram",
            calls=4,
            unit_price=Decimal("0.005"),
            total=Decimal("0.0200"),
        )
    ]


def test_the_total_is_the_sum_of_the_paid_rows_only() -> None:
    rows = [
        _row("apify", "instagram", 1, "0.0100"),
        _row("dataforseo", "/v3/x", 2, "0.0020"),
        _row("claude", "assistants", 3, "0", 100, 20),
    ]

    assert cost_statement(rows, SOURCES, SERVICES).total == Decimal("0.0120")


def test_claude_is_listed_by_use_without_a_price() -> None:
    statement = cost_statement([_row("claude", "assistants", 3, "0", 100, 20)], SOURCES, SERVICES)

    assert statement.paid == []
    assert statement.usage == [
        UsageLine(source="Claude", service="Fragen", calls=3, tokens_in=100, tokens_out=20)
    ]


def test_an_unknown_source_or_service_keeps_its_key() -> None:
    statement = cost_statement([_row("places", "sku_x", 1, "0.03")], SOURCES, SERVICES)

    assert (statement.paid[0].source, statement.paid[0].service) == ("places", "sku_x")


def test_a_free_call_is_listed_with_a_zero_price() -> None:
    statement = cost_statement([_row("places", "ids_only", 5, "0")], SOURCES, SERVICES)

    assert statement.paid[0].unit_price == Decimal(0)
    assert statement.total == Decimal(0)


def test_a_row_without_calls_has_no_unit_price() -> None:
    statement = cost_statement([_row("apify", "instagram", 0, "0.01")], SOURCES, SERVICES)

    assert statement.paid[0].unit_price is None


def test_no_rows_give_an_empty_statement() -> None:
    statement = cost_statement([], SOURCES, SERVICES)

    assert (statement.paid, statement.usage, statement.total) == ([], [], Decimal(0))
