"""Long-running, read-only-Moomoo to PaperBroker forward validation.

This module deliberately has no broker or account API integration. It polls
normalized quotes from :class:`MoomooMarketDataAdapter`, runs a selectable Rule
or Jev decision branch, and sends only ``ExecutionMode.PAPER`` orders to
:class:`PaperBroker`. The resulting ledger is written as a secret-free JSON
report by the command-line entry point.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from math import ceil
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from trader_jev.cli import load_env_file
from trader_jev.clock import LiveClock
from trader_jev.dashboard import DashboardReadModel
from trader_jev.decision import (
    JevAdapterConfig,
    JevAdapterResult,
    JevClient,
    JevDecisionAdapter,
    JevDecisionModel,
    RuleConfig,
    RuleDecisionModel,
)
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.experiments import JevInputProfile, JevOutputPolicy
from trader_jev.features import FeatureEngineConfig, InMemoryFeatureEngine
from trader_jev.fees import MoomooFeeSchedule
from trader_jev.interfaces import Clock, DecisionModel, MarketDataAdapter
from trader_jev.jev_http import JevHttpClient
from trader_jev.jev_usage import (
    JevPricingConfig,
    JevUsageRecord,
    JevUsageSummary,
    summarize_usage,
    usage_records_from_audits,
)
from trader_jev.models import (
    Action,
    DomainModel,
    ExecutionMode,
    FillEvent,
    InstrumentMetadata,
    Market,
    OrderEvent,
    OrderIntent,
    PortfolioState,
    QuoteEvent,
    RiskProfile,
    TradeIntent,
    TradingSession,
)
from trader_jev.moomoo import MoomooClientConfig, MoomooMarketDataAdapter
from trader_jev.nasdaq_calendar import NasdaqCalendar, NasdaqSession
from trader_jev.observability import TradeRecord
from trader_jev.pipeline import PipelineConfig, PipelineResult, TradingPipeline
from trader_jev.portfolio import ExitMode, HybridExitPolicy, PortfolioLedger
from trader_jev.risk import (
    DeterministicRiskEngine,
    RiskConfig,
    RiskProfileLimits,
)


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _require_summary_time(value: datetime) -> datetime:
    return _require_aware(value, "summary timestamp")


DEFAULT_US_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AVGO",
    "AMD",
    "JPM",
)

DEFAULT_USD_JPY_RATE = Decimal("157.49")
DEFAULT_USD_JPY_AS_OF = "2026-09-18T17:00:00+09:00"
DEFAULT_USD_JPY_SOURCE = "Bank of Japan Foreign Exchange Rates (17:00 JST)"
DEFAULT_UNCONSTRAINED_JPY_REFERENCE = Decimal("500000")


class ForwardDecisionMode(StrEnum):
    """Decision branch used by one independent forward-paper portfolio."""

    RULE = "RULE"
    JEV = "JEV"

    @property
    def label(self) -> str:
        return "ルール判定" if self is self.RULE else "Jev判定"


def _jpy_to_usd(jpy_amount: Decimal, usd_jpy_rate: Decimal) -> Decimal:
    """Convert JPY to USD without allowing the simulated cash to exceed JPY cash."""

    return (jpy_amount / usd_jpy_rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


class CapitalScenario(DomainModel):
    """One independently simulated capital condition."""

    scenario_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    initial_capital: Decimal = Field(gt=Decimal("0"))
    capital_constraint: Decimal | None = Field(default=None, gt=Decimal("0"))
    jpy_capital: Decimal | None = Field(default=None, gt=Decimal("0"))
    usd_jpy_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    fx_as_of: str | None = Field(default=None, min_length=1)
    fx_source: str | None = Field(default=None, min_length=1)


DEFAULT_CAPITAL_SCENARIOS: tuple[CapitalScenario, ...] = (
    CapitalScenario(
        scenario_id="jpy-100k",
        label="10万制約",
        initial_capital=_jpy_to_usd(Decimal("100000"), DEFAULT_USD_JPY_RATE),
        capital_constraint=_jpy_to_usd(Decimal("100000"), DEFAULT_USD_JPY_RATE),
        jpy_capital=Decimal("100000"),
        usd_jpy_rate=DEFAULT_USD_JPY_RATE,
        fx_as_of=DEFAULT_USD_JPY_AS_OF,
        fx_source=DEFAULT_USD_JPY_SOURCE,
    ),
    CapitalScenario(
        scenario_id="jpy-250k",
        label="25万制約",
        initial_capital=_jpy_to_usd(Decimal("250000"), DEFAULT_USD_JPY_RATE),
        capital_constraint=_jpy_to_usd(Decimal("250000"), DEFAULT_USD_JPY_RATE),
        jpy_capital=Decimal("250000"),
        usd_jpy_rate=DEFAULT_USD_JPY_RATE,
        fx_as_of=DEFAULT_USD_JPY_AS_OF,
        fx_source=DEFAULT_USD_JPY_SOURCE,
    ),
    CapitalScenario(
        scenario_id="jpy-500k",
        label="50万制約",
        initial_capital=_jpy_to_usd(Decimal("500000"), DEFAULT_USD_JPY_RATE),
        capital_constraint=_jpy_to_usd(Decimal("500000"), DEFAULT_USD_JPY_RATE),
        jpy_capital=Decimal("500000"),
        usd_jpy_rate=DEFAULT_USD_JPY_RATE,
        fx_as_of=DEFAULT_USD_JPY_AS_OF,
        fx_source=DEFAULT_USD_JPY_SOURCE,
    ),
    CapitalScenario(
        scenario_id="unconstrained",
        label="制約なし",
        initial_capital=_jpy_to_usd(
            DEFAULT_UNCONSTRAINED_JPY_REFERENCE,
            DEFAULT_USD_JPY_RATE,
        ),
        capital_constraint=None,
        jpy_capital=DEFAULT_UNCONSTRAINED_JPY_REFERENCE,
        usd_jpy_rate=DEFAULT_USD_JPY_RATE,
        fx_as_of=DEFAULT_USD_JPY_AS_OF,
        fx_source=DEFAULT_USD_JPY_SOURCE,
    ),
)


class ForwardPaperConfig(DomainModel):
    """Explicit parameters for one forward paper session."""

    symbols: tuple[str, ...] = DEFAULT_US_SYMBOLS
    initial_capital: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    runtime_seconds: int = Field(default=3600, gt=0)
    prediction_horizon_minutes: int = Field(default=5, gt=0)
    decision_cadence_seconds: float = Field(default=30.0, ge=0)
    max_positions: int = Field(default=3, gt=0)
    max_holding_seconds: int = Field(default=900, gt=0)
    exit_mode: ExitMode = ExitMode.ATR
    atr_period: int = Field(default=14, gt=0)
    stop_atr_multiple: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0"))
    take_profit_r_multiple: Decimal = Field(default=Decimal("1.5"), gt=Decimal("0"))
    stop_loss_pct: Decimal = Field(default=Decimal("0.01"), gt=Decimal("0"), lt=Decimal("1"))
    take_profit_pct: Decimal = Field(default=Decimal("0.02"), gt=Decimal("0"), lt=Decimal("1"))
    momentum_threshold: Decimal = Field(default=Decimal("0.0005"), ge=Decimal("0"), lt=Decimal("1"))
    risk_profile: RiskProfile = RiskProfile.BALANCED
    allow_short: bool = True
    market_hours_only: bool = True
    fee_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    fee_schedule: MoomooFeeSchedule = MoomooFeeSchedule.MOOMOO_US_BASIC
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    portfolio_id: str = Field(default="forward-paper-us", min_length=1)
    scenario_id: str = Field(default="single", min_length=1)
    scenario_label: str = Field(default="単一条件", min_length=1)
    capital_constraint: Decimal | None = Field(default=None, gt=Decimal("0"))
    jpy_capital: Decimal | None = Field(default=None, gt=Decimal("0"))
    usd_jpy_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    fx_as_of: str | None = Field(default=None, min_length=1)
    fx_source: str | None = Field(default=None, min_length=1)
    decision_mode: ForwardDecisionMode = ForwardDecisionMode.RULE
    jev_model: str = Field(default="jev-latest", min_length=1)
    jev_timeout_seconds: float = Field(default=5.0, gt=0)
    jev_input_profile: JevInputProfile | None = None
    jev_output_policy: JevOutputPolicy | None = None

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values)
        if not normalized or any(not value for value in normalized):
            raise ValueError("symbols must contain at least one non-blank symbol")
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must not contain duplicates")
        return normalized


class ForwardPaperSummary(DomainModel):
    """Secret-free, serializable result of a forward paper session."""

    status: str
    started_at: datetime
    finished_at: datetime
    source: str
    events_processed: int = Field(ge=0)
    decisions: int = Field(ge=0)
    approved_orders: int = Field(ge=0)
    risk_rejections: int = Field(ge=0)
    pipeline_failures: int = Field(ge=0)
    holds: int = Field(ge=0)
    fills: int = Field(ge=0)
    errors: tuple[str, ...] = ()
    portfolio: PortfolioState
    fill_events: tuple[FillEvent, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    order_events: tuple[OrderEvent, ...] = ()
    trade_records: tuple[TradeRecord, ...] = ()
    jev_usage: JevUsageSummary = Field(default_factory=JevUsageSummary)
    jev_usage_records: tuple[JevUsageRecord, ...] = ()
    run_config: Mapping[str, Any]

    _summary_time_aware = field_validator("started_at", "finished_at")(_require_summary_time)


class EqualAllocationPolicy:
    """Allocate a fixed share of starting capital to each new position.

    Exit intents always close no more than the currently open position.  Entry
    sizing uses the reference price inserted by the forward rule model, so the
    core risk engine still receives a normal ``TradeIntent``.
    """

    def __init__(
        self,
        initial_capital: Decimal,
        max_positions: int,
        capital_constraint: Decimal | None = None,
    ) -> None:
        if initial_capital <= 0 or max_positions <= 0:
            raise ValueError("initial_capital and max_positions must be positive")
        self._allocation = initial_capital / Decimal(max_positions)
        self._capital_constraint = capital_constraint

    def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        if intent.action is Action.HOLD:
            return 0
        current = portfolio.positions.get(intent.instrument.symbol, 0)
        if current != 0:
            closing = (current > 0 and intent.action is Action.SHORT) or (
                current < 0 and intent.action is Action.LONG
            )
            if closing:
                return min(intent.requested_quantity or abs(current), abs(current))

        reference = intent.limit_price or _decimal_metadata(intent.metadata, "reference_price")
        desired = intent.requested_quantity
        if (
            reference is None
            or desired is not None
            and desired > 0
            and desired <= intent.instrument.lot_size
        ):
            return self._round_lot(
                desired or intent.instrument.lot_size,
                intent.instrument.lot_size,
            )
        budget = min(portfolio.cash, self._allocation)
        if self._capital_constraint is not None:
            budget = min(budget, self._capital_constraint)
        capacity = int((budget / reference).to_integral_value(rounding=ROUND_DOWN))
        return self._round_lot(capacity, intent.instrument.lot_size)

    @staticmethod
    def _round_lot(quantity: int, lot_size: int) -> int:
        if quantity <= 0:
            return 0
        return quantity - quantity % lot_size


class _ReferencePriceDecisionModel(DecisionModel):
    """Attach a market reference price without changing a decision contract."""

    def __init__(self, delegate: DecisionModel) -> None:
        self._delegate = delegate

    async def decide(self, snapshot: Any, prediction: Any = None) -> TradeIntent:
        intent = await self._delegate.decide(snapshot, prediction)
        if intent.action is Action.LONG:
            reference = snapshot.market.ask
        elif intent.action is Action.SHORT:
            reference = snapshot.market.bid
        else:
            reference = snapshot.market.mid
        return intent.model_copy(
            update={
                "metadata": {
                    **dict(intent.metadata),
                    "reference_price": str(reference),
                }
            }
        )


class _ReferencePriceRuleModel(_ReferencePriceDecisionModel):
    """Attach a market reference price to the forward rule baseline."""

    def __init__(self, threshold: Decimal, *, allow_short: bool) -> None:
        super().__init__(
            RuleDecisionModel(
                RuleConfig(
                    momentum_key="return_30s",
                    long_threshold=threshold,
                    short_threshold=-threshold,
                    allow_short=allow_short,
                ),
                strategy_id="forward-rule",
            )
        )


class ForwardPaperRunner:
    """Run one live-clock forward-paper session over normalized quote events."""

    def __init__(
        self,
        config: ForwardPaperConfig | None = None,
        *,
        market_data: MarketDataAdapter | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
        jev_client: JevClient | None = None,
        jev_pricing: JevPricingConfig | None = None,
    ) -> None:
        self.config = config or ForwardPaperConfig()
        self._clock = clock or LiveClock()
        self._logger = logger or logging.getLogger("trader_jev.forward_paper")
        self._jev_pricing = jev_pricing or JevPricingConfig()
        self.instruments = build_us_instruments(self.config.symbols)
        capital_limit = self.config.capital_constraint
        if capital_limit is None and self.config.scenario_id != "unconstrained":
            capital_limit = self.config.initial_capital
        self.market_data = market_data or MoomooMarketDataAdapter(
            MoomooClientConfig(poll_interval_seconds=self.config.poll_interval_seconds)
        )
        initial_state = PortfolioState(
            portfolio_id=self.config.portfolio_id,
            cash=self.config.initial_capital,
            initial_capital=self.config.initial_capital,
            equity=self.config.initial_capital,
        )
        self.ledger = PortfolioLedger(initial_state)
        self.broker = PaperBroker(
            ExecutionConfig(
                fee_bps=self.config.fee_bps,
                fee_schedule=self.config.fee_schedule,
                slippage_bps=self.config.slippage_bps,
            ),
            clock=self._clock,
            ledger=self.ledger,
        )
        limits = RiskProfileLimits(
            max_positions=self.config.max_positions,
            max_open_orders=self.config.max_positions,
            max_order_notional=capital_limit,
            max_position_notional=capital_limit,
            max_daily_loss=self.config.initial_capital * Decimal("0.02"),
            max_drawdown=self.config.initial_capital * Decimal("0.05"),
            max_spread_bps=Decimal("100"),
        )
        self.risk_engine = DeterministicRiskEngine(
            config=RiskConfig(
                risk_profile=self.config.risk_profile,
                profile_limits=limits,
                execution_mode=ExecutionMode.PAPER,
                allow_short=self.config.allow_short,
                allowed_symbols=frozenset(self.config.symbols),
                max_data_age_seconds=30.0,
                max_positions=self.config.max_positions,
                max_open_orders=self.config.max_positions,
                market_hours_only=self.config.market_hours_only,
                enforce_cash=True,
            ),
            portfolio_policy=EqualAllocationPolicy(
                self.config.initial_capital,
                self.config.max_positions,
                capital_limit,
            ),
            clock=self._clock,
            logger=self._logger,
        )
        self.feature_engine = InMemoryFeatureEngine(
            FeatureEngineConfig(atr_period=self.config.atr_period)
        )
        self._jev_adapter: JevDecisionAdapter | None = None
        self._jev_transport: str | None = None
        if self.config.decision_mode is ForwardDecisionMode.JEV:
            client = jev_client or JevHttpClient.from_env()
            self._jev_transport = (
                "gateway"
                if isinstance(client, JevHttpClient) and client.config.gateway_url is not None
                else "direct"
                if isinstance(client, JevHttpClient)
                else "custom"
            )
            self._jev_adapter = JevDecisionAdapter(
                client,
                config=JevAdapterConfig(
                    timeout_seconds=self.config.jev_timeout_seconds,
                    model_version=self.config.jev_model,
                ),
                clock=self._clock,
            )
            self.decision_model = _ReferencePriceDecisionModel(
                JevDecisionModel(
                    self._jev_adapter,
                    strategy_id="forward-jev",
                    input_profile=self.config.jev_input_profile,
                    output_policy=self.config.jev_output_policy,
                )
            )
        else:
            self.decision_model = _ReferencePriceRuleModel(
                self.config.momentum_threshold,
                allow_short=False,
            )
        self.pipeline = TradingPipeline(
            feature_engine=self.feature_engine,
            decision_model=self.decision_model,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker,
            config=PipelineConfig(
                decision_timeout_seconds=(
                    self.config.jev_timeout_seconds + 0.5
                    if self.config.decision_mode is ForwardDecisionMode.JEV
                    else 2.0
                )
            ),
            clock=self._clock,
            logger=self._logger,
        )
        self.exit_policy = HybridExitPolicy(
            max_holding=timedelta(seconds=self.config.max_holding_seconds),
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
            mode=self.config.exit_mode,
            stop_atr_multiple=self.config.stop_atr_multiple,
            take_profit_r_multiple=self.config.take_profit_r_multiple,
        )
        self._last_decision_at: dict[str, datetime] = {}
        self._latest_snapshots: dict[str, Any] = {}
        self._events_processed = 0
        self._decisions = 0
        self._approved_orders = 0
        self._risk_rejections = 0
        self._pipeline_failures = 0
        self._holds = 0
        self._errors: list[str] = []

    async def run(self) -> ForwardPaperSummary:
        started_at = _require_aware(self._clock.now(), "runner clock")
        deadline = started_at + timedelta(seconds=self.config.runtime_seconds)
        status = "COMPLETED"
        try:
            async for raw_event in self.market_data.stream(self.instruments):
                now = _require_aware(self._clock.now(), "runner clock")
                if now >= deadline:
                    break
                if not isinstance(raw_event, QuoteEvent):
                    self._errors.append("unsupported non-quote event from forward market data")
                    continue
                await self.process_event(raw_event, now)
        except asyncio.CancelledError:
            status = "CANCELED"
            raise
        except Exception as exc:
            status = "FAILED"
            message = f"{type(exc).__name__}: {exc}"
            self._errors.append(message)
            self._logger.exception("forward_paper_failed")
        finally:
            if status == "COMPLETED" and self._events_processed == 0:
                status = "FAILED"
                self._errors.append("no quote events were received")
            if status == "COMPLETED":
                await self._close_open_positions()

        return self._summary(started_at, status)

    async def process_event(self, raw_event: Any, now: datetime) -> None:
        """Process one shared event; used by the single and parallel runners."""

        if not isinstance(raw_event, QuoteEvent):
            self._errors.append("unsupported non-quote event from forward market data")
            return
        await self._handle_quote(raw_event, now)
        self._events_processed += 1

    @property
    def events_processed(self) -> int:
        return self._events_processed

    def add_error(self, message: str) -> None:
        self._errors.append(message)

    @property
    def jev_results(self) -> tuple[JevAdapterResult, ...]:
        """Return all Jev calls, including holds and failed responses."""

        if self._jev_adapter is None:
            return ()
        return self._jev_adapter.results

    async def close_open_positions(self) -> None:
        await self._close_open_positions()

    def summary(self, started_at: datetime, status: str) -> ForwardPaperSummary:
        return self._summary(started_at, status)

    def _summary(self, started_at: datetime, status: str) -> ForwardPaperSummary:
        finished_at = _require_aware(self._clock.now(), "runner clock")
        jev_usage_records = (
            usage_records_from_audits(self._jev_adapter.audit_records, self._jev_pricing)
            if self._jev_adapter is not None
            else ()
        )
        return ForwardPaperSummary(
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            source=(
                "moomoo:market-snapshot"
                if isinstance(self.market_data, MoomooMarketDataAdapter)
                else "market-data-adapter"
            ),
            events_processed=self._events_processed,
            decisions=self._decisions,
            approved_orders=self._approved_orders,
            risk_rejections=self._risk_rejections,
            pipeline_failures=self._pipeline_failures,
            holds=self._holds,
            fills=len(self.ledger.fills),
            errors=tuple(self._errors[-20:]),
            portfolio=self.ledger.state,
            fill_events=tuple(self.ledger.fills),
            order_intents=tuple(self.broker.orders),
            order_events=tuple(self.broker.order_events),
            trade_records=DashboardReadModel(self.ledger, clock=self._clock).trade_history(),
            jev_usage=summarize_usage(jev_usage_records),
            jev_usage_records=jev_usage_records,
            run_config={
                "symbols": self.config.symbols,
                "initial_capital": str(self.config.initial_capital),
                "capital_constraint": (
                    str(self.config.capital_constraint)
                    if self.config.capital_constraint is not None
                    else None
                ),
                "scenario_id": self.config.scenario_id,
                "scenario_label": self.config.scenario_label,
                "jpy_capital": (
                    str(self.config.jpy_capital) if self.config.jpy_capital is not None else None
                ),
                "usd_jpy_rate": (
                    str(self.config.usd_jpy_rate)
                    if self.config.usd_jpy_rate is not None
                    else None
                ),
                "fx_as_of": self.config.fx_as_of,
                "fx_source": self.config.fx_source,
                "decision_mode": self.config.decision_mode.value,
                "decision_label": self.config.decision_mode.label,
                "prediction_horizon_minutes": self.config.prediction_horizon_minutes,
                "jev_transport": self._jev_transport,
                "jev_model": (
                    self.config.jev_model
                    if self.config.decision_mode is ForwardDecisionMode.JEV
                    else None
                ),
                "jev_input_profile": (
                    self.config.jev_input_profile.value
                    if self.config.jev_input_profile is not None
                    else None
                ),
                "jev_output_policy": (
                    self.config.jev_output_policy.model_dump(mode="json")
                    if self.config.jev_output_policy is not None
                    else None
                ),
                "jev_timeout_seconds": (
                    self.config.jev_timeout_seconds
                    if self.config.decision_mode is ForwardDecisionMode.JEV
                    else None
                ),
                "jev_pricing": self._jev_pricing.model_dump(mode="json"),
                "runtime_seconds": self.config.runtime_seconds,
                "decision_cadence_seconds": self.config.decision_cadence_seconds,
                "max_positions": self.config.max_positions,
                "max_holding_seconds": self.config.max_holding_seconds,
                "exit_mode": self.config.exit_mode.value,
                "atr_period": self.config.atr_period,
                "stop_atr_multiple": str(self.config.stop_atr_multiple),
                "take_profit_r_multiple": str(self.config.take_profit_r_multiple),
                "stop_loss_pct": str(self.config.stop_loss_pct),
                "take_profit_pct": str(self.config.take_profit_pct),
                "momentum_threshold": str(self.config.momentum_threshold),
                "risk_profile": self.config.risk_profile.value,
                "allow_short": self.config.allow_short,
                "market_hours_only": self.config.market_hours_only,
                "fee_bps": str(self.config.fee_bps),
                "fee_schedule": self.config.fee_schedule.value,
                "slippage_bps": str(self.config.slippage_bps),
                "execution_mode": ExecutionMode.PAPER.value,
            },
        )

    async def _handle_quote(self, event: QuoteEvent, now: datetime) -> None:
        self.broker.update_market(event)
        self.ledger.mark(event.instrument, event.mid, now)
        self.feature_engine.update(event)
        key = _instrument_key(event.instrument)
        try:
            snapshot = self.feature_engine.snapshot(event.instrument, now)
        except Exception as exc:
            self._pipeline_failures += 1
            self._errors.append(f"snapshot {event.instrument.symbol}: {exc}")
            return
        self._latest_snapshots[key] = snapshot

        exit_intent = self.exit_policy.evaluate(snapshot, self.ledger.state)
        if exit_intent is not None:
            await self._submit_intent(exit_intent, is_exit=True)
            return

        if not self._decision_due(key, now):
            return
        self._last_decision_at[key] = now
        if self.ledger.state.positions.get(event.instrument.symbol, 0) != 0:
            return
        result = await self.pipeline.process_event(event, self.ledger.state)
        self._record_pipeline_result(result)

    async def _submit_intent(self, intent: TradeIntent, *, is_exit: bool) -> OrderEvent | None:
        self._decisions += 1
        risk_decision = self.risk_engine.evaluate(
            intent,
            self._latest_snapshots[_instrument_key(intent.instrument)],
            self.ledger.state,
        )
        if not risk_decision.approved or risk_decision.order_intent is None:
            self._risk_rejections += 1
            self._errors.append(f"{intent.instrument.symbol}: {risk_decision.reason_code}")
            return None
        self._approved_orders += 1
        if is_exit:
            self._logger.info(
                "forward_paper_exit_approved",
                extra={"symbol": intent.instrument.symbol, "reason": intent.reason},
            )
        return await self.broker.submit(risk_decision.order_intent)

    def _record_pipeline_result(self, result: PipelineResult) -> None:
        self._decisions += int(result.trade_intent is not None)
        if result.trade_intent is not None and result.trade_intent.action is Action.HOLD:
            self._holds += 1
        if result.risk_decision is not None:
            if result.risk_decision.approved:
                self._approved_orders += 1
            else:
                self._risk_rejections += 1
        if result.failure_code is not None:
            self._pipeline_failures += 1
            self._errors.append(f"{result.failure_code}: {result.failure_reason}")

    def _decision_due(self, key: str, now: datetime) -> bool:
        previous = self._last_decision_at.get(key)
        return previous is None or (
            now - previous
        ).total_seconds() >= self.config.decision_cadence_seconds

    async def _close_open_positions(self) -> None:
        for symbol, quantity in tuple(self.ledger.state.positions.items()):
            if quantity == 0:
                continue
            snapshot = self._latest_snapshots.get(f"US:{symbol}")
            if snapshot is None:
                self._errors.append(f"{symbol}: no snapshot available for session close")
                continue
            action = Action.SHORT if quantity > 0 else Action.LONG
            intent = TradeIntent(
                snapshot_id=snapshot.snapshot_id,
                instrument=snapshot.instrument,
                action=action,
                requested_quantity=abs(quantity),
                strategy_id="session-end-exit",
                reason="scheduled session end",
                created_at=snapshot.as_of,
                metadata={
                    "exit_reason": "SESSION_END",
                    "reference_price": str(
                        snapshot.market.bid if action is Action.SHORT else snapshot.market.ask
                    ),
                },
            )
            await self._submit_intent(intent, is_exit=True)


class ParallelForwardPaperRunner:
    """Fork one read-only market-data stream into independent Paper portfolios."""

    def __init__(
        self,
        config: ForwardPaperConfig | None = None,
        *,
        scenarios: Sequence[CapitalScenario] = DEFAULT_CAPITAL_SCENARIOS,
        decision_modes: Sequence[ForwardDecisionMode | str] = (ForwardDecisionMode.RULE,),
        market_data: MarketDataAdapter | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
        jev_client: JevClient | None = None,
        jev_pricing: JevPricingConfig | None = None,
    ) -> None:
        base_config = config or ForwardPaperConfig()
        selected = tuple(scenarios)
        if not selected:
            raise ValueError("at least one capital scenario is required")
        scenario_ids = [scenario.scenario_id for scenario in selected]
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("capital scenario ids must be unique")
        modes = _normalize_decision_modes(decision_modes)
        self._clock = clock or LiveClock()
        self._logger = logger or logging.getLogger("trader_jev.parallel_forward_paper")
        self.market_data = market_data or MoomooMarketDataAdapter(
            MoomooClientConfig(poll_interval_seconds=base_config.poll_interval_seconds)
        )
        self.instruments = build_us_instruments(base_config.symbols)
        self.scenarios = selected
        self.decision_modes = modes
        self.runners = tuple(
            ForwardPaperRunner(
                base_config.model_copy(
                    update={
                        "initial_capital": scenario.initial_capital,
                        "portfolio_id": f"{base_config.portfolio_id}-{runner_id}",
                        "scenario_id": runner_id,
                        "scenario_label": runner_label,
                        "capital_constraint": scenario.capital_constraint,
                        "jpy_capital": scenario.jpy_capital,
                        "usd_jpy_rate": scenario.usd_jpy_rate,
                        "fx_as_of": scenario.fx_as_of,
                        "fx_source": scenario.fx_source,
                        "decision_mode": mode,
                    }
                ),
                market_data=self.market_data,
                clock=self._clock,
                logger=self._logger,
                jev_client=jev_client,
                jev_pricing=jev_pricing,
            )
            for scenario in selected
            for mode in modes
            for runner_id, runner_label in (
                (
                    _runner_scenario_id(scenario.scenario_id, mode, len(modes)),
                    _runner_scenario_label(scenario.label, mode, len(modes)),
                ),
            )
        )

    async def run(self) -> tuple[ForwardPaperSummary, ...]:
        """Run every scenario on the same quote stream until the shared deadline."""

        started_at = _require_aware(self._clock.now(), "runner clock")
        base_config = self.runners[0].config
        deadline = started_at + timedelta(seconds=base_config.runtime_seconds)
        status = "COMPLETED"
        processed = False
        try:
            async for raw_event in self.market_data.stream(self.instruments):
                now = _require_aware(self._clock.now(), "runner clock")
                if now >= deadline:
                    break
                await asyncio.gather(
                    *(runner.process_event(raw_event, now) for runner in self.runners)
                )
                processed = processed or any(
                    runner.events_processed > 0 for runner in self.runners
                )
        except asyncio.CancelledError:
            status = "CANCELED"
            raise
        except Exception as exc:
            status = "FAILED"
            message = f"{type(exc).__name__}: {exc}"
            for runner in self.runners:
                runner.add_error(message)
            self._logger.exception("parallel_forward_paper_failed")
        finally:
            if status == "COMPLETED" and not processed:
                status = "FAILED"
                for runner in self.runners:
                    runner.add_error("no quote events were received")
            if status == "COMPLETED":
                await asyncio.gather(*(runner.close_open_positions() for runner in self.runners))

        return tuple(runner.summary(started_at, status) for runner in self.runners)


def build_capital_scenarios(
    values: Sequence[str] | None = None,
    *,
    unconstrained_initial_capital: Decimal | None = None,
    usd_jpy_rate: Decimal = DEFAULT_USD_JPY_RATE,
    fx_as_of: str = DEFAULT_USD_JPY_AS_OF,
    fx_source: str = DEFAULT_USD_JPY_SOURCE,
) -> tuple[CapitalScenario, ...]:
    """Build JPY-denominated capital scenarios converted to USD for PaperBroker."""

    if not usd_jpy_rate.is_finite() or usd_jpy_rate <= 0:
        raise ValueError("usd_jpy_rate must be positive and finite")
    if not fx_as_of.strip() or not fx_source.strip():
        raise ValueError("fx_as_of and fx_source must not be blank")
    if values is None:
        values = ("100000", "250000", "500000", "unconstrained")
    unconstrained_cash = (
        _jpy_to_usd(DEFAULT_UNCONSTRAINED_JPY_REFERENCE, usd_jpy_rate)
        if unconstrained_initial_capital is None
        else unconstrained_initial_capital
    )
    if not unconstrained_cash.is_finite() or unconstrained_cash <= 0:
        raise ValueError("unconstrained_initial_capital must be positive")

    scenarios: list[CapitalScenario] = []
    seen: set[str] = set()
    aliases = {
        "10万": "100000",
        "100k": "100000",
        "25万": "250000",
        "250k": "250000",
        "50万": "500000",
        "500k": "500000",
        "制約なし": "unconstrained",
        "なし": "unconstrained",
        "none": "unconstrained",
        "unlimited": "unconstrained",
    }
    for raw_value in values:
        value = raw_value.strip().lower()
        normalized = aliases.get(value, value)
        if normalized == "unconstrained":
            scenario = CapitalScenario(
                scenario_id="unconstrained",
                label="制約なし",
                initial_capital=unconstrained_cash,
                jpy_capital=(
                    DEFAULT_UNCONSTRAINED_JPY_REFERENCE
                    if unconstrained_initial_capital is None
                    else None
                ),
                usd_jpy_rate=usd_jpy_rate,
                fx_as_of=fx_as_of,
                fx_source=fx_source,
            )
        else:
            try:
                capital = Decimal(normalized)
            except InvalidOperation as exc:
                raise ValueError(f"invalid capital scenario: {raw_value}") from exc
            if not capital.is_finite() or capital <= 0:
                raise ValueError(f"capital scenario must be positive: {raw_value}")
            usd_capital = _jpy_to_usd(capital, usd_jpy_rate)
            if usd_capital <= 0:
                raise ValueError(f"capital scenario converts to less than one cent: {raw_value}")
            scenario_id = _capital_scenario_id(capital)
            scenario = CapitalScenario(
                scenario_id=scenario_id,
                label=_capital_scenario_label(capital),
                initial_capital=usd_capital,
                capital_constraint=usd_capital,
                jpy_capital=capital,
                usd_jpy_rate=usd_jpy_rate,
                fx_as_of=fx_as_of,
                fx_source=fx_source,
            )
        if scenario.scenario_id in seen:
            raise ValueError(f"duplicate capital scenario: {raw_value}")
        seen.add(scenario.scenario_id)
        scenarios.append(scenario)
    if not scenarios:
        raise ValueError("at least one capital scenario is required")
    return tuple(scenarios)


def build_us_instruments(symbols: Sequence[str]) -> tuple[InstrumentMetadata, ...]:
    """Build market-neutral metadata for the configured US equity universe."""

    return tuple(
        InstrumentMetadata(
            symbol=symbol.strip().upper(),
            market=Market.US,
            currency="USD",
            timezone="America/New_York",
            tick_size=Decimal("0.01"),
            lot_size=1,
            trading_session=TradingSession(open_time=time(9, 30), close_time=time(16)),
            shortability=True,
        )
        for symbol in symbols
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-forward-paper",
        description="Run a read-only-Moomoo, PaperBroker-only forward session.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help=(
            "Optional JEV env file; process environment variables take precedence "
            "(default: .env)."
        ),
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_US_SYMBOLS),
        help="Comma-separated US symbols (default: the configured 10-symbol universe).",
    )
    parser.add_argument("--initial-capital", type=_decimal, default=Decimal("100000"))
    parser.add_argument("--runtime-seconds", type=_positive_int, default=3600)
    parser.add_argument(
        "--nasdaq-calendar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip weekends and NASDAQ regular-session holidays (default: true).",
    )
    parser.add_argument(
        "--until-nasdaq-close",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Run until the NASDAQ regular-session close, including 13:00 ET early closes "
            "(default: false)."
        ),
    )
    parser.add_argument("--prediction-horizon-minutes", type=_positive_int, default=5)
    parser.add_argument("--decision-cadence-seconds", type=_nonnegative_float, default=30.0)
    parser.add_argument("--max-positions", type=_positive_int, default=3)
    parser.add_argument("--max-holding-seconds", type=_positive_int, default=900)
    parser.add_argument(
        "--exit-mode",
        choices=tuple(mode.value for mode in ExitMode),
        default=ExitMode.ATR.value,
    )
    parser.add_argument("--atr-period", type=_positive_int, default=14)
    parser.add_argument("--stop-atr-multiple", type=_decimal, default=Decimal("1.0"))
    parser.add_argument("--take-profit-r-multiple", type=_decimal, default=Decimal("1.5"))
    parser.add_argument("--stop-loss-pct", type=_decimal, default=Decimal("0.01"))
    parser.add_argument("--take-profit-pct", type=_decimal, default=Decimal("0.02"))
    parser.add_argument("--momentum-threshold", type=_decimal, default=Decimal("0.0005"))
    parser.add_argument(
        "--allow-short",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Permit SHORT orders when needed to close a paper long position.",
    )
    parser.add_argument(
        "--market-hours-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reject decisions outside the instrument's regular session (default: true).",
    )
    parser.add_argument(
        "--fee-schedule",
        choices=tuple(schedule.value for schedule in MoomooFeeSchedule),
        default=MoomooFeeSchedule.MOOMOO_US_BASIC.value,
        help="Paper fee schedule (default: moomoo US basic).",
    )
    parser.add_argument("--poll-interval-seconds", type=_positive_float, default=1.0)
    parser.add_argument("--portfolio-id", default="forward-paper-us")
    parser.add_argument(
        "--capital-scenarios",
        help=(
            "Comma-separated JPY capital scenarios to run in parallel, such as "
            "10万,25万,50万,unconstrained."
        ),
    )
    parser.add_argument(
        "--decision-modes",
        default="rule",
        help="Comma-separated decision branches: rule,jev (default: rule).",
    )
    parser.add_argument(
        "--usd-jpy",
        type=_positive_decimal,
        default=DEFAULT_USD_JPY_RATE,
        help=f"JPY per USD conversion rate (default: {DEFAULT_USD_JPY_RATE}).",
    )
    parser.add_argument(
        "--fx-as-of",
        default=DEFAULT_USD_JPY_AS_OF,
        help="Timestamp of the configured USD/JPY rate (default: latest recorded BOJ rate).",
    )
    parser.add_argument(
        "--fx-source",
        default=DEFAULT_USD_JPY_SOURCE,
        help="Source label recorded with the conversion rate.",
    )
    parser.add_argument(
        "--unconstrained-initial-capital",
        type=_positive_decimal,
        default=None,
        help=(
            "Reference starting cash in USD for the no-capital-limit scenario; "
            "by default it equals the 500000 JPY scenario at --usd-jpy."
        ),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("/home/yappa/.local/state/trader-jev/paper"),
    )
    return parser


def resolve_nasdaq_run(
    now: datetime,
    *,
    requested_runtime_seconds: int,
    use_calendar: bool,
    until_close: bool,
) -> tuple[NasdaqSession | None, int | None]:
    """Resolve a run session and runtime, returning ``None`` for a skip day."""

    if not use_calendar:
        return None, requested_runtime_seconds

    calendar = NasdaqCalendar()
    session = calendar.session_for(now)
    if session is None or now >= session.close_at:
        return session, None
    if until_close:
        remaining = max(1, ceil((session.close_at - now).total_seconds()))
        return session, remaining
    return session, requested_runtime_seconds


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        decision_modes = _normalize_decision_modes(tuple(args.decision_modes.split(",")))
        run_now = LiveClock().now()
        _, runtime_seconds = resolve_nasdaq_run(
            run_now,
            requested_runtime_seconds=args.runtime_seconds,
            use_calendar=args.nasdaq_calendar,
            until_close=args.until_nasdaq_close,
        )
        if runtime_seconds is None:
            calendar = NasdaqCalendar()
            local_date = run_now.astimezone(calendar.timezone).date()
            reason = calendar.closure_reason(local_date) or "regular session already closed"
            print(f"NASDAQ calendar: skip {local_date.isoformat()} ({reason})")
            return 0
        env = load_env_file(args.env_file)
        jev_client: JevClient | None = None
        jev_pricing = JevPricingConfig.from_env(env)
        jev_model = "jev-latest"
        jev_timeout_seconds = 5.0
        if ForwardDecisionMode.JEV in decision_modes:
            jev_client = JevHttpClient.from_env(env)
            jev_model = jev_client.config.model
            jev_timeout_seconds = jev_client.config.timeout_seconds
        config = ForwardPaperConfig(
            symbols=tuple(args.symbols.split(",")),
            initial_capital=args.initial_capital,
            runtime_seconds=runtime_seconds,
            prediction_horizon_minutes=args.prediction_horizon_minutes,
            decision_cadence_seconds=args.decision_cadence_seconds,
            max_positions=args.max_positions,
            max_holding_seconds=args.max_holding_seconds,
            exit_mode=ExitMode(args.exit_mode),
            atr_period=args.atr_period,
            stop_atr_multiple=args.stop_atr_multiple,
            take_profit_r_multiple=args.take_profit_r_multiple,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            momentum_threshold=args.momentum_threshold,
            allow_short=args.allow_short,
            market_hours_only=args.market_hours_only,
            fee_schedule=MoomooFeeSchedule(args.fee_schedule),
            poll_interval_seconds=args.poll_interval_seconds,
            portfolio_id=args.portfolio_id,
            jev_model=jev_model,
            jev_timeout_seconds=jev_timeout_seconds,
        )
        if args.capital_scenarios:
            scenarios = build_capital_scenarios(
                tuple(args.capital_scenarios.split(",")),
                unconstrained_initial_capital=args.unconstrained_initial_capital,
                usd_jpy_rate=args.usd_jpy,
                fx_as_of=args.fx_as_of,
                fx_source=args.fx_source,
            )
            summaries = asyncio.run(
                ParallelForwardPaperRunner(
                    config,
                    scenarios=scenarios,
                    decision_modes=decision_modes,
                    jev_client=jev_client,
                    jev_pricing=jev_pricing,
                ).run()
            )
            encoded = json.dumps(
                [summary.model_dump(mode="json") for summary in summaries],
                ensure_ascii=False,
                indent=2,
            )
            print(encoded)
            for summary in summaries:
                print(f"report: {_write_report(args.report_dir, summary)}")
            return 0 if all(summary.status == "COMPLETED" for summary in summaries) else 2

        if len(decision_modes) != 1:
            scenarios = build_capital_scenarios(
                usd_jpy_rate=args.usd_jpy,
                fx_as_of=args.fx_as_of,
                fx_source=args.fx_source,
                unconstrained_initial_capital=args.unconstrained_initial_capital,
            )
            summaries = asyncio.run(
                ParallelForwardPaperRunner(
                    config,
                    scenarios=scenarios,
                    decision_modes=decision_modes,
                    jev_client=jev_client,
                    jev_pricing=jev_pricing,
                ).run()
            )
            encoded = json.dumps(
                [summary.model_dump(mode="json") for summary in summaries],
                ensure_ascii=False,
                indent=2,
            )
            print(encoded)
            for summary in summaries:
                print(f"report: {_write_report(args.report_dir, summary)}")
            return 0 if all(summary.status == "COMPLETED" for summary in summaries) else 2

        config = config.model_copy(update={"decision_mode": decision_modes[0]})
        summary = asyncio.run(
            ForwardPaperRunner(
                config,
                jev_client=jev_client,
                jev_pricing=jev_pricing,
            ).run()
        )
        encoded = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
        print(encoded)
        print(f"report: {_write_report(args.report_dir, summary)}")
        return 0 if summary.status == "COMPLETED" else 2
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


def _instrument_key(instrument: InstrumentMetadata) -> str:
    return f"{instrument.market.value}:{instrument.symbol}"


def _write_report(report_dir: Path, summary: ForwardPaperSummary) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = summary.started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    scenario_id = str(summary.run_config.get("scenario_id", "single"))
    suffix = "" if scenario_id == "single" else f"-{scenario_id}"
    report_path = report_dir / f"forward-paper-{timestamp}{suffix}.json"
    encoded = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    return report_path


def _capital_scenario_id(capital: Decimal) -> str:
    known_ids = {
        Decimal("100000"): "jpy-100k",
        Decimal("250000"): "jpy-250k",
        Decimal("500000"): "jpy-500k",
    }
    if capital in known_ids:
        return known_ids[capital]
    return f"{capital.normalize():f}".replace(".", "p")


def _capital_scenario_label(capital: Decimal) -> str:
    ten_thousand = capital / Decimal("10000")
    return f"{ten_thousand.normalize():f}万制約"


def _runner_scenario_id(
    scenario_id: str,
    mode: ForwardDecisionMode,
    mode_count: int,
) -> str:
    if mode_count == 1 and mode is ForwardDecisionMode.RULE:
        return scenario_id
    return f"{scenario_id}-{mode.value.lower()}"


def _runner_scenario_label(
    label: str,
    mode: ForwardDecisionMode,
    mode_count: int,
) -> str:
    if mode_count == 1 and mode is ForwardDecisionMode.RULE:
        return label
    return f"{label} / {mode.label}"


def _normalize_decision_modes(
    values: Sequence[ForwardDecisionMode | str],
) -> tuple[ForwardDecisionMode, ...]:
    if not values:
        raise ValueError("at least one decision mode is required")
    aliases = {
        "RULE": ForwardDecisionMode.RULE,
        "ルール": ForwardDecisionMode.RULE,
        "ルール判定": ForwardDecisionMode.RULE,
        "JEV": ForwardDecisionMode.JEV,
        "Jev": ForwardDecisionMode.JEV,
        "JEV判定": ForwardDecisionMode.JEV,
    }
    modes: list[ForwardDecisionMode] = []
    for raw_value in values:
        if isinstance(raw_value, ForwardDecisionMode):
            mode = raw_value
        else:
            normalized = str(raw_value).strip()
            mode = aliases.get(normalized.upper())
            if mode is None:
                mode = aliases.get(normalized)
            if mode is None:
                raise ValueError(f"invalid decision mode: {raw_value}")
        if mode in modes:
            raise ValueError(f"duplicate decision mode: {raw_value}")
        modes.append(mode)
    return tuple(modes)


def _decimal_metadata(metadata: Mapping[str, Any], name: str) -> Decimal | None:
    value = metadata.get(name)
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be a decimal") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def _positive_decimal(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


__all__ = [
    "CapitalScenario",
    "DEFAULT_US_SYMBOLS",
    "DEFAULT_CAPITAL_SCENARIOS",
    "DEFAULT_USD_JPY_AS_OF",
    "DEFAULT_USD_JPY_RATE",
    "DEFAULT_USD_JPY_SOURCE",
    "EqualAllocationPolicy",
    "ForwardDecisionMode",
    "ForwardPaperConfig",
    "ForwardPaperRunner",
    "ForwardPaperSummary",
    "ParallelForwardPaperRunner",
    "build_capital_scenarios",
    "build_parser",
    "build_us_instruments",
    "main",
    "resolve_nasdaq_run",
]


if __name__ == "__main__":
    raise SystemExit(main())
