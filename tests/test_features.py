from __future__ import annotations

from collections.abc import MutableMapping
from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from trader_jev.features import FeatureEngineConfig, InMemoryFeatureEngine
from trader_jev.models import Action, OrderBookEvent, OrderBookLevel, QuoteEvent, TradeEvent

from .conftest import NOW


def test_feature_engine_emits_compact_deterministic_immutable_snapshot(quote: QuoteEvent) -> None:
    engine = InMemoryFeatureEngine()
    engine.update(quote)
    engine.update(
        TradeEvent(
            event_id=uuid4(),
            instrument=quote.instrument,
            event_time=NOW,
            received_at=NOW,
            source="fixture",
            price=Decimal("101"),
            size=Decimal("4"),
            aggressor=Action.LONG,
        )
    )

    first = engine.snapshot(quote.instrument, NOW)
    second = engine.snapshot(quote.instrument, NOW)

    assert first == second
    assert first.schema_version == "1.0"
    assert {
        "return_5s",
        "return_30s",
        "return_1m",
        "return_5m",
        "vwap_distance",
        "ema_slope",
        "rsi",
        "macd_histogram",
        "atr",
        "realized_volatility",
    } <= first.technical.keys()
    assert {"spread_bps", "imbalance", "microprice"} <= first.orderbook.keys()
    assert {"buy_volume", "sell_volume", "cvd"} <= first.orderflow.keys()
    assert len(first.model_dump_json()) < 10_000

    with pytest.raises(TypeError, match="frozen mapping"):
        cast(MutableMapping[str, float], first.technical)["mid"] = 0.0


def test_feature_engine_respects_received_time_and_orderbook_features(
    quote: QuoteEvent,
) -> None:
    engine = InMemoryFeatureEngine(FeatureEngineConfig(depth_within_bps=100.0))
    engine.update(quote)
    engine.update(
        OrderBookEvent(
            event_id=uuid4(),
            instrument=quote.instrument,
            event_time=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=2),
            source="fixture",
            bids=(
                OrderBookLevel(price=Decimal("100"), size=Decimal("10")),
                OrderBookLevel(price=Decimal("99"), size=Decimal("5")),
            ),
            asks=(
                OrderBookLevel(price=Decimal("101"), size=Decimal("4")),
                OrderBookLevel(price=Decimal("102"), size=Decimal("2")),
            ),
        )
    )

    before = engine.snapshot(quote.instrument, NOW + timedelta(seconds=1))
    after = engine.snapshot(quote.instrument, NOW + timedelta(seconds=2))

    assert before.orderbook["top_bid_depth"] == 10.0
    assert after.orderbook["top_bid_depth"] == 15.0
    assert after.orderbook["top_ask_depth"] == 6.0
    assert after.orderbook["imbalance"] > 0


def test_stale_market_data_is_explicitly_unhealthy(quote: QuoteEvent) -> None:
    engine = InMemoryFeatureEngine(FeatureEngineConfig(max_data_age_seconds=1))
    engine.update(quote)

    snapshot = engine.snapshot(quote.instrument, NOW + timedelta(seconds=2))

    assert not snapshot.data_quality.healthy
    assert "market_data_stale" in snapshot.data_quality.reasons
