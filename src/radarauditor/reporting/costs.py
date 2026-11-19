from decimal import Decimal

from pydantic import BaseModel

from radarauditor.db.models import AuditCost

SUBSCRIPTION_SOURCE = "claude"


class CostLine(BaseModel):
    source: str
    service: str
    calls: int
    unit_price: Decimal | None
    total: Decimal


class UsageLine(BaseModel):
    source: str
    service: str
    calls: int
    tokens_in: int
    tokens_out: int


class CostStatement(BaseModel):
    paid: list[CostLine]
    total: Decimal
    usage: list[UsageLine]


def cost_statement(
    costs: list[AuditCost], sources: dict[str, str], services: dict[str, str]
) -> CostStatement:
    paid: list[CostLine] = []
    usage: list[UsageLine] = []
    for row in costs:
        source = sources.get(row.source, row.source)
        service = services.get(row.service, row.service)
        if row.source == SUBSCRIPTION_SOURCE:
            usage.append(
                UsageLine(
                    source=source,
                    service=service,
                    calls=row.calls,
                    tokens_in=row.tokens_in,
                    tokens_out=row.tokens_out,
                )
            )
            continue
        paid.append(
            CostLine(
                source=source,
                service=service,
                calls=row.calls,
                unit_price=row.cost / row.calls if row.calls else None,
                total=row.cost,
            )
        )
    return CostStatement(
        paid=paid, total=sum((line.total for line in paid), Decimal(0)), usage=usage
    )
