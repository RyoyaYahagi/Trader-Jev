from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader_jev.models import (
    Action,
    DecisionSnapshot,
    InstrumentMetadata,
    MarketState,
    PortfolioState,
)
from trader_jev.portfolio import ExitMode, HybridExitPolicy

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)


def snapshot_for(instrument: InstrumentMetadata, mid: str) -> DecisionSnapshot:
    price = Decimal(mid)
    return DecisionSnapshot(
        instrument=instrument,
        event_time=NOW,
        as_of=NOW,
        market=MarketState(
            bid=price - Decimal("0.01"),
            ask=price + Decimal("0.01"),
            mid=price,
            spread=Decimal("0.02"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            last_event_time=NOW,
            last_received_at=NOW,
        ),
        technical={"atr": 2.0},
    )


def test_atr_exit_uses_one_atr_stop_and_one_point_five_r_target(
    instrument: InstrumentMetadata,
) -> None:
    policy = HybridExitPolicy(
        max_holding=timedelta(minutes=15),
        mode=ExitMode.ATR,
        stop_atr_multiple=Decimal("1.0"),
        take_profit_r_multiple=Decimal("1.5"),
    )
    portfolio = PortfolioState(
        portfolio_id="paper",
        cash=Decimal("10000"),
        positions={"TEST": 1},
        average_prices={"TEST": Decimal("100")},
        position_entry_times={"TEST": NOW - timedelta(minutes=1)},
    )

    stop = policy.evaluate(snapshot_for(instrument, "98"), portfolio)
    target = policy.evaluate(snapshot_for(instrument, "103"), portfolio)

    assert stop is not None
    assert stop.action is Action.SHORT
    assert stop.reason == "STOP_LOSS"
    assert stop.metadata["exit_threshold_source"] == "ATR:atr"
    assert stop.metadata["stop_distance"] == "2.00"
    assert stop.metadata["take_profit_distance"] == "3.000"
    assert target is not None
    assert target.reason == "TAKE_PROFIT"
