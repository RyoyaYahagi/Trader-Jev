from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from trader_jev.models import (
    Action,
    InstrumentMetadata,
    OrderIntent,
    OrderType,
)


def test_instrument_metadata_is_market_neutral(instrument: InstrumentMetadata) -> None:
    assert instrument.currency == "JPY"
    assert instrument.market.value == "JP"
    assert instrument.lot_size == 1


def test_domain_models_are_frozen(instrument: InstrumentMetadata) -> None:
    with pytest.raises(ValidationError):
        instrument.symbol = "MUTATED"  # type: ignore[misc]


def test_limit_order_requires_a_limit_price(instrument: InstrumentMetadata) -> None:
    with pytest.raises(ValidationError):
        OrderIntent(
            source_trade_intent_id=UUID("00000000-0000-0000-0000-000000000001"),
            instrument=instrument,
            side=Action.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            created_at=datetime.now(UTC),
        )


def test_decimal_and_datetime_are_serializable(instrument: InstrumentMetadata) -> None:
    order = OrderIntent(
        source_trade_intent_id=UUID("00000000-0000-0000-0000-000000000001"),
        instrument=instrument,
        side=Action.LONG,
        quantity=1,
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    dumped = order.model_dump(mode="json")
    assert dumped["instrument"]["tick_size"] == "1"
    assert dumped["created_at"].startswith("2026-09-21T00:00:00")
