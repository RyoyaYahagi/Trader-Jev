from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trader_jev.clock import FixedClock
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    ExecutionMode,
    PortfolioState,
    QuoteEvent,
    TradeIntent,
)
from trader_jev.risk import DeterministicRiskEngine, FixedQuantityPortfolioPolicy, RiskConfig

from .conftest import NOW


def snapshot_for(quote: QuoteEvent) -> DecisionSnapshot:
    engine = InMemoryFeatureEngine()
    engine.update(quote)
    return engine.snapshot(quote.instrument, quote.received_at)


def intent_for(snapshot: DecisionSnapshot, action: Action = Action.LONG) -> TradeIntent:
    return TradeIntent(
        snapshot_id=snapshot.snapshot_id,
        instrument=snapshot.instrument,
        action=action,
        requested_quantity=1,
        strategy_id="risk-test",
        reason="test",
        created_at=snapshot.as_of,
    )


def test_stale_data_is_rejected(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    engine = DeterministicRiskEngine(
        config=RiskConfig(max_data_age_seconds=1),
        clock=FixedClock(NOW + timedelta(seconds=2)),
    )

    decision = engine.evaluate(
        intent_for(snapshot),
        snapshot,
        PortfolioState(portfolio_id="paper", cash=Decimal("100000")),
    )

    assert not decision.approved
    assert decision.reason_code == "STALE_DATA"


def test_live_requires_both_flags(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    engine = DeterministicRiskEngine(
        config=RiskConfig(
            execution_mode=ExecutionMode.LIVE,
            live_trading=True,
            live_armed=False,
            allowed_symbols=frozenset({quote.instrument.symbol}),
        ),
        portfolio_policy=FixedQuantityPortfolioPolicy(),
        clock=FixedClock(NOW),
    )

    decision = engine.evaluate(
        intent_for(snapshot),
        snapshot,
        PortfolioState(portfolio_id="live", cash=Decimal("100000")),
    )

    assert not decision.approved
    assert decision.reason_code == "LIVE_NOT_ARMED"
