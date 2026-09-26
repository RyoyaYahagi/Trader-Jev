from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader_jev.universe_dashboard import UniverseDashboardStore
from trader_jev.us_equity import USPaperPortfolio, USUniversePaperStore


def test_performance_reports_latest_saved_paper_portfolio(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    paper_store = USUniversePaperStore(path)
    dashboard_store = UniverseDashboardStore(path)
    assert dashboard_store.performance() == {"available": False}
    portfolio = USPaperPortfolio.initial(
        initial_cash_jpy=Decimal("100000"),
        usd_jpy_rate=Decimal("150"),
        cash_reserve_pct=Decimal("0.1"),
        at=datetime(2026, 9, 25, 13, 30, tzinfo=UTC),
    ).model_copy(
        update={
            "realized_pnl_usd": Decimal("5"),
            "unrealized_pnl_usd": Decimal("2"),
            "total_fees_usd": Decimal("0.25"),
            "positions": {"AAPL": 2},
            "market_prices_usd": {"AAPL": Decimal("110")},
        }
    )
    paper_store.save_portfolio(portfolio)

    performance = dashboard_store.performance()

    assert performance["available"] is True
    assert performance["net_pnl_usd"] == "7"
    assert performance["realized_pnl_usd"] == "5"
    assert performance["unrealized_pnl_usd"] == "2"
    assert performance["fees_usd"] == "0.25"
    assert performance["position_count"] == 1
    assert Decimal(performance["return_ratio"]) == Decimal("0.0105")


def test_dashboard_store_closes_read_only_connections(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    paper_store = USUniversePaperStore(path)
    paper_store.save_portfolio(
        USPaperPortfolio.initial(
            initial_cash_jpy=Decimal("100000"),
            usd_jpy_rate=Decimal("150"),
            cash_reserve_pct=Decimal("0.1"),
            at=datetime(2026, 9, 25, 13, 30, tzinfo=UTC),
        )
    )
    dashboard_store = UniverseDashboardStore(path)
    fd_dir = Path("/proc/self/fd")
    fd_count_before = len(os.listdir(fd_dir)) if fd_dir.is_dir() else None

    for _ in range(100):
        assert dashboard_store.performance()["available"] is True

    if fd_count_before is not None:
        assert len(os.listdir(fd_dir)) <= fd_count_before + 4


def test_performance_omits_return_when_starting_capital_is_zero(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    paper_store = USUniversePaperStore(path)
    paper_store.save_portfolio(
        USPaperPortfolio.initial(
            initial_cash_jpy=Decimal("0"),
            usd_jpy_rate=Decimal("150"),
            cash_reserve_pct=Decimal("0"),
            at=datetime(2026, 9, 25, 13, 30, tzinfo=UTC),
        )
    )

    performance = UniverseDashboardStore(path).performance()

    assert performance["available"] is True
    assert performance["return_ratio"] is None
