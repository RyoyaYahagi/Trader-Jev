from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trader_jev.clock import FixedClock
from trader_jev.us_equity import (
    USDecisionKind,
    USMarketSnapshot,
    USOHLCVBar,
    USPaperPortfolio,
    USScreenRow,
    USUniverseListing,
    USUniversePaperStore,
)
from trader_jev.us_moomoo import ScreenFetch, USMoomooError
from trader_jev.us_paper_cli import (
    USPaperConfig,
    USPaperRunSummary,
    USPaperStepResult,
    USScanResult,
    USUniversePaperRunner,
    _print_json,  # pyright: ignore[reportPrivateUsage]
    build_parser,
    parse_jev_opinion,
)

NOW = datetime(2026, 9, 24, 13, 30, tzinfo=ZoneInfo("America/New_York"))
CODE = "US.AAPL"


def test_print_json_serializes_scan_result_nested_in_mapping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scan = USScanResult(
        run_id="scan-1",
        as_of=NOW,
        universe_count=1,
        screened_count=1,
        hard_filter_rejected_count=0,
        lane_union_count=1,
        snapshot_count=1,
        minute_bar_count=0,
        candidate_count=1,
    )

    _print_json({"scan": scan, "opinions": {}})

    result = json.loads(capsys.readouterr().out)
    assert result["scan"]["run_id"] == "scan-1"
    assert result["scan"]["as_of"] == NOW.isoformat()


def test_noul_does_not_fabricate_confidence() -> None:
    opinion = parse_jev_opinion(_jev_response(noul_confidence=True))

    assert opinion.abnormal_probability == Decimal("0.1")
    assert opinion.trade_worthy_probability == Decimal("0.9")
    assert opinion.min_confidence == Decimal("0.8")
    raw_trade_worthy = opinion.raw_response["answers"]["trade_worthy"]
    assert raw_trade_worthy["confidence"] == 0.01


def test_jev_rejects_legacy_boolean_probability_shape() -> None:
    response = _jev_response()
    answers = response["answers"]
    answers["trade_worthy"] = {"type": "boolean", "probability": 0.9}

    with pytest.raises(ValueError, match="no Noul probability"):
        parse_jev_opinion(response)


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


def test_universe_refresh_defaults_to_weekly() -> None:
    config = USPaperConfig()

    assert config.universe_refresh_days == 7
    assert config.screen_refresh_interval_seconds == 300
    assert config.minute_bar_refresh_interval_seconds == 60
    assert config.decision_interval_seconds == 30


def test_scan_reuses_screen_results_but_refreshes_snapshots_every_step(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    clock = _MutableClock(NOW)
    stats: dict[str, Any] = {"screen_calls": 0, "snapshot_calls": 0}
    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=lambda: _CountingMarketSource(stats),
        clock=clock,
    )

    first = runner.scan()
    clock.advance(30)
    second = runner.scan()
    clock.advance(270)
    third = runner.scan()

    assert stats["screen_calls"] == 2
    assert stats["snapshot_calls"] == 3
    assert not first.screen_cache_hit
    assert second.screen_cache_hit
    assert second.screen_refreshed_at == NOW
    assert not third.screen_cache_hit


def test_new_runner_reuses_recently_persisted_screen_results(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    store = USUniversePaperStore(config.database_path)
    clock = _MutableClock(NOW)
    first_stats: dict[str, Any] = {}
    first_runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=lambda: _CountingMarketSource(first_stats),
        clock=clock,
    )
    original_scan = first_runner.scan()

    clock.advance(3600)
    resumed_stats: dict[str, Any] = {}
    resumed_runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=lambda: _CountingMarketSource(resumed_stats),
        clock=clock,
    )
    resumed_scan = resumed_runner.scan()

    assert original_scan.screen_refreshed_at == NOW
    assert resumed_scan.screen_cache_hit
    assert resumed_scan.screen_refreshed_at == NOW
    assert resumed_stats["screen_calls"] == 0
    assert resumed_stats["snapshot_calls"] == 1


def test_new_runner_discards_expired_persisted_screen_results(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3").model_copy(
        update={"startup_screen_cache_max_age_seconds": 3600}
    )
    store = USUniversePaperStore(config.database_path)
    clock = _MutableClock(NOW)
    first_runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=lambda: _CountingMarketSource({}),
        clock=clock,
    )
    first_runner.scan()

    clock.advance(3601)
    refreshed_stats: dict[str, Any] = {}
    refreshed_runner = USUniversePaperRunner(
        config,
        store,
        market_source_factory=lambda: _CountingMarketSource(refreshed_stats),
        clock=clock,
    )
    refreshed_scan = refreshed_runner.scan()

    assert not refreshed_scan.screen_cache_hit
    assert refreshed_stats["screen_calls"] == 1


@pytest.mark.asyncio
async def test_paper_run_stops_at_session_close_without_opening_market_source(
    tmp_path: Path,
) -> None:
    closed_at = datetime(2026, 9, 24, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    runner = USUniversePaperRunner(
        _config(tmp_path / "paper.sqlite3"),
        USUniversePaperStore(tmp_path / "paper.sqlite3"),
        market_source_factory=lambda: _UnexpectedMarketSource(),
        clock=FixedClock(closed_at),
    )

    result = await runner.paper_run(steps=None, dry_run=False, until_market_close=True)

    assert isinstance(result, USPaperRunSummary)
    assert result.steps_completed == 0
    assert result.stop_reason == "market_closed"


@pytest.mark.asyncio
async def test_paper_run_until_close_honors_optional_step_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=_market_factory,
        clock=FixedClock(NOW),
    )

    async def paper_step_stub(
        *,
        dry_run: bool = False,
        usd_jpy_rate: Decimal | None = None,
        usd_jpy_as_of: str | None = None,
        usd_jpy_source: str | None = None,
    ) -> USPaperStepResult:
        del dry_run, usd_jpy_rate, usd_jpy_as_of, usd_jpy_source
        return USPaperStepResult(
            run_id="test-run",
            as_of=NOW,
            dry_run=True,
            status="NO_TRADE",
            decision=USDecisionKind.NO_TRADE,
            reason="test",
        )

    async def unexpected_sleep(_seconds: float) -> None:
        pytest.fail("step-limited run should return without sleeping")

    monkeypatch.setattr(runner, "paper_step", paper_step_stub)
    result = await runner.paper_run(
        steps=1,
        dry_run=True,
        until_market_close=True,
        sleep=unexpected_sleep,
    )

    assert isinstance(result, USPaperRunSummary)
    assert result.steps_completed == 1
    assert result.last_run_id == "test-run"
    assert result.stop_reason == "step_limit"


def test_paper_run_parser_accepts_nightly_session_options() -> None:
    args = build_parser().parse_args(
        [
            "paper-run",
            "--until-market-close",
            "--usd-jpy",
            "157.92",
            "--usd-jpy-as-of",
            "2026-09-24T10:13:47+09:00",
            "--usd-jpy-source",
            "Investing.com USD/JPY real-time quote",
        ]
    )

    assert args.until_market_close
    assert args.usd_jpy == Decimal("157.92")
    assert args.usd_jpy_as_of == "2026-09-24T10:13:47+09:00"
    assert args.usd_jpy_source == "Investing.com USD/JPY real-time quote"


def test_failed_screen_refresh_uses_last_successful_cache_without_retry_storm(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    clock = _MutableClock(NOW)
    stats: dict[str, Any] = {"screen_calls": 0, "snapshot_calls": 0}
    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=lambda: _CountingMarketSource(stats, fail_screen_calls={2}),
        clock=clock,
    )

    first = runner.scan()
    clock.advance(300)
    failed_refresh = runner.scan()
    clock.advance(30)
    cached_fallback = runner.scan()

    assert first.candidate_count == 1
    assert failed_refresh.candidate_count == 0
    assert cached_fallback.candidate_count == 1
    assert cached_fallback.screen_cache_hit
    assert cached_fallback.screen_refreshed_at == NOW
    assert stats["screen_calls"] == 2


def test_scan_fetches_each_minute_bar_only_after_its_cache_expires(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3").model_copy(update={"minute_bar_candidates": 1})
    clock = _MutableClock(NOW)
    stats: dict[str, Any] = {"screen_calls": 0, "snapshot_calls": 0, "bar_refreshes": []}
    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=lambda: _CountingMarketSource(stats),
        clock=clock,
    )

    runner.scan()
    clock.advance(30)
    runner.scan()
    clock.advance(30)
    runner.scan()

    assert stats["bar_refreshes"] == [
        ((CODE,), (CODE,)),
        ((CODE,), ()),
        ((CODE,), (CODE,)),
    ]


@pytest.mark.asyncio
async def test_paper_run_keeps_one_market_context_open_across_steps(tmp_path: Path) -> None:
    config = _config(tmp_path / "paper.sqlite3")
    stats: dict[str, Any] = {"created": 0, "entered": 0, "exited": 0}
    jev = _FakeJevClient()

    def market_factory() -> _CountingMarketSource:
        stats["created"] += 1
        return _CountingMarketSource(stats)

    async def no_sleep(_seconds: float) -> None:
        return None

    runner = USUniversePaperRunner(
        config,
        USUniversePaperStore(config.database_path),
        market_source_factory=market_factory,
        jev_client=jev,
        clock=FixedClock(NOW),
    )

    results = await runner.paper_run(steps=2, dry_run=True, sleep=no_sleep)

    assert not isinstance(results, USPaperRunSummary)
    assert len(results) == 2
    assert jev.calls == 2
    assert stats["created"] == 1
    assert stats["entered"] == 1
    assert stats["exited"] == 1


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


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


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


class _CountingMarketSource(_FakeMarketSource):
    def __init__(
        self,
        stats: dict[str, Any],
        *,
        fail_screen_calls: set[int] | None = None,
    ) -> None:
        self.stats = stats
        self.fail_screen_calls = fail_screen_calls or set()
        self.errors: list[USMoomooError] = []
        self.stats.setdefault("screen_calls", 0)
        self.stats.setdefault("snapshot_calls", 0)
        self.stats.setdefault("bar_refreshes", [])

    def __enter__(self) -> _CountingMarketSource:
        self.stats["entered"] = self.stats.get("entered", 0) + 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.stats["exited"] = self.stats.get("exited", 0) + 1

    def screen_us(
        self,
        *,
        min_price_usd: Decimal,
        min_market_cap_usd: Decimal,
        min_avg_turnover_20d_usd: Decimal,
        min_listing_days: int | None,
        max_rows: int = 2000,
    ) -> ScreenFetch:
        self.stats["screen_calls"] += 1
        if self.stats["screen_calls"] in self.fail_screen_calls:
            raise USMoomooError("RATE_LIMIT", "simulated screener rate limit")
        return super().screen_us(
            min_price_usd=min_price_usd,
            min_market_cap_usd=min_market_cap_usd,
            min_avg_turnover_20d_usd=min_avg_turnover_20d_usd,
            min_listing_days=min_listing_days,
            max_rows=max_rows,
        )

    def fetch_snapshots(self, codes: Sequence[str]) -> Mapping[str, USMarketSnapshot]:
        self.stats["snapshot_calls"] += 1
        return super().fetch_snapshots(codes)

    def fetch_minute_bars_with_refresh(
        self,
        codes: Sequence[str],
        *,
        refresh_codes: Sequence[str],
        count: int | None = None,
    ) -> Mapping[str, tuple[USOHLCVBar, ...]]:
        del count
        self.stats["bar_refreshes"].append((tuple(codes), tuple(refresh_codes)))
        bar = USOHLCVBar(
            timestamp=NOW,
            open=Decimal("20"),
            high=Decimal("20.2"),
            low=Decimal("19.9"),
            close=Decimal("20.1"),
            volume=Decimal("1000"),
            turnover=Decimal("20100"),
        )
        return {code: (bar,) for code in refresh_codes}


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
