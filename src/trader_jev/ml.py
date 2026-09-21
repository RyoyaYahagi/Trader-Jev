"""Deterministic baseline ML models and chronological validation utilities."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from trader_jev.interfaces import PredictionModel
from trader_jev.models import DecisionSnapshot, Direction, DomainModel, PredictionOutput

CLASS_ORDER: tuple[Direction, ...] = (Direction.UP, Direction.FLAT, Direction.DOWN)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _aware(value)


class LabelConfig(DomainModel):
    horizon_seconds: int = Field(default=300, gt=0)
    cost_bps: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


class TrainingExample(DomainModel):
    """One leakage-checked example for a baseline model."""

    snapshot: DecisionSnapshot | None = None
    features: dict[str, float] = Field(default_factory=dict)
    label: Direction
    realized_return_bps: Decimal
    as_of: datetime
    label_available_at: datetime | None = None

    _time_aware = field_validator("as_of", "label_available_at")(_aware_or_none)

    @model_validator(mode="after")
    def validate_source(self) -> TrainingExample:
        if self.snapshot is None and not self.features:
            raise ValueError("training example requires snapshot or features")
        if self.label_available_at is not None and self.label_available_at <= self.as_of:
            raise ValueError("label_available_at must be after feature as_of")
        return self


class TrainingDataset(DomainModel):
    examples: tuple[TrainingExample, ...]
    label_config: LabelConfig = Field(default_factory=LabelConfig)
    trained_until: datetime | None = None

    _time_aware = field_validator("trained_until")(_aware_or_none)

    @classmethod
    def from_snapshots(
        cls,
        snapshots: Sequence[DecisionSnapshot],
        *,
        label_config: LabelConfig | None = None,
    ) -> TrainingDataset:
        config = label_config or LabelConfig()
        ordered = sorted(snapshots, key=lambda snapshot: snapshot.as_of)
        examples: list[TrainingExample] = []
        horizon = timedelta(seconds=config.horizon_seconds)
        for index, snapshot in enumerate(ordered):
            target_time = snapshot.as_of + horizon
            future = next(
                (
                    candidate
                    for candidate in ordered[index + 1 :]
                    if candidate.instrument == snapshot.instrument
                    and candidate.as_of >= target_time
                ),
                None,
            )
            if future is None or snapshot.market.mid == 0:
                continue
            realized = (
                (future.market.mid - snapshot.market.mid) / snapshot.market.mid * Decimal("10000")
            )
            if realized > config.cost_bps:
                label = Direction.UP
            elif realized < -config.cost_bps:
                label = Direction.DOWN
            else:
                label = Direction.FLAT
            examples.append(
                TrainingExample(
                    snapshot=snapshot,
                    label=label,
                    realized_return_bps=realized,
                    as_of=snapshot.as_of,
                    label_available_at=future.as_of,
                )
            )
        label_times = [
            example.label_available_at
            for example in examples
            if example.label_available_at is not None
        ]
        trained_until = max(label_times) if label_times else None
        return cls(examples=tuple(examples), label_config=config, trained_until=trained_until)


class FeatureVectorizer:
    """Stable, versioned flattening of compact snapshot feature maps."""

    feature_names: tuple[str, ...] = (
        "technical.return_5s",
        "technical.return_30s",
        "technical.return_1m",
        "technical.return_5m",
        "technical.vwap_distance",
        "technical.ema_slope",
        "technical.rsi",
        "technical.macd_histogram",
        "technical.atr",
        "technical.realized_volatility",
        "orderbook.spread_bps",
        "orderbook.imbalance",
        "orderbook.imbalance_slope",
        "orderbook.depth_within_bps",
        "orderflow.cvd",
        "orderflow.cvd_trend",
        "orderflow.relative_volume",
        "orderflow.turnover_acceleration",
        "supply_demand.net_pressure",
        "supply_demand.buy_ratio",
    )

    def __init__(self, feature_names: Sequence[str] | None = None) -> None:
        self.feature_names = tuple(feature_names or self.feature_names)

    @property
    def schema_version(self) -> str:
        return "1.0"

    def vectorize(self, snapshot: DecisionSnapshot) -> tuple[float, ...]:
        maps: dict[str, Any] = {
            "technical": snapshot.technical,
            "orderbook": snapshot.orderbook,
            "orderflow": snapshot.orderflow,
            "supply_demand": snapshot.supply_demand,
        }
        return tuple(
            self._safe_float(maps.get(group, {}).get(name, 0.0))
            for group, name in (feature.split(".", 1) for feature in self.feature_names)
        )

    def vectorize_example(self, example: TrainingExample) -> tuple[float, ...]:
        if example.snapshot is not None:
            return self.vectorize(example.snapshot)
        return tuple(
            self._safe_float(example.features.get(name, 0.0)) for name in self.feature_names
        )

    @staticmethod
    def _safe_float(value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0


class MLModelConfig(DomainModel):
    model_version: str = Field(default="logistic-baseline-1", min_length=1)
    feature_schema_version: str = Field(default="1.0", min_length=1)
    seed: int = 17
    learning_rate: float = Field(default=0.05, gt=0)
    epochs: int = Field(default=200, gt=0)
    l2: float = Field(default=0.001, ge=0)


class ModelArtifact(DomainModel):
    model_version: str
    trained_until: datetime
    feature_schema_version: str
    feature_names: tuple[str, ...]
    class_order: tuple[Direction, ...] = CLASS_ORDER
    weights: tuple[tuple[float, ...], ...]
    biases: tuple[float, ...]
    class_returns_bps: tuple[float, ...]
    config_hash: str
    seed: int

    _time_aware = field_validator("trained_until")(_aware)


class LogisticRegressionBaseline(PredictionModel):
    """Small dependency-free multinomial logistic regression baseline."""

    def __init__(
        self,
        config: MLModelConfig | None = None,
        *,
        vectorizer: FeatureVectorizer | None = None,
    ) -> None:
        self.config = config or MLModelConfig()
        self.vectorizer = vectorizer or FeatureVectorizer()
        self.artifact: ModelArtifact | None = None

    @property
    def model_version(self) -> str:
        return self.config.model_version

    @property
    def trained_until(self) -> datetime | None:
        return self.artifact.trained_until if self.artifact is not None else None

    def fit(
        self,
        dataset: TrainingDataset | Sequence[TrainingExample],
        *,
        trained_until: datetime | None = None,
    ) -> ModelArtifact:
        examples = tuple(dataset.examples if isinstance(dataset, TrainingDataset) else dataset)
        if not examples:
            raise ValueError("cannot fit ML model without examples")
        deadline = trained_until or (
            dataset.trained_until if isinstance(dataset, TrainingDataset) else None
        )
        if deadline is None:
            deadline = max(example.label_available_at or example.as_of for example in examples)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("trained_until must be timezone-aware")
        features = [self.vectorizer.vectorize_example(example) for example in examples]
        labels = [CLASS_ORDER.index(example.label) for example in examples]
        returns = [float(example.realized_return_bps) for example in examples]
        class_returns = tuple(
            self._mean(
                [value for value, label in zip(returns, labels, strict=True) if label == index]
            )
            for index in range(len(CLASS_ORDER))
        )
        weights, biases = self._train(features, labels)
        config_payload = {
            "model": self.config.model_dump(mode="json"),
            "feature_names": self.vectorizer.feature_names,
            "class_order": [direction.value for direction in CLASS_ORDER],
        }
        config_hash = hashlib.sha256(
            json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.artifact = ModelArtifact(
            model_version=self.config.model_version,
            trained_until=deadline,
            feature_schema_version=self.config.feature_schema_version,
            feature_names=self.vectorizer.feature_names,
            weights=tuple(tuple(row) for row in weights),
            biases=tuple(biases),
            class_returns_bps=class_returns,
            config_hash=config_hash,
            seed=self.config.seed,
        )
        return self.artifact

    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        return self.predict_sync(snapshot)

    def predict_sync(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        if self.artifact is None:
            raise RuntimeError("ML model is not fitted")
        values = self.vectorizer.vectorize(snapshot)
        logits = [
            bias + sum(weight * value for weight, value in zip(row, values, strict=True))
            for row, bias in zip(self.artifact.weights, self.artifact.biases, strict=True)
        ]
        probabilities = self._softmax(logits)
        best = max(range(len(probabilities)), key=lambda index: probabilities[index])
        expected = sum(
            probability * value
            for probability, value in zip(
                probabilities, self.artifact.class_returns_bps, strict=True
            )
        )
        top_two = sorted(probabilities, reverse=True)
        return PredictionOutput(
            direction_5m=CLASS_ORDER[best],
            p_up=Decimal(str(probabilities[0])),
            p_flat=Decimal(str(probabilities[1])),
            p_down=Decimal(str(probabilities[2])),
            expected_return_bps=Decimal(str(expected)),
            model_version=self.artifact.model_version,
            trained_until=self.artifact.trained_until,
            uncertainty=Decimal(str(1.0 - probabilities[best])),
            feature_schema_version=self.artifact.feature_schema_version,
            calibration_metadata={
                "config_hash": self.artifact.config_hash,
                "top_two_margin": top_two[0] - top_two[1],
            },
        )

    def save_artifact(self, path: str | Path) -> Path:
        if self.artifact is None:
            raise RuntimeError("ML model is not fitted")
        destination = Path(path)
        destination.write_text(self.artifact.model_dump_json(indent=2), encoding="utf-8")
        return destination

    @classmethod
    def load_artifact(cls, path: str | Path) -> Self:
        artifact = ModelArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))
        model = cls(
            MLModelConfig(
                model_version=artifact.model_version,
                feature_schema_version=artifact.feature_schema_version,
                seed=artifact.seed,
            ),
            vectorizer=FeatureVectorizer(artifact.feature_names),
        )
        model.artifact = artifact
        return model

    def _train(
        self, features: list[tuple[float, ...]], labels: list[int]
    ) -> tuple[list[list[float]], list[float]]:
        dimension = len(self.vectorizer.feature_names)
        classes = len(CLASS_ORDER)
        weights = [[0.0] * dimension for _ in range(classes)]
        counts = [labels.count(index) for index in range(classes)]
        total = len(labels)
        biases = [math.log((count + 1) / (total + classes)) for count in counts]
        for _ in range(self.config.epochs):
            gradient_w = [[0.0] * dimension for _ in range(classes)]
            gradient_b = [0.0] * classes
            for vector, target in zip(features, labels, strict=True):
                probabilities = self._softmax(
                    [
                        bias
                        + sum(weight * value for weight, value in zip(row, vector, strict=True))
                        for row, bias in zip(weights, biases, strict=True)
                    ]
                )
                for class_index in range(classes):
                    error = probabilities[class_index] - (1.0 if target == class_index else 0.0)
                    gradient_b[class_index] += error
                    for feature_index, value in enumerate(vector):
                        gradient_w[class_index][feature_index] += error * value
            for class_index in range(classes):
                biases[class_index] -= self.config.learning_rate * gradient_b[class_index] / total
                for feature_index in range(dimension):
                    gradient = gradient_w[class_index][feature_index] / total
                    weights[class_index][feature_index] -= self.config.learning_rate * (
                        gradient + self.config.l2 * weights[class_index][feature_index]
                    )
        return weights, biases

    @staticmethod
    def _softmax(values: Sequence[float]) -> list[float]:
        maximum = max(values)
        exponentials = [math.exp(value - maximum) for value in values]
        denominator = sum(exponentials)
        return [value / denominator for value in exponentials]

    @staticmethod
    def _mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0


class ChronologicalSplitConfig(DomainModel):
    test_fraction: Decimal = Field(default=Decimal("0.2"), gt=Decimal("0"), lt=Decimal("1"))
    purge_seconds: int = Field(default=0, ge=0)
    embargo_seconds: int = Field(default=0, ge=0)


def chronological_split(
    examples: Sequence[TrainingExample],
    config: ChronologicalSplitConfig | None = None,
) -> tuple[tuple[TrainingExample, ...], tuple[TrainingExample, ...]]:
    """Split by time and remove purge/embargo observations around the boundary."""

    settings = config or ChronologicalSplitConfig()
    ordered = tuple(sorted(examples, key=lambda example: example.as_of))
    split = max(1, min(len(ordered) - 1, int(len(ordered) * (1 - float(settings.test_fraction)))))
    train = ordered[:split]
    boundary = ordered[split].as_of
    blocked_until = boundary + timedelta(seconds=settings.purge_seconds + settings.embargo_seconds)
    test = tuple(example for example in ordered[split:] if example.as_of >= blocked_until)
    return train, test


def walk_forward_splits(
    examples: Sequence[TrainingExample],
    *,
    min_train_size: int,
    test_size: int,
    step: int | None = None,
) -> tuple[tuple[tuple[TrainingExample, ...], tuple[TrainingExample, ...]], ...]:
    """Return deterministic expanding-window walk-forward folds."""

    if min_train_size <= 0 or test_size <= 0:
        raise ValueError("min_train_size and test_size must be positive")
    ordered = tuple(sorted(examples, key=lambda example: example.as_of))
    increment = step or test_size
    folds: list[tuple[tuple[TrainingExample, ...], tuple[TrainingExample, ...]]] = []
    start = min_train_size
    while start < len(ordered):
        folds.append((ordered[:start], ordered[start : start + test_size]))
        start += increment
    return tuple(fold for fold in folds if fold[1])


def brier_score(predictions: Sequence[PredictionOutput], labels: Sequence[Direction]) -> float:
    if len(predictions) != len(labels) or not predictions:
        raise ValueError("predictions and labels must be non-empty and equal length")
    score = 0.0
    for prediction, label in zip(predictions, labels, strict=True):
        values = {
            Direction.UP: float(prediction.p_up or 0),
            Direction.FLAT: float(prediction.p_flat or 0),
            Direction.DOWN: float(prediction.p_down or 0),
        }
        score += sum(
            (values[direction] - (1.0 if direction is label else 0.0)) ** 2
            for direction in CLASS_ORDER
        )
    return score / len(predictions)


def expected_calibration_error(
    predictions: Sequence[PredictionOutput],
    labels: Sequence[Direction],
    *,
    bins: int = 10,
) -> float:
    if len(predictions) != len(labels) or not predictions:
        raise ValueError("predictions and labels must be non-empty and equal length")
    if bins <= 0:
        raise ValueError("bins must be positive")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for prediction, label in zip(predictions, labels, strict=True):
        probabilities = [
            float(prediction.p_up or 0),
            float(prediction.p_flat or 0),
            float(prediction.p_down or 0),
        ]
        confidence = max(probabilities)
        predicted = CLASS_ORDER[probabilities.index(confidence)]
        index = min(bins - 1, int(confidence * bins))
        buckets[index].append((confidence, predicted is label))
    return sum(
        len(bucket)
        / len(predictions)
        * abs(
            sum(confidence for confidence, _ in bucket) / len(bucket)
            - sum(correct for _, correct in bucket) / len(bucket)
        )
        for bucket in buckets
        if bucket
    )


def assert_no_future_leakage(examples: Iterable[TrainingExample]) -> None:
    for example in examples:
        if example.label_available_at is not None and example.label_available_at <= example.as_of:
            raise ValueError("training example exposes a label before its feature timestamp")


BaselineMLModel = LogisticRegressionBaseline
MLPredictionModel = LogisticRegressionBaseline


__all__ = [
    "BaselineMLModel",
    "CLASS_ORDER",
    "ChronologicalSplitConfig",
    "FeatureVectorizer",
    "LabelConfig",
    "LogisticRegressionBaseline",
    "MLModelConfig",
    "MLPredictionModel",
    "ModelArtifact",
    "TrainingDataset",
    "TrainingExample",
    "assert_no_future_leakage",
    "brier_score",
    "chronological_split",
    "expected_calibration_error",
    "walk_forward_splits",
]
