from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from trader_jev.adapters import KabuStationMarketDataAdapter, KabuStationMessageSource
from trader_jev.clock import FixedClock
from trader_jev.collector import CollectionConfig, MarketDataCollectionError, MarketDataCollector
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    InstrumentMetadata,
    MarketEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from trader_jev.replay import ReplayMarketDataAdapter
from trader_jev.storage import ParquetEventStore

from .conftest import NOW


def event_copy(quote: QuoteEvent, **updates: object) -> QuoteEvent:
    return quote.model_copy(update={"event_id": uuid4(), **updates})


def test_parquet_round_trip_and_duckdb_query(tmp_path: Path, quote: QuoteEvent) -> None:
    store = ParquetEventStore(tmp_path / "raw")
    path = store.append(quote)

    assert path.exists()
    assert "date=2026-09-20" in path.as_posix()
    assert "market=JP" in path.as_posix()
    assert "event_type=quote" in path.as_posix()

    events = store.iter_events(
        quote.received_at - timedelta(seconds=1),
        quote.received_at + timedelta(seconds=1),
    )
    assert events == (quote,)

    result = store.query(
        "SELECT market, symbol, event_type, COUNT(*) AS count "
        "FROM raw_events GROUP BY market, symbol, event_type"
    )
    assert result.to_dicts() == [
        {"market": "JP", "symbol": "TEST", "event_type": "quote", "count": 1}
    ]


def test_empty_store_still_exposes_a_duckdb_raw_events_view(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path / "empty")

    result = store.query("SELECT COUNT(*) AS count FROM raw_events")

    assert result.to_dicts() == [{"count": 0}]


def test_ten_instruments_and_replay_are_deterministic(
    tmp_path: Path,
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    store = ParquetEventStore(tmp_path / "raw")
    instruments = tuple(
        instrument.model_copy(update={"symbol": f"T{index:02d}"}) for index in range(10)
    )
    events = tuple(
        event_copy(quote, instrument=current, sequence_number=index)
        for index, current in enumerate(instruments)
    )
    store.append_batch(events)

    replayed = store.iter_events(
        NOW - timedelta(seconds=1),
        NOW + timedelta(seconds=1),
        instruments,
    )
    assert tuple(event.instrument.symbol for event in replayed) == tuple(
        instrument.symbol for instrument in instruments
    )

    adapter = ReplayMarketDataAdapter(
        store,
        start=NOW - timedelta(seconds=1),
        end=NOW + timedelta(seconds=1),
    )

    async def collect() -> tuple[MarketEvent, ...]:
        collected: list[MarketEvent] = []
        async for event in adapter.stream(instruments):
            collected.append(event)
        return tuple(collected)

    assert asyncio.run(collect()) == replayed


class ScriptedAdapter:
    def __init__(self, events: Sequence[MarketEvent]) -> None:
        self.events = tuple(events)
        self.calls = 0

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        del instruments
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary disconnect")
        yield self.events[0]
        yield self.events[0]
        yield self.events[1]


@pytest.mark.asyncio
async def test_collector_reconnects_and_marks_duplicates_and_order_errors(
    tmp_path: Path,
    quote: QuoteEvent,
) -> None:
    older = event_copy(
        quote,
        event_time=quote.event_time - timedelta(seconds=2),
    )
    store = ParquetEventStore(tmp_path / "raw")
    collector = MarketDataCollector(
        store,
        config=CollectionConfig(
            heartbeat_timeout_seconds=0.1,
            backoff_initial_seconds=0.001,
            backoff_max_seconds=0.001,
            max_reconnects=1,
        ),
        clock=FixedClock(NOW),
    )

    stats = await collector.collect(ScriptedAdapter((quote, older)), [quote.instrument])

    assert stats.reconnects == 1
    assert stats.received_events == 3
    assert stats.stored_events == 2
    assert stats.duplicate_events == 1
    assert stats.out_of_order_events == 1
    assert stats.errors == 1
    flags = store.query("SELECT SUM(CAST(out_of_order AS INTEGER)) AS count FROM raw_events")
    assert flags["count"].to_list() == [1]


class HangingAdapter:
    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        del instruments
        await asyncio.sleep(1)
        return
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_heartbeat_exhaustion_is_explicit(
    tmp_path: Path,
    instrument: InstrumentMetadata,
) -> None:
    collector = MarketDataCollector(
        ParquetEventStore(tmp_path / "raw"),
        config=CollectionConfig(
            heartbeat_timeout_seconds=0.001,
            backoff_initial_seconds=0.001,
            max_reconnects=0,
        ),
        clock=FixedClock(NOW),
    )

    with pytest.raises(MarketDataCollectionError, match="heartbeat timeout"):
        await collector.collect(HangingAdapter(), [instrument])


class EmptyMessageSource:
    async def stream(self, symbols: Sequence[str]) -> AsyncIterator[dict[str, Any]]:
        del symbols
        if False:
            yield {}


def make_empty_source() -> KabuStationMessageSource:
    return EmptyMessageSource()


def test_kabu_message_normalization_preserves_exchange_and_receive_time(
    instrument: InstrumentMetadata,
) -> None:
    adapter = KabuStationMarketDataAdapter(
        source=make_empty_source(),
        clock=FixedClock(NOW),
    )
    message = {
        "Symbol": instrument.symbol,
        "CurrentPriceTime": "2026-09-21T09:00:00",
        "BidPrice": "100",
        "AskPrice": "101",
        "BidQty": "10",
        "AskQty": "12",
        "SequenceNumber": 7,
    }

    event = adapter.normalize_message(message, instrument)
    same_event = adapter.normalize_message(message, instrument)

    assert isinstance(event, QuoteEvent)
    assert event.bid == Decimal("100")
    assert event.event_time.isoformat() == "2026-09-21T09:00:00+09:00"
    assert event.received_at == NOW
    assert event.sequence_number == 7
    assert event.event_id == same_event.event_id


def test_kabu_trade_and_l2_messages_are_normalized(instrument: InstrumentMetadata) -> None:
    adapter = KabuStationMarketDataAdapter(source=make_empty_source(), clock=FixedClock(NOW))
    trade = adapter.normalize_message(
        {
            "Symbol": instrument.symbol,
            "type": "execution",
            "CurrentPrice": 100,
            "CurrentPriceSize": 3,
            "AggressorSide": "BUY",
            "event_time": NOW,
        },
        instrument,
    )
    book = adapter.normalize_message(
        {
            "Symbol": instrument.symbol,
            "event_time": NOW,
            "Buy1": {"Price": 100, "Qty": 10},
            "Buy2": {"Price": 99, "Qty": 20},
            "Sell1": {"Price": 101, "Qty": 12},
        },
        instrument,
    )

    assert isinstance(trade, TradeEvent)
    assert trade.aggressor is not None
    assert trade.aggressor.value == "LONG"
    assert isinstance(book, OrderBookEvent)
    assert book.bids[0].price == Decimal("100")
    assert book.asks[0].size == Decimal("12")


def test_l2_event_can_seed_a_point_in_time_feature_snapshot(
    instrument: InstrumentMetadata,
) -> None:
    adapter = KabuStationMarketDataAdapter(source=make_empty_source(), clock=FixedClock(NOW))
    book = adapter.normalize_message(
        {
            "Symbol": instrument.symbol,
            "type": "order_book",
            "event_time": NOW,
            "bids": [[100, 10]],
            "asks": [[101, 12]],
        },
        instrument,
    )
    assert isinstance(book, OrderBookEvent)

    engine = InMemoryFeatureEngine()
    engine.update(book)
    snapshot = engine.snapshot(instrument, NOW)

    assert snapshot.market.bid == Decimal("100")
    assert snapshot.market.ask == Decimal("101")
