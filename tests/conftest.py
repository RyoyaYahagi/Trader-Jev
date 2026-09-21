from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from trader_jev.models import InstrumentMetadata, Market, QuoteEvent, TradingSession

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


@pytest.fixture
def instrument() -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol="TEST",
        market=Market.JP,
        currency="jpy",
        timezone="Asia/Tokyo",
        tick_size=Decimal("1"),
        lot_size=1,
        trading_session=TradingSession(open_time=time(9), close_time=time(15)),
        shortability=True,
    )


@pytest.fixture
def quote(instrument: InstrumentMetadata) -> QuoteEvent:
    return QuoteEvent(
        instrument=instrument,
        event_time=NOW - timedelta(seconds=1),
        received_at=NOW,
        source="test-feed",
        bid=Decimal("100"),
        ask=Decimal("101"),
        bid_size=Decimal("10"),
        ask_size=Decimal("12"),
    )
