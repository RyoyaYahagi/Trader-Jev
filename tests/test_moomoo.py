from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from trader_jev.clock import FixedClock
from trader_jev.models import InstrumentMetadata, Market
from trader_jev.moomoo import (
    MoomooApiError,
    MoomooClientConfig,
    MoomooMarketDataAdapter,
)

from .conftest import NOW


class FakeTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self.rows


class FakeContext:
    def __init__(self, responses: list[tuple[object, object]]) -> None:
        self.responses = responses
        self.requested_codes: list[list[str]] = []
        self.closed = False

    def get_market_snapshot(self, code_list: list[str]) -> tuple[object, object]:
        self.requested_codes.append(code_list)
        if not self.responses:
            raise AssertionError("fake context received more requests than expected")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def snapshot_row(
    *,
    code: str = "JP.TEST",
    bid: str = "100.25",
    ask: str = "100.75",
    update_time: str = "2026-09-21 09:00:00",
) -> dict[str, Any]:
    return {
        "code": code,
        "update_time": update_time,
        "bid_price": bid,
        "ask_price": ask,
        "bid_vol": "10",
        "ask_vol": "12",
        "sec_status": "NORMAL",
    }


def make_factory(context: FakeContext):
    def factory(_: MoomooClientConfig) -> FakeContext:
        return context

    return factory


@pytest.mark.asyncio
async def test_fetch_once_normalizes_snapshot_without_leaking_sdk_types(
    instrument: InstrumentMetadata,
) -> None:
    context = FakeContext([(0, FakeTable([snapshot_row()]))])
    adapter = MoomooMarketDataAdapter(
        MoomooClientConfig(poll_interval_seconds=0.001),
        context_factory=make_factory(context),
        clock=FixedClock(NOW),
    )

    events = await adapter.fetch_once([instrument])

    assert len(events) == 1
    event = events[0]
    assert event.instrument == instrument
    assert event.event_time == NOW
    assert event.received_at == NOW
    assert event.bid == Decimal("100.25")
    assert event.ask == Decimal("100.75")
    assert event.bid_size == Decimal("10")
    assert event.ask_size == Decimal("12")
    assert event.source == "moomoo:market-snapshot"
    assert context.requested_codes == [["JP.TEST"]]
    assert context.closed


@pytest.mark.asyncio
async def test_stream_yields_changed_quotes_and_closes_context(
    instrument: InstrumentMetadata,
) -> None:
    context = FakeContext(
        [
            (0, FakeTable([snapshot_row()])),
            (0, FakeTable([snapshot_row()])),
            (
                0,
                FakeTable(
                    [
                        snapshot_row(
                            bid="101",
                            ask="102",
                            update_time="2026-09-21 09:00:01",
                        )
                    ]
                ),
            ),
        ]
    )
    adapter = MoomooMarketDataAdapter(
        MoomooClientConfig(poll_interval_seconds=0.001),
        context_factory=make_factory(context),
        clock=FixedClock(NOW),
    )
    stream = adapter.stream([instrument])

    first = await anext(stream)
    second = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert first.bid == Decimal("100.25")
    assert second.bid == Decimal("101")
    assert first.sequence_number is not None
    assert second.sequence_number is not None
    assert second.sequence_number > first.sequence_number
    assert context.closed


@pytest.mark.asyncio
async def test_api_error_fails_closed_and_closes_context(
    instrument: InstrumentMetadata,
) -> None:
    context = FakeContext([(1, "quote permission denied")])
    adapter = MoomooMarketDataAdapter(
        context_factory=make_factory(context),
        clock=FixedClock(NOW),
    )

    with pytest.raises(MoomooApiError, match="permission denied"):
        await adapter.fetch_once([instrument])

    assert context.closed


@pytest.mark.asyncio
async def test_missing_bid_ask_is_rejected_instead_of_synthesized(
    instrument: InstrumentMetadata,
) -> None:
    row = snapshot_row()
    row["bid_price"] = "N/A"
    context = FakeContext([(0, FakeTable([row]))])
    adapter = MoomooMarketDataAdapter(
        context_factory=make_factory(context),
        clock=FixedClock(NOW),
    )

    with pytest.raises(MoomooApiError, match="bid_price"):
        await adapter.fetch_once([instrument])


def test_config_reads_only_non_secret_opend_settings() -> None:
    config = MoomooClientConfig.from_env(
        {
            "MOOMOO_OPEND_HOST": "127.0.0.1",
            "MOOMOO_OPEND_PORT": "12345",
            "MOOMOO_POLL_INTERVAL_SECONDS": "2.5",
            "MOOMOO_REQUEST_BATCH_SIZE": "10",
        }
    )

    assert config.host == "127.0.0.1"
    assert config.port == 12345
    assert config.poll_interval_seconds == 2.5
    assert config.request_batch_size == 10


def test_us_code_mapping_can_be_explicit(
    instrument: InstrumentMetadata,
) -> None:
    us_instrument = instrument.model_copy(
        update={"symbol": "AAPL", "market": Market.US, "timezone": "America/New_York"}
    )
    context = FakeContext(
        [
            (
                0,
                FakeTable(
                    [
                        snapshot_row(
                            code="US.AAPL",
                            update_time="2026-09-20 20:00:00",
                        )
                    ]
                ),
            )
        ]
    )
    adapter = MoomooMarketDataAdapter(
        context_factory=make_factory(context),
        clock=FixedClock(NOW),
        code_map={"US:AAPL": "US.AAPL"},
    )

    events = asyncio.run(adapter.fetch_once([us_instrument]))

    assert events[0].event_time == NOW
    assert context.requested_codes == [["US.AAPL"]]
