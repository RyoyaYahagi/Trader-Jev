"""Paper portfolio policies and an event-sourced portfolio ledger."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from pydantic import Field

from trader_jev.models import (
    Action,
    CapitalPolicy,
    DecisionSnapshot,
    DomainModel,
    FillEvent,
    InstrumentMetadata,
    LedgerEvent,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    PortfolioState,
    RiskProfile,
    TradeIntent,
)


class PortfolioPolicyConfig(DomainModel):
    """Capital and concurrency constraints for one paper portfolio."""

    capital_policy: CapitalPolicy = CapitalPolicy.UNCONSTRAINED
    initial_capital: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    max_positions: int = Field(default=10, gt=0)
    risk_profile: RiskProfile = RiskProfile.BALANCED
    allocation_fraction: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("1"))


class PaperPortfolioPolicy:
    """Size intents without changing the upstream strategy decision."""

    def __init__(self, config: PortfolioPolicyConfig | None = None) -> None:
        self.config = config or PortfolioPolicyConfig()

    def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        lot_size = intent.instrument.lot_size
        desired = intent.requested_quantity or lot_size
        current = portfolio.positions.get(intent.instrument.symbol, 0)
        if intent.action is Action.HOLD:
            return 0
        if current == 0:
            open_positions = sum(1 for quantity in portfolio.positions.values() if quantity != 0)
            if open_positions >= self.config.max_positions:
                return 0

        price = self._reference_price(intent)
        if price is None or self.config.capital_policy is CapitalPolicy.UNCONSTRAINED:
            return self._round_lot(desired, lot_size)

        capital = (
            Decimal("100000")
            if self.config.capital_policy
            in (
                CapitalPolicy.THEORETICAL_100K,
                CapitalPolicy.REALISTIC_100K,
            )
            else portfolio.cash
        )
        available = min(portfolio.cash, capital) * self.config.allocation_fraction
        capacity = int((available / price).to_integral_value(rounding=ROUND_DOWN))
        return self._round_lot(min(desired, capacity), lot_size)

    @staticmethod
    def _reference_price(intent: TradeIntent) -> Decimal | None:
        if intent.limit_price is not None:
            return intent.limit_price
        value = intent.metadata.get("reference_price")
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _round_lot(quantity: int, lot_size: int) -> int:
        if quantity <= 0:
            return 0
        return quantity - quantity % lot_size


class PortfolioLedger:
    """Append-only order/fill ledger that can rebuild a portfolio state."""

    def __init__(
        self,
        initial_state: PortfolioState,
        *,
        peak_equity: Decimal | None = None,
    ) -> None:
        initial_capital = initial_state.initial_capital or initial_state.cash
        self._initial_state = initial_state.model_copy(
            update={
                "initial_capital": initial_capital,
                "equity": initial_state.equity or initial_state.cash,
            }
        )
        self._state = self._initial_state
        self._orders: dict[str, OrderIntent] = {}
        self._order_events: list[OrderEvent] = []
        self._fills: list[FillEvent] = []
        self._events: list[LedgerEvent] = []
        self._peak_equity = max(
            self._state.equity or self._state.cash,
            peak_equity or Decimal("0"),
        )

    @property
    def state(self) -> PortfolioState:
        return self._state

    @property
    def orders(self) -> tuple[OrderIntent, ...]:
        return tuple(self._orders.values())

    @property
    def order_events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._order_events)

    @property
    def fills(self) -> tuple[FillEvent, ...]:
        return tuple(self._fills)

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def record_order(self, order: OrderIntent, event: OrderEvent) -> None:
        """Record an order transition and retain its normalized intent."""

        self._orders[str(order.order_intent_id)] = order
        self._order_events.append(event)
        self._events.append(event)
        self._refresh_open_orders()

    def record_fill(self, order: OrderIntent, fill: FillEvent) -> None:
        """Apply a fill exactly once and update cash/position/PnL."""

        if any(existing.fill_id == fill.fill_id for existing in self._fills):
            return
        self._orders[str(order.order_intent_id)] = order
        self._fills.append(fill)
        self._events.append(fill)
        self._apply_fill(order, fill)

    def mark(self, instrument: InstrumentMetadata, price: Decimal, at: datetime) -> PortfolioState:
        """Mark one instrument and update unrealized PnL/equity."""

        marks = dict(self._state.mark_prices)
        marks[instrument.symbol] = price
        return self._recalculate_marks(marks, at)

    def rebuild(self, at: datetime | None = None) -> PortfolioState:
        """Reconstruct state from the initial state and ledger events."""

        rebuilt = PortfolioLedger(self._initial_state)
        orders = self._orders
        for event in self._events:
            if at is not None and event.occurred_at > at:
                continue
            if isinstance(event, OrderEvent):
                order = orders.get(str(event.order_intent_id))
                if order is not None:
                    rebuilt.record_order(order, event)
            else:
                order = orders.get(str(event.order_intent_id))
                if order is not None:
                    rebuilt.record_fill(order, event)
        return rebuilt.state

    async def append(self, event: LedgerEvent) -> None:
        """Async Ledger protocol adapter for persistence-facing callers."""

        if isinstance(event, OrderEvent):
            order = self._orders.get(str(event.order_intent_id))
            if order is not None:
                self.record_order(order, event)
        else:
            order = self._orders.get(str(event.order_intent_id))
            if order is not None:
                self.record_fill(order, event)

    async def append_order(self, event: OrderEvent) -> None:
        await self.append(event)

    async def append_fill(self, event: FillEvent) -> None:
        await self.append(event)

    def _apply_fill(self, order: OrderIntent, fill: FillEvent) -> None:
        symbol = order.instrument.symbol
        positions = dict(self._state.positions)
        averages = dict(self._state.average_prices)
        entries = dict(self._state.position_entry_times)
        signed_quantity = fill.quantity if order.side is Action.LONG else -fill.quantity
        old_quantity = positions.get(symbol, 0)
        old_average = averages.get(symbol, fill.price)
        gross_realized = self._state.gross_realized_pnl
        realized = self._state.realized_pnl

        if old_quantity == 0 or old_quantity * signed_quantity > 0:
            total = abs(old_quantity) + abs(signed_quantity)
            averages[symbol] = (
                old_average * abs(old_quantity) + fill.price * abs(signed_quantity)
            ) / total
            if old_quantity == 0:
                entries[symbol] = fill.occurred_at
        else:
            closing = min(abs(old_quantity), abs(signed_quantity))
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            realized_change = (fill.price - old_average) * closing * direction
            gross_realized += realized_change
            realized += realized_change
            remainder = old_quantity + signed_quantity
            if remainder == 0:
                averages.pop(symbol, None)
                entries.pop(symbol, None)
            elif old_quantity * remainder < 0:
                averages[symbol] = fill.price
                entries[symbol] = fill.occurred_at
            positions[symbol] = remainder

        if old_quantity == 0 or old_quantity * signed_quantity > 0:
            positions[symbol] = old_quantity + signed_quantity
        total_fees = self._state.total_fees + fill.fees
        realized -= fill.fees
        if positions.get(symbol) == 0:
            positions.pop(symbol, None)
        cash = self._state.cash - (fill.price * signed_quantity) - fill.fees
        self._state = self._state.model_copy(
            update={
                "cash": cash,
                "positions": positions,
                "average_prices": averages,
                "position_entry_times": entries,
                "gross_realized_pnl": gross_realized,
                "realized_pnl": realized,
                "total_fees": total_fees,
                "daily_pnl": realized + self._state.unrealized_pnl,
                "updated_at": fill.occurred_at,
            }
        )
        self._recalculate_marks(dict(self._state.mark_prices), fill.occurred_at)

    def _recalculate_marks(self, marks: dict[str, Decimal], at: datetime) -> PortfolioState:
        unrealized = Decimal("0")
        for symbol, quantity in self._state.positions.items():
            mark = marks.get(symbol)
            average = self._state.average_prices.get(symbol)
            if mark is None or average is None:
                continue
            unrealized += (mark - average) * quantity
        equity = self._state.cash + sum(
            marks.get(symbol, self._state.average_prices.get(symbol, Decimal("0"))) * quantity
            for symbol, quantity in self._state.positions.items()
        )
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = max(Decimal("0"), self._peak_equity - equity)
        self._state = self._state.model_copy(
            update={
                "mark_prices": marks,
                "unrealized_pnl": unrealized,
                "equity": equity,
                "daily_pnl": self._state.realized_pnl + unrealized,
                "drawdown": drawdown,
                "updated_at": at,
            }
        )
        return self._state

    def _refresh_open_orders(self) -> None:
        latest: dict[str, OrderEvent] = {}
        for event in self._order_events:
            latest[str(event.order_intent_id)] = event
        open_orders = sum(
            event.status in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
            for event in latest.values()
        )
        self._state = self._state.model_copy(update={"open_orders": open_orders})


class InMemoryPortfolioRepository:
    """Small async repository useful for restart/recovery tests."""

    def __init__(self) -> None:
        self._states: dict[str, PortfolioState] = {}

    async def get_state(self, portfolio_id: str) -> PortfolioState:
        try:
            return self._states[portfolio_id]
        except KeyError as exc:
            raise KeyError(f"unknown portfolio {portfolio_id}") from exc

    async def save_state(self, state: PortfolioState) -> None:
        self._states[state.portfolio_id] = state


class FixedTimeExitPolicy:
    """Close an open position after a deterministic holding deadline."""

    def __init__(self, max_holding: timedelta = timedelta(minutes=5)) -> None:
        if max_holding <= timedelta(0):
            raise ValueError("max_holding must be positive")
        self.max_holding = max_holding

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> TradeIntent | None:
        quantity = portfolio.positions.get(snapshot.instrument.symbol, 0)
        entry_time = portfolio.position_entry_times.get(snapshot.instrument.symbol)
        if quantity == 0 or entry_time is None or snapshot.as_of - entry_time < self.max_holding:
            return None
        action = Action.SHORT if quantity > 0 else Action.LONG
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=action,
            requested_quantity=abs(quantity),
            strategy_id="fixed-time-exit",
            reason="maximum holding duration reached",
            created_at=snapshot.as_of,
            metadata={"exit_reason": "MAX_HOLDING"},
        )


class ExitMode(StrEnum):
    """Price threshold source used by the hybrid Paper exit policy."""

    FIXED_PCT = "FIXED_PCT"
    ATR = "ATR"


class HybridExitPolicy(FixedTimeExitPolicy):
    """Combine time, stop-loss, and take-profit exits."""

    def __init__(
        self,
        max_holding: timedelta = timedelta(minutes=5),
        stop_loss_pct: Decimal = Decimal("0.01"),
        take_profit_pct: Decimal = Decimal("0.02"),
        mode: ExitMode | str = ExitMode.FIXED_PCT,
        atr_key: str = "atr",
        stop_atr_multiple: Decimal = Decimal("1.0"),
        take_profit_r_multiple: Decimal = Decimal("1.5"),
    ) -> None:
        super().__init__(max_holding)
        if stop_loss_pct <= 0 or take_profit_pct <= 0:
            raise ValueError("stop_loss_pct and take_profit_pct must be positive")
        self.mode = ExitMode(mode)
        if not atr_key.strip():
            raise ValueError("atr_key must not be empty")
        if stop_atr_multiple <= 0 or take_profit_r_multiple <= 0:
            raise ValueError("ATR and reward multiples must be positive")
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.atr_key = atr_key
        self.stop_atr_multiple = stop_atr_multiple
        self.take_profit_r_multiple = take_profit_r_multiple

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> TradeIntent | None:
        base = super().evaluate(snapshot, portfolio)
        if base is not None:
            return base
        quantity = portfolio.positions.get(snapshot.instrument.symbol, 0)
        average = portfolio.average_prices.get(snapshot.instrument.symbol)
        if quantity == 0 or average is None:
            return None
        current = snapshot.market.mid
        stop_distance, target_distance, threshold_source = self._thresholds(snapshot, average)
        if stop_distance <= 0 or target_distance <= 0:
            return None
        should_exit = (
            quantity > 0
            and (current <= average - stop_distance or current >= average + target_distance)
        ) or (
            quantity < 0
            and (current >= average + stop_distance or current <= average - target_distance)
        )
        if not should_exit:
            return None
        is_stop = (quantity > 0 and current <= average - stop_distance) or (
            quantity < 0 and current >= average + stop_distance
        )
        reason = "STOP_LOSS" if is_stop else "TAKE_PROFIT"
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=Action.SHORT if quantity > 0 else Action.LONG,
            requested_quantity=abs(quantity),
            strategy_id="hybrid-exit",
            reason=reason,
            created_at=snapshot.as_of,
            metadata={
                "exit_reason": reason,
                "exit_threshold_source": threshold_source,
                "stop_distance": str(stop_distance),
                "take_profit_distance": str(target_distance),
            },
        )

    def _thresholds(
        self,
        snapshot: DecisionSnapshot,
        average: Decimal,
    ) -> tuple[Decimal, Decimal, str]:
        if self.mode is ExitMode.ATR:
            raw_atr = snapshot.technical.get(self.atr_key)
            if raw_atr is not None:
                atr = Decimal(str(raw_atr))
                if atr > 0:
                    stop_distance = atr * self.stop_atr_multiple
                    return (
                        stop_distance,
                        stop_distance * self.take_profit_r_multiple,
                        f"ATR:{self.atr_key}",
                    )
        stop_distance = average * self.stop_loss_pct
        target_distance = average * self.take_profit_pct
        return stop_distance, target_distance, "FIXED_PCT_FALLBACK"


# Short aliases make the intent explicit to users of the package.
PortfolioPolicy = PaperPortfolioPolicy


__all__ = [
    "ExitMode",
    "FixedTimeExitPolicy",
    "HybridExitPolicy",
    "InMemoryPortfolioRepository",
    "PaperPortfolioPolicy",
    "PortfolioLedger",
    "PortfolioPolicy",
    "PortfolioPolicyConfig",
]
