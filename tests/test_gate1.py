from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from trader_jev.adapters import FileMarketDataAdapter
from trader_jev.clock import ReplayClock
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    BarEvent,
    Market,
    MarketEvent,
    OrderBookEvent,
    OrderBookLevel,
    QuoteEvent,
    TradeEvent,
)
from trader_jev.quality import validate_historical_events
from trader_jev.replay import (
    PointInTimeViolation,
    ReplayConfig,
    ReplayEngine,
    ReplayMarketDataAdapter,
    validate_event_point_in_time,
)
from trader_jev.storage import ParquetEventStore

GATE_START = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
EVENT_IDS = (
    UUID("00000000-0000-0000-0000-000000000101"),
    UUID("00000000-0000-0000-0000-000000000102"),
    UUID("00000000-0000-0000-0000-000000000103"),
    UUID("00000000-0000-0000-0000-000000000104"),
)


def gate_events(quote: QuoteEvent) -> tuple[MarketEvent, ...]:
    instrument = quote.instrument
    return (
        quote.model_copy(
            update={
                "event_id": EVENT_IDS[0],
                "event_time": GATE_START,
                "received_at": GATE_START,
            }
        ),
        TradeEvent(
            event_id=EVENT_IDS[1],
            instrument=instrument,
            event_time=GATE_START + timedelta(seconds=1),
            received_at=GATE_START + timedelta(seconds=2),
            source="gate-1-fixture",
            price=Decimal("101"),
            size=Decimal("5"),
        ),
        BarEvent(
            event_id=EVENT_IDS[2],
            instrument=instrument,
            event_time=GATE_START + timedelta(seconds=3),
            received_at=GATE_START + timedelta(seconds=3),
            source="gate-1-fixture",
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("99"),
            close=Decimal("102"),
            volume=Decimal("100"),
            interval_seconds=60,
        ),
        OrderBookEvent(
            event_id=EVENT_IDS[3],
            instrument=instrument,
            event_time=GATE_START + timedelta(seconds=4),
            received_at=GATE_START + timedelta(seconds=4),
            source="gate-1-fixture",
            bids=(OrderBookLevel(price=Decimal("101"), size=Decimal("10")),),
            asks=(OrderBookLevel(price=Decimal("102"), size=Decimal("12")),),
        ),
    )


def json_row(event: MarketEvent) -> str:
    event_types = {
        BarEvent: "bar",
        OrderBookEvent: "order_book",
        QuoteEvent: "quote",
        TradeEvent: "trade",
    }
    record = event.model_dump(mode="json")
    record["event_type"] = event_types[type(event)]
    return json.dumps(record, sort_keys=True)


async def replay_payloads(events: tuple[MarketEvent, ...]) -> tuple[str, ...]:
    config = ReplayConfig(
        start=GATE_START,
        end=GATE_START + timedelta(seconds=5),
        speed="max",
        seed=23,
        markets=frozenset({Market.JP}),
        symbols=frozenset({"TEST"}),
    )
    engine = ReplayEngine(events, clock=ReplayClock(), config=config)
    payloads = [event.model_dump_json() async for event in engine.replay()]
    return tuple(payloads)


def test_gate1_fixture_round_trip_replay_and_reproducibility(
    tmp_path: Path,
    quote: QuoteEvent,
) -> None:
    source_path = tmp_path / "gate-1-events.jsonl"
    events = gate_events(quote)
    source_path.write_text("\n".join(json_row(event) for event in events) + "\n", encoding="utf-8")

    first_read = FileMarketDataAdapter(source_path).read_events([quote.instrument])
    second_read = FileMarketDataAdapter(source_path).read_events([quote.instrument])
    assert first_read == second_read
    assert tuple(str(event.event_id) for event in first_read) == tuple(
        str(event_id) for event_id in EVENT_IDS
    )

    first_replay = asyncio.run(replay_payloads(first_read))
    second_replay = asyncio.run(replay_payloads(second_read))
    assert first_replay == second_replay
    assert [json.loads(payload)["event_id"] for payload in first_replay] == [
        str(EVENT_IDS[0]),
        str(EVENT_IDS[1]),
        str(EVENT_IDS[2]),
        str(EVENT_IDS[3]),
    ]

    store = ParquetEventStore(tmp_path / "raw")
    store.append_batch(first_read)
    replay_adapter = ReplayMarketDataAdapter(
        store,
        start=GATE_START,
        end=GATE_START + timedelta(seconds=5),
        speed="max",
        seed=23,
    )

    async def collect() -> tuple[MarketEvent, ...]:
        replayed_events = [
            event
            async for event in replay_adapter.replay(
                [quote.instrument], GATE_START, GATE_START + timedelta(seconds=5)
            )
        ]
        return tuple(replayed_events)

    replayed = asyncio.run(collect())
    reloaded = store.iter_events(GATE_START, GATE_START + timedelta(seconds=5))
    assert tuple(event.model_dump_json() for event in replayed) == tuple(
        event.model_dump_json() for event in reloaded
    )


def test_gate1_availability_and_feature_point_in_time(
    quote: QuoteEvent,
) -> None:
    events = gate_events(quote)
    delayed = events[1]
    assert delayed.event_time < delayed.received_at
    assert tuple(event.event_id for event in events) == tuple(
        event.event_id for event in sorted(events, key=lambda event: event.received_at)
    )

    future = quote.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000199"),
            "event_time": GATE_START + timedelta(seconds=10),
            "received_at": GATE_START + timedelta(seconds=10),
        }
    )
    with pytest.raises(PointInTimeViolation):
        validate_event_point_in_time(future, GATE_START + timedelta(seconds=5))

    feature_engine = InMemoryFeatureEngine()
    initial = events[0]
    assert isinstance(initial, QuoteEvent)
    feature_engine.update(initial)
    feature_engine.update(future)
    snapshot = feature_engine.snapshot(initial.instrument, GATE_START + timedelta(seconds=5))
    assert snapshot.market.mid == initial.mid
    assert snapshot.as_of == GATE_START + timedelta(seconds=5)


def test_gate1_quality_report_covers_duplicate_order_and_missing_interval(
    quote: QuoteEvent,
) -> None:
    events = gate_events(quote)
    first = events[0]
    assert isinstance(first, QuoteEvent)
    duplicate = first.model_copy()
    out_of_order = first.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000198"),
            "event_time": GATE_START - timedelta(seconds=1),
        }
    )
    gap = first.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000197"),
            "event_time": GATE_START + timedelta(minutes=2),
            "received_at": GATE_START + timedelta(minutes=2),
        }
    )
    report = validate_historical_events(
        (first, duplicate, out_of_order, gap),
        instruments=[first.instrument],
        expected_interval=timedelta(minutes=1),
    )

    assert report.rows_seen == 4
    assert report.valid_rows == 4
    assert report.duplicate_events == 1
    assert report.out_of_order_events == 1
    assert report.missing_intervals == 1
    assert not report.healthy
