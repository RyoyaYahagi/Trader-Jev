"""Deterministic ML-only and Jev+ML integration experiments."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from trader_jev.decision import JevDecisionModel, hold_intent
from trader_jev.interfaces import DecisionModel, PredictionModel
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    Direction,
    DomainModel,
    PredictionOutput,
    TradeIntent,
)


class IntegrationMode(StrEnum):
    """The three Jev/ML comparison paths from the research plan."""

    A_JEV_INPUT = "A_JEV_INPUT"
    B_DETERMINISTIC_MERGE = "B_DETERMINISTIC_MERGE"
    C_ML_SCREEN_JEV = "C_ML_SCREEN_JEV"
    A = "A_JEV_INPUT"
    B = "B_DETERMINISTIC_MERGE"
    C = "C_ML_SCREEN_JEV"
    C_SCREEN_ML_JEV = "C_ML_SCREEN_JEV"


class MLDecisionConfig(DomainModel):
    allow_short: bool = True
    flat_action: Action = Action.HOLD


class MLDecisionModel(DecisionModel):
    """Use only the typed ML prediction to create a TradeIntent."""

    def __init__(
        self,
        prediction_model: PredictionModel,
        *,
        config: MLDecisionConfig | None = None,
        strategy_id: str = "ml-only",
    ) -> None:
        self.prediction_model = prediction_model
        self.config = config or MLDecisionConfig()
        self.strategy_id = strategy_id

    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        return await self.prediction_model.predict(snapshot)

    async def decide(self, snapshot: DecisionSnapshot, prediction: Any = None) -> TradeIntent:
        try:
            output = prediction or await self.predict(snapshot)
            if not isinstance(output, PredictionOutput):
                raise TypeError("prediction model must return PredictionOutput")
        except Exception as exc:
            return hold_intent(snapshot, self.strategy_id, "ML_ERROR", str(exc))
        action = self._action(output.direction_5m)
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=action,
            confidence=self._confidence(output),
            strategy_id=self.strategy_id,
            model_version=output.model_version,
            reason="ML prediction" if action is not Action.HOLD else "ML predicted FLAT",
            created_at=snapshot.as_of,
            metadata={
                "integration_mode": "ML_ONLY",
                "direction_5m": output.direction_5m.value,
                "p_up": str(output.p_up) if output.p_up is not None else None,
                "p_flat": str(output.p_flat) if output.p_flat is not None else None,
                "p_down": str(output.p_down) if output.p_down is not None else None,
                "expected_return_bps": (
                    str(output.expected_return_bps)
                    if output.expected_return_bps is not None
                    else None
                ),
                "trained_until": (
                    output.trained_until.isoformat() if output.trained_until is not None else None
                ),
                "feature_schema_version": output.feature_schema_version,
            },
        )

    def _action(self, direction: Direction) -> Action:
        if direction is Direction.UP:
            return Action.LONG
        if direction is Direction.DOWN and self.config.allow_short:
            return Action.SHORT
        return self.config.flat_action

    @staticmethod
    def _confidence(output: PredictionOutput) -> Any:
        probabilities = [
            value for value in (output.p_up, output.p_flat, output.p_down) if value is not None
        ]
        return max(probabilities) if probabilities else None


class JevMLDecisionModel(DecisionModel):
    """Run Jev-only, ML-only, or one of the A/B/C integration experiments."""

    def __init__(
        self,
        jev_model: JevDecisionModel,
        prediction_model: PredictionModel | None = None,
        *,
        mode: IntegrationMode = IntegrationMode.A_JEV_INPUT,
        ml_config: MLDecisionConfig | None = None,
        strategy_id: str = "jev-ml",
    ) -> None:
        self.jev_model = jev_model
        self.prediction_model = prediction_model
        self.mode = mode
        self.ml_model = (
            MLDecisionModel(prediction_model, config=ml_config, strategy_id="ml-only")
            if prediction_model is not None
            else None
        )
        self.strategy_id = strategy_id

    async def decide(self, snapshot: DecisionSnapshot, prediction: Any = None) -> TradeIntent:
        if self.prediction_model is None:
            return await self.jev_model.decide(snapshot, None)
        try:
            ml_prediction = prediction or await self.prediction_model.predict(snapshot)
            if not isinstance(ml_prediction, PredictionOutput):
                raise TypeError("prediction model must return PredictionOutput")
        except Exception as exc:
            return hold_intent(snapshot, self.strategy_id, "ML_ERROR", str(exc))

        if self.mode is IntegrationMode.A_JEV_INPUT:
            intent = await self.jev_model.decide(snapshot, ml_prediction)
            return self._tag(intent, ml_prediction)

        ml_intent = await self.ml_model.decide(snapshot, ml_prediction) if self.ml_model else None
        jev_intent = await self.jev_model.decide(snapshot, None)
        if ml_intent is None:
            return hold_intent(
                snapshot, self.strategy_id, "ML_NOT_LOADED", "ML model is unavailable"
            )
        if self.mode is IntegrationMode.B_DETERMINISTIC_MERGE:
            action = jev_intent.action if jev_intent.action is ml_intent.action else Action.HOLD
            reason = "Jev and ML agree" if action is not Action.HOLD else "Jev and ML disagree"
            return self._merged_intent(snapshot, action, reason, jev_intent, ml_intent)

        if ml_intent.action is Action.HOLD:
            return hold_intent(
                snapshot, self.strategy_id, "ML_SCREEN_REJECTED", "ML screening rejected"
            )
        if jev_intent.action is not ml_intent.action:
            return hold_intent(
                snapshot, self.strategy_id, "JEV_SCREEN_REJECTED", "Jev secondary check disagreed"
            )
        return self._merged_intent(
            snapshot, jev_intent.action, "ML screen and Jev agree", jev_intent, ml_intent
        )

    def _tag(self, intent: TradeIntent, prediction: PredictionOutput) -> TradeIntent:
        return intent.model_copy(
            update={
                "strategy_id": self.strategy_id,
                "metadata": {
                    **dict(intent.metadata),
                    "integration_mode": self.mode.value,
                    "ml_direction_5m": prediction.direction_5m.value,
                    "ml_expected_return_bps": (
                        str(prediction.expected_return_bps)
                        if prediction.expected_return_bps is not None
                        else None
                    ),
                },
            }
        )

    def _merged_intent(
        self,
        snapshot: DecisionSnapshot,
        action: Action,
        reason: str,
        jev_intent: TradeIntent,
        ml_intent: TradeIntent,
    ) -> TradeIntent:
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=action,
            confidence=ml_intent.confidence,
            strategy_id=self.strategy_id,
            model_version=f"{jev_intent.model_version or 'jev'}+{ml_intent.model_version or 'ml'}",
            reason=reason,
            created_at=snapshot.as_of,
            metadata={
                "integration_mode": self.mode.value,
                "jev_intent_id": str(jev_intent.intent_id),
                "ml_intent_id": str(ml_intent.intent_id),
                "jev_reason": jev_intent.reason,
                "ml_reason": ml_intent.reason,
            },
        )


MLOnlyDecisionModel = MLDecisionModel
JevWithMLDecisionModel = JevMLDecisionModel


__all__ = [
    "IntegrationMode",
    "JevMLDecisionModel",
    "JevWithMLDecisionModel",
    "MLDecisionConfig",
    "MLDecisionModel",
    "MLOnlyDecisionModel",
]
