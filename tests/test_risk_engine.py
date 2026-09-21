from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trader_jev.clock import FixedClock
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    PortfolioState,
    QuoteEvent,
    RiskProfile,
    TradeIntent,
)
from trader_jev.risk import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskProfileLimits,
)

from .conftest import NOW


def snapshot_for(quote: QuoteEvent) -> DecisionSnapshot:
    engine = InMemoryFeatureEngine()
    engine.update(quote)
    return engine.snapshot(quote.instrument, quote.received_at)


def intent_for(snapshot: DecisionSnapshot, *, strategy_id: str = "risk-engine") -> TradeIntent:
    return TradeIntent(
        snapshot_id=snapshot.snapshot_id,
        instrument=snapshot.instrument,
        action=Action.LONG,
        requested_quantity=1,
        strategy_id=strategy_id,
        reason="test signal",
        created_at=snapshot.as_of,
    )


def paper_portfolio(**kwargs: Any) -> PortfolioState:
    return PortfolioState(
        portfolio_id="paper",
        cash=Decimal("100000"),
        **kwargs,
    )


def test_risk_profiles_switch_limits_without_changing_interface(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    engine = DeterministicRiskEngine(
        config=RiskConfig(risk_profile=RiskProfile.CONSERVATIVE),
        clock=FixedClock(NOW),
    )
    portfolio = paper_portfolio(positions={"OTHER": 1})

    conservative = engine.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert not conservative.approved
    assert conservative.reason_code == "MAX_POSITIONS"

    engine.set_profile(RiskProfile.AGGRESSIVE)
    aggressive = engine.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert aggressive.approved
    assert aggressive.order_intent is not None


def test_explicit_zero_and_capital_limits_are_enforced(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    blocked = DeterministicRiskEngine(
        config=RiskConfig(
            max_open_orders=0,
            max_order_notional=Decimal("100"),
            profile_limits=RiskProfileLimits(max_open_orders=0),
        ),
        clock=FixedClock(NOW),
    )
    decision = blocked.evaluate(intent_for(snapshot), snapshot, paper_portfolio())
    assert not decision.approved
    assert decision.reason_code == "MAX_OPEN_ORDERS"

    capital_limited = DeterministicRiskEngine(
        config=RiskConfig(max_order_notional=Decimal("100")),
        clock=FixedClock(NOW),
    )
    decision = capital_limited.evaluate(intent_for(snapshot), snapshot, paper_portfolio())
    assert not decision.approved
    assert decision.reason_code == "MAX_ORDER_NOTIONAL"


def test_position_notional_daily_loss_and_drawdown_are_fail_closed(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    position_limited = DeterministicRiskEngine(
        config=RiskConfig(max_position_notional=Decimal("50")),
        clock=FixedClock(NOW),
    )
    decision = position_limited.evaluate(intent_for(snapshot), snapshot, paper_portfolio())
    assert not decision.approved
    assert decision.reason_code == "MAX_POSITION_NOTIONAL"

    daily_limited = DeterministicRiskEngine(
        config=RiskConfig(max_daily_loss=Decimal("100")),
        clock=FixedClock(NOW),
    )
    decision = daily_limited.evaluate(
        intent_for(snapshot), snapshot, paper_portfolio(daily_pnl=Decimal("-100"))
    )
    assert not decision.approved
    assert decision.reason_code == "MAX_DAILY_LOSS"

    drawdown_limited = DeterministicRiskEngine(
        config=RiskConfig(max_drawdown=Decimal("50")),
        clock=FixedClock(NOW),
    )
    decision = drawdown_limited.evaluate(
        intent_for(snapshot), snapshot, paper_portfolio(drawdown=Decimal("50"))
    )
    assert not decision.approved
    assert decision.reason_code == "MAX_DRAWDOWN"


def test_duplicate_cooldown_and_restart_state_recovery(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    clock = FixedClock(NOW)
    config = RiskConfig(cooldown_seconds=60, max_data_age_seconds=120)
    engine = DeterministicRiskEngine(config=config, clock=clock)
    portfolio = paper_portfolio()

    first = engine.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert first.approved
    duplicate = engine.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert not duplicate.approved
    assert duplicate.reason_code == "DUPLICATE_ORDER"

    next_snapshot = snapshot.model_copy(update={"snapshot_id": uuid4()})
    cooldown = engine.evaluate(intent_for(next_snapshot), next_snapshot, portfolio)
    assert not cooldown.approved
    assert cooldown.reason_code == "DECISION_COOLDOWN"

    restored = DeterministicRiskEngine(config=config, clock=clock)
    restored.restore_state(engine.export_state())
    restored_duplicate = restored.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert not restored_duplicate.approved
    assert restored_duplicate.reason_code == "DUPLICATE_ORDER"

    clock.set(NOW + timedelta(seconds=61))
    after_cooldown = restored.evaluate(intent_for(next_snapshot), next_snapshot, portfolio)
    assert after_cooldown.approved


def test_circuit_breaker_and_kill_switch_block_orders(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    portfolio = paper_portfolio()
    engine = DeterministicRiskEngine(
        config=RiskConfig(max_consecutive_errors=2),
        clock=FixedClock(NOW),
    )
    engine.record_error()
    engine.record_error()
    decision = engine.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert not decision.approved
    assert decision.reason_code == "CIRCUIT_BREAKER"

    kill_switch = DeterministicRiskEngine(clock=FixedClock(NOW))
    kill_switch.activate_kill_switch(cancel_all=True)
    decision = kill_switch.evaluate(intent_for(snapshot), snapshot, portfolio)
    assert not decision.approved
    assert decision.reason_code == "KILL_SWITCH"


def test_policy_and_metadata_errors_fail_closed(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)

    class BrokenPolicy:
        def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
            del intent, portfolio
            raise RuntimeError("sizing unavailable")

    policy_error = DeterministicRiskEngine(
        portfolio_policy=BrokenPolicy(),
        clock=FixedClock(NOW),
    )
    decision = policy_error.evaluate(intent_for(snapshot), snapshot, paper_portfolio())
    assert not decision.approved
    assert decision.reason_code == "PORTFOLIO_POLICY_ERROR"

    malformed_model = snapshot.model_copy(update={"ml": {"trained_until": "not-a-timestamp"}})
    model_error = DeterministicRiskEngine(
        config=RiskConfig(max_model_age_seconds=60),
        clock=FixedClock(NOW),
    )
    decision = model_error.evaluate(intent_for(malformed_model), malformed_model, paper_portfolio())
    assert not decision.approved
    assert decision.reason_code == "MODEL_METADATA_INVALID"


def test_long_running_unique_decisions_have_no_duplicate_approvals(quote: QuoteEvent) -> None:
    snapshot = snapshot_for(quote)
    engine = DeterministicRiskEngine(clock=FixedClock(NOW))
    portfolio = paper_portfolio()
    approved = 0

    for _ in range(120):
        current = snapshot.model_copy(update={"snapshot_id": uuid4()})
        decision = engine.evaluate(intent_for(current), current, portfolio)
        approved += decision.approved

    assert approved == 120
    assert len(engine.runtime_state.seen_keys) == 120
    assert len(engine.audit_records) == 120
