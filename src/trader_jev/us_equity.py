"""Domain models and deterministic logic for U.S. equity paper research."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from trader_jev.models import DomainModel

US_EASTERN = ZoneInfo("America/New_York")
US_EXCHANGES = frozenset({"US_NYSE", "US_NASDAQ", "US_AMEX"})
US_OTC_EXCHANGES = frozenset({"US_PINK", "US_OTC", "US_OTCBB", "US_OTC_BB"})


class USUniverseListing(DomainModel):
    """Cached static listing facts returned by moomoo's quote API."""

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    lot_size: int = Field(default=1, ge=0)
    listing_date: date | None = None
    delisted: bool = False
    security_type: str = "STOCK"
    stock_id: str | None = None
    raw: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.code.partition(".")[2] or self.code


class USScreenRow(DomainModel):
    """One stock-screener row, normalized without retaining SDK objects."""

    code: str = Field(min_length=1)
    name: str = ""
    price: Decimal | None = None
    market_cap_usd: Decimal | None = None
    listed_days: int | None = Field(default=None, ge=0)
    volume_ratio: Decimal | None = None
    avg_volume_20d: Decimal | None = None
    avg_turnover_20d_usd: Decimal | None = None
    price_change_1d: Decimal | None = None
    price_change_5d: Decimal | None = None
    amplitude_1d: Decimal | None = None
    high_to_20d_high: Decimal | None = None
    low_to_20d_low: Decimal | None = None
    raw: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_finite_metrics(self) -> USScreenRow:
        for name in (
            "price",
            "market_cap_usd",
            "volume_ratio",
            "avg_volume_20d",
            "avg_turnover_20d_usd",
            "price_change_1d",
            "price_change_5d",
            "amplitude_1d",
            "high_to_20d_high",
            "low_to_20d_low",
        ):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be finite")
        return self


class USScreenCacheSnapshot(DomainModel):
    """Persisted screener rows available to bootstrap a later Paper process."""

    run_id: str
    refreshed_at: datetime
    rows: tuple[USScreenRow, ...]
    total_count: int = Field(ge=0)
    truncated: bool
    pages: int = Field(ge=0)


class USOHLCVBar(DomainModel):
    """Normalized one-minute bar used only for candidate feature generation."""

    timestamp: datetime
    open: Decimal = Field(gt=Decimal("0"))
    high: Decimal = Field(gt=Decimal("0"))
    low: Decimal = Field(gt=Decimal("0"))
    close: Decimal = Field(gt=Decimal("0"))
    volume: Decimal = Field(ge=Decimal("0"))
    turnover: Decimal = Field(ge=Decimal("0"))

    @model_validator(mode="after")
    def validate_bar(self) -> USOHLCVBar:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar high and low do not contain the open and close")
        if self.low > self.high:
            raise ValueError("bar low cannot exceed bar high")
        return self


class USMarketSnapshot(DomainModel):
    """One point-in-time market snapshot with optional quote-side values."""

    code: str = Field(min_length=1)
    update_time: datetime
    last_price: Decimal = Field(gt=Decimal("0"))
    open_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    high_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    low_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    prev_close_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    volume: Decimal | None = Field(default=None, ge=Decimal("0"))
    turnover: Decimal | None = Field(default=None, ge=Decimal("0"))
    bid_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    ask_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    bid_volume: Decimal | None = Field(default=None, ge=Decimal("0"))
    ask_volume: Decimal | None = Field(default=None, ge=Decimal("0"))
    market_cap_usd: Decimal | None = Field(default=None, gt=Decimal("0"))
    raw: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_snapshot(self) -> USMarketSnapshot:
        if self.update_time.tzinfo is None or self.update_time.utcoffset() is None:
            raise ValueError("snapshot update_time must be timezone-aware")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        ):
            raise ValueError("snapshot bid_price cannot exceed ask_price")
        return self


class USQuantFeatures(DomainModel):
    """Compact numeric state; unavailable inputs remain null."""

    code: str
    as_of: datetime
    last_price: Decimal
    return_1m: Decimal | None = None
    return_5m: Decimal | None = None
    return_15m: Decimal | None = None
    return_60m: Decimal | None = None
    return_1d: Decimal | None = None
    return_5d: Decimal | None = None
    volume_ratio: Decimal | None = None
    current_volume: Decimal | None = None
    avg_volume_20d: Decimal | None = None
    avg_turnover_20d_usd: Decimal | None = None
    intraday_amplitude: Decimal | None = None
    realized_volatility: Decimal | None = None
    distance_from_vwap: Decimal | None = None
    distance_from_day_high: Decimal | None = None
    distance_from_day_low: Decimal | None = None
    distance_from_20d_high: Decimal | None = None
    distance_from_20d_low: Decimal | None = None
    short_term_trend_strength: Decimal | None = None
    spread_bps: Decimal | None = None
    liquidity_usd: Decimal | None = None
    unavailable: tuple[str, ...] = ()


class USRankedCandidate(DomainModel):
    """Candidate after lane union and quant ranking."""

    code: str
    name: str
    quant_score: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    lane_scores: Mapping[str, Decimal] = Field(default_factory=dict)
    screening_lanes: tuple[str, ...] = ()
    quant_rank: int = Field(ge=1)
    features: USQuantFeatures | None = None


class USJevOpinion(DomainModel):
    """Parsed Jev judgements while retaining each answer's distribution."""

    setup_type: str
    setup_probabilities: Mapping[str, Decimal] = Field(default_factory=dict)
    setup_confidence: Decimal | None = None
    trend_quality: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    trend_probabilities: Mapping[str, Decimal] = Field(default_factory=dict)
    trend_confidence: Decimal | None = None
    continuation_quality: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    continuation_probabilities: Mapping[str, Decimal] = Field(default_factory=dict)
    continuation_confidence: Decimal | None = None
    abnormal_probability: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    trade_worthy_probability: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    jev_score: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    min_confidence: Decimal | None = None
    raw_response: Mapping[str, Any]


class USDecisionKind(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    NO_TRADE = "NO TRADE"


class USPaperPosition(DomainModel):
    symbol: str
    quantity: int = Field(gt=0)
    avg_entry_price_usd: Decimal = Field(gt=Decimal("0"))
    market_price_usd: Decimal = Field(gt=Decimal("0"))
    market_value_usd: Decimal = Field(ge=Decimal("0"))
    unrealized_pnl_usd: Decimal
    realized_pnl_usd: Decimal = Decimal("0")
    entered_at: datetime


class USPaperPortfolio(DomainModel):
    """Persisted JPY reserve and USD PaperBroker ledger state."""

    portfolio_id: str = "us-equities-100k"
    initial_cash_jpy: Decimal = Field(default=Decimal("100000"), ge=Decimal("0"))
    cash_jpy: Decimal = Field(ge=Decimal("0"))
    cash_usd: Decimal = Field(ge=Decimal("0"))
    initial_usd_jpy_rate: Decimal = Field(gt=Decimal("0"))
    usd_jpy_rate: Decimal = Field(gt=Decimal("0"))
    fx_as_of: str | None = None
    fx_source: str | None = None
    positions: Mapping[str, int] = Field(default_factory=dict)
    average_prices_usd: Mapping[str, Decimal] = Field(default_factory=dict)
    market_prices_usd: Mapping[str, Decimal] = Field(default_factory=dict)
    position_entry_times: Mapping[str, datetime] = Field(default_factory=dict)
    realized_pnl_usd: Decimal = Decimal("0")
    unrealized_pnl_usd: Decimal = Decimal("0")
    total_fees_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    peak_equity_jpy: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    drawdown_jpy: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    updated_at: datetime

    @property
    def positions_value_usd(self) -> Decimal:
        return sum(
            (
                self.market_prices_usd.get(
                    symbol, self.average_prices_usd.get(symbol, Decimal("0"))
                )
                * quantity
                for symbol, quantity in self.positions.items()
            ),
            Decimal("0"),
        )

    @property
    def total_equity_jpy(self) -> Decimal:
        return self.cash_jpy + (self.cash_usd + self.positions_value_usd) * self.usd_jpy_rate

    @property
    def total_equity_usd(self) -> Decimal:
        return self.cash_jpy / self.usd_jpy_rate + self.cash_usd + self.positions_value_usd

    @classmethod
    def initial(
        cls,
        *,
        portfolio_id: str = "us-equities-100k",
        initial_cash_jpy: Decimal,
        usd_jpy_rate: Decimal,
        cash_reserve_pct: Decimal,
        at: datetime,
        fx_as_of: str | None = None,
        fx_source: str | None = None,
    ) -> USPaperPortfolio:
        if not Decimal("0") <= cash_reserve_pct < Decimal("1"):
            raise ValueError("cash_reserve_pct must be in [0, 1)")
        cash_jpy = initial_cash_jpy * cash_reserve_pct
        cash_usd = (initial_cash_jpy - cash_jpy) / usd_jpy_rate
        equity = cash_jpy + cash_usd * usd_jpy_rate
        return cls(
            portfolio_id=portfolio_id,
            initial_cash_jpy=initial_cash_jpy,
            cash_jpy=cash_jpy,
            cash_usd=cash_usd,
            initial_usd_jpy_rate=usd_jpy_rate,
            usd_jpy_rate=usd_jpy_rate,
            fx_as_of=fx_as_of,
            fx_source=fx_source,
            peak_equity_jpy=equity,
            updated_at=at,
        )


class USUniversePaperStore:
    """SQLite audit store for every stage and every virtual portfolio event."""

    _TABLES = frozenset(
        {
            "universe_snapshots",
            "screening_results",
            "market_snapshots",
            "feature_records",
            "jev_requests",
            "jev_responses",
            "candidate_rankings",
            "decisions",
            "paper_orders",
            "paper_fills",
            "positions",
            "portfolio_snapshots",
            "errors",
            "run_summaries",
        }
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            for table in self._TABLES:
                db.execute(
                    f"""CREATE TABLE IF NOT EXISTS {table} (
                        record_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        symbol TEXT,
                        recorded_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )"""
                )
                db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_run ON {table}(run_id)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS paper_portfolio_state (
                    portfolio_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS cached_minute_bars (
                    code TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(code, interval)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def record(
        self,
        table: str,
        *,
        run_id: str,
        payload: object,
        symbol: str | None = None,
        record_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        if table not in self._TABLES:
            raise ValueError(f"unsupported audit table: {table}")
        instant = recorded_at or datetime.now(UTC)
        with self._connect() as db:
            db.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(record_id, run_id, symbol, recorded_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    record_id or str(uuid4()),
                    run_id,
                    symbol,
                    instant.astimezone(UTC).isoformat(),
                    _json_dumps(payload),
                ),
            )

    def record_universe(
        self,
        listings: Sequence[USUniverseListing],
        *,
        snapshot_id: str,
        recorded_at: datetime,
    ) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO universe_snapshots "
                "(record_id, run_id, symbol, recorded_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        f"{snapshot_id}:{listing.code}",
                        snapshot_id,
                        listing.code,
                        recorded_at.astimezone(UTC).isoformat(),
                        listing.model_dump_json(),
                    )
                    for listing in listings
                ],
            )

    def load_latest_universe(
        self,
    ) -> tuple[str, datetime, tuple[USUniverseListing, ...]] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT run_id, recorded_at FROM universe_snapshots "
                "ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            snapshot_id, recorded_at_raw = str(row[0]), str(row[1])
            records = db.execute(
                "SELECT payload_json FROM universe_snapshots WHERE run_id = ? ORDER BY symbol",
                (snapshot_id,),
            ).fetchall()
        listings = tuple(USUniverseListing.model_validate_json(item[0]) for item in records)
        return snapshot_id, datetime.fromisoformat(recorded_at_raw), listings

    def load_latest_screen(
        self,
        *,
        now: datetime,
        max_age_seconds: int,
    ) -> USScreenCacheSnapshot | None:
        """Load the newest complete, successful screener result within its bootstrap age."""

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        with self._connect() as db:
            summaries = db.execute(
                """SELECT run_id, payload_json FROM run_summaries
                   WHERE run_id IN (SELECT DISTINCT run_id FROM screening_results)
                   ORDER BY recorded_at DESC LIMIT 100"""
            ).fetchall()
            for run_id_raw, summary_json in summaries:
                try:
                    summary_value = json.loads(str(summary_json))
                    if not isinstance(summary_value, Mapping):
                        continue
                    summary = cast(Mapping[str, Any], summary_value)
                    refreshed_at_raw = summary.get("screen_refreshed_at")
                    if not isinstance(refreshed_at_raw, str):
                        continue
                    refreshed_at = datetime.fromisoformat(refreshed_at_raw.replace("Z", "+00:00"))
                    age_seconds = (now - refreshed_at).total_seconds()
                    if age_seconds < 0 or age_seconds > max_age_seconds:
                        continue
                    total_count = int(summary["screen_total_count"])
                    truncated = bool(summary["screen_truncated"])
                    pages = int(summary["screen_pages"])
                    records = db.execute(
                        "SELECT payload_json FROM screening_results "
                        "WHERE run_id = ? ORDER BY symbol",
                        (str(run_id_raw),),
                    ).fetchall()
                    screen_rows: list[USScreenRow] = []
                    for (record_json,) in records:
                        record_value = json.loads(str(record_json))
                        if not isinstance(record_value, Mapping):
                            raise ValueError("screening result payload must be an object")
                        record_payload = cast(Mapping[str, Any], record_value)
                        screen_row_value = record_payload.get("screen_row")
                        if not isinstance(screen_row_value, Mapping):
                            raise ValueError("screening result row is missing")
                        screen_row = cast(Mapping[str, Any], screen_row_value)
                        screen_rows.append(USScreenRow.model_validate(screen_row))
                    if not screen_rows:
                        continue
                    return USScreenCacheSnapshot(
                        run_id=str(run_id_raw),
                        refreshed_at=refreshed_at,
                        rows=tuple(screen_rows),
                        total_count=total_count,
                        truncated=truncated,
                        pages=pages,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return None

    def save_portfolio(self, portfolio: USPaperPortfolio) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO paper_portfolio_state "
                "(portfolio_id, updated_at, payload_json) VALUES (?, ?, ?)",
                (
                    portfolio.portfolio_id,
                    portfolio.updated_at.astimezone(UTC).isoformat(),
                    portfolio.model_dump_json(),
                ),
            )

    def load_portfolio(self, portfolio_id: str) -> USPaperPortfolio | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM paper_portfolio_state WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        if row is None:
            return None
        return USPaperPortfolio.model_validate_json(str(row[0]))

    def load_cached_bars(
        self,
        code: str,
        *,
        interval: str,
        now: datetime,
        max_age_seconds: float,
    ) -> tuple[USOHLCVBar, ...] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT cached_at, payload_json FROM cached_minute_bars "
                "WHERE code = ? AND interval = ?",
                (code, interval),
            ).fetchone()
        if row is None:
            return None
        cached_at = datetime.fromisoformat(str(row[0]))
        age_seconds = (now - cached_at).total_seconds()
        if age_seconds < 0 or age_seconds > max_age_seconds:
            return None
        raw = json.loads(str(row[1]))
        return tuple(USOHLCVBar.model_validate(item) for item in raw)

    def save_cached_bars(
        self,
        code: str,
        bars: Sequence[USOHLCVBar],
        *,
        interval: str,
        cached_at: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cached_minute_bars "
                "(code, interval, cached_at, payload_json) VALUES (?, ?, ?, ?)",
                (
                    code,
                    interval,
                    cached_at.astimezone(UTC).isoformat(),
                    _json_dumps([bar.model_dump(mode="json") for bar in bars]),
                ),
            )


def hard_filter_rows(
    rows: Sequence[USScreenRow],
    listings: Mapping[str, USUniverseListing],
    *,
    min_price_usd: Decimal,
    min_market_cap_usd: Decimal,
    min_avg_turnover_20d_usd: Decimal,
    min_listing_days: int | None,
    include_etf: bool,
    include_otc: bool,
    as_of: datetime,
) -> tuple[tuple[USScreenRow, ...], Mapping[str, tuple[str, ...]]]:
    """Apply deterministic eligibility rules and return per-symbol rejections."""

    accepted: list[USScreenRow] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for row in rows:
        listing = listings.get(row.code)
        reasons: list[str] = []
        if listing is None:
            reasons.append("missing_universe_listing")
        else:
            if listing.delisted:
                reasons.append("delisted")
            allowed_exchanges = (US_EXCHANGES | US_OTC_EXCHANGES) if include_otc else US_EXCHANGES
            if listing.exchange not in allowed_exchanges:
                reasons.append("unsupported_exchange")
            if "ETF" in listing.security_type.upper() and not include_etf:
                reasons.append("etf_disabled")

        if row.price is None or row.price < min_price_usd:
            reasons.append("min_price")
        if row.market_cap_usd is None or row.market_cap_usd < min_market_cap_usd:
            reasons.append("min_market_cap")
        if row.avg_turnover_20d_usd is None or row.avg_turnover_20d_usd < min_avg_turnover_20d_usd:
            reasons.append("min_avg_turnover_20d")
        listed_days = row.listed_days
        if listed_days is None and listing is not None and listing.listing_date is not None:
            listed_days = (as_of.date() - listing.listing_date).days
        if min_listing_days is not None and (listed_days is None or listed_days < min_listing_days):
            reasons.append("min_listing_days")

        if reasons:
            rejected[row.code] = tuple(dict.fromkeys(reasons))
        else:
            accepted.append(row)
    return tuple(accepted), rejected


def percentile_ranks(values: Mapping[str, Decimal | None]) -> dict[str, Decimal]:
    """Return tie-averaged percentile ranks, robust to numeric outliers."""

    finite = sorted((value, key) for key, value in values.items() if value is not None)
    if not finite:
        return {key: Decimal("0") for key in values}
    ranks: dict[str, Decimal] = {key: Decimal("0") for key in values}
    if len(finite) == 1:
        ranks[finite[0][1]] = Decimal("1")
        return ranks
    start = 0
    denominator = Decimal(len(finite) - 1)
    while start < len(finite):
        end = start + 1
        while end < len(finite) and finite[end][0] == finite[start][0]:
            end += 1
        average_index = Decimal(start + end - 1) / Decimal("2")
        rank = average_index / denominator
        for _, key in finite[start:end]:
            ranks[key] = rank
        start = end
    return ranks


def lane_scores(rows: Sequence[USScreenRow]) -> dict[str, dict[str, Decimal]]:
    """Score four complementary lanes using in-universe percentile ranks."""

    by_code = {row.code: row for row in rows}
    values: dict[str, dict[str, Decimal | None]] = {
        "volume_ratio": {row.code: row.volume_ratio for row in rows},
        "abs_price_change": {
            row.code: abs(row.price_change_1d) if row.price_change_1d is not None else None
            for row in rows
        },
        "amplitude": {row.code: row.amplitude_1d for row in rows},
        "avg_turnover": {row.code: row.avg_turnover_20d_usd for row in rows},
        "proximity_high": {
            row.code: Decimal("1") + row.high_to_20d_high
            if row.high_to_20d_high is not None
            else None
            for row in rows
        },
        "extreme_distance": {
            row.code: -min(abs(row.high_to_20d_high), abs(row.low_to_20d_low))
            if row.high_to_20d_high is not None and row.low_to_20d_low is not None
            else None
            for row in rows
        },
    }
    ranks = {name: percentile_ranks(metric_values) for name, metric_values in values.items()}

    def weighted_rank(code: str, metrics: tuple[tuple[str, Decimal], ...]) -> Decimal:
        observed = [
            (ranks[name][code], weight)
            for name, weight in metrics
            if values[name].get(code) is not None
        ]
        if not observed:
            return Decimal("0")
        weight_sum = sum((weight for _, weight in observed), Decimal("0"))
        return sum((rank * weight for rank, weight in observed), Decimal("0")) / weight_sum

    result: dict[str, dict[str, Decimal]] = {}
    for code in by_code:
        result[code] = {
            "momentum": weighted_rank(
                code,
                (
                    ("volume_ratio", Decimal("0.40")),
                    ("abs_price_change", Decimal("0.30")),
                    ("amplitude", Decimal("0.30")),
                ),
            ),
            "breakout": weighted_rank(
                code,
                (
                    ("volume_ratio", Decimal("0.40")),
                    ("proximity_high", Decimal("0.40")),
                    ("avg_turnover", Decimal("0.20")),
                ),
            ),
            "reversal": weighted_rank(
                code,
                (
                    ("abs_price_change", Decimal("0.30")),
                    ("amplitude", Decimal("0.25")),
                    ("volume_ratio", Decimal("0.25")),
                    ("extreme_distance", Decimal("0.20")),
                ),
            ),
            "liquid": ranks["avg_turnover"][code],
        }
    return result


def lane_union(
    rows: Sequence[USScreenRow],
    scores: Mapping[str, Mapping[str, Decimal]],
    *,
    lane_top_k: int,
    union_limit: int,
) -> tuple[tuple[USScreenRow, ...], Mapping[str, tuple[str, ...]]]:
    """Union each lane's top K and deduplicate by the moomoo market code."""

    rows_by_code = {row.code: row for row in rows}
    lane_codes = {
        lane: tuple(
            code
            for code, _ in sorted(
                ((code, score[lane]) for code, score in scores.items()),
                key=lambda item: (-item[1], item[0]),
            )[:lane_top_k]
        )
        for lane in ("momentum", "breakout", "reversal", "liquid")
    }
    membership: dict[str, list[str]] = {}
    for lane, codes in lane_codes.items():
        for code in codes:
            membership.setdefault(code, []).append(lane)
    ordered = sorted(
        membership,
        key=lambda code: (
            -max(scores[code].get(lane, Decimal("0")) for lane in membership[code]),
            code,
        ),
    )[:union_limit]
    return tuple(rows_by_code[code] for code in ordered), lane_codes


def generate_quant_features(
    row: USScreenRow,
    snapshot: USMarketSnapshot,
    bars: Sequence[USOHLCVBar] = (),
) -> USQuantFeatures:
    """Generate point-in-time features without filling missing values."""

    as_of = snapshot.update_time.astimezone(UTC)
    session_date = as_of.astimezone(US_EASTERN).date()
    ordered = sorted(
        (
            bar
            for bar in bars
            if bar.timestamp.astimezone(UTC) <= as_of
            and bar.timestamp.astimezone(US_EASTERN).date() == session_date
            and time(9, 30) <= bar.timestamp.astimezone(US_EASTERN).time() < time(16, 0)
        ),
        key=lambda bar: bar.timestamp,
    )
    prices = [bar.close for bar in ordered]
    returns = [_return(prices[-1], prices[-2])] if len(prices) >= 2 else []
    ret_1m = returns[-1] if returns else None
    ret_5m = _lag_return(prices, 5)
    ret_15m = _lag_return(prices, 15)
    ret_60m = _lag_return(prices, 60)

    ret_1d = (
        _return(snapshot.last_price, snapshot.prev_close_price)
        if snapshot.prev_close_price is not None
        else row.price_change_1d
    )
    day_high = snapshot.high_price
    day_low = snapshot.low_price
    day_open = snapshot.open_price
    amplitude = None
    if day_high is not None and day_low is not None:
        denominator = snapshot.prev_close_price or day_open
        if denominator is not None and denominator > 0:
            amplitude = (day_high - day_low) / denominator

    distance_high = _return(snapshot.last_price, day_high)
    distance_low = _return(snapshot.last_price, day_low)
    high_20d = None
    low_20d = None
    if day_high is not None and row.high_to_20d_high is not None:
        divisor = Decimal("1") + row.high_to_20d_high
        if divisor > 0:
            high_20d = day_high / divisor
    if day_low is not None and row.low_to_20d_low is not None:
        divisor = Decimal("1") + row.low_to_20d_low
        if divisor > 0:
            low_20d = day_low / divisor
    distance_20d_high = _return(snapshot.last_price, high_20d)
    distance_20d_low = _return(snapshot.last_price, low_20d)

    volume = sum((bar.volume for bar in ordered), Decimal("0"))
    turnover = sum((bar.turnover for bar in ordered), Decimal("0"))
    vwap = turnover / volume if volume > 0 and turnover > 0 else None
    distance_vwap = _return(snapshot.last_price, vwap)

    log_returns: list[float] = []
    for left, right in zip(prices, prices[1:], strict=False):
        if left > 0 and right > 0:
            log_returns.append(math.log(float(right / left)))
    volatility = None
    if log_returns:
        volatility = Decimal(str(math.sqrt(sum(value * value for value in log_returns))))
    trend = None
    if ret_15m is not None and volatility is not None and volatility > 0:
        trend = ret_15m / volatility

    spread_bps = None
    if snapshot.bid_price is not None and snapshot.ask_price is not None:
        midpoint = (snapshot.bid_price + snapshot.ask_price) / Decimal("2")
        if midpoint > 0:
            spread_bps = (snapshot.ask_price - snapshot.bid_price) / midpoint * Decimal("10000")

    unavailable = tuple(
        field
        for field, value in (
            ("return_1m", ret_1m),
            ("return_5m", ret_5m),
            ("return_15m", ret_15m),
            ("return_60m", ret_60m),
            ("return_1d", ret_1d),
            ("return_5d", row.price_change_5d),
            ("volume_ratio", row.volume_ratio),
            ("avg_volume_20d", row.avg_volume_20d),
            ("avg_turnover_20d_usd", row.avg_turnover_20d_usd),
            ("intraday_amplitude", amplitude),
            ("realized_volatility", volatility),
            ("distance_from_vwap", distance_vwap),
            ("distance_from_20d_high", distance_20d_high),
            ("distance_from_20d_low", distance_20d_low),
        )
        if value is None
    )
    return USQuantFeatures(
        code=row.code,
        as_of=as_of,
        last_price=snapshot.last_price,
        return_1m=ret_1m,
        return_5m=ret_5m,
        return_15m=ret_15m,
        return_60m=ret_60m,
        return_1d=ret_1d,
        return_5d=row.price_change_5d,
        volume_ratio=row.volume_ratio,
        current_volume=snapshot.volume,
        avg_volume_20d=row.avg_volume_20d,
        avg_turnover_20d_usd=row.avg_turnover_20d_usd,
        intraday_amplitude=amplitude,
        realized_volatility=volatility,
        distance_from_vwap=distance_vwap,
        distance_from_day_high=distance_high,
        distance_from_day_low=distance_low,
        distance_from_20d_high=distance_20d_high,
        distance_from_20d_low=distance_20d_low,
        short_term_trend_strength=trend,
        spread_bps=spread_bps,
        liquidity_usd=row.avg_turnover_20d_usd,
        unavailable=unavailable,
    )


def rank_quant_candidates(
    rows: Sequence[USScreenRow],
    features: Mapping[str, USQuantFeatures],
    scores: Mapping[str, Mapping[str, Decimal]],
    *,
    count: int,
    lane_weight: Decimal = Decimal("0.25"),
    lane_membership: Mapping[str, Sequence[str]] | None = None,
) -> tuple[USRankedCandidate, ...]:
    """Rank only the lane union and preserve unavailable metric semantics."""

    if not Decimal("0") <= lane_weight <= Decimal("1"):
        raise ValueError("lane_weight must be between zero and one")
    feature_extractors: Mapping[str, Callable[[USQuantFeatures], Decimal | None]] = {
        "abs_return_5m": lambda item: abs(item.return_5m) if item.return_5m is not None else None,
        "abs_return_1d": lambda item: abs(item.return_1d) if item.return_1d is not None else None,
        "volume_ratio": lambda item: item.volume_ratio,
        "amplitude": lambda item: item.intraday_amplitude,
        "turnover": lambda item: item.avg_turnover_20d_usd,
        "spread_quality": lambda item: -item.spread_bps if item.spread_bps is not None else None,
    }
    rows_by_code = {row.code: row for row in rows}
    lanes_by_code: dict[str, list[str]] = {}
    for lane, codes in (lane_membership or {}).items():
        for code in codes:
            lanes_by_code.setdefault(code, []).append(lane)
    feature_values = {
        name: {
            code: extractor(features[code]) if code in features else None for code in rows_by_code
        }
        for name, extractor in feature_extractors.items()
    }
    rank_maps = {name: percentile_ranks(values) for name, values in feature_values.items()}
    unranked: list[tuple[USScreenRow, Decimal, Mapping[str, Decimal], USQuantFeatures | None]] = []
    for code, row in rows_by_code.items():
        lane_score = max(scores.get(code, {}).values(), default=Decimal("0"))
        observed = [
            rank_maps[name][code] for name in rank_maps if feature_values[name][code] is not None
        ]
        feature_score = (
            sum(observed, Decimal("0")) / Decimal(len(observed)) if observed else lane_score
        )
        combined = lane_score * lane_weight + feature_score * (Decimal("1") - lane_weight)
        lanes = scores.get(code, {})
        unranked.append((row, combined, lanes, features.get(code)))
    ordered = sorted(unranked, key=lambda item: (-item[1], item[0].code))[:count]
    return tuple(
        USRankedCandidate(
            code=row.code,
            name=row.name,
            quant_score=max(Decimal("0"), min(Decimal("1"), score)),
            lane_scores=lane_score,
            screening_lanes=tuple(lanes_by_code.get(row.code, ())),
            quant_rank=index,
            features=feature,
        )
        for index, (row, score, lane_score, feature) in enumerate(ordered, start=1)
    )


def stale_seconds(snapshot: USMarketSnapshot, now: datetime) -> float:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return (now.astimezone(UTC) - snapshot.update_time.astimezone(UTC)).total_seconds()


def make_paper_portfolio_from_ledger(
    previous: USPaperPortfolio,
    ledger_state: Any,
    *,
    usd_jpy_rate: Decimal,
    at: datetime,
    fx_as_of: str | None = None,
    fx_source: str | None = None,
) -> USPaperPortfolio:
    """Copy a PaperBroker ledger state into the persisted dual-currency view."""

    cash_usd = Decimal(str(ledger_state.cash))
    prices = dict(previous.market_prices_usd)
    prices.update({key: Decimal(str(value)) for key, value in ledger_state.mark_prices.items()})
    positions = {key: int(value) for key, value in ledger_state.positions.items() if int(value) > 0}
    average_prices = {
        key: Decimal(str(value))
        for key, value in ledger_state.average_prices.items()
        if key in positions
    }
    entries = {
        key: value for key, value in ledger_state.position_entry_times.items() if key in positions
    }
    equity_usd = Decimal(str(ledger_state.equity or cash_usd))
    equity_jpy = previous.cash_jpy + equity_usd * usd_jpy_rate
    peak = max(previous.peak_equity_jpy, equity_jpy)
    return USPaperPortfolio(
        portfolio_id=previous.portfolio_id,
        initial_cash_jpy=previous.initial_cash_jpy,
        cash_jpy=previous.cash_jpy,
        cash_usd=cash_usd,
        initial_usd_jpy_rate=previous.initial_usd_jpy_rate,
        usd_jpy_rate=usd_jpy_rate,
        fx_as_of=fx_as_of if fx_as_of is not None else previous.fx_as_of,
        fx_source=fx_source if fx_source is not None else previous.fx_source,
        positions=positions,
        average_prices_usd=average_prices,
        market_prices_usd=prices,
        position_entry_times=entries,
        realized_pnl_usd=Decimal(str(ledger_state.realized_pnl)),
        unrealized_pnl_usd=Decimal(str(ledger_state.unrealized_pnl)),
        total_fees_usd=Decimal(str(ledger_state.total_fees)),
        peak_equity_jpy=peak,
        drawdown_jpy=max(Decimal("0"), peak - equity_jpy),
        updated_at=at,
    )


def _lag_return(values: Sequence[Decimal], minutes: int) -> Decimal | None:
    if len(values) <= minutes:
        return None
    return _return(values[-1], values[-minutes - 1])


def _return(current: Decimal, previous: Decimal | None) -> Decimal | None:
    if previous is None or previous <= 0:
        return None
    return current / previous - Decimal("1")


def as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _json_dumps(value: object) -> str:
    if isinstance(value, DomainModel):
        return value.model_dump_json()
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, DomainModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize {type(value).__name__}")


__all__ = [
    "USDecisionKind",
    "USJevOpinion",
    "USMarketSnapshot",
    "USOHLCVBar",
    "USPaperPortfolio",
    "USPaperPosition",
    "USQuantFeatures",
    "USRankedCandidate",
    "USScreenRow",
    "USUniverseListing",
    "USUniversePaperStore",
    "US_EXCHANGES",
    "US_OTC_EXCHANGES",
    "as_decimal",
    "generate_quant_features",
    "hard_filter_rows",
    "lane_scores",
    "lane_union",
    "make_paper_portfolio_from_ledger",
    "percentile_ranks",
    "rank_quant_candidates",
    "stale_seconds",
]
