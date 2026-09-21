"""Batch ingestion for historical data sources."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import Field

from trader_jev.adapters import FileMarketDataAdapter
from trader_jev.interfaces import HistoricalDataAdapter, RawEventStore
from trader_jev.models import DomainModel, InstrumentMetadata, MarketEvent
from trader_jev.quality import DataQualityReport, validate_historical_events


class HistoricalIngestionError(RuntimeError):
    """Raised when validated historical data cannot be persisted."""


class HistoricalIngestionConfig(DomainModel):
    """Explicit quality policy for one historical ingestion run."""

    expected_interval_seconds: int | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class HistoricalIngestionResult:
    """Counts and quality findings from one append-only ingestion run."""

    source_events: int
    stored_events: int
    duplicate_events: int
    out_of_order_events: int
    rejected_rows: int
    report: DataQualityReport


class HistoricalDataIngestor:
    """Validate, deduplicate, annotate, and append historical events."""

    def __init__(
        self,
        store: RawEventStore,
        *,
        config: HistoricalIngestionConfig | None = None,
    ) -> None:
        self._store = store
        self._config = config or HistoricalIngestionConfig()

    async def ingest(
        self,
        adapter: HistoricalDataAdapter,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> HistoricalIngestionResult:
        """Ingest one half-open historical range without realtime behavior."""

        events = await self._read_events(adapter, instruments, start, end)
        expected_interval = (
            timedelta(seconds=self._config.expected_interval_seconds)
            if self._config.expected_interval_seconds is not None
            else None
        )
        report = validate_historical_events(
            events,
            instruments=instruments,
            expected_interval=expected_interval,
        )

        existing_ids = self._existing_event_ids()
        seen_ids: set[str] = set(existing_ids)
        latest_event_time: dict[tuple[str, str], datetime] = {}
        stored_events = 0
        duplicate_events = 0
        out_of_order_events = 0
        allowed = {(instrument.market.value, instrument.symbol) for instrument in instruments}

        for event in events:
            key = (event.instrument.market.value, event.instrument.symbol)
            event_id = str(event.event_id)
            if allowed and key not in allowed:
                continue
            if event_id in seen_ids:
                duplicate_events += 1
                continue
            previous = latest_event_time.get(key)
            out_of_order = previous is not None and event.event_time < previous
            if out_of_order:
                out_of_order_events += 1
            try:
                self._store.append(event, out_of_order=out_of_order)
            except Exception as exc:
                raise HistoricalIngestionError(
                    f"could not persist historical event {event.event_id}"
                ) from exc
            seen_ids.add(event_id)
            if previous is None or event.event_time > previous:
                latest_event_time[key] = event.event_time
            stored_events += 1

        rejected_rows = 0
        file_stats = getattr(adapter, "stats", None)
        if file_stats is not None:
            rejected_rows = int(getattr(file_stats, "rejected_rows", 0))
        return HistoricalIngestionResult(
            source_events=len(events),
            stored_events=stored_events,
            duplicate_events=duplicate_events,
            out_of_order_events=out_of_order_events,
            rejected_rows=rejected_rows,
            report=report,
        )

    @staticmethod
    async def _read_events(
        adapter: HistoricalDataAdapter,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> tuple[MarketEvent, ...]:
        if isinstance(adapter, FileMarketDataAdapter):
            return tuple(
                event
                for event in adapter.read_events(instruments)
                if start <= event.received_at < end
            )
        return tuple(await _collect(adapter.replay(instruments, start, end)))

    def _existing_event_ids(self) -> set[str]:
        event_ids = getattr(self._store, "event_ids", None)
        if event_ids is None:
            return set()
        return {str(event_id) for event_id in event_ids()}


async def _collect(events: AsyncIterator[MarketEvent]) -> list[MarketEvent]:
    return [event async for event in events]
