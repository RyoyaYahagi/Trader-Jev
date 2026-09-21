"""The Phase 0 decision-to-order orchestration path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from trader_jev.interfaces import (
    BrokerAdapter,
    DecisionModel,
    FeatureEngine,
    MarketDataAdapter,
    PredictionModel,
    RiskEngine,
)
from trader_jev.models import (
    DecisionSnapshot,
    DomainModel,
    InstrumentMetadata,
    MarketEvent,
    OrderEvent,
    PortfolioState,
    PredictionOutput,
    RiskDecision,
    TradeIntent,
)


class PipelineConfig(DomainModel):
    """Timeouts are explicit so a slow model cannot block order safety forever."""

    prediction_timeout_seconds: float = Field(default=5.0, gt=0)
    decision_timeout_seconds: float = Field(default=5.0, gt=0)
    broker_timeout_seconds: float = Field(default=10.0, gt=0)


class PipelineResult(DomainModel):
    """Auditable outcome of processing one market event."""

    event_id: UUID
    snapshot: DecisionSnapshot | None = None
    prediction: PredictionOutput | None = None
    trade_intent: TradeIntent | None = None
    risk_decision: RiskDecision | None = None
    order_event: OrderEvent | None = None
    failure_code: str | None = None
    failure_reason: str | None = None

    @property
    def submitted(self) -> bool:
        return self.order_event is not None


class TradingPipeline:
    """Run MarketData → Feature → Decision → Risk → Broker.

    The broker is reached only after a successful model decision and an approved
    RiskDecision containing an OrderIntent.  Prediction and decision failures are
    intentionally terminal for the event (fail closed).
    """

    def __init__(
        self,
        feature_engine: FeatureEngine,
        decision_model: DecisionModel,
        risk_engine: RiskEngine,
        broker_adapter: BrokerAdapter,
        prediction_model: PredictionModel | None = None,
        config: PipelineConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._feature_engine = feature_engine
        self._decision_model = decision_model
        self._risk_engine = risk_engine
        self._broker_adapter = broker_adapter
        self._prediction_model = prediction_model
        self._config = config or PipelineConfig()
        self._logger = logger or logging.getLogger("trader_jev.pipeline")

    async def process_event(
        self,
        event: MarketEvent,
        portfolio: PortfolioState | None = None,
    ) -> PipelineResult:
        """Process one normalized market event and return an audit record."""

        portfolio_state = portfolio or PortfolioState(
            portfolio_id="default",
            cash=Decimal("0"),
        )

        try:
            self._feature_engine.update(event)
            snapshot = self._feature_engine.snapshot(event.instrument, event.received_at)
        except Exception as exc:
            self._logger.exception("feature_engine_error", extra={"event_id": str(event.event_id)})
            return PipelineResult(
                event_id=event.event_id,
                failure_code="FEATURE_ENGINE_ERROR",
                failure_reason=str(exc),
            )

        prediction: PredictionOutput | None = None
        if self._prediction_model is not None:
            try:
                prediction = await asyncio.wait_for(
                    self._prediction_model.predict(snapshot),
                    timeout=self._config.prediction_timeout_seconds,
                )
            except Exception as exc:
                self._logger.exception(
                    "prediction_model_error",
                    extra={
                        "event_id": str(event.event_id),
                        "snapshot_id": str(snapshot.snapshot_id),
                    },
                )
                return PipelineResult(
                    event_id=event.event_id,
                    snapshot=snapshot,
                    failure_code="PREDICTION_MODEL_ERROR",
                    failure_reason=str(exc),
                )

        try:
            trade_intent = await asyncio.wait_for(
                self._decision_model.decide(snapshot, prediction),
                timeout=self._config.decision_timeout_seconds,
            )
        except Exception as exc:
            self._logger.exception(
                "decision_model_error",
                extra={
                    "event_id": str(event.event_id),
                    "snapshot_id": str(snapshot.snapshot_id),
                },
            )
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                failure_code="DECISION_MODEL_ERROR",
                failure_reason=str(exc),
            )

        if trade_intent.snapshot_id != snapshot.snapshot_id:
            self._logger.error(
                "decision_snapshot_mismatch",
                extra={
                    "event_id": str(event.event_id),
                    "snapshot_id": str(snapshot.snapshot_id),
                    "trade_snapshot_id": str(trade_intent.snapshot_id),
                },
            )
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                trade_intent=trade_intent,
                failure_code="DECISION_SNAPSHOT_MISMATCH",
                failure_reason="decision model returned an intent for another snapshot",
            )

        try:
            risk_decision = self._risk_engine.evaluate(trade_intent, snapshot, portfolio_state)
        except Exception as exc:
            self._logger.exception(
                "risk_engine_error",
                extra={
                    "event_id": str(event.event_id),
                    "snapshot_id": str(snapshot.snapshot_id),
                    "trade_intent_id": str(trade_intent.intent_id),
                },
            )
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                trade_intent=trade_intent,
                failure_code="RISK_ENGINE_ERROR",
                failure_reason=str(exc),
            )

        if not risk_decision.approved:
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                trade_intent=trade_intent,
                risk_decision=risk_decision,
            )

        if risk_decision.order_intent is None:
            self._logger.error(
                "risk_approved_without_order",
                extra={"trade_intent_id": str(trade_intent.intent_id)},
            )
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                trade_intent=trade_intent,
                risk_decision=risk_decision,
                failure_code="RISK_APPROVAL_INVALID",
                failure_reason="approved risk decision did not contain an OrderIntent",
            )

        try:
            order_event = await asyncio.wait_for(
                self._broker_adapter.submit(risk_decision.order_intent),
                timeout=self._config.broker_timeout_seconds,
            )
        except Exception as exc:
            self._logger.exception(
                "broker_submit_error",
                extra={
                    "event_id": str(event.event_id),
                    "order_intent_id": str(risk_decision.order_intent.order_intent_id),
                },
            )
            return PipelineResult(
                event_id=event.event_id,
                snapshot=snapshot,
                prediction=prediction,
                trade_intent=trade_intent,
                risk_decision=risk_decision,
                failure_code="BROKER_SUBMIT_ERROR",
                failure_reason=str(exc),
            )

        return PipelineResult(
            event_id=event.event_id,
            snapshot=snapshot,
            prediction=prediction,
            trade_intent=trade_intent,
            risk_decision=risk_decision,
            order_event=order_event,
        )

    async def run(
        self,
        market_data: MarketDataAdapter,
        instruments: Sequence[InstrumentMetadata],
        portfolio: PortfolioState | None = None,
    ) -> AsyncIterator[PipelineResult]:
        """Consume any MarketDataAdapter without changing the core pipeline."""

        async for event in market_data.stream(instruments):
            yield await self.process_event(event, portfolio)
