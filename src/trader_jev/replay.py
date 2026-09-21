"""Deterministic replay adapter backed by the raw Parquet event store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from trader_jev.interfaces import HistoricalDataAdapter, MarketDataAdapter
from trader_jev.models import InstrumentMetadata, MarketEvent
from trader_jev.storage import ParquetEventStore


class ReplayMarketDataAdapter:
    """Expose stored events through both historical and streaming contracts."""

    def __init__(
        self,
        store: ParquetEventStore,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        self._store = store
        self._start = start
        self._end = end

    async def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        for event in self._store.iter_events(start, end, instruments):
            yield event

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        if self._start is None or self._end is None:
            raise ValueError("ReplayMarketDataAdapter.stream requires start and end")
        async for event in self.replay(instruments, self._start, self._end):
            yield event


def as_historical_adapter(adapter: ReplayMarketDataAdapter) -> HistoricalDataAdapter:
    """Document the protocol boundary without exposing storage internals."""

    return adapter


def as_market_data_adapter(adapter: ReplayMarketDataAdapter) -> MarketDataAdapter:
    """Document the protocol boundary for pipeline integration."""

    return adapter
