"""Command-line runner for a Jev-backed Paper replay."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import Field

from trader_jev.adapters import (
    AdapterNormalizationError,
    FileMarketDataAdapter,
    SyntheticMarketDataAdapter,
)
from trader_jev.clock import ReplayClock
from trader_jev.decision import (
    JevAdapterConfig,
    JevClient,
    JevDecisionAdapter,
    JevDecisionModel,
)
from trader_jev.execution import ExecutionConfig, PaperBroker
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.integration import IntegrationMode, JevMLDecisionModel, MLDecisionModel
from trader_jev.interfaces import DecisionModel, MarketDataAdapter
from trader_jev.jev_http import JevHttpClient
from trader_jev.jev_usage import (
    JevPricingConfig,
    JevUsageRecord,
    JevUsageSummary,
    summarize_usage,
    usage_records_from_audits,
)
from trader_jev.ml import load_prediction_model
from trader_jev.models import (
    BarEvent,
    DomainModel,
    ExecutionMode,
    InstrumentMetadata,
    Market,
    MarketEvent,
    MarketState,
    OrderBookEvent,
    PortfolioState,
    QuoteEvent,
    RiskProfile,
    TradingSession,
)
from trader_jev.pipeline import PipelineConfig, PipelineResult, TradingPipeline
from trader_jev.portfolio import PortfolioLedger
from trader_jev.risk import DeterministicRiskEngine, FixedQuantityPortfolioPolicy, RiskConfig


class PaperRunSummary(DomainModel):
    """Stable, secret-free summary emitted by one CLI run."""

    source: str
    events_processed: int = Field(ge=0)
    decisions: int = Field(ge=0)
    approved_orders: int = Field(ge=0)
    risk_rejections: int = Field(ge=0)
    pipeline_failures: int = Field(ge=0)
    holds: int = Field(ge=0)
    fills: int = Field(ge=0)
    portfolio: PortfolioState
    data_stats: Mapping[str, Any]
    jev_usage: JevUsageSummary = Field(default_factory=JevUsageSummary)
    jev_usage_records: tuple[JevUsageRecord, ...] = ()
    run_config: Mapping[str, Any]


def build_parser() -> argparse.ArgumentParser:
    """Build the Paper runner argument parser."""

    parser = argparse.ArgumentParser(
        prog="trader-jev-paper",
        description="Run a Jev-backed historical or synthetic replay through PaperBroker.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help=(
            "Optional JEV_*/JQUANTS_*/TYPESAFE_API_KEY env file; process environment variables "
            "take precedence "
            "(default: .env)."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", type=Path, help="CSV, JSONL, or NDJSON historical market data.")
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="Use deterministic synthetic quote data instead of a historical file.",
    )
    parser.add_argument("--start", required=True, type=_timestamp, help="Replay start (ISO-8601).")
    parser.add_argument("--end", required=True, type=_timestamp, help="Replay end (ISO-8601).")
    parser.add_argument("--symbol", required=True, help="One instrument symbol to replay.")
    parser.add_argument(
        "--market", choices=tuple(item.value for item in Market), default=Market.JP.value
    )
    parser.add_argument("--currency", help="Instrument currency; defaults from --market.")
    parser.add_argument("--timezone", help="Instrument timezone; defaults from --market.")
    parser.add_argument(
        "--tick-size", type=_decimal, help="Minimum price tick; defaults from --market."
    )
    parser.add_argument("--lot-size", type=_positive_int, default=1)
    parser.add_argument(
        "--shortable",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether the instrument is shortable (default: true).",
    )
    parser.add_argument(
        "--quantity", type=_positive_int, help="Paper order quantity (default: lot size)."
    )
    parser.add_argument("--initial-capital", type=_decimal, default=Decimal("100000"))
    parser.add_argument(
        "--risk-profile",
        choices=tuple(item.value for item in RiskProfile),
        default=RiskProfile.BALANCED.value,
    )
    parser.add_argument(
        "--allow-short",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow SHORT intents through Paper risk checks (default: true).",
    )
    parser.add_argument("--fee-bps", type=_decimal, default=Decimal("0"))
    parser.add_argument("--slippage-bps", type=_decimal, default=Decimal("0"))
    parser.add_argument("--latency-ms", type=_nonnegative_int, default=0)
    parser.add_argument(
        "--interval-seconds",
        type=_positive_int,
        default=15,
        help="Synthetic quote interval (default: 15).",
    )
    parser.add_argument("--seed", type=int, default=0, help="Synthetic data seed (default: 0).")
    parser.add_argument("--max-events", type=_positive_int)
    parser.add_argument("--portfolio-id", default="paper")
    parser.add_argument("--report", type=Path, help="Optional path for the JSON summary.")
    parser.add_argument(
        "--ml-artifact",
        type=Path,
        help="Optional logistic or LightGBM prediction artifact.",
    )
    parser.add_argument(
        "--ml-mode",
        choices=(
            "ML_ONLY",
            IntegrationMode.A_JEV_INPUT.value,
            IntegrationMode.B_DETERMINISTIC_MERGE.value,
            IntegrationMode.C_ML_SCREEN_JEV.value,
        ),
        default=IntegrationMode.A_JEV_INPUT.value,
        help="How a loaded ML artifact is combined with Jev (default: A_JEV_INPUT).",
    )
    parser.add_argument(
        "--fail-on-corrupt",
        action="store_true",
        help="Stop when a historical input row cannot be normalized.",
    )
    return parser


async def run_paper(
    args: argparse.Namespace,
    *,
    client: JevClient | None = None,
) -> PaperRunSummary:
    """Run one replay through Jev, RiskEngine, PaperBroker, and the Ledger."""

    if args.start >= args.end:
        raise ValueError("--start must be earlier than --end")
    if args.initial_capital < 0:
        raise ValueError("--initial-capital must not be negative")
    if args.fee_bps < 0 or args.slippage_bps < 0:
        raise ValueError("--fee-bps and --slippage-bps must not be negative")

    instrument = _build_instrument(args)
    quantity = args.quantity or instrument.lot_size
    if quantity % instrument.lot_size != 0:
        raise ValueError("--quantity must be a multiple of --lot-size")

    ml_artifact_path = getattr(args, "ml_artifact", None)
    ml_mode = getattr(args, "ml_mode", IntegrationMode.A_JEV_INPUT.value)
    prediction_model = load_prediction_model(ml_artifact_path) if ml_artifact_path else None
    if ml_mode == "ML_ONLY" and prediction_model is None:
        raise ValueError("--ml-artifact is required when --ml-mode=ML_ONLY")
    if (
        ml_mode != "ML_ONLY"
        and prediction_model is None
        and ml_mode != IntegrationMode.A_JEV_INPUT.value
    ):
        raise ValueError("--ml-artifact is required for the selected --ml-mode")

    env = load_env_file(args.env_file)
    jev_client: JevClient | None = None
    if ml_mode != "ML_ONLY":
        jev_client = client or JevHttpClient.from_env(env)
    jev_timeout = 5.0
    jev_model = "jev-latest" if ml_mode != "ML_ONLY" else "ml"
    if isinstance(jev_client, JevHttpClient):
        jev_timeout = jev_client.config.timeout_seconds
        jev_model = jev_client.config.model

    clock = ReplayClock()
    initial_state = PortfolioState(
        portfolio_id=args.portfolio_id,
        cash=args.initial_capital,
        initial_capital=args.initial_capital,
        equity=args.initial_capital,
    )
    ledger = PortfolioLedger(initial_state)
    broker = PaperBroker(
        ExecutionConfig(
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
            latency_ms=args.latency_ms,
        ),
        clock=clock,
        ledger=ledger,
    )
    risk_engine = DeterministicRiskEngine(
        config=RiskConfig(
            risk_profile=RiskProfile(args.risk_profile),
            execution_mode=ExecutionMode.PAPER,
            allow_short=args.allow_short,
            allowed_symbols=frozenset({instrument.symbol}),
            enforce_cash=True,
        ),
        portfolio_policy=FixedQuantityPortfolioPolicy(quantity),
        clock=clock,
    )
    jev_model_instance: JevDecisionModel | None = None
    jev_adapter: JevDecisionAdapter | None = None
    if jev_client is not None:
        jev_adapter = JevDecisionAdapter(
            jev_client,
            config=JevAdapterConfig(
                timeout_seconds=jev_timeout,
                model_version=jev_model,
            ),
            clock=clock,
        )
        jev_model_instance = JevDecisionModel(jev_adapter)
    decision_model: DecisionModel
    if ml_mode == "ML_ONLY":
        if prediction_model is None:
            raise ValueError("--ml-artifact is required when --ml-mode=ML_ONLY")
        decision_model = MLDecisionModel(prediction_model)
    elif prediction_model is not None:
        if jev_model_instance is None:
            raise ValueError("a Jev client is required for Jev+ML modes")
        decision_model = JevMLDecisionModel(
            jev_model_instance,
            prediction_model,
            mode=IntegrationMode(ml_mode),
        )
    else:
        if jev_model_instance is None:
            raise ValueError("a Jev client is required")
        decision_model = jev_model_instance

    pipeline = TradingPipeline(
        feature_engine=InMemoryFeatureEngine(),
        decision_model=decision_model,
        prediction_model=prediction_model,
        risk_engine=risk_engine,
        broker_adapter=broker,
        config=PipelineConfig(decision_timeout_seconds=jev_timeout + 0.5),
        clock=clock,
    )
    market_data, source_name = _build_market_data(args)
    processed = 0
    results: list[PipelineResult] = []
    async for event in market_data.stream((instrument,)):
        if args.max_events is not None and processed >= args.max_events:
            break
        clock.advance_to(event.received_at)
        _update_broker_market(broker, event)
        results.append(await pipeline.process_event(event, ledger.state))
        processed += 1

    if processed == 0:
        raise ValueError("replay produced no events in the requested interval")

    data_stats: dict[str, Any]
    if isinstance(market_data, FileMarketDataAdapter):
        stats = market_data.stats
        data_stats = {
            "total_rows": stats.total_rows,
            "accepted_rows": stats.accepted_rows,
            "rejected_rows": stats.rejected_rows,
            "healthy": stats.healthy,
            "errors": tuple(stats.errors[:20]),
        }
    else:
        data_stats = {"source": "synthetic"}

    jev_pricing = JevPricingConfig.from_env(env)
    jev_usage_records = (
        usage_records_from_audits(jev_adapter.audit_records, jev_pricing)
        if jev_adapter is not None
        else ()
    )
    summary = PaperRunSummary(
        source=source_name,
        events_processed=processed,
        decisions=sum(result.trade_intent is not None for result in results),
        approved_orders=sum(
            result.risk_decision is not None and result.risk_decision.approved for result in results
        ),
        risk_rejections=sum(
            result.risk_decision is not None and not result.risk_decision.approved
            for result in results
        ),
        pipeline_failures=sum(result.failure_code is not None for result in results),
        holds=sum(
            result.trade_intent is not None and result.trade_intent.action.value == "HOLD"
            for result in results
        ),
        fills=len(ledger.fills),
        portfolio=ledger.state,
        data_stats=data_stats,
        jev_usage=summarize_usage(jev_usage_records),
        jev_usage_records=jev_usage_records,
        run_config={
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "symbol": instrument.symbol,
            "market": instrument.market.value,
            "risk_profile": args.risk_profile,
            "quantity": quantity,
            "initial_capital": str(args.initial_capital),
            "fee_bps": str(args.fee_bps),
            "slippage_bps": str(args.slippage_bps),
            "latency_ms": args.latency_ms,
            "ml_artifact": str(ml_artifact_path) if ml_artifact_path is not None else None,
            "ml_mode": ml_mode,
            "ml_model_version": (
                prediction_model.model_version if prediction_model is not None else None
            ),
            "jev_pricing": jev_pricing.model_dump(mode="json"),
        },
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run_paper(args))
        encoded = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2)
        if args.report is not None:
            args.report.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        return 0
    except (AdapterNormalizationError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


def _build_instrument(args: argparse.Namespace) -> InstrumentMetadata:
    market = Market(args.market)
    defaults = {
        Market.JP: ("JPY", "Asia/Tokyo", Decimal("1"), time(9), time(15)),
        Market.US: ("USD", "America/New_York", Decimal("0.01"), time(9, 30), time(16)),
    }
    currency, timezone, tick_size, open_time, close_time = defaults[market]
    return InstrumentMetadata(
        symbol=args.symbol,
        market=market,
        currency=args.currency or currency,
        timezone=args.timezone or timezone,
        tick_size=args.tick_size or tick_size,
        lot_size=args.lot_size,
        trading_session=TradingSession(open_time=open_time, close_time=close_time),
        shortability=args.shortable,
    )


def _build_market_data(args: argparse.Namespace) -> tuple[MarketDataAdapter, str]:
    if args.synthetic:
        return (
            SyntheticMarketDataAdapter(
                start=args.start,
                end=args.end,
                interval=timedelta(seconds=args.interval_seconds),
                seed=args.seed,
            ),
            f"synthetic:{args.seed}",
        )
    if args.data is None:
        raise ValueError("--data or --synthetic is required")
    return (
        FileMarketDataAdapter(
            args.data,
            skip_corrupt_rows=not args.fail_on_corrupt,
            start=args.start,
            end=args.end,
        ),
        str(args.data),
    )


def _update_broker_market(broker: PaperBroker, event: MarketEvent) -> None:
    if isinstance(event, QuoteEvent):
        broker.update_market(event)
        return
    if isinstance(event, BarEvent):
        broker.update_market(_bar_market_state(event), instrument=event.instrument)
        return
    if isinstance(event, OrderBookEvent) and event.bids and event.asks:
        broker.update_market(_book_market_state(event), instrument=event.instrument)


def _bar_market_state(event: BarEvent) -> MarketState:
    return MarketState(
        bid=event.close,
        ask=event.close,
        mid=event.close,
        spread=Decimal("0"),
        bid_size=Decimal("0"),
        ask_size=Decimal("0"),
        last_event_time=event.event_time,
        last_received_at=event.received_at,
    )


def _book_market_state(event: OrderBookEvent) -> MarketState:
    bid = event.bids[0]
    ask = event.asks[0]
    return MarketState(
        bid=bid.price,
        ask=ask.price,
        mid=(bid.price + ask.price) / Decimal("2"),
        spread=ask.price - bid.price,
        bid_size=bid.size,
        ask_size=ask.size,
        last_event_time=event.event_time,
        last_received_at=event.received_at,
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Load JEV/J-Quants KEY=VALUE entries without exposing or overriding secrets."""

    values = dict(os.environ)
    if not path.exists():
        return values
    if not path.is_file():
        raise ValueError(f"env file is not a file: {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"env file line {line_number} must use KEY=VALUE")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not name.startswith(("JEV_", "JQUANTS_")) and name != "TYPESAFE_API_KEY":
            continue
        value = _parse_env_value(raw_value.strip(), line_number)
        values.setdefault(name, value)
    return values


def _parse_env_value(value: str, line_number: int) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"env file line {line_number} has invalid quoting") from exc
        if not isinstance(parsed, str):
            raise ValueError(f"env file line {line_number} value must be a string")
        return parsed
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be a decimal") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = _positive_int(value) if value != "0" else 0
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PaperRunSummary", "build_parser", "load_env_file", "main", "run_paper"]
