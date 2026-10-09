from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from trader_jev.fees import MoomooFeeSchedule
from trader_jev.models import InstrumentMetadata, Market, TradingSession
from trader_jev.us_equity import USMarketSnapshot, USOHLCVBar, USPaperPortfolio
from trader_jev.us_jev_context import execution_context, short_history

NOW = datetime(2026, 10, 8, 15, 0, 30, tzinfo=UTC)


def bar(minutes_ago: int, price: str, volume: str = "100") -> USOHLCVBar:
    price_value = Decimal(price)
    return USOHLCVBar(
        timestamp=NOW.replace(second=0) - timedelta(minutes=minutes_ago),
        open=price_value,
        high=price_value,
        low=price_value,
        close=price_value,
        volume=Decimal(volume),
        turnover=price_value * Decimal(volume),
    )


def test_history_excludes_incomplete_future_and_previous_session_bars() -> None:
    bars = [bar(i, str(21 - i), "100" if i <= 5 else "50") for i in range(11, 0, -1)]
    bars.extend([bar(0, "999"), bar(-1, "999"), bar(1440, "999")])
    history = short_history(tuple(reversed(bars)), NOW)
    assert len(history.bars) == 11
    assert history.bars[-1].close == Decimal("20")
    assert history.newest_bar_age_seconds == Decimal("30")
    assert history.return_5m == Decimal("20") / Decimal("15") - 1
    assert history.price_efficiency_5m == 1
    assert history.max_close_pullback_5m == 0
    assert history.volume_recent_to_previous_ratio == 2


@pytest.mark.parametrize("failure", ["gap", "stale"])
def test_missing_or_stale_history_does_not_fabricate_evidence(failure: str) -> None:
    bars = [bar(i, "20") for i in range(11, 0, -1)]
    if failure == "gap":
        bars = [b for b in bars if b.timestamp != bar(4, "20").timestamp]
    at = NOW + timedelta(minutes=1) if failure == "stale" else NOW
    history = short_history(bars, at)
    assert history.return_5m is None
    assert history.volume_recent_to_previous_ratio is None


def test_zero_volume_baseline_remains_unavailable() -> None:
    history = short_history([bar(i, "20", "100" if i <= 5 else "0") for i in range(10, 0, -1)], NOW)
    assert history.volume_previous_5m == 0
    assert history.volume_recent_to_previous_ratio is None


def snapshot() -> USMarketSnapshot:
    return USMarketSnapshot(
        code="US.TEST",
        update_time=NOW,
        last_price=Decimal("20"),
        bid_price=Decimal("19.99"),
        ask_price=Decimal("20.01"),
    )


def cost_context(portfolio: USPaperPortfolio | None) -> dict[str, object]:
    instrument = InstrumentMetadata(
        symbol="TEST",
        market=Market.US,
        currency="USD",
        timezone="America/New_York",
        tick_size=Decimal("0.01"),
        lot_size=1,
        trading_session=TradingSession(open_time=time(9, 30), close_time=time(16)),
        shortability=True,
    )
    return execution_context(
        snapshot(),
        instrument,
        portfolio,
        max_position_pct=Decimal("0.3"),
        slippage_bps=Decimal("10"),
        estimated_fee_bps=Decimal("13.2"),
        fee_schedule=MoomooFeeSchedule.MOOMOO_US_BASIC,
        fee_bps=Decimal("0"),
    )


def test_cost_context_contains_round_trip_fees_spread_and_slippage() -> None:
    portfolio = USPaperPortfolio.initial(
        initial_cash_jpy=Decimal("100000"),
        usd_jpy_rate=Decimal("150"),
        cash_reserve_pct=Decimal("0.1"),
        at=NOW,
    )
    context = cost_context(portfolio)
    assert context["planned_quantity"] == 8  # 30% investable USD equity cap, integer shares.
    assert context["estimated_entry_fee_usd"] == "0.22"
    assert context["estimated_exit_fee_usd"] == "0.22"
    assert Decimal(str(context["flat_price_round_trip_cost_bps"])) > 50
    assert context["cost_status"] == "ESTIMATED_AT_UNCHANGED_QUOTE"


def test_missing_portfolio_does_not_invent_costs_or_quantity() -> None:
    context = cost_context(None)
    assert context["planned_quantity"] is None
    assert context["flat_price_round_trip_cost_bps"] is None
    assert context["cost_status"] == "UNAVAILABLE"
