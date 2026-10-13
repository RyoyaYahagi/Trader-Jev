"""Compare exit policies from saved Paper entries without fresh model or market calls."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trader_jev.clock import FixedClock
from trader_jev.models import Action, DomainModel, FillEvent
from trader_jev.us_equity import (
    USDecisionKind,
    USMarketSnapshot,
    USOHLCVBar,
    USPaperPortfolio,
    USUniverseListing,
    USUniversePaperStore,
)
from trader_jev.us_moomoo import ScreenFetch, USMoomooError
from trader_jev.us_paper_cli import (
    USPaperConfig,
    USPaperStepResult,
    USScanResult,
    USUniversePaperRunner,
)


class ExitReplayResult(DomainModel):
    max_holding_seconds: int
    closed: bool
    portfolio: USPaperPortfolio
    attempts: tuple[USPaperStepResult, ...]
    exit_fills: tuple[FillEvent, ...]
    # Unclosed trades have no completed-trade PnL, never an assumed zero.
    net_pnl_usd: Decimal | None = None
    gross_quote_pnl_usd: Decimal | None = None  # bid/ask PnL before fees/slippage
    fees_usd: Decimal | None = None
    slippage_cost_usd: Decimal | None = None
    maximum_holding_overshoot_seconds: Decimal | None = None


class _RecordedMarket:
    """Only the execution quote refresh can read from this in-memory source."""

    errors: list[USMoomooError]

    def __init__(self, quote: USMarketSnapshot) -> None:
        self.quote = quote
        self.errors = []

    def __enter__(self) -> _RecordedMarket:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        pass

    def fetch_snapshots(self, codes: Sequence[str]) -> Mapping[str, USMarketSnapshot]:
        return {code: self.quote for code in codes if code == self.quote.code}

    def fetch_universe(self, *, include_etf: bool) -> tuple[USUniverseListing, ...]:
        raise RuntimeError("exit replay cannot discover a universe")

    def screen_us(
        self,
        *,
        min_price_usd: Decimal,
        min_market_cap_usd: Decimal,
        min_avg_turnover_20d_usd: Decimal,
        min_listing_days: int | None,
        max_rows: int = 2000,
    ) -> ScreenFetch:
        raise RuntimeError("exit replay cannot screen new candidates")

    def fetch_minute_bars(
        self, codes: Sequence[str], *, count: int | None = None
    ) -> Mapping[str, tuple[USOHLCVBar, ...]]:
        raise RuntimeError("exit replay cannot fetch new evidence")

    def history_kline_quota(self) -> Mapping[str, Any]:
        raise RuntimeError("exit replay cannot connect to OpenD")


class _RecordedRunner(USUniversePaperRunner):
    recorded_scan: USScanResult

    def scan(self) -> USScanResult:
        return self.recorded_scan


def read_fills(store: USUniversePaperStore, run_id: str) -> tuple[FillEvent, ...]:
    connection = sqlite3.connect(store.path)
    try:
        return tuple(
            FillEvent.model_validate(json.loads(payload)["fill"])
            for (payload,) in connection.execute(
                "SELECT payload_json FROM paper_fills WHERE run_id=? ORDER BY rowid", (run_id,)
            )
        )
    finally:
        connection.close()


async def replay_recorded_exit(
    entry: USPaperStepResult,
    entry_fills: Sequence[FillEvent],
    quotes: Sequence[USMarketSnapshot],
    config: USPaperConfig,
) -> ExitReplayResult:
    """Keep the saved entry fixed; run only exit intents through RiskEngine/PaperBroker.

    The caller supplies a fresh ledger and the original fee/stop/slippage settings.
    Quote gaps are respected: a time exit occurs at the first usable recorded quote,
    rather than fabricating a quote exactly at the desired holding horizon.
    """
    portfolio, scan = entry.portfolio, entry.scan
    if (
        entry.status != "FILLED"
        or entry.decision is not USDecisionKind.BUY
        or entry.risk_reason != "APPROVED"
        or portfolio is None
        or scan is None
        or entry.symbol is None
        or set(portfolio.positions) != {entry.symbol}
        or portfolio.portfolio_id != config.portfolio_id
    ):
        raise ValueError("exit replay requires one approved, saved Paper entry")
    if config.database_path.exists():
        raise ValueError("exit replay requires a fresh ledger")
    quantity = portfolio.positions[entry.symbol]
    opened = portfolio.position_entry_times[entry.symbol]
    if not entry_fills or any(
        fill.side is not Action.LONG
        or fill.instrument is None
        or fill.instrument.symbol != entry.symbol
        for fill in entry_fills
    ):
        raise ValueError("entry fills do not match the saved position")
    if sum(fill.quantity for fill in entry_fills) != quantity:
        raise ValueError("entry fill quantities do not match the saved position")
    if sum((fill.fees for fill in entry_fills), Decimal(0)) != portfolio.total_fees_usd:
        raise ValueError("exit replay requires an isolated entry portfolio")
    code = f"US.{entry.symbol}"
    if any(quote.code != code for quote in quotes):
        raise ValueError("exit quotes must match the saved entry symbol")
    times = [quote.update_time for quote in quotes]
    if any(at <= entry.as_of for at in times) or times != sorted(set(times)):
        raise ValueError("exit quotes must be unique, strictly ordered and after entry")
    store = USUniversePaperStore(config.database_path)
    store.save_portfolio(portfolio)
    attempts: list[USPaperStepResult] = []
    exits: list[FillEvent] = []
    for quote in quotes:
        runner = _RecordedRunner(
            config,
            store,
            market_source_factory=lambda quote=quote: _RecordedMarket(quote),
            jev_client=None,
            clock=FixedClock(quote.update_time),
        )
        runner.recorded_scan = scan.model_copy(
            update={
                "run_id": str(uuid4()),
                "as_of": quote.update_time,
                "snapshots": {code: quote},
                "health": {},
                "opinion_as_of": {},
            }
        )
        result = await runner.paper_step(dry_run=True)
        attempts.append(result)
        if result.status != "FILLED":
            continue
        if result.decision is not USDecisionKind.SELL or result.risk_reason != "APPROVED":
            raise ValueError("exit replay produced an unexpected entry or unapproved fill")
        if result.portfolio is None:
            raise ValueError("exit fill has no resulting portfolio")
        exits.extend(read_fills(store, result.run_id))
        portfolio = result.portfolio
        # dry_run does not advance the ledger. Include every partial sale explicitly.
        store.save_portfolio(portfolio)
        if not portfolio.positions:
            break
    outcome = ExitReplayResult(
        max_holding_seconds=config.max_holding_seconds,
        closed=not portfolio.positions,
        portfolio=portfolio,
        attempts=tuple(attempts),
        exit_fills=tuple(exits),
    )
    if not outcome.closed:
        return outcome
    if sum(fill.quantity for fill in exits) != quantity or any(
        fill.side is not Action.SHORT for fill in exits
    ):
        raise ValueError("exit fills do not close exactly the saved entry")
    fees = sum((fill.fees for fill in (*entry_fills, *exits)), Decimal(0))
    net = (
        sum((fill.price * fill.quantity for fill in exits), Decimal(0))
        - sum((fill.price * fill.quantity for fill in entry_fills), Decimal(0))
        - fees
    )
    gross = sum(
        (Decimal(str(fill.metadata["base_price"])) * fill.quantity for fill in exits), Decimal(0)
    ) - sum(
        (Decimal(str(fill.metadata["base_price"])) * fill.quantity for fill in entry_fills),
        Decimal(0),
    )
    slippage = sum((fill.slippage * fill.quantity for fill in entry_fills), Decimal(0)) - sum(
        (fill.slippage * fill.quantity for fill in exits), Decimal(0)
    )
    if net != portfolio.realized_pnl_usd or fees != portfolio.total_fees_usd:
        raise ValueError("replayed fill arithmetic disagrees with the portfolio")
    if net != gross - fees - slippage:
        raise ValueError("replayed cost decomposition does not reconcile")
    overshoot = max(
        Decimal(0),
        Decimal(str((exits[-1].occurred_at - opened).total_seconds())) - config.max_holding_seconds,
    )
    return outcome.model_copy(
        update={
            "net_pnl_usd": net,
            "gross_quote_pnl_usd": gross,
            "fees_usd": fees,
            "slippage_cost_usd": slippage,
            "maximum_holding_overshoot_seconds": overshoot,
        }
    )
