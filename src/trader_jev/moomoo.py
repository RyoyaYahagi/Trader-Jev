"""Read-only real-time quote adapter backed by the moomoo OpenD gateway.

The moomoo SDK is intentionally imported lazily inside the default context
factory.  Core models therefore receive only normalized :class:`QuoteEvent`
values, while the vendor DataFrame and context objects remain inside this
adapter.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from trader_jev.clock import SystemClock
from trader_jev.interfaces import Clock, MarketDataAdapter
from trader_jev.models import DomainModel, InstrumentMetadata, QuoteEvent


class MoomooApiError(RuntimeError):
    """Raised when OpenD or a moomoo quote response cannot be used safely."""


class MoomooQuoteContext(Protocol):
    """Small vendor-neutral surface used by the read-only quote adapter."""

    def get_market_snapshot(self, code_list: list[str]) -> object:
        """Return the moomoo status code and snapshot table."""
        ...

    def close(self) -> object:
        """Close the OpenD connection."""
        ...


class MoomooClientConfig(DomainModel):
    """Connection and polling settings for a local OpenD gateway."""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=11111, gt=0, le=65535)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    request_batch_size: int = Field(default=400, gt=0, le=400)
    source_name: str = Field(default="moomoo:market-snapshot", min_length=1)

    @field_validator("host", "source_name")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> MoomooClientConfig:
        """Build configuration from non-secret ``MOOMOO_OPEND_*`` variables."""

        values: Mapping[str, str] = os.environ if env is None else env
        return cls(
            host=values.get("MOOMOO_OPEND_HOST", "127.0.0.1"),
            port=_int_env(values, "MOOMOO_OPEND_PORT", 11111),
            poll_interval_seconds=_float_env(
                values,
                "MOOMOO_POLL_INTERVAL_SECONDS",
                1.0,
            ),
            request_batch_size=_int_env(values, "MOOMOO_REQUEST_BATCH_SIZE", 400),
            source_name=values.get("MOOMOO_SOURCE_NAME", "moomoo:market-snapshot"),
        )


MoomooContextFactory = Callable[[MoomooClientConfig], MoomooQuoteContext]


class MoomooMarketDataAdapter(MarketDataAdapter):
    """Poll read-only moomoo snapshots and emit normalized quote events.

    The adapter uses ``get_market_snapshot`` rather than any trade interface.
    A snapshot contains the latest price and best bid/ask values.  Polling is
    used here so the adapter has a simple asynchronous iterator contract and
    does not expose moomoo callback or DataFrame types to the core.
    """

    def __init__(
        self,
        config: MoomooClientConfig | None = None,
        *,
        code_map: Mapping[str, str] | None = None,
        context_factory: MoomooContextFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config or MoomooClientConfig()
        self._code_map = dict(code_map or {})
        self._context_factory = context_factory or _default_context_factory
        self._clock = clock or SystemClock()
        self._sequence = 0

    async def fetch_once(
        self,
        instruments: Sequence[InstrumentMetadata],
    ) -> tuple[QuoteEvent, ...]:
        """Fetch one snapshot batch and close the OpenD connection afterward."""

        normalized = _validate_instruments(instruments)
        context = await self._open_context()
        try:
            received_at = _utc_now(self._clock.now())
            return self._fetch_events(context, normalized, received_at)
        finally:
            await self._close_context(context)

    async def stream(
        self,
        instruments: Sequence[InstrumentMetadata],
    ) -> AsyncGenerator[QuoteEvent, None]:
        """Yield changed quotes until the consumer closes or cancels the stream."""

        normalized = _validate_instruments(instruments)
        context = await self._open_context()
        previous: dict[str, tuple[object, ...]] = {}
        try:
            while True:
                received_at = _utc_now(self._clock.now())
                events = self._fetch_events(context, normalized, received_at)
                for event in events:
                    key = _instrument_key(event.instrument)
                    observation = (
                        event.event_time,
                        event.bid,
                        event.ask,
                        event.bid_size,
                        event.ask_size,
                        event.trading_status,
                    )
                    if previous.get(key) == observation:
                        continue
                    previous[key] = observation
                    yield event
                await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            await self._close_context(context)

    async def _open_context(self) -> MoomooQuoteContext:
        try:
            return self._context_factory(self.config)
        except MoomooApiError:
            raise
        except Exception as exc:
            raise MoomooApiError(
                f"could not connect to moomoo OpenD at {self.config.host}:{self.config.port}"
            ) from exc

    async def _close_context(self, context: MoomooQuoteContext) -> None:
        try:
            context.close()
        except Exception as exc:
            raise MoomooApiError("could not close the moomoo OpenD connection") from exc

    def _fetch_events(
        self,
        context: MoomooQuoteContext,
        instruments: tuple[InstrumentMetadata, ...],
        received_at: datetime,
    ) -> tuple[QuoteEvent, ...]:
        code_by_instrument = {
            _instrument_key(instrument): self._vendor_code(instrument) for instrument in instruments
        }
        instrument_by_code: dict[str, InstrumentMetadata] = {}
        for instrument in instruments:
            code = code_by_instrument[_instrument_key(instrument)]
            if code in instrument_by_code:
                raise ValueError(f"multiple instruments map to moomoo code {code!r}")
            instrument_by_code[code] = instrument

        rows_by_code: dict[str, Mapping[str, Any]] = {}
        codes = tuple(instrument_by_code)
        for start in range(0, len(codes), self.config.request_batch_size):
            batch = list(codes[start : start + self.config.request_batch_size])
            table = self._request_snapshot(context, batch)
            for row in _table_rows(table):
                raw_code = row.get("code")
                if not isinstance(raw_code, str) or not raw_code.strip():
                    raise MoomooApiError("moomoo snapshot row has no usable code")
                code = raw_code.strip()
                if code not in instrument_by_code:
                    raise MoomooApiError(f"moomoo returned an unexpected code: {code}")
                if code in rows_by_code:
                    raise MoomooApiError(f"moomoo returned duplicate code: {code}")
                rows_by_code[code] = row

        missing = [code for code in codes if code not in rows_by_code]
        if missing:
            raise MoomooApiError("moomoo snapshot omitted requested codes: " + ", ".join(missing))

        return tuple(
            self._normalize_row(
                rows_by_code[code],
                instrument_by_code[code],
                received_at,
            )
            for code in codes
        )

    def _request_snapshot(
        self,
        context: MoomooQuoteContext,
        codes: list[str],
    ) -> object:
        try:
            response = context.get_market_snapshot(codes)
        except Exception as exc:
            raise MoomooApiError("moomoo market snapshot request failed") from exc
        if not isinstance(response, (tuple, list)):
            raise MoomooApiError("moomoo market snapshot response has an invalid shape")
        response_items = cast(tuple[object, ...] | list[object], response)
        if len(response_items) != 2:
            raise MoomooApiError("moomoo market snapshot response has an invalid shape")
        ret_code = response_items[0]
        table = response_items[1]
        if ret_code != 0:
            raise MoomooApiError(f"moomoo market snapshot failed: {table}")
        return table

    def _normalize_row(
        self,
        row: Mapping[str, Any],
        instrument: InstrumentMetadata,
        received_at: datetime,
    ) -> QuoteEvent:
        try:
            event = QuoteEvent(
                event_id=uuid4(),
                instrument=instrument,
                event_time=_vendor_timestamp(
                    _row_value(row, "update_time", "data_time"),
                    instrument.timezone,
                ),
                received_at=received_at,
                source=self.config.source_name,
                schema_version="moomoo-quote-v1",
                sequence_number=self._next_sequence(),
                trading_status=_optional_text(row.get("sec_status")),
                bid=_required_decimal(row, ("bid_price",), "bid_price", positive=True),
                ask=_required_decimal(row, ("ask_price",), "ask_price", positive=True),
                bid_size=_required_decimal(row, ("bid_vol", "bid_volume"), "bid_vol"),
                ask_size=_required_decimal(row, ("ask_vol", "ask_volume"), "ask_vol"),
            )
        except MoomooApiError:
            raise
        except ValueError as exc:
            raise MoomooApiError(
                f"invalid moomoo quote for {instrument.market.value}:{instrument.symbol}"
            ) from exc
        return event

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _vendor_code(self, instrument: InstrumentMetadata) -> str:
        key = _instrument_key(instrument)
        explicit = self._code_map.get(key, self._code_map.get(instrument.symbol))
        if explicit is not None:
            code = explicit.strip()
            if not code:
                raise ValueError(f"empty moomoo code mapping for {key}")
            return code
        if "." in instrument.symbol:
            return instrument.symbol
        return f"{instrument.market.value}.{instrument.symbol}"


def _default_context_factory(config: MoomooClientConfig) -> MoomooQuoteContext:
    """Create the vendor context only when a caller actually requests data."""

    try:
        moomoo = importlib.import_module("moomoo")
    except ImportError as exc:
        raise MoomooApiError(
            "moomoo-api is not importable; run `uv sync` before using the adapter"
        ) from exc
    try:
        return cast(
            MoomooQuoteContext,
            moomoo.OpenQuoteContext(host=config.host, port=config.port),
        )
    except Exception as exc:
        raise MoomooApiError(
            f"could not create moomoo OpenD context at {config.host}:{config.port}"
        ) from exc


def _validate_instruments(
    instruments: Sequence[InstrumentMetadata],
) -> tuple[InstrumentMetadata, ...]:
    if not instruments:
        raise ValueError("at least one instrument is required")
    result = tuple(instruments)
    keys = [_instrument_key(instrument) for instrument in result]
    if len(set(keys)) != len(keys):
        raise ValueError("instruments must not contain duplicates")
    return result


def _table_rows(table: object) -> tuple[Mapping[str, Any], ...]:
    to_dict = getattr(table, "to_dict", None)
    if not callable(to_dict):
        raise MoomooApiError("moomoo snapshot is not a tabular response")
    try:
        raw_rows = to_dict(orient="records")
    except Exception as exc:
        raise MoomooApiError("could not read the moomoo snapshot table") from exc
    if not isinstance(raw_rows, list):
        raise MoomooApiError("moomoo snapshot rows must be a list")
    raw_rows = cast(list[object], raw_rows)
    rows: list[Mapping[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise MoomooApiError("moomoo snapshot contains a non-object row")
        rows.append(cast(Mapping[str, Any], raw_row))
    return tuple(rows)


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "N/A"):
            return value
    return None


def _required_decimal(
    row: Mapping[str, Any],
    names: tuple[str, ...],
    field_name: str,
    *,
    positive: bool = False,
) -> Decimal:
    value = _row_value(row, *names)
    if value is None:
        raise MoomooApiError(f"moomoo snapshot row is missing {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MoomooApiError(f"moomoo snapshot {field_name} is not numeric") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        comparison = "positive" if positive else "non-negative"
        raise MoomooApiError(f"moomoo snapshot {field_name} must be {comparison}")
    return parsed


def _vendor_timestamp(value: Any, timezone: str) -> datetime:
    if value is None:
        raise MoomooApiError("moomoo snapshot row is missing update_time")
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise MoomooApiError("moomoo snapshot update_time is invalid") from exc


def _utc_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MoomooMarketDataAdapter clock must return an aware datetime")
    return value.astimezone(UTC)


def _optional_text(value: Any) -> str | None:
    if value in (None, "", "N/A"):
        return None
    return str(value)


def _instrument_key(instrument: InstrumentMetadata) -> str:
    return f"{instrument.market.value}:{instrument.symbol}"


def _float_env(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


__all__ = [
    "MoomooApiError",
    "MoomooClientConfig",
    "MoomooContextFactory",
    "MoomooMarketDataAdapter",
    "MoomooQuoteContext",
]
