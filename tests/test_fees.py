from __future__ import annotations

from decimal import Decimal

import pytest

from trader_jev.fees import MoomooFeeCalculator, MoomooFeeSchedule
from trader_jev.forward_paper import build_us_instruments
from trader_jev.models import InstrumentMetadata, Market


def test_moomoo_us_basic_rounds_up_and_applies_order_cap() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.MOOMOO_US_BASIC)

    minimum = calculator.calculate_total(instrument, Decimal("1"), 1)
    regular = calculator.calculate_total(instrument, Decimal("100"), 1)
    capped = calculator.calculate_total(instrument, Decimal("6"), 5000)

    assert minimum.total == Decimal("0.01")
    assert regular.total == Decimal("0.14")
    assert capped.total == Decimal("22")
    assert regular.currency == "USD"
    assert regular.schedule == MoomooFeeSchedule.MOOMOO_US_BASIC.value


def test_moomoo_us_basic_allocates_one_order_minimum_across_partial_fills() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.MOOMOO_US_BASIC)

    first = calculator.calculate(instrument, Decimal("100"), 1)
    second = calculator.calculate(
        instrument,
        Decimal("100"),
        1,
        previous_quantity=1,
        previous_notional=Decimal("100"),
    )

    assert first.total == Decimal("0.14")
    assert second.total == Decimal("0.13")
    assert first.total + second.total == Decimal("0.27")


def test_moomoo_us_advanced_keeps_transaction_system_and_clearing_fees_separate() -> None:
    instrument = build_us_instruments(("AAPL",))[0]
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.MOOMOO_US_ADVANCED)

    fee = calculator.calculate_total(instrument, Decimal("100"), 100)

    assert fee.transaction_fee == Decimal("1.08")
    assert fee.system_fee == Decimal("1.10")
    assert fee.local_clearing_fee == Decimal("0.600")
    assert fee.total == Decimal("2.780")


def test_moomoo_japan_equity_is_currently_free(instrument: InstrumentMetadata) -> None:
    assert instrument.market is Market.JP
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.MOOMOO_JP_EQUITY)

    fee = calculator.calculate_total(instrument, Decimal("1000"), 100)

    assert fee.total == Decimal("0")
    assert fee.currency == "JPY"


def test_auto_schedule_follows_instrument_market(instrument: InstrumentMetadata) -> None:
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.AUTO)

    jp_fee = calculator.calculate_total(instrument, Decimal("1000"), 1)
    us_fee = calculator.calculate_total(
        build_us_instruments(("AAPL",))[0],
        Decimal("100"),
        1,
    )

    assert jp_fee.schedule == MoomooFeeSchedule.MOOMOO_JP_EQUITY.value
    assert us_fee.schedule == MoomooFeeSchedule.MOOMOO_US_BASIC.value


def test_us_schedule_rejects_japan_instrument(instrument: InstrumentMetadata) -> None:
    calculator = MoomooFeeCalculator(MoomooFeeSchedule.MOOMOO_US_BASIC)

    with pytest.raises(ValueError, match="requires a US instrument"):
        calculator.calculate_total(instrument, Decimal("100"), 1)
