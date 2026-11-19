import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import (
    DailyCapReachedError,
    MonthlyCostCapReachedError,
    SourceDisabledError,
    StorageError,
)

logger = logging.getLogger(__name__)

QuotaConsumer = Callable[[str, int], bool]
RawSink = Callable[[str, bytes], None]


class CostLedger(Protocol):
    def month_source_cost(self, source: str) -> Decimal: ...

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal: ...

    def add_audit_cost(
        self,
        audit_id: UUID,
        source: str,
        service: str,
        *,
        calls: int,
        cost: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None: ...


class SourceGate:
    def __init__(
        self,
        sources: SourcesSettings,
        consume: QuotaConsumer,
        ledger: CostLedger | None = None,
        audit_id: UUID | None = None,
        raw_sink: RawSink | None = None,
    ) -> None:
        self._audit_id = audit_id
        self._raw_sink = raw_sink
        self._sources = sources
        self._consume = consume
        self._ledger = ledger
        self._exhausted: set[Source] = set()
        self._cost_capped: set[Source] = set()
        self._month_cost: dict[Source, Decimal] = {}

    @property
    def exhausted(self) -> frozenset[Source]:
        return frozenset(self._exhausted)

    @property
    def cost_capped(self) -> frozenset[Source]:
        return frozenset(self._cost_capped)

    def remaining_cost(self, source: Source) -> Decimal | None:
        cap = self._sources.of(source).monthly_cost_cap_eur
        if cap is None or self._ledger is None:
            return None
        return max(cap - self._ledger.month_source_cost(source.value), Decimal(0))

    def is_enabled(self, source: Source) -> bool:
        return self._sources.of(source).enabled

    def acquire(self, source: Source, *, counts_cost: bool = True) -> None:
        setting = self._sources.of(source)
        if not setting.enabled:
            raise SourceDisabledError(source.value, "switched off in the app settings")
        if counts_cost:
            self._check_cost_cap(source)
        if source in self._exhausted or not self._consume(source.value, setting.daily_cap):
            self._mark_exhausted(source, setting.daily_cap)
            raise DailyCapReachedError(source.value, f"daily cap of {setting.daily_cap} reached")

    def try_acquire(self, source: Source, *, counts_cost: bool = True) -> bool:
        try:
            self.acquire(source, counts_cost=counts_cost)
        except (SourceDisabledError, DailyCapReachedError, MonthlyCostCapReachedError):
            return False
        return True

    def acquire_up_to(self, source: Source, count: int) -> int:
        for granted in range(count):
            try:
                self.acquire(source)
            except (DailyCapReachedError, MonthlyCostCapReachedError):
                return granted
        return count

    def record_cost(
        self,
        source: Source,
        cost: Decimal,
        *,
        service: str,
        calls: int = 1,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        if self._ledger is None:
            return
        self.record_usage(
            source,
            service,
            calls=calls,
            cost=cost,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        if cost <= 0:
            return
        total = self._ledger.add_source_cost(source.value, cost)
        self._month_cost[source] = total
        cap = self._sources.of(source).monthly_cost_cap_eur
        if cap is not None and total >= cap:
            self._mark_cost_capped(source, cap)

    def record_usage(
        self,
        source: Source,
        service: str,
        *,
        calls: int = 1,
        cost: Decimal = Decimal(0),
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        if self._ledger is None or self._audit_id is None:
            return
        self._ledger.add_audit_cost(
            self._audit_id,
            source.value,
            service,
            calls=calls,
            cost=cost,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def keep_raw(self, name: str, body: bytes) -> None:
        if self._raw_sink is None:
            return
        try:
            self._raw_sink(name, body)
        except StorageError as error:
            logger.warning("Raw answer %s not stored: %s", name, error)

    def _check_cost_cap(self, source: Source) -> None:
        cap = self._sources.of(source).monthly_cost_cap_eur
        if cap is None or self._ledger is None:
            return
        if source not in self._month_cost:
            self._month_cost[source] = self._ledger.month_source_cost(source.value)
        if source in self._cost_capped or self._month_cost[source] >= cap:
            self._mark_cost_capped(source, cap)
            raise MonthlyCostCapReachedError(source.value, f"monthly cost cap of {cap} EUR reached")

    def _mark_exhausted(self, source: Source, cap: int) -> None:
        if source not in self._exhausted:
            logger.warning("Daily cap of %d for %s reached", cap, source.value)
        self._exhausted.add(source)

    def _mark_cost_capped(self, source: Source, cap: Decimal) -> None:
        if source not in self._cost_capped:
            logger.warning("Monthly cost cap of %s EUR for %s reached", cap, source.value)
        self._cost_capped.add(source)
