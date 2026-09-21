"""Market-specific message normalization kept outside the core domain."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import websockets

from trader_jev.clock import SystemClock
from trader_jev.interfaces import Clock
from trader_jev.models import (
    Action,
    InstrumentMetadata,
    MarketEvent,
    OrderBookEvent,
    OrderBookLevel,
    QuoteEvent,
    TradeEvent,
)


class AdapterNormalizationError(ValueError):
    """Raised when a vendor message cannot be converted to a core event."""


class KabuStationMessageSource(Protocol):
    """Injected transport for kabuステーション WebSocket/auth details."""

    def stream(self, symbols: Sequence[str]) -> AsyncIterator[Mapping[str, Any]]:
        """Yield decoded vendor messages; no vendor type enters the core."""
        ...


class KabuStationWebSocketSource:
    """Read the official kabuステーション PUSH WebSocket as JSON mappings.

    Symbol registration is intentionally kept separate: the official API uses
    REST ``銘柄登録`` before PUSH delivery.  This source only owns the socket;
    credentials and registration can be supplied by a separate operational
    component without entering the normalized core.
    """

    def __init__(
        self,
        url: str = "ws://localhost:18080/kabusapi/websocket",
        *,
        ping_interval_seconds: float = 20.0,
    ) -> None:
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("kabu WebSocket URL must use ws:// or wss://")
        if ping_interval_seconds <= 0:
            raise ValueError("ping_interval_seconds must be positive")
        self.url = url
        self.ping_interval_seconds = ping_interval_seconds

    async def stream(self, symbols: Sequence[str]) -> AsyncIterator[Mapping[str, Any]]:
        if not symbols:
            raise ValueError("at least one symbol is required")
        if len(symbols) > 50:
            raise ValueError("kabuステーション PUSH supports at most 50 symbols")
        async with websockets.connect(
            self.url,
            ping_interval=self.ping_interval_seconds,
        ) as socket:
            async for raw_message in socket:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError as exc:
                    raise AdapterNormalizationError("kabu PUSH message is not valid JSON") from exc
                if not isinstance(message, Mapping):
                    raise AdapterNormalizationError("kabu PUSH message must be a JSON object")
                yield message


class KabuStationMarketDataAdapter:
    """Normalize kabuステーション-like messages into core MarketEvents.

    The transport is injected deliberately: credentials, token refresh, socket
    reconnect, and vendor SDK details stay in the source implementation.  This
    adapter owns only field mapping and timestamp normalization.
    """

    def __init__(
        self,
        source: KabuStationMessageSource,
        *,
        clock: Clock | None = None,
        source_name: str = "kabu-station",
    ) -> None:
        self._source = source
        self._clock = clock or SystemClock()
        self._source_name = source_name

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        by_symbol = {instrument.symbol: instrument for instrument in instruments}
        async for message in self._source.stream(tuple(by_symbol)):
            symbol_value = self._pick(message, "symbol", "Symbol", default=None)
            symbol = str(symbol_value) if symbol_value is not None else None
            if symbol is None or symbol not in by_symbol:
                if len(by_symbol) == 1:
                    instrument = next(iter(by_symbol.values()))
                else:
                    raise AdapterNormalizationError(
                        f"message symbol is missing or unknown: {symbol_value!r}"
                    )
            else:
                instrument = by_symbol[symbol]
            yield self.normalize_message(message, instrument)

    def normalize_message(
        self,
        message: Mapping[str, Any],
        instrument: InstrumentMetadata,
    ) -> MarketEvent:
        event_type = self._event_type(message)
        event_time = self._timestamp(message, instrument)
        sequence_number = self._integer(
            self._pick(message, "sequence_number", "SequenceNumber", "seq", default=None)
        )
        event_id = self._event_id(message, instrument, event_type, sequence_number, event_time)
        common: dict[str, Any] = {
            "event_id": event_id,
            "instrument": instrument,
            "event_time": event_time,
            "received_at": self._clock.now(),
            "source": self._source_name,
            "schema_version": str(self._pick(message, "schema_version", default="1.0")),
            "sequence_number": sequence_number,
            "trading_status": self._text(
                self._pick(
                    message,
                    "trading_status",
                    "TradingStatus",
                    "CurrentPriceStatus",
                    default=None,
                )
            ),
        }
        if event_type == "quote":
            return QuoteEvent(
                **common,
                bid=self._decimal(
                    self._pick(message, "bid", "BidPrice", "BestBidPrice", "Bid1"),
                    "bid",
                ),
                ask=self._decimal(
                    self._pick(message, "ask", "AskPrice", "BestAskPrice", "Ask1"),
                    "ask",
                ),
                bid_size=self._decimal(
                    self._pick(message, "bid_size", "BidQty", "BestBidQty", "Bid1Qty", default=0),
                    "bid_size",
                ),
                ask_size=self._decimal(
                    self._pick(message, "ask_size", "AskQty", "BestAskQty", "Ask1Qty", default=0),
                    "ask_size",
                ),
            )
        if event_type == "trade":
            return TradeEvent(
                **common,
                price=self._decimal(
                    self._pick(message, "price", "CurrentPrice", "trade_price"),
                    "price",
                ),
                size=self._decimal(
                    self._pick(message, "size", "TradeSize", "CurrentPriceSize", "volume"),
                    "size",
                ),
                aggressor=self._action(
                    self._pick(message, "aggressor", "AggressorSide", "side", default=None)
                ),
            )
        return OrderBookEvent(
            **common,
            bids=self._levels(message, "bids", "Bids", "bid_levels", side="bid"),
            asks=self._levels(message, "asks", "Asks", "ask_levels", side="ask"),
        )

    @classmethod
    def _event_type(cls, message: Mapping[str, Any]) -> str:
        raw = cls._pick(message, "event_type", "EventType", "type", "Type", default=None)
        if raw is not None:
            value = str(raw).lower().replace("-", "_").replace(" ", "_")
            if value in {"trade", "execution", "tick", "transaction"}:
                return "trade"
            if value in {"order_book", "orderbook", "book", "depth", "l2"}:
                return "order_book"
            if value in {"quote", "best_quote", "board"}:
                return "quote"
        if (
            cls._pick(
                message,
                "bids",
                "Bids",
                "asks",
                "Asks",
                "Buy1",
                "Sell1",
                default=None,
            )
            is not None
        ):
            return "order_book"
        return "quote"

    def _timestamp(self, message: Mapping[str, Any], instrument: InstrumentMetadata) -> datetime:
        raw = self._pick(
            message,
            "event_time",
            "exchange_time",
            "ExchangeTime",
            "CurrentPriceTime",
            "timestamp",
            "time",
            default=None,
        )
        if raw is None:
            return self._clock.now()
        if isinstance(raw, datetime):
            parsed = raw
        elif isinstance(raw, (int, float)):
            parsed = datetime.fromtimestamp(raw, tz=UTC)
        else:
            text = str(raw).replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise AdapterNormalizationError(f"invalid exchange timestamp: {raw!r}") from exc
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
        try:
            return parsed.replace(tzinfo=ZoneInfo(instrument.timezone))
        except ZoneInfoNotFoundError as exc:
            raise AdapterNormalizationError(
                f"unknown instrument timezone: {instrument.timezone!r}"
            ) from exc

    @classmethod
    def _event_id(
        cls,
        message: Mapping[str, Any],
        instrument: InstrumentMetadata,
        event_type: str,
        sequence_number: int | None,
        event_time: datetime,
    ) -> UUID:
        raw = cls._pick(message, "event_id", "EventId", "id", default=None)
        if raw is not None:
            try:
                return UUID(str(raw))
            except ValueError:
                return uuid5(NAMESPACE_URL, f"{instrument.symbol}:{raw}")
        if sequence_number is not None:
            return uuid5(
                NAMESPACE_URL,
                f"{instrument.market.value}:{instrument.symbol}:{event_type}:"
                f"{sequence_number}:{event_time.isoformat()}",
            )
        return uuid5(
            NAMESPACE_URL,
            f"{instrument.market.value}:{instrument.symbol}:{event_type}:"
            f"{event_time.isoformat()}:{canonical_message_json(message)}",
        )

    @classmethod
    def _levels(
        cls,
        message: Mapping[str, Any],
        *names: str,
        side: str,
    ) -> tuple[OrderBookLevel, ...]:
        raw_levels = cls._pick(message, *names, default=None)
        if raw_levels is None:
            raw_levels = cls._numbered_levels(message, side)
        if raw_levels is None:
            return ()
        if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
            raise AdapterNormalizationError(f"{side} levels must be a sequence")
        levels: list[OrderBookLevel] = []
        for raw_level in cast(Sequence[Any], raw_levels):
            if isinstance(raw_level, Mapping):
                mapping_level = cast(Mapping[str, Any], raw_level)
                price = cls._pick(mapping_level, "price", "Price", "p")
                size = cls._pick(mapping_level, "size", "Qty", "quantity", "q")
            elif isinstance(raw_level, Sequence):
                level_values = cast(Sequence[Any], raw_level)
                if len(level_values) < 2:
                    raise AdapterNormalizationError(f"invalid {side} level: {raw_level!r}")
                price, size = level_values[0], level_values[1]
            else:
                raise AdapterNormalizationError(f"invalid {side} level: {raw_level!r}")
            levels.append(
                OrderBookLevel(
                    price=cls._decimal(price, f"{side}.price"),
                    size=cls._decimal(size, f"{side}.size"),
                )
            )
        return tuple(levels)

    @classmethod
    def _numbered_levels(cls, message: Mapping[str, Any], side: str) -> tuple[tuple[Any, Any], ...]:
        levels: list[tuple[Any, Any]] = []
        prefix = "Buy" if side == "bid" else "Sell"
        for level_number in range(1, 11):
            raw_level = cls._pick(message, f"{prefix}{level_number}", default=None)
            if raw_level is None:
                continue
            if isinstance(raw_level, Mapping):
                mapping_level = cast(Mapping[str, Any], raw_level)
                price = cls._pick(mapping_level, "price", "Price", "p", default=None)
                size = cls._pick(mapping_level, "size", "Qty", "quantity", "q", default=None)
                if price is not None and size is not None:
                    levels.append((price, size))
                continue
            if isinstance(raw_level, Sequence) and not isinstance(raw_level, (str, bytes)):
                values = list(cast(Sequence[Any], raw_level))
                if len(values) >= 2:
                    if len(values) >= 4 and not isinstance(values[0], (int, float, Decimal)):
                        levels.append((values[2], values[3]))
                    else:
                        levels.append((values[0], values[1]))
        return tuple(levels)

    @staticmethod
    def _pick(
        message: Mapping[str, Any],
        *names: str,
        default: Any = ...,
    ) -> Any:
        for name in names:
            if name in message and message[name] is not None:
                return message[name]
        if default is not ...:
            return default
        raise AdapterNormalizationError(f"missing required market field; tried {names!r}")

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise AdapterNormalizationError(f"invalid decimal for {field_name}: {value!r}") from exc
        if not parsed.is_finite():
            raise AdapterNormalizationError(f"non-finite decimal for {field_name}: {value!r}")
        return parsed

    @staticmethod
    def _integer(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise AdapterNormalizationError(f"invalid sequence number: {value!r}") from exc

    @staticmethod
    def _text(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _action(value: Any) -> Action | None:
        if value is None:
            return None
        normalized = str(value).upper()
        if normalized in {"BUY", "B", "LONG"}:
            return Action.LONG
        if normalized in {"SELL", "S", "SHORT"}:
            return Action.SHORT
        return None


def canonical_message_json(message: Mapping[str, Any]) -> str:
    """Stable representation useful for adapter fixtures and audit diagnostics."""

    try:
        return json.dumps(message, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AdapterNormalizationError("market message is not JSON-serializable") from exc
