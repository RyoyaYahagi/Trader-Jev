"""Deterministic strategy/portfolio comparison orchestration for Paper runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from pydantic import Field

from trader_jev.clock import ReplayClock
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.interfaces import DecisionModel, FeatureEngine, PredictionModel
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    DomainModel,
    ExecutionMode,
    FillEvent,
    Market,
    MarketEvent,
    OrderEvent,
    PortfolioState,
    PredictionOutput,
    RiskDecision,
    TradeIntent,
)
from trader_jev.portfolio import PaperPortfolioPolicy, PortfolioLedger, PortfolioPolicyConfig
from trader_jev.replay import (
    PointInTimeViolation,
    event_available_at,
    validate_event_point_in_time,
    validate_prediction_metadata,
)
from trader_jev.risk import DeterministicRiskEngine, RiskConfig


class StrategyVariant(StrEnum):
    RULE = "RULE"
    JEV_ONLY = "JEV_ONLY"
    ML_ONLY = "ML_ONLY"
    JEV_ML = "JEV_ML"


class NewsVariant(StrEnum):
    NONE = "NONE"
    HEADLINE = "HEADLINE"
    STRUCTURED = "STRUCTURED"


class EntryComparison(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    LIMIT_THEN_MARKET = "LIMIT_THEN_MARKET"


class ExitComparison(StrEnum):
    FIXED_TIME = "FIXED_TIME"
    HYBRID = "HYBRID"


class ComparisonConfig(DomainModel):
    """All shared assumptions that make one comparison reproducible."""

    run_id: str = Field(min_length=1)
    data_id: str = Field(min_length=1)
    data_start: datetime | None = None
    data_end: datetime | None = None
    universe: tuple[str, ...] = ()
    markets: frozenset[Market] = Field(default_factory=lambda: frozenset[Market]())
    symbols: frozenset[str] = Field(default_factory=lambda: frozenset[str]())
    seed: int = 0
    strategy_variants: tuple[StrategyVariant, ...] = (
        StrategyVariant.RULE,
        StrategyVariant.JEV_ONLY,
        StrategyVariant.ML_ONLY,
        StrategyVariant.JEV_ML,
    )
    news_variant: NewsVariant = NewsVariant.NONE
    orderbook_ablation: bool = False
    entry_comparison: EntryComparison = EntryComparison.MARKET
    exit_comparison: ExitComparison = ExitComparison.FIXED_TIME
    model_timeout_seconds: float = Field(default=5.0, gt=0)
    decision_timeout_seconds: float = Field(default=5.0, gt=0)
    risk_config: RiskConfig = Field(default_factory=RiskConfig)

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PortfolioVariant(DomainModel):
    """One capital/position/risk branch sharing the same signal stream."""

    portfolio_id: str = Field(min_length=1)
    name: str = "Paper comparison portfolio"
    market: Market | None = None
    initial_capital: Decimal = Field(default=Decimal("100000"), ge=Decimal("0"))
    policy_config: PortfolioPolicyConfig = Field(default_factory=PortfolioPolicyConfig)
    execution_config: ExecutionConfig = Field(default_factory=ExecutionConfig)


@dataclass(frozen=True)
class StrategySpec:
    variant: StrategyVariant
    strategy_id: str
    decision_model: DecisionModel | Callable[[], DecisionModel]
    prediction_model: PredictionModel | Callable[[], PredictionModel] | None = None


@dataclass(frozen=True)
class DecisionTrace:
    event: MarketEvent
    snapshot: DecisionSnapshot | None
    prediction: PredictionOutput | None
    intent: TradeIntent | None
    failure_code: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class ExecutionRecord:
    event_id: str
    snapshot_id: str | None
    trade_intent: TradeIntent | None
    risk_decision: RiskDecision | None
    order_event: OrderEvent | None
    failure_code: str | None = None


class ComparisonMetrics(DomainModel):
    net_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    trade_count: int = 0
    win_rate: Decimal | None = None
    average_trade: Decimal | None = None
    turnover: Decimal = Decimal("0")
    fill_ratio: Decimal | None = None
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    exposure: Decimal = Decimal("0")
    decision_count: int = 0
    rejected_count: int = 0
    decision_latency_ms: float | None = None
    order_latency_ms: float | None = None


@dataclass(frozen=True)
class VariantResult:
    strategy_variant: StrategyVariant
    strategy_id: str
    portfolio: PortfolioVariant
    ledger: PortfolioLedger
    traces: tuple[DecisionTrace, ...]
    executions: tuple[ExecutionRecord, ...]
    metrics: ComparisonMetrics

    @property
    def key(self) -> str:
        return f"{self.strategy_id}:{self.portfolio.portfolio_id}"


@dataclass(frozen=True)
class ComparisonResult:
    config: ComparisonConfig
    config_hash: str
    event_ids: tuple[str, ...]
    event_timestamps: tuple[datetime, ...]
    variants: tuple[VariantResult, ...]

    def variant(self, strategy_id: str, portfolio_id: str) -> VariantResult:
        key = f"{strategy_id}:{portfolio_id}"
        for result in self.variants:
            if result.key == key:
                return result
        raise KeyError(f"unknown comparison variant: {key}")


class ComparisonOrchestrator:
    """Run strategy signals once, then fork the exact intents to Paper portfolios."""

    def __init__(
        self,
        config: ComparisonConfig,
        strategies: Sequence[StrategySpec],
        portfolios: Sequence[PortfolioVariant],
        *,
        feature_engine_factory: Callable[[], FeatureEngine] = InMemoryFeatureEngine,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy is required")
        if not portfolios:
            raise ValueError("at least one portfolio is required")
        if len({spec.strategy_id for spec in strategies}) != len(strategies):
            raise ValueError("strategy_id values must be unique")
        if len({portfolio.portfolio_id for portfolio in portfolios}) != len(portfolios):
            raise ValueError("portfolio_id values must be unique")
        self.config = config
        self.strategies = tuple(strategies)
        self.portfolios = tuple(portfolios)
        self._feature_engine_factory = feature_engine_factory

    async def run(self, events: Sequence[MarketEvent]) -> ComparisonResult:
        """Replay one immutable event stream and return all comparison branches."""

        ordered = self._common_event_stream(events)
        traces_by_strategy: dict[str, tuple[DecisionTrace, ...]] = {}
        for strategy in self.strategies:
            traces_by_strategy[strategy.strategy_id] = await self._build_traces(
                ordered, strategy
            )

        variants: list[VariantResult] = []
        for strategy in self.strategies:
            traces = traces_by_strategy[strategy.strategy_id]
            for portfolio in self.portfolios:
                variants.append(await self._execute(strategy, portfolio, traces))
        return ComparisonResult(
            config=self.config,
            config_hash=self.config.config_hash,
            event_ids=tuple(str(event.event_id) for event in ordered),
            event_timestamps=tuple(event_available_at(event) for event in ordered),
            variants=tuple(variants),
        )

    def run_sync(self, events: Sequence[MarketEvent]) -> ComparisonResult:
        return asyncio.run(self.run(events))

    def _common_event_stream(self, events: Sequence[MarketEvent]) -> tuple[MarketEvent, ...]:
        selected = [
            event
            for event in events
            if (
                self.config.data_start is None
                or event_available_at(event) >= self.config.data_start
            )
            and (self.config.data_end is None or event_available_at(event) < self.config.data_end)
            and (not self.config.markets or event.instrument.market in self.config.markets)
            and (not self.config.symbols or event.instrument.symbol in self.config.symbols)
        ]
        selected.sort(
            key=lambda event: (
                event_available_at(event),
                event.event_time,
                str(event.event_id),
            )
        )
        return tuple(selected)

    async def _build_traces(
        self,
        events: Sequence[MarketEvent],
        strategy: StrategySpec,
    ) -> tuple[DecisionTrace, ...]:
        feature_engine = self._feature_engine_factory()
        decision_model = _new_component(strategy.decision_model)
        prediction_model = (
            _new_component(strategy.prediction_model)
            if strategy.prediction_model is not None
            else None
        )
        clock = ReplayClock()
        traces: list[DecisionTrace] = []
        for event in events:
            clock.advance_to(event_available_at(event))
            try:
                validate_event_point_in_time(event, clock.now())
                feature_engine.update(event)
                snapshot = feature_engine.snapshot(event.instrument, clock.now())
            except PointInTimeViolation as exc:
                traces.append(
                    DecisionTrace(event, None, None, None, "POINT_IN_TIME_VIOLATION", str(exc))
                )
                continue
            except Exception as exc:
                traces.append(
                    DecisionTrace(event, None, None, None, "FEATURE_ENGINE_ERROR", str(exc))
                )
                continue

            prediction: PredictionOutput | None = None
            try:
                if prediction_model is not None:
                    prediction = await asyncio.wait_for(
                        prediction_model.predict(snapshot), self.config.model_timeout_seconds
                    )
                    validate_prediction_metadata(prediction, snapshot.as_of)
                intent = await asyncio.wait_for(
                    decision_model.decide(snapshot, prediction),
                    self.config.decision_timeout_seconds,
                )
                if intent.snapshot_id != snapshot.snapshot_id:
                    raise ValueError("decision returned a different snapshot_id")
                if intent.created_at > snapshot.as_of:
                    raise PointInTimeViolation("decision was created after snapshot as_of")
            except PointInTimeViolation as exc:
                traces.append(
                    DecisionTrace(
                        event,
                        snapshot,
                        prediction,
                        None,
                        "POINT_IN_TIME_VIOLATION",
                        str(exc),
                    )
                )
            except TimeoutError as exc:
                traces.append(
                    DecisionTrace(event, snapshot, prediction, None, "DECISION_TIMEOUT", str(exc))
                )
            except Exception as exc:
                traces.append(
                    DecisionTrace(event, snapshot, prediction, None, "DECISION_ERROR", str(exc))
                )
            else:
                traces.append(DecisionTrace(event, snapshot, prediction, intent))
        return tuple(traces)

    async def _execute(
        self,
        strategy: StrategySpec,
        portfolio: PortfolioVariant,
        traces: Sequence[DecisionTrace],
    ) -> VariantResult:
        ledger = PortfolioLedger(
            PortfolioState(
                portfolio_id=portfolio.portfolio_id,
                cash=portfolio.initial_capital,
                initial_capital=portfolio.initial_capital,
            )
        )
        clock = ReplayClock()
        broker = PaperBroker(portfolio.execution_config, clock=clock, ledger=ledger)
        policy = PaperPortfolioPolicy(portfolio.policy_config)
        risk_config = self.config.risk_config.model_copy(
            update={
                "risk_profile": portfolio.policy_config.risk_profile,
                "execution_mode": ExecutionMode.PAPER,
            }
        )
        risk = DeterministicRiskEngine(
            risk_config,
            portfolio_policy=policy,
            clock=clock,
        )
        executions: list[ExecutionRecord] = []
        for trace in traces:
            if trace.snapshot is None:
                executions.append(
                    ExecutionRecord(
                        event_id=str(trace.event.event_id),
                        snapshot_id=None,
                        trade_intent=None,
                        risk_decision=None,
                        order_event=None,
                        failure_code=trace.failure_code,
                    )
                )
                continue
            clock.advance_to(trace.snapshot.as_of)
            if hasattr(trace.event, "bid") and hasattr(trace.event, "ask"):
                broker.update_market(trace.event)  # type: ignore[arg-type]
            if trace.intent is None:
                executions.append(
                    ExecutionRecord(
                        event_id=str(trace.event.event_id),
                        snapshot_id=str(trace.snapshot.snapshot_id),
                        trade_intent=None,
                        risk_decision=None,
                        order_event=None,
                        failure_code=trace.failure_code or "NO_INTENT",
                    )
                )
                continue
            decision = risk.evaluate(trace.intent, trace.snapshot, ledger.state)
            if not decision.approved or decision.order_intent is None:
                executions.append(
                    ExecutionRecord(
                        event_id=str(trace.event.event_id),
                        snapshot_id=str(trace.snapshot.snapshot_id),
                        trade_intent=trace.intent,
                        risk_decision=decision,
                        order_event=None,
                        failure_code=decision.reason_code,
                    )
                )
                continue
            order_event = await broker.submit(decision.order_intent)
            executions.append(
                ExecutionRecord(
                    event_id=str(trace.event.event_id),
                    snapshot_id=str(trace.snapshot.snapshot_id),
                    trade_intent=trace.intent,
                    risk_decision=decision,
                    order_event=order_event,
                )
            )
        metrics = _metrics(ledger, traces, executions, portfolio.initial_capital)
        return VariantResult(
            strategy_variant=strategy.variant,
            strategy_id=strategy.strategy_id,
            portfolio=portfolio,
            ledger=ledger,
            traces=tuple(traces),
            executions=tuple(executions),
            metrics=metrics,
        )


def _new_component[T](component: T | Callable[[], T]) -> T:
    is_factory = callable(component) and not hasattr(component, "decide") and not hasattr(
        component, "predict"
    )
    if is_factory:
        factory = cast(Callable[[], T], component)
        return factory()
    return cast(T, component)


def _metrics(
    ledger: PortfolioLedger,
    traces: Sequence[DecisionTrace],
    executions: Sequence[ExecutionRecord],
    initial_capital: Decimal,
) -> ComparisonMetrics:
    fills: tuple[FillEvent, ...] = ledger.fills
    orders = ledger.orders
    closed_like = [fill for fill in fills if fill.side is not None]
    wins = sum(
        1
        for fill in closed_like
        if (
            fill.slippage <= Decimal("0")
            if fill.side is Action.LONG
            else fill.slippage >= Decimal("0")
        )
    )
    filled_quantity = sum(fill.quantity for fill in fills)
    ordered_quantity = sum(order.quantity for order in orders)
    turnover = sum((abs(fill.price * fill.quantity) for fill in fills), Decimal("0"))
    fees = sum((fill.fees for fill in fills), Decimal("0"))
    slippage = sum((abs(fill.slippage * fill.quantity) for fill in fills), Decimal("0"))
    state = ledger.state
    equity = state.equity or state.cash
    net = equity - initial_capital
    trade_count = len(fills)
    return ComparisonMetrics(
        net_pnl=net,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=state.unrealized_pnl,
        max_drawdown=state.drawdown,
        trade_count=trade_count,
        win_rate=Decimal(wins) / Decimal(trade_count) if trade_count else None,
        average_trade=net / Decimal(trade_count) if trade_count else None,
        turnover=turnover,
        fill_ratio=Decimal(filled_quantity) / Decimal(ordered_quantity)
        if ordered_quantity
        else None,
        fees=fees,
        slippage=slippage,
        exposure=sum(
            (
                abs(quantity)
                * state.mark_prices.get(symbol, state.average_prices.get(symbol, Decimal("0")))
                for symbol, quantity in state.positions.items()
            ),
            Decimal("0"),
        ),
        decision_count=sum(trace.intent is not None for trace in traces),
        rejected_count=sum(
            execution.risk_decision is not None and not execution.risk_decision.approved
            for execution in executions
        ),
    )


__all__ = [
    "ComparisonConfig",
    "ComparisonMetrics",
    "ComparisonOrchestrator",
    "ComparisonResult",
    "EntryComparison",
    "ExitComparison",
    "NewsVariant",
    "PortfolioVariant",
    "StrategySpec",
    "StrategyVariant",
    "VariantResult",
]
