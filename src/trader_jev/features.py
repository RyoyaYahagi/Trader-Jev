"""Deterministic point-in-time feature generation.

The feature engine deliberately keeps the hot-path representation compact. It
stores normalized events internally, but a :class:`DecisionSnapshot` contains
only the latest market state, scalar feature maps, and a short history summary.
No wall clock is read here; callers choose the ``as_of`` timestamp.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from trader_jev.models import (
    Action,
    BarEvent,
    DataQuality,
    DecisionSnapshot,
    DomainModel,
    InstrumentMetadata,
    MarketEvent,
    MarketState,
    OrderBookEvent,
    OrderBookLevel,
    QuoteEvent,
    TradeEvent,
)

FEATURE_SCHEMA_VERSION = "1.0"


class FeatureEngineConfig(DomainModel):
    """Explicit feature and data-quality policy for one engine."""

    history_window_seconds: int = Field(default=300, gt=0)
    max_data_age_seconds: float = Field(default=30.0, ge=0)
    top_n_levels: int = Field(default=5, gt=0)
    depth_within_bps: float = Field(default=5.0, gt=0)
    schema_version: str = Field(default=FEATURE_SCHEMA_VERSION, min_length=1)


class InMemoryFeatureEngine:
    """Build a deterministic, immutable snapshot from normalized events.

    ``received_at`` is the availability boundary. An event with an older
    exchange timestamp may be delayed and is therefore still unavailable until
    its receive timestamp. Events with an exchange timestamp after ``as_of``
    are also excluded as malformed future observations.
    """

    def __init__(self, config: FeatureEngineConfig | None = None) -> None:
        self._config = config or FeatureEngineConfig()
        self._events: dict[str, list[MarketEvent]] = {}
        self._event_ids: dict[str, set[UUID]] = {}

    def update(self, event: MarketEvent) -> None:
        """Consume one event, ignoring an already-seen event id."""

        key = self._key(event.instrument)
        seen = self._event_ids.setdefault(key, set())
        if event.event_id in seen:
            return
        seen.add(event.event_id)
        events = self._events.setdefault(key, [])
        events.append(event)
        events.sort(key=self._availability_key)

    def snapshot(self, instrument: InstrumentMetadata, as_of: datetime) -> DecisionSnapshot:
        """Freeze all features that were available at ``as_of``."""

        self._require_aware(as_of, "as_of")
        events = [
            event
            for event in self._events.get(self._key(instrument), ())
            if event.received_at <= as_of and event.event_time <= as_of
        ]
        if not events:
            raise ValueError(f"no market event is available for {instrument.symbol} at {as_of}")

        market_event, orderbook = self._latest_market_event(events)
        market = self._market_state(market_event, orderbook)
        reference_time = min(as_of, market.last_event_time)
        prices = self._price_samples(events)
        technical = self._technical_features(events, prices, market.mid, reference_time)
        orderbook_features = self._orderbook_features(
            events,
            orderbook,
            market,
            reference_time,
        )
        orderflow, supply_demand = self._orderflow_features(events, market, reference_time)
        data_quality = self._quality(events, market, as_of, orderbook)
        history_summary = self._history_summary(
            market.mid,
            technical,
            orderflow,
            orderbook_features,
        )
        event_fingerprint = "|".join(str(event.event_id) for event in events)
        digest = sha256(event_fingerprint.encode("utf-8")).hexdigest()
        snapshot_id = uuid5(
            NAMESPACE_URL,
            f"trader-jev/snapshot/{instrument.market.value}:{instrument.symbol}/"
            f"{as_of.isoformat()}/{digest}",
        )
        return DecisionSnapshot(
            snapshot_id=snapshot_id,
            instrument=instrument,
            event_time=market.last_event_time,
            as_of=as_of,
            market=market,
            technical=technical,
            orderbook=orderbook_features,
            orderflow=orderflow,
            supply_demand=supply_demand,
            short_history_summary=history_summary,
            data_quality=data_quality,
            schema_version=self._config.schema_version,
        )

    @staticmethod
    def _key(instrument: InstrumentMetadata) -> str:
        return f"{instrument.market.value}:{instrument.symbol}"

    @staticmethod
    def _availability_key(event: MarketEvent) -> tuple[datetime, datetime, str]:
        return (event.received_at, event.event_time, str(event.event_id))

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _latest_market_event(
        events: list[MarketEvent],
    ) -> tuple[QuoteEvent | BarEvent | None, OrderBookEvent | None]:
        books = [event for event in events if isinstance(event, OrderBookEvent)]
        quotes = [event for event in events if isinstance(event, QuoteEvent)]
        bars = [event for event in events if isinstance(event, BarEvent)]
        latest_book = max(books, key=InMemoryFeatureEngine._availability_key) if books else None
        candidates: list[QuoteEvent | BarEvent] = [*quotes, *bars]
        if latest_book is not None and latest_book.bids and latest_book.asks:
            candidates.append(
                BarEvent(
                    event_id=latest_book.event_id,
                    instrument=latest_book.instrument,
                    event_time=latest_book.event_time,
                    received_at=latest_book.received_at,
                    source=latest_book.source,
                    open=latest_book.bids[0].price,
                    high=latest_book.asks[0].price,
                    low=latest_book.bids[0].price,
                    close=(latest_book.bids[0].price + latest_book.asks[0].price) / Decimal("2"),
                    volume=Decimal("0"),
                )
            )
        if not candidates:
            return None, latest_book
        latest = max(candidates, key=InMemoryFeatureEngine._availability_key)
        return latest, latest_book

    @staticmethod
    def _market_state(
        event: QuoteEvent | BarEvent | None,
        orderbook: OrderBookEvent | None,
    ) -> MarketState:
        if isinstance(event, QuoteEvent):
            bid, ask = event.bid, event.ask
            bid_size, ask_size = event.bid_size, event.ask_size
            event_time, received_at = event.event_time, event.received_at
        elif isinstance(event, BarEvent):
            close = event.close
            bid = ask = close
            bid_size = ask_size = Decimal("0")
            event_time, received_at = event.event_time, event.received_at
            if orderbook is not None and orderbook.bids and orderbook.asks:
                bid_level, ask_level = orderbook.bids[0], orderbook.asks[0]
                bid, ask = bid_level.price, ask_level.price
                bid_size, ask_size = bid_level.size, ask_level.size
                event_time, received_at = orderbook.event_time, orderbook.received_at
        elif orderbook is not None and orderbook.bids and orderbook.asks:
            bid_level, ask_level = orderbook.bids[0], orderbook.asks[0]
            bid, ask = bid_level.price, ask_level.price
            bid_size, ask_size = bid_level.size, ask_level.size
            event_time, received_at = orderbook.event_time, orderbook.received_at
        else:
            raise ValueError("no complete quote, bar, or order book is available")
        return MarketState(
            bid=bid,
            ask=ask,
            mid=(bid + ask) / Decimal("2"),
            spread=ask - bid,
            bid_size=bid_size,
            ask_size=ask_size,
            last_event_time=event_time,
            last_received_at=received_at,
        )

    @staticmethod
    def _price_samples(events: Iterable[MarketEvent]) -> list[tuple[datetime, float]]:
        samples: list[tuple[datetime, float]] = []
        for event in events:
            if isinstance(event, QuoteEvent):
                value = event.mid
            elif isinstance(event, TradeEvent):
                value = event.price
            elif isinstance(event, BarEvent):
                value = event.close
            else:
                continue
            samples.append((event.event_time, float(value)))
        samples.sort(key=lambda item: item[0])
        return samples

    def _technical_features(
        self,
        events: list[MarketEvent],
        prices: list[tuple[datetime, float]],
        current: Decimal,
        reference_time: datetime,
    ) -> dict[str, float]:
        current_float = float(current)
        returns = {
            f"return_{label}": self._return_from(
                prices,
                reference_time,
                seconds,
                current_float,
            )
            for label, seconds in (("5s", 5), ("30s", 30), ("1m", 60), ("5m", 300))
        }
        window_prices = [
            value
            for timestamp, value in prices
            if reference_time - timedelta(seconds=300) <= timestamp <= reference_time
        ]
        if not window_prices:
            window_prices = [current_float]
        fast_values = self._ema_values(window_prices, 12)
        slow_values = self._ema_values(window_prices, 26)
        macd_values = [fast - slow for fast, slow in zip(fast_values, slow_values, strict=False)]
        macd = macd_values[-1] if macd_values else 0.0
        signal = self._ema(macd_values, 9)
        vwap = self._vwap(events, reference_time)
        return {
            **returns,
            "mid": current_float,
            "last_price": current_float,
            "vwap": vwap,
            "vwap_distance": self._relative_difference(current_float, vwap),
            "ema_fast": fast_values[-1] if fast_values else current_float,
            "ema_slow": slow_values[-1] if slow_values else current_float,
            "ema_slope": self._ema_slope(window_prices),
            "rsi": self._rsi(window_prices),
            "macd": macd,
            "macd_histogram": macd - signal,
            "atr": self._atr(events, window_prices),
            "realized_volatility": self._realized_volatility(window_prices),
        }

    @staticmethod
    def _return_from(
        samples: list[tuple[datetime, float]],
        reference_time: datetime,
        seconds: int,
        current: float,
    ) -> float:
        target = reference_time - timedelta(seconds=seconds)
        prior = [value for timestamp, value in samples if timestamp <= target]
        if not prior or prior[-1] == 0:
            return 0.0
        return (current - prior[-1]) / prior[-1]

    @staticmethod
    def _relative_difference(value: float, base: float) -> float:
        return (value - base) / base if base else 0.0

    @staticmethod
    def _ema_values(values: list[float], period: int) -> list[float]:
        if not values:
            return []
        alpha = 2.0 / (period + 1)
        result = [values[0]]
        for value in values[1:]:
            result.append(alpha * value + (1 - alpha) * result[-1])
        return result

    @classmethod
    def _ema(cls, values: list[float], period: int) -> float:
        values = cls._ema_values(values, period)
        return values[-1] if values else 0.0

    @staticmethod
    def _ema_slope(values: list[float]) -> float:
        if len(values) < 2 or values[-2] == 0:
            return 0.0
        previous = InMemoryFeatureEngine._ema_values(values[:-1], 12)[-1]
        latest = InMemoryFeatureEngine._ema_values(values, 12)[-1]
        return (latest - previous) / abs(values[-2])

    @staticmethod
    def _rsi(values: list[float]) -> float:
        changes = [
            current - previous for previous, current in zip(values, values[1:], strict=False)
        ]
        gains = [change for change in changes if change > 0]
        losses = [-change for change in changes if change < 0]
        if not gains and not losses:
            return 50.0
        if not losses:
            return 100.0
        if not gains:
            return 0.0
        relative_strength = sum(gains) / len(gains) / (sum(losses) / len(losses))
        return 100.0 - (100.0 / (1.0 + relative_strength))

    @staticmethod
    def _realized_volatility(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        returns = [
            math.log(current / previous)
            for previous, current in zip(values, values[1:], strict=False)
            if previous > 0 and current > 0
        ]
        return math.sqrt(sum(value * value for value in returns)) if returns else 0.0

    @staticmethod
    def _vwap(events: list[MarketEvent], reference_time: datetime) -> float:
        start = reference_time - timedelta(seconds=300)
        trades = [
            event
            for event in events
            if isinstance(event, TradeEvent) and start <= event.event_time <= reference_time
        ]
        volume = sum(float(event.size) for event in trades)
        if volume:
            return sum(float(event.price) * float(event.size) for event in trades) / volume
        bars = [
            event
            for event in events
            if isinstance(event, BarEvent)
            and start <= event.event_time <= reference_time
            and event.volume > 0
        ]
        bar_volume = sum(float(event.volume) for event in bars)
        return (
            sum(float(event.close) * float(event.volume) for event in bars) / bar_volume
            if bar_volume
            else 0.0
        )

    @staticmethod
    def _atr(events: list[MarketEvent], prices: list[float]) -> float:
        bars = [event for event in events if isinstance(event, BarEvent)]
        if bars:
            ranges: list[float] = []
            previous_close: float | None = None
            for bar in sorted(bars, key=lambda item: item.event_time):
                high, low, close = float(bar.high), float(bar.low), float(bar.close)
                if previous_close is None:
                    ranges.append(high - low)
                else:
                    ranges.append(
                        max(high - low, abs(high - previous_close), abs(low - previous_close))
                    )
                previous_close = close
            return sum(ranges) / len(ranges)
        if len(prices) < 2:
            return 0.0
        return sum(
            abs(current - previous) for previous, current in zip(prices, prices[1:], strict=False)
        ) / (len(prices) - 1)

    def _orderbook_features(
        self,
        events: list[MarketEvent],
        latest: OrderBookEvent | None,
        market: MarketState,
        reference_time: datetime,
    ) -> dict[str, float]:
        if latest is None:
            return {
                "spread": float(market.spread),
                "spread_bps": float(self._bps(market.spread, market.mid)),
                "imbalance": 0.0,
                "imbalance_slope": 0.0,
                "microprice": float(market.mid),
                "top_bid_depth": float(market.bid_size),
                "top_ask_depth": float(market.ask_size),
                "depth_within_bps": float(market.bid_size + market.ask_size),
                "spread_trend": 0.0,
            }
        bids = sorted(latest.bids, key=lambda level: level.price, reverse=True)
        asks = sorted(latest.asks, key=lambda level: level.price)
        n = self._config.top_n_levels
        bid_depth = sum(float(level.size) for level in bids[:n])
        ask_depth = sum(float(level.size) for level in asks[:n])
        imbalance = self._imbalance(bid_depth, ask_depth)
        previous_books = [
            event
            for event in events
            if isinstance(event, OrderBookEvent)
            and event.event_id != latest.event_id
            and event.event_time <= reference_time
        ]
        previous = max(previous_books, key=self._availability_key) if previous_books else None
        previous_imbalance = 0.0
        previous_spread = float(market.spread)
        if previous is not None and previous.bids and previous.asks:
            previous_bid = sum(float(level.size) for level in previous.bids[:n])
            previous_ask = sum(float(level.size) for level in previous.asks[:n])
            previous_imbalance = self._imbalance(previous_bid, previous_ask)
            previous_spread = float(previous.asks[0].price - previous.bids[0].price)
        depth_within = self._depth_within_bps(
            bids,
            asks,
            market.mid,
            self._config.depth_within_bps,
        )
        return {
            "spread": float(market.spread),
            "spread_bps": float(self._bps(market.spread, market.mid)),
            "spread_trend": float(market.spread) - previous_spread,
            "top_bid": float(bids[0].price) if bids else 0.0,
            "top_ask": float(asks[0].price) if asks else 0.0,
            "top_bid_depth": bid_depth,
            "top_ask_depth": ask_depth,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_within_bps": depth_within,
            "imbalance": imbalance,
            "imbalance_slope": imbalance - previous_imbalance,
            "microprice": self._microprice(market),
        }

    @staticmethod
    def _depth_within_bps(
        bids: list[OrderBookLevel],
        asks: list[OrderBookLevel],
        mid: Decimal,
        bps: float,
    ) -> float:
        band = mid * Decimal(str(bps / 10_000))
        bid_depth = sum(float(level.size) for level in bids if level.price >= mid - band)
        ask_depth = sum(float(level.size) for level in asks if level.price <= mid + band)
        return bid_depth + ask_depth

    @staticmethod
    def _imbalance(bid_depth: float, ask_depth: float) -> float:
        total = bid_depth + ask_depth
        return (bid_depth - ask_depth) / total if total else 0.0

    @staticmethod
    def _microprice(market: MarketState) -> float:
        total = market.bid_size + market.ask_size
        if not total:
            return float(market.mid)
        return float((market.ask * market.bid_size + market.bid * market.ask_size) / total)

    @staticmethod
    def _bps(spread: Decimal, mid: Decimal) -> Decimal:
        return spread / mid * Decimal("10000") if mid else Decimal("0")

    def _orderflow_features(
        self,
        events: list[MarketEvent],
        market: MarketState,
        reference_time: datetime,
    ) -> tuple[dict[str, float], dict[str, float]]:
        start = reference_time - timedelta(seconds=300)
        trades = [
            event
            for event in events
            if isinstance(event, TradeEvent) and start <= event.event_time <= reference_time
        ]
        buy_volume = sum(float(event.size) for event in trades if event.aggressor is Action.LONG)
        sell_volume = sum(float(event.size) for event in trades if event.aggressor is Action.SHORT)
        unknown_volume = sum(
            float(event.size)
            for event in trades
            if event.aggressor not in (Action.LONG, Action.SHORT)
        )
        buy_volume += unknown_volume / 2
        sell_volume += unknown_volume / 2
        total_volume = buy_volume + sell_volume
        midpoint = start + (reference_time - start) / 2
        first_half = [event for event in trades if event.event_time <= midpoint]
        second_half = [event for event in trades if event.event_time > midpoint]
        first_volume = sum(float(event.size) for event in first_half)
        second_volume = sum(float(event.size) for event in second_half)
        first_cvd = self._cvd(first_half)
        second_cvd = self._cvd(second_half)
        bars = [
            event
            for event in events
            if isinstance(event, BarEvent) and start <= event.event_time <= reference_time
        ]
        average_bar_volume = sum(float(event.volume) for event in bars) / len(bars) if bars else 0.0
        relative_volume = total_volume / average_bar_volume if average_bar_volume else 0.0
        average_size = total_volume / len(trades) if trades else 0.0
        large_volume = sum(
            float(event.size) for event in trades if float(event.size) >= average_size * 2
        )
        orderflow = {
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "total_volume": total_volume,
            "cvd": buy_volume - sell_volume,
            "cvd_trend": second_cvd - first_cvd,
            "relative_volume": relative_volume,
            "turnover_acceleration": second_volume - first_volume,
            "large_trade_ratio": large_volume / total_volume if total_volume else 0.0,
            "trade_count": float(len(trades)),
        }
        supply_demand = {
            "buy_ratio": buy_volume / total_volume if total_volume else 0.5,
            "sell_ratio": sell_volume / total_volume if total_volume else 0.5,
            "net_pressure": self._imbalance(buy_volume, sell_volume),
            "turnover": total_volume * float(market.mid),
        }
        return orderflow, supply_demand

    @staticmethod
    def _cvd(trades: Iterable[TradeEvent]) -> float:
        result = 0.0
        for event in trades:
            if event.aggressor is Action.LONG:
                result += float(event.size)
            elif event.aggressor is Action.SHORT:
                result -= float(event.size)
        return result

    def _quality(
        self,
        events: list[MarketEvent],
        market: MarketState,
        as_of: datetime,
        orderbook: OrderBookEvent | None,
    ) -> DataQuality:
        quotes = [event for event in events if isinstance(event, QuoteEvent)]
        trades = [event for event in events if isinstance(event, TradeEvent)]
        bars = [event for event in events if isinstance(event, BarEvent)]
        latest_trade = max(trades, key=self._availability_key) if trades else None
        latest_quote_or_book = max(
            [*quotes, *bars, *([orderbook] if orderbook is not None else [])],
            key=self._availability_key,
        )
        age_ms = self._age_ms(as_of, market.last_received_at)
        book_age = self._age_ms(as_of, orderbook.received_at) if orderbook else None
        trade_age = self._age_ms(as_of, latest_trade.received_at) if latest_trade else None
        source_lag = self._age_ms(latest_quote_or_book.received_at, latest_quote_or_book.event_time)
        reasons: list[str] = []
        missing: list[str] = []
        if orderbook is None:
            missing.append("orderbook")
        if latest_trade is None:
            missing.append("trade")
        if age_ms > self._config.max_data_age_seconds * 1000:
            reasons.append("market_data_stale")
        return DataQuality(
            healthy=not reasons,
            freshness_ms=age_ms,
            book_age_ms=book_age,
            trade_age_ms=trade_age,
            source_lag_ms=source_lag,
            missing_fields=tuple(missing),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _age_ms(later: datetime, earlier: datetime) -> int:
        return max(0, int((later - earlier).total_seconds() * 1000))

    @staticmethod
    def _history_summary(
        current: Decimal,
        technical: dict[str, float],
        orderflow: dict[str, float],
        orderbook: dict[str, float],
    ) -> dict[str, float]:
        return {
            "current_mid": float(current),
            "return_30s": technical["return_30s"],
            "return_5m": technical["return_5m"],
            "volatility": technical["realized_volatility"],
            "cvd": orderflow["cvd"],
            "imbalance": orderbook["imbalance"],
        }


# Keep the original small public name available to existing adapters/tests.
DeterministicFeatureEngine = InMemoryFeatureEngine
