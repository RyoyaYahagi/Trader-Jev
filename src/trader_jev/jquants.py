"""J-Quants API v2 historical minute-bar adapter.

The HTTP and vendor-specific response fields stay in this module.  The rest of
the application receives only the market-neutral ``BarEvent`` model.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic, sleep
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator

from trader_jev.interfaces import HistoricalDataAdapter, MarketDataAdapter
from trader_jev.models import BarEvent, DomainModel, InstrumentMetadata, MarketEvent


class JQuantsApiError(RuntimeError):
    """Raised when J-Quants cannot return a usable response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JQuantsClientConfig(DomainModel):
    """Safe connection and rate-limit settings for J-Quants API v2."""

    base_url: str = Field(default="https://api.jquants.com/v2", min_length=1)
    api_key: SecretStr = Field(repr=False)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_response_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=1.0, ge=0, le=60)
    requests_per_minute: int = Field(default=5, gt=0, le=10_000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain user credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        return value.rstrip("/")


class JQuantsMinuteBarAdapter(MarketDataAdapter, HistoricalDataAdapter):
    """Fetch J-Quants v2 one-minute bars and expose core historical events."""

    endpoint_path = "/equities/bars/minute"
    interval_seconds = 60

    def __init__(
        self,
        config: JQuantsClientConfig,
        *,
        source_name: str = "jquants-v2-minute",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        if not source_name.strip():
            raise ValueError("source_name must not be empty")
        _validate_range(start, end)
        self.config = config
        self.source_name = source_name
        self._start = start
        self._end = end
        self._last_request_at = 0.0

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> JQuantsMinuteBarAdapter:
        """Build an adapter from ``JQUANTS_*`` environment values."""

        values: Mapping[str, str] = os.environ if env is None else env
        api_key = values.get("JQUANTS_API_KEY", "").strip()
        if not api_key:
            raise ValueError("JQUANTS_API_KEY is required")
        return cls(
            JQuantsClientConfig(
                base_url=values.get("JQUANTS_BASE_URL", "https://api.jquants.com/v2"),
                api_key=SecretStr(api_key),
                timeout_seconds=_float_env(values, "JQUANTS_TIMEOUT_SECONDS", 30.0),
                max_response_bytes=_int_env(values, "JQUANTS_MAX_RESPONSE_BYTES", 16 * 1024 * 1024),
                max_retries=_int_env(values, "JQUANTS_MAX_RETRIES", 3),
                retry_backoff_seconds=_float_env(values, "JQUANTS_RETRY_BACKOFF_SECONDS", 1.0),
                requests_per_minute=_int_env(values, "JQUANTS_REQUESTS_PER_MINUTE", 5),
            ),
            start=start,
            end=end,
        )

    def fetch_events(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> tuple[BarEvent, ...]:
        """Fetch and normalize a half-open historical range."""

        _validate_range(start, end)
        if not instruments:
            raise ValueError("at least one instrument is required")

        events: list[BarEvent] = []
        for instrument in sorted(instruments, key=_instrument_key):
            local_zone = ZoneInfo(instrument.timezone)
            local_start = start.astimezone(local_zone).date()
            local_end = end.astimezone(local_zone).date()
            rows = self._fetch_rows(
                code=instrument.symbol,
                from_date=local_start,
                to_date=local_end,
            )
            for row in rows:
                event = self._normalize_row(row, instrument)
                if start <= event.received_at < end:
                    events.append(event)

        events.sort(key=lambda event: (event.received_at, event.event_time, str(event.event_id)))
        return tuple(events)

    async def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        """Fetch historical bars off the event loop and yield them by availability."""

        events = await asyncio.to_thread(self.fetch_events, instruments, start, end)
        for event in events:
            yield event

    async def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        """Stream the range supplied at construction time."""

        if self._start is None or self._end is None:
            raise ValueError("JQuantsMinuteBarAdapter.stream requires start and end")
        async for event in self.replay(instruments, self._start, self._end):
            yield event

    def _fetch_rows(
        self,
        *,
        code: str,
        from_date: date,
        to_date: date,
    ) -> tuple[Mapping[str, Any], ...]:
        query: dict[str, str] = {
            "code": code,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
        }
        rows: list[Mapping[str, Any]] = []
        seen_pagination_keys: set[str] = set()
        while True:
            payload = self._get_json(query)
            raw_rows_value = payload.get("data", [])
            if not isinstance(raw_rows_value, list):
                raise JQuantsApiError("J-Quants response field 'data' must be an array")
            raw_rows = cast(list[object], raw_rows_value)
            for raw_row in raw_rows:
                if not isinstance(raw_row, Mapping):
                    raise JQuantsApiError("J-Quants response contains a non-object row")
                rows.append(cast(Mapping[str, Any], raw_row))

            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                break
            pagination_key = str(pagination_key)
            if pagination_key in seen_pagination_keys:
                raise JQuantsApiError("J-Quants pagination key repeated")
            seen_pagination_keys.add(pagination_key)
            query["pagination_key"] = pagination_key
        return tuple(rows)

    def _get_json(self, query: Mapping[str, str]) -> Mapping[str, Any]:
        url = f"{self.config.base_url}{self.endpoint_path}?{urlencode(query)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Trader-Jev/jquants-v2",
                "x-api-key": self.config.api_key.get_secret_value(),
            },
            method="GET",
        )
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    body = response.read(self.config.max_response_bytes + 1)
                if len(body) > self.config.max_response_bytes:
                    raise JQuantsApiError("J-Quants response exceeded the configured size limit")
                decoded = json.loads(body.decode("utf-8"))
                if not isinstance(decoded, Mapping):
                    raise JQuantsApiError("J-Quants response must be a JSON object")
                return cast(Mapping[str, Any], decoded)
            except HTTPError as exc:
                last_error = exc
                if not _retryable_status(exc.code) or attempt >= self.config.max_retries:
                    detail = _http_error_detail(exc, self.config.api_key.get_secret_value())
                    raise JQuantsApiError(
                        f"J-Quants HTTP request failed ({exc.code}): {detail}",
                        status_code=exc.code,
                    ) from exc
            except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise JQuantsApiError("J-Quants request failed") from exc
            if attempt < self.config.max_retries:
                sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise JQuantsApiError("J-Quants request failed") from last_error

    def _wait_for_rate_limit(self) -> None:
        interval = 60.0 / self.config.requests_per_minute
        elapsed = monotonic() - self._last_request_at
        if elapsed < interval:
            sleep(interval - elapsed)
        self._last_request_at = monotonic()

    def _normalize_row(self, row: Mapping[str, Any], instrument: InstrumentMetadata) -> BarEvent:
        event_time = _bar_datetime(row, instrument.timezone)
        code = str(_row_value(row, "Code", "code") or instrument.symbol)
        raw_date = str(_row_value(row, "Date", "date") or "")
        raw_time = str(_row_value(row, "Time", "time") or "")
        open_price = _required_decimal(row, ("O", "Open", "open"), "open")
        high_price = _required_decimal(row, ("H", "High", "high"), "high")
        low_price = _required_decimal(row, ("L", "Low", "low"), "low")
        close_price = _required_decimal(row, ("C", "Close", "close"), "close")
        volume = _optional_decimal(row, ("Vo", "Volume", "volume"), Decimal("0"))
        event_id = uuid5(
            NAMESPACE_URL,
            json.dumps(
                {
                    "source": self.source_name,
                    "instrument": instrument.symbol,
                    "code": code,
                    "date": raw_date,
                    "time": raw_time,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                },
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ),
        )
        return BarEvent(
            event_id=event_id,
            instrument=instrument,
            event_time=event_time,
            received_at=event_time + timedelta(seconds=self.interval_seconds),
            source=self.source_name,
            schema_version="jquants-v2",
            sequence_number=event_time.hour * 60 + event_time.minute,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            interval_seconds=self.interval_seconds,
        )


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _required_decimal(row: Mapping[str, Any], names: tuple[str, ...], field_name: str) -> Decimal:
    value = _row_value(row, *names)
    if value is None:
        raise JQuantsApiError(f"J-Quants minute-bar row is missing {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JQuantsApiError(f"J-Quants minute-bar {field_name} is not numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise JQuantsApiError(f"J-Quants minute-bar {field_name} must be positive")
    return parsed


def _optional_decimal(row: Mapping[str, Any], names: tuple[str, ...], default: Decimal) -> Decimal:
    value = _row_value(row, *names)
    if value is None:
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise JQuantsApiError("J-Quants minute-bar volume is not numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise JQuantsApiError("J-Quants minute-bar volume must not be negative")
    return parsed


def _bar_datetime(row: Mapping[str, Any], timezone: str) -> datetime:
    raw_date = _row_value(row, "Date", "date")
    raw_time = _row_value(row, "Time", "time")
    if raw_date is None or raw_time is None:
        raise JQuantsApiError("J-Quants minute-bar row requires Date and Time")
    try:
        parsed_date = _parse_date(str(raw_date))
        parsed_time = time.fromisoformat(str(raw_time).strip())
        return datetime.combine(parsed_date, parsed_time, tzinfo=ZoneInfo(timezone))
    except (ValueError, TypeError) as exc:
        raise JQuantsApiError("J-Quants minute-bar Date/Time is invalid") from exc


def _parse_date(value: str) -> date:
    normalized = value.strip().replace("/", "-")
    if len(normalized) == 8 and normalized.isdigit():
        return datetime.strptime(normalized, "%Y%m%d").date()
    return date.fromisoformat(normalized)


def _retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _http_error_detail(error: HTTPError, secret: str) -> str:
    try:
        body = error.read(512).decode("utf-8", errors="replace").strip()
    except OSError:
        return "response body unavailable"
    if secret:
        body = body.replace(secret, "[redacted]")
    return body or "response body unavailable"


def _instrument_key(instrument: InstrumentMetadata) -> tuple[str, str]:
    return instrument.market.value, instrument.symbol


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    for name, value in (("start", start), ("end", end)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must be timezone-aware")
    if start is not None and end is not None and end < start:
        raise ValueError("end must not be earlier than start")


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


__all__ = ["JQuantsApiError", "JQuantsClientConfig", "JQuantsMinuteBarAdapter"]
