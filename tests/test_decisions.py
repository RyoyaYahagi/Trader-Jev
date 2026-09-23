from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from trader_jev.clock import FixedClock
from trader_jev.decision import (
    JevAdapterConfig,
    JevDecisionAdapter,
    JevDecisionModel,
    RuleDecisionModel,
    SingleFlightDecisionRunner,
    build_jev_request,
)
from trader_jev.execution import PaperBroker
from trader_jev.experiments import (
    JevInputProfile,
    JevOutputPolicy,
    OutputPolicyKind,
    ThresholdPolicy,
)
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import Action, DecisionSnapshot, Direction, QuoteEvent, Regime, TradeIntent
from trader_jev.pipeline import TradingPipeline
from trader_jev.risk import DeterministicRiskEngine

from .conftest import NOW


def snapshot_for(quote: QuoteEvent) -> DecisionSnapshot:
    engine = InMemoryFeatureEngine()
    engine.update(quote)
    return engine.snapshot(quote.instrument, quote.received_at)


class StaticJevClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.requests: list[Any] = []

    async def decide(self, request: Any) -> Mapping[str, Any]:
        self.requests.append(request)
        return self.response


class SlowJevClient:
    async def decide(self, request: Any) -> Mapping[str, Any]:
        del request
        await asyncio.sleep(0.05)
        return {"action": "LONG", "direction_5m": "UP", "confidence": 0.8}


@pytest.mark.asyncio
async def test_jev_adapter_normalizes_typed_decision_and_audits_compact_request(
    quote: QuoteEvent,
) -> None:
    client = StaticJevClient(
        {
            "action": "LONG",
            "direction_5m": "UP",
            "regime": "TREND_UP",
            "setup_quality": 0.7,
            "probabilities": {"up": 0.7, "flat": 0.2, "down": 0.1},
        }
    )
    adapter = JevDecisionAdapter(client, clock=FixedClock(NOW))
    request = build_jev_request(snapshot_for(quote))

    result = await adapter.decide(request)

    assert result.ok
    assert result.decision is not None
    assert result.decision.action is Action.LONG
    assert result.decision.direction_5m is Direction.UP
    assert result.decision.regime is Regime.TREND_UP
    assert result.decision.top_two_margin == 0.5
    assert len(request.model_dump_json()) < 16_384
    assert len(adapter.audit_records) == 1
    assert client.requests[0].snapshot_id == request.snapshot_id


@pytest.mark.asyncio
async def test_jev_model_returns_non_executable_intent_and_failure_is_hold(
    quote: QuoteEvent,
) -> None:
    snapshot = snapshot_for(quote)
    good_adapter = JevDecisionAdapter(
        StaticJevClient({"action": "SHORT", "direction_5m": "DOWN", "confidence": 0.6}),
        clock=FixedClock(NOW),
    )
    good_intent = await JevDecisionModel(good_adapter).decide(snapshot)
    assert good_intent.action is Action.SHORT
    assert good_intent.created_at == snapshot.as_of
    assert good_intent.metadata["direction_5m"] == "DOWN"

    bad_adapter = JevDecisionAdapter(
        StaticJevClient({"action": "not-an-action", "direction_5m": "UP"}),
        clock=FixedClock(NOW),
    )
    bad_intent = await JevDecisionModel(bad_adapter).decide(snapshot)
    assert bad_intent.action is Action.HOLD
    assert bad_intent.metadata["failure_code"] == "JEV_MALFORMED_RESPONSE"


def test_jev_request_input_profile_selects_snapshot_sections(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote).model_copy(
        update={"news": {"headline": "test"}, "ml": {"p_up": "0.8"}}
    )

    technical = build_jev_request(snapshot, input_profile=JevInputProfile.TECHNICAL_ONLY)
    full = build_jev_request(snapshot, input_profile=JevInputProfile.FULL_CONTEXT)

    assert "orderbook" not in technical.payload
    assert "portfolio" not in technical.payload
    assert full.payload["news"] == {"headline": "test"}
    assert full.payload["ml"] == {"p_up": "0.8"}
    assert full.payload["input_profile"] == JevInputProfile.FULL_CONTEXT.value


@pytest.mark.asyncio
async def test_jev_output_policy_applies_confidence_threshold(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    adapter = JevDecisionAdapter(
        StaticJevClient(
            {
                "action": "LONG",
                "direction_5m": "UP",
                "probabilities": {"up": 0.7, "flat": 0.2, "down": 0.1},
            }
        ),
        clock=FixedClock(NOW),
    )
    model = JevDecisionModel(
        adapter,
        output_policy=JevOutputPolicy(
            kind=OutputPolicyKind.CONFIDENCE_THRESHOLD,
            thresholds=ThresholdPolicy(min_confidence=Decimal("0.8")),
        ),
    )

    intent = await model.decide(snapshot)

    assert intent.action is Action.HOLD
    assert intent.metadata["output_policy_reason_code"] == "THRESHOLD_REJECTED"
    assert intent.metadata["output_policy_accepted"] is False


@pytest.mark.asyncio
async def test_timeout_and_news_invalidation_fail_closed(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    timeout_adapter = JevDecisionAdapter(
        SlowJevClient(),
        config=JevAdapterConfig(timeout_seconds=0.001),
        clock=FixedClock(NOW),
    )
    intent = await JevDecisionModel(timeout_adapter).decide(snapshot)
    assert intent.action is Action.HOLD
    assert intent.metadata["failure_code"] == "JEV_TIMEOUT"

    news_adapter = JevDecisionAdapter(
        StaticJevClient(
            {
                "action": "LONG",
                "direction_5m": "UP",
                "news_invalidates_signal": True,
            }
        ),
        clock=FixedClock(NOW),
    )
    news_intent = await JevDecisionModel(news_adapter).decide(snapshot)
    assert news_intent.action is Action.HOLD
    assert news_intent.metadata["direction_5m"] == "UP"


@pytest.mark.asyncio
async def test_jev_failure_cannot_reach_paper_broker(quote: QuoteEvent) -> None:
    broker = PaperBroker(clock=FixedClock(NOW))
    broker.update_market(quote)
    pipeline = TradingPipeline(
        feature_engine=InMemoryFeatureEngine(),
        decision_model=JevDecisionModel(
            JevDecisionAdapter(
                StaticJevClient({"action": "bad", "direction_5m": "UP"}),
                clock=FixedClock(NOW),
            )
        ),
        risk_engine=DeterministicRiskEngine(clock=FixedClock(NOW)),
        broker_adapter=broker,
        clock=FixedClock(NOW),
    )

    result = await pipeline.process_event(quote)

    assert result.trade_intent is not None and result.trade_intent.action is Action.HOLD
    assert result.risk_decision is not None
    assert not result.risk_decision.approved
    assert broker.orders == ()


@pytest.mark.asyncio
async def test_rule_baseline_and_single_flight_are_deterministic(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    rule_intent = await RuleDecisionModel().decide(snapshot)
    assert isinstance(rule_intent, TradeIntent)
    assert rule_intent.created_at == snapshot.as_of

    client_started = asyncio.Event()
    release = asyncio.Event()

    class BlockingClient:
        async def decide(self, request: Any) -> Mapping[str, Any]:
            del request
            client_started.set()
            await release.wait()
            return {"action": "LONG", "direction_5m": "UP", "confidence": 0.5}

    model = JevDecisionModel(JevDecisionAdapter(BlockingClient(), clock=FixedClock(NOW)))
    runner = SingleFlightDecisionRunner(model)
    first = asyncio.create_task(runner.decide(snapshot))
    await client_started.wait()
    second = await runner.decide(snapshot)
    release.set()
    first_result = await first

    assert second.action is Action.HOLD
    assert second.metadata["failure_code"] == "DECISION_IN_FLIGHT"
    assert first_result.action is Action.LONG
