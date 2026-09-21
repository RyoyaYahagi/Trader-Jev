"""Point-in-time, deterministic historical event replay.

Replay is driven by *availability* time rather than exchange/event time.  A
delayed event may therefore be replayed after a newer exchange event, but it
cannot be observed before it was received.  This is the central rule that
keeps historical strategy evaluation honest.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast

from trader_jev.clock import ReplayClock
from trader_jev.interfaces import HistoricalDataAdapter, MarketDataAdapter
from trader_jev.models import (
    InstrumentMetadata,
    Market,
    MarketEvent,
    NewsEvent,
    PredictionOutput,
    ReplayEvent,
)
from trader_jev.storage import ParquetEventStore

ReplaySource = Iterable[ReplayEvent] | AsyncIterable[ReplayEvent]
ReplayCallback = Callable[[ReplayEvent], object | Awaitable[object]]
ReplaySleep = Callable[[float], Awaitable[object]]


class ReplayError(RuntimeError):
    """Base class for replay configuration and point-in-time failures."""


class PointInTimeViolation(ReplayError, ValueError):
    """Raised when an event or model metadata is not available at ``as_of``."""


def event_available_at(event: ReplayEvent) -> datetime:
    """Return the earliest timestamp at which an event may enter a strategy.

    Market events use ``received_at``.  News must pass all of its publication,
    first-seen, and receive-time boundaries; taking the maximum prevents a
    backfilled headline from becoming visible before the data actually existed.
    """

    if isinstance(event, NewsEvent):
        return max(event.published_at, event.first_seen_at, event.received_at)
    return event.received_at


def validate_event_point_in_time(event: ReplayEvent, as_of: datetime) -> None:
    """Fail closed if an event contains information from after ``as_of``."""

    _require_aware(as_of, "as_of")
    available_at = event_available_at(event)
    if available_at > as_of:
        raise PointInTimeViolation(
            f"{type(event).__name__} became available at {available_at.isoformat()}, "
            f"after replay time {as_of.isoformat()}"
        )
    if event.event_time > as_of:
        raise PointInTimeViolation(
            f"{type(event).__name__} event_time {event.event_time.isoformat()} "
            f"is after replay time {as_of.isoformat()}"
        )


def available_news(events: Iterable[NewsEvent], as_of: datetime) -> tuple[NewsEvent, ...]:
    """Return only news that is fully available at a point in time.

    The helper is intentionally independent of the News worker so it can be
    used by a feature builder, a Jev adapter, or an ML adapter without changing
    any of those strategy interfaces.
    """

    _require_aware(as_of, "as_of")
    selected: list[NewsEvent] = []
    for event in events:
        if event_available_at(event) <= as_of and event.event_time <= as_of:
            selected.append(event)
    selected.sort(key=lambda event: _event_sort_key(event, 0))
    return tuple(selected)


def validate_prediction_metadata(prediction: PredictionOutput, as_of: datetime) -> None:
    """Reject a prediction trained with data that was not yet available.

    A date-only ``trained_until`` is interpreted conservatively as the end of
    that calendar day.  Consequently a model trained through the current day is
    not accepted for an intraday point-in-time decision.
    """

    _require_aware(as_of, "as_of")
    trained_until = prediction.trained_until
    if trained_until is None:
        return
    if isinstance(trained_until, datetime):
        if trained_until > as_of:
            raise PointInTimeViolation(
                f"prediction was trained until {trained_until.isoformat()}, "
                f"after replay time {as_of.isoformat()}"
            )
        return
    if trained_until >= as_of.date():
        raise PointInTimeViolation(
            f"prediction was trained through {trained_until.isoformat()}, "
            f"which is not a completed date before replay time {as_of.isoformat()}"
        )


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Immutable configuration for one replay experiment."""

    start: datetime | None = None
    end: datetime | None = None
    speed: float | str = "max"
    seed: int = 0
    markets: frozenset[Market] = frozenset()
    symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _validate_bounds(self.start, self.end)
        object.__setattr__(self, "speed", _normalise_speed_value(self.speed))
        object.__setattr__(
            self,
            "markets",
            frozenset(_normalise_market(value) for value in _values(self.markets)),
        )
        normalized_symbols = frozenset(str(value).strip() for value in _values(self.symbols))
        if "" in normalized_symbols:
            raise ValueError("symbols must not contain an empty value")
        object.__setattr__(self, "symbols", normalized_symbols)

    @property
    def speed_multiplier(self) -> float | None:
        """Return the finite playback multiplier, or ``None`` for max speed."""

        if isinstance(self.speed, str):
            return None
        return self.speed

    @property
    def max_speed(self) -> bool:
        return self.speed_multiplier is None

    def with_overrides(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        speed: float | str | None = None,
        seed: int | None = None,
        markets: Iterable[Market | str] = (),
        symbols: Iterable[str] = (),
    ) -> ReplayConfig:
        """Return a copy with explicitly supplied non-empty run overrides."""

        market_values = _values(markets)
        symbol_values = _values(symbols)
        return replace(
            self,
            start=self.start if start is None else start,
            end=self.end if end is None else end,
            speed=self.speed if speed is None else speed,
            seed=self.seed if seed is None else seed,
            markets=self.markets if not market_values else frozenset(market_values),
            symbols=self.symbols if not symbol_values else frozenset(symbol_values),
        )


class ReplaySubscription:
    """Handle returned by :meth:`ReplayEngine.subscribe`."""

    def __init__(self, engine: ReplayEngine, token: int) -> None:
        self._engine = engine
        self._token = token
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def unsubscribe(self) -> None:
        if self._active:
            self._engine.discard_subscription(self._token)
            self._active = False

    def __enter__(self) -> ReplaySubscription:
        return self

    def __exit__(self, *_: object) -> None:
        self.unsubscribe()


@dataclass(frozen=True, slots=True)
class _Subscriber:
    callback: ReplayCallback
    event_types: tuple[type[object], ...] | None


class ReplayEngine:
    """Merge and replay finite historical sources through a monotonic clock.

    ``speed=1`` preserves historical wall-time gaps, ``speed=N`` compresses
    them by N, and ``speed="max"`` never sleeps.  A custom sleeper makes timing
    behavior testable without waiting in real time.
    """

    def __init__(
        self,
        events: Iterable[ReplayEvent] | None = None,
        *,
        clock: ReplayClock | None = None,
        config: ReplayConfig | None = None,
        sleep: ReplaySleep | None = None,
    ) -> None:
        self._events = tuple(events or ())
        self._clock = clock or ReplayClock()
        self._config = config or ReplayConfig()
        self._sleep = sleep or _async_sleep
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_token = 0

    @property
    def clock(self) -> ReplayClock:
        return self._clock

    @property
    def config(self) -> ReplayConfig:
        return self._config

    def subscribe(
        self,
        callback: ReplayCallback,
        *,
        event_type: type[object] | tuple[type[object], ...] | None = None,
    ) -> ReplaySubscription:
        """Subscribe to events after the replay clock has advanced to them."""

        event_types = None
        if event_type is not None:
            event_types = event_type if isinstance(event_type, tuple) else (event_type,)
        token = self._next_token
        self._next_token += 1
        self._subscribers[token] = _Subscriber(callback, event_types)
        return ReplaySubscription(self, token)

    def discard_subscription(self, token: int) -> None:
        self._subscribers.pop(token, None)

    async def replay(
        self,
        events: ReplaySource | None = None,
        *,
        config: ReplayConfig | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        instruments: Sequence[InstrumentMetadata] = (),
        markets: Iterable[Market | str] = (),
        symbols: Iterable[str] = (),
        speed: float | str | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[ReplayEvent]:
        """Replay one source with half-open ``start <= available_at < end``."""

        selected_config = config or self._config
        selected_config = selected_config.with_overrides(
            start=start,
            end=end,
            speed=speed,
            seed=seed,
            markets=markets,
            symbols=symbols,
        )
        source = self._events if events is None else events
        collected = await _collect_source(source)
        filtered = self._filter_events(collected, selected_config, instruments)
        ordered = tuple(
            sorted(filtered, key=lambda event: _event_sort_key(event, selected_config.seed))
        )

        previous = self._prepare_clock(selected_config.start)
        for event in ordered:
            available_at = event_available_at(event)
            validate_event_point_in_time(event, available_at)
            if previous is not None:
                delay = (available_at - previous).total_seconds()
                if delay < 0:
                    raise PointInTimeViolation(
                        f"replay event availability moved backwards: {available_at.isoformat()} "
                        f"after {previous.isoformat()}"
                    )
                if not selected_config.max_speed and delay > 0:
                    multiplier = selected_config.speed_multiplier
                    assert multiplier is not None
                    await self._sleep(delay / multiplier)
            self._clock.advance_to(available_at)
            validate_event_point_in_time(event, self._clock.now())
            await self._notify(event)
            yield event
            previous = available_at

    async def replay_sources(
        self,
        sources: Iterable[ReplaySource],
        **kwargs: Any,
    ) -> AsyncIterator[ReplayEvent]:
        """Merge several finite market/news sources before replaying them."""

        events: list[ReplayEvent] = []
        for source in sources:
            events.extend(await _collect_source(source))
        async for event in self.replay(events, **kwargs):
            yield event

    async def run(
        self,
        events: ReplaySource | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ReplayEvent]:
        """Alias for callers that model replay as a running engine."""

        async for event in self.replay(events, **kwargs):
            yield event

    def _prepare_clock(self, start: datetime | None) -> datetime | None:
        previous = self._clock.current
        if start is not None and (previous is None or previous < start):
            self._clock.advance_to(start)
            previous = start
        return previous

    def _filter_events(
        self,
        events: Iterable[ReplayEvent],
        config: ReplayConfig,
        instruments: Sequence[InstrumentMetadata],
    ) -> list[ReplayEvent]:
        allowed_instruments = {
            (instrument.market.value, instrument.symbol) for instrument in instruments
        }
        return [
            event
            for event in events
            if _matches_filters(event, config, allowed_instruments)
        ]

    async def _notify(self, event: ReplayEvent) -> None:
        for subscriber in tuple(self._subscribers.values()):
            if subscriber.event_types is not None and not isinstance(event, subscriber.event_types):
                continue
            result = subscriber.callback(event)
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)


class ReplayMarketDataAdapter:
    """Expose stored events through historical and streaming contracts."""

    def __init__(
        self,
        store: ParquetEventStore,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        speed: float | str = "max",
        seed: int = 0,
        markets: Iterable[Market | str] = (),
        symbols: Iterable[str] = (),
        clock: ReplayClock | None = None,
        sleep: ReplaySleep | None = None,
        config: ReplayConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or ReplayConfig(
            start=start,
            end=end,
            speed=speed,
            seed=seed,
            markets=frozenset(_normalise_market(value) for value in _values(markets)),
            symbols=frozenset(str(value).strip() for value in _values(symbols)),
        )
        self._engine = ReplayEngine(clock=clock, config=self._config, sleep=sleep)

    @property
    def clock(self) -> ReplayClock:
        return self._engine.clock

    @property
    def config(self) -> ReplayConfig:
        return self._config

    def subscribe(
        self,
        callback: ReplayCallback,
        *,
        event_type: type[object] | tuple[type[object], ...] | None = None,
    ) -> ReplaySubscription:
        return self._engine.subscribe(callback, event_type=event_type)

    async def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        events = self._store.iter_events(start, end)
        config = replace(self._config, start=start, end=end)
        async for event in self._engine.replay(events, config=config, instruments=instruments):
            if isinstance(event, NewsEvent):
                continue
            yield event

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        if self._config.start is None or self._config.end is None:
            raise ValueError("ReplayMarketDataAdapter.stream requires start and end")
        async for event in self.replay(instruments, self._config.start, self._config.end):
            yield event


def merge_replay_events(
    sources: Iterable[Iterable[ReplayEvent]],
    *,
    seed: int = 0,
) -> tuple[ReplayEvent, ...]:
    """Synchronously merge finite sources using the replay ordering contract."""

    events = [event for source in sources for event in source]
    return tuple(sorted(events, key=lambda event: _event_sort_key(event, seed)))


def as_historical_adapter(adapter: ReplayMarketDataAdapter) -> HistoricalDataAdapter:
    """Document the protocol boundary without exposing storage internals."""

    return adapter


def as_market_data_adapter(adapter: ReplayMarketDataAdapter) -> MarketDataAdapter:
    """Document the protocol boundary for pipeline integration."""

    return adapter


def _event_sort_key(event: ReplayEvent, seed: int) -> tuple[datetime, datetime, int, str]:
    tie_breaker = hashlib.sha256(f"{seed}:{event.event_id}".encode()).hexdigest()
    return (
        event_available_at(event),
        event.event_time,
        event.sequence_number if event.sequence_number is not None else 2**63 - 1,
        tie_breaker,
    )


def _matches_filters(
    event: ReplayEvent,
    config: ReplayConfig,
    allowed_instruments: set[tuple[str, str]],
) -> bool:
    available_at = event_available_at(event)
    if config.start is not None and available_at < config.start:
        return False
    if config.end is not None and available_at >= config.end:
        return False
    key = (event.instrument.market.value, event.instrument.symbol)
    if allowed_instruments and key not in allowed_instruments:
        return False
    if config.markets and event.instrument.market not in config.markets:
        return False
    return not config.symbols or event.instrument.symbol in config.symbols


async def _collect_source(source: ReplaySource) -> list[ReplayEvent]:
    if hasattr(source, "__aiter__"):
        async_source = cast(AsyncIterable[ReplayEvent], source)
        return [event async for event in async_source]
    return list(cast(Iterable[ReplayEvent], source))


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _normalise_speed_value(value: float | str) -> float | str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"max", "maximum", "fastest", "inf", "infinite"}:
            return "max"
        if normalized.endswith("x"):
            normalized = normalized[:-1]
        try:
            value = float(normalized)
        except ValueError as exc:
            raise ValueError("speed must be a positive number or 'max'") from exc
    if isinstance(value, bool):
        raise TypeError("speed must be a positive number or 'max'")
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("speed must be a positive number or 'max'") from exc
    if math.isinf(numeric_value):
        if numeric_value > 0:
            return "max"
        raise ValueError("speed must be positive")
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise ValueError("speed must be a positive number or 'max'")
    return numeric_value


def _normalise_market(value: Market | str) -> Market:
    if isinstance(value, Market):
        return value
    try:
        return Market(str(value).strip().upper())
    except ValueError as exc:
        raise ValueError(f"unsupported market filter: {value!r}") from exc


def _values(values: Iterable[Any]) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes)):
        return (values,)
    return tuple(values)


def _validate_bounds(start: datetime | None, end: datetime | None) -> None:
    for name, value in (("start", start), ("end", end)):
        if value is not None:
            _require_aware(value, name)
    if start is not None and end is not None and end < start:
        raise ValueError("end must not be earlier than start")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
