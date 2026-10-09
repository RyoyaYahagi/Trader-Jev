"""Point-in-time evidence and independent questions for US Paper research."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from trader_jev.fees import MoomooFeeCalculator, MoomooFeeSchedule
from trader_jev.models import InstrumentMetadata
from trader_jev.us_equity import (
    US_EASTERN,
    USMarketSnapshot,
    USOHLCVBar,
    USPaperPortfolio,
    USShortHistory,
)

USJevQuestionSet = Literal["us-equity-1.0", "us-equity-atomic-2.0"]
LEGACY_QUESTION_SET: USJevQuestionSet = "us-equity-1.0"
ATOMIC_QUESTION_SET: USJevQuestionSet = "us-equity-atomic-2.0"


def short_history(bars: Sequence[USOHLCVBar], as_of: datetime) -> USShortHistory:
    """Exclude prior sessions, future/unfinished bars, duplicates and stale windows."""

    session_date = as_of.astimezone(US_EASTERN).date()
    closed = {
        bar.timestamp.astimezone(UTC): bar
        for bar in bars
        if bar.timestamp + timedelta(minutes=1) <= as_of
        and bar.timestamp.astimezone(US_EASTERN).date() == session_date
        and time(9, 30) <= bar.timestamp.astimezone(US_EASTERN).time() < time(16)
    }
    recent = tuple(closed[t] for t in sorted(closed)[-11:])
    if not recent:
        return USShortHistory()
    age = Decimal(str((as_of - recent[-1].timestamp - timedelta(minutes=1)).total_seconds()))
    contiguous = all(
        right.timestamp - left.timestamp == timedelta(minutes=1)
        for left, right in zip(recent, recent[1:], strict=False)
    )
    result = USShortHistory(bars=recent, newest_bar_age_seconds=age, contiguous=contiguous)
    if not contiguous or age >= 60:
        return result
    updates: dict[str, Any] = {}
    if len(recent) >= 6:
        prices = [bar.close for bar in recent[-6:]]
        net = prices[-1] - prices[0]
        path = sum((abs(b - a) for a, b in zip(prices, prices[1:], strict=False)), Decimal(0))
        peak = prices[0]
        pullback = Decimal(0)
        for price in prices:
            peak = max(peak, price)
            pullback = max(pullback, Decimal(1) - price / peak)
        updates.update(
            return_5m=prices[-1] / prices[0] - 1,
            price_efficiency_5m=abs(net) / path if path else Decimal(0),
            max_close_pullback_5m=pullback,
        )
    if len(recent) >= 10:
        current = sum((bar.volume for bar in recent[-5:]), Decimal(0))
        previous = sum((bar.volume for bar in recent[-10:-5]), Decimal(0))
        updates.update(
            volume_recent_5m=current,
            volume_previous_5m=previous,
            volume_recent_to_previous_ratio=current / previous if previous else None,
        )
    return result.model_copy(update=updates)


def execution_context(
    snapshot: USMarketSnapshot,
    instrument: InstrumentMetadata,
    portfolio: USPaperPortfolio | None,
    *,
    max_position_pct: Decimal,
    slippage_bps: Decimal,
    estimated_fee_bps: Decimal,
    fee_schedule: MoomooFeeSchedule,
    fee_bps: Decimal,
) -> dict[str, Any]:
    """Expose a size/cost estimate, without authorizing or submitting any order."""

    context: dict[str, Any] = {
        "direction": "LONG",
        "holding_horizon_seconds": 300,
        "fee_schedule": fee_schedule.value,
        "slippage_bps_per_side": str(slippage_bps),
        "planned_quantity": None,
        "estimated_entry_fee_usd": None,
        "estimated_exit_fee_usd": None,
        "flat_price_round_trip_cost_bps": None,
        "cost_status": "UNAVAILABLE",
    }
    if portfolio is None or snapshot.bid_price is None or snapshot.ask_price is None:
        return context
    if slippage_bps >= 10000:
        return context
    # The sizing policy's ledger excludes the reserved JPY cash.
    equity = portfolio.cash_usd + portfolio.positions_value_usd
    ask, bid = snapshot.ask_price, snapshot.bid_price
    cap = int((equity * max_position_pct / ask).to_integral_value(rounding=ROUND_DOWN))
    conservative_cost = ask * (1 + slippage_bps / 10000) * (1 + estimated_fee_bps / 10000)
    if fee_schedule is MoomooFeeSchedule.MOOMOO_US_BASIC:
        conservative_cost += Decimal("0.01")
    cash_cap = int((portfolio.cash_usd / conservative_cost).to_integral_value(rounding=ROUND_DOWN))
    quantity = min(cap, cash_cap)
    quantity -= quantity % instrument.lot_size
    context["planned_quantity"] = quantity
    if quantity <= 0:
        context["cost_status"] = "NO_AFFORDABLE_QUANTITY"
        return context
    entry = ask * (1 + slippage_bps / 10000)
    exit_price = bid * (1 - slippage_bps / 10000)
    fees = MoomooFeeCalculator(fee_schedule, fee_bps=fee_bps)
    entry_fee = fees.calculate_total(instrument, entry, quantity).total
    exit_fee = fees.calculate_total(instrument, exit_price, quantity).total
    outlay = entry * quantity + entry_fee
    proceeds = exit_price * quantity - exit_fee
    context.update(
        estimated_entry_fee_usd=str(entry_fee),
        estimated_exit_fee_usd=str(exit_fee),
        flat_price_round_trip_cost_bps=str((outlay - proceeds) / outlay * 10000),
        cost_status="ESTIMATED_AT_UNCHANGED_QUOTE",
    )
    return context


def atomic_questions() -> dict[str, dict[str, Any]]:
    """Every question has an explicit LONG premise and reads evidence in state."""

    return {
        "setup_type": {
            "type": "choice",
            "instructions": (
                "Classify the observed LONG-entry pattern from evidence.bars and features. "
                "A reversal means an upward recovery after a decline, never a downward reversal. "
                "Missing evidence is not evidence of a pattern."
            ),
            "criteria": {
                "MOMENTUM_BREAKOUT": "Price has crossed above a recent trading range.",
                "REVERSAL": "Price is recovering upward after an observed decline.",
                "TREND_CONTINUATION": "Price is extending an already established upward move.",
                "RANGE": "Price oscillates in a range without sustained upward progress.",
                "NO_SETUP": "No supported LONG pattern, including insufficient price history.",
            },
        },
        "trend_quality": {
            "type": "score",
            "instructions": (
                "Rate only observed upward price progress over the last five closed minutes. "
                "Use evidence.return_5m, evidence.price_efficiency_5m and evidence.bars. "
                "Do not judge volume, fees, or future returns. Null means unavailable."
            ),
            "criteria": [
                "Closed prices fall or oscillate with little net upward progress.",
                "Closed prices make net upward progress but repeatedly change direction.",
                "Closed prices make consistent net upward progress with few reversals.",
            ],
        },
        "volume_support": {
            "type": "score",
            "instructions": (
                "Rate only participation change in the five most recent closed minutes "
                "against the preceding five closed minutes. Use evidence.volume_recent_5m, "
                "evidence.volume_previous_5m and evidence.volume_recent_to_previous_ratio. "
                "Do not compare cumulative day volume to a full-day average."
            ),
            "criteria": [
                "The recent window has clearly less traded volume than the preceding window.",
                "The two adjacent windows have broadly similar traded volume.",
                "The recent window has clearly more traded volume than the preceding window.",
            ],
        },
        "pullback_quality": {
            "type": "score",
            "instructions": (
                "Rate only how much upward price progress survives pullbacks within the "
                "last five closed minutes, using evidence.bars and "
                "evidence.max_close_pullback_5m. Do not judge volume or future prices."
            ),
            "criteria": [
                "No net upward progress, or pullbacks erase most observed upward price progress.",
                "Pullbacks erase part of the progress, but some upward progress remains.",
                "Pullbacks are shallow and most observed upward price progress is retained.",
            ],
        },
        "continuation_quality": {
            "type": "score",
            "instructions": (
                "Rate price evidence supporting an explicitly hypothesized LONG over the "
                "next five minutes. Read evidence.bars; do not assume another question's "
                "selected setup. This is a structural evidence score, not a calibrated "
                "probability of future profit."
            ),
            "criteria": [
                "Price is breaking below its recent range or extending an observed decline.",
                "Price remains in its recent range with no clear upward extension.",
                "Price has broken above its recent range and retained the upward extension.",
            ],
        },
        "abnormal_activity": {
            "type": "noul",
            "instructions": (
                "Does the observed closed-minute history contain a discontinuous price jump "
                "or repeated violent reversals that invalidate the hypothesized LONG? "
                "Use evidence.bars. Ordinary rising volume or smooth directional movement "
                "alone is not abnormal. Distinguish unavailable evidence from an observed anomaly."
            ),
            "criteria": {
                "true": "Observed discontinuous jumps or violent reversals invalidate the LONG.",
                "false": (
                    "Observed history is continuous; ordinary volatility alone is not an anomaly."
                ),
            },
        },
        "trade_worthy": {
            "type": "noul",
            "instructions": (
                "Does the observed LONG opportunity offer enough plausible five-minute "
                "upward price movement to cover execution.flat_price_round_trip_cost_bps? "
                "Read execution.planned_quantity, execution.cost_status, evidence and features. "
                "Costs are code-computed estimates, not predicted returns; do not invent fees "
                "or authorize an order."
            ),
            "criteria": {
                "true": (
                    "Observed upward opportunity plausibly exceeds the computed round-trip cost."
                ),
                "false": (
                    "No affordable quantity, unavailable costs, or insufficient upward opportunity."
                ),
            },
        },
    }
