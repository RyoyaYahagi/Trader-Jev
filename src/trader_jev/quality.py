"""Historical market-data quality checks independent of any data vendor."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trader_jev.models import (
    BarEvent,
    InstrumentMetadata,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)

_KNOWN_EVENT_TYPES = (BarEvent, OrderBookEvent, QuoteEvent, TradeEvent)


@dataclass(frozen=True)
class DataQualityReport:
    """Observable quality findings for one historical sequence."""

    rows_seen: int
    valid_rows: int
    invalid_rows: int
    duplicate_events: int
    out_of_order_events: int
    missing_intervals: int
    errors: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not (
            self.invalid_rows
            or self.duplicate_events
            or self.out_of_order_events
            or self.missing_intervals
            or self.errors
        )


def validate_historical_events(
    events: Iterable[object],
    *,
    instruments: Sequence[InstrumentMetadata] = (),
    expected_interval: timedelta | None = None,
) -> DataQualityReport:
    """Validate schema, timestamps, duplicates, order, and regular gaps.

    The input order is intentionally preserved.  Callers can validate a file's
    source order first and sort the valid events separately for replay.
    """

    if expected_interval is not None and expected_interval <= timedelta(0):
        raise ValueError("expected_interval must be positive")

    allowed = {(instrument.market.value, instrument.symbol) for instrument in instruments}
    rows_seen = 0
    valid_rows = 0
    invalid_rows = 0
    duplicate_events = 0
    out_of_order_events = 0
    missing_intervals = 0
    errors: list[str] = []
    seen_ids: set[str] = set()
    latest_event_time: dict[tuple[str, str], datetime] = {}

    for row_number, event in enumerate(events, start=1):
        rows_seen += 1
        if not isinstance(event, _KNOWN_EVENT_TYPES):
            invalid_rows += 1
            errors.append(f"row {row_number}: unsupported event model")
            continue

        key = (event.instrument.market.value, event.instrument.symbol)
        if allowed and key not in allowed:
            invalid_rows += 1
            errors.append(f"row {row_number}: instrument {key[0]}:{key[1]} is not allowed")
            continue
        if event.event_time.tzinfo is None or event.event_time.utcoffset() is None:
            invalid_rows += 1
            errors.append(f"row {row_number}: event_time is not timezone-aware")
            continue
        if event.received_at.tzinfo is None or event.received_at.utcoffset() is None:
            invalid_rows += 1
            errors.append(f"row {row_number}: received_at is not timezone-aware")
            continue

        event_id = str(event.event_id)
        if event_id in seen_ids:
            duplicate_events += 1
        seen_ids.add(event_id)

        previous = latest_event_time.get(key)
        if previous is not None:
            delta = event.event_time - previous
            if delta < timedelta(0):
                out_of_order_events += 1
            elif expected_interval is not None and delta > expected_interval:
                missing_intervals += max(1, delta // expected_interval - 1)
        if previous is None or event.event_time > previous:
            latest_event_time[key] = event.event_time
        valid_rows += 1

    return DataQualityReport(
        rows_seen=rows_seen,
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        duplicate_events=duplicate_events,
        out_of_order_events=out_of_order_events,
        missing_intervals=missing_intervals,
        errors=tuple(errors),
    )
