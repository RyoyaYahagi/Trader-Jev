"""Long-running, read-only-Moomoo to PaperBroker forward validation.

This module deliberately has no broker or account API integration.  It polls
normalized quotes from :class:`MoomooMarketDataAdapter`, runs a deterministic
rule baseline, and sends only ``ExecutionMode.PAPER`` orders to
:class:`PaperBroker`.  The resulting ledger is written as a secret-free JSON
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
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from trader_jev.clock import LiveClock
from trader_jev.decision import RuleConfig, RuleDecisionModel
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.interfaces import Clock, DecisionModel, MarketDataAdapter
from trader_jev.models import (
    Action,
    DomainModel,
    ExecutionMode,
    InstrumentMetadata,
    Market,
    OrderEvent,
    PortfolioState,
    QuoteEvent,
    RiskProfile,
    TradeIntent,
    TradingSession,
)
from trader_jev.moomoo import MoomooClientConfig, MoomooMarketDataAdapter
from trader_jev.pipeline import PipelineConfig, PipelineResult, TradingPipeline
from trader_jev.portfolio import HybridExitPolicy, PortfolioLedger
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


class ForwardPaperConfig(DomainModel):
    """Explicit parameters for one forward paper session."""

    symbols: tuple[str, ...] = DEFAULT_US_SYMBOLS
    initial_capital: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    runtime_seconds: int = Field(default=3600, gt=0)
    decision_cadence_seconds: float = Field(default=15.0, ge=0)
    max_positions: int = Field(default=3, gt=0)
    max_holding_seconds: int = Field(default=300, gt=0)
    stop_loss_pct: Decimal = Field(default=Decimal("0.01"), gt=Decimal("0"), lt=Decimal("1"))
    take_profit_pct: Decimal = Field(default=Decimal("0.02"), gt=Decimal("0"), lt=Decimal("1"))
    momentum_threshold: Decimal = Field(default=Decimal("0.0005"), ge=Decimal("0"), lt=Decimal("1"))
    risk_profile: RiskProfile = RiskProfile.BALANCED
    allow_short: bool = True
    market_hours_only: bool = True
    fee_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    portfolio_id: str = Field(default="forward-paper-us", min_length=1)

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
    fill_events: tuple[Any, ...] = ()
    run_config: Mapping[str, Any]

    _summary_time_aware = field_validator("started_at", "finished_at")(_require_summary_time)


class EqualAllocationPolicy:
    """Allocate a fixed share of starting capital to each new position.

    Exit intents always close no more than the currently open position.  Entry
    sizing uses the reference price inserted by the forward rule model, so the
    core risk engine still receives a normal ``TradeIntent``.
    """

    def __init__(self, initial_capital: Decimal, max_positions: int) -> None:
        if initial_capital <= 0 or max_positions <= 0:
            raise ValueError("initial_capital and max_positions must be positive")
        self._allocation = initial_capital / Decimal(max_positions)

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
        capacity = int((budget / reference).to_integral_value(rounding=ROUND_DOWN))
        return self._round_lot(capacity, intent.instrument.lot_size)

    @staticmethod
    def _round_lot(quantity: int, lot_size: int) -> int:
        if quantity <= 0:
            return 0
        return quantity - quantity % lot_size


class _ReferencePriceRuleModel(DecisionModel):
    """Attach a market reference price without changing the rule contract."""

    def __init__(self, threshold: Decimal, *, allow_short: bool) -> None:
        self._delegate = RuleDecisionModel(
            RuleConfig(
                momentum_key="return_30s",
                long_threshold=threshold,
                short_threshold=-threshold,
                allow_short=allow_short,
            ),
            strategy_id="forward-rule",
        )

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


class ForwardPaperRunner:
    """Run one live-clock forward-paper session over normalized quote events."""

    def __init__(
        self,
        config: ForwardPaperConfig | None = None,
        *,
        market_data: MarketDataAdapter | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or ForwardPaperConfig()
        self._clock = clock or LiveClock()
        self._logger = logger or logging.getLogger("trader_jev.forward_paper")
        self.instruments = build_us_instruments(self.config.symbols)
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
                slippage_bps=self.config.slippage_bps,
            ),
            clock=self._clock,
            ledger=self.ledger,
        )
        limits = RiskProfileLimits(
            max_positions=self.config.max_positions,
            max_open_orders=self.config.max_positions,
            max_order_notional=self.config.initial_capital,
            max_position_notional=self.config.initial_capital,
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
            ),
            clock=self._clock,
            logger=self._logger,
        )
        self.feature_engine = InMemoryFeatureEngine()
        self.decision_model = _ReferencePriceRuleModel(
            self.config.momentum_threshold,
            allow_short=False,
        )
        self.pipeline = TradingPipeline(
            feature_engine=self.feature_engine,
            decision_model=self.decision_model,
            risk_engine=self.risk_engine,
            broker_adapter=self.broker,
            config=PipelineConfig(decision_timeout_seconds=2.0),
            clock=self._clock,
            logger=self._logger,
        )
        self.exit_policy = HybridExitPolicy(
            max_holding=timedelta(seconds=self.config.max_holding_seconds),
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
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
                await self._handle_quote(raw_event, now)
                self._events_processed += 1
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

    def _summary(self, started_at: datetime, status: str) -> ForwardPaperSummary:
        finished_at = _require_aware(self._clock.now(), "runner clock")
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
            run_config={
                "symbols": self.config.symbols,
                "initial_capital": str(self.config.initial_capital),
                "runtime_seconds": self.config.runtime_seconds,
                "decision_cadence_seconds": self.config.decision_cadence_seconds,
                "max_positions": self.config.max_positions,
                "max_holding_seconds": self.config.max_holding_seconds,
                "stop_loss_pct": str(self.config.stop_loss_pct),
                "take_profit_pct": str(self.config.take_profit_pct),
                "momentum_threshold": str(self.config.momentum_threshold),
                "risk_profile": self.config.risk_profile.value,
                "allow_short": self.config.allow_short,
                "market_hours_only": self.config.market_hours_only,
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
        "--symbols",
        default=",".join(DEFAULT_US_SYMBOLS),
        help="Comma-separated US symbols (default: the configured 10-symbol universe).",
    )
    parser.add_argument("--initial-capital", type=_decimal, default=Decimal("100000"))
    parser.add_argument("--runtime-seconds", type=_positive_int, default=3600)
    parser.add_argument("--decision-cadence-seconds", type=_nonnegative_float, default=15.0)
    parser.add_argument("--max-positions", type=_positive_int, default=3)
    parser.add_argument("--max-holding-seconds", type=_positive_int, default=300)
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
    parser.add_argument("--poll-interval-seconds", type=_positive_float, default=1.0)
    parser.add_argument("--portfolio-id", default="forward-paper-us")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("/home/yappa/.local/state/trader-jev/paper"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = ForwardPaperConfig(
            symbols=tuple(args.symbols.split(",")),
            initial_capital=args.initial_capital,
            runtime_seconds=args.runtime_seconds,
            decision_cadence_seconds=args.decision_cadence_seconds,
            max_positions=args.max_positions,
            max_holding_seconds=args.max_holding_seconds,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            momentum_threshold=args.momentum_threshold,
            allow_short=args.allow_short,
            market_hours_only=args.market_hours_only,
            poll_interval_seconds=args.poll_interval_seconds,
            portfolio_id=args.portfolio_id,
        )
        summary = asyncio.run(ForwardPaperRunner(config).run())
        args.report_dir.mkdir(parents=True, exist_ok=True)
        filename = f"forward-paper-{summary.started_at.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
        report_path = args.report_dir / filename
        encoded = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
        report_path.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        print(f"report: {report_path}")
        return 0 if summary.status == "COMPLETED" else 2
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


def _instrument_key(instrument: InstrumentMetadata) -> str:
    return f"{instrument.market.value}:{instrument.symbol}"


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
    "DEFAULT_US_SYMBOLS",
    "EqualAllocationPolicy",
    "ForwardPaperConfig",
    "ForwardPaperRunner",
    "ForwardPaperSummary",
    "build_parser",
    "build_us_instruments",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
