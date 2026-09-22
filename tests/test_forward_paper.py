from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader_jev.clock import FixedClock
from trader_jev.forward_paper import (
    CapitalScenario,
    ForwardDecisionMode,
    ForwardPaperConfig,
    ForwardPaperRunner,
    ParallelForwardPaperRunner,
    build_capital_scenarios,
    build_us_instruments,
)
from trader_jev.models import ExecutionMode, InstrumentMetadata, MarketEvent, QuoteEvent

NOW = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)


class FakeMarketData:
    def __init__(self, events: Sequence[MarketEvent]) -> None:
        self.events = tuple(events)
        self.instruments: tuple[InstrumentMetadata, ...] = ()

    async def stream(
        self,
        instruments: Sequence[InstrumentMetadata],
    ) -> AsyncIterator[MarketEvent]:
        self.instruments = tuple(instruments)
        for event in self.events:
            yield event


class FakeJevClient:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, request: object) -> dict[str, object]:
        del request
        self.calls += 1
        return {
            "action": "HOLD",
            "direction_5m": "FLAT",
            "regime": "RANGE",
            "confidence": "0.2",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }


def quote(
    instrument: InstrumentMetadata,
    *,
    event_time: datetime,
    mid: Decimal,
) -> QuoteEvent:
    return QuoteEvent(
        instrument=instrument,
        event_time=event_time,
        received_at=NOW,
        source="test-feed",
        bid=mid - Decimal("0.01"),
        ask=mid + Decimal("0.01"),
        bid_size=Decimal("1000"),
        ask_size=Decimal("1000"),
    )


@pytest.mark.asyncio
async def test_forward_paper_entry_and_session_exit_are_paper_only() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    market_data = FakeMarketData(
        (
            quote(instrument, event_time=NOW - timedelta(seconds=31), mid=Decimal("100")),
            quote(instrument, event_time=NOW, mid=Decimal("101")),
        )
    )
    runner = ForwardPaperRunner(
        ForwardPaperConfig(
            symbols=("AAPL",),
            initial_capital=Decimal("10000"),
            runtime_seconds=60,
            decision_cadence_seconds=0,
            max_positions=1,
            market_hours_only=False,
        ),
        market_data=market_data,
        clock=FixedClock(NOW),
    )

    summary = await runner.run()

    assert market_data.instruments == (instrument,)
    assert summary.status == "COMPLETED"
    assert summary.events_processed == 2
    assert summary.fills == 2
    assert summary.portfolio.positions == {}
    assert summary.approved_orders >= 2
    assert summary.run_config["execution_mode"] == ExecutionMode.PAPER.value
    assert runner.broker.orders
    assert all(order.execution_mode is ExecutionMode.PAPER for order in runner.broker.orders)


@pytest.mark.asyncio
async def test_forward_paper_fails_closed_when_no_quotes_arrive() -> None:
    market_data = FakeMarketData(())
    runner = ForwardPaperRunner(
        ForwardPaperConfig(symbols=("AAPL",), runtime_seconds=1),
        market_data=market_data,
        clock=FixedClock(NOW),
    )

    summary = await runner.run()

    assert summary.status == "FAILED"
    assert summary.events_processed == 0
    assert summary.errors == ("no quote events were received",)


def test_forward_paper_rejects_duplicate_symbols() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        ForwardPaperConfig(symbols=("AAPL", "aapl"))


@pytest.mark.asyncio
async def test_parallel_forward_paper_forks_one_stream_into_capital_scenarios() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    market_data = FakeMarketData(
        (
            quote(instrument, event_time=NOW - timedelta(seconds=31), mid=Decimal("100")),
            quote(instrument, event_time=NOW, mid=Decimal("101")),
        )
    )
    scenarios = (
        CapitalScenario(
            scenario_id="100k",
            label="10万制約",
            initial_capital=Decimal("100000"),
            capital_constraint=Decimal("100000"),
        ),
        CapitalScenario(
            scenario_id="250k",
            label="25万制約",
            initial_capital=Decimal("250000"),
            capital_constraint=Decimal("250000"),
        ),
    )
    runner = ParallelForwardPaperRunner(
        ForwardPaperConfig(
            symbols=("AAPL",),
            runtime_seconds=60,
            decision_cadence_seconds=0,
            max_positions=1,
            market_hours_only=False,
        ),
        scenarios=scenarios,
        market_data=market_data,
        clock=FixedClock(NOW),
    )

    summaries = await runner.run()

    assert market_data.instruments == (instrument,)
    assert [summary.run_config["scenario_label"] for summary in summaries] == [
        "10万制約",
        "25万制約",
    ]
    assert [summary.portfolio.initial_capital for summary in summaries] == [
        Decimal("100000"),
        Decimal("250000"),
    ]
    assert all(summary.status == "COMPLETED" for summary in summaries)
    assert summaries[0].fills == 2
    assert summaries[1].fills == 1
    assert summaries[1].portfolio.positions == {"AAPL": 1000}


def test_capital_scenario_parser_supports_requested_labels() -> None:
    scenarios = build_capital_scenarios(("10万", "25万", "50万", "制約なし"))

    assert [scenario.scenario_id for scenario in scenarios] == [
        "jpy-100k",
        "jpy-250k",
        "jpy-500k",
        "unconstrained",
    ]
    assert [scenario.initial_capital for scenario in scenarios[:3]] == [
        Decimal("634.96"),
        Decimal("1587.40"),
        Decimal("3174.80"),
    ]
    assert [scenario.jpy_capital for scenario in scenarios[:3]] == [
        Decimal("100000"),
        Decimal("250000"),
        Decimal("500000"),
    ]
    assert all(scenario.usd_jpy_rate == Decimal("157.49") for scenario in scenarios)
    assert scenarios[-1].capital_constraint is None
    assert scenarios[-1].initial_capital == Decimal("3174.80")
    assert scenarios[-1].jpy_capital == Decimal("500000")


@pytest.mark.asyncio
async def test_parallel_forward_paper_runs_rule_and_jev_branches_independently() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    market_data = FakeMarketData(
        (
            quote(instrument, event_time=NOW - timedelta(seconds=31), mid=Decimal("100")),
            quote(instrument, event_time=NOW, mid=Decimal("101")),
        )
    )
    jev_client = FakeJevClient()
    runner = ParallelForwardPaperRunner(
        ForwardPaperConfig(
            symbols=("AAPL",),
            runtime_seconds=60,
            decision_cadence_seconds=0,
            max_positions=1,
            market_hours_only=False,
        ),
        scenarios=build_capital_scenarios(("10万",), usd_jpy_rate=Decimal("100")),
        decision_modes=(ForwardDecisionMode.RULE, ForwardDecisionMode.JEV),
        market_data=market_data,
        clock=FixedClock(NOW),
        jev_client=jev_client,
    )

    summaries = await runner.run()

    assert [summary.run_config["decision_mode"] for summary in summaries] == ["RULE", "JEV"]
    assert [summary.run_config["scenario_label"] for summary in summaries] == [
        "10万制約 / ルール判定",
        "10万制約 / Jev判定",
    ]
    assert summaries[1].run_config["jev_transport"] == "custom"
    assert summaries[0].portfolio.initial_capital == Decimal("1000.00")
    assert summaries[1].portfolio.initial_capital == Decimal("1000.00")
    assert summaries[0].jev_usage.request_count == 0
    assert summaries[1].jev_usage.request_count == 2
    assert len(summaries[1].jev_usage_records) == 2
    assert summaries[1].jev_usage.cost_status == "UNPRICED"
    assert jev_client.calls == 2
