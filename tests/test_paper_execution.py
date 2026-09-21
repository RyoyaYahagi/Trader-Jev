from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

from trader_jev.clock import FixedClock
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    Action,
    CapitalPolicy,
    DecisionSnapshot,
    EntryModel,
    OrderIntent,
    OrderStatus,
    PortfolioState,
    PredictionOutput,
    QuoteEvent,
    TradeIntent,
)
from trader_jev.pipeline import TradingPipeline
from trader_jev.portfolio import (
    PaperPortfolioPolicy,
    PortfolioLedger,
    PortfolioPolicyConfig,
)
from trader_jev.risk import DeterministicRiskEngine, FixedQuantityPortfolioPolicy

from .conftest import NOW


def order_for(
    quote: QuoteEvent,
    *,
    quantity: int = 5,
    action: Action = Action.LONG,
    **kwargs: Any,
) -> OrderIntent:
    return OrderIntent(
        source_trade_intent_id=quote.event_id,
        instrument=quote.instrument,
        side=action,
        quantity=quantity,
        created_at=quote.received_at,
        **kwargs,
    )


def test_market_fill_costs_and_ledger_rebuild(quote: QuoteEvent) -> None:
    ledger = PortfolioLedger(PortfolioState(portfolio_id="paper", cash=Decimal("10000")))
    broker = PaperBroker(
        ExecutionConfig(fee_bps=Decimal("10"), slippage_bps=Decimal("10")),
        clock=FixedClock(NOW),
        ledger=ledger,
    )
    broker.update_market(quote)

    event = asyncio.run(broker.submit(order_for(quote)))

    assert event.status is OrderStatus.FILLED
    assert len(broker.fills) == 1
    fill = broker.fills[0]
    assert fill.price == Decimal("101.101")
    assert fill.fees == Decimal("0.505505")
    assert ledger.state.positions == {"TEST": 5}
    assert ledger.state.cash == Decimal("9493.989495")
    assert ledger.rebuild() == ledger.state


def test_limit_no_fill_then_fill_and_limit_then_market(quote: QuoteEvent) -> None:
    clock = FixedClock(NOW)
    broker = PaperBroker(
        ExecutionConfig(entry_model=EntryModel.LIMIT, limit_timeout_seconds=5),
        clock=clock,
    )
    broker.update_market(quote)
    pending = asyncio.run(
        broker.submit(
            order_for(quote, limit_price=Decimal("100.5"), metadata={"entry_model": "LIMIT"})
        )
    )
    assert pending.status is OrderStatus.ACCEPTED
    assert not broker.fills

    improved = quote.model_copy(
        update={
            "event_time": NOW + timedelta(seconds=1),
            "received_at": NOW + timedelta(seconds=1),
            "ask": Decimal("100.4"),
        }
    )
    fills = broker.update_market(improved)
    assert fills[-1].status is OrderStatus.FILLED
    assert broker.fills[0].price == Decimal("100.4")

    timeout_clock = FixedClock(NOW)
    timeout_broker = PaperBroker(
        ExecutionConfig(entry_model=EntryModel.LIMIT_THEN_MARKET, limit_timeout_seconds=5),
        clock=timeout_clock,
    )
    timeout_broker.update_market(quote)
    timeout_order = order_for(
        quote,
        limit_price=Decimal("100.5"),
        metadata={"entry_model": "LIMIT_THEN_MARKET"},
    )
    assert asyncio.run(timeout_broker.submit(timeout_order)).status is OrderStatus.ACCEPTED
    timeout_clock.set(NOW + timedelta(seconds=6))
    timeout_events = timeout_broker.update_market(quote)
    assert timeout_events[-1].status is OrderStatus.FILLED
    assert timeout_broker.fills[0].price == Decimal("101")


def test_partial_fill_and_portfolio_constraints(quote: QuoteEvent) -> None:
    broker = PaperBroker(
        ExecutionConfig(partial_fill_ratio=Decimal("0.5")),
        clock=FixedClock(NOW),
    )
    broker.update_market(quote)
    event = asyncio.run(broker.submit(order_for(quote, quantity=10)))

    assert event.status is OrderStatus.PARTIALLY_FILLED
    assert broker.fills[0].quantity == 5
    assert len(broker.pending_orders) == 1

    policy = PaperPortfolioPolicy(
        PortfolioPolicyConfig(
            capital_policy=CapitalPolicy.REALISTIC_100K,
            max_positions=1,
        )
    )
    intent = TradeIntent(
        snapshot_id=quote.event_id,
        instrument=quote.instrument,
        action=Action.LONG,
        requested_quantity=1000,
        limit_price=Decimal("100"),
        strategy_id="test",
        reason="test",
        created_at=NOW,
    )
    quantity = policy.quantity_for(
        intent,
        PortfolioState(portfolio_id="paper", cash=Decimal("100000")),
    )
    assert quantity == 1000


def test_short_close_latency_and_cancel_are_auditable(quote: QuoteEvent) -> None:
    ledger = PortfolioLedger(PortfolioState(portfolio_id="paper", cash=Decimal("10000")))
    broker = PaperBroker(
        ExecutionConfig(latency_ms=5),
        clock=FixedClock(NOW),
        ledger=ledger,
    )
    broker.update_market(quote)
    short_order = order_for(quote, quantity=2, action=Action.SHORT)
    short_event = asyncio.run(broker.submit(short_order))

    assert short_event.status is OrderStatus.FILLED
    assert broker.fills[0].occurred_at == NOW + timedelta(milliseconds=5)
    assert ledger.state.positions == {"TEST": -2}

    long_order = order_for(quote, quantity=2, action=Action.LONG)
    asyncio.run(broker.submit(long_order))
    assert ledger.state.positions == {}
    assert ledger.state.realized_pnl == Decimal("-2")

    limit_broker = PaperBroker(clock=FixedClock(NOW))
    limit_broker.update_market(quote)
    pending_order = order_for(quote, limit_price=Decimal("99"))
    assert asyncio.run(limit_broker.submit(pending_order)).status is OrderStatus.ACCEPTED
    assert asyncio.run(limit_broker.cancel(pending_order)).status is OrderStatus.CANCELED
    assert not limit_broker.pending_orders


class E2EDecisionModel:
    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: PredictionOutput | None = None,
    ) -> TradeIntent:
        del prediction
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=Action.LONG,
            requested_quantity=2,
            strategy_id="e2e",
            reason="fixture signal",
            created_at=snapshot.as_of,
        )


def test_replay_decision_risk_paper_fill_and_portfolio_e2e(quote: QuoteEvent) -> None:
    ledger = PortfolioLedger(PortfolioState(portfolio_id="paper", cash=Decimal("1000")))
    broker = PaperBroker(clock=FixedClock(NOW), ledger=ledger)
    broker.update_market(quote)
    pipeline = TradingPipeline(
        feature_engine=InMemoryFeatureEngine(),
        decision_model=E2EDecisionModel(),
        risk_engine=DeterministicRiskEngine(
            portfolio_policy=FixedQuantityPortfolioPolicy(),
            clock=FixedClock(NOW),
        ),
        broker_adapter=broker,
        clock=FixedClock(NOW),
    )

    result = asyncio.run(pipeline.process_event(quote, ledger.state))

    assert result.failure_code is None
    assert result.order_event is not None
    assert result.order_event.status is OrderStatus.FILLED
    assert len(broker.fills) == 1
    assert ledger.state.positions == {"TEST": 2}
