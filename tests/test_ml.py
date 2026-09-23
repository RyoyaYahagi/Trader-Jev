from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from trader_jev.clock import FixedClock
from trader_jev.decision import JevDecisionAdapter, JevDecisionModel
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.integration import (
    IntegrationMode,
    JevMLDecisionModel,
    MLDecisionModel,
)
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
    walk_forward_splits,
)
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    Direction,
    PredictionOutput,
    QuoteEvent,
    TradeIntent,
)

from .conftest import NOW


def make_snapshots(quote: QuoteEvent) -> tuple[DecisionSnapshot, ...]:
    engine = InMemoryFeatureEngine()
    snapshots: list[DecisionSnapshot] = []
    for index in range(8):
        current = NOW + timedelta(seconds=index * 5)
        price = Decimal(100 + index)
        event = quote.model_copy(
            update={
                "event_id": uuid4(),
                "event_time": current,
                "received_at": current,
                "bid": price,
                "ask": price + Decimal("1"),
            }
        )
        engine.update(event)
        snapshots.append(engine.snapshot(quote.instrument, current))
    return tuple(snapshots)


def test_ml_dataset_training_artifact_and_metrics(quote: QuoteEvent, tmp_path: Path) -> None:
    snapshots = make_snapshots(quote)
    dataset = TrainingDataset.from_snapshots(
        snapshots,
        label_config=LabelConfig(horizon_seconds=5, cost_bps=Decimal("1")),
    )
    assert dataset.examples
    assert_no_future_leakage(dataset.examples)
    train, test = chronological_split(
        dataset.examples,
        ChronologicalSplitConfig(test_fraction=Decimal("0.25"), purge_seconds=0),
    )
    assert train[-1].as_of < test[0].as_of
    assert walk_forward_splits(dataset.examples, min_train_size=2, test_size=2)

    model = LogisticRegressionBaseline(MLModelConfig(epochs=20, model_version="test-ml"))
    artifact = model.fit(dataset)
    prediction = asyncio.run(model.predict(snapshots[-1]))
    assert prediction.model_version == "test-ml"
    assert prediction.trained_until == artifact.trained_until
    assert (
        prediction.p_up is not None
        and prediction.p_flat is not None
        and prediction.p_down is not None
    )
    assert abs(prediction.p_up + prediction.p_flat + prediction.p_down - Decimal("1")) < Decimal(
        "0.000001"
    )

    path = model.save_artifact(tmp_path / "model.json")
    loaded = LogisticRegressionBaseline.load_artifact(path)
    assert asyncio.run(loaded.predict(snapshots[-1])) == prediction

    predictions = [prediction, prediction]
    labels = [prediction.direction_5m, Direction.FLAT]
    assert brier_score(predictions, labels) >= 0
    assert expected_calibration_error(predictions, labels) >= 0

    lightgbm = LightGBMBaseline(
        LightGBMModelConfig(num_boost_round=10, min_data_in_leaf=1, model_version="test-lightgbm")
    )
    lightgbm_artifact = lightgbm.fit(dataset)
    lightgbm_prediction = asyncio.run(lightgbm.predict(snapshots[-1]))
    assert lightgbm_prediction.model_version == "test-lightgbm"
    assert lightgbm_prediction.trained_until == lightgbm_artifact.trained_until
    assert (
        lightgbm_prediction.p_up is not None
        and lightgbm_prediction.p_flat is not None
        and lightgbm_prediction.p_down is not None
    )
    lightgbm_path = lightgbm.save_artifact(tmp_path / "lightgbm-model.json")
    loaded_lightgbm = LightGBMBaseline.load_artifact(lightgbm_path)
    assert asyncio.run(loaded_lightgbm.predict(snapshots[-1])) == lightgbm_prediction


class FakePredictionModel:
    def __init__(self, direction: Direction) -> None:
        self.direction = direction

    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        return PredictionOutput(
            direction_5m=self.direction,
            p_up=Decimal("0.8") if self.direction is Direction.UP else Decimal("0.1"),
            p_flat=Decimal("0.1"),
            p_down=Decimal("0.8") if self.direction is Direction.DOWN else Decimal("0.1"),
            expected_return_bps=Decimal("4"),
            model_version="fake-ml",
            trained_until=snapshot.as_of,
        )


class StaticDecisionClient:
    async def decide(self, request: Any) -> Mapping[str, Any]:
        del request
        return {"action": "LONG", "direction_5m": "UP", "confidence": 0.8}


@pytest.mark.asyncio
async def test_ml_only_and_jev_ml_modes_are_switchable(quote: QuoteEvent) -> None:
    snapshot = make_snapshots(quote)[-1]
    ml_model = FakePredictionModel(Direction.UP)
    ml_intent = await MLDecisionModel(ml_model).decide(snapshot)
    assert ml_intent.action is Action.LONG

    jev = JevDecisionModel(JevDecisionAdapter(StaticDecisionClient(), clock=FixedClock(NOW)))
    for mode in IntegrationMode:
        model = JevMLDecisionModel(jev, ml_model, mode=mode)
        intent = await model.decide(snapshot)
        assert isinstance(intent, TradeIntent)
        assert intent.created_at == snapshot.as_of
        if mode is not IntegrationMode.C_SCREEN_ML_JEV:
            assert intent.action is Action.LONG

    no_ml = JevMLDecisionModel(jev, None)
    jev_only_intent = await no_ml.decide(snapshot)
    assert jev_only_intent.action is Action.LONG


@pytest.mark.asyncio
async def test_integration_disagreement_fails_closed(quote: QuoteEvent) -> None:
    snapshot = make_snapshots(quote)[-1]
    jev = JevDecisionModel(JevDecisionAdapter(StaticDecisionClient(), clock=FixedClock(NOW)))
    model = JevMLDecisionModel(
        jev,
        FakePredictionModel(Direction.DOWN),
        mode=IntegrationMode.B_DETERMINISTIC_MERGE,
    )

    intent = await model.decide(snapshot)

    assert intent.action is Action.HOLD
    assert "disagree" in intent.reason
