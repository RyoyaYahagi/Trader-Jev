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

USJevQuestionSet = Literal["us-equity-1.0", "us-equity-atomic-2.0", "us-equity-atomic-2.1"]
LEGACY_QUESTION_SET: USJevQuestionSet = "us-equity-1.0"
ATOMIC_QUESTION_SET: USJevQuestionSet = "us-equity-atomic-2.0"
REFINED_QUESTION_SET: USJevQuestionSet = "us-equity-atomic-2.1"
_REFINED_FIELDS = {
    "return_previous_5m",
    "distance_from_previous_5m_high",
    "upward_excursion_5m",
    "retained_upward_progress_fraction_5m",
    "previous_window_direction",
    "latest_window_direction",
    "close_above_previous_5m_high",
}


def is_atomic_question_set(version: str) -> bool:
    return version in (ATOMIC_QUESTION_SET, REFINED_QUESTION_SET)


def evidence_payload(history: USShortHistory, version: USJevQuestionSet) -> dict[str, Any]:
    """Keep the 2.0 input contract intact while adding evidence to 2.1."""
    excluded: set[str] = _REFINED_FIELDS if version == ATOMIC_QUESTION_SET else set()
    return history.model_dump(mode="json", exclude=excluded)


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
        retained = None
        if max(prices) > prices[0]:
            retained = net / (max(prices) - prices[0]) if net > 0 else Decimal(0)
        updates.update(
            return_5m=prices[-1] / prices[0] - 1,
            price_efficiency_5m=abs(net) / path if path else Decimal(0),
            max_close_pullback_5m=pullback,
            upward_excursion_5m=max(prices) / prices[0] - 1,
            retained_upward_progress_fraction_5m=retained,
            latest_window_direction="UP" if net > 0 else "DOWN" if net < 0 else "FLAT",
        )
    if len(recent) >= 10:
        current = sum((bar.volume for bar in recent[-5:]), Decimal(0))
        previous = sum((bar.volume for bar in recent[-10:-5]), Decimal(0))
        updates.update(
            volume_recent_5m=current,
            volume_previous_5m=previous,
            volume_recent_to_previous_ratio=current / previous if previous else None,
        )
    if len(recent) >= 11:
        previous_high = max(bar.high for bar in recent[-10:-5])
        updates.update(
            return_previous_5m=recent[-6].close / recent[-11].close - 1,
            distance_from_previous_5m_high=recent[-1].close / previous_high - 1,
            previous_window_direction=(
                "UP"
                if recent[-6].close > recent[-11].close
                else "DOWN"
                if recent[-6].close < recent[-11].close
                else "FLAT"
            ),
            close_above_previous_5m_high=recent[-1].close > previous_high,
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


def atomic_questions(
    version: USJevQuestionSet = ATOMIC_QUESTION_SET,
) -> dict[str, dict[str, Any]]:
    """Every question has an explicit LONG premise and reads evidence in state."""

    questions: dict[str, dict[str, Any]] = {
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
    if version == REFINED_QUESTION_SET:
        questions["setup_type"] = {
            "type": "choice",
            "instructions": (
                "Classify only the observed LONG price episode in the supplied closed-minute "
                "window. Code has already compared the prices: read "
                "evidence.previous_window_direction, evidence.latest_window_direction and "
                "evidence.close_above_previous_5m_high. UP/DOWN/FLAT describe the two "
                "adjacent five-minute close changes; the boolean says whether the final "
                "close is strictly above the earlier window's highest high. "
                "Use these explicit observations to match the separate cases below. "
                "Ignore whole-day changes, volume, fees and predictions. A decline in the "
                "latest window is not an upward reversal, even after an earlier rally. "
                "This classification describes observed geometry, not a trade permission."
            ),
            "criteria": {
                "MOMENTUM_BREAKOUT": (
                    "Previous direction is UP or FLAT, latest direction is UP, and "
                    "close_above_previous_5m_high is true. "
                    "This window broke above its earlier high."
                ),
                "REVERSAL": (
                    "Previous direction is DOWN and latest direction is UP: an observed "
                    "upward recovery after a decline, including a recovery above the earlier high."
                ),
                "TREND_CONTINUATION": (
                    "Previous direction is UP, latest direction is UP, and "
                    "close_above_previous_5m_high is false. This window has no breakout."
                ),
                "RANGE": (
                    "Latest direction is FLAT, or previous direction is FLAT and latest "
                    "direction is UP with close_above_previous_5m_high false."
                ),
                "NO_SETUP": (
                    "Latest direction is DOWN, or the required two-window evidence "
                    "is unavailable. There is no supported current LONG episode in this window."
                ),
            },
        }
        questions["pullback_quality"] = {
            "type": "score",
            "instructions": (
                "Rate only the fraction of the latest five-minute upward excursion retained "
                "at the final closed price. evidence.upward_excursion_5m is the peak rise "
                "above the window's first close; evidence.retained_upward_progress_fraction_5m "
                "is max(final close - first close, 0) divided by (peak close - first close). "
                "An excursion of zero is an observed absence of an upward move; its null "
                "retained fraction is not missing price history. Use the stated fraction bands, "
                "not absolute drawdown size, whole-day returns, volume or predictions."
            ),
            "criteria": [
                "The observed window has no upward excursion, or retains at most one third "
                "of its peak upward progress at the final close.",
                "The window has an upward excursion and retains more than one third but "
                "less than two thirds of its peak upward progress at the final close.",
                "The window has an upward excursion and retains at least two thirds "
                "of its peak upward progress at the final close.",
            ],
        }
    return questions
