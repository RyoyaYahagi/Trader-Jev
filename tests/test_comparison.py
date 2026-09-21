from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from trader_jev.comparison import (
    ComparisonConfig,
    ComparisonOrchestrator,
    PortfolioVariant,
    StrategySpec,
    StrategyVariant,
)
from trader_jev.models import Action, DecisionSnapshot, QuoteEvent, TradeIntent
from trader_jev.portfolio import PortfolioPolicyConfig

from .conftest import NOW


class CountingSignal:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: object = None,
    ) -> TradeIntent:
        del prediction
        self.calls += 1
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=Action.LONG,
            requested_quantity=1,
            strategy_id="comparison-signal",
            reason="shared deterministic signal",
            created_at=snapshot.as_of,
        )


def event_stream(quote: QuoteEvent) -> tuple[QuoteEvent, ...]:
    return (
        quote,
        quote.model_copy(
            update={
                "event_time": NOW + timedelta(seconds=1),
                "received_at": NOW + timedelta(seconds=1),
                "bid": Decimal("101"),
                "ask": Decimal("102"),
            }
        ),
    )


def test_comparison_forks_one_signal_stream_to_multiple_portfolios(quote: QuoteEvent) -> None:
    signal = CountingSignal()
    config = ComparisonConfig(
        run_id="run-1",
        data_id="fixture-quotes",
        universe=(quote.instrument.symbol,),
        symbols=frozenset({quote.instrument.symbol}),
    )
    portfolios = (
        PortfolioVariant(
            portfolio_id="max-1",
            initial_capital=Decimal("10000"),
            policy_config=PortfolioPolicyConfig(max_positions=1),
        ),
        PortfolioVariant(
            portfolio_id="max-3",
            initial_capital=Decimal("10000"),
            policy_config=PortfolioPolicyConfig(max_positions=3),
        ),
    )
    orchestrator = ComparisonOrchestrator(
        config,
        strategies=(
            StrategySpec(StrategyVariant.RULE, "rule", signal),
            StrategySpec(StrategyVariant.JEV_ONLY, "jev", signal),
        ),
        portfolios=portfolios,
    )

    result = asyncio.run(orchestrator.run(event_stream(quote)))

    assert signal.calls == 4
    assert len(result.variants) == 4
    assert result.event_ids == tuple(str(event.event_id) for event in event_stream(quote))
    assert result.config_hash == config.config_hash
    assert result.variant("rule", "max-1").traces is result.variant("rule", "max-3").traces
    assert result.variant("rule", "max-1").metrics.decision_count == 2
    assert result.variant("jev", "max-3").metrics.decision_count == 2
    assert len(result.variant("rule", "max-1").ledger.fills) == 2
    assert len(result.variant("rule", "max-3").ledger.fills) == 2


def test_comparison_config_hash_changes_with_fairness_assumptions(quote: QuoteEvent) -> None:
    base = ComparisonConfig(run_id="run", data_id="fixture")
    changed = base.model_copy(update={"seed": 42, "universe": (quote.instrument.symbol,)})
    assert base.config_hash != changed.config_hash


def test_comparison_filter_keeps_same_market_event_range(quote: QuoteEvent) -> None:
    signal = CountingSignal()
    config = ComparisonConfig(
        run_id="run-filter",
        data_id="fixture",
        data_start=NOW + timedelta(seconds=1),
        symbols=frozenset({quote.instrument.symbol}),
    )
    result = asyncio.run(
        ComparisonOrchestrator(
            config,
            strategies=(StrategySpec(StrategyVariant.RULE, "rule", signal),),
            portfolios=(PortfolioVariant(portfolio_id="paper"),),
        ).run(event_stream(quote))
    )
    assert len(result.event_ids) == 1
    assert signal.calls == 1
