"""Deterministic, fail-closed risk boundary."""

from __future__ import annotations

import logging
from decimal import Decimal

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
    TimeInForce,
    TradeIntent,
)


class RiskConfig(DomainModel):
    """Typed risk and live-safety configuration.

    Live trading is disabled and unarmed by default.  Setting one flag without
    the other can never enable live order submission.
    """

    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_trading: bool = False
    live_armed: bool = False
    allow_short: bool = True
    allowed_symbols: frozenset[str] = Field(default_factory=frozenset)
    max_order_notional: Decimal | None = Field(default=None, gt=Decimal("0"))
    max_data_age_seconds: float | None = Field(default=30.0, ge=0.0)
    max_open_orders: int | None = Field(default=None, ge=0)
    require_healthy_data: bool = True


class FixedQuantityPortfolioPolicy:
    """Minimal portfolio policy used by the Phase 0 wiring and tests."""

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

    def evaluate(
        self,
        intent: TradeIntent,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        """Return an approval with an OrderIntent, or a reasoned rejection.

        Every validation failure returns a rejection rather than raising.  A
        malformed or unexpected risk input therefore cannot accidentally bypass
        the BrokerAdapter boundary.
        """

        now = self._clock.now()

        def reject(code: str, reason: str) -> RiskDecision:
            self._logger.info(
                "risk_rejected",
                extra={
                    "risk_reason": code,
                    "symbol": intent.instrument.symbol,
                    "trade_intent_id": str(intent.intent_id),
                },
            )
            return RiskDecision(
                trade_intent_id=intent.intent_id,
                approved=False,
                reason_code=code,
                reason=reason,
                checked_at=now,
            )

        if intent.instrument != snapshot.instrument:
            return reject("INSTRUMENT_MISMATCH", "trade intent and snapshot instruments differ")

        if intent.action is Action.HOLD:
            return reject("HOLD_NOT_ACTIONABLE", "HOLD does not produce an order")

        if self._config.require_healthy_data and not snapshot.data_quality.healthy:
            return reject("DATA_UNHEALTHY", "snapshot data quality is not healthy")

        if snapshot.as_of > now:
            return reject("SNAPSHOT_FROM_FUTURE", "snapshot as_of is later than the risk clock")

        age_seconds = (now - snapshot.market.last_received_at).total_seconds()
        if age_seconds < 0:
            return reject("DATA_FROM_FUTURE", "market data was received in the future")
        if (
            self._config.max_data_age_seconds is not None
            and age_seconds > self._config.max_data_age_seconds
        ):
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

        if (
            self._config.max_open_orders is not None
            and portfolio.open_orders >= self._config.max_open_orders
        ):
            return reject("MAX_OPEN_ORDERS", "portfolio open-order limit has been reached")

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

        notional = reference_price * quantity
        if (
            self._config.max_order_notional is not None
            and notional > self._config.max_order_notional
        ):
            return reject("MAX_ORDER_NOTIONAL", "order notional exceeds the configured limit")

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
                metadata={**intent.metadata, "entry_model": entry_model.value},
            )
        except Exception as exc:
            self._logger.exception(
                "risk_order_build_error",
                extra={"symbol": intent.instrument.symbol},
            )
            return reject("ORDER_BUILD_ERROR", f"order intent could not be built: {exc}")

        self._logger.info(
            "risk_approved",
            extra={
                "symbol": intent.instrument.symbol,
                "quantity": quantity,
                "execution_mode": self._config.execution_mode.value,
                "trade_intent_id": str(intent.intent_id),
            },
        )
        return RiskDecision(
            trade_intent_id=intent.intent_id,
            approved=True,
            reason_code="APPROVED",
            reason="risk checks passed",
            order_intent=order,
            checked_at=now,
        )

    def _quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        if self._portfolio_policy is not None:
            return self._portfolio_policy.quantity_for(intent, portfolio)
        if intent.requested_quantity is not None:
            return intent.requested_quantity
        raise ValueError("no portfolio policy and trade intent has no requested quantity")
