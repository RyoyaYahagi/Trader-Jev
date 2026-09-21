"""Append-only Parquet storage and DuckDB queries for normalized market events."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb
import polars as pl

from trader_jev.models import (
    InstrumentMetadata,
    MarketEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)


class EventStorageError(RuntimeError):
    """Raised when a raw event cannot be persisted or reconstructed."""


_ROW_SCHEMA: dict[str, Any] = {
    "event_id": pl.Utf8,
    "event_time": pl.Utf8,
    "received_at": pl.Utf8,
    "source": pl.Utf8,
    "schema_version": pl.Utf8,
    "sequence_number": pl.Int64,
    "trading_status": pl.Utf8,
    "payload_json": pl.Utf8,
    "out_of_order": pl.Boolean,
}

_EVENT_TYPES: dict[type[MarketEvent], str] = {
    QuoteEvent: "quote",
    TradeEvent: "trade",
    OrderBookEvent: "order_book",
}
_EVENT_MODELS: dict[str, type[MarketEvent]] = {
    event_type: event_model for event_model, event_type in _EVENT_TYPES.items()
}


class ParquetEventStore:
    """Persist normalized events as immutable partitioned Parquet parts.

    A new uniquely named part file is written for every append.  Existing parts
    are never opened in write mode, which keeps the raw event layer append-only
    and makes an interrupted write recoverable by ignoring temporary files.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, event: MarketEvent, *, out_of_order: bool = False) -> Path:
        """Atomically append one event and return its Parquet part path."""

        self._validate_event_type(event)
        partition = self._partition_path(event)
        row = self._row(event, out_of_order=out_of_order)
        return self._write_rows(partition, [row])

    def append_batch(
        self,
        events: Sequence[MarketEvent],
        *,
        out_of_order: Sequence[bool] | None = None,
    ) -> tuple[Path, ...]:
        """Append a batch, grouping rows by their partition."""

        if not events:
            return ()
        flags = out_of_order or (False,) * len(events)
        if len(flags) != len(events):
            raise ValueError("out_of_order must have one flag per event")

        grouped: dict[Path, list[dict[str, object]]] = {}
        for event, is_out_of_order in zip(events, flags, strict=True):
            self._validate_event_type(event)
            grouped.setdefault(self._partition_path(event), []).append(
                self._row(event, out_of_order=is_out_of_order)
            )
        return tuple(self._write_rows(partition, rows) for partition, rows in grouped.items())

    def query(self, sql: str) -> pl.DataFrame:
        """Run DuckDB SQL against the ``raw_events`` Parquet view.

        The view includes Hive partition columns: ``date``, ``market``,
        ``symbol``, and ``event_type``.  Queries are intentionally read-only from
        this API; the connection is closed immediately after materialization.
        """

        connection = duckdb.connect(database=":memory:")
        try:
            files = self._parquet_files()
            if files:
                glob_path = (self.root / "**" / "*.parquet").as_posix().replace("'", "''")
                connection.execute(
                    "CREATE VIEW raw_events AS "
                    f"SELECT * FROM read_parquet('{glob_path}', hive_partitioning=true)"
                )
            else:
                connection.execute(
                    "CREATE VIEW raw_events AS SELECT "
                    "CAST(NULL AS VARCHAR) AS event_id, "
                    "CAST(NULL AS VARCHAR) AS event_time, "
                    "CAST(NULL AS VARCHAR) AS received_at, "
                    "CAST(NULL AS VARCHAR) AS source, "
                    "CAST(NULL AS VARCHAR) AS schema_version, "
                    "CAST(NULL AS BIGINT) AS sequence_number, "
                    "CAST(NULL AS VARCHAR) AS trading_status, "
                    "CAST(NULL AS VARCHAR) AS payload_json, "
                    "CAST(NULL AS BOOLEAN) AS out_of_order, "
                    "CAST(NULL AS VARCHAR) AS date, "
                    "CAST(NULL AS VARCHAR) AS market, "
                    "CAST(NULL AS VARCHAR) AS symbol, "
                    "CAST(NULL AS VARCHAR) AS event_type "
                    "WHERE FALSE"
                )
            result = connection.execute(sql)
            columns = [str(column[0]) for column in result.description]
            return pl.DataFrame(result.fetchall(), schema=columns, orient="row")
        finally:
            connection.close()

    def iter_events(
        self,
        start: datetime,
        end: datetime,
        instruments: Sequence[InstrumentMetadata] = (),
    ) -> tuple[MarketEvent, ...]:
        """Reconstruct events in deterministic local-receive order.

        The interval is half-open: ``start <= received_at < end``.  Filtering on
        receive time preserves replay causality even when exchange timestamps are
        out of order.
        """

        self._require_aware(start, "start")
        self._require_aware(end, "end")
        if end < start:
            raise ValueError("end must not be earlier than start")

        allowed = {(instrument.market.value, instrument.symbol) for instrument in instruments}
        rows = self.query(
            "SELECT * FROM raw_events ORDER BY received_at ASC, event_time ASC, event_id ASC"
        ).to_dicts()
        selected: list[tuple[datetime, datetime, int, str, MarketEvent]] = []
        for row in rows:
            received_at = self._parse_datetime(row["received_at"])
            if not (start <= received_at < end):
                continue
            key = (str(row["market"]), str(row["symbol"]))
            if allowed and key not in allowed:
                continue
            event = self._deserialize(row)
            selected.append(
                (
                    received_at,
                    self._parse_datetime(row["event_time"]),
                    int(row["sequence_number"])
                    if row["sequence_number"] is not None
                    else 2**63 - 1,
                    str(row["event_id"]),
                    event,
                )
            )
        selected.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        return tuple(item[4] for item in selected)

    def _write_rows(self, partition: Path, rows: Sequence[dict[str, object]]) -> Path:
        partition.mkdir(parents=True, exist_ok=True)
        part_id = os.urandom(16).hex()
        final_path = partition / f"part-{part_id}.parquet"
        temporary_path = partition / f".part-{part_id}.tmp.parquet"
        frame = pl.DataFrame(rows, schema=_ROW_SCHEMA, strict=False)
        try:
            frame.write_parquet(temporary_path, compression="zstd", statistics=True)
            os.replace(temporary_path, final_path)
        except Exception as exc:
            if temporary_path.exists():
                temporary_path.unlink()
            raise EventStorageError(f"could not append Parquet event part: {exc}") from exc
        return final_path

    @staticmethod
    def _validate_event_type(event: MarketEvent) -> None:
        if type(event) not in _EVENT_TYPES:
            raise EventStorageError(f"unsupported market event type: {type(event)!r}")

    @staticmethod
    def _row(event: MarketEvent, *, out_of_order: bool) -> dict[str, object]:
        return {
            "event_id": str(event.event_id),
            "event_time": event.event_time.isoformat(),
            "received_at": event.received_at.isoformat(),
            "source": event.source,
            "schema_version": event.schema_version,
            "sequence_number": event.sequence_number,
            "trading_status": event.trading_status,
            "payload_json": event.model_dump_json(),
            "out_of_order": out_of_order,
        }

    def _partition_path(self, event: MarketEvent) -> Path:
        event_type = _EVENT_TYPES[type(event)]
        return (
            self.root
            / f"date={event.event_time.date().isoformat()}"
            / f"market={event.instrument.market.value}"
            / f"symbol={quote(event.instrument.symbol, safe='')}"
            / f"event_type={event_type}"
        )

    def _parquet_files(self) -> list[Path]:
        return sorted(self.root.rglob("*.parquet"))

    @staticmethod
    def _deserialize(row: dict[str, object]) -> MarketEvent:
        event_type = str(row["event_type"])
        model = _EVENT_MODELS.get(event_type)
        if model is None:
            raise EventStorageError(f"unknown stored event type: {event_type}")
        try:
            return model.model_validate_json(str(row["payload_json"]))
        except Exception as exc:
            raise EventStorageError(f"could not reconstruct {event_type} event") from exc

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise EventStorageError(f"stored timestamp is not text: {value!r}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventStorageError(f"invalid stored timestamp: {value!r}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise EventStorageError(f"stored timestamp is not timezone-aware: {value!r}")
        return parsed

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
