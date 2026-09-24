"""U.S. equity universe screening and Paper-only CLI orchestration."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field, model_validator

from trader_jev.clock import LiveClock
from trader_jev.decision import JevRequest
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.fees import MoomooFeeSchedule
from trader_jev.interfaces import Clock
from trader_jev.jev_http import JevHttpClient
from trader_jev.models import (
    Action,
    DataQuality,
    DecisionSnapshot,
    DomainModel,
    ExecutionMode,
    InstrumentMetadata,
    Market,
    MarketState,
    PortfolioState,
    RiskProfile,
    TradeIntent,
    TradingSession,
)
from trader_jev.moomoo import MoomooClientConfig
from trader_jev.nasdaq_calendar import NasdaqCalendar, NasdaqSession
from trader_jev.portfolio import PortfolioLedger
from trader_jev.risk import DeterministicRiskEngine, RiskConfig
from trader_jev.us_equity import (
    USDecisionKind,
    USJevOpinion,
    USMarketSnapshot,
    USOHLCVBar,
    USPaperPortfolio,
    USQuantFeatures,
    USRankedCandidate,
    USScreenRow,
    USUniverseListing,
    USUniversePaperStore,
    generate_quant_features,
    hard_filter_rows,
    lane_scores,
    lane_union,
    make_paper_portfolio_from_ledger,
    rank_quant_candidates,
    stale_seconds,
)
from trader_jev.us_moomoo import (
    MoomooUSMarketAdapter,
    ScreenFetch,
    USMoomooConfig,
    USMoomooError,
)

US_EASTERN = ZoneInfo("America/New_York")
MARKET_SYMBOL = "US"


class USPaperConfig(DomainModel):
    """File-based settings for universe discovery, screening and paper sizing."""

    database_path: Path = Path("data/us_equity_paper.sqlite3")
    include_etf: bool = False
    include_otc: bool = False
    universe_refresh_days: int = Field(default=7, ge=0)
    min_price_usd: Decimal = Field(default=Decimal("3"), gt=Decimal("0"))
    min_market_cap_usd: Decimal = Field(default=Decimal("300000000"), ge=Decimal("0"))
    min_avg_turnover_20d_usd: Decimal = Field(default=Decimal("10000000"), ge=Decimal("0"))
    min_listing_days: int = Field(default=90, ge=0)
    max_screen_rows: int = Field(default=2000, gt=0)
    screen_refresh_interval_seconds: int = Field(default=300, gt=0)
    startup_screen_cache_max_age_seconds: int = Field(default=86400, ge=0)
    lane_top_k: int = Field(default=50, gt=0)
    union_limit: int = Field(default=200, gt=0)
    minute_bar_candidates: int = Field(default=30, ge=0)
    jev_candidates: int = Field(default=30, ge=0)
    minute_bar_count: int = Field(default=390, ge=2, le=1000)
    minute_bar_refresh_interval_seconds: int = Field(default=60, gt=0)
    cached_bars_max_age_seconds: int = Field(default=120, ge=0)
    max_quote_age_seconds: int = Field(default=15, gt=0)
    decision_interval_seconds: int = Field(default=30, gt=0)
    alpha_quant: Decimal = Field(default=Decimal("0.4"), ge=Decimal("0"), le=Decimal("1"))
    beta_jev: Decimal = Field(default=Decimal("0.6"), ge=Decimal("0"), le=Decimal("1"))
    quant_lane_weight: Decimal = Field(default=Decimal("0.25"), ge=Decimal("0"), le=Decimal("1"))
    min_jev_confidence: Decimal = Field(default=Decimal("0.55"), ge=Decimal("0"), le=Decimal("1"))
    min_trade_worthy_probability: Decimal = Field(
        default=Decimal("0.6"), ge=Decimal("0"), le=Decimal("1")
    )
    max_abnormal_probability: Decimal = Field(
        default=Decimal("0.35"), ge=Decimal("0"), le=Decimal("1")
    )
    initial_cash_jpy: Decimal = Field(default=Decimal("100000"), gt=Decimal("0"))
    usd_jpy_rate: Decimal | None = Field(default=None, gt=Decimal("0"))
    fx_as_of: str | None = None
    fx_source: str | None = None
    cash_reserve_pct: Decimal = Field(default=Decimal("0.1"), ge=Decimal("0"), lt=Decimal("1"))
    max_positions: int = Field(default=3, gt=0)
    max_position_pct: Decimal = Field(default=Decimal("0.3"), gt=Decimal("0"), le=Decimal("1"))
    max_drawdown_pct: Decimal = Field(default=Decimal("0.05"), gt=Decimal("0"), le=Decimal("1"))
    max_spread_bps: Decimal = Field(default=Decimal("50"), gt=Decimal("0"))
    slippage_bps: Decimal = Field(default=Decimal("10"), ge=Decimal("0"))
    estimated_fee_bps: Decimal = Field(default=Decimal("13.2"), ge=Decimal("0"))
    fee_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    fee_schedule: MoomooFeeSchedule = MoomooFeeSchedule.MOOMOO_US_BASIC
    stop_loss_pct: Decimal = Field(default=Decimal("0.02"), gt=Decimal("0"), lt=Decimal("1"))
    take_profit_pct: Decimal = Field(default=Decimal("0.03"), gt=Decimal("0"))
    max_holding_seconds: int = Field(default=900, gt=0)
    risk_profile: RiskProfile = RiskProfile.CONSERVATIVE
    portfolio_id: str = "us-equities-100k"
    moomoo: USMoomooConfig = Field(default_factory=USMoomooConfig)

    @model_validator(mode="after")
    def validate_weights_and_limits(self) -> USPaperConfig:
        if self.alpha_quant + self.beta_jev != Decimal("1"):
            raise ValueError("alpha_quant + beta_jev must equal 1")
        if self.max_screen_rows < self.union_limit:
            raise ValueError("max_screen_rows must be at least union_limit")
        if self.jev_candidates > self.union_limit + self.max_positions:
            raise ValueError("jev_candidates cannot exceed union_limit plus max_positions")
        return self


class USMarketSource(Protocol):
    errors: list[USMoomooError]

    def __enter__(self) -> USMarketSource: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def fetch_universe(self, *, include_etf: bool) -> tuple[USUniverseListing, ...]: ...

    def screen_us(
        self,
        *,
        min_price_usd: Decimal,
        min_market_cap_usd: Decimal,
        min_avg_turnover_20d_usd: Decimal,
        min_listing_days: int | None,
        max_rows: int = 2000,
    ) -> ScreenFetch: ...

    def fetch_snapshots(self, codes: Sequence[str]) -> Mapping[str, USMarketSnapshot]: ...

    def fetch_minute_bars(
        self, codes: Sequence[str], *, count: int | None = None
    ) -> Mapping[str, tuple[USOHLCVBar, ...]]: ...

    def history_kline_quota(self) -> Mapping[str, Any]: ...


class USJevClient(Protocol):
    async def ask(
        self,
        request: JevRequest,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


MarketSourceFactory = Callable[[], AbstractContextManager[USMarketSource]]


class USScanResult(DomainModel):
    run_id: str
    as_of: datetime
    universe_count: int
    screened_count: int
    hard_filter_rejected_count: int
    lane_union_count: int
    snapshot_count: int
    minute_bar_count: int
    candidate_count: int
    screen_total_count: int = 0
    screen_pages: int = 0
    screen_truncated: bool = False
    screen_cache_hit: bool = False
    screen_refreshed_at: datetime | None = None
    rejected: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    candidates: tuple[USRankedCandidate, ...] = ()
    snapshots: Mapping[str, USMarketSnapshot] = Field(default_factory=dict)
    features: Mapping[str, USQuantFeatures] = Field(default_factory=dict)
    lane_membership: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    errors: tuple[str, ...] = ()


class USPaperStepResult(DomainModel):
    run_id: str
    as_of: datetime
    dry_run: bool
    status: str
    decision: USDecisionKind
    symbol: str | None = None
    reason: str
    risk_reason: str | None = None
    jev_opinion: USJevOpinion | None = None
    order_id: str | None = None
    fill_id: str | None = None
    portfolio: USPaperPortfolio | None = None
    scan: USScanResult | None = None


class USPaperRunSummary(DomainModel):
    """Bounded summary for a session-long Paper process."""

    started_at: datetime
    ended_at: datetime
    steps_completed: int = Field(ge=0)
    stop_reason: str
    last_run_id: str | None = None
    last_status: str | None = None
    last_reason: str | None = None


class USShareSizingPolicy:
    """Size long entries against a per-position cap and reserve-aware USD cash."""

    def __init__(
        self,
        *,
        config: USPaperConfig,
        slippage_bps: Decimal,
        fee_bps: Decimal,
    ) -> None:
        self.config = config
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps

    def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        symbol = intent.instrument.symbol
        current = int(portfolio.positions.get(symbol, 0))
        if intent.action is Action.SHORT:
            if current <= 0:
                return 0
            requested = intent.requested_quantity or current
            return min(current, requested)
        if intent.action is not Action.LONG or current != 0:
            return 0
        price = intent.limit_price or intent.metadata.get("reference_price")
        if price is None:
            return 0
        reference = Decimal(str(price))
        if reference <= 0:
            return 0
        equity = portfolio.equity or portfolio.cash
        allocation_cap = equity * self.config.max_position_pct
        cap_qty = int((allocation_cap / reference).to_integral_value(rounding=ROUND_DOWN))
        cost_multiplier = Decimal("1") + self.slippage_bps / Decimal("10000")
        cost_multiplier *= Decimal("1") + self.fee_bps / Decimal("10000")
        conservative_cost = reference * cost_multiplier
        if self.config.fee_schedule is MoomooFeeSchedule.MOOMOO_US_BASIC:
            conservative_cost += Decimal("0.01")
        cash_qty = int((portfolio.cash / conservative_cost).to_integral_value(rounding=ROUND_DOWN))
        requested = intent.requested_quantity or max(cap_qty, cash_qty)
        quantity = min(requested, cap_qty, cash_qty)
        lot_size = intent.instrument.lot_size
        return quantity - quantity % lot_size


class USUniversePaperRunner:
    """One scan/decision step, with all executions routed through RiskEngine."""

    def __init__(
        self,
        config: USPaperConfig,
        store: USUniversePaperStore,
        *,
        market_source_factory: MarketSourceFactory,
        jev_client: USJevClient | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._market_source_factory = market_source_factory
        self._jev_client = jev_client
        self._clock = clock or LiveClock()
        self._logger = logger or logging.getLogger("trader_jev.us_paper")
        self._calendar = NasdaqCalendar()
        self._active_market: USMarketSource | None = None
        self._cached_screen: ScreenFetch | None = None
        self._screen_cached_at: datetime | None = None
        self._screen_attempted_at: datetime | None = None
        self._startup_screen_cache_pending = False
        cached_screen = self.store.load_latest_screen(
            now=self._clock.now(),
            max_age_seconds=self.config.startup_screen_cache_max_age_seconds,
        )
        if cached_screen is not None:
            self._cached_screen = ScreenFetch(
                cached_screen.rows,
                cached_screen.total_count,
                cached_screen.truncated,
                cached_screen.pages,
            )
            self._screen_cached_at = cached_screen.refreshed_at
            self._screen_attempted_at = cached_screen.refreshed_at
            self._startup_screen_cache_pending = True

    @contextmanager
    def _market_session(self) -> Generator[USMarketSource, None, None]:
        if self._active_market is not None:
            yield self._active_market
            return
        with self._market_source_factory() as market:
            yield market

    def update_universe(self, *, include_etf: bool | None = None) -> tuple[USUniverseListing, ...]:
        now = self._clock.now()
        with self._market_session() as market:
            listings = market.fetch_universe(
                include_etf=self.config.include_etf if include_etf is None else include_etf
            )
        snapshot_id = str(uuid4())
        self.store.record_universe(listings, snapshot_id=snapshot_id, recorded_at=now)
        self.store.record(
            "run_summaries",
            run_id=snapshot_id,
            payload={"status": "UNIVERSE_UPDATED", "count": len(listings)},
            recorded_at=now,
        )
        self._cached_screen = None
        self._screen_cached_at = None
        self._screen_attempted_at = None
        self._startup_screen_cache_pending = False
        return listings

    def scan(self) -> USScanResult:
        now = self._clock.now()
        run_id = str(uuid4())
        latest = self.store.load_latest_universe()
        needs_etf_refresh = bool(
            latest
            and self.config.include_etf
            and not any("ETF" in listing.security_type.upper() for listing in latest[2])
        )
        if latest is None or self._universe_expired(latest[1], now) or needs_etf_refresh:
            try:
                self.update_universe()
                latest = self.store.load_latest_universe()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.store.record(
                    "errors",
                    run_id=run_id,
                    payload={"stage": "universe_update", "message": message},
                    recorded_at=now,
                )
                return self._failed_scan(run_id, now, message)
        if latest is None:
            return self._failed_scan(run_id, now, "universe cache is unavailable")
        _, _, listings = latest
        listing_map = {listing.code: listing for listing in listings}
        previous = self.store.load_portfolio(self.config.portfolio_id)
        held_codes = tuple(
            f"US.{symbol}"
            for symbol, quantity in (previous.positions.items() if previous else ())
            if quantity > 0
        )
        errors: list[str] = []
        screen = ScreenFetch((), 0, False, 0)
        screen_cache_hit = False
        snapshots: Mapping[str, USMarketSnapshot] = {}
        bars: Mapping[str, tuple[USOHLCVBar, ...]] = {}
        rejected: Mapping[str, tuple[str, ...]] = {}
        ranked: tuple[USRankedCandidate, ...] = ()
        features: dict[str, USQuantFeatures] = {}
        lane_membership: Mapping[str, tuple[str, ...]] = {}
        accepted_rows: tuple[USScreenRow, ...] = ()
        union_rows: tuple[USScreenRow, ...] = ()
        try:
            with self._market_session() as market:
                market.errors.clear()
                if self._startup_screen_cache_pending and self._cached_screen is not None:
                    screen = self._cached_screen
                    screen_cache_hit = True
                    self._startup_screen_cache_pending = False
                    self._screen_attempted_at = now
                elif self._screen_cache_is_fresh(now):
                    cached_screen = self._cached_screen
                    if cached_screen is None:
                        raise RuntimeError("fresh screener cache is missing")
                    screen = cached_screen
                    screen_cache_hit = True
                elif not self._screen_refresh_is_due(now):
                    if self._cached_screen is None:
                        raise USMoomooError(
                            "EMPTY_DATA",
                            "screener refresh is cooling down and no successful cache is available",
                            method="get_stock_screen",
                        )
                    screen = self._cached_screen
                    screen_cache_hit = True
                else:
                    self._screen_attempted_at = now
                    screen = market.screen_us(
                        min_price_usd=self.config.min_price_usd,
                        min_market_cap_usd=self.config.min_market_cap_usd,
                        min_avg_turnover_20d_usd=self.config.min_avg_turnover_20d_usd,
                        min_listing_days=self.config.min_listing_days,
                        max_rows=self.config.max_screen_rows,
                    )
                    self._cached_screen = screen
                    self._screen_cached_at = self._clock.now()
                    self._startup_screen_cache_pending = False
                accepted_rows, rejected = hard_filter_rows(
                    screen.rows,
                    listing_map,
                    min_price_usd=self.config.min_price_usd,
                    min_market_cap_usd=self.config.min_market_cap_usd,
                    min_avg_turnover_20d_usd=self.config.min_avg_turnover_20d_usd,
                    min_listing_days=self.config.min_listing_days,
                    include_etf=self.config.include_etf,
                    include_otc=self.config.include_otc,
                    as_of=now,
                )
                for row in screen.rows:
                    self.store.record(
                        "screening_results",
                        run_id=run_id,
                        symbol=row.code,
                        payload={
                            "screen_row": row.model_dump(mode="json"),
                            "accepted": row.code not in rejected,
                            "rejection_reasons": rejected.get(row.code, ()),
                        },
                        recorded_at=now,
                    )
                screening_scores = lane_scores(accepted_rows)
                union_rows, lane_membership = lane_union(
                    accepted_rows,
                    screening_scores,
                    lane_top_k=self.config.lane_top_k,
                    union_limit=self.config.union_limit,
                )
                union_codes = [row.code for row in union_rows]
                snapshot_codes = list(dict.fromkeys([*union_codes, *held_codes]))
                if snapshot_codes:
                    snapshots = market.fetch_snapshots(snapshot_codes)
                source_error_count = len(market.errors)
                source_errors = tuple(str(error) for error in market.errors)
                errors.extend(source_errors)
                for error in market.errors:
                    self.store.record(
                        "errors",
                        run_id=run_id,
                        payload={
                            "stage": "moomoo",
                            "kind": error.kind,
                            "method": error.method,
                            "message": str(error),
                        },
                        recorded_at=now,
                    )
                for missing_code in getattr(market, "missing_snapshot_codes", ()):
                    message = f"missing snapshot: {missing_code}"
                    errors.append(message)
                    self.store.record(
                        "errors",
                        run_id=run_id,
                        symbol=missing_code,
                        payload={
                            "stage": "snapshot",
                            "error_type": "MISSING_SNAPSHOT",
                            "message": message,
                        },
                        recorded_at=now,
                    )
                row_by_code = {row.code: row for row in union_rows}
                for code in held_codes:
                    if code in snapshots and code not in row_by_code:
                        listing = listing_map.get(code)
                        snap = snapshots[code]
                        row_by_code[code] = USScreenRow(
                            code=code,
                            name=listing.name if listing else code,
                            price=snap.last_price,
                        )
                preliminary = {
                    code: generate_quant_features(row, snapshots[code])
                    for code, row in row_by_code.items()
                    if code in snapshots
                }
                preliminary_ranked = rank_quant_candidates(
                    tuple(row_by_code.values()),
                    preliminary,
                    screening_scores,
                    count=len(row_by_code),
                    lane_weight=self.config.quant_lane_weight,
                    lane_membership=lane_membership,
                )
                selected_codes = [
                    candidate.code
                    for candidate in preliminary_ranked[: self.config.minute_bar_candidates]
                ]
                selected_codes.extend(code for code in held_codes if code not in selected_codes)
                cache_max_age = max(0, self.config.minute_bar_refresh_interval_seconds - 1)
                refresh_codes = [
                    code
                    for code in selected_codes
                    if self.store.load_cached_bars(
                        code,
                        interval="1m",
                        now=now,
                        max_age_seconds=cache_max_age,
                    )
                    is None
                ]
                scheduled_fetch = cast(
                    Callable[..., Mapping[str, tuple[USOHLCVBar, ...]]] | None,
                    getattr(market, "fetch_minute_bars_with_refresh", None),
                )
                if scheduled_fetch is not None:
                    bars = scheduled_fetch(
                        selected_codes,
                        refresh_codes=refresh_codes,
                        count=self.config.minute_bar_count,
                    )
                elif refresh_codes:
                    bars = market.fetch_minute_bars(
                        refresh_codes,
                        count=self.config.minute_bar_count,
                    )
                for error in market.errors[source_error_count:]:
                    message = str(error)
                    errors.append(message)
                    self.store.record(
                        "errors",
                        run_id=run_id,
                        payload={
                            "stage": "minute_bars",
                            "kind": error.kind,
                            "method": error.method,
                            "message": message,
                        },
                        recorded_at=now,
                    )
        except (USMoomooError, OSError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            self.store.record(
                "errors",
                run_id=run_id,
                payload={"stage": "scan", "error_type": type(exc).__name__, "message": str(exc)},
                recorded_at=now,
            )

        scores = lane_scores(accepted_rows)
        rows_for_rank: list[USScreenRow] = list(union_rows)
        row_by_code = {row.code: row for row in rows_for_rank}
        for code in held_codes:
            if code not in row_by_code and code in snapshots:
                listing = listing_map.get(code)
                rows_for_rank.append(
                    USScreenRow(
                        code=code,
                        name=listing.name if listing else code,
                        price=snapshots[code].last_price,
                    )
                )
        for row in rows_for_rank:
            snapshot = snapshots.get(row.code)
            if snapshot is None:
                continue
            cached = self.store.load_cached_bars(
                row.code,
                interval="1m",
                now=now,
                max_age_seconds=self.config.cached_bars_max_age_seconds,
            )
            bars_for_code = bars.get(row.code) or cached or ()
            if row.code in bars:
                self.store.save_cached_bars(
                    row.code,
                    bars[row.code],
                    interval="1m",
                    cached_at=now,
                )
            feature = generate_quant_features(row, snapshot, bars_for_code)
            features[row.code] = feature
            self.store.record(
                "market_snapshots",
                run_id=run_id,
                symbol=row.code,
                payload=snapshot,
                recorded_at=now,
            )
            self.store.record(
                "feature_records",
                run_id=run_id,
                symbol=row.code,
                payload={"features": feature, "bars": bars_for_code},
                recorded_at=now,
            )
        ranked = rank_quant_candidates(
            rows_for_rank,
            features,
            scores,
            count=len(rows_for_rank),
            lane_weight=self.config.quant_lane_weight,
            lane_membership=lane_membership,
        )
        for candidate in ranked:
            self.store.record(
                "candidate_rankings",
                run_id=run_id,
                symbol=candidate.code,
                payload=candidate,
                recorded_at=now,
            )
        result = USScanResult(
            run_id=run_id,
            as_of=now,
            universe_count=len(listings),
            screened_count=len(accepted_rows),
            hard_filter_rejected_count=len(rejected),
            lane_union_count=len(union_rows),
            snapshot_count=len(snapshots),
            minute_bar_count=sum(len(item) for item in bars.values()),
            candidate_count=len(ranked),
            screen_total_count=screen.total_count,
            screen_pages=screen.pages,
            screen_truncated=screen.truncated,
            screen_cache_hit=screen_cache_hit,
            screen_refreshed_at=self._screen_cached_at,
            rejected=rejected,
            candidates=ranked,
            snapshots=snapshots,
            features=features,
            lane_membership=lane_membership,
            errors=tuple(errors),
        )
        self.store.record(
            "run_summaries",
            run_id=run_id,
            payload=_jsonable(result),
            recorded_at=now,
        )
        return result

    def _screen_cache_is_fresh(self, now: datetime) -> bool:
        if self._cached_screen is None or self._screen_cached_at is None:
            return False
        age_seconds = (now - self._screen_cached_at).total_seconds()
        return 0 <= age_seconds < self.config.screen_refresh_interval_seconds

    def _screen_refresh_is_due(self, now: datetime) -> bool:
        if self._screen_attempted_at is None:
            return True
        age_seconds = (now - self._screen_attempted_at).total_seconds()
        return age_seconds < 0 or age_seconds >= self.config.screen_refresh_interval_seconds

    async def decide(
        self,
        *,
        portfolio_override: USPaperPortfolio | None = None,
    ) -> tuple[USScanResult, Mapping[str, USJevOpinion]]:
        if self._jev_client is None:
            raise RuntimeError("Jev client is required for decide")
        scan = self.scan()
        opinions: dict[str, USJevOpinion] = {}
        positions = portfolio_override or self.store.load_portfolio(self.config.portfolio_id)
        held = {
            f"US.{code}"
            for code, quantity in (positions.positions.items() if positions else ())
            if quantity > 0
        }
        eligible = [
            candidate
            for candidate in scan.candidates
            if candidate.code in scan.snapshots
            and candidate.code in scan.features
            and (candidate.code in held or candidate.quant_rank <= self.config.jev_candidates)
        ]
        eligible.sort(key=lambda candidate: (candidate.code not in held, candidate.quant_rank))
        calls = 0
        for candidate in eligible:
            if candidate.code not in held and calls >= self.config.jev_candidates:
                continue
            snapshot = scan.snapshots[candidate.code]
            if not self._snapshot_fresh(snapshot, scan.as_of):
                self.store.record(
                    "errors",
                    run_id=scan.run_id,
                    symbol=candidate.code,
                    payload={
                        "stage": "jev",
                        "error_type": "STALE_QUOTE",
                        "message": "quote is stale",
                    },
                    recorded_at=scan.as_of,
                )
                continue
            request_snapshot = self._decision_snapshot(
                candidate,
                snapshot,
                scan.features[candidate.code],
                positions,
                scan.as_of,
            )
            request = JevRequest(
                snapshot_id=request_snapshot.snapshot_id,
                market=MARKET_SYMBOL,
                symbol=candidate.code.partition(".")[2],
                as_of=snapshot.update_time,
                input_schema_version="us-equity-1.0",
                payload={
                    "features": scan.features[candidate.code].model_dump(mode="json"),
                    "quant_score": str(candidate.quant_score),
                    "lane_scores": {
                        key: str(value) for key, value in candidate.lane_scores.items()
                    },
                    "screening_lanes": candidate.screening_lanes,
                    "portfolio": _portfolio_summary(positions),
                    "horizon_minutes": 5,
                },
            )
            self.store.record(
                "jev_requests",
                run_id=scan.run_id,
                symbol=candidate.code,
                payload=request,
                recorded_at=scan.as_of,
            )
            try:
                raw = await self._jev_client.ask(request, _us_jev_questions())
                opinion = parse_jev_opinion(raw)
            except Exception as exc:
                self.store.record(
                    "errors",
                    run_id=scan.run_id,
                    symbol=candidate.code,
                    payload={"stage": "jev", "error_type": type(exc).__name__, "message": str(exc)},
                    recorded_at=scan.as_of,
                )
                continue
            opinions[candidate.code] = opinion
            self.store.record(
                "jev_responses",
                run_id=scan.run_id,
                symbol=candidate.code,
                payload={"opinion": opinion, "raw_response": raw},
                recorded_at=scan.as_of,
            )
            proposed = (
                USDecisionKind.BUY
                if self._entry_eligible(opinion, request_snapshot)
                else USDecisionKind.HOLD
            )
            self.store.record(
                "decisions",
                run_id=scan.run_id,
                symbol=candidate.code,
                payload={
                    "decision": proposed.value,
                    "reason": "JeV thresholds passed"
                    if proposed is USDecisionKind.BUY
                    else "JeV thresholds not met",
                    "quant_score": candidate.quant_score,
                    "opinion": opinion,
                    "execution": False,
                },
                recorded_at=scan.as_of,
            )
            if candidate.code not in held:
                calls += 1
        return scan, opinions

    async def paper_step(
        self,
        *,
        dry_run: bool = False,
        usd_jpy_rate: Decimal | None = None,
        usd_jpy_as_of: str | None = None,
        usd_jpy_source: str | None = None,
    ) -> USPaperStepResult:
        now = self._clock.now()
        run_id = str(uuid4())
        current = self.store.load_portfolio(self.config.portfolio_id)
        if usd_jpy_rate is not None:
            rate = usd_jpy_rate
            fx_source = usd_jpy_source or "command-line override"
            fx_as_of = usd_jpy_as_of or now.astimezone(UTC).isoformat()
        elif self.config.usd_jpy_rate is not None:
            rate = self.config.usd_jpy_rate
            fx_source = self.config.fx_source or "configuration"
            fx_as_of = self.config.fx_as_of or now.astimezone(UTC).isoformat()
        elif current is not None:
            rate = current.usd_jpy_rate
            fx_source = current.fx_source or "persisted portfolio"
            fx_as_of = current.fx_as_of or now.astimezone(UTC).isoformat()
        else:
            rate = None
            fx_source = self.config.fx_source or "configuration"
            fx_as_of = self.config.fx_as_of or now.astimezone(UTC).isoformat()
        if rate is None:
            reason = "USD/JPY rate is required for JPY-denominated paper capital"
            return self._record_no_trade(run_id, now, dry_run, reason, current)
        portfolio = current or USPaperPortfolio.initial(
            portfolio_id=self.config.portfolio_id,
            initial_cash_jpy=self.config.initial_cash_jpy,
            usd_jpy_rate=rate,
            cash_reserve_pct=self.config.cash_reserve_pct,
            at=now,
            fx_as_of=fx_as_of,
            fx_source=fx_source,
        )
        portfolio = portfolio.model_copy(
            update={
                "usd_jpy_rate": rate,
                "fx_source": fx_source,
                "fx_as_of": fx_as_of,
                "updated_at": now,
            }
        )
        # Reserve the configured JPY portion; the PaperBroker account is USD-only.
        if current is not None and current.initial_cash_jpy != self.config.initial_cash_jpy:
            self._logger.warning(
                "existing_portfolio_initial_cash_overrides_config",
                extra={"portfolio_id": current.portfolio_id},
            )
        if not _inside_regular_session(now, self._calendar):
            return self._record_no_trade(
                run_id,
                now,
                dry_run,
                "outside_regular_us_equity_session",
                portfolio.model_copy(update={"usd_jpy_rate": rate, "updated_at": now}),
            )
        if self._jev_client is None:
            scan = self.scan()
            opinions: Mapping[str, USJevOpinion] = {}
        else:
            scan, opinions = await self.decide(portfolio_override=portfolio)
        run_id = scan.run_id
        ledger = _restore_ledger(portfolio, at=now)
        snapshots_by_symbol: dict[
            str, tuple[USRankedCandidate, DecisionSnapshot, USJevOpinion | None]
        ] = {}
        for candidate in scan.candidates:
            opinion = opinions.get(candidate.code)
            snapshot = scan.snapshots.get(candidate.code)
            feature = scan.features.get(candidate.code)
            symbol = candidate.code.partition(".")[2]
            if (
                snapshot is None
                or feature is None
                or (opinion is None and symbol not in portfolio.positions)
            ):
                continue
            if not self._snapshot_fresh(snapshot, now):
                continue
            decision_snapshot = self._decision_snapshot(
                candidate, snapshot, feature, portfolio, now
            )
            snapshots_by_symbol[symbol] = (candidate, decision_snapshot, opinion)
            instrument = decision_snapshot.instrument
            ledger.mark(instrument, snapshot.last_price, now)

        decision = USDecisionKind.NO_TRADE
        selected: (
            tuple[str, CandidateAction, USRankedCandidate, DecisionSnapshot, USJevOpinion | None]
            | None
        ) = None
        for symbol, quantity in portfolio.positions.items():
            if quantity <= 0:
                continue
            selected_context = snapshots_by_symbol.get(symbol)
            if selected_context is None:
                continue
            candidate, decision_snapshot, opinion = selected_context
            exit_reason = self._exit_reason(symbol, portfolio, decision_snapshot, opinion, now)
            if exit_reason:
                selected = (symbol, CandidateAction.SELL, candidate, decision_snapshot, opinion)
                decision = USDecisionKind.SELL
                break
        if selected is None:
            eligible: list[
                tuple[Decimal, str, USRankedCandidate, DecisionSnapshot, USJevOpinion]
            ] = []
            for symbol, (candidate, decision_snapshot, opinion) in snapshots_by_symbol.items():
                if symbol in portfolio.positions or opinion is None:
                    continue
                if not self._entry_eligible(opinion, decision_snapshot):
                    continue
                combined = (
                    self.config.alpha_quant * candidate.quant_score
                    + self.config.beta_jev * opinion.jev_score
                )
                eligible.append((combined, symbol, candidate, decision_snapshot, opinion))
            if eligible and len(ledger.state.positions) < self.config.max_positions:
                _, symbol, candidate, decision_snapshot, opinion = max(
                    eligible, key=lambda item: (item[0], item[1])
                )
                selected = (symbol, CandidateAction.BUY, candidate, decision_snapshot, opinion)
                decision = USDecisionKind.BUY

        if selected is None:
            return self._record_no_trade(
                run_id,
                now,
                dry_run,
                "no fresh candidate passed the Jev and risk preconditions",
                portfolio,
                scan=scan,
            )

        symbol, action, candidate, decision_snapshot, opinion = selected
        instrument = decision_snapshot.instrument
        requested_quantity = (
            int(ledger.state.positions.get(symbol, 0)) if action is CandidateAction.SELL else None
        )
        intent = TradeIntent(
            snapshot_id=decision_snapshot.snapshot_id,
            instrument=instrument,
            action=Action.LONG if action is CandidateAction.BUY else Action.SHORT,
            requested_quantity=requested_quantity,
            confidence=opinion.min_confidence if opinion is not None else None,
            strategy_id="us-universe-jev-paper",
            model_version="jev-custom-questions",
            reason=(
                "Jev quality gate passed"
                if action is CandidateAction.BUY
                else self._exit_reason(symbol, portfolio, decision_snapshot, opinion, now)
                or "paper exit"
            ),
            created_at=now,
            metadata={
                "reference_price": str(
                    decision_snapshot.market.ask
                    if action is CandidateAction.BUY
                    else decision_snapshot.market.bid
                ),
                "quant_score": str(candidate.quant_score),
                "jev_score": str(opinion.jev_score) if opinion is not None else None,
                "dry_run": dry_run,
            },
        )
        equity_usd = _portfolio_equity_usd(portfolio)
        max_position_notional = max(Decimal("1"), equity_usd * self.config.max_position_pct)
        if action is CandidateAction.SELL:
            held_notional = (
                int(ledger.state.positions.get(symbol, 0)) * decision_snapshot.market.bid
            )
            max_position_notional = max(max_position_notional, held_notional)
        size_policy = USShareSizingPolicy(
            config=self.config,
            slippage_bps=self.config.slippage_bps,
            fee_bps=self._estimated_fee_bps,
        )
        risk = DeterministicRiskEngine(
            RiskConfig(
                risk_profile=self.config.risk_profile,
                execution_mode=ExecutionMode.PAPER,
                live_trading=False,
                live_armed=False,
                allow_short=True,
                max_positions=self.config.max_positions,
                max_order_notional=max_position_notional,
                max_position_notional=max_position_notional,
                max_drawdown=max(Decimal("0.01"), equity_usd * self.config.max_drawdown_pct),
                max_data_age_seconds=self.config.max_quote_age_seconds,
                max_spread_bps=self.config.max_spread_bps,
                enforce_cash=True,
                market_hours_only=True,
            ),
            portfolio_policy=size_policy,
            clock=self._clock,
            logger=self._logger,
        )
        risk_result = risk.evaluate(intent, decision_snapshot, ledger.state)
        risk_reason = risk_result.reason_code
        if not risk_result.approved or risk_result.order_intent is None:
            self.store.record(
                "decisions",
                run_id=run_id,
                symbol=symbol,
                payload={
                    "decision": USDecisionKind.NO_TRADE.value,
                    "proposed_action": decision.value,
                    "risk_reason": risk_reason,
                    "risk_message": risk_result.reason,
                    "intent": intent,
                    "opinion": opinion,
                },
                recorded_at=now,
            )
            return USPaperStepResult(
                run_id=run_id,
                as_of=now,
                dry_run=dry_run,
                status="RISK_REJECTED",
                decision=USDecisionKind.NO_TRADE,
                symbol=symbol,
                reason=risk_result.reason,
                risk_reason=risk_reason,
                jev_opinion=opinion,
                portfolio=portfolio,
                scan=scan,
            )

        order = risk_result.order_intent
        self.store.record(
            "decisions",
            run_id=run_id,
            symbol=symbol,
            payload={
                "decision": decision.value,
                "risk_reason": risk_reason,
                "intent": intent,
                "order": order,
                "opinion": opinion,
            },
            recorded_at=now,
        )
        self.store.record(
            "paper_orders",
            run_id=run_id,
            symbol=symbol,
            payload={"order": order, "dry_run": dry_run},
            recorded_at=now,
        )
        broker_ledger = _restore_ledger(portfolio, at=now)
        broker = PaperBroker(
            ExecutionConfig(
                fee_schedule=self.config.fee_schedule,
                fee_bps=self.config.fee_bps,
                slippage_bps=self.config.slippage_bps,
            ),
            clock=self._clock,
            ledger=broker_ledger,
        )
        broker.update_market(decision_snapshot.market, instrument)
        event = await broker.submit(order)
        fills = broker.fills
        fill = fills[-1] if fills else None
        if fill is not None:
            self.store.record(
                "paper_fills",
                run_id=run_id,
                symbol=symbol,
                payload={"fill": fill, "dry_run": dry_run},
                recorded_at=now,
            )
        for code, snapshot in scan.snapshots.items():
            if code.partition(".")[2] in broker_ledger.state.positions:
                instrument_for_mark = _instrument(
                    code.partition(".")[2],
                    _listing_for_code(code, scan, self.store),
                )
                broker_ledger.mark(instrument_for_mark, snapshot.last_price, now)
        updated = make_paper_portfolio_from_ledger(
            portfolio,
            broker_ledger.state,
            usd_jpy_rate=rate,
            at=now,
            fx_source=fx_source,
            fx_as_of=fx_as_of,
        )
        if not dry_run:
            self.store.save_portfolio(updated)
        self.store.record(
            "positions",
            run_id=run_id,
            symbol=symbol,
            payload={"positions": broker_ledger.state.positions, "dry_run": dry_run},
            recorded_at=now,
        )
        self.store.record(
            "portfolio_snapshots",
            run_id=run_id,
            payload={"portfolio": updated, "dry_run": dry_run},
            recorded_at=now,
        )
        return USPaperStepResult(
            run_id=run_id,
            as_of=now,
            dry_run=dry_run,
            status="FILLED" if fill else f"PAPER_{event.status.value}",
            decision=decision if fill else USDecisionKind.NO_TRADE,
            symbol=symbol,
            reason="PaperBroker simulated a fill"
            if fill
            else event.reason or "paper order rejected",
            risk_reason=risk_reason,
            jev_opinion=opinion,
            order_id=str(order.order_intent_id),
            fill_id=str(fill.fill_id) if fill else None,
            portfolio=updated,
            scan=scan,
        )

    async def paper_run(
        self,
        *,
        steps: int | None,
        dry_run: bool,
        usd_jpy_rate: Decimal | None = None,
        usd_jpy_as_of: str | None = None,
        usd_jpy_source: str | None = None,
        until_market_close: bool = False,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> tuple[USPaperStepResult, ...] | USPaperRunSummary:
        if steps == 0:
            if until_market_close:
                started_at = self._clock.now()
                return USPaperRunSummary(
                    started_at=started_at,
                    ended_at=started_at,
                    steps_completed=0,
                    stop_reason="step_limit",
                )
            return ()
        if self._active_market is not None:
            raise RuntimeError("paper_run cannot be started inside an active market session")
        started_at = self._clock.now()
        if until_market_close and not _inside_regular_session(started_at, self._calendar):
            return USPaperRunSummary(
                started_at=started_at,
                ended_at=started_at,
                steps_completed=0,
                stop_reason="market_closed",
            )
        results: list[USPaperStepResult] = []
        last_result: USPaperStepResult | None = None
        steps_completed = 0
        stop_reason = "step_limit"
        with self._market_source_factory() as market:
            self._active_market = market
            try:
                while steps is None or steps_completed < steps:
                    if until_market_close and not _inside_regular_session(
                        self._clock.now(), self._calendar
                    ):
                        stop_reason = "market_closed"
                        break
                    last_result = await self.paper_step(
                        dry_run=dry_run,
                        usd_jpy_rate=usd_jpy_rate,
                        usd_jpy_as_of=usd_jpy_as_of,
                        usd_jpy_source=usd_jpy_source,
                    )
                    steps_completed += 1
                    if not until_market_close:
                        results.append(last_result)
                    if steps is not None and steps_completed >= steps:
                        stop_reason = "step_limit"
                        break
                    await sleep(self.config.decision_interval_seconds)
            finally:
                self._active_market = None
        if until_market_close:
            return USPaperRunSummary(
                started_at=started_at,
                ended_at=self._clock.now(),
                steps_completed=steps_completed,
                stop_reason=stop_reason,
                last_run_id=last_result.run_id if last_result is not None else None,
                last_status=last_result.status if last_result is not None else None,
                last_reason=last_result.reason if last_result is not None else None,
            )
        return tuple(results)

    def portfolio(self) -> USPaperPortfolio | None:
        return self.store.load_portfolio(self.config.portfolio_id)

    def history_kline_quota(self) -> Mapping[str, Any]:
        with self._market_session() as market:
            return market.history_kline_quota()

    @property
    def _estimated_fee_bps(self) -> Decimal:
        return (
            self.config.fee_bps
            if self.config.fee_schedule is MoomooFeeSchedule.BASIS_POINTS
            else self.config.estimated_fee_bps
        )

    def _entry_eligible(self, opinion: USJevOpinion, snapshot: DecisionSnapshot) -> bool:
        if (
            opinion.min_confidence is None
            or opinion.min_confidence < self.config.min_jev_confidence
        ):
            return False
        if opinion.trade_worthy_probability < self.config.min_trade_worthy_probability:
            return False
        if opinion.abnormal_probability > self.config.max_abnormal_probability:
            return False
        if not snapshot.data_quality.healthy:
            return False
        if (
            snapshot.market.spread / snapshot.market.mid * Decimal("10000")
            > self.config.max_spread_bps
        ):
            return False
        return opinion.setup_type != "NO_SETUP"

    def _exit_reason(
        self,
        symbol: str,
        portfolio: USPaperPortfolio,
        snapshot: DecisionSnapshot,
        opinion: USJevOpinion | None,
        now: datetime,
    ) -> str | None:
        entry = portfolio.average_prices_usd.get(symbol)
        quantity = portfolio.positions.get(symbol, 0)
        if entry is None or quantity <= 0:
            return None
        change = snapshot.market.bid / entry - Decimal("1")
        if change <= -self.config.stop_loss_pct:
            return "stop_loss"
        if change >= self.config.take_profit_pct:
            return "take_profit"
        opened = portfolio.position_entry_times.get(symbol)
        if opened and (now - opened).total_seconds() >= self.config.max_holding_seconds:
            return "maximum_holding_time"
        if opinion is not None and (
            opinion.abnormal_probability > self.config.max_abnormal_probability
            or opinion.trade_worthy_probability < Decimal("0.35")
        ):
            return "Jev_exit_signal"
        return None

    def _decision_snapshot(
        self,
        candidate: USRankedCandidate,
        market_snapshot: USMarketSnapshot,
        features: USQuantFeatures,
        portfolio: USPaperPortfolio | None,
        now: datetime,
    ) -> DecisionSnapshot:
        code = candidate.code
        symbol = code.partition(".")[2]
        listing = _listing_from_cache(self.store, code)
        instrument = _instrument(symbol, listing)
        bid = market_snapshot.bid_price or market_snapshot.last_price
        ask = market_snapshot.ask_price or market_snapshot.last_price
        if bid > ask:
            raise ValueError(f"invalid bid/ask for {symbol}")
        midpoint = (bid + ask) / Decimal("2")
        spread = ask - bid
        age = stale_seconds(market_snapshot, now)
        healthy = age >= 0 and age <= self.config.max_quote_age_seconds
        missing: list[str] = []
        if market_snapshot.bid_price is None:
            missing.append("bid_price")
        if market_snapshot.ask_price is None:
            missing.append("ask_price")
        if market_snapshot.bid_price is None or market_snapshot.ask_price is None:
            healthy = False
        if features.unavailable:
            missing.extend(features.unavailable)
        return DecisionSnapshot(
            instrument=instrument,
            event_time=market_snapshot.update_time,
            as_of=now,
            market=MarketState(
                bid=bid,
                ask=ask,
                mid=midpoint,
                spread=spread,
                bid_size=market_snapshot.bid_volume or Decimal("0"),
                ask_size=market_snapshot.ask_volume or Decimal("0"),
                last_event_time=market_snapshot.update_time,
                last_received_at=now,
            ),
            technical=_float_features(features),
            orderbook={
                "spread_bps": float(spread / midpoint * Decimal("10000")),
                "bid_size": float(market_snapshot.bid_volume or Decimal("0")),
                "ask_size": float(market_snapshot.ask_volume or Decimal("0")),
            },
            short_history_summary={
                "quant_score": float(candidate.quant_score),
                "quant_rank": float(candidate.quant_rank),
            },
            portfolio=_portfolio_summary(portfolio),
            data_quality=DataQuality(
                healthy=healthy,
                freshness_ms=max(0, int(max(0, age) * 1000)),
                missing_fields=tuple(dict.fromkeys(missing)),
                reasons=tuple(
                    reason
                    for reason, applies in (
                        (
                            "stale_or_future_quote",
                            age < 0 or age > self.config.max_quote_age_seconds,
                        ),
                        (
                            "missing_best_bid_or_ask",
                            market_snapshot.bid_price is None or market_snapshot.ask_price is None,
                        ),
                    )
                    if applies
                ),
            ),
        )

    def _snapshot_fresh(self, snapshot: USMarketSnapshot, now: datetime) -> bool:
        age = stale_seconds(snapshot, now)
        return 0 <= age <= self.config.max_quote_age_seconds

    def _universe_expired(self, recorded_at: datetime, now: datetime) -> bool:
        return (now - recorded_at).total_seconds() > self.config.universe_refresh_days * 86400

    def _failed_scan(self, run_id: str, now: datetime, message: str) -> USScanResult:
        self.store.record(
            "errors",
            run_id=run_id,
            payload={"stage": "universe", "error_type": "MISSING_UNIVERSE", "message": message},
            recorded_at=now,
        )
        return USScanResult(
            run_id=run_id,
            as_of=now,
            universe_count=0,
            screened_count=0,
            hard_filter_rejected_count=0,
            lane_union_count=0,
            snapshot_count=0,
            minute_bar_count=0,
            candidate_count=0,
            errors=(message,),
        )

    def _record_no_trade(
        self,
        run_id: str,
        now: datetime,
        dry_run: bool,
        reason: str,
        portfolio: USPaperPortfolio | None,
        *,
        scan: USScanResult | None = None,
    ) -> USPaperStepResult:
        self.store.record(
            "decisions",
            run_id=run_id,
            payload={
                "decision": USDecisionKind.NO_TRADE.value,
                "reason": reason,
                "dry_run": dry_run,
            },
            recorded_at=now,
        )
        if portfolio is not None and not dry_run:
            self.store.save_portfolio(portfolio)
        return USPaperStepResult(
            run_id=run_id,
            as_of=now,
            dry_run=dry_run,
            status="NO_TRADE",
            decision=USDecisionKind.NO_TRADE,
            reason=reason,
            portfolio=portfolio,
            scan=scan,
        )


class CandidateAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def load_config(path: Path) -> USPaperConfig:
    if not path.exists():
        return USPaperConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("configuration root must be a mapping")
    return USPaperConfig.model_validate(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev",
        description="Screen U.S. equities and run a read-only-moomoo, PaperBroker-only workflow.",
    )
    parser.add_argument("--config", type=Path, default=Path("configs/us-equity-paper.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("universe-update", help="refresh the cached U.S. security master")
    subparsers.add_parser("scan", help="screen, rank lanes and calculate quant features")
    subparsers.add_parser("decide", help="ask Jev for candidate opinions without paper execution")
    step = subparsers.add_parser("paper-step", help="run one Jev and PaperBroker step")
    step.add_argument("--dry-run", action="store_true")
    step.add_argument("--usd-jpy", type=_decimal_arg)
    step.add_argument("--usd-jpy-as-of")
    step.add_argument("--usd-jpy-source")
    run = subparsers.add_parser("paper-run", help="repeat paper steps at the configured interval")
    run.add_argument("--steps", type=_positive_int_arg)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--usd-jpy", type=_decimal_arg)
    run.add_argument("--usd-jpy-as-of")
    run.add_argument("--usd-jpy-source")
    run.add_argument(
        "--until-market-close",
        action="store_true",
        help="stop at the end of the current regular U.S. equity session",
    )
    subparsers.add_parser("portfolio", help="show the persisted paper portfolio")
    subparsers.add_parser(
        "history-quota", help="read moomoo historical-kline quota without using it"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if config.usd_jpy_rate is None:
            env_rate = os.environ.get("US_EQUITY_USD_JPY_RATE")
            if env_rate:
                config = config.model_copy(update={"usd_jpy_rate": Decimal(env_rate)})
        store = USUniversePaperStore(config.database_path)
        env_values = _load_env(args.env_file)
        connection = MoomooClientConfig.from_env(env_values)

        def market_factory() -> MoomooUSMarketAdapter:
            return MoomooUSMarketAdapter(connection=connection, config=config.moomoo)

        jev: USJevClient | None = None
        if args.command in {"decide", "paper-step", "paper-run"}:
            jev = cast(USJevClient, JevHttpClient.from_env(env_values))
        runner = USUniversePaperRunner(
            config,
            store,
            market_source_factory=market_factory,
            jev_client=jev,
        )
        if args.command == "universe-update":
            _print_json({"count": len(runner.update_universe())})
        elif args.command == "scan":
            _print_json(runner.scan())
        elif args.command == "decide":
            scan, opinions = asyncio.run(runner.decide())
            _print_json({"scan": scan, "opinions": opinions})
        elif args.command == "paper-step":
            result = asyncio.run(
                runner.paper_step(
                    dry_run=args.dry_run,
                    usd_jpy_rate=args.usd_jpy,
                    usd_jpy_as_of=args.usd_jpy_as_of,
                    usd_jpy_source=args.usd_jpy_source,
                )
            )
            _print_json(result)
        elif args.command == "paper-run":
            results = asyncio.run(
                runner.paper_run(
                    steps=args.steps,
                    dry_run=args.dry_run,
                    usd_jpy_rate=args.usd_jpy,
                    usd_jpy_as_of=args.usd_jpy_as_of,
                    usd_jpy_source=args.usd_jpy_source,
                    until_market_close=args.until_market_close,
                )
            )
            _print_json(results)
        elif args.command == "portfolio":
            _print_json(runner.portfolio())
        elif args.command == "history-quota":
            _print_json(runner.history_kline_quota())
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


def parse_jev_opinion(response: Mapping[str, Any]) -> USJevOpinion:
    answers_value = response.get("answers")
    if not isinstance(answers_value, Mapping):
        raise ValueError("Jev response is missing the answers object")
    answers = cast(Mapping[str, Any], answers_value)
    setup = _answer(answers, "setup_type")
    trend = _answer(answers, "trend_quality")
    continuation = _answer(answers, "continuation_quality")
    abnormal = _answer(answers, "abnormal_activity")
    trade_worthy = _answer(answers, "trade_worthy")
    setup_value = _choice_value(setup, "setup_type")
    setup_probs = _probability_map(setup)
    trend_probs = _probability_map(trend)
    continuation_probs = _probability_map(continuation)
    trend_score = _score_value(trend, "trend_quality")
    continuation_score = _score_value(continuation, "continuation_quality")
    abnormal_probability = _noul_probability(abnormal, "abnormal_activity")
    trade_probability = _noul_probability(trade_worthy, "trade_worthy")
    confidence_values = [
        value
        for value in (
            _optional_confidence(setup),
            _optional_confidence(trend),
            _optional_confidence(continuation),
        )
        if value is not None
    ]
    minimum_confidence = min(confidence_values) if confidence_values else None
    jev_score = (
        Decimal("0.30") * trend_score
        + Decimal("0.25") * continuation_score
        + Decimal("0.30") * trade_probability
        + Decimal("0.15") * (Decimal("1") - abnormal_probability)
    )
    return USJevOpinion(
        setup_type=setup_value,
        setup_probabilities=setup_probs,
        setup_confidence=_optional_confidence(setup),
        trend_quality=trend_score,
        trend_probabilities=trend_probs,
        trend_confidence=_optional_confidence(trend),
        continuation_quality=continuation_score,
        continuation_probabilities=continuation_probs,
        continuation_confidence=_optional_confidence(continuation),
        abnormal_probability=abnormal_probability,
        trade_worthy_probability=trade_probability,
        jev_score=max(Decimal("0"), min(Decimal("1"), jev_score)),
        min_confidence=minimum_confidence,
        raw_response=response,
    )


def _us_jev_questions() -> dict[str, dict[str, Any]]:
    return {
        "setup_type": {
            "type": "choice",
            "instructions": "Classify the current short-term U.S. equity setup.",
            "criteria": {
                "MOMENTUM_BREAKOUT": "Strong participation and a price holding near a recent high.",
                "REVERSAL": "A stretched move is showing evidence of a short-term reversal.",
                "TREND_CONTINUATION": "An established short-term trend is likely to continue.",
                "RANGE": "The price is range-bound without a clear directional setup.",
                "NO_SETUP": "The available data does not support a trade setup.",
            },
        },
        "trend_quality": {
            "type": "score",
            "instructions": (
                "Rate the clarity and stability of the five-minute trend from zero to one."
            ),
            "criteria": ["0.0: no usable trend", "0.5: mixed evidence", "1.0: clear stable trend"],
        },
        "continuation_quality": {
            "type": "score",
            "instructions": (
                "Rate the odds that the current setup continues over the next five minutes."
            ),
            "criteria": [
                "0.0: continuation unlikely",
                "0.5: uncertain",
                "1.0: strong continuation evidence",
            ],
        },
        "abnormal_activity": {
            "type": "noul",
            "instructions": (
                "Is abnormal or unstable price/volume activity likely to invalidate this setup?"
            ),
        },
        "trade_worthy": {
            "type": "noul",
            "instructions": (
                "Is this candidate suitable for a long-only paper entry after fees and spread?"
            ),
        },
    }


def _answer(answers: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = answers.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"Jev response is missing the {name} answer")
    return cast(Mapping[str, Any], value)


def _choice_value(answer: Mapping[str, Any], name: str) -> str:
    value = answer.get("choice")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Jev {name} answer has no choice")
    return value.strip().upper().replace(" ", "_")


def _score_value(answer: Mapping[str, Any], name: str) -> Decimal:
    value = answer.get("score")
    if value is None:
        raise ValueError(f"Jev {name} answer has no score")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        raise ValueError(f"Jev {name} score must be between zero and one")
    return parsed


def _noul_probability(answer: Mapping[str, Any], name: str) -> Decimal:
    value = answer.get("noul", answer.get("probability"))
    if value is None:
        raise ValueError(f"Jev {name} answer has no Noul probability")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        raise ValueError(f"Jev {name} probability must be between zero and one")
    return parsed


def _probability_map(answer: Mapping[str, Any]) -> Mapping[str, Decimal]:
    values = answer.get("probabilities")
    if not isinstance(values, Mapping):
        return {}
    parsed: dict[str, Decimal] = {}
    probability_values = cast(Mapping[str, Any], values)
    for key, value in probability_values.items():
        number = _optional_decimal(value)
        if number is not None and number.is_finite() and Decimal("0") <= number <= Decimal("1"):
            parsed[str(key)] = number
    return parsed


def _optional_confidence(answer: Mapping[str, Any]) -> Decimal | None:
    value = _optional_decimal(answer.get("confidence"))
    if value is None:
        return None
    if value < 0 or value > 1:
        return None
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _restore_ledger(portfolio: USPaperPortfolio, *, at: datetime) -> PortfolioLedger:
    cash = portfolio.cash_usd
    equity = cash + sum(
        (
            portfolio.market_prices_usd.get(
                symbol, portfolio.average_prices_usd.get(symbol, Decimal("0"))
            )
            * quantity
            for symbol, quantity in portfolio.positions.items()
        ),
        Decimal("0"),
    )
    state = PortfolioState(
        portfolio_id=portfolio.portfolio_id,
        cash=cash,
        positions=dict(portfolio.positions),
        daily_pnl=portfolio.realized_pnl_usd + portfolio.unrealized_pnl_usd,
        drawdown=portfolio.drawdown_jpy / portfolio.usd_jpy_rate,
        initial_capital=(portfolio.initial_cash_jpy / portfolio.initial_usd_jpy_rate)
        * (Decimal("1") - (portfolio.cash_jpy / portfolio.initial_cash_jpy)),
        equity=equity,
        realized_pnl=portfolio.realized_pnl_usd,
        total_fees=portfolio.total_fees_usd,
        unrealized_pnl=portfolio.unrealized_pnl_usd,
        average_prices=dict(portfolio.average_prices_usd),
        mark_prices=dict(portfolio.market_prices_usd),
        position_entry_times=dict(portfolio.position_entry_times),
        updated_at=at,
    )
    ledger = PortfolioLedger(
        state,
        peak_equity=(portfolio.peak_equity_jpy - portfolio.cash_jpy) / portfolio.usd_jpy_rate,
    )
    return ledger


def _instrument(symbol: str, listing: USUniverseListing | None) -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol=symbol,
        market=Market.US,
        currency="USD",
        timezone="America/New_York",
        tick_size=Decimal("0.01"),
        lot_size=max(1, listing.lot_size) if listing else 1,
        trading_session=TradingSession(open_time=time(9, 30), close_time=time(16, 0)),
        # SHORT is used only for the RiskEngine/PaperBroker sell-to-close route.
        shortability=True,
    )


def _listing_from_cache(store: USUniversePaperStore, code: str) -> USUniverseListing | None:
    latest = store.load_latest_universe()
    if latest is None:
        return None
    return next((listing for listing in latest[2] if listing.code == code), None)


def _listing_for_code(
    code: str, scan: USScanResult, store: USUniversePaperStore
) -> USUniverseListing | None:
    del scan
    return _listing_from_cache(store, code)


def _portfolio_equity_usd(portfolio: USPaperPortfolio) -> Decimal:
    return portfolio.cash_usd + portfolio.positions_value_usd


def _portfolio_summary(portfolio: USPaperPortfolio | None) -> Mapping[str, Any]:
    if portfolio is None:
        return {}
    return {
        "portfolio_id": portfolio.portfolio_id,
        "cash_jpy": str(portfolio.cash_jpy),
        "cash_usd": str(portfolio.cash_usd),
        "positions": dict(portfolio.positions),
        "total_equity_jpy": str(portfolio.total_equity_jpy),
        "usd_jpy_rate": str(portfolio.usd_jpy_rate),
    }


def _float_features(features: USQuantFeatures) -> dict[str, float]:
    result: dict[str, float] = {}
    for name, value in features.model_dump().items():
        if isinstance(value, Decimal):
            result[name] = float(value)
    return result


def _inside_regular_session(now: datetime, calendar: NasdaqCalendar) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        return False
    session: NasdaqSession | None = calendar.session_for(now)
    if session is None:
        return False
    eastern_now = now.astimezone(US_EASTERN)
    return session.open_at <= eastern_now < session.close_at


def _load_env(path: Path) -> Mapping[str, str]:
    from trader_jev.cli import load_env_file

    return load_env_file(path)


def _print_json(value: object) -> None:
    output: object = value
    if isinstance(value, DomainModel):
        output = value.model_dump(mode="json")
    elif isinstance(value, tuple):
        serialized: list[object] = []
        for item in cast(tuple[object, ...], value):
            serialized.append(
                item.model_dump(mode="json") if isinstance(item, DomainModel) else item
            )
        output = serialized
    print(json.dumps(output, ensure_ascii=False, default=_json_default, indent=2))


def _jsonable(value: object) -> Any:
    if isinstance(value, DomainModel):
        return value.model_dump(mode="json")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, (Decimal, datetime, date)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _decimal_arg(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be a decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive finite decimal")
    return parsed


def _positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


__all__ = [
    "USPaperConfig",
    "USPaperStepResult",
    "USScanResult",
    "USShareSizingPolicy",
    "USUniversePaperRunner",
    "build_parser",
    "load_config",
    "main",
    "parse_jev_opinion",
]
