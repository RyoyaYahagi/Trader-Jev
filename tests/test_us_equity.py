from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader_jev.us_equity import (
    USMarketSnapshot,
    USOHLCVBar,
    USPaperPortfolio,
    USScreenRow,
    USUniverseListing,
    USUniversePaperStore,
    generate_quant_features,
    hard_filter_rows,
    lane_scores,
    lane_union,
    percentile_ranks,
    rank_quant_candidates,
)

NOW = datetime(2026, 9, 24, 17, 30, tzinfo=UTC)


def test_hard_filters_reject_missing_data_delisted_etf_and_otc() -> None:
    rows = (
        _row("US.AAA"),
        _row("US.BBB"),
        _row("US.CCC"),
        _row("US.DDD", market_cap_usd=None),
        _row("US.EEE", listed_days=10),
    )
    listings = {
        "US.AAA": _listing("US.AAA"),
        "US.BBB": _listing("US.BBB", delisted=True),
        "US.CCC": _listing("US.CCC", exchange="US_OTC"),
        "US.DDD": _listing("US.DDD"),
        "US.EEE": _listing("US.EEE"),
    }

    accepted, rejected = hard_filter_rows(
        rows,
        listings,
        min_price_usd=Decimal("3"),
        min_market_cap_usd=Decimal("300000000"),
        min_avg_turnover_20d_usd=Decimal("10000000"),
        min_listing_days=90,
        include_etf=False,
        include_otc=False,
        as_of=NOW,
    )

    assert [row.code for row in accepted] == ["US.AAA"]
    assert rejected["US.BBB"] == ("delisted",)
    assert rejected["US.CCC"] == ("unsupported_exchange",)
    assert "min_market_cap" in rejected["US.DDD"]
    assert "min_listing_days" in rejected["US.EEE"]


def test_percentiles_average_ties_and_keep_missing_out_of_the_ranked_group() -> None:
    ranks = percentile_ranks(
        {"low": Decimal("1"), "tie_a": Decimal("2"), "tie_b": Decimal("2"), "missing": None}
    )

    assert ranks["low"] == Decimal("0")
    assert ranks["tie_a"] == ranks["tie_b"] == Decimal("0.75")
    assert ranks["missing"] == Decimal("0")


def test_lane_union_deduplicates_and_ranking_reports_actual_lane_membership() -> None:
    rows = tuple(
        _row(
            f"US.{symbol}",
            volume_ratio=Decimal(value),
            avg_turnover_20d_usd=Decimal(turnover),
        )
        for symbol, value, turnover in (
            ("AAA", "4", "90000000"),
            ("BBB", "3", "80000000"),
            ("CCC", "2", "70000000"),
        )
    )
    scores = lane_scores(rows)
    union, membership = lane_union(rows, scores, lane_top_k=1, union_limit=3)
    snapshots = {row.code: _snapshot(row.code) for row in union}
    features = {row.code: generate_quant_features(row, snapshots[row.code]) for row in union}
    ranked = rank_quant_candidates(
        union,
        features,
        scores,
        count=3,
        lane_membership=membership,
    )

    assert len(union) == len({row.code for row in union})
    assert ranked[0].code == "US.AAA"
    assert ranked[0].screening_lanes
    assert all("momentum" not in candidate.screening_lanes for candidate in ranked[1:])


def test_feature_generation_keeps_unavailable_minute_features_null() -> None:
    row = _row("US.AAA")
    snapshot = _snapshot("US.AAA")

    features = generate_quant_features(row, snapshot)

    assert features.return_1d == Decimal("0.01")
    assert features.return_5m is None
    assert features.realized_volatility is None
    assert "return_5m" in features.unavailable


def test_minute_feature_generation_uses_only_bars_at_or_before_snapshot() -> None:
    row = _row("US.AAA")
    snapshot = _snapshot("US.AAA")
    bars = (
        _bar(NOW - timedelta(minutes=2), "10"),
        _bar(NOW - timedelta(minutes=1), "11"),
        _bar(NOW + timedelta(minutes=1), "200"),
    )

    features = generate_quant_features(row, snapshot, bars)

    assert features.return_1m == Decimal("0.1")
    assert features.last_price == snapshot.last_price


def test_minute_features_use_only_current_regular_session_for_vwap() -> None:
    row = _row("US.AAA")
    snapshot = _snapshot("US.AAA")
    bars = (
        _bar(NOW - timedelta(days=1, minutes=2), "1"),
        _bar(NOW - timedelta(hours=4, minutes=1), "100"),
        _bar(NOW - timedelta(minutes=2), "10"),
        _bar(NOW - timedelta(minutes=1), "20"),
        _bar(NOW + timedelta(minutes=1), "200"),
    )

    features = generate_quant_features(row, snapshot, bars)

    assert features.return_1m == Decimal("1")
    assert features.distance_from_vwap == snapshot.last_price / Decimal("15") - Decimal("1")


def test_store_round_trips_universe_portfolio_and_bars(tmp_path: Path) -> None:
    store = USUniversePaperStore(tmp_path / "paper.sqlite3")
    listing = _listing("US.AAA")
    store.record_universe((listing,), snapshot_id="universe-1", recorded_at=NOW)
    cached = (_bar(NOW - timedelta(minutes=1), "10"),)
    store.save_cached_bars("US.AAA", cached, interval="1m", cached_at=NOW)
    portfolio = USPaperPortfolio.initial(
        initial_cash_jpy=Decimal("100000"),
        usd_jpy_rate=Decimal("150"),
        cash_reserve_pct=Decimal("0.1"),
        at=NOW,
        fx_source="test fixture",
    )
    store.save_portfolio(portfolio)

    latest = store.load_latest_universe()
    restored_bars = store.load_cached_bars("US.AAA", interval="1m", now=NOW, max_age_seconds=60)
    store.save_cached_bars("US.BBB", cached, interval="1m", cached_at=NOW + timedelta(seconds=1))
    future_bars = store.load_cached_bars("US.BBB", interval="1m", now=NOW, max_age_seconds=60)

    assert latest is not None
    assert latest[2] == (listing,)
    assert restored_bars == cached
    assert future_bars is None
    assert store.load_portfolio(portfolio.portfolio_id) == portfolio


def _row(
    code: str,
    *,
    price: Decimal | None = Decimal("20"),
    market_cap_usd: Decimal | None = Decimal("1000000000"),
    listed_days: int = 400,
    volume_ratio: Decimal | None = Decimal("2"),
    avg_turnover_20d_usd: Decimal | None = Decimal("50000000"),
) -> USScreenRow:
    return USScreenRow(
        code=code,
        name=code.partition(".")[2],
        price=price,
        market_cap_usd=market_cap_usd,
        listed_days=listed_days,
        volume_ratio=volume_ratio,
        avg_volume_20d=Decimal("1000000"),
        avg_turnover_20d_usd=avg_turnover_20d_usd,
        price_change_1d=Decimal("0.01"),
        price_change_5d=Decimal("0.03"),
        amplitude_1d=Decimal("0.04"),
        high_to_20d_high=Decimal("-0.02"),
        low_to_20d_low=Decimal("0.08"),
    )


def _listing(
    code: str,
    *,
    exchange: str = "US_NASDAQ",
    delisted: bool = False,
    security_type: str = "STOCK",
) -> USUniverseListing:
    return USUniverseListing(
        code=code,
        name=code.partition(".")[2],
        exchange=exchange,
        lot_size=1,
        delisted=delisted,
        security_type=security_type,
    )


def _snapshot(code: str) -> USMarketSnapshot:
    return USMarketSnapshot(
        code=code,
        update_time=NOW,
        last_price=Decimal("20.2"),
        open_price=Decimal("20"),
        high_price=Decimal("20.5"),
        low_price=Decimal("19.8"),
        prev_close_price=Decimal("20"),
        volume=Decimal("100000"),
        turnover=Decimal("2000000"),
        bid_price=Decimal("20.19"),
        ask_price=Decimal("20.21"),
        bid_volume=Decimal("100"),
        ask_volume=Decimal("120"),
    )


def _bar(timestamp: datetime, close: str) -> USOHLCVBar:
    value = Decimal(close)
    return USOHLCVBar(
        timestamp=timestamp,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("100"),
        turnover=value * Decimal("100"),
    )
