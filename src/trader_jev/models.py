"""Immutable, serializable domain models shared by every adapter.

The models in this module deliberately contain no broker- or market-SDK-specific
types.  Adapters are responsible for translating their native objects into these
models before they enter the core.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainModel(BaseModel):
    """Base class for domain values.

    Freezing the top-level model prevents accidental mutation after a snapshot or
    intent has been handed to another component.  Nested sections use read-only
    typing and are copied by Pydantic during validation.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class Market(StrEnum):
    JP = "JP"
    US = "US"


class Action(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"


class Direction(StrEnum):
    UP = "UP"
    FLAT = "FLAT"
    DOWN = "DOWN"


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    NEWS_SHOCK = "NEWS_SHOCK"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class TradingSession(DomainModel):
    """A regular trading session expressed in the instrument timezone."""

    open_time: time
    close_time: time


class PriceLimit(DomainModel):
    lower: Decimal | None = Field(default=None, gt=Decimal("0"))
    upper: Decimal | None = Field(default=None, gt=Decimal("0"))

    @model_validator(mode="after")
    def validate_order(self) -> PriceLimit:
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ValueError("price limit lower must be less than upper")
        return self


class InstrumentMetadata(DomainModel):
    """Market-neutral instrument information required by core components."""

    symbol: str = Field(min_length=1)
    market: Market
    currency: str = Field(min_length=3, max_length=3)
    timezone: str = Field(min_length=1)
    tick_size: Decimal = Field(gt=Decimal("0"))
    lot_size: int = Field(gt=0)
    trading_session: TradingSession
    shortability: bool
    price_limit: PriceLimit | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class EventModel(DomainModel):
    event_id: UUID = Field(default_factory=uuid4)
    instrument: InstrumentMetadata
    event_time: datetime
    received_at: datetime
    source: str = Field(min_length=1)
    schema_version: str = "1.0"
    sequence_number: int | None = Field(default=None, ge=0)
    trading_status: str | None = None

    _event_time_aware = field_validator("event_time", "received_at")(_aware)


class QuoteEvent(EventModel):
    bid: Decimal = Field(gt=Decimal("0"))
    ask: Decimal = Field(gt=Decimal("0"))
    bid_size: Decimal = Field(ge=Decimal("0"))
    ask_size: Decimal = Field(ge=Decimal("0"))

    @model_validator(mode="after")
    def validate_market(self) -> QuoteEvent:
        if self.bid > self.ask:
            raise ValueError("bid must not be greater than ask")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


class TradeEvent(EventModel):
    price: Decimal = Field(gt=Decimal("0"))
    size: Decimal = Field(gt=Decimal("0"))
    aggressor: Action | None = None


class OrderBookLevel(DomainModel):
    price: Decimal = Field(gt=Decimal("0"))
    size: Decimal = Field(ge=Decimal("0"))


class OrderBookEvent(EventModel):
    bids: tuple[OrderBookLevel, ...] = ()
    asks: tuple[OrderBookLevel, ...] = ()


class BarEvent(EventModel):
    """A time-bucketed OHLCV observation from a historical dataset."""

    open: Decimal = Field(gt=Decimal("0"))
    high: Decimal = Field(gt=Decimal("0"))
    low: Decimal = Field(gt=Decimal("0"))
    close: Decimal = Field(gt=Decimal("0"))
    volume: Decimal = Field(ge=Decimal("0"))
    interval_seconds: int = Field(default=60, gt=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> BarEvent:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high must be at least open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low must be at most open, close, and high")
        return self


class NewsEvent(EventModel):
    headline: str = Field(min_length=1)
    summary: str | None = None
    published_at: datetime
    first_seen_at: datetime
    related_symbols: tuple[str, ...] = ()
    event_type: str | None = None
    direction: Direction | None = None
    materiality: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))

    _news_time_aware = field_validator("published_at", "first_seen_at")(_aware)


class MarketState(DomainModel):
    bid: Decimal = Field(gt=Decimal("0"))
    ask: Decimal = Field(gt=Decimal("0"))
    mid: Decimal = Field(gt=Decimal("0"))
    spread: Decimal = Field(ge=Decimal("0"))
    bid_size: Decimal = Field(ge=Decimal("0"))
    ask_size: Decimal = Field(ge=Decimal("0"))
    last_event_time: datetime
    last_received_at: datetime

    _market_time_aware = field_validator("last_event_time", "last_received_at")(_aware)

    @model_validator(mode="after")
    def validate_quote(self) -> MarketState:
        if self.bid > self.ask:
            raise ValueError("market bid must not be greater than ask")
        if self.mid < self.bid or self.mid > self.ask:
            raise ValueError("market mid must be between bid and ask")
        if self.spread != self.ask - self.bid:
            raise ValueError("market spread must equal ask minus bid")
        return self


class DataQuality(DomainModel):
    healthy: bool = True
    freshness_ms: int | None = Field(default=None, ge=0)
    reasons: tuple[str, ...] = ()


class DecisionSnapshot(DomainModel):
    """Point-in-time input shared by rule, Jev, and ML decision models."""

    snapshot_id: UUID = Field(default_factory=uuid4)
    instrument: InstrumentMetadata
    event_time: datetime
    as_of: datetime
    market: MarketState
    technical: Mapping[str, float] = Field(default_factory=dict)
    orderbook: Mapping[str, float] = Field(default_factory=dict)
    orderflow: Mapping[str, float] = Field(default_factory=dict)
    supply_demand: Mapping[str, float] = Field(default_factory=dict)
    short_history_summary: Mapping[str, float] = Field(default_factory=dict)
    news: Mapping[str, Any] = Field(default_factory=dict)
    ml: Mapping[str, Any] = Field(default_factory=dict)
    portfolio: Mapping[str, Any] = Field(default_factory=dict)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    schema_version: str = "1.0"

    _snapshot_time_aware = field_validator("event_time", "as_of")(_aware)

    @model_validator(mode="after")
    def validate_point_in_time(self) -> DecisionSnapshot:
        if self.event_time > self.as_of:
            raise ValueError("snapshot event_time cannot be after as_of")
        return self


class PredictionOutput(DomainModel):
    direction_5m: Direction
    p_up: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    p_flat: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    p_down: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    expected_return_bps: Decimal | None = None
    model_version: str = Field(min_length=1)
    trained_until: datetime | date | None = None
    calibration_metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("trained_until")
    @classmethod
    def validate_trained_until(cls, value: datetime | date | None) -> datetime | date | None:
        if isinstance(value, datetime):
            return _aware(value)
        return value


class TradeIntent(DomainModel):
    """A model decision; it is not permission to place an order."""

    intent_id: UUID = Field(default_factory=uuid4)
    snapshot_id: UUID
    instrument: InstrumentMetadata
    action: Action
    requested_quantity: int | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    confidence: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    strategy_id: str = Field(min_length=1)
    model_version: str | None = None
    reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    _intent_time_aware = field_validator("created_at")(_aware)


class PortfolioState(DomainModel):
    portfolio_id: str = Field(min_length=1)
    cash: Decimal = Field(ge=Decimal("0"))
    positions: Mapping[str, int] = Field(default_factory=dict)
    open_orders: int = Field(default=0, ge=0)
    daily_pnl: Decimal = Decimal("0")
    drawdown: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


class OrderIntent(DomainModel):
    """The only order-shaped object a BrokerAdapter may receive."""

    order_intent_id: UUID = Field(default_factory=uuid4)
    source_trade_intent_id: UUID
    instrument: InstrumentMetadata
    side: Action
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    time_in_force: TimeInForce = TimeInForce.DAY
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _order_time_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def validate_order(self) -> OrderIntent:
        if self.side is Action.HOLD:
            raise ValueError("an order cannot have HOLD as its side")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        return self


class RiskDecision(DomainModel):
    risk_decision_id: UUID = Field(default_factory=uuid4)
    trade_intent_id: UUID
    approved: bool
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    order_intent: OrderIntent | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _risk_time_aware = field_validator("checked_at")(_aware)


class OrderEvent(DomainModel):
    order_intent_id: UUID
    status: OrderStatus
    occurred_at: datetime
    broker_order_id: str | None = None
    reason: str | None = None

    _order_event_time_aware = field_validator("occurred_at")(_aware)


class FillEvent(DomainModel):
    order_intent_id: UUID
    fill_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime
    price: Decimal = Field(gt=Decimal("0"))
    quantity: int = Field(gt=0)
    fees: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    broker_fill_id: str | None = None

    _fill_time_aware = field_validator("occurred_at")(_aware)


MarketEvent = QuoteEvent | TradeEvent | OrderBookEvent | BarEvent
LedgerEvent = OrderEvent | FillEvent
