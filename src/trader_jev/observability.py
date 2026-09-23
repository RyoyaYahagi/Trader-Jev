"""Read-only Paper dashboard models, audit trails, and structured telemetry.

The dashboard layer deliberately depends on the normalized core models and the
Paper ledger only.  It never submits, cancels, or mutates an order, so a failed
dashboard process cannot change the trading path.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from statistics import mean, pstdev
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from trader_jev.clock import SystemClock
from trader_jev.decision import JevAuditRecord, JevDecision, JevRequest
from trader_jev.interfaces import Clock
from trader_jev.logging import redact_sensitive
from trader_jev.models import (
    Action,
    CapitalPolicy,
    DecisionSnapshot,
    DomainModel,
    ExecutionMode,
    FillEvent,
    InstrumentMetadata,
    Market,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    PredictionOutput,
    RiskDecision,
    RiskProfile,
    TradeIntent,
)
from trader_jev.portfolio import PortfolioLedger


class DashboardMode(StrEnum):
    HISTORICAL = "HISTORICAL"
    PAPER = "PAPER"
    FORWARD_PAPER = "FORWARD_PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class StructuredLogRecord(DomainModel):
    """Normalized log data suitable for filtering in the dashboard."""

    timestamp: datetime
    level: str = Field(min_length=1)
    component: str = Field(min_length=1)
    message: str = Field(min_length=1)
    run_id: str | None = None
    snapshot_id: str | None = None
    portfolio_id: str | None = None
    market: str | None = None
    symbol: str | None = None
    strategy_id: str | None = None
    model_version: str | None = None
    decision_id: str | None = None
    risk_reason: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    jev_latency_ms: int | None = Field(default=None, ge=0)
    feed_latency_ms: int | None = Field(default=None, ge=0)
    fields: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    def model_post_init(self, __context: Any) -> None:
        del __context
        object.__setattr__(self, "fields", redact_sensitive(self.fields))

    @classmethod
    def from_log_record(cls, record: logging.LogRecord) -> StructuredLogRecord:
        """Translate a standard log record without exposing logging internals."""

        standard = cls._standard_log_fields()
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in standard and not key.startswith("_")
        }
        values: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
            "fields": extras,
        }
        for field_name in (
            "run_id",
            "snapshot_id",
            "portfolio_id",
            "market",
            "symbol",
            "strategy_id",
            "model_version",
            "decision_id",
            "risk_reason",
            "order_id",
            "fill_id",
            "jev_latency_ms",
            "feed_latency_ms",
        ):
            if field_name in extras:
                values[field_name] = extras.pop(field_name)
        return cls.model_validate(values)

    @staticmethod
    def _standard_log_fields() -> frozenset[str]:
        return frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class LogQuery(DomainModel):
    """Read-only filters for structured logs."""

    start_at: datetime | None = None
    end_at: datetime | None = None
    level: str | None = None
    component: str | None = None
    run_id: str | None = None
    snapshot_id: str | None = None
    portfolio_id: str | None = None
    market: str | None = None
    symbol: str | None = None
    strategy_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def _validate_bound(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("log query timestamps must be timezone-aware")
        return value


class InMemoryLogStore:
    """Bounded-in-process structured log store for local Paper runs."""

    def __init__(self, max_records: int = 50_000) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.max_records = max_records
        self._records: list[StructuredLogRecord] = []

    @property
    def records(self) -> tuple[StructuredLogRecord, ...]:
        return tuple(self._records)

    def append(self, record: StructuredLogRecord) -> None:
        self._records.append(record)
        if len(self._records) > self.max_records:
            del self._records[: len(self._records) - self.max_records]

    def capture(self, record: logging.LogRecord) -> StructuredLogRecord:
        normalized = StructuredLogRecord.from_log_record(record)
        self.append(normalized)
        return normalized

    def query(self, query: LogQuery | None = None) -> tuple[StructuredLogRecord, ...]:
        current = query or LogQuery()
        return tuple(record for record in self._records if _matches_log(record, current))


class StructuredLogHandler(logging.Handler):
    """A standard logging handler that feeds the dashboard store."""

    def __init__(self, store: InMemoryLogStore) -> None:
        super().__init__()
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.capture(record)
        except Exception:
            self.handleError(record)


class HealthStatus(DomainModel):
    component: str = Field(min_length=1)
    state: HealthState = HealthState.UNKNOWN
    checked_at: datetime
    last_event_time: datetime | None = None
    last_received_at: datetime | None = None
    freshness_seconds: float | None = Field(default=None, ge=0)
    error_count: int = Field(default=0, ge=0)
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator(
        "checked_at", "last_event_time", "last_received_at"
    )
    @classmethod
    def _validate_health_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("health timestamps must be timezone-aware")
        return value


class SystemHealth(DomainModel):
    overall: HealthState = HealthState.UNKNOWN
    components: tuple[HealthStatus, ...] = ()
    checked_at: datetime
    risk_engine_state: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("checked_at")
    @classmethod
    def _validate_checked_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value


class PortfolioMetadata(DomainModel):
    """Display metadata kept outside the immutable PortfolioState."""

    portfolio_id: str = Field(min_length=1)
    name: str = "Paper Portfolio"
    market: Market | None = None
    strategy_id: str | None = None
    capital_policy: CapitalPolicy = CapitalPolicy.UNCONSTRAINED
    max_positions: int | None = Field(default=None, ge=0)
    risk_profile: RiskProfile = RiskProfile.BALANCED
    initial_capital: Decimal | None = Field(default=None, ge=Decimal("0"))
    instruments: Mapping[str, InstrumentMetadata] = Field(default_factory=dict)


class PositionView(DomainModel):
    portfolio_id: str
    market: Market | None = None
    symbol: str
    side: Action
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal = Decimal("0")
    entry_time: datetime | None = None
    holding_duration_seconds: float | None = Field(default=None, ge=0)
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    max_holding_deadline: datetime | None = None
    strategy_id: str | None = None


class PortfolioView(DomainModel):
    portfolio_id: str
    name: str
    market: Market | None = None
    strategy_id: str | None = None
    capital_policy: CapitalPolicy
    max_positions: int | None = None
    risk_profile: RiskProfile
    initial_capital: Decimal
    current_equity: Decimal
    available_cash: Decimal
    market_value: Decimal
    gross_realized_pnl: Decimal
    realized_pnl: Decimal
    fees: Decimal
    unrealized_pnl: Decimal
    total_return: Decimal
    drawdown: Decimal
    positions: tuple[PositionView, ...] = ()
    open_orders: int = 0


class OrderView(DomainModel):
    order_id: UUID
    source_trade_intent_id: UUID
    portfolio_id: str
    timestamp: datetime
    market: Market
    symbol: str
    side: Action
    quantity: int
    filled_quantity: int = 0
    status: OrderStatus = OrderStatus.ACCEPTED
    order_type: str
    limit_price: Decimal | None = None
    execution_mode: ExecutionMode
    strategy_id: str | None = None
    risk_reason: str | None = None


class TradeRecord(DomainModel):
    """A matched paper round trip or an explicitly open position lot."""

    trade_id: str
    timestamp: datetime
    exit_timestamp: datetime | None = None
    market: Market
    symbol: str
    side: Action
    entry_price: Decimal
    exit_price: Decimal | None = None
    quantity: int
    gross_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    fee_currency: str | None = None
    fee_schedule: str | None = None
    slippage: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    holding_duration_seconds: float | None = Field(default=None, ge=0)
    entry_reason: str | None = None
    exit_reason: str | None = None
    strategy_id: str | None = None
    portfolio_id: str
    risk_profile: RiskProfile
    order_type: str
    execution_mode: ExecutionMode
    entry_order_id: UUID
    entry_fill_id: UUID
    exit_fill_ids: tuple[UUID, ...] = ()
    closed: bool = False
    win: bool | None = None


class DashboardQuery(DomainModel):
    """Trade and portfolio history filters shared by read-model methods."""

    start_at: datetime | None = None
    end_at: datetime | None = None
    market: Market | None = None
    symbol: str | None = None
    strategy_id: str | None = None
    portfolio_id: str | None = None
    side: Action | None = None
    winning: bool | None = None
    execution_mode: ExecutionMode | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def _validate_query_bound(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("dashboard query timestamps must be timezone-aware")
        return value


class EquityPoint(DomainModel):
    timestamp: datetime
    equity: Decimal
    cumulative_pnl: Decimal

    @field_validator("timestamp")
    @classmethod
    def _validate_equity_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("equity timestamp must be timezone-aware")
        return value


class PerformanceMetrics(DomainModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    cumulative_net_pnl: Decimal = Decimal("0")
    daily_pnl: Mapping[str, Decimal] = Field(default_factory=dict)
    equity_curve: tuple[EquityPoint, ...] = ()
    return_pct: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    sharpe: Decimal | None = None
    profit_factor: Decimal | None = None
    win_rate: Decimal | None = None
    hit_rate: Decimal | None = None
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    win_loss_ratio: Decimal | None = None
    average_trade: Decimal | None = None
    trade_count: int = 0
    turnover: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    fill_ratio: Decimal | None = None
    average_holding_duration_seconds: float | None = None
    by_side: Mapping[str, Decimal] = Field(default_factory=dict)
    by_symbol: Mapping[str, Decimal] = Field(default_factory=dict)
    by_strategy: Mapping[str, Decimal] = Field(default_factory=dict)
    by_portfolio: Mapping[str, Decimal] = Field(default_factory=dict)
    by_market: Mapping[str, Decimal] = Field(default_factory=dict)


class DashboardOverview(DomainModel):
    mode: DashboardMode
    markets: tuple[Market, ...] = ()
    current_time: datetime
    session_state: str = "UNKNOWN"
    current_equity: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    cumulative_pnl: Decimal = Decimal("0")
    current_exposure: Decimal = Decimal("0")
    positions_count: int = 0
    open_orders_count: int = 0
    latest_jev_decision: Mapping[str, Any] | None = None
    latest_ml_prediction: Mapping[str, Any] | None = None
    data_health: HealthState = HealthState.UNKNOWN
    risk_engine_health: HealthState = HealthState.UNKNOWN
    kill_switch_active: bool = False
    new_orders_enabled: bool = True
    live_connected: bool = False


class TradeAuditTrail(DomainModel):
    """All available links in Snapshot → Decision → Risk → Order → Fill."""

    trade_id: str
    snapshot: DecisionSnapshot | None = None
    prediction: PredictionOutput | None = None
    jev_request: JevRequest | None = None
    jev_decision: JevDecision | None = None
    jev_audit: JevAuditRecord | None = None
    trade_intent: TradeIntent | None = None
    risk_decision: RiskDecision | None = None
    order_intents: tuple[OrderIntent, ...] = ()
    order_events: tuple[OrderEvent, ...] = ()
    fills: tuple[FillEvent, ...] = ()
    outcome: Mapping[str, Any] = Field(default_factory=dict)


class ExperimentReport(DomainModel):
    run_id: str = Field(min_length=1)
    git_commit: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    data_start: datetime
    data_end: datetime
    universe: tuple[str, ...] = ()
    strategy_version: str | None = None
    model_version: str | None = None
    jev_version: str | None = None
    portfolio_policy: CapitalPolicy = CapitalPolicy.UNCONSTRAINED
    risk_profile: RiskProfile = RiskProfile.BALANCED
    execution_model: str = "PAPER"
    metrics: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("data_start", "data_end")
    @classmethod
    def _validate_report_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("experiment timestamps must be timezone-aware")
        return value


class ForwardPaperProgress(DomainModel):
    started_at: datetime | None = None
    elapsed_trading_days: int = 0
    total_trades: int = 0
    market_regime_distribution: Mapping[str, int] = Field(default_factory=dict)
    strategy_performance: Mapping[str, Decimal] = Field(default_factory=dict)
    technical_error_count: int = 0
    unexplained_order_count: int = 0
    reconciliation_mismatch_count: int = 0
    gate_status: Mapping[str, str] = Field(default_factory=dict)
    future_live_readiness: str = "REFERENCE_ONLY"


@dataclass(frozen=True)
class PortfolioRegistration:
    metadata: PortfolioMetadata
    ledger: PortfolioLedger


@dataclass
class _OpenLot:
    portfolio_id: str
    order: OrderIntent
    fill: FillEvent
    signed_quantity: int
    fee_per_unit: Decimal
    slippage_per_unit: Decimal


class AuditTrailStore:
    """Mutable append/update boundary for read-only trade drill-downs."""

    def __init__(self) -> None:
        self._records: dict[str, TradeAuditTrail] = {}

    @property
    def records(self) -> tuple[TradeAuditTrail, ...]:
        return tuple(self._records.values())

    def record(
        self,
        trade_id: str,
        *,
        snapshot: DecisionSnapshot | None = None,
        prediction: PredictionOutput | None = None,
        jev_request: JevRequest | None = None,
        jev_decision: JevDecision | None = None,
        jev_audit: JevAuditRecord | None = None,
        trade_intent: TradeIntent | None = None,
        risk_decision: RiskDecision | None = None,
        order_intent: OrderIntent | None = None,
        order_event: OrderEvent | None = None,
        fill: FillEvent | None = None,
        outcome: Mapping[str, Any] | None = None,
    ) -> TradeAuditTrail:
        """Upsert one chain link while retaining all previously captured links."""

        current = self._records.get(trade_id)
        order_intents = list(current.order_intents) if current is not None else []
        order_events = list(current.order_events) if current is not None else []
        fills = list(current.fills) if current is not None else []
        if order_intent is not None and order_intent not in order_intents:
            order_intents.append(order_intent)
        if order_event is not None and order_event not in order_events:
            order_events.append(order_event)
        if fill is not None and fill not in fills:
            fills.append(fill)
        existing_snapshot = current.snapshot if current is not None else None
        existing_prediction = current.prediction if current is not None else None
        existing_jev_request = current.jev_request if current is not None else None
        existing_jev_decision = current.jev_decision if current is not None else None
        existing_jev_audit = current.jev_audit if current is not None else None
        existing_trade_intent = current.trade_intent if current is not None else None
        existing_risk_decision = current.risk_decision if current is not None else None
        existing_outcome: Mapping[str, Any] = current.outcome if current is not None else {}
        trail = TradeAuditTrail(
            trade_id=trade_id,
            snapshot=snapshot if snapshot is not None else existing_snapshot,
            prediction=prediction if prediction is not None else existing_prediction,
            jev_request=jev_request if jev_request is not None else existing_jev_request,
            jev_decision=jev_decision if jev_decision is not None else existing_jev_decision,
            jev_audit=jev_audit if jev_audit is not None else existing_jev_audit,
            trade_intent=trade_intent if trade_intent is not None else existing_trade_intent,
            risk_decision=risk_decision if risk_decision is not None else existing_risk_decision,
            order_intents=tuple(order_intents),
            order_events=tuple(order_events),
            fills=tuple(fills),
            outcome=dict(outcome) if outcome is not None else existing_outcome,
        )
        self._records[trade_id] = trail
        return trail

    def record_pipeline_result(self, result: Any, *, trade_id: str | None = None) -> str:
        """Capture the pipeline's available chain without coupling the pipeline to UI code."""

        order_event = getattr(result, "order_event", None)
        risk_decision = getattr(result, "risk_decision", None)
        trade_intent = getattr(result, "trade_intent", None)
        identity = trade_id or (
            str(order_event.order_intent_id)
            if order_event is not None
            else str(trade_intent.intent_id)
            if trade_intent is not None
            else str(getattr(result, "event_id", "unknown"))
        )
        order_intent = risk_decision.order_intent if risk_decision is not None else None
        self.record(
            identity,
            snapshot=getattr(result, "snapshot", None),
            prediction=getattr(result, "prediction", None),
            trade_intent=trade_intent,
            risk_decision=risk_decision,
            order_intent=order_intent,
            order_event=order_event,
        )
        return identity

    def get(self, trade_id: str) -> TradeAuditTrail | None:
        return self._records.get(trade_id)


class HealthRegistry:
    """Process-local component health registry with no execution side effects."""

    def __init__(self) -> None:
        self._statuses: dict[str, HealthStatus] = {}

    def set(self, status: HealthStatus) -> None:
        self._statuses[status.component] = status

    def get(self, component: str) -> HealthStatus | None:
        return self._statuses.get(component)

    def snapshot(self) -> tuple[HealthStatus, ...]:
        return tuple(self._statuses.values())

    def overall(self) -> HealthState:
        states = [status.state for status in self._statuses.values()]
        if not states:
            return HealthState.UNKNOWN
        if HealthState.UNHEALTHY in states:
            return HealthState.UNHEALTHY
        if HealthState.DEGRADED in states:
            return HealthState.DEGRADED
        if all(state is HealthState.HEALTHY for state in states):
            return HealthState.HEALTHY
        return HealthState.UNKNOWN


class ExperimentReportStore:
    """In-memory report index; persistence can be added behind the same read API."""

    def __init__(self) -> None:
        self._reports: dict[str, ExperimentReport] = {}

    def save(self, report: ExperimentReport) -> None:
        self._reports[report.run_id] = report

    def get(self, run_id: str) -> ExperimentReport | None:
        return self._reports.get(run_id)

    def list(self) -> tuple[ExperimentReport, ...]:
        return tuple(self._reports.values())


class DashboardReadModel:
    """Read model for Paper portfolio, trade, health, and experiment views."""

    def __init__(
        self,
        ledger: PortfolioLedger | None = None,
        *,
        portfolio_metadata: PortfolioMetadata | None = None,
        portfolios: Sequence[PortfolioRegistration] = (),
        mode: DashboardMode = DashboardMode.PAPER,
        clock: Clock | None = None,
        audit_store: AuditTrailStore | None = None,
        log_store: InMemoryLogStore | None = None,
        health_registry: HealthRegistry | None = None,
        experiment_reports: ExperimentReportStore | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.mode = mode
        self._clock = clock or SystemClock()
        self._portfolios: dict[str, PortfolioRegistration] = {}
        if ledger is not None:
            metadata = portfolio_metadata or PortfolioMetadata(
                portfolio_id=ledger.state.portfolio_id,
                initial_capital=ledger.state.initial_capital or ledger.state.cash,
            )
            self.register_portfolio(PortfolioRegistration(metadata, ledger))
        for registration in portfolios:
            self.register_portfolio(registration)
        self.audit_store = audit_store or AuditTrailStore()
        self.log_store = log_store or InMemoryLogStore()
        self.health_registry = health_registry or HealthRegistry()
        self.experiment_reports = experiment_reports or ExperimentReportStore()
        self._started_at = started_at or self._clock.now()
        self._latest_jev: Mapping[str, Any] | None = None
        self._latest_ml: Mapping[str, Any] | None = None
        self._risk_state: Mapping[str, Any] = {}
        self._gate_status: dict[str, str] = {}

    @property
    def portfolios(self) -> tuple[PortfolioRegistration, ...]:
        return tuple(self._portfolios.values())

    def register_portfolio(self, registration: PortfolioRegistration) -> None:
        self._portfolios[registration.metadata.portfolio_id] = registration

    def set_latest_decisions(
        self,
        *,
        jev: Mapping[str, Any] | None = None,
        ml: Mapping[str, Any] | None = None,
    ) -> None:
        self._latest_jev = dict(jev) if jev is not None else None
        self._latest_ml = dict(ml) if ml is not None else None

    def set_risk_state(self, state: Mapping[str, Any]) -> None:
        self._risk_state = dict(state)

    def set_gate_status(self, status: Mapping[str, str]) -> None:
        self._gate_status = dict(status)

    def overview(self) -> DashboardOverview:
        now = self._clock.now()
        views = self.portfolio_views()
        states = [registration.ledger.state for registration in self._portfolios.values()]
        current_equity = sum((state.equity or state.cash for state in states), Decimal("0"))
        cash = sum((state.cash for state in states), Decimal("0"))
        unrealized = sum((state.unrealized_pnl for state in states), Decimal("0"))
        realized = sum((state.realized_pnl for state in states), Decimal("0"))
        daily = sum((state.daily_pnl for state in states), Decimal("0"))
        fees = sum((state.total_fees for state in states), Decimal("0"))
        initial = sum((view.initial_capital for view in views), Decimal("0"))
        exposure = sum((view.market_value for view in views), Decimal("0"))
        positions = sum(len(view.positions) for view in views)
        open_orders = sum(view.open_orders for view in views)
        return DashboardOverview(
            mode=self.mode,
            markets=tuple(
                sorted(
                    {
                        registration.metadata.market
                        for registration in self._portfolios.values()
                        if registration.metadata.market is not None
                    },
                    key=lambda market: market.value,
                )
            ),
            current_time=now,
            session_state=self._session_state(now),
            current_equity=current_equity,
            cash=cash,
            unrealized_pnl=unrealized,
            fees=fees,
            realized_pnl=realized,
            daily_pnl=daily,
            cumulative_pnl=current_equity - initial,
            current_exposure=exposure,
            positions_count=positions,
            open_orders_count=open_orders,
            latest_jev_decision=self._latest_jev,
            latest_ml_prediction=self._latest_ml,
            data_health=self._component_health("market_data"),
            risk_engine_health=self._risk_health(),
            kill_switch_active=bool(self._risk_state.get("kill_switch", False)),
            new_orders_enabled=bool(self._risk_state.get("new_orders_enabled", True)),
            live_connected=False,
        )

    def portfolio_views(self) -> tuple[PortfolioView, ...]:
        return tuple(
            self._portfolio_view(registration) for registration in self._portfolios.values()
        )

    def portfolio_view(self, portfolio_id: str) -> PortfolioView:
        try:
            return self._portfolio_view(self._portfolios[portfolio_id])
        except KeyError as exc:
            raise KeyError(f"unknown dashboard portfolio: {portfolio_id}") from exc

    def positions(self, portfolio_id: str | None = None) -> tuple[PositionView, ...]:
        registrations = self._selected_portfolios(portfolio_id)
        return tuple(
            position
            for registration in registrations
            for position in self._position_views(registration)
        )

    def orders(self, portfolio_id: str | None = None) -> tuple[OrderView, ...]:
        """Return order history without invoking the broker or ledger mutators."""

        views: list[OrderView] = []
        for registration in self._selected_portfolios(portfolio_id):
            events = {
                str(event.order_intent_id): event for event in registration.ledger.order_events
            }
            filled: defaultdict[str, int] = defaultdict(int)
            for fill in registration.ledger.fills:
                filled[str(fill.order_intent_id)] += fill.quantity
            for order in registration.ledger.orders:
                event = events.get(str(order.order_intent_id))
                views.append(
                    OrderView(
                        order_id=order.order_intent_id,
                        source_trade_intent_id=order.source_trade_intent_id,
                        portfolio_id=registration.metadata.portfolio_id,
                        timestamp=order.created_at,
                        market=order.instrument.market,
                        symbol=order.instrument.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        filled_quantity=filled[str(order.order_intent_id)],
                        status=event.status if event is not None else OrderStatus.ACCEPTED,
                        order_type=order.order_type.value,
                        limit_price=order.limit_price,
                        execution_mode=order.execution_mode,
                        strategy_id=_metadata_text(order.metadata, "strategy_id")
                        or registration.metadata.strategy_id,
                        risk_reason=_metadata_text(order.metadata, "risk_reason"),
                    )
                )
        return tuple(sorted(views, key=lambda view: (view.timestamp, str(view.order_id))))

    def trade_history(self, query: DashboardQuery | None = None) -> tuple[TradeRecord, ...]:
        current = query or DashboardQuery()
        records = [
            trade
            for registration in self._selected_portfolios(current.portfolio_id)
            for trade in self._trade_records(registration)
            if _matches_trade(trade, current)
        ]
        return tuple(sorted(records, key=lambda trade: (trade.timestamp, trade.trade_id)))

    def trade_detail(self, trade_id: str) -> TradeAuditTrail | None:
        recorded = self.audit_store.get(trade_id)
        if recorded is not None:
            return recorded
        for registration in self._portfolios.values():
            for trade in self._trade_records(registration):
                if trade.trade_id != trade_id:
                    continue
                orders = tuple(
                    order
                    for order in registration.ledger.orders
                    if order.order_intent_id == trade.entry_order_id
                )
                order_events = tuple(
                    event
                    for event in registration.ledger.order_events
                    if event.order_intent_id == trade.entry_order_id
                )
                fill_ids = {trade.entry_fill_id, *trade.exit_fill_ids}
                fills = tuple(
                    fill for fill in registration.ledger.fills if fill.fill_id in fill_ids
                )
                return TradeAuditTrail(
                    trade_id=trade_id,
                    order_intents=orders,
                    order_events=order_events,
                    fills=fills,
                    outcome={
                        "closed": trade.closed,
                        "gross_pnl": str(trade.gross_pnl),
                        "net_pnl": str(trade.net_pnl),
                    },
                )
        return None

    def performance(self, query: DashboardQuery | None = None) -> PerformanceMetrics:
        current = query or DashboardQuery()
        trades = list(self.trade_history(current))
        closed = [trade for trade in trades if trade.closed]
        if not closed:
            return self._empty_performance(trades)

        closed.sort(key=lambda trade: trade.exit_timestamp or trade.timestamp)
        initial = self._initial_capital(current.portfolio_id)
        cumulative = Decimal("0")
        equity_points = [
            EquityPoint(
                timestamp=closed[0].timestamp,
                equity=initial,
                cumulative_pnl=Decimal("0"),
            )
        ]
        daily: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        returns: list[float] = []
        peak = initial
        max_drawdown = Decimal("0")
        for trade in closed:
            cumulative += trade.net_pnl
            at = trade.exit_timestamp or trade.timestamp
            daily[at.date().isoformat()] += trade.net_pnl
            equity = initial + cumulative
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
            equity_points.append(
                EquityPoint(timestamp=at, equity=equity, cumulative_pnl=cumulative)
            )
            if initial:
                returns.append(float(trade.net_pnl / initial))

        wins = [trade.net_pnl for trade in closed if trade.net_pnl > 0]
        losses = [trade.net_pnl for trade in closed if trade.net_pnl < 0]
        gross_profit = sum(wins, Decimal("0"))
        gross_loss = abs(sum(losses, Decimal("0")))
        durations = [
            trade.holding_duration_seconds
            for trade in closed
            if trade.holding_duration_seconds is not None
        ]
        all_fills, all_orders = self._fills_and_orders(current.portfolio_id)
        fees = sum((fill.fees for fill in all_fills), Decimal("0"))
        slippage = sum((abs(fill.slippage * fill.quantity) for fill in all_fills), Decimal("0"))
        turnover = sum((abs(fill.price * fill.quantity) for fill in all_fills), Decimal("0"))
        ordered_quantity = sum(order.quantity for order in all_orders)
        filled_quantity = sum(fill.quantity for fill in all_fills)
        by_side = _group_pnl(closed, lambda trade: trade.side.value)
        by_symbol = _group_pnl(closed, lambda trade: trade.symbol)
        by_strategy = _group_pnl(closed, lambda trade: trade.strategy_id or "unknown")
        by_portfolio = _group_pnl(closed, lambda trade: trade.portfolio_id)
        by_market = _group_pnl(closed, lambda trade: trade.market.value)
        return PerformanceMetrics(
            period_start=closed[0].timestamp,
            period_end=closed[-1].exit_timestamp or closed[-1].timestamp,
            cumulative_net_pnl=cumulative,
            daily_pnl=dict(daily),
            equity_curve=tuple(equity_points),
            return_pct=cumulative / initial if initial else Decimal("0"),
            max_drawdown=max_drawdown,
            sharpe=_sharpe(returns),
            profit_factor=gross_profit / gross_loss if gross_loss else None,
            win_rate=Decimal(len(wins)) / Decimal(len(closed)),
            hit_rate=Decimal(len(wins)) / Decimal(len(closed)),
            average_win=mean(wins) if wins else None,
            average_loss=mean(losses) if losses else None,
            win_loss_ratio=(mean(wins) / abs(mean(losses))) if wins and losses else None,
            average_trade=cumulative / Decimal(len(closed)),
            trade_count=len(closed),
            turnover=turnover,
            fees=fees,
            slippage=slippage,
            fill_ratio=(Decimal(filled_quantity) / Decimal(ordered_quantity))
            if ordered_quantity
            else None,
            average_holding_duration_seconds=mean(durations) if durations else None,
            by_side=by_side,
            by_symbol=by_symbol,
            by_strategy=by_strategy,
            by_portfolio=by_portfolio,
            by_market=by_market,
        )

    def logs(self, query: LogQuery | None = None) -> tuple[StructuredLogRecord, ...]:
        return self.log_store.query(query)

    def system_health(self) -> SystemHealth:
        components = self.health_registry.snapshot()
        return SystemHealth(
            overall=self.health_registry.overall(),
            components=components,
            checked_at=self._clock.now(),
            risk_engine_state=dict(self._risk_state),
        )

    def forward_paper_progress(self) -> ForwardPaperProgress:
        trades = self.trade_history()
        strategy_performance: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        regimes: defaultdict[str, int] = defaultdict(int)
        for trade in trades:
            strategy_performance[trade.strategy_id or "unknown"] += trade.net_pnl
            trail = self.audit_store.get(trade.trade_id)
            if trail is not None and trail.jev_decision is not None:
                regimes[trail.jev_decision.regime.value] += 1
        error_count = sum(
            record.level.upper() in {"ERROR", "CRITICAL"} for record in self.log_store.records
        )
        return ForwardPaperProgress(
            started_at=self._started_at,
            elapsed_trading_days=max(0, (self._clock.now().date() - self._started_at.date()).days),
            total_trades=len([trade for trade in trades if trade.closed]),
            market_regime_distribution=dict(regimes),
            strategy_performance=dict(strategy_performance),
            technical_error_count=error_count,
            unexplained_order_count=max(0, len(self.orders()) - len(trades)),
            reconciliation_mismatch_count=sum(
                record.level.upper() == "ERROR"
                and record.message == "reconciliation_mismatch"
                for record in self.log_store.records
            ),
            gate_status=dict(self._gate_status),
        )

    def _portfolio_view(self, registration: PortfolioRegistration) -> PortfolioView:
        state = registration.ledger.state
        positions = self._position_views(registration)
        initial = (
            registration.metadata.initial_capital
            or state.initial_capital
            or state.cash
        )
        equity = state.equity or state.cash
        return PortfolioView(
            portfolio_id=registration.metadata.portfolio_id,
            name=registration.metadata.name,
            market=registration.metadata.market,
            strategy_id=registration.metadata.strategy_id,
            capital_policy=registration.metadata.capital_policy,
            max_positions=registration.metadata.max_positions,
            risk_profile=registration.metadata.risk_profile,
            initial_capital=initial,
            current_equity=equity,
            available_cash=state.cash,
            market_value=sum((position.market_value for position in positions), Decimal("0")),
            gross_realized_pnl=state.gross_realized_pnl,
            realized_pnl=state.realized_pnl,
            fees=state.total_fees,
            unrealized_pnl=state.unrealized_pnl,
            total_return=(equity - initial) / initial if initial else Decimal("0"),
            drawdown=state.drawdown,
            positions=positions,
            open_orders=state.open_orders,
        )

    def _position_views(self, registration: PortfolioRegistration) -> tuple[PositionView, ...]:
        state = registration.ledger.state
        now = self._clock.now()
        positions: list[PositionView] = []
        for symbol, quantity in state.positions.items():
            if quantity == 0:
                continue
            average = state.average_prices.get(symbol, Decimal("0"))
            current = state.mark_prices.get(symbol, average)
            entry_time = state.position_entry_times.get(symbol)
            positions.append(
                PositionView(
                    portfolio_id=registration.metadata.portfolio_id,
                    market=_instrument_market(registration.metadata, symbol),
                    symbol=symbol,
                    side=Action.LONG if quantity > 0 else Action.SHORT,
                    quantity=abs(quantity),
                    average_entry_price=average,
                    current_price=current,
                    market_value=abs(quantity) * current,
                    unrealized_pnl=(current - average) * quantity,
                    entry_time=entry_time,
                    holding_duration_seconds=(now - entry_time).total_seconds()
                    if entry_time is not None and now >= entry_time
                    else None,
                    strategy_id=registration.metadata.strategy_id,
                )
            )
        return tuple(sorted(positions, key=lambda position: position.symbol))

    def _selected_portfolios(self, portfolio_id: str | None) -> tuple[PortfolioRegistration, ...]:
        if portfolio_id is None:
            return self.portfolios
        registration = self._portfolios.get(portfolio_id)
        return (registration,) if registration is not None else ()

    def _initial_capital(self, portfolio_id: str | None) -> Decimal:
        return sum(
            (
                self._portfolio_view(registration).initial_capital
                for registration in self._selected_portfolios(portfolio_id)
            ),
            Decimal("0"),
        )

    def _fills_and_orders(
        self, portfolio_id: str | None
    ) -> tuple[tuple[FillEvent, ...], tuple[OrderIntent, ...]]:
        fills: list[FillEvent] = []
        orders: list[OrderIntent] = []
        for registration in self._selected_portfolios(portfolio_id):
            fills.extend(registration.ledger.fills)
            orders.extend(registration.ledger.orders)
        return tuple(fills), tuple(orders)

    def _trade_records(self, registration: PortfolioRegistration) -> tuple[TradeRecord, ...]:
        orders = {str(order.order_intent_id): order for order in registration.ledger.orders}
        fills = sorted(
            registration.ledger.fills,
            key=lambda fill: fill.occurred_at,
        )
        lots: list[_OpenLot] = []
        records: list[TradeRecord] = []
        for fill in fills:
            order = orders.get(str(fill.order_intent_id))
            if order is None:
                continue
            signed = fill.quantity if order.side is Action.LONG else -fill.quantity
            remaining = signed
            fee_per_unit = fill.fees / Decimal(fill.quantity)
            slippage_per_unit = abs(fill.slippage)
            while remaining:
                match_index = next(
                    (
                        index
                        for index, lot in enumerate(lots)
                        if lot.order.instrument.symbol == order.instrument.symbol
                        and lot.signed_quantity * remaining < 0
                    ),
                    None,
                )
                if match_index is None:
                    lots.append(
                        _OpenLot(
                            portfolio_id=registration.metadata.portfolio_id,
                            order=order,
                            fill=fill,
                            signed_quantity=remaining,
                            fee_per_unit=fee_per_unit,
                            slippage_per_unit=slippage_per_unit,
                        )
                    )
                    break
                lot = lots[match_index]
                quantity = min(abs(lot.signed_quantity), abs(remaining))
                entry_side = Action.LONG if lot.signed_quantity > 0 else Action.SHORT
                gross = (fill.price - lot.fill.price) * quantity * (
                    Decimal("1") if entry_side is Action.LONG else Decimal("-1")
                )
                fees = (lot.fee_per_unit + fee_per_unit) * quantity
                slippage = (lot.slippage_per_unit + slippage_per_unit) * quantity
                exit_at = fill.occurred_at
                entry_reason = _metadata_text(lot.order.metadata, "entry_reason")
                exit_reason = _metadata_text(order.metadata, "exit_reason")
                net = gross - fees
                records.append(
                    TradeRecord(
                        trade_id=_trade_id(
                            registration.metadata.portfolio_id,
                            lot.fill.fill_id,
                            fill.fill_id,
                            quantity,
                        ),
                        timestamp=lot.fill.occurred_at,
                        exit_timestamp=exit_at,
                        market=lot.order.instrument.market,
                        symbol=lot.order.instrument.symbol,
                        side=entry_side,
                        entry_price=lot.fill.price,
                        exit_price=fill.price,
                        quantity=quantity,
                        gross_pnl=gross,
                        fees=fees,
                        fee_currency=_fee_currency(lot.fill, fill, lot.order.instrument.currency),
                        fee_schedule=_fee_schedule(lot.fill, fill),
                        slippage=slippage,
                        net_pnl=net,
                        holding_duration_seconds=max(
                            0.0, (exit_at - lot.fill.occurred_at).total_seconds()
                        ),
                        entry_reason=entry_reason,
                        exit_reason=exit_reason,
                        strategy_id=_metadata_text(lot.order.metadata, "strategy_id")
                        or registration.metadata.strategy_id,
                        portfolio_id=registration.metadata.portfolio_id,
                        risk_profile=_risk_profile(
                            lot.order.metadata, registration.metadata.risk_profile
                        ),
                        order_type=lot.order.order_type.value,
                        execution_mode=lot.order.execution_mode,
                        entry_order_id=lot.order.order_intent_id,
                        entry_fill_id=lot.fill.fill_id,
                        exit_fill_ids=(fill.fill_id,),
                        closed=True,
                        win=net > 0,
                    )
                )
                lot.signed_quantity -= quantity if lot.signed_quantity > 0 else -quantity
                remaining -= quantity if remaining > 0 else -quantity
                if lot.signed_quantity == 0:
                    lots.pop(match_index)
        for lot in lots:
            mark = registration.ledger.state.mark_prices.get(
                lot.order.instrument.symbol, lot.fill.price
            )
            quantity = abs(lot.signed_quantity)
            side = Action.LONG if lot.signed_quantity > 0 else Action.SHORT
            gross = (mark - lot.fill.price) * quantity * (
                Decimal("1") if side is Action.LONG else Decimal("-1")
            )
            fees = lot.fee_per_unit * quantity
            records.append(
                TradeRecord(
                    trade_id=_trade_id(
                        registration.metadata.portfolio_id, lot.fill.fill_id, "open"
                    ),
                    timestamp=lot.fill.occurred_at,
                    market=lot.order.instrument.market,
                    symbol=lot.order.instrument.symbol,
                    side=side,
                    entry_price=lot.fill.price,
                    quantity=quantity,
                    gross_pnl=gross,
                    fees=fees,
                    fee_currency=_fee_currency(lot.fill, None, lot.order.instrument.currency),
                    fee_schedule=_fee_schedule(lot.fill, None),
                    slippage=lot.slippage_per_unit * quantity,
                    net_pnl=gross - fees,
                    holding_duration_seconds=max(
                        0.0, (self._clock.now() - lot.fill.occurred_at).total_seconds()
                    ),
                    entry_reason=_metadata_text(lot.order.metadata, "entry_reason"),
                    strategy_id=_metadata_text(lot.order.metadata, "strategy_id")
                    or registration.metadata.strategy_id,
                    portfolio_id=registration.metadata.portfolio_id,
                    risk_profile=_risk_profile(
                        lot.order.metadata, registration.metadata.risk_profile
                    ),
                    order_type=lot.order.order_type.value,
                    execution_mode=lot.order.execution_mode,
                    entry_order_id=lot.order.order_intent_id,
                    entry_fill_id=lot.fill.fill_id,
                    closed=False,
                    win=None,
                )
            )
        return tuple(records)

    def _empty_performance(self, trades: Sequence[TradeRecord]) -> PerformanceMetrics:
        initial = self._initial_capital(None)
        latest = max((trade.timestamp for trade in trades), default=None)
        fills, _ = self._fills_and_orders(None)
        return PerformanceMetrics(
            period_start=latest,
            period_end=latest,
            equity_curve=(
                EquityPoint(
                    timestamp=latest or self._clock.now(),
                    equity=initial,
                    cumulative_pnl=Decimal("0"),
                ),
            )
            if initial
            else (),
            return_pct=Decimal("0"),
            trade_count=0,
            fees=sum((fill.fees for fill in fills), Decimal("0")),
        )

    def _session_state(self, now: datetime) -> str:
        for registration in self._portfolios.values():
            for instrument in registration.metadata.instruments.values():
                local_time = now.astimezone(UTC if instrument.timezone == "UTC" else None)
                if instrument.timezone != "UTC":
                    try:
                        from zoneinfo import ZoneInfo

                        local_time = now.astimezone(ZoneInfo(instrument.timezone))
                    except Exception:
                        return "UNKNOWN"
                current = local_time.time()
                session = instrument.trading_session
                return (
                    "OPEN"
                    if session.open_time <= current <= session.close_time
                    else "CLOSED"
                )
        return "UNKNOWN"

    def _component_health(self, component: str) -> HealthState:
        status = self.health_registry.get(component)
        return status.state if status is not None else HealthState.UNKNOWN

    def _risk_health(self) -> HealthState:
        status = self.health_registry.get("risk_engine")
        if status is not None:
            return status.state
        if self._risk_state.get("kill_switch") or self._risk_state.get("circuit_breaker_tripped"):
            return HealthState.DEGRADED
        return HealthState.UNKNOWN


class DashboardAPI:
    """Framework-neutral read-only facade suitable for a future FastAPI route layer."""

    def __init__(self, read_model: DashboardReadModel) -> None:
        self.read_model = read_model

    def overview(self) -> DashboardOverview:
        return self.read_model.overview()

    def portfolios(self) -> tuple[PortfolioView, ...]:
        return self.read_model.portfolio_views()

    def positions(self, portfolio_id: str | None = None) -> tuple[PositionView, ...]:
        return self.read_model.positions(portfolio_id)

    def orders(self, portfolio_id: str | None = None) -> tuple[OrderView, ...]:
        return self.read_model.orders(portfolio_id)

    def trades(self, query: DashboardQuery | None = None) -> tuple[TradeRecord, ...]:
        return self.read_model.trade_history(query)

    def performance(self, query: DashboardQuery | None = None) -> PerformanceMetrics:
        return self.read_model.performance(query)

    def logs(self, query: LogQuery | None = None) -> tuple[StructuredLogRecord, ...]:
        return self.read_model.logs(query)

    def health(self) -> SystemHealth:
        return self.read_model.system_health()


DashboardService = DashboardReadModel


def _matches_log(record: StructuredLogRecord, query: LogQuery) -> bool:
    if query.start_at is not None and record.timestamp < query.start_at:
        return False
    if query.end_at is not None and record.timestamp >= query.end_at:
        return False
    for field_name in (
        "level",
        "component",
        "run_id",
        "snapshot_id",
        "portfolio_id",
        "market",
        "symbol",
        "strategy_id",
        "order_id",
        "fill_id",
    ):
        expected = getattr(query, field_name)
        if expected is not None and getattr(record, field_name) != expected:
            return False
    return True


def _matches_trade(trade: TradeRecord, query: DashboardQuery) -> bool:
    if query.start_at is not None and trade.timestamp < query.start_at:
        return False
    if query.end_at is not None and trade.timestamp >= query.end_at:
        return False
    if query.market is not None and trade.market is not query.market:
        return False
    if query.symbol is not None and trade.symbol != query.symbol:
        return False
    if query.strategy_id is not None and trade.strategy_id != query.strategy_id:
        return False
    if query.portfolio_id is not None and trade.portfolio_id != query.portfolio_id:
        return False
    if query.side is not None and trade.side is not query.side:
        return False
    if query.winning is not None and trade.win is not query.winning:
        return False
    return not (
        query.execution_mode is not None and trade.execution_mode is not query.execution_mode
    )


def _metadata_text(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return str(value) if value is not None else None


def _fee_currency(entry: FillEvent, exit: FillEvent | None, fallback: str) -> str:
    for fill in (entry, exit):
        if fill is not None and fill.fee_breakdown is not None:
            return fill.fee_breakdown.currency
    return fallback


def _fee_schedule(entry: FillEvent, exit: FillEvent | None) -> str | None:
    for fill in (entry, exit):
        if fill is not None and fill.fee_breakdown is not None:
            return fill.fee_breakdown.schedule
    return None


def _risk_profile(metadata: Mapping[str, Any], default: RiskProfile) -> RiskProfile:
    value = metadata.get("risk_profile")
    if value is None:
        return default
    try:
        return RiskProfile(str(value))
    except ValueError:
        return default


def _instrument_market(metadata: PortfolioMetadata, symbol: str) -> Market | None:
    instrument = metadata.instruments.get(symbol)
    return instrument.market if instrument is not None else metadata.market


def _trade_id(portfolio_id: str, *parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join((portfolio_id, *(str(part) for part in parts)))))


def _group_pnl(
    trades: Iterable[TradeRecord],
    key: Callable[[TradeRecord], str],
) -> dict[str, Decimal]:
    totals: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for trade in trades:
        totals[str(key(trade))] += trade.net_pnl
    return dict(totals)


def _sharpe(returns: Sequence[float]) -> Decimal | None:
    if len(returns) < 2:
        return None
    deviation = pstdev(returns)
    if math.isclose(deviation, 0.0):
        return None
    return Decimal(str(mean(returns) / deviation * math.sqrt(len(returns))))


__all__ = [
    "AuditTrailStore",
    "DashboardAPI",
    "DashboardMode",
    "DashboardOverview",
    "DashboardQuery",
    "DashboardReadModel",
    "DashboardService",
    "EquityPoint",
    "ExperimentReport",
    "ExperimentReportStore",
    "ForwardPaperProgress",
    "HealthRegistry",
    "HealthState",
    "HealthStatus",
    "InMemoryLogStore",
    "LogQuery",
    "OrderView",
    "PerformanceMetrics",
    "PortfolioMetadata",
    "PortfolioRegistration",
    "PortfolioView",
    "PositionView",
    "StructuredLogHandler",
    "StructuredLogRecord",
    "SystemHealth",
    "TradeAuditTrail",
    "TradeRecord",
]
