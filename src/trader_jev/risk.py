"""Deterministic, fail-closed Risk Engine and Paper risk profiles."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field

from trader_jev.clock import SystemClock
from trader_jev.interfaces import Clock, PortfolioPolicy
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    DomainModel,
    EntryModel,
    ExecutionMode,
    OrderIntent,
    OrderType,
    PortfolioState,
    RiskDecision,
    RiskProfile,
    TimeInForce,
    TradeIntent,
)


class RiskProfileLimits(DomainModel):
    """Configurable defaults for one Paper Risk Profile."""

    max_positions: int | None = Field(default=None, ge=0)
    max_open_orders: int | None = Field(default=None, ge=0)
    max_order_notional: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_position_notional: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_daily_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_drawdown: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_spread_bps: Decimal | None = Field(default=None, gt=Decimal("0"))
    cooldown_seconds: float = Field(default=0.0, ge=0)
    max_consecutive_errors: int | None = Field(default=None, ge=0)


_PROFILE_DEFAULTS: dict[RiskProfile, RiskProfileLimits] = {
    RiskProfile.CONSERVATIVE: RiskProfileLimits(
        max_positions=1,
        max_open_orders=1,
        max_order_notional=Decimal("100000"),
        max_daily_loss=Decimal("1000"),
        max_drawdown=Decimal("2000"),
        max_spread_bps=Decimal("1000"),
    ),
    RiskProfile.BALANCED: RiskProfileLimits(
        max_positions=3,
        max_open_orders=3,
        max_order_notional=Decimal("200000"),
        max_daily_loss=Decimal("2000"),
        max_drawdown=Decimal("5000"),
        max_spread_bps=Decimal("1000"),
    ),
    RiskProfile.AGGRESSIVE: RiskProfileLimits(
        max_positions=10,
        max_open_orders=10,
        max_order_notional=Decimal("500000"),
        max_daily_loss=Decimal("5000"),
        max_drawdown=Decimal("10000"),
        max_spread_bps=Decimal("1000"),
    ),
}


class RiskConfig(DomainModel):
    """Typed Risk policy. All numeric limits are explicit and override profile defaults."""

    risk_profile: RiskProfile = RiskProfile.BALANCED
    profile_limits: RiskProfileLimits | None = None
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_trading: bool = False
    live_armed: bool = False
    allow_short: bool = True
    allowed_symbols: frozenset[str] = Field(default_factory=frozenset)
    disabled_symbols: frozenset[str] = Field(default_factory=frozenset)
    max_order_notional: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_position_notional: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_data_age_seconds: float | None = Field(default=30.0, ge=0.0)
    max_news_age_seconds: float | None = Field(default=None, ge=0.0)
    max_model_age_seconds: float | None = Field(default=None, ge=0.0)
    max_open_orders: int | None = Field(default=None, ge=0)
    max_positions: int | None = Field(default=None, ge=0)
    max_daily_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_drawdown: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_spread_bps: Decimal | None = Field(default=None, gt=Decimal("0"))
    cooldown_seconds: float | None = Field(default=None, ge=0)
    max_consecutive_errors: int | None = Field(default=None, ge=0)
    duplicate_window_seconds: float = Field(default=0.0, ge=0)
    require_healthy_data: bool = True
    market_hours_only: bool = False
    enforce_cash: bool = False
    kill_switch: bool = False
    new_orders_enabled: bool = True
    cancel_all_requested: bool = False


class RiskRuntimeState(DomainModel):
    """Serializable state needed to recover duplicate/cooldown/circuit checks."""

    consecutive_errors: int = Field(default=0, ge=0)
    circuit_breaker_tripped: bool = False
    kill_switch: bool = False
    new_orders_enabled: bool = True
    cancel_all_requested: bool = False
    disabled_symbols: frozenset[str] = Field(default_factory=frozenset)
    seen_keys: tuple[str, ...] = ()
    last_approved_at: Mapping[str, datetime] = Field(default_factory=dict)


class RiskAuditRecord(DomainModel):
    """One immutable risk check result for observability and replay."""

    trade_intent_id: str
    symbol: str
    approved: bool
    reason_code: str
    checked_at: datetime
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class FixedQuantityPortfolioPolicy:
    """Minimal portfolio policy used by wiring and tests."""

    def __init__(self, default_quantity: int = 1) -> None:
        if default_quantity <= 0:
            raise ValueError("default_quantity must be positive")
        self._default_quantity = default_quantity

    def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        del portfolio
        return intent.requested_quantity or self._default_quantity


class DeterministicRiskEngine:
    """Convert a TradeIntent to an OrderIntent only after every check passes."""

    def __init__(
        self,
        config: RiskConfig | None = None,
        portfolio_policy: PortfolioPolicy | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config or RiskConfig()
        self._portfolio_policy = portfolio_policy
        self._clock = clock or SystemClock()
        self._logger = logger or logging.getLogger("trader_jev.risk")
        self._profile = self._config.risk_profile
        self._runtime = RiskRuntimeState(
            kill_switch=self._config.kill_switch,
            new_orders_enabled=self._config.new_orders_enabled,
            cancel_all_requested=self._config.cancel_all_requested,
            disabled_symbols=self._config.disabled_symbols,
        )
        self._audit: list[RiskAuditRecord] = []

    @property
    def profile(self) -> RiskProfile:
        return self._profile

    @property
    def audit_records(self) -> tuple[RiskAuditRecord, ...]:
        return tuple(self._audit)

    @property
    def runtime_state(self) -> RiskRuntimeState:
        return self._runtime

    def set_profile(self, profile: RiskProfile) -> None:
        """Switch Paper limits without changing the core interface."""

        self._profile = profile

    def activate_kill_switch(self, *, cancel_all: bool = True) -> None:
        self._runtime = self._runtime.model_copy(
            update={
                "kill_switch": True,
                "new_orders_enabled": False,
                "cancel_all_requested": cancel_all,
            }
        )

    def clear_kill_switch(self) -> None:
        self._runtime = self._runtime.model_copy(
            update={"kill_switch": False, "new_orders_enabled": True, "cancel_all_requested": False}
        )

    def disable_symbol(self, symbol: str) -> None:
        self._runtime = self._runtime.model_copy(
            update={"disabled_symbols": frozenset((*self._runtime.disabled_symbols, symbol))}
        )

    def enable_symbol(self, symbol: str) -> None:
        self._runtime = self._runtime.model_copy(
            update={
                "disabled_symbols": frozenset(
                    item for item in self._runtime.disabled_symbols if item != symbol
                )
            }
        )

    def record_error(self) -> RiskRuntimeState:
        errors = self._runtime.consecutive_errors + 1
        limit = self._limit("max_consecutive_errors")
        tripped = limit is not None and errors >= limit
        self._runtime = self._runtime.model_copy(
            update={"consecutive_errors": errors, "circuit_breaker_tripped": tripped}
        )
        return self._runtime

    def record_success(self) -> RiskRuntimeState:
        self._runtime = self._runtime.model_copy(update={"consecutive_errors": 0})
        return self._runtime

    def export_state(self) -> RiskRuntimeState:
        return self._runtime

    def restore_state(self, state: RiskRuntimeState) -> None:
        self._runtime = state

    def evaluate(
        self,
        intent: TradeIntent,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        """Evaluate an intent and fail closed if a risk check itself errors."""

        try:
            return self._evaluate(intent, snapshot, portfolio)
        except Exception as exc:
            self._logger.exception(
                "risk_engine_unhandled_error",
                extra={
                    "symbol": intent.instrument.symbol,
                    "trade_intent_id": str(intent.intent_id),
                },
            )
            decision = RiskDecision(
                trade_intent_id=intent.intent_id,
                approved=False,
                reason_code="RISK_ENGINE_ERROR",
                reason=f"risk evaluation failed closed: {exc}",
                checked_at=snapshot.as_of,
            )
            self._record_audit(intent, decision, {"exception_type": type(exc).__name__})
            return decision

    def _evaluate(
        self,
        intent: TradeIntent,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        """Return an approval with an OrderIntent, or a reasoned rejection."""

        now = self._clock.now()

        def reject(code: str, reason: str, **metadata: Any) -> RiskDecision:
            self._logger.info(
                "risk_rejected",
                extra={
                    "risk_reason": code,
                    "symbol": intent.instrument.symbol,
                    "trade_intent_id": str(intent.intent_id),
                },
            )
            decision = RiskDecision(
                trade_intent_id=intent.intent_id,
                approved=False,
                reason_code=code,
                reason=reason,
                checked_at=now,
            )
            self._record_audit(intent, decision, metadata)
            return decision

        def approve(order: OrderIntent) -> RiskDecision:
            decision = RiskDecision(
                trade_intent_id=intent.intent_id,
                approved=True,
                reason_code="APPROVED",
                reason="risk checks passed",
                order_intent=order,
                checked_at=now,
            )
            key = self._decision_key(intent)
            last = dict(self._runtime.last_approved_at)
            last[key] = now
            last[self._cooldown_key(intent)] = now
            self._runtime = self._runtime.model_copy(
                update={"seen_keys": (*self._runtime.seen_keys, key), "last_approved_at": last}
            )
            self._record_audit(intent, decision, {"profile": self._profile.value})
            self._logger.info(
                "risk_approved",
                extra={
                    "symbol": intent.instrument.symbol,
                    "quantity": order.quantity,
                    "execution_mode": order.execution_mode.value,
                    "trade_intent_id": str(intent.intent_id),
                },
            )
            return decision

        if intent.instrument != snapshot.instrument:
            return reject("INSTRUMENT_MISMATCH", "trade intent and snapshot instruments differ")
        if intent.action is Action.HOLD:
            return reject("HOLD_NOT_ACTIONABLE", "HOLD does not produce an order")
        if self._runtime.kill_switch or self._config.kill_switch:
            return reject("KILL_SWITCH", "global kill switch is active")
        if not self._runtime.new_orders_enabled or not self._config.new_orders_enabled:
            return reject("NEW_ORDER_DISABLED", "new orders are disabled")
        if self._runtime.cancel_all_requested or self._config.cancel_all_requested:
            return reject("CANCEL_ALL_ACTIVE", "cancel-all state is active")
        if (
            intent.instrument.symbol in self._runtime.disabled_symbols
            or intent.instrument.symbol in self._config.disabled_symbols
        ):
            return reject("SYMBOL_DISABLED", "symbol is disabled")
        if self._runtime.circuit_breaker_tripped:
            return reject("CIRCUIT_BREAKER", "consecutive error circuit breaker is tripped")
        if self._config.require_healthy_data and not snapshot.data_quality.healthy:
            return reject("DATA_UNHEALTHY", "snapshot data quality is not healthy")
        if snapshot.as_of > now:
            return reject("SNAPSHOT_FROM_FUTURE", "snapshot as_of is later than the risk clock")

        age_seconds = (now - snapshot.market.last_received_at).total_seconds()
        if age_seconds < 0:
            return reject("DATA_FROM_FUTURE", "market data was received in the future")
        max_data_age = self._config.max_data_age_seconds
        if max_data_age is not None and age_seconds > max_data_age:
            return reject("STALE_DATA", f"market data is {age_seconds:.3f}s old")

        if (
            self._config.allowed_symbols
            and intent.instrument.symbol not in self._config.allowed_symbols
        ):
            return reject("SYMBOL_NOT_ALLOWED", "symbol is not in the configured allowlist")
        if self._config.execution_mode is ExecutionMode.LIVE:
            if not self._config.live_trading:
                return reject("LIVE_DISABLED", "live trading is disabled")
            if not self._config.live_armed:
                return reject("LIVE_NOT_ARMED", "live trading is not explicitly armed")
            if intent.action is not Action.LONG:
                return reject("LIVE_LONG_ONLY", "initial live execution permits LONG only")
        if intent.action is Action.SHORT and (
            not self._config.allow_short or not intent.instrument.shortability
        ):
            return reject("SHORT_NOT_ALLOWED", "short selling is not allowed for this instrument")
        if self._config.market_hours_only and not self._inside_session(snapshot):
            return reject("OUTSIDE_MARKET_HOURS", "snapshot is outside the instrument session")

        news_error = self._check_news_freshness(snapshot)
        if news_error is not None:
            return reject(*news_error)
        model_error = self._check_model_freshness(snapshot)
        if model_error is not None:
            return reject(*model_error)

        limits = self._limits()
        max_open_orders = self._limit("max_open_orders")
        if max_open_orders is not None and portfolio.open_orders >= max_open_orders:
            return reject("MAX_OPEN_ORDERS", "portfolio open-order limit has been reached")
        max_positions = self._limit("max_positions")
        current_position = portfolio.positions.get(intent.instrument.symbol, 0)
        if max_positions is not None and current_position == 0:
            open_positions = sum(quantity != 0 for quantity in portfolio.positions.values())
            if open_positions >= max_positions:
                return reject("MAX_POSITIONS", "portfolio position limit has been reached")

        try:
            quantity = self._quantity_for(intent, portfolio)
        except Exception as exc:
            self._logger.exception("risk_policy_error", extra={"symbol": intent.instrument.symbol})
            return reject("PORTFOLIO_POLICY_ERROR", f"portfolio policy failed: {exc}")
        if quantity <= 0:
            return reject("INVALID_QUANTITY", "risk policy returned a non-positive quantity")
        if quantity % intent.instrument.lot_size != 0:
            return reject("LOT_SIZE_VIOLATION", "quantity is not a multiple of instrument lot_size")

        reference_price = (
            intent.limit_price
            if intent.limit_price is not None
            else (snapshot.market.ask if intent.action is Action.LONG else snapshot.market.bid)
        )
        price_limit = intent.instrument.price_limit
        if price_limit is not None:
            if price_limit.lower is not None and reference_price < price_limit.lower:
                return reject("PRICE_LIMIT", "order price is below the instrument price limit")
            if price_limit.upper is not None and reference_price > price_limit.upper:
                return reject("PRICE_LIMIT", "order price is above the instrument price limit")

        spread_bps = self._spread_bps(snapshot)
        max_spread = self._limit("max_spread_bps")
        if max_spread is not None and spread_bps > max_spread:
            return reject("SPREAD_TOO_WIDE", f"spread is {spread_bps:.3f} bps")

        notional = reference_price * quantity
        max_order_notional = self._limit("max_order_notional")
        if max_order_notional is not None and notional > max_order_notional:
            return reject("MAX_ORDER_NOTIONAL", "order notional exceeds the configured limit")
        max_position_notional = self._limit("max_position_notional")
        resulting_quantity = current_position + (
            quantity if intent.action is Action.LONG else -quantity
        )
        if (
            max_position_notional is not None
            and abs(resulting_quantity) * reference_price > max_position_notional
        ):
            return reject("MAX_POSITION_NOTIONAL", "position notional exceeds the configured limit")
        max_daily_loss = self._limit("max_daily_loss")
        if max_daily_loss is not None and -portfolio.daily_pnl >= max_daily_loss:
            return reject("MAX_DAILY_LOSS", "daily loss limit has been reached")
        max_drawdown = self._limit("max_drawdown")
        if max_drawdown is not None and portfolio.drawdown >= max_drawdown:
            return reject("MAX_DRAWDOWN", "drawdown limit has been reached")
        if self._config.enforce_cash and intent.action is Action.LONG and portfolio.cash < notional:
            return reject("INSUFFICIENT_CASH", "available cash is below order notional")

        duplicate_key = self._decision_key(intent)
        if duplicate_key in self._runtime.seen_keys:
            return reject("DUPLICATE_ORDER", "equivalent decision has already been approved")
        cooldown = self._config.cooldown_seconds
        if cooldown is None:
            cooldown = limits.cooldown_seconds
        previous = self._runtime.last_approved_at.get(self._cooldown_key(intent))
        if previous is not None and (now - previous).total_seconds() < cooldown:
            return reject("DECISION_COOLDOWN", "symbol decision cooldown is active")

        try:
            entry_model_value = intent.metadata.get("entry_model", EntryModel.MARKET.value)
            try:
                entry_model = EntryModel(str(entry_model_value))
            except ValueError:
                entry_model = EntryModel.MARKET
            order_type = (
                OrderType.LIMIT if entry_model is not EntryModel.MARKET else OrderType.MARKET
            )
            order = OrderIntent(
                source_trade_intent_id=intent.intent_id,
                instrument=intent.instrument,
                side=intent.action,
                quantity=quantity,
                order_type=order_type,
                limit_price=intent.limit_price,
                time_in_force=TimeInForce.DAY,
                execution_mode=self._config.execution_mode,
                created_at=now,
                metadata={
                    **intent.metadata,
                    "entry_model": entry_model.value,
                    "strategy_id": intent.strategy_id,
                    "model_version": intent.model_version,
                    "risk_profile": self._profile.value,
                    "risk_reason": "APPROVED",
                },
            )
        except Exception as exc:
            self._logger.exception(
                "risk_order_build_error", extra={"symbol": intent.instrument.symbol}
            )
            return reject("ORDER_BUILD_ERROR", f"order intent could not be built: {exc}")
        return approve(order)

    def _limits(self) -> RiskProfileLimits:
        return self._config.profile_limits or _PROFILE_DEFAULTS[self._profile]

    def _limit(self, name: str) -> Any:
        configured = getattr(self._config, name)
        return configured if configured is not None else getattr(self._limits(), name)

    def _quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        if self._portfolio_policy is not None:
            return self._portfolio_policy.quantity_for(intent, portfolio)
        if intent.requested_quantity is not None:
            return intent.requested_quantity
        raise ValueError("no portfolio policy and trade intent has no requested quantity")

    @staticmethod
    def _spread_bps(snapshot: DecisionSnapshot) -> Decimal:
        value = snapshot.orderbook.get("spread_bps")
        if value is not None:
            return Decimal(str(value))
        if snapshot.market.mid == 0:
            return Decimal("0")
        return snapshot.market.spread / snapshot.market.mid * Decimal("10000")

    @staticmethod
    def _inside_session(snapshot: DecisionSnapshot) -> bool:
        try:
            local_time = snapshot.as_of.astimezone(ZoneInfo(snapshot.instrument.timezone)).time()
        except Exception:
            return False
        session = snapshot.instrument.trading_session
        return session.open_time <= local_time <= session.close_time

    def _check_news_freshness(self, snapshot: DecisionSnapshot) -> tuple[str, str] | None:
        if self._config.max_news_age_seconds is None or not snapshot.news:
            return None
        age = snapshot.news.get("age_seconds")
        if age is not None and float(age) > self._config.max_news_age_seconds:
            return "STALE_NEWS", "NewsState is older than the configured limit"
        return None

    def _check_model_freshness(self, snapshot: DecisionSnapshot) -> tuple[str, str] | None:
        if self._config.max_model_age_seconds is None or not snapshot.ml:
            return None
        trained_until = snapshot.ml.get("trained_until")
        if trained_until is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(trained_until).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return "MODEL_METADATA_INVALID", "ML trained_until must be timezone-aware"
            age = (snapshot.as_of - parsed).total_seconds()
        except (TypeError, ValueError):
            return "MODEL_METADATA_INVALID", "ML trained_until is not a valid timestamp"
        if age < 0:
            return "MODEL_FROM_FUTURE", "ML trained_until is later than the decision snapshot"
        if age > self._config.max_model_age_seconds:
            return "STALE_MODEL", "ML metadata is older than the configured limit"
        return None

    @staticmethod
    def _decision_key(intent: TradeIntent) -> str:
        return ":".join(
            (
                intent.instrument.market.value,
                intent.instrument.symbol,
                str(intent.snapshot_id),
                intent.strategy_id,
                intent.action.value,
            )
        )

    @staticmethod
    def _cooldown_key(intent: TradeIntent) -> str:
        return f"{intent.instrument.market.value}:{intent.instrument.symbol}:{intent.strategy_id}"

    def _record_audit(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        metadata: Mapping[str, Any],
    ) -> None:
        self._audit.append(
            RiskAuditRecord(
                trade_intent_id=str(intent.intent_id),
                symbol=intent.instrument.symbol,
                approved=decision.approved,
                reason_code=decision.reason_code,
                checked_at=decision.checked_at,
                metadata=metadata,
            )
        )


RiskEngine = DeterministicRiskEngine
RiskLimits = RiskProfileLimits


__all__ = [
    "DeterministicRiskEngine",
    "FixedQuantityPortfolioPolicy",
    "RiskAuditRecord",
    "RiskConfig",
    "RiskEngine",
    "RiskLimits",
    "RiskProfileLimits",
    "RiskRuntimeState",
]
