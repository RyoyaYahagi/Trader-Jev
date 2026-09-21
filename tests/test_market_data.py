from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trader_jev.adapters import FileMarketDataAdapter, SyntheticMarketDataAdapter
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.ingest import HistoricalDataIngestor, HistoricalIngestionConfig
from trader_jev.models import (
    BarEvent,
    InstrumentMetadata,
    Market,
    MarketEvent,
    OrderBookEvent,
    OrderBookLevel,
    QuoteEvent,
)
from trader_jev.quality import validate_historical_events
from trader_jev.replay import ReplayMarketDataAdapter
from trader_jev.storage import ParquetEventStore

from .conftest import NOW


def event_copy(quote: QuoteEvent, **updates: object) -> QuoteEvent:
    return quote.model_copy(update={"event_id": uuid4(), **updates})


def _us_instrument(instrument: InstrumentMetadata) -> InstrumentMetadata:
    return instrument.model_copy(
        update={
            "symbol": "AAPL",
            "market": Market.US,
            "currency": "USD",
            "timezone": "America/New_York",
        }
    )


def _json_event(event: MarketEvent, event_type: str) -> str:
    record = event.model_dump(mode="json")
    record["event_type"] = event_type
    return json.dumps(record, sort_keys=True)


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
    assert store.event_ids() == ()


def test_bar_events_are_part_of_the_shared_schema_and_round_trip(
    tmp_path: Path,
    instrument: InstrumentMetadata,
) -> None:
    bar = BarEvent(
        instrument=instrument,
        event_time=NOW - timedelta(minutes=1),
        received_at=NOW,
        source="historical-fixture",
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        volume=Decimal("5000"),
        interval_seconds=60,
    )
    store = ParquetEventStore(tmp_path / "raw")

    path = store.append(bar)

    assert "event_type=bar" in path.as_posix()
    assert store.iter_events(NOW - timedelta(minutes=2), NOW + timedelta(minutes=1)) == (bar,)


def test_ten_instruments_can_be_read_from_one_file_and_replayed(
    tmp_path: Path,
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    instruments = tuple(
        instrument.model_copy(update={"symbol": f"T{index:02d}"}) for index in range(10)
    )
    path = tmp_path / "historical.jsonl"
    path.write_text(
        "".join(
            _json_event(event_copy(quote, instrument=current, sequence_number=index), "quote")
            + "\n"
            for index, current in enumerate(instruments)
        ),
        encoding="utf-8",
    )
    adapter = FileMarketDataAdapter(path)

    events = adapter.read_events(instruments)

    assert tuple(event.instrument.symbol for event in events) == tuple(
        instrument.symbol for instrument in instruments
    )
    assert adapter.stats.total_rows == 10
    assert adapter.stats.accepted_rows == 10
    assert adapter.stats.healthy


def test_file_adapter_skips_corrupt_rows_and_preserves_timestamp_semantics(
    tmp_path: Path,
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    path = tmp_path / "historical.jsonl"
    path.write_text(
        _json_event(quote, "quote")
        + "\nnot-json\n"
        + json.dumps(
            {
                "event_type": "bar",
                "symbol": instrument.symbol,
                "market": instrument.market.value,
                "event_time": NOW.isoformat(),
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = FileMarketDataAdapter(path)

    events = adapter.read_events([instrument])

    assert len(events) == 2
    assert isinstance(events[1], BarEvent)
    assert events[1].received_at == NOW
    assert adapter.stats.total_rows == 3
    assert adapter.stats.accepted_rows == 2
    assert adapter.stats.rejected_rows == 1
    assert not adapter.stats.healthy


def test_file_adapter_can_read_csv_rows(
    tmp_path: Path,
    instrument: InstrumentMetadata,
) -> None:
    path = tmp_path / "historical.csv"
    path.write_text(
        "event_type,symbol,market,event_time,received_at,bid,ask,bid_size,ask_size\n"
        f"quote,{instrument.symbol},{instrument.market.value},{NOW.isoformat()},{NOW.isoformat()},100,101,10,12\n",
        encoding="utf-8",
    )

    events = FileMarketDataAdapter(path).read_events([instrument])

    assert len(events) == 1
    assert isinstance(events[0], QuoteEvent)
    assert events[0].bid == Decimal("100")


def test_synthetic_adapter_is_deterministic_for_japan_and_us(
    instrument: InstrumentMetadata,
) -> None:
    us_instrument = _us_instrument(instrument)
    start = NOW
    end = NOW + timedelta(minutes=2)
    first_adapter = SyntheticMarketDataAdapter(seed=7)
    second_adapter = SyntheticMarketDataAdapter(seed=7)

    async def collect(adapter: SyntheticMarketDataAdapter) -> tuple[MarketEvent, ...]:
        return tuple(
            [event async for event in adapter.replay((instrument, us_instrument), start, end)]
        )

    first = asyncio.run(collect(first_adapter))
    second = asyncio.run(collect(second_adapter))

    assert first == second
    assert len(first) == 4
    assert {event.instrument.market for event in first} == {Market.JP, Market.US}


def test_historical_quality_checks_find_duplicates_order_errors_and_gaps(
    quote: QuoteEvent,
) -> None:
    duplicate = quote.model_copy()
    older = event_copy(quote, event_time=quote.event_time - timedelta(seconds=2))
    gap = event_copy(quote, event_time=quote.event_time + timedelta(minutes=2))

    report = validate_historical_events(
        (quote, duplicate, older, gap),
        instruments=[quote.instrument],
        expected_interval=timedelta(minutes=1),
    )

    assert report.rows_seen == 4
    assert report.valid_rows == 4
    assert report.duplicate_events == 1
    assert report.out_of_order_events == 1
    assert report.missing_intervals == 1
    assert not report.healthy


@pytest.mark.asyncio
async def test_historical_ingestor_appends_unique_rows_and_marks_order_errors(
    tmp_path: Path,
    quote: QuoteEvent,
) -> None:
    older = event_copy(quote, event_time=quote.event_time - timedelta(seconds=2))
    path = tmp_path / "historical.jsonl"
    path.write_text(
        _json_event(quote, "quote")
        + "\n"
        + _json_event(quote, "quote")
        + "\n"
        + _json_event(older, "quote")
        + "\n",
        encoding="utf-8",
    )
    store = ParquetEventStore(tmp_path / "raw")
    result = await HistoricalDataIngestor(
        store,
        config=HistoricalIngestionConfig(expected_interval_seconds=60),
    ).ingest(
        FileMarketDataAdapter(path),
        [quote.instrument],
        NOW - timedelta(seconds=1),
        NOW + timedelta(seconds=1),
    )

    assert result.source_events == 3
    assert result.stored_events == 2
    assert result.duplicate_events == 1
    assert result.out_of_order_events == 1
    assert result.report.duplicate_events == 1
    flags = store.query("SELECT SUM(CAST(out_of_order AS INTEGER)) AS count FROM raw_events")
    assert flags["count"].to_list() == [1]


def test_replay_adapter_is_deterministic_and_point_in_time(
    tmp_path: Path,
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    store = ParquetEventStore(tmp_path / "raw")
    events = (
        event_copy(quote, event_time=NOW - timedelta(seconds=3), received_at=NOW),
        event_copy(quote, event_time=NOW - timedelta(seconds=2), received_at=NOW),
    )
    store.append_batch(events)
    adapter = ReplayMarketDataAdapter(store)

    async def collect() -> tuple[MarketEvent, ...]:
        return tuple(
            [
                event
                async for event in adapter.replay(
                    [instrument], NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)
                )
            ]
        )

    assert asyncio.run(collect()) == events


def test_l2_event_can_seed_a_point_in_time_feature_snapshot(
    instrument: InstrumentMetadata,
) -> None:
    book = OrderBookEvent(
        instrument=instrument,
        event_time=NOW,
        received_at=NOW,
        source="historical-fixture",
        bids=(OrderBookLevel(price=Decimal("100"), size=Decimal("10")),),
        asks=(OrderBookLevel(price=Decimal("101"), size=Decimal("12")),),
    )
    engine = InMemoryFeatureEngine()
    engine.update(book)
    snapshot = engine.snapshot(instrument, NOW)

    assert snapshot.market.bid == Decimal("100")
    assert snapshot.market.ask == Decimal("101")
