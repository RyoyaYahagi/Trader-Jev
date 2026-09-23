"""Typed Rule/Jev decision models and the Paper-safe decision adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from trader_jev.clock import SystemClock
from trader_jev.experiments import (
    JevInputProfile,
    JevOutputPolicy,
    evaluate_output_policy,
)
from trader_jev.interfaces import Clock, DecisionModel
from trader_jev.models import (
    Action,
    DecisionSnapshot,
    Direction,
    DomainModel,
    Regime,
    TradeIntent,
)


class JevRequest(DomainModel):
    """Compact, point-in-time request sent to a replaceable Jev client."""

    request_id: UUID = Field(default_factory=uuid4)
    snapshot_id: UUID
    market: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    as_of: datetime
    input_schema_version: str = Field(default="1.0", min_length=1)
    payload: Mapping[str, Any]


class JevDecision(DomainModel):
    """Normalized typed response from Jev."""

    action: Action
    direction_5m: Direction
    regime: Regime = Regime.RANGE
    setup_quality: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))
    p_up: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    p_flat: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    p_down: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    confidence: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))
    top_probability: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    top_two_margin: Decimal | None = None
    news_invalidates_signal: bool = False
    model_version: str = Field(default="jev", min_length=1)
    input_schema_version: str = Field(default="1.0", min_length=1)
    latency_ms: int | None = Field(default=None, ge=0)
    usage: Mapping[str, Any] | None = None

    @model_validator(mode="after")
    def validate_margin(self) -> JevDecision:
        if self.top_two_margin is not None and not Decimal("-1") <= self.top_two_margin <= Decimal(
            "1"
        ):
            raise ValueError("top_two_margin must be between -1 and 1")
        return self


class JevAuditRecord(DomainModel):
    """Request/response metadata retained for later trade explanation."""

    request_id: UUID
    snapshot_id: UUID
    sent_at: datetime
    received_at: datetime
    success: bool
    error_code: str | None = None
    error_reason: str | None = None
    response: Mapping[str, Any] | None = None


class JevAdapterResult(DomainModel):
    request: JevRequest
    decision: JevDecision | None = None
    audit: JevAuditRecord

    @property
    def ok(self) -> bool:
        return self.decision is not None and self.audit.success


class JevAdapterConfig(DomainModel):
    timeout_seconds: float = Field(default=5.0, gt=0)
    model_version: str = Field(default="jev-paper", min_length=1)
    input_schema_version: str = Field(default="1.0", min_length=1)
    max_payload_bytes: int = Field(default=16_384, gt=0)


class JevClient(Protocol):
    async def decide(self, request: JevRequest) -> JevDecision | Mapping[str, Any] | str:
        """Return a Jev response without exposing transport types to core."""
        ...


ClientCallable = Callable[[JevRequest], Awaitable[JevDecision | Mapping[str, Any] | str]]


class JevDecisionAdapter:
    """Adapt a mock/local Jev client into a typed, auditable response."""

    def __init__(
        self,
        client: JevClient | ClientCallable | object,
        *,
        config: JevAdapterConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config or JevAdapterConfig()
        self._client = client
        self._clock = clock or SystemClock()
        self._audit: list[JevAuditRecord] = []

    @property
    def audit_records(self) -> tuple[JevAuditRecord, ...]:
        return tuple(self._audit)

    async def decide(self, request: JevRequest) -> JevAdapterResult:
        sent_at = self._clock.now()
        try:
            payload_size = len(request.model_dump_json().encode("utf-8"))
            if payload_size > self.config.max_payload_bytes:
                return self._failure(
                    request, sent_at, "PAYLOAD_TOO_LARGE", "Jev payload exceeds limit"
                )
            raw = await asyncio.wait_for(self._invoke(request), self.config.timeout_seconds)
            decision = self._normalize(raw, request)
            received_at = self._clock.now()
            audit = JevAuditRecord(
                request_id=request.request_id,
                snapshot_id=request.snapshot_id,
                sent_at=sent_at,
                received_at=received_at,
                success=True,
                response=self._safe_response(raw),
            )
            self._audit.append(audit)
            return JevAdapterResult(request=request, decision=decision, audit=audit)
        except TimeoutError:
            return self._failure(request, sent_at, "JEV_TIMEOUT", "Jev response timed out")
        except Exception as exc:
            return self._failure(request, sent_at, "JEV_MALFORMED_RESPONSE", str(exc))

    async def _invoke(self, request: JevRequest) -> JevDecision | Mapping[str, Any] | str:
        client = self._client
        method = getattr(client, "decide", None)
        if method is None:
            method = getattr(client, "complete", None)
        if method is None and callable(client):
            method = client
        if method is None:
            raise TypeError("Jev client must expose decide(), complete(), or be callable")
        result: Any = method(request)
        if inspect.isawaitable(result):
            awaited: Any = await result
            return cast(JevDecision | Mapping[str, Any] | str, awaited)
        return cast(JevDecision | Mapping[str, Any] | str, result)

    def _normalize(
        self,
        raw: JevDecision | Mapping[str, Any] | str,
        request: JevRequest,
    ) -> JevDecision:
        if isinstance(raw, JevDecision):
            return raw
        if isinstance(raw, str):
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                raise TypeError("Jev JSON response must be an object")
            parsed_mapping = cast(Mapping[object, Any], parsed)
            data: dict[str, Any] = {str(key): value for key, value in parsed_mapping.items()}
        else:
            data = {str(key): value for key, value in raw.items()}
        nested = data.get("decision")
        if isinstance(nested, Mapping):
            nested_mapping = cast(Mapping[object, Any], nested)
            data = {str(key): value for key, value in nested_mapping.items()}
        probabilities = data.get("probabilities")
        if isinstance(probabilities, Mapping):
            for name in ("up", "flat", "down"):
                if f"p_{name}" not in data and name in probabilities:
                    data[f"p_{name}"] = probabilities[name]
            data.pop("probabilities", None)
        data.setdefault("regime", Regime.RANGE.value)
        data.setdefault("setup_quality", data.get("confidence", 0))
        data.setdefault("model_version", self.config.model_version)
        data.setdefault("input_schema_version", request.input_schema_version)
        probability_values = [
            Decimal(str(data[name]))
            for name in ("p_up", "p_flat", "p_down")
            if data.get(name) is not None
        ]
        if probability_values:
            data.setdefault("top_probability", max(probability_values))
            data.setdefault(
                "confidence",
                max(probability_values),
            )
            if len(probability_values) >= 2:
                ordered = sorted(probability_values, reverse=True)
                data.setdefault("top_two_margin", ordered[0] - ordered[1])
        return JevDecision.model_validate(data)

    def _failure(
        self,
        request: JevRequest,
        sent_at: datetime,
        code: str,
        reason: str,
    ) -> JevAdapterResult:
        received_at = self._clock.now()
        audit = JevAuditRecord(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            sent_at=sent_at,
            received_at=received_at,
            success=False,
            error_code=code,
            error_reason=reason,
        )
        self._audit.append(audit)
        return JevAdapterResult(request=request, audit=audit)

    @staticmethod
    def _safe_response(raw: JevDecision | Mapping[str, Any] | str) -> Mapping[str, Any] | None:
        if isinstance(raw, JevDecision):
            return raw.model_dump(mode="json")
        if isinstance(raw, str):
            return {"raw": raw[:2_048]}
        return {str(key): value for key, value in raw.items()}


class JevDecisionModel(DecisionModel):
    """Turn adapter output into a non-executable TradeIntent."""

    def __init__(
        self,
        adapter: JevDecisionAdapter,
        *,
        strategy_id: str = "jev-only",
        input_profile: JevInputProfile | None = None,
        output_policy: JevOutputPolicy | None = None,
    ) -> None:
        self.adapter = adapter
        self.strategy_id = strategy_id
        self.input_profile = input_profile
        self.output_policy = output_policy

    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: Any = None,
    ) -> TradeIntent:
        request = build_jev_request(
            snapshot,
            prediction=prediction,
            input_schema_version=self.adapter.config.input_schema_version,
            input_profile=self.input_profile,
        )
        try:
            result = await self.adapter.decide(request)
        except Exception as exc:
            return hold_intent(snapshot, self.strategy_id, "JEV_ADAPTER_ERROR", str(exc))
        if not result.ok or result.decision is None:
            return hold_intent(
                snapshot,
                self.strategy_id,
                result.audit.error_code or "JEV_ERROR",
                result.audit.error_reason or "Jev did not return a decision",
                metadata={"request_id": str(request.request_id)},
            )
        decision = result.decision
        if self.output_policy is None:
            action = Action.HOLD if decision.news_invalidates_signal else decision.action
            if decision.news_invalidates_signal:
                reason = "news invalidated signal"
            else:
                reason = "Jev decision"
            policy_metadata: dict[str, Any] = {}
        else:
            evaluation = evaluate_output_policy(decision, decision.action, self.output_policy)
            action = evaluation.action
            reason = evaluation.reason
            policy_metadata = {
                "output_policy": self.output_policy.model_dump(mode="json"),
                "output_policy_accepted": evaluation.accepted,
                "output_policy_reason_code": evaluation.reason_code,
            }
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=action,
            confidence=decision.confidence,
            strategy_id=self.strategy_id,
            model_version=decision.model_version,
            reason=reason,
            created_at=snapshot.as_of,
            metadata={
                "request_id": str(request.request_id),
                "input_profile": (
                    self.input_profile.value if self.input_profile is not None else None
                ),
                "direction_5m": decision.direction_5m.value,
                "regime": decision.regime.value,
                "setup_quality": str(decision.setup_quality),
                "p_up": str(decision.p_up) if decision.p_up is not None else None,
                "p_flat": str(decision.p_flat) if decision.p_flat is not None else None,
                "p_down": str(decision.p_down) if decision.p_down is not None else None,
                "top_two_margin": (
                    str(decision.top_two_margin) if decision.top_two_margin is not None else None
                ),
                "input_schema_version": decision.input_schema_version,
                "latency_ms": decision.latency_ms,
                "jev_audit": result.audit.model_dump(mode="json"),
                **policy_metadata,
            },
        )


class RuleConfig(DomainModel):
    """Explicit Rule baseline parameters; no hidden confidence threshold."""

    momentum_key: str = "return_30s"
    long_threshold: Decimal = Decimal("0")
    short_threshold: Decimal = Decimal("0")
    confidence_scale: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    allow_short: bool = True


class RuleDecisionModel(DecisionModel):
    """Small deterministic baseline used for fair Rule vs Jev comparisons."""

    def __init__(self, config: RuleConfig | None = None, *, strategy_id: str = "rule-only") -> None:
        self.config = config or RuleConfig()
        self.strategy_id = strategy_id

    async def decide(self, snapshot: DecisionSnapshot, prediction: Any = None) -> TradeIntent:
        del prediction
        momentum = Decimal(str(snapshot.technical.get(self.config.momentum_key, 0.0)))
        if momentum > self.config.long_threshold:
            action = Action.LONG
            direction = Direction.UP
        elif self.config.allow_short and momentum < self.config.short_threshold:
            action = Action.SHORT
            direction = Direction.DOWN
        else:
            action = Action.HOLD
            direction = Direction.FLAT
        confidence = min(Decimal("1"), abs(momentum) * self.config.confidence_scale)
        return TradeIntent(
            snapshot_id=snapshot.snapshot_id,
            instrument=snapshot.instrument,
            action=action,
            confidence=confidence,
            strategy_id=self.strategy_id,
            model_version="rule-1",
            reason="rule momentum signal" if action is not Action.HOLD else "rule has no signal",
            created_at=snapshot.as_of,
            metadata={"direction_5m": direction.value, "momentum_key": self.config.momentum_key},
        )


class DecisionCadenceConfig(DomainModel):
    interval_seconds: int = Field(default=15, gt=0)


class SingleFlightDecisionRunner:
    """Prevent overlapping requests for the same symbol."""

    def __init__(self, decision_model: DecisionModel) -> None:
        self._decision_model = decision_model
        self._locks: dict[str, asyncio.Lock] = {}

    async def decide(self, snapshot: DecisionSnapshot) -> TradeIntent:
        key = f"{snapshot.instrument.market.value}:{snapshot.instrument.symbol}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return hold_intent(
                snapshot, "single-flight", "DECISION_IN_FLIGHT", "previous request pending"
            )
        async with lock:
            return await self._decision_model.decide(snapshot)


def build_jev_request(
    snapshot: DecisionSnapshot,
    *,
    prediction: Any = None,
    input_schema_version: str = "1.0",
    input_profile: JevInputProfile | None = None,
) -> JevRequest:
    """Build the compact Jev payload from one immutable snapshot."""

    sections: dict[str, Any] = {
        "technical": dict(snapshot.technical),
        "orderbook": dict(snapshot.orderbook),
        "orderflow": dict(snapshot.orderflow),
        "supply_demand": dict(snapshot.supply_demand),
        "short_history_summary": dict(snapshot.short_history_summary),
        "news": dict(snapshot.news),
        "ml": dict(snapshot.ml),
        "portfolio": dict(snapshot.portfolio),
        "data_quality": snapshot.data_quality.model_dump(mode="json"),
    }
    legacy_sections = (
        "technical",
        "orderbook",
        "orderflow",
        "supply_demand",
        "short_history_summary",
        "portfolio",
        "data_quality",
    )
    selected_sections = legacy_sections if input_profile is None else input_profile.payload_sections
    payload: dict[str, Any] = {
        "event_time": snapshot.event_time.isoformat(),
        "as_of": snapshot.as_of.isoformat(),
        **{name: sections[name] for name in selected_sections},
    }
    if input_profile is not None:
        payload["input_profile"] = input_profile.value
    if prediction is not None:
        payload["prediction"] = (
            prediction.model_dump(mode="json") if hasattr(prediction, "model_dump") else prediction
        )
    return JevRequest(
        snapshot_id=snapshot.snapshot_id,
        market=snapshot.instrument.market.value,
        symbol=snapshot.instrument.symbol,
        as_of=snapshot.as_of,
        input_schema_version=input_schema_version,
        payload=payload,
    )


def hold_intent(
    snapshot: DecisionSnapshot,
    strategy_id: str,
    code: str,
    reason: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> TradeIntent:
    return TradeIntent(
        snapshot_id=snapshot.snapshot_id,
        instrument=snapshot.instrument,
        action=Action.HOLD,
        strategy_id=strategy_id,
        model_version="fail-closed",
        reason=f"{code}: {reason}",
        created_at=snapshot.as_of,
        metadata={"failure_code": code, **(dict(metadata) if metadata else {})},
    )


__all__ = [
    "DecisionCadenceConfig",
    "JevAdapterConfig",
    "JevAdapterResult",
    "JevAuditRecord",
    "JevClient",
    "JevDecision",
    "JevDecisionAdapter",
    "JevDecisionModel",
    "JevRequest",
    "RuleConfig",
    "RuleDecisionModel",
    "SingleFlightDecisionRunner",
    "build_jev_request",
    "hold_intent",
]
