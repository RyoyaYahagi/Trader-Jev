"""Reliable collection loop around the normalized MarketDataAdapter boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import Field

from trader_jev.clock import SystemClock
from trader_jev.interfaces import Clock, MarketDataAdapter, RawEventStore
from trader_jev.models import DomainModel, InstrumentMetadata, MarketEvent


class MarketDataCollectionError(RuntimeError):
    """Raised after configured reconnect attempts are exhausted."""


class HeartbeatTimeout(TimeoutError):
    """Raised when a source stops producing events within the heartbeat window."""


class CollectionConfig(DomainModel):
    heartbeat_timeout_seconds: float = Field(default=30.0, gt=0)
    backoff_initial_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_reconnects: int = Field(default=3, ge=0)


@dataclass
class CollectionStats:
    started_at: datetime
    ended_at: datetime | None = None
    received_events: int = 0
    stored_events: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    invalid_events: int = 0
    reconnects: int = 0
    heartbeat_timeouts: int = 0
    errors: int = 0


class MarketDataCollector:
    """Collect a session while preserving raw events and detecting feed faults."""

    def __init__(
        self,
        store: RawEventStore,
        *,
        config: CollectionConfig | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._config = config or CollectionConfig()
        self._clock = clock or SystemClock()
        self._logger = logger or logging.getLogger("trader_jev.collector")

    async def collect(
        self,
        adapter: MarketDataAdapter,
        instruments: Sequence[InstrumentMetadata],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> CollectionStats:
        """Run until the source ends, a stop is requested, or retries are exhausted."""

        stats = CollectionStats(started_at=self._clock.now())
        allowed_keys = {(instrument.market.value, instrument.symbol) for instrument in instruments}
        seen_event_ids: set[UUID] = set()
        latest_event_time: dict[tuple[str, str], datetime] = {}
        reconnects = 0
        backoff = self._config.backoff_initial_seconds

        try:
            while not self._is_stopped(stop_event):
                try:
                    stream = adapter.stream(instruments)
                    iterator = stream.__aiter__()
                    while not self._is_stopped(stop_event):
                        event = await self._next_event(iterator, stop_event)
                        if event is None:
                            break

                        stats.received_events += 1
                        key = (event.instrument.market.value, event.instrument.symbol)
                        if key not in allowed_keys:
                            stats.invalid_events += 1
                            self._logger.warning(
                                "market_event_for_unknown_instrument",
                                extra={"market": key[0], "symbol": key[1]},
                            )
                            continue
                        if event.event_id in seen_event_ids:
                            stats.duplicate_events += 1
                            self._logger.warning(
                                "duplicate_market_event",
                                extra={"event_id": str(event.event_id), "symbol": key[1]},
                            )
                            continue

                        previous_event_time = latest_event_time.get(key)
                        out_of_order = (
                            previous_event_time is not None
                            and event.event_time < previous_event_time
                        )
                        if out_of_order:
                            stats.out_of_order_events += 1
                            self._logger.warning(
                                "out_of_order_market_event",
                                extra={
                                    "event_id": str(event.event_id),
                                    "market": key[0],
                                    "symbol": key[1],
                                },
                            )
                        try:
                            self._store.append(event, out_of_order=out_of_order)
                        except Exception as exc:
                            raise MarketDataCollectionError(
                                f"could not persist market event {event.event_id}"
                            ) from exc
                        seen_event_ids.add(event.event_id)
                        if previous_event_time is None or event.event_time > previous_event_time:
                            latest_event_time[key] = event.event_time
                        stats.stored_events += 1
                    await self._close_iterator(iterator)
                    break
                except HeartbeatTimeout:
                    stats.heartbeat_timeouts += 1
                    stats.errors += 1
                    await self._close_iterator(locals().get("iterator"))
                    reconnects = await self._reconnect_or_raise(
                        reconnects,
                        backoff,
                        stats,
                        stop_event,
                        "heartbeat timeout",
                    )
                except MarketDataCollectionError:
                    raise
                except (ConnectionError, OSError) as exc:
                    stats.errors += 1
                    await self._close_iterator(locals().get("iterator"))
                    reconnects = await self._reconnect_or_raise(
                        reconnects,
                        backoff,
                        stats,
                        stop_event,
                        str(exc),
                    )
                except Exception as exc:
                    stats.errors += 1
                    await self._close_iterator(locals().get("iterator"))
                    reconnects = await self._reconnect_or_raise(
                        reconnects,
                        backoff,
                        stats,
                        stop_event,
                        f"unexpected adapter error: {type(exc).__name__}",
                    )
                backoff = min(
                    self._config.backoff_max_seconds,
                    backoff * self._config.backoff_multiplier,
                )
        finally:
            stats.ended_at = self._clock.now()
        return stats

    async def _reconnect_or_raise(
        self,
        reconnects: int,
        backoff: float,
        stats: CollectionStats,
        stop_event: asyncio.Event | None,
        reason: str,
    ) -> int:
        if self._is_stopped(stop_event):
            return reconnects
        if reconnects >= self._config.max_reconnects:
            raise MarketDataCollectionError(
                f"market data stream failed after {reconnects} reconnects: {reason}"
            )
        reconnects += 1
        stats.reconnects = reconnects
        self._logger.warning(
            "market_data_reconnect",
            extra={"attempt": reconnects, "backoff_seconds": backoff, "reason": reason},
        )
        await self._wait_or_stop(backoff, stop_event)
        return reconnects

    async def _next_event(
        self,
        iterator: AsyncIterator[MarketEvent],
        stop_event: asyncio.Event | None,
    ) -> MarketEvent | None:
        next_task = asyncio.create_task(self._get_next(iterator))
        stop_task: asyncio.Task[bool] | None = None
        try:
            tasks: set[asyncio.Task[object]] = {next_task}  # type: ignore[assignment]
            if stop_event is not None:
                stop_task = asyncio.create_task(stop_event.wait())
                tasks.add(stop_task)  # type: ignore[arg-type]
            done, _ = await asyncio.wait(
                tasks,
                timeout=self._config.heartbeat_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                next_task.cancel()
                await asyncio.gather(next_task, return_exceptions=True)
                raise HeartbeatTimeout
            if stop_task is not None and stop_task in done:
                next_task.cancel()
                await asyncio.gather(next_task, return_exceptions=True)
                return None
            try:
                return next_task.result()
            except StopAsyncIteration:
                return None
        finally:
            if stop_task is not None:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)

    @staticmethod
    async def _close_iterator(iterator: object) -> None:
        if iterator is None:
            return
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()

    @staticmethod
    async def _get_next(iterator: AsyncIterator[MarketEvent]) -> MarketEvent:
        return await iterator.__anext__()

    @staticmethod
    def _is_stopped(stop_event: asyncio.Event | None) -> bool:
        return stop_event is not None and stop_event.is_set()

    @staticmethod
    async def _wait_or_stop(delay: float, stop_event: asyncio.Event | None) -> None:
        if stop_event is None:
            await asyncio.sleep(delay)
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
