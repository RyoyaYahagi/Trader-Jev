"""Historical, file, and deterministic synthetic market data adapters.

The current milestone deliberately has no broker trade/account SDK.  The
read-only realtime Moomoo adapter lives in ``trader_jev.moomoo``; every adapter
in this module reads an existing dataset or creates deterministic fixtures, then
emits only market-neutral core events.
"""

from __future__ import annotations

import csv
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from trader_jev.interfaces import HistoricalDataAdapter, MarketDataAdapter
from trader_jev.models import (
    BarEvent,
    InstrumentMetadata,
    MarketEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)


class AdapterNormalizationError(ValueError):
    """Raised when a historical record cannot be converted to a core event."""


def _empty_strings() -> list[str]:
    return []


@dataclass
class FileReadStats:
    """Counters and row-level diagnostics from one file read."""

    total_rows: int = 0
    accepted_rows: int = 0
    rejected_rows: int = 0
    errors: list[str] = field(default_factory=_empty_strings)

    @property
    def healthy(self) -> bool:
        return self.rejected_rows == 0


class FileMarketDataAdapter:
    """Read normalized JSONL/NDJSON or CSV market data without a broker API.

    JSON records may be serialized core events, records with an ``event_type``
    field, or raw rows containing a symbol and event fields.  CSV rows use the
    same field names; ``bids`` and ``asks`` may contain JSON arrays.  When a
    historical row has no availability timestamp, ``received_at`` is set to
    ``event_time`` explicitly so replay remains deterministic.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        skip_corrupt_rows: bool = True,
        source_name: str = "file",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        self.path = Path(path)
        self.skip_corrupt_rows = skip_corrupt_rows
        self.source_name = source_name
        self._start = start
        self._end = end
        self._stats = FileReadStats()
        _validate_range(start, end)
        if not source_name.strip():
            raise ValueError("source_name must not be empty")

    @property
    def stats(self) -> FileReadStats:
        """Return diagnostics for the most recent ``read_events`` call."""

        return self._stats

    def read_events(self, instruments: Sequence[InstrumentMetadata]) -> tuple[MarketEvent, ...]:
        """Read rows in source order, preserving the order for quality checks."""

        self._stats = FileReadStats()
        if not self.path.is_file():
            raise AdapterNormalizationError(f"historical data file does not exist: {self.path}")
        try:
            if self.path.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
                rows = self._read_json_rows(instruments)
            elif self.path.suffix.lower() == ".csv":
                rows = self._read_csv_rows(instruments)
            else:
                raise AdapterNormalizationError(
                    f"unsupported historical data format: {self.path.suffix or '<none>'}"
                )
            return tuple(rows)
        except OSError as exc:
            raise AdapterNormalizationError(
                f"could not read historical data file: {self.path}"
            ) from exc

    async def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        """Yield available events in deterministic receive-time order."""

        _validate_range(start, end)
        events = sorted(self.read_events(instruments), key=_event_sort_key)
        for event in events:
            if start <= event.received_at < end:
                yield event

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        """Expose a configured file range through the pipeline adapter contract."""

        if self._start is None or self._end is None:
            raise ValueError("FileMarketDataAdapter.stream requires start and end")
        async for event in self.replay(instruments, self._start, self._end):
            yield event

    def _read_json_rows(self, instruments: Sequence[InstrumentMetadata]) -> Iterator[MarketEvent]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                self._stats.total_rows += 1
                try:
                    record = json.loads(line)
                    if not isinstance(record, Mapping):
                        raise AdapterNormalizationError("row must be a JSON object")
                    yield self._event_from_record(cast(Mapping[str, Any], record), instruments)
                except (AdapterNormalizationError, TypeError, ValueError, ValidationError) as exc:
                    self._reject_row(line_number, exc)

    def _read_csv_rows(self, instruments: Sequence[InstrumentMetadata]) -> Iterator[MarketEvent]:
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise AdapterNormalizationError("CSV historical data has no header")
            for row in reader:
                line_number = reader.line_num
                self._stats.total_rows += 1
                try:
                    record: dict[str, Any] = {
                        key: value for key, value in row.items() if key is not None and value != ""
                    }
                    for field_name in ("instrument", "payload", "payload_json", "bids", "asks"):
                        value = record.get(field_name)
                        if isinstance(value, str) and value:
                            record[field_name] = json.loads(value)
                    yield self._event_from_record(record, instruments)
                except (AdapterNormalizationError, TypeError, ValueError, ValidationError) as exc:
                    self._reject_row(line_number, exc)

    def _reject_row(self, line_number: int, error: Exception) -> None:
        message = f"{self.path}:{line_number}: {error}"
        self._stats.rejected_rows += 1
        self._stats.errors.append(message)
        if not self.skip_corrupt_rows:
            raise AdapterNormalizationError(message) from error

    def _event_from_record(
        self,
        record: Mapping[str, Any],
        instruments: Sequence[InstrumentMetadata],
    ) -> MarketEvent:
        outer = dict(record)
        event_type = _event_type(outer)
        payload_value = outer.get("payload_json", outer.get("payload"))
        if payload_value is None:
            payload: dict[str, Any] = dict(outer)
        elif isinstance(payload_value, Mapping):
            payload = dict(cast(Mapping[str, Any], payload_value))
        elif isinstance(payload_value, str):
            try:
                decoded = json.loads(payload_value)
            except json.JSONDecodeError as exc:
                raise AdapterNormalizationError("payload is not valid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise AdapterNormalizationError("payload must be a JSON object")
            payload = dict(cast(Mapping[str, Any], decoded))
        else:
            raise AdapterNormalizationError("payload must be a mapping or JSON object")

        event_type = event_type or _event_type(payload)
        if event_type is None:
            raise AdapterNormalizationError("could not infer historical event_type")

        instrument = _resolve_instrument(outer, payload, instruments)
        clean_payload = dict(payload)
        for key in (
            "event_type",
            "type",
            "EventType",
            "symbol",
            "Symbol",
            "market",
            "Market",
            "date",
            "payload",
            "payload_json",
        ):
            clean_payload.pop(key, None)
        clean_payload["instrument"] = instrument
        _copy_alias(clean_payload, "event_time", "timestamp", "time", "exchange_time")
        _copy_alias(clean_payload, "received_at", "available_at", "available_time")
        if "event_time" not in clean_payload:
            raise AdapterNormalizationError("event_time/timestamp is required")
        clean_payload.setdefault("received_at", clean_payload["event_time"])
        clean_payload.setdefault("source", self.source_name)
        clean_payload.setdefault("schema_version", "1.0")
        if "event_id" not in clean_payload or clean_payload["event_id"] in (None, ""):
            clean_payload["event_id"] = str(
                uuid5(
                    NAMESPACE_URL,
                    canonical_record_json({"event_type": event_type, **clean_payload}),
                )
            )

        try:
            event = _validate_event(event_type, clean_payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise AdapterNormalizationError(
                f"invalid {event_type} event for {instrument.symbol}: {exc}"
            ) from exc
        self._stats.accepted_rows += 1
        return event


class SyntheticMarketDataAdapter:
    """Generate deterministic quote events for tests and dry-run examples."""

    def __init__(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: timedelta = timedelta(minutes=1),
        seed: int = 0,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("interval must be positive")
        self._start = start
        self._end = end
        self.interval = interval
        self.seed = seed
        _validate_range(start, end)

    async def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        _validate_range(start, end)
        ordered_instruments = tuple(sorted(instruments, key=_instrument_sort_key))
        current = start
        step = 0
        while current < end:
            for instrument in ordered_instruments:
                yield self._quote(instrument, current, step)
            current += self.interval
            step += 1

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        if self._start is None or self._end is None:
            raise ValueError("SyntheticMarketDataAdapter.stream requires start and end")
        async for event in self.replay(instruments, self._start, self._end):
            yield event

    def _quote(
        self,
        instrument: InstrumentMetadata,
        event_time: datetime,
        step: int,
    ) -> QuoteEvent:
        symbol_offset = sum(ord(character) for character in instrument.symbol) % 17
        mid = Decimal("100") + Decimal(symbol_offset) / Decimal("10")
        mid += Decimal(step % 20) * Decimal("0.05") + Decimal(self.seed % 11)
        spread = max(instrument.tick_size, Decimal("0.02"))
        event_id = uuid5(
            NAMESPACE_URL,
            f"synthetic:{self.seed}:{instrument.market.value}:{instrument.symbol}:{event_time.isoformat()}",
        )
        return QuoteEvent(
            event_id=event_id,
            instrument=instrument,
            event_time=event_time,
            received_at=event_time,
            source=f"synthetic:{self.seed}",
            sequence_number=step,
            bid=mid - spread / Decimal("2"),
            ask=mid + spread / Decimal("2"),
            bid_size=Decimal(100 + (step % 5) * 10),
            ask_size=Decimal(110 + (step % 5) * 10),
        )


def _event_type(record: Mapping[str, Any]) -> str | None:
    raw = record.get("event_type", record.get("type", record.get("EventType")))
    if raw is not None:
        normalized = str(raw).lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "quote": "quote",
            "best_quote": "quote",
            "trade": "trade",
            "tick": "trade",
            "execution": "trade",
            "order_book": "order_book",
            "orderbook": "order_book",
            "book": "order_book",
            "depth": "order_book",
            "l2": "order_book",
            "bar": "bar",
            "ohlcv": "bar",
            "candle": "bar",
        }
        if normalized not in aliases:
            raise AdapterNormalizationError(f"unsupported event_type: {raw!r}")
        return aliases[normalized]
    if {"open", "high", "low", "close"}.issubset(record):
        return "bar"
    if {"bid", "ask"}.issubset(record):
        return "quote"
    if {"price", "size"}.issubset(record):
        return "trade"
    if "bids" in record or "asks" in record:
        return "order_book"
    return None


def _resolve_instrument(
    outer: Mapping[str, Any],
    payload: Mapping[str, Any],
    instruments: Sequence[InstrumentMetadata],
) -> InstrumentMetadata:
    raw_instrument = payload.get("instrument", outer.get("instrument"))
    if raw_instrument is not None:
        if isinstance(raw_instrument, InstrumentMetadata):
            candidate = raw_instrument
        elif isinstance(raw_instrument, Mapping):
            candidate = InstrumentMetadata.model_validate(raw_instrument)
        else:
            raise AdapterNormalizationError("instrument must be an object")
        matches = [
            item for item in instruments if _instrument_key(item) == _instrument_key(candidate)
        ]
        if instruments and not matches:
            raise AdapterNormalizationError(
                f"instrument {candidate.market.value}:{candidate.symbol} is not in the requested "
                "universe"
            )
        return matches[0] if matches else candidate

    raw_symbol = payload.get(
        "symbol", payload.get("Symbol", outer.get("symbol", outer.get("Symbol")))
    )
    raw_market = payload.get(
        "market", payload.get("Market", outer.get("market", outer.get("Market")))
    )
    candidates = list(instruments)
    if raw_symbol is not None:
        candidates = [item for item in candidates if item.symbol == str(raw_symbol)]
    if raw_market is not None:
        market_value = getattr(raw_market, "value", raw_market)
        candidates = [item for item in candidates if item.market.value == str(market_value)]
    if len(candidates) != 1:
        raise AdapterNormalizationError(
            "a unique instrument is required when the row has no instrument object"
        )
    return candidates[0]


def _validate_event(event_type: str, payload: Mapping[str, Any]) -> MarketEvent:
    if event_type == "quote":
        return QuoteEvent.model_validate(payload)
    if event_type == "trade":
        return TradeEvent.model_validate(payload)
    if event_type == "order_book":
        return OrderBookEvent.model_validate(payload)
    if event_type == "bar":
        return BarEvent.model_validate(payload)
    raise AdapterNormalizationError(f"unsupported event_type: {event_type}")


def _copy_alias(payload: dict[str, Any], target: str, *aliases: str) -> None:
    if target in payload:
        return
    for alias in aliases:
        if alias in payload:
            payload[target] = payload[alias]
            return


def canonical_record_json(record: Mapping[str, Any]) -> str:
    """Return a stable representation for deterministic file-event IDs."""

    try:
        return json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AdapterNormalizationError("historical record is not JSON-serializable") from exc


def _instrument_key(instrument: InstrumentMetadata) -> tuple[str, str]:
    return instrument.market.value, instrument.symbol


def _instrument_sort_key(instrument: InstrumentMetadata) -> tuple[str, str]:
    return _instrument_key(instrument)


def _event_sort_key(event: MarketEvent) -> tuple[datetime, datetime, int, str]:
    return (
        event.received_at,
        event.event_time,
        event.sequence_number if event.sequence_number is not None else 2**63 - 1,
        str(event.event_id),
    )


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    for name, value in (("start", start), ("end", end)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must be timezone-aware")
    if start is not None and end is not None and end < start:
        raise ValueError("end must not be earlier than start")


def as_historical_adapter(adapter: HistoricalDataAdapter) -> HistoricalDataAdapter:
    """Make the intended historical adapter boundary explicit for callers."""

    return adapter


def as_market_data_adapter(adapter: MarketDataAdapter) -> MarketDataAdapter:
    """Make the pipeline adapter boundary explicit for callers."""

    return adapter
