from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta

import pytest

from trader_jev.clock import FixedClock, ReplayClock
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.interfaces import PredictionModel
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    Direction,
    InstrumentMetadata,
    MarketEvent,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    PredictionOutput,
    QuoteEvent,
    TradeIntent,
)
from trader_jev.pipeline import PipelineConfig, TradingPipeline
from trader_jev.risk import DeterministicRiskEngine, FixedQuantityPortfolioPolicy, RiskConfig

from .conftest import NOW


class FakeDecisionModel:
    def __init__(self, action: Action = Action.LONG, quantity: int = 1) -> None:
        self.action = action
        self.quantity = quantity

    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: PredictionOutput | None = None,
    ) -> TradeIntent:
        del prediction
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=self.action,
            requested_quantity=self.quantity,
            strategy_id="fake-strategy",
            model_version="fake-1",
            reason="test decision",
            created_at=snapshot.as_of,
        )


class ErrorDecisionModel(FakeDecisionModel):
    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: PredictionOutput | None = None,
    ) -> TradeIntent:
        del snapshot, prediction
        raise RuntimeError("decision failed")


class SlowDecisionModel(FakeDecisionModel):
    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: PredictionOutput | None = None,
    ) -> TradeIntent:
        await asyncio.sleep(0.05)
        return await super().decide(snapshot, prediction)


class ErrorPredictionModel:
    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        del snapshot
        raise RuntimeError("prediction failed")


class FutureMetadataPredictionModel:
    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        return PredictionOutput(
            direction_5m=Direction.UP,
            model_version="future-test",
            trained_until=snapshot.as_of + timedelta(seconds=1),
        )


class FakeBroker:
    def __init__(self) -> None:
        self.submitted: list[OrderIntent] = []

    async def submit(self, order: OrderIntent) -> OrderEvent:
        self.submitted.append(order)
        return OrderEvent(
            order_intent_id=order.order_intent_id,
            status=OrderStatus.ACCEPTED,
            occurred_at=NOW,
            broker_order_id=f"fake-{len(self.submitted)}",
        )

    async def cancel(self, order: OrderIntent) -> OrderEvent:
        return OrderEvent(
            order_intent_id=order.order_intent_id,
            status=OrderStatus.CANCELED,
            occurred_at=NOW,
        )


class FakeMarketAdapter:
    def __init__(self, event: MarketEvent) -> None:
        self.event = event

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        del instruments
        yield self.event


def make_pipeline(
    instrument: InstrumentMetadata,
    decision_model: FakeDecisionModel,
    broker: FakeBroker,
    *,
    prediction_model: PredictionModel | None = None,
    risk_config: RiskConfig | None = None,
    pipeline_config: PipelineConfig | None = None,
    clock: ReplayClock | None = None,
) -> TradingPipeline:
    return TradingPipeline(
        feature_engine=InMemoryFeatureEngine(),
        decision_model=decision_model,
        prediction_model=prediction_model,
        risk_engine=DeterministicRiskEngine(
            config=risk_config,
            portfolio_policy=FixedQuantityPortfolioPolicy(),
            clock=FixedClock(NOW),
        ),
        broker_adapter=broker,
        config=pipeline_config,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_fake_market_decision_risk_broker_path(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(instrument, FakeDecisionModel(), broker)

    result = await pipeline.process_event(quote)

    assert result.failure_code is None
    assert result.submitted
    assert result.risk_decision is not None and result.risk_decision.approved
    assert len(broker.submitted) == 1
    assert broker.submitted[0].instrument == instrument


@pytest.mark.asyncio
async def test_prediction_model_is_optional(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(instrument, FakeDecisionModel(), broker)

    result = await pipeline.process_event(quote)

    assert result.prediction is None
    assert result.submitted


@pytest.mark.asyncio
async def test_risk_rejection_never_calls_broker(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(
        instrument,
        FakeDecisionModel(),
        broker,
        risk_config=RiskConfig(allowed_symbols=frozenset({"NOT_TEST"})),
    )

    result = await pipeline.process_event(quote)

    assert result.risk_decision is not None
    assert not result.risk_decision.approved
    assert result.risk_decision.reason_code == "SYMBOL_NOT_ALLOWED"
    assert not result.submitted
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_prediction_error_fails_closed(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(
        instrument,
        FakeDecisionModel(),
        broker,
        prediction_model=ErrorPredictionModel(),
    )

    result = await pipeline.process_event(quote)

    assert result.failure_code == "PREDICTION_MODEL_ERROR"
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_future_prediction_metadata_fails_closed(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(
        instrument,
        FakeDecisionModel(),
        broker,
        prediction_model=FutureMetadataPredictionModel(),
        clock=ReplayClock(NOW),
    )

    result = await pipeline.process_event(quote)

    assert result.failure_code == "PREDICTION_METADATA_FROM_FUTURE"
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_decision_error_and_timeout_fail_closed(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    for model, config, expected in (
        (
            ErrorDecisionModel(),
            PipelineConfig(),
            "DECISION_MODEL_ERROR",
        ),
        (
            SlowDecisionModel(),
            PipelineConfig(decision_timeout_seconds=0.001),
            "DECISION_MODEL_ERROR",
        ),
    ):
        broker = FakeBroker()
        pipeline = make_pipeline(
            instrument,
            model,
            broker,
            pipeline_config=config,
        )

        result = await pipeline.process_event(quote)

        assert result.failure_code == expected
        assert broker.submitted == []


@pytest.mark.asyncio
async def test_market_adapter_can_be_replaced_without_changing_pipeline(
    instrument: InstrumentMetadata,
    quote: QuoteEvent,
) -> None:
    broker = FakeBroker()
    pipeline = make_pipeline(instrument, FakeDecisionModel(), broker)

    results = [result async for result in pipeline.run(FakeMarketAdapter(quote), [instrument])]

    assert len(results) == 1
    assert results[0].submitted
