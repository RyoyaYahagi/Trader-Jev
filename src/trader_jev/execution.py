"""Paper-only execution models and a deterministic PaperBroker.

The broker accepts only normalized :class:`OrderIntent` values.  It never
contacts an external venue and keeps enough order/fill state for a Portfolio
Ledger to reconstruct the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from pydantic import Field

from trader_jev.clock import SystemClock
from trader_jev.fees import MoomooFeeCalculator, MoomooFeeSchedule
from trader_jev.interfaces import BrokerAdapter, Clock
from trader_jev.models import (
    Action,
    DomainModel,
    EntryModel,
    ExecutionMode,
    FillEvent,
    InstrumentMetadata,
    MarketState,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    OrderType,
    QuoteEvent,
)

if TYPE_CHECKING:
    from trader_jev.portfolio import PortfolioLedger


class ExecutionConfig(DomainModel):
    """Explicit costs and fill assumptions for one Paper execution run."""

    entry_model: EntryModel = EntryModel.MARKET
    fee_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    fee_schedule: MoomooFeeSchedule = MoomooFeeSchedule.BASIS_POINTS
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    latency_ms: int = Field(default=0, ge=0)
    partial_fill_ratio: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("1"))
    max_volume_participation: Decimal = Field(
        default=Decimal("1"),
        gt=Decimal("0"),
        le=Decimal("1"),
    )
    limit_timeout_seconds: int = Field(default=30, gt=0)
    fill_on_touch: bool = True


@dataclass
class _TrackedOrder:
    order: OrderIntent
    broker_order_id: str
    submitted_at: datetime
    filled_quantity: int = 0
    filled_notional: Decimal = Decimal("0")
    canceled: bool = False


class PaperBroker(BrokerAdapter):
    """Deterministic broker simulator with market/limit/partial fills."""

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        *,
        clock: Clock | None = None,
        market_states: Mapping[str, MarketState] | None = None,
        ledger: PortfolioLedger | None = None,
    ) -> None:
        self.config = config or ExecutionConfig()
        self._clock = clock or SystemClock()
        self._market_states: dict[str, MarketState] = dict(market_states or {})
        self._orders: dict[str, _TrackedOrder] = {}
        self._order_events: list[OrderEvent] = []
        self._fills: list[FillEvent] = []
        self._sequence = 0
        self._ledger = ledger
        self._fee_calculator = MoomooFeeCalculator(
            self.config.fee_schedule,
            fee_bps=self.config.fee_bps,
        )

    @property
    def orders(self) -> tuple[OrderIntent, ...]:
        return tuple(tracked.order for tracked in self._orders.values())

    @property
    def order_events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._order_events)

    @property
    def fills(self) -> tuple[FillEvent, ...]:
        return tuple(self._fills)

    @property
    def pending_orders(self) -> tuple[OrderIntent, ...]:
        return tuple(
            tracked.order
            for tracked in self._orders.values()
            if not tracked.canceled and tracked.filled_quantity < tracked.order.quantity
        )

    def update_market(
        self,
        market: QuoteEvent | MarketState,
        instrument: InstrumentMetadata | None = None,
    ) -> tuple[OrderEvent, ...]:
        """Update a cached quote and retry pending limit orders."""

        if isinstance(market, QuoteEvent):
            state = MarketState(
                bid=market.bid,
                ask=market.ask,
                mid=market.mid,
                spread=market.spread,
                bid_size=market.bid_size,
                ask_size=market.ask_size,
                last_event_time=market.event_time,
                last_received_at=market.received_at,
            )
            key = self._key(market.instrument)
        else:
            if instrument is None:
                raise ValueError("instrument is required when updating with MarketState")
            state = market
            key = self._key(instrument)
        self._market_states[key] = state
        return self._process_pending()

    async def submit(self, order: OrderIntent) -> OrderEvent:
        """Accept and immediately simulate an order when the market permits."""

        existing = self._orders.get(str(order.order_intent_id))
        if existing is not None:
            return self._latest_event(existing.order.order_intent_id)

        now = self._clock.now()
        self._sequence += 1
        tracked = _TrackedOrder(
            order=order,
            broker_order_id=f"paper-{self._sequence:08d}",
            submitted_at=now,
        )
        self._orders[str(order.order_intent_id)] = tracked

        if order.execution_mode is not ExecutionMode.PAPER:
            return self._record_order_event(
                tracked,
                OrderStatus.REJECTED,
                "PAPER_BROKER_REQUIRES_PAPER_MODE",
            )
        market = self._market_states.get(self._key(order.instrument))
        if market is None:
            return self._record_order_event(tracked, OrderStatus.REJECTED, "NO_MARKET_DATA")
        return self._attempt_fill(tracked, market, force_market=False)

    async def cancel(self, order: OrderIntent) -> OrderEvent:
        tracked = self._orders.get(str(order.order_intent_id))
        if tracked is None:
            return OrderEvent(
                order_intent_id=order.order_intent_id,
                status=OrderStatus.REJECTED,
                occurred_at=self._clock.now(),
                reason="UNKNOWN_ORDER",
            )
        if tracked.canceled or tracked.filled_quantity >= order.quantity:
            return self._latest_event(order.order_intent_id)
        tracked.canceled = True
        return self._record_order_event(tracked, OrderStatus.CANCELED, "CANCELED_BY_REQUEST")

    def _process_pending(self) -> tuple[OrderEvent, ...]:
        events: list[OrderEvent] = []
        for tracked in tuple(self._orders.values()):
            if tracked.canceled or tracked.filled_quantity >= tracked.order.quantity:
                continue
            market = self._market_states.get(self._key(tracked.order.instrument))
            if market is None:
                continue
            timed_out = self._entry_model(
                tracked.order
            ) is EntryModel.LIMIT_THEN_MARKET and self._clock.now() >= (
                tracked.submitted_at + timedelta(seconds=self.config.limit_timeout_seconds)
            )
            event = self._attempt_fill(tracked, market, force_market=timed_out)
            events.append(event)
        return tuple(events)

    def _attempt_fill(
        self,
        tracked: _TrackedOrder,
        market: MarketState,
        *,
        force_market: bool,
    ) -> OrderEvent:
        order = tracked.order
        remaining = order.quantity - tracked.filled_quantity
        entry_model = self._entry_model(order)
        is_limit = (
            not force_market
            and entry_model is EntryModel.LIMIT
            or not force_market
            and entry_model is EntryModel.LIMIT_THEN_MARKET
            or not force_market
            and order.order_type is OrderType.LIMIT
        )
        base_price = self._base_price(order, market)
        fill_price = (
            self._limit_price(order, market) if is_limit else self._slipped_price(order, base_price)
        )
        if fill_price is None:
            if entry_model is EntryModel.LIMIT_THEN_MARKET and not force_market:
                return self._record_order_event(tracked, OrderStatus.ACCEPTED, "LIMIT_PENDING")
            return self._record_order_event(tracked, OrderStatus.ACCEPTED, "NO_FILL")

        fill_quantity = self._fill_quantity(order, market, remaining)
        if fill_quantity <= 0:
            return self._record_order_event(tracked, OrderStatus.ACCEPTED, "NO_LIQUIDITY")
        occurred_at = self._clock.now() + timedelta(milliseconds=self.config.latency_ms)
        fee_breakdown = self._fee_calculator.calculate(
            order.instrument,
            fill_price,
            fill_quantity,
            previous_quantity=tracked.filled_quantity,
            previous_notional=tracked.filled_notional,
            side=order.side,
        )
        slippage = fill_price - base_price
        fill = FillEvent(
            order_intent_id=order.order_intent_id,
            occurred_at=occurred_at,
            price=fill_price,
            quantity=fill_quantity,
            fees=fee_breakdown.total,
            fee_breakdown=fee_breakdown,
            instrument=order.instrument,
            side=order.side,
            slippage=slippage,
            liquidity="PAPER",
            broker_fill_id=f"paper-fill-{len(self._fills) + 1:08d}",
            metadata={
                "entry_model": entry_model.value,
                "base_price": str(base_price),
                "fee_schedule": fee_breakdown.schedule,
                "fee_currency": fee_breakdown.currency,
            },
        )
        tracked.filled_quantity += fill_quantity
        tracked.filled_notional += fee_breakdown.notional
        status = (
            OrderStatus.FILLED
            if tracked.filled_quantity >= order.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        order_event = self._record_order_event(tracked, status, "FILLED")
        self._fills.append(fill)
        if self._ledger is not None:
            self._ledger.record_fill(order, fill)
        return order_event

    def _fill_quantity(
        self,
        order: OrderIntent,
        market: MarketState,
        remaining: int,
    ) -> int:
        available = market.ask_size if order.side is Action.LONG else market.bid_size
        if available <= 0:
            available_quantity = remaining
        else:
            volume_limit = available * self.config.max_volume_participation
            available_quantity = int(volume_limit.to_integral_value(rounding=ROUND_DOWN))
        ratio_quantity = int(
            (Decimal(remaining) * self.config.partial_fill_ratio).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        quantity = min(remaining, available_quantity, ratio_quantity or (1 if remaining else 0))
        lot_size = order.instrument.lot_size
        if quantity < remaining and lot_size > 1:
            quantity -= quantity % lot_size
        return quantity

    def _base_price(self, order: OrderIntent, market: MarketState) -> Decimal:
        return market.ask if order.side is Action.LONG else market.bid

    def _slipped_price(self, order: OrderIntent, base_price: Decimal) -> Decimal:
        multiplier = Decimal("1") + self.config.slippage_bps / Decimal("10000")
        if order.side is Action.SHORT:
            multiplier = Decimal("1") - self.config.slippage_bps / Decimal("10000")
        return base_price * multiplier

    def _limit_price(self, order: OrderIntent, market: MarketState) -> Decimal | None:
        if order.limit_price is None:
            return None
        if order.side is Action.LONG:
            executable = market.ask <= order.limit_price
        else:
            executable = market.bid >= order.limit_price
        if not executable:
            return None
        return (
            min(order.limit_price, market.ask)
            if order.side is Action.LONG
            else max(order.limit_price, market.bid)
        )

    def _entry_model(self, order: OrderIntent) -> EntryModel:
        default = (
            EntryModel.LIMIT.value
            if order.limit_price is not None
            else self.config.entry_model.value
        )
        value = order.metadata.get("entry_model", default)
        try:
            return EntryModel(str(value))
        except ValueError:
            return self.config.entry_model

    def _record_order_event(
        self,
        tracked: _TrackedOrder,
        status: OrderStatus,
        reason: str,
    ) -> OrderEvent:
        event = OrderEvent(
            order_intent_id=tracked.order.order_intent_id,
            status=status,
            occurred_at=self._clock.now(),
            broker_order_id=tracked.broker_order_id,
            reason=reason,
            quantity=tracked.filled_quantity or tracked.order.quantity,
            instrument=tracked.order.instrument,
            side=tracked.order.side,
            metadata={"filled_quantity": tracked.filled_quantity},
        )
        self._order_events.append(event)
        if self._ledger is not None:
            self._ledger.record_order(tracked.order, event)
        return event

    def _latest_event(self, order_id: object) -> OrderEvent:
        for event in reversed(self._order_events):
            if event.order_intent_id == order_id:
                return event
        raise RuntimeError("order has no recorded event")

    @staticmethod
    def _key(instrument: InstrumentMetadata) -> str:
        return f"{instrument.market.value}:{instrument.symbol}"


PaperExecutionConfig = ExecutionConfig


__all__ = ["ExecutionConfig", "PaperBroker", "PaperExecutionConfig"]
