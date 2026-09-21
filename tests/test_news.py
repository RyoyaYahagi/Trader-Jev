from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import Direction, NewsEvent, QuoteEvent
from trader_jev.news import (
    InMemoryNewsAdapter,
    NewsFeatureEngine,
    NewsIntegrationMode,
    NewsStateCache,
    NewsWorkerService,
    classify_headline,
)

from .conftest import NOW


def make_news(
    quote: QuoteEvent,
    *,
    event_id: UUID | None = None,
    first_seen_offset: int = 0,
    headline: str = "profit growth",
) -> NewsEvent:
    return NewsEvent(
        event_id=event_id or uuid4(),
        instrument=quote.instrument,
        event_time=NOW,
        received_at=NOW,
        source="source-a",
        headline=headline,
        published_at=NOW - timedelta(seconds=5),
        first_seen_at=NOW + timedelta(seconds=first_seen_offset),
        related_symbols=(quote.instrument.symbol,),
    )


def test_news_cache_respects_first_seen_and_modes(quote: QuoteEvent) -> None:
    cache = NewsStateCache()
    news = make_news(quote, first_seen_offset=10)
    cache.upsert((news,))

    assert cache.state_for(quote.instrument, NOW + timedelta(seconds=9)) == {}
    headline = cache.state_for(
        quote.instrument,
        NOW + timedelta(seconds=10),
        mode=NewsIntegrationMode.HEADLINE,
    )
    structured = cache.state_for(
        quote.instrument,
        NOW + timedelta(seconds=20),
        mode=NewsIntegrationMode.STRUCTURED,
    )
    assert headline["headline"] == "profit growth"
    assert structured["direction"] == "UP"
    assert structured["age_seconds"] == 10.0
    assert cache.state_for(quote.instrument, NOW, mode=NewsIntegrationMode.NONE) == {}


def test_news_worker_deduplicates_and_handles_corrections(quote: QuoteEvent) -> None:
    original = make_news(quote)
    duplicate = make_news(quote, event_id=uuid4())
    corrected = make_news(quote, event_id=original.event_id, headline="loss decline")
    cache = NewsStateCache()
    worker = NewsWorkerService(cache)

    first = asyncio.run(worker.run_once(InMemoryNewsAdapter((original,)), [quote.instrument]))
    second = asyncio.run(worker.run_once(InMemoryNewsAdapter((duplicate,)), [quote.instrument]))
    third = asyncio.run(worker.run_once(InMemoryNewsAdapter((corrected,)), [quote.instrument]))

    assert first.inserted_events == 1
    assert second.duplicate_events == 1
    assert third.corrections == 1
    assert cache.size == 1
    state = cache.state_for(quote.instrument, NOW, mode=NewsIntegrationMode.STRUCTURED)
    assert state["headline"] == "loss decline"
    assert state["direction"] == "DOWN"


def test_news_feature_engine_keeps_news_out_of_hot_path(quote: QuoteEvent) -> None:
    base = InMemoryFeatureEngine()
    base.update(quote)
    cache = NewsStateCache()
    cache.upsert((make_news(quote, first_seen_offset=10),))
    engine = NewsFeatureEngine(base, cache, mode=NewsIntegrationMode.STRUCTURED)

    before = engine.snapshot(quote.instrument, NOW + timedelta(seconds=9))
    after = engine.snapshot(quote.instrument, NOW + timedelta(seconds=10))

    assert before.news == {}
    assert after.news["event_type"] == "corporate_positive"
    assert after.news["direction"] == "UP"


def test_news_source_and_classifier_are_replaceable(quote: QuoteEvent) -> None:
    direction, event_type, materiality = classify_headline("下方修正 loss")
    assert direction is Direction.DOWN
    assert event_type == "corporate_negative"
    assert materiality > 0

    event = make_news(quote)
    worker = NewsWorkerService()
    result = asyncio.run(worker.run_once(InMemoryNewsAdapter((event,)), [quote.instrument]))
    assert result.normalized_events == 1
