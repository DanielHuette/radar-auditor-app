"""Tests for the source gate.

Context: Unit tests with an in-memory quota counter. | Input: SourcesSettings. | Output: Assertions.
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from radarauditor.collectors.quota import SourceGate
from radarauditor.db.app_settings import Source, SourcesSettings
from radarauditor.errors import (
    CollectionError,
    DailyCapReachedError,
    MonthlyCostCapReachedError,
    SourceDisabledError,
    StorageError,
)


class Counter:
    """Allows a fixed number of uses per source and records every call."""

    def __init__(self, allowed: int) -> None:
        """Start with the number of allowed uses."""
        self.allowed = allowed
        self.calls: list[tuple[str, int]] = []

    def consume(self, source: str, cap: int) -> bool:
        """Count one use."""
        self.calls.append((source, cap))
        return len(self.calls) <= self.allowed


def test_acquire_counts_with_the_configured_cap() -> None:
    counter = Counter(allowed=5)
    sources = SourcesSettings.model_validate({"chatgpt": {"daily_cap": 3}})
    gate = SourceGate(sources, counter.consume)

    gate.acquire(Source.CHATGPT)

    assert counter.calls == [("chatgpt", 3)]
    assert gate.exhausted == frozenset()


PLACES_OFF = SourcesSettings.model_validate({"places": {"enabled": False}})


def test_disabled_source_is_refused_without_counting() -> None:
    counter = Counter(allowed=5)
    gate = SourceGate(PLACES_OFF, counter.consume)

    with pytest.raises(SourceDisabledError, match="places: switched off") as raised:
        gate.acquire(Source.PLACES)

    assert raised.value.source == "places"
    assert isinstance(raised.value, CollectionError)
    assert counter.calls == []
    assert gate.try_acquire(Source.PLACES) is False
    assert gate.is_enabled(Source.PLACES) is False


def test_exhausted_source_is_refused_without_asking_again() -> None:
    counter = Counter(allowed=1)
    gate = SourceGate(SourcesSettings(), counter.consume)

    assert gate.try_acquire(Source.CLAUDE) is True
    with pytest.raises(DailyCapReachedError, match="daily cap of 200 reached"):
        gate.acquire(Source.CLAUDE)
    assert gate.try_acquire(Source.CLAUDE) is False

    assert len(counter.calls) == 2
    assert gate.exhausted == {Source.CLAUDE}


def test_acquire_up_to_grants_until_the_cap() -> None:
    gate = SourceGate(SourcesSettings(), Counter(allowed=2).consume)

    assert gate.acquire_up_to(Source.GOOGLE_SEARCH, 5) == 2
    assert gate.acquire_up_to(Source.GOOGLE_SEARCH, 5) == 0
    assert gate.exhausted == {Source.GOOGLE_SEARCH}


def test_acquire_up_to_raises_for_a_disabled_source() -> None:
    gate = SourceGate(PLACES_OFF, Counter(allowed=2).consume)

    with pytest.raises(SourceDisabledError):
        gate.acquire_up_to(Source.PLACES, 2)


def test_counter_errors_propagate() -> None:
    def broken(source: str, cap: int) -> bool:
        raise StorageError("consume quota of claude failed: offline")

    gate = SourceGate(SourcesSettings(), broken)

    with pytest.raises(StorageError, match="offline"):
        gate.try_acquire(Source.CLAUDE)


class Ledger:
    """Monthly spend in memory that counts how often it was read."""

    def __init__(self, spent: Decimal) -> None:
        """Start with the spend of the month so far."""
        self.spent = spent
        self.reads = 0
        self.added: list[tuple[str, Decimal]] = []
        self.audited: list[tuple[UUID, str, str, int, Decimal, int, int]] = []

    def month_source_cost(self, source: str) -> Decimal:
        """Return the month's spend."""
        self.reads += 1
        return self.spent

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
    ) -> None:
        self.audited.append((audit_id, source, service, calls, cost, tokens_in, tokens_out))

    def add_source_cost(self, source: str, cost: Decimal) -> Decimal:
        """Add one request's cost and return the month's spend."""
        self.added.append((source, cost))
        self.spent += cost
        return self.spent


def _capped_sources(cap: str) -> SourcesSettings:
    return SourcesSettings.model_validate({"dataforseo": {"monthly_cost_cap_eur": cap}})


def test_cost_cap_reads_the_spend_once_and_follows_the_recorded_costs() -> None:
    ledger = Ledger(Decimal("0.90"))
    counter = Counter(allowed=10)
    gate = SourceGate(_capped_sources("1"), counter.consume, ledger)

    gate.acquire(Source.DATAFORSEO)
    gate.record_cost(Source.DATAFORSEO, Decimal("0.05"), service="serp")
    gate.acquire(Source.DATAFORSEO)
    gate.record_cost(Source.DATAFORSEO, Decimal("0.05"), service="serp")

    with pytest.raises(MonthlyCostCapReachedError, match="monthly cost cap of 1 EUR reached"):
        gate.acquire(Source.DATAFORSEO)
    assert ledger.reads == 1
    assert ledger.added == [("dataforseo", Decimal("0.05")), ("dataforseo", Decimal("0.05"))]
    assert len(counter.calls) == 2
    assert gate.cost_capped == {Source.DATAFORSEO}
    assert gate.exhausted == frozenset()
    assert gate.try_acquire(Source.DATAFORSEO) is False


def test_spent_month_is_refused_before_counting() -> None:
    counter = Counter(allowed=10)
    gate = SourceGate(_capped_sources("50"), counter.consume, Ledger(Decimal(50)))

    assert gate.acquire_up_to(Source.DATAFORSEO, 3) == 0
    assert counter.calls == []
    assert gate.cost_capped == {Source.DATAFORSEO}


def test_sources_without_cost_cap_or_ledger_ignore_costs() -> None:
    ledger = Ledger(Decimal(1000))
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, ledger)

    gate.acquire(Source.GOOGLE_SEARCH)
    gate.record_cost(Source.DATAFORSEO, Decimal(0), service="serp")
    without_ledger = SourceGate(_capped_sources("0"), Counter(allowed=10).consume)
    without_ledger.acquire(Source.DATAFORSEO)
    without_ledger.record_cost(Source.DATAFORSEO, Decimal(5), service="serp")

    assert ledger.reads == 0
    assert ledger.added == []
    assert without_ledger.cost_capped == frozenset()


AUDIT = uuid4()


def test_a_recorded_cost_goes_to_the_month_and_to_the_statement_of_the_audit() -> None:
    ledger = Ledger(Decimal(0))
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, ledger, AUDIT)

    gate.record_cost(Source.APIFY, Decimal("0.02"), service="instagram", calls=3)

    assert ledger.added == [("apify", Decimal("0.02"))]
    assert ledger.audited == [(AUDIT, "apify", "instagram", 3, Decimal("0.02"), 0, 0)]


def test_a_free_request_is_counted_in_the_statement_but_not_in_the_month() -> None:
    ledger = Ledger(Decimal(0))
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, ledger, AUDIT)

    gate.record_cost(Source.PLACES, Decimal(0), service="find_place")

    assert ledger.added == []
    assert ledger.audited == [(AUDIT, "places", "find_place", 1, Decimal(0), 0, 0)]


def test_without_an_audit_the_statement_stays_empty_and_the_month_still_grows() -> None:
    ledger = Ledger(Decimal(0))
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, ledger)

    gate.record_cost(Source.APIFY, Decimal("0.02"), service="instagram")

    assert ledger.added == [("apify", Decimal("0.02"))]
    assert ledger.audited == []


def test_usage_adds_calls_and_tokens_without_a_price() -> None:
    ledger = Ledger(Decimal(0))
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, ledger, AUDIT)

    gate.record_usage(Source.CLAUDE, "assistants", tokens_in=120, tokens_out=30)

    assert ledger.added == []
    assert ledger.audited == [(AUDIT, "claude", "assistants", 1, Decimal(0), 120, 30)]


def test_without_a_ledger_nothing_is_recorded_anywhere() -> None:
    gate = SourceGate(SourcesSettings(), Counter(allowed=10).consume, None, AUDIT)

    gate.record_cost(Source.APIFY, Decimal("0.02"), service="instagram")
    gate.record_usage(Source.CLAUDE, "assistants")


def test_remaining_cost_is_the_cap_minus_the_month_spend() -> None:
    gate = SourceGate(_capped_sources("1"), Counter(allowed=1).consume, Ledger(Decimal("0.90")))

    assert gate.remaining_cost(Source.DATAFORSEO) == Decimal("0.10")


def test_remaining_cost_never_goes_below_zero() -> None:
    gate = SourceGate(_capped_sources("1"), Counter(allowed=1).consume, Ledger(Decimal("1.50")))

    assert gate.remaining_cost(Source.DATAFORSEO) == Decimal(0)


def test_remaining_cost_is_unknown_without_a_cap_or_a_ledger() -> None:
    uncapped = SourceGate(SourcesSettings(), Counter(allowed=1).consume, Ledger(Decimal(0)))
    unledgered = SourceGate(_capped_sources("1"), Counter(allowed=1).consume)

    assert uncapped.remaining_cost(Source.GOOGLE_SEARCH) is None
    assert unledgered.remaining_cost(Source.DATAFORSEO) is None
