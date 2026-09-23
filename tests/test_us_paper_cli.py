from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trader_jev.clock import FixedClock
from trader_jev.us_equity import (
    USMarketSnapshot,
    USPaperPortfolio,
    USScreenRow,
    USUniverseListing,
    USUniversePaperStore,
)
from trader_jev.us_moomoo import ScreenFetch, USMoomooError
from trader_jev.us_paper_cli import (
    USPaperConfig,
    USUniversePaperRunner,
    parse_jev_opinion,
)

NOW = datetime(2026, 9, 24, 13, 30, tzinfo=ZoneInfo("America/New_York"))
CODE = "US.AAPL"


def test_noul_does_not_fabricate_confidence() -> None:
    opinion = parse_jev_opinion(_jev_response(noul_confidence=True))

    assert opinion.abnormal_probability == Decimal("0.1")
    assert opinion.trade_worthy_probability == Decimal("0.9")
    assert opinion.min_confidence == Decimal("0.8")
    raw_trade_worthy = opinion.raw_response["answers"]["trade_worthy"]
    assert raw_trade_worthy["confidence"] == 0.01


@pytest.mark.asyncio
async def test_dry_run_simulates_through_paper_broker_without_saving_positions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    store = USUniversePaperStore(config.database_path)
    jev = _FakeJevClient()
    runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=_market_factory,
        jev_client=jev,
        clock=FixedClock(NOW),
    )

    result = await runner.paper_step(dry_run=True)

    assert result.status == "FILLED"
    assert result.decision.value == "BUY"
    assert result.symbol == "AAPL"
    paper_portfolio = result.portfolio
    assert paper_portfolio is not None
    assert paper_portfolio.positions["AAPL"] > 0
    assert paper_portfolio.fx_source == "test FX source"
    assert store.load_portfolio(config.portfolio_id) is None
    assert jev.calls == 1


@pytest.mark.asyncio
async def test_paper_step_persists_usd_position_and_fx_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    store = USUniversePaperStore(config.database_path)
    runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=_market_factory,
        jev_client=_FakeJevClient(),
        clock=FixedClock(NOW),
    )

    result = await runner.paper_step()
    portfolio = store.load_portfolio(config.portfolio_id)
    simulated_portfolio = result.portfolio

    assert result.status == "FILLED"
    assert portfolio is not None
    assert simulated_portfolio is not None
    assert portfolio.positions["AAPL"] == simulated_portfolio.positions["AAPL"]
    assert portfolio.usd_jpy_rate == Decimal("150")
    assert portfolio.initial_usd_jpy_rate == Decimal("150")
    assert portfolio.fx_source == "test FX source"
    assert portfolio.fx_as_of == "2026-09-24T13:00:00-04:00"
    assert portfolio.cash_jpy == Decimal("10000.0")


@pytest.mark.asyncio
async def test_paper_step_preserves_persisted_fx_provenance_when_config_has_no_rate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "paper.sqlite3").model_copy(
        update={"usd_jpy_rate": None, "fx_as_of": None, "fx_source": None}
    )
    store = USUniversePaperStore(config.database_path)
    store.save_portfolio(
        USPaperPortfolio.initial(
            portfolio_id=config.portfolio_id,
            initial_cash_jpy=Decimal("100000"),
            usd_jpy_rate=Decimal("150"),
            cash_reserve_pct=Decimal("0.1"),
            at=NOW,
            fx_as_of="2026-09-24T12:00:00-04:00",
            fx_source="manual rate sheet",
        )
    )
    runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=_market_factory,
        jev_client=_FakeJevClient(),
        clock=FixedClock(NOW),
    )

    result = await runner.paper_step(dry_run=True)

    assert result.portfolio is not None
    assert result.portfolio.fx_source == "manual rate sheet"
    assert result.portfolio.fx_as_of == "2026-09-24T12:00:00-04:00"


@pytest.mark.asyncio
async def test_missing_fx_fails_closed_before_market_or_jev_calls(tmp_path: Path) -> None:
    config = USPaperConfig(database_path=tmp_path / "paper.sqlite3", usd_jpy_rate=None)
    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=lambda: _UnexpectedMarketSource(),
        clock=FixedClock(NOW),
    )

    result = await runner.paper_step()

    assert result.status == "NO_TRADE"
    assert result.decision.value == "NO TRADE"
    assert result.reason.startswith("USD/JPY rate is required")


def test_config_rejects_inconsistent_final_blend_weights(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must equal 1"):
        USPaperConfig(
            database_path=tmp_path / "paper.sqlite3",
            alpha_quant=Decimal("0.5"),
            beta_jev=Decimal("0.6"),
        )


def _config(path: Path) -> USPaperConfig:
    return USPaperConfig(
        database_path=path,
        usd_jpy_rate=Decimal("150"),
        fx_as_of="2026-09-24T13:00:00-04:00",
        fx_source="test FX source",
        max_screen_rows=10,
        lane_top_k=1,
        union_limit=1,
        minute_bar_candidates=0,
        jev_candidates=1,
        max_positions=1,
    )


def _market_factory() -> _FakeMarketSource:
    return _FakeMarketSource()


class _FakeMarketSource:
    def __init__(self) -> None:
        self.errors: list[USMoomooError] = []

    def __enter__(self) -> _FakeMarketSource:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    def fetch_universe(self, *, include_etf: bool) -> tuple[USUniverseListing, ...]:
        assert not include_etf
        return (
            USUniverseListing(
                code=CODE,
                name="Apple Inc.",
                exchange="US_NASDAQ",
                listing_date=datetime(1980, 12, 12).date(),
                security_type="STOCK",
            ),
        )

    def screen_us(
        self,
        *,
        min_price_usd: Decimal,
        min_market_cap_usd: Decimal,
        min_avg_turnover_20d_usd: Decimal,
        min_listing_days: int | None,
        max_rows: int = 2000,
    ) -> ScreenFetch:
        del min_price_usd, min_market_cap_usd, min_avg_turnover_20d_usd, min_listing_days, max_rows
        return ScreenFetch((_screen_row(),), 1, False, 1)

    def fetch_snapshots(self, codes: Sequence[str]) -> Mapping[str, USMarketSnapshot]:
        return {code: _snapshot(code) for code in codes}

    def fetch_minute_bars(
        self, codes: Sequence[str], *, count: int | None = None
    ) -> Mapping[str, tuple[Any, ...]]:
        del codes, count
        return {}

    def history_kline_quota(self) -> Mapping[str, Any]:
        return {"used_quota": 0, "remain_quota": 100}


class _UnexpectedMarketSource(_FakeMarketSource):
    def __enter__(self) -> _FakeMarketSource:
        raise AssertionError("paper step should stop before opening the market source")


class _FakeJevClient:
    def __init__(self) -> None:
        self.calls = 0

    async def ask(
        self,
        request: Any,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        assert request.symbol == "AAPL"
        assert questions["trade_worthy"]["type"] == "noul"
        self.calls += 1
        return _jev_response()


def _jev_response(*, noul_confidence: bool = False) -> dict[str, Any]:
    trade_worthy: dict[str, Any] = {"type": "noul", "noul": 0.9}
    if noul_confidence:
        trade_worthy["confidence"] = 0.01
    return {
        "model": "jev-test",
        "answers": {
            "setup_type": {
                "type": "choice",
                "choice": "TREND_CONTINUATION",
                "probabilities": {"TREND_CONTINUATION": 0.8, "NO_SETUP": 0.2},
                "confidence": 0.85,
            },
            "trend_quality": {
                "type": "score",
                "score": 0.9,
                "probabilities": {"0.9": 0.8, "0.5": 0.2},
                "confidence": 0.8,
            },
            "continuation_quality": {
                "type": "score",
                "score": 0.9,
                "probabilities": {"0.9": 0.8, "0.5": 0.2},
                "confidence": 0.82,
            },
            "abnormal_activity": {"type": "noul", "noul": 0.1},
            "trade_worthy": trade_worthy,
        },
    }


def _screen_row() -> USScreenRow:
    return USScreenRow(
        code=CODE,
        name="Apple Inc.",
        price=Decimal("20"),
        market_cap_usd=Decimal("1000000000"),
        listed_days=500,
        volume_ratio=Decimal("2"),
        avg_volume_20d=Decimal("1000000"),
        avg_turnover_20d_usd=Decimal("50000000"),
        price_change_1d=Decimal("0.02"),
        price_change_5d=Decimal("0.04"),
        amplitude_1d=Decimal("0.03"),
        high_to_20d_high=Decimal("-0.01"),
        low_to_20d_low=Decimal("0.1"),
    )


def _snapshot(code: str) -> USMarketSnapshot:
    return USMarketSnapshot(
        code=code,
        update_time=NOW.replace(second=0),
        last_price=Decimal("20"),
        open_price=Decimal("19.5"),
        high_price=Decimal("20.1"),
        low_price=Decimal("19.4"),
        prev_close_price=Decimal("19.6"),
        volume=Decimal("100000"),
        turnover=Decimal("2000000"),
        bid_price=Decimal("19.99"),
        ask_price=Decimal("20.01"),
        bid_volume=Decimal("100"),
        ask_volume=Decimal("100"),
    )
