"""Command-line training entry point for the offline Paper ML models."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field

from trader_jev.adapters import AdapterNormalizationError, FileMarketDataAdapter
from trader_jev.cli import load_env_file
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.interfaces import MarketDataAdapter
from trader_jev.jquants import JQuantsMinuteBarAdapter
from trader_jev.ml import (
    ChronologicalSplitConfig,
    LabelConfig,
    LightGBMBaseline,
    LightGBMModelConfig,
    LogisticRegressionBaseline,
    MLModelConfig,
    TrainingDataset,
    assert_no_future_leakage,
    brier_score,
    chronological_split,
    expected_calibration_error,
)
from trader_jev.models import (
    DecisionSnapshot,
    Direction,
    DomainModel,
    InstrumentMetadata,
    Market,
    PredictionOutput,
    TradingSession,
)


class TrainingRunSummary(DomainModel):
    """Secret-free report emitted after one offline ML training run."""

    model_type: str
    model_version: str
    source: str
    snapshots: int = Field(ge=0)
    examples: int = Field(ge=0)
    train_examples: int = Field(ge=0)
    test_examples: int = Field(ge=0)
    trained_until: datetime
    artifact_path: str
    metrics: dict[str, float]


async def run_training(args: argparse.Namespace) -> TrainingRunSummary:
    if args.start >= args.end:
        raise ValueError("--start must be earlier than --end")

    instrument = _build_instrument(args)
    if args.source == "jquants":
        adapter: MarketDataAdapter = JQuantsMinuteBarAdapter.from_env(
            load_env_file(args.env_file),
            start=args.start,
            end=args.end,
        )
        source_name = f"jquants:{instrument.symbol}"
    else:
        if args.data is None:
            raise ValueError("--data is required when --source=file")
        adapter = FileMarketDataAdapter(
            args.data,
            start=args.start,
            end=args.end,
            skip_corrupt_rows=not args.fail_on_corrupt,
        )
        source_name = str(args.data)
    snapshots = await _snapshots_from_market_data(adapter, instrument)
    if len(snapshots) < 2:
        raise ValueError("training requires at least two point-in-time snapshots")

    dataset = TrainingDataset.from_snapshots(
        snapshots,
        label_config=LabelConfig(
            horizon_seconds=args.horizon_seconds,
            cost_bps=args.cost_bps,
        ),
    )
    if len(dataset.examples) < 2:
        raise ValueError("training requires at least two labeled examples")
    assert_no_future_leakage(dataset.examples)
    train, test = chronological_split(
        dataset.examples,
        ChronologicalSplitConfig(
            test_fraction=args.test_fraction,
            purge_seconds=args.purge_seconds,
            embargo_seconds=args.embargo_seconds,
        ),
    )
    if not train:
        raise ValueError("chronological split produced no training examples")

    model = _build_model(args)
    artifact = model.fit(train)
    predictions: list[PredictionOutput] = []
    labels: list[Direction] = []
    for example in test:
        if example.snapshot is None:
            continue
        predictions.append(model.predict_sync(example.snapshot))
        labels.append(example.label)
    metrics = {}
    if predictions:
        metrics = {
            "brier_score": brier_score(predictions, labels),
            "expected_calibration_error": expected_calibration_error(predictions, labels),
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    model.save_artifact(output)
    return TrainingRunSummary(
        model_type=args.model,
        model_version=model.model_version,
        source=source_name,
        snapshots=len(snapshots),
        examples=len(dataset.examples),
        train_examples=len(train),
        test_examples=len(test),
        trained_until=artifact.trained_until,
        artifact_path=str(output),
        metrics=metrics,
    )


async def _snapshots_from_market_data(
    adapter: MarketDataAdapter,
    instrument: InstrumentMetadata,
) -> tuple[DecisionSnapshot, ...]:
    engine = InMemoryFeatureEngine()
    snapshots: list[DecisionSnapshot] = []
    async for event in adapter.stream((instrument,)):
        engine.update(event)
        snapshots.append(engine.snapshot(instrument, event.received_at))
    return tuple(snapshots)


def _build_model(args: argparse.Namespace) -> LightGBMBaseline | LogisticRegressionBaseline:
    model_version = args.model_version or (
        "lightgbm-baseline-1" if args.model == "lightgbm" else "logistic-baseline-1"
    )
    if args.model == "lightgbm":
        return LightGBMBaseline(
            LightGBMModelConfig(
                model_version=model_version,
                seed=args.seed,
                num_boost_round=args.num_boost_round,
                learning_rate=args.learning_rate,
                num_leaves=args.num_leaves,
                min_data_in_leaf=args.min_data_in_leaf,
            )
        )
    return LogisticRegressionBaseline(
        MLModelConfig(
            model_version=model_version,
            seed=args.seed,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
            l2=args.l2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-train",
        description="Fetch historical data and train a chronological ML artifact for Paper replay.",
    )
    parser.add_argument("--source", choices=("file", "jquants"), default="file")
    parser.add_argument("--data", type=Path, help="CSV, JSONL, or NDJSON market data.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Env file for JQUANTS_API_KEY and JQUANTS_* settings (default: .env).",
    )
    parser.add_argument("--start", required=True, type=_timestamp, help="Replay start (ISO-8601).")
    parser.add_argument("--end", required=True, type=_timestamp, help="Replay end (ISO-8601).")
    parser.add_argument("--symbol", required=True, help="One instrument symbol to train.")
    parser.add_argument(
        "--market", choices=tuple(item.value for item in Market), default=Market.JP.value
    )
    parser.add_argument("--currency")
    parser.add_argument("--timezone")
    parser.add_argument("--tick-size", type=_decimal)
    parser.add_argument("--lot-size", type=_positive_int, default=1)
    parser.add_argument("--model", choices=("lightgbm", "logistic"), default="lightgbm")
    parser.add_argument("--model-version", help="Artifact model version override.")
    parser.add_argument("--output", type=Path, required=True, help="Output model artifact path.")
    parser.add_argument("--horizon-seconds", type=_positive_int, default=300)
    parser.add_argument("--cost-bps", type=_nonnegative_decimal, default=Decimal("0"))
    parser.add_argument("--test-fraction", type=_fraction, default=Decimal("0.2"))
    parser.add_argument("--purge-seconds", type=_nonnegative_int, default=0)
    parser.add_argument("--embargo-seconds", type=_nonnegative_int, default=0)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--learning-rate", type=_positive_float, default=0.05)
    parser.add_argument("--num-boost-round", type=_positive_int, default=100)
    parser.add_argument("--num-leaves", type=_positive_int, default=15)
    parser.add_argument("--min-data-in-leaf", type=_positive_int, default=5)
    parser.add_argument("--epochs", type=_positive_int, default=200)
    parser.add_argument("--l2", type=_nonnegative_float, default=0.001)
    parser.add_argument(
        "--fail-on-corrupt",
        action="store_true",
        help="Stop when a historical input row cannot be normalized.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run_training(args))
        print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
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
        shortability=True,
    )


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


def _nonnegative_decimal(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _fraction(value: str) -> Decimal:
    parsed = _decimal(value)
    if not Decimal("0") < parsed < Decimal("1"):
        raise argparse.ArgumentTypeError("value must be between zero and one")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
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
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


__all__ = ["TrainingRunSummary", "build_parser", "main", "run_training"]
