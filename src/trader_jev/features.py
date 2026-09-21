"""Small deterministic FeatureEngine implementation for Phase 0 and replay tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trader_jev.models import (
    DataQuality,
    DecisionSnapshot,
    InstrumentMetadata,
    MarketEvent,
    MarketState,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)


class InMemoryFeatureEngine:
    """Keep normalized events in memory and expose a point-in-time snapshot.

    This is intentionally a small baseline.  It is useful for Phase 0 wiring and
    deterministic tests; production adapters can replace it without changing a
    DecisionModel or RiskEngine.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[MarketEvent]] = {}

    def update(self, event: MarketEvent) -> None:
        key = self._key(event.instrument)
        self._events.setdefault(key, []).append(event)

    def snapshot(self, instrument: InstrumentMetadata, as_of: datetime) -> DecisionSnapshot:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")

        events = [
            event
            for event in self._events.get(self._key(instrument), [])
            if event.event_time <= as_of and event.received_at <= as_of
        ]
        latest_orderbook = self._latest_event(events, OrderBookEvent)
        latest_trade = self._latest_event(events, TradeEvent)
        quotes = [event for event in events if isinstance(event, QuoteEvent)]
        latest_quote = (
            max(quotes, key=lambda event: (event.event_time, event.received_at)) if quotes else None
        )
        previous_quotes = [quote for quote in quotes if quote is not latest_quote]
        previous_quote = (
            max(previous_quotes, key=lambda event: (event.event_time, event.received_at))
            if previous_quotes
            else None
        )
        if latest_quote is not None:
            bid = latest_quote.bid
            ask = latest_quote.ask
            bid_size = latest_quote.bid_size
            ask_size = latest_quote.ask_size
            last_event_time = latest_quote.event_time
            last_received_at = latest_quote.received_at
            spread = latest_quote.spread
        elif latest_orderbook is not None and latest_orderbook.bids and latest_orderbook.asks:
            top_bid = latest_orderbook.bids[0]
            top_ask = latest_orderbook.asks[0]
            bid = top_bid.price
            ask = top_ask.price
            bid_size = top_bid.size
            ask_size = top_ask.size
            last_event_time = latest_orderbook.event_time
            last_received_at = latest_orderbook.received_at
            spread = ask - bid
        else:
            raise ValueError(
                f"no complete quote or order book is available for {instrument.symbol} "
                f"at {as_of.isoformat()}"
            )
        freshness_ms = max(
            0,
            int((as_of - last_received_at).total_seconds() * 1000),
        )
        current_mid = (bid + ask) / Decimal("2")
        previous_mid = previous_quote.mid if previous_quote is not None else current_mid

        technical = {
            "mid": float(current_mid),
            "spread": float(spread),
            "relative_spread": float(spread / current_mid),
        }
        short_history_summary = {
            "return_since_previous": float(self._return_ratio(current_mid, previous_mid)),
        }
        orderbook = self._orderbook_features(latest_orderbook)
        orderflow = {
            "last_trade_price": float(latest_trade.price) if latest_trade else 0.0,
            "last_trade_size": float(latest_trade.size) if latest_trade else 0.0,
        }
        market = MarketState(
            bid=bid,
            ask=ask,
            mid=current_mid,
            spread=spread,
            bid_size=bid_size,
            ask_size=ask_size,
            last_event_time=last_event_time,
            last_received_at=last_received_at,
        )
        return DecisionSnapshot(
            instrument=instrument,
            event_time=last_event_time,
            as_of=as_of,
            market=market,
            technical=technical,
            orderbook=orderbook,
            orderflow=orderflow,
            short_history_summary=short_history_summary,
            data_quality=DataQuality(healthy=True, freshness_ms=freshness_ms),
        )

    @staticmethod
    def _key(instrument: InstrumentMetadata) -> str:
        return f"{instrument.market.value}:{instrument.symbol}"

    @staticmethod
    def _return_ratio(current: Decimal, previous: Decimal) -> Decimal:
        if previous == 0:
            return Decimal("0")
        return (current - previous) / previous

    @staticmethod
    def _latest_event[T](events: list[MarketEvent], event_type: type[T]) -> T | None:
        matching = [event for event in events if isinstance(event, event_type)]
        if not matching:
            return None
        return max(matching, key=lambda event: (event.event_time, event.received_at))

    @staticmethod
    def _orderbook_features(event: OrderBookEvent | None) -> dict[str, float]:
        if event is None:
            return {}
        top_bid = event.bids[0] if event.bids else None
        top_ask = event.asks[0] if event.asks else None
        return {
            "top_bid": float(top_bid.price) if top_bid else 0.0,
            "top_ask": float(top_ask.price) if top_ask else 0.0,
            "top_bid_size": float(top_bid.size) if top_bid else 0.0,
            "top_ask_size": float(top_ask.size) if top_ask else 0.0,
        }
