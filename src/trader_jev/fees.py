"""Fee schedules used by the Paper execution model.

The module keeps broker-specific fee rules behind one small calculation
interface.  The PaperBroker only needs the incremental charge for the current
fill, so order-level minimums and caps remain local to this implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from trader_jev.models import Action, FeeBreakdown, InstrumentMetadata, Market


class MoomooFeeSchedule(StrEnum):
    """Supported fee assumptions for the current Paper milestone."""

    ZERO = "ZERO"
    BASIS_POINTS = "BASIS_POINTS"
    AUTO = "AUTO"
    MOOMOO_JP_EQUITY = "MOOMOO_JP_EQUITY"
    MOOMOO_US_BASIC = "MOOMOO_US_BASIC"
    MOOMOO_US_ADVANCED = "MOOMOO_US_ADVANCED"


@dataclass(frozen=True)
class _FeeAmounts:
    transaction_fee: Decimal = Decimal("0")
    system_fee: Decimal = Decimal("0")
    local_clearing_fee: Decimal = Decimal("0")
    regulatory_fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return sum(
            (
                self.transaction_fee,
                self.system_fee,
                self.local_clearing_fee,
                self.regulatory_fee,
                self.tax,
            ),
            Decimal("0"),
        )

    def __sub__(self, other: _FeeAmounts) -> _FeeAmounts:
        return _FeeAmounts(
            transaction_fee=self.transaction_fee - other.transaction_fee,
            system_fee=self.system_fee - other.system_fee,
            local_clearing_fee=self.local_clearing_fee - other.local_clearing_fee,
            regulatory_fee=self.regulatory_fee - other.regulatory_fee,
            tax=self.tax - other.tax,
        )


class MoomooFeeCalculator:
    """Calculate one fill's incremental charge from an order-level schedule.

    ``previous_quantity`` and ``previous_notional`` let the calculator apply a
    minimum or cap once per order, even when the order is filled in pieces.
    Amounts returned by the calculator are in the instrument currency.
    """

    def __init__(
        self,
        schedule: MoomooFeeSchedule = MoomooFeeSchedule.MOOMOO_US_BASIC,
        *,
        fee_bps: Decimal = Decimal("0"),
    ) -> None:
        self.schedule = MoomooFeeSchedule(schedule)
        if not fee_bps.is_finite() or fee_bps < 0:
            raise ValueError("fee_bps must be finite and non-negative")
        self.fee_bps = fee_bps

    def calculate(
        self,
        instrument: InstrumentMetadata,
        price: Decimal,
        quantity: int,
        *,
        previous_quantity: int = 0,
        previous_notional: Decimal = Decimal("0"),
        side: Action = Action.LONG,
    ) -> FeeBreakdown:
        """Return the fee increment attributable to the current fill."""

        del side  # The currently supported equity schedules are side-neutral.
        if price <= 0:
            raise ValueError("fee calculation price must be positive")
        if quantity <= 0:
            raise ValueError("fee calculation quantity must be positive")
        if previous_quantity < 0:
            raise ValueError("previous_quantity must not be negative")
        if previous_notional < 0:
            raise ValueError("previous_notional must not be negative")

        notional = abs(price * Decimal(quantity))
        schedule = self._resolved_schedule(instrument)
        before = self._amounts(schedule, previous_quantity, previous_notional)
        after = self._amounts(
            schedule,
            previous_quantity + quantity,
            previous_notional + notional,
        )
        charge = after - before
        return FeeBreakdown(
            schedule=schedule.value,
            currency=instrument.currency,
            quantity=quantity,
            notional=notional,
            transaction_fee=charge.transaction_fee,
            system_fee=charge.system_fee,
            local_clearing_fee=charge.local_clearing_fee,
            regulatory_fee=charge.regulatory_fee,
            tax=charge.tax,
            total=charge.total,
        )

    def calculate_total(
        self,
        instrument: InstrumentMetadata,
        price: Decimal,
        quantity: int,
        *,
        side: Action = Action.LONG,
    ) -> FeeBreakdown:
        """Return the complete charge for one order filled at one price."""

        return self.calculate(instrument, price, quantity, side=side)

    def _resolved_schedule(self, instrument: InstrumentMetadata) -> MoomooFeeSchedule:
        schedule = self.schedule
        if schedule is MoomooFeeSchedule.AUTO:
            schedule = (
                MoomooFeeSchedule.MOOMOO_JP_EQUITY
                if instrument.market is Market.JP
                else MoomooFeeSchedule.MOOMOO_US_BASIC
            )
        if schedule is MoomooFeeSchedule.MOOMOO_JP_EQUITY and instrument.market is not Market.JP:
            raise ValueError("MOOMOO_JP_EQUITY requires a JP instrument")
        if (
            schedule
            in (
                MoomooFeeSchedule.MOOMOO_US_BASIC,
                MoomooFeeSchedule.MOOMOO_US_ADVANCED,
            )
            and instrument.market is not Market.US
        ):
            raise ValueError(f"{schedule.value} requires a US instrument")
        return schedule

    def _amounts(
        self,
        schedule: MoomooFeeSchedule,
        quantity: int,
        notional: Decimal,
    ) -> _FeeAmounts:
        if quantity == 0:
            return _FeeAmounts()
        if schedule is MoomooFeeSchedule.ZERO or schedule is MoomooFeeSchedule.MOOMOO_JP_EQUITY:
            return _FeeAmounts()
        if schedule is MoomooFeeSchedule.BASIS_POINTS:
            return _FeeAmounts(
                transaction_fee=notional * self.fee_bps / Decimal("10000"),
            )
        if schedule is MoomooFeeSchedule.MOOMOO_US_BASIC:
            fee = _ceil_cent(notional * Decimal("0.00132"))
            fee = min(Decimal("22"), max(Decimal("0.01"), fee))
            return _FeeAmounts(transaction_fee=fee)
        if schedule is MoomooFeeSchedule.MOOMOO_US_ADVANCED:
            transaction_fee = _ceil_cent(
                max(
                    Decimal("1.08"),
                    min(
                        Decimal(quantity) * Decimal("0.00539"),
                        notional * Decimal("0.0055"),
                    ),
                )
            )
            system_fee = _ceil_cent(
                max(
                    Decimal("1.10"),
                    min(
                        Decimal(quantity) * Decimal("0.0055"),
                        notional * Decimal("0.0055"),
                    ),
                )
            )
            return _FeeAmounts(
                transaction_fee=transaction_fee,
                system_fee=system_fee,
                local_clearing_fee=Decimal(quantity) * Decimal("0.006"),
            )
        raise ValueError(f"unsupported fee schedule: {schedule.value}")


def _ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


__all__ = ["MoomooFeeCalculator", "MoomooFeeSchedule"]
