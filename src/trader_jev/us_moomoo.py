"""Read-only moomoo quote adapter for a broad U.S. equity universe."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from pydantic import Field

from trader_jev.models import DomainModel
from trader_jev.moomoo import MoomooClientConfig
from trader_jev.us_equity import (
    USMarketSnapshot,
    USOHLCVBar,
    USScreenRow,
    USUniverseListing,
    as_decimal,
)

US_EASTERN = ZoneInfo("America/New_York")


class USMoomooFailureKind(StrEnum):
    CONNECTION = "CONNECTION"
    QUOTE_PERMISSION = "QUOTE_PERMISSION"
    SUBSCRIPTION_QUOTA = "SUBSCRIPTION_QUOTA"
    RATE_LIMIT = "RATE_LIMIT"
    EMPTY_DATA = "EMPTY_DATA"
    API_ERROR = "API_ERROR"


class USMoomooError(RuntimeError):
    """A classified, fail-closed error from a read-only OpenD quote call."""

    def __init__(self, kind: str, message: str, *, method: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.method = method


class QuoteContext(Protocol):
    """Only quote and market-data methods are exposed by this adapter."""

    def close(self) -> object: ...

    def get_stock_basicinfo(self, market: object, stock_type: object) -> object: ...

    def get_stock_screen(self, request: object) -> object: ...

    def get_market_snapshot(self, code_list: list[str]) -> object: ...

    def query_subscription(self, is_all_conn: bool = True) -> object: ...

    def subscribe(
        self, code_list: list[str], subtype_list: list[object], **kwargs: object
    ) -> object: ...

    def unsubscribe(self, code_list: list[str], subtype_list: list[object]) -> object: ...

    def get_cur_kline(self, code: str, num: int, ktype: object, autype: object) -> object: ...

    def get_history_kl_quota(self, get_detail: bool = False) -> object: ...


@dataclass(frozen=True)
class ScreenFetch:
    rows: tuple[USScreenRow, ...]
    total_count: int
    truncated: bool
    pages: int


class USMoomooConfig(DomainModel):
    """Quota, page, connection and retry settings for the U.S. market adapter."""

    connection_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    screen_requests_per_30_seconds: int = Field(default=10, gt=0, le=10)
    snapshot_requests_per_30_seconds: int = Field(default=60, gt=0, le=60)
    max_screen_page_size: int = Field(default=200, gt=0, le=200)
    max_snapshot_batch_size: int = Field(default=400, gt=0, le=400)
    retry_attempts: int = Field(default=2, ge=0, le=5)
    retry_initial_seconds: float = Field(default=0.5, gt=0, le=10)
    minute_bar_count: int = Field(default=390, gt=1, le=1000)
    minute_bar_type: str = "K_1M"


class _SlidingRateLimiter:
    """Conservative minimum-spacing limiter with injectable time for tests."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = window_seconds / max_requests
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = monotonic()
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                self._sleep(delay)
                now = self._monotonic()
            self._next_allowed = max(now, self._next_allowed) + self.interval


ContextFactory = Callable[[MoomooClientConfig], QuoteContext]
ScreenRequestBuilder = Callable[[Any, int, int], object]


class MoomooUSMarketAdapter:
    """A context-managed quote client; no trade or account context is created."""

    def __init__(
        self,
        connection: MoomooClientConfig | None = None,
        config: USMoomooConfig | None = None,
        *,
        context_factory: ContextFactory | None = None,
        screen_request_builder: ScreenRequestBuilder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.connection = connection or MoomooClientConfig.from_env()
        self.config = config or USMoomooConfig()
        if context_factory is None:

            def context_factory_default(configured: MoomooClientConfig) -> QuoteContext:
                return _default_context_factory(
                    configured,
                    timeout_seconds=self.config.connection_timeout_seconds,
                    monotonic=monotonic,
                    sleep=sleep,
                )

            self._context_factory: ContextFactory = context_factory_default
        else:
            self._context_factory = context_factory
        self._screen_request_builder = screen_request_builder or _build_stock_screen_request
        self._sleep = sleep
        self._screen_limiter = _SlidingRateLimiter(
            self.config.screen_requests_per_30_seconds,
            30.0,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._snapshot_limiter = _SlidingRateLimiter(
            self.config.snapshot_requests_per_30_seconds,
            30.0,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._context: QuoteContext | None = None
        self._subscribed_codes: set[str] = set()
        self.errors: list[USMoomooError] = []
        self.missing_snapshot_codes: tuple[str, ...] = ()

    def __enter__(self) -> MoomooUSMarketAdapter:
        if self._context is not None:
            raise RuntimeError("moomoo market adapter is already open")
        try:
            self._context = self._context_factory(self.connection)
        except USMoomooError:
            raise
        except Exception as exc:
            raise USMoomooError(
                USMoomooFailureKind.CONNECTION,
                "could not connect to moomoo OpenD at "
                f"{self.connection.host}:{self.connection.port}",
            ) from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        context = self._context
        self._context = None
        self._subscribed_codes.clear()
        if context is not None:
            try:
                context.close()
            except Exception:
                self.errors.append(
                    USMoomooError(
                        USMoomooFailureKind.CONNECTION,
                        "could not close moomoo OpenD quote context",
                        method="close",
                    )
                )

    def fetch_universe(self, *, include_etf: bool) -> tuple[USUniverseListing, ...]:
        """Fetch static U.S. stocks and optionally ETFs for the local cache."""

        sdk = self._sdk()
        market = sdk.Market.US
        types = [sdk.SecurityType.STOCK]
        if include_etf:
            types.append(sdk.SecurityType.ETF)
        listings: dict[str, USUniverseListing] = {}
        for security_type in types:
            data = self._call("get_stock_basicinfo", market, security_type)
            for row in _table_rows(data, "get_stock_basicinfo"):
                code = _text(row.get("code"))
                name = _text(row.get("name")) or "unknown"
                if not code:
                    continue
                if not code.startswith("US."):
                    code = f"US.{code}"
                listing_date = _parse_date(row.get("listing_date"))
                listings[code] = USUniverseListing(
                    code=code,
                    name=name,
                    exchange=_text(row.get("exchange_type")) or "UNKNOWN",
                    lot_size=max(0, _integer(row.get("lot_size")) or 1),
                    listing_date=listing_date,
                    delisted=_boolean(row.get("delisting")),
                    security_type=(
                        _text(row.get("stock_type"))
                        or _text(getattr(security_type, "name", None))
                        or str(security_type)
                    ).upper(),
                    stock_id=_text(row.get("stock_id")),
                    raw=_plain_mapping(row),
                )
        if not listings:
            raise USMoomooError(
                USMoomooFailureKind.EMPTY_DATA,
                "get_stock_basicinfo returned no U.S. listings",
                method="get_stock_basicinfo",
            )
        return tuple(listings[code] for code in sorted(listings))

    def screen_us(
        self,
        *,
        min_price_usd: Decimal,
        min_market_cap_usd: Decimal,
        min_avg_turnover_20d_usd: Decimal,
        min_listing_days: int | None,
        max_rows: int = 2000,
    ) -> ScreenFetch:
        """Use Stock Screener V2 for broad filters and page through matched rows."""

        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        page_size = self.config.max_screen_page_size
        rows: list[USScreenRow] = []
        total_count = 0
        truncated = False
        pages = 0
        while len(rows) < max_rows:
            request = self._screen_request_builder(
                {
                    "min_price_usd": min_price_usd,
                    "min_market_cap_usd": min_market_cap_usd,
                    "min_avg_turnover_20d_usd": min_avg_turnover_20d_usd,
                    "min_listing_days": min_listing_days,
                },
                pages * page_size,
                page_size,
            )
            data = self._call(
                "get_stock_screen",
                request,
                limiter=self._screen_limiter,
            )
            parsed = _screen_page(data)
            if pages == 0:
                total_count = parsed[1]
            rows.extend(parse_screen_rows(parsed[2]))
            pages += 1
            if parsed[0] or len(parsed[2]) < page_size:
                break
        if len(rows) >= max_rows and total_count > len(rows):
            rows = rows[:max_rows]
            truncated = True
        if not rows:
            raise USMoomooError(
                USMoomooFailureKind.EMPTY_DATA,
                "Stock Screener V2 returned no U.S. stocks after configured hard filters",
                method="get_stock_screen",
            )
        return ScreenFetch(tuple(rows), total_count, truncated, pages)

    def fetch_snapshots(self, codes: Sequence[str]) -> Mapping[str, USMarketSnapshot]:
        """Fetch snapshot records in at most 400-symbol batches."""

        if not codes:
            return {}
        if len(set(codes)) != len(codes):
            raise ValueError("snapshot codes must be unique")
        snapshots: dict[str, USMarketSnapshot] = {}
        self.missing_snapshot_codes = ()
        for offset in range(0, len(codes), self.config.max_snapshot_batch_size):
            batch = list(codes[offset : offset + self.config.max_snapshot_batch_size])
            data = self._call(
                "get_market_snapshot",
                batch,
                limiter=self._snapshot_limiter,
            )
            raw_rows = _table_rows(data, "get_market_snapshot")
            for row in raw_rows:
                code = _text(row.get("code"))
                if not code:
                    continue
                try:
                    snapshots[code] = parse_snapshot_row(row)
                except (ValueError, TypeError) as exc:
                    error = USMoomooError(
                        USMoomooFailureKind.EMPTY_DATA,
                        f"snapshot for {code} is incomplete or invalid: {exc}",
                        method="get_market_snapshot",
                    )
                    self.errors.append(error)
        missing = tuple(code for code in codes if code not in snapshots)
        self.missing_snapshot_codes = missing
        if not snapshots:
            raise USMoomooError(
                USMoomooFailureKind.EMPTY_DATA,
                "get_market_snapshot returned no usable rows",
                method="get_market_snapshot",
            )
        for code in missing:
            self.errors.append(
                USMoomooError(
                    USMoomooFailureKind.EMPTY_DATA,
                    f"get_market_snapshot omitted {code}",
                    method="get_market_snapshot",
                )
            )
        return snapshots

    def fetch_minute_bars(
        self,
        codes: Sequence[str],
        *,
        count: int | None = None,
    ) -> Mapping[str, tuple[USOHLCVBar, ...]]:
        """Subscribe only to selected symbols; quota failure returns no bars."""

        if not codes:
            return {}
        sdk = self._sdk()
        try:
            subscription = self._call("query_subscription", is_all_conn=True)
        except USMoomooError as exc:
            self.errors.append(
                USMoomooError(
                    USMoomooFailureKind.SUBSCRIPTION_QUOTA,
                    "could not read realtime candlestick subscription quota",
                    method="query_subscription",
                )
            )
            self.errors.append(exc)
            return {}
        remaining = _subscription_remaining(subscription)
        if remaining is None:
            self.errors.append(
                USMoomooError(
                    USMoomooFailureKind.SUBSCRIPTION_QUOTA,
                    "subscription quota state did not include a remaining count",
                    method="query_subscription",
                )
            )
            return {}
        already_subscribed = [code for code in codes if code in self._subscribed_codes]
        to_subscribe = [code for code in codes if code not in self._subscribed_codes]
        unavailable_count = max(0, len(to_subscribe) - max(0, remaining))
        if unavailable_count:
            self.errors.append(
                USMoomooError(
                    USMoomooFailureKind.SUBSCRIPTION_QUOTA,
                    f"subscription quota left {unavailable_count} selected symbols without 1m bars",
                    method="query_subscription",
                )
            )
        selected = already_subscribed + to_subscribe[: max(0, remaining)]
        if not selected:
            self.errors.append(
                USMoomooError(
                    USMoomooFailureKind.SUBSCRIPTION_QUOTA,
                    "no realtime candlestick subscription quota remains",
                    method="query_subscription",
                )
            )
            return {}
        subtype = getattr(sdk.SubType, self.config.minute_bar_type)
        kline_type = getattr(sdk.KLType, self.config.minute_bar_type)
        newly_selected = [code for code in selected if code not in self._subscribed_codes]
        if newly_selected:
            try:
                self._call(
                    "subscribe",
                    newly_selected,
                    [subtype],
                    limiter=self._snapshot_limiter,
                    kwargs={"is_first_push": False, "subscribe_push": False},
                )
            except USMoomooError as exc:
                self.errors.append(exc)
                return {}
            self._subscribed_codes.update(newly_selected)

        bars_by_code: dict[str, tuple[USOHLCVBar, ...]] = {}
        for code in selected:
            try:
                data = self._call(
                    "get_cur_kline",
                    code,
                    count or self.config.minute_bar_count,
                    kline_type,
                    sdk.AuType.QFQ,
                    limiter=self._snapshot_limiter,
                )
                rows = _table_rows(data, "get_cur_kline")
                bars_by_code[code] = tuple(parse_bar_row(row) for row in rows)
            except USMoomooError as exc:
                self.errors.append(exc)
            except (TypeError, ValueError) as exc:
                self.errors.append(
                    USMoomooError(
                        USMoomooFailureKind.EMPTY_DATA,
                        f"candlesticks for {code} are incomplete: {exc}",
                        method="get_cur_kline",
                    )
                )
        return bars_by_code

    def history_kline_quota(self) -> Mapping[str, Any]:
        """Expose historical-candle quota without requesting any history."""

        result: object = self._call("get_history_kl_quota", get_detail=True)
        if isinstance(result, Mapping):
            return dict(cast(Mapping[str, Any], result))
        if isinstance(result, (tuple, list)):
            items = cast(Sequence[object], result)
            if len(items) < 2:
                raise USMoomooError(
                    USMoomooFailureKind.API_ERROR,
                    "get_history_kl_quota returned an invalid response",
                    method="get_history_kl_quota",
                )
            used = _integer(items[0])
            remaining = _integer(items[1])
            detail: object = items[2] if len(items) > 2 else ()
            return {"used_quota": used, "remain_quota": remaining, "detail_list": detail}
        raise USMoomooError(
            USMoomooFailureKind.API_ERROR,
            "get_history_kl_quota returned an invalid response",
            method="get_history_kl_quota",
        )

    def _call(
        self,
        method: str,
        *args: object,
        limiter: _SlidingRateLimiter | None = None,
        kwargs: Mapping[str, object] | None = None,
        **named: object,
    ) -> Any:
        context = self._context
        if context is None:
            raise RuntimeError("moomoo market adapter must be used inside a with block")
        arguments = dict(kwargs or {})
        arguments.update(named)
        for attempt in range(self.config.retry_attempts + 1):
            if limiter is not None:
                limiter.acquire()
            try:
                response: object = getattr(context, method)(*args, **arguments)
            except Exception as exc:
                error = _classify_error(str(exc), method)
                retryable = (
                    error.kind == USMoomooFailureKind.RATE_LIMIT or "timeout" in str(exc).lower()
                )
                if retryable and attempt < self.config.retry_attempts:
                    self._sleep(self.config.retry_initial_seconds * (2**attempt))
                    continue
                self.errors.append(error)
                raise error from exc
            if not isinstance(response, (tuple, list)):
                error = USMoomooError(
                    USMoomooFailureKind.API_ERROR,
                    f"{method} returned an invalid response shape",
                    method=method,
                )
                self.errors.append(error)
                raise error
            response_items = cast(Sequence[object], response)
            if len(response_items) != 2:
                error = USMoomooError(
                    USMoomooFailureKind.API_ERROR,
                    f"{method} returned an invalid response shape",
                    method=method,
                )
                self.errors.append(error)
                raise error
            ret_code, data = response_items
            if ret_code == 0:
                return data
            error = _classify_error(str(data), method)
            if (
                error.kind == USMoomooFailureKind.RATE_LIMIT
                and attempt < self.config.retry_attempts
            ):
                self._sleep(self.config.retry_initial_seconds * (2**attempt))
                continue
            self.errors.append(error)
            raise error
        raise AssertionError("unreachable retry state")

    @staticmethod
    def _sdk() -> Any:
        try:
            return importlib.import_module("moomoo")
        except ImportError as exc:
            raise USMoomooError(
                USMoomooFailureKind.CONNECTION,
                "moomoo-api is unavailable; run `uv sync` before using the adapter",
            ) from exc


def parse_screen_rows(items: Sequence[object]) -> tuple[USScreenRow, ...]:
    rows: list[USScreenRow] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        fields: dict[str, Any] = {}
        item_mapping = cast(Mapping[str, Any], item)
        raw_results = item_mapping.get("results")
        if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
            continue
        for result in cast(Sequence[object], raw_results):
            if not isinstance(result, Mapping):
                continue
            result_mapping = cast(Mapping[str, Any], result)
            prop = result_mapping.get("property")
            if not isinstance(prop, Mapping):
                continue
            property_mapping = cast(Mapping[str, Any], prop)
            property_id = _integer(property_mapping.get("name"))
            days = _integer(property_mapping.get("days"))
            value = _screen_value(result_mapping)
            key = _screen_field_key(property_id, days)
            if key is not None:
                fields[key] = value
        code = _text(fields.get("code") or item_mapping.get("code"))
        if not code:
            continue
        if not code.startswith("US."):
            code = f"US.{code}"
        rows.append(
            USScreenRow(
                code=code,
                name=_text(fields.get("name") or item_mapping.get("name")) or "",
                price=as_decimal(fields.get("price")),
                market_cap_usd=as_decimal(fields.get("market_cap_usd")),
                listed_days=_integer(fields.get("listed_days")),
                volume_ratio=as_decimal(fields.get("volume_ratio")),
                avg_volume_20d=as_decimal(fields.get("avg_volume_20d")),
                avg_turnover_20d_usd=as_decimal(fields.get("avg_turnover_20d_usd")),
                price_change_1d=as_decimal(fields.get("price_change_1d")),
                price_change_5d=as_decimal(fields.get("price_change_5d")),
                amplitude_1d=as_decimal(fields.get("amplitude_1d")),
                high_to_20d_high=as_decimal(fields.get("high_to_20d_high")),
                low_to_20d_low=as_decimal(fields.get("low_to_20d_low")),
                raw=_plain_mapping(fields),
            )
        )
    return tuple(rows)


def parse_snapshot_row(row: Mapping[str, Any]) -> USMarketSnapshot:
    code = _text(row.get("code"))
    update_time = _vendor_time(row.get("update_time") or row.get("data_time"))
    last_price = as_decimal(row.get("last_price") or row.get("price"))
    if not code or update_time is None or last_price is None or last_price <= 0:
        raise ValueError("required code, update_time, or last_price is missing")
    return USMarketSnapshot(
        code=code,
        update_time=update_time,
        last_price=last_price,
        open_price=as_decimal(row.get("open_price")),
        high_price=as_decimal(row.get("high_price")),
        low_price=as_decimal(row.get("low_price")),
        prev_close_price=as_decimal(row.get("prev_close_price") or row.get("last_close_price")),
        volume=as_decimal(row.get("volume")),
        turnover=as_decimal(row.get("turnover")),
        bid_price=as_decimal(row.get("bid_price")),
        ask_price=as_decimal(row.get("ask_price")),
        bid_volume=as_decimal(row.get("bid_vol") or row.get("bid_volume")),
        ask_volume=as_decimal(row.get("ask_vol") or row.get("ask_volume")),
        market_cap_usd=as_decimal(row.get("market_cap")),
        raw=_plain_mapping(row),
    )


def parse_bar_row(row: Mapping[str, Any]) -> USOHLCVBar:
    timestamp = _vendor_time(row.get("time_key") or row.get("time"))
    if timestamp is None:
        raise ValueError("bar timestamp is missing")
    required = {key: as_decimal(row.get(key)) for key in ("open", "high", "low", "close")}
    if any(value is None for value in required.values()):
        raise ValueError("bar OHLC values are missing")
    volume = as_decimal(row.get("volume"))
    turnover = as_decimal(row.get("turnover"))
    return USOHLCVBar(
        timestamp=timestamp,
        open=cast(Decimal, required["open"]),
        high=cast(Decimal, required["high"]),
        low=cast(Decimal, required["low"]),
        close=cast(Decimal, required["close"]),
        volume=volume or Decimal("0"),
        turnover=turnover or Decimal("0"),
    )


def _build_stock_screen_request(filters: Any, page_from: int, page_count: int) -> object:
    sdk = importlib.import_module("moomoo")
    constants = importlib.import_module("moomoo.quote.stock_screen_const")
    request = sdk.StockScreenRequest()
    request.add_simple_field(
        field=constants.SimpleField.MARKET,
        values=[constants.ScrMarket.US],
    )
    request.add_simple_property(
        name=constants.SimpleProperty.PRICE,
        lower=float(filters["min_price_usd"]),
    )
    request.add_simple_property(
        name=constants.SimpleProperty.MARKET_CAP,
        lower=float(filters["min_market_cap_usd"]),
    )
    request.add_cumulative_property(
        name=constants.CumulativeProperty.AVG_TURNOVER,
        days=20,
        lower=float(filters["min_avg_turnover_20d_usd"]),
    )
    if filters["min_listing_days"] is not None:
        request.add_simple_property(
            name=constants.SimpleProperty.LISTED_DAYS,
            lower=int(filters["min_listing_days"]),
        )
    for name in (
        "CODE",
        "NAME",
    ):
        request.add_retrieve_basic(name=getattr(constants.BasicProperty, name))
    for name in (
        "PRICE",
        "MARKET_CAP",
        "LISTED_DAYS",
        "VOLUME_RATIO",
        "OPEN_PRICE",
        "HIGH",
        "LOW",
        "LAST_CLOSE",
    ):
        request.add_retrieve_simple(name=getattr(constants.SimpleProperty, name))
    cumulative_fields = (
        ("PRICE_CHANGE_PCT", 1),
        ("PRICE_CHANGE_PCT", 5),
        ("AMPLITUDE", 1),
        ("AVG_VOLUME", 20),
        ("AVG_TURNOVER", 20),
        ("HIGH_TO_N_DAY_HIGH", 20),
        ("LOW_TO_N_DAY_LOW", 20),
    )
    for name, days in cumulative_fields:
        value = getattr(constants.CumulativeProperty, name)
        request.add_retrieve_cumulative(name=value, days=days)
    request.set_sort(
        direction=constants.ScrSortDir.DESC,
        property_type="cumulative",
        property_params={
            "name": int(constants.CumulativeProperty.AVG_TURNOVER),
            "days": 20,
        },
    )
    request.page_from = page_from
    request.page_count = page_count
    return request


def _default_context_factory(
    config: MoomooClientConfig,
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> QuoteContext:
    """Create an asynchronous OpenQuoteContext with a bounded connect wait."""

    sdk = importlib.import_module("moomoo")
    context = sdk.OpenQuoteContext(
        host=config.host,
        port=config.port,
        is_async_connect=True,
    )
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        status = getattr(context, "status", None)
        if status == "READY":
            return cast(QuoteContext, context)
        if status == "CLOSED":
            break
        sleep(min(0.05, max(0.0, deadline - monotonic())))
    context.close()
    raise USMoomooError(
        USMoomooFailureKind.CONNECTION,
        f"moomoo OpenD did not become ready at {config.host}:{config.port} within "
        f"{timeout_seconds:g} seconds",
    )


def _screen_page(data: object) -> tuple[bool, int, list[object]]:
    if not isinstance(data, (tuple, list)):
        raise USMoomooError(
            USMoomooFailureKind.API_ERROR,
            "get_stock_screen returned an invalid page shape",
            method="get_stock_screen",
        )
    page = cast(Sequence[object], data)
    if len(page) != 3:
        raise USMoomooError(
            USMoomooFailureKind.API_ERROR,
            "get_stock_screen returned an invalid page shape",
            method="get_stock_screen",
        )
    last_page, count, items = page
    if not isinstance(items, list):
        items = list(cast(Sequence[object], items)) if isinstance(items, Sequence) else []
    total_count = _integer(count)
    if total_count is None:
        raise USMoomooError(
            USMoomooFailureKind.API_ERROR,
            "get_stock_screen returned an invalid total count",
            method="get_stock_screen",
        )
    return bool(last_page), total_count, cast(list[object], items)


def _screen_field_key(property_id: int | None, days: int | None) -> str | None:
    basic = {1101: "code", 1102: "name"}
    simple = {
        2201: "price",
        2301: "market_cap_usd",
        2307: "listed_days",
        2217: "volume_ratio",
    }
    cumulative = {
        (3102, 1): "price_change_1d",
        (3102, 5): "price_change_5d",
        (3103, 1): "amplitude_1d",
        (3104, 20): "avg_volume_20d",
        (3105, 20): "avg_turnover_20d_usd",
        (3107, 20): "high_to_20d_high",
        (3108, 20): "low_to_20d_low",
    }
    if property_id is None:
        return None
    basic_name = basic.get(property_id)
    simple_name = simple.get(property_id)
    if days is None:
        return basic_name or simple_name
    return basic_name or simple_name or cumulative.get((property_id, days))


def _screen_value(result: Mapping[str, Any]) -> Any:
    for key in ("sval", "ival", "aval", "dval", "enum_name"):
        value = result.get(key)
        if value not in (None, "", "N/A"):
            return value
    return None


def _table_rows(data: object, method: str) -> tuple[Mapping[str, Any], ...]:
    if isinstance(data, list):
        raw_rows: object = cast(list[object], data)
    else:
        to_dict = getattr(data, "to_dict", None)
        if not callable(to_dict):
            raise USMoomooError(
                USMoomooFailureKind.EMPTY_DATA,
                f"{method} returned a non-tabular response",
                method=method,
            )
        try:
            raw_rows = to_dict(orient="records")
        except Exception as exc:
            raise USMoomooError(
                USMoomooFailureKind.API_ERROR,
                f"could not read {method} response rows",
                method=method,
            ) from exc
    if not isinstance(raw_rows, list):
        raise USMoomooError(
            USMoomooFailureKind.API_ERROR,
            f"{method} did not return a row list",
            method=method,
        )
    rows: list[Mapping[str, Any]] = []
    for row in cast(list[object], raw_rows):
        if isinstance(row, Mapping):
            rows.append(cast(Mapping[str, Any], row))
    return tuple(rows)


def _subscription_remaining(data: object) -> int | None:
    if isinstance(data, Mapping):
        return _integer(cast(Mapping[str, Any], data).get("remain"))
    try:
        rows = _table_rows(data, "query_subscription")
    except USMoomooError:
        return None
    if not rows:
        return None
    return _integer(rows[0].get("remain"))


def _plain_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _plain_value(value) for key, value in values.items()}


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _plain_mapping(cast(Mapping[str, Any], value))
    if isinstance(value, (tuple, list)):
        return [_plain_value(item) for item in cast(Sequence[object], value)]
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return _plain_value(scalar())
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _vendor_time(value: Any) -> datetime | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=US_EASTERN)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if value is None or value == "" or value == "N/A":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value in (None, "", "N/A"):
        return None
    return str(value).strip() or None


def _integer(value: Any) -> int | None:
    number = as_decimal(value)
    return int(number) if number is not None else None


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _classify_error(message: str, method: str) -> USMoomooError:
    normalized = message.lower()
    if any(
        text in normalized for text in ("permission", "no authority", "quote right", "no right")
    ):
        kind = USMoomooFailureKind.QUOTE_PERMISSION
    elif any(
        text in normalized for text in ("subscription quota", "remain quota", "insufficient quota")
    ):
        kind = USMoomooFailureKind.SUBSCRIPTION_QUOTA
    elif any(
        text in normalized
        for text in ("rate limit", "too frequent", "frequency", "too many request")
    ):
        kind = USMoomooFailureKind.RATE_LIMIT
    elif any(text in normalized for text in ("empty", "no data", "not found", "omitted")):
        kind = USMoomooFailureKind.EMPTY_DATA
    elif any(text in normalized for text in ("connect", "connection", "timeout", "opend")):
        kind = USMoomooFailureKind.CONNECTION
    else:
        kind = USMoomooFailureKind.API_ERROR
    return USMoomooError(kind, f"{method} failed: {message}", method=method)


__all__ = [
    "MoomooUSMarketAdapter",
    "QuoteContext",
    "ScreenFetch",
    "USMoomooConfig",
    "USMoomooError",
    "USMoomooFailureKind",
    "parse_bar_row",
    "parse_screen_rows",
    "parse_snapshot_row",
]
