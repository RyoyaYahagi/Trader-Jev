from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from trader_jev.clock import ReplayClock
from trader_jev.models import (
    Direction,
    InstrumentMetadata,
    NewsEvent,
    PredictionOutput,
    QuoteEvent,
    ReplayEvent,
)
from trader_jev.replay import (
    PointInTimeViolation,
    ReplayConfig,
    ReplayEngine,
    available_news,
    event_available_at,
    merge_replay_events,
    validate_event_point_in_time,
    validate_prediction_metadata,
)

from .conftest import NOW


def event_copy(quote: QuoteEvent, **updates: object) -> QuoteEvent:
    return quote.model_copy(update={"event_id": uuid4(), **updates})


@pytest.mark.asyncio
async def test_replay_engine_merges_by_availability_and_advances_replay_clock(
    quote: QuoteEvent,
) -> None:
    delayed = event_copy(
        quote,
        event_time=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=2),
    )
    later = event_copy(
        quote,
        event_time=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=3),
    )
    clock = ReplayClock()
    engine = ReplayEngine(
        [later, delayed, quote],
        clock=clock,
        config=ReplayConfig(start=NOW, end=NOW + timedelta(seconds=4), speed="max"),
    )
    observed: list[object] = []

    async def listener(event: ReplayEvent) -> None:
        assert isinstance(event, QuoteEvent)
        observed.append(clock.now())

    subscription = engine.subscribe(listener, event_type=QuoteEvent)
    events = [event async for event in engine.replay()]

    assert [event.received_at for event in events] == [
        NOW,
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
    ]
    assert observed == [
        NOW,
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
    ]
    assert clock.now() == NOW + timedelta(seconds=3)
    assert subscription.active
    subscription.unsubscribe()
    assert not subscription.active


@pytest.mark.asyncio
async def test_replay_speed_filters_and_seeded_tie_breaking(
    quote: QuoteEvent,
) -> None:
    second = event_copy(quote, received_at=NOW + timedelta(seconds=2))
    third = event_copy(quote, received_at=NOW + timedelta(seconds=4))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    engine = ReplayEngine(
        [third, second, quote],
        config=ReplayConfig(start=NOW, end=NOW + timedelta(seconds=5), speed=2),
        sleep=fake_sleep,
    )
    replayed = [event async for event in engine.replay(symbols="TEST")]

    assert len(replayed) == 3
    assert sleeps == [1.0, 1.0]

    same_time_a = event_copy(quote)
    same_time_b = event_copy(quote)
    first = merge_replay_events(((same_time_a,), (same_time_b,)), seed=7)
    second_order = merge_replay_events(((same_time_b,), (same_time_a,)), seed=7)
    assert first == second_order


def test_replay_config_rejects_invalid_ranges_and_normalizes_speed() -> None:
    assert ReplayConfig(speed="4x").speed_multiplier == 4
    assert ReplayConfig(speed="max").max_speed
    with pytest.raises(ValueError, match="end must not be earlier"):
        ReplayConfig(start=NOW, end=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="positive"):
        ReplayConfig(speed=0)


def test_news_and_prediction_metadata_are_checked_at_point_in_time(
    instrument: InstrumentMetadata,
) -> None:
    news = NewsEvent(
        instrument=instrument,
        event_time=NOW,
        received_at=NOW,
        source="test-news",
        headline="headline",
        published_at=NOW,
        first_seen_at=NOW + timedelta(seconds=1),
    )

    assert event_available_at(news) == NOW + timedelta(seconds=1)
    with pytest.raises(PointInTimeViolation):
        validate_event_point_in_time(news, NOW)
    assert available_news((news,), NOW) == ()
    assert available_news((news,), NOW + timedelta(seconds=1)) == (news,)

    future = PredictionOutput(
        direction_5m=Direction.UP,
        model_version="test-1",
        trained_until=NOW + timedelta(seconds=1),
    )
    with pytest.raises(PointInTimeViolation):
        validate_prediction_metadata(future, NOW)
    current_date = PredictionOutput(
        direction_5m=Direction.UP,
        model_version="test-1",
        trained_until=date(NOW.year, NOW.month, NOW.day),
    )
    with pytest.raises(PointInTimeViolation):
        validate_prediction_metadata(current_date, NOW)
