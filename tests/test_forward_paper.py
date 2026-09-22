from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader_jev.clock import FixedClock
from trader_jev.forward_paper import ForwardPaperConfig, ForwardPaperRunner, build_us_instruments
from trader_jev.models import ExecutionMode, InstrumentMetadata, MarketEvent, QuoteEvent

NOW = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)


class FakeMarketData:
    def __init__(self, events: Sequence[MarketEvent]) -> None:
        self.events = tuple(events)
        self.instruments: tuple[InstrumentMetadata, ...] = ()

    async def stream(
        self,
        instruments: Sequence[InstrumentMetadata],
    ) -> AsyncIterator[MarketEvent]:
        self.instruments = tuple(instruments)
        for event in self.events:
            yield event


def quote(
    instrument: InstrumentMetadata,
    *,
    event_time: datetime,
    mid: Decimal,
) -> QuoteEvent:
    return QuoteEvent(
        instrument=instrument,
        event_time=event_time,
        received_at=NOW,
        source="test-feed",
        bid=mid - Decimal("0.01"),
        ask=mid + Decimal("0.01"),
        bid_size=Decimal("1000"),
        ask_size=Decimal("1000"),
    )


@pytest.mark.asyncio
async def test_forward_paper_entry_and_session_exit_are_paper_only() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    market_data = FakeMarketData(
        (
            quote(instrument, event_time=NOW - timedelta(seconds=31), mid=Decimal("100")),
            quote(instrument, event_time=NOW, mid=Decimal("101")),
        )
    )
    runner = ForwardPaperRunner(
        ForwardPaperConfig(
            symbols=("AAPL",),
            initial_capital=Decimal("10000"),
            runtime_seconds=60,
            decision_cadence_seconds=0,
            max_positions=1,
            market_hours_only=False,
        ),
        market_data=market_data,
        clock=FixedClock(NOW),
    )

    summary = await runner.run()

    assert market_data.instruments == (instrument,)
    assert summary.status == "COMPLETED"
    assert summary.events_processed == 2
    assert summary.fills == 2
    assert summary.portfolio.positions == {}
    assert summary.approved_orders >= 2
    assert summary.run_config["execution_mode"] == ExecutionMode.PAPER.value
    assert runner.broker.orders
    assert all(order.execution_mode is ExecutionMode.PAPER for order in runner.broker.orders)


@pytest.mark.asyncio
async def test_forward_paper_fails_closed_when_no_quotes_arrive() -> None:
    market_data = FakeMarketData(())
    runner = ForwardPaperRunner(
        ForwardPaperConfig(symbols=("AAPL",), runtime_seconds=1),
        market_data=market_data,
        clock=FixedClock(NOW),
    )

    summary = await runner.run()

    assert summary.status == "FAILED"
    assert summary.events_processed == 0
    assert summary.errors == ("no quote events were received",)


def test_forward_paper_rejects_duplicate_symbols() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        ForwardPaperConfig(symbols=("AAPL", "aapl"))
